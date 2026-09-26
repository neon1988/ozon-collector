"""Тесты отбора процессов для уборки (tests/_procguard.py).

Проверяется чистая логика на подставных снимках: реальные процессы при
этих тестах не создаются и не убиваются.
"""
from __future__ import annotations

import os

import pytest

import _procguard as pg


def _snap(*rows: tuple[int, int, str]) -> dict[int, pg.ProcInfo]:
    return {pid: pg.ProcInfo(pid=pid, ppid=ppid, name=name) for pid, ppid, name in rows}


# pytest(100) -> worker(200) -> node(300) -> chrome(400)
#                                     \-> python(500, сирота: ppid умер)
TREE = _snap(
    (100, 1, "python.exe"),
    (200, 100, "python.exe"),
    (300, 200, "node.exe"),
    (400, 300, "chrome-headless-shell.exe"),
    (500, 777, "node.exe"),
    (900, 1, "python.exe"),  # демон
    (901, 900, "chrome.exe"),  # браузер демона
)


def test_descendants_walks_whole_tree():
    assert pg.descendants(TREE, [100]) == {200, 300, 400}
    assert pg.descendants(TREE, [300]) == {400}
    assert pg.descendants(TREE, []) == set()


def test_descendants_ignores_unknown_root():
    assert pg.descendants(TREE, [12345]) == set()


def test_top_level_returns_only_roots_of_owned():
    owned = {200, 300, 400}
    assert pg.top_level(owned, TREE) == [200]


def test_plan_reap_keeps_only_live_descendants_of_pytest():
    plan = pg.plan_reap(TREE, 100)
    assert [i.pid for i in plan] == [200]
    assert plan[0].name == "python.exe"


def test_plan_reap_respects_protected():
    plan = pg.plan_reap(TREE, 100, protected={200})
    assert plan == []


def test_plan_reap_never_targets_daemon_subtree():
    """Демон запущен ДО прогона и не является потомком pytest — не трогаем."""
    plan = pg.plan_reap(TREE, 100)
    assert not {900, 901} & {i.pid for i in plan}


def test_is_orphan_detects_dead_parent():
    assert pg.is_orphan(TREE, TREE[500]) is True
    assert pg.is_orphan(TREE, TREE[300]) is False


@pytest.mark.parametrize("name,cmd,expected", [
    ("node.exe", r"…\ms-playwright\driver\node.exe …\cli.js run-driver", True),
    ("node.exe", r"…\playwright\driver\package\lib\cli.js", True),
    ("chrome-headless-shell.exe", r"--user-data-dir=C:\Temp\pytest-of-Neon\p123", True),
    ("python.exe", "-c \"from multiprocessing.spawn import spawn_main\"", True),
    ("python.exe", r"-c ""import multiprocessing.resource_tracker;main(fd)""", True),
    ("powershell.exe", r"…\launch_chrome.ps1 -Profile …pytest…", True),
    ("node.exe", r"…\Adobe Creative Cloud\js\main.js", False),
    ("python.exe", r"D:\Projects\ozon-collector\scripts\key_daemon.py", False),
])
def test_stale_signature_matrix(name, cmd, expected):
    orphan = pg.ProcInfo(pid=42, ppid=999, name=name)
    snap = _snap((42, 999, name))
    assert pg.is_stale_test_process(snap, orphan, cmd) is expected


def test_stale_requires_dead_parent():
    alive = pg.ProcInfo(pid=42, ppid=100, name="node.exe")
    cmd = r"…\ms-playwright\driver\node.exe"
    assert pg.is_stale_test_process(TREE, alive, cmd) is False


def test_protected_pids_parsing(monkeypatch):
    monkeypatch.setenv("KD_PROTECT_PIDS", "59144, 2222;broken")
    monkeypatch.setattr(pg, "_daemon_pid", lambda: set())
    assert pg.protected_pids() == {59144, 2222}
    monkeypatch.setenv("KD_PROTECT_PIDS", "")
    assert pg.protected_pids() == set()


def test_protected_pids_include_daemon_from_pidfile(monkeypatch, tmp_path):
    """Демон защищается автоматически по data/key_daemon.pid."""
    monkeypatch.setenv("KD_PROTECT_PIDS", "")
    monkeypatch.setattr(pg, "_daemon_pid", lambda: {59144})
    assert pg.protected_pids() == {59144}


def test_daemon_pid_rejects_recycled_pid(monkeypatch, tmp_path):
    """Если pid из pid-файла уже занят другой программой — защиты нет."""
    import json

    data = tmp_path / "data"
    data.mkdir()
    (data / "key_daemon.pid").write_text(json.dumps({"pid": 4242}), encoding="utf-8")
    monkeypatch.setattr(pg, "_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(pg, "snapshot", lambda: {4242: pg.ProcInfo(4242, 1, "notepad.exe")})
    monkeypatch.setattr(pg, "cmdlines", lambda pids: {4242: r"C:\Windows\notepad.exe"})
    monkeypatch.setattr(pg, "_DAEMON_CACHE", None)
    assert pg._daemon_pid() == set()


def test_daemon_pid_accepted_for_real_daemon(monkeypatch, tmp_path):
    import json

    data = tmp_path / "data"
    data.mkdir()
    (data / "key_daemon.pid").write_text(json.dumps({"pid": 777}), encoding="utf-8")
    monkeypatch.setattr(pg, "_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(pg, "snapshot", lambda: {777: pg.ProcInfo(777, 1, "python.exe")})
    monkeypatch.setattr(pg, "cmdlines",
                        lambda pids: {777: r"python.exe scripts\key_daemon.py --url ..."})
    monkeypatch.setattr(pg, "_DAEMON_CACHE", None)
    assert pg._daemon_pid() == {777}


def test_daemon_pid_missing_file_is_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setattr(pg, "_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(pg, "_DAEMON_CACHE", None)
    assert pg._daemon_pid() == set()


def test_disabled_flag(monkeypatch):
    monkeypatch.delenv("KD_KEEP_TEST_PROCS", raising=False)
    assert pg._disabled() is False
    monkeypatch.setenv("KD_KEEP_TEST_PROCS", "1")
    assert pg._disabled() is True
    monkeypatch.setenv("KD_KEEP_TEST_PROCS", "0")
    assert pg._disabled() is False


def test_sweep_never_kills_live_daemon_children(monkeypatch):
    """Sweep бьёт только по сигнатуре ИЛИ-И мёртвый родитель."""
    snap = _snap(
        (900, 1, "python.exe"),
        (901, 900, "chrome.exe"),
        (902, 999, "node.exe"),
    )
    monkeypatch.setattr(pg, "snapshot", lambda: snap)
    monkeypatch.setattr(pg, "cmdlines", lambda pids: {
        901: r"chrome.exe --user-data-dir=…\ozon-profile --remote-debugging-port=9223",
        902: r"…\ms-playwright\driver\node.exe …\cli.js run-driver",
    })
    monkeypatch.setattr(pg, "kill_tree", lambda pid: pytest.fail(f"kill {pid}"))
    stale = [i.pid for i in snap.values() if pg.is_stale_test_process(snap, i, {
        901: r"chrome.exe …ozon-profile…", 902: r"…ms-playwright…",
    }.get(i.pid, ""))]
    assert stale == [902], "браузер демона с живым родителем не трогаем"


def test_snapshot_returns_own_pid():
    """Снимок процессов должен работать в этой ОС (базовый smoke)."""
    snap = pg.snapshot()
    assert os.getpid() in snap
    assert snap[os.getpid()].ppid > 0
