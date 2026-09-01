"""Тест-дубли воркер-движка для Фазы B (включаются env OZON_WORKER_DOUBLES=1).

Используются только из worker_main (spawn-процесс): подменяют Session,
BrowserProcessManager и шард-цикл на детерминированные заглушки, чтобы тесты
(unit/worker-макет/изоляция recovery) работали без живого Ozon и Chromium.
Сценарии шард-цикла — через env OZON_WORKER_TEST_SCENARIO:
  ok          — все товары собраны (ok=True)
  crash_once  — у воркера-1 ровно ОДНО recovery-событие (имитация kill браузера
                + успешный recycle), у воркера-2 ничего
  hang        — бесконечное ожидание (проверка terminate родителя)
  raise       — RuntimeError внутри шард-цикла → payload fatal_error
"""
from __future__ import annotations

import os
import time


class FakeSession:
    """Session-дубль: только close() нужен worker_main в finally."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.closed = False

    def close(self):
        self.closed = True

    def new_page(self, close_current: bool = True):
        return None


class FakeManager:
    """BrowserProcessManager-дубль: force_stop запоминает records."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.profile_token = spec["profile_token"]
        self.stopped_records = []

    def force_stop(self, records, wait_s: float | None = None):
        self.stopped_records = list(records)


def fake_session(spec: dict) -> FakeSession:
    return FakeSession(spec)


def fake_manager(spec: dict) -> FakeManager:
    return FakeManager(spec)


def _ok(pid: str) -> dict:
    return {
        "pid": pid, "ok": True, "error": None,
        "raw": {"pid": pid, "reviews": [], "product": {"title": f"Товар {pid}"}},
        "markdown": f"# Товар {pid}\n", "cache": "hit", "ocr": "off",
        "recycle_attempts": 0,
    }


def fake_run_shard(spec, sess, manager, items, gather_cfg,
                   max_recycles, recovery, on_recovery,
                   on_progress=None) -> list[dict]:
    """Детерминированный шард-цикл по сценарию из env OZON_WORKER_TEST_SCENARIO."""
    k = spec["worker_id"]
    scenario = os.environ.get("OZON_WORKER_TEST_SCENARIO", "ok")
    if scenario == "hang":
        time.sleep(3600)
        raise RuntimeError("unreachable: hang worker вернулся")
    if scenario == "hang_after_progress":
        results = []
        for idx, pid, _url in items:
            results.append(_ok(pid))
            if on_progress is not None:
                try:
                    on_progress(results, len(results), len(items))
                except Exception:
                    pass
            if len(results) >= 5:
                break
        time.sleep(3600)
        raise RuntimeError("unreachable: hang_after_progress worker вернулся")
    if scenario == "raise":
        raise RuntimeError("boom in worker")
    results = []
    for idx, pid, _url in items:
        if scenario == "crash_once" and k == 1 and idx == items[0][0]:
            ev = {"pid": pid, "attempt": 1, "kind": "renderer_failure"}
            recovery["recycles"] += 1
            recovery["events"].append(ev)
            on_recovery(ev)
        results.append(_ok(pid))
        if on_progress is not None:
            try:
                on_progress(results, len(results), len(items))
            except Exception:
                pass
    return results
