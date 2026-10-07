# Режим выдачи по одному товару (batch_size=1) — честная реализация.
#
# Проверяется поведение, а не «не упало»:
#   * воркер получает РОВНО один товар за раз и запрашивает следующий;
#   * prefer_miss не инвертируется: с браузером — cache-miss, без — cache-hit,
#     fallback срабатывает только когда товаров приоритетного типа не осталось;
#   * ленивый старт браузера: cache-hit его не поднимает, cache-miss поднимает;
#   * per-item payload не теряется (аудит 2026-10-06, находка 4);
#   * товар без результата возвращается в очередь РОВНО ОДИН раз: completed_indexes
#     не даёт повторной выдачи, обрыв p2w-канала не удваивает товар в очереди.
#
# Настоящий Chromium/CDP не поднимается: воркеры — заглушки, а
# PL._cleanup_worker подменён (он умеет гасить браузер по порту+профилю, а
# порты 9222-9230 на этой машине заняты демоном пользователя).
from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ozon_collector import parallel as PL  # noqa: E402


# ---- заглушки -----------------------------------------------------------------

def _items(n, start=0):
    return [(start + i, str(1000 + start + i), f"https://p/{start + i}")
            for i in range(n)]


def _cache_entry_ok(pid):
    return {"pid": pid, "product": {"title": f"Товар {pid}"}, "reviews": []}


class _FakeCache:
    """Матрица cache_check: pid → (raw, reason)."""

    def __init__(self, hits: set, stale: set = None):
        self.hits = hits
        self.stale = stale or set()
        self.calls = []

    def __call__(self, pid, max_age_s, data_dir="data", sorts=None):
        self.calls.append(pid)
        if pid in self.stale:
            return None, "stale"
        if pid in self.hits:
            return _cache_entry_ok(pid), "hit"
        return None, "miss"


def _patch_cache(monkeypatch, hits, stale=None):
    monkeypatch.setattr(PL.K, "cache_check",
                        _FakeCache(hits=set(hits), stale=set(stale or ())))
    monkeypatch.setattr(PL.C, "variant_review_problems", lambda raw: [])


def _take(monkeypatch, queue, hits, stale, prefer_miss, size=1, worker=1):
    _patch_cache(monkeypatch, hits, stale)
    owner = {}
    batch = PL._take_batch(queue, size, prefer_miss, {"max_age_s": 3600},
                           worker, owner, {})
    return batch, owner, list(queue)


class _FakePipe:
    """p2w-канал-заглушка: пишет отправленные выдачи, умеет «оборваться»."""

    def __init__(self, broken: bool = False):
        self.sent = []
        self.broken = broken

    def send(self, msg):
        if self.broken:
            raise BrokenPipeError("pipe closed")
        self.sent.append(msg)

    def batches(self):
        return [m["batch"] for m in self.sent if m.get("t") == "next"]

    def close(self):
        pass


class _FakeProc:
    def __init__(self, alive=True, exitcode=0):
        self._alive = alive
        self.exitcode = exitcode

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        pass

    def terminate(self):
        self._alive = False
        self.exitcode = 1


def _mk_worker(worker_id=1, pipe=None, alive=True, exitcode=0, done=False,
               payload=None):
    spec = {"worker_id": worker_id, "port": 9990 + worker_id,
            "cdp_url": f"http://127.0.0.1:{9990 + worker_id}",
            "profile_token": f"test-worker-{worker_id}",
            "profile_path": "x", "log_path": "x"}
    w = PL.WorkerContext(spec, _FakeProc(alive=alive, exitcode=exitcode),
                         None, pipe or _FakePipe())
    w.waiting_for_batch = True
    w.last_activity = time.monotonic()
    w.done = done
    w.payload = payload
    return w


def _wait_until(pred, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ---- значение по умолчанию и его защита ---------------------------------------

def test_default_batch_size_is_one():
    """Режим владельца: один товар за раз (грузчик)."""
    assert PL.DEFAULT_BATCH_SIZE == 1


def test_resolve_batch_size_reads_cfg_and_clamps():
    """Явное значение уважается; вырожденное — не даёт тихого «собрано 0»."""
    assert PL._resolve_batch_size({}) == 1
    assert PL._resolve_batch_size({"batch_size": 5}) == 5
    assert PL._resolve_batch_size({"batch_size": 0}) == 1
    assert PL._resolve_batch_size({"batch_size": -3}) == 1
    assert PL._resolve_batch_size({"batch_size": None}) == 1
    assert PL._resolve_batch_size({"batch_size": "abc"}) == 1


# ---- _take_batch на одном товаре ---------------------------------------------

def test_take_batch_single_item_only(monkeypatch):
    """size=1 → ровно один товар, owner помечен, порядок очереди сохранён."""
    q = deque(_items(4))
    batch, owner, rest = _take(monkeypatch, q, {"1000", "1001", "1002", "1003"},
                               set(), prefer_miss=False)
    assert len(batch) == 1, "одна товарная единица за раздачу"
    assert batch[0][0] == 0
    assert owner == {0: 1}
    assert [i for i, _p, _u in rest] == [1, 2, 3]


def test_take_batch_prefer_miss_not_inverted(monkeypatch):
    """С браузером — miss, без браузера — hit. Не наоборот, даже при size=1."""
    q = deque(_items(4))
    miss_first, _owner, rest = _take(monkeypatch, q, {"1001", "1002", "1003"},
                                     {"1000"}, prefer_miss=True)
    assert [p for _i, p, _u in miss_first] == ["1000"], (
        "воркер с браузером берёт cache-miss, а не cache-hit")

    batch2, _owner2, rest2 = _take(monkeypatch, deque(rest),
                                   {"1001", "1002", "1003"}, {"1000"},
                                   prefer_miss=False)
    assert [p for _i, p, _u in batch2] == ["1001"], (
        "воркер без браузера берёт cache-hit и не поднимает Chromium")
    assert [p for _i, p, _u in rest2] == ["1002", "1003"]


def test_take_batch_fallback_only_when_priority_exhausted(monkeypatch):
    """Пока есть miss — воркер с браузером не получает hit (ленивость цела)."""
    q = deque(_items(3))
    batch, _owner, rest = _take(monkeypatch, q, {"1001", "1002"}, {"1000"},
                                prefer_miss=True)
    assert [p for _i, p, _u in batch] == ["1000"], "только miss, hit'ы ждут"
    assert [p for _i, p, _u in rest] == ["1001", "1002"]

    q2 = deque([(i, p, u) for i, p, u in rest])
    batch2, _o2, rest2 = _take(monkeypatch, q2, {"1001", "1002"}, set(),
                               prefer_miss=True)
    assert [p for _i, p, _u in batch2] == ["1001"], (
        "fallback включается только когда приоритетного типа не осталось")
    assert [p for _i, p, _u in rest2] == ["1002"], "остальное ждёт своей очереди"


def test_loader_loop_hands_out_every_item_once(monkeypatch):
    """Модель «грузчик»: 8 воркеров, 40 товаров (32 hit + 8 miss), выдача по одному.

    Три свойства разом: каждая выдача — ровно один товар, каждый товар отдан
    ровно один раз, и ленивый старт НЕ сломан: ни один браузер не поднимается,
    пока в очереди есть cache-hit'ы (ровно те 32 hit'а уходят воркерам без
    браузера). Число браузеров на хвосте (где hit'ов не осталось) равно числу
    miss'ов, а не пачкам: при batch_size=1 каждый свободный воркер забирает по
    одному miss'у — это расплата за параллелизм, а не за лень раздачи.
    """
    hits = {str(p) for p in range(1000, 1032)}      # 32 cache-hit
    stale = {str(p) for p in range(1032, 1040)}     # 8 cache-miss
    _patch_cache(monkeypatch, hits, stale)
    q = deque(_items(40))
    cfg = {"max_age_s": 3600}
    browsers = {w: False for w in range(1, 9)}
    given = []
    batches = 0
    hits_before_first_browser = None
    guard = 0
    while q and guard < 500:
        guard += 1
        for w in range(1, 9):
            batch = PL._take_batch(q, 1, browsers[w], cfg, w, {}, {})
            if not batch:
                continue
            assert len(batch) == 1, "выдача строго одного товара"
            batches += 1
            for _idx, pid, _url in batch:
                given.append(pid)
                if pid in hits and hits_before_first_browser is None \
                        and any(browsers.values()):
                    hits_before_first_browser = len(given)
                if pid in stale and not browsers[w]:
                    browsers[w] = True
    assert batches == 40, "40 раздач на 40 товаров"
    assert len(given) == len(set(given)), "повторной выдачи нет"
    assert sorted(given) == sorted(str(p) for p in range(1000, 1040)), (
        "ни один товар не потерян и не выдан дважды")
    assert hits_before_first_browser is None, (
        "браузер поднялся, пока в очереди ещё были cache-hit'ы — ленивость "
        f"сломана (первый браузер после {hits_before_first_browser} товаров)")
    assert sum(browsers.values()) <= len(stale)


# ---- ленивый старт браузера на одном товаре ----------------------------------

def _process_one(monkeypatch, items, *, sess=None, hits=(), stale=()):
    _patch_cache(monkeypatch, hits=hits, stale=stale)
    calls = {"cache": [], "shard": [], "sessions": 0}
    reports = []

    def _fake_cache_only(batch, cfg, on_progress=None):
        calls["cache"].extend(pid for _i, pid, _u in batch)
        out = [{"pid": p, "ok": True} for _i, p, _u in batch]
        if on_progress is not None:
            on_progress(out, len(out), len(out))
        return out

    def _fake_shard(spec, s, m, batch, cfg, **kw):
        calls["shard"].append([pid for _i, pid, _u in batch])
        if kw.get("on_progress") is not None:
            out = [{"pid": p, "ok": True} for _i, p, _u in batch]
            kw["on_progress"](out, len(out), len(out))
        return out

    def _fake_ensure():
        calls["sessions"] += 1
        return "SESS", "MGR"

    monkeypatch.setattr(PL, "_collect_batch_without_browser", _fake_cache_only)
    monkeypatch.setattr(PL, "_run_shard", _fake_shard)
    results, new_sess, _mgr = PL._process_batch(
        {"worker_id": 1}, items, {"max_age_s": 3600}, sess, None,
        ensure_session=_fake_ensure, report=reports.append,
        shard_cfg={"max_recycles": 2, "recovery": {}, "on_recovery": None})
    return results, calls, reports, new_sess


def test_single_cache_hit_never_starts_browser(monkeypatch):
    """Товар из кэша не поднимает Chromium даже при выдаче по одному."""
    results, calls, _reports, new_sess = _process_one(
        monkeypatch, _items(1), hits={"1000"})
    assert calls["sessions"] == 0, "браузер не поднимаем ради cache-hit"
    assert calls["shard"] == [], "живой шард не нужен"
    assert calls["cache"] == ["1000"]
    assert new_sess is None
    assert [r["pid"] for r in results] == ["1000"]


def test_single_cache_miss_starts_browser(monkeypatch):
    """cache-miss поднимает браузер ровно один раз на этот товар."""
    _results, calls, _reports, new_sess = _process_one(
        monkeypatch, _items(1), hits=set())
    assert calls["sessions"] == 1
    assert calls["shard"] == [["1000"]]
    assert new_sess == "SESS"


def test_single_item_sends_no_partial_progress_report(monkeypatch):
    """PROGRESS_EVERY=5 при выдаче в 1 товар не даёт отчёта — и не должен.

    Частичного состояния внутри выдачи не существует; per-item payload идёт
    через done_batch (см. test_reader_accumulates_products).
    """
    _results, _calls, reports, _sess = _process_one(
        monkeypatch, _items(1), hits=set())
    assert reports == [], "n=1 < PROGRESS_EVERY → лишнего отчёта в pipe нет"


# ---- per-item payload не теряется (аудит 2026-10-06, находка 4) ---------------

def test_reader_accumulates_products_across_done_batches():
    """done_batch несёт products за ОДНУ выдачу — payload обязан копить их.

    Без накопления при terminate/respawn в merge уезжал бы последний товар
    (при batch_size=1 — ровно один), а остальные собранные помечались бы
    terminated/failed.
    """
    import multiprocessing
    parent, child = multiprocessing.Pipe(duplex=False)
    wctx = PL.WorkerContext({"worker_id": "1"}, proc=object(), conn=parent)
    events = []
    t = threading.Thread(
        target=PL._reader_loop,
        args=(wctx, lambda w, m: events.append(m)), daemon=True)
    t.start()
    child.send({"t": "done_batch", "worker": 1, "payload": {
        "products": [{"index": 0, "result": {"pid": "1000", "ok": True}}],
        "processed_indexes": [0], "failed_indexes": []}})
    assert _wait_until(lambda: len(wctx.acc_products) == 1)
    child.send({"t": "done_batch", "worker": 1, "payload": {
        "products": [{"index": 1, "result": {"pid": "1001", "ok": False}}],
        "processed_indexes": [0, 1], "failed_indexes": [1]}})
    assert _wait_until(lambda: len(wctx.acc_products) == 2)
    child.close()
    t.join(3)

    merged = PL._with_acc_products(wctx.payload, wctx)
    assert [e["index"] for e in merged["products"]] == [0, 1], (
        "payload обязан содержать products всех выдач, а не последней")
    assert wctx.waiting_for_batch is True, "после done_batch воркер ждёт выдачи"
    assert [e["t"] for e in events] == ["done_batch", "done_batch"], (
        "каждый товар даёт событие оркестратору — живой сбор виден в логе "
        "демона даже при выдаче по одному")


def test_with_acc_products_keeps_payload_when_nothing_absorbed():
    """Пустой накопитель — payload возвращается как есть (без мусорных ключей)."""
    wctx = PL.WorkerContext({"worker_id": "1"}, proc=object(), conn=None)
    pl = {"worker_idx": 1, "x": 1}
    assert PL._with_acc_products(pl, wctx) is pl


# ---- восстановление: товар возвращается в очередь ровно один раз -------------

def test_broken_pipe_returns_item_once_without_owner_leak(monkeypatch):
    """Обрыв p2w-канала: товар возвращён в очередь ОДИН раз и без записи в owner.

    Если owner оставить, _requeue_unfinished павшего воркера вернул бы те же
    индексы вторым разом — товар оказался бы в очереди дважды.
    """
    monkeypatch.setattr(PL, "_cleanup_worker", lambda wctx: None)
    monkeypatch.setattr(PL, "_finalize_worker", lambda wctx: None)
    shared = deque(_items(2))
    completed, owner = set(), {}
    w = _mk_worker(1, pipe=_FakePipe(broken=True))
    PL._await_workers([w], deadline=time.monotonic() + 0.4,
                      max_worker_restarts=0, items_by_index={}, gather_cfg={},
                      max_recycles=2, session_budget=None, poll=0.01,
                      shared_items=shared, completed_indexes=completed,
                      owner=owner, pid_by_idx={0: "1000", 1: "1001"},
                      url_by_idx={0: "u0", 1: "u1"}, batch_size=1)
    assert [i for i, _p, _u in shared] == [0, 1], (
        "оба товара в очереди, ни один не продублирован")
    assert owner == {}, "owner не должен хранить индексы невыданного товара"
    assert w.scheduler_metrics["p2w_broken"] == 1
    assert w.scheduler_metrics["requeue_count"] == 0


def test_crash_returns_unfinished_item_once(monkeypatch):
    """Упавший воркер: незавершённый товар вернулся в очередь ровно один раз.

    Полный цикл выдачи по одному: выдача ровно 1 товар → падение без
    done_batch → _requeue_unfinished → респавн получает тот же индекс →
    завершение → индекс уходит в completed_indexes и больше не выдаётся.
    """
    _patch_cache(monkeypatch, hits=set(), stale=set())
    monkeypatch.setattr(PL, "_cleanup_worker", lambda wctx: None)
    monkeypatch.setattr(PL, "_finalize_worker", lambda wctx: None)
    shared = deque(_items(2))
    completed, owner = set(), {}
    w1 = _mk_worker(1)
    box = {}

    def _fake_respawn(wctx, pending, items_by_index, gather_cfg, max_recycles,
                      session_budget, on_event=None, worker_log=None,
                      dynamic=False, canary_url=None):
        new = _mk_worker(wctx.worker_idx)
        box["w"] = new
        box["pending"] = list(pending)
        return new

    monkeypatch.setattr(PL, "_respawn", _fake_respawn)

    t = threading.Thread(
        target=PL._await_workers, args=([w1],),
        kwargs=dict(deadline=time.monotonic() + 10,
                    max_worker_restarts=2, items_by_index={}, gather_cfg={},
                    max_recycles=2, session_budget=None, poll=0.01,
                    shared_items=shared, completed_indexes=completed,
                    owner=owner, pid_by_idx={0: "1000", 1: "1001"},
                    url_by_idx={0: "u0", 1: "u1"}, batch_size=1),
        daemon=True)
    t.start()

    assert _wait_until(lambda: len(w1.p2w_conn.batches()) == 1), (
        "воркер должен получить ровно одну выдачу")
    first = w1.p2w_conn.batches()[0]
    assert len(first) == 1 and first[0][0] == 0

    # Воркер упал, не отдав результат: done_batch не приходил.
    w1.payload = {"fatal_error": "boom", "products": []}
    w1.done = True
    w1.proc._alive = False

    assert _wait_until(lambda: "w" in box), "респавн не случился"
    new = box["w"]
    assert _wait_until(lambda: len(new.p2w_conn.batches()) == 1), (
        "замена должна получить возвращённый товар")
    assert [i for i, _p, _u in new.p2w_conn.batches()[0]] == [0], (
        "тот же индекс вернулся в работу")
    assert [i for i, _p, _u in shared] == [1], (
        "в очереди не должно быть двух копий возвращённого товара")

    # Завершение товара 0 → completed_indexes; дальше выдаётся только товар 1.
    new.batch_completed.extend([0])
    new.waiting_for_batch = True
    assert _wait_until(lambda: len(new.p2w_conn.batches()) == 2)
    assert [i for i, _p, _u in new.p2w_conn.batches()[1]] == [1]
    new.batch_completed.extend([1])
    new.waiting_for_batch = True
    assert _wait_until(lambda: any(m.get("t") == "none"
                                   for m in new.p2w_conn.sent))
    new.payload = {"products": [], "fatal_error": None}
    new.done = True
    new.proc._alive = False
    t.join(5)

    hand_out = [i for b in new.p2w_conn.batches() for i, _p, _u in b]
    assert hand_out == [0, 1], "ни один товар не выдан повторно после done_batch"
    assert completed == {0, 1}, (
        "completed_indexes — immutable guard; сюда обязаны попасть индексы, "
        "завершённые воркером-ЗАМЕНОЙ (их раньше терял обход исходного "
        "списка воркеров, и товар уходил в повторную выдачу)")