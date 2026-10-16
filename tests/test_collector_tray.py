"""Тесты трей-пульта окон сбора: скрипт + привязка к жизненному циклу демона."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scripts.key_daemon as k  # noqa: E402

TRAY = os.path.join(ROOT, "scripts", "collector_tray.ps1")


def _text(path: str) -> str:
    with open(path, "rb") as f:
        return f.read().decode("utf-8-sig")


def test_tray_script_utf8_bom():
    """BOM обязателен: PowerShell 5.1 без BOM читает файл как ANSI (cp1251)
    и коверкает кириллицу в меню — та же ловушка, что у launch_chrome.ps1."""
    with open(TRAY, "rb") as f:
        raw = f.read()
    assert raw.startswith(b"\xef\xbb\xbf"), "collector_tray.ps1 потерял UTF-8 BOM"
    assert raw.strip(), "collector_tray.ps1 пуст"


def test_tray_script_parses_as_powershell():
    if os.name != "nt":
        pytest.skip("парсер PowerShell доступен только на Windows")
    ps = shutil.which("powershell") or shutil.which("pwsh")
    assert ps, "не найден powershell/pwsh"
    check = (
        "$e=$null;$t=$null;"
        "[void][System.Management.Automation.Language.Parser]::"
        "ParseFile('%s',[ref]$t,[ref]$e);"
        "if($e -and $e.Count){$e[0].Message;exit 1}else{'OK'}" % TRAY
    )
    out = subprocess.run([ps, "-NoProfile", "-Command", check],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, \
        f"collector_tray.ps1 не парсится: {out.stdout}{out.stderr}"
    assert "OK" in out.stdout


def test_tray_watchdog_contract():
    """Трей обязан: принять -WatchPid, проверять cmdline демона (защита от
    PID-reuse) и выходить тем же путём, что кнопка «Выход»."""
    t = _text(TRAY)
    assert "[int]$WatchPid = 0" in t
    assert "$WatchPid -gt 0" in t
    assert "*key_daemon*" in t, "watchdog не проверяет cmdline демона"
    assert "Close-Tray" in t, "выход по watchdog не закрывает иконку"


def test_tray_script_matches_daemon_constant():
    assert os.path.isfile(TRAY)
    assert k.TRAY_SCRIPT.name == "collector_tray.ps1"
    assert k.TRAY_SCRIPT.exists()


class TestDaemonTrayLifecycle:
    """key_daemon: трей спавнится с демоном, отключается флагами, не лезет в тесты."""

    def test_skipped_under_pytest(self, monkeypatch):
        monkeypatch.delenv("OZON_TRAY", raising=False)
        assert os.environ.get("PYTEST_CURRENT_TEST"), "pytest не выставил маркер"
        assert k._start_tray_companion() is None

    def test_skipped_when_env_off(self, monkeypatch):
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setenv("OZON_TRAY", "0")
        assert k._start_tray_companion() is None

    def test_skipped_with_disable_flag(self, monkeypatch):
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delenv("OZON_TRAY", raising=False)
        assert k._start_tray_companion(disabled=True) is None

    @pytest.mark.skipif(os.name != "nt", reason="трей — Windows-only")
    def test_spawn_command_watches_daemon_pid(self, monkeypatch):
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delenv("OZON_TRAY", raising=False)
        monkeypatch.setattr(k, "_kill_stale_trays", lambda: [])
        calls = {}

        class FakeProc:
            pid = 4321

        def fake_popen(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
            return FakeProc()

        monkeypatch.setattr(k.subprocess, "Popen", fake_popen)
        proc = k._start_tray_companion()
        assert proc is not None
        cmd = calls["cmd"]
        assert cmd[0] == "powershell"
        assert "-WatchPid" in cmd
        assert cmd[cmd.index("-WatchPid") + 1] == str(os.getpid())
        assert str(k.TRAY_SCRIPT) in cmd
        assert calls["kwargs"].get("creationflags") == 0x08000000

    def test_kill_stale_trays_uses_identity_kill(self, monkeypatch):
        sentinel = ["T1", "T2"]
        captured = {}

        def fake_kill(idents, wait_s=3.0):
            captured["idents"] = list(idents)
            captured["wait_s"] = wait_s
            return [7]

        monkeypatch.setattr(k, "_stale_tray_processes", lambda: sentinel)
        monkeypatch.setattr(k, "kill_identities", fake_kill)
        assert k._kill_stale_trays() == [7]
        assert captured["idents"] == sentinel
        assert captured["wait_s"] == k._TRAY_KILL_WAIT_S

    def test_main_passes_no_tray_flag(self, monkeypatch):
        monkeypatch.setattr(k, "run_campaign", lambda **kw: {
            "campaign": kw["campaign"], "target_count": 5, "min_valid": 1,
            "valid_pids": [], "discovered_pids": [], "failed_pids": [],
            "permanently_failed_pids": [], "runs": [],
            "final_state": "COMPLETED", "final_reason": "target_reached"})
        captured = {}

        def fake_start(disabled=False):
            captured["disabled"] = disabled

        monkeypatch.setattr(k, "_start_tray_companion", fake_start)
        k.main(["--url", "https://www.ozon.ru/category/x-1/", "--limit", "5",
                "--min-valid", "1", "--no-tray"])
        assert captured["disabled"] is True


def test_tray_reconcile_contract():
    """Патруль панели задач: каждые 2с снимать кнопки окон сбора.

    Требование владельца 2026-10-16: окна сбора не висят на панели задач,
    восстанавливаются из трея. Даже если кнопку создал кто-то в обход
    лаунчера — патруль её удаляет (смена TOOLWINDOW кнопку не убирает).
    """
    t = _text(TRAY)
    assert "Reconcile-CollectorWindows" in t
    assert "EnsureNoTaskbar" in t, "патруль не восстанавливает TOOLWINDOW/кнопку"
    assert "DeleteTab" in t, "патруль не вызывает ITaskbarList::DeleteTab"
    assert "$reconcile.Interval = 2000" in t, "патруль не поставлен на таймер"


def test_tray_startup_shows_no_balloon():
    """Стартового всплывающего уведомления быть не должно.

    Win11 рисует его тостом-«плиткой» над панелью задач и подсвечивает
    иконку при каждом старте сбора (живой отзыв владельца 2026-10-16:
    «плитка над панелью задач до сих пор появляется»). Уведомление
    «окна не найдены» по клику в меню остаётся.
    """
    t = _text(TRAY)
    assert "Иконка окон сбора активна" not in t, \
        "стартовый balloon вернулся"
    assert "ShowBalloonTip" in t, "потерян balloon «окна не найдены»"
