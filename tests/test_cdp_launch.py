"""Тесты авто-запуска CDP-браузера (моки, без живого Chromium)."""
from __future__ import annotations

from contextlib import ExitStack
import unittest.mock as mock

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


class TestFreeStaleProfile:
    """Освобождение профиля, который держит нерабочий экземпляр браузера."""

    def _install(self, monkeypatch, procs=None, killed=None, marker=True,
                 raises=None):
        """procs — что вернёт browser_processes; killed — что вернёт kill."""
        def fake_procs(cdp_url, profile_token=None):
            if raises is not None:
                raise raises
            return list(procs or [])

        m_kill = mock.Mock(return_value=list(killed or []))
        monkeypatch.setattr(cdp.P, "browser_processes", fake_procs)
        monkeypatch.setattr(cdp.P, "kill_identities", m_kill)
        monkeypatch.setattr(cdp, "_launch_marker", lambda p: marker)
        return m_kill

    def test_no_holders_is_noop(self, monkeypatch):
        m_kill = self._install(monkeypatch)
        assert cdp._free_stale_profile("http://127.0.0.1:9222", "ozon-profile") == []
        m_kill.assert_not_called()

    def test_our_profile_holder_is_killed(self, monkeypatch):
        holders = [cdp.P.ProcessIdentity(pid=111, start_key="k1"),
                   cdp.P.ProcessIdentity(pid=222, start_key="k2")]
        m_kill = self._install(monkeypatch, procs=holders, killed=[111, 222])
        # потомков нет — ждать освобождения нечего
        monkeypatch.setattr(cdp.P, "wait_profile_released",
                            lambda *_a, **_k: [])
        freed = cdp._free_stale_profile("http://127.0.0.1:9222", "ozon-profile")
        assert freed == [111, 222]
        m_kill.assert_called_once_with(holders)

    def test_surviving_children_are_killed_and_profile_freed(self, monkeypatch):
        """Потомки renderer/gpu держат профиль — их надо дождаться и добить.

        Живой замер 2026-10-03 16:0x: MAIN найден и убит, семь процессов-потомков
        остались жить, следующий Chromium EXITED с «profile captured by a live
        instance» — четыре запуска подряд. browser_processes() их не видит
        (ищет по порту и отбрасывает --type=), поэтому нужен отдельный проход.
        """
        holders = [cdp.P.ProcessIdentity(pid=111, start_key="k1")]
        children = [cdp.P.ProcessIdentity(pid=222, start_key="k2"),
                    cdp.P.ProcessIdentity(pid=333, start_key="k3")]
        m_kill = self._install(monkeypatch, procs=holders, killed=[111])
        m_prof = mock.Mock(return_value=children)
        monkeypatch.setattr(cdp.P, "profile_processes", m_prof)
        waits = []

        def _wait(_token, timeout_s=8.0):
            waits.append(timeout_s)
            return children if not waits[-1] or timeout_s >= 8.0 else []

        # первый вызов (после MAIN) возвращает потомков, второй — пусто
        monkeypatch.setattr(cdp.P, "wait_profile_released",
                            mock.Mock(side_effect=[[222, 333], []]))
        m_kill2 = mock.Mock(return_value=[222, 333])
        monkeypatch.setattr(cdp.P, "kill_identities", m_kill2)
        cdp._free_stale_profile("http://127.0.0.1:9222", "ozon-profile")
        killed_sets = [c[0][0] for c in m_kill2.call_args_list]
        assert children in killed_sets, \
            f"потомки должны быть добиты, убито: {killed_sets}"

    def test_foreign_profile_untouched(self, monkeypatch):
        """Без маркера запуска профиль считается чужим — окно не рвём."""
        holders = [cdp.P.ProcessIdentity(pid=333, start_key="k3")]
        m_kill = self._install(monkeypatch, procs=holders, killed=[333],
                              marker=False)
        assert cdp._free_stale_profile("http://127.0.0.1:9222", "ozon-profile") == []
        m_kill.assert_not_called()

    def test_listing_error_is_not_fatal(self, monkeypatch):
        m_kill = self._install(monkeypatch, raises=RuntimeError("WMI down"))
        assert cdp._free_stale_profile("http://127.0.0.1:9222", "ozon-profile") == []
        m_kill.assert_not_called()


class TestLaunchAndWaitRetry:
    """Необратимый сценарий: профиль держит мёртвый-по-CDP экземпляр."""

    def _install(self, alive_seq, freed):
        """Возвращает (patcher_launch, mock_free) — вход в ExitStack."""
        p_alive, p_launch, p_popen = _patch_alive(alive_seq)
        m_free = mock.Mock(return_value=freed)
        return p_alive, p_launch, p_popen, m_free

    def test_retries_after_freeing_profile(self, monkeypatch):
        # alive: мёртв на старте, мёртв в цикле 1-й попытки, жив во 2-й
        p_alive, p_launch, p_popen, m_free = self._install([False, False, True],
                                                           [4242])
        with ExitStack() as s:
            m = s.enter_context(p_launch)
            for p in (p_alive, p_popen):
                s.enter_context(p)
            monkeypatch.setattr(cdp, "_free_stale_profile", m_free)
            assert cdp.launch_and_wait("http://127.0.0.1:9999", wait_s=5) is True
            assert m.call_count == 2, "профиль освободили — запускаемся заново"

    def test_no_retry_when_nothing_to_free(self, monkeypatch):
        with ExitStack() as s:
            p_alive, p_launch, p_popen = _patch_alive([False])
            m = s.enter_context(p_launch)
            for p in (p_alive, p_popen):
                s.enter_context(p)
            m_free = s.enter_context(
                mock.patch.object(cdp, "_free_stale_profile", return_value=[]))
            with pytest.raises(RuntimeError) as e:
                cdp.launch_and_wait("http://127.0.0.1:9999", wait_s=0.1)
            assert m.call_count == 1
            m_free.assert_called_once()
            assert "профиль свободен" in str(e.value)

    def test_error_reports_freed_pids(self, monkeypatch):
        with ExitStack() as s:
            p_alive, p_launch, p_popen = _patch_alive([False])
            m = s.enter_context(p_launch)
            for p in (p_alive, p_popen):
                s.enter_context(p)
            s.enter_context(mock.patch.object(cdp, "_free_stale_profile",
                                              return_value=[777]))
            with pytest.raises(RuntimeError) as e:
                cdp.launch_and_wait("http://127.0.0.1:9999", wait_s=0.1)
            assert m.call_count == 2, "освободили профиль — вторая попытка"
            assert "освобождён от pid=[777]" in str(e.value)


@pytest.mark.allow_browser_launch
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
            contexts = [type("FakeCtx", (), {"pages": [object()],
                                             "new_page": lambda self=0: object()})()]

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

    def test_connects_to_shifted_port_not_dead_one(self, monkeypatch):
        """База сдвинулась (9222 забрала Windows) — connect идёт на живой порт.

        Живой замер 2026-10-03: браузер поднялся на 9444, но Session._connect
        уходил в connect_over_cdp на исходный 9222 → ECONNREFUSED, хотя
        браузер был жив. Порт обязан разрешаться ДО подключения.
        """
        seen = {}
        launches = self._install(monkeypatch, lambda: False)
        monkeypatch.setattr(cdp, "browser_alive",
                            lambda url: url.endswith(":9444"))
        monkeypatch.setattr(cdp.P, "profile_debug_ports",
                            lambda token=None: {9444: [35968]})
        real_connect = None

        class FakeBrowser:
            contexts = [type("FakeCtx", (), {"pages": [object()],
                                             "new_page": lambda self=0: object()})()]

        class FakeChromium:
            def connect_over_cdp(self, url):
                seen["url"] = url
                return FakeBrowser()

        class FakePW:
            chromium = FakeChromium()

            def start(self):
                return self

        monkeypatch.setattr(cdp, "sync_playwright", lambda: FakePW())
        s = cdp.Session("http://127.0.0.1:9222")
        assert real_connect is None
        assert seen["url"] == "http://127.0.0.1:9444", \
            f"подключились к {seen['url']}, а жив наш браузер на 9444"
        assert s.cdp_url == "http://127.0.0.1:9444", \
            "Session.cdp_url остался на мёртвом порту — воркеры возьмут его"

    def test_effective_cdp_url_prefers_alive_given_url(self, monkeypatch):
        monkeypatch.setattr(cdp, "browser_alive", lambda url: True)
        assert cdp.effective_cdp_url("http://127.0.0.1:9222") == \
            "http://127.0.0.1:9222"

    def test_effective_cdp_url_falls_back_to_free_base(self, monkeypatch):
        monkeypatch.setattr(cdp, "browser_alive", lambda url: False)
        monkeypatch.setattr(cdp.P, "profile_debug_ports",
                            lambda token=None: {})
        monkeypatch.setattr(cdp.P, "cdp_port_base", lambda: (9447, "свободен"))
        assert cdp.effective_cdp_url("http://127.0.0.1:9222") == \
            "http://127.0.0.1:9447"

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
                        {"contexts": [type("FakeCtx", (), {"pages": [object()],
                                                           "new_page": lambda self=0: object()})()]})()

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

    def test_guard_blocks_extract_on_invalid_page(self, monkeypatch):
        """ADR §14.1: INVALID-страница блокирует извлечение (K4).

        После challenge страница помечается is_invalid; любые eval/goto/scroll/
        wait на ней бросают AccessChallengeError ДО обращения к page."""
        s = self._session(monkeypatch)
        s.page.is_invalid = True
        with pytest.raises(B.AccessChallengeError):
            s.eval("1+1")
        assert s.page.evals == 0          # DOM не читался
        with pytest.raises(B.AccessChallengeError):
            s.scroll(100)
        with pytest.raises(B.AccessChallengeError):
            s.wait(10)
        # новая вкладка снимает INVALID
        s.page = _FakePage()
        s.eval("1+1")
        assert s.page.evals == 1


class _TabPage:
    """Мини-фейк вкладки: close + init_script для Session.new_page."""

    def __init__(self):
        self.closed = False
        self.init_scripts = []

    def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed

    def add_init_script(self, js):
        self.init_scripts.append(js)


class TestSessionNewPage:
    """new_page (recovery new_tab): новая вкладка в том же контексте браузера."""

    def _session(self):
        ctx = type("Ctx", (), {"new_tabs": []})()
        ctx.new_page = lambda: ctx.new_tabs.append(_TabPage()) or ctx.new_tabs[-1]
        browser = type("Browser", (),
                       {"contexts": [ctx]})()
        s = cdp.Session.__new__(cdp.Session)
        s.cdp_url = "http://127.0.0.1:9222"
        s._browser = browser
        s.page = _TabPage()
        return s, ctx

    def test_new_page_switches_active_and_closes_old(self):
        s, ctx = self._session()
        old = s.page
        new = s.new_page()
        assert s.page is new
        assert new is not old
        assert ctx.new_tabs == [new]
        assert old.closed is True
        # анти-детект init_script БОЛЬШЕ не ставится: подмена navigator.webdriver
        # делает свойство геттером (вместо data-свойства false) и Ozon отслеживает
        # это как признак бота → «нет соединения». Проверено эмпирически.
        assert new.init_scripts == []

    def test_new_page_keeps_old_when_close_false(self):
        s, ctx = self._session()
        old = s.page
        new = s.new_page(close_current=False)
        assert old.closed is False
        assert s.page is new

    def test_new_page_without_browser_raises(self):
        s = cdp.Session.__new__(cdp.Session)
        s._browser = None
        s.page = None
        with pytest.raises(RuntimeError):
            s.new_page()


# ---- этап 1: ownership (ADR §14.1) -------------------------------------------

def test_launch_marker_detects_collector_profile(tmp_path):
    """launch_chrome ставит маркер → Session считает профиль collector'ским."""
    assert cdp._launch_marker(str(tmp_path)) is False
    (tmp_path / ".ozon_collector_launched").write_text("2026-08-31", encoding="utf-8")
    assert cdp._launch_marker(str(tmp_path)) is True


class TestEnsureSingleWorkerTab:
    """ensure_single_worker_tab: закрывает только свои managed, чужие не трогает."""

    def _session_with_pages(self, n_managed=2, n_foreign=1):
        ctx = type("Ctx", (), {"new_tabs": []})()
        ctx.new_page = lambda: ctx.new_tabs.append(_TabPage()) or ctx.new_tabs[-1]
        browser = type("Browser", (),
                       {"contexts": [ctx]})()
        s = cdp.Session.__new__(cdp.Session)
        s.cdp_url = "http://127.0.0.1:9222"
        s._browser = browser
        # managed: несколько «своих»; foreign: чужая вкладка пользователя
        managed = [_TabPage() for _ in range(n_managed)]
        foreign = [_TabPage() for _ in range(n_foreign)]
        s._managed_pages = list(managed)
        s.page = managed[0]
        ctx.foreign = foreign
        return s, managed, foreign

    def test_closes_only_managed_and_keeps_one(self):
        s, managed, foreign = self._session_with_pages()
        cur = s.ensure_single_worker_tab()
        # все старые managed закрыты, кроме рабочей
        assert managed[0].closed is True
        assert managed[1].closed is True
        assert cur.closed is False
        assert s.page is cur
        assert s.collector_tabs() == 1
        # чужие вкладки НЕ тронуты
        assert all(f.closed is False for f in foreign)

    def test_new_page_failure_keeps_old(self):
        s, managed, _foreign = self._session_with_pages()
        # new_page упадёт: _browser.contexts пустой → RuntimeError
        s._browser = type("Browser", (), {"contexts": []})()
        s.page = managed[0]
        with pytest.raises(RuntimeError):
            s.ensure_single_worker_tab()
        # старая рабочая вкладка НЕ уничтожена
        assert managed[0].closed is False


class TestGeometryGuard:
    """Read-only инвариант HWND↔CDP перед решением пазла (моки, без окна)."""

    def _session(self, hwnd_rect, cdp_bounds, window_state=None):
        import unittest.mock as mock
        from ozon_collector import cdp as cdp_mod
        s = cdp.Session.__new__(cdp.Session)
        s.browser_pid = type("P", (), {"pid": 1234})()
        fake_sess = type("FakeCDP", (), {
            "send": staticmethod(
                lambda m, *a, **kw: (
                    {"windowId": 9} if m == "Browser.getWindowForTarget"
                    else {"bounds": dict(cdp_bounds,
                                         windowState=window_state),
                          "windowState": window_state}))})()
        ctx = type("Ctx", (), {})()
        ctx.new_cdp_session = lambda *a: fake_sess
        page = type("PPage", (), {"context": ctx})()
        m = mock.patch.object(cdp_mod, "_physical_window_rect",
                              return_value=hwnd_rect)
        return s, page, m

    def test_match_ok_window_state_ignored(self):
        """Совпадение размеров = ok; windowState='minimized' не мешает."""
        from ozon_collector import cdp
        s, page, m = self._session(
            [10, 20, 1440, 900],
            {"left": 0, "top": 0, "width": 1440, "height": 900},
            window_state="minimized")
        with m:
            ok, metrics = s._geometry_guard(page)
        assert ok is True
        assert metrics["cdp_window_state"] == "minimized"

    def test_mismatch_detected(self):
        """HWND шире CDP-модели на 160px → ok=False + оба прямоугольника."""
        from ozon_collector import cdp
        s, page, m = self._session(
            [10, 20, 1600, 900],
            {"left": 0, "top": 0, "width": 1440, "height": 900})
        with m:
            ok, metrics = s._geometry_guard(page)
        assert ok is False
        assert metrics["hwnd_rect"][2] == 1600
        assert metrics["cdp_bounds"][2] == 1440

    def test_no_pid_or_non_nt_noop(self):
        """Без browser_pid проверки нет (внешний браузер)."""
        from ozon_collector import cdp
        s, page, m = self._session(
            [10, 20, 1440, 900],
            {"left": 0, "top": 0, "width": 1440, "height": 900})
        s.browser_pid = None
        with m:
            ok, _ = s._geometry_guard(page)
        assert ok is True

    def test_cdp_unavailable_returns_true(self):
        """CDP-модель недоступна (bounds=None) — по одному HWND не судим.

        Моделируем ЯВНО: `send` отдаёт bounds=None. Раньше тест «проходил»
        потому, что fake падал на dict(None) и исключение гасилось внутри
        guard'а, — то есть проверял не то, что думал.
        """
        from ozon_collector import cdp
        s, page, m = self._session([10, 20, 1440, 900], None)
        with m:
            ok, metrics = s._geometry_guard(page)
        assert ok is True
        assert "cdp_bounds_err" in metrics or metrics.get("cdp_bounds") is None

    def test_guard_normalizes_and_rechecks_on_mismatch(self):
        """Расхождение → normalize геометрии → повторная проверка = ok.

        Патч `_physical_window_rect` живёт ВЕСЬ вызов (обычный `with`):
        recheck происходит уже после возврата из `_normalize_window_geometry`,
        поэтому «подменить и сразу снять» нельзя — вернётся старый прямоугольник.
        """
        from ozon_collector import cdp
        s, page, _m = self._session(
            [10, 20, 1600, 900],
            {"left": 0, "top": 0, "width": 1440, "height": 900})

        seen = []
        normalized = []

        def _rect(*_a, **_kw):
            # первый замер — широкий HWND (рассинхрон), дальше — выровненный
            seen.append(1)
            return ([10, 20, 1600, 900] if len(seen) == 1
                    else [10, 20, 1440, 900])

        with mock.patch.object(cdp, "_physical_window_rect", side_effect=_rect):
            s._normalize_window_geometry = lambda: normalized.append(1)
            ok, metrics = s._geometry_guard(page)

        assert ok is True
        assert metrics.get("normalized") is True
        assert normalized == [1], "normalize вызван ровно один раз"
        assert len(seen) >= 2, "без повторного замера recheck не доказать"

    def test_attach_on_page(self):
        from ozon_collector import cdp
        s, page, m = self._session([10, 20, 1440, 900],
                                   {"left": 0, "top": 0, "width": 1440,
                                    "height": 900})
        s.page = page
        s._attach_geom_guard()
        assert getattr(page, "_geom_guard", None) == s._geometry_guard


# ---- ревизия 2.9: Page Visibility/focus телеметрия fingerprint --------------

class _FpPage:
    """Страница, отдающая заранее заданный fingerprint-словарь."""

    def __init__(self, fp):
        self._fp = fp
        self.evals = 0

    def evaluate(self, script, *a, **kw):
        self.evals += 1
        if "innerWidth" in script:
            return self._fp
        return None


def _fp_session(fp):
    s = cdp.Session.__new__(cdp.Session)
    s.page = _FpPage(fp)
    s.keep_window = False
    s._fingerprint_captured = False
    s.fingerprint = None
    return s


def test_fingerprint_captures_page_visibility():
    fp = {"hidden": True, "visibility": "hidden", "focused": False,
          "webdriver": False, "ua": "UA", "vw": 1200}
    s = _fp_session(fp)
    s._capture_fingerprint_once()
    assert s.fingerprint == fp
    assert s.fingerprint["hidden"] is True
    assert s.fingerprint["visibility"] == "hidden"
    assert s.fingerprint["focused"] is False


def test_fingerprint_visibility_js_fields_present():
    """В evaluate-запросе должны быть поля видимости (иначе телеметрия пуста)."""
    captured = {}

    class _P:
        def evaluate(self, script, *a, **kw):
            captured["js"] = script
            return {"hidden": False, "visibility": "visible",
                    "webdriver": False}

    s = cdp.Session.__new__(cdp.Session)
    s.page = _P()
    s.keep_window = True
    s._fingerprint_captured = False
    s.fingerprint = None
    s._capture_fingerprint_once()
    js = captured["js"]
    for field in ("document.hidden", "visibilityState", "hasFocus()",
                  "outerWidth", "outerHeight", "navigator.webdriver"):
        assert field in js, field


def test_fingerprint_captured_once():
    s = _fp_session({"hidden": False, "visibility": "visible",
                     "webdriver": False})
    s._capture_fingerprint_once()
    first = s.page.evals
    s._capture_fingerprint_once()
    assert s.page.evals == first


def test_webdriver_true_logs_warning():
    """webdriver === true = браузер запущен с чужими флагами → warning."""
    import unittest.mock as mock
    s = _fp_session({"hidden": False, "visibility": "visible",
                     "webdriver": True})
    with mock.patch.object(cdp.log, "warning") as warn:
        s._capture_fingerprint_once()
    assert warn.called
    assert "webdriver" in warn.call_args[0][0]


def test_hidden_window_logs_info():
    import unittest.mock as mock
    s = _fp_session({"hidden": True, "visibility": "hidden",
                     "webdriver": False})
    with mock.patch.object(cdp.log, "info") as info:
        s._capture_fingerprint_once()
    assert any("hidden" in c[0][0] for c in info.call_args_list)


def test_visibility_telemetry_survives_broken_eval():
    class _Bad:
        def evaluate(self, script, *a, **kw):
            raise RuntimeError("detached")

    s = cdp.Session.__new__(cdp.Session)
    s.page = _Bad()
    s.keep_window = False
    s._fingerprint_captured = False
    s.fingerprint = None
    s._capture_fingerprint_once()          # не бросает
    assert s.fingerprint is None
