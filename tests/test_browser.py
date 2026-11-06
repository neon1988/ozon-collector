"""Тесты браузерного слоя (ozon_collector/browser.py): моки, без живого Chromium.

Покрытие: классификация ошибок, независимый CDP HTTP-проб, health probe,
BrowserProcessManager (launch/restart/force_stop с ownership-гарантиями).
"""
from __future__ import annotations

import unittest.mock as mock

import pytest

from ozon_collector import browser as B
from ozon_collector import process as P


# ---- фикстуры-заглушки ------------------------------------------------------

class _FakePage:
    def __init__(self, closed=False, raise_on_is_closed=False):
        self.closed = closed
        self.raise_on_is_closed = raise_on_is_closed

    def is_closed(self):
        if self.raise_on_is_closed:
            raise RuntimeError("transport broken")
        return self.closed


class _FakeSess:
    def __init__(self, cdp_url="http://127.0.0.1:9222",
                 identity=None, page=None, owner="collector",
                 profile_token="ozon-profile", kind="chrome"):
        self.cdp_url = cdp_url
        self.browser_pid = identity      # имя поля — как в cdp.Session
        self.page = page
        self.browser_owner = owner        # ADR §14.1
        self.profile_token = profile_token
        self.kind = kind


def _no_os_procs(monkeypatch):
    """Ни одного процесса браузера на (порт, профиль) — браузер мёртв."""
    monkeypatch.setattr(P, "browser_processes",
                        lambda url, profile_token=None: [])
    monkeypatch.setattr(P, "firefox_browser_processes", lambda token: [])


def _cdp_down(monkeypatch):
    monkeypatch.setattr(B.BrowserProbe, "cdp_http_ok", lambda self, url: False)
    monkeypatch.setattr(B.time, "sleep", lambda s: None)


def _identity(pid=1000, key="k"):
    return P.ProcessIdentity(pid=pid, start_key=key)


def _rec(pid=1000, key="k"):
    return P.ProcessRecord("R1", _identity(pid, key))


# ---- _launch_script / выбор браузера (OZON_CHROME_PATH) ---------------------

@pytest.mark.allow_browser_launch
def test_launch_script_injects_chrome_override(monkeypatch, tmp_path):
    import subprocess

    script = tmp_path / "launch_chrome.ps1"
    script.write_text("# stub", encoding="utf-8")
    captured = {}

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        return mock.Mock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(B, "_launch_script_path", lambda: script)
    monkeypatch.setenv("OZON_CHROME_PATH", r"D:\browsers\cft\chrome.exe")
    monkeypatch.delenv("CHROME", raising=False)

    B._launch_script("http://127.0.0.1:9277", profile_path=r"C:\prof\ozon")

    assert captured["env"]["PORT"] == "9277"
    assert captured["env"]["PROFILE"] == r"C:\prof\ozon"
    assert captured["env"]["CHROME"] == r"D:\browsers\cft\chrome.exe"


@pytest.mark.allow_browser_launch
def test_launch_script_no_override_keeps_env(monkeypatch, tmp_path):
    import subprocess

    script = tmp_path / "launch_chrome.ps1"
    script.write_text("# stub", encoding="utf-8")
    captured = {}

    def fake_popen(cmd, **kw):
        captured["env"] = kw.get("env")
        return mock.Mock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(B, "_launch_script_path", lambda: script)
    monkeypatch.delenv("OZON_CHROME_PATH", raising=False)
    monkeypatch.delenv("CHROME", raising=False)

    B._launch_script("http://127.0.0.1:9222")

    assert "CHROME" not in captured["env"]


@pytest.mark.allow_browser_launch
def test_launch_script_keeps_predefined_chrome(monkeypatch, tmp_path):
    import subprocess

    script = tmp_path / "launch_chrome.ps1"
    script.write_text("# stub", encoding="utf-8")
    captured = {}

    def fake_popen(cmd, **kw):
        captured["env"] = kw.get("env")
        return mock.Mock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(B, "_launch_script_path", lambda: script)
    monkeypatch.delenv("OZON_CHROME_PATH", raising=False)
    monkeypatch.setenv("CHROME", r"C:\my\chrome.exe")

    B._launch_script("http://127.0.0.1:9222")

    assert captured["env"]["CHROME"] == r"C:\my\chrome.exe"


@pytest.mark.allow_browser_launch
def test_launch_script_create_no_window_flag(monkeypatch, tmp_path):
    """Recycle-путь лаунчера тоже без окна терминала (CREATE_NO_WINDOW).

    Тот же живой кейс 2026-10-17, что в cdp._launch_browser: консоль-родителя
    нет → Windows открывает лаунчеру новую консоль видимым окном.
    """
    import os
    import subprocess

    script = tmp_path / "launch_chrome.ps1"
    script.write_text("# stub", encoding="utf-8")
    captured = {}

    def fake_popen(cmd, **kw):
        captured.update(kw)
        return mock.Mock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(B, "_launch_script_path", lambda: script)

    B._launch_script("http://127.0.0.1:9277", profile_path=r"C:\prof\ozon")

    if os.name == "nt":
        assert captured.get("creationflags") == 0x08000000
    else:
        assert "creationflags" not in captured


def test_chrome_override_parses_env(monkeypatch):
    monkeypatch.delenv("OZON_CHROME_PATH", raising=False)
    assert B._chrome_override() is None
    monkeypatch.setenv("OZON_CHROME_PATH", r"D:\cft\chrome.exe")
    assert B._chrome_override() == r"D:\cft\chrome.exe"


# ---- has_renderer_signature -------------------------------------------------

def test_renderer_signature_detached_frame():
    assert B.has_renderer_signature(
        "net::ERR_ABORTED: The frame was detached from the DOM"
    ) is True


def test_renderer_signature_renderer_crashed():
    assert B.has_renderer_signature("Renderer process crashed: page 123") is True


def test_renderer_signature_plain_error():
    assert B.has_renderer_signature("Timeout 30000ms exceeded") is False


def test_renderer_signature_out_of_memory():
    """OOM-краш вкладки: «Out of memory», «Aw, Snap!», гибель контекста."""
    assert B.has_renderer_signature("Out of memory: renderer process") is True
    assert B.has_renderer_signature("Aw, Snap! Something went wrong") is True
    assert B.has_renderer_signature(
        "Execution context was destroyed, most likely because of a navigation") is True


# ---- is_renderer_failure ----------------------------------------------------

def test_is_renderer_failure_true():
    from playwright.sync_api import Error as PWError, TimeoutError
    assert B.is_renderer_failure(PWError(
        "net::ERR_ABORTED: The frame was detached from the DOM")) is True
    assert B.is_renderer_failure(PWError("Out of memory: renderer")) is True
    assert B.is_renderer_failure(PWError("Browser has been closed.")) is True
    assert B.is_renderer_failure(TimeoutError("Timeout 15000ms exceeded")) is True


def test_is_renderer_failure_false():
    from playwright.sync_api import Error as PWError
    assert B.is_renderer_failure(PWError("Timeout 30000ms exceeded")) is False
    assert B.is_renderer_failure(PWError("waiting for selector")) is False
    assert B.is_renderer_failure(ValueError("boom")) is False
    assert B.is_renderer_failure(RuntimeError("boom")) is False


# ---- Classification ---------------------------------------------------------

def test_classification_should_recycle():
    assert B.Classification("browser").should_recycle is True
    assert B.Classification("renderer_failure").should_recycle is True
    assert B.Classification("access_challenge").should_recycle is True
    assert B.Classification("target").should_recycle is False
    assert B.Classification("unknown").should_recycle is False


def test_classification_failure_type():
    """ADR §14.1: разделение product/session/browser/access failure."""
    assert B.Classification("access_challenge").failure_type == "access_failure"
    assert B.Classification("browser").failure_type == "browser_failure"
    assert B.Classification("renderer_failure").failure_type == "browser_failure"
    assert B.Classification("target").failure_type == "browser_failure"
    assert B.Classification("structure").failure_type == "product_failure"
    assert B.Classification("variant_short").failure_type == "product_failure"
    assert B.Classification("unknown").failure_type == "unknown"
    # access_failure ≠ product_failure (не штрафует PID)
    assert (B.Classification("access_challenge").failure_type
            != B.Classification("variant_short").failure_type)


# ---- классификация ошибок ---------------------------------------------------

def test_classify_browser_closed():
    from playwright.sync_api import Error as PWError
    c = B.classify_error(PWError("Browser has been closed."))
    assert c.error_kind == "browser"
    assert c.should_recycle is True


def test_classify_target_closed():
    from playwright.sync_api import Error as PWError
    c = B.classify_error(
        PWError("Target page, context or browser has been closed"))
    assert c.error_kind == "browser"


def test_classify_timeout_is_unknown():
    from playwright.sync_api import Error as PWError
    c = B.classify_error(PWError("Timeout 30000ms exceeded"))
    assert c.error_kind == "unknown"
    assert c.should_recycle is False


class _HangPage:
    """Зависший renderer: контрольная проба не возвращается (TimeoutError)."""

    def wait_for_function(self, *args, **kwargs):
        from playwright.sync_api import TimeoutError
        raise TimeoutError("Timeout 4000ms exceeded")


class _LivePage:
    """Живая страница: контрольная проба отвечает мгновенно."""

    def __init__(self):
        self.calls = []

    def wait_for_function(self, expression, **kwargs):
        self.calls.append((expression, kwargs))
        return None


def test_classify_hang_probe_is_bounded_and_not_raf():
    """У evaluate в Playwright нет таймаута: проба зависшего renderer'а
    обязана быть ограничена драйвером и не полагаться на rAF (на скрытой
    вкладке он не приходит). Живой случай 2026-11-06: 38 минут блокировки
    главного потока, из-за чего Ctrl+C не срабатывал."""
    from playwright.sync_api import TimeoutError
    page = _LivePage()
    c = B.classify_error(TimeoutError("Timeout 60000ms exceeded"),
                         sess=_FakeSess(page=page))
    assert c.error_kind == "unknown"
    assert page.calls, "контрольная проба не вызывалась"
    _, kwargs = page.calls[0]
    assert 1000 <= kwargs["timeout"] <= B.HANG_PROBE_TIMEOUT_MS
    assert isinstance(kwargs["polling"], (int, float))
    assert kwargs["polling"] > 0


def test_classify_hang_without_bounded_probe_does_not_call_evaluate():
    """Нет wait_for_function → unknown, а не неограниченный evaluate.

    evaluate на зависшем renderer'е не возвращается и вешает главный поток
    навсегда; такой fallback запрещён."""

    class _EvalOnlyPage:
        def evaluate(self, *args, **kwargs):
            raise AssertionError("неограниченный evaluate вызван")

    from playwright.sync_api import TimeoutError
    c = B.classify_error(TimeoutError("Timeout 60000ms exceeded"),
                         sess=_FakeSess(page=_EvalOnlyPage()))
    assert c.error_kind == "unknown"
    assert c.should_recycle is False


def test_classify_timeout_hang_recycles():
    """TimeoutError + страница не отвечает на контрольную пробу →
    renderer_hang, recyclable (перезапуск браузера)."""
    from playwright.sync_api import TimeoutError
    c = B.classify_error(
        TimeoutError("Timeout 15000ms exceeded"),
        sess=_FakeSess(page=_HangPage()))
    assert c.error_kind == "renderer_failure"
    assert c.probe_state == "renderer_hang"
    assert c.should_recycle is True


def test_classify_timeout_live_page_not_recyclable():
    """TimeoutError, но страница жива (контрольная проба отвечает) →
    временная задержка, НЕ recycle."""
    from playwright.sync_api import TimeoutError
    c = B.classify_error(
        TimeoutError("Timeout 15000ms exceeded"),
        sess=_FakeSess(page=_LivePage()))
    assert c.error_kind == "unknown"
    assert c.should_recycle is False


def test_classify_non_playwright_error():
    c = B.classify_error(RuntimeError("something else"))
    assert c.error_kind == "unknown"


def test_classify_access_challenge():
    """Анти-бот-заглушка (FAB «Похоже, нет соединения») — отдельный error_kind,
    recyclable (перезапуск браузера), НЕ маскируется под unknown."""
    c = B.classify_error(B.AccessChallengeError(
        "анти-бот (product_page): title:похоже, нет соединения"))
    assert c.error_kind == "access_challenge"
    assert c.should_recycle is True


def test_classify_without_session_is_unknown():
    from playwright.sync_api import Error as PWError
    c = B.classify_error(PWError("Some other Playwright error"), sess=None)
    assert c.error_kind == "unknown"


def test_classify_renderer_signature_recorded():
    from playwright.sync_api import Error as PWError
    c = B.classify_error(PWError(
        "net::ERR_ABORTED: The frame was detached from the DOM"))
    assert c.signature is True
    assert c.error_kind == "unknown"          # без сессии сигнатуру не подтвердить


# ---- BrowserProbe: cdp_http_ok ----------------------------------------------

def test_cdp_http_ok_true(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"Browser": "Chrome/123", "Protocol-Version": "1.3"}'

    monkeypatch.setattr(
        B.urllib.request, "urlopen",
        lambda url, timeout: _Resp())
    assert B.BrowserProbe().cdp_http_ok("http://127.0.0.1:9222") is True


def test_cdp_http_ok_false_on_exception(monkeypatch):
    def boom(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(B.urllib.request, "urlopen", boom)
    assert B.BrowserProbe().cdp_http_ok("http://127.0.0.1:9222") is False


def test_cdp_http_ok_false_without_browser(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{}'

    monkeypatch.setattr(
        B.urllib.request, "urlopen",
        lambda url, timeout: _Resp())
    assert B.BrowserProbe().cdp_http_ok("http://127.0.0.1:9222") is False


# ---- BrowserProbe: json_list_targets ----------------------------------------

def test_json_list_targets_returns_list(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'[{"type": "page", "url": "https://ozon.ru"}]'

    monkeypatch.setattr(
        B.urllib.request, "urlopen",
        lambda url, timeout: _Resp())
    data = B.BrowserProbe().json_list_targets("http://127.0.0.1:9222")
    assert isinstance(data, list) and len(data) == 1


def test_json_list_targets_none_on_error(monkeypatch):
    def boom(url, timeout):
        raise OSError("refused")

    monkeypatch.setattr(B.urllib.request, "urlopen", boom)
    assert B.BrowserProbe().json_list_targets("http://127.0.0.1:9222") is None


# ---- BrowserProbe: page_target_state ----------------------------------------

def test_page_target_state_none_without_page():
    sess = _FakeSess()
    assert B.BrowserProbe().page_target_state(sess) is None


def test_page_target_state_closed_page(monkeypatch):
    monkeypatch.setattr(B.BrowserProbe, "json_list_targets",
                        lambda self, url: [])
    sess = _FakeSess(page=_FakePage(closed=True))
    assert B.BrowserProbe().page_target_state(sess) == "gone"


def test_page_target_state_unknown_on_broken_transport():
    sess = _FakeSess(page=_FakePage(raise_on_is_closed=True))
    assert B.BrowserProbe().page_target_state(sess) is None


def test_page_target_state_alive_via_page(monkeypatch):
    monkeypatch.setattr(B.BrowserProbe, "json_list_targets",
                        lambda self, url: None)
    sess = _FakeSess(page=_FakePage(closed=False))
    assert B.BrowserProbe().page_target_state(sess) == "alive"


def test_page_target_state_gone_no_page_targets(monkeypatch):
    monkeypatch.setattr(B.BrowserProbe, "json_list_targets",
                        lambda self, url: [{"type": "other"}])
    sess = _FakeSess(page=_FakePage(closed=False))
    assert B.BrowserProbe().page_target_state(sess) == "gone"


def test_page_target_state_alive_with_page_target(monkeypatch):
    monkeypatch.setattr(
        B.BrowserProbe, "json_list_targets",
        lambda self, url: [{"type": "page", "url": "https://ozon.ru"}])
    sess = _FakeSess(page=_FakePage(closed=False))
    assert B.BrowserProbe().page_target_state(sess) == "alive"


# ---- BrowserProbe: probe ----------------------------------------------------

def test_probe_no_identity_falls_back_to_cdp(monkeypatch):
    """Внешний браузер (не наш, identity=None) → владение не проверяем.

    Регресс: раньше `ident is None` означал BROWSER_DEAD, поэтому любая
    неопознанная PW-ошибка приводила к лишнему recycle здорового браузера.
    Теперь судим по CDP HTTP — это и есть сигнал живости.
    """
    seen = []
    monkeypatch.setattr(P, "identity_alive", lambda i: seen.append(i) or True)
    monkeypatch.setattr(B.BrowserProbe, "cdp_http_ok", lambda self, url: True)
    monkeypatch.setattr(B.BrowserProbe, "page_target_state",
                        lambda self, s: "alive")
    state, attempts = B.BrowserProbe().probe(_FakeSess(identity=None),
                                             signature=False)
    assert state == B.ProbeState.HEALTHY
    assert seen == []          # identity_alive не вызывался вовсе


def test_probe_never_reads_foreign_identity_attr():
    """probe() обязан читать атрибут, который реально кладёт cdp.Session.

    Раньше читался несуществующий `browser_identity` → ident всегда None →
    BROWSER_DEAD на каждой неопознанной PW-ошибке (лишний recycle) и
    бесполезная probe_state телеметрия.
    """
    import inspect
    from ozon_collector import cdp
    assert "browser_identity" not in inspect.getsource(B.BrowserProbe.probe)
    assert "browser_identity" not in inspect.getsource(cdp.Session)


def test_probe_browser_dead_identity_gone(monkeypatch):
    monkeypatch.setattr(P, "identity_alive", lambda i: False)
    _cdp_down(monkeypatch)
    _no_os_procs(monkeypatch)
    sess = _FakeSess(identity=_identity())
    state, attempts = B.BrowserProbe().probe(sess, signature=False)
    assert state == B.ProbeState.BROWSER_DEAD


def test_probe_browser_dead_without_snapshot(monkeypatch):
    """Снимка browser_pid нет, но процесса тоже нет → смерть доказуема.

    Регресс: раньше ident=None означал «владение не проверяем», поэтому мёртвый
    браузер без снимка (external, Firefox-private, гонка перечисления сразу
    после launch) уезжал в BROWSER_UNHEALTHY — телеметрия врала, что браузер
    жив, и диагностика инцидента теряла время.
    """
    _cdp_down(monkeypatch)
    _no_os_procs(monkeypatch)
    state, _ = B.BrowserProbe().probe(_FakeSess(identity=None), signature=False)
    assert state == B.ProbeState.BROWSER_DEAD


def test_probe_unhealthy_when_process_alive_but_cdp_silent(monkeypatch):
    """Процесс на (порт, профиль) есть, CDP молчит → транзиент, не смерть."""
    _cdp_down(monkeypatch)
    monkeypatch.setattr(P, "browser_processes",
                        lambda url, profile_token=None: [_identity()])
    state, attempts = B.BrowserProbe(retries=2, interval=0.01).probe(
        _FakeSess(identity=None), signature=False)
    assert state == B.ProbeState.BROWSER_UNHEALTHY
    assert attempts == 3          # retries+1 попыток


def test_probe_never_calls_browser_dead_for_external_owner(monkeypatch):
    """ADR §14.1: чужой браузер — смерть недоказуема, рестарт запрещён.

    У external профиль не наш, поэтому в перечислении его процесса не будет
    никогда: без этой защиты probe объявил бы BROWSER_DEAD и воркер пошёл бы
    перезапускать/убивать НЕ НАШ процесс.
    """
    _cdp_down(monkeypatch)
    _no_os_procs(monkeypatch)
    state, _ = B.BrowserProbe().probe(
        _FakeSess(identity=None, owner="external"), signature=False)
    assert state == B.ProbeState.BROWSER_UNHEALTHY


def test_probe_stale_snapshot_does_not_kill_live_browser(monkeypatch):
    """Протухший снимок (рестарт без reconnect) ≠ смерть.

    Снимок указывает на мёртвый PID, но на (порт, профиль) есть живой процесс:
    BROWSER_DEAD здесь означал бы убийство/рестарт здорового браузера.
    """
    _cdp_down(monkeypatch)
    monkeypatch.setattr(P, "identity_alive", lambda i: False)
    monkeypatch.setattr(P, "browser_processes",
                        lambda url, profile_token=None: [_identity(pid=2000)])
    state, _ = B.BrowserProbe().probe(
        _FakeSess(identity=_identity(pid=1000)), signature=False)
    assert state == B.ProbeState.BROWSER_UNHEALTHY


def test_os_process_alive_failsafe_on_enumeration_error(monkeypatch):
    """Не смогли перечислить процессы → считаем живыми (никогда не death)."""
    def boom(*a, **kw):
        raise RuntimeError("wmi down")

    monkeypatch.setattr(P, "browser_processes", boom)
    monkeypatch.setattr(P, "firefox_browser_processes", boom)
    assert B.BrowserProbe().os_process_alive("http://127.0.0.1:9222") is True


def test_os_process_alive_uses_firefox_lookup(monkeypatch):
    seen = []
    monkeypatch.setattr(P, "firefox_browser_processes",
                        lambda token: seen.append(token) or [_identity()])
    assert B.BrowserProbe().os_process_alive(
        "http://127.0.0.1:9222", "ozon-profile", "firefox") is True
    assert seen == ["ozon-profile"]


def test_probe_browser_unhealthy_no_http(monkeypatch):
    monkeypatch.setattr(P, "identity_alive", lambda i: True)
    monkeypatch.setattr(B.BrowserProbe, "cdp_http_ok", lambda self, url: False)
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    sess = _FakeSess(identity=_identity(), page=_FakePage(closed=False))
    state, attempts = B.BrowserProbe(retries=2, interval=0.01).probe(
        sess, signature=False)
    assert state == B.ProbeState.BROWSER_UNHEALTHY
    assert attempts == 3          # retries+1 попыток


def test_probe_renderer_failure_with_signature(monkeypatch):
    monkeypatch.setattr(P, "identity_alive", lambda i: True)
    monkeypatch.setattr(B.BrowserProbe, "cdp_http_ok", lambda self, url: True)
    monkeypatch.setattr(B.BrowserProbe, "json_list_targets",
                        lambda self, url: [])
    sess = _FakeSess(identity=_identity(), page=_FakePage(closed=False))
    state, _ = B.BrowserProbe().probe(sess, signature=True)
    assert state == B.ProbeState.RENDERER_FAILURE


def test_probe_target_lost_without_signature(monkeypatch):
    monkeypatch.setattr(P, "identity_alive", lambda i: True)
    monkeypatch.setattr(B.BrowserProbe, "cdp_http_ok", lambda self, url: True)
    monkeypatch.setattr(B.BrowserProbe, "json_list_targets",
                        lambda self, url: [])
    sess = _FakeSess(identity=_identity(), page=_FakePage(closed=False))
    state, _ = B.BrowserProbe().probe(sess, signature=False)
    assert state == B.ProbeState.TARGET_LOST


def test_probe_healthy(monkeypatch):
    monkeypatch.setattr(P, "identity_alive", lambda i: True)
    monkeypatch.setattr(B.BrowserProbe, "cdp_http_ok", lambda self, url: True)
    monkeypatch.setattr(
        B.BrowserProbe, "json_list_targets",
        lambda self, url: [{"type": "page", "url": "https://ozon.ru"}])
    sess = _FakeSess(identity=_identity(), page=_FakePage(closed=False))
    state, _ = B.BrowserProbe().probe(sess, signature=False)
    assert state == B.ProbeState.HEALTHY


def test_probe_unknown_when_page_state_unknown(monkeypatch):
    monkeypatch.setattr(P, "identity_alive", lambda i: True)
    monkeypatch.setattr(B.BrowserProbe, "cdp_http_ok", lambda self, url: True)
    monkeypatch.setattr(B.BrowserProbe, "page_target_state",
                        lambda self, sess: None)
    sess = _FakeSess(identity=_identity(), page=_FakePage())
    state, _ = B.BrowserProbe().probe(sess, signature=False)
    assert state == B.ProbeState.UNKNOWN


# ---- классификация с сессией (probe-решение) -------------------------------

def test_classify_browser_when_probe_dead(monkeypatch):
    from playwright.sync_api import Error as PWError
    monkeypatch.setattr(B.BrowserProbe, "probe",
                        lambda self, sess, sig:
                        (B.ProbeState.BROWSER_DEAD, 0))
    sess = _FakeSess(identity=_identity())
    c = B.classify_error(PWError("Unknown pw error"), sess=sess)
    assert c.error_kind == "browser"
    assert c.probe_state == B.ProbeState.BROWSER_DEAD
    assert c.should_recycle is True


def test_classify_renderer_failure_when_probe_renderer(monkeypatch):
    from playwright.sync_api import Error as PWError
    monkeypatch.setattr(B.BrowserProbe, "probe",
                        lambda self, sess, sig:
                        (B.ProbeState.RENDERER_FAILURE, 2))
    sess = _FakeSess(identity=_identity())
    c = B.classify_error(PWError("Unknown pw error"), sess=sess)
    assert c.error_kind == "renderer_failure"
    assert c.probe_attempts == 2
    assert c.should_recycle is True


def test_classify_target_when_probe_target_lost(monkeypatch):
    from playwright.sync_api import Error as PWError
    monkeypatch.setattr(B.BrowserProbe, "probe",
                        lambda self, sess, sig:
                        (B.ProbeState.TARGET_LOST, 1))
    sess = _FakeSess(identity=_identity())
    c = B.classify_error(PWError("Unknown pw error"), sess=sess)
    assert c.error_kind == "target"
    assert c.should_recycle is False


def test_classify_unknown_when_probe_healthy(monkeypatch):
    from playwright.sync_api import Error as PWError
    monkeypatch.setattr(B.BrowserProbe, "probe",
                        lambda self, sess, sig:
                        (B.ProbeState.HEALTHY, 1))
    sess = _FakeSess(identity=_identity())
    c = B.classify_error(PWError("Unknown pw error"), sess=sess)
    assert c.error_kind == "unknown"
    assert c.should_recycle is False


# ---- BrowserProcessManager: launch ------------------------------------------

def _patch_manager(monkeypatch, processes=None, cdp_ready=None):
    """Подмена окружения manager: browser_processes/_cdp_ready/sleep/launch_script."""
    m = B.BrowserProcessManager(
        cdp_url="http://127.0.0.1:9222",
        launch_wait_s=0.2, kill_wait_s=0.2, capture_interval=0.01)
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    monkeypatch.setattr(B, "_launch_script",
                        lambda cdp_url, profile_path=None,
                        profile_token=None: None)
    if processes is not None:
        monkeypatch.setattr(P, "browser_processes",
                            lambda *a, **kw: processes() if callable(processes)
                            else processes)
    if cdp_ready is not None:
        monkeypatch.setattr(B.BrowserProcessManager, "_cdp_ready",
                            lambda self: cdp_ready() if callable(cdp_ready)
                            else cdp_ready)
    return m


def test_launch_success(monkeypatch):
    ident = _identity()
    m = _patch_manager(monkeypatch,
                       processes=[ident], cdp_ready=True)
    rec = m.launch("R1")
    assert rec is not None
    assert rec.lifecycle == P.Lifecycle.CDP_READY
    assert rec.partial is False
    assert rec.identity == ident


def test_launch_process_appeared_but_no_cdp(monkeypatch):
    ident = _identity()
    m = _patch_manager(monkeypatch,
                       processes=[ident], cdp_ready=False)
    rec = m.launch("R1")
    assert rec is not None
    assert rec.lifecycle == P.Lifecycle.ALIVE
    assert rec.partial is True


def test_launch_no_process_returns_none(monkeypatch):
    m = _patch_manager(monkeypatch, processes=[], cdp_ready=False)
    assert m.launch("R1") is None


def test_launch_guards_launcher_window_early(monkeypatch):
    """launch() hands the launcher pid to the early window watcher at once.

    Audit regression 2026-10-19: on the recycle path (restart -> launch) the
    new pid stayed unguarded until _await_launch returned — the window could
    flash in the gap.
    """
    import types
    from ozon_collector import cdp as cdp_mod
    seen, order = [], []
    monkeypatch.setattr(
        cdp_mod, "_watch_launcher_window",
        lambda url, run, **kw: (order.append("watch"),
                                seen.append((url, run))))
    m = _patch_manager(monkeypatch, processes=[], cdp_ready=False)
    sentinel = types.SimpleNamespace(
        path="t.log", proc=types.SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(B, "_launch_script",
                        lambda cdp_url, profile_path=None,
                        profile_token=None: sentinel)

    def _rec_await(self, *a, **kw):
        order.append("await")
        return None

    monkeypatch.setattr(B.BrowserProcessManager, "_await_launch", _rec_await)
    assert m.launch("R1") is None
    assert seen == [("http://127.0.0.1:9222", sentinel)]
    assert order == ["watch", "await"], \
        "сторож обязан навешиваться ДО ожидания CDP"


def test_launch_multiple_matches_raises(monkeypatch):
    m = _patch_manager(monkeypatch,
                       processes=[_identity(1000), _identity(1001)],
                       cdp_ready=True)
    with pytest.raises(B.BrowserOwnershipError):
        m.launch("R1")


# ---- BrowserProcessManager: restart -----------------------------------------

def test_restart_success(monkeypatch):
    old = _rec(1000)
    monkeypatch.setattr(P, "kill_records",
                        lambda records, wait_s=None: records)
    ident = _identity(1001, "k2")
    calls = {"n": 0}

    def processes(*a, **kw):
        # первый вызов — проверка чужих после остановки (пусто),
        # дальнейшие — launch подхватывает новый процесс
        calls["n"] += 1
        return [] if calls["n"] == 1 else [ident]

    m = _patch_manager(monkeypatch, processes=processes, cdp_ready=True)
    old.set_lifecycle(P.Lifecycle.VERIFIED_DEAD)
    result = m.restart([old], "R2")
    assert result.created is not None
    assert result.created.lifecycle == P.Lifecycle.CDP_READY
    assert result.old == [old]
    assert result.killed == [old]


def test_restart_fails_when_old_not_dead(monkeypatch):
    old = _rec(1000)
    monkeypatch.setattr(P, "kill_records", lambda records, wait_s=None: [])
    m = _patch_manager(monkeypatch, processes=[], cdp_ready=True)
    with pytest.raises(B.BrowserOwnershipError):
        m.restart([old], "R2")


def test_restart_fails_on_foreign_process(monkeypatch):
    old = _rec(1000)
    monkeypatch.setattr(P, "kill_records",
                        lambda records, wait_s=None: records)
    foreign = _identity(2000, "foreign")
    m = _patch_manager(monkeypatch,
                       processes=[foreign], cdp_ready=True)
    old.set_lifecycle(P.Lifecycle.VERIFIED_DEAD)
    with pytest.raises(B.BrowserOwnershipError):
        m.restart([old], "R2")


def test_restart_fails_when_new_never_appears(monkeypatch):
    old = _rec(1000)
    monkeypatch.setattr(P, "kill_records",
                        lambda records, wait_s=None: records)
    m = _patch_manager(monkeypatch,
                       processes=[], cdp_ready=False)
    old.set_lifecycle(P.Lifecycle.VERIFIED_DEAD)
    with pytest.raises(B.BrowserRecoveryError):
        m.restart([old], "R2")


# ---- BrowserProcessManager: force_stop --------------------------------------

def test_force_stop_foreign_process_raises(monkeypatch):
    own = _rec(1000)
    foreign = _identity(2000, "foreign")
    m = _patch_manager(monkeypatch, processes=[foreign])
    with pytest.raises(B.BrowserOwnershipError):
        m.force_stop([own], wait_s=0.1)
    assert own.lifecycle == P.Lifecycle.CREATED     # ничего не убито


def test_force_stop_own_processes(monkeypatch):
    own = _rec(1000)
    m = _patch_manager(monkeypatch, processes=[own.identity])
    killed = mock.Mock(return_value=[own])
    monkeypatch.setattr(P, "kill_records", killed)
    m.force_stop([own], wait_s=0.1)
    killed.assert_called_once()


# ---- BrowserWatchdog --------------------------------------------------------

def test_watchdog_check_passes_when_alive():
    w = B.BrowserWatchdog("http://127.0.0.1:9222")
    w.check()          # живой по умолчанию — не должно бросать
    assert w.alive is True


def test_watchdog_check_raises_when_dead():
    w = B.BrowserWatchdog("http://127.0.0.1:9222")
    w._dead = True
    with pytest.raises(B.BrowserRecoveryError):
        w.check()
    assert w.alive is False


def test_watchdog_debounce_grace(monkeypatch):
    """Плохой сэмпл сам по себе НЕ убивает флаг: нужно окно >= grace_s."""
    t = {"now": 1000.0}
    monkeypatch.setattr(B.time, "monotonic", lambda: t["now"])
    w = B.BrowserWatchdog("http://127.0.0.1:9222", grace_s=4.0)
    w._report(True)
    assert w.alive is True
    t["now"] = 1002.0
    w._report(False)              # bad-окно 0с < grace_s
    assert w.alive is True
    t["now"] = 1006.5             # bad-окно 4.5с >= grace_s
    w._report(False)
    assert w.alive is False
    t["now"] = 1010.0
    w._report(True)               # снова жив — флаг сброшен
    assert w.alive is True


def test_watchdog_sample_http_ok_skips_process(monkeypatch):
    """CDP HTTP отвечает → процесс (дорогой PowerShell) не опрашиваем."""
    def boom(url):
        raise AssertionError("browser_processes не должен вызываться")

    w = B.BrowserWatchdog("http://127.0.0.1:9222")
    monkeypatch.setattr(w.probe, "cdp_http_ok", lambda url: True)
    monkeypatch.setattr(P, "browser_processes", boom)
    assert w._sample() is True


def test_watchdog_sample_http_down_process_alive(monkeypatch):
    """CDP молчит, но процесс ещё жив → транзиент, НЕ смерть."""
    w = B.BrowserWatchdog("http://127.0.0.1:9222")
    monkeypatch.setattr(w.probe, "cdp_http_ok", lambda url: False)
    monkeypatch.setattr(P, "browser_processes",
                        lambda url, **kw: [_identity()])
    assert w._sample() is True


def test_watchdog_sample_dead(monkeypatch):
    """CDP молчит И процесса нет → браузер мёртв."""
    w = B.BrowserWatchdog("http://127.0.0.1:9222")
    monkeypatch.setattr(w.probe, "cdp_http_ok", lambda url: False)
    monkeypatch.setattr(P, "browser_processes", lambda url, **kw: [])
    assert w._sample() is False


def test_watchdog_start_starts_thread(monkeypatch):
    monkeypatch.setattr(B.BrowserProbe, "cdp_http_ok", lambda self, url: True)
    w = B.BrowserWatchdog("http://127.0.0.1:9222", poll_interval=0.01)
    w.start()
    assert w._thread is not None
    assert w._thread.is_alive()
    w.stop()
    w._thread.join(2)
    assert not w._thread.is_alive()


def test_classify_watchdog_error_recyclable():
    """BrowserRecoveryError (сторож зафиксировал смерть) → browser, recyclable."""
    c = B.classify_error(B.BrowserRecoveryError("watchdog: Chromium мёртв"))
    assert c.error_kind == "browser"
    assert c.probe_state == "watchdog"
    assert c.should_recycle is True
