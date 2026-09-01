#!/usr/bin/env python3
"""CLI для однократного сбора данных с Ozon (отладочные/офлайновые команды).

ПОИСК выдачи (query/url/pids) здесь НЕ запускается: его выполняет оркестратор
(scripts/key_daemon.py, in-process _inproc_collect) и рассылает товары
воркерам. Эта команда остаётся утилитой: offline-qa (ocr/report) и сбор
конкретного товара (collect/gather).

Примеры:
  python3 cli.py collect --pid 3627230434 --variant 'Цвет:белый' --category headphones
  python3 cli.py ocr    --raw data/3627230434_raw.json
  python3 cli.py report --raw data/3627230434_raw.json --out docs/headphones_xiaomi_redmi_buds8.md
  python3 cli.py gather --pid 3627230434 --variant 'Цвет:белый' --out docs/xiaomi.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ozon_collector import collect as C
from ozon_collector import ocr as O
from ozon_collector import report as R
from ozon_collector import cache as K
from ozon_collector import log
from ozon_collector.gather import run_gather

# Сортировки отзывов по умолчанию: сначала «новые и полезные» (published_at_desc),
# затем «сначала низкие оценки» (score_asc) — негативные отзывы попадают в сбор,
# дедуп по uuid убирает дубли между проходами.
DEFAULT_SORTS = "published_at_desc,score_asc"


def _parse_sorts(value):
    """Парсит --sorts 'a,b,c' в список (None при пустом значении)."""
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_pids(raw):
    """Разбирает сырые токены (артикулы/URL Ozon) в список уникальных PID.

    Принимает голые артикулы ('3627230434') и полные URL выдачи/карточки
    ('https://www.ozon.ru/product/xiaomi-...-3627230434' или
    '/product/3627230434/'). Нечисловые токены пропускаются.
    """
    pids, seen = [], set()

    def add(tok):
        t = (tok or "").strip()
        if not t:
            return
        t = t.split("#")[0].split("?")[0].rstrip("/")
        if "/product/" in t:
            m = re.search(r"(?:/product/|\D)(\d+)$", t)
            p = m.group(1) if m else None
        else:
            p = t if t.isdigit() else None
        if p and p not in seen:
            seen.add(p)
            pids.append(p)

    for tok in raw:
        add(tok)
    return pids


def _apply_scroll_jitter_seed(args):
    """Применяет детерминированный seed jitter-пауз (None = случайный)."""
    seed = getattr(args, "scroll_jitter_seed", None)
    C.set_scroll_jitter_seed(seed)
    effective = C.effective_scroll_jitter_seed()
    log.info(f"[cli] scroll jitter seed: {effective} "
             f"({'задан' if seed is not None else 'случайный'})")
    return effective


def cmd_collect(args):
    url = args.url or f"https://www.ozon.ru/product/{args.pid}"
    _apply_scroll_jitter_seed(args)
    log.info(f"[cli] collect: url={url} variant={args.variant!r} "
             f"sorts={args.sorts!r} max_pages={args.max_pages} "
             f"min_reviews={args.min_reviews}")
    raw = C.collect(
        product_url=url,
        reviews_url=args.reviews_url,
        variant=args.variant or "",
        max_pages=args.max_pages,
        min_reviews=args.min_reviews,
        out_path=args.out,
        category=args.category,
        save_dom=not args.no_dom,
        apply_variant_mode=not args.no_variant_mode,
        sorts=_parse_sorts(args.sorts),
    )
    log.info(f"[cli] DONE. reviews={raw['reviews_collected']}, "
             f"out=data/{C.pid_from_url(url)}_raw.json")


def cmd_ocr(args):
    raw = json.load(open(args.raw, encoding="utf-8"))
    pid = args.pid or C.pid_from_url(raw.get("product_url", ""))
    gdir = args.gallery_dir or os.path.join("data", f"gallery_{pid}")
    log.info(f"[cli] ocr: raw={args.raw} pid={pid} gdir={gdir} "
             f"threads={args.threads} preproc={not args.no_preproc}")
    O.ocr_gallery(raw, gdir, threads=args.threads, preproc=not args.no_preproc)
    with open(args.raw, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    log.info(f"[cli] OCR: {raw.get('gallery_stats_text')}; "
             f"DONE. raw обновлён: {args.raw}")


def cmd_report(args):
    raw = json.load(open(args.raw, encoding="utf-8"))
    log.info(f"[cli] report: raw={args.raw} out={args.out}")
    md = R.render(raw, dedup_replies=not args.no_dedup_replies)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        log.info(f"[cli] SAVED {args.out}")
    else:
        sys.stdout.write(md)


def cmd_gather(args):
    url = args.url or f"https://www.ozon.ru/product/{args.pid}"
    pid = C.pid_from_url(url)
    _apply_scroll_jitter_seed(args)
    try:
        max_age_s = K.parse_max_age(args.max_age)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    cfg = {
        "pid": pid,
        "url": url,
        "reviews_url": args.reviews_url,
        "variant": args.variant,
        "max_pages": args.max_pages,
        "min_reviews": args.min_reviews,
        "category": args.category,
        "out_raw": args.out_raw,
        "no_dom": args.no_dom,
        "no_variant_mode": args.no_variant_mode,
        "sorts": _parse_sorts(args.sorts),
        "max_age_s": max_age_s,
        "no_ocr": not args.ocr,
        "threads": args.threads,
        "no_preproc": args.no_preproc,
        "gallery_dir": args.gallery_dir,
        "overwrite": args.overwrite,
    }
    log.info(f"[cli] gather: pid={pid} variant={args.variant!r} "
             f"sorts={cfg['sorts']!r} max_age_s={max_age_s} "
             f"ocr={args.ocr}")
    res = run_gather(cfg)
    if not res["ok"]:
        log.error(f"[cli] Error: {res['error']} "
                  f"(kind={res.get('error_kind')})", stderr=True)
        sys.exit(1)
    log.info(f"[cli] gather OK pid={pid}: cache={res['cache']} "
             f"ocr={res['ocr']}")
    md = res["markdown"]
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        log.info(f"[cli] SAVED {args.out}")
    else:
        sys.stdout.write(md)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ozon-collector")
    p.add_argument("--log", default=None,
                   help="путь файла лога прогона (по умолч. logs/ozon_collector.log; "
                        "ротация при каждом запуске: предыдущий — logs/ozon_collector.log.1)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("collect", help="сбор карточки + отзывов варианта (CDP)")
    pc.add_argument("--pid", help="Ozon ID товара")
    pc.add_argument("--url", help="полный URL товара")
    pc.add_argument("--variant", default="",
                    help="'ДЛИНАxШИРИНАxВЫСОТА' (напр. 200x90x6) или 'ключ:значение' (напр. 'Цвет:белый')")
    pc.add_argument("--reviews-url", help="URL страницы отзывов (по умолчанию ?tab=reviews)")
    pc.add_argument("--max-pages", type=int, default=12)
    pc.add_argument("--min-reviews", type=int, default=40,
                    help="минимум отзывов на странице (для бесконечного скролла), по умолч. 40")
    pc.add_argument("--category", help="категория товара (сохраняется в product.category)")
    pc.add_argument("--out", help="путь raw.json (по умолчанию data/{pid}_raw.json)")
    pc.add_argument("--no-dom", action="store_true",
                    help="не сохранять DOM-снимок (data/{pid}/dom/…)")
    pc.add_argument("--no-variant-mode", action="store_true",
                    help="не включать нативный фильтр «Этот вариант товара» (собрать все отзывы)")
    pc.add_argument("--sorts", default=DEFAULT_SORTS,
                    help=f"сортировки отзывов через запятую: published_at_desc,score_desc,score_asc "
                         f"(по умолчанию: {DEFAULT_SORTS} — новые и полезные + сначала низкие оценки)")
    pc.add_argument("--scroll-jitter-seed", type=int, default=None,
                    help="детерминированный seed jitter-пауз скролла (для воспроизводимых прогонов; "
                         "по умолчанию случайный)")
    pc.set_defaults(fn=cmd_collect)

    po = sub.add_parser("ocr", help="OCR галереи в raw.json")
    po.add_argument("--raw", required=True)
    po.add_argument("--pid")
    po.add_argument("--gallery-dir")
    po.add_argument("--threads", type=int, default=8,
                    help="кол-во процессов OCR (по умолч. 8)")
    po.add_argument("--no-preproc", action="store_true",
                    help="без cv2-предобработки (grayscale/upscale/CLAHE)")
    po.set_defaults(fn=cmd_ocr)

    pr = sub.add_parser("report", help="рендер markdown-отчёта (без --out — в stdout)")
    pr.add_argument("--raw", required=True)
    pr.add_argument("--out")
    pr.add_argument("--no-dedup-replies", action="store_true",
                    help="показывать все ответы продавца, без дедупа повторяющихся (по умолч. дедуп включён)")
    pr.set_defaults(fn=cmd_report)

    pg = sub.add_parser("gather", help="collect → ocr → report одним вызовом (отчёт в stdout без --out)")
    pg.add_argument("--pid", help="Ozon ID товара")
    pg.add_argument("--url", help="полный URL товара")
    pg.add_argument("--variant", default="",
                    help="'200x90x6' или 'Цвет:белый'")
    pg.add_argument("--reviews-url", help="URL страницы отзывов (по умолчанию ?tab=reviews)")
    pg.add_argument("--max-pages", type=int, default=12)
    pg.add_argument("--min-reviews", type=int, default=40)
    pg.add_argument("--category", help="категория товара (сохраняется в product.category)")
    pg.add_argument("--out", help="путь markdown-отчёта (по умолчанию stdout)")
    pg.add_argument("--out-raw", help="путь raw.json (по умолчанию data/{pid}_raw.json)")
    pg.add_argument("--gallery-dir", help="каталог скачанной галереи")
    pg.add_argument("--threads", type=int, default=8,
                    help="кол-во процессов OCR (по умолч. 8)")
    pg.add_argument("--no-preproc", action="store_true",
                    help="без cv2-предобработки (grayscale/upscale/CLAHE)")
    pg.add_argument("--no-dom", action="store_true", help="не сохранять DOM-снимок")
    pg.add_argument("--ocr", action="store_true",
                    help="запустить OCR (tesseract) — по умолчанию выключен")
    pg.add_argument("--no-variant-mode", action="store_true",
                    help="не включать нативный фильтр «Этот вариант товара» (собрать все отзывы)")
    pg.add_argument("--overwrite", action="store_true",
                    help="разрешить перезапись существующего data/{pid}_raw.json "
                         "(по умолчанию новый прогон пишется в data/{pid}_raw.<ts>.<i>.json)")
    pg.add_argument("--sorts", default=DEFAULT_SORTS,
                    help=f"сортировки отзывов через запятую: published_at_desc,score_desc,score_asc "
                         f"(по умолчанию: {DEFAULT_SORTS} — новые и полезные + сначала низкие оценки)")
    pg.add_argument("--max-age", default="7d",
                    help="макс. возраст кеша raw: '7d', '1d', '12h', '6h'; '0' — кеш выключен (по умолч. 7d)")
    pg.add_argument("--no-dedup-replies", action="store_true",
                    help="показывать все ответы продавца, без дедупа повторяющихся (по умолч. дедуп включён)")
    pg.add_argument("--scroll-jitter-seed", type=int, default=None,
                    help="детерминированный seed jitter-пауз скролла (для воспроизводимых прогонов; "
                         "по умолчанию случайный)")
    pg.set_defaults(fn=cmd_gather)

    return p


def main():
    # Перекодировать stdout/stderr в UTF-8 ДО argparse: иначе help/usage
    # (с кириллицей и стрелкой →) падают с UnicodeEncodeError в cp1251-консоли
    # (Windows), когда вывод редиректится в файл/pipe.
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
    p = build_parser()
    args = p.parse_args()
    if args.log:
        os.environ["OZON_COLLECTOR_LOG"] = args.log
    log.setup()
    args.fn(args)


if __name__ == "__main__":
    main()
