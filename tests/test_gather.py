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
                        lambda pid, ma, **kw: (_raw(), "hit"))
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
                        lambda pid, ma, **kw: (None, "old_version"))
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
                        lambda pid, ma, **kw: (None, "newer_version"))
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
                        lambda pid, ma, **kw: (None, "missing"))
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
                        lambda pid, ma, **kw: (None, "missing"))
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
                        lambda pid, ma, **kw: (None, "missing"))
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
                        lambda pid, ma, **kw: (raw, "hit"))
    res = run_gather({"pid": "42", "max_age_s": 86400, "no_ocr": True})
    assert res["ok"] is False
    assert res["error_kind"] == "access_challenge"
    assert res["failure_type"] == "access_failure"   # НЕ product_failure (K4/ADR)
    assert res["recyclable"] is True


def test_run_gather_broken_structure_fails(monkeypatch, capsys):
    """Битая структура (product пуст) → StructureError, НЕ recyclable."""
    raw = _raw()
    raw["product"] = {}
    monkeypatch.setattr("ozon_collector.gather.K.cache_check",
                        lambda pid, ma, **kw: (None, "missing"))
    monkeypatch.setattr("ozon_collector.gather.C.collect", lambda **kw: raw)
    res = run_gather({"pid": "42", "url": "https://www.ozon.ru/product/42",
                      "max_age_s": 86400, "no_ocr": True})
    assert res["ok"] is False
    assert res["error_kind"] == "structure"
    assert res["recyclable"] is False


# ---- проверка брака варианта/числа отзывов (2026-08-21) ----------------------

from ozon_collector.collect import variant_review_problems


def test_variant_problems_short_count():
    raw = _raw()
    raw["reviews"] = [{"uuid": f"u{i}"} for i in range(8)]
    raw["reviews_variant_mode"] = {"used": True, "variant_count": 64}
    assert variant_review_problems(raw) == ["short(8/64)"]


def test_variant_problems_wall_boundary_tolerance():
    """vc=100 (граница стены): недобор в пределах допуска 3% — не брак.

    Реальный кейс 4483627458: собрано 98/100, без логина в DOM видно
    меньше; требовать «ровно vc» у границы нельзя."""
    raw = _raw()
    raw["reviews"] = [{"uuid": f"u{i}"} for i in range(98)]
    raw["reviews_variant_mode"] = {"used": True, "variant_count": 100}
    assert variant_review_problems(raw) == []


def test_variant_problems_wall_boundary_beyond_tolerance():
    """Недобор больше допуска (vc=100, tol=3 -> порог 97) всё ещё брак."""
    raw = _raw()
    raw["reviews"] = [{"uuid": f"u{i}"} for i in range(96)]
    raw["reviews_variant_mode"] = {"used": True, "variant_count": 100}
    assert variant_review_problems(raw) == ["short(96/100)"]


def test_variant_problems_zero_reviews_variant():
    """Явный variant_count=0 при used=True: вариант без отзывов — не брак,
    даже если rating_count всего товара большой (кейс 5575856356)."""
    raw = _raw()
    raw["reviews"] = []
    raw["reviews_variant_mode"] = {"used": True, "variant_count": 0}
    raw["product"] = {"rating_count": 370}
    assert variant_review_problems(raw) == []


def test_variant_problems_small_count_tolerance():
    """Малый vc=30: tol=max(1, round(0.9))=1 -> 29/30 ок, 28/30 брак."""
    ok_raw = _raw()
    ok_raw["reviews"] = [{"uuid": f"u{i}"} for i in range(29)]
    ok_raw["reviews_variant_mode"] = {"used": True, "variant_count": 30}
    assert variant_review_problems(ok_raw) == []

    bad_raw = _raw()
    bad_raw["reviews"] = [{"uuid": f"u{i}"} for i in range(28)]
    bad_raw["reviews_variant_mode"] = {"used": True, "variant_count": 30}
    assert variant_review_problems(bad_raw) == ["short(28/30)"]


def test_variant_problems_large_cap_ok():
    """vc > 100: без логина видно ~96-100 — 96 отзывов это норма."""
    raw = _raw()
    raw["reviews"] = [{"uuid": f"u{i}"} for i in range(96)]
    raw["reviews_variant_mode"] = {"used": True, "variant_count": 2829}
    assert variant_review_problems(raw) == []


def test_variant_problems_short_sort_masked_by_total():
    """vc > 100: одна сортировка 30, вторая 96 — брак по относительному
    отставанию (30 < 70% от 96), даже если сумма >= 90."""
    raw = _raw()
    reviews = [{"uuid": f"a{i}", "review_sorts": ["published_at_desc"]}
               for i in range(96)]
    reviews += [{"uuid": f"b{i}", "review_sorts": ["score_asc"]}
                for i in range(30)]
    raw["reviews"] = reviews
    raw["reviews_variant_mode"] = {"used": True, "variant_count": 2829}
    problems = variant_review_problems(raw)
    assert len(problems) == 1
    assert "short_sort(score_asc=30" in problems[0]


def test_variant_problems_wall_off_by_one_ok():
    """Реальный кейс 3164773737: стена логина дала score_asc=89 при
    published_at_desc=96 — это НЕ брак (89 >= 70% от 96)."""
    raw = _raw()
    reviews = [{"uuid": f"a{i}", "review_sorts": ["published_at_desc"]}
               for i in range(96)]
    reviews += [{"uuid": f"b{i}", "review_sorts": ["score_asc"]}
                for i in range(89)]
    raw["reviews"] = reviews
    raw["reviews_variant_mode"] = {"used": True, "variant_count": 1616}
    assert variant_review_problems(raw) == []


def test_variant_problems_both_sorts_ok():
    """vc > 100: обе сортировки >= 90 — ок, даже если суммы разнятся."""
    raw = _raw()
    reviews = [{"uuid": f"a{i}", "review_sorts": ["published_at_desc",
                                                   "score_asc"]}
               for i in range(80)]
    reviews += [{"uuid": f"b{i}", "review_sorts": ["published_at_desc"]}
                for i in range(20)]
    reviews += [{"uuid": f"c{i}", "review_sorts": ["score_asc"]}
                for i in range(16)]
    raw["reviews"] = reviews
    raw["reviews_variant_mode"] = {"used": True, "variant_count": 500}
    # published_at_desc: 100, score_asc: 96 — обе >= 90
    assert variant_review_problems(raw) == []


def test_variant_problems_single_pass_no_sort_check():
    """Один проход (без --sorts): review_sorts нет — per-sort не проверяется."""
    raw = _raw()
    raw["reviews"] = [{"uuid": f"u{i}"} for i in range(95)]
    raw["reviews_variant_mode"] = {"used": True, "variant_count": 2000}
    assert variant_review_problems(raw) == []


def test_variant_problems_no_widget_uses_rating_count():
    """Без тумблера варианта ориентир — rating_count из карточки:
    85 собрано из 465 -> брак (как 2859778701)."""
    raw = _raw()
    raw["reviews"] = [{"uuid": f"u{i}"} for i in range(85)]
    raw["product"]["rating_count"] = 465
    raw["reviews_variant_mode"] = {"used": False, "reason": "no_widget",
                                   "variant_count": None}
    problems = variant_review_problems(raw)
    assert len(problems) == 1
    assert "short(85/465" in problems[0]


def test_variant_problems_no_widget_rating_count_ok():
    """Без тумблера, rating_count > 100, обе сортировки >= 90 — ок."""
    raw = _raw()
    reviews = [{"uuid": f"a{i}", "review_sorts": ["published_at_desc"]}
               for i in range(96)]
    reviews += [{"uuid": f"b{i}", "review_sorts": ["score_asc"]}
                for i in range(92)]
    raw["reviews"] = reviews
    raw["product"]["rating_count"] = 1616
    raw["reviews_variant_mode"] = {"used": False, "reason": "no_widget",
                                   "variant_count": None}
    assert variant_review_problems(raw) == []


def test_variant_problems_no_widget_rating_count_short_sort():
    """Без тумблера: одна сортировка 50 при лучшей 96 (<70%) — брак."""
    raw = _raw()
    reviews = [{"uuid": f"a{i}", "review_sorts": ["published_at_desc"]}
               for i in range(96)]
    reviews += [{"uuid": f"b{i}", "review_sorts": ["score_asc"]}
                for i in range(50)]
    raw["reviews"] = reviews
    raw["product"]["rating_count"] = 1616
    raw["reviews_variant_mode"] = {"used": False, "reason": "no_widget",
                                   "variant_count": None}
    problems = variant_review_problems(raw)
    assert len(problems) == 1
    assert "short_sort(score_asc=50" in problems[0]


def test_variant_problems_no_counts_no_check():
    """Ни variant_count, ни rating_count — числовой проверки нет."""
    raw = _raw()
    raw["reviews_variant_mode"] = {"used": False, "reason": "no_widget"}
    assert variant_review_problems(raw) == []


def test_variant_problems_no_widget_ok():
    """no_widget — легитимно (тумблера нет), браком не считается."""
    raw = _raw()
    raw["reviews_variant_mode"] = {"used": False, "reason": "no_widget"}
    assert variant_review_problems(raw) == []


def test_variant_problems_click_failure():
    raw = _raw()
    raw["reviews_variant_mode"] = {"used": False, "reason": "click_no_effect"}
    problems = variant_review_problems(raw)
    assert len(problems) == 1
    assert "click_no_effect" in problems[0]


def test_run_gather_short_collect_raises_variant_short(monkeypatch, capsys):
    """Свежий сбор с 8/64 → VariantShortError, recyclable, raw удалён."""
    raw = _raw()
    raw["reviews"] = [{"uuid": f"u{i}"} for i in range(8)]
    raw["reviews_variant_mode"] = {"used": True, "variant_count": 64}
    deleted = []
    monkeypatch.setattr("ozon_collector.gather.K.cache_check",
                        lambda pid, ma, **kw: (None, "missing"))
    monkeypatch.setattr("ozon_collector.gather.C.collect", lambda **kw: raw)
    monkeypatch.setattr("ozon_collector.gather._delete_raw_file",
                        lambda p: deleted.append(p))
    res = run_gather({"pid": "42", "url": "https://www.ozon.ru/product/42",
                      "max_age_s": 86400, "no_ocr": True})
    out = capsys.readouterr().out
    assert res["ok"] is False
    assert res["error_kind"] == "variant_short"
    assert res["recyclable"] is True
    assert "short(8/64)" in out
    assert deleted  # битый raw удаляется, в кеш не попадёт


def test_run_gather_cache_hit_short_recollects(monkeypatch, capsys):
    """Кеш-хит с браком игнорируется → живой пересбор, cache=miss."""
    short = _raw()
    short["reviews"] = [{"uuid": f"u{i}"} for i in range(8)]
    short["reviews_variant_mode"] = {"used": True, "variant_count": 64}
    good = _raw()
    good["reviews_variant_mode"] = {"used": True, "variant_count": 2}
    calls = []
    monkeypatch.setattr("ozon_collector.gather.K.cache_check",
                        lambda pid, ma, **kw: (short, "hit"))
    monkeypatch.setattr("ozon_collector.gather.C.collect",
                        lambda **kw: calls.append(1) or good)
    monkeypatch.setattr("ozon_collector.gather.R.render",
                        lambda r, **kw: "# ok")
    res = run_gather({"pid": "42", "url": "https://www.ozon.ru/product/42",
                      "max_age_s": 86400, "no_ocr": True})
    out = capsys.readouterr().out
    assert calls  # живой сбор был
    assert res["ok"] is True
    assert res["cache"] == "miss"
    assert "кеш-хит с браком" in out
