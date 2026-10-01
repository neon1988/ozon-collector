"""Чтение DOM пазла и признаков состояния челленджа.

Только page.evaluate(): есть ли пазл, есть ли капча-заглушка, решено ли,
и где на странице лежит слайдер (трек + хэндл). Ни CV, ни мыши.
_firefox_page — обход только для диагностических аргументов.

Перенесено из puzzle.py без изменения кода (механический сплит).
"""

import re
import time
from typing import Optional


def is_puzzle(signature: Optional[str]) -> bool:
    """Сигнатура challenge указывает на слайдер-пазл (а не FAB-заглушку)."""
    return bool(signature and ("пазл" in signature or "ползунок" in signature))


def looks_like_puzzle(page) -> bool:
    """Есть ли на странице элементы слайдер-пазла (фон + кусочек) по DOM.

    Не полагается на текстовую сигнатуру title/body: если detect_challenge
    вернул общий анти-бот маркер (например 'antibot captcha'), но в DOM есть
    фон и кусочек — это пазл, который можно решить, а не глухой FAB-reload.
    """
    info = _capture_info(page)
    return bool(info and info.get("has"))


def is_captcha_shell(page) -> bool:
    """Капча v10 «скелетом»: контейнер/слайдер уже в DOM, но картинки ещё с
    пустым src и нулевым размером (captcha_v10.js проставляет их через
    0.3–2s), поэтому looks_like_puzzle() пока False. По скелету это РЕШАЕМЫЙ
    пазл — классификатор должен звать solver, который дождётся src поллингом.
    В has не влияет (не ломает _still_challenge после снятия пазла)."""
    info = _capture_info(page)
    return bool(info and info.get("shell"))
def _capture_info(page) -> Optional[dict]:
    """Извлечь элементы пазла (фон, кусочек, слайдер) из DOM страницы."""
    try:
        return page.evaluate(
            """() => {
              const out = {bg:null, puzzle:null, slider:null, has:false};
              document.querySelectorAll('img').forEach(el => {
                const r = el.getBoundingClientRect();
                const c = el.className || '';
                if (c.includes('image') && r.width > 200)
                  out.bg = {src: el.currentSrc||el.src,
                            r:[r.x,r.y,r.width,r.height]};
                if (c.includes('puzzle'))
                  out.puzzle = {src: el.currentSrc||el.src,
                                r:[r.x,r.y,r.width,r.height]};
              });
              document.querySelectorAll(
                '[class*=slider],[class*=slide],[class*=handler],[class*=track],[class*=drag],[class*=ckbox]'
              ).forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width > 30 && r.height > 15)
                  out.slider = {r:[r.x,r.y,r.width,r.height]};
              });
              out.shell = !!(document.querySelector(
                '#captcha-container, #captcha, #slider-container, img#image, img#puzzle'));
              out.has = !!(out.bg && out.puzzle);
              return out;
            }""")
    except Exception:
        return None


def _still_challenge(page) -> bool:
    """Остался ли на странице ЛЮБОЙ анти-бот челлендж (пазл, FAB, VPN-блок).

    Не считаем «решённым», если после драга страница оказалась другой
    заглушкой (например, 'выключите vpn'): пазл исчез, но выдача не открыта —
    это НЕ успех, а подмена челленджа.
    """
    if looks_like_puzzle(page):
        return True
    from .collect import detect_challenge
    return bool(detect_challenge(page))


def _captcha_done(page) -> bool:
    """Единственный достоверный признак успешного снятия пазла: URL получил
    параметр captchaDone=true.

    Исчезновение челленджа из DOM НЕ является успехом (подмена FAB/VPN-блоком,
    реролл, обрыв). Валидировано: url уже содержит captchaDone=true, а челлендж
    в DOM живёт ещё ~минуту — маркер именно в URL, не в DOM.
    """
    try:
        href = page.evaluate("() => location.href") or ""
    except Exception:
        return False
    return "captchaDone=true" in href


def _classify(url_done: bool, challenge_gone: bool, stub: bool,
              has_content: bool = True) -> str:
    """Чистый вердикт снятия капты из четырёх признаков (без DOM).

    'done'      — URL несёт captchaDone=true (окончательный маркер старого
                  поколения капчи со слайдером).
    'unblocked' — челлендж исчез, страница не заглушка И отдала реальный
                  контент. Это приёмка текущего поколения (v10, кусочек без
                  трека): после верного дропа капча снимает челлендж и
                  перезапрашивает выдачу НА МЕСТЕ, без навигации, поэтому
                  captchaDone=true в URL не появляется НИКОГДА.
    'no'        — челлендж жив, страница подменена заглушкой, либо контента
                  на странице нет.

    has_content — обязательное условие для 'unblocked' (наблюдение 2026-09-30
    с живого прогона): исчезновение челленджа само по себе бывает подменой —
    после верного дропа Ozon успевает снять пазл и тут же показать заглушку
    «Похоже, нет соединения». Без проверки контента такой случай засчитывался
    как solved (ложный успех: хантер показывал 100% при реальном ~15%).
    """
    if url_done:
        return "done"
    if not challenge_gone or stub or not has_content:
        return "no"
    return "unblocked"


# Страница «отдала контент» = есть заголовок h1 и непустой текст. Тот же
# признак, что collect._CHALLENGE_LOOKS_JS, но с обратной логикой и своим
# резолвом: puzzle_dom не должен зависеть от collect (collect импортирует
# puzzle — круговая ссылка).
_PAGE_HAS_CONTENT_JS = (
    "() => {"
    " const u = location.href || '';"
    " if (/__rr|abt_att|captcha|antibot/i.test(u)) return false;"
    " const h1 = document.querySelector('h1');"
    " const h1t = h1 ? (h1.innerText || '').trim() : '';"
    " if (h1t.length >= 5) return true;"
    " const body = document.body ? (document.body.innerText || '') : '';"
    " return body.length >= 300;"
    "}"
)


def _page_has_content(page) -> bool:
    """Есть ли на странице реальный контент (не заглушка, не пустой каркас).

    Обратная к collect._looks_like_challenge: там True = «страница тонкая,
    имеет смысл переспросить капчу», здесь True = «контент есть, доступ
    выдан». Ошибка evaluate трактуется как «контента нет» — осторожная
    сторона: лучше недосчитать успех, чем засчитать заглушку.
    """
    try:
        return bool(page.evaluate(_PAGE_HAS_CONTENT_JS))
    except Exception:
        return False


def _solved_state(page) -> str:
    """Вердикт снятия одним вызовом (резолвит признаки в этом модуле)."""
    return _classify(_captcha_done(page), not _still_challenge(page),
                     _stub_page(page), _page_has_content(page))


def _await_solved(page, seconds: float = 5.0, step: float = 0.4,
                  probe=None) -> str:
    """Поллить вердикт снятия до 'done'/'unblocked' или до дедлайна.

    probe — необязательный callable вместо _solved_state: вызывающий модуль
    подставляет свою резолюцию признаков (см. _verdict в puzzle.py и
    puzzle_drag.py), чтобы юнит-тесты патчили свои глобалы.
    """
    t0 = time.monotonic()
    while True:
        st = probe() if probe is not None else _solved_state(page)
        if st != "no" or time.monotonic() - t0 >= seconds:
            return st
        time.sleep(step)


# Отличительные строки Ozon-заглушки. НЕ включать общие слова вроде
# «попробуйте»/«challenge»/«antibot»: они встречаются на живой капче.
STUB_MARKERS = ("нет соединения", "отключите vpn", "проверьте vpn",
                "vpn-блок", "access denied")


def _stub_page(page) -> bool:
    """Страница — заглушка «нет соединения»/«отключите VPN» (FAB/VPN).

    Маркеры намеренно ТОЛЬКО отличительные строки заглушки. Раньше здесь стояли
    ещё «попробуйте», «challenge», «antibot» — и это давало ложные срабатывания
    на ЖИВОМ пазле: в инструкции капчи есть «Попробуйте сопоставить», а в
    разметке есть «antibot». Guard «сессия сожжена» (puzzle.solve_puzzle) на
    этом отказывался двигать кусок вообще — пазл стоял, не шело nothing
    (наблюдение пользователя 2026-10-01, live6: 8 пазлов, все 0.0s, 0 дропов).

    Основную защиту от «заглушка вместо разблокировки» несёт не этот список,
    а _page_has_content (есть h1 и текст): он проверяет факт выдачи доступа,
    а не совпадение слова. Этот список — только быстрый фильтр.
    """
    try:
        doc = (page.evaluate(
            "() => document.title + '||' + "
            "((document.body && document.body.innerText) || '').slice(0, 400)"
        ) or "").lower()
    except Exception:
        return False
    return any(m in doc for m in STUB_MARKERS)


# Перехват координаты лунки из JS капчи.
#
# Скилл ozon-puzzle-slot-fallback документирует: slot_x НЕ приходит из сети и
# НЕ лежит в DOM, он передаётся АРГУМЕНТОМ в вызовы инициализации капты
# (runCaptcha/_runCaptcha/initCaptcha). Живой дамп 131847 видел cx=112 в этих
# аргументах, и драг 48px снял капчу.
#
# Зачем при живом фоне и точной геометрии драга: CV-детекторы по
# фотографическому фону принципиально ненадёжны (79 кадров: 27 всегда успех,
# 40 всегда провал; переупорядочивание кандидатов не помогает - 20% против
# 17%). Здесь координата берётся из самого кода капчи, а не угадывается.
#
# Ставится ДО любых дропов и живёт на странице: после каждого неверного дропа
# капча перерисовывается и инициализация вызывается заново, так что запас
# обновляется сам. Аргументы DOM-узлов читаем через getBoundingClientRect.
_SLOT_SPY_JS = r"""
() => {
  const S = {hits: [], installed: true};
  const rect = a => { try { const r = a.getBoundingClientRect();
      return [Math.round(r.x), Math.round(r.y),
              Math.round(r.width), Math.round(r.height)]; } catch (e) { return null; } };
  const NAMES = ['runCaptcha', '_runCaptcha', 'initCaptcha', 'initSlide',
                 'init', 'renderCaptcha', 'showCaptcha', 'drawCaptcha',
                 'setSlide', 'slide', 'initPuzzle', 'onCaptcha'];
  for (const n of NAMES) {
    const f = window[n];
    if (typeof f !== 'function') continue;
    const wrapped = function () {
      try {
        const args = [];
        for (let i = 0; i < arguments.length && i < 12; i++) {
          const a = arguments[i];
          if (a && typeof a.getBoundingClientRect === 'function')
            args.push({t: 'el', v: rect(a)});
          else if (a && typeof a === 'object' && !Array.isArray(a)) {
            const o = {};
            for (const k in a) {
              const v = a[k];
              o[k] = (typeof v === 'number') ? v
                   : (v && typeof v.getBoundingClientRect === 'function'
                      ? {el: rect(v)} : typeof v);
            }
            args.push({t: 'obj', v: o});
          } else args.push({t: 'prim', v: a});
        }
        S.hits.push({fn: n, args: args, at: Date.now()});
        if (S.hits.length > 24) S.hits.shift();
      } catch (e) { /* не мешаем капче */ }
      return f.apply(this, arguments);
    };
    try { window[n] = wrapped; } catch (e) { /* readonly */ }
  }
  window.__slotSpy = S;
  return S.installed;
}
"""

_SLOT_READ_JS = r"""
() => {
  const S = window.__slotSpy;
  if (!S || !S.hits || !S.hits.length) return null;
  const out = [];
  for (const h of S.hits) {
    for (const a of (h.args || [])) {
      if (a.t === 'prim' && typeof a.v === 'number') {
        out.push({fn: h.fn, kind: 'num', v: a.v});
      } else if (a.t === 'el' && Array.isArray(a.v)) {
        out.push({fn: h.fn, kind: 'el', v: a.v});
      } else if (a.t === 'obj' && a.v) {
        for (const k in a.v) {
          const v = a.v[k];
          if (typeof v === 'number') {
            out.push({fn: h.fn, kind: 'field', k: k, v: v});
          } else if (v && typeof v === 'object' && v.el) {
            out.push({fn: h.fn, kind: 'fieldEl', k: k, v: v.el});
          }
        }
      }
    }
  }
  return {hits: S.hits.length, vals: out};
}
"""


def _slot_spy_install(page) -> bool:
    """Повесить перехватчик координат лунки (до первого дропа)."""
    try:
        return bool(page.evaluate(_SLOT_SPY_JS))
    except Exception:
        return False


def _slot_from_js(page) -> Optional[dict]:
    """Координата лунки, перехваченная в аргументах вызовов капты.

    Возвращает {'x': float, 'y': float, 'src': str} — координаты в экранных
    px, если удалось; иначе None. Молча: это улучшение поверх детекторов, а не
    замена им.
    """
    try:
        d = page.evaluate(_SLOT_READ_JS)
    except Exception:
        return None
    if not d or not d.get("vals"):
        return None
    vals = d["vals"]
    best = None
    # 1) явное числовое поле с координатой слота
    for v in vals:
        if v["kind"] == "field" and re.fullmatch(
                r"(x|sx|slotX|slot_x|left|leftX|cx|centerX|offsetX|targetX)", v["k"]):
            best = (float(v["v"]), v["fn"] + "." + v["k"])
            break
    # 2) единственное «круглое» число в разумном диапазоне ширины фона
    if best is None:
        nums = [float(v["v"]) for v in vals
                if v["kind"] == "num" and 20 <= float(v["v"]) <= 520]
        if len(nums) == 1:
            best = (nums[0], "num")
    # 3) DOM-узел, который уже не кусок и не фон (например сам оверлей лунки)
    if best is None:
        els = [v for v in vals if v["kind"] in ("el", "fieldEl")]
        for v in els:
            x, y, w, h = v["v"]
            if 20 <= w <= 260 and 20 <= h <= 200 and x > 1:
                best = (float(x), v["fn"] + "@el")
                break
    if best is None:
        return None
    return {"x": best[0], "src": best[1], "hits": d.get("hits")}


def _await_done(page, seconds: float, step: float = 0.5) -> bool:
    """Поллить URL до появления captchaDone=true (или дедлайна)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        if _captcha_done(page):
            return True
        time.sleep(step)
    return False


def _confirm_solved(page, grace_s: float = 18.0) -> bool:
    """Вердикт «solved» признаётся ТОЛЬКО при url captchaDone=true.

    Сначала короткий поллинг (редирект обычно приходит за секунды), затем
    _grace_resolve (DOM ещё может держать #captcha ~минуту после снятия).
    """
    if _await_done(page, 6.0):
        return True
    return _grace_resolve(page, grace_s)


def _grace_resolve(page, grace_s: float = 18.0) -> bool:
    """Пазл мог быть решён, но страница долго перестраивается (догрузка после
    снятия + бегунок проверки): DOM ещё секунды держит #captcha, и финальный
    вердикт получается ошибочно «failed» (валидировано на живом пазле
    1011675752: в url уже captchaDone=true, а челлендж в DOM дожил ~минуту).
    Поллим до grace_s: если url получил captchaDone=true — пазл снят (True),
    иначе честный fail (False)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < grace_s:
        time.sleep(2.0)
        if _captcha_done(page):
            return True
    return False


def _find_slider(page) -> Optional[dict]:
    """Найти ручку и трек слайдера по геометрии (у Ozon классы пустые).

    Возвращает {track:[x,y,w,h], handle:[x,y,w,h]} в экранных px или None.
    Трек — самый широкий тонкий DIV ниже фона; ручка — DIV ~40x32 у левого
    края трека (содержит svg-иконку).
    """
    try:
        return page.evaluate(
            """() => {
              let bg = null;
              [...document.querySelectorAll('img')].forEach(i => {
                const r = i.getBoundingClientRect();
                const c = String(i.className||'');
                if (c.includes('image') && r.width>200) bg = r;
              });
              if (!bg) return null;
              const all = [...document.querySelectorAll('div')];
              const belowY = bg.y + bg.height;
              let track=null, handle=null;
              all.forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width<=0 || r.height<=0) return;
                if (r.y >= belowY-5 && r.y < belowY+200
                    && r.width>=300 && r.height<=6)
                  if (!track || r.width>track)
                    track=[r.x, r.y, r.width, r.height];
              });
              if (!track) return null;
              const ty = track[1] + track[3]/2;
              all.forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width<=0 || r.height<=0) return;
                const cy = r.y + r.height/2;
                if (Math.abs(cy-ty) < 30
                    && Math.abs(r.width-40) <= 15
                    && Math.abs(r.height-32) <= 18)
                  if (!handle || r.x < handle[0])
                    handle=[r.x, r.y, r.width, r.height];
              });
              if (!handle) return null;
              return {track: track, handle: handle};
            }""")
    except Exception:
        return None
