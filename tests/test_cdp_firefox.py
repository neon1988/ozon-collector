"""Firefox-бэкенд (OZON_BROWSER=firefox): моки, без живого Firefox."""
from __future__ import annotations

import os

import pytest

import ozon_collector.cdp as cdp
from ozon_collector import browser as B
from ozon_collector import process as P


# ---- browser_kind -----------------------------------------------------------

class TestBrowserKind:
    def test_default_chrome(self, monkeypatch):
        """Дефолт = chrome с 2026-09-25: marionette-сессии Firefox блокируются
        заглушкой, Chromium CDP проходит (см. browser_kind)."""
        monkeypatch.delenv("OZON_BROWSER", raising=False)
        assert B.browser_kind() == "chrome"

    def test_chrome_when_env_set(self, monkeypatch):
        monkeypatch.setenv("OZON_BROWSER", "chrome")
        assert B.browser_kind() == "chrome"

    def test_firefox_when_env_set(self, monkeypatch):
        monkeypatch.setenv("OZON_BROWSER", "firefox")
        assert B.browser_kind() == "firefox"

    def test_normalizes_case_and_space(self, monkeypatch):
        monkeypatch.setenv("OZON_BROWSER", "  FireFox ")
        assert B.browser_kind() == "firefox"

    def test_invalid_raises(self, monkeypatch):
        monkeypatch.setenv("OZON_BROWSER", "safari")
        with pytest.raises(ValueError) as e:
            B.browser_kind()
        assert "OZON_BROWSER" in str(e.value)


# ---- process: профиль и матчинг -------------------------------------------

class TestFirefoxProfilePath:
    def test_suffix_firefox(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OZON_PROFILE_BASE", str(tmp_path))
        p = P.firefox_profile_path("ozon-profile")
        assert p == str(tmp_path / "ozon-profile.firefox")
        # Firefox-профиль НЕ разделяет Chromium-профиль того же токена
        assert p != P.profile_path("ozon-profile")

    def test_worker_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OZON_PROFILE_BASE", str(tmp_path))
        assert P.firefox_profile_path("ozon-profile-worker-2").endswith(
            "ozon-profile-worker-2.firefox")


class TestMatchesFirefoxProfile:
    def test_match_separate_args(self, tmp_path):
        prof = str(tmp_path / "ozon-profile.firefox")
        argv = [r"C:\x\firefox.exe", "-no-remote", "-profile", prof]
        assert P._matches_firefox_profile(argv, prof) is True

    def test_match_quoted(self, tmp_path):
        prof = str(tmp_path / "ozon-profile.firefox")
        argv = [r"C:\x\firefox.exe", "-profile", f'"{prof}"']
        assert P._matches_firefox_profile(argv, prof) is True

    def test_no_match_other_profile(self, tmp_path):
        prof = str(tmp_path / "ozon-profile.firefox")
        argv = [r"C:\x\firefox.exe", "-profile",
                str(tmp_path / "other.firefox")]
        assert P._matches_firefox_profile(argv, prof) is False

    def test_no_profile_arg(self, tmp_path):
        prof = str(tmp_path / "ozon-profile.firefox")
        argv = [r"C:\x\firefox.exe", "-headless"]
        assert P._matches_firefox_profile(argv, prof) is False


class TestFirefoxBrowserProcesses:
    def _fake_iter(self, monkeypatch, items):
        monkeypatch.setattr(P, "_iter_firefox_processes", lambda: iter(items))
        monkeypatch.setattr(
            P, "process_identity",
            lambda pid: P.ProcessIdentity(pid, f"k{pid}"))

    def test_filters_by_profile(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OZON_PROFILE_BASE", str(tmp_path))
        prof = str(tmp_path / "ozon-profile.firefox")
        self._fake_iter(monkeypatch, [
            (111, ["firefox.exe", "-profile", prof], 1),
            (222, ["firefox.exe", "-profile",
                   str(tmp_path / "else.firefox")], 1),
            (333, ["firefox.exe"], 1),
        ])
        idents = P.firefox_browser_processes("ozon-profile")
        assert [i.pid for i in idents] == [111]

    def test_empty_when_no_processes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OZON_PROFILE_BASE", str(tmp_path))
        self._fake_iter(monkeypatch, [])
        assert P.firefox_browser_processes("ozon-profile") == []


# ---- watchdog ---------------------------------------------------------------

class TestWatchdogFirefox:
    def _wd(self, monkeypatch, procs):
        monkeypatch.setattr(P, "firefox_browser_processes",
                            lambda token: procs)
        return B.BrowserWatchdog("http://127.0.0.1:9999",
                                 profile_token="ozon-profile", kind="firefox")

    def test_sample_alive(self, monkeypatch):
        ident = P.ProcessIdentity(42, "k42")
        wd = self._wd(monkeypatch, [ident])
        assert wd._sample() is True

    def test_sample_dead(self, monkeypatch):
        wd = self._wd(monkeypatch, [])
        assert wd._sample() is False

    def test_kind_from_env(self):
        wd = B.BrowserWatchdog("http://127.0.0.1:9999")
        assert wd.kind == "chrome"  # env закреплён автофикстурой conftest

    def test_default_kind_chrome(self, monkeypatch):
        monkeypatch.delenv("OZON_BROWSER", raising=False)
        wd = B.BrowserWatchdog("http://127.0.0.1:9999")
        assert wd.kind == "chrome"


# ---- Session._connect_firefox ----------------------------------------------

class _FakeCtx:
    def __init__(self, page=None):
        self.pages = [page] if page is not None else []
        self.new_page_calls = 0

    def new_page(self):
        self.new_page_calls += 1
        p = _FakePage()
        self.pages.append(p)
        return p


class _FakePage:
    def __init__(self):
        self.closed = False

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


class _FakeAdapter:
    """Адаптер browser: .contexts[0] + quit() — контракт cdp.Session для Firefox."""

    def __init__(self, page):
        self.contexts = [_FakeCtx(page)]
        self.quit_called = False

    def quit(self):
        self.quit_called = True


class _FakeDriver:
    def __init__(self):
        self.session_id = "fake-session"


class TestParallelEngine:
    """E (2026-09-25): firefox в parallel разрешён БЕЗ CDP.

    Каждый firefox-воркер = отдельный процесс со СВОИМ stock Firefox через
    geckodriver/Selenium на своём профиле; живость оркестратор оценивает по
    процессу воркера + heartbeat, НЕ по CDP (см. resolve_browser_for_parallel
    в cdp.py). resolve возвращает kind как есть — движок выбирает только
    пользователь (OZON_BROWSER / --browser).
    """
    def test_default_chrome_passes_through(self, monkeypatch):
        monkeypatch.delenv("OZON_BROWSER", raising=False)
        assert B.resolve_browser_for_parallel() == "chrome"

    def test_chrome_passthrough(self, monkeypatch):
        monkeypatch.setenv("OZON_BROWSER", "chrome")
        assert B.resolve_browser_for_parallel() == "chrome"

    def test_whitespace_case_normalized(self, monkeypatch):
        monkeypatch.setenv("OZON_BROWSER", "  Chrome ")
        assert B.resolve_browser_for_parallel() == "chrome"

    def test_explicit_firefox_allowed(self, monkeypatch):
        monkeypatch.setenv("OZON_BROWSER", "firefox")
        assert B.resolve_browser_for_parallel() == "firefox"


class TestSessionConnectFirefox:
    def _install(self, monkeypatch, procs=None):
        """Мок-окружение firefox-сессии: env, ffsel.connect, process-поиск."""
        monkeypatch.setenv("OZON_BROWSER", "firefox")
        ident = P.ProcessIdentity(777, "k777")
        if procs is None:
            procs = [ident]
        monkeypatch.setattr(P, "firefox_browser_processes",
                            lambda token: list(procs))
        monkeypatch.setattr(P, "kill_records", lambda recs, **kw: [])
        monkeypatch.setattr(cdp, "browser_alive",
                            lambda url: (_ for _ in ()).throw(
                                AssertionError("firefox не зовёт browser_alive")))
        monkeypatch.setattr(
            cdp, "launch_and_wait",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("firefox не зовёт launch_and_wait")))
        state = {"calls": [], "pages": [], "adapters": []}

        def fake_connect(profile_path=None, prefs=None, binary=None, **kw):
            state["calls"].append({"profile_path": profile_path,
                                   "prefs": prefs, "binary": binary, **kw})
            page = _FakePage()
            adapter = _FakeAdapter(page)
            state["pages"].append(page)
            state["adapters"].append(adapter)
            return _FakeDriver(), page, adapter

        monkeypatch.setattr(cdp.ffsel, "connect", fake_connect)
        return state

    def test_connect_sets_up_firefox_backend(self, monkeypatch):
        state = self._install(monkeypatch)
        s = cdp.Session("http://127.0.0.1:9999")
        assert s.kind == "firefox"
        assert s.browser_owner == "collector"
        assert s.browser_profile.endswith("ozon-profile.firefox")
        assert state["calls"], "ffsel.connect не вызван"
        call = state["calls"][0]
        assert call["profile_path"] == s.browser_profile
        assert call["prefs"] == cdp.FIREFOX_USER_PREFS
        assert call["binary"] is None
        adapter = state["adapters"][0]
        assert s._browser is adapter
        assert s.page is state["pages"][0]
        assert s.browser_pid is not None
        assert s._managed_pages == [s.page]

    def test_private_mode_uses_temp_profile(self, monkeypatch):
        state = self._install(monkeypatch)
        monkeypatch.setenv("OZON_FF_PRIVATE", "1")
        cdp.Session("http://127.0.0.1:9999")
        assert state["calls"][0]["profile_path"] is None

    def test_stale_profile_killed_before_launch(self, monkeypatch):
        ident = P.ProcessIdentity(777, "k777")
        killed = []
        monkeypatch.setenv("OZON_BROWSER", "firefox")

        def procs(token):
            # до kill — stale процесс, после — профиль уже освобождён
            return [] if killed else [ident]

        monkeypatch.setattr(P, "firefox_browser_processes", procs)
        monkeypatch.setattr(P, "kill_records",
                            lambda recs, **kw: killed.extend(recs) or [])
        monkeypatch.setattr(cdp, "browser_alive", lambda url: False)
        monkeypatch.setattr(cdp, "launch_and_wait", lambda *a, **kw: True)
        state = {"n": 0}

        def fake_connect(profile_path=None, prefs=None, binary=None, **kw):
            state["n"] += 1
            page = _FakePage()
            return _FakeDriver(), page, _FakeAdapter(page)

        monkeypatch.setattr(cdp.ffsel, "connect", fake_connect)
        s = cdp.Session("http://127.0.0.1:9999")
        assert killed, "stale firefox не был остановлен перед запуском"
        assert state["n"] == 1, "ffsel.connect не вызван"
        assert s.browser_pid is None  # браузер запущен, мок-поиск вернул []

    def test_driver_closed_on_close(self, monkeypatch):
        state = self._install(monkeypatch)
        s = cdp.Session("http://127.0.0.1:9999")
        adapter = state["adapters"][0]
        s.close()
        # firefox: владение драйвером → quit() (окно закрыто, куки в профиле)
        assert adapter.quit_called is True
        assert s._browser is None

    def test_reconnect_recreates_page(self, monkeypatch):
        state = self._install(monkeypatch)
        s = cdp.Session("http://127.0.0.1:9999")
        first_page = s.page
        s.reconnect()
        assert len(state["calls"]) == 2
        assert s.page is not first_page
        assert s._managed_pages == [s.page]

    def test_window_not_minimized(self, monkeypatch):
        """Свёрнутое окно Firefox не монтирует lazy-блоки отзывов (0 отзывов).

        Живой замер 2026-09-25: cli.py gather дал 0 карточек при minimized,
        тот же профиль с видимым окном — 12..151. Поэтому окно разворачиваем
        без активации фокуса, а не сворачиваем."""
        self._install(monkeypatch)
        calls = []
        monkeypatch.setattr(cdp.Session, "_minimize_window",
                            lambda self: calls.append("minimize"))
        monkeypatch.setattr(cdp.Session, "_keep_window_visible",
                            lambda self: calls.append("keep"))
        monkeypatch.setattr(cdp.Session, "_restore_window_native",
                            lambda self, **kw: calls.append(("restore", kw)))
        monkeypatch.setattr(cdp.Session, "_firefox_pids",
                            lambda self: [777])
        cdp.Session("http://127.0.0.1:9999")
        assert "minimize" not in calls
        assert ("restore", {"pids": [777],
                            "window_class": cdp.FIREFOX_WINDOW_CLASS}) in calls

    def test_keep_window_env_restores_instead(self, monkeypatch):
        self._install(monkeypatch)
        calls = []
        monkeypatch.setenv("OZON_KEEP_WINDOW", "1")
        monkeypatch.setattr(cdp.Session, "_minimize_window",
                            lambda self: calls.append("minimize"))
        monkeypatch.setattr(cdp.Session, "_keep_window_visible",
                            lambda self: calls.append("keep"))
        monkeypatch.setattr(cdp.Session, "_restore_window_native",
                            lambda self, **kw: calls.append(("restore", kw)))
        monkeypatch.setattr(cdp.Session, "_firefox_pids", lambda self: [777])
        cdp.Session("http://127.0.0.1:9999")
        assert calls == ["keep"]

    def test_new_page_never_minimizes_window(self, monkeypatch):
        """new_page (ensure_single_worker_tab на каждый товар) — тоже без minimize.

        Живой прогон 12 воркеров 2026-09-25: воркер сворачивал окно в
        new_page → Firefox выгружал вкладку → «Browsing context has been
        discarded» на первом товаре.
        """
        state = self._install(monkeypatch)
        calls = []
        monkeypatch.setattr(cdp.Session, "_minimize_window",
                            lambda self: calls.append("minimize"))
        monkeypatch.setattr(cdp.Session, "_keep_window_visible",
                            lambda self: calls.append("keep"))
        monkeypatch.setattr(cdp.Session, "_restore_window_native",
                            lambda self, **kw: calls.append(("restore", kw)))
        monkeypatch.setattr(cdp.Session, "_firefox_pids", lambda self: [777])
        s = cdp.Session("http://127.0.0.1:9999")
        calls.clear()
        s.new_page()
        assert "minimize" not in calls
        assert "keep" not in calls
        assert calls == [("restore", {"pids": [777],
                                     "window_class": cdp.FIREFOX_WINDOW_CLASS})]

    def test_unload_tabs_pref_disabled(self):
        """Firefox не должен выгружать вкладки при нехватке памяти.

        Гейт внутри Firefox (TabUnloader, omni.ja): getBoolPref
        ("browser.tabs.unloadOnLowMemory", true) — с true 12 воркеров
        теряли рабочую вкладку.
        """
        assert cdp.FIREFOX_USER_PREFS["browser.tabs.unloadOnLowMemory"] is False


# ---- geometry: firefox short-circuit ---------------------------------------

class TestGeometryFirefox:
    def _session(self, monkeypatch, kind="firefox"):
        import unittest.mock as mock
        s = cdp.Session.__new__(cdp.Session)
        s.kind = kind
        s.browser_pid = type("P", (), {"pid": 1234})()
        m = mock.patch.object(cdp, "_physical_window_rect",
                              return_value=[10, 20, 1600, 900])
        return s, m

    def test_assert_geometry_ok_no_cdp_model(self, monkeypatch):
        s, m = self._session(monkeypatch)
        page = object()  # new_cdp_session не должна вызываться
        with m:
            ok, metrics = s._assert_geometry_ok(page)
        assert ok is True
        assert "cdp_bounds" not in metrics
        assert metrics["browser_kind"] == "firefox"
        assert metrics["hwnd_rect"] == [10, 20, 1600, 900]

    def test_normalize_returns_false_without_cdp(self, monkeypatch):
        s, m = self._session(monkeypatch)
        with m:
            # физическое окно 1600x900 ≠ 1440x900 — chromium пошёл бы в CDP
            assert s._normalize_window_geometry() is False


# ---- search._recycle_browser ------------------------------------------------

class TestRecycleFirefox:
    def test_firefox_restart_kills_and_reconnects(self, monkeypatch):
        from ozon_collector import search as S

        ident = P.ProcessIdentity(555, "k555")
        killed = []
        monkeypatch.setattr(P, "firefox_browser_processes",
                            lambda token: [ident])
        monkeypatch.setattr(P, "kill_records",
                            lambda recs, **kw: killed.extend(recs) or [])
        monkeypatch.setattr(
            P, "browser_processes",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("firefox-recycle не зовёт chromium-поиск")))

        class Sess:
            kind = "firefox"
            browser_owner = "collector"
            profile_token = "ozon-profile"
            cdp_url = "http://127.0.0.1:9222"
            reconnected = 0

            def reconnect(self):
                self.reconnected += 1

            def ensure_single_worker_tab(self):
                raise AssertionError("не должен вызываться")

        class Manager:
            kill_wait_s = 1.0
            profile_token = "ozon-profile"

        sess = Sess()
        S._recycle_browser(sess, Manager(), "launch-1")
        assert len(killed) == 1
        assert killed[0].launch_id == "launch-1"
        assert sess.reconnected == 1

    def test_chromium_path_untouched(self, monkeypatch):
        """kind не задан (ручная сессия) → chromium-ветка (регистр browser_processes)."""
        from ozon_collector import search as S

        called = {"chromium": 0, "firefox": 0}
        monkeypatch.setattr(P, "firefox_browser_processes",
                            lambda token: called.__setitem__("firefox", 1))
        monkeypatch.setattr(P, "browser_processes",
                            lambda *a, **kw: called.__setitem__(
                                "chromium", 1) or [])
        monkeypatch.setattr(P, "kill_records", lambda recs, **kw: [])
        monkeypatch.setattr(P, "ProcessRecord", lambda *a, **kw: object())

        class Sess:
            browser_owner = "collector"
            profile_token = "ozon-profile"
            cdp_url = "http://127.0.0.1:9222"

            def reconnect(self):
                pass

            def ensure_single_worker_tab(self):
                pass

        class Manager:
            kill_wait_s = 1.0
            profile_token = "ozon-profile"
            restarts = 0

            def restart(self, old, launch_id):
                self.restarts += 1

        sess = Sess()
        S._recycle_browser(Sess(), Manager(), "launch-2")
        assert called["chromium"] == 1
        assert called["firefox"] == 0


# ---- Session.goto: самовосстановление после потери вкладки -----------------

class _LostTabError(Exception):
    pass


_LostTabError.__name__ = "NoSuchWindowException"


class TestGotoSelfHealFirefox:
    def _sess(self):
        class _WD:
            def check(self):
                pass

        s = cdp.Session.__new__(cdp.Session)
        s.kind = "firefox"
        s._watchdog = _WD()
        s._fingerprint_captured = True
        s._managed_pages = []
        return s

    def test_discarded_context_reopens_tab_and_retries(self, monkeypatch):
        s = self._sess()
        state = {"goto": 0, "new_page": 0, "waits": 0}

        class Page:
            def goto(self, url, wait_until=None, timeout=None):
                state["goto"] += 1
                if state["goto"] == 1:
                    raise _LostTabError("Message: Browsing context has been "
                                        "discarded")

            def wait_for_timeout(self, ms):
                state["waits"] += 1

        s.page = Page()
        s.new_page = lambda close_current=True: state.__setitem__(
            "new_page", state["new_page"] + 1)
        monkeypatch.setattr(cdp, "is_renderer_failure", lambda e: False)
        s.goto("https://www.ozon.ru/product/1", wait_ms=1)
        assert state == {"goto": 2, "new_page": 1, "waits": 1}

    def test_chromium_not_self_healed(self, monkeypatch):
        """kind=chrome: NoSuchWindow = мёртвый браузер → recycle, не новая вкладка."""
        s = self._sess()
        s.kind = "chrome"
        state = {"goto": 0, "new_page": 0}

        class Page:
            def goto(self, url, wait_until=None, timeout=None):
                state["goto"] += 1
                raise _LostTabError("Message: Browsing context has been "
                                    "discarded")

            def wait_for_timeout(self, ms):
                pass

        s.page = Page()
        s.new_page = lambda close_current=True: state.__setitem__(
            "new_page", state["new_page"] + 1)
        monkeypatch.setattr(cdp, "is_renderer_failure", lambda e: False)
        s.goto("https://www.ozon.ru/product/1")
        assert state["new_page"] == 0


class TestSingleWorkerTabFirefox:
    """Инвариант «одна рабочая вкладка»: старая закрывается, НОВАЯ живая.

    Регрессия 2026-09-25: шим закрывал текущую (новую) вкладку вместо старой
    → следующий goto/new_window падал «Browsing context has been discarded».
    """

    def _sess(self):
        class _WD:
            def check(self):
                pass

        s = cdp.Session.__new__(cdp.Session)
        s.kind = "firefox"
        s.cdp_url = None
        s._watchdog = _WD()
        s._fingerprint_captured = True
        s._managed_pages = []
        s.keep_window = False
        s._apply_window_policy = lambda why=None: None
        s._firefox_pids = lambda: []
        return s

    def test_old_tabs_closed_and_new_one_survives(self, monkeypatch):
        s = self._sess()
        ctx = _FakeCtx()
        s._browser = _FakeAdapter(ctx)
        s._browser.contexts = [ctx]
        old = _FakePage()
        s.page = old
        s._managed_pages = [old]

        new = s.ensure_single_worker_tab()
        assert new is s.page
        assert new.closed is False
        assert old.closed is True
        assert s._managed_pages == [new]
        assert ctx.new_page_calls == 1

    def test_second_call_keeps_latest_tab_alive(self, monkeypatch):
        s = self._sess()
        ctx = _FakeCtx()
        s._browser = _FakeAdapter(ctx)
        s._browser.contexts = [ctx]
        first = _FakePage()
        s.page = first
        s._managed_pages = [first]

        second = s.ensure_single_worker_tab()
        third = s.ensure_single_worker_tab()
        assert second.closed is True
        assert first.closed is True
        assert third.closed is False
        assert s._managed_pages == [third]

    def test_new_page_close_current_closes_old_only(self, monkeypatch):
        s = self._sess()
        ctx = _FakeCtx()
        s._browser = _FakeAdapter(ctx)
        s._browser.contexts = [ctx]
        old = _FakePage()
        s.page = old
        s._managed_pages = [old]

        new = s.new_page(close_current=True)
        assert old.closed is True
        assert new.closed is False
        assert s.page is new
        assert s._managed_pages == [new]
