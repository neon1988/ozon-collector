"""Тесты кеша raw: parse_max_age, _parse_utc, cache_check, внешняя сессия."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from ozon_collector.cache import (
    parse_max_age,
    _parse_utc,
    _version_tuple,
    version_status,
    raw_path,
    raw_save_path,
    cache_is_compatible,
    cache_check,
    cached_version,
    _raw_candidates,
)
from ozon_collector.collect import collect
from ozon_collector import VERSION


def _now_utc_str(**delta):
    dt = datetime.now(timezone.utc) + timedelta(**delta)
    return dt.isoformat(timespec="seconds")


# ---- parse_max_age ---------------------------------------------------------

def test_parse_max_age_ok():
    assert parse_max_age("7d") == 604800
    assert parse_max_age("1d") == 86400
    assert parse_max_age("12h") == 43200
    assert parse_max_age("6h") == 21600
    assert parse_max_age("0") == 0
    assert parse_max_age("") == 0


def test_parse_max_age_bad():
    for bad in ("1.5d", "1h30m", "abc", "d", " 2x ", "1d2h"):
        with pytest.raises(ValueError):
            parse_max_age(bad)


# ---- _parse_utc ------------------------------------------------------------

def test_parse_utc_aware():
    dt = _parse_utc("2026-08-08T10:02:10+00:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_utc_z_suffix():
    dt = _parse_utc("2026-08-08T10:02:10Z")
    assert dt is not None
    assert dt.utcoffset() == timedelta(0)


def test_parse_utc_naive_assumes_utc():
    dt = _parse_utc("2026-08-08T10:02:10")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)


def test_parse_utc_broken():
    for bad in ("", "not-a-date", None, 42):
        assert _parse_utc(bad) is None


# ---- версии ----------------------------------------------------------------

def test_version_tuple_parse():
    assert _version_tuple("1.10.0") == (1, 10, 0)
    assert _version_tuple("1.5.0") == (1, 5, 0)
    assert _version_tuple("2.0") == (2, 0)


def test_version_tuple_bad():
    for bad in ("", "  ", None, 42, "abc", "1.x.0"):
        assert _version_tuple(bad) is None


def test_version_status():
    assert version_status(VERSION) == "ok"
    assert version_status("1.5.0") == "old"
    assert version_status("1.0.0") == "old"
    assert version_status("2.0.0") == "newer"
    assert version_status(None) == "invalid"
    assert version_status("bogus") == "invalid"


# ---- cache_is_compatible ---------------------------------------------------

def test_cache_is_compatible():
    assert cache_is_compatible({"schema_version": "2.0",
                                "collector_version": VERSION})
    assert not cache_is_compatible({"schema_version": "2.0"})  # версия отсутствует
    assert not cache_is_compatible({"schema_version": "2.0",
                                    "collector_version": "1.5.0"})  # старее
    assert not cache_is_compatible({"schema_version": "2.0",
                                    "collector_version": "2.0.0"})  # новее
    assert not cache_is_compatible({"schema_version": "1.0",
                                    "collector_version": VERSION})
    assert not cache_is_compatible({})


# ---- cache_check -----------------------------------------------------------

@pytest.fixture
def fresh_raw(tmp_path):
    """Свежий полный raw: schema 2.0, версия == текущей, incomplete=False,
    валидный timestamp."""
    raw = {
        "schema_version": "2.0",
        "collector_version": VERSION,
        "pid": "42",
        "incomplete": False,
        "collected_at_utc": _now_utc_str(minutes=-5),
    }
    with open(os.path.join(tmp_path, "42_raw.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    return tmp_path


def test_cache_check_off_does_not_read(tmp_path, monkeypatch):
    called = {"stat": False}

    def fake_exists(p):
        called["stat"] = True
        return False

    monkeypatch.setattr(os.path, "exists", fake_exists)
    raw, reason = cache_check("1", 0, str(tmp_path))
    assert raw is None and reason == "off"
    assert not called["stat"]


def test_cache_check_missing(tmp_path):
    raw, reason = cache_check("999", 86400, str(tmp_path))
    assert raw is None and reason == "missing"


def test_cache_check_hit(fresh_raw):
    raw, reason = cache_check("42", 86400, str(fresh_raw))
    assert reason == "hit"
    assert raw["pid"] == "42"


def test_cache_check_stale(fresh_raw):
    raw, reason = cache_check("42", 60, str(fresh_raw))
    assert raw is None and reason == "stale"


def test_cache_check_incomplete(tmp_path):
    raw = {
        "schema_version": "2.0", "pid": "7", "incomplete": True,
        "collected_at_utc": _now_utc_str(minutes=-1),
    }
    with open(os.path.join(tmp_path, "7_raw.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    r, reason = cache_check("7", 86400, str(tmp_path))
    assert r is None and reason == "incomplete"


def test_cache_check_bad_timestamp(tmp_path):
    raw = {"schema_version": "2.0", "collector_version": VERSION,
           "pid": "8", "incomplete": False,
           "collected_at_utc": "garbage"}
    with open(os.path.join(tmp_path, "8_raw.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    r, reason = cache_check("8", 86400, str(tmp_path))
    assert r is None and reason == "bad_timestamp"


def test_cache_check_missing_timestamp(tmp_path):
    raw = {"schema_version": "2.0", "collector_version": VERSION,
           "pid": "9", "incomplete": False}
    with open(os.path.join(tmp_path, "9_raw.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    r, reason = cache_check("9", 86400, str(tmp_path))
    assert r is None and reason == "bad_timestamp"


def test_cache_check_incompatible(tmp_path):
    raw = {"schema_version": "1.0", "collector_version": VERSION,
           "pid": "10", "incomplete": False,
           "collected_at_utc": _now_utc_str(minutes=-1)}
    with open(os.path.join(tmp_path, "10_raw.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    r, reason = cache_check("10", 86400, str(tmp_path))
    assert r is None and reason == "incompatible"


def test_cache_check_old_version(tmp_path):
    raw = {"schema_version": "2.0", "collector_version": "1.5.0",
           "pid": "11", "incomplete": False,
           "collected_at_utc": _now_utc_str(minutes=-1)}
    with open(os.path.join(tmp_path, "11_raw.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    r, reason = cache_check("11", 86400, str(tmp_path))
    assert r is None and reason == "old_version"
    assert cached_version("11", str(tmp_path)) == "1.5.0"


def test_cache_check_missing_version(tmp_path):
    raw = {"schema_version": "2.0", "pid": "12", "incomplete": False,
           "collected_at_utc": _now_utc_str(minutes=-1)}
    with open(os.path.join(tmp_path, "12_raw.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    r, reason = cache_check("12", 86400, str(tmp_path))
    assert r is None and reason == "old_version"


def test_cache_check_newer_version(tmp_path):
    raw = {"schema_version": "2.0", "collector_version": "2.0.0",
           "pid": "13", "incomplete": False,
           "collected_at_utc": _now_utc_str(minutes=-1)}
    with open(os.path.join(tmp_path, "13_raw.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    r, reason = cache_check("13", 86400, str(tmp_path))
    assert r is None and reason == "newer_version"


def test_cache_check_hit_newer_is_miss(tmp_path):
    """Версия новее текущей НЕ берётся из кеша (защита от старого кода
    поверх данных новой версии)."""
    raw = {"schema_version": "2.0", "collector_version": "3.0.0",
           "pid": "14", "incomplete": False,
           "collected_at_utc": _now_utc_str(minutes=-1)}
    with open(os.path.join(tmp_path, "14_raw.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    r, reason = cache_check("14", 86400, str(tmp_path))
    assert r is None and reason == "newer_version"


def test_cached_version_missing_file(tmp_path):
    assert cached_version("nope", str(tmp_path)) is None


# ---- discovery: канон + timestamp-файлы -------------------------------------

def _write_raw(tmp_path, pid, fname, version=VERSION, incomplete=False,
               reviews=0, minutes=-5, schema="2.0"):
    raw = {
        "schema_version": schema,
        "collector_version": version,
        "pid": pid,
        "incomplete": incomplete,
        "reviews_collected": reviews,
        "collected_at_utc": _now_utc_str(minutes=minutes),
    }
    with open(os.path.join(tmp_path, fname), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def test_raw_candidates_canonical_first_then_timestamp(tmp_path):
    _write_raw(tmp_path, "77", "77_raw.json")
    _write_raw(tmp_path, "77", "77_raw.20260815-203144.1.json")
    _write_raw(tmp_path, "77", "77_raw.20260815-220702.1.json")
    cands = _raw_candidates("77", str(tmp_path))
    assert cands[0].endswith("77_raw.json")
    assert len(cands) == 3
    assert all(c.startswith(str(tmp_path)) for c in cands)


def test_raw_candidates_ignores_other_pids(tmp_path):
    _write_raw(tmp_path, "77", "77_raw.json")
    _write_raw(tmp_path, "77", "77_raw.20260815-203144.1.json")
    _write_raw(tmp_path, "99", "99_raw.json")
    _write_raw(tmp_path, "770", "770_raw.json")     # префикс, но другой pid
    assert len(_raw_candidates("77", str(tmp_path))) == 2


def test_raw_candidates_missing_dir(tmp_path):
    assert _raw_candidates("77", os.path.join(str(tmp_path), "nope")) == []


def test_cache_check_picks_valid_timestamp_over_old_canonical(tmp_path):
    """Канон старой версии (1.5.0), но timestamp-файл текущей версии свежий →
    hit по timestamp, без похода в Ozon."""
    _write_raw(tmp_path, "77", "77_raw.json", version="1.5.0", reviews=1)
    _write_raw(tmp_path, "77", "77_raw.20260815-203144.1.json",
               reviews=78, minutes=-10)
    raw, reason = cache_check("77", 86400, str(tmp_path))
    assert reason == "hit"
    assert raw["reviews_collected"] == 78


def test_cache_check_picks_newest_valid_timestamp(tmp_path):
    """Несколько валидных timestamp с отзывами: предпочтение — свежайший."""
    _write_raw(tmp_path, "77", "77_raw.20260815-203144.1.json",
               reviews=78, minutes=-120)
    _write_raw(tmp_path, "77", "77_raw.20260815-211744.1.json",
               reviews=75, minutes=-60)
    raw, reason = cache_check("77", 86400, str(tmp_path))
    assert reason == "hit"
    assert raw["reviews_collected"] == 75


def test_cache_check_prefers_reviews_over_newer_empty(tmp_path):
    """Регресс: свежий stress-прогон с 0 отзывов НЕ вытесняет полный raw
    с отзывами только из-за более поздней даты (ранжирование кеша)."""
    _write_raw(tmp_path, "77", "77_raw.20260815-211744.1.json",
               reviews=0, minutes=-60)
    _write_raw(tmp_path, "77", "77_raw.20260815-210856.1.json",
               reviews=97, minutes=-120)
    raw, reason = cache_check("77", 86400, str(tmp_path))
    assert reason == "hit"
    assert raw["reviews_collected"] == 97


def test_cache_check_prefers_more_reviews_at_same_date(tmp_path):
    """Одинаковая дата: больший reviews_collected предпочтительнее."""
    ts = _now_utc_str(minutes=-120)
    _write_raw(tmp_path, "77", "77_raw.20260815-210856.1.json",
               reviews=96, minutes=0)
    path1 = os.path.join(tmp_path, "77_raw.20260815-210856.1.json")
    with open(path1, "r", encoding="utf-8") as f:
        raw = json.load(f)
    raw["collected_at_utc"] = ts
    with open(path1, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    _write_raw(tmp_path, "77", "77_raw.20260815-210856.2.json",
               reviews=97, minutes=0)
    path2 = os.path.join(tmp_path, "77_raw.20260815-210856.2.json")
    with open(path2, "r", encoding="utf-8") as f:
        raw = json.load(f)
    raw["collected_at_utc"] = ts
    with open(path2, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    r, reason = cache_check("77", 86400, str(tmp_path))
    assert reason == "hit"
    assert r["reviews_collected"] == 97


def test_cache_check_skips_incomplete_timestamp(tmp_path):
    """Свежий timestamp, но неполный (челлендж-прогон) → не берётся; канон
    старой версии тоже → miss old_version."""
    _write_raw(tmp_path, "77", "77_raw.json", version="1.5.0", reviews=1)
    _write_raw(tmp_path, "77", "77_raw.20260815-220702.1.json",
               incomplete=True, reviews=0, minutes=-5)
    raw, reason = cache_check("77", 86400, str(tmp_path))
    assert raw is None and reason == "old_version"


def test_cache_check_hit_from_timestamp_only(tmp_path):
    """Канона нет вовсе, но есть свежий валидный timestamp-файл → hit."""
    _write_raw(tmp_path, "77", "77_raw.20260815-203144.1.json", reviews=96)
    raw, reason = cache_check("77", 86400, str(tmp_path))
    assert reason == "hit"
    assert raw["reviews_collected"] == 96


def test_cache_check_pid_mismatch_rejected(tmp_path):
    """Файл с чужим pid внутри (битый/подменённый) → не берётся."""
    _write_raw(tmp_path, "77", "77_raw.json", reviews=5)
    path = os.path.join(tmp_path, "77_raw.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    raw["pid"] = "99"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    r, reason = cache_check("77", 86400, str(tmp_path))
    assert r is None and reason == "pid_mismatch"


def test_cache_check_valid_timestamp_beats_pid_mismatch_canonical(tmp_path):
    """Канон с чужим pid, но свежий валидный timestamp → hit по timestamp."""
    _write_raw(tmp_path, "77", "77_raw.json", reviews=5)
    path = os.path.join(tmp_path, "77_raw.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    raw["pid"] = "99"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    _write_raw(tmp_path, "77", "77_raw.20260815-203144.1.json",
               reviews=78, minutes=-10)
    r, reason = cache_check("77", 86400, str(tmp_path))
    assert reason == "hit"
    assert r["reviews_collected"] == 78


# ---- внешняя сессия в collect() -------------------------------------------

class _FakeSession:
    def __init__(self):
        self.closed = False
        self.page = object()

    def close(self):
        self.closed = True


def test_collect_reuses_external_session(monkeypatch):
    """collect(sess=X) не создаёт свою Session и не закрывает внешнюю."""
    created = []

    def fake_session(cdp_url):
        created.append(cdp_url)
        return _FakeSession()

    monkeypatch.setattr("ozon_collector.collect.Session", fake_session)
    external = _FakeSession()
    monkeypatch.setattr("ozon_collector.collect.collect_product",
                        lambda page, url, category=None: {})
    monkeypatch.setattr("ozon_collector.collect.collect_reviews",
                        lambda page, url, variant, max_pages, min_reviews,
                        apply_variant_mode, sorts: ([], 1, [], None))
    monkeypatch.setattr("ozon_collector.collect.pid_from_url",
                        lambda url: "42")

    with pytest.raises(Exception):
        # dom snapshot отсутствует — exception, но finally должен НЕ закрыть
        collect(product_url="https://www.ozon.ru/product/42", sess=external)

    assert created == []
    assert not external.closed


def test_collect_owns_own_session(monkeypatch):
    """Без внешней сессии collect() создаёт и закрывает свою."""
    closed = []
    made = []

    class _S:
        def __init__(self):
            self.page = object()

        def close(self):
            closed.append(True)

    def fake_session(cdp_url):
        made.append(cdp_url)
        return _S()

    monkeypatch.setattr("ozon_collector.collect.Session", fake_session)
    monkeypatch.setattr("ozon_collector.collect.collect_product",
                        lambda page, url, category=None: {})
    monkeypatch.setattr("ozon_collector.collect.collect_reviews",
                        lambda page, url, variant, max_pages, min_reviews,
                        apply_variant_mode, sorts: ([], 1, [], None))
    monkeypatch.setattr("ozon_collector.collect.pid_from_url",
                        lambda url: "42")

    with pytest.raises(Exception):
        collect(product_url="https://www.ozon.ru/product/42")
    assert len(made) == 1
    assert closed == [True]


def test_raw_path():
    assert raw_path("42") == os.path.join("data", "42_raw.json")


def test_raw_save_path_returns_canonical_when_absent(tmp_path):
    """Нет существующего raw → канонический data/{pid}_raw.json."""
    p = raw_save_path("42", data_dir=str(tmp_path))
    assert p == os.path.join(str(tmp_path), "42_raw.json")


def test_raw_save_path_no_overwrite(tmp_path):
    """Существующий raw НЕ перезаписывается: новый путь с суффиксом."""
    canon = tmp_path / "42_raw.json"
    canon.write_text('{"pid": "42", "reviews": []}', encoding="utf-8")
    p = raw_save_path("42", data_dir=str(tmp_path))
    assert p != str(canon)
    assert os.path.basename(p).startswith("42_raw.")
    assert p.endswith(".json")
    # оригинал не тронут
    assert canon.read_text(encoding="utf-8").startswith('{"pid": "42"')


def test_raw_save_path_unique_collision(tmp_path):
    """После записи run-id файла следующий вызов даёт другой файл (счётчик i)."""
    canon = tmp_path / "42_raw.json"
    canon.write_text("x", encoding="utf-8")
    p1 = raw_save_path("42", data_dir=str(tmp_path))
    tmp_path.joinpath(os.path.basename(p1)).write_text("data", encoding="utf-8")
    p2 = raw_save_path("42", data_dir=str(tmp_path))
    assert p1 != p2


def test_raw_save_path_overwrite_force(tmp_path):
    """overwrite=True → всегда канонический путь."""
    canon = tmp_path / "42_raw.json"
    canon.write_text("old", encoding="utf-8")
    assert raw_save_path("42", data_dir=str(tmp_path),
                         overwrite=True) == str(canon)
