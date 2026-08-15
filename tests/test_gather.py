"""Тесты run_gather: кеш-hit логирование, инвалидация по версии, стадии."""
from __future__ import annotations

import pytest

from ozon_collector.gather import run_gather, assert_raw_structure
from ozon_collector import browser as B


def _raw(pid="42"):
    return {
        "pid": pid, "schema_version": "2.0", "collector_version": "1.11.0",
        "collected_at_utc": "2026-08-08T10:00:00+00:00",
        "gallery_ocr": {"x.png": "текст"},
        "product": {"title": "Товар"},
        "reviews": [{"uuid": "u1"}, {"uuid": "u2"}],
    }


def test_run_gather_cache_hit_logs(monkeypatch, capsys):
    monkeypatch.setattr("ozon_collector.gather.K.cache_check",
                        lambda pid, ma: (_raw(), "hit"))
    monkeypatch.setattr("ozon_collector.gather.R.render", lambda r, **kw: "# ok")
    res = run_gather({"pid": "42", "max_age_s": 86400})
    out = capsys.readouterr().out
    assert "cache=hit" in out
    assert "ocr=hit" in out
    assert res["ok"] is True
    assert res["cache"] == "hit"
    assert res["ocr"] == "hit"


def test_run_gather_old_version_forces_recollect(monkeypatch, capsys):
    monkeypatch.setattr("ozon_collector.gather.K.cache_check",
                        lambda pid, ma: (None, "old_version"))
    monkeypatch.setattr("ozon_collector.gather.K.cached_version",
                        lambda pid: "1.5.0")
    monkeypatch.setattr("ozon_collector.gather.C.collect",
                        lambda **kw: _raw())
    monkeypatch.setattr("ozon_collector.gather.R.render", lambda r, **kw: "# ok")
    res = run_gather({"pid": "42", "url": "https://www.ozon.ru/product/42",
                      "max_age_s": 86400, "no_ocr": True})
    out = capsys.readouterr().out
    assert "cache=miss (old_version 1.5.0" in out
    assert "collect — 2 отзывов" in out
    assert res["cache"] == "miss"
    assert res["ok"] is True


def test_run_gather_newer_version_miss(monkeypatch, capsys):
    monkeypatch.setattr("ozon_collector.gather.K.cache_check",
                        lambda pid, ma: (None, "newer_version"))
    monkeypatch.setattr("ozon_collector.gather.K.cached_version",
                        lambda pid: "2.0.0")
    monkeypatch.setattr("ozon_collector.gather.C.collect",
                        lambda **kw: _raw())
    monkeypatch.setattr("ozon_collector.gather.R.render", lambda r, **kw: "# ok")
    res = run_gather({"pid": "42", "url": "https://www.ozon.ru/product/42",
                      "max_age_s": 86400, "no_ocr": True})
    out = capsys.readouterr().out
    assert "cache=miss (newer_version 2.0.0" in out
    assert res["cache"] == "miss"


def test_run_gather_error_logs(monkeypatch, capsys):
    monkeypatch.setattr("ozon_collector.gather.K.cache_check",
                        lambda pid, ma: (None, "missing"))
    def boom(**kw):
        raise RuntimeError("сеть пропала")
    monkeypatch.setattr("ozon_collector.gather.C.collect", boom)
    res = run_gather({"pid": "42", "url": "https://www.ozon.ru/product/42",
                      "max_age_s": 86400})
    out = capsys.readouterr().out
    assert "error: RuntimeError: сеть пропала" in out
    assert res["ok"] is False


def test_run_gather_classifies_recyclable_error(monkeypatch, capsys):
    """Playwright browser-closed ошибка → error_kind=browser, recyclable=True."""
    from playwright.sync_api import Error as PWError

    monkeypatch.setattr("ozon_collector.gather.K.cache_check",
                        lambda pid, ma: (None, "missing"))
    def boom(**kw):
        raise PWError("Browser has been closed")
    monkeypatch.setattr("ozon_collector.gather.C.collect", boom)
    res = run_gather({"pid": "42", "url": "https://www.ozon.ru/product/42",
                      "max_age_s": 86400})
    out = capsys.readouterr().out
    assert res["ok"] is False
    assert res["error_kind"] == "browser"
    assert res["recyclable"] is True


def test_run_gather_classifies_non_recyclable(monkeypatch, capsys):
    """Пользовательское исключение → error_kind=unknown, recyclable=False."""
    monkeypatch.setattr("ozon_collector.gather.K.cache_check",
                        lambda pid, ma: (None, "missing"))
    def boom(**kw):
        raise ValueError("внутренняя ошибка")
    monkeypatch.setattr("ozon_collector.gather.C.collect", boom)
    res = run_gather({"pid": "42", "url": "https://www.ozon.ru/product/42",
                      "max_age_s": 86400})
    assert res["ok"] is False
    assert res["error_kind"] == "unknown"
    assert res["recyclable"] is False


# ---- acceptance-gate: assert_raw_structure ---------------------------------

def test_structure_gate_valid_raw():
    raw = _raw()
    assert_raw_structure(raw, "42")          # не бросает


def test_structure_gate_pid_mismatch():
    with pytest.raises(B.StructureError):
        assert_raw_structure(_raw(), "999")


def test_structure_gate_not_a_dict():
    with pytest.raises(B.StructureError):
        assert_raw_structure(None, "42")


def test_structure_gate_product_missing():
    raw = _raw()
    raw["product"] = {}
    with pytest.raises(B.StructureError):
        assert_raw_structure(raw, "42")


def test_structure_gate_empty_title():
    raw = _raw()
    raw["product"] = {"title": "   "}
    with pytest.raises(B.StructureError):
        assert_raw_structure(raw, "42")


def test_structure_gate_reviews_not_list():
    raw = _raw()
    raw["reviews"] = None
    with pytest.raises(B.StructureError):
        assert_raw_structure(raw, "42")


def test_structure_gate_zero_reviews_ok():
    raw = _raw()
    raw["reviews"] = []
    assert_raw_structure(raw, "42")          # 0 отзывов ≠ ошибка


def test_structure_gate_challenge_title_recyclable():
    """Челлендж-title в product.title → AccessChallengeError (не StructureError)."""
    raw = _raw()
    raw["product"] = {"title": "Похоже, нет соединения"}
    with pytest.raises(B.AccessChallengeError):
        assert_raw_structure(raw, "42")


def test_run_gather_challenge_title_cache_hit_fails(monkeypatch, capsys):
    """Challenge-raw даже из кеша → ok=False, error_kind=access_challenge."""
    raw = _raw()
    raw["product"] = {"title": "Подтвердите, что вы не робот"}
    monkeypatch.setattr("ozon_collector.gather.K.cache_check",
                        lambda pid, ma: (raw, "hit"))
    res = run_gather({"pid": "42", "max_age_s": 86400, "no_ocr": True})
    assert res["ok"] is False
    assert res["error_kind"] == "access_challenge"
    assert res["recyclable"] is True


def test_run_gather_broken_structure_fails(monkeypatch, capsys):
    """Битая структура (product пуст) → StructureError, НЕ recyclable."""
    raw = _raw()
    raw["product"] = {}
    monkeypatch.setattr("ozon_collector.gather.K.cache_check",
                        lambda pid, ma: (None, "missing"))
    monkeypatch.setattr("ozon_collector.gather.C.collect", lambda **kw: raw)
    res = run_gather({"pid": "42", "url": "https://www.ozon.ru/product/42",
                      "max_age_s": 86400, "no_ocr": True})
    assert res["ok"] is False
    assert res["error_kind"] == "structure"
    assert res["recyclable"] is False
