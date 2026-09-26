#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Собрать Markdown-дайджест кампании из локального кеша, БЕЗ сети и браузера.

Зачем: демон пишет дайджест в финале прогона (`key_daemon._write_digest`).
Если прогон упал на финальной фазе (главный профиль занят, CDP не поднялся,
Ctrl+C после сбора), сырьё уже лежит в `data/*_raw*.json`, а дайджеста нет —
и повторный прогон ради файла не нужен: рендер идёт из кеша.

Использование:
    python scripts/digest_from_cache.py <campaign> [--dry-run] [--compact]

Пример:
    python scripts/digest_from_cache.py fonari_dlya_velosipedov

Что НЕ делает: не запускает Chromium, не ходит в сеть, не меняет манифест
кампании (`data/campaigns/<campaign>/progress.json` читается только на чтение).
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import key_daemon as KD  # noqa: E402


def accepted_pids(progress: dict) -> list[str]:
    """PID, принятые в финал: сначала accepted, потом valid (поле манифеста)."""
    for key in ("campaign_accepted_pids", "campaign_final_pids", "valid_pids"):
        pids = progress.get(key)
        if pids:
            return [str(p) for p in pids]
    return []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Дайджест кампании из кеша data/ (офлайн, без браузера).")
    ap.add_argument("campaign", help="имя кампании (как в data/campaigns/)")
    ap.add_argument("--out", default=None,
                    help="переопределить путь .md (по умолчанию progress['out'])")
    ap.add_argument("--compact", action="store_true",
                    help="дополнительно краткий дайджест *_compact.md")
    ap.add_argument("--dry-run", action="store_true",
                    help="только показать pid и путь, ничего не писать")
    args = ap.parse_args(argv)

    prog_path = ROOT / "data" / "campaigns" / args.campaign / "progress.json"
    if not prog_path.is_file():
        print(f"нет манифеста кампании: {prog_path}", file=sys.stderr)
        return 2
    progress = json.load(io.open(prog_path, encoding="utf-8"))

    pids = accepted_pids(progress)
    out = args.out or progress.get("out") or f"docs/digests/{args.campaign}.md"
    if not pids:
        print(f"в кампании {args.campaign} нет принятых PID — дайджест пуст, "
              f"писать нечего", file=sys.stderr)
        return 1

    print(f"кампания: {args.campaign}")
    print(f"принято PID: {len(pids)}")
    print(f"куда: {out}")
    print(f"compact: {bool(args.compact)}")
    if args.dry_run:
        print("dry-run: файл не тронут")
        return 0

    # Демон ждёт accepted-список в campaign_accepted_pids; в манифесте он
    # называется valid_pids — подкладываем в копию, файл не переписываем.
    # Путь отдаём абсолютным, чтобы не делать os.chdir (побочный эффект).
    shim = dict(progress)
    shim["campaign_accepted_pids"] = pids
    shim["out"] = str(ROOT / out)
    KD._write_digest(shim, str(ROOT / "data"),
                     compact=True if args.compact else None)

    out_path = ROOT / out
    if not out_path.is_file():
        print("дайджест не создан (проверь accepted PID и наличие raw в data/)",
              file=sys.stderr)
        return 1
    txt = io.open(out_path, encoding="utf-8").read()
    print(f"готово: {out} — {len(txt.encode('utf-8'))} байт, "
          f"{txt.count(chr(10))} строк")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
