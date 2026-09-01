"""Тесты top-up добора виртуализованных карточек (_topup_virtualized)
и раннего выхода при variant_count=0 (_collect_reviews_pass)."""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ozon_collector import collect as C


def _rev(uid, text="текст отзыва"):
    return {"uuid": uid, "text": text, "stars": 5, "variant": "Спрей"}


class FakePage:
    def __init__(self, payloads, ups, downs=None, cards=0):
        self.payloads = payloads
        self.ups = ups
        self.downs = downs or []
        self.cards = cards
        self.i = 0
        self.j = 0
        self.k = 0
        self.waits = []
        self.gotos = []

    def evaluate(self, js, *a, **k):
        if js == C._CARDS_COUNT_JS:
            return self.cards
        if "step-up" in js:
            v = self.ups[min(self.j, len(self.ups) - 1)]
            self.j += 1
            return v
        if "step-down" in js:
            v = self.downs[min(self.k, len(self.downs) - 1)]
            self.k += 1
            return v
        v = self.payloads[min(self.i, len(self.payloads) - 1)]
        self.i += 1
        return v

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def goto(self, url, **k):
        self.gotos.append(url)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(C, "scroll_pause_ms", lambda: 0)
    monkeypatch.setattr(C, "_expand_review_texts", lambda page, **k: 0)
    monkeypatch.setattr(C, "_expand_review_comments", lambda page, **k: 0)


def _initial(n=30):
    reviews = [_rev(f"u{i:02d}") for i in range(n)]
    reviews[2]["text"] = ""
    seen = {C._review_key(r) for r in reviews}
    index = {C._review_key(r): r for r in reviews}
    return reviews, seen, index


def test_topup_collects_unmounted_cards():
    reviews, seen, index = _initial(30)
    page = FakePage(
        payloads=[
            {"ok": True, "reviews": [_rev("u30")]},
            {"ok": True, "reviews": [_rev("u31"), _rev("u32")]},
        ],
        ups=[False, False],
    )
    added = C._topup_virtualized(page, 33, seen, index, reviews, {}, page_no=1)
    assert added == 3
    assert len(reviews) == 33
    assert page.j == 2
    keys = {C._review_key(r) for r in reviews}
    assert {"u30", "u31", "u32"} <= keys


def test_topup_recovers_seen_but_uncollected():
    reviews, seen, index = _initial(30)
    for i in range(30, 34):
        seen.add(f"u{i:02d}")
    page = FakePage(
        payloads=[
            {"ok": True, "reviews": [_rev("u30"), _rev("u31")]},
            {"ok": True, "reviews": [_rev("u32"), _rev("u33")]},
        ],
        ups=[False, False],
    )
    added = C._topup_virtualized(page, 34, seen, index, reviews, {}, page_no=1)
    assert added == 4
    assert len(reviews) == 34


def test_topup_refreshes_truncated_text():
    reviews, seen, index = _initial(30)
    fuller = dict(_rev("u02", text="полный текст после дораскрытия"))
    page = FakePage(
        payloads=[{"ok": True, "reviews": [fuller]}],
        ups=[False],
    )
    C._topup_virtualized(page, 34, seen, index, reviews, {}, page_no=1)
    assert reviews[2]["text"] == "полный текст после дораскрытия"


def test_final_reextract_fills_fields_after_expand(monkeypatch):
    reviews, seen, index = _initial(30)
    page = FakePage(payloads=[], ups=[True], downs=[True])
    monkeypatch.setattr(C, "_expand_review_texts", lambda page, **k: 2)
    monkeypatch.setattr(C, "_expand_review_comments", lambda page, **k: 0)
    page.payloads = [{"ok": True, "reviews": [
        dict(_rev("u05", text="развернутый текст")),
        {"uuid": "u07", "text": "", "stars": 5, "variant": "Спрей",
         "helpful_yes": 3},
    ]}]
    C._topup_virtualized(page, 34, seen, index, reviews, {}, page_no=1)
    assert reviews[5]["text"] == "развернутый текст"
    assert reviews[7]["helpful_yes"] == 3
    assert reviews[7]["text"] == "текст отзыва"


def test_phase2_walks_down_when_deficit_remains():
    reviews, seen, index = _initial(30)
    page = FakePage(
        payloads=[{"ok": True, "reviews": [_rev("u30")]}],
        ups=[True],
        downs=[False, False, True],
    )
    added = C._topup_virtualized(page, 99, seen, index, reviews, {}, page_no=1)
    assert added == 1
    assert len(reviews) == 31
    assert page.j == 1
    assert page.k == 3


def test_phase2_stops_at_bottom():
    reviews, seen, index = _initial(30)
    page = FakePage(
        payloads=[{"ok": True, "reviews": []}],
        ups=[True],
        downs=[True],
    )
    added = C._topup_virtualized(page, 99, seen, index, reviews, {}, page_no=1)
    assert added == 0
    assert page.j == 1
    assert page.k == 1


def test_topup_variant_filter_applies():
    reviews, seen, index = _initial(30)
    page = FakePage(
        payloads=[{"ok": True, "reviews": [
            _rev("u30"),
            {"uuid": "uX", "text": "чужой вариант", "stars": 4,
             "variant": "Мазь, 50 мл"},
        ]}],
        ups=[True],
        downs=[True],
    )
    tv = {"Размер": "Спрей"}
    added = C._topup_virtualized(page, 99, seen, index, reviews, tv, page_no=1)
    assert added == 1
    texts = [r["uuid"] for r in reviews]
    assert "uX" not in texts


# ---- ранний выход при явном variant_count=0 (2026-08-24) ---------------------

def test_pass_zero_variant_count_skips_scroll(monkeypatch):
    """used=True и явный 0 от виджета: проход сразу возвращает пустой
    результат без converge/релоадов (кейсы 5274359430, 5575856356)."""
    def boom(*a, **k):
        raise AssertionError("прокрутка не должна запускаться при vc=0")
    monkeypatch.setattr(C, "_scroll_to_converge", boom)
    monkeypatch.setattr(C, "detect_challenge", lambda page, *a, **k: None)
    monkeypatch.setattr(C, "_pass_age_gate", lambda page: False)
    monkeypatch.setattr(
        C, "_activate_variant_mode",
        lambda page: {"used": True, "variant_count": 0})
    page = FakePage(payloads=[], ups=[], downs=[])
    reviews, tp, page_log, vm = C._collect_reviews_pass(
        page, "https://x/?tab=reviews", "published_at_desc", {},
        apply_variant_mode=True,
    )
    assert reviews == []
    assert tp == 1
    assert len(page_log) == 1
    assert page_log[0]["stop_reason"] == "variant_zero_reviews"
    assert page_log[0]["ok"] is True
    assert vm == {"used": True, "variant_count": 0}


def test_collect_reviews_multi_sort_breaks_on_zero(monkeypatch):
    """После первого прохода с vc=0 остальные сортировки не запускаются."""
    calls = {"n": 0}

    def fake_pass(page, url, sort, target_variant, **k):
        calls["n"] += 1
        return [], 1, [{"page": 1, "n_found": 0, "n_matched": 0,
                        "ok": True, "stop_reason": "variant_zero_reviews",
                        "sort": sort}], {"used": True, "variant_count": 0}

    monkeypatch.setattr(C, "_collect_reviews_pass", fake_pass)
    reviews, tp, page_log, vm = C.collect_reviews(
        None, "https://x/?tab=reviews", {}, sorts=["published_at_desc",
                                                   "score_asc"],
    )
    assert calls["n"] == 1
    assert reviews == []
    assert vm.get("variant_count") == 0


def test_pass_zero_without_used_still_scrolls(monkeypatch):
    """vm.used=False при variant_count=0 — виджету не доверяем, обычный путь
    (заглушка: converge вернул 0, релоадов ждём EMPTY_RETRY_PASSES раз)."""
    zero = [0]

    def fake_converge(page, **k):
        return 0, False

    monkeypatch.setattr(C, "_scroll_to_converge", fake_converge)
    monkeypatch.setattr(C, "detect_challenge", lambda page, *a, **k: None)

    class P(FakePage):
        def __init__(self):
            super().__init__(payloads=[], ups=[], downs=[])
            self.reloads = 0

        def reload(self, **k):
            self.reloads += 1

    monkeypatch.setattr(C, "_activate_variant_mode",
                        lambda page: {"used": False, "variant_count": 0})
    monkeypatch.setattr(C, "_pass_age_gate", lambda page: False)
    monkeypatch.setattr(C, "EMPTY_RETRY_PASSES", 0)
    page = P()
    reviews, tp, page_log, vm = C._collect_reviews_pass(
        page, "https://x/?tab=reviews", "score_asc", {},
        apply_variant_mode=True,
    )
    assert reviews == []
    assert page_log and page_log[-1]["stop_reason"] == "no_scroll"


def test_pass_no_cards_no_hint_skips_scroll(monkeypatch):
    """no_count_hint=True и 0 карточек на странице: проход завершается
    сразу, без converge/релоадов (кейс 4551551032 — ни рейтинга,
    ни виджета варианта, раздел пуст)."""
    def boom(*a, **k):
        raise AssertionError("прокрутка не должна запускаться без карточек")
    monkeypatch.setattr(C, "_scroll_to_converge", boom)
    monkeypatch.setattr(C, "detect_challenge", lambda page, *a, **k: None)
    monkeypatch.setattr(C, "_pass_age_gate", lambda page: False)
    monkeypatch.setattr(
        C, "_activate_variant_mode",
        lambda page: {"used": False, "variant_count": None})
    page = FakePage(payloads=[], ups=[], downs=[], cards=0)
    reviews, tp, page_log, vm = C._collect_reviews_pass(
        page, "https://x/?tab=reviews", "published_at_desc", {},
        apply_variant_mode=True, no_count_hint=True,
    )
    assert reviews == []
    assert page_log[-1]["stop_reason"] == "no_cards_no_count_hint"
    assert vm == {"used": False, "variant_count": None}


def test_pass_cards_present_with_hint_still_scrolls(monkeypatch):
    """no_count_hint=True, но карточки есть (parse-gap рейтинга у товара
    с отзывами) — обычный путь сбора сохраняется."""
    seen_calls = {"n": 0}

    def fake_converge(page, **k):
        seen_calls["n"] += 1
        return 2, False

    monkeypatch.setattr(C, "_scroll_to_converge", fake_converge)
    monkeypatch.setattr(C, "detect_challenge", lambda page, *a, **k: None)
    monkeypatch.setattr(C, "_pass_age_gate", lambda page: False)
    monkeypatch.setattr(
        C, "_activate_variant_mode",
        lambda page: {"used": True, "variant_count": None})
    monkeypatch.setattr(C, "_next_page", lambda page: None)
    page = FakePage(
        payloads=[{"ok": True, "reviews": [_rev("u1"), _rev("u2")]}],
        ups=[], downs=[], cards=2)
    reviews, tp, page_log, vm = C._collect_reviews_pass(
        page, "https://x/?tab=reviews", "published_at_desc", {},
        apply_variant_mode=True, no_count_hint=True,
    )
    assert seen_calls["n"] == 1
    assert len(reviews) == 2


def test_collect_reviews_threads_no_count_hint(monkeypatch):
    """collect_reports передаёт no_count_hint вниз и брейкает после
    первого пустого прохода."""
    got = {}

    def fake_pass(page, url, sort, target_variant, **k):
        got.setdefault("hint", k.get("no_count_hint"))
        got["passes"] = got.get("passes", 0) + 1
        return [], 1, [], {"used": False, "variant_count": None}

    monkeypatch.setattr(C, "_collect_reviews_pass", fake_pass)
    reviews, tp, page_log, vm = C.collect_reviews(
        None, "https://x/?tab=reviews", {}, sorts=["published_at_desc",
                                                   "score_asc"],
        no_count_hint=True,
    )
    assert got["hint"] is True
    assert got["passes"] == 1
    assert reviews == []
