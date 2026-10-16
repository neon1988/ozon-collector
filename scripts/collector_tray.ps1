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
#   pwsh -NoProfile -File scripts/collector_tray.ps1 -Hide  # minimize collector windows once
#
# Finds ONLY collector Chromium windows: chrome.exe whose command line contains
# --user-data-dir with "ozon-profile" token (main + worker-N profiles). Foreign
# Chrome windows of the user are never touched.
#
# Collector windows carry WS_EX_TOOLWINDOW (no taskbar button, not in Alt+Tab);
# Restore clears WS_EX_NOACTIVATE so the user can interact with the page and
# keeps WS_EX_TOOLWINDOW so the button does not come back; the launcher and
# cdp.Session re-add NOACTIVATE on every collector minimize.
#
# Minimize also calls ITaskbarList::DeleteTab + SW_HIDE/SW_SHOWMINNOACTIVE:
# changing WS_EX_TOOLWINDOW alone does NOT remove a taskbar button that was
# already created at window birth — Explorer must re-create the state (live
# finding 2026-10-16: styles were on, buttons stayed). A reconcile timer
# (every 2s) additionally deletes any button that reappears while the tray
# runs, so collector windows can never sit on the taskbar (owner's demand:
# windows are restored from the tray icon, not from the taskbar).

param(
    [switch]$List,
    [switch]$Show,
    [switch]$Hide,
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
    [DllImport("user32.dll", SetLastError=true)] public static extern int GetWindowLong(IntPtr hWnd, int idx);
    [DllImport("user32.dll", SetLastError=true)] public static extern int SetWindowLong(IntPtr hWnd, int idx, int val);

    public const int GWL_EXSTYLE = -20;
    public const int WS_EX_NOACTIVATE = 0x08000000;
    public const int WS_EX_TOOLWINDOW = 0x00000080;
    public const int SW_RESTORE = 9;
    public const int SW_HIDE = 0;
    public const int SW_SHOWMINNOACTIVE = 7;

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

    public static void Restore(IntPtr h) {
        int st = GetWindowLong(h, GWL_EXSTYLE);
        SetWindowLong(h, GWL_EXSTYLE, (st & ~WS_EX_NOACTIVATE) | WS_EX_TOOLWINDOW);
        ShowWindow(h, SW_RESTORE);
        SetForegroundWindow(h);
    }

    public static void Minimize(IntPtr h) {
        int st = GetWindowLong(h, GWL_EXSTYLE);
        SetWindowLong(h, GWL_EXSTYLE, st | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW);
        TaskbarTab.Delete(h);
        ShowWindow(h, SW_HIDE);
        ShowWindow(h, SW_SHOWMINNOACTIVE);
    }

    public static void EnsureNoTaskbar(IntPtr h) {
        int st = GetWindowLong(h, GWL_EXSTYLE);
        if ((st & WS_EX_TOOLWINDOW) == 0)
            SetWindowLong(h, GWL_EXSTYLE, st | WS_EX_TOOLWINDOW);
        TaskbarTab.Delete(h);
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
    public static bool Delete(IntPtr h) {
        object o;
        try {
            o = new CTaskbarList();
        } catch {
            try {
                o = new CTaskbarListLegacy();
            } catch {
                return false;
            }
        }
        try {
            if (o is ITaskbarList) {
                var t = (ITaskbarList)o;
                t.HrInit();
                t.DeleteTab(h);
            } else {
                var t = (ITaskbarListLegacy)o;
                t.HrInit();
                t.DeleteTab(h);
            }
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

function Show-CollectorWindows {
    $wins = @(Get-CollectorWindows)
    foreach ($h in $wins) { [CollectorTrayWin]::Restore($h) }
    return $wins.Count
}

function Hide-CollectorWindows {
    $wins = @(Get-CollectorWindows)
    foreach ($h in $wins) { [CollectorTrayWin]::Minimize($h) }
    return $wins.Count
}

function Reconcile-CollectorWindows {
    # Патруль панели задач: даже если окно создало кнопку в обход лаунчера
    # (или её пересоздал Explorer/Chromium), снимаем её в течение пары секунд.
    # Живой отзыв владельца 2026-10-16: «мини-плитки» на панели при сборе.
    $wins = @(Get-CollectorWindows)
    foreach ($h in $wins) { [CollectorTrayWin]::EnsureNoTaskbar($h) }
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
    $notify.Visible = $false
    $notify.Dispose()
    $ctx.ExitThread()
}

$itemExit.add_Click({ Close-Tray })
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
