"""Ручная уборка кэша профилей Chromium (OZON_PROFILE_BASE).

Список каталогов и правила безопасности живут в ozon_collector/profile_gc.py —
этот скрипт только CLI-обёртка (тот же список нужен демону в preflight,
дублировать его опаснее лишней строчки).

Зачем: без уборки 9 профилей = 6738 МБ, 92.3% из них — воспроизводимый кэш.
Состояние сессии (Cookies/Local Storage/Sessions) — 78.6 МБ и не трогается.

Безопасность:
  - профиль с живым Chromium пропускается (удаление у живого Chromium ломает
    профиль: следующий запуск увидит битый кэш);
  - удаляется только белый список JUNK_DIRS; STATE_DIRS в него не входит;
  - dry-run по умолчанию: без --apply ничего не удаляется.

Запуск:
    python scripts/clean_profile_cache.py                  # dry-run, размеры
    python scripts/clean_profile_cache.py --apply          # удалить
    python scripts/clean_profile_cache.py --apply --all    # + не-озон профили
    python scripts/clean_profile_cache.py --min-mb 300     # порог "грязно"
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ozon_collector import process as P  # noqa: E402
from ozon_collector import profile_gc  # noqa: E402


def targets(base: str, all_profiles: bool) -> list[str]:
    """Каталоги-кандидаты: ozon-profile* — свои; --all — всё в base."""
    if not os.path.isdir(base):
        return []
    return [os.path.join(base, n) for n in sorted(os.listdir(base))
            if os.path.isdir(os.path.join(base, n))
            and (all_profiles or n.startswith("ozon-profile"))]


def main() -> int:
    ap = argparse.ArgumentParser(description="Уборка кэша профилей Chromium")
    ap.add_argument("--apply", action="store_true",
                    help="реально удалить (без флага — только dry-run)")
    ap.add_argument("--all", action="store_true",
                    help="чистить все профили в base, не только ozon-*")
    args = ap.parse_args()

    base = P.profile_base_dir()
    print(f"base: {base}")
    if not os.path.isdir(base):
        print(f"нет каталога {base}")
        return 1

    total_freed = 0.0
    skipped = 0
    for profile_dir in targets(base, args.all):
        name = os.path.basename(profile_dir)
        # Размер ДО уборки: при --apply он уже изменён, и «осталось» вышло бы
        # отрицательным (поймано на живом прогоне 07.10).
        total_mb = profile_gc.dir_size_mb(profile_dir)
        res = profile_gc.clean_profile(profile_dir, apply=args.apply)
        total_freed += res["freed_mb"]
        if res["skipped"]:
            skipped += 1
            print(f"{name:<28} ПРОПУЩЕН: живой Chromium на профиле")
            continue
        verb = "убрано" if args.apply else "мусор"
        print(f"{name:<28} {verb} {res['freed_mb']:7.1f} МБ "
              f"({res['dirs']} каталогов) из {total_mb:7.1f} МБ → "
              f"останется {total_mb - res['freed_mb']:7.1f} МБ")

    print()
    if skipped:
        print(f"ПРОПУЩЕНО профилей с живым браузером: {skipped} "
              f"(останови демон и повтори)")
    if not args.apply:
        print(f"DRY-RUN: освободится ~{total_freed:.1f} МБ. "
              f"С флагом --apply удалится по-настоящему.")
    else:
        print(f"DONE: освобождено {total_freed:.1f} МБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())