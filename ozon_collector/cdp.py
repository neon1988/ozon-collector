"""Подключение к живому Chromium по CDP (обход антибота Ozon)."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from .browser import BrowserWatchdog, is_renderer_failure
from . import log


def _cdp_port(cdp_url: str) -> int:
    port = urlparse(cdp_url).port
    return port if port else 9222


def _js_label(js: str) -> str:
    """Короткая метка JS-выражения для логов (до 40 символов, без переносов)."""
    s = " ".join(js.split())
    return s[:40] + ("…" if len(s) > 40 else "")


def _launch_script_name() -> str:
    """Имя скрипта запуска браузера для текущей ОС."""
    return "launch_chrome.ps1" if os.name == "nt" else "launch_chrome.sh"


def _launch_script_path() -> Path:
    """Путь к скрипту запуска браузера (Windows → .ps1, иначе → .sh)."""
    return (Path(__file__).resolve().parent.parent / "scripts"
            / _launch_script_name())


def _launch_browser(cdp_url: str):
    """Запуск живого Chromium через scripts/launch_chrome (порт из cdp_url)."""
    script = _launch_script_path()
    if not script.exists():
        raise RuntimeError(
            f"{_launch_script_name()} не найден: {script}\n"
            f"Запустите браузер вручную: "
            f"powershell -ExecutionPolicy Bypass -File scripts/launch_chrome.ps1"
            if os.name == "nt" else
            f"sh scripts/launch_chrome.sh"
        )
    env = dict(os.environ)
    env["PORT"] = str(_cdp_port(cdp_url))
    cmd = (["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)]
           if os.name == "nt" else [str(script)])
    subprocess.Popen(cmd, env=env,
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def launch_and_wait(cdp_url: str = "http://127.0.0.1:9222",
                    wait_s: float = 30.0) -> bool:
    """Авто-запуск браузера, если он закрыт; ждёт готовности до wait_s.

    Возвращает True, если браузер жив после ожидания (запущен нами или уже был).
    При недоступности — RuntimeError с подсказкой ручного запуска.
    """
    if browser_alive(cdp_url):
        return True
    print(f"[cdp] браузер не запущен на {cdp_url}, запускаю…")
    log.warning(f"браузер не запущен на {cdp_url}, запускаю…",
                extra={"cdp_url": cdp_url})
    _launch_browser(cdp_url)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if browser_alive(cdp_url):
            return True
        time.sleep(0.5)
    raise RuntimeError(
        f"CDP-браузер не поднялся за {wait_s:.0f}с на {cdp_url}\n"
        f"Проверьте DISPLAY и Chromium, либо запустите вручную: "
        f"powershell -ExecutionPolicy Bypass -File scripts/launch_chrome.ps1"
        if os.name == "nt" else
        f"sh scripts/launch_chrome.sh"
    )


class Session:
    """Сессия к живому Chromium (remote-debugging-port=9222).

    Браузер запускается НАПРЯМУЮ (без --enable-automation), иначе
    navigator.webdriver === true и Ozon FAB блочит. См. scripts/launch_chrome
    (Windows → launch_chrome.ps1, Linux → launch_chrome.sh).
    auto_launch=True (по умолчанию, отключение через env OZON_AUTO_LAUNCH=0) —
    если браузер закрыт, автоматически запускается через scripts/launch_chrome.
    """

    def __init__(self, cdp_url: str = "http://127.0.0.1:9222",
                 auto_launch: Optional[bool] = None):
        self.cdp_url = cdp_url
        self.auto_launch = (auto_launch if auto_launch is not None
                            else os.environ.get("OZON_AUTO_LAUNCH", "1") != "0")
        self._playwright = None
        self._browser = None
        self.page = None
        self._watchdog = None
        self._connect()

    def _connect(self):
        if self.auto_launch and not browser_alive(self.cdp_url):
            launch_and_wait(self.cdp_url)
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(
                self.cdp_url)
        except Exception:
            if self.auto_launch and self._browser is None:
                launch_and_wait(self.cdp_url)
                self._browser = self._playwright.chromium.connect_over_cdp(
                    self.cdp_url)
            else:
                raise
        ctx = self._browser.contexts[0]
        self.page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            self.page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
        except Exception:
            pass

    def goto(self, url: str, wait_ms: int = 7000):
        """Перейти на страницу, дождаться domcontentloaded + паузу."""
        self._guard_check()
        log.debug(f"goto: {url}", extra={"wait_ms": wait_ms})
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            if is_renderer_failure(e):
                log.warning(f"goto renderer_failure: {url} → {type(e).__name__}: {e}")
                raise
            print(f"[cdp] goto warn: {e}")
            log.warning(f"goto warn: {url} → {type(e).__name__}: {e}")
        self.page.wait_for_timeout(wait_ms)
        log.debug(f"goto done: {url}")

    def scroll(self, by: int = 600):
        self._guard_check()
        log.debug(f"scroll by={by}")
        self.page.evaluate("(dy) => window.scrollBy(0, dy)", by)

    def wait(self, ms: int = 1000):
        self._guard_check()
        log.debug(f"wait {ms}ms")
        self.page.wait_for_timeout(ms)

    def eval(self, js: str):
        self._guard_check()
        label = _js_label(js)
        log.debug(f"eval: {label}")
        return self.page.evaluate(js)

    def _ensure_watchdog(self):
        """Ленивый запуск фонового сторожа браузера (первая guarded-операция).

        Сторож работает весь прогон (в т.ч. длинный search) и останавливается
        в close(). После reconnect() создаётся новый инстанс — состояние «мёртв»
        от старого браузера не переносится.
        """
        if self._watchdog is None:
            self._watchdog = BrowserWatchdog(self.cdp_url)
            self._watchdog.start()
            log.debug(f"watchdog started: {self.cdp_url}")

    def watchdog_stop(self):
        """Остановить фоновый сторож (если запущен)."""
        if self._watchdog is not None:
            self._watchdog.stop()
            log.debug("watchdog stopped")
            self._watchdog = None

    def _guard_check(self):
        """Fail-fast перед операцией: мёртвый браузер → BrowserRecoveryError."""
        if self._watchdog is not None:
            self._watchdog.check()
        else:
            self._ensure_watchdog()

    def reconnect(self):
        """Переподключиться к (новому) Chromium на том же CDP URL.

        Используется после recycle браузера (recovery R1): старая сессия
        закрывается, соединение устанавливается заново. auto_launch при этом
        НЕ запускает браузер повторно — после restart новый процесс уже жив.
        """
        log.info(f"reconnect: {self.cdp_url}")
        self.close()
        self._connect()
        log.info("reconnect: done")

    def screenshot(self, path: str):
        self.page.screenshot(path=path)

    def close(self):
        """Закрыть лишние вкладки и завершить сессию Playwright.

        Браузер НЕ гасится — остаётся жив для следующей команды.
        Все вкладки кроме pages[0] закрываются; pages[0] уводится на about:blank.
        """
        self.watchdog_stop()
        log.debug("Session.close()")
        try:
            ctx = (self._browser.contexts[0]
                   if self._browser and self._browser.contexts else None)
            if ctx:
                pages = ctx.pages
                for p in pages[1:]:
                    try:
                        p.close()
                    except Exception:
                        pass
                if pages:
                    try:
                        pages[0].goto("about:blank", timeout=5000)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            self._playwright.stop()
        except Exception:
            pass
        self._browser = None


def browser_alive(cdp_url: str = "http://127.0.0.1:9222") -> bool:
    """Проверка, что CDP-браузер жив (без полной сессии)."""
    import urllib.request

    try:
        with urllib.request.urlopen(cdp_url + "/json/version", timeout=3) as r:
            data = json.loads(r.read().decode())
            return bool(data.get("Browser"))
    except Exception:
        return False
