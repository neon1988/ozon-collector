"""Сбор одного товара: collect → ocr → report, с кешем raw.

run_gather(cfg, sess) — единая точка для одиночного сбора (CLI gather) и
поиска (collect_search): cache-hit берёт raw из data/{pid}_raw.json, cache-miss
собирает через collect. Внешняя CDP-сессия переиспользуется и не закрывается.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from . import VERSION, SCHEMA_VERSION
from . import browser as B
from . import cache as K
from . import collect as C
from . import log
from . import ocr as O
from . import report as R


def _delete_raw_file(path):
    """Удалить битый (короткий) raw, чтобы не попал в кеш/дайджест."""
    try:
        os.remove(path)
    except OSError:
        pass


def _save_raw(raw, path):
    """Атомарная запись raw (tmp→os.replace): canonical всегда целый файл.

    Читатель (QA-demon, cache) никогда не увидит частично записанный raw —
    исчезает race «CLI пишет raw / демон QA читает raw» (raw ownership).
    """
    K.atomic_write_json(path, raw)


def assert_raw_structure(raw, pid) -> None:
    """Acceptance-gate: ok только для успешной карточки с валидной структурой.

    Проверки: raw — dict; pid raw совпадает с запрошенным; product — непустой
    dict; product.title — непустой текст (не challenge-маркер); reviews —
    список (0 отзывов допустимо и НЕ является ошибкой).

    Битая структура → StructureError (не recyclable, ретрай бесполезен).
    Challenge-заглушка в product.title → AccessChallengeError (recyclable:
    retry с recycle браузера). Gate применяется и к кеш-хиту, и к свежему
    сбору — challenge никогда не проходит в products как успех.
    """
    if not isinstance(raw, dict):
        raise B.StructureError("raw не является dict")
    if str(raw.get("pid") or "") != str(pid):
        raise B.StructureError(
            f"pid raw={raw.get('pid')!r} != ожидаемый {pid!r}")
    product = raw.get("product")
    if not isinstance(product, dict) or not product:
        raise B.StructureError("product отсутствует или пуст")
    title = (product.get("title") or "").strip()
    if not title:
        raise B.StructureError("product.title пуст")
    low = title.lower()
    m = C.title_challenge_marker(low)
    if m:
        raise B.AccessChallengeError(f"анти-бот (product.title): {m}")
    # Слайдер-пазл: его h1 («Сопоставьте пазл, двигая ползунок») не входит в
    # CHALLENGE_TITLE_MARKERS (это текст внутри страницы, а не <title>), но
    # собран как название товара. Стоп-фразы — защита от капчи в product.title.
    for m in ("сопоставьте пазл", "сопоставляйте пазл", "двигая ползунок"):
        if m in low:
            raise B.AccessChallengeError(f"анти-бот (product.title): {m}")
    if not isinstance(raw.get("reviews"), list):
        raise B.StructureError("reviews не является списком")


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
    overwrite = cfg.get("overwrite", False)
    out_raw = cfg.get("out_raw") or K.raw_save_path(pid, overwrite=overwrite)
    canon = K.raw_path(pid)
    if out_raw != canon and os.path.exists(canon):
        log.warning(f"[gather] pid={pid}: {canon} уже существует — "
                    f"новый прогон в {os.path.basename(out_raw)} "
                    f"(перезапись только с --overwrite)")
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
        raw, reason = K.cache_check(pid, max_age_s, sorts=cfg.get("sorts"))
        result["cache"] = ("hit" if reason == "hit"
                           else "stale" if reason == "stale" else "miss")
        if reason == "hit":
            ver = raw.get("collector_version", "?")
            dt = K._parse_utc(raw.get("collected_at_utc"))
            age = ""
            if dt is not None:
                age = f", age={int((datetime.now(timezone.utc) - dt).total_seconds() // 60)}m"
            src = (raw.get("collected_at_utc") or "?")[:19]
            revs = int(raw.get("reviews_collected") or 0)
            log.info(f"[gather] pid={pid}: cache=hit (v{ver}, reviews={revs}, from={src}{age})")
        elif max_age_s <= 0:
            log.info(f"[gather] pid={pid}: cache=off (--max-age 0)")
        elif reason == "old_version":
            ver = K.cached_version(pid)
            log.info(f"[gather] pid={pid}: cache=miss (old_version {ver or '—'} < {VERSION})")
        elif reason == "newer_version":
            ver = K.cached_version(pid)
            log.info(f"[gather] pid={pid}: cache=miss (newer_version {ver or '—'} > {VERSION})")
        elif reason == "incompatible":
            log.info(f"[gather] pid={pid}: cache=miss (schema != {SCHEMA_VERSION})")
        else:
            log.info(f"[gather] pid={pid}: cache=miss ({reason})")

        if raw is not None:
            problems = C.variant_review_problems(raw)
            if problems:
                log.warning(f"[gather] pid={pid}: кеш-хит с браком "
                            f"({'; '.join(problems)}) — игнорирую кеш, "
                            f"пересобираю живьём")
                raw = None
                result["cache"] = "miss"

        if raw is None:
            raw = C.collect(
                product_url=cfg["url"],
                reviews_url=cfg.get("reviews_url"),
                variant=cfg.get("variant") or "",
                max_pages=cfg.get("max_pages", 12),
                min_reviews=cfg.get("min_reviews", 40),
                out_path=out_raw,
                category=cfg.get("category"),
                save_dom=not cfg.get("no_dom"),
                apply_variant_mode=not cfg.get("no_variant_mode"),
                sorts=cfg.get("sorts"),
                sess=sess,
                skip_reviews=cfg.get("skip_reviews", False),
                reviews_api=cfg.get("reviews_api"),
            )
            log.info(f"[gather] pid={pid}: collect — {len(raw.get('reviews', []))} отзывов")
            # Проверки объёма отзывов неприменимы в режиме без отзывов
            # (reviews намеренно пустые, а не брак сбора).
            problems = ([] if cfg.get("skip_reviews")
                        else C.variant_review_problems(raw))
            if problems:
                _delete_raw_file(out_raw)
                raise B.VariantShortError(
                    f"брак числа отзывов: {'; '.join(problems)}")
        result["raw"] = raw
        assert_raw_structure(raw, pid)

        if not cfg.get("no_ocr"):
            if raw.get("gallery_ocr"):
                result["ocr"] = "hit"
                log.info(f"[gather] pid={pid}: ocr=hit (в кеше)")
            else:
                urls = (raw.get("product", {}).get("gallery")
                        or raw.get("product", {}).get("infographics"))
                if not urls:
                    result["ocr"] = "error"
                    log.warning(f"[gather] pid={pid}: ocr=error (нет галереи/инфографики)")
                else:
                    pid_ = raw.get("pid") or pid
                    gdir = cfg.get("gallery_dir") or os.path.join(
                        "data", f"gallery_{pid_}")
                    O.ocr_gallery(raw, gdir, threads=cfg.get("threads", 8),
                                  preproc=not cfg.get("no_preproc"))
                    _save_raw(raw, out_raw)
                    result["ocr"] = "ran"
                    n_img = len(raw.get("gallery_ocr") or []) or len(urls)
                    log.info(f"[gather] pid={pid}: ocr=ran ({n_img} картинок)")
        else:
            result["ocr"] = "off"
            log.info(f"[gather] pid={pid}: ocr=off")

        result["markdown"] = R.render(
            raw, dedup_replies=cfg.get("dedup_replies", True))
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        cls = B.classify_error(e, sess=sess)
        result["error_kind"] = cls.error_kind
        result["failure_type"] = cls.failure_type
        result["error_probe_state"] = cls.probe_state
        result["error_probe_attempts"] = cls.probe_attempts
        result["error_signature"] = cls.signature
        result["recyclable"] = cls.should_recycle
        log.error(f"[gather] pid={pid}: error: {result['error']} "
                  f"(kind={cls.error_kind}, probe={cls.probe_state}, "
                  f"recyclable={cls.should_recycle})")
    return result
