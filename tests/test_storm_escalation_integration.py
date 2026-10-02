"""Ревизия 2.9: интеграция soft/hard-эскалации на уровне родителя.

Связка проверяется целиком, как в бою:

    worker payload (recovery_metrics)
        → parallel._storm_summary()   # сводка шторма + block_kind
        → key_daemon._storm_block_kind()  # soft/hard с асимметрией
        → key_daemon._storm_escalates()   # all_stormed, escalate
        → _challenge_cooldown_seconds()   # пауза

Плюс регрессия: «чистый» воркер с block_kind (терминальный session-блок без
challenge_storm) НЕ должен переводить soft-шторм соседа в hard.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scripts.key_daemon as k  # noqa: E402
from ozon_collector import parallel as PL  # noqa: E402


class _W:
    def __init__(self, idx):
        self.worker_idx = idx
        self.pid = 1000 + idx


def _worker(storm=None, block_kind=None, reason=None, extra=None):
    rm = {"recycles": 0, "events": []}
    if storm:
        rm["challenge_storm"] = storm
    if block_kind:
        rm["block_kind"] = block_kind
    if reason:
        rm["block_kind_reason"] = reason
    if extra:
        rm.update(extra)
    return {"recovery_metrics": rm}


def _decide(payloads, parallel):
    """Прогнать боевую цепочку решений по набору payload'ов воркеров."""
    summary = PL._storm_summary([_W(i + 1) for i in range(len(payloads))],
                                payloads, parallel)
    aborted = summary or {}
    kind = k._storm_block_kind(aborted)
    all_stormed, escalate = k._storm_escalates(aborted, parallel)
    return {"summary": summary, "kind": kind, "all_stormed": all_stormed,
            "escalate": escalate}


def _react(progress: dict, aborted: dict, parallel: int) -> dict:
    """Прогнать ровно тот блок решений, что в run_campaign (CHALLENGE_STORM)."""
    _all_stormed, escalate = k._storm_escalates(aborted, parallel)
    pause_s = k.CHALLENGE_PARTIAL_PAUSE_S
    bumped = False
    if escalate:
        k._bump_challenge_control(progress, aborted.get("signature"))
        pause_s = k._challenge_cooldown_seconds(progress)
        bumped = True
    return {"escalate": escalate, "pause_s": pause_s, "bumped": bumped}


# ------------------------------------------------------------- hard ----------

def test_hard_storm_all_workers_escalates():
    r = _decide([
        _worker({"streak": 1, "pid": "1"}, "hard", "session_blocked"),
        _worker({"streak": 1, "pid": "2"}, "hard", "session_blocked"),
    ], 2)
    assert r["summary"]["all_workers"] is True
    assert r["kind"] == "hard"
    assert r["all_stormed"] is True
    assert r["escalate"] is True
    # hard-эскалация обязана дать adaptive-паузу (1м ±20%) и рост storm_streak
    prog = {"challenge_control": {"storm_streak": 0}}
    react = _react(prog, r["summary"], 2)
    assert react["bumped"] is True
    assert 48 <= react["pause_s"] <= 72, react["pause_s"]
    assert prog["challenge_control"]["storm_streak"] == 1
    assert prog["challenge_control"]["total_storms"] == 1


# ------------------------------------------------------------- soft ----------

def test_soft_storm_all_workers_does_not_escalate():
    r = _decide([
        _worker({"streak": 1, "pid": "1"}, "soft", "session_exhausted"),
        _worker({"streak": 1, "pid": "2"}, "soft", "session_exhausted"),
    ], 2)
    assert r["summary"]["block_kind"] == "soft"
    assert r["kind"] == "soft"
    assert r["all_stormed"] is True, "шторм был полным — но вид мягкий"
    assert r["escalate"] is False, "soft не должен жечь profile_cooldown"
    prog = {"challenge_control": {"storm_streak": 0}}
    react = _react(prog, r["summary"], 2)
    assert react["bumped"] is False
    assert react["pause_s"] == k.CHALLENGE_PARTIAL_PAUSE_S, "только частичная пауза"
    assert prog["challenge_control"]["storm_streak"] == 0
    assert "profile_cooldown" not in prog


def test_single_worker_soft_storm_does_not_escalate():
    r = _decide([_worker({"streak": 1, "pid": "1"}, "soft",
                         "session_exhausted")], 1)
    assert r["all_stormed"] is True, "parallel=1 = шторм всей кампании"
    assert r["escalate"] is False


# ------------------------------------------------- регрессия: тихий воркер ----

def test_clean_worker_block_kind_does_not_poison_soft_storm():
    """Воркер без challenge_storm, но с block_kind=hard не влияет на соседа.

    recovery.block_kind ставится любому прогону, упёршемуся в терминальный
    session-блок, даже без шторма. Раньше такой воркер попадал в `kinds` и
    переводил soft-шторм соседа в hard → лишний полный карантин.
    """
    r = _decide([
        _worker({"streak": 1, "pid": "1"}, "soft", "session_exhausted"),
        _worker(None, "hard", "session_blocked"),   # не штормил
    ], 2)
    assert r["summary"]["workers_stormed"] == [1]
    assert r["summary"]["all_workers"] is False
    assert r["summary"]["block_kind"] == "soft"
    assert r["summary"]["block_reasons"] == ["session_exhausted"]
    assert r["escalate"] is False


def test_storm_reason_ignores_non_storming_worker_reasons():
    r = _decide([
        _worker({"streak": 1, "pid": "1"}, "soft", "session_exhausted"),
        _worker(None, "hard", "origin_denied"),
    ], 2)
    assert r["summary"]["block_reasons"] == ["session_exhausted"]


# ------------------------------------------ нет шторма = нет эскалации -------

def test_no_storm_no_escalation_even_parallel_one():
    summary = PL._storm_summary([_W(1)], [_worker(None)], 1)
    assert summary is None, "нет шторма — нет сводки"
    # Пустой aborted в бою недостижим: блок эскалации живёт под
    # `if status == "CHALLENGE_STORM"`. На всякий случай — fail-closed
    # (асимметрия «сомнение = hard»), а не тихий отказ от карантина.
    assert k._storm_block_kind({}) == "hard", "нет данных = hard (асимметрия)"
    assert k._storm_escalates({}, 1) == (True, True)


# ------------------------------------------ одиночный путь (без parallel) ----

def test_single_run_path_terminal_block_marks_kind():
    """combined.aborted без challenge_storm (одиночный сбор) — вид блока виден."""
    for reason, expected in (("session_exhausted", "soft"),
                             ("session_blocked", "hard")):
        aborted = {"reason": reason, "count": 1}
        assert k._storm_block_kind(aborted) == expected
        assert k._storm_escalates(aborted, 1) == (True, expected == "hard")


def test_origin_deny_is_hard():
    """Отказ origin, не снятый лестницей, = жёсткий блок сессии."""
    aborted = {"reason": "origin_denied", "block_kind": "hard"}
    assert k._storm_block_kind(aborted) == "hard"
    assert k._storm_escalates(aborted, 1)[1] is True


def test_mixed_kinds_escalate_to_hard():
    r = _decide([
        _worker({"streak": 1, "pid": "1"}, "soft", "session_exhausted"),
        _worker({"streak": 1, "pid": "2"}, "hard", "access_challenge"),
    ], 2)
    assert r["kind"] == "hard", "смешанный шторм = hard"
    assert r["escalate"] is True


def test_storm_without_block_kind_is_hard():
    """Нет block_kind (старый payload/воркер упал раньше разметки) = hard."""
    r = _decide([_worker({"streak": 1, "pid": "1"})], 1)
    assert r["kind"] == "hard"
    assert r["escalate"] is True
