"""Сбор карточки товара и отзывов варианта с Ozon через CDP.

Схема raw-файла (JSON, schema_version 2.0, аддитивно расширяется):
  collector_version, schema_version, collected_at_utc, product_url, reviews_url,
  pid, incomplete, missing_fields[] (полные пути, напр. 'product.rating'),
  target_variant {Длина_см, Ширина_см, Высота_см},
  product {title, rating, rating_source, rating_count, price_text, price_parts, price_parsed,
           price {current, old, bank, confidence, source},
           characteristics, gallery, descriptions, infographics},
   reviews[], reviews_collected, reviews_expected, reviews_sorts,
   reviews_variant_mode {used, clicked, variant_present, variant_count, active,
                         total_count, reason, reviews_matched_variant,
                         reviews_dropped_by_client_filter},
   pages, review_pages, artifacts {dom_snapshot}, source
   (при sorts: у каждого отзыва review_sort — сортировка первого появления,
    review_sorts — список всех сортировок, где отзыв встретился)
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import random as _random
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import List, Optional

from . import VERSION, SCHEMA_VERSION, DEFAULT_CDP_URL
from . import browser as B
from . import cache as K
from . import log
from . import puzzle as _puzzle
from .cdp import Session

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.7922.34 Safari/537.36"

# ---------------------------------------------------------------------------
# Параметры докрутки отзывов (tuning Phase A: скорость без потери данных)
# ---------------------------------------------------------------------------
SCROLL_STEP_PX = 1500           # px за один скролл (speed-тест 2026-08-16: 1500/400/6, missing=0 на 2 товарах; шаг 2000 → пропуск отзывов)
SCROLL_PAUSE_MS = 400           # базовая пауза скролла, мс (speed-тест 2026-08-16: 1500/400/6; было 1600, ускорение ~58%)
SCROLL_PAUSE_JITTER = 0.15      # ±15% случайная вариация паузы (антибот)
MAX_SCROLL_STABLE = 6          # плато без роста → конец загрузки (было 4; A/B: см. PROJECT_NOTES)
MAX_SCROLL_BOTTOM = 3          # финальная докрутка к низу (не менялось)
MAX_TOPUP_STEPS = 40           # шагов top-up в каждой фазе (вверх/вниз)
# «Тихий» ноль отзывов: Ozon разово не отдаёт блок отзывов при навигации на
# карточку (rate-limit/флак), скролл возвращает 0 без исключения, и проход
# считает товар «без отзывов». Такой 0 на ПЕРВОЙ странице прохода — подозрителен:
# reload страницы даёт второй шанс (докрутка повторяется до RETRY_EMPTY_PASSES).
EMPTY_RETRY_PASSES = 2         # reload-попыток при 0 отзывов на стр. 1 (0 — выключено)
EMPTY_RETRY_WAIT_MS = 4000     # пауза после reload перед повторной прокруткой

# Heartbeat-хук сбора (2026-09-01): контроль зависания воркера перенесён в
# ОРКЕСТРАТОР (parallel._await_workers), а не локальный сторож-поток. Воркер
# сообщает о прогрессе через _touch_watchdog(), а worker_main (parallel.py)
# перехватывает это heartbeat-hook'ом и шлёт {"t":"hb"} в pipe родителю.
# Родитель по простою heartbeat-сообщений (> WORKER_STALL_S) terminate'ит
# зависший воркер и перезапускает его (recovery) — единый путь и для
# parallel=1, и для parallel>1 (без костыля со сторожем внутри collect).
_active_heartbeat = None        # hook (callable, throttled), ставит worker_main


def set_heartbeat_hook(fn):
    """Установить функцию-хук прогресса (воркер шлёт heartbeat в pipe)."""
    global _active_heartbeat
    _active_heartbeat = fn


def _touch_watchdog():
    """Отметить прогресс сбора (heartbeat для оркестратора)."""
    if _active_heartbeat is not None:
        try:
            _active_heartbeat()
        except Exception:
            pass


# Порог «полных» variant_count: до 100 отзывов Ozon отдаёт все и без логина,
# собранное должно совпасть точно; выше — без логина видно ~96-100, требуем >=90.
VARIANT_COUNT_FULL_LIMIT = 100
VARIANT_COUNT_MIN_LARGE = 90
# Допуск недобора при vc<=100: стена логина «плавает» (~96-99) и на границе
# vc≈100 может отрезать 1-3 последних карточки (пример: 4483627458, 98/100).
# Зеркалит TOLERANCE_RATIO из scripts/qa_raw.py (UNDER_TOLERANCE).
VARIANT_TOLERANCE_RATIO = 0.03

# Per-sort: стена логина плавает по сортировкам (на одном товаре наблюдали
# published_at_desc=96 и score_asc=89), поэтому абсолютный порог даёт ложный
# брак с разницей в 1 отзыв. Оборванный проход ловим относительно лучшей
# сортировки: < 70% от неё — пассива залипла на полпути (15-30 против 96).
SORT_DEVIATION_RATIO = 0.7

# used=False с этими reason — реальный сбой активации варианта (брак);
# no_widget/no_variant_opt — легитимно (тумблера нет, список и так варианта).
VM_BAD_REASONS = {"click_error", "click_no_effect", "timeout", "disabled"}


def variant_review_problems(raw) -> list:
    """Брак варианта/числа отзывов по правилу «собранное = Этот вариант (N)».

    Возвращает список проблем ([] = ок):
    - reviews_variant_mode.used == False с reason из VM_BAD_REASONS;
    - variant_count <= 100 и собрано меньше с учётом допуска 3%
      (стена логина на границе vc≈100 отрезает ~1-3 карточки; пример:
      4483627458 — собрано 98/100, в DOM без логина доступно 96);
    - явный variant_count=0 при used=True — вариант без отзывов, не брак
      (кейс 5575856356: rating_count=370 принадлежит другим вариантам);
    - variant_count > 100 и собрано < 90 (кап без логина ~96-100);
    - variant_count > 100 и сортировка собрала < 70% от лучшей (каждый
      проход докручивается до стены логина; стена плавает по сортировкам,
      поэтому сравнение относительное, а не по абсолютному порогу).
    Если тумблера варианта нет (variant_count=null) — товар одновариантный,
    все отзывы его: ориентир берётся из карточки товара (rating_count).
    """
    if not isinstance(raw, dict):
        return ["raw_not_dict"]
    vm = raw.get("reviews_variant_mode") or {}
    n = len(raw.get("reviews") or [])
    vc = vm.get("variant_count")
    if isinstance(vc, int) and vc == 0 and vm.get("used"):
        # Явный ноль от виджета варианта: у выбранного варианта отзывов нет.
        # Подставлять rating_count всего товара нельзя — это чужие отзывы
        # других вариантов (ложный short(0/370, cap~100), кейс 5575856356).
        return []
    if not (isinstance(vc, int) and vc > 0):
        # Нет виджета варианта -> ожидаемый объём = число отзывов товара.
        rc = (raw.get("product") or {}).get("rating_count")
        if isinstance(rc, int) and rc > 0:
            vc = rc
    problems = []
    if not vm.get("used"):
        reason = vm.get("reason")
        if reason in VM_BAD_REASONS:
            problems.append(f"filter_not_used(reason={reason})")
    if isinstance(vc, int) and vc > 0:
        if vc <= VARIANT_COUNT_FULL_LIMIT:
            tol = max(1, round(vc * VARIANT_TOLERANCE_RATIO))
            if n < vc - tol:
                problems.append(f"short({n}/{vc})")
        elif n < VARIANT_COUNT_MIN_LARGE:
            problems.append(f"short({n}/{vc}, cap~100)")
        if vc > VARIANT_COUNT_FULL_LIMIT:
            per_sort: dict = {}
            for r in raw.get("reviews") or []:
                if not isinstance(r, dict):
                    continue
                ss = r.get("review_sorts")
                keys = ss if isinstance(ss, list) else [r.get("review_sort")]
                for s in keys:
                    if s:
                        per_sort[s] = per_sort.get(s, 0) + 1
            if len(per_sort) > 1:
                best = max(per_sort.values())
                for s in sorted(per_sort):
                    if per_sort[s] < best * SORT_DEVIATION_RATIO:
                        problems.append(
                            f"short_sort({s}={per_sort[s]}, best={best}, "
                            f"min {int(best * SORT_DEVIATION_RATIO)})")
    return problems

_scroll_rng = _random.Random()
SCROLL_JITTER_SEED: int | None = None


def set_scroll_jitter_seed(seed: int | None):
    """Задать детерминированный seed jitter-пауз (None = случайный при 1-м использовании)."""
    global SCROLL_JITTER_SEED
    SCROLL_JITTER_SEED = int(seed) if seed is not None else None
    if SCROLL_JITTER_SEED is not None:
        _scroll_rng.seed(SCROLL_JITTER_SEED)


def effective_scroll_jitter_seed() -> int:
    """Фактический seed текущего прогона (self-seed при None — для логирования)."""
    if SCROLL_JITTER_SEED is None:
        set_scroll_jitter_seed(_random.randrange(1 << 32))
    return SCROLL_JITTER_SEED


def scroll_pause_ms(base: int = SCROLL_PAUSE_MS,
                    jitter: float = SCROLL_PAUSE_JITTER) -> int:
    """Пауза скролла с равномерным jitter ±jitter% (детерминированный seed)."""
    effective_scroll_jitter_seed()
    if jitter <= 0:
        return int(base)
    low = int(base * (1 - jitter))
    high = int(base * (1 + jitter))
    return _scroll_rng.randint(low, high)

# ---------------------------------------------------------------------------
# Сортировки отзывов (URL-параметр sort=)
# ---------------------------------------------------------------------------
# У Ozon ровно три сортировки в выдаче отзывов:
#   published_at_desc — «новые и полезные» (по умолчанию, параметр не нужен);
#   score_desc        — «сначала высокие оценки»;
#   score_asc         — «сначала низкие оценки».
# Параметр подтверждён на живой странице: ?tab=reviews&sort=score_desc /
# &sort=score_asc меняют выдачу и сохраняются в URL при пагинации.
SORT_PARAMS = {
    "published_at_desc": None,   # без параметра — сортировка по умолчанию
    "score_desc": "score_desc",
    "score_asc": "score_asc",
}
SORT_LABELS = {
    "published_at_desc": "новые и полезные",
    "score_desc": "сначала высокие оценки",
    "score_asc": "сначала низкие оценки",
}


def _sort_url(url: str, sort: str | None) -> str:
    """Добавляет/убирает URL-параметр sort (идемпотентно).

    sort=None — убрать параметр (сортировка «новые и полезные», по умолчанию).
    sort='score_desc'/'score_asc' — установить соответствующий параметр."""
    u = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    if sort:
        q["sort"] = sort
    else:
        q.pop("sort", None)
    return urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(q)))

# ---------------------------------------------------------------------------
# JS-извлечение карточки товара (включая структурированную цену price_parts)
# ---------------------------------------------------------------------------
PRODUCT_JS = r"""
() => {
  const T = document.body.innerText;
  const parse = (v) => {
    const f = parseFloat(String(v).replace(/\s/g, '').replace(',', '.'));
    return isNaN(f) ? null : f;
  };
  const title = () => {
    const h1 = document.querySelector('h1');
    if (h1) return h1.innerText.trim();
    const m = T.match(/(?:\n|^)([^\n]{15,160})\n/);
    return m ? m[1].trim() : null;
  };
  const SCORE_WIDGETS = ["webProductScore", "webSingleProductScore", "webReviewProductScore"];
  const rating = () => {
    for (const wname of SCORE_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      const text = String(w.innerText);
      let m = text.match(/([\d.,]+)\s*\/\s*5/);
      if (m) {
        const v = parseFloat(m[1].replace(',', '.'));
        if (v > 0 && v <= 5) return { value: v, source: wname };
      }
      m = text.match(/([\d.,]+)\s*[•·]/);
      if (m) {
        const v = parseFloat(m[1].replace(',', '.'));
        if (v > 0 && v <= 5) return { value: v, source: wname };
      }
    }
    const m = T.match(/([\d.,]+)\s*\/\s*5\s*зв[её]зд/);
    if (m) {
      const v = parseFloat(m[1].replace(',', '.'));
      if (v > 0 && v <= 5) return { value: v, source: 'fallback_text' };
    }
    return null;
  };
  const ratingCount = () => {
    for (const wname of SCORE_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      const text = String(w.innerText);
      let m = text.match(/([\d\s\u00a0]+)\s*(?:оцен(?:ка|ок|ки))/i);
      if (m) return parse(m[1]);
      m = text.match(/([\d\s\u00a0]+)\s*отзыв/i);
      if (m) return parse(m[1]);
      const nums = [];
      const re = /\d+(?:[\s\u00a0]\d+)*/g;
      let mm;
      while ((mm = re.exec(text)) !== null) {
        const digits = mm[0].replace(/[\s\u00a0]/g, '');
        const num = parseInt(digits, 10);
        if (num > 5 && digits.length >= 2) nums.push(num);
      }
      if (nums.length) return Math.max(...nums);
    }
    const m = T.match(/([\d\s]+)\s*оцен(?:ка|ок|ки)/);
    return m ? parse(m[1]) : null;
  };
  const CHAR_WIDGETS = ["webCharacteristics", "webShortCharacteristics"];
  const characteristics = () => {
    const out = {};
    const grab = (dl) => {
      const dt = dl.querySelector('dt');
      const dd = dl.querySelector('dd');
      if (dt && dd) {
        const k = dt.innerText.trim();
        const v = dd.innerText.trim();
        if (k && v && !out[k]) out[k] = v;
      }
    };
    for (const wname of CHAR_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      w.querySelectorAll('dl').forEach(grab);
      if (Object.keys(out).length > 0) return out;
    }
    document.querySelectorAll('dl.tsBodyMBold').forEach(grab);
    if (Object.keys(out).length > 0) return out;
    document.querySelectorAll('dl').forEach((dl) => {
      const dt = dl.querySelector(':scope > dt');
      const dd = dl.querySelector(':scope > dd');
      if (dt && dd) {
        const k = dt.innerText.trim();
        const v = dd.innerText.trim();
        if (k && v && !out[k]) out[k] = v;
      }
    });
    if (Object.keys(out).length >= 5) return out;
    for (const wname of CHAR_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      w.querySelectorAll('[class*="Row"]').forEach((r) => {
        const t = r.innerText.split('\n');
        if (t.length >= 2) {
          const k = t[0].trim();
          const v = t[t.length - 1].trim();
          if (k && v && !out[k]) out[k] = v;
        }
      });
    }
    return out;
  };
  const gallery = () => {
    const out = [];
    const push = (i) => {
      const s = i.currentSrc || i.src || '';
      if (s && !out.includes(s) && !/\/video-[^/]+\/[^/]+\/cover\//.test(s)) out.push(s);
    };
    const GALLERY_WIDGETS = ['webGallery', 'webDescription', 'webProductDescription'];
    let found = 0;
    for (const wname of GALLERY_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      w.querySelectorAll('img').forEach(push);
      found++;
    }
    if (found === 0) {
      document.querySelectorAll('img[src*="cdn1.ozone.ru"], img[src*="ir.ozone.ru"]')
        .forEach(push);
    }
    return out;
  };

  const DESC_WIDGETS = ["webProductDescription", "webDescription"];
  const descriptions = () => {
    const out = [];
    for (const wname of DESC_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      const t = w.innerText.trim();
      if (t) out.push(t);
      if (out.length) return out;
    }
    document.querySelectorAll('[class*="ProductDescription"], [class*="description"]').forEach((n) => {
      const t = n.innerText.trim();
      if (t && t.length > 60) out.push(t);
    });
    if (out.length) return out;
    const charWidget = document.querySelector('[data-widget="webCharacteristics"]');
    if (charWidget) {
      let next = charWidget.nextElementSibling;
      while (next) {
        const t = next.innerText.trim();
        if (t.length > 100) {
          out.push(t);
          break;
        }
        next = next.nextElementSibling;
      }
    }
    return out;
  };

  const infographics = () => {
    const out = [];
    const GALLERY_WIDGETS = ['webMediaViewer', 'webGallery', 'webDescription', 'webProductDescription'];
    for (const wname of GALLERY_WIDGETS) {
      const w = document.querySelector('[data-widget="' + wname + '"]');
      if (!w) continue;
      w.querySelectorAll('img[src*="info"], img[src*="schema"]').forEach((i) => {
        const s = i.currentSrc || i.src || '';
        if (s) out.push(s);
      });
    }
    return out;
  };

  // ---- price: структурированная цена (TreeWalker по текстовым узлам) ----
  const priceParts = () => {
    const parts = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      const t = (n.textContent || '').replace(/\s+/g, ' ').trim();
      if (!t) continue;
      const p = n.parentElement;
      if (!p) continue;
      const st = p.getBoundingClientRect ? p.getBoundingClientRect() : null;
      let fs = 0;
      const cs = p && window.getComputedStyle ? window.getComputedStyle(p) : null;
      if (cs && cs.fontSize) fs = parseFloat(cs.fontSize) || 0;
      let weight = 0;
      if (cs && cs.fontWeight) weight = parseInt(cs.fontWeight, 10) || 0;
      const strike = !!(p.classList && (p.classList.contains('_8mQ_f') || (cs && cs.textDecorationLine && cs.textDecorationLine.includes('line-through'))));
      if (/[₽]|руб|р\.\s*$/.test(t) && t.length < 25) {
        parts.push({ t, y: st ? st.y : 0, x: st ? st.x : 0, fs, weight, strike });
      }
    }
    parts.sort((a, b) => (b.fs * (b.weight ? 2 : 1)) - (a.fs * (a.weight ? 2 : 1)));
    return parts.slice(0, 8);
  };

  const priceParsed = () => {
    const els = Array.from(document.querySelectorAll('[data-widget="webPrice"] [class*="mainPrice"], [class*="tsHeadline500Medium"]'));
    const seen = new Set();
    const out = [];
    els.forEach((e) => {
      const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
      if (!t || seen.has(t)) return;
      seen.add(t);
      const n = parse(t);
      if (n) out.push({ raw: t, value: n });
    });
    return out;
  };

  const price_text = () => {
    const m = T.match(/[\d\s]{3,}\s*[₽р]/);
    return m ? m[0].trim() : null;
  };

  // ---- price: точная цена из виджета webPrice ----
  // current — крупная не-кнопочная цена (fs 20, bold); old — зачёркнутая/меньшая
  // рядом; bank — цена в кнопке банка («С банками»); рассрочки/аксессуары вне
  // виджета webPrice не попадают вовсе.
  const priceExtract = () => {
    const w = document.querySelector('[data-widget="webPrice"]');
    const res = { current: null, old: null, bank: null, confidence: 0, source: null };
    if (!w) return res;
    const priceRe = /^\s*[\d\s\u2009\u00a0]{1,12}\s*[₽р]\s*$/;
    const items = [];
    const walk = (el, depth) => {
      if (!el || depth > 14) return;
      const direct = el.childNodes.length === 1 && el.childNodes[0].nodeType === 3;
      const txt = direct ? (el.textContent || '').trim() : '';
      if (txt && priceRe.test(txt)) {
        const cs = getComputedStyle(el);
        let node = el, inBtn = false;
        for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
          if (node.tagName === 'BUTTON') { inBtn = true; break; }
        }
        items.push({ txt, fs: parseFloat(cs.fontSize) || 0, w: parseInt(cs.fontWeight, 10) || 0,
          strike: (cs.textDecorationLine || '').includes('line-through'), inBtn });
      }
      for (const c of el.children) walk(c, depth + 1);
    };
    walk(w, 0);
    const num = (s) => parseInt(String(s || '').replace(/[\s\u2009\u00a0]/g, '').replace(/[₽р]/g, ''), 10);
    const main = items.filter((i) => !i.inBtn).sort((a, b) => (b.fs * 100 + b.w) - (a.fs * 100 + a.w));
    const cur = main[0];
    if (!cur) return res;
    res.current = num(cur.txt);
    res.source = 'webPrice';
    const oldCand = main.slice(1).filter((i) => i.strike || i.fs < cur.fs);
    if (oldCand.length) res.old = num(oldCand[0].txt);
    const bank = items.find((i) => i.inBtn);
    if (bank) res.bank = num(bank.txt);
    res.confidence = cur.fs >= 16 ? 0.97 : 0.85;
    return res;
  };

  const ratingData = rating();

  // ---- «По мнению покупателей»: % рекомендуют + разбивка оценок по звёздам ----
  const customerOpinion = () => {
    const out = { recommend_percent: null, stars: {}, counts: {}, text: null };
    const starRe = /^([1-5])\s*зв[её]зд[а-я]*$/i;
    const rows = [];
    for (const el of document.querySelectorAll('div, span')) {
      const t = (el.innerText || '').trim();
      const m = starRe.exec(t);
      if (m && t.length <= 20) {
        const row = el.parentElement;
        if (row) rows.push([parseInt(m[1], 10), row]);
      }
    }
    rows.forEach(([star, row]) => {
      // процент из style="width: N%" внутри строки гистограммы
      const wEl = row.querySelector('[style*="width"]');
      const wm = wEl && (wEl.getAttribute('style') || '').match(/width:\s*([\d.]+)%/);
      if (wm) out.stars[star] = parseFloat(wm[1]);
      // количество отзывов — число в конце текста строки
      const t2 = (row.innerText || '').replace(/\s+/g, ' ').trim();
      const cm = t2.match(/(\d[\d\s\u00a0]*)$/);
      if (cm) out.counts[star] = parseInt(cm[1].replace(/[\s\u00a0]/g, ''), 10);
    });
    // «N% покупателей рекомендуют» / «N% рекомендуют» по всей странице
    if (!Object.keys(out.stars).length || out.recommend_percent === null) {
      const pageTxt = document.body ? (document.body.innerText || '') : '';
      const rec = pageTxt.match(/(\d{1,3})\s*%\s*покупател/i)
              || pageTxt.match(/покупател[^\n%]{0,25}\s*(\d{1,3})\s*%/i)
              || pageTxt.match(/(\d{1,3})\s*%\s*рекомендуют/i);
      if (rec) out.recommend_percent = parseInt(rec[1], 10);
    }
    // fallback: пары «N» + «M%» подряд (гистограмма без слов «звезда»)
    if (!Object.keys(out.stars).length) {
      const allEls = Array.from(document.querySelectorAll(
        'span, div, [class*="score"], [class*="bar"], [class*="percent"]'));
      const texts = allEls.map((n) => (n.innerText || '').trim()).filter(Boolean);
      for (let i = 0; i + 1 < texts.length; i++) {
        const star = /^([1-5])$/.exec(texts[i]);
        const pct = /^(\d{1,3})\s*%$/.exec(texts[i + 1]);
        if (star && pct) out.stars[parseInt(star[1], 10)] = parseInt(pct[1], 10);
      }
    }
    out.text = (document.body ? (document.body.innerText || '') : '')
      .match(/По\s+мнению\s+покупателей[\s\S]{0,1200}/)?.[0]
      || '';
    return out;
  };

  return {
    title: title(),
    rating: ratingData ? ratingData.value : null,
    rating_source: ratingData ? ratingData.source : null,
    rating_count: ratingCount(),
    price_text: price_text(),
    price_parts: priceParts(),
    price_parsed: priceParsed(),
    price: priceExtract(),
    characteristics: characteristics(),
    gallery: gallery(),
    descriptions: descriptions(),
    infographics: infographics(),
    customer_opinion: customerOpinion(),
  };
}
"""

# ---------------------------------------------------------------------------
# JS-извлечение отзывов текущей страницы
# ---------------------------------------------------------------------------
REVIEWS_JS = r"""
() => {
  const lists = Array.from(document.querySelectorAll('div[data-widget="webListReviews"]'));
  const cards = Array.from(document.querySelectorAll('[data-review-uuid]'));
  const RE_DATE = /(\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+\d{4})/;
  const RE_ASPECT = /^[^:\n]{1,40}:\s*\S/;
  const blockIsAspects = (t) =>
    t.split('\n').filter((l) => l.trim()).every((l) => RE_ASPECT.test(l.trim()));
  const isStale = (el) => {
    if (el.getAttribute?.('title') === 'Отзыв устарел') return true;
    const t = el.querySelector?.('[title="Отзыв устарел"], [title*="устарел"]');
    if (t) return true;
    for (const n of el.querySelectorAll('div, span')) {
      const s = (n.innerText || '').trim();
      if (s && /^Отзыв\s+устарел$/i.test(s)) return true;
    }
    return false;
  };
  const extract = (card) => {
    const wrapper = card.querySelector('div[data-review-uuid]') || card;
    const uuid = wrapper.getAttribute('data-review-uuid');
    const publishedAt = wrapper.getAttribute('publishedat');
    const fullText = (wrapper.innerText || '');
    const dateMatch = fullText.match(RE_DATE);
    const date = dateMatch ? dateMatch[1] : '';
    const spans = Array.from(wrapper.querySelectorAll('span.tsCompactControl500Medium'));
    let author = (spans[0]?.innerText || '').trim();
    let author2 = (spans[1]?.innerText || '').trim();
    if (!author) {
      const alt = wrapper.querySelector('span[class*="tsCompact"]');
      if (alt) author = (alt.innerText || '').trim();
    }
    if (author2 && author2 === author) author2 = '';
    let variant = '';
    let aspectsEl = null;
    let bestLen = -1;
    Array.from(wrapper.querySelectorAll('a[href*="/product/"]')).forEach((a) => {
      const t = (a.innerText || '').trim();
      if (!RE_ASPECT.test(t)) return;
      let cur = a;
      while (cur && cur !== wrapper) {
        const inner = (cur.innerText || '').trim();
        if (inner.length <= 600 && cur.children.length <= 3 && blockIsAspects(inner)) {
          if (inner.length >= bestLen) {
            bestLen = inner.length;
            variant = inner;
            aspectsEl = cur;
          }
        }
        cur = cur.parentElement;
      }
    });
    let text = '';
    if (aspectsEl && aspectsEl.parentElement) {
      let bestT = '';
      let bestTlen = -1;
      Array.from(aspectsEl.parentElement.children).forEach((c) => {
        if (c === aspectsEl) return;
        const t = (c.innerText || '').trim();
        if (t.length > bestTlen) { bestTlen = t.length; bestT = t; }
      });
      text = bestT;
    }
    if (!text) {
      text = fullText
        .replace(/^[^\n]*\n/, '')
        .split('\n').filter((l) => !RE_ASPECT.test(l.trim())).join('\n');
    }
    text = text
      .replace(RE_DATE, '')
      .replace(/\n\s*(?:Вам помог этот отзыв\?.*|[\d\s]+\s*комментар.*)$/s, '')
      .replace(/\s*Да \d+\s*Нет \d+/, '')
      .replace(/\s*\d+\s*комментар\S*/, '')
      .replace(/\n\s*(?:Читать полностью|Читать дальше|Развернуть|Свернуть)\s*$/g, '')
      .trim();
    const svgs = Array.from(wrapper.querySelectorAll('svg'));
    let stars = 0;
    svgs.forEach((s) => {
      const cls = s.getAttribute('class') || '';
      if (cls.includes('a9')) stars++;
    });
    if (!stars && svgs.length === 5) stars = 0;
    const helpful = fullText.match(/Да (\d+)\s*Нет (\d+)/);
    const comments = fullText.match(/(\d+)\s*комментар\S*/);
    return {
      uuid, publishedAt, author, author2, date, variant, stars,
      stale: isStale(wrapper),
      text: text.slice(0, 20000),
      helpful_yes: helpful ? parseInt(helpful[1], 10) : 0,
      helpful_no: helpful ? parseInt(helpful[2], 10) : 0,
      comments_count: comments ? parseInt(comments[1], 10) : 0,
      comments: parseComments(card),
    };
  };
  const parseComments = (card) => {
    const parent = card.parentElement;
    const out = [];
    if (!parent) return out;
    Array.from(parent.querySelectorAll('[data-comment-uuid]')).forEach((cm) => {
      const author = (cm.querySelector('span.tsCompactControl500Medium')?.innerText || '').trim();
      const replyEl = cm.querySelector('a.rpProduct_c4a') || cm.querySelector('a.l9j_25');
      let replyTo = replyEl ? (replyEl.innerText || '').trim() : '';
      if (/^в ответ/i.test(replyTo)) replyTo = replyTo.replace(/^в ответ\s*/i, '').trim();
      const date = (cm.querySelector('div.rpProduct_a4c')?.innerText || cm.querySelector('div.j9l_25')?.innerText || '').trim();
      const text = (cm.querySelector('span.rpProduct_ca5')?.innerText || cm.querySelector('span.jm0_25')?.innerText || '').trim();
      const votes = Array.from(cm.querySelectorAll('div.tsBodyControl400Small'))
        .map((v) => parseInt((v.innerText || '').trim().replace(/\D/g, ''), 10) || 0);
      out.push({
        uuid: cm.getAttribute('data-comment-uuid'),
        author, reply_to: replyTo, date,
        text: text.slice(0, 20000),
        like: votes[0] || 0,
        dislike: votes[1] || 0,
      });
    });
    return out;
  };
  const reviews = cards.map(extract).filter((r) => r.uuid);
  let pag = null;
  for (let i = lists.length - 1; i >= 0 && !pag; i--) {
    const c = lists[i].children[2];
    if (c && (c.innerText || '').trim()) pag = c;
  }
  let pages = 1;
  if (pag) {
    const txt = (pag.innerText || '');
    const m = txt.match(/(\d+)/);
    if (m && /страниц/i.test(txt)) pages = parseInt(m[1], 10);
  }
  const total = document.querySelector('[data-widget="webReviewProductScore"]');
  return {
    ok: reviews.length > 0,
    reviews,
    pages,
    totalText: total ? total.innerText.trim() : null,
  };
}
"""


REVIEW_IDS_JS = r"""
() => {
  return Array.from(document.querySelectorAll('[data-review-uuid]'))
    .map((c) => (c.getAttribute('data-review-uuid') || '').trim())
    .filter(Boolean);
}
"""


SCROLL_TO_LAST_REVIEW_JS = r"""
() => {
  const els = document.querySelectorAll('[data-review-uuid]');
  if (els.length) {
    els[els.length - 1].scrollIntoView({ block: 'center' });
  } else {
    window.scrollBy(0, 1500);
  }
}
"""


SCROLL_STEP_UP_JS = r"""
/*step-up*/
() => {
  const before = window.scrollY;
  window.scrollBy({ top: -Math.round(window.innerHeight * 2.2), behavior: 'instant' });
  return window.scrollY <= 0 || window.scrollY === before;
}
"""


SCROLL_STEP_DOWN_JS = r"""
/*step-down*/
() => {
  const before = window.scrollY;
  window.scrollBy({ top: Math.round(window.innerHeight * 2.2), behavior: 'instant' });
  return window.scrollY === before
      || window.scrollY + window.innerHeight
      >= document.documentElement.scrollHeight - 2;
}
"""


EXPAND_REVIEWS_JS = r"""
() => {
  const RE = /^(Читать полностью|Читать дальше|Развернуть)$/;
  const clicked = new Set();
  let total = 0;
  for (let pass = 0; pass < 20; pass++) {
    let got = 0;
    const btns = Array.from(document.querySelectorAll('[data-review-uuid] *'))
      .filter((el) => {
        const t = (el.innerText || '').trim();
        if (!RE.test(t)) return false;
        if (clicked.has(el)) return false;
        const kids = Array.from(el.children);
        return !kids.some((k) => RE.test((k.innerText || '').trim()));
      });
    for (const b of btns) {
      clicked.add(b);
      try { b.click(); got++; } catch (e) {}
    }
    if (!got) break;
    total += got;
  }
  return total;
}
"""


MARK_COMMENT_CARDS_JS = r"""
() => {
  // Помечает карточки отзывов, у которых есть нераскрытая кнопка
  // «N комментарий» (N>=1). Сами клики делает Playwright (trusted) —
  // untrusted dispatchEvent игнорируется React (A/B-эксперимент 6/62 vs 31/31).
  const RE = /^\s*(\d+)\s*комментар/i;
  let total = 0;
  for (const b of document.querySelectorAll('button')) {
    const m = (b.innerText || '').trim().match(RE);
    if (!m || parseInt(m[1], 10) < 1) continue;
    const card = b.closest('[data-review-uuid]');
    if (!card || card.hasAttribute('data-comments-expanded')) continue;
    if (card.querySelector('[data-comment-uuid]')) continue;
    card.setAttribute('data-comments-expanded', '1');
    total++;
  }
  return total;
}
"""


VARIANT_MODE_JS = r"""
() => {
  const w = document.querySelector('[data-widget="webReviewsVariantMode"]');
  const totalEl = document.querySelector('[data-widget="webReviewProductScore"]');
  const tm = (totalEl ? (totalEl.innerText || '') : '').match(/(\d[\d\s]*)/);
  const total_count = tm ? parseInt(tm[1].replace(/\s/g, ''), 10) : null;
  if (!w) {
    return { found: false, options: [], variant_present: false,
             variant_count: null, active: null, total_count };
  }
  const activeByClass = (cls) => /(?:^|\s)[\w\d-]*-a8(?:\s|$)/.test(cls);
  const opts = Array.from(w.querySelectorAll('div[tabindex="0"]')).map((o) => {
    const text = (o.innerText || '').replace(/\s+/g, ' ').trim();
    const cls = String(o.className || '');
    const bg = getComputedStyle(o).backgroundColor;
    const active = activeByClass(cls)
      || (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent');
    return { text, active };
  });
  const variantOpt = opts.find((o) => /^Этот вариант товара/.test(o.text));
  const variant_count = variantOpt
    ? (variantOpt.text.match(/\((\d+)\)/) ? parseInt(variantOpt.text.match(/\((\d+)\)/)[1], 10) : null)
    : null;
  const activeOpt = opts.find((o) => o.active);
  return {
    found: true,
    options: opts.map((o) => o.text),
    variant_present: !!variantOpt,
    variant_count,
    active: activeOpt ? activeOpt.text.split(' (')[0] : null,
    total_count,
  };
}
"""


def normalize_text(s: str) -> str:
    """Нормализация текста отзыва: регистр, ё, пробелы."""
    s = re.sub(r"\s+", " ", (s or "").strip())
    s = s.replace("ё", "е").replace("Ё", "Е")
    return s


def _variant_matches(target_variant: dict | None, review: dict) -> bool:
    """Совпадает ли отзыв с описанием варианта (все непустые значения).

    Общий предикат фильтрации отзывов варианта (topup-добор и основной
    проход) — раньше дублировался в двух местах.
    """
    for key, val in (target_variant or {}).items():
        if val and str(val) not in normalize_text(review.get("variant", "")):
            return False
    return True


def parse_variant(spec: str) -> dict:
    """Разбор описания варианта для фильтрации отзывов.

    Два формата:
      1. Габариты '200x90x6' -> {'Длина_см': 200, 'Ширина_см': 90, 'Высота_см': 6}
         (порядок ДЛИНА x ШИРИНА x ВЫСОТА, как в названии товара).
      2. Произвольные пары 'ключ:значение' (через ';' или ',') ->
         {'Цвет': 'белый-прозрачный'}. Значение ищется подстрокой в variant-поле
         отзыва (для техники варианты по цвету/комплектации, не по габаритам).
    """
    s = spec.strip()
    if not s:
        return {}
    m = re.match(r"^\s*(\d+)\s*[xхХX*]\s*(\d+)\s*[xхХX*]\s*(\d+)\s*$", s)
    if m:
        return {
            "Длина_см": int(m.group(1)),
            "Ширина_см": int(m.group(2)),
            "Высота_см": int(m.group(3)),
        }
    if ":" in s:
        out = {}
        for part in re.split(r"[;,]", s):
            if ":" not in part:
                continue
            k, _, v = part.partition(":")
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
        if out:
            return out
    raise ValueError(
        f"Не удалось разобрать variant={spec!r}: ожидается '200x90x6' "
        f"или 'Цвет:белый-прозрачный'"
    )


def _t_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


CHALLENGE_TITLE_MARKERS = (
    "похоже, нет соединения",
    "доступ ограничен",
    "подтвердите, что вы не робот",
    "подтвердите, что вы человек",
    "проверка, что вы не робот",
    "antibot captcha",
    "captcha",
)

CHALLENGE_BODY_MARKERS = (
    "fab_chlg_",
    "fab_cp_",
    "выключите vpn",
    "сопоставьте пазл",
    "двигая ползунок",
)


def detect_challenge(page) -> str | None:
    """Сигнатура анти-бот-страницы Ozon (FAB/captcha) или None.

    Смотрим document.title (дёшево) и body-текст (маркеры DOM). 0 отзывов
    НЕ является признаком челленджа — детектор полагается только на эти
    сигнатуры, чтобы не ронять реальные карточки без отзывов.
    """
    try:
        title = (page.title() or "").strip().lower()
    except Exception:
        title = ""
    for m in CHALLENGE_TITLE_MARKERS:
        if m in title:
            return f"title:{m}"
    try:
        body = (page.evaluate(
            "() => (document.body ? document.body.innerText : '')") or "")
        body = body.lower()
    except Exception:
        return None
    for m in CHALLENGE_BODY_MARKERS:
        if m in body:
            return f"dom:{m}"
    return None


FAB_RELOAD_ATTEMPTS = 3
FAB_RELOAD_DELAY_MS = 2500


def _reload_fn(page):
    """Перезагрузить страницу (FAB-заглушка снимается обычным reload)."""
    try:
        page.reload(wait_until="domcontentloaded", timeout=60000)
    except Exception:
        try:
            page.reload(timeout=60000)
        except Exception:
            return False
    return True


def check_challenge(page, where: str, reload_on_fab: bool = True) -> None:
    """Поднять AccessChallengeError, если страница — анти-бот-заглушка.

    reload_on_fab=True: FAB-заглушка Ozon («Похоже, нет соединения») снимается
    простым обновлением страницы (подтверждено вручную: reload открывает Ozon).
    Делаем до FAB_RELOAD_ATTEMPTS перезагрузок с паузой; если FAB остаётся —
    бросаем AccessChallengeError.
    """
    sig = detect_challenge(page)
    if _puzzle.is_puzzle(sig):
        # Слайдер-пазл (НЕ FAB-заглушка): сначала пытаемся РЕШИТЬ, а не
        # перезагружать вслепую. Успех → пазл снят, никакого AccessChallenge
        # (не тратим бюджет challenge/new_tab/recycle).
        if _puzzle.solve_puzzle(page):
            log.warning(f"[collect] пазл решён ({where})")
            return
        log.warning(f"[collect] пазл не решён за {_puzzle.failures()} "
                    f"попыток ({where}) → FAB-reload fallback")
        sig = detect_challenge(page)
    for _ in range(FAB_RELOAD_ATTEMPTS) if (sig and reload_on_fab) else range(0):
        log.warning(f"[collect] FAB-заглушка ({sig}) → reload для разблокировки")
        _reload_fn(page)
        time.sleep(FAB_RELOAD_DELAY_MS / 1000.0)
        sig = detect_challenge(page)
        if not sig:
            break
    if sig:
        # INVALID (ADR §14.1): страница-заглушка не годится для извлечения.
        # Любые evaluate/scroll/goto на ней блокируются (Session._guard_check).
        try:
            page.is_invalid = True
        except Exception:
            pass
        raise B.AccessChallengeError(f"анти-бот ({where}): {sig}")


_AGE_GATE_PROBE_JS = (
    "() => /Подтвердите возраст/.test(document.body.innerText || '')"
)


def _pass_age_gate(page) -> bool:
    """Пройти модалку «Подтвердите возраст» (18+ категории).

    Маска даты ДД.ММ.ГГГГ не принимает программный value — вводим
    дату рождения посимвольно и жмём «Подтвердить». Возвращает True,
    если гейт был обнаружен и пройден.
    """
    try:
        if not page.evaluate(_AGE_GATE_PROBE_JS):
            return False
        inp = page.locator("input:visible").first
        inp.click()
        try:
            inp.press_sequentially("01011990", delay=40)
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            log.warning(f"[collect] age-gate press_sequentially warn: {e}")
            inp.type("01011990", delay=40)
        btn = page.get_by_role("button", name="Подтвердить")
        try:
            btn.wait_for(state="visible", timeout=5000)
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            log.warning(f"[collect] age-gate button wait warn: {e}")
        if btn.is_enabled():
            btn.click()
            page.wait_for_timeout(1500)
            log.info("[collect] возрастной гейт 18+ пройден")
            return True
        log.warning("[collect] age-gate: кнопка Подтвердить осталась disabled")
        return False
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        log.warning(f"[collect] age-gate warn: {e}")
        return False


def collect_product(page, product_url: str, category: str | None = None) -> dict:
    product = page.evaluate(PRODUCT_JS)
    if not product:
        raise RuntimeError("JS-извлечение карточки вернуло None (антибот?)")
    title = (product.get("title") or "").strip().lower()
    for m in CHALLENGE_TITLE_MARKERS:
        if m in title:
            raise B.AccessChallengeError(f"анти-бот (product.title): {m}")
    if category:
        product["category"] = category
    return product


def _next_page(page) -> Optional[str]:
    """URL следующей страницы отзывов или None.

    Приоритет:
      1. state-paginator (data-state JSON) — только если fetchType != 'scroll'
         (scroll-подгрузка обрабатывается _scroll_to_converge);
      2. webListReviews — ссылка «Дальше» или кнопка-клик (старая разметка);
      3. fallback: любая ссылка с layout_page_index.
    Возвращает абсолютный URL; '__CLICK__'-ветка клика по кнопке «Дальше»
    применяется на месте и возвращает None (страница обновилась в DOM)."""
    try:
        sp = page.evaluate(
            """() => {
              const el = document.querySelector('[id^="state-paginator"]');
              if (!el) return null;
              try {
                const d = JSON.parse(el.getAttribute('data-state'));
                if (d.fetchType === 'scroll') return null;
                return d.nextPage || null;
              } catch (e) { return null; }
            }""",
                )
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        sp = None
    if sp:
        if sp.startswith("/"):
            return "https://www.ozon.ru" + sp
        if sp.startswith("http"):
            return sp
    legacy = next_url(page)
    if legacy:
        return legacy
    try:
        href = page.evaluate(
            """() => {
              const a = document.querySelector('a[href*="layout_page_index="]');
              return a ? a.getAttribute('href') : null;
            }""",
                )
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        href = None
    if href:
        if href.startswith("/"):
            return "https://www.ozon.ru" + href
        if href.startswith("http"):
            return href
    return None


def next_url(page) -> Optional[str]:
    """URL следующей страницы отзывов (клик по «Дальше») или None."""
    try:
        pag = page.evaluate(
            """() => {
              const list = document.querySelector('div[data-widget="webListReviews"]');
              if (!list || !list.children[2]) return null;
              const node = list.children[2];
              const a = node.querySelector('a[href]');
              if (a) return a.getAttribute('href');
              const m = String(node.innerText || '').match(/Дальше/);
              return m ? '__CLICK__' : null;
            }""",
                )
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        return None
    if not pag:
        return None
    if pag == "__CLICK__":
        try:
            page.evaluate(
                """() => {
                  const list = document.querySelector('div[data-widget="webListReviews"]');
                  if (!list || !list.children[2]) return null;
                  const node = list.children[2];
                  const btn = node.querySelector('button') || node;
                  btn.click();
                }""",
                )
            page.wait_for_timeout(6000)
            return None  # страница уже обновилась на месте
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            return None
    if pag.startswith("/"):
        return "https://www.ozon.ru" + pag
    if pag.startswith("http"):
        return pag
    return None


def _scroll_to_reviews(
    page, target_min: int = 40, max_iters: int = 90, stable_iters: int = 8,
) -> bool:
    """Прокручивает страницу, пока не наберутся отзывы или не появится пагинация.

    Ozon грузит отзывы лениво по мере прокрутки: часть товаров — пагинацией
    («Дальше» в list.children[2]), часть — бесконечным скроллом. Крутим колесо,
    пока не выполнится одно из условий: появилась пагинация «Дальше», число
    отзывов достигло target_min, либо count не растёт stable_iters итераций.
    Возвращает True, если отзывы найдены."""
    last_count, stale = -1, 0
    count = 0
    for _ in range(max_iters):
        try:
            count = page.evaluate(
                "() => document.querySelectorAll('[data-review-uuid]').length",
                )
            has_next = page.evaluate(
                """() => {
                  const list = document.querySelector('div[data-widget="webListReviews"]');
                  if (!list || !list.children[2]) return false;
                  return /Дальше/.test(list.children[2].innerText || '');
                }""",
                )
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            count, has_next = 0, False
        if count > 0 and has_next:
            break
        if count >= target_min:
            break
        if count == last_count:
            stale += 1
            if stale >= stable_iters:
                break
        else:
            stale = 0
        last_count = count
        try:
            page.evaluate(f"() => window.scrollBy(0, {SCROLL_STEP_PX})")
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            break
        page.wait_for_timeout(scroll_pause_ms())
    return count > 0


def _scroll_to_converge(page, target_min: int = 40, max_iters: int = 90,
                        telemetry: dict | None = None):
    """Докручивает страницу до сходимости числа уникальных отзывов.

    Ozon грузит отзывы лениво: классическая пагинация («Дальше»), бесконечный
    скролл (state-paginator fetchType=scroll) и любые будущие схемы. Единственный
    надёжный инвариант — рост числа уникальных [data-review-uuid]. Крутим колесо,
    пока число уникальных отзывов растёт; стабильность MAX_SCROLL_STABLE итераций
    → финальная докрутка к низу (MAX_SCROLL_BOTTOM раз) для ленивых чанков.
    Возвращает (count, resumed): число уникальных отзывов и признак того,
    что рост возобновился при финальной докрутке к низу.

    telemetry (dict, мутируется) — диагностика сходимости: scroll_count,
    scroll_wait_seconds, stable_rounds, stable_iterations, last_growth_iteration,
    progression (уникальные после каждой итерации), bottom_passes, bottom_growth.
    Аккумуляторный стиль: при повторном вызове на той же странице значения
    складываются (нужно для «возобновления роста» в _collect_reviews_pass).
    """
    def unique_count():
        try:
            ids = page.evaluate(REVIEW_IDS_JS)
            return len(set(ids)) if isinstance(ids, list) else 0
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            return 0

    tel = telemetry
    if tel is None:
        tel = {}
    tel.setdefault("scroll_count", 0)
    tel.setdefault("scroll_wait_seconds", 0.0)
    tel.setdefault("progression", [])

    last, stable = -1, 0
    stable_iterations = 0
    last_growth_iteration = 0
    for it in range(max_iters):
        _touch_watchdog()
        n = unique_count()
        tel["progression"].append(n)
        if n > last:
            stable = 0
            last_growth_iteration = it + 1
        else:
            stable += 1
        last = n
        log.debug(f"converge it={it}: unique={n} stable={stable}")
        if stable >= MAX_SCROLL_STABLE:
            stable_iterations = it + 1
            log.debug(f"converge done: stable плато {MAX_SCROLL_STABLE}, unique={n}")
            break
        try:
            page.evaluate(SCROLL_TO_LAST_REVIEW_JS)
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            break
        tel["scroll_count"] += 1
        pause = scroll_pause_ms()
        tel["scroll_wait_seconds"] += pause / 1000.0
        page.wait_for_timeout(pause)
    resumed = False
    for _ in range(MAX_SCROLL_BOTTOM):
        _touch_watchdog()
        try:
            page.evaluate(SCROLL_TO_LAST_REVIEW_JS)
            pause = scroll_pause_ms()
            tel["scroll_wait_seconds"] += pause / 1000.0
            page.wait_for_timeout(pause)
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            break
        n = unique_count()
        if n > last:
            last = n
            resumed = True
            tel["progression"].append(n)
    tel["stable_rounds"] = stable
    tel["stable_iterations"] = stable_iterations
    tel["last_growth_iteration"] = last_growth_iteration
    tel["bottom_passes"] = tel.get("bottom_passes", 0) + MAX_SCROLL_BOTTOM
    tel["bottom_growth"] = tel.get("bottom_growth", False) or resumed
    log.debug(f"converge final: unique={last} resumed={resumed}")
    return last, resumed


def _expand_review_texts(page, max_passes: int = 8) -> int:
    """Кликает по всем кнопкам «Читать полностью»/«Читать дальше» в отзывах.

    Ozon усекает длинные отзывы и дораскрывает их по клику. Без клика
    REVIEWS_JS получает обрезанный innerText. Клик повторяется до сходимости
    (раскрытие может подгрузить ещё один уровень текста). Возвращает
    суммарное число кликов."""
    total = 0
    for _ in range(max_passes):
        try:
            n = page.evaluate(EXPAND_REVIEWS_JS) or 0
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            n = 0
        if not n:
            break
        total += n
        try:
            page.wait_for_timeout(800)
        except Exception:
            time.sleep(0.8)
        _touch_watchdog()  # долгий легитимный прогресс — сброс таймера сторожа
    return total


_COMMENT_BTN_RE = re.compile(r'[1-9]\d*\s*комментар', re.IGNORECASE)

# Перехват API комментариев к отзывам (rpGetCommentsByReviewUuid, B0 2026-09-02):
# API устойчив к смене hash-классов DOM и отдаёт текст/дату/голоса напрямую.
# POST json: {"reviewUuid": <uuid отзыва>, "sku": .., "rootSku": .., "limit":10,"offset":0}
# Ответ: {"cellTrackingInfo":{...},"comments":[{uuid, createdAt, comment,
#   publishedAt, publishedAtFormatted, parentUuid, rootParentUuid, isDeleted,
#   author:{firstName,clientOfficial:{brandName}}, usefulness:{useful,unuseful}}]}
# Привязка к отзыву: reviewUuid в post_data == review.uuid из DOM
# (rootParentUuid в ответе == тот же uuid отзыва).
_COMMENTS_API_URL_FRAGMENT = "/_action/rpGetCommentsByReviewUuid"
# Защитные пределы перехвата (на один проход сбора):
MAX_COMMENT_RESPONSES = 500      # максимум обработанных ответов API
MAX_RESPONSE_BYTES = 200_000     # тело ответа до response.json() (байт)
MAX_COMMENTS_PER_REVIEW = 500    # комментариев на один отзыв
MAX_TOTAL_COMMENTS = 100_000     # всех комментариев за сбор

_STICKY_INTERCEPT_HINT = "intercepts pointer events"
_UNSTABLE_ELEMENT_HINT = "element is not stable"


def _is_sticky_intercept(exc) -> bool:
    """True, если клик не прошёл из-за перекрытия/нестабильности элемента.

    Playwright сообщает об этом в тексте ошибки: «... subtree intercepts
    pointer events» или «... element is not stable» (элемент двигается:
    lazy-load, анимация, реклама). Это НЕ hang renderer: клик таймаутит,
    а renderer жив. Такую ошибку нельзя пробрасывать как renderer_failure —
    надо продолжить сбор (карточка уже помечена data-comments-clicked и
    больше не тронется)."""
    msg = str(exc)
    return _STICKY_INTERCEPT_HINT in msg or _UNSTABLE_ELEMENT_HINT in msg


def _expand_review_comments(page, max_passes: int = 4) -> int:
    """Раскрывает комментарии к отзывам («N комментарий»).

    Каждый клик дёргает API rpGetCommentsByReviewUuid и подгружает блок
    [data-comment-uuid] рядом с отзывом. MARK_COMMENT_CARDS_JS только помечает
    карточки с кнопкой, сами клики выполняет Playwright locator-кликом — это
    trusted-событие, которое React обрабатывает (A/B-эксперимент: dispatchEvent
    6/62, trusted-клики 15/16 и 31/31). Между кликами ждём асинхронную загрузку;
    повторный клик по одной карточке исключён маркером data-comments-clicked.
    Возвращает суммарное число кликов (0 — комментариев нет)."""
    total = 0
    for _ in range(max_passes):
        try:
            n = page.evaluate(MARK_COMMENT_CARDS_JS) or 0
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            n = 0
        if not n:
            break
        clicked = 0
        try:
            for _ in range(n):
                btn = page.locator(
                    '[data-comments-expanded]:not([data-comments-clicked]) button',
                    has_text=_COMMENT_BTN_RE,
                ).first
                if btn.count() == 0:
                    break
                h = btn.element_handle()
                if h is None:
                    # элемент отсоединился между count() и element_handle()
                    # (React-рендер/lazy-load) — пропускаем, не падаем
                    continue
                h.evaluate(
                    "(el) => { const c = el.closest('[data-review-uuid]');"
                    " if (c) c.setAttribute('data-comments-clicked', '1'); }"
                )
                try:
                    h.scroll_into_view_if_needed(timeout=5000)
                    h.click(timeout=8000)
                except Exception as e:
                    if B.is_renderer_failure(e) and not _is_sticky_intercept(e):
                        try:
                            h.evaluate("(el) => el.click()",
                                       timeout=8000)
                        except Exception as e2:
                            if B.is_renderer_failure(e2):
                                raise
                    else:
                        try:
                            h.evaluate("(el) => el.click()")
                        except Exception:
                            pass
                clicked += 1
                page.wait_for_timeout(300)
                _touch_watchdog()  # долгий прогресс — сброс таймера сторожа
        except Exception as e:
            if B.is_renderer_failure(e) and not _is_sticky_intercept(e):
                raise
            clicked = 0
        if not clicked:
            break
        total += clicked
        try:
            page.wait_for_timeout(2500)
        except Exception:
            time.sleep(2.5)
        _touch_watchdog()  # завершение прохода — сброс таймера сторожа
    return total


def _parse_comment_api(body: bytes) -> tuple[list | None, str]:
    """Разбирает тело ответа rpGetCommentsByReviewUuid в комментарии.

    Базовый API перехвачен в B0 (2026-09-02): комментарии приходят в fields,
    названная полями Ozon (comment, publishedAtFormatted, usefulness и пр.).
    Удалённые (isDeleted=true) и пустые комментарии пропускаются.
    Возвращает (comments, reason): comments — список dict в формате DOM-поля
    (uuid/author/reply_to/date/text/like/dislike, см. parseComments) либо None,
    если тело не JSON / не список comments (тогда остаётся DOM fallback);
    reason — краткая причина для лога (или "")."""
    if len(body) > MAX_RESPONSE_BYTES:
        return None, f"body>={MAX_RESPONSE_BYTES}B"
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception as e:
        return None, "json"
    if not isinstance(payload, dict):
        return None, "body-not-object"
    raw = payload.get("comments")
    if not isinstance(raw, list):
        return None, "no-comments-list"
    out = []
    for c in raw[:MAX_COMMENTS_PER_REVIEW]:
        if not isinstance(c, dict) or not c.get("uuid"):
            continue
        if c.get("isDeleted"):
            continue
        text = (c.get("comment") or "").strip()
        if not text:
            continue
        author = (c.get("author") or {})
        name = (author.get("firstName") or "").strip()
        if not name:
            brand = ((c.get("author") or {}).get("clientOfficial") or {})
            name = (brand.get("brandName") or "").strip()
        usefulness = (c.get("usefulness") or {})
        out.append({
            "uuid": c.get("uuid"),
            "author": name,
            "reply_to": "",
            "date": c.get("publishedAtFormatted") or "",
            "text": text[:20000],
            "like": int(usefulness.get("useful") or 0),
            "dislike": int(usefulness.get("unuseful") or 0),
        })
    return (out, "") if out else (None, "empty")


def _expand_comments_snapshot(page, max_passes: int = 4) -> tuple[int, dict]:
    """Раскрывает комментарии с перехватом API rpGetCommentsByReviewUuid.

    Делает то же, что _expand_review_comments, но параллельно ловит ответы
    API и привязывает их к отзыву по reviewUuid из post_data запроса
    (response-listener, см. _parse_comment_api). Слушатель снимается в
    try/finally; после кликов — короткая пауза, чтобы догнать хвост API.

    Возвращает (n_clicks, by_review): by_review — {review_uuid: [comments]}
    (пустой, если платформа не отвечала API). Отзывы с неоднозначной
    привязкой (нет reviewUuid / не JSON / пустое тело) остаются с DOM
    fallback: перехват просто их пропускает."""
    by_review: dict = {}
    n_resp = 0
    total_comments = 0

    def _on_response(resp):
        nonlocal n_resp, total_comments
        if _COMMENTS_API_URL_FRAGMENT not in (resp.url or ""):
            return
        if n_resp >= MAX_COMMENT_RESPONSES:
            log.warning(f"[comments-api] перехвачено ответов >= {MAX_COMMENT_RESPONSES}, "
                        f"пропускаю остальные")
            return
        n_resp += 1
        rid = ""
        try:
            post = resp.request.post_data or ""
            if post:
                data = json.loads(post)
                rid = (data.get("reviewUuid") or "").strip()
        except Exception:
            pass
        if not rid:
            return  # неоднозначно — остаётся DOM
        try:
            body = resp.body()
        except Exception:
            return  # тело недоступно (redirect/отмена) — остаётся DOM
        comments, reason = _parse_comment_api(body)
        if comments is None:
            log.debug(f"[comments-api] {rid}: пропущен ({reason})")
            return
        total_comments += len(comments)
        by_review[rid] = comments
        _touch_watchdog()  # один сброс на обработанный ответ

    try:
        page.on("response", _on_response)
    except Exception as e:
        log.warning(f"[comments-api] подписка warn: {e}")
        return 0, by_review
    try:
        n_clicks = _expand_review_comments(page, max_passes=max_passes)
        try:
            page.wait_for_timeout(1200)  # хвост API после последнего клика
        except Exception:
            time.sleep(1.2)
        _touch_watchdog()
    finally:
        page.remove_listener("response", _on_response)
    if by_review:
        log.info(f"[comments-api] перехвачено отзывов: {len(by_review)}, "
                 f"комментариев: {total_comments}")
    return n_clicks, by_review


def _apply_api_comments(reviews: list, api_comments: dict) -> int:
    """Вливает API-комментарии в отзывы (после финального top-up).

    Источник комментариев — один на отзыв: если API для отзыва дал
    непустые комментарии, они перезаписывают DOM-комментарии (API полнее:
    приходит с текстом/датами напрямую); иначе остаются DOM (fallback).
    Ставит на отзыв comments_source: api|dom — телеметрия происхождения.
    Возвращает число отзывов, где API заменил DOM."""
    replaced = 0
    for r in reviews:
        uid = r.get("uuid")
        api = api_comments.get(uid) if uid else None
        if not api or not isinstance(api, list) or not api:
            if r.get("comments"):
                r["comments_source"] = "dom"
            continue
        dom = r.get("comments") or []
        r["comments"] = api
        r["comments_source"] = "api"
        replaced += 1
        if dom:
            log.debug(f"[comments-api] {uid}: API заменил DOM "
                      f"({len(api)} vs {len(dom)})")
    return replaced



def _detect_pagination(page, grew: bool = True) -> str:
    """Определяет тип пагинации отзывов на текущей странице.

    Приоритет: state-paginator fetchType=scroll → infinite_scroll;
    state-paginator (не scroll) / webListReviews «Дальше» → next_button;
    рост без пагинатора → converged; иначе unknown."""
    try:
        sp = page.evaluate(
            """() => {
              const el = document.querySelector('[id^="state-paginator"]');
              if (!el) return null;
              try {
                const d = JSON.parse(el.getAttribute('data-state'));
                return d.fetchType || 'page';
              } catch (e) { return 'page'; }
            }""",
                )
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        sp = None
    if sp == "scroll":
        result = "infinite_scroll"
    elif sp is not None:
        result = "next_button"
    elif next_url(page):
        result = "next_button"
    else:
        result = "converged" if grew else "unknown"
    log.debug(f"pagination detected: {result} (sp={sp!r}, grew={grew})")
    return result


def _review_key(r: dict) -> str:
    """Ключ дедупликации отзыва.

    data-review-uuid (основной); fallback — sha1 от (author + date + text[:120])
    на случай выдачи без uuid. Fallback-ключ используется только внутри
    дедупликации и не пишется в отзыв."""
    uid = r.get("uuid")
    if uid:
        return uid
    raw = f"{r.get('author') or ''}|{r.get('date') or ''}|{(r.get('text') or '')[:120]}"
    return "sha1:" + hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()


def _dedupe_reviews(reviews: list, seen: set) -> list:
    """Возвращает отзывы, отсутствующие в seen; добавляет новые ключи в seen."""
    out = []
    for r in reviews:
        uid = _review_key(r)
        if uid in seen:
            continue
        seen.add(uid)
        out.append(r)
    return out


def _merge_topup(data_reviews, seen: set, index: dict, reviews: list,
                 target_variant: dict) -> tuple[int, int]:
    """Сливает выдачу шага top-up.

    Три случая: (1) uid в seen и в index — уже собран, дозаполняем поля
    (текст длиннее, счётчики больше); (2) uid в seen, но НЕТ в index —
    карточка была видна converge при прокрутке, но размонтировалась к моменту
    экстракции: это и есть добираемый дефицит — добавляем; (3) uid новый.
    Добавление идёт через вариантный фильтр. Возвращает (added, refreshed)."""
    added = refreshed = 0
    for r in data_reviews or []:
        uid = _review_key(r)
        if uid in seen:
            dst = index.get(uid)
            if dst is None:
                pass  # seen-но-не-собран: добираем как новое ниже
            else:
                v = r.get("text")
                if v and len(v) > len(dst.get("text") or ""):
                    dst["text"] = v
                    refreshed += 1
                for k in ("helpful_yes", "helpful_no", "comments_count"):
                    v = r.get(k)
                    if isinstance(v, int) and v > (dst.get(k) or 0):
                        dst[k] = v
                        refreshed += 1
                vc = r.get("comments")
                if isinstance(vc, list) \
                        and len(vc) > len(dst.get("comments") or []):
                    dst["comments"] = vc
                    refreshed += 1
                continue
        else:
            seen.add(uid)
        ok_v = _variant_matches(target_variant, r)
        if ok_v:
            reviews.append(r)
            index[uid] = r
            added += 1
    return added, refreshed


def _topup_virtualized(page, target_count: int, seen: set, index: dict,
                       reviews: list, target_variant: dict, page_no: int,
                       max_steps: int | None = None) -> int:
    """Добирает карточки, размонтированные виртуализацией infinite-scroll.

    Converge считает uuid за всё время прокрутки, а REVIEWS_JS снимает DOM
    в один момент: в нижней позиции окна часть верхних карточек размонтирована
    (пример: converge unique=34, экстракция 30 -> VariantShortError).
    Фаза 1: подъём к началу списка БЕЗ досрочных стопов — пропавшие карточки
    сидят у вершины, лимит «без роста» рубил подъём на полпути.
    Фаза 2 (если дефицит остался): спуск вниз для срединных случаев.
    В конце дораскрытие текстов и финальный пересъём. Возвращает число
    добавленных отзывов."""
    steps = MAX_TOPUP_STEPS if max_steps is None else max_steps
    added_total = 0

    def _sweep(step_js: str, label: str) -> bool:
        nonlocal added_total
        for step in range(1, steps + 1):
            _touch_watchdog()
            try:
                edge = page.evaluate(step_js)
                page.wait_for_timeout(scroll_pause_ms())
                data = page.evaluate(REVIEWS_JS)
            except Exception as e:
                if B.is_renderer_failure(e):
                    raise
                log.warning(f"[collect] стр. {page_no}: top-up {label} "
                            f"шаг {step} warn: {e}")
                break
            added, _ = _merge_topup((data or {}).get("reviews"), seen, index,
                                    reviews, target_variant)
            added_total += added
            log.info(f"[collect] стр. {page_no}: top-up {label} шаг {step}: "
                     f"+{added}, собрано {len(reviews)}/{target_count}")
            if len(reviews) >= target_count:
                return True
            if edge:
                break
        return False

    done = _sweep(SCROLL_STEP_UP_JS, "вверх")
    if not done and len(reviews) < target_count:
        _sweep(SCROLL_STEP_DOWN_JS, "вниз")
    api_comments = {}
    try:
        _touch_watchdog()
        n_exp = _expand_review_texts(page)
        n_com, api_comments = _expand_comments_snapshot(page)
        data = page.evaluate(REVIEWS_JS) if (n_exp or n_com) else None
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        data = None
    if data and data.get("ok"):
        _, refreshed = _merge_topup(data.get("reviews"), seen, index,
                                    reviews, target_variant)
        if refreshed:
            log.info(f"[collect] стр. {page_no}: top-up дозаполнил полей: "
                     f"{refreshed}")
    n_api = _apply_api_comments(reviews, api_comments) if api_comments else 0
    if n_api:
        log.info(f"[collect] стр. {page_no}: API-комментарии заменили "
                 f"DOM у {n_api} отзывов")
    return added_total


def _activate_variant_mode(page, timeout_s: float = 8.0) -> dict:
    """Включает нативный фильтр «Этот вариант товара» (webReviewsVariantMode).

    Управление фильтром происходит серверно: URL-параметр reviewsVariantMode=1
    показывает тумблер активным. Клик по опции — только если параметра ещё нет.
    Виджет webReviewsVariantMode рендерится лениво (после прокрутки к отзывам),
    поэтому перед первым evaluate делается скролл, если виджет ещё не появился.
    Возвращает диагностику:
      used       — фильтр был активен в момент сбора (True/False)
      clicked    — был ли выполнен клик по «Этот вариант товара»
      variant_present, variant_count, active, total_count — из VARIANT_MODE_JS
      reason     — no_widget / url_param / click_ok / click_no_effect / timeout / disabled
    Не бросает исключений: неудача клика не должна ронять сбор."""
    snap = None
    try:
        snap = page.evaluate(VARIANT_MODE_JS)
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        log.warning(f"[variant_mode] evaluate warn: {e}")
    if not snap or not snap.get("found"):
        # Виджет рендерится после прокрутки к отзывам: скроллим до его появления,
        # но не более 3 скроллов после появления списка отзывов (товар без
        # тумблера — не тратим время на бесконечную прокрутку).
        scrolled_after_reviews = 0
        for _ in range(60):
            try:
                page.evaluate(SCROLL_TO_LAST_REVIEW_JS)
            except Exception as e:
                if B.is_renderer_failure(e):
                    raise
                break
            page.wait_for_timeout(scroll_pause_ms())
            try:
                snap = page.evaluate(VARIANT_MODE_JS)
            except Exception as e:
                if B.is_renderer_failure(e):
                    raise
                snap = None
            if snap and snap.get("found"):
                break
            try:
                has_reviews = page.evaluate(
                    "() => !!document.querySelector('div[data-widget=\"webListReviews\"]')")
            except Exception:
                has_reviews = False
            if has_reviews:
                scrolled_after_reviews += 1
                if scrolled_after_reviews >= 3:
                    break
    if not snap or not snap.get("found"):
        return {"used": False, "clicked": False, "variant_present": False,
                "variant_count": None, "active": None, "total_count": None,
                "reason": "no_widget"}
    base = {k: snap.get(k) for k in ("variant_present", "variant_count", "active", "total_count")}
    if not snap.get("variant_present"):
        return {**base, "used": False, "clicked": False, "reason": "no_variant_opt"}
    if snap.get("active") == "Этот вариант товара":
        return {**base, "used": True, "clicked": False, "reason": "already_active"}
    try:
        url = page.url
    except Exception:
        url = ""
    if "reviewsVariantMode=1" in url:
        return {**base, "used": True, "clicked": False, "reason": "url_param"}
    try:
        page.evaluate(
            """() => {
              const w = document.querySelector('[data-widget="webReviewsVariantMode"]');
              if (!w) return;
              const opts = Array.from(w.querySelectorAll('div[tabindex="0"]'));
              const target = opts.find((o) =>
                /^Этот вариант товара/.test((o.innerText || '').replace(/\\s+/g, ' ').trim())
              );
              if (target) target.click();
            }"""
        )
    except Exception as e:
        log.warning(f"[variant_mode] click warn: {e}")
        return {**base, "used": False, "clicked": True, "reason": "click_error"}
    deadline = time.monotonic() + timeout_s
    active = False
    while time.monotonic() < deadline:
        try:
            snap = page.evaluate(VARIANT_MODE_JS)
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            snap = None
        if snap and snap.get("active") == "Этот вариант товара":
            active = True
            break
        time.sleep(0.8)
    if not active:
        log.warning("[variant_mode] клик не применился, собираем без нативного фильтра")
        return {**base, "used": False, "clicked": True, "reason": "click_no_effect"}
    try:
        page.wait_for_timeout(2500)
    except Exception:
        time.sleep(2.5)
    final = dict(base)
    try:
        snap = page.evaluate(VARIANT_MODE_JS)
    except Exception:
        snap = None
    if snap:
        for k in ("variant_present", "variant_count", "active", "total_count"):
            if snap.get(k) is not None:
                final[k] = snap.get(k)
    return {**final, "used": True, "clicked": True, "reason": "click_ok"}


_CARDS_COUNT_JS = "() => document.querySelectorAll('[data-review-uuid]').length"


def _count_review_cards(page):
    """Число карточек отзывов в DOM сейчас; None при ошибке evaluate."""
    try:
        return page.evaluate(_CARDS_COUNT_JS)
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        return None


def _collect_reviews_pass(
    page, page_url: str, sort: str | None, target_variant: dict,
    max_pages: int = 12, min_reviews: int = 40, apply_variant_mode: bool = True,
    telemetry: dict | None = None, no_count_hint: bool = False,
) -> tuple[List[dict], int, List[dict], dict | None]:
    """Один проход сбора отзывов по одной сортировке (см. collect_reviews).

    Возвращает (reviews, total_pages, page_log, variant_mode): reviews уже
    профильтрованы по target_variant и дедуплицированы внутри прохода;
    variant_mode — сырая диагностика фильтра варианта (finalize делает
    collect_reviews на объединённом списке). В page_log каждая запись несёт
    поле 'sort' (сортировка прохода) для диагностики multi-sort сборки.

    telemetry (dict, мутируется) — аккумулятор диагностики: scroll_time
    (только _scroll_to_converge), extract_time (раскрытие текстов/комментариев
    + извлечение REVIEWS_JS; БЕЗ goto и активации варианта), pages[].
    """
    seen = set()
    reviews = []
    total_pages = 1
    page_log = []
    variant_mode: dict | None = None
    tel = telemetry
    if tel is None:
        tel = {}
    tel.setdefault("scroll_time", 0.0)
    tel.setdefault("extract_time", 0.0)
    tel.setdefault("pages", [])
    for p in range(1, max_pages + 1):
        _touch_watchdog()
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            name = type(e).__name__
            if name == "TimeoutError" or "net::ERR_" in str(e) \
                    or "Timeout" in str(e):
                # навигация не удалась (таймаут/DNS/ERR) — продолжать разбор
                # по старой/ошибочной странице нельзя: соберём чужой контент
                raise
            log.warning(f"[collect] page {p} goto warn: {e}")
            page.wait_for_timeout(3000)
        check_challenge(page, f"reviews_page p{p}")
        _pass_age_gate(page)
        if apply_variant_mode:
            vm = _activate_variant_mode(page)
            if variant_mode is None or vm.get("used"):
                variant_mode = vm
            if vm.get("used") and vm.get("variant_count") == 0:
                # Виджет варианта явно говорит «Отзывы 0»: прокручивать
                # нечего — ни converge с релоадами, ни остальные сортировки.
                # Экономия ~70с на пустой вариант (кейсы 5274359430,
                # 5575856356).
                log.info(f"[collect] стр. {p}: у варианта 0 отзывов — "
                         f"пропускаю прокрутку (sort={sort})")
                page_log.append({"page": p, "n_found": 0, "n_matched": 0,
                                 "ok": True,
                                 "stop_reason": "variant_zero_reviews",
                                 "sort": sort})
                return [], 1, page_log, variant_mode
        if no_count_hint and p == 1:
            # Товар без явных счётчиков (rating_count/variant_count = None):
            # если на странице отзывов нет ни одной карточки (двойная
            # проверка с паузой), прокрутка и релоады бесполезны — кейс
            # 4551551032: нет ни рейтинга, ни виджета варианта, раздел пуст.
            n_cards = _count_review_cards(page)
            if n_cards == 0:
                page.wait_for_timeout(2500)
                n_cards = _count_review_cards(page)
            if n_cards == 0:
                log.info(f"[collect] стр. {p}: карточек нет, счётчиков нет "
                         f"— пропускаю проход (sort={sort})")
                page_log.append({"page": p, "n_found": 0, "n_matched": 0,
                                 "ok": True,
                                 "stop_reason": "no_cards_no_count_hint",
                                 "sort": sort})
                return [], 1, page_log, variant_mode
        page_tel: dict = {}
        _t0 = time.monotonic()
        empty_retries = 0
        while True:
            _touch_watchdog()
            count, resumed = _scroll_to_converge(
                page, target_min=min_reviews, telemetry=page_tel)
            if resumed:
                count, _ = _scroll_to_converge(
                    page, target_min=min_reviews, telemetry=page_tel)
            if count > 0 or p > 1 or empty_retries >= EMPTY_RETRY_PASSES:
                break
            empty_retries += 1
            log.warning(f"page {p}: 0 отзывов при прокрутке — "
                        f"reload ({empty_retries}/{EMPTY_RETRY_PASSES})")
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(EMPTY_RETRY_WAIT_MS)
            except Exception as e:
                if B.is_renderer_failure(e):
                    raise
                log.warning(f"page {p}: reload при пустой выдаче warn: {e}")
            check_challenge(page, f"reviews_page p{p} retry{empty_retries}")
        page_tel["count"] = count
        page_tel["resumed"] = resumed
        page_tel["empty_retries"] = empty_retries
        page_tel["page"] = p
        page_tel["sort"] = sort
        tel["scroll_time"] += time.monotonic() - _t0
        tel["pages"].append(page_tel)
        if count == 0:
            log.warning(f"[collect] page {p}: отзывы не появились при прокрутке, стоп")
            page_log.append({"page": p, "n_found": 0, "n_matched": 0, "ok": False,
                             "stop_reason": "no_scroll", "sort": sort,
                             "empty_retries": empty_retries})
            break
        if variant_mode is None:
            variant_mode = {}
        if "pagination_type" not in variant_mode:
            variant_mode["pagination_type"] = _detect_pagination(page, grew=count > 0)
        _et0 = time.monotonic()
        _touch_watchdog()
        try:
            n_expanded = _expand_review_texts(page)
            if n_expanded:
                log.info(f"[collect] стр. {p}: раскрыто «Читать полностью»: {n_expanded} кнопок")
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            log.warning(f"[collect] стр. {p}: раскрытие отзывов warn: {e}")
        api_comments = {}
        try:
            _touch_watchdog()
            n_comment, api_comments = _expand_comments_snapshot(page)
            if n_comment:
                log.info(f"[collect] стр. {p}: раскрыто комментариев к отзывам: {n_comment} кнопок")
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            log.warning(f"[collect] стр. {p}: раскрытие комментариев warn: {e}")
        _touch_watchdog()
        data = page.evaluate(REVIEWS_JS)
        tel["extract_time"] += time.monotonic() - _et0
        ok = bool(data and data.get("ok"))
        if not ok:
            log.warning(f"[collect] page {p}: пустая выдача, стоп")
            page_log.append({"page": p, "n_found": 0, "n_matched": 0, "ok": False,
                             "stop_reason": "empty", "sort": sort})
            break
        if data.get("totalText"):
            try:
                m = re.search(r"(\d+)\s*страниц", data["totalText"])
                if m:
                    total_pages = max(total_pages, int(m.group(1)))
            except Exception:
                pass
        matched_this = 0
        for r in _dedupe_reviews(data["reviews"], seen):
            if _variant_matches(target_variant, r):
                reviews.append(r)
                matched_this += 1
        log.debug(f"стр. {p}: всего на странице {len(data['reviews'])}, "
                  f"под вариант {len(reviews)} (виден {len(seen)})")
        # Виртуализация infinite-scroll: REVIEWS_JS видит только смонтированное
        # окно карточек, а converge насчитал count за всё время прокрутки.
        # Дефицит меряем по СОБРАННЫМ отзывам: seen после converge уже содержит
        # все uuid (в т.ч. размонтированные), поэтому len(seen) тут не годится.
        log.info(f"[collect][topup-check] стр. {p}: "
                 f"pt={(variant_mode or {}).get('pagination_type')!r} "
                 f"count={count} собрано={len(reviews)} видно={len(seen)}")
        if ((variant_mode or {}).get("pagination_type") == "infinite_scroll"
                and count > len(reviews)):
            idx0 = max(len(reviews) - matched_this, 0)
            pass_index = {_review_key(r): r for r in reviews[idx0:]}
            added = _topup_virtualized(page, count, seen, pass_index,
                                       reviews, target_variant, p)
            log.info(f"стр. {p}: top-up завершён: +{added} "
                     f"(собрано {len(reviews)}/{count})")
            if added:
                matched_this += added
                page_tel["topup_added"] = page_tel.get("topup_added", 0) + added
        n_api = _apply_api_comments(reviews, api_comments) if api_comments else 0
        if n_api:
            log.info(f"стр. {p}: API-комментарии заменили DOM у {n_api} отзывов")
        nxt = _next_page(page)
        if nxt and sort is not None:
            nxt = _sort_url(nxt, SORT_PARAMS.get(sort))
        if not nxt:
            log.warning(f"[collect] стр. {p}: пагинации нет, стоп")
            page_log.append({
                "page": p, "n_found": len(data["reviews"]), "n_matched": matched_this,
                "ok": True, "stop_reason": "no_next", "sort": sort,
            })
            break
        page_log.append({
            "page": p, "n_found": len(data["reviews"]), "n_matched": matched_this,
            "ok": True, "stop_reason": None, "sort": sort,
        })
        page_url = nxt
    return reviews, total_pages, page_log, variant_mode


def _merge_review_sorts(pass_reviews: list, merged: dict, order: list, sort: str) -> None:
    """Сливает отзывы одного прохода в объединённый список с дедупом по uuid.

    Один отзыв — одна запись: у первой записи проставляется review_sort
    (сортировка первого появления); при повторе в другой сортировке в
    существующую запись добавляется значение в review_sorts."""
    for r in pass_reviews:
        uid = _review_key(r)
        if uid in merged:
            if sort not in merged[uid]["review_sorts"]:
                merged[uid]["review_sorts"].append(sort)
        else:
            r["review_sort"] = sort
            r["review_sorts"] = [sort]
            merged[uid] = r
            order.append(uid)


def _finalize_variant_mode(variant_mode: dict | None, reviews: list,
                           apply_variant_mode: bool) -> dict:
    """Обогащает/собирает diagnostic variant_mode (см. collect_reviews)."""
    if apply_variant_mode:
        matched_variant = sum(1 for r in reviews if r.get("variant"))
        dropped = len(reviews) - matched_variant
        variant_mode = variant_mode or {
            "used": False, "clicked": False, "variant_present": False,
            "variant_count": None, "active": None, "total_count": None,
            "reason": "no_pages",
        }
        variant_mode["reviews_matched_variant"] = matched_variant
        variant_mode["reviews_dropped_by_client_filter"] = dropped
        if variant_mode.get("used") is True and matched_variant == 0:
            warning = "native_filter_active_no_matching_reviews"
            variant_mode["warning"] = warning
            log.warning(f"[collect] warning: {warning}")
        return variant_mode
    base = {"used": False, "variant_present": False, "active": None,
            "variant_count": None, "total_count": None,
            "reason": "disabled"}
    if variant_mode and variant_mode.get("pagination_type"):
        base["pagination_type"] = variant_mode["pagination_type"]
    return base


def collect_reviews(
    page, reviews_url: str, target_variant: dict, max_pages: int = 12,
    min_reviews: int = 40, apply_variant_mode: bool = True,
    sorts: Optional[List[str]] = None, telemetry: dict | None = None,
    no_count_hint: bool = False,
) -> tuple[List[dict], int, List[dict], dict]:
    """Собирает отзывы варианта. Возвращает (reviews, total_pages, page_log, variant_mode).

    Фильтр варианта: отзыв считается относящимся к варианту, если нормализованный
    текст его variant-поля содержит подстроки всех значений target_variant
    (для габаритов 'Длина_см' и т.п. — числа, для остальных — текст/значение).
    page_log — журнал страниц [{page, n_found, n_matched, ok, stop_reason, sort}],
    используется в компоненте confidence «стабильность сбора».

    apply_variant_mode=True — перед скроллом каждой страницы активировать
    нативный фильтр «Этот вариант товара» (webReviewsVariantMode); False —
    собрать все отзывы. variant_mode — диагностика фильтра (см. _activate_variant_mode).

    sorts=None — один проход без параметра sort (сортировка Ozon по умолчанию
    «новые и полезные»); отзывы не получают полей review_sort/review_sorts.
    Иначе — по одному проходу на каждую сортировку из SORT_PARAMS
    (published_at_desc / score_desc / score_asc), результаты объединяются в
    один список с дедупликацией по uuid: у каждого отзыва поле review_sort
    (сортировка первого появления) и review_sorts (все сортировки, где отзыв
    встретился). page_log — сквозной по всем сортировкам (с полем sort).

    telemetry (dict, мутируется) — аккумулятор диагностики сбора
    (scroll_time, extract_time, pages[]); см. _collect_reviews_pass.
    """
    tel = {} if telemetry is None else telemetry
    tel.setdefault("scroll_time", 0.0)
    tel.setdefault("extract_time", 0.0)
    tel.setdefault("pages", [])
    if sorts is None:
        reviews, total_pages, page_log, variant_mode = _collect_reviews_pass(
            page, reviews_url, None, target_variant,
            max_pages=max_pages, min_reviews=min_reviews,
            apply_variant_mode=apply_variant_mode, telemetry=tel,
            no_count_hint=no_count_hint,
        )
        variant_mode = _finalize_variant_mode(variant_mode, reviews, apply_variant_mode)
        if telemetry is not None:
            telemetry.update(tel)
        return reviews, total_pages, page_log, variant_mode

    unknown = [s for s in sorts if s not in SORT_PARAMS]
    if unknown:
        raise ValueError(
            f"Неизвестная сортировка {unknown[0]!r}; допустимо: {sorted(SORT_PARAMS)}"
        )
    merged: dict = {}
    order: list = []
    total_pages = 1
    page_log: list = []
    variant_mode: dict | None = None
    for sort in sorts:
        start_url = _sort_url(reviews_url, SORT_PARAMS[sort])
        log.info(f"[collect] сортировка «{SORT_LABELS.get(sort, sort)}» (sort={sort}): {start_url}")
        pass_reviews, tp, pl, vm = _collect_reviews_pass(
            page, start_url, sort, target_variant,
            max_pages=max_pages, min_reviews=min_reviews,
            apply_variant_mode=apply_variant_mode, telemetry=tel,
            no_count_hint=no_count_hint,
        )
        page_log.extend(pl)
        total_pages = max(total_pages, tp)
        if vm is not None and (
            variant_mode is None or (vm.get("used") and not variant_mode.get("used"))
        ):
            variant_mode = vm
        _merge_review_sorts(pass_reviews, merged, order, sort)
        log.info(f"[collect] сортировка «{SORT_LABELS.get(sort, sort)}»: "
                 f"собрано {len(pass_reviews)}, всего уникальных {len(order)}")
        # Оптимизация (2026-08-21): если вариант небольшой (variant_count <= 100)
        # и проход собрал его целиком, остальные сортировки вернут тот же
        # набор в другом порядке — пропускаем их (экономия ~минуты на товар
        # и меньше контакта с анти-ботом). Имеет смысл только когда список
        # обрезан стеной логина (vc > 100): там разные сортировки дают
        # разные подмножества.
        remaining = sorts[sorts.index(sort) + 1:]
        if remaining and not order and (
                no_count_hint
                or (variant_mode or {}).get("variant_count") == 0):
            log.info(f"[collect] отзывов у варианта нет — "
                     f"пропускаю сортировки: {remaining}")
            break
        vc_now = (variant_mode or {}).get("variant_count")
        if not isinstance(vc_now, int):
            # Фолбэк (2026-08-21): variant_count бывает None. Берём счётчик
            # converge последней страницы прохода — на небольших списках он
            # равен полному размеру выдачи. Граница 60 — заведомо ниже зоны
            # логин-стены (~100), где сортировки дают разные подмножества.
            pages_tel = tel.get("pages") or []
            c = (pages_tel[-1] or {}).get("count") if pages_tel else None
            if isinstance(c, int) and 0 < c <= 60:
                vc_now = c
        if (remaining and isinstance(vc_now, int)
                and 0 < vc_now <= VARIANT_COUNT_FULL_LIMIT
                and len(order) >= vc_now):
            log.info(f"[collect] вариант полный ({len(order)}/{vc_now}) — "
                     f"пропускаю сортировки: {remaining}")
            break
    reviews = [merged[uid] for uid in order]
    variant_mode = _finalize_variant_mode(variant_mode, reviews, apply_variant_mode)
    if telemetry is not None:
        telemetry.update(tel)
    return reviews, total_pages, page_log, variant_mode


def pid_from_url(url: str) -> str:
    url = (url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
    m = re.search(r"(?:-(\d+)|/(\d+))/?$", url)
    return (m.group(1) or m.group(2)) if m else "unknown"


def collect(
    product_url: str,
    reviews_url: str | None = None,
    variant: str = "",
    max_pages: int = 12,
    min_reviews: int = 40,
    out_path: str | None = None,
    cdp_url: str = DEFAULT_CDP_URL,
    category: str | None = None,
    save_dom: bool = True,
    apply_variant_mode: bool = True,
    sorts: Optional[List[str]] = None,
    sess=None,
    overwrite: bool = False,
) -> dict:
    """Полный сбор: карточка + отзывы варианта.

    По умолчанию сохраняет raw.json в data/{pid}_raw.json и DOM-снимок страницы
    в data/{pid}/dom/{YYYY-MM-DD}.html.gz (raw['artifacts']['dom_snapshot']).
    apply_variant_mode=True — включить нативный фильтр «Этот вариант товара»
    (raw['reviews_variant_mode']); False — собрать все отзывы без фильтра.
    sorts=None — одна сортировка по умолчанию; иначе список сортировок из
    SORT_PARAMS (published_at_desc/score_desc/score_asc) — отзывы собираются
    по каждой и объединяются с дедупом по uuid (raw['reviews_sorts']).
    sess — внешняя CDP-сессия (для переиспользования в search/gather):
    если передана, используется и НЕ закрывается; иначе создаётся своя.
    Возвращает dict."""
    target_variant = parse_variant(variant)
    sess_owned = sess is None
    if sess_owned:
        sess = Session(cdp_url)
    dom_html = None
    telemetry: dict = {}
    try:
        sess.goto(product_url)
        check_challenge(sess.page, "product_page")
        _pass_age_gate(sess.page)
        requested_pid = pid_from_url(product_url)
        actual_pid = pid_from_url(sess.page.url)
        if (requested_pid and actual_pid and requested_pid != "unknown"
                and actual_pid != "unknown" and requested_pid != actual_pid):
            raise RuntimeError(
                f"Page mismatch: requested PID {requested_pid}, "
                f"but browser shows PID {actual_pid}"
            )
        product = collect_product(sess.page, product_url, category=category)
        chars = product.get("characteristics") or {}
        if "Артикул" in chars and str(chars["Артикул"]) != str(requested_pid):
            log.warning(f"[collect] warning: артикул в характеристиках "
                        f"({chars['Артикул']}) отличается от PID {requested_pid}")
        if save_dom:
            try:
                dom_html = sess.page.content()
            except Exception as e:
                if B.is_renderer_failure(e):
                    raise
                log.warning(f"[collect] dom snapshot warn: {e}")
                dom_html = None
        rurl = reviews_url or product_url + "?tab=reviews"
        rc = product.get("rating_count")
        no_count_hint = not (isinstance(rc, int) and rc > 0)
        if isinstance(rc, int) and rc <= 0:
            # У товара 0 отзывов целиком (кейс 4551551032: нет тумблера
            # варианта и пустой раздел) — прокручивать нечего, сортировки
            # и релоады бесполезны. Явный ноль из карточки доверяем.
            log.info("[collect] у товара 0 отзывов (rating_count=0) — "
                     "пропускаю сбор отзывов")
            reviews, total_pages, page_log = [], 1, []
            variant_mode = {"used": False, "reason": "no_reviews_product",
                            "variant_count": 0}
        else:
            reviews, total_pages, page_log, variant_mode = collect_reviews(
                sess.page, rurl, target_variant, max_pages=max_pages,
                min_reviews=min_reviews, apply_variant_mode=apply_variant_mode,
                sorts=sorts, telemetry=telemetry,
                no_count_hint=no_count_hint,
            )
    finally:
        if sess_owned:
            sess.close()

    raw = {
        "collector_version": VERSION,
        "schema_version": SCHEMA_VERSION,
        "collected_at_utc": _t_utc(),
        "product_url": product_url,
        "reviews_url": rurl,
        "pid": requested_pid,
        "target_variant": target_variant,
        "product": product,
        "reviews": reviews,
        "reviews_collected": len(reviews),
        "reviews_expected": None,
        "reviews_sorts": sorts,
        "reviews_variant_mode": variant_mode,
        "pages": total_pages,
        "review_pages": page_log,
        "source": "cdp",
    }

    telemetry.setdefault("scroll_time", 0.0)
    telemetry.setdefault("extract_time", 0.0)
    telemetry.setdefault("pages", [])
    telemetry["scroll_jitter_seed"] = effective_scroll_jitter_seed()
    telemetry["total_review_collection_time"] = (
        telemetry["scroll_time"] + telemetry["extract_time"])
    raw["collection_telemetry"] = telemetry

    missing_fields = []
    if not (product.get("price") and product["price"].get("current")):
        missing_fields.append("product.price.current")
    if product.get("rating") is None:
        missing_fields.append("product.rating")
    raw["incomplete"] = bool(missing_fields)
    raw["missing_fields"] = missing_fields

    if save_dom and dom_html:
        pid = pid_from_url(product_url)
        dom_dir = os.path.join("data", pid, "dom")
        os.makedirs(dom_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dom_path = os.path.join(dom_dir, f"{stamp}.html.gz")
        with gzip.open(dom_path, "wt", encoding="utf-8") as f:
            f.write(dom_html)
        raw["artifacts"] = {"dom_snapshot": dom_path}

    if not out_path:
        pid = pid_from_url(product_url)
        out_path = K.raw_save_path(pid, overwrite=overwrite)
    if out_path != K.raw_path(pid_from_url(product_url)) and not overwrite:
        print(f"[collect] {K.raw_path(pid_from_url(product_url))} уже "
              f"существует — сохраняю в {os.path.basename(out_path)} "
              f"(перезапись только с --overwrite)")
    K.atomic_write_json(out_path, raw)
    log.info(f"[collect] сохранено: {out_path} ({len(reviews)} отзывов)")
    return raw
