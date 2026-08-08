#!/usr/bin/env python3
"""CLI для однократного сбора данных с Ozon.

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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ozon_collector import collect as C
from ozon_collector import ocr as O
from ozon_collector import report as R
from ozon_collector import cache as K
from ozon_collector import search as S
from ozon_collector.gather import run_gather


def _parse_sorts(value):
    """Парсит --sorts 'a,b,c' в список (None при пустом значении)."""
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def cmd_collect(args):
    url = args.url or f"https://www.ozon.ru/product/{args.pid}"
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
    print(f"DONE. reviews={raw['reviews_collected']}, "
          f"out=data/{C.pid_from_url(url)}_raw.json")


def cmd_ocr(args):
    raw = json.load(open(args.raw, encoding="utf-8"))
    pid = args.pid or C.pid_from_url(raw.get("product_url", ""))
    gdir = args.gallery_dir or os.path.join("data", f"gallery_{pid}")
    O.ocr_gallery(raw, gdir, threads=args.threads, preproc=not args.no_preproc)
    with open(args.raw, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    print("OCR:", raw.get("gallery_stats_text"))
    print("DONE. raw обновлён:", args.raw)


def cmd_report(args):
    raw = json.load(open(args.raw, encoding="utf-8"))
    md = R.render(raw, dedup_replies=not args.no_dedup_replies)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"SAVED {args.out}")
    else:
        sys.stdout.write(md)


def cmd_gather(args):
    url = args.url or f"https://www.ozon.ru/product/{args.pid}"
    pid = C.pid_from_url(url)
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
        "no_ocr": args.no_ocr,
        "threads": args.threads,
        "no_preproc": args.no_preproc,
        "gallery_dir": args.gallery_dir,
    }
    res = run_gather(cfg)
    if not res["ok"]:
        print(f"Error: {res['error']}", file=sys.stderr)
        sys.exit(1)
    print(f"[gather] {pid}: cache={res['cache']} ocr={res['ocr']}")
    md = res["markdown"]
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"SAVED {args.out}")
    else:
        sys.stdout.write(md)


def _gather_cfg_from_args(args, max_age_s):
    """Базовый gather-конфиг из аргументов search (общий для всех товаров)."""
    return {
        "variant": args.variant,
        "max_pages": args.max_pages,
        "min_reviews": args.min_reviews,
        "category": args.category,
        "no_dom": args.no_dom,
        "no_variant_mode": args.no_variant_mode,
        "sorts": _parse_sorts(args.sorts),
        "max_age_s": max_age_s,
        "no_ocr": args.no_ocr,
        "threads": args.threads,
        "no_preproc": args.no_preproc,
        "dedup_replies": not args.no_dedup_replies,
    }


def cmd_search(args):
    from ozon_collector.cdp import Session

    if args.url:
        url = args.url
    elif args.query:
        url = S.search_url(args.query)
    else:
        print("Error: нужен --query или --url", file=sys.stderr)
        sys.exit(1)
    try:
        max_age_s = K.parse_max_age(args.max_age)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    sess = Session()
    try:
        combined = S.collect_search(
            sess, url, limit=args.limit,
            gather_cfg=_gather_cfg_from_args(args, max_age_s),
            query=args.query,
        )
    finally:
        sess.close()

    md = R.render_search_digest(
        combined, reviews_per_product=args.reviews_per_product,
        dedup_replies=not args.no_dedup_replies)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"SAVED {args.out}")
    else:
        sys.stdout.write(md)

    if args.out_raw:
        os.makedirs(os.path.dirname(args.out_raw) or ".", exist_ok=True)
        with open(args.out_raw, "w", encoding="utf-8") as f:
            json.dump(combined, f, ensure_ascii=False, indent=2)
        print(f"SAVED {args.out_raw}")

    ok = sum(1 for p in combined["products"] if p["ok"])
    print(f"[search] found={combined['found']} limit={combined['limit']} "
          f"collected={combined['collected']} ok={ok}")


def main():
    p = argparse.ArgumentParser(prog="ozon-collector")
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
    pc.add_argument("--sorts", default=None,
                    help="сортировки отзывов через запятую: published_at_desc,score_desc,score_asc "
                         "(по умолчанию — одна сортировка «новые и полезные»)")
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
    pg.add_argument("--no-ocr", action="store_true",
                    help="пропустить OCR (быстрый прогон, без tesseract)")
    pg.add_argument("--no-variant-mode", action="store_true",
                    help="не включать нативный фильтр «Этот вариант товара» (собрать все отзывы)")
    pg.add_argument("--sorts", default=None,
                    help="сортировки отзывов через запятую: published_at_desc,score_desc,score_asc "
                         "(по умолчанию — одна сортировка «новые и полезные»)")
    pg.add_argument("--max-age", default="1d",
                    help="макс. возраст кеша raw: '1d', '12h', '6h'; '0' — кеш выключен (по умолч. 1d)")
    pg.add_argument("--no-dedup-replies", action="store_true",
                    help="показывать все ответы продавца, без дедупа повторяющихся (по умолч. дедуп включён)")
    pg.set_defaults(fn=cmd_gather)

    ps = sub.add_parser(
        "search",
        help="сбор выдачи по запросу/URL → markdown-дайджест для LLM (CDP)")
    ps.add_argument("--query", help="текстовый поисковый запрос")
    ps.add_argument("--url", help="URL выдачи Ozon (/search/ или /category/…)")
    ps.add_argument("--limit", type=int, default=5,
                    help="макс. товаров для сбора (по умолч. 5)")
    ps.add_argument("--reviews-per-product", type=int, default=15,
                    help="макс. отзывов на товар в дайджесте (по умолч. 15)")
    ps.add_argument("--variant", default="", help="'200x90x6' или 'Цвет:белый'")
    ps.add_argument("--max-pages", type=int, default=12)
    ps.add_argument("--min-reviews", type=int, default=40)
    ps.add_argument("--category", help="категория товара (сохраняется в product.category)")
    ps.add_argument("--out", help="путь markdown-дайджеста (по умолчанию stdout)")
    ps.add_argument("--out-raw", help="путь combined-json (схема 2.0)")
    ps.add_argument("--threads", type=int, default=8,
                    help="кол-во процессов OCR (по умолч. 8)")
    ps.add_argument("--no-preproc", action="store_true",
                    help="без cv2-предобработки (grayscale/upscale/CLAHE)")
    ps.add_argument("--no-dom", action="store_true", help="не сохранять DOM-снимок")
    ps.add_argument("--no-ocr", action="store_true",
                    help="пропустить OCR (быстрый прогон, без tesseract)")
    ps.add_argument("--no-variant-mode", action="store_true",
                    help="не включать нативный фильтр «Этот вариант товара» (собрать все отзывы)")
    ps.add_argument("--sorts", default=None,
                    help="сортировки отзывов через запятую: published_at_desc,score_desc,score_asc "
                         "(по умолчанию — одна сортировка «новые и полезные»)")
    ps.add_argument("--max-age", default="1d",
                    help="макс. возраст кеша raw: '1d', '12h', '6h'; '0' — кеш выключен (по умолч. 1d)")
    ps.add_argument("--no-dedup-replies", action="store_true",
                    help="показывать все ответы продавца, без дедупа повторяющихся (по умолч. дедуп включён)")
    ps.set_defaults(fn=cmd_search)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
