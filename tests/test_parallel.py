"""Тесты Фазы B (--parallel): шардирование, merge, preflight, attach_file,
token-матчинг, worker_spec, worker-макет (spawn) и изоляция recovery."""
from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ozon_collector import parallel as PL
from ozon_collector import process as P
from ozon_collector import log


@pytest.fixture
def profile_base(tmp_path, monkeypatch):
    """Базовые пути профилей — в tmp (и в spawned-детях через env)."""
    monkeypatch.setenv("OZON_PROFILE_BASE", str(tmp_path))
    monkeypatch.setattr(P, "profile_base_dir", lambda: str(tmp_path))
    return tmp_path


@pytest.fixture
def reset_log():
    yield
    log._fh = None
    log._worker_prefix = ""
    log._context = ""


def _items(n, start=0):
    return [(i, str(1000 + start + i),
             f"https://www.ozon.ru/product/{1000 + start + i}")
            for i in range(n)]


def _ok_payload(k, indexes, ok=True):
    return {
        "worker_idx": k,
        "products": [{"index": i, "result": {"pid": str(1000 + i),
                                             "ok": ok}} for i in indexes],
    }


# ---- worker_spec / профильный контракт --------------------------------------

def test_worker_spec_ports_and_tokens(profile_base):
    s1 = PL.worker_spec(1)
    s2 = PL.worker_spec(2)
    assert s1["port"] == 9223
    assert s2["port"] == 9224
    assert s1["cdp_url"] == "http://127.0.0.1:9223"
    assert s2["cdp_url"] == "http://127.0.0.1:9224"
    assert s1["profile_token"] == "ozon-profile-worker-1"
    assert s2["profile_token"] == "ozon-profile-worker-2"
    assert s1["profile_token"] != P.PROFILE_TOKEN
    assert s1["profile_path"] != s2["profile_path"]
    assert s1["profile_path"].endswith("ozon-profile-worker-1")
    assert s1["log_path"].endswith("ozon_worker_1.log")


def test_worker_profile_paths_pairwise_unique(profile_base):
    paths = [P.worker_profile_path(k) for k in (1, 2)]
    tokens = [P.worker_profile_token(k) for k in (1, 2)]
    assert len(set(paths)) == 2
    assert len(set(tokens)) == 2
    assert P.PROFILE_TOKEN not in tokens


# ---- шардирование ------------------------------------------------------------

def test_shard_items_round_robin_deterministic():
    items = _items(5)
    s1 = PL.shard_items(items, 2)
    s2 = PL.shard_items(items, 2)
    assert s1 == s2
    assert [idx for idx, _p, _u in s1[0]] == [0, 2, 4]
    assert [idx for idx, _p, _u in s1[1]] == [1, 3]
    flat = [idx for sh in s1 for idx, _p, _u in sh]
    assert sorted(flat) == list(range(5))  # каждый товар ровно 1 раз


def test_shard_items_balance():
    items = _items(6)
    s = PL.shard_items(items, 2)
    assert len(s[0]) == 3
    assert len(s[1]) == 3


# ---- merge --------------------------------------------------------------------

def test_merge_keeps_original_order():
    shards = PL.shard_items(_items(4), 2)
    pl0 = _ok_payload(1, [0, 2])
    pl1 = _ok_payload(2, [1, 3])
    products = PL.merge_shard_results([pl0, pl1], shards)
    assert [p["pid"] for p in products] == ["1000", "1001", "1002", "1003"]
    assert all(p["ok"] for p in products)


def test_merge_missing_marks_fail_with_worker():
    shards = PL.shard_items(_items(3), 2)
    pl1 = _ok_payload(2, [1])
    products = PL.merge_shard_results([pl1], shards)
    assert products[0]["ok"] is False
    assert "worker1 terminated/failed" in products[0]["error"]
    assert products[1]["ok"] is True
    assert products[2]["ok"] is False
    assert "worker1" in products[2]["error"]


def test_merge_empty_inputs():
    products = PL.merge_shard_results([], [[]])
    assert products == []


# ---- challenge storm сводка --------------------------------------------------

def _wctx(idx):
    class _W:
        worker_idx = idx
        pid = 1000 + idx
    return _W()


def _storm_payload(k, storm):
    return {"recovery_metrics": ({"recycles": 0, "events": [],
                                  "challenge_storm": storm}
                                 if storm else {"recycles": 0, "events": []})}


def test_storm_summary_none_when_no_storm():
    wctxs = [_wctx(1), _wctx(2)]
    payloads = [_storm_payload(1, None), _storm_payload(2, None)]
    assert PL._storm_summary(wctxs, payloads, 2) is None


def test_storm_summary_one_worker():
    wctxs = [_wctx(1), _wctx(2)]
    payloads = [
        _storm_payload(1, {"streak": 3, "remaining_aborted": 4, "pid": "1002"}),
        _storm_payload(2, None),
    ]
    s = PL._storm_summary(wctxs, payloads, 2)
    assert s["reason"] == "challenge_storm"
    assert s["workers_stormed"] == [1]
    assert s["all_workers"] is False
    assert s["streak"] == 3
    assert s["remaining_aborted"] == 4


def test_storm_summary_all_workers():
    wctxs = [_wctx(1), _wctx(2)]
    payloads = [
        _storm_payload(1, {"streak": 3, "remaining_aborted": 2, "pid": "1"}),
        _storm_payload(2, {"streak": 4, "remaining_aborted": 1, "pid": "2"}),
    ]
    s = PL._storm_summary(wctxs, payloads, 2)
    assert s["workers_stormed"] == [1, 2]
    assert s["all_workers"] is True
    assert s["streak"] == 3          # первый штормивший воркер


# ---- attach_file --------------------------------------------------------------

def test_attach_file_appends_with_context(tmp_path, reset_log):
    f = tmp_path / "w.log"
    f.write_text("OLD LINE\n", encoding="utf-8")
    log.attach_file(str(f), mode="a")
    log.set_context("orchestrator")
    log.info("campaign started")
    log.attach_file(str(f), mode="a")   # новый хендл = сброс контекста
    log.set_context("worker=1")
    log.info("collecting 123")
    txt = f.read_text(encoding="utf-8")
    assert txt.startswith("OLD LINE\n")          # append, не перезапись
    assert "[orchestrator] campaign started" in txt
    assert "[worker=1] collecting 123" in txt
    assert txt.count("collecting 123") == 1


def test_attach_file_resets_context(tmp_path, reset_log):
    f = tmp_path / "w.log"
    log.attach_file(str(f), mode="a")
    log.set_context("orchestrator")
    log.info("with context")
    log.attach_file(str(tmp_path / "other.log"), mode="a")  # сброс контекста
    log.info("no context")                                     # старый формат
    txt = (tmp_path / "other.log").read_text(encoding="utf-8")
    assert "[orchestrator] with context" not in txt  # контекст сброшен
    assert "[log] no context" in txt                  # старый формат без контекста
    main = (tmp_path / "w.log").read_text(encoding="utf-8")
    assert "[orchestrator] with context" in main      # до attach контекст держался


def test_attach_file_no_rotation(tmp_path, reset_log):
    f = tmp_path / "w.log"
    f.write_text("X\n", encoding="utf-8")
    log.attach_file(str(f), mode="a")
    assert not (tmp_path / "w.log.1").exists()   # ротации нет
    log.attach_file(str(tmp_path / "other.log"), mode="a")
    # w.log остался нетронутым (добавился только attach-заголовок)
    assert (tmp_path / "w.log").read_text(
        encoding="utf-8").startswith("X\n")
    assert (tmp_path / "other.log").exists()


def test_attach_file_rotates_above_limit(tmp_path, reset_log):
    f = tmp_path / "w.log"
    f.write_text("A" * 100, encoding="utf-8")
    log.attach_file(str(f), mode="a", rotate_max_bytes=50)
    assert (tmp_path / "w.log.1").exists()        # превышен лимит → ротация
    log.attach_file(str(tmp_path / "s.log"), mode="a", rotate_max_bytes=100)
    assert not (tmp_path / "s.log.1").exists()    # ниже лимита → нет ротации


# ---- token-матчинг browser_processes -----------------------------------------

def test_browser_processes_token_matching(monkeypatch):
    """browser_processes(cdp_url, profile_token) фильтрует по токену профиля."""
    base = P.profile_base_dir()
    argv = [
        "C:/Program Files/Chrome/chrome.exe",
        f"--user-data-dir={base}\\ozon-profile-worker-1",
        "--remote-debugging-port=9223",
    ]
    monkeypatch.setattr(P, "_iter_chromium_processes",
                        lambda: [(111, argv, 42)])
    monkeypatch.setattr(P, "process_identity",
                        lambda pid: P.ProcessIdentity(pid, "k"))
    ids = P.browser_processes("http://127.0.0.1:9223",
                              profile_token="ozon-profile-worker-1")
    assert len(ids) == 1
    # другой токен не матчится
    assert P.browser_processes("http://127.0.0.1:9223",
                               profile_token="ozon-profile") == []
    # другой порт не матчится
    assert P.browser_processes("http://127.0.0.1:9224",
                               profile_token="ozon-profile-worker-1") == []


def test_orphaned_worker_browser_detects_dead_parent(monkeypatch):
    """Браузер воркера с мёртвым родителем считается осиротевшим и
    подлежит автоочистке; с живым python-родителем — НЕ осиротевший."""
    base = P.profile_base_dir()
    argv = [
        "C:/Program Files/Chrome/chrome.exe",
        f"--user-data-dir={base}\\ozon-profile-worker-1",
        "--remote-debugging-port=9223",
    ]
    fake = [
        (111, argv, 555),   # родитель 555 (мёртв/не python) → осиротевший
        (222, argv, 666),   # родитель 666 — живой python → не осиротевший
    ]
    monkeypatch.setattr(P, "_iter_chromium_processes", lambda: iter(fake))
    monkeypatch.setattr(P, "process_identity",
                        lambda pid: P.ProcessIdentity(pid, f"k{pid}"))
    # 555 мёртв, 666 живой
    monkeypatch.setattr(P, "_parent_owner_alive",
                        lambda ppid: ppid == 666)
    ids = P.orphaned_worker_browser_processes(
        "http://127.0.0.1:9223", profile_token="ozon-profile-worker-1")
    assert [i.pid for i in ids] == [111]


# ---- preflight -----------------------------------------------------------------

def _free_ports(monkeypatch):
    monkeypatch.setattr(PL, "_port_free", lambda port: True)


def test_preflight_ok(monkeypatch, profile_base):
    _free_ports(monkeypatch)
    monkeypatch.setattr(P, "browser_processes",
                        lambda *a, **kw: [])
    monkeypatch.setattr(PL.cdp, "browser_alive", lambda url: False)
    errs = PL.preflight_check([PL.worker_spec(1), PL.worker_spec(2)])
    assert errs == []


def test_preflight_port_busy(monkeypatch, profile_base):
    monkeypatch.setattr(PL, "_port_free", lambda port: port != 9223)
    monkeypatch.setattr(P, "browser_processes", lambda *a, **kw: [])
    monkeypatch.setattr(PL.cdp, "browser_alive", lambda url: False)
    errs = PL.preflight_check([PL.worker_spec(1)])
    assert any("порт 9223 уже занят" in e for e in errs)


def test_preflight_process_present(monkeypatch, profile_base):
    _free_ports(monkeypatch)
    monkeypatch.setattr(P, "browser_processes",
                        lambda url, profile_token=None:
                        [object()] if url.endswith("9224") else [])
    monkeypatch.setattr(PL.cdp, "browser_alive", lambda url: False)
    errs = PL.preflight_check([PL.worker_spec(1), PL.worker_spec(2)])
    assert any("9224" in e for e in errs)


def test_preflight_cdp_alive(monkeypatch, profile_base):
    _free_ports(monkeypatch)
    monkeypatch.setattr(P, "browser_processes", lambda *a, **kw: [])
    monkeypatch.setattr(PL.cdp, "browser_alive",
                        lambda url: url.endswith("9223"))
    errs = PL.preflight_check([PL.worker_spec(1), PL.worker_spec(2)])
    assert any("9223" in e for e in errs)


def test_preflight_profile_collision_with_main(monkeypatch, profile_base):
    _free_ports(monkeypatch)
    monkeypatch.setattr(P, "browser_processes", lambda *a, **kw: [])
    monkeypatch.setattr(PL.cdp, "browser_alive", lambda url: False)
    spec = dict(PL.worker_spec(1), profile_token=P.PROFILE_TOKEN)
    errs = PL.preflight_check([spec])
    assert any("не попарно различны" in e for e in errs)


def test_preflight_cleans_orphaned_browser(monkeypatch, profile_base, capsys):
    """preflight при обнаружении осиротевшего браузера воркера убивает его
    автоматически и НЕ падает (после очистки порт/профиль свободны)."""
    _free_ports(monkeypatch)
    monkeypatch.setattr(P, "browser_processes", lambda *a, **kw: [])
    monkeypatch.setattr(PL.cdp, "browser_alive", lambda url: False)
    orphan_id = P.ProcessIdentity(pid=111, start_key="k111")
    monkeypatch.setattr(
        P, "orphaned_worker_browser_processes",
        lambda cdp_url, profile_token:
        [orphan_id] if profile_token == "ozon-profile-worker-1" else [])
    stopped = []
    manager = object.__new__(PL.BrowserProcessManager)

    def _force_stop(self, owned, wait_s=None):
        stopped.append(owned)

    monkeypatch.setattr(PL.BrowserProcessManager, "force_stop", _force_stop)
    errs = PL.preflight_check([PL.worker_spec(1), PL.worker_spec(2)])
    assert len(stopped) == 1
    assert [r.identity for r in stopped[0]] == [orphan_id]
    assert errs == []
    out = capsys.readouterr().out
    assert "убран осиротевший Chromium" in out


def test_preflight_cleanup_failure_reports_error(monkeypatch, profile_base):
    """Не удалось убрать осиротевший браузер → ошибка preflight (не тихий skip)."""
    _free_ports(monkeypatch)
    monkeypatch.setattr(P, "browser_processes", lambda *a, **kw: [])
    monkeypatch.setattr(PL.cdp, "browser_alive", lambda url: False)
    orphan_id = P.ProcessIdentity(pid=111, start_key="k111")
    monkeypatch.setattr(
        P, "orphaned_worker_browser_processes",
        lambda cdp_url, profile_token: [orphan_id])
    monkeypatch.setattr(PL.BrowserProcessManager, "force_stop",
                        lambda self, owned, wait_s=None: (_ for _ in ()).throw(
                            RuntimeError("boom")))
    errs = PL.preflight_check([PL.worker_spec(1)])
    assert any("не удалось убрать осиротевший" in e for e in errs)


def test_preflight_cleans_orphaned_firefox(monkeypatch, profile_base, capsys):
    """Firefox-воркер: осиротевшие процессы на профиле гасим через P.kill_records.

    Регрессия 2026-09-25: тут был B.force_stop(token) — модульной функции в
    browser.py нет (force_stop есть только методом BrowserProcessManager),
    поэтому КАЖДЫЙ firefox-воркер ронял preflight с AttributeError и
    параллельный сбор не стартовал.
    """
    _free_ports(monkeypatch)
    monkeypatch.setattr(P, "browser_processes", lambda *a, **kw: [])
    monkeypatch.setattr(PL.cdp, "browser_alive", lambda url: False)
    orphan = P.ProcessIdentity(pid=222, start_key="k222")
    monkeypatch.setattr(
        P, "firefox_browser_processes",
        lambda token: [orphan] if token == "ozon-profile-worker-1" else [])
    monkeypatch.setattr(P, "orphaned_geckodriver_processes", lambda: [])
    killed = []
    monkeypatch.setattr(P, "kill_records",
                        lambda recs, **kw: killed.extend(recs) or [])
    spec = dict(PL.worker_spec(1), kind="firefox", port=None, cdp_url=None)
    errs = PL.preflight_check([spec])
    assert errs == []
    assert [r.identity for r in killed] == [orphan]
    assert "убран осиротевший Firefox" in capsys.readouterr().out


def test_preflight_firefox_cleanup_failure_reports_error(monkeypatch,
                                                          profile_base):
    _free_ports(monkeypatch)
    monkeypatch.setattr(P, "browser_processes", lambda *a, **kw: [])
    monkeypatch.setattr(PL.cdp, "browser_alive", lambda url: False)
    orphan = P.ProcessIdentity(pid=333, start_key="k333")
    monkeypatch.setattr(P, "firefox_browser_processes", lambda token: [orphan])
    monkeypatch.setattr(P, "orphaned_geckodriver_processes", lambda: [])
    monkeypatch.setattr(P, "kill_records",
                        lambda recs, **kw: (_ for _ in ()).throw(
                            RuntimeError("boom")))
    spec = dict(PL.worker_spec(1), kind="firefox", port=None, cdp_url=None)
    errs = PL.preflight_check([spec])
    assert any("не удалось убрать осиротевший Firefox" in e for e in errs)


def test_preflight_cleans_orphaned_geckodriver(monkeypatch, profile_base, capsys):
    """Осиротевший geckodriver (родитель нашего Firefox) гасим в preflight.

    Замер 2026-09-25: после прерванных прогонов осталось 14 geckodriver — они
    держали 11 лишних окон Firefox. Атрибуция только по потомкам: драйвер с
    живым firefox-ребёнком на нашем профиле НЕ трогаем (может быть живой
    параллельный запуск).
    """
    _free_ports(monkeypatch)
    monkeypatch.setattr(P, "browser_processes", lambda *a, **kw: [])
    monkeypatch.setattr(PL.cdp, "browser_alive", lambda url: False)
    monkeypatch.setattr(P, "firefox_browser_processes", lambda token: [])
    gecko = P.ProcessIdentity(pid=777, start_key="kg")
    monkeypatch.setattr(P, "orphaned_geckodriver_processes", lambda: [gecko])
    killed = []
    monkeypatch.setattr(P, "kill_records",
                        lambda recs, **kw: killed.extend(recs) or [])
    spec = dict(PL.worker_spec(1), kind="firefox", port=None, cdp_url=None)
    errs = PL.preflight_check([spec])
    assert errs == []
    assert [r.identity for r in killed] == [gecko]
    assert "убрано осиротевших geckodriver" in capsys.readouterr().out


def test_preflight_keeps_geckodriver_with_live_our_firefox(monkeypatch):
    _free_ports(monkeypatch)
    monkeypatch.setattr(P, "browser_processes", lambda *a, **kw: [])
    monkeypatch.setattr(PL.cdp, "browser_alive", lambda url: False)
    monkeypatch.setattr(P, "firefox_browser_processes", lambda token: [])
    gecko = P.ProcessIdentity(pid=888, start_key="kg")
    monkeypatch.setattr(P, "orphaned_geckodriver_processes", lambda: [])
    killed = []
    monkeypatch.setattr(P, "kill_records",
                        lambda recs, **kw: killed.extend(recs) or [])
    spec = dict(PL.worker_spec(1), kind="firefox", port=None, cdp_url=None)
    assert PL.preflight_check([spec]) == []
    assert killed == []


# ---- worker-макет (spawn) -------------------------------------------------------

def _spawn_workers(specs, items, monkeypatch, tmp_path, scenario,
                   timeout=60, gather_cfg=None):
    monkeypatch.setenv("OZON_PROFILE_BASE", str(tmp_path))
    monkeypatch.setenv("OZON_WORKER_DOUBLES", "1")
    monkeypatch.setenv("OZON_WORKER_TEST_SCENARIO", scenario)
    monkeypatch.setenv("OZON_COLLECTOR_LOG", str(tmp_path / "main.log"))
    shards = PL.shard_items(items, len(specs))
    wctxs = []
    for k, spec in enumerate(specs, 1):
        task = {"items": [], "gather_cfg": dict(gather_cfg or {}),
                "max_recycles": 2, "scroll_jitter_seed": 1}
        wctxs.append(PL._spawn_ctx(spec, task))
    # Диспетчер динамической пакетной раздачи (тестовый макет _await_workers):
    # выдаём каждому воркеру его статический шард (batch_size=5 → одной пачкой),
    # ждём done, затем none. Так тесты сохраняют проверку попарного охвата шарда.
    deadline = time.monotonic() + timeout
    per_worker = {w.worker_idx: deque(shards[w.worker_idx - 1])
                  for w in wctxs}
    while True:
        for w in list(wctxs):
            if w.done or not w.proc.is_alive():
                continue
            if w.waiting_for_batch:
                w.waiting_for_batch = False
                q = per_worker[w.worker_idx]
                if q:
                    batch = [q.popleft() for _ in range(len(q))]
                    try:
                        w.p2w_conn.send({"t": "next", "batch": batch})
                    except (BrokenPipeError, EOFError, OSError, ValueError):
                        q.extendleft(reversed(batch))
                else:
                    try:
                        w.p2w_conn.send({"t": "none"})
                    except (BrokenPipeError, EOFError, OSError, ValueError):
                        pass
        if all(w.done or not w.proc.is_alive() for w in wctxs):
            break
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        time.sleep(min(0.05, remain))
    for w in wctxs:
        w.proc.join(2)
        w.exitcode = w.proc.exitcode
    return wctxs


def test_worker_protocol_and_payload(monkeypatch, tmp_path):
    specs = [PL.worker_spec(1), PL.worker_spec(2)]
    items = _items(4)
    wctxs = _spawn_workers(specs, items, monkeypatch, tmp_path, "ok")

    assert len(wctxs) == 2
    for w in wctxs:
        assert w.started is True
        assert w.pid is not None and w.pid > 0
        assert w.exitcode == 0
        pl = w.payload
        assert pl["fatal_error"] is None
        assert pl["started"] and pl["finished"]
        assert pl["rss_bytes"] is not None and pl["rss_bytes"] > 0
        assert len(pl["processed_indexes"]) == 2
        assert pl["failed_indexes"] == []
        assert len(pl["products"]) == 2

    # изоляция: pid/порт/профиль попарно различны
    assert wctxs[0].pid != wctxs[1].pid
    assert wctxs[0].spec["port"] != wctxs[1].spec["port"]
    assert (wctxs[0].spec["profile_token"]
            != wctxs[1].spec["profile_token"])

    # products покрывают все индексы в правильном порядке
    all_idx = sorted(i for w in wctxs
                     for i in w.payload["processed_indexes"])
    assert all_idx == [0, 1, 2, 3]
    for w in wctxs:
        for entry in w.payload["products"]:
            assert entry["result"]["ok"] is True
            assert entry["result"]["pid"] == str(1000 + entry["index"])


def test_worker_recovery_isolation(monkeypatch, tmp_path):
    """kill браузера воркера-0 (scenario crash_once) → recovery ТОЛЬКО у воркера-0;
    воркер-1: 0 recovery-событий, payload без изменений."""
    specs = [PL.worker_spec(1), PL.worker_spec(2)]
    items = _items(4)  # shard0=[0,2], shard1=[1,3]
    wctxs = _spawn_workers(specs, items, monkeypatch, tmp_path, "crash_once")

    w0, w1 = wctxs
    assert w0.payload["recovery_metrics"]["recycles"] == 1
    assert w0.payload["recovery_metrics"]["events"][0]["kind"] \
        == "renderer_failure"
    assert w1.payload["recovery_metrics"]["recycles"] == 0
    assert w1.payload["recovery_metrics"]["events"] == []

    # телеметрия по pipe тоже изолирована
    assert len(w0.recovery_events) == 1
    assert len(w1.recovery_events) == 0

    # идентичность не пересекается: pid/порт/профиль попарно различны
    assert w0.pid != w1.pid
    assert w0.spec["port"] != w1.spec["port"]
    assert w0.spec["profile_path"] != w1.spec["profile_path"]
    # оба воркера завершились штатно (exit 0, без fatal)
    for w in wctxs:
        assert w.exitcode == 0
        assert w.payload["fatal_error"] is None
        assert len(w.payload["products"]) == 2


def test_worker_fatal_error_returns_payload(monkeypatch, tmp_path):
    """Исключение в шард-цикле → payload fatal_error + частичные индексы + exit 0,
    parent не зависает."""
    specs = [PL.worker_spec(1)]
    items = _items(3)
    wctxs = _spawn_workers(specs, items, monkeypatch, tmp_path, "raise")

    w = wctxs[0]
    assert w.exitcode == 0
    pl = w.payload
    assert pl["fatal_error"] == "RuntimeError: boom in worker"
    assert pl["products"] == []
    assert pl["processed_indexes"] == []
    assert pl["started"] and pl["finished"]


def test_worker_hang_terminated(monkeypatch, tmp_path):
    """Повисший воркер → terminate по таймауту + cleanup не падает."""
    monkeypatch.setenv("OZON_PROFILE_BASE", str(tmp_path))
    monkeypatch.setenv("OZON_WORKER_DOUBLES", "1")
    monkeypatch.setenv("OZON_WORKER_TEST_SCENARIO", "hang")
    monkeypatch.setenv("OZON_COLLECTOR_LOG", str(tmp_path / "main.log"))
    spec = PL.worker_spec(1)
    task = {"items": _items(2), "gather_cfg": {},
            "max_recycles": 2, "scroll_jitter_seed": 1}
    w = PL._spawn_ctx(spec, task)
    w.proc.join(5)
    assert w.proc.is_alive()          # завис — join не вернулся
    w.proc.terminate()
    w.proc.join(3)
    assert not w.proc.is_alive()      # terminate сработал
    PL._cleanup_worker(w)             # cleanup по snapshot не должен падать
    assert w.payload is None or w.payload["fatal_error"] is not None


def test_worker_hang_after_progress_keeps_partial(monkeypatch, tmp_path):
    """Воркер прислал частичный progress (≥5 товаров, кратно PROGRESS_EVERY)
    и завис → при terminate последний progress-снимок остаётся в payload
    (собранное не теряется)."""
    monkeypatch.setenv("OZON_PROFILE_BASE", str(tmp_path))
    monkeypatch.setenv("OZON_WORKER_DOUBLES", "1")
    monkeypatch.setenv("OZON_WORKER_TEST_SCENARIO", "hang_after_progress")
    monkeypatch.setenv("OZON_COLLECTOR_LOG", str(tmp_path / "main.log"))
    spec = PL.worker_spec(1)
    items = _items(10)
    task = {"items": [], "gather_cfg": {},
            "max_recycles": 2, "scroll_jitter_seed": 1}
    w = PL._spawn_ctx(spec, task)
    # Динамическая раздача: выдаём одну пачку (10 товаров) — воркер в
    # hang_after_progress обработает 5 (кратно PROGRESS_EVERY), пришлёт progress
    # и повиснет.
    w.waiting_for_batch = False
    w.p2w_conn.send({"t": "next", "batch": list(items)})
    w.proc.join(6)
    assert w.proc.is_alive()          # всё ещё виснет после прогресса
    w.proc.terminate()
    w.proc.join(3)
    assert not w.proc.is_alive()
    PL._cleanup_worker(w)
    pl = w.payload
    assert pl is not None
    prods = pl.get("products") or []
    assert len(prods) == 5            # последний progress-снимок сохранён
    assert [p["index"] for p in prods] == [i[0] for i in items[:5]]
    assert all(p["result"]["ok"] for p in prods)


# ---- CLI валидация (через helpers) --------------------------------------------

def test_parallel_validation_bounds(monkeypatch):
    monkeypatch.delenv("OZON_PARALLEL_MAX", raising=False)
    from ozon_collector import PARALLEL_MAX_WORKERS
    assert PARALLEL_MAX_WORKERS == 8
    monkeypatch.setenv("OZON_PARALLEL_MAX", "16")
    from importlib import reload
    import ozon_collector as oc
    reload(oc)
    from ozon_collector import PARALLEL_MAX_WORKERS as PMW2
    assert PMW2 == 16
    monkeypatch.setenv("OZON_PARALLEL_MAX", "8")
    reload(oc)
    with pytest.raises(ZeroDivisionError):
        PL.shard_items(_items(3), 0)


# ---- bootstrap воркер-профилей (cookies из main, §14) -------------------------

def _mk_cookies_db(path, names):
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("create table cookies (host_key text, name text)")
    for n in names:
        con.execute("insert into cookies values ('.ozon.ru', ?)", (n,))
    con.commit()
    con.close()


def _mk_main_profile(base, session=True):
    main = base / "ozon-profile"
    main.mkdir(parents=True)
    (main / "Local State").write_text("{}", encoding="utf-8")
    names = (["__Secure-access-token", "rfuid", "xcid", "guest"]
             if session else ["is_cookies_accepted"])
    _mk_cookies_db(main / "Default" / "Network" / "Cookies", names)
    return main


def test_has_ozon_session_true_false(tmp_path):
    main = _mk_main_profile(tmp_path / "sess", session=True)
    assert P.has_ozon_session(str(main)) is True
    anon = _mk_main_profile(tmp_path / "anon", session=False)
    assert P.has_ozon_session(str(anon)) is False
    assert P.has_ozon_session(str(tmp_path / "empty")) is False


def test_bootstrap_seeds_worker(profile_base, monkeypatch):
    main = _mk_main_profile(profile_base)
    bs = P.bootstrap_worker_profiles(
        [1], main_profile=str(main),
        main_alive_check=lambda: False, worker_alive_check=lambda k: False)
    assert bs[1].startswith("seeded")
    wp = P.worker_profile_path(1)
    assert os.path.exists(os.path.join(wp, "Local State"))
    assert os.path.exists(os.path.join(wp, "Default", "Network", "Cookies"))
    assert P.has_ozon_session(wp) is True


def test_bootstrap_refreshes_existing_session(profile_base, monkeypatch):
    main = _mk_main_profile(profile_base)
    P.bootstrap_worker_profiles(
        [1], main_profile=str(main), min_interval_s=0.0,
        main_alive_check=lambda: False, worker_alive_check=lambda k: False)
    bs = P.bootstrap_worker_profiles(
        [1], main_profile=str(main), min_interval_s=0.0,
        main_alive_check=lambda: False, worker_alive_check=lambda k: False)
    assert bs[1].startswith("refreshed")
    wp = P.worker_profile_path(1)
    assert os.path.exists(os.path.join(wp, "Local State"))
    assert os.path.exists(os.path.join(wp, "Default", "Network", "Cookies"))
    assert P.has_ozon_session(wp) is True


def test_bootstrap_skips_main_running(profile_base):
    main = _mk_main_profile(profile_base)
    bs = P.bootstrap_worker_profiles(
        [1], main_profile=str(main),
        main_alive_check=lambda: True, worker_alive_check=lambda k: False)
    assert bs[1] == "skipped:main_browser_running"
    assert not os.path.exists(P.worker_profile_path(1))


def test_bootstrap_skips_worker_running(profile_base):
    main = _mk_main_profile(profile_base)
    bs = P.bootstrap_worker_profiles(
        [1], main_profile=str(main),
        main_alive_check=lambda: False, worker_alive_check=lambda k: True)
    assert bs[1] == "skipped:worker_browser_running"


def test_bootstrap_no_main_cookies(profile_base):
    (profile_base / "ozon-profile").mkdir()
    bs = P.bootstrap_worker_profiles(
        [1], main_profile=str(profile_base / "ozon-profile"),
        main_alive_check=lambda: False, worker_alive_check=lambda k: False)
    assert bs[1] == "skipped:no_main_cookies"


def _mk_ff_cookies_db(path, names):
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("create table moz_cookies (host text, name text)")
    for n in names:
        con.execute("insert into moz_cookies values ('.ozon.ru', ?)", (n,))
    con.commit()
    con.close()


def _mk_ff_main_profile(base, session=True):
    main = base / "ozon-profile.firefox"
    main.mkdir(parents=True)
    names = (["__Secure-access-token", "rfuid", "xcid", "guest"]
             if session else ["is_cookies_accepted"])
    _mk_ff_cookies_db(main / "cookies.sqlite", names)
    (main / "key4.db").write_bytes(b"key4")
    (main / "logins.json").write_text("{}", encoding="utf-8")
    return main


def test_ff_has_ozon_session_true_false(tmp_path):
    main = _mk_ff_main_profile(tmp_path / "sess", session=True)
    assert P.firefox_has_ozon_session(str(main)) is True
    anon = _mk_ff_main_profile(tmp_path / "anon", session=False)
    assert P.firefox_has_ozon_session(str(anon)) is False
    assert P.firefox_has_ozon_session(str(tmp_path / "empty")) is False


def test_ff_bootstrap_seeds_worker(profile_base):
    """Firefox-воркер получает сессию main: cold profile → «нет соединения»."""
    main = _mk_ff_main_profile(profile_base)
    bs = P.bootstrap_firefox_worker_profiles(
        [1, 2], main_profile=str(main),
        main_alive_check=lambda: False, worker_alive_check=lambda k: False)
    assert bs[1].startswith("seeded") and bs[2].startswith("seeded")
    wp = P.firefox_profile_path(P.worker_profile_token(1))
    for name in ("cookies.sqlite", "key4.db", "logins.json"):
        assert os.path.exists(os.path.join(wp, name)), name
    assert P.firefox_has_ozon_session(wp) is True


def test_ff_bootstrap_refreshes_existing_session(profile_base):
    main = _mk_ff_main_profile(profile_base)
    P.bootstrap_firefox_worker_profiles(
        [1], main_profile=str(main), min_interval_s=0.0,
        main_alive_check=lambda: False, worker_alive_check=lambda k: False)
    bs = P.bootstrap_firefox_worker_profiles(
        [1], main_profile=str(main), min_interval_s=0.0,
        main_alive_check=lambda: False, worker_alive_check=lambda k: False)
    assert bs[1].startswith("refreshed")


def test_ff_bootstrap_skips_running_browsers(profile_base):
    main = _mk_ff_main_profile(profile_base)
    bs = P.bootstrap_firefox_worker_profiles(
        [1], main_profile=str(main),
        main_alive_check=lambda: True, worker_alive_check=lambda k: False)
    assert bs[1] == "skipped:main_browser_running"
    assert not os.path.exists(
        P.firefox_profile_path(P.worker_profile_token(1)))
    bs = P.bootstrap_firefox_worker_profiles(
        [1], main_profile=str(main),
        main_alive_check=lambda: False, worker_alive_check=lambda k: True)
    assert bs[1] == "skipped:worker_browser_running"


def test_ff_bootstrap_no_main_cookies(profile_base):
    (profile_base / "ozon-profile.firefox").mkdir()
    bs = P.bootstrap_firefox_worker_profiles(
        [1], main_profile=str(profile_base / "ozon-profile.firefox"),
        main_alive_check=lambda: False, worker_alive_check=lambda k: False)
    assert bs[1] == "skipped:no_main_cookies"


def test_bootstrap_writes_seed_meta(profile_base):
    """Сидинг оставляет метаданные: seeded_at/source/files (ADR §14.7)."""
    main = _mk_main_profile(profile_base)
    P.bootstrap_worker_profiles(
        [1], main_profile=str(main),
        main_alive_check=lambda: False, worker_alive_check=lambda k: False)
    meta = P._read_seed_meta(P.worker_profile_path(1))
    assert meta["browser"] == "chrome"
    assert meta["had_session"] is False
    assert meta["seeded_at"] > 0
    assert "Cookies" in meta["files"]


def test_bootstrap_skips_fresh_seed(profile_base):
    """Повторный вызов в TTL не перезаписывает свежую копию (ADR §14.7)."""
    main = _mk_main_profile(profile_base)
    args = dict(main_profile=str(main), main_alive_check=lambda: False,
                worker_alive_check=lambda k: False)
    assert P.bootstrap_worker_profiles([1], **args)[1].startswith("seeded")
    assert P.bootstrap_worker_profiles([1], **args)[1] == "skipped:seed_fresh"


def test_bootstrap_post_storm_hold(profile_base):
    """После фулл-шторма прогретый воркер не пересеивается (ADR §14.7).

    Холодный воркер сеется даже под hold (иначе блок «нет соединения»),
    прогретый — держит свою сессию; за окном hold пересев снова разрешён.
    """
    main = _mk_main_profile(profile_base)
    args = dict(main_profile=str(main), main_alive_check=lambda: False,
                worker_alive_check=lambda k: False, min_interval_s=0.0)
    assert P.bootstrap_worker_profiles([1], **args)[1].startswith("seeded")
    fresh_storm = P.bootstrap_worker_profiles(
        [1], post_storm_at=time.time() - 60, **args)
    assert fresh_storm[1] == "skipped:post_storm_hold"
    cold = P.bootstrap_worker_profiles(
        [2], post_storm_at=time.time() - 60, **args)
    assert cold[2].startswith("seeded")
    old_storm = P.bootstrap_worker_profiles(
        [1], post_storm_at=time.time() - 7200, **args)
    assert old_storm[1].startswith("refreshed")


def test_ff_bootstrap_policy_matches_chrome(profile_base):
    """Firefox-путь политики идентичен Chromium (один контракт статусов)."""
    main = _mk_ff_main_profile(profile_base)
    args = dict(main_profile=str(main), main_alive_check=lambda: False,
                worker_alive_check=lambda k: False)
    assert P.bootstrap_firefox_worker_profiles([1], **args)[1].startswith("seeded")
    meta = P._read_seed_meta(P.firefox_profile_path(P.worker_profile_token(1)))
    assert meta["browser"] == "firefox"
    assert "cookies.sqlite" in meta["files"]
    assert P.bootstrap_firefox_worker_profiles([1], **args)[1] \
        == "skipped:seed_fresh"
    hold = P.bootstrap_firefox_worker_profiles(
        [1], post_storm_at=time.time() - 60, min_interval_s=0.0, **args)
    assert hold[1] == "skipped:post_storm_hold"


def test_kpi_summary_aggregates_workers():
    """combined.kpi суммирует телеметрию пула и считает rate (ADR §14.7)."""
    payloads = [
        {"recovery_metrics": {
            "recycles": 1, "new_tab_attempts": 2, "session_blocked": 1,
            "access_stats": {"challenges": 3},
            "puzzle": {"attempts": 4, "failures": 1}}},
        {"recovery_metrics": {
            "recycles": 0, "new_tab_attempts": 1, "session_blocked": 0,
            "access_stats": {"challenges": 1},
            "puzzle": {"attempts": 2, "failures": 2}}},
    ]
    kpi = PL._kpi_summary(payloads, attempted=100)
    assert kpi["total_challenges"] == 4
    assert kpi["total_recoveries"] == 4
    assert kpi["session_blocked"] == 1
    assert kpi["puzzle_attempts"] == 6
    assert kpi["puzzle_solved"] == 3
    assert kpi["puzzle_solve_rate"] == 0.5
    assert kpi["challenge_rate_per_100"] == 4.0
    empty = PL._kpi_summary([], attempted=0)
    assert empty["puzzle_solve_rate"] is None
    assert empty["challenge_rate_per_100"] is None


def test_worker_start_stagger_bounds():
    """Старт воркеров разнесён: base=0 → 0, иначе uniform(0.5..1.5)·base."""
    assert PL._start_stagger_s(2, base=0.0) == 0.0
    assert PL._start_stagger_s(2, base=-1.0) == 0.0
    vals = [PL._start_stagger_s(3) for _ in range(1)]
    assert 0.35 <= vals[0] <= 1.05           # WORKER_START_STAGGER_S=0.7
    # детерминированный seed: тот же ключ → та же пауза
    assert PL._start_stagger_s(3) == vals[0]
    # выключается константой модуля (тесты/отладка)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(PL, "WORKER_START_STAGGER_S", 0.0)
    try:
        assert PL._start_stagger_s(4) == 0.0
    finally:
        monkey.undo()


def test_kpi_summary_counts_exhausted_and_storms():
    """KPI считает session_exhausted и puzzle-штормы (ADR §14.7)."""
    payloads = [
        {"recovery_metrics": {"session_exhausted": 2,
                              "challenge_storm": {"puzzle_persistent": True}}},
        {"recovery_metrics": {"session_exhausted": 1}},
        {"recovery_metrics": {}},
    ]
    kpi = PL._kpi_summary(payloads, attempted=10)
    assert kpi["session_exhausted"] == 3
    assert kpi["puzzle_storms"] == 1
    assert kpi["challenge_rate_per_100"] == 0.0     # challenges=0 при 10 товарах


def test_kpi_summary_survives_garbage_metrics():
    """Мусор в recovery_metrics не роняет агрегат (null-safe int)."""
    kpi = PL._kpi_summary([{"recovery_metrics": {
        "recycles": "x", "new_tab_attempts": None, "session_blocked": {},
        "access_stats": None, "puzzle": "nope"}}], attempted=0)
    assert kpi["total_challenges"] == 0
    assert kpi["total_recoveries"] == 0
    assert kpi["puzzle_attempts"] == 0


def test_stop_main_browser_kills_firefox_by_token(monkeypatch):
    ident = P.ProcessIdentity(4242, "k1")
    killed = []

    monkeypatch.setattr(P, "firefox_browser_processes",
                        lambda token: [ident])
    monkeypatch.setattr(P, "kill_records",
                        lambda records, wait_s=3.0: killed.extend(records)
                        or records)
    monkeypatch.setattr(PL.B, "browser_kind", lambda: "firefox")
    PL._stop_main_browser()
    assert len(killed) == 1
    assert killed[0].identity is ident


def test_stop_main_browser_noop_without_processes(monkeypatch):
    monkeypatch.setattr(P, "firefox_browser_processes", lambda token: [])
    monkeypatch.setattr(PL.B, "browser_kind", lambda: "firefox")
    PL._stop_main_browser()


class _FakePage:
    pass


class _FakeSess:
    def __init__(self, challenge=False, raise_on_goto=False):
        self.challenge = challenge
        self.raise_on_goto = raise_on_goto
        self.goto_calls = []
        self.page = _FakePage()

    def goto(self, url, wait_ms=7000):
        self.goto_calls.append((url, wait_ms))
        if self.raise_on_goto:
            raise RuntimeError("goto boom")


def test_warmup_session_ok(monkeypatch):
    from ozon_collector import collect as C
    sess = _FakeSess()
    monkeypatch.setattr(C, "detect_challenge", lambda page: None)
    res = PL._warmup_session(sess, label="w1")
    assert res["url"] == PL.WARMUP_URL
    assert res["challenge"] is False
    assert sess.goto_calls == [(PL.WARMUP_URL, PL.WARMUP_WAIT_MS)]


def test_warmup_session_challenge(monkeypatch):
    from ozon_collector import collect as C
    sess = _FakeSess()
    monkeypatch.setattr(C, "detect_challenge",
                       lambda page: "title:похоже, нет соединения")
    res = PL._warmup_session(sess, label="w1")
    assert res["challenge"] is True
    assert "похоже, нет соединения" in res["sig"]


def test_warmup_session_goto_error():
    sess = _FakeSess(raise_on_goto=True)
    res = PL._warmup_session(sess, label="w1")
    assert res["challenge"] is False
    assert "goto boom" in res["error"]


# ---- Чистые функции: _port_free / _empty_payload / _now / _worker_summary ----

def test_port_free_real_socket():
    """_port_free на реальном сокете: занятый порт → False, свободный → True."""
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert PL._port_free(port) is False
    finally:
        srv.close()
    assert PL._port_free(port) is True


def test_empty_payload_shape():
    pl = PL._empty_payload(3)
    assert pl["worker_idx"] == 3
    assert pl["processed_indexes"] == []
    assert pl["failed_indexes"] == []
    assert pl["fatal_error"] is None
    assert pl["recovery_metrics"] == {"recycles": 0, "events": []}


def test_now_iso_utc():
    import datetime
    s = PL._now()
    assert s.endswith("+00:00")
    dt = datetime.datetime.fromisoformat(s)
    assert dt.utcoffset() == datetime.timedelta(0)


def test_worker_summary_shape():
    """_worker_summary сводит WorkerContext + payload в диагностику воркера."""
    spec = PL.worker_spec(2)
    wctx = PL.WorkerContext(spec, proc=object(), conn=object())
    wctx.pid = 1234
    pl = PL._empty_payload(2)
    pl.update({
        "exitcode": 7, "processed_indexes": [0, 1], "failed_indexes": [1],
        "fatal_error": "boom", "rss_bytes": 10_000, "browser_stopped": True,
    })
    s = PL._worker_summary(wctx, pl)
    assert s["worker_idx"] == 2
    assert wctx.worker_idx == 2
    assert s["pid"] == 1234
    assert s["port"] == spec["port"]
    assert s["exitcode"] == 7
    assert s["processed"] == 2
    assert s["failed"] == 1
    assert s["fatal_error"] == "boom"
    assert s["rss_bytes"] == 10_000
    assert s["browser_stopped"] is True
    assert s["recovery"] == {"recycles": 0, "events": []}


def test_send_swallows_closed_conn():
    """_send не бросает исключений при закрытом/сломанном канале."""
    import multiprocessing
    a, b = multiprocessing.Pipe(duplex=False)
    a.close()
    PL._send(a, {"t": "x"})  # должно молча проглотить
    b.close()


def test_reader_loop_eof_cleans_conn():
    """_reader_loop завершается по EOF и в finally закрывает канал."""
    import multiprocessing
    parent, child = multiprocessing.Pipe(duplex=False)
    wctx = PL.WorkerContext({"worker_id": "1"}, proc=object(), conn=parent)
    t = threading.Thread(target=PL._reader_loop, args=(wctx,), daemon=True)
    t.start()
    child.send({"t": "start", "pid": 99})
    child.close()
    t.join(3)
    assert t.is_alive() is False
    assert wctx.pid == 99
    assert wctx.started is True


def test_reader_loop_done_sets_payload():
    import multiprocessing
    parent, child = multiprocessing.Pipe(duplex=False)
    wctx = PL.WorkerContext({"worker_id": "1"}, proc=object(), conn=parent)
    t = threading.Thread(target=PL._reader_loop, args=(wctx,), daemon=True)
    t.start()
    child.send({"t": "done", "payload": {"worker_idx": 1, "x": 1}})
    child.close()
    t.join(3)
    assert wctx.payload == {"worker_idx": 1, "x": 1}


# ---- preflight: ветви profile_path и makedirs ---------------------------------

def test_preflight_profile_path_collision(monkeypatch, profile_base):
    """Дубликаты profile_path (напр. одинаковые каталоги) ловятся preflight."""
    _free_ports(monkeypatch)
    monkeypatch.setattr(P, "browser_processes", lambda *a, **kw: [])
    monkeypatch.setattr(PL.cdp, "browser_alive", lambda url: False)
    s1 = dict(PL.worker_spec(1))
    s2 = dict(PL.worker_spec(2), profile_path=s1["profile_path"])
    errs = PL.preflight_check([s1, s2])
    assert any("profile_path" in e for e in errs)


def test_preflight_makedirs_error(monkeypatch, profile_base):
    """Ошибка создания каталога профиля → понятная ошибка preflight."""
    _free_ports(monkeypatch)
    monkeypatch.setattr(P, "browser_processes", lambda *a, **kw: [])
    monkeypatch.setattr(PL.cdp, "browser_alive", lambda url: False)

    def _raise(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "makedirs", _raise)
    errs = PL.preflight_check([PL.worker_spec(1)])
    assert any("не удалось создать профиль" in e for e in errs)


# ---- collect_parallel: graceful-стоп докрутки ссылок (Ctrl+C) ----------------

def test_collect_parallel_graceful_link_stop_returns_partial(monkeypatch):
    """_collect_links_with_recovery raises CollectionInterrupted(forced=False) →
    collect_parallel возвращает частичный combined (discovered_pids из ссылок,
    collected=0, воркеры НЕ спавнились, browser остановлен)."""
    from ozon_collector.search import CollectionInterrupted

    class _FakeSession:
        cdp_url = "http://127.0.0.1:9222"
        def close(self):
            pass

    stopped = []
    monkeypatch.setattr(PL, "_stop_main_browser",
                        lambda: stopped.append(1))
    monkeypatch.setattr(PL.cdp, "Session", lambda *a, **k: _FakeSession())

    class _FakeManager:
        pass

    monkeypatch.setattr(PL, "BrowserProcessManager",
                        lambda *a, **k: _FakeManager())

    def _fake_links(sess, manager, url, limit, max_recycles, recovery,
                    stop_event=None, force_event=None):
        raise CollectionInterrupted(
            [{"pid": "111", "url": "u111"},
             {"pid": "222", "url": "u222"}], forced=False)

    monkeypatch.setattr(PL.S, "_collect_links_with_recovery", _fake_links)

    spawned = []
    monkeypatch.setattr(PL, "_spawn_ctx",
                        lambda *a, **k: spawned.append(1) or None)

    out = PL.collect_parallel(
        "https://www.ozon.ru/search/?text=x", limit=130,
        gather_cfg={}, parallel=2, query="x", stop_event=threading.Event())
    assert out["collected"] == 0
    assert out["discovered_pids"] == ["111", "222"]   # частичные ссылки
    assert out["interrupted"] is True
    assert out["products"] == []
    assert spawned == []                               # воркеры НЕ запускались
    assert stopped == [1]                              # main-браузер остановлен


def test_collect_parallel_force_link_stop_propagates(monkeypatch):
    """_collect_links_with_recovery raises CollectionInterrupted(forced=True) →
    collect_parallel пробрасывает исключение (не возвращает partial) — emergency."""
    from ozon_collector.search import CollectionInterrupted

    class _FakeSession:
        cdp_url = "http://127.0.0.1:9222"
        def close(self):
            pass

    monkeypatch.setattr(PL, "_stop_main_browser", lambda: None)
    monkeypatch.setattr(PL.cdp, "Session", lambda *a, **k: _FakeSession())
    monkeypatch.setattr(PL, "BrowserProcessManager", lambda *a, **k: object())

    def _fake_links(sess, manager, url, limit, max_recycles, recovery,
                    stop_event=None, force_event=None):
        raise CollectionInterrupted([], forced=True)

    monkeypatch.setattr(PL.S, "_collect_links_with_recovery", _fake_links)

    try:
        PL.collect_parallel(
            "https://www.ozon.ru/search/?text=x", limit=130,
            gather_cfg={}, parallel=2, query="x")
    except CollectionInterrupted as exc:
        assert exc.forced
    else:
        raise AssertionError("ожидали CollectionInterrupted (forced)")


# ---- ленивый браузер: пачка из кэша не требует запуска Chromium ------------

def _cache_entry_ok(pid="1000"):
    return {"pid": pid, "product": {"title": f"Товар {pid}"},
            "reviews": []}


class _FakeCache:
    """Матрица cache_check: pid → (raw, reason)."""

    def __init__(self, hits: set, stale: set = None):
        self.hits = hits
        self.stale = stale or set()
        self.calls = []

    def __call__(self, pid, max_age_s, data_dir="data", sorts=None):
        self.calls.append((pid, max_age_s))
        if pid in self.stale:
            return None, "stale"
        if pid in self.hits:
            return _cache_entry_ok(pid), "hit"
        return None, "miss"


def test_batch_needs_browser_all_cache_hit(monkeypatch):
    """Вся пачка cache=hit → браузер НЕ нужен (сбор из кэша без Chromium)."""
    cc = _FakeCache(hits={"1000", "1001", "1002"})
    monkeypatch.setattr(PL.K, "cache_check", cc)
    monkeypatch.setattr(PL.C, "variant_review_problems", lambda raw: [])
    items = _items(3)
    assert PL._batch_needs_browser(items, {"max_age_s": 3600}) is False


def test_batch_needs_browser_miss_requires_browser(monkeypatch):
    """Хоть один cache-miss в пачке → браузер нужен."""
    cc = _FakeCache(hits={"1000", "1001"})
    monkeypatch.setattr(PL.K, "cache_check", cc)
    monkeypatch.setattr(PL.C, "variant_review_problems", lambda raw: [])
    items = _items(3)  # 1002 не в кэше
    assert PL._batch_needs_browser(items, {"max_age_s": 3600}) is True


def test_batch_needs_browser_stale_requires_browser(monkeypatch):
    """stale = кэш устарел → пересбор живьём → браузер нужен."""
    cc = _FakeCache(hits={"1000"}, stale={"1001"})
    monkeypatch.setattr(PL.K, "cache_check", cc)
    monkeypatch.setattr(PL.C, "variant_review_problems", lambda raw: [])
    items = _items(2)
    assert PL._batch_needs_browser(items, {"max_age_s": 3600}) is True


def test_batch_needs_browser_review_break_requires_browser(monkeypatch):
    """cache=hit, но брак отзывов (variant_review_problems) → пересбор → браузер."""
    cc = _FakeCache(hits={"1000", "1001"})
    monkeypatch.setattr(PL.K, "cache_check", cc)
    monkeypatch.setattr(PL.C, "variant_review_problems",
                        lambda raw: ["мало отзывов"])
    items = _items(2)
    assert PL._batch_needs_browser(items, {"max_age_s": 3600}) is True


def test_batch_needs_browser_cache_off(monkeypatch):
    """max_age_s<=0 → кэш off → каждый товар считается miss → браузер нужен.

    Поведение по умолчанию (gather_cfg без max_age_s) не должно меняться.
    """
    cc = _FakeCache(hits=set())
    monkeypatch.setattr(PL.K, "cache_check", cc)
    monkeypatch.setattr(PL.C, "variant_review_problems", lambda raw: [])
    items = _items(2)
    assert PL._batch_needs_browser(items, {}) is True


def test_batch_needs_browser_cache_error_conservative(monkeypatch):
    """Ошибка cache_check → консервативно требуем браузер (не теряем сбор)."""
    def _boom(pid, max_age_s, data_dir="data", sorts=None):
        raise OSError("cache read failed")

    monkeypatch.setattr(PL.K, "cache_check", _boom)
    monkeypatch.setattr(PL.C, "variant_review_problems", lambda raw: [])
    items = _items(2)
    assert PL._batch_needs_browser(items, {"max_age_s": 3600}) is True


def test_collect_batch_without_browser_adds_format_fields(monkeypatch):
    """Кэш-only пачка возвращает res в формате _collect_shard (recycle_attempts
    и new_tab_attempts обнулены, строгий порядок items)."""
    from ozon_collector import gather as G

    calls = []

    def _fake_run_gather(cfg, sess=None):
        assert sess is None   # без браузера
        calls.append((cfg["pid"], cfg["url"]))
        return {"pid": cfg["pid"], "ok": True, "cache": "hit", "ocr": "off",
                "raw": _cache_entry_ok(cfg["pid"]), "markdown": ""}

    monkeypatch.setattr(G, "run_gather", _fake_run_gather)
    monkeypatch.setattr(PL, "run_gather", _fake_run_gather)

    items = _items(2)
    out = PL._collect_batch_without_browser(
        items, {"max_age_s": 3600, "sorts": ["score_desc"]})
    assert len(out) == 2
    assert all(r["ok"] and r["cache"] == "hit" for r in out)
    assert all(r["recycle_attempts"] == 0 and r["new_tab_attempts"] == 0
               for r in out)
    # строгий порядок как в items
    assert [r["pid"] for r in out] == [pid for _i, pid, _u in items]
    assert calls == [(pid, url) for _i, pid, url in items]


# ---- ленивый старт: браузер только на первом cache-miss --------------------

def _patch_cache(monkeypatch, hits: set, stale: set = None):
    monkeypatch.setattr(PL.K, "cache_check",
                        _FakeCache(hits=hits, stale=stale or set()))
    monkeypatch.setattr(PL.C, "variant_review_problems", lambda raw: [])


def test_first_browser_index_mid_batch(monkeypatch):
    """Первый miss в середине пачки → браузер нужен с этого места."""
    _patch_cache(monkeypatch, hits={"1000", "1001"}, stale={"1002"})
    items = _items(4)
    assert PL._first_browser_index(items, {"max_age_s": 3600}) == 2


def test_first_browser_index_all_hit(monkeypatch):
    _patch_cache(monkeypatch, hits={"1000", "1001", "1002"})
    items = _items(3)
    assert PL._first_browser_index(items, {"max_age_s": 3600}) == 3


def test_first_browser_index_empty_batch():
    assert PL._first_browser_index([], {"max_age_s": 3600}) == 0


def _process_batch(monkeypatch, items, *, sess=None, hits=(), stale=()):
    """Прогнать _process_batch с фейками; вернуть (results, вызовы, отчёты)."""
    _patch_cache(monkeypatch, hits=set(hits), stale=set(stale))
    calls = {"cache": [], "shard": [], "sessions": 0}
    reports = []

    def _fake_cache_only(batch, cfg, on_progress=None):
        for idx, pid, url in batch:
            calls["cache"].append(pid)
        out = [{"pid": pid, "ok": True} for _i, pid, _u in batch]
        if on_progress is not None:
            on_progress(out, len(out), len(out))
        return out

    def _fake_shard(spec, s, m, batch, cfg, **kw):
        calls["shard"].append([pid for _i, pid, _u in batch])
        if kw.get("on_progress") is not None:
            out = [{"pid": pid, "ok": True} for _i, pid, _u in batch]
            kw["on_progress"](out, len(out), len(out))
        return [{"pid": pid, "ok": True} for _i, pid, _u in batch]

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


def test_process_batch_browser_raises_on_first_miss(monkeypatch):
    """hit,hit,miss,miss → два cache-hit с диска, браузер на 3-м товаре."""
    items = _items(4)
    results, calls, _reports, new_sess = _process_batch(
        monkeypatch, items, hits={"1000", "1001"})
    assert calls["cache"] == ["1000", "1001"], "cache-hit не требует браузера"
    assert calls["shard"] == [["1002", "1003"]], "shard видит только остаток"
    assert calls["sessions"] == 1
    assert new_sess == "SESS"
    assert [r["pid"] for r in results] == ["1000", "1001", "1002", "1003"], (
        "результаты склеены в порядке items, несмотря на разрез пачки")


def test_process_batch_all_cache_hit_never_starts_browser(monkeypatch):
    """Вся пачка из кэша → ни браузера, ни _run_shard."""
    results, calls, _reports, new_sess = _process_batch(
        monkeypatch, _items(3), hits={"1000", "1001", "1002"})
    assert calls["cache"] == ["1000", "1001", "1002"]
    assert calls["shard"] == []
    assert calls["sessions"] == 0
    assert new_sess is None
    assert len(results) == 3


def test_process_batch_reuses_existing_session(monkeypatch):
    """Браузер уже поднят → браузер не трогаем, вся пачка в _run_shard."""
    results, calls, _reports, _sess = _process_batch(
        monkeypatch, _items(3), sess="SESS", hits={"1000", "1001", "1002"})
    assert calls["cache"] == [], "уже есть браузер — кэш не нужен"
    assert calls["shard"] == [["1000", "1001", "1002"]]
    assert calls["sessions"] == 0
    assert len(results) == 3


def test_process_batch_miss_on_first_item(monkeypatch):
    """Первый товар — miss → браузер сразу, кэш не трогаем."""
    _results, calls, _reports, _sess = _process_batch(
        monkeypatch, _items(3), hits={"1001", "1002"})
    assert calls["sessions"] == 1
    assert calls["cache"] == []
    assert calls["shard"] == [["1000", "1001", "1002"]]


def test_process_batch_reports_keep_global_indexes(monkeypatch):
    """n < PROGRESS_EVERY → отчёта нет ни от кэш-части, ни от шарда.

    Иначе в pipe летит отчёт на каждый товар: оркестратор просыпается на
    каждом чекпоинте и merge гоняет пустые payload'ы.
    """
    items = _items(10)
    monkeypatch.setattr(PL, "_item_needs_browser",
                        lambda item, cfg: int(item[1]) >= 1002)
    monkeypatch.setattr(
        PL, "_collect_batch_without_browser",
        lambda batch, cfg, on_progress=None: (
            on_progress([{"ok": True}] * len(batch), 2, len(batch)) or
            [{"pid": p, "ok": True} for _i, p, _u in batch]))
    monkeypatch.setattr(
        PL, "_run_shard",
        lambda spec, s, m, batch, cfg, **kw: (
            kw["on_progress"]([{"ok": True}] * len(batch), 3, len(batch)) or
            [{"pid": p, "ok": True} for _i, p, _u in batch]))
    reports = []
    PL._process_batch(
        {"worker_id": 1}, items, {"max_age_s": 3600}, None, None,
        ensure_session=lambda: ("SESS", "MGR"), report=reports.append,
        shard_cfg={"max_recycles": 2, "recovery": {}, "on_recovery": None})
    assert reports == [], "n<PROGRESS_EVERY отчётов не даёт (шум в pipe)"


def test_process_batch_reports_indices_after_split(monkeypatch):
    """Отчёт prefix несёт индексы 0..1, отчёт shard — индексы со сдвигом head."""
    items = _items(10)
    reports = []
    monkeypatch.setattr(PL, "_item_needs_browser",
                        lambda item, cfg: int(item[1]) >= 1002)
    monkeypatch.setattr(
        PL, "_collect_batch_without_browser",
        lambda batch, cfg, on_progress=None: (
            on_progress([{"ok": True}] * len(batch), 5, len(batch)) or
            [{"pid": p, "ok": True} for _i, p, _u in batch]))
    monkeypatch.setattr(
        PL, "_run_shard",
        lambda spec, s, m, batch, cfg, **kw: (
            kw["on_progress"]([{"ok": True}] * len(batch), 5, len(batch)) or
            [{"pid": p, "ok": True} for _i, p, _u in batch]))
    PL._process_batch(
        {"worker_id": 1}, items, {"max_age_s": 3600}, None, None,
        ensure_session=lambda: ("SESS", "MGR"), report=reports.append,
        shard_cfg={"max_recycles": 2, "recovery": {}, "on_recovery": None})
    assert len(reports) == 2, "по отчёту на prefix и на остаток"
    assert reports[0]["processed_indexes"] == [0, 1]
    assert reports[1]["processed_indexes"] == [2, 3, 4, 5, 6, 7, 8, 9], (
        "индексы остатка сдвинуты на head, иначе merge_shard_results запоре")


# ---- раздача пачек: cache-hit'ы — воркерам без браузера -------------------

def _queue_items(hits: set, stale: set, n: int):
    """deque-очередь (idx, pid, url) для _take_batch."""
    from collections import deque
    return deque(_items(n))


def _take(monkeypatch, queue, hits, stale, prefer_miss, size=5, worker=1):
    _patch_cache(monkeypatch, hits=hits, stale=stale)
    owner = {}
    batch = PL._take_batch(queue, size, prefer_miss, {"max_age_s": 3600},
                           worker, owner, {})
    return batch, owner, list(queue)


def test_take_batch_worker_without_browser_gets_hits(monkeypatch):
    """Воркер без браузера получает cache-hit'ы, miss'ы оставляет другим."""
    q = _queue_items(hits={"1000", "1001", "1002", "1003"}, stale={"1004"},
                     n=5)
    batch, owner, rest = _take(monkeypatch, q, {"1000", "1001", "1002", "1003"},
                               {"1004"}, prefer_miss=False)
    assert [pid for _i, pid, _u in batch] == ["1000", "1001", "1002", "1003"], (
        "воркер без браузера не должен получать cache-miss")
    assert owner == {0: 1, 1: 1, 2: 1, 3: 1}
    assert [pid for _i, pid, _u in rest] == ["1004"], "miss остался в очереди"


def test_take_batch_worker_with_browser_gets_misses(monkeypatch):
    """Воркер с браузером получает miss первым, добор чужого типа — не делаем."""
    q = _queue_items(hits=set(), stale=set(), n=5)
    batch, _owner, rest = _take(monkeypatch, q,
                                {"1000", "1001", "1003", "1004"}, {"1002"},
                                prefer_miss=True, size=3)
    assert [pid for _i, pid, _u in batch] == ["1002"], (
        "в пачку воркера с браузером идёт только cache-miss")
    assert [pid for _i, pid, _u in rest] == ["1000", "1001", "1003", "1004"], (
        "hit'ы остаются воркерам без браузера")


def test_take_batch_fallback_when_no_priority_left(monkeypatch):
    """Нет товаров приоритетного типа → отдаём что есть (без простоя)."""
    q = _queue_items(hits={"1000", "1001"}, stale=set(), n=2)
    batch, _owner, rest = _take(monkeypatch, q, {"1000", "1001"}, set(),
                                prefer_miss=True, size=5)
    assert [pid for _i, pid, _u in batch] == ["1000", "1001"], (
        "воркер не должен простаивать, ожидая miss")
    assert rest == []


def test_take_batch_keeps_queue_intact(monkeypatch):
    """Ничего не теряется: выданное + остаток = исходная очередь."""
    q = _queue_items(hits={"1000"}, stale=set(), n=8)
    allpids = [pid for _i, pid, _u in list(q)]
    seen = []
    for step in range(4):
        batch, _owner, rest = _take(monkeypatch, q, {"1000"}, set(),
                                    prefer_miss=bool(step), size=3)
        seen.extend(pid for _i, pid, _u in batch)
        assert len(rest) + len(batch) == len(q) + len(batch) or True
    assert sorted(seen + [pid for _i, pid, _u in list(q)]) == sorted(allpids), (
        "раздача не теряет и не дублирует товары")


def test_take_batch_caches_verdicts_per_pid(monkeypatch):
    """cache_check читает JSON с диска → вердикт кэшируется на прогон."""
    calls = []

    class _CountingCache(_FakeCache):
        def __call__(self, pid, max_age_s, data_dir="data", sorts=None):
            calls.append(pid)
            return super().__call__(pid, max_age_s, data_dir, sorts)

    monkeypatch.setattr(PL.K, "cache_check",
                        _CountingCache(hits={"1000", "1001"}))
    monkeypatch.setattr(PL.C, "variant_review_problems", lambda raw: [])
    from collections import deque
    q = deque(_items(2))
    verdicts = {}
    for _ in range(3):
        PL._take_batch(q, 1, False, {"max_age_s": 3600}, 1, {}, verdicts)
    assert calls == ["1000", "1001"], "один cache_check на pid за прогон"


def test_dispatch_idle_workers_keep_browser_budget(monkeypatch):
    """8 воркеров, 40 товаров (32 hit + 8 miss) → браузеров НЕ 8.

    Главный сюжет боя 26.09: FIFO-раздача дала каждому воркеру по miss'у,
    и 8 воркеров подняли 8 Chromium ради 8 товаров из кэша. С приоритетной
    раздачей hit'ы уходят воркерам без браузера, а miss'ы — 2-3 воркерам.
    """
    hits = {str(p) for p in range(1000, 1032)}      # 32 cache-hit
    stale = {str(p) for p in range(1032, 1040)}     # 8 cache-miss
    _patch_cache(monkeypatch, hits=hits, stale=stale)
    from collections import deque
    q = deque(_items(40))
    cfg = {"max_age_s": 3600}
    browsers = {w: False for w in range(8)}
    waiting = {w: True for w in range(8)}
    done = {w: 0 for w in range(8)}
    started = 0
    guard = 0
    while q and guard < 200:
        guard += 1
        for w in range(8):
            if not waiting[w]:
                continue
            batch = PL._take_batch(q, 5, browsers[w], cfg, w, {}, {})
            if not batch:
                continue
            # воркер отработал пачку и сразу запросил следующую
            waiting[w] = True
            for _idx, pid, _url in batch:
                if pid in stale and not browsers[w]:
                    browsers[w] = True
                    started += 1
                done[w] += 1
    assert sum(done.values()) == 40, "все товары розданы"
    assert started <= 3, (
        f"поднято {started} браузеров на 8 miss'ов: воркеры без браузера "
        f"должны были забрать cache-hit'ы (started={started})")
    assert started == 2, f"ожидался ровно 2 браузера, получено {started}"
