<#
.SYNOPSIS
    Single verification entry point for ozon-collector.

.DESCRIPTION
    Cheap to expensive: syntax -> cli -> tests. A live smoke check is deliberately
    absent: it needs a live Chromium on a real profile and the Ozon site, and a
    collector that cannot reach the network must still be verifiable.

    The suite is started through scripts\run_tests_guarded.py on purpose: it
    reaps leftover children and orphans of earlier runs. A plain pytest start
    leaves browsers behind, and they hold the profile directory.

    NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1 reads
    .ps1 files as ANSI unless they carry a UTF-8 BOM, so non-ASCII text here
    corrupts the parser.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1 -Quick
#>
[CmdletBinding()]
param([switch]$Quick)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

$script:failures = @()

function Invoke-Step {
    param([string]$Name, [scriptblock]$Action)
    Write-Host ''
    Write-Host "==> $Name" -ForegroundColor Cyan
    & $Action
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        Write-Host "    OK    $Name" -ForegroundColor Green
    } else {
        Write-Host "    FAIL  $Name (exit $code)" -ForegroundColor Red
        $script:failures += $Name
    }
}

Invoke-Step 'syntax (compileall)' {
    python -m compileall -q ozon_collector cli.py scripts tests
}

Invoke-Step 'cli --help' {
    python cli.py --help
}

if ($Quick) {
    Pop-Location
    Write-Host ''
    Write-Host 'QUICK GREEN' -ForegroundColor Green
    exit 0
}

Invoke-Step 'tests (pytest, guarded)' {
    python scripts\run_tests_guarded.py --timeout 900
}

Pop-Location

Write-Host ''
if ($script:failures.Count -gt 0) {
    Write-Host "FAILED: $($script:failures -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host 'ALL GREEN' -ForegroundColor Green
exit 0