"""Тесты run_gather: кеш-hit логирование, инвалидация по версии, стадии."""
from __future__ import annotations

import pytest

from ozon_collector.gather import run_gather


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
