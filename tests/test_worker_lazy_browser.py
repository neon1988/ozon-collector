"""Контракт: браузер воркера поднимается ТОЛЬКО когда нужны данные со страницы.

Проверяется сквозной путь через настоящий `worker_main` (spawn, doubles-режим):
диспетчер выдаёт пачку → воркер решает `_batch_needs_browser` → и только при
cache-miss создаёт Session (в проде Session.__init__ → `launch_and_wait()`,
то есть реальный запуск Chromium) и шлёт warmup-событие родителю.

Кейсы:
  * вся пачка из свежего кэша → 0 запусков браузера, товары собраны из кэша;
  * пачка с одним cache-miss → ровно 1 запуск, и только у этого воркера;
  * две пачки с cache-miss → всё ещё 1 запуск (Session переиспользуется).

Раньше это ленивое поведение было только в комментарии и в логе воркера —
проверяем его тестом, чтобы регрессия (эвристика «создать Session сразу»)
не прошла молча.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque

import pytest

import test_parallel as TP
from ozon_collector import parallel as PL
from ozon_collector import VERSION

MAX_AGE_S = 86400


def _seed_cache(tmp_path, pids) -> None:
    """Свежий валидный raw-кэш (cache_check -> hit) для указанных pid."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    from datetime import datetime, timedelta, timezone

    stamp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    for pid in pids:
        raw = {
            "schema_version": "2.0",
            "collector_version": VERSION,
            "pid": str(pid),
            "incomplete": False,
            "collected_at_utc": stamp,
            "reviews": [],
            "product": {"title": f"Товар {pid}"},
        }
        with open(data / f"{pid}_raw.json", "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False)


@pytest.mark.timeout(120)
def test_worker_does_not_start_browser_on_full_cache_hit(monkeypatch, tmp_path):
    """Вся пачка в кэше → воркер не создаёт Session, браузер не поднимается."""
    items = TP._items(4)
    _seed_cache(tmp_path, [pid for _i, pid, _u in items])
    # CWD воркера наследует tmp_path → cache_check("data/…") читает наш кэш,
    # а не боевой data/ в репозитории.
    monkeypatch.chdir(tmp_path)

    wctxs = TP._spawn_workers(
        [PL.worker_spec(1)], items, monkeypatch, tmp_path, "ok",
        gather_cfg={"max_age_s": MAX_AGE_S})

    w = wctxs[0]
    assert w.exitcode == 0
    assert w.payload["fatal_error"] is None
    # товары собраны — но браузер не понадобился
    assert sorted(w.payload["processed_indexes"]) == [0, 1, 2, 3]
    assert w.browsers_started == 0, "браузер поднялся без cache-miss"
    assert w.warmups == []


@pytest.mark.timeout(120)
def test_worker_starts_browser_only_for_cache_miss(monkeypatch, tmp_path):
    """Один воркер целиком из кэша, второй с miss → браузер только у второго.

    shard_items раскладывает round-robin: worker-1 получает индексы 0,2,4...,
    worker-2 — 1,3,5... Поэтому кэшируем pid'ы ИНДЕКСОВ 0 и 2 (то есть 1000 и
    1002) — шард worker-1 целиком попадает в кэш, шард worker-2 целиком мимо.
    """
    items = TP._items(4)                       # индексы 0..3, pid 1000..1003
    w1_pids = {items[0][1], items[2][1]}       # шард worker-1 по round-robin
    _seed_cache(tmp_path, w1_pids)
    monkeypatch.chdir(tmp_path)

    specs = [PL.worker_spec(1), PL.worker_spec(2)]
    wctxs = TP._spawn_workers(
        specs, items, monkeypatch, tmp_path, "ok",
        gather_cfg={"max_age_s": MAX_AGE_S})

    started = {w.spec["worker_id"]: w.browsers_started for w in wctxs}
    assert started == {1: 0, 2: 1}, f"браузер не по требованию: {started}"
    for w in wctxs:
        assert w.exitcode == 0
        assert w.payload["fatal_error"] is None
    launched = [w for w in wctxs if w.browsers_started]
    assert launched[0].warmups[0]["worker"] == launched[0].spec["worker_id"]
    done = sorted(sum((w.payload["processed_indexes"] for w in wctxs), []))
    assert done == [0, 1, 2, 3]


@pytest.mark.timeout(120)
def test_worker_reuses_one_browser_across_batches(monkeypatch, tmp_path):
    """Две пачки с cache-miss → браузер поднимается ОДИН раз и переиспользуется."""
    items = TP._items(2, start=0)   # без кэша → каждая пачка тянет браузер
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OZON_PROFILE_BASE", str(tmp_path))
    monkeypatch.setenv("OZON_WORKER_DOUBLES", "1")
    monkeypatch.setenv("OZON_WORKER_TEST_SCENARIO", "ok")
    monkeypatch.setenv("OZON_COLLECTOR_LOG", str(tmp_path / "main.log"))

    spec = PL.worker_spec(1)
    task = {"items": [], "gather_cfg": {"max_age_s": MAX_AGE_S},
            "max_recycles": 2, "scroll_jitter_seed": 1}
    w = PL._spawn_ctx(spec, task)

    # диспетчер: две пачки по одному товару, потом "none"
    queue = deque([items[0:1], items[1:2], None])
    deadline = time.monotonic() + 60
    while not (w.done or not w.proc.is_alive()) and time.monotonic() < deadline:
        if w.waiting_for_batch and queue:
            batch = queue.popleft()
            msg = {"t": "none"} if batch is None else {"t": "next", "batch": batch}
            w.waiting_for_batch = False
            try:
                w.p2w_conn.send(msg)
            except (BrokenPipeError, EOFError, OSError, ValueError):
                break
        time.sleep(0.02)
    w.proc.join(5)
    w.exitcode = w.proc.exitcode

    assert w.exitcode == 0
    assert w.payload["fatal_error"] is None
    assert sorted(w.payload["processed_indexes"]) == [0, 1]
    # ключевое: НЕ по одному браузеру на пачку
    assert w.browsers_started == 1, "браузер переподнимается на каждой пачке"
