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


def test_ps1_pins_russian_locale():
    """--lang=ru-RU: Accept-Language/navigator.language не должен зависеть от
    локали машины. Антибот отличает en-US профиль от ru-RU (ADR §19, 2026-09-26)."""
    t = _text(PS1)
    args = [ln for ln in t.splitlines() if ln.strip().startswith('"')]
    assert any("--lang=ru-RU" in ln for ln in args), (
        "launch_chrome.ps1 не фиксирует --lang=ru-RU")
    assert any("--window-size=" in ln for ln in args), (
        "launch_chrome.ps1 не задаёт размер окна")


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


def test_both_cap_disk_cache():
    """Потолок дискового кеша: профиль озона раздувался до 880 МБ на воркер,
    92% из этого — воспроизводимый кэш (Default/Cache + Code Cache), а не
    состояние сессии. --disk-cache-size в килобайтах режет это до ~100 МБ."""
    for path, t in ((PS1, _text(PS1)), (SH, _text(SH))):
        name = os.path.basename(path)
        assert "--disk-cache-size=" in t, f"{name} не ограничивает дисковый кеш"
        assert "* 1024" in t, f"{name}: флаг ждёт КБ, а не МБ (нет * 1024)"


def test_ps1_disk_cache_env_override():
    """OZON_DISK_CACHE_MB переопределяет дефолт; мусор в переменной → дефолт,
    а не падение лаунчера (сбой запуска = нет сбора вообще)."""
    t = _text(PS1)
    assert "$env:OZON_DISK_CACHE_MB" in t
    assert "TryParse" in t, "значение из env проверяется как число"
    warn = [ln for ln in t.splitlines() if "LAUNCHER-WARN" in ln]
    assert warn, "мусор в OZON_DISK_CACHE_MB молча проглатывается"
    for ln in warn:
        assert ln.isascii(), (
            f"LAUNCHER-WARN содержит не-ASCII: кириллица в перенаправленном "
            f"выводе PowerShell едет через кодировку консоли и в логе нечитаема")


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


def test_both_deny_permission_prompts():
    """Permission-бабл Chrome — отдельное ВИДИМОЕ окно даже при скрытом главном.

    Живой замер 2026-10-16: через ~2с после загрузки ozon-страницы Chrome
    создаёт окно «www.ozon.ru запрашивает разрешение на:» (vis=True noact=False
    tool=True rect=1218,136,1552,442) и держит его открытым — владелец видит
    «плитку» поверх своих окон. Флаг авто-отклоняет промпты, UI не рисуется.
    Источник семантики: chromium components/permissions/README.md.
    """
    for path, t in ((PS1, _text(PS1)), (SH, _text(SH))):
        name = os.path.basename(path)
        assert "--deny-permission-prompts" in t, (
            f"{name} потерял --deny-permission-prompts: permission-бабл "
            f"Chrome снова станет видимым окном поверх чужих окон")


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


def test_ps1_deletes_taskbar_tab_on_show():
    """Применяя стиль, лаунчер обязан снять кнопку панели задач (DeleteTab).

    Старт окно НЕ показывает (см. test_ps1_leaves_window_hidden), но кнопка
    могла создаться в обход (Explorer/Chromium), а смена WS_EX_TOOLWINDOW уже
    созданную кнопку не убирает (живая находка 2026-10-16) — нужен явный
    ITaskbarList::DeleteTab."""
    ps1 = _text(PS1)
    assert "ITaskbarList" in ps1 and "DeleteTab" in ps1, \
        "launch_chrome.ps1 потерял снятие кнопки панели задач"
    assert "[TaskbarTab]::Delete($h)" in ps1, \
        "DeleteTab объявлен, но не вызывается при применении стиля"


def test_ps1_leaves_window_hidden_on_start():
    """Лаунчер НЕ показывает окно на старте — даже свёрнутым.

    Живой замер 2026-10-16: SW_SHOWMINNOACTIVE рождает над панелью задач
    «мини-плитку-призрак» с крестиком (vis=True icon=True
    rect=0,1004,160,1032 — та самая «плитка» владельца) и на свёрнутом окне
    не идут кадры. Окно остаётся скрытым (SW_HIDE от CreateProcess), к нему
    применяется только стиль TOOLWINDOW|NOACTIVATE.
    """
    ps1 = _text(PS1)
    assert "Invoke-MinimizeNoActivate $targetPid 300 $true" not in ps1, \
        "в лаунчер вернулся шоу-луп (показ окна свёрнутым)"
    assert "ShowWindow($h, 7)" not in ps1, \
        "лаунчер снова показывает окно (SW_SHOWMINNOACTIVE=$h,7)"
    assert "окно PID $targetPid скрыто" in ps1, \
        "лаунчер не сообщает, что окно скрыто"


def test_ps1_compiles_helpers_before_createprocess():
    """Add-Type классов скрытия/фокуса — ДО CreateProcess.

    Живой замер 2026-10-16: Chrome показывает стартовое окно в первые
    секунды, а компиляция C#-классов ПОСЛЕ его старта (~1-3 с вместе с
    загрузкой PS-профиля) откладывала первый проход скрытия — окно жило
    секундами (тёмное окно с одним крестиком на скриншотах владельца).
    """
    ps1 = _text(PS1)
    launch = ps1.index("[ChromeLaunch]::Launch")
    for cls in ("public class ChromeLaunch",
                "public class LaunchChromeMin",
                "public class FocusSave"):
        assert ps1.index(cls) < launch, (
            f"{cls} компилируется после CreateProcess — окно успеет показаться")


def test_ps1_rehides_window_until_cdp_ready():
    """Пере-скрытие окна: первый проход, каждый тик ожидания CDP, финал.

    Chrome может ПОКАЗАТЬ окно в любой момент старта (живой замер 2026-10-16:
    вспышка vis=True ~100 мс при тике 100 мс; при опоздании первого прохода —
    секунды, пока не подключится сторож). Поэтому тик ожидания CDP — 50 мс
    с пере-скрытием, а финальный проход безусловный в hidden-режиме: иначе
    показанное ПОСЛЕ первого прохода окно остаётся видимым до сторожа
    (2-7 с) или до connect. В visible-режиме первый и финальный проходы
    обязаны молчать (ревью 2026-10-19).
    """
    ps1 = _text(PS1)
    assert ps1.count("Invoke-MinimizeNoActivate $targetPid") >= 3, (
        "меньше трёх проходов скрытия: первый + тики CDP + финал")
    assert "for ($i = 0; $i -lt 200; $i++) {" in ps1, \
        "ожидание CDP снова с тиком 500 мс (пере-скрытие в тике потеряно)"
    assert "if (-not $minimized) {" not in ps1, \
        "финальный проход снова условный — показанное после первого прохода " \
        "окно не перепрятывается"
    assert "$minimized = $minimized -or $finalHide" in ps1, \
        "финальный проход потерял объединение статуса ($finalHide)"
    assert "$showNow = Test-ShowAnyway" in ps1, \
        "финальный гейт обязан фиксировать состояние один раз (лог не врёт)"
    gates = (
        ("$minimized = Invoke-MinimizeNoActivate $targetPid 10000",
         "if (-not (Test-ShowAnyway)) {"),
        ("$finalHide = Invoke-MinimizeNoActivate $targetPid 3000",
         "if (-not $showNow) {"),
    )
    for call, gate in gates:
        at = ps1.index(call)
        gate_at = ps1.rindex(gate, 0, at)
        assert at - gate_at < 120, (
            f"{call!r} не под гейтом {gate!r}: лаунчер прячет окно "
            f"в visible-режиме")


def test_ps1_keeps_guard_duty_until_daemon_connect():
    """Караул окон после готовности CDP: щель до регистрации в сторожe демона.

    Живой замер 2026-10-17 (.tmp/repro_owner.py): между «CDP готов» (на этом
    лаунчер завершался) и guard.watch демона проходят секунды (поиск
    browser_pid, connect, 8 воркеров) — показанное Chrome окно (при создании
    вкладки через CDP) некому погасить, вспышка жила 0.73 с. Лаунчер теперь
    остаётся караульным OZON_LAUNCHER_DUTY_S секунд (дефолт 30; 0 —
    выключить) и пере-скрывает окно каждые ~50 мс.
    """
    ps1 = _text(PS1)
    assert "OZON_LAUNCHER_DUTY_S" in ps1, "караул окон потерян (env-ручка)"
    duty = ps1.index("OZON_LAUNCHER_DUTY_S")
    assert duty > ps1.index(
        "$finalHide = Invoke-MinimizeNoActivate $targetPid 3000"), \
        "караул обязан идти после финального скрытия"
    assert duty > ps1.rindex("Restore-Focus -PrevFg $prevFg"), \
        "караул обязан идти после возврата фокуса"
    tail = ps1[duty:]
    assert "Invoke-MinimizeNoActivate $targetPid 20" in tail, \
        "караул не пере-скрывает окно в цикле"
    assert "Start-Sleep -Milliseconds 50" in tail, \
        "караул обязан быть с тиком 50 мс, а не ждать весь таймаут"


def test_ps1_prints_launcher_pid_marker():
    """LAUNCHER-PID печатается сразу после CreateProcess.

    На маркере стоит ранняя регистрация окна в сторожe (cdp._watch_launcher_window):
    с ним щель launch→connect закрыта, без него окно владельца жило видимым
    ~4 с (замер 2026-10-18, .tmp/gap_harness.py).
    """
    ps1 = _text(PS1)
    assert "LAUNCHER-PID:" in ps1, "маркер ранней охраны окна потерян"
    launch = ps1.index("[ChromeLaunch]::Launch($cmdLine)")
    marker = ps1.index("LAUNCHER-PID:")
    assert launch < marker, "маркер обязан идти после CreateProcess"
    assert marker < ps1.index("function Invoke-MinimizeNoActivate"), \
        "маркер обязан печататься до первого прохода скрытия"
    assert marker < ps1.index("$focusDone = $false")


def test_ps1_no_blind_slots_before_daemon_connect():
    """Слепые слоты пути запуска закрыты: 500-мс sleep убран, проба 1 с.

    Замер 2026-10-18 (.tmp/gap_harness.py): окно владельца жило видимым
    4014 мс в щели launch→connect; 500-мс sleep и REST-проба -TimeoutSec 2
    (Int32-параметр — дробный сконвертировался бы в 0 = бесконечность)
    блокировали пере-скрытие.
    """
    ps1 = _text(PS1)
    assert "Start-Sleep -Milliseconds 500" not in ps1, \
        "слепой 500-мс sleep вернулся в путь запуска"
    assert ps1.count("-TimeoutSec 2") <= 1, \
        "двухсекундная REST-проба вернулась (в цикле ожидания ей не место)"
    t2 = ps1.find("-TimeoutSec 2")
    if t2 != -1:
        assert t2 < ps1.index("[ChromeLaunch]::Launch($cmdLine)"), \
            "-TimeoutSec 2 допустим только в pre-start гейте свободного порта"
    assert ("Invoke-RestMethod -Uri \"http://127.0.0.1:$Port/json/version\" "
            "-TimeoutSec 1" in ps1), "проба обязана быть 1 с (Int32)"


def test_ps1_parks_hidden_window_offscreen():
    """Скрытое окно уводится за край экрана (серое мигание при старте сбора).

    Chrome показывает своё окно сам (старт/новая вкладка/пазл), гашение
    опаздывает на миллисекунды — окно успевает мигнуть серым пустым
    прямоугольником (живой замер 2026-10-18: show→hide 30 мс). За краем
    виртуального рабочего стола его не видно; CDP-модель следует за позицией.
    Парковка — строго ПОСЛЕ SW_HIDE.
    """
    ps1 = _text(PS1)
    i = ps1.index("function Invoke-MinimizeNoActivate")
    block = ps1[i:ps1.index("# Возвращаем фокус окну", i)]
    assert "ParkOffscreen($h)" in block, "пере-скрытие не паркует окно за экран"
    assert block.index("ShowWindow($h, 0)") < block.index("ParkOffscreen($h)"), \
        "парковка обязана идти после SW_HIDE"
    assert "GetSystemMetrics(78)" in ps1 and "GetSystemMetrics(79)" in ps1, \
        "ParkOffscreen потерял размеры виртуального экрана"
    assert "GetWindowRect" in ps1, "ParkOffscreen потерял проверку позиции окна"


def test_ps1_window_is_born_offscreen():
    """The window is born beyond the screen edge: no "created -> parked" gap.

    Chrome first creates the window at its default position and applies
    --window-position right after; the launcher's first pass parks it a few
    milliseconds later, and in that gap a show could reach the screen (live
    measurement 2026-10-18, 5 ms sampling: hidden on screen at 140 ms -> moved
    off screen at 250 ms -> first show at 343 ms already off screen). The
    position is computed from the virtual desktop of all monitors and must hit
    the command line BEFORE CreateProcess.
    """
    ps1 = _text(PS1)
    assert "--window-position=" in ps1, "окно не рождается за экраном"
    assert "[LaunchChromeMin]::GetSystemMetrics(76) - $winW - 100" in ps1, \
        "позиция не привязана к левому краю виртуального рабочего стола"
    assert "--window-size=$winW,$winH" in ps1, \
        "размер окна и парковочная позиция разъехались (один источник)"
    assert ps1.index("$windowPosArg") < ps1.index("[ChromeLaunch]::Launch"), \
        "--window-position должен попасть в командную строку до CreateProcess"


def test_ps1_focus_restore_waits_out_theft_window():
    """Мгновенный успех порта: возврат фокуса ждёт 450 мс и перепрятывает окно.

    Chrome крадёт фокус через 0.3-0.4 с после старта: возврат раньше кражи
    бесполезен, а досып до 450 мс — слепой слот без пере-скрытия (находки
    ревью 2026-10-18, раунд 3). Гейт visible-режима у пере-скрытия обязателен
    (иначе лаунчер спрячет развёрнутое пользователем окно — раунд 4).
    """
    ps1 = _text(PS1)
    i = ps1.index("if (-not $focusDone) {")
    block = ps1[i:ps1.index("\n}", i)]
    assert ("$focusLeft = 450 - ((Get-Date) - $loopT0).TotalMilliseconds"
            in block), "досып до 450 мс потерян (возврат фокуса раньше кражи)"
    assert "if (-not (Test-ShowAnyway)) {" in block, \
        "гейт visible-режима у пере-скрытия потерян"
    order = [block.index("$focusLeft"), block.index("Start-Sleep"),
             block.index("Test-ShowAnyway"),
             block.index("Invoke-MinimizeNoActivate"),
             block.index("Restore-Focus")]
    assert order == sorted(order), \
        "порядок: остаток -> сон -> гейт -> пере-скрытие -> возврат фокуса"
    assert "WAS VISIBLE before hide" in ps1, "датчик показа в щели потерян"
    assert "LAUNCHER-DIAG: window pid=" in ps1


def test_ps1_tray_hidden_beats_keep_window_env():
    """Explicit tray "0" beats OZON_KEEP_WINDOW (owner 2026-10-19).

    The console/env sets the initial mode; after start the tray is
    authoritative, so the launcher must re-read the flag (the owner can hide
    the windows during the 30 s duty loop) and never treat "0" as visible.
    """
    ps1 = _text(PS1)
    fn = ps1[ps1.index("function Test-ShowAnyway"):]
    fn = fn[:fn.index("$minimized = Invoke-MinimizeNoActivate")]
    assert "Test-Path $windowVisibleFlag" in fn
    assert '$state -eq "0"' in fn and "return $false" in fn, \
        'явный "0" из трея не побеждает env-дефолт'
    assert '$state -eq "1"' in fn and "return $true" in fn
    assert "return $keepWindowMode" in fn, \
        "без файла режим обязан решаться env (owner 2026-10-19)"
    startup = ps1[ps1.index("for ($i = 0; $i -lt 200"):]
    assert "Test-ShowAnyway" in startup, \
        "стартовый цикл не перечитывает трей (флаг может появиться за 10 с)"
    duty = ps1[ps1.index("while ((Get-Date) -lt $dutyUntil)"):]
    assert "Test-ShowAnyway" in duty, "караул не перечитывает состояние трея"
