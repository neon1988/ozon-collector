"""Сбор одного товара: collect → ocr → report, с кешем raw.

run_gather(cfg, sess) — единая точка для одиночного сбора (CLI gather) и
поиска (collect_search): cache-hit берёт raw из data/{pid}_raw.json, cache-miss
собирает через collect. Внешняя CDP-сессия переиспользуется и не закрывается.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from . import VERSION, SCHEMA_VERSION
from . import browser as B
from . import cache as K
from . import collect as C
from . import log
from . import ocr as O
from . import report as R


def _save_raw(raw, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def run_gather(cfg: dict, sess=None) -> dict:
    """Сбор одного товара: collect → ocr → report, с кешем и без sys.exit.

    cfg: pid, url, variant, reviews_url, max_pages, min_reviews, category,
         no_dom, no_variant_mode, sorts, max_age_s, no_ocr, threads,
         no_preproc, out_raw (путь raw.json), gallery_dir.
    sess — внешняя CDP-сессия (передаётся в collect, НЕ закрывается).
    Возвращает dict: {pid, ok, error, raw, markdown, cache, ocr}.
    cache: off|miss|stale|hit; ocr: off|error|hit|ran.
    """
    pid = cfg["pid"]
    max_age_s = cfg.get("max_age_s", 0)
    result = {
        "pid": pid,
        "ok": False,
        "error": None,
        "raw": None,
        "markdown": "",
        "cache": "off",
        "ocr": "off",
    }
    try:
        raw, reason = K.cache_check(pid, max_age_s)
        result["cache"] = ("hit" if reason == "hit"
                           else "stale" if reason == "stale" else "miss")
        if reason == "hit":
            ver = raw.get("collector_version", "?")
            dt = K._parse_utc(raw.get("collected_at_utc"))
            age = ""
            if dt is not None:
                age = f", age={int((datetime.now(timezone.utc) - dt).total_seconds() // 60)}m"
            print(f"[gather] pid={pid}: cache=hit ({ver}{age})")
            log.info(f"pid={pid}: cache=hit ({ver}{age})")
        elif max_age_s <= 0:
            print(f"[gather] pid={pid}: cache=off (--max-age 0)")
            log.info(f"pid={pid}: cache=off (--max-age 0)")
        elif reason == "old_version":
            ver = K.cached_version(pid)
            print(f"[gather] pid={pid}: cache=miss (old_version {ver or '—'} < {VERSION})")
            log.info(f"pid={pid}: cache=miss (old_version {ver or '—'} < {VERSION})")
        elif reason == "newer_version":
            ver = K.cached_version(pid)
            print(f"[gather] pid={pid}: cache=miss (newer_version {ver or '—'} > {VERSION})")
            log.info(f"pid={pid}: cache=miss (newer_version {ver or '—'} > {VERSION})")
        elif reason == "incompatible":
            print(f"[gather] pid={pid}: cache=miss (schema != {SCHEMA_VERSION})")
            log.info(f"pid={pid}: cache=miss (schema != {SCHEMA_VERSION})")
        else:
            print(f"[gather] pid={pid}: cache=miss ({reason})")
            log.info(f"pid={pid}: cache=miss ({reason})")

        if raw is None:
            raw = C.collect(
                product_url=cfg["url"],
                reviews_url=cfg.get("reviews_url"),
                variant=cfg.get("variant") or "",
                max_pages=cfg.get("max_pages", 12),
                min_reviews=cfg.get("min_reviews", 40),
                out_path=cfg.get("out_raw"),
                category=cfg.get("category"),
                save_dom=not cfg.get("no_dom"),
                apply_variant_mode=not cfg.get("no_variant_mode"),
                sorts=cfg.get("sorts"),
                sess=sess,
            )
            print(f"[gather] pid={pid}: collect — {len(raw.get('reviews', []))} отзывов")
            log.info(f"pid={pid}: collect — {len(raw.get('reviews', []))} отзывов")
        result["raw"] = raw

        if not cfg.get("no_ocr"):
            if raw.get("gallery_ocr"):
                result["ocr"] = "hit"
                print(f"[gather] pid={pid}: ocr=hit (в кеше)")
                log.info(f"pid={pid}: ocr=hit (в кеше)")
            else:
                urls = (raw.get("product", {}).get("gallery")
                        or raw.get("product", {}).get("infographics"))
                if not urls:
                    result["ocr"] = "error"
                    print(f"[gather] pid={pid}: ocr=error (нет галереи/инфографики)")
                    log.warning(f"pid={pid}: ocr=error (нет галереи/инфографики)")
                else:
                    pid_ = raw.get("pid") or pid
                    gdir = cfg.get("gallery_dir") or os.path.join(
                        "data", f"gallery_{pid_}")
                    O.ocr_gallery(raw, gdir, threads=cfg.get("threads", 8),
                                  preproc=not cfg.get("no_preproc"))
                    raw_path = cfg.get("out_raw") or K.raw_path(pid_)
                    _save_raw(raw, raw_path)
                    result["ocr"] = "ran"
                    n_img = len(raw.get("gallery_ocr") or []) or len(urls)
                    print(f"[gather] pid={pid}: ocr=ran ({n_img} картинок)")
                    log.info(f"pid={pid}: ocr=ran ({n_img} картинок)")
        else:
            result["ocr"] = "off"
            print(f"[gather] pid={pid}: ocr=off")
            log.info(f"pid={pid}: ocr=off")

        result["markdown"] = R.render(
            raw, dedup_replies=cfg.get("dedup_replies", True))
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        cls = B.classify_error(e, sess=sess)
        result["error_kind"] = cls.error_kind
        result["error_probe_state"] = cls.probe_state
        result["error_probe_attempts"] = cls.probe_attempts
        result["error_signature"] = cls.signature
        result["recyclable"] = cls.should_recycle
        print(f"[gather] pid={pid}: error: {result['error']} "
              f"(kind={cls.error_kind})")
        log.error(f"pid={pid}: error: {result['error']} "
                  f"(kind={cls.error_kind}, probe={cls.probe_state}, "
                  f"recyclable={cls.should_recycle})")
    return result
