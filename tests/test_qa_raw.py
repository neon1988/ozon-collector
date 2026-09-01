"""Тесты scripts/qa_raw.py: вердикты, collection_mode, каталог проверок."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ozon_collector.collect import CHALLENGE_TITLE_MARKERS
from scripts.qa_raw import exit_code_for, qa_product, scan_file, summarize

# Данные должны быть «свежими» относительно момента прогона: qa_raw ругается
# OLD_DATA (WARN), если age_days > OLD_DATA_DAYS (7). Замораживать дату нельзя —
# тесты протухают за неделю и OK-кейсы падают на лишнем WARN.
NOW = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
TS = int(dt.datetime.fromisoformat(NOW).timestamp())


def _rev(uuid, stars=5, text=None, sort="published_at_desc", age_s=3600):
    return {
        "uuid": uuid,
        "publishedAt": str(TS - age_s),
        "author": f"Иван {uuid}",
        "author2": "",
        "date": "19 августа 2026",
        "variant": "Спрей, 20 мл",
        "stars": stars,
        "text": text if text is not None else f"Отличный спрей номер {uuid}",
        "helpful_yes": 0,
        "helpful_no": 0,
        "comments_count": 0,
        "comments": [],
        "review_sort": sort,
        "review_sorts": [sort],
    }


def _raw(pid="42", vc=5, reviews=None, rc=None, sorts=("published_at_desc",)):
    vm = {
        "variant_present": bool(vc),
        "variant_count": vc,
        "active": "Этот вариант товара",
        "total_count": rc or vc or 0,
        "used": bool(vc),
        "clicked": bool(vc),
        "reason": "click_ok" if vc else "no_widget",
        "pagination_type": "infinite_scroll",
        "reviews_matched_variant": len(reviews or []),
        "reviews_dropped_by_client_filter": 0,
    }
    return {
        "pid": pid,
        "schema_version": "2.0",
        "collector_version": "1.16.0",
        "collected_at_utc": NOW,
        "product_url": f"https://www.ozon.ru/product/{pid}",
        "reviews_url": f"https://www.ozon.ru/product/{pid}?tab=reviews",
        "target_variant": {},
        "product": {
            "title": "Спрей для горла с прополисом",
            "rating": 4.8,
            "rating_source": "webSingleProductScore",
            "rating_count": rc,
            "price_text": "156 ₽",
            "customer_opinion": {"recommend_percent": None, "stars": {},
                                 "counts": {}, "text": ""},
        },
        "reviews": reviews if reviews is not None else [
            _rev(f"u{i}") for i in range(vc)
        ],
        "reviews_collected": len(reviews) if reviews is not None else vc,
        "reviews_expected": None,
        "reviews_sorts": list(sorts),
        "reviews_variant_mode": vm,
        "pages": [],
        "review_pages": [],
        "source": "test",
        "collection_telemetry": {},
        "incomplete": False,
        "missing_fields": [],
        "artifacts": {},
    }


def _codes(rep):
    return {i["code"] for i in rep["issues"]}


def test_full_mode_ok():
    rep = qa_product(_raw(vc=5), "42")
    assert rep["verdict"] == "OK"
    assert rep["collection_mode"] == "FULL"
    assert rep["official_count_source"] == "variant_count"
    assert not (_codes(rep) & {"DUP_UUID", "OVERCOUNT", "UNDER_TOLERANCE",
                               "GARBAGE_TEXT", "FUTURE_DATE"})


def test_dup_uuid_fail():
    raw = _raw(vc=3, reviews=[_rev("a"), _rev("a"), _rev("b")])
    rep = qa_product(raw, "42")
    assert rep["verdict"] == "FAIL"
    assert "DUP_UUID" in _codes(rep)


def test_overcount_fail():
    raw = _raw(vc=3, reviews=[_rev(f"u{i}") for i in range(4)])
    rep = qa_product(raw, "42")
    assert rep["verdict"] == "FAIL"
    assert "OVERCOUNT" in _codes(rep)


def test_under_tolerance_warn():
    raw = _raw(vc=100, reviews=[_rev(f"u{i}") for i in range(90)])
    rep = qa_product(raw, "42")
    assert rep["collection_mode"] == "FULL"
    assert rep["verdict"] == "WARN"
    assert "UNDER_TOLERANCE" in _codes(rep)


def test_wall_limited_ok_with_info():
    raw = _raw(vc=2248, reviews=[_rev(f"u{i}") for i in range(176)])
    rep = qa_product(raw, "42")
    assert rep["collection_mode"] == "WALL_LIMITED"
    assert rep["verdict"] == "OK"
    info = {i["code"] for i in rep["issues"] if i["severity"] == "INFO"}
    assert "LOW_COVERAGE" in info


def test_short_total_warn():
    raw = _raw(vc=500, reviews=[_rev(f"u{i}") for i in range(85)])
    rep = qa_product(raw, "42")
    assert rep["collection_mode"] == "WALL_LIMITED"
    assert rep["verdict"] == "WARN"
    assert "SHORT_TOTAL" in _codes(rep)


def test_no_widget_fallback_warn():
    raw = _raw(vc=0, rc=465, reviews=[_rev(f"u{i}") for i in range(85)])
    rep = qa_product(raw, "42")
    codes = _codes(rep)
    assert rep["official_count_source"] == "rating_count_fallback"
    assert rep["official_count"] == 465
    assert "FALLBACK_OFFICIAL_COUNT" in codes
    assert "SHORT_TOTAL" in codes
    assert rep["verdict"] == "WARN"


def test_garbage_text_fail():
    raw = _raw(vc=2, reviews=[
        _rev("a", text="Хороший \ufffd спрей"),
        _rev("b"),
    ])
    rep = qa_product(raw, "42")
    assert rep["verdict"] == "FAIL"
    assert "GARBAGE_TEXT" in _codes(rep)


def test_future_date_fail():
    raw = _raw(vc=2, reviews=[_rev("a", age_s=-99999), _rev("b")])
    rep = qa_product(raw, "42")
    assert rep["verdict"] == "FAIL"
    assert "FUTURE_DATE" in _codes(rep)


def test_empty_text_and_stars_missing_warn():
    raw = _raw(vc=5, reviews=[_rev(f"u{i}") for i in range(4)] + [_rev("z", stars=0, text="")])
    rep = qa_product(raw, "42")
    codes = _codes(rep)
    assert "EMPTY_TEXT" in codes
    assert "STARS_MISSING" in codes
    assert rep["verdict"] == "WARN"


def test_rating_only_pattern_info_only():
    ro = _rev("ro", stars=0,
              text="Иван И.\nКуплено на Ozon\nПоложено в пункт выдачи")
    good = [_rev(f"u{i}") for i in range(20)]
    rep = qa_product(_raw(vc=21, reviews=good + [ro]), "42")
    assert rep["metrics"]["rating_only_count"] == 1
    assert rep["metrics"]["review_count"] == 20
    assert rep["metrics"]["stars_missing_count"] == 1
    assert "RATING_ONLY_PATTERN" in _codes(rep)
    assert rep["verdict"] == "OK"


def test_challenge_page_fail():
    marker = sorted(CHALLENGE_TITLE_MARKERS)[0]
    raw = _raw(vc=2)
    raw["product"]["title"] = f"Спрей {marker} тест"
    rep = qa_product(raw, "42")
    assert rep["verdict"] == "FAIL"
    assert "CHALLENGE_PAGE" in _codes(rep)
    assert rep["metrics"]["challenge_suspect"] is True


def test_sort_imbalance_warn():
    revs = ([_rev(f"d{i}", sort="published_at_desc") for i in range(10)]
            + [_rev(f"a{i}", sort="score_asc") for i in range(3)])
    raw = _raw(vc=20, reviews=revs,
               sorts=("published_at_desc", "score_asc"))
    rep = qa_product(raw, "42")
    assert "SORT_IMBALANCE" in _codes(rep)
    assert rep["metrics"]["per_sort_coverage"] == {
        "published_at_desc": 10, "score_asc": 3}
    assert rep["verdict"] == "WARN"


def test_sort_imbalance_skipped_on_complete_collection():
    revs = [_rev(f"u{i}", sort="published_at_desc") for i in range(5)]
    raw = _raw(vc=5, reviews=revs,
               sorts=("published_at_desc", "score_asc"))
    rep = qa_product(raw, "42")
    assert "SORT_IMBALANCE" not in _codes(rep)
    assert rep["verdict"] == "OK"


def test_zero_reviews_warn():
    raw = _raw(vc=0, rc=None, reviews=[])
    rep = qa_product(raw, "42")
    assert "ZERO_REVIEWS" in _codes(rep)
    assert rep["verdict"] == "WARN"
    assert rep["collection_mode"] == "UNKNOWN"


def test_bad_json(tmp_path):
    p = tmp_path / "77_raw.json"
    p.write_text("{битый json", encoding="utf-8")
    rep = scan_file(p, "77")
    assert rep["verdict"] == "FAIL"
    assert "BAD_JSON" in _codes(rep)


def test_scope_unknown_skips_overcount():
    raw = _raw(vc=2, reviews=[_rev(f"u{i}") for i in range(4)])
    raw["reviews_variant_mode"].update(
        {"used": False, "clicked": False, "reason": "click_error"})
    rep = qa_product(raw, "42")
    codes = _codes(rep)
    assert "SCOPE_UNKNOWN" in codes
    assert "OVERCOUNT" not in codes
    fails = {i["code"] for i in rep["issues"] if i["severity"] == "FAIL"}
    assert not fails


def test_stars_over_histogram_fail():
    raw = _raw(vc=2, reviews=[_rev("a"), _rev("b")])
    raw["product"]["customer_opinion"]["counts"] = {"5": 1, "4": 0}
    rep = qa_product(raw, "42")
    assert rep["verdict"] == "FAIL"
    assert "STARS_OVER_HISTOGRAM" in _codes(rep)


def test_histogram_skipped_when_empty():
    rep = qa_product(_raw(vc=2), "42")
    assert "HISTOGRAM_SKIPPED" in _codes(rep)
    assert rep["verdict"] == "OK"


def test_dup_text_warn():
    revs = [_rev(f"u{i}", text="Отличный спрей!") for i in range(7)]
    rep = qa_product(_raw(vc=7, reviews=revs), "42")
    assert "DUP_TEXT" in _codes(rep)
    assert rep["verdict"] == "WARN"


def test_exit_codes():
    assert exit_code_for({"fail": 1, "warn": 0}, strict=False) == 1
    assert exit_code_for({"fail": 0, "warn": 3}, strict=False) == 0
    assert exit_code_for({"fail": 0, "warn": 3}, strict=True) == 1
    assert exit_code_for({"fail": 0, "warn": 0}, strict=True) == 0


def test_summarize_kpi():
    reports = [
        qa_product(_raw(pid="41", vc=5), "41"),
        qa_product(_raw(vc=2248,
                        reviews=[_rev(f"u{i}") for i in range(176)]), "42"),
    ]
    s = summarize(reports)
    assert s["products"] == 2
    assert s["ok"] == 2
    assert s["fail"] == 0
    assert s["wall_limited_count"] == 1
    assert s["reviews_total_sum"] == 181
    assert s["rating_only_pattern_total"] == 0
