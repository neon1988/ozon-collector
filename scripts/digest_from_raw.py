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
    python scripts/digest_from_raw.py --pids 1234567,2345678 --out d.md
    python scripts/digest_from_raw.py --pids-file pids.txt --out d.md
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows-консоль (cp1251) не кодирует ₽ из отзывов — переключаем stdout на UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from cli import parse_pids
from ozon_collector.report import (build_search_digest_combined,
                                   minimum_header_size_bytes,
                                   render_search_digest,
                                   render_search_digest_compact,
                                   write_search_digest,
                                   write_search_digest_compact)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out", default=None,
                    help="файл для дайджеста (по умолчанию печать в stdout)")
    ap.add_argument("--query", default="роботы пылесосы (офлайн, из raw)")
    ap.add_argument("--pids", default=None,
                    help="артикулы через запятую (можно URL карточек) — "
                         "только их и обрабатывать")
    ap.add_argument("--pids-file", default=None,
                    help="файл со списком артикулов/URL (по одному в строке)")
    ap.add_argument("--max-file-size-mb", type=float, default=3.0,
                    help="макс. размер одного md-файла дайджеста в МиБ "
                         "(1 МиБ = 1024² байт; по умолч. 3.0; должен быть > 0, "
                         "иначе разбиение невозможно)")
    ap.add_argument("--compact", action="store_true",
                    help="создать краткий дайджест {root}_compact.md: карточка "
                         "товара + цена + характеристики + итог звёзд, БЕЗ "
                         "текстов отзывов/описаний/OCR. С --out пишет и полный, "
                         "и краткий файл; без --out печатает краткий в stdout")
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

    only = set()
    if args.pids:
        only |= set(parse_pids(args.pids.split(",")))
    if args.pids_file:
        try:
            with open(args.pids_file, encoding="utf-8") as f:
                only |= set(parse_pids(f.read().splitlines()))
        except OSError as e:
            print(f"Error: не могу прочитать --pids-file: {e}", file=sys.stderr)
            return 1
    if only:
        pids &= only

    combined, table = build_search_digest_combined(
        pids, args.data_dir,
        query=(None if only else args.query))
    ok_n = combined["collected"]
    print(f"pid'ов: {len(pids)}, из них с валидными данными: {ok_n}")
    print("\n".join(table))

    if args.out:
        if args.max_file_size_mb <= 0:
            ap.error("--max-file-size-mb должен быть > 0")
        max_bytes = int(args.max_file_size_mb * 1024 * 1024)
        min_bytes = minimum_header_size_bytes(combined)
        if args.max_file_size_mb and max_bytes < min_bytes:
            ap.error(
                f"--max-file-size-mb={args.max_file_size_mb} даёт {max_bytes} байт, "
                f"что меньше минимального размера шапки ({min_bytes} байт)")
        written = write_search_digest(
            combined, args.out, max_file_size_bytes=max_bytes)
        for path in written:
            size = os.path.getsize(path) / 1024
            print(f"\nДайджест сохранён: {path} ({size:.2f} КБ)")
        if args.compact:
            root, ext = os.path.splitext(args.out)
            compact_out = f"{root}_compact{ext}"
            write_search_digest_compact(combined, compact_out, full_out=args.out)
            size = os.path.getsize(compact_out) / 1024
            print(f"Краткий дайджест сохранён: {compact_out} ({size:.2f} КБ)")
    else:
        if args.compact:
            md = render_search_digest_compact(combined)
        else:
            md = render_search_digest(combined)
        print("\n" + md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
