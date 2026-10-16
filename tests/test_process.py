"""Тесты изолированного process layer (ozon_collector/process.py).

Обязательные сценарии (по ревью):
- PID-reuse: тот же pid с чужим start_key НЕ считается живым и НЕ убивается;
- non-child Chromium: exit-detection через pidfd работает для процесса, которого
  мы не «забираем» через waitpid (Linux);
- проверка identity непосредственно перед kill;
- lifecycle строго монотонен.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from ozon_collector import process as P

IS_WINDOWS = os.name == "nt"
IS_POSIX = os.name == "posix"


def _spawn_sleeper():
    """Долгоживущий процесс-заглушка для тестов kill/identity."""
    if IS_WINDOWS:
        return subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", "Start-Sleep -Seconds 60"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.Popen(["sh", "-c", "sleep 60"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---- ProcessIdentity / ProcessRecord / lifecycle ---------------------------

def test_lifecycle_forward_and_backward():
    rec = P.ProcessRecord("R1", P.ProcessIdentity(pid=1, start_key="x"))
    assert rec.lifecycle == P.Lifecycle.CREATED
    rec.set_lifecycle(P.Lifecycle.ALIVE)
    rec.set_lifecycle(P.Lifecycle.CDP_READY)
    assert rec.cdp_ready_at is not None
    rec.set_lifecycle(P.Lifecycle.KILLED)
    rec.set_lifecycle(P.Lifecycle.VERIFIED_DEAD)
    with pytest.raises(ValueError):
        rec.set_lifecycle(P.Lifecycle.ALIVE)  # назад нельзя


def test_record_partial_flag():
    rec = P.ProcessRecord("R1", P.ProcessIdentity(pid=1, start_key="x"),
                          partial=True)
    assert rec.partial is True
    assert rec.lifecycle == P.Lifecycle.CREATED


def test_identity_eq_hash():
    a = P.ProcessIdentity(pid=42, start_key="s")
    b = P.ProcessIdentity(pid=42, start_key="s")
    c = P.ProcessIdentity(pid=42, start_key="other")
    assert a == b
    assert hash(a) == hash(b)
    assert a != c


# ---- cdp_port / tokenize / matches -----------------------------------------

def test_cdp_port():
    assert P.cdp_port("http://127.0.0.1:9222") == 9222
    assert P.cdp_port("http://127.0.0.1:7777") == 7777
    assert P.cdp_port("http://127.0.0.1") == 9222


# ---- порт CDP: зарезервирован Windows vs занят процессом (2026-10-03) -------

def test_port_bindable_distinguishes_reserved_from_occupied():
    """Диагностика порта различает «зарезервирован Windows» и «занят».

    Живой замер 2026-10-03: и main (9222), и все воркерские (9223-9230) попали в
    диапазон исключений 9172-9271 (WSL/Hyper-V пересчитали резервации). Bind
    давал WSAEACCES, поэтому Chromium не мог поднять DevTools, и демон получал
    «Chromium EXITED, CDP never came up» при ПОЛНОСТЬЮ СВОБОДНОМ порте. Старая
    подсказка «порт занят старым» уводила в сторону.
    """
    ok_free, why_free = P.port_bindable(0)      # порт 0 = любой свободный
    assert ok_free is True, why_free
    reserved = None
    for cand in range(9200, 9300):
        ok, _why = P.port_bindable(cand)
        if not ok:
            reserved = cand
            break
    if reserved is not None:
        ok, why = P.port_bindable(reserved)
        assert ok is False
        assert "WSAEACCES" in why or "зарезервирован" in why.lower(), why
        assert "netsh" in why, f"в диагностике нужна команда проверки: {why}"


def test_cdp_port_base_falls_back_when_preferred_unusable(monkeypatch):
    """Предпочитаемый порт недоступен → база уезжает на свободный, с причиной."""
    calls = []

    def _bindable(port):
        calls.append(port)
        good = port != P.MAIN_CDP_PORT
        return (good, "свободен" if good
                else "порт зарезервирован Windows (WSAEACCES)")

    monkeypatch.setattr(P, "port_bindable", _bindable)
    base, why = P.cdp_port_base()
    assert base != P.MAIN_CDP_PORT, "при зарезервированном порте база обязана уехать"
    assert "WSAEACCES" in why, why
    assert calls, "проверка доступности обязана быть"


def test_cdp_port_base_keeps_preferred_when_free(monkeypatch):
    monkeypatch.setattr(P, "port_bindable", lambda port: (True, "свободен"))
    base, why = P.cdp_port_base()
    assert base == P.MAIN_CDP_PORT
    assert why == "свободен"


def test_worker_ports_shift_with_base(monkeypatch):
    """Порты воркеров следуют за базой, а не за жёстким 9222."""
    monkeypatch.setattr(P, "port_bindable", lambda port: (True, "свободен"))
    monkeypatch.setattr(P, "MAIN_CDP_PORT", 9222)
    assert P.worker_cdp_port(1) == 9223
    monkeypatch.setattr(P, "MAIN_CDP_PORT", 9444)
    assert P.worker_cdp_port(1) == 9445
    assert P.worker_cdp_url(7) == "http://127.0.0.1:9451"


def test_worker_port_env_override(monkeypatch):
    monkeypatch.setenv("OZON_WORKER_CDP_PORT", "9500")
    assert P.worker_cdp_port(1) == 9501


def test_tokenize_cmdline_respects_quotes():
    s = ('"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
         '--remote-debugging-port=9222 '
         '--user-data-dir="C:\\Users\\Neon\\AppData\\Local\\opencode\\ozon-profile"')
    toks = P._tokenize_cmdline(s)
    assert toks[0] == r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    assert toks[1] == "--remote-debugging-port=9222"
    assert toks[2] == (r"--user-data-dir=C:\Users\Neon\AppData\Local"
                       r"\opencode\ozon-profile")


def test_matches_browser_positive():
    args = ["chrome", "--remote-debugging-port=9222",
            "--user-data-dir=/tmp/opencode/ozon-profile", "--no-first-run"]
    assert P._matches_browser(args, 9222)


def test_matches_browser_wrong_port():
    args = ["chrome", "--remote-debugging-port=9333",
            "--user-data-dir=/tmp/opencode/ozon-profile"]
    assert not P._matches_browser(args, 9222)


def test_matches_browser_wrong_profile():
    args = ["chrome", "--remote-debugging-port=9222",
            "--user-data-dir=/tmp/opencode/other-profile"]
    assert not P._matches_browser(args, 9222)


def test_matches_browser_renderer_child_excluded():
    args = ["chrome", "--remote-debugging-port=9222",
            "--user-data-dir=/tmp/opencode/ozon-profile",
            "--type=renderer", "--no-sandbox"]
    assert not P._matches_browser(args, 9222)


def test_matches_browser_missing_udp():
    args = ["chrome", "--remote-debugging-port=9222"]
    assert not P._matches_browser(args, 9222)


def test_browser_processes_filters_to_browser_only(monkeypatch):
    """Из потока процессов берём только Browser-процесс на нашем profile+port."""
    fake = [
        (1001, ["chrome", "--remote-debugging-port=9222",
                "--user-data-dir=/tmp/opencode/ozon-profile"], 900),
        (1002, ["chrome", "--remote-debugging-port=9222",
                "--user-data-dir=/tmp/opencode/ozon-profile",
                "--type=renderer"], 1001),
        (1003, ["chrome", "--remote-debugging-port=9333",
                "--user-data-dir=/tmp/opencode/ozon-profile"], 900),
        (1004, ["chrome", "--remote-debugging-port=9222",
                "--user-data-dir=/tmp/opencode/other-profile"], 900),
    ]
    monkeypatch.setattr(P, "_iter_chromium_processes", lambda: iter(fake))
    monkeypatch.setattr(P, "process_identity",
                        lambda pid: P.ProcessIdentity(pid=pid, start_key=f"s{pid}"))
    ids = P.browser_processes("http://127.0.0.1:9222")
    assert [i.pid for i in ids] == [1001]


# ---- geckodriver: осиротевшие драйверы Firefox (2026-09-25) -------------------

def _ff_argv(profile):
    return ["firefox.exe", "-profile", profile]


def test_geckodriver_processes_lists_all(monkeypatch):
    fake = [(501, ["geckodriver.exe"], 400), (502, ["geckodriver.exe"], 401)]
    monkeypatch.setattr(P, "_iter_named_processes",
                        lambda name: iter(fake if name == "geckodriver" else []))
    monkeypatch.setattr(P, "process_identity",
                        lambda pid: P.ProcessIdentity(pid=pid, start_key=f"s{pid}"))
    assert [i.pid for i in P.geckodriver_processes()] == [501, 502]


def test_orphaned_geckodriver_keeps_driver_with_our_firefox(monkeypatch):
    """geckodriver с живым firefox-ребёнком на НАШЕМ профиле — не наш мусор."""
    ff_profile = P.firefox_profile_path(P.worker_profile_token(1))
    ff = [(601, _ff_argv(ff_profile), 500)]
    drivers = [(500, ["geckodriver.exe"], 400), (700, ["geckodriver.exe"], 400)]
    monkeypatch.setattr(P, "_iter_firefox_processes", lambda: iter(ff))
    monkeypatch.setattr(P, "_iter_named_processes", lambda name: iter(drivers))
    monkeypatch.setattr(P, "process_identity",
                        lambda pid: P.ProcessIdentity(pid=pid, start_key=f"s{pid}"))
    assert [i.pid for i in P.orphaned_geckodriver_processes()] == [700]


def test_orphaned_geckodriver_reaps_driver_without_our_firefox(monkeypatch):
    """Драйвер без нашего firefox (окно убито/краш) — осиротевший, убираем."""
    personal = [(601, ["firefox.exe"], 900)]  # личный Firefox, не наш профиль
    drivers = [(500, ["geckodriver.exe"], 400)]
    monkeypatch.setattr(P, "_iter_firefox_processes", lambda: iter(personal))
    monkeypatch.setattr(P, "_iter_named_processes", lambda name: iter(drivers))
    monkeypatch.setattr(P, "process_identity",
                        lambda pid: P.ProcessIdentity(pid=pid, start_key=f"s{pid}"))
    assert [i.pid for i in P.orphaned_geckodriver_processes()] == [500]


# ---- живой процесс: kill, identity, PID-reuse ------------------------------

def test_identity_alive_and_kill_real_process():
    """Полный цикл: identity → alive → kill_records → verified_dead."""
    proc = _spawn_sleeper()
    try:
        ident = P.process_identity(proc.pid)
        assert ident is not None
        assert P.identity_alive(ident) is True

        rec = P.ProcessRecord("R1", ident)
        killed = P.kill_records([rec], wait_s=5)
        assert killed == [rec]
        assert rec.lifecycle == P.Lifecycle.VERIFIED_DEAD
        assert P.identity_alive(ident) is False
        proc.wait(timeout=10)
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            pass


def test_pid_reuse_foreign_identity_not_killed():
    """PID-reuse: чужой start_key → процесс не наш → НЕ убиваем живой процесс.

    Рекорд с чужой identity корректно уходит в verified_dead (того инстанса
    не существует), но реальный процесс с этим pid остаётся жив.
    """
    proc = _spawn_sleeper()
    try:
        ident = P.process_identity(proc.pid)
        assert ident is not None
        foreign = P.ProcessIdentity(pid=ident.pid, start_key="0000")
        assert P.identity_alive(foreign) is False

        rec = P.ProcessRecord("R1", foreign)
        P.kill_records([rec], wait_s=0.5)
        assert proc.poll() is None           # живой процесс не тронут
        assert P.identity_alive(ident) is True
        assert rec.lifecycle == P.Lifecycle.VERIFIED_DEAD
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            pass


def test_identity_alive_dead_pid():
    assert P.identity_alive(P.ProcessIdentity(pid=999999999, start_key="x")) is False


def test_command_line_processes_filters_by_token(monkeypatch):
    """Трей-пульт находится по маркеру в cmdline, чужие powershell — нет."""
    fake = [
        (111, ["powershell", "-File", "D:\\proj\\scripts\\collector_tray.ps1"], 1),
        (222, ["powershell", "-File", "D:\\proj\\scripts\\other.ps1"], 1),
    ]
    monkeypatch.setattr(P, "_iter_named_processes", lambda name: iter(fake))
    monkeypatch.setattr(P, "process_identity",
                        lambda pid: P.ProcessIdentity(pid=pid, start_key="t"))
    got = P.command_line_processes("powershell", "collector_tray.ps1")
    assert [i.pid for i in got] == [111]


def test_wait_exit_dead_identity_is_true():
    assert P.wait_exit(P.ProcessIdentity(pid=999999999, start_key="x"),
                       wait_s=0.2) is True


def test_kill_records_already_dead_mark_verified():
    rec = P.ProcessRecord("R1", P.ProcessIdentity(pid=999999999, start_key="x"))
    killed = P.kill_records([rec], wait_s=0.2)
    assert killed == [rec]
    assert rec.lifecycle == P.Lifecycle.VERIFIED_DEAD


# ---- kill_identities (свободный профиль) -----------------------------------

def test_kill_identities_kills_and_reports_pid():
    proc = _spawn_sleeper()
    try:
        ident = P.process_identity(proc.pid)
        assert ident is not None
        assert P.kill_identities([ident], wait_s=5) == [proc.pid]
        assert P.identity_alive(ident) is False
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_kill_identities_skips_dead():
    """Мёртвый (или переиспользованный pid с чужим start_key) — не трогаем."""
    assert P.kill_identities(
        [P.ProcessIdentity(pid=999999999, start_key="x")], wait_s=0.2) == []


def test_kill_identities_empty():
    assert P.kill_identities([], wait_s=0.2) == []


# ---- Linux-only: pidfd exit detection (non-child) --------------------------

@pytest.mark.skipif(not IS_POSIX, reason="pidfd доступен только на Linux")
def test_linux_wait_exit_non_child():
    """non-child: exit через pidfd+poll у процесса, который мы не re-ap'им.

    Запускаем sleep в отдельной сессии (не обязаны быть его waitpid-родителем),
    проверяем «жив → нет события», затем kill через pidfd → POLLIN → исчез.
    """
    proc = subprocess.Popen(["sh", "-c", "sleep 60"],
                            start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ident = P.process_identity(proc.pid)
        assert ident is not None
        assert P.identity_alive(ident) is True
        assert P.wait_exit(ident, wait_s=0.3) is False  # жив: события нет

        rec = P.ProcessRecord("R1", ident)
        killed = P.kill_records([rec], wait_s=5)
        assert killed == [rec]
        assert rec.lifecycle == P.Lifecycle.VERIFIED_DEAD
        assert P.identity_alive(ident) is False
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


@pytest.mark.skipif(not IS_POSIX, reason="pidfd доступен только на Linux")
def test_linux_proc_stat_self():
    ident = P.process_identity(os.getpid())
    assert ident is not None
    assert P.identity_alive(ident) is True


@pytest.mark.skipif(not IS_POSIX, reason="pidfd доступен только на Linux")
def test_linux_pidfd_signal_supported_returns_bool():
    assert isinstance(P._pidfd_signal_supported(), bool)


@pytest.mark.skipif(not IS_POSIX, reason="pidfd доступен только на Linux")
def test_linux_pidfd_open_self():
    fd = P._pidfd_open(os.getpid())
    assert fd is not None
    os.close(fd)
