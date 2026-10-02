"""Ревизия 2.9: недетерминированный активный прогрев (L3) + soft/hard-эскалация.

Прогрев:
- паузы с джиттером (фиксированные 8000/8000 давали одинаковый отпечаток);
- активный прогрев (скролл + наведение) и его выключатель OZON_ACTIVE_WARMUP=0;
- счётчик warmup-challenges в recovery (прогрев не тратит session_used, но
  воркер не должен час гонять прогрев по кругу молча).

Эскалация: SESSION_EXHAUSTED (soft) → короткая пауза без profile_cooldown и
без роста storm_streak; SESSION_BLOCKED (hard) → полный карантин.
"""
from __future__ import annotations

import os
import random
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scripts.key_daemon as k  # noqa: E402
from ozon_collector import browser as B  # noqa: E402
import ozon_collector.search as S  # noqa: E402


class _Mouse:
    def __init__(self):
        self.moves = []

    def move(self, x, y):
        self.moves.append((x, y))


class _Page:
    def __init__(self):
        self.mouse = _Mouse()
        self.scrolls = []

    def evaluate(self, script, arg=None):
        if "scrollBy" in script:
            self.scrolls.append(arg)
            return None
        if "innerWidth" in script:
            return 1200
        if "innerHeight" in script:
            return 800
        return None

    def goto(self, url, wait_ms=None, **kw):
        pass


class _Sess:
    def __init__(self):
        self.page = _Page()
        self.gotos = []
        self.new_pages = 0

    def goto(self, url, wait_ms=None, **kw):
        self.gotos.append((url, wait_ms))

    def new_page(self):
        self.new_pages += 1


def _no_challenge(page, where, reload_on_fab=True):
    return None


# ---------------------------------------------------------------- прогрев ----

def test_warmup_waits_are_jittered(monkeypatch):
    monkeypatch.setattr(S.C, "check_challenge", _no_challenge)
    monkeypatch.setenv("OZON_ACTIVE_WARMUP", "0")
    waits = set()
    for i in range(30):
        sess = _Sess()
        S._warmup_to_category(sess, "https://www.ozon.ru/category/c")
        waits.update(w for _u, w in sess.gotos)
    assert len(waits) > 5, "фиксированная пауза = одинаковый отпечаток"
    assert all(6000 <= w <= 10800 for w in waits)


def test_warmup_active_moves_mouse_and_scrolls(monkeypatch):
    monkeypatch.setattr(S.C, "check_challenge", _no_challenge)
    monkeypatch.delenv("OZON_ACTIVE_WARMUP", raising=False)
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    sess = _Sess()
    info = S._warmup_to_category(sess, "https://www.ozon.ru/category/c")
    assert "scroll" in info["activity"]
    assert "mouse" in info["activity"]
    assert sess.page.scrolls
    assert 2 <= len(sess.page.mouse.moves) <= 3


def test_warmup_activity_can_be_disabled(monkeypatch):
    monkeypatch.setattr(S.C, "check_challenge", _no_challenge)
    monkeypatch.setenv("OZON_ACTIVE_WARMUP", "0")
    sess = _Sess()
    info = S._warmup_to_category(sess, "https://www.ozon.ru/category/c")
    assert info["activity"] == "off"
    assert sess.page.mouse.moves == []


def test_warmup_reports_challenges_into_recovery(monkeypatch):
    def _challenge(page, where, reload_on_fab=True):
        raise B.AccessChallengeError(f"на，让我们 {where}): title:captcha")

    monkeypatch.setattr(S.C, "check_challenge", _challenge)
    monkeypatch.setenv("OZON_ACTIVE_WARMUP", "0")
    recovery = {}
    sess = _Sess()
    info = S._warmup_to_category(sess, "https://www.ozon.ru/category/c",
                                 recovery=recovery)
    assert info["challenges"] == 1
    assert sess.new_pages == 1
    assert recovery["warmup_challenges"] == 1
    assert recovery["warmup_entry"] == "home_category"
    assert len(recovery["warmup_waits_ms"]) == 2
    # прогрев НЕ тратит session_used (это не лестница recovery)
    assert "session_used" not in recovery


def test_warmup_challenges_accumulate(monkeypatch):
    def _challenge(page, where, reload_on_fab=True):
        raise B.AccessChallengeError(f"на {where}): title:captcha")

    monkeypatch.setattr(S.C, "check_challenge", _challenge)
    monkeypatch.setenv("OZON_ACTIVE_WARMUP", "0")
    recovery = {}
    for _ in range(3):
        S._warmup_to_category(_Sess(), "https://www.ozon.ru/category/c",
                              recovery=recovery)
    assert recovery["warmup_challenges"] == 3


def test_warmup_survives_broken_page(monkeypatch):
    """Прогрев не роняет сбор: битая мышь/страница = warn, не исключение."""
    class _BadPage(_Page):
        def evaluate(self, script, arg=None):
            raise RuntimeError("detached")

    monkeypatch.setattr(S.C, "check_challenge", _no_challenge)
    monkeypatch.delenv("OZON_ACTIVE_WARMUP", raising=False)
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    sess = _Sess()
    sess.page = _BadPage()
    info = S._warmup_to_category(sess, "https://www.ozon.ru/category/c")
    assert info["activity"] in ("none", "no_page", "")
    assert len(sess.gotos) == 2


def test_warmup_no_page(monkeypatch):
    monkeypatch.setattr(S.C, "check_challenge", _no_challenge)
    sess = _Sess()
    sess.page = None
    info = S._warmup_to_category(sess, "https://www.ozon.ru/category/c")
    assert info["activity"] == "no_page"
    assert len(sess.gotos) == 2


# ------------------------------------------------------- soft vs hard блок ----

def test_abort_kind_of():
    assert S.abort_kind_of("session_exhausted") == "session_exhausted"
    for kind in ("session_blocked", "origin_denied", "access_challenge", None):
        assert S.abort_kind_of(kind) == "session_blocked"


def test_storm_block_kind_soft():
    assert k._storm_block_kind({"reason": "session_exhausted"}) == "soft"
    assert k._storm_block_kind({"reason": "session_blocked"}) == "hard"
    assert k._storm_block_kind({"block_kind": "soft",
                                "reason": "challenge_storm"}) == "soft"
    assert k._storm_block_kind({"block_kind": "hard"}) == "hard"
    # асимметрия: неизвестное = hard (жалко ошибиться в сторону «мягко»)
    assert k._storm_block_kind({}) == "hard"
    assert k._storm_block_kind({"reason": "challenge_storm"}) == "hard"
    assert k._storm_block_kind({"puzzle_persistent": True}) == "hard"


def test_storm_summary_carries_block_kind():
    from ozon_collector import parallel


    class _W:
        def __init__(self, idx):
            self.worker_idx = idx

    def _pl(kind, reason):
        return {"recovery_metrics": {"challenge_storm": {"streak": 1},
                                     "block_kind": kind,
                                     "block_kind_reason": reason}}

    storms = parallel._storm_summary([_W(1), _W(2)],
                                     [_pl("soft", "session_exhausted"),
                                      _pl("soft", "session_exhausted")], 2)
    assert storms["block_kind"] == "soft"
    assert storms["all_workers"] is True
    assert storms["block_reasons"] == ["session_exhausted"]

    mixed = parallel._storm_summary([_W(1), _W(2)],
                                    [_pl("soft", "session_exhausted"),
                                     _pl("hard", "access_challenge")], 2)
    assert mixed["block_kind"] == "hard", "смешанный шторм = hard"
    assert mixed["block_reasons"] == ["access_challenge",
                                       "session_exhausted"]


def test_storm_summary_none_without_storm():
    from ozon_collector import parallel

    class _W:
        def __init__(self, idx):
            self.worker_idx = idx

    assert parallel._storm_summary([_W(1)], [{"recovery_metrics": {}}], 1) is None
