#!/usr/bin/env python3
"""Live-проба вариант-статистики Ozon: меняется ли статистика после «Этот вариант товара».

Запуск на ИЗОЛИРОВАННОМ профиле (порт по умолч. 9336), чтобы не трогать
production ozon-profile/9222. Открывает карточку и снимает состояние виджетов
до и после клика «Этот вариант товара»:

  * webReviewProductScore  — рейтинг/гистограмма из data-state JSON (score,
    reviewsCount, score[], itemId) — по всей карточке или по варианту?
  * webListReviews         — paging.total + itemId отзывов в листе
  * webReviewsVariantMode  — вариант-фильтр (VARIANT_MODE_JS из collect.py)

Ключевой вопрос: меняется ли paging.total / score / itemId при переключении
варианта. Результат — JSON-отчёт, отдаётся на анализ, production не меняет.

Использование:
  python tests/probe_variant_stats.py --url https://www.ozon.ru/product/246074722
  python tests/probe_variant_stats.py --url <...> --profile <path>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from ozon_collector import cdp
from ozon_collector import collect as C

DEFAULT_PORT = 9336
GOTO_WAIT_MS = 10000
AFTER_CLICK_WAIT_S = 4.0

MAIN_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "opencode", "ozon-profile")
PROBE_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                             "opencode", "ozon-profile-variant-stats-probe")

# Виджеты, чьи data-state JSON-блоки снимаем. Полный/краткий вывод по
# webReviewProductScore и webListReviews, остальные — только факт наличия.
WIDGETS_OF_INTEREST = {
    "webReviewProductScore": "full",
    "webListReviews": "full",
    "webReviewsVariantMode": "full",
    "webProductScore": "brief",
    "webSingleProductScore": "brief",
}

# Краткое извлечение релевантных полей из state-JSON виджета (для отчёта),
# чтобы не тащить в отчёт гигабайты DOM-снимков.
SNAPSHOT_JS = """
() => {
  const out = { widget_states: {}, item_ids: [], score: null, paging_total: null };
  const pick = (s) => {
    if (!s || typeof s !== 'object') return s;
    const res = {};
    for (const k of ['totalScore','scoreCount','reviewsCount','rating',
                     'itemId','totalItemId','variantId','total','totalCount','count']) {
      if (k in s) res[k] = s[k];
    }
    if (Array.isArray(s.score)) res.score = s.score;
    if (Array.isArray(s.reviews)) {
      res.reviews_count = s.reviews.length;
      res.item_ids = s.reviews
        .map((r) => (typeof r === 'object' && r !== null ? r.itemId : undefined))
        .filter((v) => v !== undefined);
    }
    return res;
  };
  document.querySelectorAll('[id^="state-"]').forEach((el) => {
    const id = el.id || '';
    let state = null;
    try { state = JSON.parse(el.getAttribute('data-state') || 'null'); } catch (e) {}
    for (const [wname, mode] of Object.entries(WIDGETS)) {
      if (!id.includes(wname)) continue;
      const cur = { mode, present: true, state: (mode === 'full' ? pick(state) : null) };
      if (mode === 'brief') cur.shape = state ? Object.keys(state) : [];
      out.widget_states[wname] = cur;
      break;
    }
  });
  const sc = out.widget_states.webReviewProductScore;
  if (sc && sc.state && typeof sc.state.totalScore === 'number') {
    out.score = { value: sc.state.totalScore, reviewsCount: sc.state.reviewsCount,
                  score: sc.state.score };
  }
  const li = out.widget_states.webListReviews;
  if (li && li.state) {
    if (typeof li.state.total === 'number') out.paging_total = li.state.total;
    if (Array.isArray(li.state.item_ids)) out.item_ids = li.state.item_ids;
  }
  return out;
}
"""

# Диагностический дамп РЕАЛЬНОЙ структуры state-JSON виджетов отзывов
# (весь state каждого листа отзывов + score-виджета), чтобы увидеть фактические
# ключи: где лежат paging/total, itemId отзывов, гистограмма score.
DUMP_STRUCTURE_JS = """
() => {
  const dump = {};
  const walk = (o, depth, maxDepth) => {
    if (o === null || typeof o !== 'object') return o;
    if (depth >= maxDepth) return { __truncated__: true, type: Array.isArray(o) ? 'array' : typeof o };
    if (Array.isArray(o)) {
      return { __array_len__: o.length, __first__: o.length ? walk(o[0], depth + 1, maxDepth) : null };
    }
    const res = {};
    for (const k of Object.keys(o)) {
      if (k.includes('review') || k.includes('Review') || k.includes('item') ||
          k.includes('Item') || k.includes('page') || k.includes('counter') ||
          k === 'total' || k === 'score' || k === 'paging' || k === 'text' ||
          k === 'id' || k === 'sku') {
        res[k] = walk(o[k], depth + 1, maxDepth);
      }
    }
    return res;
  };
  const INTEREST = ['webListReviews', 'webReviewProductScore', 'webReviewsVariantMode',
                    'webProductScore', 'webSingleProductScore', 'webProductRating',
                    'webReviewSort'];
  document.querySelectorAll('[id^="state-"]').forEach((el) => {
    const id = el.id || '';
    const hit = INTEREST.find((w) => id.includes(w));
    if (!hit) return;
    let state = null;
    try { state = JSON.parse(el.getAttribute('data-state') || 'null'); } catch (e) {}
    dump[id] = {
      keys: state ? Object.keys(state) : [],
      structure: state ? walk(state, 0, 3) : null,
    };
  });
  return dump;
}
"""


def _copy_profile(dst: str, src: str, force: bool = False) -> None:
    import shutil
    import subprocess
    if force and os.path.isdir(dst):
        try:
            shutil.rmtree(dst, ignore_errors=True)
        except Exception as e:
            print(f"[probe] rmtree warn: {e}")
    if os.path.isdir(dst):
        print(f"[probe] профиль уже есть: {dst}")
        return
    if not src or not os.path.isdir(src):
        print(f"[probe] источник профиля не найден: {src!r} — чистый {dst}")
        os.makedirs(dst, exist_ok=True)
        return
    print(f"[probe] копирую профиль {src} → {dst}")
    if os.name == "nt":
        cmd = ["robocopy", src, dst, "/E", "/R:0", "/W:0",
               "/NFL", "/NDL", "/NJH", "/NJS"]
        try:
            subprocess.run(cmd, timeout=120, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception as e:
            print(f"[probe] robocopy warn: {e}")
    try:
        shutil.copytree(src, dst,
                        ignore=shutil.ignore_patterns("Singleton*", "*.lock"))
    except Exception as e:
        print(f"[probe] copytree warn: {e}")
        os.makedirs(dst, exist_ok=True)


def _ensure_browser(port: int, profile: str) -> None:
    cdp_url = f"http://127.0.0.1:{port}"
    if cdp.browser_alive(cdp_url):
        print(f"[probe] браузер уже жив на {cdp_url}")
        return
    print(f"[probe] запускаю Chromium на {cdp_url} профиль={profile}")
    cdp._launch_browser(cdp_url, profile_path=profile)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if cdp.browser_alive(cdp_url):
            return
        time.sleep(0.5)
    raise SystemExit(f"браузер не поднялся на {cdp_url}")


def _snapshot(sess, dump_structure: bool = False) -> dict:
    """Снять состояние виджетов (score, paging.total, itemId, variant_mode)."""
    snap = {"variant_mode": None, "widget": None}
    try:
        snap["widget"] = sess.page.evaluate(
            SNAPSHOT_JS.replace("WIDGETS", json.dumps(WIDGETS_OF_INTEREST)))
    except Exception as e:
        snap["widget_error"] = f"{type(e).__name__}: {e}"
    if dump_structure:
        try:
            snap["structure"] = sess.page.evaluate(DUMP_STRUCTURE_JS)
        except Exception as e:
            snap["structure_error"] = f"{type(e).__name__}: {e}"
    try:
        snap["variant_mode"] = sess.page.evaluate(C.VARIANT_MODE_JS)
    except Exception as e:
        snap["variant_mode_error"] = f"{type(e).__name__}: {e}"
    return snap


def _scroll_to_reviews(sess) -> None:
    """Прокрутка к отзывам (виджет webReviewsVariantMode рендерится лениво)."""
    try:
        sess.page.evaluate(C.SCROLL_TO_LAST_REVIEW_JS)
        sess.page.wait_for_timeout(1500)
    except Exception as e:
        print(f"[probe] scroll warn: {e}")


def _start_net_capture(sess) -> list:
    """Начать сбор сетевых ответов composer-api (лист/score отзывов).

    Возвращает список-аккумулятор, куда складываются фрагменты тел ответов
    с интересующими полями (paging.total, itemId, score-гистограмма).
    """
    captured: list = []

    def _on_response(resp):
        try:
            url = resp.url
            if "composer-api" not in url and "reviews" not in url.lower():
                return
            if not url.endswith(".json") and "reviews" not in url.lower():
                return
            body = resp.text()
            if not body:
                return
            entry = {"url": url[:220], "len": len(body)}
            # лист отзывов (особенно после reviewsVariantMode=1) — сохраняем
            # полное тело для анализа paging.total / itemId / гистограммы
            if "reviews" in url and "reviews" in url.split("url=")[-1][:120]:
                entry["body"] = body[:40000]
            else:
                import re as _re
                for key in ("total", "totalCount", "pages", "itemId",
                            "variantId", "totalScore", "reviewsCount", "score"):
                    m = _re.search(r'"' + key + r'"\s*:\s*([0-9.]+|\[[^\]]*\])', body)
                    if m:
                        val = m.group(1)
                        if len(val) > 200:
                            val = val[:200] + "…"
                        entry.setdefault("fields", {})[key] = val
            captured.append(entry)
        except Exception:
            pass

    try:
        sess.page.on("response", _on_response)
    except Exception as e:
        print(f"[probe] net capture attach warn: {e}")
    return captured


def _click_variant(sess) -> bool:
    """Кликнуть «Этот вариант товара» (untrusted). Вернуть факт клика."""
    try:
        ok = sess.page.evaluate(
            """() => {
              const w = document.querySelector('[data-widget="webReviewsVariantMode"]');
              if (!w) return false;
              const opts = Array.from(w.querySelectorAll('div[tabindex="0"]'));
              const target = opts.find((o) =>
                /^Этот вариант товара/.test((o.innerText || '').replace(/\\s+/g, ' ').trim())
              );
              if (!target) return false;
              target.click();
              return true;
            }"""
        )
        return bool(ok)
    except Exception as e:
        print(f"[probe] click warn: {e}")
        return False


def _scroll_deep_to_load_reviews(sess, rounds: int = 6) -> int:
    """Прокрутка вниз, чтобы подгрузить список текстов отзывов (scroll-пагинатор).

    повторяет логику коллектора: scrollIntoView последней карточки
    [data-review-uuid] (или scrollBy, пока карточек нет).
    returns: сколько карточек [data-review-uuid] нашлось на странице.
    """
    last = 0
    for _ in range(rounds):
        try:
            sess.page.evaluate(C.SCROLL_TO_LAST_REVIEW_JS)
            sess.page.wait_for_timeout(1800)
        except Exception:
            break
        try:
            last = sess.page.evaluate(
                "() => document.querySelectorAll('[data-review-uuid]').length") or 0
        except Exception:
            pass
    return last


def _dump_dom_card(sess) -> dict:
    """Снять ключевые атрибуты первой карточки отзыва + есть ли itemId в DOM."""
    try:
        return sess.page.evaluate(
            """() => {
              const card = document.querySelector('[data-review-uuid]');
              if (!card) return { present: false };
              const attrs = {};
              for (const a of card.attributes) attrs[a.name] = a.value;
              const html = card.outerHTML;
              return {
                present: true,
                attrs,
                has_itemId: html.includes('itemId'),
                has_galleryParams: html.includes('galleryParams'),
                has_product_href: html.includes('/product/'),
                itemId_matches: (html.match(/itemId[:=]?(\\d+)/) || [])[1] || null,
                product_hrefs: [...new Set(
                  Array.from(card.querySelectorAll('a[href*="/product/"]'))
                    .map(a => (a.getAttribute('href')||'').match(/product[/]*(\\d+)/)?.[1] || '')
                    .filter(Boolean))].slice(0,5),
                html_head: html.slice(0, 1200),
              };
            }""")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _safe_title(page) -> str:
    try:
        return page.title() or ""
    except Exception:
        return ""


def _probe(sess, url: str, dump_structure: bool = False) -> dict:
    out = {"url": url, "steps": []}
    t0 = time.monotonic()
    net = _start_net_capture(sess)
    try:
        sess.goto(url, wait_ms=GOTO_WAIT_MS)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["elapsed_s"] = round(time.monotonic() - t0, 2)
        return out
    out["title"] = _safe_title(sess.page)

    _scroll_to_reviews(sess)
    out["before"] = _snapshot(sess, dump_structure=dump_structure)
    out["before"]["net"] = list(net)
    net.clear()
    out["steps"].append({"step": "before",
                         "elapsed_s": round(time.monotonic() - t0, 2)})

    clicked = _click_variant(sess)
    out["clicked_variant"] = clicked
    if clicked:
        time.sleep(AFTER_CLICK_WAIT_S)
        try:
            sess.page.wait_for_timeout(2500)
        except Exception:
            pass
    out["after"] = _snapshot(sess, dump_structure=dump_structure)
    time.sleep(1.5)
    out["after"]["net"] = list(net)
    net.clear()
    out["steps"].append({"step": "after",
                         "elapsed_s": round(time.monotonic() - t0, 2)})

    out["reviews_loaded_cards"] = _scroll_deep_to_load_reviews(sess)
    time.sleep(1.0)
    out["reviews_net"] = list(net)
    out["dom_card"] = _dump_dom_card(sess)
    out["steps"].append({"step": "reviews_load",
                         "elapsed_s": round(time.monotonic() - t0, 2)})

    b = out.get("before", {})
    a = out.get("after", {})
    bw = (b.get("widget") or {})
    aw = (a.get("widget") or {})
    out["paging_total_changed"] = bw.get("paging_total") != aw.get("paging_total")
    out["score_changed"] = (bw.get("score") or {}).get("value") != (aw.get("score") or {}).get("value")
    out["item_ids_changed"] = bw.get("item_ids") != aw.get("item_ids")

    # itemId, найденные в сетевых ответах (galleryParams отзывов и т.п.)
    net_item_ids: list = []
    for entry in (out.get("after", {}).get("net") or []) + (out.get("reviews_net") or []):
        body = entry.get("body")
        if not body:
            continue
        import re as _re
        for m in _re.finditer(r'itemId=(\d+)', body):
            net_item_ids.append(m.group(1))
        for m in _re.finditer(r'"itemId"\s*:\s*"?(\d+)"?', body):
            net_item_ids.append(m.group(1))
    out["net_item_ids_count"] = len(net_item_ids)
    out["net_item_ids_unique"] = sorted(set(net_item_ids))[:30]

    out["analysis"] = {
        "paging_total_before": bw.get("paging_total"),
        "paging_total_after": aw.get("paging_total"),
        "score_before": (bw.get("score") or {}).get("value"),
        "score_after": (aw.get("score") or {}).get("value"),
        "item_ids_before": (bw.get("item_ids") or [])[:5],
        "item_ids_after": (aw.get("item_ids") or [])[:5],
        "variant_active_before": (b.get("variant_mode") or {}).get("active"),
        "variant_active_after": (a.get("variant_mode") or {}).get("active"),
        "variant_count_before": (b.get("variant_mode") or {}).get("variant_count"),
        "variant_count_after": (a.get("variant_mode") or {}).get("variant_count"),
        "reviews_loaded_cards": out.get("reviews_loaded_cards"),
        "net_item_ids_unique": out.get("net_item_ids_unique"),
    }
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="probe_variant_stats",
        description="Live-проба вариант-статистики Ozon (изолированный профиль)")
    p.add_argument("--url", required=True, help="URL карточки товара")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"CDP-порт пробы (по умолч. {DEFAULT_PORT})")
    p.add_argument("--profile", default=None,
                   help="путь профиля-копии (по умолч. изолированный)")
    p.add_argument("--no-copy", action="store_true",
                   help="не создавать профиль-копию")
    p.add_argument("--force-copy", action="store_true",
                   help="пересоздать профиль-копию")
    p.add_argument("--out", default="probe_variant_stats_report.json",
                   help="путь JSON-отчёта")
    p.add_argument("--yes", action="store_true",
                   help="не спрашивать подтверждения (live-трафик!)")
    p.add_argument("--dump-structure", action="store_true",
                   help="добавить в отчёт реальную структуру state-JSON листов отзывов")
    return p


def main() -> int:
    args = build_parser().parse_args()
    port = args.port
    profile = args.profile or PROBE_PROFILE
    if not args.no_copy:
        _copy_profile(profile, MAIN_PROFILE, force=args.force_copy)
    if not args.yes:
        print("ВНИМАНИЕ: live-эксперимент, тратит живой бюджет Ozon "
              f"(url={args.url}, порт {port}).")
        try:
            input("Продолжить? [Enter] ")
        except KeyboardInterrupt:
            print("\nотменено")
            return 0
    _ensure_browser(port, profile)
    sess = cdp.Session(f"http://127.0.0.1:{port}",
                       profile_token="ozon-profile-variant-stats-probe")
    try:
        report = _probe(sess, args.url, dump_structure=args.dump_structure)
    finally:
        try:
            sess.close()
        except Exception:
            pass
    report["url"] = args.url
    report["port"] = port
    report["profile"] = profile
    print(json.dumps(report, ensure_ascii=False, indent=2))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nJSON-отчёт сохранён: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
