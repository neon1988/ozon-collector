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


def test_needs_restart_challenge_storm_restarts():
    """Пазл-шторм (recovery_metrics.challenge_storm): перезапускаем даже при
    exitcode 0 / без fatal_error — воркер съел шард из-за нерешённого пазла."""
    w = _mk_worker(done=True, exitcode=0,
                   payload={"fatal_error": None,
                            "recovery_metrics": {"challenge_storm": True}},
                   pending=[1, 3])
    assert PL._needs_restart(w, max_restarts=2) is True


def test_needs_restart_challenge_storm_limit_exhausted():
    """Пазл-шторм подчиняется max_restarts (защита от зомби-цикла при бане)."""
    w = _mk_worker(done=True, exitcode=0,
                   payload={"fatal_error": None,
                            "recovery_metrics": {"challenge_storm": True}},
                   pending=[1, 3], restarts=2)
    assert PL._needs_restart(w, max_restarts=2) is False


def test_needs_restart_challenge_storm_no_pending():
    """Пазл-шторм без незавершённой работы — перезапуск бессмыслен."""
    w = _mk_worker(done=True, exitcode=0,
                   payload={"fatal_error": None,
                            "recovery_metrics": {"challenge_storm": True}},
                   pending=[])
    assert PL._needs_restart(w, max_restarts=2) is False


def test_needs_restart_clean_no_challenge_not_restarted():
    """Штатное завершение без challenge_storm остаётся не-перезапускаемым."""
    w = _mk_worker(done=True, exitcode=0,
                   payload={"fatal_error": None}, pending=[1, 3])
    assert PL._needs_restart(w, max_restarts=2) is False


def test_needs_restart_browser_dead_restarts():
    """Мёртвый браузер воркера: перезапускаем при exitcode 0 / без fatal_error.

    Инцидент 2026-09-26: браузер воркера убили, воркер до конца прогона жёг
    recycle-бюджет (нечего пересоздавать) и сыпал SESSION_EXHAUSTED на каждый
    товар. Свежий браузер на респауне — единственное лечение.
    """
    w = _mk_worker(done=True, exitcode=0,
                   payload={"fatal_error": None,
                            "recovery_metrics": {"browser_dead": 5}},
                   pending=[1, 3])
    assert PL._needs_restart(w, max_restarts=2) is True


def test_needs_restart_browser_dead_limit_exhausted():
    """browser_dead тоже подчиняется max_restarts (защита от зомби-цикла)."""
    w = _mk_worker(done=True, exitcode=0,
                   payload={"fatal_error": None,
                            "recovery_metrics": {"browser_dead": 5}},
                   pending=[1, 3], restarts=2)
    assert PL._needs_restart(w, max_restarts=2) is False


def test_needs_restart_browser_dead_no_pending():
    """browser_dead без незавершённой работы — перезапуск бессмыслен."""
    w = _mk_worker(done=True, exitcode=0,
                   payload={"fatal_error": None,
                            "recovery_metrics": {"browser_dead": 5}},
                   pending=[])
    assert PL._needs_restart(w, max_restarts=2) is False


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
                     max_recycles, session_budget, on_event, worker_log=None,
                     dynamic=False, canary_url=None):
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


def test_await_restarts_puzzle_storm_worker(monkeypatch):
    """Воркер, завершившийся "чисто" (exitcode 0) но с challenge_storm
    (нерешённый пазл), перезапускается: новое окно продолжает шард."""
    ok = _mk_worker(worker_id=1, done=True, exitcode=0,
                    payload={"fatal_error": None, "recovery_metrics": {}},
                    pending=[])
    stormed = _mk_worker(worker_id=2, done=True, alive=False, exitcode=0,
                         payload={"fatal_error": None,
                                  "recovery_metrics": {"challenge_storm": True}},
                         pending=[1, 3])
    repl = _mk_worker(worker_id=2, done=True, exitcode=0,
                      payload={"fatal_error": None}, pending=[])
    calls = []
    def fake_respawn(wctx, pending, items_by_index, gather_cfg,
                     max_recycles, session_budget, on_event, worker_log=None,
                     dynamic=False, canary_url=None):
        calls.append((wctx.worker_idx, sorted(pending)))
        return repl
    monkeypatch.setattr(PL, "_respawn", fake_respawn)
    deadline = time.monotonic() + 30
    out = PL._await_workers([ok, stormed], deadline=deadline,
                            max_worker_restarts=2,
                            items_by_index={1: ("1001", "u")},
                            gather_cfg={},
                            max_recycles=2, session_budget=None,
                            poll=0.01)
    assert calls == [(2, [1, 3])]      # пазл-шторм воркер перезапущен с шардом
    assert len(out) == 2


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
                     max_recycles, session_budget, on_event, worker_log=None,
                     dynamic=False, canary_url=None):
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
                     max_recycles, session_budget, on_event, worker_log=None,
                     dynamic=False, canary_url=None):
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


# ---- Кооперативный стоп (Ctrl+C → stop_event) --------------------------------

def test_await_stop_event_terminates_no_restart_limit0(monkeypatch):
    """Ветка limit=0: stop_event сворачивает живых воркеров БЕЗ респавна."""
    import threading as _t
    w = _mk_worker(worker_id=1, alive=True, done=False, pending=[0, 1, 2])
    w.last_activity = time.monotonic()  # свежий heartbeat — не stall
    called = []
    monkeypatch.setattr(PL, "_respawn", lambda *a, **k: called.append(1))
    stop = _t.Event()
    stop.set()  # стоп уже активен до входа
    out = PL._await_workers([w], deadline=time.monotonic() + 30,
                            max_worker_restarts=0, items_by_index={},
                            gather_cfg={}, max_recycles=2,
                            session_budget=None, poll=0.01, stop_event=stop)
    assert called == []                 # респавна нет
    assert not w.proc.is_alive()        # воркер terminate'нут
    assert len(out) == 1 and out[0] is w


def test_await_stop_event_terminates_no_restart_restart_branch(monkeypatch):
    """Ветка restart (limit>0): stop_event сворачивает живых воркеров без respawn."""
    import threading as _t
    w = _mk_worker(worker_id=1, alive=True, done=False, pending=[0, 1, 2])
    w.last_activity = time.monotonic()  # свежий heartbeat — не stall
    called = []
    monkeypatch.setattr(PL, "_respawn", lambda *a, **k: called.append(1))
    stop = _t.Event()
    stop.set()
    out = PL._await_workers([w], deadline=time.monotonic() + 30,
                            max_worker_restarts=2, items_by_index={},
                            gather_cfg={}, max_recycles=2,
                            session_budget=None, poll=0.01, stop_event=stop)
    assert called == []                 # НЕ респавним остановленного воркера
    assert not w.proc.is_alive()        # terminate'нут
    assert len(out) == 1 and out[0] is w


def test_await_stop_event_late_break():
    """stop_event, поднятый ПОСЛЕ старта цикла, прерывает ожидание досрочно."""
    import threading as _t
    w = _mk_worker(worker_id=1, alive=True, done=False, pending=[0, 1])
    w.last_activity = time.monotonic()
    stop = _t.Event()

    def _set_stop_later():
        time.sleep(0.3)
        stop.set()

    _t.Thread(target=_set_stop_later, daemon=True).start()
    # большой дедлайн: цикл должен завершиться по stop (~0.3с), а не по дедлайну
    deadline = time.monotonic() + 30
    t0 = time.monotonic()
    out = PL._await_workers([w], deadline=deadline,
                            max_worker_restarts=2, items_by_index={},
                            gather_cfg={}, max_recycles=2,
                            session_budget=None, poll=0.01, stop_event=stop)
    assert time.monotonic() - t0 < 10    # вышел по stop, не по дедлайну (30с)
    assert not w.proc.is_alive()         # воркер stop'нут (terminate/join)
    assert len(out) == 1 and out[0] is w


# ---- Принудительный стоп (2-й Ctrl+C → force_event) --------------------------

def test_await_force_event_terminates_no_restart_limit0(monkeypatch):
    """Ветка limit=0: force_event принудительно сворачивает воркеров без respawn."""
    import threading as _t
    w = _mk_worker(worker_id=1, alive=True, done=False, pending=[0, 1, 2])
    w.last_activity = time.monotonic()
    called = []
    monkeypatch.setattr(PL, "_respawn", lambda *a, **k: called.append(1))
    force = _t.Event()
    force.set()
    out = PL._await_workers([w], deadline=time.monotonic() + 30,
                            max_worker_restarts=0, items_by_index={},
                            gather_cfg={}, max_recycles=2,
                            session_budget=None, poll=0.01,
                            force_event=force)
    assert called == []                 # респавна нет
    assert not w.proc.is_alive()        # terminate'нут (зачистка браузера в родителе)
    assert len(out) == 1 and out[0] is w


def test_await_force_event_terminates_no_restart_restart_branch(monkeypatch):
    """Ветка restart: force_event принудительно завершает всех воркеров, без
    выдачи новых задач и без респавна."""
    import threading as _t
    w1 = _mk_worker(worker_id=1, alive=True, done=False, pending=[0, 1])
    w2 = _mk_worker(worker_id=2, alive=True, done=False, pending=[0, 1])
    for w in (w1, w2):
        w.last_activity = time.monotonic()
    called = []
    monkeypatch.setattr(PL, "_respawn", lambda *a, **k: called.append(1))
    force = _t.Event()
    force.set()
    out = PL._await_workers([w1, w2], deadline=time.monotonic() + 30,
                            max_worker_restarts=2, items_by_index={},
                            gather_cfg={}, max_recycles=2,
                            session_budget=None, poll=0.01,
                            force_event=force)
    assert called == []                 # НЕ респавним
    assert not w1.proc.is_alive() and not w2.proc.is_alive()  # оба terminate'нуты
    assert len(out) == 2                # оба в результате (зачищены)


def test_await_force_priority_over_stop(monkeypatch):
    """Если установлены И stop_event, И force_event — побеждает force."""
    import threading as _t
    w = _mk_worker(worker_id=1, alive=True, done=False, pending=[0, 1])
    w.last_activity = time.monotonic()
    called = []
    monkeypatch.setattr(PL, "_respawn", lambda *a, **k: called.append(1))
    stop = _t.Event()
    force = _t.Event()
    stop.set()
    force.set()
    out = PL._await_workers([w], deadline=time.monotonic() + 30,
                            max_worker_restarts=2, items_by_index={},
                            gather_cfg={}, max_recycles=2,
                            session_budget=None, poll=0.01,
                            stop_event=stop, force_event=force)
    assert called == []
    assert not w.proc.is_alive()
    assert len(out) == 1 and out[0] is w


def test_await_force_stop_covers_restarted_replacement(monkeypatch):
    """Force stop must also terminate a respawned replacement.

    Audit regression 2026-10-19: _force_stop iterated the original wctxs list
    only, so a live replacement kept running until the daemon's os._exit.
    """
    import threading as _t
    dead = _mk_worker(worker_id=1, done=True, exitcode=1,
                      payload={"fatal_error": "boom"}, pending=[1, 3])
    repl = _mk_worker(worker_id=1, done=False, alive=True, pending=[1, 3])
    repl.last_activity = time.monotonic()
    force = _t.Event()

    def fake_respawn(*a, **k):
        _t.Timer(0.2, force.set).start()
        return repl

    monkeypatch.setattr(PL, "_respawn", fake_respawn)
    out = PL._await_workers([dead], deadline=time.monotonic() + 30,
                            max_worker_restarts=2,
                            items_by_index={1: ("1001", "u")},
                            gather_cfg={}, max_recycles=2,
                            session_budget=None, poll=0.01,
                            force_event=force)
    assert not repl.proc.is_alive(), \
        "замена пережила форс-стоп — сила не покрыла респавн"
    assert out == [repl], \
        "финализация: только замена (дедуп по worker_idx), без дублей"


# ---- _collect_worker_payloads (регрессия dc9edcd, аудит 2026-10-15) ----------

def test_collect_payloads_no_keyerror_on_terminate_done_batch():
    """Terminate-путь: payload done_batch без ключа fatal_error не роняет merge.

    Регрессия dc9edcd: `pl["fatal_error"]` давал KeyError при exitcode≠0 (в том
    числе deadline_halted — `and` вычислял subscript раньше флага)."""
    w = _mk_worker(done=False, exitcode=1,
                   payload={"products": [], "processed_indexes": [0],
                            "failed_indexes": []})
    w.deadline_halted = True
    pl = PL._collect_worker_payloads([w])[0]
    assert pl["exitcode"] == 1
    assert pl.get("fatal_error") is None    # дедлайн — не fatal


def test_collect_payloads_marks_fatal_without_key():
    """Тот же payload без deadline_halted → fatal проставляется, не KeyError."""
    w = _mk_worker(done=False, exitcode=1,
                   payload={"products": [], "processed_indexes": [],
                            "failed_indexes": []})
    pl = PL._collect_worker_payloads([w])[0]
    assert pl["fatal_error"] == "exit 1"


def test_collect_payloads_exitcode_none_not_fatal():
    """exitcode None (процесс не сообщил код) — не fatal."""
    w = _mk_worker(done=False, exitcode=None, payload={"products": []})
    pl = PL._collect_worker_payloads([w])[0]
    assert pl["exitcode"] is None
    assert pl.get("fatal_error") is None
