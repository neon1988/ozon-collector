"""Тесты Phase A: baseline-артефакт и инструмент сравнения (compare_baseline).

Проверяет:
- самоконсистентность tests/data/baseline_products.json (immutable эталон:
  sha256 репересчитывается и совпадает, unique == count, есть товар >= 200);
- логику missing/extra сравнения и exit codes (missing == ∅ → PASS;
  extra — диагностика, FAIL только при --strict-extra).
"""
from __future__ import annotations

import hashlib
import json

from ozon_collector.collect import _review_key

from tests.compare_baseline import compare, extract_all, extract_product, run

BASELINE = "tests/data/baseline_products.json"


def _load_baseline():
    with open(BASELINE, encoding="utf-8") as f:
        return json.load(f)


def _recompute_sha(keys):
    return hashlib.sha256("\n".join(sorted(keys)).encode("utf-8")).hexdigest()


def test_baseline_self_consistent():
    """Baseline immutable: sha256 совпадает, count == unique, есть 200+ товар."""
    data = _load_baseline()
    assert data["schema_version"] == "1.0"
    products = data["products"]
    assert len(products) >= 3
    for p in products:
        assert p["pid"]
        assert p["baseline_count"] == len(p["review_keys"])
        assert len(set(p["review_keys"])) == p["baseline_count"]
        assert p["review_keys_sha256"] == _recompute_sha(p["review_keys"])
    assert any(p["baseline_count"] >= 200 for p in products), \
        "нужен товар с baseline_count >= 200 (граница сходимости)"


def test_extract_product_and_review_keys():
    raw = {
        "pid": "42",
        "product": {"characteristics": {"Артикул": "42"}},
        "reviews": [
            {"uuid": "u1", "author": "A", "date": "1", "text": "x"},
            {"uuid": "u2", "author": "B", "date": "2", "text": "y"},
        ],
    }
    p = extract_product(raw)
    assert p["pid"] == "42"
    assert p["count"] == 2
    assert p["review_keys"] == ["u1", "u2"]


def test_extract_all_combined_vs_single():
    single = {"pid": "1", "product": {}, "reviews": [{"uuid": "a"}]}
    assert extract_all(single) == {"1": extract_product(single)}
    combined = {
        "products": [
            {"pid": "1", "ok": True, "raw": single},
            {"pid": "2", "ok": False, "raw": None},
        ]
    }
    got = extract_all(combined)
    assert list(got) == ["1"]  # ok=False пропущен


def _mk_raw(pid, keys):
    return {"pid": pid, "product": {},
            "reviews": [{"uuid": k} for k in keys]}


def test_compare_missing_detected():
    base = {"p1": {"pid": "p1", "sku": None, "baseline_count": 3,
                   "review_keys": ["a", "b", "c"]}}
    res = compare({"p1": extract_product(_mk_raw("p1", ["a", "b"]))}, base)
    assert res["p1"]["missing"] == {"c"}
    assert res["p1"]["extra"] == set()


def test_compare_extra_diagnostic_not_fail(tmp_path):
    """extra (дрейф Ozon) — не fail по умолчанию; --strict-extra делает fail."""
    base = {"p1": {"pid": "p1", "sku": None, "baseline_count": 2,
                   "review_keys": ["a", "b"]}}
    actual_raw = tmp_path / "a.json"
    base_path = tmp_path / "b.json"
    actual_raw.write_text(json.dumps(_mk_raw("p1", ["a", "b", "c"])),
                          encoding="utf-8")
    base_path.write_text(json.dumps({"products": [base["p1"]]}),
                         encoding="utf-8")
    assert run(str(actual_raw), str(base_path), only_pid="p1") == 0
    assert run(str(actual_raw), str(base_path), only_pid="p1",
               strict_extra=True) == 1


def test_run_missing_returns_1(tmp_path):
    base_path = tmp_path / "b.json"
    actual_raw = tmp_path / "a.json"
    base_path.write_text(json.dumps({"products": [
        {"pid": "p1", "sku": None, "baseline_count": 3,
         "review_keys": ["a", "b", "c"]}]}), encoding="utf-8")
    actual_raw.write_text(json.dumps(_mk_raw("p1", ["a", "b"])),
                          encoding="utf-8")
    assert run(str(actual_raw), str(base_path), only_pid="p1") == 1


def test_review_key_fallback_sha():
    r = {"author": "A", "date": "1", "text": "x" * 30}
    assert _review_key(r).startswith("sha1:")


def test_baseline_matches_source_raw() -> None:
    """Каждый baseline-товар по-прежнему воспроизводим из source raw.

    Не требует сети: сравнивает извлечённые ключи из data/{pid}_raw.json
    с baseline (источник тех же данных)."""
    import os

    data = _load_baseline()
    for p in data["products"]:
        src = p.get("source_file") or f"data/{p['pid']}_raw.json"
        if not os.path.exists(src):
            continue
        raw = json.load(open(src, encoding="utf-8"))
        assert raw.get("pid") == p["pid"], f"pid mismatch: {src}"
        got = extract_product(raw)
        assert set(got["review_keys"]) == set(p["review_keys"]), \
            f"ключи разошлись с baseline: {p['pid']}"
