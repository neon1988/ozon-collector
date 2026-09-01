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
                            max_worker_restarts=0,
                            items_by_index={}, gather_cfg={},
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
                            max_worker_restarts=2,
                            items_by_index={1: ("1001", "u")},
                            gather_cfg={},
                            max_recycles=2, session_budget=None,
                            poll=0.01)
    assert calls == [(2, [1, 3])]          # перезапущен именно упавший
    assert len(out) == 2                   # ok + replacement (упавший заменён)
    assert any(w.pending_indexes == [] and w.exitcode == 0 for w in out)


def test_await_restart_carries_collected_products(monkeypatch):
    """При перезапуске упавшего воркера его уже собранные товары переносятся
    в воркера-замену (carried_payload), чтобы не потеряться в merge."""
    ok = _mk_worker(worker_id=1, done=True, exitcode=0,
                    payload={"fatal_error": None}, pending=[])
    # Упавший воркер успел собрать 2 товара (индексы 0,2) из шарда [0,1,2,3].
    dead = _mk_worker(worker_id=2, done=False, alive=False, exitcode=1,
                      payload={
                          "fatal_error": None,
                          "products": [
                              {"index": 0, "result": {"ok": True}},
                              {"index": 2, "result": {"ok": True}},
                          ],
                          "processed_indexes": [0, 2],
                          "failed_indexes": [],
                      },
                      pending=[1, 3])
    # dead.pending рассчитывается из payload.processed_indexes: shard [0..3] -
    # processed {0,2} = [1,3]; зададим явно для надёжности.
    dead.shard_indexes = [0, 1, 2, 3]
    dead.pending_indexes = [1, 3]

    repl = _mk_worker(worker_id=2, done=True, exitcode=0,
                      payload={"fatal_error": None,
                               "products": [
                                   {"index": 1, "result": {"ok": True}},
                                   {"index": 3, "result": {"ok": True}},
                               ],
                               "processed_indexes": [1, 3],
                               "failed_indexes": []},
                      pending=[])

    def fake_respawn(wctx, pending, items_by_index, gather_cfg,
                     max_recycles, session_budget, on_event, worker_log=None):
        return repl
    monkeypatch.setattr(PL, "_respawn", fake_respawn)
    deadline = time.monotonic() + 30
    out = PL._await_workers([ok, dead], deadline=deadline,
                            max_worker_restarts=2,
                            items_by_index={}, gather_cfg={},
                            max_recycles=2, session_budget=None, poll=0.01)
    repl = [w for w in out if w.worker_idx == 2][0]
    assert repl.carried_payload is not None
    carried_idx = {p["index"] for p in repl.carried_payload["products"]}
    assert carried_idx == {0, 2}      # товары упавшего воркера не потеряны
    assert sorted(repl.carried_payload["processed_indexes"]) == [0, 2]


def test_merge_carried_combines_products():
    """_merge_carried склеивает товары упавшего воркера с товарами замены."""
    w = _mk_worker(worker_id=2, done=True, exitcode=0, payload={}, pending=[])
    w.carried_payload = {
        "products": [
            {"index": 0, "result": {"ok": True}},
            {"index": 2, "result": {"ok": True}},
        ],
        "processed_indexes": [0, 2],
        "failed_indexes": [],
    }
    pl = {
        "products": [
            {"index": 1, "result": {"ok": True}},
            {"index": 3, "result": {"ok": True}},
        ],
        "processed_indexes": [1, 3],
        "failed_indexes": [],
    }
    merged = PL._merge_carried(pl, w)
    idxs = [p["index"] for p in merged["products"]]
    assert idxs == [0, 2, 1, 3]           # carried идут первыми, всё сохранено
    assert sorted(merged["processed_indexes"]) == [0, 1, 2, 3]


def test_merge_carried_no_carry_is_identity():
    """Без carried_payload _merge_carried возвращает pl без изменений."""
    w = _mk_worker(worker_id=1, done=True, exitcode=0, payload={}, pending=[])
    w.carried_payload = None
    pl = {"products": [{"index": 0, "result": {"ok": True}}],
          "processed_indexes": [0], "failed_indexes": []}
    assert PL._merge_carried(pl, w) is pl


# ---- stall-terminate (heartbeat от воркера) ---------------------------------

class _FakeProcAlive:
    """proc-дубль: is_alive/join/terminate/exitcode для «зависшего» воркера.

    join() имитирует дожидание до конца (завершает процесс, как реальный
    multiprocessing join после выхода воркера). Для НЕ завершившегося по done
    воркера _await_workers сначала terminate'ит, поэтому здесь достаточно
    простого join-завершения."""

    def __init__(self):
        self._alive = True
        self.exitcode = None

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self._alive = False
        if self.exitcode is None:
            self.exitcode = 0   # штатное завершение (иначе сохраняем terminate=1)

    def terminate(self):
        self._alive = False
        self.exitcode = 1


def _mk_stalled_worker(worker_id=1, stale_s=9999, done=False):
    spec = {"worker_id": worker_id, "port": 9222 + worker_id,
            "cdp_url": f"http://127.0.0.1:{9222 + worker_id}",
            "profile_token": f"ozon-profile-worker-{worker_id}",
            "profile_path": "x", "log_path": "x"}
    w = WorkerContext(spec, _FakeProcAlive(), None)
    w.shard_indexes = [0, 1, 2, 3]
    w.pending_indexes = [0, 1, 2, 3]
    w.done = done
    w.last_activity = time.monotonic() - stale_s
    return w


def test_await_stall_terminates_worker_no_restart(monkeypatch):
    """Воркер без heartbeat (stale last_activity) → terminate, без рестарта
    при max_worker_restarts=0 (базовый режим)."""
    w = _mk_stalled_worker(worker_id=1, stale_s=PL.WORKER_STALL_S + 60)
    cleaned = []
    monkeypatch.setattr(PL, "_cleanup_worker",
                        lambda wctx: cleaned.append(wctx.worker_idx))
    deadline = time.monotonic() + 5
    out = PL._await_workers([w], deadline=deadline,
                            max_worker_restarts=0, items_by_index={},
                            gather_cfg={}, max_recycles=2,
                            session_budget=None, poll=0.05)
    assert cleaned == [1]            # stall → cleanup выполнен
    assert not w.deadline_halted     # stall — не «дедлайн», флаг не ставим
    assert not w.proc.is_alive()     # terminate() вызван


def test_await_stall_terminates_and_respawns(monkeypatch):
    """Воркер без heartbeat → terminate + перезапуск (max_worker_restarts>0)."""
    w = _mk_stalled_worker(worker_id=1, stale_s=PL.WORKER_STALL_S + 60)
    repl = _mk_worker(worker_id=1, done=True, exitcode=0,
                      payload={"fatal_error": None}, pending=[])
    calls = []

    def fake_respawn(wctx, pending, items_by_index, gather_cfg,
                     max_recycles, session_budget, on_event, worker_log=None):
        calls.append((wctx.worker_idx, sorted(pending)))
        return repl
    monkeypatch.setattr(PL, "_respawn", fake_respawn)
    deadline = time.monotonic() + 5
    out = PL._await_workers([w], deadline=deadline,
                            max_worker_restarts=2, items_by_index={},
                            gather_cfg={}, max_recycles=2,
                            session_budget=None, poll=0.05)
    assert calls == [(1, [0, 1, 2, 3])]    # stall-воркер перезапущен с шардом
    assert any(x.worker_idx == 1 and x.done for x in out)


def test_await_no_stall_when_heartbeat_recent():
    """Активный воркер (свежий last_activity) НЕ terminate'ится по stall."""
    w = _mk_stalled_worker(worker_id=1, stale_s=1)  # 1s простоя < WORKER_STALL_S
    import threading

    stop = threading.Event()
    def beat():
        # heartbeat: обновляем активность; через ~0.4с воркер завершается (done)
        for _ in range(100):
            if stop.is_set():
                break
            w.last_activity = time.monotonic()
            time.sleep(0.05)
        w.done = True
    t = threading.Thread(target=beat, daemon=True)
    t.start()
    deadline = time.monotonic() + 10   # большой дедлайн: завершение по done, не по времени
    out = PL._await_workers([w], deadline=deadline,
                            max_worker_restarts=0, items_by_index={},
                            gather_cfg={}, max_recycles=2,
                            session_budget=None, poll=0.05)
    stop.set()
    t.join(1.0)
    assert not w.deadline_halted      # воркер НЕ считался зависшим (ни stall, ни дедлайн)
    assert not w.proc.is_alive()      # завершён штатно (done → join) — terminate не вызывался
