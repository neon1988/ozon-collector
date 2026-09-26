"""Целостность launch-скриптов (ревизия 2.9).

Мотивация: `scripts/launch_chrome.ps1` однажды оказался обнулён до 3 байт
(BOM) — типичная ловушка PowerShell, см. .skills/powershell-file-traps. Коллектор
при этом рапортовал «CDP-браузер не поднялся за 30с. Проверьте DISPLAY» на
Windows, и диагностика уводила не туда. Скрипт запуска — критический артефакт:
без него не работает весь сбор, а пустой файл это НЕ проявляет в тестах кода.

Поэтому здесь проверяем САМИ файлы: размер, обязательные флаги, паритет .sh/.ps1.
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PS1 = os.path.join(ROOT, "scripts", "launch_chrome.ps1")
SH = os.path.join(ROOT, "scripts", "launch_chrome.sh")


def _text(path: str) -> str:
    with open(path, "rb") as fh:
        raw = fh.read()
    # ps1 намеренно с BOM (Windows PowerShell 5.1 + кириллица)
    return raw.decode("utf-8-sig")


def test_launch_scripts_exist():
    assert os.path.isfile(PS1), "scripts/launch_chrome.ps1 отсутствует"
    assert os.path.isfile(SH), "scripts/launch_chrome.sh отсутствует"


def test_ps1_not_truncated():
    """Файл не пуст: 3 байта (только BOM) = тихая поломка всего сбора."""
    size = os.path.getsize(PS1)
    assert size > 4000, (
        f"launch_chrome.ps1 подозрительно мал ({size} байт) — вероятно "
        f"обнулён правкой из PowerShell")
    body = _text(PS1)
    assert len(body.splitlines()) > 100, "скрипт потерял тело"
    # пустой файл с BOM = ровно 3 байта и 0 строк
    assert body.strip(), "launch_chrome.ps1 пуст"


def test_sh_not_truncated():
    size = os.path.getsize(SH)
    assert size > 1000, f"launch_chrome.sh подозрительно мал ({size} байт)"
    assert len(_text(SH).splitlines()) > 30


def test_ps1_has_required_flags():
    t = _text(PS1)
    for needle in ("--remote-debugging-port", "--user-data-dir",
                   "--no-first-run", "about:blank",
                   "CreateProcessW", "LAUNCHER-DIAG"):
        assert needle in t, f"launch_chrome.ps1 потерял {needle}"


def test_ps1_supports_proxy_env():
    """cdp.py/browser.py кладут PROXY в env — .ps1 обязан его принять."""
    t = _text(PS1)
    assert "$env:PROXY" in t, "launch_chrome.ps1 не читает PROXY из env"
    assert "--proxy-server=" in t, "launch_chrome.ps1 не строит --proxy-server"
    # блок диагностики лаунчера маскирует пароль прокси
    diag = t[t.find("$proxyNote"):]
    assert diag, "нет блока $proxyNote (диагностика прокси)"
    assert "***" in diag, "диагностика лаунчера не маскирует пароль прокси"
    assert "proxy=$proxyNote" in diag, "LAUNCHER-DIAG печатает сырой $Proxy"


def test_sh_supports_proxy_env():
    t = _text(SH)
    assert "${PROXY:-}" in t or "$PROXY" in t
    assert "--proxy-server=" in t
    assert "DISPLAY=${DISPLAY:-:0}" in t, "Xvfb-режим: DISPLAY должен переопределяться"


def test_both_use_automation_disabled():
    """--enable-automation нельзя: navigator.webdriver → блок FAB."""
    for path, t in ((PS1, _text(PS1)), (SH, _text(SH))):
        args = [ln for ln in t.splitlines() if ln.strip().startswith('"')]
        assert not any("--enable-automation" in ln for ln in args), (
            f"{os.path.basename(path)} передаёт --enable-automation")


def test_ps1_parses_as_powershell():
    """Синтаксическая проверка PS1 (на Windows; на других ОС — пропуск)."""
    if os.name != "nt":
        import pytest
        pytest.skip("парсер PowerShell доступен только на Windows")
    import shutil
    import subprocess
    ps = shutil.which("powershell") or shutil.which("pwsh")
    assert ps, "не найден powershell/pwsh"
    check = (
        "$e=$null;$t=$null;"
        "[void][System.Management.Automation.Language.Parser]::"
        "ParseFile('%s',[ref]$t,[ref]$e);"
        "if($e -and $e.Count){$e[0].Message;exit 1}else{'OK'}" % PS1
    )
    out = subprocess.run([ps, "-NoProfile", "-Command", check],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"PS1 не парсится: {out.stdout}{out.stderr}"
    assert "OK" in out.stdout


def test_sh_parses_with_bash():
    import shutil
    import subprocess
    bash = shutil.which("bash") or shutil.which("sh")
    if not bash:
        import pytest
        pytest.skip("bash недоступен")
    # Путь ОТНОСИТЕЛЬНЫЙ: под WSL абсолютный путь вида D:\... не резолвится
    # (это баг теста, а не скрипта — раньше проверка падала на 127).
    out = subprocess.run([bash, "-n", "scripts/launch_chrome.sh"], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"sh -n: {out.stderr}"


def test_launcher_name_platform():
    from ozon_collector import browser as B
    expected = "launch_chrome.ps1" if os.name == "nt" else "launch_chrome.sh"
    assert B._launch_script_name() == expected
    assert os.path.isfile(B._launch_script_path())


def test_ps1_refuses_busy_port_before_start():
    """Порт занят -> отказ ДО CreateProcess, иначе ложный OK и тихий уход в [::1].

    Гейт ловит ЛЮБОГО слушателя порта (Get-NetTCPConnection), а не только
    HTTP-ответ /json/version: в бою порт держал посторонний процесс, /json/version
    молчал, гейт считал порт свободным, а Chrome поднял DevTools на [::1] и стал
    невидим для коллектора (он ходит на 127.0.0.1).
    """
    ps1 = _text(PS1)
    assert "port $Port is NOT free" in ps1
    assert "Get-NetTCPConnection -State Listen -LocalPort $Port" in ps1
    assert "refusing to start" in ps1
    # гейт стоит ДО создания процесса, иначе смысла нет
    assert ps1.index("is NOT free") < ps1.index("[ChromeLaunch]::Launch")


def test_ps1_explains_ipv6_escape():
    """Причина отказа объяснена: Chrome ушёл бы в [::1] и стал невидим."""
    ps1 = _text(PS1)
    assert "Chrome would bind [::1] instead" in ps1


def test_ps1_reports_port_listener_on_failure():
    """В блоке неудачи видно, кто слушает порт: уводит диагностику не туда."""
    ps1 = _text(PS1)
    assert "port $Port IS LISTENING by pid=" in ps1
    assert "port $Port is free - Chromium died for another reason" in ps1


def test_ps1_fails_loudly_when_cdp_never_comes_up():
    """Раньше ps1 писал одну строку и выходил с кодом 0 (нельзя отличить)."""
    ps1 = _text(PS1)
    assert "Chromium pid=$targetPid EXITED" in ps1
    assert "profile already open in another Chrome?" in ps1
    assert "is ALIVE but" in ps1          # живой PID, но порт молчит
    assert "exit 1" in ps1
    tail = ps1[ps1.index("if (-not $ready)"):]
    assert "exit 0" in tail, "успех должен завершаться явным exit 0"


def test_ps1_diag_printed_on_failure_path():
    """LAUNCHER-DIAG нужен и при неудаче — ради этого он и пишется в лог."""
    ps1 = _text(PS1)
    assert ps1.count("LAUNCHER-DIAG:") >= 2
    err_block = ps1[ps1.index("if (-not $ready)"):ps1.index("exit 1", ps1.index("if (-not $ready)"))]
    assert "LAUNCHER-DIAG:" in err_block


def test_ps1_markers_are_ascii():
    """Кириллица в перенаправленном выводе PowerShell едет -> маркеры ASCII."""
    ps1 = _text(PS1)
    for line in ps1.splitlines():
        if "LAUNCHER-ERROR:" in line or "LAUNCHER-WARN:" in line:
            marker = line[line.index("LAUNCHER-"):]
            assert all(ord(ch) < 128 for ch in marker), line
