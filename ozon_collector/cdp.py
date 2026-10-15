"""Подключение к живому Chromium по CDP (обход антибота Ozon)."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import NamedTuple
from typing import Optional


from playwright.sync_api import sync_playwright

from . import browser as B
from .browser import (BrowserWatchdog, is_renderer_failure,
                      AccessChallengeError, _chrome_override)
from . import launchlog as _LL
from . import log
from . import ffsel
from . import process as P
from . import window_policy as _WP


# ---- Firefox-бэкенд (OZON_BROWSER=firefox) ---------------------------------

# Минимум user-prefs для persistent-контекста Playwright Firefox.
#
# intl-префы — не маскировка, а выравнивание профиля. Замерено на Playwright
# Firefox 153 (D:\Temp\opencode\ff_lang_probe.py): дефолтный контекст отдаёт
# navigator.languages="en-US,en" на русском Ozon, тогда как chrome-путь идёт с
# системными настройками (ru-RU, Europe/Moscow). Это нативное расхождение,
# видимое Ozon без всякой автоматизации, поэтому выравниваем его prefs'ами.
#
# НЕ выставляем dom.webdriver.enabled=false: замерено (ff_webdriver_probe.py),
# при выключенном префе Juggler всё равно оставляет navigator.webdriver === true
# (свойство на Navigator.prototype, геттер). Преф не помогает, а JS-подмена
# геттером вредна — Ozon детектит подмену (см. tests/test_cdp_launch.py).
#
# browser.tabs.unloadOnLowMemory=false — НЕ выгружать вкладки при нехватке
# памяти. Гейт стоит в самом Firefox (TabUnloader, omni.ja: getBoolPref
# ("browser.tabs.unloadOnLowMemory", true)). При 12 воркерах-FF память кончается,
# Firefox выгружал рабочую вкладку, и Marionette падал с NoSuchWindowError
# «Browsing context has been discarded» (живой прогон 2026-09-25).
FIREFOX_WINDOW_CLASS = "MozillaWindowClass"
FIREFOX_USER_PREFS = {
    "toolkit.telemetry.enabled": False,
    "browser.shell.checkDefaultBrowser": False,
    "browser.tabs.unloadOnLowMemory": False,
    "intl.accept_languages": "ru-RU, ru, en-US, en",
    "intl.locale.requested": "ru-RU",
}



def _window_for_pid(pid: int, require_visible: bool = True,
                    window_class: str | None = "Chrome_WidgetWin_1"):
    """Главное окно браузера процесса pid (user32 EnumWindows) или None.

    require_visible=True (по умолчанию) — первое ВИДИМОЕ окно (для минимизации);
    require_visible=False — главное окно независимо от видимости (для ПОКАЗА
    скрытого окна: фильтр по IsWindowVisible не годится, т.к. скрытое с
    рождения SW_HIDE окно иначе найти нельзя и показать нечем).
    window_class — имя класса окна; None = любой класс (для Firefox
    MozillaWindowClass; Chromium по умолчанию Chrome_WidgetWin_1).
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
        if window_class is not None:
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value != window_class:
                return True
        if require_visible and not user32.IsWindowVisible(hwnd):
            return True
        found.append(hwnd)
        return False  # первое подходящее окно — и достаточно
    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return found[0] if found else None


def _chrome_pids_for_port(port: int):
    """PID chrome-процессов с --remote-debugging-port=port (наш воркер)."""
    if os.name != "nt":
        return set()
    out = subprocess.run(
        ["wmic", "process", "where", "Name='chrome.exe'",
         "get", "ProcessId,CommandLine", "/format:csv"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=10).stdout
    pids = set()
    for line in out.splitlines():
        if f"--remote-debugging-port={port}" in line:
            parts = line.strip().split(",")
            if parts and parts[-1].strip().isdigit():
                pids.add(int(parts[-1].strip()))
    return pids


def _physical_window_rect(pid: int):
    """(left, top, width, height) OS-геометрии главного окна pid (Windows).

    По GetWindowPlacement.wrRestore: корректный размер окна Chrome В СВЁРНУТОМ
    состоянии (GetWindowRect при SW_MINIMIZE возвращает координаты свернутого
    окна в таскбаре, а не восстановленный прямоугольник). Берём наибольший по
    площади wrRestore среди Chrome_WidgetWin_* — главное окно Chrome может
    быть Chrome_WidgetWin_1 либо Chrome_WidgetWin_0; мелкие окна (всплывашки)
    игнорируются. Нужен для детекции рассинхрона «виртуальная геометрия CDP vs
    реальная OS-геометрия», из-за которого композитор рисует контент смещённо.
    """
    if os.name != "nt" or pid is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        best = None
        best_area = -1
        WINDOWPLACEMENT_SIZE = 44  # sizeof(WINDOWPLACEMENT) Win64

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_uint),
                ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint),
                ("ptMinPosition", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT),
            ]

        def _cb(hwnd, _lparam):
            nonlocal best, best_area
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value != pid:
                return True
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if not cls.value.startswith("Chrome_WidgetWin_"):
                return True
            wp = WINDOWPLACEMENT()
            wp.length = WINDOWPLACEMENT_SIZE
            user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
            r = wp.rcNormalPosition
            w = r.right - r.left
            h = r.bottom - r.top
            area = w * h
            if area > best_area:
                best = (r.left, r.top, w, h)
                best_area = area
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return best
    except Exception as e:
        log.debug(f"physical_window_rect: {type(e).__name__}: {e}")
    return None


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


class LaunchRun(NamedTuple):
    """Результат запуска лаунчера: путь лога + процесс (для fail-fast)."""

    path: str
    proc: subprocess.Popen


def _launch_browser(cdp_url: str, profile_path: str | None = None,
                    profile_token: str | None = None) -> LaunchRun:
    """Запуск живого Chromium через scripts/launch_chrome (порт из cdp_url).

    profile_path — полный путь --user-data-dir (для воркеров передаёт PROFILE
    env в launch_chrome.ps1/.sh; None = дефолтный профиль скрипта).
    CHROME env (из OZON_CHROME_PATH или уже заданной CHROME) выбирает
    исполняемый файл: системный Chrome либо автономный Chrome-for-Testing/Edge.
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
    chrome = _chrome_override()
    if chrome:
        env["CHROME"] = chrome
    # per-worker прокси (ревизия 2.9, net.py): 1 профиль = 1 IP. Дефолт — OFF
    # (нет env → нет переменной PROXY в лаунчере → Chromium идёт напрямую).
    from . import net
    k = net.worker_index_from_profile_token(profile_token)
    if k:
        proxy = net.proxy_url_for_worker(k)
        if proxy:
            try:
                net.chrome_proxy_arg(proxy)
            except ValueError as e:
                log.warning(f"[cdp] worker-{k}: некорректный прокси "
                            f"({type(e).__name__}: {e}) — запуск без прокси")
            else:
                env["PROXY"] = proxy
                log.info(f"[cdp] worker-{k}: прокси {net.mask_proxy(proxy)}"
                         + (" [ creds в URL: Chrome применит]" if net.
                            has_credentials(proxy) and not
                            proxy.lower().startswith("socks5") else
                            " [ creds в socks5-URL Chrome игнорирует — нужен "
                            "PAC]" if net.has_credentials(proxy) else ""))
    cmd = (["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)]
           if os.name == "nt" else [str(script)])
    # Вывод лаунчера НЕ выбрасываем (ревизия 2.9): при пустом/битом
    # launch_chrome.ps1 коллектор раньше получал только «CDP-браузер не
    # поднялся за 30с» и неделю гадал про DISPLAY, хотя Chromium даже не
    # запускался. Пишем в temp-файл, читаем хвост при неудаче.
    path = _LL.launcher_log_path(P.cdp_port(cdp_url))
    try:
        with open(path, "wb") as fh:      # truncate: лог всегда от THIS запуска
            proc = subprocess.Popen(cmd, env=env,
                                    stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=subprocess.STDOUT)
    except OSError as e:
        raise RuntimeError(f"не удалось запустить {_launch_script_name()}: "
                           f"{type(e).__name__}: {e}") from e
    return LaunchRun(path, proc)


def _launcher_log_tail(path, limit: int = 12) -> str:
    """Хвост вывода лаунчера (общий хелпер — см. launchlog.py)."""
    return _LL.read_log_tail(getattr(path, "path", path), limit)


def _launcher_tail_after_grace(path, grace_s: float = 3.0,
                               limit: int = 12) -> str:
    """Хвост лога, но пустоту подтверждаем только после паузы.

    Лаунчер — отдельный процесс: он мог ещё писать, когда мы уже сдались
    (наблюдалось: 0 байт при живом лаунчере, который дописал 293 байта через
    секунду). Без паузы «пусто» врёт и уводит диагностику не туда.
    """
    tail = _launcher_log_tail(path, limit)
    if tail and "не вывел ничего" not in tail:
        return tail
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        time.sleep(0.25)
        tail = _launcher_log_tail(path, limit)
        if tail and "не вывел ничего" not in tail:
            return tail
    return tail


# Запас на «опоздавший» браузер. Живой замер 2026-10-03 16:02-16:03: лаунчер
# сам писал «LAUNCHER-ERROR: Chromium pid=48408 is ALIVE but /json/version is
# silent» — то есть Chromium ЖИВ, DevTools ещё поднимается (профиль тяжёлый, на
# это уходит до минуты). Демон считал это провалом, снимал держателей и
# поднимал новый — тот умирал о тот же профиль, четыре раза подряд
# UNKNOWN_FAILURE, при том что браузер в итоге ЖИЛ и отвечал на 9222.
#
# Grace даётся ТОЛЬКО когда лаунчер прямо сообщил, что Chromium жив: признак
# «is ALIVE» в его выводе. При «EXITED» ждать нечего и fail-fast остаётся
# мгновенным (иначе каждая неудачная попытка стоила бы +30 с).
_LAUNCHER_EXIT_GRACE_S = 30.0


def _launcher_says_browser_alive(path: str) -> bool:
    """Лаунчер сообщил, что Chromium жив, но CDP молчит (браузер поднимается)."""
    try:
        tail = _launcher_log_tail(path, 40) or ""
    except Exception:
        return False
    low = tail.lower()
    return ("is alive" in low) or ("жив" in low)


def _await_browser(cdp_url: str, run: LaunchRun, deadline: float,
                   grace_s: float = _LAUNCHER_EXIT_GRACE_S) -> bool:
    """Ждать готовности CDP до deadline. False — не дождались.

    Отдельный хелпер, потому что попытка запуска может повторяться (см.
    _free_stale_profile): тело цикла и fail-fast на умершем лаунчере должны
    быть одними и теми же.

    Умерший лаунчер — fail-fast, ЕСЛИ он сообщил, что Chromium умер («EXITED»):
    ждать остаток 30 с бессмысленно, раньше неудачный старт стоил полные 30 с.
    Но если лаунчер написал, что Chromium ЖИВ, а CDP молчит — браузер ещё
    поднимается, и мы ждём grace_s. Иначе объявляем провал при живом браузере.
    """
    exited_at: float | None = None
    grace: float | None = None
    while time.monotonic() < deadline:
        if browser_alive(cdp_url):
            return True
        if run.proc.poll() is not None:
            if exited_at is None:
                exited_at = time.monotonic()
                if _launcher_says_browser_alive(run.path):
                    grace = grace_s
                    log.warning(f"[cdp] лаунчер сообщил, что Chromium жив, но CDP "
                                f"молчит — жду до {grace_s:.0f}с подъёма "
                                f"(профиль тяжёлый)")
                else:
                    # Лаунчер прямо сказал, что Chromium мёртв: ждать нечего,
                    # fail-fast без единого сна (иначе +30 с на каждую попытку).
                    return False
            elif time.monotonic() - exited_at >= (grace or 0.0):
                return False
        time.sleep(0.5)
    return False


def _free_stale_profile(cdp_url: str, profile_token: str) -> list[int]:
    """Снять держателей нашего профиля, когда CDP не отвечает. pid завершённых.

    Живой browser-процесс на нашем profile+port, у которого CDP не поднялся, для
    коллектора бесполезен: следующий Chrome уйдёт в него (профиль занят) и сразу
    умрёт — ровно тот «EXITED, CDP never came up», что наблюдался вживую: порт в
    момент старта держателя был занят, DevTools не поднялся, а профиль остался
    захваченным навсегда, три ретрая подряд.

    Убиваем ТОЛЬКО наш профиль: токен в --user-data-dir + маркер запуска
    .ozon_collector_launched (ручное окно пользователя не трогаем). Вызывается
    только после неудачного запуска, когда browser_alive == False.
    """
    try:
        holders = P.browser_processes(cdp_url, profile_token=profile_token)
    except Exception as e:                       # перечисление не критично
        log.warning("[cdp] не удалось перечислить процессы профиля "
                    f"{profile_token!r}: {type(e).__name__}: {e}")
        return []
    if not holders:
        # Порт в cdp_url мог уже смениться (динамическая база), а зомби
        # держит профиль на СТАРОМ порту: browser_processes матчит
        # (порт+профиль) и его не видит. Ищем по профилю на любом порту —
        # иначе следующий Chrome умрёт о захваченный профиль (ревью
        # 2026-10-15). Маркер запуска ниже по-прежнему отсекает чужой Chrome.
        try:
            holders = P.profile_processes(profile_token)
        except Exception as e:
            log.warning("[cdp] не удалось перечислить процессы по профилю "
                        f"{profile_token!r}: {type(e).__name__}: {e}")
            return []
    if not holders:
        return []
    if not _launch_marker(P.profile_path(profile_token)):
        log.warning(f"[cdp] профиль {profile_token!r} занят посторонним "
                    f"браузером (pid={[h.pid for h in holders]}), маркера "
                    f"запуска нет — чужое окно не трогаю")
        return []
    pids = [h.pid for h in holders]
    freed = P.kill_identities(holders)
    # Главный процесс убит — но renderer/gpu/utility держат профиль ещё секунды.
    # Живой замер 2026-10-03 16:0x: MAIN найден и убит, семь потомков остались,
    # и четыре запуска подряд Chromium EXITED с «profile captured by a live
    # instance». Поэтому ждём освобождения и добиваем тех, кто не ушёл.
    left = P.wait_profile_released(profile_token)
    if left:
        log.warning(f"[cdp] профиль {profile_token!r} всё ещё держат "
                    f"{len(left)} процессов-потомков {left} после убийства "
                    f"главного — добиваю")
        leftover = P.profile_processes(profile_token)
        freed += P.kill_identities(leftover)
        still = P.wait_profile_released(profile_token, timeout_s=5.0)
        if still:
            log.error(f"[cdp] профиль {profile_token!r} НЕ освободился: "
                      f"{still} держат профиль, следующий Chromium упадёт")
        else:
            log.info(f"[cdp] профиль {profile_token!r} освобождён полностью")
    log.warning(f"[cdp] профиль {profile_token!r} держал нерабочий браузер "
                f"(CDP не отвечает): снят pid={pids} -> освобождён={freed}")
    return freed


def profile_browser_alive(profile_token: str = P.PROFILE_TOKEN) -> bool:
    """Жив ли НАШ браузер на этом профиле — на любом порту.

    Проверять живость по номеру порта нельзя: база выбирается динамически, и
    когда наш браузер уже занял 9444, «свободная база» уезжает на 9445, а
    проверка на жёстком 9222 всегда False. Живой замер 2026-10-03: демон счёл
    main остановленным, скопировал куки из профиля, который держал живой
    Chromium, и упал на shutil.copy2 с [WinError 32].
    """
    return any(browser_alive(f"http://127.0.0.1:{p}")
               for p in P.profile_debug_ports(profile_token))


def effective_cdp_url(cdp_url: str,
                       profile_token: str | None = None) -> str:
    """Фактический CDP-URL, по которому надо подключаться.

    Зачем: launch_and_wait возвращает только bool, поэтому сдвинутая база
    (Windows забрала 9222 в исключения) терялась, и Session._connect уходил в
    connect_over_cdp на мёртвый 9222 — ECONNREFUSED при живом браузере на 9444
    (живой замер 2026-10-03). Здесь сдвиг разрешается ОДИН раз и попадает в
    Session.cdp_url, оттуда же берут порт воркеры и BrowserProcessManager.
    """
    if browser_alive(cdp_url):
        return cdp_url
    _token = profile_token or P.PROFILE_TOKEN
    live = sorted(p for p in P.profile_debug_ports(_token)
                  if browser_alive(f"http://127.0.0.1:{p}"))
    if live:
        if P.cdp_port(cdp_url) != live[0]:
            log.warning(f"[cdp] подключаюсь к нашему браузеру на порту "
                        f"{live[0]}, а cdp_url={cdp_url} — база сдвинулась")
        return f"http://127.0.0.1:{live[0]}"
    _base, _why = P.cdp_port_base()
    return f"http://127.0.0.1:{_base}"


def launch_and_wait(cdp_url: str = "http://127.0.0.1:9222",
                    wait_s: float = 30.0,
                    profile_token: str | None = None) -> bool:
    """Авто-запуск браузера, если он закрыт; ждёт готовности до wait_s.

    Возвращает True, если браузер жив после ожидания (запущен нами или уже был).
    При недоступности — RuntimeError с подсказкой ручного запуска.
    profile_token — токен профиля воркера (None = дефолтный профиль).

    Одна попытка доводится до конца: если запуск упал, а профиль при этом
    держал нерабочий экземпляр — снимаем его и пробуем ещё раз (иначе падение
    необратимо, т.к. каждый следующий запуск умирает о тот же профиль).
    """
    if browser_alive(cdp_url):
        return True

    # ПОРТ ПРОВЕРЯЕТСЯ ДО ЗАПУСКА. Живой замер 2026-10-03: Windows взяла
    # диапазон 9172-9271 в исключения, поэтому 9222 (и воркерские 9223-9230)
    # давали bind WSAEACCES — Chromium физически не мог поднять DevTools, и
    # каждый запуск падал с «EXITED, CDP never came up» при свободном порте.
    # Диагностика печаталась в тексте ошибки, но запуск всё равно шёл на
    # зарезервированный порт, поэтому здесь порт ПОДМЕНЯЕТСЯ до запуска.
    _want_port = P.cdp_port(cdp_url)
    _bindable, _why = P.port_bindable(_want_port)
    if not _bindable:
        # Порт занят — но кем? Различаем принципиально:
        #   * держит НАШ браузер (поднимается/перезапускается) → ЖДЁМ его, а не
        #     начинаем второй: второй Chrome с тем же профилем умирает
        #     («profile captured by a live instance»);
        #   * никого нашего нет → порт забрала ОС (WSAEACCES) → переключаемся.
        # Живой замер 2026-10-03: Session._connect вызывает launch_and_wait
        # повторно, когда connect_over_cdp не удался; порт был занят нашим же
        # только что поднятым Chrome, код уезжал на 9445 и стартовал второй
        # браузер с тем же профилем — тот EXITED, и так четыре раунда подряд.
        _token = profile_token or P.PROFILE_TOKEN
        _held = P.browser_processes(cdp_url, _token)
        if not _held:
            # Слепая зона browser_processes: он матчит (порт + профиль), а
            # повторный вызов приходит со СТАРЫМ cdp_url, когда наш браузер уже
            # поднят на новой базе. Ищем по профилю на любом порту.
            _ports = {p: pids for p, pids in P.profile_debug_ports(
                _token).items() if p != _want_port}
            if _ports:
                _live = sorted(p for p in _ports if browser_alive(
                    f"http://127.0.0.1:{p}"))
                log.warning(f"[cdp] наш браузер жив на другом порту "
                            f"{sorted(_ports)} (профиль {_token}), а вызов пришёл "
                            f"с {_want_port}: второй Chromium с тем же профилем "
                            f"умер бы EXITED — беру живой порт")
                if _live:
                    cdp_url = f"http://127.0.0.1:{_live[0]}"
                    return True
                _dl = time.monotonic() + wait_s
                while time.monotonic() < _dl:
                    _live = sorted(p for p in _ports if browser_alive(
                        f"http://127.0.0.1:{p}"))
                    if _live:
                        cdp_url = f"http://127.0.0.1:{_live[0]}"
                        return True
                    time.sleep(0.5)
                log.warning(f"[cdp] наш браузер на {sorted(_ports)} не ожил за "
                            f"{wait_s:.0f}с — освобождаю профиль и запускаю "
                            f"заново")
                _free_stale_profile(cdp_url, _token)
                _ports = {}
        if _held:
            log.warning(f"[cdp] порт {_want_port} занят НАШИМ браузером "
                        f"(pid={[h.pid for h in _held]}): {_why} — жду его "
                        f"подъёма, второй браузер не запускаю")
            _dl = time.monotonic() + wait_s
            while time.monotonic() < _dl:
                if browser_alive(cdp_url):
                    log.info(f"[cdp] наш браузер на {_want_port} ожил — "
                             f"продолжаю")
                    return True
                time.sleep(0.5)
            log.warning(f"[cdp] наш браузер на {_want_port} не ожил за "
                        f"{wait_s:.0f}с — освобождаю профиль и запускаю заново")
            _free_stale_profile(cdp_url, _token)
        if not _held and not _ports:
            _base, _base_why = P.cdp_port_base()
            cdp_url = f"http://127.0.0.1:{_base}"
            log.error(f"[cdp] ПОРТ {_want_port} НЕПРИГОДЕН ({_why}); "
                      f"переключаюсь на {_base} — {_base_why}. Если 9222 "
                      f"нужен именно, освободи диапазон: netsh int ipv4 show "
                      f"excludedportrange protocol=tcp")

    log.warning(f"[cdp] браузер не запущен на {cdp_url}, запускаю…",
                extra={"cdp_url": cdp_url})
    profile_path = None
    if profile_token:
        profile_path = P.profile_path(profile_token)
    token = profile_token or P.PROFILE_TOKEN
    run = _launch_browser(cdp_url, profile_path=profile_path,
                          profile_token=profile_token)
    logf = run.path
    ok = _await_browser(cdp_url, run, time.monotonic() + wait_s)
    freed: list[int] = []
    if not ok:
        freed = _free_stale_profile(cdp_url, token)
        if freed:
            run = _launch_browser(cdp_url, profile_path=profile_path,
                                  profile_token=profile_token)
            ok = _await_browser(cdp_url, run, time.monotonic() + wait_s)
    try:
        if not ok:
            # Подсказка — платформенная (DISPLAY значим только для X-сервера),
            # плюс фактический вывод лаунчера: иначе «не поднялся за 30с» без
            # причины (ревизия 2.9: так и не увидели обнулённый launch_chrome.ps1).
            #
            # ВАЖНО: различаем «порт занят» и «порт зарезервирован Windows».
            # Живой замер 2026-10-03: Chromium EXITED, CDP never came up — а порт
            # был СВОБОДЕН, просто лежал в диапазоне исключений 9172-9271
            # (WSL/Hyper-V пересчитали резервации), и bind давал WSAEACCES.
            # Старая подсказка «порт занят старым» уводила в сторону.
            _port = P.cdp_port(cdp_url)
            _ok, _why = P.port_bindable(_port)
            _port_note = ("" if _ok else
                          f"; ДИАГНОСТИКА ПОРТА: {_why}")
            if os.name == "nt":
                hint = (r"проверьте Chromium и профиль (частая причина: профиль "
                        r"уже открыт другим Chrome), либо запустите вручную: "
                        r"powershell -ExecutionPolicy Bypass "
                        r"-File scripts\launch_chrome.ps1"
                        + _port_note)
            else:
                hint = ("проверьте DISPLAY/Xvfb и Chromium, либо запустите вручную: "
                        "sh scripts/launch_chrome.sh" + _port_note)
            code = run.proc.poll()
            exit_note = ("лаунчер ещё работает" if code is None
                         else f"код возврата лаунчера={code}")
            freed_note = (f"; профиль освобождён от pid={freed}" if freed
                          else "; профиль свободен по нашим процессам — "
                               "виновник вне коллектора")
            raise RuntimeError(
                f"CDP-браузер не поднялся за {wait_s:.0f}с на {cdp_url} "
                f"({exit_note}); {hint}{freed_note}"
                + f"\nвывод {_launch_script_name()} ({logf}):"
                + _launcher_tail_after_grace(logf))
    finally:
        # лог не удаляем намеренно: после неудачи это улика, путь известен
        # (_LL.launcher_log_path) и печатается в тексте ошибки выше
        pass
    return True


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
        # kind: движок браузера (OZON_BROWSER, дефолт = chrome). 'chrome' —
        # CDP-подключение к живому Chromium; 'firefox' — stock Firefox +
        # geckodriver/Selenium, persistent-профиль (CDP нет).
        self.kind = B.browser_kind()
        self.auto_launch = (auto_launch if auto_launch is not None
                            else os.environ.get("OZON_AUTO_LAUNCH", "1") != "0")
        # keep_window: None → env OZON_KEEP_WINDOW (1 = окно видимо/наверху),
        # иначе явный bool из конфига CLI (--window-mode visible/hidden).
        self.keep_window = (keep_window if keep_window is not None
                            else os.environ.get("OZON_KEEP_WINDOW") == "1")
        self.browser_owner = "external"   # external | collector (ADR §14.1)
        self.browser_pid = None
        self.browser_profile = (
            P.firefox_profile_path(profile_token)
            if self.kind == "firefox"
            else P.profile_path(profile_token))
        self._playwright = None
        self._browser = None
        self.page = None
        self._watchdog = None
        self._managed_pages: list = []
        self.fingerprint = None
        self._fingerprint_captured = False
        self._connect()

    def _connect(self):
        if self.kind == "firefox":
            self._connect_firefox()
            return
        # Окно пользователя запоминаем ТОЛЬКО когда нашего браузера ещё нет:
        # Chromium крадёт фокус на старте (замер 2026-10-06: foreground ==
        # chrome через 0.3-0.4 с при скрытом окне), и вернуть фокус можно
        # только на то окно, которое было в фокусе ДО запуска. Если браузер
        # уже жив и фокус на нём — запоминать нечего, и запомнить своё же
        # окно нельзя (`note_user_window` отфильтровывает pid из реестра гварда,
        # а на этом шаге pid ещё не зарегистрирован — ревью 2026-10-06).
        # Второй вызов — после регистрации pid, для случая «браузер уже был жив».
        # Гвард по умолчанию выключен, guard() может быть None.
        _wp_guard = _WP.guard()
        was_alive = browser_alive(self.cdp_url)
        if self.auto_launch and not was_alive:
            if _wp_guard is not None:
                _wp_guard.note_user_window()
            launch_and_wait(self.cdp_url, profile_token=self.profile_token)
            # База могла сдвинуться (9222 забрала Windows) — подключаться
            # надо к фактическому порту, иначе ECONNREFUSED при живом браузере.
            self.cdp_url = effective_cdp_url(self.cdp_url,
                                             self.profile_token)
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
                try:
                    self._browser = self._playwright.chromium.connect_over_cdp(
                        self.cdp_url)
                except Exception:
                    # Второй connect тоже упал — гасим запущенный
                    # Playwright-подпроцесс, иначе он утекает (ревью
                    # 2026-10-15).
                    try:
                        self._playwright.stop()
                    except Exception:
                        pass
                    raise
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
        self._attach_geom_guard()
        if self.keep_window:
            # Окно коллектора видимо и на всех: не сворачиваем (env/CLI
            # --window-mode visible). SWP/ShowWindow снаружи бесполезны,
            # т.к. Chrome возвращает окно: запрет минимизации — здесь.
            self._keep_window_visible()
            self._sync_window_guard(" (connect)", enforce_hidden=False)
        else:
            self._minimize_window()
            self._sync_window_guard(" (connect)", enforce_hidden=True)
        # Теперь pid зарегистрирован (или гвард выключен/в external — тогда
        # note_user_window ничего не запомнит, см. его проверку по реестру).
        if _wp_guard is not None:
            _wp_guard.note_user_window()

    def _connect_firefox(self):
        """Firefox-бэкенд: stock Firefox через geckodriver (Selenium/Marionette).

        ВНИМАНИЕ, статус 2026-09-25: Ozon отдаёт заглушку «нет соединения» на
        первом же goto ЛЮБОЙ сессии geckodriver/Marionette — тёплый профиль,
        пустой профиль, без prefs, с включённым OCSP: все варианты заблокированы
        за 6-8с. Chromium CDP при этом проходит, в том числе с
        navigator.webdriver === true (то есть сам флаг — не причина; см.
        browser_kind с полной таблицей замеров). Причина — marionette-сессия
        как таковая; отдельный её tell не локализован, лечится сменой транспорта.

        Транспорт: НЕ Playwright, а Selenium + шим ffsel.FirefoxPage,
        повторяющий нужный поднабор page-API (evaluate/locator/mouse/screenshot).
        Ветка остаётся для отладки профилей и офлайн-разбора, боевой сбор —
        на OZON_BROWSER=chrome (дефолт).

        Профиль всегда наш → browser_owner='collector' (перезапуск разрешён).
        OZON_FF_PRIVATE=1 — без профиля (аналог инкогнито, временный профиль).
        OZON_FIREFOX_PATH — путь к firefox.exe; по умолчанию системный. Сюда
        НЕЛЬЗЯ указывать patched-сборку ms-playwright/firefox-*.
        """
        private = os.environ.get("OZON_FF_PRIVATE", "0") == "1"
        if not private:
            # Профиль не должен быть занят чужим firefox (после crash-инстанса).
            # Процессы на нашем профиле = наши по ownership-контракту — гасим.
            stale = P.firefox_browser_processes(self.profile_token)
            if stale:
                log.warning(f"[cdp] firefox: профиль занят {len(stale)} процессами "
                            f"— останавливаю перед запуском")
                P.kill_records([P.ProcessRecord("prelaunch", ident)
                                for ident in stale], wait_s=3.0)
        binary = os.environ.get("OZON_FIREFOX_PATH") or None
        driver, page, adapter = ffsel.connect(
            profile_path=None if private else self.browser_profile,
            prefs=FIREFOX_USER_PREFS,
            binary=binary)
        self._playwright = None
        self._driver = driver
        self._browser = adapter
        self.browser_owner = "collector"
        try:
            procs = P.firefox_browser_processes(self.profile_token)
            self.browser_pid = procs[0] if procs else None
        except Exception:
            self.browser_pid = None
        self.page = page
        self._managed_pages = [self.page]
        self._attach_geom_guard()
        log.info(f"[cdp] firefox connected via selenium: profile="
                 f"{'private' if private else self.browser_profile}, "
                 f"pid={getattr(self.browser_pid, 'pid', None)}, "
                 f"binary={binary or 'system'}")
        if self.keep_window:
            self._keep_window_visible()
        elif getattr(self, "kind", "chrome") == "firefox":
            # Свёрнутое окно Firefox не рендерит lazy-блоки: IntersectionObserver
            # не срабатывает, webListReviews не монтируется — 0 отзывов через
            # cli.py при 12 в том же профиле с видимым окном (замер 2026-09-25,
            # D:\Temp\opencode\ff_vmode_probe.py). Поэтому окно разворачиваем
            # без активации (WS_EX_NOACTIVATE + TOPMOST) — фокус не крадём.
            log.info("[cdp] firefox: окно разворачиваю без фокуса — свёрнутое "
                     "окно не монтирует lazy-блоки отзывов и теряет вкладку "
                     "при выгрузке по памяти")
            self._restore_window_native(pids=self._firefox_pids(),
                                        window_class=FIREFOX_WINDOW_CLASS)
        else:
            self._minimize_window()

    def _minimize_window(self):
        """Сворачивает окно Chromium, чтобы не перекрывало работу пользователя.

        При запуске и подключении CDP Chromium разворачивает окно поверх всех
        и крадёт фокус. Сворачивание только user32:
        WS_EX_NOACTIVATE + SW_MINIMIZE на главное окно — окно уходит свёрнутым
        и не получает фокус при разворачивании (важно при live-сборе, чтобы
        печать в другом приложении не прерывалась).
        Игнорируем ошибки: сбой минимизации некритичен.

        ВАЖНО (fixсдвига рендера): НЕ использовать CDP Browser.setWindowBounds
        с windowState=minimized. Он «замораживает» внутреннюю модель окна
        Chromium на стартовом размере (1440x900): при последующем
        восстановлении/максимизации пользователем физическое окно растёт
        (например до 1920x1032), а viewport (innerWidth/innerHeight) остаётся
        от свёрнутого состояния → контент рисуется в левом верхнем углу.
        user32-сворачивание движет окно Windows, но CDP-модель остаётся
        normal/синхронна, поэтому максимизация корректно пересчитывает viewport.
        """
        # fix рассинхрона OS-window vs Chromium-модель: если физическое окно
        # другого размера, чем внутренняя модель Chromium (1440x900),
        # композитор рисует контент со смещением от оси мыши → синхронизируем
        # геометрию ДО сворачивания. Только для collector-браузеров:
        # external (чужой Chrome пользователя) не трогаем — его окно личное.
        if getattr(self, "browser_owner", None) == "collector":
            self._normalize_window_geometry()
        if getattr(self, "kind", "chrome") == "firefox":
            # Firefox не сворачиваем НИКОГДА: свёрнутое окно не рендерит
            # lazy-блоки (0 отзывов) и теряет вкладку при выгрузке по памяти
            # («Browsing context has been discarded», живой прогон 12 воркеров
            # 2026-09-25). Разворачиваем без фокуса — см. _apply_window_policy.
            log.info("firefox: игнорирую minimize — разворачиваю без фокуса")
            self._restore_window_native(pids=self._firefox_pids(),
                                        window_class=FIREFOX_WINDOW_CLASS)
        else:
            self._minimize_window_native(P.cdp_port(self.cdp_url))

    def _apply_window_policy(self, why: str = ""):
        """Единая политика окна браузера коллектора (connect/new_page/recycle).

        keep_window=1 (OZON_KEEP_WINDOW) → развернуть и показать.
        Firefox (always) → развернуть БЕЗ фокуса: свёрнутое окно не рендерит
        lazy-блоки (IntersectionObserver молчит) и попадает под выгрузку
        вкладок Firefox при нехватке памяти — живой прогон 2026-09-25 с 12
        воркерами отваливался на «Browsing context has been discarded» (worker
        падал на ПЕРВОМ товаре, когда new_page сворачивал окно).
        Chromium → свернуть, чтобы не перекрывал работу пользователя.

        Сторож окон (window_policy) держит инвариант ПОСЛЕ этих вызовов: окно
        само всплывает через ~30-40 с после старта и забирает фокус, а этот
        код вызывается только на connect/new_page/recycle.
        """
        if getattr(self, "keep_window", False):
            self._keep_window_visible()
            self._sync_window_guard(why, enforce_hidden=False)
        elif getattr(self, "kind", "chrome") == "firefox":
            log.debug("firefox: окно разворачиваю без фокуса%s", why)
            self._restore_window_native(pids=self._firefox_pids(),
                                        window_class=FIREFOX_WINDOW_CLASS)
            self._sync_window_guard(why, enforce_hidden=False)
        else:
            self._minimize_window()
            self._sync_window_guard(why, enforce_hidden=True)

    def _sync_window_guard(self, why: str = "", enforce_hidden: bool = False):
        """Регистрировать окно браузера в стороже окон (см. window_policy).

        По умолчанию сторож ВЫКЛЮЧЕН (`OZON_WINDOW_GUARD=1` включает): окна
        браузеров коллектора — рабочие окна пользователя, он сам их открывает
        и сворачивает, и просьба «трогай только свои» (2026-10-06) означает
        отказ от любого вмешательства по умолчанию. Здесь же проверяется
        keep_window, owner и движок: даже при включённом флаге сторож работает
        только для собственных браузеров коллектора в режиме hidden.
        """
        pid = getattr(getattr(self, "browser_pid", None), "pid", None)
        if not pid:
            return
        guard = _WP.guard()
        if guard is None:
            return                      # выключено — окна пользователя не трогаем
        hide = (enforce_hidden
                and not getattr(self, "keep_window", False)
                and getattr(self, "browser_owner", None) == "collector"
                and getattr(self, "kind", "chrome") == "chrome")
        if hide:
            guard.watch(pid, P.cdp_port(self.cdp_url))
        else:
            guard.forget(pid)
        log.debug(f"window_policy: pid={pid} hidden={hide}{why}")

    def _firefox_pids(self) -> list:
        """PIDs нашего Firefox (все процессы на профиле, для native-окна).

        Playwright поднимает ДВА firefox.exe на одном -profile (launcher +
        main browser); главное окно MozillaWindowClass висит на одном из них —
        перебираем все, иначе не найдём окно для minimize/restore.
        """
        try:
            return [i.pid for i in
                    P.firefox_browser_processes(self.profile_token)]
        except Exception:
            return []

    def _normalize_window_geometry(self, width: int = 1440,
                                   height: int = 900,
                                   timeout: float = 2.0) -> bool:
        """Синхронизирует физическое OS-окно воркера с моделью Chromium.

        Бывает рассинхрон: Chromium свёрнут, а его внутренняя модель окна
        (Browser.getWindowForTarget) не совпадает с реальной OS-геометрией
        (GetWindowRect). Тогда композитор рисует viewport по «виртуальным»
        координатам, а OS показывает окно в других пикселях → визуально весь
        контент «съехал» относительно кликов (клики по DOM попадают в место,
        а картинка на экране — выше/левее). Лекарство: принудительно выставить
        browser bounds (= установка геометрии Chromium), подтвердить, что
        физическое окно стало правильного размера, — после этого отрисовка
        совпадает с осью мыши. Возвращает True если синхронизация нужна и
        выполнена (окно было рассинхронизировано).
        """
        if os.name != "nt":
            return False
        if getattr(self, "kind", "chrome") == "firefox":
            # Firefox: нет CDP Browser.setWindowBounds — чинить нечем, нечего
            # и проверять (рассинхрон — chromium-специфика композитора).
            return False
        pid = getattr(getattr(self, "browser_pid", None), "pid", None)
        phys = _physical_window_rect(pid)
        if phys is None:
            # окно не нашли (ещё не создано / не Windows) — нечего чинить
            return False
        if abs(phys[2] - width) < 2 and abs(phys[3] - height) < 2:
            # физический размер совпадает с ожидаемым — рассинхрона нет
            return False
        log.info(f"window geometry desync: physical={phys}, want {width}x{height}"
                 f" — fixing via CDP setWindowBounds")
        ok = False
        try:
            sess = self._browser.new_browser_cdp_session()
            targets = sess.send("Target.getTargets")
            for t in targets.get("targetInfos", []):
                if t.get("type") != "page":
                    continue
                res = sess.send("Browser.getWindowForTarget",
                                {"targetId": t["targetId"]})
                sess.send("Browser.setWindowBounds", {
                    "windowId": res["windowId"],
                    "bounds": {"left": 0, "top": 0,
                               "width": width, "height": height,
                               "windowState": "normal"},
                })
                break
            deadline = time.time() + timeout
            while time.time() < deadline:
                cur = _physical_window_rect(pid)
                if cur and abs(cur[2] - width) < 2 and abs(cur[3] - height) < 2:
                    ok = True
                    break
                time.sleep(0.05)
        except Exception as e:
            log.debug(f"normalize_window_geometry: {type(e).__name__}: {e}")
        if ok:
            log.info("window geometry sync: OK")
        else:
            log.info("window geometry sync: failed, physical="
                     f"{_physical_window_rect(pid)}")
        return ok

    def _assert_geometry_ok(self, page, tolerance: float = 3.0):
        """Read-only инвариант «физическое OS-окно == CDP-модель» (для solve).

        Сравнивает ЧИСЛОВУЮ геометрию: GetWindowRect(hwnd) vs CDP
        Browser.getWindowBounds. Поле windowState в сравнении НЕ участвует
        (2026-09-22 live: CDP говорит minimized при физически восстановленном
        окне — это ожидаемый компромисс от восстановления через Win32 в обход
        CDP, а не рассинхрон; иначе guard ловил бы ложное срабатывание каждый
        прогон).

        Только чтение — ничего не двигает и не ресайзит. Возвращает
        (ok: bool, metrics: dict). Не-Windows / unknown pid / нет окна → True
        (нечего проверять, solve не блокируем).
        """
        if os.name != "nt":
            return True, {}
        pid = getattr(getattr(self, "browser_pid", None), "pid", None)
        if pid is None:
            return True, {}
        phys = _physical_window_rect(pid)
        if phys is None:
            # окно не нашли — ничего не сравниваем (external браузеры тоже тут)
            return True, {}
        l, t, w, h = int(phys[0]), int(phys[1]), int(phys[2]), int(phys[3])
        metrics = {"hwnd_rect": [l, t, w, h]}
        if getattr(self, "kind", "chrome") == "firefox":
            # нет CDP-модели окна (Firefox) — сравнивать не с чем
            metrics["browser_kind"] = "firefox"
            return True, metrics
        cw = ch = None
        try:
            sess = page.context.new_cdp_session(page)
            res = sess.send("Browser.getWindowForTarget")
            bnd = sess.send("Browser.getWindowBounds", {"windowId": res["windowId"]})
            b = bnd.get("bounds") or {}
            cw, ch = b.get("width"), b.get("height")
            metrics["cdp_bounds"] = [b.get("left"), b.get("top"), cw, ch]
            metrics["cdp_window_state"] = b.get("windowState")
        except Exception as e:
            log.debug(f"_assert_geometry_ok: cdp bounds err "
                      f"{type(e).__name__}: {e}")
            metrics["cdp_bounds_err"] = f"{type(e).__name__}"
        if cw is None or ch is None:
            # CDP-модель недоступна — по одному HWND не судиим о рассинхроне
            return True, metrics
        ok = (abs(w - int(cw)) <= tolerance and abs(h - int(ch)) <= tolerance)
        log.debug(f"geometry check hwnd={[l, t, w, h]} "
                  f"cdp={metrics.get('cdp_bounds')} ok={ok}")
        return ok, metrics

    def _geometry_guard(self, page, tolerance: float = 3.0):
        """Инвариант перед решением пазла: validate → normalize if fail →
        revalidate. Возвращает (ok, metrics). Разделяет ЧТЕНИЕ (всегда
        читает-редактирует только при расхождении) и ДЕЙСТВИЕ подъёма
        геометрии — не пере-устанавливает геометрию на каждый пазл.
        """
        ok, m = self._assert_geometry_ok(page, tolerance=tolerance)
        if ok:
            m["normalized"] = False
            return True, m
        log.warning(f"geometry desync перед solve: metrics={m} — "
                    f"пытаюсь синхронизировать")
        try:
            self._normalize_window_geometry()
        except Exception as e:
            log.debug(f"_geometry_guard: normalize err "
                      f"{type(e).__name__}: {e}")
        ok2, m2 = self._assert_geometry_ok(page, tolerance=tolerance)
        m2.update({"normalized": True, "after_each": m2.get("hwnd_rect")})
        return ok2, m2

    @staticmethod
    def _minimize_window_native(port: int | None = None, pids=None,
                                window_class: str = "Chrome_WidgetWin_1"):
        """user32: свёртывание + запрет активации окна браузера (Windows).

        Chromium: pids вычисляются по port (chrome с --remote-debugging-port,
        наш воркер). Firefox: pids передаются явно (нет порта), класс окна
        MozillaWindowClass. Не-Windows — no-op.
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

            if pids is None:
                if port is None:
                    return
                pids = _chrome_pids_for_port(port)
            for pid in pids:
                hwnd = _window_for_pid(pid, window_class=window_class)
                if hwnd:
                    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                          style | WS_EX_NOACTIVATE)
                    user32.ShowWindow(hwnd, SW_MINIMIZE)
                    break
        except Exception as e:
            log.debug(f"minimize_window native: {type(e).__name__}: {e}")

    def _keep_window_visible(self):
        """OZON_KEEP_WINDOW=1: развернуть окно браузера и показать его.

        Chromium: через CDP Browser.setWindowBounds просим normal + ставим
        координаты в границы экрана (Chrome сам задаёт позицию, SWP снаружи
        бесполезен). Firefox: CDP bounds нет — только native SW_RESTORE+TOPMOST.
        """
        if getattr(self, "kind", "chrome") == "firefox":
            self._restore_window_native(pids=self._firefox_pids(),
                                        window_class=FIREFOX_WINDOW_CLASS)
            return
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
    def _restore_window_native(port: int | None = None, pids=None,
                               window_class: str = "Chrome_WidgetWin_1"):
        """user32: SW_RESTORE + TOPMOST окна браузера (Windows), без активации.

        Дублирует путь launch_chrome.ps1, но применим к уже работающему окну
        (после reconnect/перезапуска коллектором). Chromium: pids из порта;
        Firefox: pids явные + класс MozillaWindowClass. NOACTIVATE + SW_RESTORE,
        фокус не крадём.
        """
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            SW_RESTORE = 9
            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010

            if pids is None:
                if port is None:
                    return
                pids = _chrome_pids_for_port(port)
            for pid in pids:
                # require_visible=False: главное окно браузера находим даже В
                # СКРЫТОМ состоянии (SW_HIDE при старте) — иначе его нельзя
                # найти и показать (замкнутый круг). SW_RESTORE +
                # SWP_SHOWWINDOW гарантированно показывают невидимое окно.
                hwnd = _window_for_pid(pid, require_visible=False,
                                       window_class=window_class)
                if hwnd:
                    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                          style | WS_EX_NOACTIVATE)
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                                        | 0x0040)  # SWP_SHOWWINDOW
                    break
        except Exception as e:
            log.debug(f"restore_window native: {type(e).__name__}: {e}")

    @staticmethod
    def _firefox_context_lost(e: Exception) -> bool:
        """Firefox потерял вкладку: её выгрузили (дискард) или закрыли.

        Marionette отдаёт NoSuchWindowException «Browsing context has been
        discarded» (см. Firefox: driver.sys.mjs assert.open). Лечится свежей
        вкладкой — сгоревшая вкладка не восстанавливается сама.
        """
        msg = str(e).lower()
        return ("browsing context has been discarded" in msg
                or type(e).__name__ in ("NoSuchWindowException",
                                        "NoSuchWindowError"))

    def goto(self, url: str, wait_ms: int = 7000):
        """Перейти на страницу, дождаться domcontentloaded + паузу."""
        self._guard_check()
        log.debug(f"goto: {url}", extra={"wait_ms": wait_ms})
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            if (getattr(self, "kind", "chrome") == "firefox"
                    and self._firefox_context_lost(e)):
                log.warning(f"[cdp] firefox: вкладка потеряна при goto "
                            f"({type(e).__name__}) — новая вкладка + повтор")
                self.new_page(close_current=True)
                self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            elif is_renderer_failure(e):
                log.warning(f"goto renderer_failure: {url} → {type(e).__name__}: {e}")
                raise
            else:
                msg = str(e)
                # Навигационные сбои (таймаут, DNS, ERR_*) означают, что
                # страница НЕ загрузилась: продолжать разбор по старой/
                # ошибочной странице нельзя — пробрасываем, иначе соберём
                # чужой/пустой контент.
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
                " hidden: document.hidden,"
                " visibility: document.visibilityState,"
                " focused: document.hasFocus(),"
                " ow: window.outerWidth,"
                " oh: window.outerHeight,"
                "})"
            )
            if isinstance(fp, dict):
                self.fingerprint = fp
                self._log_visibility_telemetry(fp)
        except Exception:
            pass

    def _log_visibility_telemetry(self, fp: dict):
        """Отметить Page Visibility/focus-след окна (ревизия 2.9, L1/L10).

        У minimized-окна `document.hidden === true`, у окна на неактивном
        виртуальном рабочем столе Windows — тоже. Это измеримый признак
        «человека за столом нет»; сам по себе он не блокирует, но позволяет
        A/B-сравнить режимы окна по `challenges` вместо догадок. Ничего не
        подделывается — только логируется.
        """
        hidden = fp.get("hidden")
        vis = fp.get("visibility") or "?"
        if hidden:
            log.info(f"[cdp] page visibility: hidden=True (state={vis}, "
                     f"keep_window={self.keep_window}) — окно свёрнуто или "
                     f"на неактивном виртуальном столе; A/B против "
                     f"--window-mode visible см. §10")
        else:
            log.debug(f"[cdp] page visibility: visible (state={vis})")
        if fp.get("webdriver") is True:
            log.warning(f"[cdp] navigator.webdriver === true — браузер запущен "
                        f"с чужими флагами (см. §5: --enable-automation не "
                        f"передаём, init-script подмены нет)")


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
        self._attach_geom_guard()
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
        self._apply_window_policy(" (new_page)")
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
                                             profile_token=self.profile_token,
                                             kind=self.kind)
            self._watchdog.start()
            log.debug(f"watchdog started: {self.cdp_url}")

    def watchdog_stop(self):
        """Остановить фоновый сторож (если запущен)."""
        if self._watchdog is not None:
            self._watchdog.stop()
            log.debug("watchdog stopped")
            self._watchdog = None

    def _attach_geom_guard(self):
        """Привязать read-only геометрический guard к текущей странице.

        page-атрибут (не глобальное состояние, не меняет сигнатуры вызовов):
        puzzle.solve_puzzle берёт `getattr(page, "_geom_guard", None)` и
        вызывает его перед решением. Guard живёт в Session — ему нужны
        browser_pid/новый browser (Win32 GetWindowRect + CDP bounds).
        External-браузеры тоже получают guard, но его проверка для
        не-Windows/без-окна возвращает True (no-op) — личную геометрию
        пользователя не трогаем.
        """
        try:
            self.page._geom_guard = self._geometry_guard
        except Exception:
            pass

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
        """Закрыть лишние вкладки и завершить сессию браузера.

        Браузер НЕ гасится — остаётся жив для следующей команды (chrome).
        Закрываются только managed-вкладки коллектора; рабочая вкладка:
        collector → about:blank, external → закрывается (она создана нами
        через new_page; вкладки оператора не трогаются).
        """
        self.watchdog_stop()
        log.debug("Session.close()")
        if getattr(self, "kind", "chrome") == "firefox":
            # Firefox/Selenium: владение драйвером → quit() закрывает и окно.
            # Куки/сессия остаются в профиле и переживают перезапуск.
            log.info("[cdp] close: firefox driver quit (окно закрыто, сессия "
                     "сохранена в профиле)")
            try:
                if self._browser is not None:
                    self._browser.quit()
            except Exception:
                pass
            self._playwright = None
            self._driver = None
            self._browser = None
            if getattr(self, "_managed_pages", None) is not None:
                self._managed_pages = []
            return
        try:
            ctx = (self._browser.contexts[0]
                   if self._browser and self._browser.contexts else None)
            if ctx:
                # Закрываем ТОЛЬКО те вкладки, которые открыл коллектор.
                # Раньше закрывался `ctx.pages[1:]`, а `pages[0]` уводилась на
                # about:blank — для внешнего браузера (browser_owner ==
                # "external", то есть чужой Chrome оператора) это закрывало его
                # вкладки и теряло несохранённый ввод (ревью 2026-10-06).
                managed = list(getattr(self, "_managed_pages", None) or [])
                for p in managed:
                    try:
                        if p.is_closed() or p is self.page:
                            continue      # рабочую вкладку не закрываем
                        p.close()
                    except Exception:
                        pass
                self._managed_pages = ([self.page]
                                      if self.page is not None else [])
                # Вкладку, на которой мы работаем, оставляем открытой: если она
                # наша (owner=collector) — её можно увести на about:blank, если
                # чужая — нельзя.
                if self.browser_owner == "collector" and self.page is not None:
                    try:
                        if not self.page.is_closed():
                            self.page.goto("about:blank", timeout=5000)
                    except Exception:
                        pass
                elif self.page is not None and self.page in managed:
                    # external: рабочая вкладка — НАША managed-страница
                    # (ctx.new_page при подключении): закрываем её, чтобы не
                    # копить свои вкладки в чужом Chrome (ревью 2026-10-15).
                    # Чужая вкладка в managed попасть не может.
                    try:
                        if not self.page.is_closed():
                            self.page.close()
                    except Exception:
                        pass
                else:
                    log.debug("сессия закрыта: вкладки коллектора закрыты, "
                              "чужие не тронуты")
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
    """Проверка, что CDP-браузер жив (без полной сессии).

    Таймаут 0.5 с по той же причине, что и в browser_tabs_count: отказ
    соединения к localhost на машине разработки занимает ~2 с (замер
    2026-10-02), а живой CDP отвечает на /json/version за миллисекунды.
    """
    import urllib.request

    try:
        with urllib.request.urlopen(cdp_url + "/json/version", timeout=0.5) as r:
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

    Таймаут 0.5 с, а не 3: это ТЕЛЕМЕТРИЯ, и она зовётся на каждом
    recovery-событии (`search._recovery_meta`). Замер 2026-10-02: отказ
    соединения к localhost на этой машине занимает 2.05 с (обычно мгновенно),
    поэтому старые 3 с превращали каждый снимок в 2 с ожидания — в юнит-тестах
    это был 81% времени набора (15 вызовов = 30.7 с из 38 с профиля), а в бою
    платило 2 с за событие при мёртвом браузере. Живой CDP отвечает на
    /json/list за миллисекунды, поэтому 0.5 с с запасом.
    """
    import urllib.request

    try:
        with urllib.request.urlopen(cdp_url + "/json/list", timeout=0.5) as r:
            data = json.loads(r.read().decode())
            if not isinstance(data, list):
                return None
            return sum(1 for t in data if t.get("type") == "page")
    except Exception:
        return None
