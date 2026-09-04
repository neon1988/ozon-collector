#!/usr/bin/env pwsh
# Запуск живого Chromium для CDP-сбора Ozon (Windows).
# НЕ передавать --enable-automation (иначе navigator.webdriver === true → антибот).
# Использование: powershell -ExecutionPolicy Bypass -File scripts/launch_chrome.ps1
# Переменные: PORT (по умолч. 9222), PROFILE (по умолч. %LOCALAPPDATA%\opencode\ozon-profile),
#             CHROME (по умолч. ищет ms-playwright chromium → системный Chrome).
# Скрипт запускает браузер и сразу завершается; браузер живёт сам по себе.
param(
    [string]$Port = $env:PORT,
    [string]$Profile = $env:PROFILE,
    [string]$Chrome = $env:CHROME
)

if (-not $Port) { $Port = "9222" }
if (-not $Profile) { $Profile = Join-Path $env:LOCALAPPDATA "opencode\ozon-profile" }

function Find-Chrome {
    # 1) Системный Chrome (реальный отпечаток браузера) — Ozon не блочит.
    $candidates = @(
        (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
        (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    # 2) Playwright chromium (запасной — отпечаток чаще блочится Ozon)
    $pwDirs = @(
        (Join-Path $env:LOCALAPPDATA "ms-playwright\chromium-*\chrome-win64\chrome.exe"),
        (Join-Path $env:LOCALAPPDATA "ms-playwright\chromium-*\chrome-win\chrome.exe")
    )
    foreach ($pat in $pwDirs) {
        $hit = Get-ChildItem $pat -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

if (-not $Chrome) { $Chrome = Find-Chrome }
if (-not $Chrome) {
    Write-Host "Chromium не найден. Установи: python -m playwright install chromium" -ForegroundColor Red
    exit 1
}

# Если на этом профиле уже запущен Chromium, он получит команду и сам выйдет —
# тогда полагаемся на уже запущенный инстанс.
New-Item -ItemType Directory -Force -Path $Profile | Out-Null

# Маркер запуска коллектором (ADR §14.1, ownership): Session по нему отличает
# наш запуск (browser_owner=collector) от ручного/чужого (external).
Set-Content -Path (Join-Path $Profile ".ozon_collector_launched") `
    -Value (Get-Date -Format "o") -Encoding UTF8 | Out-Null

$chromeArgs = @(
    "--remote-debugging-port=$Port",
    "--user-data-dir=$Profile",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=OptimizationHints,MediaRouter",
    "--window-size=1440,900",
    # Не показывать баббл «Восстановить страницы?» после насильственного
    # завершения Chrome (kill коллектором) и инфобар WebDriver — они мешают.
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    # Маскировка автоматизации: прячем признаки webdriver/automation от антибота.
    # НЕ ставим --no-sandbox (сильный сигнал automation; системному Chrome не нужен).
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "about:blank"
)

# Запуск через CreateProcess со STARTUPINFO.wShowWindow=SW_HIDE (0):
# окно создаётся СКРЫТЫМ и физически не может активироваться/украсть фокус,
# даже если Chrome при старте пытается показать себя. После полной готовности
# CDP (конец скрипта) показываем окно свёрнутым без активации (SW_SHOWMINNOACTIVE).
# В отличие от Start-Process -WindowStyle Minimized (SW_SHOWMINIMIZED), который
# АКТИВИРУЕТ окно на ~30-100мс и крадёт фокус у пользователя (прерывает печать).
# Весь вызов — внутри C#-метода: маршалинг структуры STARTUPINFO через
# [ref] из PowerShell возвращал Win32Error 123 (ERROR_INVALID_NAME).
Add-Type @"
using System;
using System.Runtime.InteropServices;

public class ChromeLaunch {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct STARTUPINFO {
        public int cb;
        public string lpReserved;
        public string lpDesktop;
        public string lpTitle;
        public int dwX, dwY, dwXSize, dwYSize;
        public int dwXCountChars, dwYCountChars, dwFillAttribute;
        public int dwFlags;
        public short wShowWindow;
        public short cbReserved2;
        public IntPtr lpReserved2;
        public IntPtr hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct PROCESS_INFORMATION {
        public IntPtr hProcess;
        public IntPtr hThread;
        public int dwProcessId;
        public int dwThreadId;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CreateProcessW(
        string lpApplicationName, string lpCommandLine,
        IntPtr lpProcessAttributes, IntPtr lpThreadAttributes,
        bool bInheritHandles, uint dwCreationFlags,
        IntPtr lpEnvironment, string lpCurrentDirectory,
        ref STARTUPINFO lpStartupInfo,
        out PROCESS_INFORMATION lpProcessInformation);

    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr hObject);

    public const int STARTF_USESHOWWINDOW = 0x00000001;
    public const short SW_HIDE = 0;
    public const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;

    public static int Launch(string commandLine) {
        STARTUPINFO si = new STARTUPINFO();
        si.cb = Marshal.SizeOf(typeof(STARTUPINFO));
        si.dwFlags = STARTF_USESHOWWINDOW;
        si.wShowWindow = SW_HIDE;
        PROCESS_INFORMATION pi;
        bool ok = CreateProcessW(null, commandLine, IntPtr.Zero, IntPtr.Zero, false,
            CREATE_UNICODE_ENVIRONMENT, IntPtr.Zero, null, ref si, out pi);
        if (!ok) {
            return -Marshal.GetLastWin32Error();
        }
        CloseHandle(pi.hProcess);
        CloseHandle(pi.hThread);
        return pi.dwProcessId;
    }
}
"@

# Полная командная строка: chrome.exe + аргументы (CreateProcessW, Unicode).
$cmdLine = "`"$Chrome`" " + ($chromeArgs -join " ")
$targetPid = [ChromeLaunch]::Launch($cmdLine)
if ($targetPid -lt 0) {
    Write-Host "Не удалось запустить Chromium (CreateProcess, Win32Error=$(-$targetPid))" -ForegroundColor Red
    exit 1
}

# Показываем главное окно запущенного инстанса свёрнутым без активации:
# SW_SHOWMINNOACTIVE показывает сразу свёрнутым (без мелькания), без кражи фокуса.
# Фильтр по PID запущенного процесса — окна обычного Chrome не трогаем.
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public class LaunchChromeMin {
    public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder sb, int max);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    public static IntPtr FindMainWindow(int pid) {
        IntPtr found = IntPtr.Zero;
        EnumWindows(delegate(IntPtr h, IntPtr lp) {
            uint p; GetWindowThreadProcessId(h, out p);
            if (p != (uint)pid) return true;
            StringBuilder cls = new StringBuilder(256);
            GetClassName(h, cls, 256);
            if (cls.ToString() == "Chrome_WidgetWin_1") {
                found = h;
                return false;
            }
            return true;
        }, IntPtr.Zero);
        return found;
    }

    [DllImport("user32.dll")] public static extern bool SetWindowPos(
        IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);

    // Прижать окно поверх всех (HWND_TOPMOST), не активируя (SWP_NOACTIVATE).
    public static bool SetTopmost(IntPtr hWnd) {
        return SetWindowPos(hWnd, new IntPtr(-1), 0, 0, 0, 0,
                            0x0002 | 0x0001 | 0x0010);  // NOMOVE|NOSIZE|NOACTIVATE
    }
}
"@

# Запоминаем окно, которое было в фокусе ДО запуска — вернём ему фокус после
# (Chrome при старте сам вызывает SetForegroundWindow и крадёт фокус).
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class FocusSave {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
}
"@
$prevFg = [FocusSave]::GetForegroundWindow()

function Invoke-MinimizeNoActivate($PidToWatch, $WaitMs, [bool]$ShowWindow = $true) {
    # Показывает окно БЕЗ активации и прижимает поверх всех (HWND_TOPMOST):
    # окно свёрнутое и не перехватывает фокус. SW_SHOWMINNOACTIVE показывает
    # сразу свёрнутым без промежуточного разворачивания (без мелькания).
    # WS_EX_NOACTIVATE НЕ ставится — окно остаётся обычным (taskbar работает).
    # $ShowWindow=$false — только ждём появления окна, не показывая.
    $deadline = (Get-Date).AddMilliseconds($WaitMs)
    while ((Get-Date) -lt $deadline) {
        $win = Get-Process -Id $PidToWatch -ErrorAction SilentlyContinue
        $h = if ($win) { [LaunchChromeMin]::FindMainWindow($PidToWatch) } else { [IntPtr]::Zero }
        if ($h -ne [IntPtr]::Zero) {
            if ($ShowWindow) {
                [LaunchChromeMin]::ShowWindow($h, 7) | Out-Null          # SW_SHOWMINNOACTIVE (свёрнутое, без активации)
                [void][LaunchChromeMin]::SetTopmost($h)                  # поверх всех, без активации
            }
            return $true
        }
        Start-Sleep -Milliseconds 30
    }
    return $false
}

# Возвращаем фокус окну, которое было активным до запуска Chromium
# (Chrome сам вызывает SetForegroundWindow при старте — фокус крадётся даже
# у свёрнутого окна). SetForegroundWindow не сработает, если вызывающий
# процесс не находится на переднем плане, поэтому применяем трюк с
# AttachThreadInput: привязываем НАШ поток ввода к foreground-потоку chrome,
# тогда вызов SetForegroundWindow из нашего потока разрешён системой.
function Restore-Focus {
    param([IntPtr]$PrevFg, [bool]$Verbose = $true)
    if ($PrevFg -eq [IntPtr]::Zero) { return }
    $curFg = [FocusSave]::GetForegroundWindow()
    if ($curFg -eq $PrevFg) { return }
    $selfThread = [FocusSave]::GetCurrentThreadId()
    $curThread = 0; [FocusSave]::GetWindowThreadProcessId($curFg, [ref]$curThread) | Out-Null
    [FocusSave]::AttachThreadInput($selfThread, $curThread, $true) | Out-Null
    $restored = [FocusSave]::SetForegroundWindow($PrevFg)
    [FocusSave]::AttachThreadInput($selfThread, $curThread, $false) | Out-Null
    if ($Verbose) {
        if ($restored) {
            Write-Host "OK: фокус возвращён предыдущему окну"
        } else {
            Write-Warning "не удалось вернуть фокус предыдущему окну (SetForegroundWindow=$restored)"
        }
    }
}

# Первый проход: находим окно как можно раньше (оно скрыто с рождения — SW_HIDE),
# НЕ показывая его.
$minimized = Invoke-MinimizeNoActivate $targetPid 10000 $false

# Ждём, пока CDP-порт ответит: к этому моменту Chrome закончил старт и
# свои попытки активации окна (мелькание).
$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $ver = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2
        $ready = $true
        Write-Host "OK: $($ver.Browser) на порту $Port" -ForegroundColor Green
        break
    } catch { }
}
if (-not $ready) {
    Write-Host "ждём... проверьте http://127.0.0.1:$Port/json/version"
}

# Контрольные проходы ПОСЛЕ полного старта: показываем окно свёрнутым
# без активации (SW_SHOWMINNOACTIVE) и прижимаем поверх всех (HWND_TOPMOST),
# затем возвращаем фокус. Окно ВИДИМО свёрнутым, фокус не крадёт.
if (-not $minimized) {
    $minimized = Invoke-MinimizeNoActivate $targetPid 3000 $false
}
for ($i = 0; $i -lt 3; $i++) {
    [void](Invoke-MinimizeNoActivate $targetPid 300 $true)
    Restore-Focus -PrevFg $prevFg -Verbose ($i -eq 0)
}
if ($minimized) {
    Write-Host "OK: окно PID $targetPid показано поверх всех (TOPMOST), фокус не крадётся"
} else {
    Write-Warning "не удалось найти окно PID $targetPid для показа"
}
