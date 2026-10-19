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


def test_tray_distinguishes_wmi_failure_from_missing_process():
    """WMI failure must not be treated as "daemon is gone".

    Audit regression 2026-10-19: Get-CimInstance with -ErrorAction
    SilentlyContinue yielded $null on a WMI error too, and both the watchdog
    and the exit item closed the tray while the daemon was still alive.
    Owner 2026-10-19: the tray must still stop the daemon when WMI is down —
    the pidfile + process start time prove identity without WMI.
    """
    t = _text(TRAY)
    assert t.count("$cimOk = $false") == 2, \
        "обе проверки демона обязаны различать сбой WMI"
    assert t.count("Test-DaemonIdentity") >= 2, \
        "нет WMI-free проверки личности демона"
    assert t.index("if (-not $cimOk)") < t.index("$script:stopStage++"), \
        "эскалация Ctrl+C/Stop-Process недостижима при сбое WMI"
    import re
    loud = re.findall(
        r'Get-CimInstance Win32_Process -Filter "ProcessId = \$WatchPid"'
        r'[\s`]*?-ErrorAction Stop', t)
    assert len(loud) == 2, \
        "оба запроса демона обязаны падать громко (-ErrorAction Stop)"


def test_tray_script_matches_daemon_constant():
    assert os.path.isfile(TRAY)
    assert k.TRAY_SCRIPT.name == "collector_tray.ps1"
    assert k.TRAY_SCRIPT.exists()


class TestDaemonTrayLifecycle:
    """key_daemon: трей спавнится с демоном, отключается флагами, не лезет в тесты."""

    @pytest.fixture(autouse=True)
    def _no_real_flag_writes(self, monkeypatch):
        """Тесты не трогают живые флаг и трей кампании владельца.

        `_start_tray_companion` снимает stale-треи до early-return; тесты,
        которые убирают PYTEST_CURRENT_TEST, иначе убили бы настоящий
        collector_tray.ps1 (BLOCKER ревью 2026-10-19).
        """
        from ozon_collector import window_policy
        monkeypatch.setattr(window_policy, "clear_user_visible", lambda: None)
        monkeypatch.setattr(k, "_stale_tray_processes", lambda: [])

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

    def test_disabled_tray_still_clears_stale_flag(self, monkeypatch):
        """--no-tray не должен оживлять старый "0" в visible-прогоне."""
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        from ozon_collector import window_policy
        called = []
        monkeypatch.setattr(window_policy, "clear_user_visible",
                            lambda: called.append(1))
        assert k._start_tray_companion(disabled=True) is None
        assert called, "залипший hidden-флаг пережил старт сбора без трея"

    @pytest.mark.skipif(os.name != "nt", reason="kill трея — Windows-only")
    def test_stale_tray_killed_before_flag_clear(self, monkeypatch):
        """Old tray must not see the cleared flag and resurrect windows."""
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        order = []
        monkeypatch.setattr(k, "_kill_stale_trays",
                            lambda: order.append("kill") or [])
        from ozon_collector import window_policy
        monkeypatch.setattr(window_policy, "clear_user_visible",
                            lambda: order.append("clear"))
        assert k._start_tray_companion(disabled=True) is None
        assert order == ["kill", "clear"], order

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


def test_tray_parks_hidden_and_brings_shown_window_onscreen():
    """Скрытие паркует окно за край экрана, показ возвращает его обратно.

    Chrome показывает окно сам (старт/новая вкладка/пазл) и успевает мигнуть
    серым прямоугольником до гашения (живой замер 2026-10-18: show→hide 30 мс).
    За краем экрана показ не виден; любой штатный показ обязан вернуть окно на
    рабочий стол, иначе «Развернуть окна сбора» покажет пустоту.
    """
    t = _text(TRAY)
    assert "ParkOffscreen" in t and "BringOnScreen" in t, \
        "трей не паркует/не возвращает окно"
    mini = t[t.index("public static void Minimize"):]
    mini = mini[:mini.index("public static void EnsureNoTaskbar")]
    assert "ShowWindow(h, SW_HIDE)" in mini and "ParkOffscreen(h)" in mini, \
        "Minimize обязан парковать окно после SW_HIDE"
    assert mini.index("ShowWindow(h, SW_HIDE)") < mini.index("ParkOffscreen(h)"), \
        "парковка обязана идти после SW_HIDE"
    restore = t[t.index("public static void Restore(IntPtr h)"):]
    restore = restore[:restore.index("public static void RestoreBackground")]
    assert restore.index("BringOnScreen(h)") < restore.index("ShowWindow(h, SW_RESTORE)"), \
        "Restore обязан вернуть окно на экран до SW_RESTORE"
    bg = t[t.index("public static void RestoreBackground"):]
    bg = bg[:bg.index("public static void Minimize")]
    assert bg.index("BringOnScreen(h)") < bg.index("ShowWindow(h, SW_RESTORE)"), \
        "RestoreBackground обязан вернуть окно на экран до SW_RESTORE"


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
    assert "Set-UserHiddenFlag" in t, "трей не различает явный hidden"
    hide_body = t[hide:t.index("function Reconcile-CollectorWindows")]
    assert "Set-UserHiddenFlag" in hide_body, \
        "«Свернуть» не пишет явный hidden (env-дефолт воскресит окна)"
    fn = t[t.index("function Test-UserVisibleFlag"):]
    fn = fn[:fn.index("function Send-TrayCtrlC")]
    assert "OZON_KEEP_WINDOW" in fn, \
        ("без файла трей не чтит стартовый --window-mode visible "
         "(BLOCKER ревью 2026-10-19)")


def test_tray_daemon_identity_uses_pidfile_without_wmi():
    """WMI-free identity: data/key_daemon.pid (pid + started_at) + StartTime.

    PidLock writes those fields atomically (scripts/key_daemon.py); this keeps
    the tray able to stop the daemon when WMI is unavailable, without ever
    signalling an unverified pid.
    """
    t = _text(TRAY)
    fn = t[t.index("function Test-DaemonIdentity"):]
    fn = fn[:fn.index("function Stop-Daemon")]
    assert "key_daemon.pid" in fn
    assert "started_at" in fn
    assert "Get-Process -Id $TargetPid" in fn
    assert "Get-CimInstance" not in fn, \
        "identity-проверка не должна зависеть от WMI"
    assert "-le 30" in fn, "нет допуска между стартом процесса и pid-файлом"


def test_tray_daemon_identity_behavior(tmp_path):
    """Runs the extracted Test-DaemonIdentity under PowerShell 5.1.

    Source-text pins alone would accept a typo in the datetime parse; this
    harness writes a real pidfile for the current PowerShell process and
    checks match/mismatch behavior (review 2026-10-19).
    """
    if os.name != "nt":
        pytest.skip("PowerShell: Windows-only")
    ps = shutil.which("powershell") or shutil.which("pwsh")
    assert ps, "не найден powershell/pwsh"
    t = _text(TRAY)
    fn = t[t.index("function Test-DaemonIdentity"):]
    fn = fn[:fn.index("function Stop-Daemon")]
    work = tmp_path / "scripts"
    work.mkdir()
    (tmp_path / "data").mkdir()
    harness = work / "identity.ps1"
    harness.write_text(fn + '''

$me = $PID
$pf = Join-Path (Split-Path -Parent $PSScriptRoot) "data\\key_daemon.pid"
$start = (Get-Process -Id $me).StartTime.ToUniversalTime().ToString("o")
@{ pid = $me; campaign = "t"; started_at = $start } | ConvertTo-Json |
    Set-Content -LiteralPath $pf -Encoding UTF8
$okSame = Test-DaemonIdentity -TargetPid $me
$old = (Get-Date).ToUniversalTime().AddHours(-1).ToString("o")
@{ pid = $me; campaign = "t"; started_at = $old } | ConvertTo-Json |
    Set-Content -LiteralPath $pf -Encoding UTF8
$okOld = Test-DaemonIdentity -TargetPid $me
$newStart = (Get-Date).ToUniversalTime().ToString("o")
@{ pid = $me + 1; campaign = "t"; started_at = $newStart } | ConvertTo-Json |
    Set-Content -LiteralPath $pf -Encoding UTF8
$okPid = Test-DaemonIdentity -TargetPid $me
Write-Output ("{0} {1} {2}" -f $okSame, $okOld, $okPid)
''', encoding="utf-8-sig")
    out = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", str(harness)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"harness упал: {out.stderr}"
    tokens = out.stdout.strip().split()
    assert tokens[-3:] == ["True", "False", "False"], \
        f"identity: совпадение/старый старт/чужой pid → {tokens} " \
        f"(stderr={out.stderr!r})"


def test_tray_visible_flag_behavior(tmp_path):
    """PS 5.1 harness: absent/empty flag falls back to env; "0"/"1" win."""
    if os.name != "nt":
        pytest.skip("PowerShell: Windows-only")
    ps = shutil.which("powershell") or shutil.which("pwsh")
    assert ps, "не найден powershell/pwsh"
    t = _text(TRAY)
    fn = t[t.index("function Test-UserVisibleFlag"):]
    fn = fn[:fn.index("function Send-TrayCtrlC")]
    harness = tmp_path / "flag.ps1"
    harness.write_text(f'''$script:FlagPath = "{tmp_path / 'flag'}"
{fn}
function Try-Env($v) {{
    $env:OZON_KEEP_WINDOW = $v
    Write-Output (Test-UserVisibleFlag)
}}
Remove-Item -LiteralPath $script:FlagPath -ErrorAction SilentlyContinue
$absentOn = Try-Env "1"
$absentOff = Try-Env "0"
Set-Content -LiteralPath $script:FlagPath -Value "0" -Encoding UTF8
$hiddenOn = Try-Env "1"
Set-Content -LiteralPath $script:FlagPath -Value "1" -Encoding UTF8
$shownOff = Try-Env "0"
[IO.File]::WriteAllText($script:FlagPath, "")
$emptyOn = Try-Env "1"
$emptyOff = Try-Env "0"
Write-Output ("{{0}} {{1}} {{2}} {{3}} {{4}} {{5}}" -f $absentOn,
    $absentOff, $hiddenOn, $shownOff, $emptyOn, $emptyOff)
''', encoding="utf-8-sig")
    out = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", str(harness)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"harness упал: {out.stderr}"
    tokens = out.stdout.strip().split()
    assert tokens[-6:] == ["True", "False", "False", "True",
                           "True", "False"], \
        f"absent/\"0\"/\"1\"/empty → {tokens} (stderr={out.stderr!r})"


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
