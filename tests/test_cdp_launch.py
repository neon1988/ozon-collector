"""Тесты авто-запуска CDP-браузера (моки, без живого Chromium)."""
from __future__ import annotations

from contextlib import ExitStack

import pytest

import ozon_collector.cdp as cdp
from ozon_collector import browser as B


def _patch_alive(sequence):
    """mock.browser_alive: значения по очереди, дальше — последнее."""
    import unittest.mock as mock

    state = {"i": 0}

    def _alive(cdp_url=None):
        i = state["i"]
        state["i"] = min(i + 1, len(sequence) - 1)
        return sequence[i]

    return (
        mock.patch.object(cdp, "browser_alive", _alive),
        mock.patch.object(cdp, "_launch_browser"),
        mock.patch.object(cdp.subprocess, "Popen"),
    )


class TestLaunchAndWait:
    def test_already_alive_no_launch(self):
        with ExitStack() as s:
            p_alive, p_launch, p_popen = _patch_alive([True])
            m_launch = s.enter_context(p_launch)
            s.enter_context(p_alive)
            s.enter_context(p_popen)
            assert cdp.launch_and_wait("http://127.0.0.1:9999", wait_s=5) is True
            m_launch.assert_not_called()

    def test_launches_when_dead(self):
        with ExitStack() as s:
            p_alive, p_launch, p_popen = _patch_alive([False, True])
            m_launch = s.enter_context(p_launch)
            s.enter_context(p_alive)
            s.enter_context(p_popen)
            assert cdp.launch_and_wait("http://127.0.0.1:9999", wait_s=5) is True
            m_launch.assert_called_once()

    def test_timeout_raises(self):
        with ExitStack() as s:
            p_alive, p_launch, p_popen = _patch_alive([False])
            for p in (p_alive, p_launch, p_popen):
                s.enter_context(p)
            with pytest.raises(RuntimeError) as e:
                cdp.launch_and_wait("http://127.0.0.1:9999", wait_s=0.1)
            assert "launch_chrome" in str(e.value)


class TestLaunchBrowser:
    def test_port_from_url(self, monkeypatch):
        captured = {}

        def fake_popen(cmd, **kw):
            captured["env"] = kw["env"]
            return None

        monkeypatch.setattr(cdp.subprocess, "Popen", fake_popen)
        cdp._launch_browser("http://127.0.0.1:12345")
        assert captured["env"]["PORT"] == "12345"

    def test_default_port_9222(self, monkeypatch):
        captured = {}

        def fake_popen(cmd, **kw):
            captured["env"] = kw["env"]
            return None

        monkeypatch.setattr(cdp.subprocess, "Popen", fake_popen)
        cdp._launch_browser("http://127.0.0.1")
        assert captured["env"]["PORT"] == "9222"

    def test_script_missing_raises(self, monkeypatch, tmp_path):
        missing = tmp_path / "launch_chrome.sh"
        monkeypatch.setattr(cdp, "_launch_script_path", lambda: missing)
        with pytest.raises(RuntimeError) as e:
            cdp._launch_browser("http://127.0.0.1:9999")
        assert "launch_chrome" in str(e.value)


class TestSessionAutoLaunch:
    def _install(self, monkeypatch, alive, connect=None):
        """Подмена окружения Session: browser_alive, launch_and_wait, playwright."""
        launches = {"n": 0}
        monkeypatch.setattr(cdp, "_launch_browser", lambda *a, **kw: None)
        monkeypatch.setattr(
            cdp, "launch_and_wait",
            lambda *a, **kw: launches.__setitem__("n", launches["n"] + 1) or True)
        monkeypatch.setattr(cdp, "browser_alive", lambda url: alive())

        class FakeBrowser:
            contexts = [type("FakeCtx", (), {"pages": [object()]})()]

        class FakeChromium:
            def __init__(self):
                self.fail_on = connect

            def connect_over_cdp(self, url):
                if self.fail_on is not None:
                    return self.fail_on(url)
                return FakeBrowser()

        class FakePW:
            def __init__(self):
                self.chromium = FakeChromium()

            def start(self):
                return self

        monkeypatch.setattr(cdp, "sync_playwright", lambda: FakePW())
        return launches

    def test_launch_when_dead(self, monkeypatch):
        seq = iter([False, True])
        launches = self._install(monkeypatch, lambda: next(seq))
        s = cdp.Session("http://127.0.0.1:9999")
        assert s._browser is not None
        assert launches["n"] == 1

    def test_no_launch_when_alive(self, monkeypatch):
        seq = iter([True])
        launches = self._install(monkeypatch, lambda: next(seq))
        s = cdp.Session("http://127.0.0.1:9999")
        assert s._browser is not None
        assert launches["n"] == 0

    def test_env_disable(self, monkeypatch):
        monkeypatch.setenv("OZON_AUTO_LAUNCH", "0")
        launches = self._install(monkeypatch, lambda: True)
        s = cdp.Session("http://127.0.0.1:9999")
        assert s.auto_launch is False
        assert s._browser is not None

    def test_retry_after_connect_failure(self, monkeypatch):
        fail = {"flag": True}

        def connect(url):
            if fail["flag"]:
                fail["flag"] = False
                raise RuntimeError("TargetClosedError")
            return type("FakeBrowser", (),
                        {"contexts": [type("FakeCtx", (), {"pages": [object()]})()]})()

        launches = self._install(monkeypatch, lambda: True, connect=connect)
        s = cdp.Session("http://127.0.0.1:9999")
        assert s._browser is not None
        assert launches["n"] == 1

    def test_no_retry_when_auto_off(self, monkeypatch):
        monkeypatch.setenv("OZON_AUTO_LAUNCH", "0")
        calls = {"n": 0}

        def connect(url):
            raise RuntimeError("TargetClosedError")

        self._install(monkeypatch, lambda: False, connect=connect)
        monkeypatch.setattr(
            cdp, "launch_and_wait",
            lambda *a, **kw: calls.__setitem__("n", calls["n"] + 1) or True)
        with pytest.raises(RuntimeError):
            cdp.Session("http://127.0.0.1:9999")
        assert calls["n"] == 0

    def test_reconnect_after_close(self, monkeypatch):
        """reconnect: закрывает старую сессию и подключается заново."""
        self._install(monkeypatch, lambda: True)
        s = cdp.Session("http://127.0.0.1:9999")
        assert s._browser is not None
        old_browser = s._browser
        s.reconnect()
        assert s._browser is not None
        assert s._browser is not old_browser
        assert s.cdp_url == "http://127.0.0.1:9999"


class _FakePage:
    def __init__(self):
        self.evals = 0
        self.evaluated = []

    def evaluate(self, js, *args, **kwargs):
        self.evals += 1
        self.evaluated.append(js)
        return 2

    def goto(self, *a, **kw):
        return None

    def wait_for_timeout(self, ms):
        return None

    def is_closed(self):
        return False


class TestSessionWatchdogGuard:
    def _session(self, monkeypatch):
        TestSessionAutoLaunch()._install(monkeypatch, lambda: True)
        s = cdp.Session("http://127.0.0.1:9999")
        s.page = _FakePage()
        return s

    def test_guard_raises_when_watchdog_dead(self, monkeypatch):
        """Мёртвый сторож → eval падает BrowserRecoveryError ДО обращения к page."""
        s = self._session(monkeypatch)
        s._watchdog = B.BrowserWatchdog("http://127.0.0.1:9999")
        s._watchdog._dead = True
        with pytest.raises(B.BrowserRecoveryError):
            s.eval("1+1")
        assert s.page.evals == 0

    def test_guard_auto_starts_watchdog_and_scope(self, monkeypatch):
        """Первая guarded-операция стартует сторож; close() его останавливает."""
        monkeypatch.setattr(B.BrowserWatchdog, "_sample", lambda self: True)
        s = self._session(monkeypatch)
        assert s._watchdog is None
        s.eval("1+1")
        assert s._watchdog is not None
        assert s._watchdog._thread is not None
        assert s._watchdog._thread.is_alive()
        s.wait(10)
        s.scroll(100)
        s.goto("https://www.ozon.ru/product/1", wait_ms=0)
        assert s.page.evals >= 1
        assert any("window.scrollBy" in e for e in s.page.evaluated)
        s.close()
        assert s._watchdog is None
