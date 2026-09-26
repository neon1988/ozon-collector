#!/usr/bin/env pwsh
# Запуск живого Chromium для CDP-сбора Ozon (Windows).
# НЕ передавать --enable-automation (иначе navigator.webdriver === true → антибот).
# Использование: powershell -ExecutionPolicy Bypass -File scripts/launch_chrome.ps1
# Переменные: PORT (по умолч. 9222), PROFILE (по умолч. %LOCALAPPDATA%\opencode\ozon-profile),
#             CHROME (по умолч. ищет ms-playwright chromium → системный Chrome),
#             PROXY (ревизия 2.9: per-worker прокси; пусто → напрямую).
# Скрипт запускает браузер и сразу завершается; браузер живёт сам по себе.
param(
    [string]$Port = $env:PORT,
    [string]$Profile = $env:PROFILE,
    [string]$Chrome = $env:CHROME,
    [string]$Proxy = $env:PROXY
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
    # Свежая ревизия важнее старой: при нескольких установленных сборках
    # берём максимальный числовой хвост каталога (chromium-1234), иначе
    # Get-ChildItem отдаёт произвольную (часто старую) ревизию.
    foreach ($pat in $pwDirs) {
        $hit = Get-ChildItem $pat -ErrorAction SilentlyContinue |
            Sort-Object { [int]([regex]::Match($_.Directory.Name, '\d+').Value) } `
                      -Descending |
            Select-Object -First 1
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
    # Язык интерфейса и Accept-Language — ru-RU независимо от локали машины и
    # профиля. Система сейчас ru-RU, поэтому поведение не меняется, но при
    # смене локали ОС браузер не уедет в en-US (антибот это видит).
    "--lang=ru-RU",
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

# Per-worker прокси (ревизия 2.9, ADR §18.7): пусто → напрямую, лишнего флага
# не будет (пустые элементы фильтруем при сборке командной строки).
# ВНИМАНИЕ: аргумент виден в CommandLine процесса (WMI/CIM) — не кладите
# пароль прокси в общий доступ. Для socks5 с кредами нужен PAC/расширение:
# Chrome игнорирует логин/пароль из URL для socks5 (предупреждает cdp.py).
$proxyArg = if ($Proxy) { "--proxy-server=$Proxy" } else { $null }

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

# --- Гейт: порт ДОЛЖЕН быть полностью свободен ------------------------------
# Проверяем НЕ ТОЛЬКО HTTP-ответ /json/version, но и сам TCP-listener. Ловушка,
# найденная в бою: порт был занят посторонним процессом, /json/version не
# отвечал (гейт считал порт свободным), Chrome стартовал, НО не смог привязать
# DevTools к 127.0.0.1 и тихо ушёл в [::1] — CDP там живой, но коллектор
# (который ходит на 127.0.0.1) «не видит» браузера, запускает новый, а тот
# умирает о занятый профиль. Три ретрая в никуда вместо одного честного отказа.
# Плюс чужой/старый Chromium на этом порту даёт ложный "OK" на /json/version.
$listener = $null
try {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port `
                  -ErrorAction Stop | Select-Object -First 1
} catch { }
$httpBusy = $false
try {
    $null = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2
    $httpBusy = $true
} catch { }
if ($listener -or $httpBusy) {
    $lp = if ($listener) { $listener.OwningProcess } else { "?" }
    $lname = try { (Get-Process -Id $lp -ErrorAction Stop).ProcessName } catch { "?" }
    Write-Host "LAUNCHER-ERROR: port $Port is NOT free (listener pid=$lp $lname, /json/version responds=$httpBusy) - refusing to start" -ForegroundColor Red
    Write-Host "LAUNCHER-ERROR: Chrome would bind [::1] instead and go invisible to CDP on 127.0.0.1" -ForegroundColor Red
    Write-Host "LAUNCHER-ERROR: chrome=$Chrome profile=$Profile" -ForegroundColor Red
    exit 1
}

# Полная командная строка: chrome.exe + аргументы (CreateProcessW, Unicode).
$allArgs = @($chromeArgs) + @($proxyArg)
$cmdLine = "`"$Chrome`" " + (($allArgs | Where-Object { $_ }) -join " ")
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
    [DllImport("user32.dll", SetLastError=true)] public static extern int SetWindowLong(IntPtr hWnd, int nIndex, int dwNewLong);
    [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr hWnd, int nIndex);

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

$GWL_EXSTYLE = -20
$WS_EX_NOACTIVATE = 0x08000000

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
    # окно ВИДИМО и не перехватывает фокус. SW_RESTORE выставляет WS_VISIBLE
    # (иначе окно остаётся «видимым» для EnumWindows, но не на экране).
    # $ShowWindow=$false — только ставим WS_EX_NOACTIVATE, не показывая окно.
    $deadline = (Get-Date).AddMilliseconds($WaitMs)
    while ((Get-Date) -lt $deadline) {
        $win = Get-Process -Id $PidToWatch -ErrorAction SilentlyContinue
        if (-not $win) {
            # Процесса нет — окна уже не будет. Раньше мы здесь молча ждали весь
            # таймаут (10s), из-за чего вывод лаунчера приходил позже, чем
            # Python успевал прочитать лог: диагностика выглядела как "пусто".
            return $false
        }
        $h = if ($win) { [LaunchChromeMin]::FindMainWindow($PidToWatch) } else { [IntPtr]::Zero }
        if ($h -ne [IntPtr]::Zero) {
            $style = [LaunchChromeMin]::GetWindowLong($h, $GWL_EXSTYLE)
            [LaunchChromeMin]::SetWindowLong($h, $GWL_EXSTYLE, $style -bor $WS_EX_NOACTIVATE) | Out-Null
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

# Первый проход: находим окно и ставим WS_EX_NOACTIVATE как можно раньше,
# НЕ показывая его (оно скрыто с рождения — SW_HIDE).
$minimized = Invoke-MinimizeNoActivate $targetPid 10000 $false

# Ждём, пока CDP-порт ответит: к этому моменту Chrome закончил старт и
# свои попытки активации окна (мелькание).
$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 500
    if (-not (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) {
        break        # Chromium умер (например, профиль уже открыт) — ждать порт бессмысленно
    }
    try {
        $ver = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2
        $ready = $true
        Write-Host "OK: $($ver.Browser) на порту $Port" -ForegroundColor Green
        break
    } catch { }
}
# Прокси маскируем (в выводе лаунчера не должно быть пароля).
$proxyNote = if ($Proxy) {
    ($Proxy -replace '://([^:@/]+):[^@/]+@', '://$1:***@')
} else { "<прямое подключение>" }

# Диагностика для коллектора: при неудаче _launch_browser печатает этот вывод.
# Маркеры LAUNCHER-* — ASCII: кириллица в перенаправленном выводе PowerShell
# едет через кодировку консоли и в логе нечитаема.
if (-not $ready) {
    $alive = $null -ne (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)
    # Кто слушает порт СЕЙЧАС: разделяем «порт занят чужим» (наш Chrome не смог
    # привязать DevTools) и «порт свободен» (профиль захвачен другим
    # экземпляром — наш pid умер, уйдя в него). Раньше обе причины выглядели
    # одинаково («EXITED, CDP never came up») и диагностика уводила не туда.
    $listenerPid = $null
    try {
        $listenerPid = (Get-NetTCPConnection -State Listen -LocalPort $Port `
                       -ErrorAction SilentlyContinue |
                       Select-Object -First 1).OwningProcess
    } catch { }
    if ($alive) {
        Write-Host "LAUNCHER-ERROR: Chromium pid=$targetPid is ALIVE but http://127.0.0.1:$Port/json/version is silent" -ForegroundColor Red
        Write-Host "LAUNCHER-ERROR: port $Port taken by another process? chrome=$Chrome profile=$Profile" -ForegroundColor Red
    } else {
        Write-Host "LAUNCHER-ERROR: Chromium pid=$targetPid EXITED, CDP never came up on port $Port" -ForegroundColor Red
        if ($listenerPid) {
            Write-Host "LAUNCHER-ERROR: port $Port IS LISTENING by pid=$listenerPid (not CDP) - our Chromium could not bind DevTools" -ForegroundColor Red
        } else {
            Write-Host "LAUNCHER-ERROR: port $Port is free - Chromium died for another reason (profile captured by a live instance is the usual one)" -ForegroundColor Red
        }
        Write-Host "LAUNCHER-ERROR: profile already open in another Chrome? profile=$Profile" -ForegroundColor Red
        Write-Host "LAUNCHER-ERROR: need own profile per campaign (--user-data-dir) and a free port" -ForegroundColor Red
    }
    Write-Host "LAUNCHER-DIAG: chrome=$Chrome profile=$Profile port=$Port proxy=$proxyNote pid=$targetPid"
    exit 1
}

# Контрольные проходы ПОСЛЕ полного старта: повторно применяем стиль
# WS_EX_NOACTIVATE, показываем окно (SW_RESTORE) и прижимаем поверх всех
# (HWND_TOPMOST), затем возвращаем фокус. Окно ВИДИМО всегда, фокус не крадёт.
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
Write-Host "LAUNCHER-DIAG: chrome=$Chrome profile=$Profile port=$Port proxy=$proxyNote pid=$targetPid"
exit 0
