#!/usr/bin/env pwsh
# Tray companion for ozon-collector Chromium windows (Windows, pwsh 7+).
#
# Usage:
#   pwsh -NoProfile -File scripts/collector_tray.ps1        # tray icon, resident
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

param(
    [switch]$List,
    [switch]$Show,
    [switch]$Hide
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
    public const int SW_MINIMIZE = 6;

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
        ShowWindow(h, SW_MINIMIZE);
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
$itemExit.add_Click({
        $notify.Visible = $false
        $notify.Dispose()
        $ctx.ExitThread()
    })
$notify.ContextMenuStrip = $menu
$notify.add_MouseDoubleClick({ [void](Show-CollectorWindows) })

$ctx = New-Object System.Windows.Forms.ApplicationContext
$notify.ShowBalloonTip(3000, "ozon-collector", "Иконка окон сбора активна: правый клик — меню", [System.Windows.Forms.ToolTipIcon]::Info)
[System.Windows.Forms.Application]::Run($ctx)
