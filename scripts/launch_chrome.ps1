#!/usr/bin/env pwsh
# Запуск живого Chromium для CDP-сбора Ozon (Windows).
# НЕ передавать --enable-automation (иначе navigator.webdriver === true → антибот).
# Использование: powershell -ExecutionPolicy Bypass -File scripts/launch_chrome.ps1
# Переменные: PORT (по умолч. 9222), PROFILE (по умолч. %LOCALAPPDATA%\opencode\ozon-profile),
#             CHROME (по умолч. ищет ms-playwright chromium → системный Chrome),
#             PROXY (ревизия 2.9: per-worker прокси; пусто → напрямую),
#             DISK_CACHE_MB (env OZON_DISK_CACHE_MB, по умолч. 100; 0 = выключить).
# Скрипт запускает браузер и сразу завершается; браузер живёт сам по себе.
param(
    [string]$Port = $env:PORT,
    [string]$Profile = $env:PROFILE,
    [string]$Chrome = $env:CHROME,
    [string]$Proxy = $env:PROXY,
    [int]$DiskCacheMb = 0
)

if (-not $Port) { $Port = "9222" }
if (-not $Profile) { $Profile = Join-Path $env:LOCALAPPDATA "opencode\ozon-profile" }

# Browser window size (Chromium model + --window-size). The parked off-screen
# position is computed from it, so the two must stay one constant.
$winW = 1440
$winH = 900

# Потолок дискового кеша Chromium. Живой замер 2026-10-07: профиль озона
# раздувался до 880 МБ, из них Default/Cache 448 МБ + Code Cache 138 МБ — 92%
# мусора, воспроизводимого заново; состояние сессии (Cookies/Local Storage/
# Sessions) — 79 МБ на 9 профилей. Флаг в килобайтах, 0 = потолка нет.
# Антибот: флаг не виден странице (не JS-наблюдаемый), влияет только на
# eviction кеша, поэтому отпечаток страницы не меняется.
if ($DiskCacheMb -le 0 -and $env:OZON_DISK_CACHE_MB) {
    [int]$parsed = 0
    if ([int]::TryParse($env:OZON_DISK_CACHE_MB, [ref]$parsed) -and $parsed -gt 0) {
        $DiskCacheMb = $parsed
    } else {
        Write-Host "LAUNCHER-WARN: OZON_DISK_CACHE_MB is not a positive number - disk cache cap not set" -ForegroundColor Yellow
    }
}
if ($DiskCacheMb -le 0) { $DiskCacheMb = 100 }

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

# Маркер запуска коллектором пишется ПОСЛЕ CreateProcess (см. ниже): запись до
# port-gate оставляла «наш» маркер после отказа лаунчера, и ручной Chrome на
# том же профиле ошибочно считался collector (review 2026-10-20).

$chromeArgs = @(
    "--remote-debugging-port=$Port",
    "--user-data-dir=`"$Profile`"",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=OptimizationHints,MediaRouter",
    # Язык интерфейса и Accept-Language — ru-RU независимо от локали машины и
    # профиля. Система сейчас ru-RU, поэтому поведение не меняется, но при
    # смене локали ОС браузер не уедет в en-US (антибот это видит).
    "--lang=ru-RU",
    "--window-size=$winW,$winH",
    # Не показывать баббл «Восстановить страницы?» после насильственного
    # завершения Chrome (kill коллектором) и инфобар WebDriver — они мешают.
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    # Маскировка автоматизации: прячем признаки webdriver/automation от антибота.
    # НЕ ставим --no-sandbox (сильный сигнал automation; системному Chrome не нужен).
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    # Permission-промпты («www.ozon.ru запрашивает разрешение на: …») Chrome
    # рисует ОТДЕЛЬНЫМ ВИДИМЫМ окном-бабблом, даже когда главное окно скрыто.
    # Живой замер 2026-10-16: бабл появляется через ~2с после загрузки страницы
    # (vis=True noact=False tool=True rect=1218,136,1552,442) и висит до
    # закрытия — для владельца это «плитка» поверх его окон. Флаг авто-
    # отклоняет запросы разрешений, UI не создаётся. Побочный эффект:
    # Notification.permission === 'denied' (JS-наблюдаемо) — как у живого
    # пользователя, нажавшего «блокировать»; сбору разрешения не нужны.
    "--deny-permission-prompts",
    # Потолок дискового кеша (МБ → килобайты для Chromium): 8 воркеров × 100 МБ
    # вместо × 700 МБ. Набивать кеш бессмысленно — коллектор читает DOM/CDP,
    # а не кеш, и на прогоне решается всё равно кеш-мисс.
    "--disk-cache-size=$($DiskCacheMb * 1024)",
    "about:blank"
)

# Per-worker прокси (ревизия 2.9, ADR §18.7): пусто → напрямую, лишнего флага
# не будет (пустые элементы фильтруем при сборке командной строки).
# ВНИМАНИЕ: аргумент виден в CommandLine процесса (WMI/CIM) — не кладите
# пароль прокси в общий доступ. Для socks5 с кредами нужен PAC/расширение:
# Chrome игнорирует логин/пароль из URL для socks5 (предупреждает cdp.py).
$proxyArg = if ($Proxy) { "--proxy-server=`"$Proxy`"" } else { $null }

# Запуск через CreateProcess со STARTUPINFO.wShowWindow=SW_HIDE (0):
# окно создаётся СКРЫТЫМ и физически не может активироваться/украсть фокус,
# даже если Chrome при старте пытается показать себя. К скрытому окну
# применяется WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW; показывать его (даже
# свёрнутым) нельзя: SW_SHOWMINNOACTIVE рисует над панелью задач
# мини-плитку-призрак с крестиком, а свёрнутое окно перестаёт отдавать кадры
# (живой замер 2026-10-16). Окно показывает трей («Развернуть окна сбора»)
# или ensure_render_window при необходимости.
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

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

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

# Классы и константы стилей КОМПИЛИРУЮТСЯ ЗДЕСЬ — ДО CreateProcess.
# Раньше первый Add-Type стоял ПОСЛЕ старта Chrome и его компиляция
# (~1-3 с вместе с загрузкой PS-профиля) откладывала первый проход
# скрытия: показанное Chrome в эту щель стартовое окно жило секундами
# (живой замер 2026-10-16: пустое тёмное окно с одним крестиком, без кнопки
# панели задач — показано ДО того, как первый проход вообще начался).
# Теперь опрос окна стартует через миллисекунды после CreateProcess.
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
    [DllImport("user32.dll")] public static extern bool GetWindowRect(
        IntPtr hWnd, out RECT r);
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int i);

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int L, T, R, B; }

    // Прижать окно поверх всех (HWND_TOPMOST), не активируя (SWP_NOACTIVATE).
    public static bool SetTopmost(IntPtr hWnd) {
        return SetWindowPos(hWnd, new IntPtr(-1), 0, 0, 0, 0,
                            0x0002 | 0x0001 | 0x0010);  // NOMOVE|NOSIZE|NOACTIVATE
    }

    // Увести СКРЫТОЕ окно за край виртуального рабочего стола. Chrome
    // показывает окно сам (старт/новая вкладка/пазл), и пере-скрытие опаздывает
    // на миллисекунды — окно успевает мигнуть серым пустым прямоугольником
    // (живой замер 2026-10-18: show->hide 30 мс). За краем экрана его не видно
    // даже в этот миг; размер и z-порядок не меняются. CDP-модель следует за
    // позицией, скриншоты/visibility не страдают (живой стенд).
    public static bool ParkOffscreen(IntPtr hWnd) {
        RECT r;
        if (!GetWindowRect(hWnd, out r)) return false;
        int sw = GetSystemMetrics(78), sh = GetSystemMetrics(79);  // CXV/CYV
        int sx = GetSystemMetrics(76), sy = GetSystemMetrics(77);  // XV/XYV
        if (sw <= 0 || sh <= 0) return false;
        int w = r.R - r.L, h = r.B - r.T;
        if (r.R <= sx || r.L >= sx + sw || r.B <= sy || r.T >= sy + sh) return false;
        return SetWindowPos(hWnd, IntPtr.Zero, sx - w - 100, sy + 100, 0, 0,
                            0x0001 | 0x0004 | 0x0010);  // NOSIZE|NOZORDER|NOACTIVATE
    }
}
"@

$GWL_EXSTYLE = -20
$WS_EX_NOACTIVATE = 0x08000000
# WS_EX_TOOLWINDOW: окно-«инструмент» — Windows не рисует его на панели задач
# и не включает в Alt+Tab. Ставим вместе с NOACTIVATE: окна коллектора не
# мешают пользователю, разворачивать их — через scripts/collector_tray.ps1
# (или сам коллектор под пазл). Chrome может пере-навешивать свои стили —
# ниже стиль переприменяется в контрольных проходах.
$WS_EX_TOOLWINDOW = 0x00000080

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

# Окно РОЖДАЕТСЯ за краем виртуального рабочего стола. Chrome сначала создаёт
# окно с дефолтной позицией, и только потом применяет --window-position; первый
# проход лаунчера паркует его через миллисекунды, но в этой щели окно может
# показаться. Живой замер 2026-10-18 (опрос 5 мс): окно скрыто на экране в
# 140 мс, переехало за экран в 250 мс, первый показ в 343 мс — уже за экраном;
# ни одного видимого кадра на экране. Позиция — по границам всех мониторов;
# страховка снизу — ParkOffscreen после каждого скрытия, а любой штатный показ
# возвращает окно на экран (window_policy.bring_onscreen, трей — BringOnScreen).
$virtW = [LaunchChromeMin]::GetSystemMetrics(78)
if ($virtW -le 0) {
    $parkX = -3000 - $winW
    $parkY = 100
} else {
    $parkX = [LaunchChromeMin]::GetSystemMetrics(76) - $winW - 100
    $parkY = [LaunchChromeMin]::GetSystemMetrics(77) + 100
}
$windowPosArg = "--window-position=$parkX,$parkY"

# Полная командная строка: chrome.exe + аргументы (CreateProcessW, Unicode).
$allArgs = @($chromeArgs) + @($windowPosArg) + @($proxyArg)
$cmdLine = "`"$Chrome`" " + (($allArgs | Where-Object { $_ }) -join " ")
# Фокус запоминаем ДО запуска: Chrome крадёт его через 0.3-0.4 с после
# CreateProcess (замер 2026-10-06), и поздний захват сделал бы «предыдущим»
# сам Chrome — Restore-Focus возвращал бы фокус ему (пустышка).
$prevFg = [ChromeLaunch]::GetForegroundWindow()
$targetPid = [ChromeLaunch]::Launch($cmdLine)
if ($targetPid -lt 0) {
    Write-Host "Не удалось запустить Chromium (CreateProcess, Win32Error=$(-$targetPid))" -ForegroundColor Red
    exit 1
}

# Ранний маркер для коллектора: cdp.launch_and_wait ловит LAUNCHER-PID из лога
# и берёт окно под охрану ДО connect (щель launch→connect, живой замер
# 2026-10-18: окно владельца жило видимым ~4 с — скрин в щели). Печатается
# сразу после CreateProcess, до всех остальных шагов; маркер ASCII.
Write-Host "LAUNCHER-PID: $targetPid"

# Ownership marker must appear before the CDP-ready loop: launch_and_wait can
# give up after 30 s while the launcher is still polling for up to 210 s.
# Write it early only when no live marker exists, so a manual browser's marker
# is never overwritten by a delegated launch.
$markerPath = Join-Path $Profile ".ozon_collector_launched"
$startedAt = $null
try {
    $startedAt = (Get-Process -Id $targetPid -ErrorAction Stop).StartTime.ToString("o")
} catch { }
$existingAlive = $false
if (Test-Path -LiteralPath $markerPath) {
    try {
        $existing = Get-Content -LiteralPath $markerPath -Raw |
                    ConvertFrom-Json
        $existingProc = Get-Process -Id $existing.pid `
                        -ErrorAction SilentlyContinue
        if ($null -ne $existingProc) {
            # Same one-sided check as cdp._launch_marker_pid: a reused PID
            # that started after the marker cannot own the profile.
            $markerStart = [DateTimeOffset]$existing.started_at
            $procStart = [DateTimeOffset]$existingProc.StartTime
            $skew = ($procStart - $markerStart).TotalSeconds
            $existingAlive = ($skew -le 2 -and $skew -ge -60)
        }
    } catch { }
}
if ($startedAt -and -not $existingAlive) {
    Set-Content -Path $markerPath -Encoding ASCII `
        -Value (@{ pid = $targetPid; started_at = $startedAt } |
                ConvertTo-Json -Compress) | Out-Null
}

function Invoke-MinimizeNoActivate($PidToWatch, $WaitMs) {
    # Применяет к окну WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW, НЕ показывая его:
    # показ даже свёрнутым рождает над панелью задач «мини-плитку-призрак» с
    # крестиком (живой замер 2026-10-16) и на свёрнутом окне не идут кадры.
    # Показывающей ветки здесь больше нет; показ окна — трей или CDP
    # setWindowBounds, когда он реально нужен.
    # DeleteTab снимает кнопку панели задач, если она успела создаться.
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
            $want = $style -bor $WS_EX_NOACTIVATE -bor $WS_EX_TOOLWINDOW
            if ($want -ne $style) {
                # Только изменение: путь зовётся в CDP-цикле каждые ~50 мс,
                # лишний SetWindowLong зря дёргал бы неклиентскую рамку.
                [LaunchChromeMin]::SetWindowLong($h, $GWL_EXSTYLE, $want) | Out-Null
            }
            # SW_HIDE безусловно: если окно всё-таки успело показаться, смена
            # стилей его не прячет и оно остаётся видимым до сторожа (2-7 с).
            # ShowWindow возвращает ПРЕЖНЮЮ видимость (MS docs) — это датчик
            # показа в щели (диагностика для прогонов владельца; один раз).
            $wasVisible = [LaunchChromeMin]::ShowWindow($h, 0)
            # Парковка ПОСЛЕ скрытия: показ Chrome (старт/новая вкладка) гасится
            # хуком/пере-скрытием через миллисекунды, но это ещё видимый миг;
            # за краем экрана окно не видно даже тогда (замер 2026-10-18).
            [void][LaunchChromeMin]::ParkOffscreen($h)
            if ($wasVisible -and -not $script:showCaughtLogged) {
                $script:showCaughtLogged = $true
                Write-Host "LAUNCHER-DIAG: window pid=$PidToWatch WAS VISIBLE before hide (gap catch)"
            }
            [void][TaskbarTab]::Delete($h)   # снять кнопку панели задач: смена TOOLWINDOW уже созданную не убирает
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

# Окна хотят видимыми: режим keep_window (env) или развёрнуто из трея (флаг
# window_policy.USER_VISIBLE_FLAG). Тогда циклы пере-скрытия молчат — иначе
# лаунчер боролся бы с треем (пере-скрытие раз в 50 мс против показа треем;
# находка ревью 2026-10-17).
$keepWindowMode = $env:OZON_KEEP_WINDOW -match '^(1|true|yes|on)$'
$windowVisibleFlag = Join-Path (Split-Path -Parent $PSScriptRoot) "data\window_visible.flag"

# Tray state: "1" = user shows the windows, "0" = user hid them (beats the
# console/env initial mode — owner 2026-10-19). Absent = env decides.
# Re-read on every tick: the owner can hide the windows at any moment.
function Test-ShowAnyway {
    $state = ""
    if (Test-Path $windowVisibleFlag) {
        $state = Get-Content -LiteralPath $windowVisibleFlag -Raw -ErrorAction SilentlyContinue
        if ($null -eq $state) { $state = "" }
        $state = $state.Trim()
    }
    if ($state -eq "0") { return $false }
    if ($state -eq "1") { return $true }
    return $keepWindowMode
}

# Первый проход: находим окно и ставим WS_EX_NOACTIVATE как можно раньше,
# НЕ показывая его (оно скрыто с рождения — SW_HIDE). В visible-режиме окно
# не трогаем: стартовый env или явный "1" из трея означает «показывать»
# (owner 2026-10-19; ревью: финальный hide игнорировал флаг).
$minimized = $false
if (-not (Test-ShowAnyway)) {
    $minimized = Invoke-MinimizeNoActivate $targetPid 10000
}

# Фокус: Chrome крадёт его через 0.3-0.4 с после старта (замер 2026-10-06).
# Слепых 500 мс здесь больше НЕТ: они вместе с поздним первым проходом
# составляли щель, в которой владелец сфотографировал показанное окно (замер
# 2026-10-18, репро .tmp/gap_harness.py: окно видимо 4014 мс). Возврат фокуса
# стал отложенным действием CDP-цикла ниже ($focusDone, тик цикла 50 мс).
$focusDone = $false
$loopT0 = Get-Date

# Ждём, пока CDP-порт ответит: к этому моменту Chrome закончил старт.
# Тик 50 мс (а не 500): каждый тик окно перепрятывается — Chrome может
# ПОКАЗАТЬ его в любой момент старта, а первый проход к этому времени уже
# завершился (живой замер 2026-10-16: вспышка vis=True ~100 мс при тике
# 100 мс сразу после навигации; при опоздании первого прохода — секунды).
$ready = $false
for ($i = 0; $i -lt 200; $i++) {
    Start-Sleep -Milliseconds 50
    if (-not (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) {
        break        # Chromium умер (например, профиль уже открыт) — ждать порт бессмысленно
    }
    # Пере-скрытие: NO-OP, если окно уже скрыто (стиль не переставляется).
    # Пользователь развернул окна (флаг) / режим visible — не боремся с ним.
    # Явный трей-«0» прячет окно даже при OZON_KEEP_WINDOW=1.
    if (-not (Test-ShowAnyway)) {
        $null = Invoke-MinimizeNoActivate $targetPid 20
    }
    if (-not $focusDone -and ((Get-Date) - $loopT0).TotalMilliseconds -ge 450) {
        Restore-Focus -PrevFg $prevFg
        $focusDone = $true
    }
    # Таймаут пробы 1 с, а не 2: при привязанном, но ещё не отвечающем порте
    # время пробы — слепой слот цикла без пере-скрытия (замер 2026-10-18).
    # Дробный TimeoutSec НЕЛЬЗЯ: параметр Int32, 0.3 сконвертировался бы в 0
    # (= бесконечное ожидание, docs: Invoke-RestMethod -ConnectionTimeoutSeconds).
    try {
        $ver = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 1
        $ready = $true
        Write-Host "OK: $($ver.Browser) на порту $Port" -ForegroundColor Green
        break
    } catch { }
}
# Ownership marker: the launched PID is ours even when CDP never came up.
# Without this marker a live Chrome with a silent DevTools port is classified
# as foreign and the profile can never be freed (incident 2026-10-03). A
# delegated launch (Chrome joined an existing instance and our PID exited)
# must not overwrite the live browser's marker, hence the listener check when
# the port answered.
$aliveAfterLaunch = $null -ne (Get-Process -Id $targetPid `
                               -ErrorAction SilentlyContinue)
$ownsPort = $true
if ($ready) {
    $ownedListener = $null
    try {
        $ownedListener = (Get-NetTCPConnection -State Listen -LocalPort $Port `
                          -ErrorAction SilentlyContinue |
                          Where-Object { $_.LocalAddress -eq "127.0.0.1" } |
                          Select-Object -First 1).OwningProcess
    } catch { }
    if ($null -ne $ownedListener) {
        $ownsPort = ($ownedListener -eq $targetPid)
    }
}
if ($ownsPort -and ($ready -or $aliveAfterLaunch)) {
    try {
        $startedAt = (Get-Process -Id $targetPid `
                      -ErrorAction Stop).StartTime.ToString("o")
    } catch { }
    if ($startedAt) {
        Set-Content -Path $markerPath -Encoding ASCII `
            -Value (@{ pid = $targetPid; started_at = $startedAt } |
                    ConvertTo-Json -Compress) | Out-Null
    }
}

if (-not $focusDone) {
    # Мгновенный успех порта (break на первых итерациях): возврат фокуса
    # случился бы ДО кражи (Chrome крадёт его на 0.3-0.4 с) — досыпаем
    # остаток до 450 мс, как в цикле. Досып — слепой слот: после него один
    # проход пере-скрытия, и только потом фокус (находки ревью 2026-10-18).
    $focusLeft = 450 - ((Get-Date) - $loopT0).TotalMilliseconds
    if ($focusLeft -gt 0) { Start-Sleep -Milliseconds ([int]$focusLeft + 1) }
    if (-not (Test-ShowAnyway)) {
        $null = Invoke-MinimizeNoActivate $targetPid 20
    }
    Restore-Focus -PrevFg $prevFg
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

# Окно НЕ показываем вообще. Показ даже свёрнутым (SW_SHOWMINNOACTIVE) рождает
# над панелью задач мини-плитку-призрак с крестиком (живой замер 2026-10-16:
# vis=True icon=True rect=0,1004,160,1032 — «плитка» владельца) и на свёрнутом
# окне перестают идти кадры. Окно остаётся скрытым (SW_HIDE от CreateProcess).
# Финальный проход — безусловный в hidden-режиме (до 2026-10-16 он шёл только
# при провале первого): между проходами Chrome мог показать окно повторно.
# Visible-режим (env или трей) финальный проход не трогает — иначе лаунчер
# прятал бы окно, которое трей только что показал (ревью 2026-10-19).
$showNow = Test-ShowAnyway
$finalHide = $false
if (-not $showNow) {
    $finalHide = Invoke-MinimizeNoActivate $targetPid 3000
}
$minimized = $minimized -or $finalHide
# Фокус: Chrome крадёт его на старте (SetForegroundWindow, замер 2026-10-06),
# а демон-сторож вернёт его только к connect (2-7 с) — всё это время окно
# пользователя без фокуса. Вызов был потерян при переписывании лаунчера,
# функция Restore-Focus оставалась мёртвой; возвращён 2026-10-16.
Restore-Focus -PrevFg $prevFg
if ($showNow) {
    Write-Host "OK: окно PID $targetPid не скрыто лаунчером (visible-режим; показ — трей/CDP)"
} elseif ($minimized) {
    Write-Host "OK: окно PID $targetPid скрыто, стиль TOOLWINDOW|NOACTIVATE применён"
} else {
    Write-Warning "не удалось найти окно PID $targetPid для применения стиля"
}

# КАРАУЛ ОКОН (OZON_LAUNCHER_DUTY_S секунд, дефолт 30; 0 — выключить): щель
# между «CDP готов» (здесь) и регистрацией в сторожe демона (browser_pid +
# connect занимают секунды, на 8 воркеров — тем более). В этой щели окно некому
# прятать, а Chrome умеет показать его сам (живой замер 2026-10-17: показ при
# создании вкладки; репро — окно жило видимым 0.73 с). Лаунчер остаётся
# караульным: пере-скрытие каждые ~50 мс, пока демон не подключился.
# Выйти из караула раньше срока: Chromium умер, пользователь развернул окна
# из трея (флаг) или включён режим visible.
# 2026-10-17: дефолт поднят 10→30 с — показ браузера при шторме перезапусков
# случался на 10-15-й секунде, когда караул уже ушёл, а сторож ещё не встал.
$dutyS = 30
$parsedDuty = 0
if ($env:OZON_LAUNCHER_DUTY_S) {
    if ([int]::TryParse($env:OZON_LAUNCHER_DUTY_S, [ref]$parsedDuty)) {
        $dutyS = $parsedDuty
    } else {
        Write-Warning "OZON_LAUNCHER_DUTY_S='$($env:OZON_LAUNCHER_DUTY_S)' не число — беру дефолт $dutyS с"
    }
}
if ($dutyS -gt 0) {
    $dutyUntil = (Get-Date).AddSeconds($dutyS)
    while ((Get-Date) -lt $dutyUntil) {
        Start-Sleep -Milliseconds 50
        if (-not (Get-Process -Id $targetPid -ErrorAction SilentlyContinue)) {
            break        # Chromium закрылся — караулить больше нечего
        }
        # Перечитываем состояние трея: «Свернуть» может прийти уже во время
        # караула, и явный "0" обязан победить env-дефолт (owner 2026-10-19).
        if (Test-ShowAnyway) {
            break        # окна хотят видимыми: не боремся с треем/режимом
        }
        $null = Invoke-MinimizeNoActivate $targetPid 20
    }
}
Write-Host "LAUNCHER-DIAG: chrome=$Chrome profile=$Profile port=$Port proxy=$proxyNote pid=$targetPid"
exit 0
