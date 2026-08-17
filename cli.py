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
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ozon_collector import collect as C
from ozon_collector import ocr as O
from ozon_collector import report as R
from ozon_collector import cache as K
from ozon_collector import search as S
from ozon_collector import log
from ozon_collector.gather import run_gather


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
    print(f"DONE. reviews={raw['reviews_collected']}, "
          f"out=data/{C.pid_from_url(url)}_raw.json")
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
    print("OCR:", raw.get("gallery_stats_text"))
    print("DONE. raw обновлён:", args.raw)
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
        print(f"SAVED {args.out}")
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
        "no_ocr": args.no_ocr,
        "threads": args.threads,
        "no_preproc": args.no_preproc,
        "gallery_dir": args.gallery_dir,
        "overwrite": args.overwrite,
    }
    log.info(f"[cli] gather: pid={pid} variant={args.variant!r} "
             f"sorts={cfg['sorts']!r} max_age_s={max_age_s} "
             f"no_ocr={args.no_ocr}")
    res = run_gather(cfg)
    if not res["ok"]:
        print(f"Error: {res['error']}", file=sys.stderr)
        log.error(f"[cli] gather FAILED pid={pid}: {res['error']} "
                  f"(kind={res.get('error_kind')})", stderr=True)
        sys.exit(1)
    print(f"[gather] {pid}: cache={res['cache']} ocr={res['ocr']}")
    log.info(f"[cli] gather OK pid={pid}: cache={res['cache']} "
             f"ocr={res['ocr']}")
    md = res["markdown"]
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"SAVED {args.out}")
        log.info(f"[cli] SAVED {args.out}")
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
        "overwrite": args.overwrite,
        "min_rating": getattr(args, "min_rating", None),
    }


def cmd_search(args):
    from ozon_collector.cdp import Session
    from ozon_collector.parallel import collect_parallel

    pids = []
    if args.pids:
        pids += parse_pids(args.pids.split(","))
    if args.pids_file:
        try:
            with open(args.pids_file, encoding="utf-8") as f:
                pids += parse_pids(f.read().splitlines())
        except OSError as e:
            print(f"Error: не могу прочитать --pids-file: {e}", file=sys.stderr)
            sys.exit(1)
    pids = parse_pids(pids)

    if pids:
        limit = len(pids)
        url = f"список из {limit} артикулов: " + ", ".join(pids)
    else:
        limit = args.limit
        if args.url:
            url = args.url
        elif args.query:
            url = S.search_url(args.query)
        else:
            print("Error: нужен --query, --url или --pids/--pids-file",
                  file=sys.stderr)
            sys.exit(1)
    try:
        max_age_s = K.parse_max_age(args.max_age)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    parallel = args.parallel
    from ozon_collector import PARALLEL_MAX_WORKERS
    if not (1 <= parallel <= PARALLEL_MAX_WORKERS):
        print(f"Error: --parallel должен быть в 1..{PARALLEL_MAX_WORKERS}",
              file=sys.stderr)
        sys.exit(1)
    if parallel > 1 and not args.no_ocr and not args.allow_parallel_ocr:
        print(f"Error: OCR в параллельном сборе требует --allow-parallel-ocr "
              f"(или --no-ocr)", file=sys.stderr)
        sys.exit(1)
    if not (5 <= args.worker_timeout <= 3600):
        print("Error: --worker-timeout должен быть в 5..3600 секунд",
              file=sys.stderr)
        sys.exit(1)

    seed = _apply_scroll_jitter_seed(args)
    log.info(f"[cli] search: url={url} limit={limit} "
             f"query={args.query!r} max_age_s={max_age_s} "
             f"parallel={parallel} pids={len(pids)}")
    gather_cfg = _gather_cfg_from_args(args, max_age_s)
    gather_cfg["scroll_jitter_seed"] = seed
    if parallel > 1:
        if not args.no_bootstrap_workers:
            from ozon_collector.process import bootstrap_worker_profiles
            bs = bootstrap_worker_profiles(range(1, parallel + 1))
            for k, st in bs.items():
                print(f"[parallel] bootstrap worker-{k}: {st}")
                log.info(f"[parallel] bootstrap worker-{k}: {st}")
        if args.allow_parallel_ocr:
            import os as _os
            gather_cfg["threads"] = max(2, (_os.cpu_count() or 2) // parallel)
        combined = collect_parallel(
            url, limit=limit, gather_cfg=gather_cfg,
            parallel=parallel, query=args.query,
            max_recycles=2, join_timeout_s=args.worker_timeout,
            pids=pids or None)
    else:
        sess = Session()
        try:
            combined = S.collect_search(
                sess, url, limit=limit,
                gather_cfg=gather_cfg,
                query=args.query,
                pids=pids or None,
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
        log.info(f"[cli] SAVED {args.out}")
    else:
        sys.stdout.write(md)

    if args.out_raw:
        os.makedirs(os.path.dirname(args.out_raw) or ".", exist_ok=True)
        with open(args.out_raw, "w", encoding="utf-8") as f:
            json.dump(combined, f, ensure_ascii=False, indent=2)
        print(f"SAVED {args.out_raw}")
        log.info(f"[cli] SAVED {args.out_raw}")

    ok = sum(1 for p in combined["products"] if p["ok"])
    excluded = sum(1 for p in combined["products"] if p.get("excluded"))
    print(f"[search] found={combined['found']} limit={combined['limit']} "
          f"collected={combined['collected']} ok={ok} excluded={excluded}")
    log.info(f"[cli] search DONE: found={combined['found']} "
             f"limit={combined['limit']} collected={combined['collected']} "
             f"ok={ok} excluded={excluded}")

    aborted = combined.get("aborted")
    if aborted:
        extra = ""
        if "workers_stormed" in aborted:
            extra = (f" (workers_stormed={aborted['workers_stormed']}, "
                     f"all_workers={aborted.get('all_workers')})")
        print(f"[search] ПРОГОН ОСТАНОВЛЕН: challenge storm{extra}. "
              "Уйди в cooldown 2+ часа перед повторным запуском.",
              file=sys.stderr)
        log.error(f"[cli] search ABORTED: challenge storm "
                  f"{aborted.get('reason')}: "
                  f"streak={aborted.get('streak')}, "
                  f"remaining_aborted={aborted.get('remaining_aborted')}")


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
    pc.add_argument("--sorts", default=None,
                    help="сортировки отзывов через запятую: published_at_desc,score_desc,score_asc "
                         "(по умолчанию — одна сортировка «новые и полезные»)")
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
    pg.add_argument("--no-ocr", action="store_true",
                    help="пропустить OCR (быстрый прогон, без tesseract)")
    pg.add_argument("--no-variant-mode", action="store_true",
                    help="не включать нативный фильтр «Этот вариант товара» (собрать все отзывы)")
    pg.add_argument("--overwrite", action="store_true",
                    help="разрешить перезапись существующего data/{pid}_raw.json "
                         "(по умолчанию новый прогон пишется в data/{pid}_raw.<ts>.<i>.json)")
    pg.add_argument("--sorts", default=None,
                    help="сортировки отзывов через запятую: published_at_desc,score_desc,score_asc "
                         "(по умолчанию — одна сортировка «новые и полезные»)")
    pg.add_argument("--max-age", default="7d",
                    help="макс. возраст кеша raw: '7d', '1d', '12h', '6h'; '0' — кеш выключен (по умолч. 7d)")
    pg.add_argument("--no-dedup-replies", action="store_true",
                    help="показывать все ответы продавца, без дедупа повторяющихся (по умолч. дедуп включён)")
    pg.add_argument("--scroll-jitter-seed", type=int, default=None,
                    help="детерминированный seed jitter-пауз скролла (для воспроизводимых прогонов; "
                         "по умолчанию случайный)")
    pg.set_defaults(fn=cmd_gather)

    ps = sub.add_parser(
        "search",
        help="сбор выдачи по запросу/URL → markdown-дайджест для LLM (CDP)")
    ps.add_argument("--query", help="текстовый поисковый запрос")
    ps.add_argument("--url", help="URL выдачи Ozon (/search/ или /category/…)")
    ps.add_argument("--pids", default=None,
                    help="артикулы через запятую (можно полные URL карточек "
                         "'https://www.ozon.ru/product/<pid>'); гибрид: "
                         "кеш + досбор без докрутки выдачи")
    ps.add_argument("--pids-file", default=None,
                    help="файл со списком артикулов/URL (по одному в строке; "
                         "совместим с --pids)")
    ps.add_argument("--limit", type=int, default=5,
                    help="макс. товаров для сбора (по умолч. 5)")
    ps.add_argument("--reviews-per-product", type=int,
                    default=R.DEFAULT_REVIEWS_PER_PRODUCT,
                    help=f"макс. отзывов на товар в дайджесте "
                         f"(по умолч. {R.DEFAULT_REVIEWS_PER_PRODUCT})")
    ps.add_argument("--min-rating", type=float,
                    default=S.DEFAULT_MIN_RATING,
                    help="игнорировать товары с рейтингом ниже значения "
                         "(по умолч. 4.7; 0 — отключить фильтр)")
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
    ps.add_argument("--parallel", type=int, default=1,
                    help="кол-во воркер-процессов для search (1..N; "
                         "по умолчанию лимит 2, выше — OZON_PARALLEL_MAX; "
                         "каждый со своим Chromium)")
    ps.add_argument("--allow-parallel-ocr",
                    action="store_true",
                    help="разрешить OCR в параллельном сборе "
                         "(потоки OCR на воркера уменьшаются)")
    ps.add_argument("--worker-timeout", type=float, default=3600.0,
                    help="макс. время ожидания воркеров, сек (по умолч. 3600)")
    ps.add_argument("--no-bootstrap-workers", action="store_true",
                    help="не сеять cookies из ozon-profile в холодные "
                         "воркер-профили (по умолч. автоматически при --parallel>1)")
    ps.add_argument("--no-variant-mode", action="store_true",
                    help="не включать нативный фильтр «Этот вариант товара» (собрать все отзывы)")
    ps.add_argument("--overwrite", action="store_true",
                    help="разрешить перезапись существующего data/{pid}_raw.json "
                         "(по умолчанию новый прогон пишется в data/{pid}_raw.<ts>.<i>.json)")
    ps.add_argument("--sorts", default=None,
                    help="сортировки отзывов через запятую: published_at_desc,score_desc,score_asc "
                         "(по умолчанию — одна сортировка «новые и полезные»)")
    ps.add_argument("--max-age", default="7d",
                    help="макс. возраст кеша raw: '7d', '1d', '12h', '6h'; '0' — кеш выключен (по умолч. 7d)")
    ps.add_argument("--no-dedup-replies", action="store_true",
                    help="показывать все ответы продавца, без дедупа повторяющихся (по умолч. дедуп включён)")
    ps.add_argument("--scroll-jitter-seed", type=int, default=None,
                    help="детерминированный seed jitter-пауз скролла (для воспроизводимых прогонов; "
                         "по умолчанию случайный)")
    ps.set_defaults(fn=cmd_search)

    return p


def main():
    p = build_parser()
    args = p.parse_args()
    if args.log:
        os.environ["OZON_COLLECTOR_LOG"] = args.log
    log.setup()
    args.fn(args)


if __name__ == "__main__":
    main()
