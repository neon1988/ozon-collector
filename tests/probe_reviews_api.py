#!/usr/bin/env python3
"""P1-проба: отзывы через JSON API из контекста страницы против DOM.

Зачем: DOM-скролл упирается в потолок ~100 отзывов и медленный (виртуализация,
по ~1 карточке за итерацию). Ozon отдаёт те же отзывы обычным GET
(ozon_collector/ozon_fetch.py) — куки/заголовки/TLS те же, запрос идёт из
контекста вкладки. Открытый вопрос: упирается ли API в тот же потолок.

Проба отвечает на 4 вопроса, всё измеряет и ничего не меняет в production:
  1. работает ли GET из вкладки и какой у отзыва ключ идентификатора;
  2. сколько уникальных отзывов даёт DOM-скролл (граница, N итераций);
  3. сколько даёт API и пагинируется ли он (nextPage / layout_page_index);
  4. совпадает ли множество UUID API с DOM и с уже собранным raw этого pid.

Запуск на ИЗОЛИРОВАННОМ профиле (порт по умолч. 9337), чтобы не трогать
production ozon-profile/9222. Отчёт — JSON.

Использование:
  python tests/probe_reviews_api.py --pid 210502516
  python tests/probe_reviews_api.py --pid 210502516 --api-pages 6 --yes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Лаунчер (копия профиля + подъём Chromium) — тот же, что у variant-stats пробы,
# чтобы не плодить третью копию.
import probe_variant_stats as PV  # noqa: E402

from ozon_collector import cdp  # noqa: E402
from ozon_collector import collect as C  # noqa: E402
from ozon_collector import ozon_fetch as OF  # noqa: E402

DEFAULT_PORT = 9337
GOTO_WAIT_MS = 10000
SCROLL_SETTLE_S = 2.5
# Ключ идентификатора отзыва в API неизвестен заранее (в разборе widgetStates
# документированы content/author/createdAt/comments/... без id) — перебираем
# кандидатов и пишем реальный ключ в отчёт.
ID_KEYS = ("uuid", "id", "itemId", "reviewUuid", "reviewId")


def _rev_id(review: dict):
    """Идентификатор отзыва из API-объекта: первый непустой из ID_KEYS."""
    for k in ID_KEYS:
        v = review.get(k)
        if v not in (None, "", 0):
            return str(v), k
    return None, None


def _uuids(reviews) -> tuple[set, dict]:
    """Множество id + статистика по ключам (какой ключ реально сработал)."""
    out, key_used = set(), None
    empty = 0
    for r in reviews or []:
        if not isinstance(r, dict):
            continue
        uid, key = _rev_id(r)
        if uid is None:
            empty += 1
            continue
        key_used = key_used or key
        out.add(uid)
    return out, {"id_key": key_used, "without_id": empty}


def _scalar_keys(payload, wanted) -> dict:
    """Плоские скаляры верхнего уровня — для отчёта, чтобы не тащить виджет.

    Терпима к форме: sortings приходит списком, paging — объектом (замер
    2026-09-25: widget['sortings'] = [...]), поэтому не-dict → пусто.
    """
    if not isinstance(payload, dict):
        return {}
    out = {}
    for k, v in payload.items():
        if k in wanted and isinstance(v, (int, float, str, bool)):
            out[k] = v if not isinstance(v, str) or len(v) < 120 else v[:120]
    return out


def _dom_uuids(page, max_scrolls: int) -> tuple[set, dict]:
    """DOM-базис: сколько уникальных [data-review-uuid] даёт ограниченный скролл."""
    seen, t0, last_n, stalls = set(), time.monotonic(), -1, 0
    per_iter = []
    for i in range(max_scrolls):
        ids = page.evaluate(C.REVIEW_IDS_JS) or []
        before = len(seen)
        seen.update(str(x) for x in ids if x)
        per_iter.append(len(seen))
        if len(seen) == before:
            stalls += 1
            if stalls >= 2:
                break
        else:
            stalls = 0
        last_n = len(seen)
        page.evaluate(C.SCROLL_TO_LAST_REVIEW_JS)
        page.wait_for_timeout(int(SCROLL_SETTLE_S * 1000))
    return seen, {
        "unique": last_n if last_n > 0 else len(seen),
        "scrolls_done": len(per_iter),
        "growth_per_scroll": per_iter,
        "seconds": round(time.monotonic() - t0, 1),
    }


def _api_pages(page, pid: str, max_pages: int) -> tuple[set, dict]:
    """API-обход: page 1, дальше по nextPage виджета, иначе layout_page_index+1.

    Возвращает (uuids, статистика). Страница без nextPage и без новых отзывов
    означает конец пагинации — это и есть искомый «потолок» со стороны API.
    """
    seen, t0, pages, key_stat = set(), time.monotonic(), [], {}
    url = OF.reviews_url(pid, 1)
    for n in range(1, max_pages + 1):
        ta = time.monotonic()
        payload = OF.fetch_json(page, url)
        if payload is None:
            pages.append({"page": n, "ok": False,
                          "note": "fetch_json=None (сеть/парсинг)"})
            break
        widget = OF.find_reviews_widget(payload)
        if widget is None:
            pages.append({"page": n, "ok": False,
                          "note": "виджет webListReviews не найден",
                          "widget_states": sorted(
                              (payload.get("widgetStates") or {}).keys())[:20]})
            break
        revs = widget.get("reviews") or []
        ids, ks = _uuids(revs)
        key_stat = ks
        before = len(seen)
        seen.update(ids)
        pages.append({
            "page": n,
            "ok": True,
            "reviews": len(revs),
            "new_ids": len(seen) - before,
            "unique_total": len(seen),
            "has_next_page": bool((widget.get("nextPage") or "").strip()),
            "has_page_token": bool((widget.get("pageToken") or "").strip()),
            "seconds": round(time.monotonic() - ta, 2),
            "paging": _scalar_keys(widget.get("paging") or {},
                                   ("total", "from", "to", "count", "itemsPerPage")),
            "sortings": _scalar_keys(widget.get("sortings") or {}, ("title", "selected")),
        })
        if not revs or len(seen) == before:
            break
        nxt = (widget.get("nextPage") or "").strip()
        url = nxt if nxt else OF.reviews_url(pid, n + 1)
    return seen, {
        "pages": pages,
        "id_key": key_stat.get("id_key"),
        "reviews_without_id": key_stat.get("without_id"),
        "unique": len(seen),
        "seconds": round(time.monotonic() - t0, 1),
    }


def _raw_uuids(pid: str) -> set:
    """UUID из уже собранного canonical raw — бесплатный третий базис."""
    path = os.path.join("data", f"{pid}_raw.json")
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return set()
    return {str(r.get("uuid")) for r in (raw.get("reviews") or [])
            if isinstance(r, dict) and r.get("uuid")}


def _probe(sess, pid: str, api_pages: int, scrolls: int,
          target_min: int = 40, max_scrolls: int = 40) -> dict:
    page = sess.page
    url = f"https://www.ozon.ru/product/{pid}/?tab=reviews"
    out: dict = {"pid": pid, "url": url, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(GOTO_WAIT_MS)
        C.check_challenge(page, "probe_reviews_api")
        C._pass_age_gate(page)
        out["auth"] = C.detect_auth_state(page)
    except Exception as e:
        out["fatal"] = f"{type(e).__name__}: {e}"
        return out

    # Отзывы Ozon рендерятся лениво ниже фолда: без прокрутки главной
    # страницы [data-review-uuid] пуст (замер 2026-09-25: 0 карточек при
    # 10 с ожидания на ?tab=reviews). Тот же путь, что и в боевом сборе.
    t_scroll = time.monotonic()
    got_reviews = C._scroll_to_reviews(page, target_min=target_min,
                                       max_iters=max_scrolls)
    pre_scroll_s = round(time.monotonic() - t_scroll, 1)

    dom_ids, dom_stat = _dom_uuids(page, scrolls)
    api_ids, api_stat = _api_pages(page, pid, api_pages)
    raw_ids = _raw_uuids(pid)

    out["auth"] = C.detect_auth_state(page)
    out["dom"] = dom_stat
    out["dom"]["pre_scroll_found_reviews"] = bool(got_reviews)
    out["dom"]["pre_scroll_seconds"] = pre_scroll_s
    out["dom"]["pre_scroll_target_min"] = target_min
    out["api"] = api_stat
    out["raw"] = {"exists": bool(raw_ids), "unique": len(raw_ids)}
    out["compare"] = {
        "api_vs_dom": {
            "api_unique": len(api_ids),
            "dom_unique": len(dom_ids),
            "intersection": len(api_ids & dom_ids),
            "api_only": len(api_ids - dom_ids),
            "dom_only": len(dom_ids - api_ids),
        },
        "api_vs_raw": {
            "raw_unique": len(raw_ids),
            "intersection": len(api_ids & raw_ids),
            "api_only": len(api_ids - raw_ids),
        },
        "dom_vs_raw": {
            "intersection": len(dom_ids & raw_ids),
            "dom_only": len(dom_ids - raw_ids),
        },
    }
    # Скорость: отзывов в секунду по каждому пути.
    if dom_stat.get("seconds"):
        out["dom"]["reviews_per_s"] = round(dom_stat["unique"] / dom_stat["seconds"], 2)
    if api_stat.get("seconds"):
        out["api"]["reviews_per_s"] = round(api_stat["unique"] / api_stat["seconds"], 2)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="probe_reviews_api",
        description="P1-проба: JSON API отзывов против DOM (изолированный профиль)")
    p.add_argument("--pid", required=True, help="pid товара Ozon")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"CDP-порт пробы (по умолч. {DEFAULT_PORT})")
    p.add_argument("--profile", default=None, help="путь профиля-копии")
    p.add_argument("--no-copy", action="store_true", help="не создавать профиль-копию")
    p.add_argument("--force-copy", action="store_true", help="пересоздать профиль-копию")
    p.add_argument("--api-pages", type=int, default=4,
                   help="максимум страниц API (по умолч. 4)")
    p.add_argument("--scrolls", type=int, default=6,
                   help="итераций DOM-скролла после предскролла (по умолч. 6)")
    p.add_argument("--pre-scroll-iters", type=int, default=40,
                   help="бюджет прокрутки до отзывов, шаг SCROLL_STEP_PX (по умолч. 40)")
    p.add_argument("--out", default="data/probe_reviews_api.json",
                   help="путь JSON-отчёта")
    p.add_argument("--yes", action="store_true",
                   help="не спрашивать подтверждения (live-трафик!)")
    return p


def main() -> int:
    args = build_parser().parse_args()
    profile = args.profile or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "opencode",
        "ozon-profile-reviews-api-probe")
    if not args.no_copy:
        PV._copy_profile(profile, PV.MAIN_PROFILE, force=args.force_copy)
    if not args.yes:
        print("ВНИМАНИЕ: live-эксперимент, тратит живой бюджет Ozon "
              f"(pid={args.pid}, порт {args.port}).")
        try:
            input("Продолжить? [Enter] ")
        except KeyboardInterrupt:
            print("\nотменено")
            return 0
    PV._ensure_browser(args.port, profile)
    sess = cdp.Session(f"http://127.0.0.1:{args.port}",
                       profile_token="ozon-profile-reviews-api-probe")
    try:
        report = _probe(sess, args.pid, args.api_pages, args.scrolls,
                        max_scrolls=args.pre_scroll_iters)
    finally:
        try:
            sess.close()
        except Exception:
            pass
    report["port"] = args.port
    report["profile"] = profile
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nJSON-отчёт сохранён: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
