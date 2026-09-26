#!/usr/bin/env python3
"""P1b-проба: каким запросом Ozon догружает следующие отзывы.

Контекст: tests/probe_reviews_api.py показал, что синтезированный GET
entrypoint-api отдаёт ровно 4 отзыва, без nextPage/pageToken, и
layout_page_index=2 возвращает ту же первую четвёрку. Значит настоящая
пагинация отзывов ходит другим запросом (ожидаем composer-api, POST с телом),
и его форму надо снять, а не угадать.

Проба ничего не меняет: перехват только читает URL/тело/статус запросов,
страница прокручивается как обычно. Отчёт — JSON со списком запросов.

Использование:
  python tests/probe_reviews_req_shape.py --pid 210502516
  python tests/probe_reviews_req_shape.py --pid 210502516 --max-scrolls 14 --yes
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

import probe_variant_stats as PV  # noqa: E402

from ozon_collector import cdp  # noqa: E402
from ozon_collector import collect as C  # noqa: E402

DEFAULT_PORT = 9337
GOTO_WAIT_MS = 12000
SETTLE_MS = 1800
# Что считаем «интересным» запросом: служебные ответы Ozon (аналитика, счётчики,
# баннеры) в отчёт не берём — нужен только транспорт отзывов.
INTERESTING = ("entrypoint-api", "composer-api", "reviewshelfpaginator",
               "webListReviews", "reviews")
MAX_BODY = 3000


def _is_interesting(url: str) -> bool:
    low = url.lower()
    return any(marker in low for marker in INTERESTING)


class _Recorder:
    """Сбор запросов/ответов по маршрутам отзывов.

    Тела ответов в обработчике НЕ читаем: в sync-API Playwright event-handler
    нельзя вызывать блокирующие методы (response.text() → дедлок). Объекты
    Response копятся и разбираются после прокрутки.
    """

    def __init__(self, max_responses: int = 8):
        self.requests: list[dict] = []
        self.responses: dict[int, dict] = {}
        self.pending: list = []
        self.max_responses = max_responses

    def on_request(self, req) -> None:
        try:
            url = req.url
        except Exception:
            return
        if not _is_interesting(url):
            return
        body = None
        try:
            body = req.post_data
        except Exception:
            body = None
        self.requests.append({
            "t": round(time.monotonic(), 2),
            "method": req.method,
            "url": url,
            "post_data": (body[:MAX_BODY] if body else None),
            "post_len": (len(body) if body else 0),
        })

    def on_response(self, resp) -> None:
        try:
            if not _is_interesting(resp.url):
                return
            self.responses[id(resp.request)] = {
                "url": resp.url,
                "status": resp.status,
            }
            if len(self.pending) < self.max_responses:
                self.pending.append(resp)
        except Exception:
            pass


def _widget_summary(resp) -> dict | None:
    """Разбор тела ответа: сколько отзывов и какие id (если это виджет отзывов)."""
    from ozon_collector import ozon_fetch as OF
    try:
        text = resp.text()
    except Exception as e:
        return {"url": resp.url, "error": f"{type(e).__name__}: {e}"[:200]}
    try:
        payload = json.loads(text)
    except Exception:
        return {"url": resp.url, "error": "not JSON", "len": len(text)}
    widget = OF.find_reviews_widget(payload)
    if widget is None:
        states = payload.get("widgetStates") if isinstance(payload, dict) else None
        return {"url": resp.url, "widget": None,
                "widget_states": sorted(states.keys())[:15] if isinstance(states, dict) else None}
    revs = widget.get("reviews") or []
    ids = {str(r.get("uuid")) for r in revs
           if isinstance(r, dict) and r.get("uuid")}
    return {
        "url": resp.url,
        "widget": True,
        "reviews": len(revs),
        "unique_uuids": len(ids),
        "uuids": sorted(ids)[:60],
        "has_next_page": bool((widget.get("nextPage") or "").strip()),
        "has_page_token": bool((widget.get("pageToken") or "").strip()),
        "paging_total": (widget.get("paging") or {}).get("total")
        if isinstance(widget.get("paging"), dict) else None,
    }


def _page_token_params(url: str) -> dict:
    """Разобрать параметры вложенного url= из entrypoint-запроса."""
    import urllib.parse as up
    out: dict = {}
    try:
        outer = up.parse_qs(up.urlparse(url).query)
        inner = (outer.get("url") or [""])[0]
        q = up.parse_qs(up.urlparse(inner).query)
        for k in ("layout_container", "layout_page_index", "sh",
                  "start_page_id", "tab"):
            if k in q:
                out[k] = q[k][0]
    except Exception:
        pass
    return out


def _replay(page, base_url: str, count: int) -> list[dict]:
    """Повторить пойманный запрос отзывов N раз, подставляя layout_page_index.

    Смысл: без start_page_id Ozon игнорирует индекс и отдаёт первую четвёрку.
    С реальными токенами страницы следующие страницы должны дать новые uuid.
    """
    from ozon_collector import ozon_fetch as OF
    import urllib.parse as up
    out = []
    base_index = int(_page_token_params(base_url).get("layout_page_index") or 1)
    for n in range(count):
        idx = base_index + n
        url = re.sub(r"layout_page_index%3D\d+",
                     f"layout_page_index%3D{idx}", base_url)
        t0 = time.monotonic()
        payload = OF.fetch_json(page, url)
        dt = round(time.monotonic() - t0, 2)
        widget = OF.find_reviews_widget(payload) if payload else None
        if widget is None:
            out.append({"layout_page_index": idx, "ok": False, "seconds": dt})
            continue
        revs = widget.get("reviews") or []
        ids = {str(r.get("uuid")) for r in revs
               if isinstance(r, dict) and r.get("uuid")}
        out.append({
            "layout_page_index": idx, "ok": True, "seconds": dt,
            "reviews": len(revs), "unique_uuids": len(ids),
            "uuids": sorted(ids)[:60],
            "has_next_page": bool((widget.get("nextPage") or "").strip()),
            "paging_total": (widget.get("paging") or {}).get("total")
            if isinstance(widget.get("paging"), dict) else None,
        })
    return out


def _cards(page) -> int:
    try:
        return int(page.evaluate(
            "() => document.querySelectorAll('[data-review-uuid]').length") or 0)
    except Exception:
        return -1


def _probe(sess, pid: str, max_scrolls: int, replay_pages: int = 2) -> dict:
    page = sess.page
    rec = _Recorder()
    page.on("request", rec.on_request)
    page.on("response", rec.on_response)

    url = f"https://www.ozon.ru/product/{pid}/?tab=reviews"
    out: dict = {"pid": pid, "url": url,
                 "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(GOTO_WAIT_MS)
        C.check_challenge(page, "probe_reviews_req_shape")
        C._pass_age_gate(page)
        out["auth"] = C.detect_auth_state(page)
    except Exception as e:
        out["fatal"] = f"{type(e).__name__}: {e}"
        return out

    # Скроллим вниз шагом окна: виджет отзывов ленивый, монтируется около y≈2800.
    timeline, seen_cards = [], -1
    for step in range(1, max_scrolls + 1):
        page.evaluate("() => window.scrollBy(0, Math.round(window.innerHeight * 0.9))")
        page.wait_for_timeout(SETTLE_MS)
        n = _cards(page)
        timeline.append({"scroll": step, "cards": n})
        if n > 0 and n == seen_cards and step > 3:
            # Карточки перестали расти — ждём один-два шага на подгрузку.
            stalled = sum(1 for t in timeline[-3:] if t["cards"] == n)
            if stalled >= 3:
                break
        seen_cards = n

    out["timeline"] = timeline
    out["cards_final"] = _cards(page)
    out["requests"] = rec.requests
    out["requests_count"] = len(rec.requests)
    out["responses"] = list(rec.responses.values())

    # Тела ответов по маршрутам отзывов: сколько отзывов Ozon реально прислал
    # и какие uuid — сравниваем с тем, что отрисовано в DOM (4 карточки).
    bodies = []
    for resp in rec.pending:
        summary = _widget_summary(resp)
        if summary:
            summary["params"] = _page_token_params(resp.url)
            bodies.append(summary)
    out["bodies"] = bodies

    # Replay пойманного paginator-запроса: главный вопрос P1.
    paginator = [r["url"] for r in rec.requests
                 if "reviewshelfpaginator" in r["url"]]
    if paginator and replay_pages:
        out["paginator_url"] = paginator[-1]
        out["paginator_params"] = _page_token_params(paginator[-1])
        out["replay"] = _replay(page, paginator[-1], replay_pages)
        dom_ids = set(page.evaluate(C.REVIEW_IDS_JS) or [])
        for row in out["replay"]:
            new = set(row.get("uuids") or []) - dom_ids
            row["new_vs_dom"] = len(new)
    else:
        out["paginator_url"] = None
        out["replay"] = []
    out["dom_uuids"] = sorted(dom_ids)[:60] if paginator and replay_pages else []
    # Компактно: уникальные шаблоны URL (без query) + методы.
    shapes: dict[str, dict] = {}
    for r in rec.requests:
        base = r["url"].split("?")[0]
        key = f"{r['method']} {base}"
        entry = shapes.setdefault(key, {"count": 0, "sample_post": None,
                                        "sample_url": None, "statuses": []})
        entry["count"] += 1
        if entry["sample_post"] is None and r["post_data"]:
            entry["sample_post"] = r["post_data"]
        if entry["sample_url"] is None:
            entry["sample_url"] = r["url"]
    for resp in rec.responses.values():
        key = "GET " + resp["url"].split("?")[0]
        if key in shapes:
            shapes[key]["statuses"].append(resp["status"])
    out["shapes"] = shapes
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="probe_reviews_req_shape",
        description="P1b-проба: форма запроса догрузки отзывов (изолированный профиль)")
    p.add_argument("--pid", required=True, help="pid товара Ozon")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"CDP-порт пробы (по умолч. {DEFAULT_PORT})")
    p.add_argument("--profile", default=None, help="путь профиля-копии")
    p.add_argument("--no-copy", action="store_true", help="не создавать профиль-копию")
    p.add_argument("--max-scrolls", type=int, default=14,
                   help="итераций прокрутки (по умолч. 14)")
    p.add_argument("--replay-pages", type=int, default=2,
                   help="сколько страниц переиграть пойманным запросом (0 — нет)")
    p.add_argument("--out", default="data/probe_reviews_req_shape.json",
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
        PV._copy_profile(profile, PV.MAIN_PROFILE)
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
        report = _probe(sess, args.pid, args.max_scrolls, args.replay_pages)
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
    print(json.dumps(report, ensure_ascii=False, indent=2)[:12000])
    print(f"\nJSON-отчёт сохранён: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
