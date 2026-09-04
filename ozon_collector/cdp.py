"""Подключение к живому Chromium по CDP (обход антибота Ozon)."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional


from playwright.sync_api import sync_playwright

from .browser import (BrowserWatchdog, is_renderer_failure,
                      AccessChallengeError)
from . import log
from . import process as P





def _window_for_pid(pid: int, require_visible: bool = True):
    """Главное окно браузера процесса pid (user32 EnumWindows) или None.

    require_visible=True (по умолчанию) — первое ВИДИМОЕ окно (для минимизации);
    require_visible=False — главное окно Chrome_WidgetWin_1 независимо от
    видимости (для ПОКАЗА скрытого окна: фильтр по IsWindowVisible не годится,
    т.к. скрытое с рождения SW_HIDE окно иначе найти нельзя и показать нечем).
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = []

    def _cb(hwnd, lparam):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != "Chrome_WidgetWin_1":
            return True
        if require_visible and not user32.IsWindowVisible(hwnd):
            return True
        found.append(hwnd)
        return False  # первое подходящее окно — и достаточно
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
    env["PORT"] = str(P.cdp_port(cdp_url))
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
    log.warning(f"[cdp] браузер не запущен на {cdp_url}, запускаю…",
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
                 profile_token: str = P.PROFILE_TOKEN,
                 keep_window: Optional[bool] = None):
        self.cdp_url = cdp_url
        self.profile_token = profile_token
        self.auto_launch = (auto_launch if auto_launch is not None
                            else os.environ.get("OZON_AUTO_LAUNCH", "1") != "0")
        # keep_window: None → env OZON_KEEP_WINDOW (1 = окно видимо/наверху),
        # иначе явный bool из конфига CLI (--window-mode visible/hidden).
        self.keep_window = (keep_window if keep_window is not None
                            else os.environ.get("OZON_KEEP_WINDOW") == "1")
        self.browser_owner = "external"   # external | collector (ADR §14.1)
        self.browser_pid = None
        self.browser_profile = P.profile_path(profile_token)
        self._playwright = None
        self._browser = None
        self.page = None
        self._watchdog = None
        self._managed_pages: list = []
        self.fingerprint = None
        self._fingerprint_captured = False
        self._connect()

    def _connect(self):
        was_alive = browser_alive(self.cdp_url)
        if self.auto_launch and not was_alive:
            launch_and_wait(self.cdp_url, profile_token=self.profile_token)
            self.browser_owner = "collector"
        else:
            # Браузер уже жив. Если профиль помечен нашим launch_chrome
            # (маркер-файл) — считаем его collector'ским; иначе external.
            self.browser_owner = (
                "collector" if _launch_marker(self.browser_profile)
                else "external")
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
                self.browser_owner = "collector"
            else:
                # второй connect тоже упал (или auto_launch выключен):
                # гасим запущенный Playwright-подпроцесс, иначе он утекает
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                raise
        self.browser_pid = _browser_pid(self.cdp_url, self.profile_token)
        ctx = self._browser.contexts[0]
        if self.browser_owner == "external":
            # external: НЕ трогаем первую попавшуюся вкладку (может быть
            # личной пользователя) — создаём свою managed-страницу.
            self.page = ctx.new_page()
        else:
            self.page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self._managed_pages = [self.page]
        if self.keep_window:
            # Окно коллектора видимо и на всех: не сворачиваем (env/CLI
            # --window-mode visible). SWP/ShowWindow снаружи бесполезны,
            # т.к. Chrome возвращает окно: запрет минимизации — здесь.
            self._keep_window_visible()
        else:
            self._minimize_window()

    def _minimize_window(self):
        """Сворачивает окно Chromium, чтобы не перекрывало работу пользователя.

        При запуске и подключении CDP Chromium разворачивает окно поверх всех
        и крадёт фокус. Двойная защита:
        1) CDP Browser.setWindowBounds (windowState=minimized) — сворачивание
           через сам браузер;
        2) user32: SW_MINIMIZE на главное окно процесса —
           окно не получает фокус при разворачивании (важно при live-сборе,
           чтобы печать в другом приложении не прерывалась).
        WS_EX_NOACTIVATE НЕ ставится — окно остаётся обычным (taskbar работает).
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
        self._minimize_window_native(P.cdp_port(self.cdp_url))

    @staticmethod
    def _minimize_window_native(port: int):
        """user32: свёртывание окна CDP-Chromium (Windows).

        Находит главное окно процесса chrome с remote-debugging-port=port
        (наш воркер) и сворачивает (SW_MINIMIZE). WS_EX НЕ ставится — окно
        остаётся обычным (taskbar работает, двойной клик восстанавливает).
        Окна обычного Chrome пользователя не трогаются. Не-Windows — no-op.
        """
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            SW_MINIMIZE = 6

            # ищем PID процесса chrome с remote-debugging-port через wmic
            import subprocess
            out = subprocess.run(
                ["wmic", "process", "where",
                 "Name='chrome.exe'",
                 "get", "ProcessId,CommandLine",
                 "/format:csv"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=10).stdout
            pids = set()
            for line in out.splitlines():
                if f"--remote-debugging-port={port}" in line:
                    parts = line.strip().split(",")
                    if parts and parts[-1].strip().isdigit():
                        pids.add(int(parts[-1].strip()))
            for pid in pids:
                hwnd = _window_for_pid(pid)
                if hwnd:
                    user32.ShowWindow(hwnd, SW_MINIMIZE)
                    break
        except Exception as e:
            log.debug(f"minimize_window native: {type(e).__name__}: {e}")

    def _keep_window_visible(self):
        """OZON_KEEP_WINDOW=1: развернуть окно Chromium и показать его.

        `_minimize_window` вызывается на каждом __init__/reconnect — окно вечно
        «пропадает», т.к. Chrome сам задаёт позицию (SWP снаружи бесполезен).
        Здесь через CDP Browser.setWindowBounds просим normal + ставим
        координаты в границы экрана, чтобы окно было видимым и наверху.
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
                    "bounds": {
                        "left": 120, "top": 60,
                        "width": 1500, "height": 880,
                        "windowState": "normal",
                    },
                })
        except Exception as e:
            log.debug(f"keep_window cdp: {type(e).__name__}: {e}")
        self._restore_window_native(P.cdp_port(self.cdp_url))

    @staticmethod
    def _restore_window_native(port: int):
        """user32: SW_SHOWNOACTIVATE + TOPMOST окна Chromium (Windows), без активации.

        Дублирует путь launch_chrome.ps1, но применим к уже работающему окну
        (после reconnect/перезапуска коллектором). SW_SHOWNOACTIVATE показывает
        без активации; WS_EX_NOACTIVATE НЕ ставится — окно остаётся обычным
        (taskbar работает, двойной клик восстанавливает).
        """
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010

            import subprocess
            out = subprocess.run(
                ["wmic", "process", "where",
                 "Name='chrome.exe'",
                 "get", "ProcessId,CommandLine",
                 "list", "brief"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15)
            pids = set()
            for raw in out.stdout.splitlines():
                if "remote-debugging-port" in raw:
                    parts = raw.strip().split(",")
                    if parts and parts[-1].strip().isdigit():
                        pids.add(int(parts[-1].strip()))
            for pid in pids:
                # require_visible=False: главное окно браузера находим даже В
                # СКРЫТОМ состоянии (SW_HIDE при старте) — иначе его нельзя
                # найти и показать (замкнутый круг). SW_SHOWNOACTIVATE +
                # SWP_SHOWWINDOW гарантированно показывают невидимое окно.
                hwnd = _window_for_pid(pid, require_visible=False)
                if hwnd:
                    user32.ShowWindow(hwnd, 8)  # SW_SHOWNOACTIVATE
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                                        | 0x0040)  # SWP_SHOWWINDOW
                    break
        except Exception as e:
            log.debug(f"restore_window native: {type(e).__name__}: {e}")

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
            msg = str(e)
            # Навигационные сбои (таймаут, DNS, ERR_*) означают, что страница
            # НЕ загрузилась: продолжать разбор по старой/ошибочной странице
            # нельзя — пробрасываем, иначе соберём чужой/пустой контент.
            name = type(e).__name__
            if name == "TimeoutError" or "net::ERR_" in msg \
                    or "Timeout" in msg:
                log.warning(f"goto nav failure: {url} → {name}: {e}")
                raise
            log.warning(f"[cdp] goto warn: {url} → {type(e).__name__}: {e}")
        self.page.wait_for_timeout(wait_ms)
        self._capture_fingerprint_once()
        log.debug(f"goto done: {url}")

    def _capture_fingerprint_once(self):
        """Базовый fingerprint сессии (ADR §16, ревизия 2.7): один раз на сессию.

        Только наблюдение (тест manual vs daemon сравнения), НЕ влияет на
        поведение. Собирает UA, Client-Hints-намёки, viewport, timezone и
        наличие navigator.webdriver. Сбой сбора не роняет goto.
        """
        if self._fingerprint_captured or self.page is None:
            return
        self._fingerprint_captured = True
        try:
            fp = self.page.evaluate(
                "() => ({"
                " ua: navigator.userAgent,"
                " webdriver: navigator.webdriver,"
                " languages: navigator.languages,"
                " platform: navigator.platform,"
                " hardwareConcurrency: navigator.hardwareConcurrency,"
                " deviceMemory: (navigator.deviceMemory !== undefined) ? navigator.deviceMemory : null,"
                " chrome: (navigator.chrome !== undefined),"
                " tz: Intl.DateTimeFormat().resolvedOptions().timeZone,"
                " lang: Intl.DateTimeFormat().resolvedOptions().locale,"
                " vw: window.innerWidth,"
                " vh: window.innerHeight,"
                " sw: window.screen.width,"
                " sh: window.screen.height,"
                " colorDepth: window.screen.colorDepth,"
                " })"
            )
            if isinstance(fp, dict):
                self.fingerprint = fp
        except Exception:
            pass

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
        self.page = new
        if getattr(self, "_managed_pages", None) is None:
            self._managed_pages = [old] if old is not None else []
        self._managed_pages.append(new)
        if close_current and old is not None:
            try:
                old.close()
            except Exception:
                pass
            # убрать закрытую страницу из набора managed, чтобы список не
            # копил мёртвые Page-объекты при долгих recovery-циклах
            try:
                if old in self._managed_pages:
                    self._managed_pages.remove(old)
            except Exception:
                pass
        if getattr(self, "keep_window", False):
            self._keep_window_visible()
        else:
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
        """Fail-fast перед операцией: мёртвый браузер → BrowserRecoveryError.

        Также блокирует извлечение данных с INVALID-страницы (ADR §14.1):
        после детекта challenge текущая page помечается is_invalid и любые
        evaluate/goto/scroll/wait на ней бросают AccessChallengeError — заглушка
        не попадает в данные (K4). Флаг снимается только на новой чистой
        вкладке (new_page/ensure_single_worker_tab/reconnect).
        """
        page = getattr(self, "page", None)
        if page is not None and getattr(page, "is_invalid", False):
            raise AccessChallengeError(
                "page INVALID (после challenge) — извлечение запрещено")
        if self._watchdog is not None:
            self._watchdog.check()
        else:
            self._ensure_watchdog()

    def collector_tabs(self) -> int:
        """Число страниц, принадлежащих коллектору (managed pages).

        Источник истины для `collector_tabs` в телеметрии (ADR §14.5) —
        собственный набор созданных коллектором Page-объектов, а не внешний
        /json/list. Безопасно: никакого доступа к чужим вкладкам пользователя.
        """
        alive = 0
        for p in getattr(self, "_managed_pages", []):
            try:
                if not p.is_closed():
                    alive += 1
            except Exception:
                continue
        return alive

    def ensure_single_worker_tab(self):
        """Инвариант «одна рабочая вкладка коллектора» (ADR §14.5).

        Безопасный порядок: создать новую чистую вкладку →
        перепривязать self.page к ней → закрыть старые managed-вкладки (только
        свои, не чужие) → assert collector_tabs == 1. При ошибке создания
        новой вкладки старую рабочую НЕ уничтожаем (исключение пробрасывается).
        Вызывать ТОЛЬКО вне critical section collect (evaluate/scroll/topup).
        """
        try:
            new = self.new_page(close_current=False)
        except Exception:
            # не смогли создать новую — старую не трогаем
            raise
        current = new
        for p in getattr(self, "_managed_pages", []):
            if p is current:
                continue
            try:
                p.close()
            except Exception:
                pass
        self._managed_pages = [current]
        return current

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
        if getattr(self, "_managed_pages", None) is not None:
            self._managed_pages = []


def browser_alive(cdp_url: str = "http://127.0.0.1:9222") -> bool:
    """Проверка, что CDP-браузер жив (без полной сессии)."""
    import urllib.request

    try:
        with urllib.request.urlopen(cdp_url + "/json/version", timeout=3) as r:
            data = json.loads(r.read().decode())
            return bool(data.get("Browser"))
    except Exception:
        return False


def _launch_marker(profile_path: str) -> bool:
    """Маркер запуска launch_chrome: файл .ozon_collector_launched в профиле.

    Ставится launch_chrome.ps1/.sh при запуске браузера коллектором.
    Используется для определения browser_owner (ADR §14.1): браузер, поднятый
    нашим скриптом, помечен; ручной браузер (или чужой профиль) — нет.
    """
    try:
        return os.path.isfile(os.path.join(profile_path,
                                           ".ozon_collector_launched"))
    except Exception:
        return False


def _browser_pid(cdp_url: str, profile_token: str = P.PROFILE_TOKEN):
    """PID первого matching-процесса (port+profile) или None."""
    try:
        procs = P.browser_processes(cdp_url, profile_token=profile_token)
        return procs[0] if procs else None
    except Exception:
        return None


def browser_tabs_count(cdp_url: str = "http://127.0.0.1:9222") -> Optional[int]:
    """Число page-вкладок браузера (диагностическая телеметрия).

    Внешний снимок через HTTP /json/list — дёшево и без блокировки
    Playwright-транспорта. Используется для `browser_tabs` в телеметрии и
    status.json. НЕ является источником истины для ownership (см. ADR §14.5):
    `collector_tabs` считается по собственному набору страниц Session.
    Возвращает None, если браузер/CDP недоступен.
    """
    import urllib.request

    try:
        with urllib.request.urlopen(cdp_url + "/json/list", timeout=3) as r:
            data = json.loads(r.read().decode())
            if not isinstance(data, list):
                return None
            return sum(1 for t in data if t.get("type") == "page")
    except Exception:
        return None
