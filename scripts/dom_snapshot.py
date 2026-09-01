"""Диагностический DOM-снапшот вкладки отзывов одного товара.

Зачем: офлайн-анализ baseline показал детерминированную потерю звёзд у
отзывов старше ~365 дней (260/260). Один снапшот отвечает на вопрос:
(A) Ozon сам не рендерит звёзды у архивных отзывов — тогда данные верны
и меняем семантику qa_raw/дайджеста; или (B) рендерит другой разметкой,
которую наш REVIEWS_JS не читает — тогда чиним селектор.

Что делает:
- подключается к CDP (по умолчанию ИЗОЛИРОВАННЫЙ порт 9333 и отдельный
  профиль-токен probe — не трогает боевой сбор на 9222);
- открывает {pid}?tab=reviews, ждёт контент;
- порциями прокручивает вкладку, после каждой пачки снимает page.content()
  и ищет в нём целевые uuid из --targets-file;
- останавливается, когда все цели найдены, при исчерпании --max-scrolls
  или при антибот-челлендже (без ретраев);
- сохраняет: dom_snapshot.html (последний контент), snapshot_report.json
  (какие uuid найдены, на каком шаге), scroll_log.jsonl (по шагу: длина
  контента и найденные uuid).

ЗАПУСКАТЬ ТОЛЬКО КОГДА БОЕВОЙ СБОР НЕАКТИВЕН.

Использование:
    python scripts/dom_snapshot.py --pid 144377998 \
        --targets-file D:/Temp/opencode/snap_targets_144377998.json \
        --out D:/Temp/opencode/snap_144377998
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from ozon_collector.cdp import Session  # noqa: E402
from ozon_collector.collect import CHALLENGE_TITLE_MARKERS  # noqa: E402


def _find_uuids(content: str, uuids) -> set:
    return {u for u in uuids if u and u in content}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="DOM-снапшот вкладки отзывов одного товара")
    ap.add_argument("--pid", required=True)
    ap.add_argument("--targets-file", required=True,
                    help="json: {'old_s0': [uuid], 'young_ok': [uuid]}")
    ap.add_argument("--out", required=True, help="каталог для артефактов")
    ap.add_argument("--cdp-url", default="http://127.0.0.1:9333")
    ap.add_argument("--profile-token", default="probe",
                    help="токен профиля воркера (изолированный от боевого)")
    ap.add_argument("--max-scrolls", type=int, default=60)
    ap.add_argument("--pause-s", type=float, default=1.5)
    args = ap.parse_args(argv)

    targets = json.loads(Path(args.targets_file).read_text(encoding="utf-8"))
    want_old = set(targets.get("old_s0") or [])
    want_young = set(targets.get("young_ok") or [])
    if not want_old or not want_young:
        raise SystemExit("dom_snapshot: в targets-file нужны old_s0 и young_ok")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    url = f"https://www.ozon.ru/product/{args.pid}?tab=reviews"
    print(f"[snap] pid={args.pid} cdp={args.cdp_url} "
          f"цели: old={len(want_old)} young={len(want_young)}")

    sess = Session(cdp_url=args.cdp_url, profile_token=args.profile_token)
    page = sess.page

    page.goto(url, timeout=60000, wait_until="domcontentloaded")
    time.sleep(4.0)

    title = (page.title() or "").strip().lower()
    if any(m in title for m in CHALLENGE_TITLE_MARKERS):
        print(f"[snap] ЧЕЛЛЕНДЖ вместо страницы (title={title[:60]!r}); "
              f"без ретраев — перезапуск вручную позже.")
        return 3

    log_lines = []
    found_old, found_young = set(), set()
    first_step = {}
    for step in range(1, args.max_scrolls + 1):
        content = page.content()
        o = _find_uuids(content, want_old - found_old)
        y = _find_uuids(content, want_young - found_young)
        for u in sorted(o):
            first_step.setdefault(u, ("old", step))
        for u in sorted(y):
            first_step.setdefault(u, ("young", step))
        found_old |= o
        found_young |= y
        log_lines.append(json.dumps({
            "step": step, "content_len": len(content),
            "found_old": sorted(o), "found_young": sorted(y),
            "total_old": len(found_old), "total_young": len(found_young),
        }, ensure_ascii=False))
        print(f"[snap] шаг {step}: len={len(content)} "
              f"+old={len(o)} +young={len(y)} | "
              f"итого old={len(found_old)}/{len(want_old)} "
              f"young={len(found_young)}/{len(want_young)}")
        if len(found_old) == len(want_old) and len(found_young) == len(want_young):
            print("[snap] все цели найдены")
            break
        page.evaluate(
            "window.scrollBy({top: Math.round(window.innerHeight * 1.8), "
            "behavior: 'instant'})")
        time.sleep(args.pause_s)

    content = page.content()
    (out / "dom_snapshot.html").write_text(content, encoding="utf-8")
    (out / "scroll_log.jsonl").write_text("\n".join(log_lines), encoding="utf-8")
    report = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "pid": args.pid,
        "url": url,
        "cdp_url": args.cdp_url,
        "profile_token": args.profile_token,
        "steps": len(log_lines),
        "targets_file": str(args.targets_file),
        "found_old": sorted(found_old),
        "missing_old": sorted(want_old - found_old),
        "found_young": sorted(found_young),
        "missing_young": sorted(want_young - found_young),
        "first_seen_step": {u: v for u, v in first_step.items()},
        "challenge_suspect": any(m in title for m in CHALLENGE_TITLE_MARKERS),
    }
    (out / "snapshot_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[snap] готово: {out / 'dom_snapshot.html'}")
    print(f"[snap] old найдено {len(found_old)}/{len(want_old)}, "
          f"young найдено {len(found_young)}/{len(want_young)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
