# Самовосстанавливающийся watchdog для кампании naushniki.
# Каждые CHECK_S проверяет: жив ли демон, растёт ли лог CLI, жив ли браузер.
# При сбое — перезапускает демон с теми же параметрами (KD_NO_COOLDOWN=1).
# Лог: logs/watchdog.log. Запуск: powershell -ExecutionPolicy Bypass -File scripts/watchdog_naushniki.ps1
param([int]$CheckS = 300, [int]$MaxLogIdleS = 900)

$ErrorActionPreference = "SilentlyContinue"
$ROOT = "D:\Users\Neon\ozon-collector"
$LOGDIR = Join-Path $ROOT "logs"
$WL = Join-Path $LOGDIR "watchdog.log"
$DAEMON_LOG = Join-Path $LOGDIR "naushniki_daemon.log"
$STATUS = Join-Path $ROOT "data\campaigns\naushniki\status.json"
$PIDFILE = Join-Path $ROOT "data\key_daemon.pid"
$Q = "https://www.ozon.ru/category/naushniki-15547/?category_was_predicted=true&deny_category_prediction=true&from_global=true&headphonemount=277802&is_high_rating=t&noisesuppression=169797&text=%D0%BD%D0%B0%D1%83%D1%88%D0%BD%D0%B8%D0%BA%D0%B8+%D0%B1%D0%B5%D1%81%D0%BF%D1%80%D0%BE%D0%B2%D0%BE%D0%B4%D0%BD%D1%8B%D0%B5&wire=32735"

function WLog($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File -FilePath $WL -Append -Encoding UTF8
}

function Is-DaemonAlive {
    $p = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*key_daemon*naushniki*' }
    return [bool]$p
}

function Get-DaemonPid {
    $p = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*key_daemon*naushniki*' } | Select-Object -First 1
    return if ($p) { $p.ProcessId } else { $null }
}

function Get-CliPids {
    return (Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*cli.py search*' }).ProcessId
}

function Get-LastWriteAge($path) {
    if (-not (Test-Path $path)) { return 999999 }
    return [int]((Get-Date) - (Get-Item $path).LastWriteTime).TotalSeconds
}

function Start-Daemon {
    $inner = "cmd.exe /c set KD_NO_COOLDOWN=1 && set PYTHONIOENCODING=utf-8 && cd /d $ROOT && python scripts\key_daemon.py --campaign naushniki --url `"$Q`" --limit 300 --out docs\naushniki.md --max-age 7d --min-reviews 40 --parallel 1 --min-valid 200 --max-no-progress-runs 3 --max-rescans 2 --max-total-hours 12 --notify toast --session-budget-wall-clock 43200 > logs\naushniki_wd_out.log 2>&1"
    $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $inner }
    if ($res.ReturnValue -eq 0) {
        WLog "daemon restarted, new pid=$($res.ProcessId)"
    } else {
        WLog "ERROR: failed to start daemon, ReturnValue=$($res.ReturnValue)"
    }
}

function Restart-Daemon {
    WLog "restarting daemon..."
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*key_daemon*' -or $_.CommandLine -like '*cli.py*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Seconds 4
    Remove-Item -LiteralPath $PIDFILE -Force -ErrorAction SilentlyContinue
    Start-Daemon
}

function Check-Browser {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:9222/json/version" -TimeoutSec 4 | Out-Null
        return $true
    } catch { return $false }
}

WLog "=== watchdog started (check every ${CheckS}s, max log idle ${MaxLogIdleS}s) ==="

while ($true) {
    Start-Sleep -Seconds $CheckS

    # 1) демон жив?
    if (-not (Is-DaemonAlive)) {
        WLog "daemon NOT alive -> restart"
        Restart-Daemon
        continue
    }

    # 2) лог CLI не завис (нет новых строк)?
    $age = Get-LastWriteAge $DAEMON_LOG
    if ($age -gt $MaxLogIdleS) {
        WLog "daemon log idle ${age}s > ${MaxLogIdleS}s -> kill CLI, daemon will spawn new run"
        Get-CliPids | ForEach-Object { Stop-Process -Id $_ -Force }
        # НЕ перезапускаем демон — он сам перезапустит CLI (KD_NO_COOLDOWN)
        continue
    }

    # 3) браузер жив?
    if (-not (Check-Browser)) {
        WLog "browser DOWN -> kill CLI (daemon will reconnect/relaunch browser)"
        Get-CliPids | ForEach-Object { Stop-Process -Id $_ -Force }
        continue
    }

    # 4) терминальное состояние кампании?
    if (Test-Path $STATUS) {
        try {
            $s = Get-Content $STATUS -Encoding UTF8 -Raw | ConvertFrom-Json
            if ($s.final_state) {
                WLog "campaign final_state=$($s.final_state) reason=$($s.final_reason) valid=$($s.valid) -> stopping watchdog"
                break
            }
        } catch { }
    }
}

WLog "=== watchdog stopped ==="