"""Диагностика автозапуска браузера (ревизия 2.9).

Контекст: `scripts/launch_chrome.ps1` был обнулён до 3 байт, и коллектор
сообщал только «CDP-браузер не поднялся за 30с. Проверьте DISPLAY» — на
Windows, где DISPLAY не при чём. Теперь вывод лаунчера попадает в ошибку.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ozon_collector.cdp as cdp  # noqa: E402
import ozon_collector.browser as br  # noqa: E402


def _fake_launcher(tmp_path, text: str, name: str = "ozon_launch_test.log"):
    p = tmp_path / name
    p.write_bytes(text.encode("utf-8"))
    return str(p)


class _AliveProc:
    """Заглушка Popen: лаунчер ещё работает (poll() -> None)."""

    returncode = None

    def poll(self):
        return self.returncode


class _DeadProc:
    """Заглушка «лаунчер завершился»: poll() отдаёт код возврата."""

    def __init__(self, code: int = 1):
        self.returncode = code

    def poll(self):
        return self.returncode


def test_tail_contains_last_lines(tmp_path):
    log = _fake_launcher(tmp_path, "a\nb\nc\nd\n")
    assert cdp._launcher_log_tail(log) == " | a | b | c | d"
    # limit режет начало, а не конец (в логе важен финальный статус)
    short = cdp._launcher_log_tail(log, limit=2)
    assert short == " | c | d"


def test_tail_reports_empty_output(tmp_path):
    """Ровно случай обнулённого лаунчера: файл есть, вывода нет."""
    log = _fake_launcher(tmp_path, "   \n")
    tail = cdp._launcher_log_tail(log)
    assert "не вывел ничего" in tail
    assert "пуст" in tail or "бит" in tail


def test_tail_handles_missing_file(tmp_path):
    tail = cdp._launcher_log_tail(str(tmp_path / "nope.log"))
    assert tail == ""


def test_tail_of_none():
    assert cdp._launcher_log_tail(None) == ""


def test_browser_tail_masking_is_ps1_side(tmp_path):
    """Маскирование пароля — задача .ps1 ($proxyNote), а не Python."""
    log = _fake_launcher(tmp_path, "LAUNCHER-DIAG: proxy=http://u:***@1.2.3.4:8080")
    assert "***" in cdp._launcher_log_tail(log)
    assert br._launch_log_tail(log) == cdp._launcher_log_tail(log)
    assert "***" in cdp._launcher_log_tail(log, limit=1)


def test_launch_and_wait_error_includes_launcher_output(tmp_path, monkeypatch):
    log = _fake_launcher(tmp_path, "LAUNCHER-DIAG: chrome=C:\\chrome.exe\n"
                                   "Chromium не найден\n")
    monkeypatch.setattr(cdp, "browser_alive", lambda *_a, **_k: False)
    monkeypatch.setattr(cdp, "_launch_browser",
                        lambda *_a, **_k: cdp.LaunchRun(log, _AliveProc()))
    with pytest.raises(RuntimeError) as ei:
        cdp.launch_and_wait("http://127.0.0.1:9222", wait_s=0.1)
    msg = str(ei.value)
    assert "не поднялся" in msg
    assert "Chromium не найден" in msg, msg
    assert "LAUNCHER-DIAG" in msg
    assert log in msg, "в ошибке должен быть путь к логу лаунчера"
    if os.name == "nt":
        assert "DISPLAY" not in msg, "на Windows подсказка про DISPLAY вводит в заблуждение"
        assert "launch_chrome.ps1" in msg


def test_launch_and_wait_keeps_log_for_postmortem(tmp_path, monkeypatch):
    """Лог НЕ удаляется после неудачи: это улика, путь печатается в ошибке.

    Раньше владение файлом было на вызывающем, и его забывали — мусор в %TEMP%.
    Теперь путь фиксирован на порт, перезаписывается при следующем запуске.
    """
    log = _fake_launcher(tmp_path, "boot\n")
    monkeypatch.setattr(cdp, "browser_alive", lambda *_a, **_k: False)
    monkeypatch.setattr(cdp, "_launch_browser",
                        lambda *_a, **_k: cdp.LaunchRun(log, _AliveProc()))
    with pytest.raises(RuntimeError):
        cdp.launch_and_wait("http://127.0.0.1:9222", wait_s=0.1)
    assert os.path.exists(log), "лог лаунчера остаётся как улика"


def test_launch_and_wait_ok_when_browser_alive(monkeypatch):
    monkeypatch.setattr(cdp, "browser_alive", lambda *_a, **_k: True)
    called = []
    monkeypatch.setattr(cdp, "_launch_browser",
                        lambda *_a, **_k: called.append(1))
    assert cdp.launch_and_wait("http://127.0.0.1:9222") is True
    assert called == [], "живой браузер не перезапускаем"


def test_browser_module_tail_helpers(tmp_path):
    log = _fake_launcher(tmp_path, "x\ny\n")
    assert "y" in br._launch_log_tail(log)
    assert br._launch_log_tail(None) == ""
    assert br._launch_log_tail(str(tmp_path / "no.log")) == ""
    assert "не вывел ничего" in br._launch_log_tail(_fake_launcher(
        tmp_path, "\n", name="empty.log"))


@pytest.mark.allow_browser_launch
def test_launch_browser_writes_fixed_path_per_port(monkeypatch):
    """Лог автозапуска: один путь на порт, truncate при каждом запуске."""
    from ozon_collector import launchlog as LL
    calls = []
    monkeypatch.setattr(cdp.subprocess, "Popen",
                        lambda *a, **k: calls.append(1))
    p1 = cdp._launch_browser("http://127.0.0.1:9222")
    assert p1.path == LL.launcher_log_path(9222)
    with open(p1.path, "wb") as fh:
        fh.write(b"first run\n")
    p2 = cdp._launch_browser("http://127.0.0.1:9222")
    assert p2.path == p1.path
    assert os.path.getsize(p1.path) == 0, "лог должен обрезаться на каждом запуске"
    assert calls == [1, 1]


def test_fixed_path_differs_per_port():
    from ozon_collector import launchlog as LL
    assert LL.launcher_log_path(9222) != LL.launcher_log_path(9333)
    assert LL.launcher_log_path(9222).endswith("ozon_launch_9222.log")


def test_read_log_tail_variants(tmp_path):
    from ozon_collector import launchlog as LL
    p = tmp_path / "l.log"
    p.write_text("line1\nline2\n", encoding="utf-8")
    assert LL.read_log_tail(str(p)) == " | line1 | line2"
    assert LL.read_log_tail(str(p), limit=1) == " | line2"
    p.write_text("  \n", encoding="utf-8")
    assert "не вывел ничего" in LL.read_log_tail(str(p))
    assert LL.read_log_tail(None) == ""
    assert LL.read_log_tail(str(tmp_path / "none.log")) == ""


@pytest.mark.allow_browser_launch
def test_browser_launch_script_uses_same_log_path(monkeypatch):
    """browser.py и cdp.py пишут в ОДИН лог на порт (параллельность)."""
    from ozon_collector import launchlog as LL
    monkeypatch.setattr(br.subprocess, "Popen", lambda *a, **k: None)
    got = br._launch_script("http://127.0.0.1:9222")
    assert got.path == LL.launcher_log_path(9222)
    # Popen замотан — файл пустой, и хвост обязан это проговорить
    assert "не вывел ничего" in br._launch_log_tail(got.path)


def test_tail_after_grace_waits_for_slow_launcher(tmp_path, monkeypatch):
    """Пустой лог на первой попытке -> ждём и читаем заново (гонка записи).

    Наблюдалось на живом запуске: лог был 0 байт при ещё работающем лаунчере,
    который дописал вывод через секунду. Без grace «пусто» = ложная диагностика.
    """
    log = tmp_path / "slow.log"
    log.write_bytes(b"")
    calls = {"n": 0}

    def fake_sleep(_s):
        calls["n"] += 1
        if calls["n"] == 2:               # лаунчер «проснулся» на 2-й итерации
            log.write_text("LAUNCHER-ERROR: profile busy\n", encoding="utf-8")

    monkeypatch.setattr(cdp.time, "sleep", fake_sleep)
    tail = cdp._launcher_tail_after_grace(str(log), grace_s=1.0)
    assert "profile busy" in tail
    assert calls["n"] >= 2


def test_tail_after_grace_confirms_real_emptiness(tmp_path, monkeypatch):
    log = tmp_path / "empty.log"
    log.write_bytes(b"")
    monkeypatch.setattr(cdp.time, "sleep", lambda _s: None)
    tail = cdp._launcher_tail_after_grace(str(log), grace_s=0.5)
    assert "не вывел ничего" in tail


def test_tail_after_grace_fast_path_no_sleep(tmp_path, monkeypatch):
    log = tmp_path / "ready.log"
    log.write_text("OK: Chrome\n", encoding="utf-8")
    monkeypatch.setattr(cdp.time, "sleep",
                        lambda _s: pytest.fail("sleep не нужен, лог уже готов"))
    assert "Chrome" in cdp._launcher_tail_after_grace(str(log))


def _write_launch_log(text: str, port: int = 9222) -> str:
    """Положить текст в лог автозапуска (путь считает сам launchlog)."""
    from ozon_collector import launchlog as LL
    path = LL.launcher_log_path(port)
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8"))
    return path


def test_launch_and_wait_fails_fast_when_launcher_exits(monkeypatch):
    """Лаунчер с exit 1 знает, что браузер не поднялся — ждать остаток нечего.

    Раньше неудачный старт стоил полные wait_s на попытку (30с x 3 ретрая демона).
    """
    log = _write_launch_log("LAUNCHER-ERROR: Chromium pid=1 EXITED\n")
    waited = []
    monkeypatch.setattr(cdp, "browser_alive", lambda *_a, **_k: False)
    monkeypatch.setattr(cdp, "_launch_browser",
                        lambda *_a, **_k: cdp.LaunchRun(log, _DeadProc(1)))
    monkeypatch.setattr(cdp.time, "sleep", lambda s: waited.append(s))
    with pytest.raises(RuntimeError) as e:
        cdp.launch_and_wait("http://127.0.0.1:9222", wait_s=600)
    assert waited == [], "не должен ни разу спать, лаунчер уже ушёл"
    assert "код возврата лаунчера=1" in str(e.value)
    assert "EXITED" in str(e.value)


def test_launcher_says_browser_alive_detected(tmp_path):
    """Распознаём «Chromium жив, CDP молчит» — браузер ещё поднимается.

    Живой замер 2026-10-03 16:03 лог лаунчера:
      LAUNCHER-ERROR: Chromium pid=48408 is ALIVE but /json/version is silent
    Раньше это читалось как провал: демон снимал держателей и поднимал новый,
    тот умирал о тот же профиль — четыре UNKNOWN_FAILURE подряд при живом
    браузере на 9222.
    """
    p = _fake_launcher(tmp_path, "LAUNCHER-ERROR: Chromium pid=48408 is ALIVE "
                               "but http://127.0.0.1:9222/json/version is "
                               "silent\n")
    assert cdp._launcher_says_browser_alive(p) is True
    p2 = _fake_launcher(tmp_path, "LAUNCHER-ERROR: Chromium pid=1 EXITED, CDP "
                                "never came up on port 9222\n",
                        name="exit.log")
    assert cdp._launcher_says_browser_alive(p2) is False


def test_await_browser_graces_when_launcher_says_alive(tmp_path, monkeypatch):
    """Лаунчер ушёл, но Chromium ЖИВ → ждём подъёма, а не объявляем провал."""
    log = _fake_launcher(tmp_path, "LAUNCHER-ERROR: Chromium pid=48408 is "
                                  "ALIVE but /json/version is silent\n")
    checks = {"n": 0}

    def _alive(*_a, **_k):
        checks["n"] += 1
        return checks["n"] >= 4        # CDP ожил на четвёртой проверке

    monkeypatch.setattr(cdp, "browser_alive", _alive)
    run = cdp.LaunchRun(log, _DeadProc(1))
    assert cdp._await_browser("http://127.0.0.1:9222", run,
                              time.monotonic() + 30, grace_s=10) is True
    assert checks["n"] >= 4


def test_await_browser_no_grace_when_launcher_reports_death(tmp_path, monkeypatch):
    """Лаунчер сказал EXITED → fail-fast мгновенно (без сна)."""
    log = _fake_launcher(tmp_path, "LAUNCHER-ERROR: Chromium pid=1 EXITED, CDP "
                                  "never came up\n")
    slept = []
    monkeypatch.setattr(cdp, "browser_alive", lambda *_a, **_k: False)
    monkeypatch.setattr(cdp.time, "sleep", lambda s: slept.append(s))
    run = cdp.LaunchRun(log, _DeadProc(1))
    assert cdp._await_browser("http://127.0.0.1:9222", run,
                              time.monotonic() + 30) is False
    assert slept == [], "при EXITED ждать нечего"


def test_launch_and_wait_keeps_waiting_while_launcher_alive(monkeypatch):
    """Живой лаунчер = продолжаем ждать порт (false alarm не делаем)."""
    log = _write_launch_log("")
    monkeypatch.setattr(cdp, "browser_alive", lambda *_a, **_k: False)
    monkeypatch.setattr(cdp, "_launch_browser",
                        lambda *_a, **_k: cdp.LaunchRun(log, _AliveProc()))
    slept = []
    monkeypatch.setattr(cdp.time, "sleep", lambda s: slept.append(s))
    with pytest.raises(RuntimeError) as e:
        cdp.launch_and_wait("http://127.0.0.1:9222", wait_s=0.2)
    assert slept, "пока лаунчер жив, ждём порт"
    assert "лаунчер ещё работает" in str(e.value)


def test_browser_await_launch_fails_fast_when_launcher_exits(monkeypatch):
    """Тот же fail-fast в ProcessManager._await_launch."""
    mgr = br.BrowserProcessManager(cdp_url="http://127.0.0.1:9222")
    log = _write_launch_log("LAUNCHER-ERROR: EXITED\n")
    slept = []
    monkeypatch.setattr(mgr, "_matching", lambda: [])
    monkeypatch.setattr(br.time, "sleep", lambda s: slept.append(s))
    rec = mgr._await_launch("L1", 600, br.LaunchRun(log, _DeadProc(1)))
    assert rec is None
    assert slept == [], "не должен ждать полный wait_s после exit лаунчера"
