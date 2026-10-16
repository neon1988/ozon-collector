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

    @pytest.mark.skipif(os.name != "nt", reason="трей — Windows-only")
    def test_spawn_clears_stale_user_visible_flag(self, monkeypatch):
        """Залипший флаг видимых окон (трей убили taskkill'ом) при старте
        сбора снимается, иначе сторож молчал бы весь прогон."""
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delenv("OZON_TRAY", raising=False)
        monkeypatch.setattr(k, "_kill_stale_trays", lambda: [])
        from ozon_collector import window_policy
        called = []
        monkeypatch.setattr(window_policy, "clear_user_visible",
                            lambda: called.append(1))

        class FakeProc:
            pid = 4322

        monkeypatch.setattr(k.subprocess, "Popen", lambda *a, **kw: FakeProc())
        assert k._start_tray_companion() is not None
        assert called, "key_daemon не снял залипший флаг user-visible"

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


def test_tray_hide_uses_sw_hide_not_show_minimized():
    """«Свернуть окна сбора» прячет окно, а не показывает свёрнутым.

    Живой замер 2026-10-16: SW_MINIMIZE / SW_SHOWMINNOACTIVE на
    TOOLWINDOW-окне рисует над панелью задач «мини-плитку-призрак» с
    крестиком (vis=True icon=True rect=0,1004,160,1032) и окно перестаёт
    отдавать кадры. Прячем только SW_HIDE.
    """
    t = _text(TRAY)
    code = "\n".join(ln for ln in t.splitlines()
                     if not ln.lstrip().startswith(("#", "//")))
    assert "SW_SHOWMINNOACTIVE" not in code, \
        "трей снова показывает окно свёрнутым (плитка-призрак)"
    assert "ShowWindow(h, SW_HIDE)" in code, "трей не прячет окно через SW_HIDE"


def test_tray_restore_makes_normal_taskbar_window():
    """«Развернуть окна сбора» → обычное окно на панели задач, не «toolbox».

    Требование владельца 2026-10-16: «пусть уж будут на панель задач а не в
    toolbox». Значит Restore снимает ОБА EX-стиля (TOOLWINDOW убирает
    toolbox-вид и Alt+Tab-исключение, NOACTIVATE — блокировку ввода) и явно
    создаёт кнопку: Explorer не создаёт её для окна, рождённого скрытым
    (живая находка 2026-10-16: стили сняты, кнопки не появились сами).
    """
    t = _text(TRAY)
    assert "& ~WS_EX_NOACTIVATE & ~WS_EX_TOOLWINDOW" in t, \
        "Restore не снимает оба EX-стиля"
    assert "TaskbarTab.Add(h)" in t, "Restore не создаёт кнопку панели задач"
    assert "t.AddTab(h)" in t, "AddTab не вызывается у COM-объекта"


def test_tray_user_visible_flag_contract():
    """Флаг data/window_visible.flag — контракт трея с Python-сторожем.

    Пока окна показаны, сторож (window_policy.user_visible) и проба окна в
    пайплайне пазла не должны ничего прятать. Флаг ставится в Show и
    снимается в Hide/Close-Tray; патруль панели задач при флаге спит.
    """
    t = _text(TRAY)
    assert "window_visible.flag" in t, "трей не пишет флаг user-visible"
    assert "Set-UserVisibleFlag" in t
    assert "Clear-UserVisibleFlag" in t
    show = t.index("function Show-CollectorWindows")
    hide = t.index("function Hide-CollectorWindows")
    show_body = t[show:hide]
    assert show_body.index("Set-UserVisibleFlag") < show_body.index("Restore($h)"), \
        "флаг ставится после показа (сторож успеет спрятать окно)"


def test_tray_reconcile_enforces_both_modes():
    """Трей — переключатель РЕЖИМА (слова владельца 2026-10-16): visible —
    «должны показываться» (патруль возвращает скрытое окно нового воркера),
    hidden — «не должны показываться» (патруль прячет самовольно показанное).
    """
    t = _text(TRAY)
    rec = t[t.index("function Reconcile-CollectorWindows"):]
    rec = rec[:rec.index("if ($List)")]
    assert "Test-UserVisibleFlag" in rec, "патруль не читает режим"
    assert "RestoreBackground" in rec, \
        "visible-режим не восстанавливает скрытые окна"
    assert "Minimize($h)" in rec, \
        "hidden-режим не прячет самовольно показанное окно"
    assert "EnsureNoTaskbar" in rec, "патруль не чистит кнопки панели задач"


def test_tray_exit_stops_daemon():
    """«Выход» останавливает и сбор, а не только иконку.

    Ответ владельца 2026-10-16 на уточнение: «Выход завершает и сбор».
    Механика: Ctrl+C в консоль демона (graceful SIGINT → INTERRUPTED), при
    повторном клике — форс-путь, при неудаче — Stop-Process. Живая проверка
    -SendCtrlC выполняется отдельно; здесь — контракт кода.
    """
    t = _text(TRAY)
    assert "$itemExit.add_Click({ Stop-Daemon })" in t, \
        "«Выход» снова только закрывает иконку"
    assert "GenerateConsoleCtrlEvent" in t, "нет Ctrl+C в консоль демона"
    assert "AttachConsole" in t, "нет привязки к консоли демона"
    assert "SetConsoleCtrlHandler" in t, "трей убьёт себя своим же Ctrl+C"
    assert "[int]$SendCtrlC = 0" in t and "Send-TrayCtrlC" in t, \
        "нет скрытого CLI -SendCtrlC для живой проверки"
    assert "Stop-DaemonHard" in t, "нет жёсткого fallback-пути"
