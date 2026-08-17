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
from . import process as P


def _cdp_port(cdp_url: str) -> int:
    port = urlparse(cdp_url).port
    return port if port else 9222


def _window_for_pid(pid: int):
    """Главное видимое окно процесса pid (user32 EnumWindows) или None."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = []

    def _cb(hwnd, lparam):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False  # первое видимое окно процесса — и достаточно
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return found[0] if found else None


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


def _launch_browser(cdp_url: str, profile_path: str | None = None):
    """Запуск живого Chromium через scripts/launch_chrome (порт из cdp_url).

    profile_path — полный путь --user-data-dir (для воркеров передаёт PROFILE
    env в launch_chrome.ps1/.sh; None = дефолтный профиль скрипта).
    """
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
    if profile_path:
        env["PROFILE"] = profile_path
    cmd = (["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)]
           if os.name == "nt" else [str(script)])
    subprocess.Popen(cmd, env=env,
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def launch_and_wait(cdp_url: str = "http://127.0.0.1:9222",
                    wait_s: float = 30.0,
                    profile_token: str | None = None) -> bool:
    """Авто-запуск браузера, если он закрыт; ждёт готовности до wait_s.

    Возвращает True, если браузер жив после ожидания (запущен нами или уже был).
    При недоступности — RuntimeError с подсказкой ручного запуска.
    profile_token — токен профиля воркера (None = дефолтный профиль).
    """
    if browser_alive(cdp_url):
        return True
    print(f"[cdp] браузер не запущен на {cdp_url}, запускаю…")
    log.warning(f"браузер не запущен на {cdp_url}, запускаю…",
                extra={"cdp_url": cdp_url})
    profile_path = None
    if profile_token:
        profile_path = P.profile_path(profile_token)
    _launch_browser(cdp_url, profile_path=profile_path)
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
                 auto_launch: Optional[bool] = None,
                 profile_token: str = P.PROFILE_TOKEN):
        self.cdp_url = cdp_url
        self.profile_token = profile_token
        self.auto_launch = (auto_launch if auto_launch is not None
                            else os.environ.get("OZON_AUTO_LAUNCH", "1") != "0")
        self._playwright = None
        self._browser = None
        self.page = None
        self._watchdog = None
        self._connect()

    def _connect(self):
        if self.auto_launch and not browser_alive(self.cdp_url):
            launch_and_wait(self.cdp_url, profile_token=self.profile_token)
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(
                self.cdp_url)
        except Exception:
            if self.auto_launch and self._browser is None:
                launch_and_wait(self.cdp_url,
                                profile_token=self.profile_token)
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
        self._minimize_window()

    def _minimize_window(self):
        """Сворачивает окно Chromium, чтобы не перекрывало работу пользователя.

        При запуске и подключении CDP Chromium разворачивает окно поверх всех
        и крадёт фокус. Двойная защита:
        1) CDP Browser.setWindowBounds (windowState=minimized) — сворачивание
           через сам браузер;
        2) user32: WS_EX_NOACTIVATE + SW_MINIMIZE на главное окно процесса —
           окно не получает фокус при разворачивании (важно при live-сборе,
           чтобы печать в другом приложении не прерывалась).
        Игнорируем ошибки: сбой минимизации некритичен.
        """
        try:
            sess = self._browser.new_browser_cdp_session()
            targets = sess.send("Target.getTargets")
            tid = None
            for t in targets.get("targetInfos", []):
                if t.get("type") == "page":
                    tid = t["targetId"]
                    break
            if tid is not None:
                res = sess.send("Browser.getWindowForTarget", {"targetId": tid})
                sess.send("Browser.setWindowBounds", {
                    "windowId": res["windowId"],
                    "bounds": {"windowState": "minimized"},
                })
        except Exception as e:
            log.debug(f"minimize_window cdp: {type(e).__name__}: {e}")
        self._minimize_window_native(_cdp_port(self.cdp_url))

    @staticmethod
    def _minimize_window_native(port: int):
        """user32: свёртывание + запрет активации окна CDP-Chromium (Windows).

        Находит главное окно процесса chrome с remote-debugging-port=port
        (наш воркер), ставит WS_EX_NOACTIVATE и сворачивает (SW_MINIMIZE).
        Окна обычного Chrome пользователя не трогаются. Не-Windows — no-op.
        """
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            SW_MINIMIZE = 6

            # ищем PID процесса chrome с remote-debugging-port через wmic
            import subprocess
            out = subprocess.run(
                ["wmic", "process", "where",
                 "Name='chrome.exe'",
                 "get", "ProcessId,CommandLine",
                 "/format:csv"],
                capture_output=True, text=True, timeout=10).stdout
            pids = set()
            for line in out.splitlines():
                if f"--remote-debugging-port={port}" in line:
                    parts = line.strip().split(",")
                    if parts and parts[-1].strip().isdigit():
                        pids.add(int(parts[-1].strip()))
            for pid in pids:
                hwnd = _window_for_pid(pid)
                if hwnd:
                    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                          style | WS_EX_NOACTIVATE)
                    user32.ShowWindow(hwnd, SW_MINIMIZE)
                    break
        except Exception as e:
            log.debug(f"minimize_window native: {type(e).__name__}: {e}")

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

    def new_page(self, close_current: bool = True):
        """Recovery new_tab: НОВАЯ вкладка в том же контексте браузера.

        Эксперимент 2026-08-17: access_challenge (FAB) Ozon залипает на
        навигации КОНКРЕТНОЙ вкладки — повторы/reload в той же вкладке не
        помогают, а свежая вкладка проходит (Qrator выдаёт auth-куки и
        разблокирует всю сессию). Метод закрывает текущую (FAB-залипшую)
        вкладку, открывает новую и переключает self.page на неё.
        """
        if self._browser is None or not self._browser.contexts:
            raise RuntimeError("нет CDP-контекста для новой вкладки")
        ctx = self._browser.contexts[0]
        old = self.page
        new = ctx.new_page()
        try:
            new.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
        except Exception:
            pass
        self.page = new
        if close_current and old is not None:
            try:
                old.close()
            except Exception:
                pass
        self._minimize_window()
        log.debug(f"new_page: {self.cdp_url}")
        return new

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
            self._watchdog = BrowserWatchdog(self.cdp_url,
                                             profile_token=self.profile_token)
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
