"""Тесты Фазы B (--parallel): шардирование, merge, preflight, attach_file,
token-матчинг, worker_spec, worker-макет (spawn) и изоляция recovery."""
from __future__ import annotations

import os
import sys
import threading
import time

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

def test_attach_file_appends_with_prefix(tmp_path, reset_log):
    f = tmp_path / "w.log"
    f.write_text("OLD LINE\n", encoding="utf-8")
    log.attach_file(str(f), mode="a", prefix="[worker=1 pid=1] ")
    log.info("hello worker1")
    log.attach_file(str(f), mode="a", prefix="[worker=2 pid=2] ")
    log.info("hello worker2")
    txt = f.read_text(encoding="utf-8")
    assert txt.startswith("OLD LINE\n")          # append, не перезапись
    assert "[worker=1 pid=1] hello worker1" in txt
    assert "[worker=2 pid=2] hello worker2" in txt
    assert txt.count("hello worker") == 2


def test_attach_file_no_rotation(tmp_path, reset_log):
    f = tmp_path / "w.log"
    f.write_text("X\n", encoding="utf-8")
    log.attach_file(str(f), mode="a", prefix="[worker=1 pid=1] ")
    assert not (tmp_path / "w.log.1").exists()   # ротации нет
    log.attach_file(str(tmp_path / "other.log"), mode="a")
    # w.log остался нетронутым (добавился только attach-заголовок)
    assert (tmp_path / "w.log").read_text(
        encoding="utf-8").startswith("X\n")
    assert (tmp_path / "other.log").exists()


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


# ---- worker-макет (spawn) -------------------------------------------------------

def _spawn_workers(specs, items, monkeypatch, tmp_path, scenario,
                   timeout=60):
    monkeypatch.setenv("OZON_PROFILE_BASE", str(tmp_path))
    monkeypatch.setenv("OZON_WORKER_DOUBLES", "1")
    monkeypatch.setenv("OZON_WORKER_TEST_SCENARIO", scenario)
    monkeypatch.setenv("OZON_COLLECTOR_LOG", str(tmp_path / "main.log"))
    shards = PL.shard_items(items, len(specs))
    wctxs = []
    for k, spec in enumerate(specs, 1):
        task = {"items": shards[k - 1], "gather_cfg": {},
                "max_recycles": 2, "scroll_jitter_seed": 1}
        wctxs.append(PL._spawn_ctx(spec, task))
    deadline = time.monotonic() + timeout
    for w in wctxs:
        remain = deadline - time.monotonic()
        if remain > 0:
            w.proc.join(remain)
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
    task = {"items": items, "gather_cfg": {},
            "max_recycles": 2, "scroll_jitter_seed": 1}
    w = PL._spawn_ctx(spec, task)
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
        [1], main_profile=str(main),
        main_alive_check=lambda: False, worker_alive_check=lambda k: False)
    bs = P.bootstrap_worker_profiles(
        [1], main_profile=str(main),
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
