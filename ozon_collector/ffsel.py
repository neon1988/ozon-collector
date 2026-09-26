"""Firefox через Selenium/Marionette: Playwright-совместимый слой поверх geckodriver.

Зачем: Ozon блокирует PATCHED-сборку Playwright для Firefox (Juggler) и отдаёт
заглушку «нет соединения», а stock Firefox через Marionette отдаёт нормальную
страницу (замер D:\\Temp\\opencode\\ff_marionette_test.py). Значит Firefox живёт
только на Selenium, а весь код ozon_collector (collect/search/puzzle) продолжает
работать с привычным page-API.

Модуль делает: поднимает stock Firefox через geckodriver и переводит
Playwright-вызовы page/locator/element_handle в Selenium.

Смежные зоны вынесены отдельно: ffsel_js (нормализация JS-строк и запуск),
ffsel_mouse (page.mouse через W3C Actions), ozon_fetch (JSON Ozon из
страницы). Модуль НЕ делает: перехват сети (он вешает страницу, см.
FirefoxPage.on), биометрию, CDP-специфичные вызовы.

Селекторы: CSS — нативно, но Playwright-псевдоклассы (`:visible`) не
поддерживаются Firefox-ом, поэтому в локаторе разворачиваются в XPath.
"""
from __future__ import annotations

import io
import time
from typing import Any, Optional

from . import ffsel_js, log
from .ffsel_mouse import SeleniumMouse
from .ffsel_query import css_to_xpath, is_xpath, role_xpath

_OK = ffsel_js.OK


def _is_nav_timeout(exc: Exception) -> bool:
    """Таймаут навигации WebDriver (get/refresh), а не JS-ошибка."""
    name = type(exc).__name__
    if name in ("TimeoutException", "TimeoutError"):
        return True
    return "timed out" in str(exc).lower() or "timeout" in str(exc).lower()
_VAL = ffsel_js.VAL
_ERR = ffsel_js.ERR


class SeleniumElement:
    """Обёртка WebElement с Playwright-методом evaluate(el, …)."""

    def __init__(self, page: "FirefoxPage", element):
        self._page = page
        self._el = element

    def evaluate(self, js: str, *args, **kwargs) -> Any:
        """Выполняет JS, передав DOM-элемент первым аргументом."""
        return self._page._evaluate(js, args=(self._el,) + args)

    def scroll_into_view_if_needed(self, timeout: int = 2000) -> None:
        self._page._scroll_into(self._el)

    def click(self, timeout: int = 5000, **kwargs) -> None:
        """Клик с ожиданием интерактивности (аналог actionability Playwright).

        Selenium сам падает на неотображаемом/неактивном элементе, но React
        ререндерит узлы ленивой загрузкой, поэтому нужны повторы: элемент
        успевает пересоздаться к следующей попытке.
        """
        deadline = time.monotonic() + max(timeout, 0) / 1000.0
        last = None
        while True:
            try:
                self._page._scroll_into(self._el)
                if self._el.is_displayed() and self._el.is_enabled():
                    self._el.click()
                    return
                last = "not interactable"
            except Exception as e:
                last = "%s: %s" % (type(e).__name__, e)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.15)
        raise TimeoutError("element.click: %s (%s)" % (last, self._el))

    def get_attribute(self, name: str) -> Optional[str]:
        return self._el.get_attribute(name)

    @property
    def raw(self):
        return self._el


class SeleniumLocator:
    """Минимальный Playwright-локатор поверх find_elements(By.*).

    Реализован ровно тот набор, который использует ozon_collector:
    first, count, click, wait_for, is_enabled, element_handle,
    press_sequentially, type, scroll_into_view_if_needed, bounding_box.
    """

    def __init__(self, page: "FirefoxPage", selector: str, single: bool = False,
                 has_text=None):
        self._page = page
        self._selector = selector
        self._single = single
        self._has_text = has_text

    @property
    def first(self) -> "SeleniumLocator":
        return SeleniumLocator(self._page, self._selector, single=True,
                               has_text=self._has_text)

    def _matches_text(self, element) -> bool:
        """has_text: compiled regex → re.search, строка → подстрока.

        Playwright ищет по видимому тексту элемента, поэтому берём el.text
        ( Selenium отдаёт именно видимый текст), а не innerHTML.
        """
        if self._has_text is None:
            return True
        text = ""
        try:
            text = element.text or ""
        except Exception:
            return False
        if hasattr(self._has_text, "search"):
            return bool(self._has_text.search(text))
        return str(self._has_text) in text

    def _by(self):
        from selenium.webdriver.common.by import By

        sel = css_to_xpath(self._selector)
        if is_xpath(sel):
            return By.XPATH, sel
        return By.CSS_SELECTOR, sel

    def _elements(self) -> list:
        self._page._activate()
        by, val = self._by()
        try:
            found = self._page._d.find_elements(by, val)
        except Exception as e:
            log.debug(f"locator find failed: {self._selector!r} "
                      f"{type(e).__name__}: {e}")
            return []
        if self._has_text is not None:
            found = [el for el in found if self._matches_text(el)]
        return found[:1] if self._single else found

    def count(self) -> int:
        return len(self._elements())

    def _first_or_none(self, timeout_ms: int = 0):
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000.0
        while True:
            els = self._elements()
            if els:
                return els[0]
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)

    def element_handle(self) -> Optional[SeleniumElement]:
        el = self._first_or_none()
        return SeleniumElement(self._page, el) if el is not None else None

    def click(self, timeout: int = 5000, **kwargs) -> None:
        el = self._first_or_none(timeout)
        if el is None:
            raise RuntimeError("click: элемент не найден: %r" % self._selector)
        self._page._scroll_into(el)
        el.click()

    def wait_for(self, state: str = "visible", timeout: int = 5000) -> None:
        el = self._first_or_none(timeout)
        if el is None or (state == "visible" and not el.is_displayed()):
            raise TimeoutError("wait_for(%s): не дождались %r"
                               % (state, self._selector))

    def is_enabled(self) -> bool:
        el = self._first_or_none(500)
        try:
            return bool(el is not None and el.is_enabled())
        except Exception:
            return False

    def is_visible(self) -> bool:
        el = self._first_or_none(500)
        try:
            return bool(el is not None and el.is_displayed())
        except Exception:
            return False

    def text_content(self, timeout: int = 2000) -> str:
        el = self._first_or_none(timeout)
        return (el.text or "") if el is not None else ""

    def scroll_into_view_if_needed(self, timeout: int = 2000) -> None:
        el = self._first_or_none(timeout)
        if el is not None:
            self._page._scroll_into(el)

    def press_sequentially(self, text: str, delay: int = 0, **kwargs) -> None:
        el = self._first_or_none(5000)
        if el is None:
            raise RuntimeError("press_sequentially: элемент не найден: %r"
                               % self._selector)
        self._page._scroll_into(el)
        el.click()
        if delay and delay > 0:
            for ch in text:
                el.send_keys(ch)
                time.sleep(delay / 1000.0)
        else:
            el.send_keys(text)

    def type(self, text: str, delay: int = 0, **kwargs) -> None:
        self.press_sequentially(text, delay=delay)

    def bounding_box(self) -> Optional[dict]:
        el = self._first_or_none(1000)
        if el is None:
            return None
        rect = self._page.evaluate(
            "(el) => { const r = el.getBoundingClientRect();"
            " return {x: r.x, y: r.y, width: r.width, height: r.height}; }",
            el)
        return rect if isinstance(rect, dict) else None


class FirefoxPage:
    """Страница Playwright-API поверх Selenium WebDriver."""

    def __init__(self, driver, log_js: bool = False):
        self._d = driver
        self._log_js = log_js
        self._closed = False
        self.mouse = SeleniumMouse(driver)
        self.is_invalid = False
        self._geom_guard = None
        self.network_capture = False
        try:
            self._handle = driver.current_window_handle
        except Exception:
            self._handle = None
        if self._handle is None:
            # текущий контекст мёртв (discarded) — берём любую живую вкладку,
            # иначе страница-объект будет работать с чужим окном
            try:
                handles = list(driver.window_handles)
            except Exception:
                handles = []
            if handles:
                self._handle = handles[-1]
                try:
                    driver.switch_to.window(self._handle)
                except Exception:
                    pass

    def _activate(self) -> None:
        """Переключить драйвер на СВОЮ вкладку.

        Selenium оперирует текущим окном: любой вызов page.* идёт в
        driver.current_window_handle, а не в handle этой страницы. Без
        явного switch вкладка-объект работает с ЧУЖОЙ вкладкой. Именно это
        ломало ensure_single_worker_tab: close() старой страницы закрывал
        текущую (новую рабочую) вкладку, дальше switch_to.new_window падал
        с «Browsing context has been discarded» (cdp.py:937, ffsel.py:329).
        """
        if self._handle is None or self._closed:
            return
        try:
            if self._d.current_window_handle == self._handle:
                return
        except Exception:
            pass  # текущий контекст мёртв — switch на свою вкладку
        try:
            self._d.switch_to.window(self._handle)
        except Exception as e:
            log.debug(f"page._activate: {type(e).__name__}: {e}")

    def _evaluate(self, js: str, args: tuple = (), timeout: Optional[int] = None
                  ) -> Any:
        if self._log_js:
            log.debug("ffsel eval: %s" % " ".join(js.split())[:60])
        self._activate()
        timeout_s = max(timeout / 1000.0, 1.0) if timeout is not None else None
        return ffsel_js.run(self._d, js, args, timeout_s)

    def evaluate(self, js: str, *args, **kwargs) -> Any:
        timeout = kwargs.pop("timeout", None)
        if kwargs:
            log.debug("ffsel evaluate: игнорирую kwargs %s" % list(kwargs))
        return self._evaluate(js, tuple(args), timeout=timeout)

    def _scroll_into(self, element) -> None:
        try:
            self._d.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
                element)
        except Exception as e:
            log.debug(f"scroll_into_view: {type(e).__name__}: {e}")

    def _dom_interactive(self) -> bool:
        """Документ уже разобран (readyState != loading)."""
        try:
            state = self._d.execute_script("return document.readyState;")
        except Exception:
            return False
        return state in ("interactive", "complete")

    def _navigate(self, action, wait_until: Optional[str],
                  timeout: Optional[int]) -> Any:
        """Навигация Selenium-ом: не ждём load, если просят domcontentloaded.

        geckodriver возвращает управление из get()/refresh() по событию load.
        Страницы Ozon (особенно ?tab=reviews) грузятся долго и load-событие
        может не наступить за timeout — на живой проверке 2026-09-25 reviews-URL
        висел 60с и отдавал недогидрированную оболочку (0 карточек), тогда как
        после DOMContentLoaded список отзывов уже рендерился. Поэтому при
        wait_until=domcontentloaded/commit терпим таймаут, если документ
        интерактивен; остальные таймауты пробрасываем как раньше."""
        if timeout:
            self._d.set_page_load_timeout(max(timeout / 1000.0, 1.0))
        self._activate()
        try:
            action()
        except Exception as e:
            if not _is_nav_timeout(e):
                raise
            if wait_until not in ("domcontentloaded", "commit"):
                raise
            if not self._dom_interactive():
                raise
            log.info("[ffsel] навигация: load не наступил, но DOM готов — "
                     "продолжаю (pageLoadStrategy=eager)")
        return None

    def goto(self, url: str, wait_until: Optional[str] = None,
             timeout: Optional[int] = None) -> Any:
        return self._navigate(lambda: self._d.get(url), wait_until, timeout)

    def reload(self, wait_until: Optional[str] = None,
               timeout: Optional[int] = None) -> Any:
        """Перезагрузка страницы (снимает FAB-заглушку, см. collect._reload_fn)."""
        return self._navigate(self._d.refresh, wait_until, timeout)

    def wait_for_timeout(self, ms: int) -> None:
        time.sleep(max(ms, 0) / 1000.0)

    def wait_for_load_state(self, state: str = "load", timeout: int = 30000):
        return None

    def title(self, timeout: Optional[int] = None) -> str:
        self._activate()
        return self._d.title or ""

    @property
    def url(self) -> str:
        self._activate()
        try:
            return self._d.current_url
        except Exception:
            return ""

    def content(self) -> str:
        self._activate()
        return self._d.page_source

    def is_closed(self) -> bool:
        if self._closed:
            return True
        try:
            if self._handle is not None:
                return self._handle not in self._d.window_handles
            return not bool(self._d.window_handles)
        except Exception:
            return True

    def close(self) -> None:
        try:
            handles = list(self._d.window_handles)
            if self._handle is None or self._handle not in handles:
                self._closed = True
                return
            # switch на СВОЮ вкладку: driver.close() закрывает текущее окно,
            # без switch закрылась бы чужая (новая рабочая) вкладка.
            try:
                keep = self._d.current_window_handle
            except Exception:
                keep = None
            if keep == self._handle or keep not in handles:
                rest = [h for h in handles if h != self._handle]
                keep = rest[-1] if rest else None
            self._d.switch_to.window(self._handle)
            self._d.close()
            if keep is not None and keep in self._d.window_handles:
                self._d.switch_to.window(keep)
        except Exception as e:
            log.debug(f"page.close: {type(e).__name__}: {e}")
        self._closed = True

    def locator(self, selector: str, has_text=None, **kwargs) -> SeleniumLocator:
        return SeleniumLocator(self, selector, has_text=has_text)

    def get_by_role(self, role: str, name: Optional[str] = None,
                    **kwargs) -> SeleniumLocator:
        return SeleniumLocator(self, role_xpath(role, name))

    def on(self, event: str, callback) -> None:
        """Заглушка сетевых событий Playwright.

        Перехват сети в Firefox/Selenium невозможен безопасно: Selenium
            network.addDataCollector + network.getData не поддерживается
            Firefox (getData требует request, а в response-событии requestId
            отсутствует), а addRequestHandler вешает страницу (continueRequest
            падает «no such request» на завершённых запросах). Данные отзывов
            берутся прямым fetch из страницы — см. collect.fetch_reviews.
        """
        if event == "response":
            log.warning("[ffsel] page.on('response') игнорируется: перехват "
                        "сети в Firefox недоступен, используйте fetch")
            return
        log.warning(f"[ffsel] page.on({event!r}) не поддерживается")

    def remove_listener(self, event: str, callback=None) -> None:
        """Пара к on(): подписки снимать нечего — on() их не создавал.

        Нужна симметрия API: вызывающий код (collect.py) снимает слушатель в
        finally, иначе на Firefox падение AttributeError маскировало бы
        результат сбора.
        """
        return None

    def screenshot(self, clip: Optional[dict] = None, **kwargs) -> bytes:
        self._activate()
        data = self._d.get_screenshot_as_png()
        if not clip:
            return data
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(data))
            x = int(clip.get("x", 0))
            y = int(clip.get("y", 0))
            w = int(clip.get("width", img.width))
            h = int(clip.get("height", img.height))
            box = (max(x, 0), max(y, 0), min(x + w, img.width),
                   min(y + h, img.height))
            if box[2] <= box[0] or box[3] <= box[1]:
                return b""
            out = io.BytesIO()
            img.crop(box).save(out, format="PNG")
            return out.getvalue()
        except Exception as e:
            log.debug(f"screenshot clip: {type(e).__name__}: {e}")
            return data

    def add_init_script(self, script: Optional[str] = None,
                        path: Optional[str] = None) -> None:
        log.warning("[ffsel] add_init_script не поддерживается Firefox-бэкендом")

    def set_default_timeout(self, ms: int) -> None:
        self._d.set_script_timeout(max(ms / 1000.0, 1.0))

    def bring_to_front(self) -> None:
        self._activate()


class _SeleniumContext:
    """Контекст-API: pages / new_page (нужно cdp.Session)."""

    def __init__(self, driver, page: FirefoxPage):
        self._d = driver
        self._pages = [page]

    @property
    def pages(self) -> list:
        alive = [p for p in self._pages if not p.is_closed()]
        if len(alive) != len(self._pages):
            self._pages = alive
        return list(self._pages)

    def _recover_current(self) -> None:
        """Починить текущую вкладку перед switch_to.new_window.

        Marionette открывает новую вкладку ОТ текущего контекста: если текущая
        вкладка закрыта/выгружена, new_window падает с NoSuchWindowException
        «Browsing context has been discarded» ( Marionette driver.sys.mjs
        assert.open). Поэтому сначала уходим на любую живую вкладку.
        """
        handles = list(self._d.window_handles)
        if not handles:
            raise RuntimeError("firefox: живых вкладок нет — нужен recycle")
        try:
            cur = self._d.current_window_handle
        except Exception:
            cur = None
        if cur in handles:
            return
        self._d.switch_to.window(handles[-1])
        log.debug("[ffsel] current tab was dead → switched to live tab")

    def new_page(self) -> FirefoxPage:
        try:
            self._d.switch_to.new_window("tab")
        except Exception as e:
            log.debug(f"new_window failed ({type(e).__name__}: {e}) — "
                      "восстанавливаю текущую вкладку")
            self._recover_current()
            self._d.switch_to.new_window("tab")
        page = FirefoxPage(self._d)
        self._pages.append(page)
        return page


class SeleniumBrowserAdapter:
    """Адаптер browser с .contexts[0] — тот же контракт, что ждёт cdp.Session."""

    def __init__(self, driver, page: FirefoxPage):
        self._d = driver
        self.contexts = [_SeleniumContext(driver, page)]

    def quit(self) -> None:
        try:
            self._d.quit()
        except Exception as e:
            log.debug(f"driver.quit: {type(e).__name__}: {e}")


def _firefox_options(profile_path, prefs, binary, eager: bool = True):
    """Собрать Options Firefox; eager — pageLoadStrategy=eager (geckodriver)."""
    from selenium.webdriver.firefox.options import Options

    opts = Options()
    if profile_path:
        opts.add_argument("-profile")
        opts.add_argument(profile_path)
    for key, val in (prefs or {}).items():
        try:
            opts.set_preference(key, val)
        except Exception as e:
            log.debug(f"set_preference {key}: {type(e).__name__}: {e}")
    if binary:
        opts.binary_location = binary
    if eager:
        opts.set_capability("pageLoadStrategy", "eager")
    opts.set_capability("webSocketUrl", True)
    return opts


def connect(profile_path: Optional[str] = None,
            prefs: Optional[dict] = None,
            page_load_timeout: int = 60,
            script_timeout: int = 120,
            binary: Optional[str] = None):
    """Поднять stock Firefox через geckodriver и вернуть (driver, page, adapter).

    profile_path — наш профиль (-profile); None = временный профиль Firefox.
    prefs — user_prefs (например FIREFOX_USER_PREFS из cdp).
    binary — путь к firefox.exe; по умолчанию системный Firefox. Важно:
    НЕ указывать сюда patched-сборку Playwright (ms-playwright/firefox-*) —
    Ozon блокирует именно её.

    pageLoadStrategy=eager — geckodriver возвращает управление из get()/
    refresh() на DOMContentLoaded, а не по load. Иначе страницы Ozon висят
    до page_load_timeout и отдают недогидрированную оболочку (замер
    2026-09-25, ff_fail_probe.py: reviews-URL висел 60с, 0 карточек).
    Если capability не поддержан — сессия поднимается без него, таймауты
    навигации тогда терпит FirefoxPage._navigate (готовый DOM)."""
    from selenium import webdriver
    from selenium.webdriver.firefox.service import Service

    def _new(eager: bool):
        return webdriver.Firefox(
            options=_firefox_options(profile_path, prefs, binary, eager),
            service=Service(log_output=None))

    try:
        driver = _new(eager=True)
    except Exception as e:
        if "pageloadstrategy" not in str(e).lower():
            raise
        log.warning("[ffsel] geckodriver не принял pageLoadStrategy=eager "
                    f"({type(e).__name__}) — сессия без него")
        driver = _new(eager=False)
    driver.set_page_load_timeout(page_load_timeout)
    driver.set_script_timeout(script_timeout)
    try:
        driver.implicitly_wait(0)
    except Exception:
        pass
    page = FirefoxPage(driver)
    adapter = SeleniumBrowserAdapter(driver, page)
    log.info(f"[ffsel] firefox connected: profile={profile_path}, "
             f"binary={binary or 'system'}, session={driver.session_id}")
    return driver, page, adapter
