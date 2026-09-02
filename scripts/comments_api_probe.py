"""B0-пробник: перехват API rpGetCommentsByReviewUuid.

Задача: зафиксировать формат запроса/ответа API комментариев к отзывам
и способ привязки ответа к отзыву (reviewId/uuId), чтобы построить
устойчивый сбор комментариев через API (вместо hash-классов DOM).

Использует ИЗОЛИРОВАННЫЙ порт 9333 и профиль-токен probe — не трогает
боевой сбор. ЗАПУСКАТЬ ТОЛЬКО КОГДА БОЕВОЙ СБОР НЕАКТИВЕН.

Сохраняет в --out: all_requests.jsonl (все запросы с post_data),
interesting_responses.jsonl (тело ответов по ключевым url),
comments_dom.html (дамп блока комментариев после раскрытия),
probe.log. Останавливается при антибот-челлендже.

Использование:
    python scripts/comments_api_probe.py --pid 1032557475 \
        --out D:/Temp/opencode/comments_probe
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import gzip
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from ozon_collector import collect as C  # noqa: E402
from ozon_collector.cdp import Session  # noqa: E402
from ozon_collector.collect import CHALLENGE_TITLE_MARKERS  # noqa: E402

INTERESTING = (
    "rpGetCommentsByReviewUuid", "comment", "review", "feedback",
    "GetReviews", "GetComments", "oid", "sku",
)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Перехват API комментариев Ozon")
    ap.add_argument("--pid", required=True)
    ap.add_argument("--out", required=True, help="каталог для артефактов")
    ap.add_argument("--cdp-url", default="http://127.0.0.1:9333")
    ap.add_argument("--profile-token", default="probe",
                    help="токен профиля воркера (изолированный от боевого)")
    ap.add_argument("--max-wait-s", type=float, default=45.0,
                    help="сколько ждать карточки кнопок «N комментарий»")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    all_req_f = out / "all_requests.jsonl"
    int_resp_f = out / "interesting_responses.jsonl"
    all_req_f = open(all_req_f, "w", encoding="utf-8")
    int_resp_f = open(int_resp_f, "w", encoding="utf-8")

    def _log(msg: str):
        line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}"
        print(line)
        (out / "probe.log").open("a", encoding="utf-8").write(line + "\n")

    url = f"https://www.ozon.ru/product/{args.pid}?tab=reviews"
    _log(f"pid={args.pid} cdp={args.cdp_url} profile={args.profile_token}")

    sess = Session(cdp_url=args.cdp_url, profile_token=args.profile_token)
    page = sess.page
    stats = {"req": 0, "resp": 0, "interesting": 0}

    def _on_request(req):
        stats["req"] += 1
        rec = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "method": req.method,
            "url": req.url,
            "resource_type": getattr(req, "resource_type", None),
        }
        try:
            rec["post_data"] = req.post_data
        except Exception as e:
            rec["post_data_error"] = str(e)
        if any(k.lower() in req.url.lower() for k in INTERESTING):
            stats["interesting"] += 1
            all_req_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            all_req_f.flush()
            _log(f"REQ interesting: {req.method} {req.url[:200]}")

    def _on_response(resp):
        stats["resp"] += 1
        url_lower = resp.url.lower()
        if not any(k.lower() in url_lower for k in INTERESTING):
            return
        rec = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "status": resp.status,
            "url": resp.url,
            "content_type": resp.headers.get("content-type", ""),
        }
        try:
            body = resp.body()
        except Exception as e:
            rec["body_error"] = str(e)
            body = None
        if body is not None:
            rec["body_len"] = len(body)
            try:
                text = body.decode("utf-8", "replace")
                rec["body_preview"] = text[:4000]
            except Exception as e:
                rec["body_decode_error"] = str(e)
        int_resp_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        int_resp_f.flush()
        _log(f"RESP {resp.status} len={rec.get('body_len', '?')} "
             f"ct={rec.get('content_type', '?')} {resp.url[:140]}")

    page.on("request", _on_request)
    page.on("response", _on_response)

    try:
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        time.sleep(4.0)
        title = (page.title() or "").strip().lower()
        if any(m in title for m in CHALLENGE_TITLE_MARKERS):
            _log(f"ЧЕЛЛЕНДЖ вместо страницы (title={title[:60]!r}); стоп без "
                 f"ретраев. код=3")
            return 3

        # Скроллим страницу вниз порциями, пока не встретятся кнопки
        # «N комментарий» (или не упрёмся в предел).
        t0 = time.time()
        marked = 0
        while time.time() - t0 < args.max_wait_s:
            try:
                marked = page.evaluate(C.MARK_COMMENT_CARDS_JS) or 0
            except Exception as e:
                _log(f"MARK_COMMENT_CARDS_JS warn: {e}")
                marked = 0
            _log(f"помечено кнопок «N комментарий»: {marked}")
            if marked:
                break
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(600)

        if not marked:
            _log(f"за {args.max_wait_s}s кнопок «N комментарий» не найдено.")
        else:
            # Собственный кликер с диагностикой (не глотает ошибки как
            # _expand_review_comments): кликаем самую первую необработанную
            # кнопку в цикле, после каждого клика — пауза на API.
            clicks = 0
            t0 = time.time()
            while time.time() - t0 < 90:
                try:
                    btns = page.locator(
                        '[data-comments-expanded]:not([data-comments-clicked]) button',
                        has_text=re.compile(r'[1-9]\d*\s*комментар', re.IGNORECASE),
                    )
                    n_btns = btns.count()
                except Exception as e:
                    _log(f"count кнопок warn: {e}")
                    n_btns = 0
                _log(f"необработанных кнопок: {n_btns}")
                if n_btns == 0:
                    break
                btn = btns.first
                try:
                    btn.scroll_into_view_if_needed(timeout=5000)
                except Exception as e:
                    _log(f"scroll_into_view warn: {type(e).__name__}: {e}")
                try:
                    # пометить кликнутой до клика (как в боевом коде)
                    btn.evaluate(
                        "(el) => { const c = el.closest('[data-review-uuid]');"
                        " if (c) c.setAttribute('data-comments-clicked', '1'); }"
                    )
                    btn.click(timeout=8000)
                    _log("клик OK")
                except Exception as e:
                    _log(f"клик warn: {type(e).__name__}: {str(e)[:200]}")
                    # fallback JS-click
                    try:
                        btn.evaluate("(el) => el.click()")
                        _log("fallback JS-клик OK")
                    except Exception as e2:
                        _log(f"fallback warn: {type(e2).__name__}: {e2}")
                clicks += 1
                page.wait_for_timeout(1200)
            _log(f"кликов выполнено: {clicks}")

            # Дамп DOM, где есть комментарии
            try:
                dom = page.content()
                idx = dom.find("data-comment-uuid")
                _log(f"data-comment-uuid в DOM: {dom.count('data-comment-uuid')} "
                     f"(первый индекс {idx})")
                if idx > 0:
                    snippet = dom[max(0, idx - 3000):idx + 8000]
                    (out / "comments_dom_snippet.html").write_text(
                        snippet, encoding="utf-8")
                with gzip.open(out / "reviews_dom.html.gz", "wt",
                               encoding="utf-8") as f:
                    f.write(dom)
                _log("DOM сохранён: reviews_dom.html.gz")
            except Exception as e:
                _log(f"DOM дамп warn: {e}")

        page.wait_for_timeout(1000)
    finally:
        page.remove_listener("request", _on_request)
        page.remove_listener("response", _on_response)

    _log(f"Итог: req_total={stats['req']}, resp_total={stats['resp']}, "
         f"interesting={stats['interesting']}")
    all_req_f.close()
    int_resp_f.close()
    _log(f"готово: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())