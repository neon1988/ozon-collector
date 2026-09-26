"""Firefox/Selenium-шим (ffsel): JS-интероп и селекторы, без живого Firefox."""
import pytest

from ozon_collector import ffsel, ffsel_js, ffsel_query


class _FakeDriver:
    def __init__(self):
        self.script_timeout = None
        self.scripts = []

    def set_script_timeout(self, s):
        self.script_timeout = s


class _FakeCtx:
    def __init__(self, page=None):
        self.pages = [page] if page else []


class TestJsIsFunction:
    def test_arrow_no_args(self):
        assert ffsel_js.js_is_function("() => document.title")

    def test_arrow_with_arg(self):
        assert ffsel_js.js_is_function("(dy) => window.scrollBy(0, dy)")

    def test_multiline_block(self):
        assert ffsel_js.js_is_function("""() => {
            const r = document.body.getBoundingClientRect();
            return r.width;
        }""")

    def test_async(self):
        assert ffsel_js.js_is_function("async () => await fetch('/x')")

    def test_function_keyword(self):
        assert ffsel_js.js_is_function("function () { return 1; }")

    def test_plain_expression(self):
        assert not ffsel_js.js_is_function("1+1")

    def test_parenthesised_expression_is_not_function(self):
        assert not ffsel_js.js_is_function("(1 + 1)")

    def test_none_safe(self):
        assert not ffsel_js.js_is_function(None)


class TestScriptWrapping:
    def test_function_script_resolves_promise(self):
        s = ffsel_js.function_script("() => fetch('/x')")
        assert "Promise.resolve" in s
        assert "execute" not in s
        assert "__done" in s
        assert ffsel_js.OK in s and ffsel_js.VAL in s

    def test_expression_script_returns(self):
        assert ffsel_js.expression_script("1+1") == "return (1+1);"


class TestUnwrap:
    def test_value_passthrough(self):
        assert ffsel_js.unwrap({"a": 1}) == {"a": 1}

    def test_marker_value(self):
        assert ffsel_js.unwrap({ffsel_js.OK: 1, ffsel_js.VAL: 7}) == 7

    def test_undefined_becomes_none(self):
        assert ffsel_js.unwrap({ffsel_js.OK: 1}) is None

    def test_error_raises(self):
        with pytest.raises(RuntimeError, match="boom"):
            ffsel_js.unwrap({ffsel_js.ERR: "boom"})

    def test_legit_dict_with_v_key_not_confused(self):
        # сырое значение-объект не должно читаться как маркер
        assert ffsel_js.unwrap({"v": 1}) == {"v": 1}


class TestXPathLiteral:
    def test_plain(self):
        assert ffsel_query.xq("Подтвердить") == "'Подтвердить'"

    def test_single_quote_switches_to_double(self):
        assert ffsel_query.xq("it's") == '"it\'s"'

    def test_both_quotes_use_concat(self):
        out = ffsel_query.xq("a'b\"c")
        assert out.startswith("concat(")


class TestRoleXPath:
    def test_role_only(self):
        assert ffsel_query.role_xpath("button") == \
            "//*[@role='button' or self::button]"

    def test_role_with_name(self):
        out = ffsel_query.role_xpath("button", "ОК")
        assert "contains(@aria-label, 'ОК')" in out


class TestSelectorHelpers:
    def test_visible_pseudo_stripped(self):
        assert ffsel_query.css_to_xpath("input:visible") == "input"

    def test_plain_selector_unchanged(self):
        assert ffsel_query.css_to_xpath("[data-widget]") == "[data-widget]"

    def test_none_safe(self):
        assert ffsel_query.css_to_xpath(None) == ""

    def test_xpath_detected(self):
        assert ffsel_query.is_xpath("//div")
        assert ffsel_query.is_xpath("(//div)[1]")
        assert not ffsel_query.is_xpath("div.foo")


class TestLocator:
    def _page(self):
        return ffsel.FirefoxPage(_FakeDriver())

    def test_first_is_single(self):
        loc = ffsel.SeleniumLocator(self._page(), "button")
        assert loc.first._single is True

    def test_get_by_role_builds_xpath(self):
        page = self._page()
        loc = page.get_by_role("button", name="Подтвердить")
        by, val = loc._by()
        assert by == "xpath"
        assert "Подтвердить" in val
        assert "button" in val

    def test_get_by_role_without_name(self):
        loc = self._page().get_by_role("button")
        _by, val = loc._by()
        assert "Подтвердить" not in val

    def test_count_zero_when_driver_fails(self):
        page = self._page()
        loc = ffsel.SeleniumLocator(page, "button")
        assert loc.count() == 0
        assert loc.is_enabled() is False
        assert loc.element_handle() is None

    def test_element_handle_is_wrapped(self):
        page = self._page()
        el = object()
        page._d.find_elements = lambda by, val: [el]
        h = ffsel.SeleniumLocator(page, "button").element_handle()
        assert isinstance(h, ffsel.SeleniumElement)
        assert h.raw is el

    def test_has_text_regex_filters(self):
        import re

        class _El:
            def __init__(self, text):
                self.text = text

        page = self._page()
        page._d.find_elements = lambda by, val: [
            _El("12 комментариев"), _El("Читать далее"), _El("1 комментарий")]
        loc = ffsel.SeleniumLocator(
            page, "button", has_text=re.compile(r"[1-9]\d*\s*комментар", re.I))
        assert loc.count() == 2

    def test_has_text_substring(self):
        class _El:
            def __init__(self, text):
                self.text = text

        page = self._page()
        page._d.find_elements = lambda by, val: [_El("Нет отзывов"), _El("ОК")]
        assert page.locator("button", has_text="ОК").count() == 1

    def test_has_text_survives_first(self):
        import re

        class _El:
            def __init__(self, text):
                self.text = text

        page = self._page()
        page._d.find_elements = lambda by, val: [_El("нет"), _El("1 комментарий")]
        loc = page.locator("button",
                           has_text=re.compile(r"комментар", re.I)).first
        assert loc.count() == 1


class TestElementClick:
    class _El:
        def __init__(self, shown=True, enabled=True, fail_first=0):
            self.shown, self.enabled, self.fail_first = shown, enabled, fail_first
            self.clicks = 0

        def is_displayed(self):
            return self.shown

        def is_enabled(self):
            return self.enabled

        def click(self):
            if self.fail_first > 0:
                self.fail_first -= 1
                raise RuntimeError("stale")
            self.clicks += 1

    def _page(self):
        return ffsel.FirefoxPage(_FakeDriver())

    def test_clicks_when_interactable(self):
        el = self._El()
        ffsel.SeleniumElement(self._page(), el).click()
        assert el.clicks == 1

    def test_retries_stale_then_clicks(self):
        el = self._El(fail_first=2)
        ffsel.SeleniumElement(self._page(), el).click(timeout=3000)
        assert el.clicks == 1

    def test_raises_when_never_interactable(self):
        el = self._El(shown=False)
        with pytest.raises(TimeoutError):
            ffsel.SeleniumElement(self._page(), el).click(timeout=200)

    def test_scroll_into_view_is_available(self):
        page = self._page()
        seen = []
        page._d.execute_script = lambda s, *a: seen.append(s)
        ffsel.SeleniumElement(page, object()).scroll_into_view_if_needed()
        assert seen and "scrollIntoView" in seen[0]


class TestEvaluateRouting:
    def test_function_goes_async_script(self):
        calls = []
        drv = _FakeDriver()
        drv.execute_async_script = lambda s, *a: calls.append(("async", s)) or {
            ffsel._OK: 1, ffsel._VAL: 5}
        drv.execute_script = lambda s, *a: calls.append(("sync", s))
        page = ffsel.FirefoxPage(drv)
        assert page.evaluate("() => 5") == 5
        assert calls[0][0] == "async"

    def test_expression_goes_sync_script(self):
        calls = []
        drv = _FakeDriver()
        drv.execute_script = lambda s, *a: calls.append(("sync", s)) or 2
        drv.execute_async_script = lambda s, *a: calls.append(("async", s))
        page = ffsel.FirefoxPage(drv)
        assert page.evaluate("1+1") == 2
        assert calls[0][0] == "sync"

    def test_timeout_kwarg_sets_script_timeout(self):
        drv = _FakeDriver()
        drv.execute_script = lambda s, *a: 1
        page = ffsel.FirefoxPage(drv)
        page.evaluate("1+1", timeout=5000)
        assert drv.script_timeout == 5.0

    def test_unknown_kwargs_ignored(self):
        drv = _FakeDriver()
        drv.execute_script = lambda s, *a: 1
        assert ffsel.FirefoxPage(drv).evaluate("1+1", foo=1) == 1

    def test_element_evaluate_passes_element_first(self):
        seen = {}
        drv = _FakeDriver()
        drv.execute_async_script = lambda s, *a: seen.update(args=a) or {
            ffsel._OK: 1, ffsel._VAL: None}
        page = ffsel.FirefoxPage(drv)
        el = object()
        ffsel.SeleniumElement(page, el).evaluate("(el) => el.tagName")
        assert seen["args"][0] is el


class TestPageBasics:
    def _page(self):
        drv = _FakeDriver()
        drv.current_url = "https://www.ozon.ru/product/1/"
        drv.title = "Ozon"
        drv.page_source = "<html></html>"
        drv.window_handles = ["w1"]
        drv.page_load_timeout = None
        drv.refresh = lambda: None
        drv.set_page_load_timeout = lambda s: setattr(
            drv, "page_load_timeout", s)
        return ffsel.FirefoxPage(drv)

    def test_url_and_title_and_content(self):
        p = self._page()
        assert p.url.endswith("/1/")
        assert p.title(timeout=8000) == "Ozon"
        assert p.content() == "<html></html>"

    def test_is_closed_tracks_flag(self):
        p = self._page()
        assert p.is_closed() is False
        p._d.close = lambda: None
        p.close()
        assert p.is_closed() is True

    def test_reload_calls_refresh(self):
        drv = self._page()._d
        calls = []
        drv.refresh = lambda: calls.append("refresh")
        p = self._page()
        p._d = drv
        p.reload(wait_until="domcontentloaded", timeout=60000)
        assert calls == ["refresh"]
        assert drv.page_load_timeout == 60.0

    def test_goto_swallows_load_timeout_when_dom_ready(self):
        p = self._page()
        calls = []
        p._d.get = lambda url: calls.append(url) or (_ for _ in ()).throw(
            TimeoutError("Navigation timed out after 60000 ms"))
        p._d.execute_script = lambda s, *a: "interactive"
        p.goto("https://x/?tab=reviews", wait_until="domcontentloaded",
               timeout=60000)
        assert calls == ["https://x/?tab=reviews"]

    def test_goto_reraises_timeout_when_dom_loading(self):
        p = self._page()
        p._d.get = lambda url: (_ for _ in ()).throw(
            TimeoutError("Navigation timed out"))
        p._d.execute_script = lambda s, *a: "loading"
        with pytest.raises(TimeoutError):
            p.goto("https://x/", wait_until="domcontentloaded", timeout=1000)

    def test_goto_reraises_timeout_for_load_wait(self):
        p = self._page()
        p._d.get = lambda url: (_ for _ in ()).throw(
            TimeoutError("Navigation timed out"))
        p._d.execute_script = lambda s, *a: "interactive"
        with pytest.raises(TimeoutError):
            p.goto("https://x/", wait_until="load", timeout=1000)

    def test_goto_reraises_non_timeout_error(self):
        p = self._page()
        p._d.get = lambda url: (_ for _ in ()).throw(
            RuntimeError("net::ERR_NAME_NOT_RESOLVED"))
        p._d.execute_script = lambda s, *a: "interactive"
        with pytest.raises(RuntimeError):
            p.goto("https://x/", wait_until="domcontentloaded", timeout=1000)

    def test_wait_for_timeout_sleeps(self):
        import time as _t
        p = self._page()
        t0 = _t.monotonic()
        p.wait_for_timeout(120)
        assert _t.monotonic() - t0 >= 0.1

    def test_is_invalid_flag_defaults_false(self):
        assert self._page().is_invalid is False

    def test_network_capture_disabled(self):
        # collect.py по этому флагу уходит на DOM-комментарии
        assert self._page().network_capture is False

    def test_remove_listener_is_noop(self):
        page = self._page()
        page.on("response", lambda r: None)
        assert page.remove_listener("response", lambda r: None) is None

    def test_geom_guard_attribute_settable(self):
        p = self._page()
        p._geom_guard = lambda page, **kw: (True, {})
        assert callable(p._geom_guard)


class TestAdapter:
    def test_contexts_contract(self):
        drv = _FakeTabs()
        page = ffsel.FirefoxPage(drv)
        ad = ffsel.SeleniumBrowserAdapter(drv, page)
        assert ad.contexts[0].pages == [page]

    def test_quit_is_swallowed(self):
        drv = _FakeDriver()
        drv.quit = lambda: (_ for _ in ()).throw(RuntimeError("already gone"))
        page = ffsel.FirefoxPage(drv)
        ffsel.SeleniumBrowserAdapter(drv, page).quit()


class _Discarded(Exception):
    pass


class _FakeSwitchTo:
    def __init__(self, drv):
        self._d = drv

    def window(self, handle):
        if handle not in self._d.window_handles:
            raise _Discarded("Browsing context has been discarded")
        self._d.current = handle

    def new_window(self, kind="tab"):
        if self._d.current not in self._d.window_handles:
            raise _Discarded("Browsing context has been discarded")
        h = "w%d" % (len(self._d.window_handles) + 1)
        self._d.window_handles.append(h)
        self._d.current = h
        return h


class _FakeTabs:
    """Драйвер с вкладками: close() закрывает ТЕКУЩУЮ (как Selenium)."""

    def __init__(self, handles=("w1",)):
        self.window_handles = list(handles)
        self.current = self.window_handles[0]
        self.closed = []
        self.switch_to = _FakeSwitchTo(self)
        self.script_timeout = None
        self.title = "Ozon"

    @property
    def current_window_handle(self):
        if self.current not in self.window_handles:
            raise _Discarded("Browsing context has been discarded")
        return self.current

    def close(self):
        if self.current not in self.window_handles:
            raise _Discarded("Browsing context has been discarded")
        self.closed.append(self.current)
        self.window_handles.remove(self.current)
        self.current = (self.window_handles or [None])[-1]

    def set_script_timeout(self, s):
        self.script_timeout = s


class TestPageHandleBinding:
    """Страница привязана к СВОЕЙ вкладке (root cause «discarded», 2026-09-25).

    Selenium работает с текущим окном: page.close() без switch закрывал
    текущую (новую рабочую) вкладку, и следующий switch_to.new_window падал
    с «Browsing context has been discarded».
    """

    def test_close_switches_to_own_handle_first(self):
        drv = _FakeTabs(["w1", "w2"])
        page = ffsel.FirefoxPage(drv)
        drv.current = "w2"
        page.close()
        assert drv.closed == ["w1"]
        assert drv.current == "w2"

    def test_close_returns_focus_to_previous_tab(self):
        drv = _FakeTabs(["w1", "w2"])
        page = ffsel.FirefoxPage(drv)
        drv.current = "w2"
        page.close()
        assert drv.window_handles == ["w2"]
        assert drv.current == "w2"

    def test_close_skipped_when_own_tab_already_gone(self):
        drv = _FakeTabs(["w1", "w2"])
        page = ffsel.FirefoxPage(drv)
        drv.window_handles.remove("w1")
        drv.current = "w2"
        page.close()
        assert drv.closed == []
        assert page.is_closed() is True

    def test_is_closed_checks_own_handle_not_any_tab(self):
        drv = _FakeTabs(["w1", "w2"])
        page = ffsel.FirefoxPage(drv)
        assert page.is_closed() is False
        drv.window_handles.remove("w1")
        assert page.is_closed() is True

    def test_operations_switch_to_own_handle(self):
        drv = _FakeTabs(["w1", "w2"])
        page = ffsel.FirefoxPage(drv)
        drv.current = "w2"
        assert page.title() == "Ozon"
        assert drv.current == "w1"

    def test_goto_switches_to_own_handle(self):
        drv = _FakeTabs(["w1", "w2"])
        drv.get = lambda url: setattr(drv, "current", drv.current)
        page = ffsel.FirefoxPage(drv)
        drv.current = "w2"
        page.goto("https://www.ozon.ru/product/1/")
        assert drv.current == "w1"

    def test_page_created_on_dead_context_takes_live_tab(self):
        drv = _FakeTabs(["w1", "w2"])
        drv.current = "w-dead"
        page = ffsel.FirefoxPage(drv)
        assert page._handle == "w2"
        assert drv.current == "w2"


class TestContextNewPage:
    def test_new_page_returns_page_with_new_handle(self):
        drv = _FakeTabs(["w1"])
        page = ffsel.FirefoxPage(drv)
        ctx = ffsel._SeleniumContext(drv, page)
        new = ctx.new_page()
        assert new._handle == "w2"
        assert ctx.pages == [page, new]

    def test_new_page_recovers_dead_current_tab(self):
        drv = _FakeTabs(["w1", "w2"])
        page = ffsel.FirefoxPage(drv)
        ctx = ffsel._SeleniumContext(drv, page)
        drv.current = "w-dead"
        new = ctx.new_page()
        assert new._handle == "w3"
        assert drv.window_handles == ["w1", "w2", "w3"]

    def test_new_page_raises_when_no_tabs_left(self):
        drv = _FakeTabs(["w1"])
        page = ffsel.FirefoxPage(drv)
        ctx = ffsel._SeleniumContext(drv, page)
        drv.window_handles.clear()
        drv.current = "w1"
        with pytest.raises(RuntimeError, match="живых вкладок нет"):
            ctx.new_page()

    def test_pages_prunes_closed(self):
        drv = _FakeTabs(["w1", "w2"])
        page = ffsel.FirefoxPage(drv)
        ctx = ffsel._SeleniumContext(drv, page)
        new = ctx.new_page()
        new.close()
        assert ctx.pages == [page]
