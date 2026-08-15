"""Офлайн-дайджест выдачи из уже собранных raw-файлов (без обращений к Ozon).

Зачем: боевой прогон 2026-08-15 попал под FAB-челлендж и записал challenge-
страницы («Похоже, нет соединения», 0 отзывов) как успех. Хорошие данные
(v1.14.0, 75-96 отзывов) остались в data/{pid}_raw.<ts>.<i>.json от ранних
прогонов (overwrite-защита). Скрипт выбирает лучший валидный raw на каждый pid
(критерии: версия → дата сбора → число отзывов) с проверками:
- базовая валидность (cache._validate_raw: полный, schema, версия коллектора);
- реальный title (без анти-бот-сигнатур CHALLENGE_TITLE_MARKERS);
- отзывы > 0 (дайджесту нужны реальные данные);
и рендерит Markdown-дайджест через report.render_search_digest.

Использование:
    python scripts/digest_from_raw.py [--data-dir data] [--out data/digest_from_raw.md]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ozon_collector import cache as K
from ozon_collector.collect import CHALLENGE_TITLE_MARKERS
from ozon_collector.report import render_search_digest


def _best_for_digest(pid: str, data_dir: str):
    """Лучший raw pid для дайджеста.

    Возвращает (raw|None, path|None, reason): reason == "ok" при валидном
    кандидате; иначе причина отклонения (missing / challenge / no_reviews /
    old_version / incompatible / incomplete / bad_timestamp).
    """
    best = None
    best_path = None
    best_key = None
    reason = "missing"
    for path in K._raw_candidates(pid, data_dir):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            continue
        r = K._validate_raw(raw, pid)
        if r is not None:
            reason = r
            continue
        title = (raw.get("product") or {}).get("title") or ""
        t = title.strip().lower()
        if any(m in t for m in CHALLENGE_TITLE_MARKERS):
            reason = "challenge"
            continue
        n = int(raw.get("reviews_collected") or 0)
        if n <= 0:
            reason = "no_reviews"
            continue
        dt = K._parse_utc(raw.get("collected_at_utc"))
        key = (
            K._version_tuple(raw.get("collector_version")) or (0,),
            dt.timestamp() if dt is not None else 0.0,
            n,
        )
        if best is None or key > best_key:
            best, best_path, best_key = raw, path, key
    if best is None:
        return None, None, reason
    return best, best_path, "ok"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out", default=None,
                    help="файл для дайджеста (по умолчанию печать в stdout)")
    ap.add_argument("--query", default="роботы пылесосы (офлайн, из raw)")
    args = ap.parse_args(argv)

    pids = set()
    try:
        names = os.listdir(args.data_dir)
    except OSError:
        names = []
    for n in names:
        pid = None
        if n.endswith("_raw.json"):
            pid = n[: -len("_raw.json")]
        elif "_raw." in n and n.endswith(".json"):
            pid = n.split("_raw.", 1)[0]
        if pid is not None and pid.isdigit():
            pids.add(pid)

    products = []
    table = []
    for pid in sorted(pids):
        raw, path, reason = _best_for_digest(pid, args.data_dir)
        if raw is None:
            products.append({"ok": False, "pid": pid,
                             "error": f"нет валидных данных ({reason})"})
            table.append(f"{pid:>12}  SKIP  {reason}")
            continue
        n = int(raw.get("reviews_collected") or 0)
        ver = raw.get("collector_version") or "?"
        title = (raw.get("product") or {}).get("title") or ""
        title_short = (title[:70] + "…") if len(title) > 70 else title
        table.append(f"{pid:>12}  {n:>3} отз.  {ver:>6}  "
                     f"{os.path.basename(path):<38} {title_short}")
        products.append({
            "ok": True,
            "pid": pid,
            "raw": raw,
            "cache": f"raw:{os.path.basename(path)}",
        })

    ok_n = sum(1 for p in products if p.get("ok"))
    print(f"pid'ов: {len(pids)}, из них с валидными данными: {ok_n}")
    print("\n".join(table))

    from datetime import datetime, timezone
    combined = {
        "search": {"query": args.query},
        "collected_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "found": len(pids),
        "collected": ok_n,
        "products": products,
    }
    md = render_search_digest(combined)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        size = os.path.getsize(args.out) / 1024
        print(f"\nДайджест сохранён: {args.out} ({size:.2f} КБ)")
    else:
        print("\n" + md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
