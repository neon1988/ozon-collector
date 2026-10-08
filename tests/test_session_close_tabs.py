"""Session.close() не трогает вкладки пользователя (регрессия 2026-10-06).

close() закрывал `ctx.pages[1:]` и уводил `pages[0]` на about:blank. Для
внешнего браузера (`browser_owner == "external"`, то есть чужой Chrome
оператора) это закрывало ЕГО вкладки и теряло несохранённый ввод. Теперь
закрываются только вкладки, которые открыл коллектор.

Живой браузер не поднимается: Session собирается через `object.__new__` и
заполняется минимальным набором полей, нужным `close()`.
"""
from __future__ import annotations

from types import SimpleNamespace

import ozon_collector.cdp as cdp


class FakePage:
    def __init__(self, name):
        self.name = name
        self.closed = False
        self.goto_calls: list[str] = []

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def goto(self, url, timeout=None):
        self.goto_calls.append(url)


def _session(owner, page, managed):
    s = object.__new__(cdp.Session)
    s.browser_owner = owner
    s.page = page
    s._managed_pages = managed
    s._playwright = SimpleNamespace(stop=lambda: None)
    s._browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
    s._watchdog = None          # BrowserWatchdog не поднимался
    s._geometry_guard = None
    s._driver = None
    return s


def test_close_leaves_user_tabs_alone_on_external_browser():
    """Вкладки оператора не закрываются и не уводятся на about:blank."""
    user_page = FakePage("user")
    ours = FakePage("collector")
    s = _session("external", user_page, [ours])
    s.close()
    assert ours.closed is True, "свою вкладку коллектор закрывает"
    assert user_page.closed is False, "вкладка пользователя обязана остаться"
    assert user_page.goto_calls == [], "чужую вкладку не уводим на about:blank"
    assert s._managed_pages == []


def test_close_navigates_own_work_tab_on_collector():
    """На своём браузере рабочая вкладка остаётся и уходит на about:blank."""
    work = FakePage("work")
    s = _session("collector", work, [work])
    s.close()
    assert work.closed is False, "рабочую вкладку не закрываем"
    assert work.goto_calls == ["about:blank"]


def test_close_with_no_managed_pages_touches_nothing():
    user_page = FakePage("user2")
    s = _session("external", user_page, [])
    s.close()
    assert user_page.closed is False
    assert user_page.goto_calls == []


def test_close_skips_already_closed_managed_pages():
    dead = FakePage("dead")
    dead.closed = True
    ours = FakePage("collector2")
    work = FakePage("work2")
    s = _session("collector", work, [dead, ours, work])
    s.close()                       # не должно бросить
    assert ours.closed is True, "остальные наши вкладки закрываются"
    assert work.closed is False, "рабочая остаётся"