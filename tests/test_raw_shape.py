"""Контрактные проверки формы raw.json.

Эталон — tests/raw_fixtures/shape_minimal.json (минимальный образец v1.4,
покрывающий все документированные поля). Ловит случай, когда collect работает,
а report падает из-за отсутствующего ключа/неверной формы.
"""
from __future__ import annotations

import json
import os

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "raw_fixtures", "shape_minimal.json")


def load_raw() -> dict:
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def test_top_level_fields():
    raw = load_raw()
    assert raw["schema_version"] == "2.0"
    assert raw["collector_version"]
    assert raw["pid"]
    assert raw["product_url"]
    assert raw["reviews_url"]
    assert isinstance(raw["target_variant"], dict)
    assert isinstance(raw["reviews"], list)
    assert isinstance(raw["review_pages"], list)
    assert isinstance(raw["pages"], int)
    assert raw["reviews_collected"] == len(raw["reviews"])


def test_incomplete_contract():
    raw = load_raw()
    assert "incomplete" in raw
    assert "missing_fields" in raw
    assert isinstance(raw["incomplete"], bool)
    assert isinstance(raw["missing_fields"], list)


def test_product_shape():
    p = load_raw()["product"]
    assert p["title"]
    assert p["price"]["current"] > 0
    assert p["price"].get("source") == "webPrice"
    assert isinstance(p["rating"], (int, float)) and 0 < p["rating"] <= 5
    assert p["rating_source"] in {
        "webProductScore", "webSingleProductScore", "webReviewProductScore", "fallback_text",
    }
    assert p["rating_count"] > 0
    assert len(p["characteristics"]) > 0
    assert "Артикул" in p["characteristics"]
    assert len(p["gallery"]) > 0


def test_reviews_shape():
    for r in load_raw()["reviews"]:
        assert r["uuid"]
        assert r["text"]
        assert isinstance(r["stars"], int) and 1 <= r["stars"] <= 5
        assert "variant" in r
        assert "author" in r
        assert "date" in r
        assert r["publishedAt"]


def test_gallery_ocr_shape():
    for o in load_raw()["gallery_ocr"]:
        assert o["path"]
        assert o["url"].startswith("http")
        assert "text" in o
        assert isinstance(o.get("mean_conf"), (int, float))
        assert o["md5"]


def test_gallery_stats_shape():
    stats = load_raw()["gallery_stats"]
    assert stats["with_text"] + stats["no_text"] == stats["total"]
    for name, info in stats["images"].items():
        assert info["url"].startswith("http")
        assert info["md5"]


def test_gallery_stats_ocr_diagnostics():
    """v1.7: диагностические поля OCR (аддитивны к схеме 2.0)."""
    stats = load_raw()["gallery_stats"]
    assert isinstance(stats["ocr_engine"], str) and stats["ocr_engine"]
    assert isinstance(stats["ocr_threads"], int)
    assert stats["preprocess"] in ("cv2v1", "none")
    assert isinstance(stats["ocr_wall_seconds"], (int, float))
    assert stats["cache_hits"] + stats["cache_misses"] == stats["total"]
    assert stats["total"] == stats["downloaded"] + stats["skipped_small"] \
        + stats["skipped_plain"] + stats["failed"]
    assert "engine" in load_raw()["gallery_stats_text"]
