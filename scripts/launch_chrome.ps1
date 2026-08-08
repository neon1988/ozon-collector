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
    # 1) Playwright chromium (как в launch_chrome.sh)
    $pwDirs = @(
        (Join-Path $env:LOCALAPPDATA "ms-playwright\chromium-*\chrome-win64\chrome.exe"),
        (Join-Path $env:LOCALAPPDATA "ms-playwright\chromium-*\chrome-win\chrome.exe")
    )
    foreach ($pat in $pwDirs) {
        $hit = Get-ChildItem $pat -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    # 2) Системный Chrome (установленный)
    $candidates = @(
        (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
        (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
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

$args = @(
    "--remote-debugging-port=$Port",
    "--user-data-dir=$Profile",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=OptimizationHints,MediaRouter",
    "--window-size=1440,900",
    "--no-sandbox",
    "about:blank"
)

Start-Process -FilePath $Chrome -ArgumentList $args | Out-Null

Start-Sleep -Seconds 2
try {
    $ver = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 5
    Write-Host "OK: $($ver.Browser) на порту $Port" -ForegroundColor Green
} catch {
    Write-Host "ждём... проверьте http://127.0.0.1:$Port/json/version"
}
