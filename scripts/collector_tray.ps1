#!/usr/bin/env pwsh
# Tray companion for ozon-collector Chromium windows (Windows, pwsh 7+).
#
# Usage:
#   pwsh -NoProfile -File scripts/collector_tray.ps1 -WatchPid <pid>
#       normal mode: key_daemon.main() starts the tray exactly like this. The
#       icon lives with the collection: the watchdog below exits the tray
#       within ~2s after the watched daemon pid dies (works on taskkill too,
#       so the icon can never hang around after the run stops).
#   pwsh -NoProfile -File scripts/collector_tray.ps1        # tray icon, no watchdog (debug)
#   pwsh -NoProfile -File scripts/collector_tray.ps1 -List  # print found windows, no GUI
#   pwsh -NoProfile -File scripts/collector_tray.ps1 -Show  # restore collector windows once
#   pwsh -NoProfile -File scripts/collector_tray.ps1 -Hide  # hide collector windows once
#   pwsh -NoProfile -File scripts/collector_tray.ps1 -SendCtrlC <pid>  # Ctrl+C to that console (debug)
#
# Finds ONLY collector Chromium windows: chrome.exe whose command line contains
# --user-data-dir with "ozon-profile" token (main + worker-N profiles). Foreign
# Chrome windows of the user are never touched.
#
# The tray is a MODE TOGGLE for collector windows (owner's wording 2026-10-16):
# «Свернуть» → hidden mode: no collector window is ever shown; «Развернуть» →
# visible mode: all collector windows are shown. Mode lives in the flag file
# data/window_visible.flag, shared with Python (window_policy.user_visible).
# In visible mode the reconcile timer also re-shows a newly born (still hidden)
# window without stealing focus; in hidden mode it hides a self-shown window.
# Restore («Развернуть») makes collectors NORMAL taskbar windows (owner's demand
# 2026-10-16: «пусть уж будут на панель задач а не в toolbox»): clears BOTH
# WS_EX_TOOLWINDOW and WS_EX_NOACTIVATE, SW_RESTOREs, applies
# ITaskbarList::AddTab and takes foreground. While windows are shown the flag is
# set — the window guard and the puzzle capture touch nothing the user is
# looking at. Hide clears the flag and re-adds TOOLWINDOW/NOACTIVATE + SW_HIDE +
# DeleteTab.
#
# Hide calls SW_HIDE + ITaskbarList::DeleteTab — NOT SW_MINIMIZE /
# SW_SHOWMINNOACTIVE: a minimized TOOLWINDOW window is rendered by Windows as a
# small "ghost strip" with a close cross above the taskbar (live finding
# 2026-10-16 — owner's «мини-плитка») and stops serving frames. Changing
# WS_EX_TOOLWINDOW alone does NOT remove a taskbar button that was already
# created at window birth — Explorer must re-create the state (live finding
# 2026-10-16: styles were on, buttons stayed). A reconcile timer (every 2s)
# additionally deletes any button that reappears while the tray runs; in
# visible mode the same timer instead re-shows hidden collector windows.
#
# «Выход» stops the COLLECTION, not just the icon (owner's demand 2026-10-16:
# «выход в трее, а сборщик продолжает работать» → «Выход завершает и сбор»):
# the first click sends Ctrl+C to the daemon console — its graceful path
# (progress saved, final_state=INTERRUPTED). The icon stays until the daemon is
# really gone (the watchdog closes it within ~2s). A second click escalates
# (another Ctrl+C = the daemon's own _FORCE_EXIT path); a third click or a
# console-less daemon falls back to Stop-Process -Force (workers drop their
# browsers via the parent watchdog).

param(
    [switch]$List,
    [switch]$Show,
    [switch]$Hide,
    [int]$SendCtrlC = 0,
    [int]$WatchPid = 0
)

$ErrorActionPreference = "Stop"

Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public class CollectorTrayWin {
    public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder sb, int max);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder sb, int max);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int cmd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll", SetLastError=true)] public static extern int GetWindowLong(IntPtr hWnd, int idx);
    [DllImport("user32.dll", SetLastError=true)] public static extern int SetWindowLong(IntPtr hWnd, int idx, int val);

    public const int GWL_EXSTYLE = -20;
    public const int WS_EX_NOACTIVATE = 0x08000000;
    public const int WS_EX_TOOLWINDOW = 0x00000080;
    public const int SW_RESTORE = 9;
    public const int SW_HIDE = 0;

    public static List<IntPtr> FindByPids(int[] pids) {
        var set = new HashSet<int>(pids);
        var found = new List<IntPtr>();
        EnumWindows(delegate(IntPtr h, IntPtr lp) {
            uint pid;
            GetWindowThreadProcessId(h, out pid);
            if (!set.Contains((int)pid)) return true;
            var cls = new StringBuilder(256);
            GetClassName(h, cls, 256);
            if (cls.ToString() == "Chrome_WidgetWin_1") found.Add(h);
            return true;
        }, IntPtr.Zero);
        return found;
    }

    public static string Title(IntPtr h) {
        var sb = new StringBuilder(512);
        GetWindowText(h, sb, 512);
        return sb.ToString();
    }

    public static int ExStyle(IntPtr h) {
        return GetWindowLong(h, GWL_EXSTYLE);
    }

    // «Окно реально видно пользователю»: не SW_HIDE и не иконизировано.
    public static bool IsShown(IntPtr h) {
        return IsWindowVisible(h) && !IsIconic(h);
    }

    // Restore = «окно снова обычное»: панель задач + взаимодействие. Оба
    // EX-стиля снимаются (TOOLWINDOW давал «toolbox» — жалоба владельца),
    // кнопку создаём явно: Explorer сам её не создаёт для окна, которое уже
    // рождалось скрытым без кнопки (живая находка 2026-10-16).
    public static void Restore(IntPtr h) {
        int st = GetWindowLong(h, GWL_EXSTYLE);
        SetWindowLong(h, GWL_EXSTYLE, st & ~WS_EX_NOACTIVATE & ~WS_EX_TOOLWINDOW);
        ShowWindow(h, SW_RESTORE);
        TaskbarTab.Add(h);
        SetForegroundWindow(h);
    }

    // Показ нового окна в режиме visible без кражи фокуса: воркер может
    // родиться в любой момент, выдирать фокус у печатающего пользователя
    // нельзя (жалоба владельца про «печатаю — вылезают окна»).
    public static void RestoreBackground(IntPtr h) {
        int st = GetWindowLong(h, GWL_EXSTYLE);
        SetWindowLong(h, GWL_EXSTYLE, st & ~WS_EX_NOACTIVATE & ~WS_EX_TOOLWINDOW);
        ShowWindow(h, SW_RESTORE);
        TaskbarTab.Add(h);
    }

    public static void Minimize(IntPtr h) {
        int st = GetWindowLong(h, GWL_EXSTYLE);
        SetWindowLong(h, GWL_EXSTYLE, st | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW);
        TaskbarTab.Delete(h);
        // Только SW_HIDE: SW_MINIMIZE/SW_SHOWMINNOACTIVE рисуют над панелью
        // задач «мини-плитку-призрак» с крестиком (живой замер 2026-10-16).
        ShowWindow(h, SW_HIDE);
    }

    public static void EnsureNoTaskbar(IntPtr h) {
        int st = GetWindowLong(h, GWL_EXSTYLE);
        if ((st & WS_EX_TOOLWINDOW) == 0)
            SetWindowLong(h, GWL_EXSTYLE, st | WS_EX_TOOLWINDOW);
        TaskbarTab.Delete(h);
    }

    // «Выход» = остановка сбора: Ctrl+C в консоль демона. Это его штатный
    // graceful-путь: SIGINT → INTERRUPTED, воркеры сворачиваются, прогресс
    // сохраняется; ВТОРОЙ Ctrl+C — его же форс-путь (_FORCE_EXIT). Приём:
    // трей живёт без консоли → FreeConsole; AttachConsole к демону; у себя
    // Ctrl+C игнорируем (иначе убьём трей вместе с событием);
    // GenerateConsoleCtrlEvent(0, 0) шлёт событие всем, кто привязан к этой
    // консоли, — демону и его дереву.
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool FreeConsole();
    [DllImport("kernel32.dll", SetLastError=true)] public static extern bool AttachConsole(uint dwProcessId);
    [DllImport("kernel32.dll")] public static extern bool SetConsoleCtrlHandler(IntPtr handler, bool add);
    [DllImport("kernel32.dll")] public static extern bool GenerateConsoleCtrlEvent(uint ctrlEvent, uint processGroup);

    public static bool SendCtrlC(uint pid) {
        FreeConsole();
        if (!AttachConsole(pid)) return false;
        try {
            SetConsoleCtrlHandler(IntPtr.Zero, true);
            return GenerateConsoleCtrlEvent(0, 0);
        } finally {
            FreeConsole();
        }
    }
}

// ITaskbarList pairs: live check 2026-10-16 (Win11, explorerframe.dll) —
// CLSID {56FDF344…} + IID {56FDF342…} works; the reversed pair from older
// SDK samples gives REGDB_E_CLASSNOTREG / E_NOINTERFACE. Try both.
[ComImport, Guid("56FDF344-FD6D-11d0-958A-006097C9A090")]
public class CTaskbarList { }

[ComImport, Guid("56FDF342-FD6D-11d0-958A-006097C9A090")]
public class CTaskbarListLegacy { }

[ComImport, Guid("56FDF342-FD6D-11d0-958A-006097C9A090")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface ITaskbarList {
    void HrInit();
    void AddTab(IntPtr hwnd);
    void DeleteTab(IntPtr hwnd);
    void ActivateTab(IntPtr hwnd);
    void SetActiveAlt(IntPtr hwnd);
}

[ComImport, Guid("56FDF344-FD6D-11d0-958A-006097C9A090")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface ITaskbarListLegacy {
    void HrInit();
    void AddTab(IntPtr hwnd);
    void DeleteTab(IntPtr hwnd);
    void ActivateTab(IntPtr hwnd);
    void SetActiveAlt(IntPtr hwnd);
}

public static class TaskbarTab {
    static object Create() {
        try {
            return new CTaskbarList();
        } catch {
            try {
                return new CTaskbarListLegacy();
            } catch {
                return null;
            }
        }
    }

    public static bool Add(IntPtr h) {
        var o = Create();
        if (o == null) return false;
        try {
            if (o is ITaskbarList) { var t = (ITaskbarList)o; t.HrInit(); t.AddTab(h); }
            else { var t = (ITaskbarListLegacy)o; t.HrInit(); t.AddTab(h); }
            return true;
        } catch {
            return false;
        }
    }

    public static bool Delete(IntPtr h) {
        var o = Create();
        if (o == null) return false;
        try {
            if (o is ITaskbarList) { var t = (ITaskbarList)o; t.HrInit(); t.DeleteTab(h); }
            else { var t = (ITaskbarListLegacy)o; t.HrInit(); t.DeleteTab(h); }
            return true;
        } catch {
            return false;
        }
    }
}
"@

function Get-CollectorWindows {
    $pids = @(Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*ozon-profile*" } |
        ForEach-Object { [int]$_.ProcessId })
    if ($pids.Count -eq 0) { return @() }
    return @([CollectorTrayWin]::FindByPids($pids))
}

# Флаг «пользователь смотрит окна сбора» — общий контракт с Python-сторожем
# (ozon_collector/window_policy.py: USER_VISIBLE_FLAG, user_visible): пока файл
# существует, сторож и проба окна в пайплайне пазла не прячут НИЧЕГО. Пишет
# только трей; снимает трей при «Свернуть»/«Выход» или key_daemon при старте
# сбора (залипший флаг).
$script:FlagDir = Join-Path (Split-Path -Parent $PSScriptRoot) "data"
$script:FlagPath = Join-Path $script:FlagDir "window_visible.flag"

function Set-UserVisibleFlag {
    New-Item -ItemType Directory -Path $script:FlagDir -Force | Out-Null
    New-Item -ItemType File -Path $script:FlagPath -Force | Out-Null
}

function Clear-UserVisibleFlag {
    Remove-Item -LiteralPath $script:FlagPath -Force -ErrorAction SilentlyContinue
}

function Test-UserVisibleFlag {
    return (Test-Path -LiteralPath $script:FlagPath)
}

function Send-TrayCtrlC {
    param([int]$TargetPid)
    return [CollectorTrayWin]::SendCtrlC([uint32]$TargetPid)
}

function Show-CollectorWindows {
    # Флаг ДО показа: сторож не должен успеть спрятать окно в момент показа.
    Set-UserVisibleFlag
    $wins = @(Get-CollectorWindows)
    foreach ($h in $wins) { [CollectorTrayWin]::Restore($h) }
    return $wins.Count
}

function Hide-CollectorWindows {
    $wins = @(Get-CollectorWindows)
    foreach ($h in $wins) { [CollectorTrayWin]::Minimize($h) }
    # Флаг ПОСЛЕ скрытия: пока прятали — сторож молчал.
    Clear-UserVisibleFlag
    return $wins.Count
}

function Reconcile-CollectorWindows {
    # Патруль трея — исполнитель РЕЖИМА окна (переключатель «Развернуть/
    # Свернуть», требование владельца 2026-10-16): visible — все окна сбора
    # видны; hidden — ни одно окно не видно. Режим живёт в флаге, его же
    # читает Python-сторож (window_policy.user_visible).
    # visible: новое окно (рестарт воркера) рождается скрытым — показываем
    # без кражи фокуса. hidden: самовольно показавшееся окно прячем, у
    # остальных снимаем кнопку панели задач (подстраховка от Explorer).
    $wins = @(Get-CollectorWindows)
    if (Test-UserVisibleFlag) {
        foreach ($h in $wins) {
            if (-not [CollectorTrayWin]::IsShown($h)) {
                [CollectorTrayWin]::RestoreBackground($h)
            }
        }
        return
    }
    foreach ($h in $wins) {
        if ([CollectorTrayWin]::IsWindowVisible($h) -or [CollectorTrayWin]::IsIconic($h)) {
            [CollectorTrayWin]::Minimize($h)
        } else {
            [CollectorTrayWin]::EnsureNoTaskbar($h)
        }
    }
}

if ($List) {
    $wins = @(Get-CollectorWindows)
    Write-Output ("collector windows: {0}" -f $wins.Count)
    foreach ($h in $wins) {
        $ex = [CollectorTrayWin]::ExStyle($h)
        Write-Output ("  hwnd=0x{0:X}  ex=0x{1:X8} toolwindow={2} noactivate={3}  title={4}" -f `
                [int64]$h, $ex, [bool]($ex -band 0x80), [bool]($ex -band 0x08000000), `
                [CollectorTrayWin]::Title($h))
    }
    exit 0
}
if ($Show) {
    Write-Output ("restored: {0}" -f (Show-CollectorWindows))
    exit 0
}
if ($Hide) {
    Write-Output ("minimized: {0}" -f (Hide-CollectorWindows))
    exit 0
}
if ($SendCtrlC -gt 0) {
    if (Send-TrayCtrlC -TargetPid $SendCtrlC) {
        Write-Output ("ctrl+c sent to pid {0}" -f $SendCtrlC)
        exit 0
    }
    Write-Output ("ctrl+c FAILED for pid {0} (no console attached?)" -f $SendCtrlC)
    exit 1
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$icon = [System.Drawing.SystemIcons]::Application
foreach ($c in @(
        (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
        (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe"))) {
    if (Test-Path $c) {
        try { $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($c) } catch { }
        break
    }
}

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = $icon
$notify.Text = "Ozon collector: окна сбора"
$notify.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$itemShow = $menu.Items.Add("Развернуть окна сбора")
$itemHide = $menu.Items.Add("Свернуть окна сбора")
[void]$menu.Items.Add("-")
$itemExit = $menu.Items.Add("Выход")

$itemShow.add_Click({
        $n = Show-CollectorWindows
        if ($n -eq 0) { $notify.ShowBalloonTip(2000, "ozon-collector", "Окна сбора не найдены (демон не запущен?)", [System.Windows.Forms.ToolTipIcon]::Info) }
    })
$itemHide.add_Click({
        $n = Hide-CollectorWindows
        if ($n -eq 0) { $notify.ShowBalloonTip(2000, "ozon-collector", "Окна сбора не найдены (демон не запущен?)", [System.Windows.Forms.ToolTipIcon]::Info) }
    })
function Close-Tray {
    # Иконка уходит — флаг «пользователь смотрит окна» снимаем: он живёт
    # ровно столько, сколько трей (key_daemon при старте сбора тоже
    # подстрахуется clear_user_visible).
    Clear-UserVisibleFlag
    $notify.Visible = $false
    $notify.Dispose()
    $ctx.ExitThread()
}

$script:stopStage = 0

function Stop-DaemonHard {
    Stop-Process -Id $WatchPid -Force -ErrorAction SilentlyContinue
    Close-Tray
}

function Stop-Daemon {
    # «Выход» = остановка СБОРА (требование владельца: «Выход завершает и
    # сбор»). Каскад: 1-й клик — Ctrl+C демону (graceful: прогресс
    # сохраняется, final_state=INTERRUPTED), иконка живёт до реальной смерти
    # демона (watchdog закроет трей ≤2с); 2-й клик — ещё Ctrl+C (форс-путь
    # демона, _FORCE_EXIT); 3-й — Stop-Process -Force (воркеры сами снимут
    # браузеры через parent watchdog). Ctrl+C послать некуда (у демона нет
    # консоли / ошибка) — жёсткий путь сразу.
    if ($WatchPid -le 0) { Close-Tray; return }   # отладочный режим без демона
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $WatchPid" `
        -ErrorAction SilentlyContinue
    if (-not $proc -or $proc.CommandLine -notlike "*key_daemon*") {
        Close-Tray                                # демона уже нет
        return
    }
    $script:stopStage++
    if ($script:stopStage -eq 1) {
        $notify.ShowBalloonTip(3000, "ozon-collector",
            "Останавливаю сбор… Прогресс сохранится. Повторное «Выход» — принудительно.",
            [System.Windows.Forms.ToolTipIcon]::Info)
        if (-not (Send-TrayCtrlC -TargetPid $WatchPid)) { Stop-DaemonHard }
        return
    }
    if ($script:stopStage -eq 2) {
        $notify.ShowBalloonTip(3000, "ozon-collector",
            "Принудительная остановка…",
            [System.Windows.Forms.ToolTipIcon]::Info)
        if (-not (Send-TrayCtrlC -TargetPid $WatchPid)) { Stop-DaemonHard }
        return
    }
    Stop-DaemonHard
}

$itemExit.add_Click({ Stop-Daemon })
$notify.ContextMenuStrip = $menu
$notify.add_MouseDoubleClick({ [void](Show-CollectorWindows) })

$ctx = New-Object System.Windows.Forms.ApplicationContext

# Патруль панели задач каждые 2с: кнопки окон сбора не должны существовать.
$reconcile = New-Object System.Windows.Forms.Timer
$reconcile.Interval = 2000
$reconcile.add_Tick({ Reconcile-CollectorWindows })
$reconcile.Start()

if ($WatchPid -gt 0) {
    # Watchdog: трей живёт ровно столько, сколько демон. Проверка по cmdline,
    # а не только по pid, — защита от переиспользования PID (identity-подход).
    $watch = New-Object System.Windows.Forms.Timer
    $watch.Interval = 2000
    $watch.add_Tick({
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $WatchPid" `
                -ErrorAction SilentlyContinue
            if (-not $proc -or $proc.CommandLine -notlike "*key_daemon*") {
                $watch.Stop()
                Close-Tray
            }
        })
    $watch.Start()
}

# Стартовый balloon убран (живой отзыв владельца 2026-10-16: «плитка над
# панелью задач до сих пор появляется»): Win11 рисует всплывающее уведомление
# тостом над панелью и подсвечивает иконку при каждом запуске сбора. Тост о
# «окна не найдены» остаётся — он только по явному клику в меню.
[System.Windows.Forms.Application]::Run($ctx)
