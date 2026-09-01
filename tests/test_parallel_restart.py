# Unit-тесты оркестрации воркеров: перезапуск упавших с переносом шарда.
# Не требуют живого Ozon/Chromium — используют фейковые WorkerContext/proc.
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ozon_collector import parallel as PL
from ozon_collector.parallel import WorkerContext


class _FakeProc:
    """proc-дубль: is_alive/join/terminate/exitcode."""

    def __init__(self, alive: bool = True, exitcode: int | None = 0):
        self._alive = alive
        self.exitcode = exitcode

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self._alive = False

    def terminate(self):
        self._alive = False
        self.exitcode = 1


def _mk_worker(worker_id=1, spec=None, done=False, alive=True,
               exitcode=0, payload=None, pending=None, restarts=0,
               deadline_halted=False):
    spec = spec or {"worker_id": worker_id, "port": 9222 + worker_id,
                    "cdp_url": f"http://127.0.0.1:{9222 + worker_id}",
                    "profile_token": f"ozon-profile-worker-{worker_id}",
                    "profile_path": "x", "log_path": "x"}
    w = WorkerContext(spec, _FakeProc(alive=alive, exitcode=exitcode), None)
    w.shard_indexes = list(range(0, 4)) if not pending else pending
    w.pending_indexes = list(pending) if pending else []
    w.restarts = restarts
    w.deadline_halted = deadline_halted
    w.done = done
    w.payload = payload
    return w


# ---- _needs_restart ----------------------------------------------------------

def test_needs_restart_crash_with_pending():
    w = _mk_worker(done=True, exitcode=1,
                   payload={"fatal_error": "boom"}, pending=[0, 2])
    assert PL._needs_restart(w, max_restarts=2) is True


def test_needs_restart_ok_worker_not_restarted():
    w = _mk_worker(done=True, exitcode=0, payload={"fatal_error": None},
                   pending=[])
    assert PL._needs_restart(w, max_restarts=2) is False


def test_needs_restart_limit_exhausted():
    w = _mk_worker(done=True, exitcode=1, payload={"fatal_error": "x"},
                   pending=[0, 2], restarts=2)
    assert PL._needs_restart(w, max_restarts=2) is False


def test_needs_restart_deadline_halted_not_restarted():
    w = _mk_worker(done=True, exitcode=1, payload={"fatal_error": "x"},
                   pending=[0, 2], deadline_halted=True)
    assert PL._needs_restart(w, max_restarts=2) is False


def test_needs_restart_zero_limit():
    w = _mk_worker(done=True, exitcode=1, payload={"fatal_error": "x"},
                   pending=[0, 2])
    assert PL._needs_restart(w, max_restarts=0) is False


# ---- _await_workers ---------------------------------------------------------

def test_await_no_restart_when_limit_zero(monkeypatch):
    ok = _mk_worker(worker_id=1, done=True, exitcode=0,
                    payload={"fatal_error": None}, pending=[])
    dead = _mk_worker(worker_id=2, done=True, exitcode=1,
                      payload={"fatal_error": "boom"}, pending=[1, 3])
    called = []
    monkeypatch.setattr(PL, "_respawn", lambda *a, **k: called.append(1))
    deadline = time.monotonic() + 30
    out = PL._await_workers([ok, dead], deadline=deadline,
                            join_timeout_s=30, max_worker_restarts=0,
                            items_by_index={}, specs=[], gather_cfg={},
                            max_recycles=2, session_budget=None)
    assert called == []          # при limit=0 перезапуска нет
    assert len(out) == 2


def test_await_restarts_dead_worker(monkeypatch):
    ok = _mk_worker(worker_id=1, done=True, exitcode=0,
                    payload={"fatal_error": None}, pending=[])
    dead = _mk_worker(worker_id=2, done=True, exitcode=1,
                      payload={"fatal_error": "boom"}, pending=[1, 3])
    repl = _mk_worker(worker_id=2, done=True, exitcode=0,
                      payload={"fatal_error": None}, pending=[])
    calls = []
    def fake_respawn(wctx, pending, items_by_index, gather_cfg,
                     max_recycles, session_budget, on_event, worker_log=None):
        calls.append((wctx.worker_idx, sorted(pending)))
        return repl
    monkeypatch.setattr(PL, "_respawn", fake_respawn)
    deadline = time.monotonic() + 30
    out = PL._await_workers([ok, dead], deadline=deadline,
                            join_timeout_s=30, max_worker_restarts=2,
                            items_by_index={1: ("1001", "u")},
                            specs=[], gather_cfg={},
                            max_recycles=2, session_budget=None,
                            poll=0.01)
    assert calls == [(2, [1, 3])]          # перезапущен именно упавший
    assert len(out) == 2                   # ok + replacement (упавший заменён)
    assert any(w.pending_indexes == [] and w.exitcode == 0 for w in out)
