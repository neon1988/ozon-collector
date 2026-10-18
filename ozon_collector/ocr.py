"""OCR галереи товара (tesseract rus) и инфографики.

Зависимости: pillow, pytesseract (+ бинарь tesseract-ocr с rus).
Опционально: opencv-python (cv2) для консервативной предобработки
(grayscale → upscale → CLAHE). Без cv2 предобработка отключается,
кэш-ключ меняется (preproc_ver=none), поведение = прежнее.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import List, Optional

import pytesseract
from PIL import Image, ImageStat

try:  # опциональный ML-стек для предобработки (numpy приходит с cv2)
    import numpy as _np
    import cv2 as _cv2
    CV2_AVAILABLE = True
except ImportError:
    _np = None
    _cv2 = None
    CV2_AVAILABLE = False

from . import log

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.7922.34 Safari/537.36"

VIDEO_RE = re.compile(r"/video-[^/]+/[^/]+/cover/", re.I)
MIN_BYTES = 20 * 1024
MIN_STDDEV = 6.0

# --- Версионирование кэша OCR -------------------------------------------------
# Ключ кэша: {md5}_{lang}_{psm}_{preproc_ver}_{tessdata_ver}_schema{N}.json
# Любое изменение движка/предобработки/структуры payload = новый ключ =
# автоматическая инвалидация старых результатов.
CACHE_SCHEMA = 2
PREPROC_VER = "cv2v1"  # версия алгоритма _preprocess
PREPROC_NONE = "none"
PREPROC_TARGET_LONG = 2000  # апскейл по длинной стороне, если меньше
PREPROC_CLIP_LIMIT = 2.0

# --- PSM-стратегия ------------------------------------------------------------
# psm 3 (авто) — первичный; если результат слабый (conf < 60 или text_len < 10),
# пробуем psm 11 (sparse text, лучше для баннеров) и берём лучший.
PSM_FALLBACK = 11
CONF_MIN = 60.0
TEXT_LEN_MIN = 10
# Per-image tesseract timeout (seconds); a hung process must not freeze the
# whole OCR batch (audit 2026-10-18, B5). Env override: OZON_OCR_TIMEOUT_S.
try:
    TESSERACT_TIMEOUT_S = int(os.environ.get("OZON_OCR_TIMEOUT_S", "60"))
except ValueError:
    TESSERACT_TIMEOUT_S = 60

TESSDATA_BEST_URL = "https://github.com/tesseract-ocr/tessdata_best/raw/main/{lang}.traineddata"


def _fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def dedup(urls: List[str]) -> List[str]:
    """Дедупликация и фильтрация ссылок галереи."""
    seen, out = set(), []
    for u in urls:
        if VIDEO_RE.search(u):
            continue
        key = re.sub(r"/wc\d+/", "/wc1000/", u)
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def _upgrade_url(url: str) -> str:
    """Берёт максимальное доступное разрешение картинки Ozon (wc1200)."""
    return re.sub(r"/wc\d+/", "/wc1200/", url)


def _has_pixels(fp: str) -> bool:
    """Отбрасывает не-картинки и монохромные (stddev ниже порога)."""
    try:
        with Image.open(fp) as im:
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            small = im.resize((80, 60))
            gray = small.convert("L")
            stat = ImageStat.Stat(gray)
            return stat.stddev[0] >= MIN_STDDEV
    except Exception:
        return False


def _md5(fp: str) -> str:
    h = hashlib.md5()
    try:
        with open(fp, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except Exception:
        return ""
    return h.hexdigest()


def image_metadata(fp: str) -> dict:
    """Метаданные изображения: width/height/size_bytes/md5."""
    try:
        with Image.open(fp) as im:
            return {
                "width": im.width,
                "height": im.height,
                "size_bytes": os.path.getsize(fp),
                "md5": _md5(fp),
            }
    except Exception:
        return {}


def _preprocess(arr) -> Optional["_np.ndarray"]:
    """Консервативная предобработка: gray → upscale до 2000px → CLAHE.

    Без агрессивного denoise/бинаризации (не терять тонкие подписи,
    логотипы). Возвращает 2D grayscale uint8 ndarray.
    """
    if not CV2_AVAILABLE:
        return None
    gray = _cv2.cvtColor(arr, _cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    long_side = max(h, w)
    if long_side < PREPROC_TARGET_LONG:
        scale = PREPROC_TARGET_LONG / float(long_side)
        gray = _cv2.resize(
            gray,
            (int(round(w * scale)), int(round(h * scale))),
            interpolation=_cv2.INTER_CUBIC,
        )
    clahe = _cv2.createCLAHE(clipLimit=PREPROC_CLIP_LIMIT, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _run_tesseract(fp: str, lang: str, psm: int, preproc: bool,
                   tessdata_dir: Optional[str]) -> tuple[str, float]:
    """Запуск tesseract на одном файле. Возвращает (text, mean_conf).

    Конвейер: PIL open → (опц.) cv2-предобработка → pytesseract
    image_to_data (psm задан). Слова собираются с conf >= 40.
    """
    with Image.open(fp) as im:
        img = im
        if preproc and CV2_AVAILABLE:
            arr = _np.asarray(im.convert("RGB"))
            proc = _preprocess(arr)
            if proc is not None:
                img = proc
        config = f"--psm {psm}"
        if tessdata_dir:
            config += f" --tessdata-dir {tessdata_dir}"
        d = pytesseract.image_to_data(
            img, lang=lang, config=config,
            output_type=pytesseract.Output.DICT,
            timeout=TESSERACT_TIMEOUT_S,
        )
    words, confs = [], []
    for i, w in enumerate(d["text"]):
        w = (w or "").strip()
        if not w:
            continue
        try:
            c = float(d["conf"][i])
        except Exception:
            c = 0.0
        if c >= 40:
            words.append(w)
            confs.append(c)
    text = " ".join(words)
    mean_conf = round(sum(confs) / len(confs), 1) if confs else 0.0
    return text, mean_conf


def cache_key(md5: str, lang: str, psm: int, preproc_ver: str,
              tessdata_ver: str, cache_schema: int = CACHE_SCHEMA) -> str:
    """Составной ключ кэша OCR."""
    return f"{md5}_{lang}_{psm}_{preproc_ver}_{tessdata_ver}_schema{cache_schema}.json"


def _load_cache(cache_path: str) -> Optional[dict]:
    try:
        with open(cache_path, encoding="utf-8") as f:
            c = json.load(f)
    except Exception:
        return None
    if c.get("cache_schema") != CACHE_SCHEMA:
        return None
    if c.get("text") is None:
        return None
    return c


def _save_cache(cache_path: str, payload: dict) -> None:
    """Атомарная запись кэша: mkstemp в той же директории + os.replace."""
    d = os.path.dirname(cache_path)
    os.makedirs(d, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(
            prefix=os.path.basename(cache_path) + ".", suffix=".tmp",
            dir=d,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, cache_path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _accepted(text: str, conf: float) -> bool:
    """Приемлем ли результат первичного PSM (conf >= 60 И text_len >= 10)."""
    return conf >= CONF_MIN and len(text) >= TEXT_LEN_MIN


def _better(text_a: str, conf_a: float, text_b: str, conf_b: float) -> bool:
    """True, если (text_b, conf_b) лучше (text_a, conf_a)."""
    if text_a and not text_b:
        return False
    if text_b and not text_a:
        return True
    return conf_b > conf_a


def _ocr_psm(fp: str, md5: str, cache_dir: str, lang: str, psm: int,
             preproc_ver: str, preproc: bool, tessdata_dir: Optional[str],
             tessdata_ver: str) -> tuple[str, float, bool]:
    """OCR с одним PSM через кэш. Возвращает (text, conf, from_cache)."""
    key = cache_key(md5, lang, psm, preproc_ver, tessdata_ver)
    cache_path = os.path.join(cache_dir, key)
    cached = _load_cache(cache_path)
    if cached is not None:
        return cached.get("text", ""), cached.get("mean_conf", 0.0), True
    text, conf = _run_tesseract(fp, lang, psm, preproc, tessdata_dir)
    _save_cache(cache_path, {
        "md5": md5, "lang": lang, "psm": psm,
        "preproc_ver": preproc_ver, "tessdata_ver": tessdata_ver,
        "cache_schema": CACHE_SCHEMA,
        "text": text, "mean_conf": conf,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return text, conf, False


def _ocr_one(fp: str, cache_dir: str, lang: str, psm: int, preproc: bool,
             tessdata_dir: Optional[str], tessdata_ver: str,
             preproc_ver: str) -> tuple[dict, int]:
    """OCR одного файла (исполняется в воркере). Возвращает (record, failed).

    PSM-стратегия: primary (psm, по умолч. 3) → если результат слабый,
    пробуем fallback (psm 11) и берём лучший. record содержит cache_hit
    (все нужные psm найдены в кэше, tesseract не запускался).
    """
    base = os.path.basename(fp)
    meta = image_metadata(fp)
    md5 = meta.get("md5", "")
    if not md5:
        return ({"path": base, "text": "", "mean_conf": 0.0,
                 "cache_hit": False, "ocr_error": True, **meta}, 1)

    ran = False
    try:
        text, conf, from_cache = _ocr_psm(fp, md5, cache_dir, lang, psm,
                                          preproc_ver, preproc, tessdata_dir,
                                          tessdata_ver)
        ran = ran or not from_cache
        if not _accepted(text, conf) and psm != PSM_FALLBACK:
            text_fb, conf_fb, from_cache_fb = _ocr_psm(
                fp, md5, cache_dir, lang, PSM_FALLBACK,
                preproc_ver, preproc, tessdata_dir, tessdata_ver)
            ran = ran or not from_cache_fb
            if _better(text, conf, text_fb, conf_fb):
                text, conf = text_fb, conf_fb
    except RuntimeError as exc:
        # pytesseract raises RuntimeError('Tesseract process timeout') when
        # the timeout (passed to image_to_data) expires; a hung tesseract
        # must fail this image only, not freeze the whole OCR batch
        # (audit 2026-10-18, B5). Other RuntimeErrors propagate as before.
        if "timeout" not in str(exc).lower():
            raise
        log.warning(f"[ocr] tesseract timeout на {base} "
                    f"(> {TESSERACT_TIMEOUT_S}s) — пропускаю файл")
        return ({"path": base, "text": "", "mean_conf": 0.0,
                 "cache_hit": False, "ocr_error": True,
                 "ocr_error_kind": "tesseract_timeout", **meta}, 1)
    rec = {"path": base, "text": text, "mean_conf": conf,
           "cache_hit": not ran, **meta}
    return rec, 0


def _engine_version() -> str:
    try:
        return str(pytesseract.get_tesseract_version())
    except Exception:
        return "tesseract-unknown"


def _tessdata_version(tessdata_dir: Optional[str]) -> str:
    return os.path.basename(tessdata_dir) if tessdata_dir else "sys"


def download_tessdata_best(target_dir: str = "data/tessdata_best",
                           langs=("rus", "eng")) -> List[str]:
    """Скачивает tessdata_best модели в target_dir (идемпотентно).

    Локальные traineddata без задействования системных путей apt.
    Возвращает список установленных языков.
    """
    os.makedirs(target_dir, exist_ok=True)
    installed = []
    for lang in langs:
        fp = os.path.join(target_dir, f"{lang}.traineddata")
        if os.path.exists(fp) and os.path.getsize(fp) > 100 * 1024:
            installed.append(lang)
            continue
        log.info(f"[ocr] download tessdata_best {lang}...")
        data = _fetch(TESSDATA_BEST_URL.format(lang=lang), timeout=60)
        tmp = fp + ".download"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, fp)
        installed.append(lang)
    return installed


def download_gallery(urls: List[str], gallery_dir: str,
                     threads: int = 8) -> tuple[List[str], dict]:
    """Скачивает галерею в gallery_dir (параллельно, порядок детерминирован).

    Имена img_NNN.jpg назначаются по индексу в dedup-списке до фетча.
    Возвращает (пути, stats): stats = {downloaded, skipped_small,
    skipped_plain, failed, images: {имя: {width, height, size_bytes, md5,
    url}}}.
    """
    os.makedirs(gallery_dir, exist_ok=True)
    stats = {"downloaded": 0, "skipped_small": 0, "skipped_plain": 0,
             "failed": 0, "images": {}}
    urls = dedup(urls)
    n = len(urls)
    if n == 0:
        return [], stats

    def _dl(idx: int, url: str) -> tuple[str, str, Optional[str]]:
        ext = ".png" if ".png" in url.lower() else ".jpg"
        fp = os.path.join(gallery_dir, f"img_{idx:03d}{ext}")
        try:
            data = _fetch(_upgrade_url(url))
        except Exception as e:
            log.warning(f"[ocr] fail fetch {url}: {type(e).__name__}: {e}")
            return "failed", url, None
        if len(data) < MIN_BYTES:
            print(f"[ocr] skip small ({len(data)}B): {url}")
            log.debug(f"skip small ({len(data)}B): {url}")
            return "skipped_small", url, None
        with open(fp, "wb") as f:
            f.write(data)
        if not _has_pixels(fp):
            print(f"[ocr] skip plain image: {url}")
            log.debug(f"skip plain image: {url}")
            os.remove(fp)
            return "skipped_plain", url, None
        return "downloaded", url, fp

    workers = max(1, min(threads, n))
    if workers == 1:
        results = [_dl(i, u) for i, u in enumerate(urls, 1)]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_dl, range(1, n + 1), urls))

    paths = []
    for status, url, fp in results:
        stats[status] += 1
        if status == "downloaded" and fp:
            paths.append(fp)
            meta = image_metadata(fp)
            if meta.get("md5"):
                stats["images"][os.path.basename(fp)] = {**meta, "url": url}
    return paths, stats


def ocr_texts(paths: List[str], cache_dir: str = "data/ocr_cache",
              lang: str = "rus", psm: int = 3,
              urls: Optional[dict] = None,
              threads: int = 8, preproc: bool = True,
              tessdata_dir: Optional[str] = None) -> tuple[List[dict], int, dict]:
    """OCR каждого файла с дисковым кэшем и параллельным исполнением.

    Кэш: data/ocr_cache/{md5}_{lang}_{psm}_{preproc_ver}_{tessdata_ver}
    _schema{N}.json — составной ключ инвалидирует старые результаты при
    смене движка/предобработки/структуры. Возвращает ([records], n_failed,
    stats), где records = [{path, url?, text, mean_conf, width, height,
    size_bytes, md5, cache_hit}], stats = {engine, threads, preprocess,
    cache_hits, cache_misses}.

    threads=1 — последовательный режим (для тестов/отладки). Воркеры
    получают пути к файлам и сами открывают изображения (без передачи
    numpy-массивов между процессами).
    """
    out, failed = [], 0
    n = len(paths)
    if n == 0:
        stats = {"engine": _engine_version(), "threads": threads,
                 "preprocess": PREPROC_NONE,
                 "cache_hits": 0, "cache_misses": 0}
        return out, failed, stats

    os.makedirs(cache_dir, exist_ok=True)
    preproc_ver = PREPROC_VER if (preproc and CV2_AVAILABLE) else PREPROC_NONE
    tessdata_ver = _tessdata_version(tessdata_dir)
    workers = max(1, min(threads, n))
    worker = functools.partial(
        _ocr_one, cache_dir=cache_dir, lang=lang, psm=psm,
        preproc=preproc, tessdata_dir=tessdata_dir,
        tessdata_ver=tessdata_ver, preproc_ver=preproc_ver)

    if workers == 1:
        for fp in paths:
            rec, fail = worker(fp)
            failed += fail
            out.append(rec)
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for rec, fail in ex.map(worker, paths):
                failed += fail
                out.append(rec)

    if urls:
        for rec in out:
            if rec.get("path") in urls:
                rec["url"] = urls[rec["path"]]

    cache_hits = sum(1 for r in out if r.get("cache_hit"))
    stats = {
        "engine": _engine_version(),
        "threads": workers,
        "preprocess": preproc_ver,
        "cache_hits": cache_hits,
        "cache_misses": len(out) - cache_hits,
    }
    return out, failed, stats


def ocr_gallery(raw: dict, gallery_dir: str, cache_dir: str = "data/ocr_cache",
                threads: int = 8, preproc: bool = True,
                tessdata_dir: Optional[str] = None) -> dict:
    """OCR галереи из product.gallery + product.infographics, мутирует raw.

    Добавляет: gallery_ocr, gallery_dir, gallery_stats, gallery_stats_text.
    gallery_stats дополнительно: ocr_failed, ocr_engine, ocr_threads,
    preprocess, ocr_wall_seconds, cache_hits, cache_misses, images.
    """
    t0 = time.time()
    urls = list(raw.get("product", {}).get("gallery", [])) or []
    inf = list(raw.get("product", {}).get("infographics", [])) or []
    for u in inf:
        if u not in urls:
            urls.append(u)

    if tessdata_dir is None:
        bundled = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "tessdata_best")
        if not os.path.exists(os.path.join(bundled, "rus.traineddata")):
            try:
                download_tessdata_best(bundled)
            except Exception as e:
                log.warning(f"[ocr] tessdata_best download failed, use system: "
                        f"{type(e).__name__}: {e}")
                bundled = None
        tessdata_dir = bundled if bundled and os.path.exists(
            os.path.join(bundled, "rus.traineddata")) else None

    paths, stats = download_gallery(urls, gallery_dir, threads=threads)
    urls_by_name = {name: info.get("url") for name, info in stats["images"].items()}
    results, n_failed, ocr_stats = ocr_texts(
        paths, cache_dir=cache_dir, urls=urls_by_name,
        threads=threads, preproc=preproc, tessdata_dir=tessdata_dir)

    stats["ocr_failed"] = n_failed
    stats["with_text"] = sum(1 for r in results if r["text"])
    stats["no_text"] = sum(1 for r in results if not r["text"])
    confs = [r["mean_conf"] for r in results if r["mean_conf"] > 0]
    stats["ocr_mean_conf"] = round(sum(confs) / len(confs), 1) if confs else 0.0
    stats["total"] = len(results)
    stats["ocr_engine"] = ocr_stats["engine"]
    stats["ocr_threads"] = ocr_stats["threads"]
    stats["preprocess"] = ocr_stats["preprocess"]
    stats["ocr_wall_seconds"] = round(time.time() - t0, 1)
    stats["cache_hits"] = ocr_stats["cache_hits"]
    stats["cache_misses"] = ocr_stats["cache_misses"]

    raw["gallery_ocr"] = results
    raw["gallery_dir"] = gallery_dir
    raw["gallery_stats"] = stats
    raw["gallery_stats_text"] = (
        f"gallery_ocr: {stats.get('downloaded', 0)} скачано, "
        f"{stats.get('with_text', 0)} с текстом, {stats.get('no_text', 0)} без текста, "
        f"{stats.get('failed', 0)} ошибок загрузки, {stats.get('ocr_failed', 0)} ошибок OCR, "
        f"mean_conf {stats.get('ocr_mean_conf', 0.0)}, "
        f"engine {stats.get('ocr_engine', '?')}, threads {stats.get('ocr_threads', 1)}, "
        f"preproc {stats.get('preprocess', 'none')}, "
        f"cache {stats.get('cache_hits', 0)} hit / {stats.get('cache_misses', 0)} miss, "
        f"{stats.get('ocr_wall_seconds', 0.0)}s"
    )
    return raw
