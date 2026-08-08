"""Тесты OCR v1.7: кэш-ключ, PSM-стратегия, предобработка, параллелизм.

Параллельный путь (ProcessPool) тестируется на реальном tesseract с
синтетическими картинками (в воркерах недоступны monkeypatch). Логика
PSM/кэша — на threads=1 с мокнутым pytesseract (детерминированно).
"""
from __future__ import annotations

import os
import sys

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ozon_collector import ocr as O  # noqa: E402

OCR_RE = pytest.importorskip("pytesseract")


def _make_img(fp: str, text: str = "hello world", w: int = 500, h: int = 200):
    """Шумный фон (stddev выше порога, JPEG > 20KB) + контрастный блок.

    Шум даёт гарантированный проход _has_pixels и MIN_BYTES; tesseract
    распознаёт плохо, но тестам нужны только порядок/равенство/кэш.
    """
    import numpy as np
    arr = np.random.default_rng(0).integers(0, 255, (h, w, 3), dtype=np.uint8)
    im = Image.fromarray(arr, "RGB")
    dr = ImageDraw.Draw(im)
    dr.rectangle([20, h // 2 - 40, w - 20, h // 2 + 40], fill=(0, 0, 0))
    dr.text((40, h // 2 - 20), text, fill=(255, 255, 255))
    im.save(fp, quality=92)


# --- cache_key ----------------------------------------------------------------

def test_cache_key_composition():
    k = O.cache_key("md5abc", "rus", 11, "cv2v1", "tessdata_best")
    assert k == "md5abc_rus_11_cv2v1_tessdata_best_schema2.json"
    assert "schema2" in k


def test_cache_key_differs_on_every_component():
    base = ("md5abc", "rus", 3, "cv2v1", "tessdata_best")
    variants = [
        ("md5abd", "rus", 3, "cv2v1", "tessdata_best"),
        ("md5abc", "eng", 3, "cv2v1", "tessdata_best"),
        ("md5abc", "rus", 11, "cv2v1", "tessdata_best"),
        ("md5abc", "rus", 3, "none", "tessdata_best"),
        ("md5abc", "rus", 3, "cv2v1", "sys"),
    ]
    keys = {O.cache_key(*v) for v in [base] + variants}
    assert len(keys) == 6


# --- PSM-стратегия ------------------------------------------------------------

def test_accepted_thresholds():
    assert O._accepted("x" * 10, 60.0) is True
    assert O._accepted("x" * 10, 59.9) is False   # conf ниже 60
    assert O._accepted("x" * 9, 90.0) is False    # text_len ниже 10
    assert O._accepted("", 0.0) is False


def test_better_prefers_text_then_conf():
    assert O._better("text", 50.0, "", 99.0) is False   # a с текстом, b пуст
    assert O._better("", 0.0, "text", 50.0) is True     # b с текстом
    assert O._better("text", 50.0, "text", 70.0) is True   # выше conf
    assert O._better("text", 70.0, "text", 50.0) is False


# --- предобработка ------------------------------------------------------------

@pytest.mark.skipif(not O.CV2_AVAILABLE, reason="cv2 не установлен")
def test_preprocess_grayscale_upscaled():
    import numpy as np
    arr = np.zeros((100, 300, 3), dtype=np.uint8)
    arr[:, :, 2] = 200
    out = O._preprocess(arr)
    assert out.ndim == 2                 # grayscale
    assert out.dtype == np.uint8
    assert max(out.shape) >= O.PREPROC_TARGET_LONG  # апскейл до 2000


def test_preprocess_none_without_cv2(monkeypatch):
    monkeypatch.setattr(O, "CV2_AVAILABLE", False)
    monkeypatch.setattr(O, "_cv2", None)
    assert O._preprocess(None) is None


# --- PSM fallback + кэш (threads=1, мокнутый tesseract) ----------------------

class _FakeTesseract:
    """Отдаёт разный результат по --psm в config."""

    def __init__(self, texts: dict):
        self.texts = texts      # {psm: {"text": [...], "conf": [...]}}
        self.calls: list[int] = []

    def __call__(self, img, lang=None, config="", output_type=None):
        parts = config.split()
        psm = int(parts[parts.index("--psm") + 1])
        self.calls.append(psm)
        d = self.texts[psm]
        return {"text": list(d["text"]), "conf": list(d["conf"])}


def _fake_ocr(monkeypatch, texts):
    fake = _FakeTesseract(texts)
    monkeypatch.setattr(O.pytesseract, "image_to_data", fake)
    return fake


@pytest.fixture()
def img_dir(tmp_path):
    d = tmp_path / "gal"
    d.mkdir()
    fp = str(d / "img_001.jpg")
    _make_img(fp, text="MAIN TEXT")
    return fp


def test_psm_fallback_when_primary_weak(monkeypatch, img_dir, tmp_path):
    fake = _fake_ocr(monkeypatch, {
        3: {"text": ["short"], "conf": ["30"]},           # слабый (conf<60)
        11: {"text": ["good", "result"], "conf": ["80", "90"]},
    })
    rec, failed = O._ocr_one(
        img_dir, str(tmp_path / "cache"), "rus", 3,
        preproc=False, tessdata_dir=None, tessdata_ver="sys",
        preproc_ver=O.PREPROC_NONE)
    assert failed == 0
    assert rec["text"] == "good result"     # взят лучший (psm 11)
    assert rec["mean_conf"] == 85.0
    assert fake.calls == [3, 11]
    assert rec["cache_hit"] is False


def test_no_fallback_when_primary_strong(monkeypatch, img_dir, tmp_path):
    fake = _fake_ocr(monkeypatch, {
        3: {"text": ["okay", "primary", "long", "text", "here"],
            "conf": ["80", "81", "82", "83", "84"]},
    })
    rec, _ = O._ocr_one(
        img_dir, str(tmp_path / "cache"), "rus", 3,
        preproc=False, tessdata_dir=None, tessdata_ver="sys",
        preproc_ver=O.PREPROC_NONE)
    assert rec["text"] == "okay primary long text here"
    assert fake.calls == [3]                # fallback не вызывался


def test_cache_hit_second_run(monkeypatch, img_dir, tmp_path):
    fake = _fake_ocr(monkeypatch, {
        3: {"text": ["cached", "text", "long", "enough"],
            "conf": ["90", "90", "90", "90"]},
    })
    cache = str(tmp_path / "cache")
    r1, _ = O._ocr_one(img_dir, cache, "rus", 3, preproc=False,
                       tessdata_dir=None, tessdata_ver="sys",
                       preproc_ver=O.PREPROC_NONE)
    assert r1["cache_hit"] is False
    n_calls = len(fake.calls)
    r2, _ = O._ocr_one(img_dir, cache, "rus", 3, preproc=False,
                       tessdata_dir=None, tessdata_ver="sys",
                       preproc_ver=O.PREPROC_NONE)
    assert r2["cache_hit"] is True
    assert r2["text"] == r1["text"]
    assert len(fake.calls) == n_calls       # tesseract не вызывался повторно


def test_cache_schema_change_invalidates(monkeypatch, img_dir, tmp_path):
    cache = str(tmp_path / "cache")
    os.makedirs(cache, exist_ok=True)
    md5 = O._md5(img_dir)
    import json as _j
    payload = {"md5": md5, "lang": "rus", "psm": 3, "cache_schema": 1,
               "text": "old schema", "mean_conf": 99.0}
    with open(os.path.join(cache, f"{md5}_rus_3_none_sys_schema1.json"),
              "w", encoding="utf-8") as f:
        _j.dump(payload, f)
    fake = _fake_ocr(monkeypatch, {
        3: {"text": ["new", "schema", "text", "long"], "conf": ["70"] * 4},
    })
    rec, _ = O._ocr_one(img_dir, cache, "rus", 3, preproc=False,
                        tessdata_dir=None, tessdata_ver="sys",
                        preproc_ver=O.PREPROC_NONE)
    assert rec["text"] == "new schema text long"  # старый кэш проигнорирован
    assert len(fake.calls) == 1


# --- ocr_texts: параллелизм и порядок (реальный tesseract) --------------------

@pytest.mark.skipif(not OCR_RE, reason="pytesseract недоступен")
def test_ocr_texts_parallel_order_and_cache(tmp_path):
    gal = tmp_path / "gal"
    gal.mkdir()
    fps = []
    for i in range(4):
        fp = str(gal / f"img_{i:03d}.jpg")
        _make_img(fp, text="TEST ONE")
        fps.append(fp)
    cache = str(tmp_path / "cache")

    r1, f1, s1 = O.ocr_texts(fps, cache_dir=cache, threads=2,
                             preproc=False)
    assert f1 == 0
    assert [r["path"] for r in r1] == [os.path.basename(p) for p in fps]
    assert s1["threads"] == 2
    assert s1["preprocess"] == O.PREPROC_NONE

    r2, f2, s2 = O.ocr_texts(list(reversed(fps)), cache_dir=cache, threads=3,
                             preproc=False)
    assert [r["path"] for r in r2] == [os.path.basename(p) for p in reversed(fps)]
    assert s2["cache_hits"] == 4
    assert s2["cache_misses"] == 0
    assert [r["text"] for r in r1] == [r["text"] for r in r2]


def test_ocr_texts_sequential_threads1(tmp_path):
    gal = tmp_path / "gal"
    gal.mkdir()
    fps = [str(gal / f"a{i}.jpg") for i in range(2)]
    for fp in fps:
        _make_img(fp, text="SEQ")
    r, f, s = O.ocr_texts(fps, cache_dir=str(tmp_path / "cache"),
                          threads=1, preproc=False)
    assert f == 0
    assert s["threads"] == 1
    assert len(r) == 2


def test_ocr_texts_empty():
    r, f, s = O.ocr_texts([], threads=8)
    assert r == [] and f == 0
    assert s["cache_hits"] == s["cache_misses"] == 0


# --- download_gallery: параллельный порядок -----------------------------------

def test_download_gallery_parallel_order(tmp_path, monkeypatch):
    buf = tmp_path / "src.jpg"
    _make_img(str(buf), text="G", w=400, h=300)
    data = open(buf, "rb").read()

    monkeypatch.setattr(O, "_fetch", lambda url, timeout=20: data)
    urls = ["https://x/wc200/a.jpg", "https://x/wc200/b.jpg",
            "https://x/wc200/a.jpg", "https://x/wc200/d.png"]  # дубль a
    gal = str(tmp_path / "gal")
    paths, stats = O.download_gallery(urls, gal, threads=3)

    assert stats["downloaded"] == 3            # dedup убрал дубль
    assert stats["failed"] == 0
    assert [os.path.basename(p) for p in paths] == [
        "img_001.jpg", "img_002.jpg", "img_003.png"]  # порядок детерминирован
    assert set(stats["images"]) == {"img_001.jpg", "img_002.jpg", "img_003.png"}


def test_download_gallery_threads1(tmp_path, monkeypatch):
    buf = tmp_path / "src.jpg"
    _make_img(str(buf), text="G", w=400, h=300)
    data = open(buf, "rb").read()
    monkeypatch.setattr(O, "_fetch", lambda url, timeout=20: data)
    paths, stats = O.download_gallery(["https://x/wc200/a.jpg"], str(tmp_path / "g"), threads=1)
    assert len(paths) == 1 and stats["downloaded"] == 1


# --- ocr_gallery: контракт stats ---------------------------------------------

def test_ocr_gallery_stats_fields(tmp_path, monkeypatch):
    buf = tmp_path / "src.jpg"
    _make_img(str(buf), text="FIELD", w=400, h=300)
    data = open(buf, "rb").read()
    monkeypatch.setattr(O, "_fetch", lambda url, timeout=20: data)
    monkeypatch.setattr(O, "download_tessdata_best", lambda *a, **k: [])  # сеть не нужна

    raw = {"product": {"gallery": ["https://x/wc200/a.jpg"]}}
    out = O.ocr_gallery(raw, str(tmp_path / "gal"),
                        cache_dir=str(tmp_path / "cache"), threads=1)
    st = out["gallery_stats"]
    for k in ("ocr_engine", "ocr_threads", "preprocess", "ocr_wall_seconds",
              "cache_hits", "cache_misses", "ocr_failed", "with_text",
              "no_text", "total", "ocr_mean_conf"):
        assert k in st, f"отсутствует поле {k}"
    assert st["ocr_threads"] == 1
    assert st["preprocess"] in (O.PREPROC_VER, O.PREPROC_NONE)
    assert st["total"] == st["with_text"] + st["no_text"]
    assert "engine" in out["gallery_stats_text"]
