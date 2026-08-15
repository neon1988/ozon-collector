"""Тесты Фазы B (--parallel): шардирование, merge, preflight, attach_file,
token-матчинг, worker_spec, worker-макет (spawn) и изоляция recovery."""
from __future__ import annotations

import os
import sys
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
                        lambda: [(111, argv)])
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


# ---- CLI валидация (через helpers) --------------------------------------------

def test_parallel_validation_bounds(monkeypatch):
    monkeypatch.delenv("OZON_PARALLEL_MAX", raising=False)
    from ozon_collector import PARALLEL_MAX_WORKERS
    assert PARALLEL_MAX_WORKERS == 2
    monkeypatch.setenv("OZON_PARALLEL_MAX", "8")
    from importlib import reload
    import ozon_collector as oc
    reload(oc)
    from ozon_collector import PARALLEL_MAX_WORKERS as PMW2
    assert PMW2 == 8
    monkeypatch.setenv("OZON_PARALLEL_MAX", "2")
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


def test_bootstrap_skips_if_session_present(profile_base, monkeypatch):
    main = _mk_main_profile(profile_base)
    P.bootstrap_worker_profiles(
        [1], main_profile=str(main),
        main_alive_check=lambda: False, worker_alive_check=lambda k: False)
    wp = P.worker_profile_path(1)
    before = os.path.getmtime(os.path.join(wp, "Default", "Network", "Cookies"))
    bs = P.bootstrap_worker_profiles(
        [1], main_profile=str(main),
        main_alive_check=lambda: False, worker_alive_check=lambda k: False)
    assert bs[1] == "skipped:already_has_session"
    assert os.path.getmtime(
        os.path.join(wp, "Default", "Network", "Cookies")) == before


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
