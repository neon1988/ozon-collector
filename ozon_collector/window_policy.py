"""Политика окон браузеров коллектора: окно всегда свёрнуто, фокус не крадётся.

Зачем модуль. Живой замер 2026-10-06 (Win32-монитор, EnumWindows каждые 20 мс):

1. Chrome крадёт фокус на старте даже при SW_HIDE: пустой запуск
   (CreateProcess + STARTF_USESHOWWINDOW/SW_HIDE, окно никто не показывает)
   дал `GetForegroundWindow() == chrome` через 0.3-0.4 с при `IsWindowVisible ==
   False`. То есть «окно спрятано» не значит «фокус у пользователя».
2. Окно браузера коллектора само выходит из minimized в normal и забирает фокус
   примерно через 30-40 с после старта: воспроизведено дважды (t=39.31 и
   t=43.63 от старта), окно жило ~1.4 с в normal и снова уходило в minimized.
   Воспроизводится и без кода коллектора (только scripts/launch_chrome.ps1),
   и по отдельности не лечится ни SW_HIDE, ни ShowWindow(SW_SHOWMINNOACTIVE),
   ни SetTopmost, ни «чистый» профиль, ни один браузер вместо трёх — источник
   внутри Chromium/Windows, а не в одной строке нашего кода.
3. Лаунчер возвращает фокус сразу после старта (Restore-Focus), но окно может
   уйти в foreground и через минуту, когда лаунчер давно отработал.

Поэтому лечение симптома, а не «одной строки»: фоновый сторож, который держит
инвариант «окно браузера коллектора скрыто, фокус принадлежит пользователю».
Источник событий логируется, чтобы при следующем живом прогоне было видно, кто
именно показывает окно.

Обновление 2026-10-16 (мандат владельца: окна «вылезают» сами — их видно):
теперь окна коллектора рождаются скрытыми (SW_HIDE от CreateProcess), кадры
на скрытом окне идут, а всплытие — это Chrome/Windows, показавшие окно через
30-40 с после старта. Сторож снова ВКЛЮЧЁН ПО УМОЛЧАНИЮ (выключить:
`OZON_WINDOW_GUARD=0`). Исключение из инварианта — флаг
`data/window_visible.flag`: его ставит трей, когда пользователь сам развернул
окна сбора («Развернуть окна сбора»); пока флаг есть, сторож спит.

Обновление 2026-10-17 (живой репро `.tmp/repro_owner.py`, замер показов 10-мс
пробником): Chrome показывает окно САМ при создании вкладки через CDP
(`PUT /json/new` → окно vis=True уже через ~20 мс), поэтому тик 150 мс не
успевал — добавлен WinEvent-хук EVENT_OBJECT_SHOW (гашение за миллисекунды).
Identity-сверка переведена с PowerShell (0.5-1 с блокировки первого тика —
первая вспышка жила 0.73 с) на сокет-таблицу ядра (~1 мс: `port_owner_pids`).
Щель «CDP готов → сторож зарегистрирован» (поиск browser_pid и connect
занимают секунды на воркер) закрывает караул лаунчера
(`scripts/launch_chrome.ps1`, `OZON_LAUNCHER_DUTY_S`, дефолт 30 с).

Что guard НЕ делает (намеренно):
- не трогает окна чужих браузеров: фильтр по pid, зарегистрированным Session
  с `browser_owner == "collector"`; чужой Chrome пользователя не сворачивается;
- не трогает НИЧЕГО, пока пользователь развернул окна из трея (флаг-файл);
- не работает в режиме `--window-mode visible` (там окно должно быть видно);
- не трогает Firefox: у него своя политика (свёрнутое окно не рендерит lazy-блоки,
  см. cdp._apply_window_policy);
- не «восстанавливает» фокус, если он не у нашего браузера: вызов
  SetForegroundWindow на чужое окно сам умеет воровать фокус у того, кто
  сейчас печатает.
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Dict, Iterable, NamedTuple, Optional

from . import log as plog
from . import process as _proc


class _ProjectLogBridge(logging.Handler):
    """Дублирует записи сторожа в файл проектного лога (daemon-логи).

    Стандартный logging к файлу демона не подключён: события сторожа («было
    видимо», «убран из охраны», возврат фокуса) в логах не были видны, и
    жалобу «окна открываются» нечем было проверить (разбор 2026-10-16).
    События идут в файл (debug), предупреждения — и в консоль.
    """

    def emit(self, record: logging.LogRecord):
        try:
            if record.levelno >= logging.WARNING:
                plog.warning(record.getMessage())
            else:
                plog.debug(record.getMessage())
        except Exception:
            pass


log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)
log.addHandler(_ProjectLogBridge())

# Интервал опроса: окно в normal живёт у нас ~1.4 с (замер), 150 мс даёт
# ~10 кадров на событие и не грузит CPU (EnumWindows по всем окнам <1 мс).
POLL_S = 0.15

CHROME_WINDOW_CLASS_PREFIX = "Chrome_WidgetWin_"

# Порог «это настоящее окно, а не попап». Chromium сам открывает мелкие окна
# (инфобар, «сохранить пароль», диалог профиля): они и мелькают, и сворачивать
# их нельзя — это ломает их жизненный цикл. Главное окно браузера в проекте
# 1440x900, берём с запасом.
MIN_GUARD_W = 400
MIN_GUARD_H = 300

# A hidden window is parked beyond the VIRTUAL desktop edge (all monitors):
# Chrome shows its window by itself (new tab, start, puzzle), and the WinEvent
# hook suppresses it only after milliseconds — long enough for an empty grey
# rectangle to flash (live measurement 2026-10-18: show->hide 30 ms, the owner
# saw it at collection start). A window beyond the screen edge is invisible even
# then; the CDP model follows the position (Browser.getWindowBounds reports the
# same left/top), screenshots and visibilityState=visible keep working — proven
# live. SWP: NOSIZE|NOZORDER|NOACTIVATE.
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
PARK_MARGIN = 100
ONSCREEN_MARGIN = 100
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010

# Пауза между попытками вернуть фокус. Нужна потому, что «лечение» фокуса
# опирается на синтетический Alt: если не помогло, повтор каждые 150 мс —
# это уже помеха пользователю, а не помощь.
FOCUS_RETRY_S = 3.0

# Как часто сверять, что охраняемый pid всё ещё держит наш CDP-порт. Живость
# проверяется каждый тик (дёшево). Сверка с 2026-10-17 тоже дешёвая
# (сокет-таблица ядра вместо PowerShell — замер: ~1 мс против ~0.5-1 с), но
# каденция остаётся редкой: pid-реюзы редки, а суетиться и спамить лог нечего.
IDENTITY_RECHECK_S = 15.0

# Ранняя регистрация (watch_launcher_pid) держит pid без сверки порта дольше
# обычного троттлинга: Chromium занимает CDP-порт через секунды после
# CreateProcess (замер: 7-9 с; шторм из 8 браузеров — дольше), а сверка «не
# держит порт» до привязки выбросила бы ЖИВОЙ pid, оставив щель launch→connect
# без охраны (находка ревью 2026-10-18).
EARLY_WATCH_GRACE_S = 45.0

# WinEvent-хук мгновенного гашения показа (замер 2026-10-17): Chrome ПОКАЗЫВАЕТ
# окно сам при создании вкладки через CDP (`PUT /json/new`, Target.createTarget):
# окно vis=True уже через ~20 мс после запроса. Тик сторожа (150 мс) на такое
# опаздывает, и окно успевает мигнуть; EVENT_OBJECT_SHOW приходит в момент
# показа — прячем, не дожидаясь тика.
EVENT_OBJECT_SHOW = 0x8002
OBJID_WINDOW = 0
WINEVENT_OUTOFCONTEXT = 0x0000
WM_QUIT = 0x0012

# «Пользователь смотрит окна сбора»: флаг-файл ставит/снимает трей-пульт
# («Развернуть окна сбора» / «Свернуть окна сбора»), а при старте сбора его
# снимает key_daemon (clear_user_visible — на случай трея прошлого запуска,
# убитого жёстко). Пока файл есть, сторож НИЧЕГО не трогает: ни окна, ни
# фокус — пользователь осознанно развернул окна и работает с ними.
# Раньше это состояние угадывалось по свежести реального ввода
# (INPUT_FRESH_S/HOLD_S); от эвристики отказались 2026-10-16: всплывающий
# Chrome крадёт фокус, и «пользователь печатает» ошибочно принималось за
# «пользователь открыл окно» — окно оставалось на экране (ровно то, на что
# жалуется владелец).
USER_VISIBLE_FLAG = (Path(__file__).resolve().parent.parent
                     / "data" / "window_visible.flag")


def _flag_text() -> str:
    try:
        return USER_VISIBLE_FLAG.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def user_visible() -> bool:
    """Пользователь развернул окна сбора из трея (флаг активен).

    Explicit "0" is the tray's "hidden" verdict; it must beat
    OZON_KEEP_WINDOW after start (owner 2026-10-19: the console sets the
    initial mode, the tray is authoritative afterwards). Absent = no opinion.
    """
    try:
        if not USER_VISIBLE_FLAG.exists():
            return False
        text = _flag_text()
        # Empty file = interrupted write / manual edit: same as absent, the
        # env decides (consistent with tray and launcher; review 2026-10-19).
        return bool(text) and text != "0"
    except OSError:            # сбой ФС — не повод падать и не повод молчать
        return False


def user_hidden() -> bool:
    """Трей явно свернул окна: флаг содержит "0" (сильнее env-дефолта)."""
    try:
        return USER_VISIBLE_FLAG.exists() and _flag_text() == "0"
    except OSError:
        return False


def clear_user_visible() -> None:
    """Снять залипший флаг (вызов при старте сбора, из key_daemon)."""
    try:
        USER_VISIBLE_FLAG.unlink(missing_ok=True)
    except OSError as e:
        log.debug(f"window_policy: clear_user_visible err "
                  f"{type(e).__name__}: {e}")


class WindowRow(NamedTuple):
    hwnd: int
    pid: int
    visible: bool
    iconic: bool
    title: str
    width: int = 0
    height: int = 0

    @property
    def is_main_window(self) -> bool:
        return (self.width >= MIN_GUARD_W and self.height >= MIN_GUARD_H)


_USER32 = None
_USER32_LOCK = threading.Lock()


def _user32():
    """СВОЙ дескриптор user32, а не `ctypes.windll.user32` (мемоизирован).

    `ctypes.windll.*` возвращает глобальный кэш функций: объявление нами
    argtypes меняет их для ВСЕГО процесса. Из-за этого наша настройка типов
    ломала чужой колбэк: `cdp._physical_window_rect` объявляет свой
    WINFUNCTYPE(c_bool, HWND, LPARAM), а мы ставили для EnumWindows
    `[WINFUNCTYPE(BOOL, HWND, LPARAM), LONG]` → ArgumentError, cdp глотал его в
    log.debug и возвращал None → `_normalize_window_geometry` и
    `_assert_geometry_ok` молча выходили, то есть инвариант «физическое окно ==
    CDP-модель» отключался (живой замер ревью 2026-10-06: после первого
    `foreground()` геометрия переставала определяться).
    Отдельный WinDLL даёт нам собственное пространство имён имён функций.
    Мемоизация обязательна: `_setup_api` ставит argtypes ОДНОМУ инстансу, а
    прежний возврат нового инстанса на каждый вызов отдавал функции БЕЗ типов
    — HWND обрезался до 32 бит, и геометрия/фокус молча ломались со второго
    вызова (ревью 2026-10-15).
    """
    global _USER32
    if _USER32 is not None:
        return _USER32
    if os.name != "nt":
        return None
    with _USER32_LOCK:
        if _USER32 is None:
            try:
                _USER32 = ctypes.WinDLL("user32", use_last_error=True)
            except Exception:  # pragma: no cover - нет Windows
                return None
    return _USER32


_API_READY = False


def _setup_api(user32) -> None:
    """Объявить типы аргументов ИМЕННО нашего дескриптора user32.

    Без этого ctypes передаёт HWND/pid как 32-битный int, и на x64 дескриптор
    окна обрезается: живой замер 2026-10-06 дал restore_focus()=False при
    заведомо валидном окне (и верный hwnd в last_fg). Типы обязательны.
    """
    global _API_READY
    if _API_READY:
        return
    L, U, I = ctypes.c_long, ctypes.c_uint, ctypes.c_int
    P = ctypes.POINTER
    HWND = wintypes.HWND
    user32.IsWindowVisible.argtypes = [HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [HWND, I]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = HWND
    user32.SetForegroundWindow.argtypes = [HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetWindowLongW.argtypes = [HWND, I]
    user32.GetWindowLongW.restype = L
    user32.SetWindowLongW.argtypes = [HWND, I, L]
    user32.SetWindowLongW.restype = L
    user32.SetWindowPos.argtypes = [HWND, HWND, I, I, I, I, U]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [HWND, P(U)]
    user32.GetWindowThreadProcessId.restype = U
    user32.AttachThreadInput.argtypes = [U, U, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, U,
                                   ctypes.c_ulong]
    user32.keybd_event.restype = None
    user32.GetWindowTextLengthW.argtypes = [HWND]
    user32.GetWindowTextLengthW.restype = I
    user32.GetWindowTextW.argtypes = [HWND, ctypes.c_wchar_p, I]
    user32.GetWindowTextW.restype = I
    user32.GetClassNameW.argtypes = [HWND, ctypes.c_wchar_p, I]
    user32.GetClassNameW.restype = I
    user32.GetWindowRect.argtypes = [HWND, P(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetSystemMetrics.argtypes = [I]
    user32.GetSystemMetrics.restype = I
    user32.EnumWindows.argtypes = [_ENUMPROC, L]
    user32.EnumWindows.restype = wintypes.BOOL
    # WinEvent-хук показа окон (мгновенное гашение) + насос сообщений потока.
    if _WINEVENTPROC is not None:
        user32.SetWinEventHook.argtypes = [
            ctypes.c_ulong, ctypes.c_ulong, wintypes.HWND, _WINEVENTPROC,
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        user32.SetWinEventHook.restype = wintypes.HANDLE
        # HANDLE/хук без явного restype обрезается до 32 бит на x64 (находка
        # ревью 2026-10-17): UnhookWinEvent тихо фейлился — хук тёк до конца
        # процесса и держал колбэк сентинела.
        user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        user32.UnhookWinEvent.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                       wintypes.HWND, ctypes.c_uint,
                                       ctypes.c_uint]
        user32.GetMessageW.restype = ctypes.c_int
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                        wintypes.HWND, ctypes.c_uint,
                                        ctypes.c_uint, ctypes.c_uint]
        user32.PeekMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = ctypes.c_size_t
        user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostThreadMessageW.restype = wintypes.BOOL
    k32 = _kernel32()
    if k32 is not None:
        k32.GetCurrentThreadId.argtypes = []
        k32.GetCurrentThreadId.restype = U
    _API_READY = True


_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                               wintypes.LPARAM) if os.name == "nt" else None

# WinEventProc: (HWINEVENTHOOK, DWORD event, HWND, LONG idObject, LONG idChild,
# DWORD idEventThread, DWORD dwmsEventTime).
_WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, ctypes.c_void_p, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
    wintypes.LONG, wintypes.DWORD, wintypes.DWORD) if os.name == "nt" else None


def _window_text(user32, hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _window_class(user32, hwnd) -> str:
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


def _window_pid(user32, hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def list_chrome_windows() -> list:
    """Окна Chromium (Chrome_WidgetWin_*) с состоянием видимости.

    Отдельная функция (а не логика внутри тика) — чтобы тесты подменяли её и
    проверяли решение, не трогая настоящий Windows.
    """
    user32 = _user32()
    if user32 is None or _ENUMPROC is None:
        return []
    _setup_api(user32)
    rows = []

    def cb(hwnd, _lparam):
        if not _window_class(user32, hwnd).startswith(
                CHROME_WINDOW_CLASS_PREFIX):
            return True
        rect = wintypes.RECT()
        width = height = 0
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            width = rect.right - rect.left
            height = rect.bottom - rect.top
        rows.append(WindowRow(
            hwnd=int(hwnd),
            pid=_window_pid(user32, hwnd),
            visible=bool(user32.IsWindowVisible(hwnd)),
            iconic=bool(user32.IsIconic(hwnd)),
            title=_window_text(user32, hwnd)[:60],
            width=width,
            height=height,
        ))
        return True

    user32.EnumWindows(_ENUMPROC(cb), 0)
    return rows


def foreground() -> Optional[WindowRow]:
    """Окно в фокусе или None (нет user32 / пустой десктоп)."""
    user32 = _user32()
    if user32 is None:
        return None
    _setup_api(user32)
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    width = height = 0
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        width = rect.right - rect.left
        height = rect.bottom - rect.top
    return WindowRow(hwnd=int(hwnd), pid=_window_pid(user32, hwnd),
                     visible=bool(user32.IsWindowVisible(hwnd)),
                     iconic=bool(user32.IsIconic(hwnd)),
                     title=_window_text(user32, hwnd)[:60],
                     width=width, height=height)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(d1, d2, d3, rest):
    return _GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*rest))


# ITaskbarList: живая проверка 2026-10-16 (Windows 11, explorerframe.dll) —
# работает CLSID {56FDF344…} + IID {56FDF342…}; обратная пара из старых
# SDK-примеров даёт REGDB_E_CLASSNOTREG / E_NOINTERFACE. Пробуем обе.
_TASKBAR_LIST_PAIRS = (
    (_guid(0x56FDF344, 0xFD6D, 0x11D0,
           (0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90)),
     _guid(0x56FDF342, 0xFD6D, 0x11D0,
           (0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90))),
    (_guid(0x56FDF342, 0xFD6D, 0x11D0,
           (0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90)),
     _guid(0x56FDF344, 0xFD6D, 0x11D0,
           (0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90))),
)


def delete_taskbar_tab(hwnd: int) -> bool:
    """ITaskbarList::DeleteTab(hwnd) — снять кнопку окна с панели задач.

    Смена WS_EX_TOOLWINDOW уже созданную кнопку НЕ убирает (живая находка
    2026-10-16: стиль на всех окнах, кнопки остались), поэтому после каждого
    показа окна кнопку снимаем явно. comtypes/pywin32 в зависимостях проекта
    нет — COM вручную на ctypes: CoCreateInstance(CLSID_TaskbarList) и вызовы
    через vtable (IUnknown: 0..2, ITaskbarList: HrInit=3, DeleteTab=5).
    """
    if os.name != "nt":
        return False
    try:
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoCreateInstance.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        ole32.CoCreateInstance.restype = ctypes.c_long
        # STA; RPC_E_CHANGED_MODE (поток уже инициализирован иначе) не мешает.
        ole32.CoInitializeEx(None, 2)
        taskbar = ctypes.c_void_p()
        for clsid, iid in _TASKBAR_LIST_PAIRS:
            taskbar = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(
                ctypes.byref(clsid), None, 1, ctypes.byref(iid),
                ctypes.byref(taskbar))
            if hr == 0 and taskbar:
                break
        if not taskbar:
            return False
        vtable = ctypes.cast(
            taskbar, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        hr_init = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[3])
        delete_tab = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(vtable[5])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
        try:
            hr_init(taskbar)
            rc = delete_tab(taskbar, ctypes.c_void_p(int(hwnd)))
        finally:
            release(taskbar)
        return rc == 0
    except Exception as e:
        log.debug(f"window_policy: delete_taskbar_tab err {type(e).__name__}: {e}")
        return False


def _virtual_screen(user32):
    """(x, y, w, h) of the virtual desktop (all monitors), or None."""
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def _window_rect(user32, hwnd):
    """(left, top, width, height) of a window, or None."""
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right - rect.left,
            rect.bottom - rect.top)


def _on_screen(rect, screen) -> bool:
    """Whether the window rectangle intersects the virtual desktop."""
    x, y, w, h = rect
    sx, sy, sw, sh = screen
    return not (x + w <= sx or x >= sx + sw or y + h <= sy or y >= sy + sh)


def park_offscreen(hwnd: int) -> bool:
    """Move a hidden window beyond the virtual desktop edge.

    Chrome SHOWS its window by itself (new tab, start, puzzle); the WinEvent
    hook suppresses the show within milliseconds, but an empty grey rectangle
    still flashes (live measurement 2026-10-18: show->hide 30 ms — what the
    owner saw at collection start). Beyond the screen edge it is invisible even
    then. Size and z-order are preserved (SWP_NOSIZE|NOZORDER|NOACTIVATE), the
    CDP model follows the position — geometry and screenshots are unaffected
    (live stand). True when the window was on screen and got parked.
    """
    user32 = _user32()
    if user32 is None:
        return False
    _setup_api(user32)
    try:
        screen = _virtual_screen(user32)
        rect = _window_rect(user32, hwnd)
        if screen is None or rect is None or not _on_screen(rect, screen):
            return False
        _x, _y, w, _h = rect
        sx, sy, _sw, _sh = screen
        user32.SetWindowPos(hwnd, 0, sx - w - PARK_MARGIN,
                            sy + ONSCREEN_MARGIN, 0, 0,
                            _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE)
        return True
    except Exception as e:
        log.debug(f"window_policy: park_offscreen err {type(e).__name__}: {e}")
        return False


def bring_onscreen(hwnd: int) -> bool:
    """Bring a parked window back into the visible area (tray / keep_window).

    A window parked beyond the screen edge stays invisible even when Chrome
    shows it, so every legitimate show must first return it to the desktop.
    True when the window was off screen and got moved back.
    """
    user32 = _user32()
    if user32 is None:
        return False
    _setup_api(user32)
    try:
        screen = _virtual_screen(user32)
        rect = _window_rect(user32, hwnd)
        if screen is None or rect is None or _on_screen(rect, screen):
            return False
        sx, sy, _sw, _sh = screen
        user32.SetWindowPos(hwnd, 0, sx + ONSCREEN_MARGIN,
                            sy + ONSCREEN_MARGIN, 0, 0,
                            _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE)
        return True
    except Exception as e:
        log.debug(f"window_policy: bring_onscreen err {type(e).__name__}: {e}")
        return False


def minimize_hwnd(hwnd: int) -> bool:
    """user32: WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW + SW_HIDE — как Session.

    Именно SW_HIDE, а не SW_MINIMIZE: свёрнутое TOOLWINDOW-окно Windows
    рисуется «мини-плиткой-призраком» над панелью задач и не отдаёт кадры
    (живой замер 2026-10-16); скрытое окно кадры отдаёт.
    """
    user32 = _user32()
    if user32 is None:
        return False
    _setup_api(user32)
    try:
        style = user32.GetWindowLongW(hwnd, -20)       # GWL_EXSTYLE
        # NOACTIVATE — не крадёт фокус; TOOLWINDOW — нет кнопки на панели задач
        user32.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x00000080)
        user32.ShowWindow(hwnd, 0)                    # SW_HIDE
        # Park AFTER hiding: Chrome may show the window at any moment, and
        # beyond the screen edge it is invisible even then.
        park_offscreen(int(hwnd))
        delete_taskbar_tab(int(hwnd))
        return True
    except Exception as e:
        log.debug(f"window_policy: minimize err {type(e).__name__}: {e}")
        return False


def show_hwnd(hwnd: int) -> bool:
    """user32: SW_RESTORE + TOPMOST — the single show canon (same as Session).

    A window hidden with SW_HIDE at start is shown only by SW_RESTORE +
    SetWindowPos(HWND_TOPMOST, SWP_SHOWWINDOW). NOACTIVATE keeps the user
    focus; TOOLWINDOW stays — the taskbar stays clean. The taskbar button is
    removed explicitly: a style change does not remove an already created
    button (finding 2026-10-16). Same contract as the show path in
    launch_chrome.ps1 and collector_tray.ps1 (audit 2026-10-18, D2).
    """
    user32 = _user32()
    if user32 is None:
        return False
    _setup_api(user32)
    try:
        style = user32.GetWindowLongW(hwnd, -20)       # GWL_EXSTYLE
        user32.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x00000080)
        # The window may have been parked beyond the screen edge when hidden —
        # return it to the desktop BEFORE showing, or the user sees nothing.
        bring_onscreen(int(hwnd))
        user32.ShowWindow(hwnd, 9)                    # SW_RESTORE
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0,     # HWND_TOPMOST
                            0x0002 | 0x0001 | 0x0010 | 0x0040)
        # SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE|SWP_SHOWWINDOW
        delete_taskbar_tab(int(hwnd))
        return True
    except Exception as e:
        log.debug(f"window_policy: show_hwnd err {type(e).__name__}: {e}")
        return False


def window_exists(hwnd: int) -> bool:
    user32 = _user32()
    if user32 is None:
        return False
    _setup_api(user32)
    return bool(user32.IsWindow(hwnd))


def restore_focus(hwnd: int) -> bool:
    """Вернуть фокус окну пользователя.

    Порядок попыток (все три проверены вживую 2026-10-06, когда фокус сидел на
    окне Chromium):

    1. AttachThreadInput к потоку foreground + SetForegroundWindow — отказ;
    2. AttachThreadInput к потоку целевого окна — отказ;
    3. SwitchToThisWindow — отказ.

    Windows не даёт менять foreground процессу, который сам не foreground и не
    владеет последним вводом: AttachThreadInput возвращает 0. Работает только
    «ALT-trick» — мнимый Alt (keybd_event) даёт право на SetForegroundWindow.
    Побочный эффект известен и логируется: синтетический Alt у пользователя.
    """
    user32 = _user32()
    k32 = _kernel32()
    if user32 is None or k32 is None or not hwnd:
        return False
    _setup_api(user32)
    try:
        fg = user32.GetForegroundWindow()
        if fg and int(fg) == int(hwnd):
            return True
        self_thread = k32.GetCurrentThreadId()
        fg_thread = wintypes.DWORD()
        user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_thread))
        attached = bool(user32.AttachThreadInput(self_thread,
                                                  fg_thread.value, True))
        try:
            ok = bool(user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(self_thread, fg_thread.value, False)
        if ok:
            return True
        # ALT-trick: система разрешает SetForegroundWindow после обработки
        # ввода. Мнимый Alt вверх-вниз, без залипания.
        user32.keybd_event(0x12, 0, 0, 0)                 # VK_MENU down
        user32.keybd_event(0x12, 0, 0x0002, 0)           # KEYEVENTF_KEYUP
        ok = bool(user32.SetForegroundWindow(hwnd))
        log.info(f"window_policy: фокус возвращён через ALT-trick на hwnd="
                 f"{hwnd} (успех={ok}, синтетический Alt)")
        return ok
    except Exception as e:
        log.debug(f"window_policy: restore_focus err {type(e).__name__}: {e}")
        return False


def _kernel32():
    if os.name != "nt":
        return None
    try:
        return ctypes.windll.kernel32
    except Exception:      # pragma: no cover
        return None


def _pid_alive(pid: int) -> bool:
    """Process liveness — shared process.pid_alive implementation.

    This used to be a third OpenProcess+GetExitCodeProcess duplicate without a
    guard for unrealistic PIDs; now one code path project-wide
    (audit 2026-10-18, D4).
    """
    return _proc.pid_alive(pid)


def port_owner_pids(port: int):
    """PIDs, держащие LISTEN-сокет порта (сокет-таблица ядра, без WMI).

    Раньше «держит ли pid наш порт» выяснялось через PowerShell + WMI —
    живой замер 2026-10-17: один вызов ~0.5-1 с, и всё это время показанное
    Chrome окно оставалось не погашенным (первый тик сторожа блокировался на
    этой проверке: вспышка 0.73 с в репро). GetExtendedTcpTable отвечает за
    ~1 мс. Возвращает None, если таблицу получить не удалось (вызывающий
    решает сам — у _pid_owns_port есть fallback на старый путь).
    """
    if os.name != "nt":
        return None
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
        AF_INET = 2
        TCP_TABLE_OWNER_PID_LISTENER = 3
        ERROR_INSUFFICIENT_BUFFER = 122
        iphlpapi.GetExtendedTcpTable.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), wintypes.BOOL,
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        iphlpapi.GetExtendedTcpTable.restype = ctypes.c_ulong
        # Таблица TCP между запросом размера и заполнением может вырасти —
        # ERROR_INSUFFICIENT_BUFFER на втором вызове ретраим (до 3 попыток):
        # возврат None увёл бы identity на медленный PowerShell (0.5-1 с на
        # вызов, замер 2026-10-17), а это и есть окно вспышки.
        buf = None
        for _attempt in range(3):
            size = ctypes.c_ulong(0)
            rc = iphlpapi.GetExtendedTcpTable(
                None, ctypes.byref(size), False, AF_INET,
                TCP_TABLE_OWNER_PID_LISTENER, 0)
            if rc != ERROR_INSUFFICIENT_BUFFER or not size.value:
                return None
            buf = ctypes.create_string_buffer(size.value)
            rc = iphlpapi.GetExtendedTcpTable(
                buf, ctypes.byref(size), False, AF_INET,
                TCP_TABLE_OWNER_PID_LISTENER, 0)
            if rc == 0:
                break
            if rc != ERROR_INSUFFICIENT_BUFFER:
                return None
        else:
            return None
        count = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong)).contents.value
        row = ctypes.c_ulong * 6          # MIB_TCPROW_OWNER_PID
        rows = ctypes.cast(ctypes.byref(buf, 4), ctypes.POINTER(row))
        pids = set()
        for i in range(count):
            _state, _la, lport, _ra, _rp, pid = rows[i]
            # dwLocalPort — в сетевом порядке байт в младшем слове
            if (((lport & 0xFF) << 8) | ((lport >> 8) & 0xFF)) == int(port):
                pids.add(int(pid))
        return pids
    except Exception as e:
        log.debug(f"window_policy: port_owner_pids err {type(e).__name__}: {e}")
        return None


def process_image_basename(pid: int):
    """Имя исполняемого файла процесса (QueryFullProcessImageNameW, без WMI)."""
    k32 = _kernel32()
    if k32 is None:
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        k32.OpenProcess.argtypes = [ctypes.c_uint, wintypes.BOOL,
                                    wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, ctypes.c_ulong, ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulong)]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                            int(pid))
        if not h:
            return None
        try:
            size = ctypes.c_ulong(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if not k32.QueryFullProcessImageNameW(h, 0, buf,
                                                  ctypes.byref(size)):
                return None
            return buf.value.rsplit("\\", 1)[-1]
        finally:
            k32.CloseHandle(h)
    except Exception as e:
        log.debug(f"window_policy: process_image_basename err "
                  f"{type(e).__name__}: {e}")
        return None


def _pid_owns_port(pid: int, port) -> Optional[bool]:
    """Ровно ли этот PID держит CDP-порт (identity-сверка).

    Требуется правилом проекта: трогать можно только процесс, который держит
    наш порт. Быстрый путь — сокет-таблица ядра (port_owner_pids). Fallback —
    командная строка процесса через PowerShell (историческая проверка),
    только если таблицу получить не удалось.
    None = the check could not be performed at all ("identity unknown");
    callers must not treat it as "does not own the port" (audit 2026-10-19:
    a failing check evicted a live pid from the watch list).
    """
    if not port:
        return False
    try:
        owners = port_owner_pids(int(port))
    except Exception:
        owners = None
    if owners is not None:
        return int(pid) in owners
    return _pid_owns_port_via_cmdline(pid, port)


def _pid_owns_port_via_cmdline(pid: int, port) -> Optional[bool]:
    """Fallback identity: cmdline содержит --remote-debugging-port=port.

    Returns None when the command line could not be retrieved (query failed
    or the process vanished mid-check) — unknown, not "does not own the port".
    """
    import subprocess
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter "
             f"\"ProcessId={int(pid)}\").CommandLine"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW",
                                   0x08000000) if os.name == "nt" else 0))
    except Exception as e:
        log.debug(f"window_policy: cmdline не прочитан: {type(e).__name__}: {e}")
        return None
    cmdline = (out.stdout or "").strip()
    if out.returncode != 0 or not cmdline:
        # Query failed / process not found: identity неизвестна. Живость
        # и так проверяется каждым тиком (_pid_alive).
        return None
    return f"--remote-debugging-port={port}" in cmdline


def should_hide_show_event(pid: int, class_name: str, width: int, height: int,
                           watched: Dict[int, str], uv: bool) -> bool:
    """Прятать ли окно, показавшееся по EVENT_OBJECT_SHOW (чистое решение).

    Тот же фильтр, что у тика: наш pid, класс Chromium, главное окно (мелкие
    попапы/инфобары не трогаем), пользователь не разворачивал окна из трея.
    Отдельная функция — чтобы тест проверял решение, не трогая живой Windows.
    """
    return (
        not uv
        and pid in watched
        and class_name.startswith(CHROME_WINDOW_CLASS_PREFIX)
        and width >= MIN_GUARD_W
        and height >= MIN_GUARD_H
    )


class WindowGuard:
    """Сторож окон браузеров коллектора: один поток на процесс.

    Реестр pid → сторож держит их окна свёрнутыми и возвращает фокус, если он
    ушёл нашему браузеру. Наружу смотрит только через модульные функции
    (list_chrome_windows/foreground/minimize_hwnd/restore_focus), поэтому
    проверяется тестами без живого Windows.
    """

    def __init__(self, poll_s: float = POLL_S):
        self.poll_s = poll_s
        self._pids: Dict[int, str] = {}          # pid -> порт (для лога)
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()   # сериализует start()/stop()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_fg: Optional[int] = None       # последнее «пользовательское» окно
        self._last_focus_try: float = 0.0
        self._uv_logged = False                    # «сторож спит» в лог один раз
        self._identity_checked: Dict[int, float] = {}  # pid -> когда сверяли порт
        self._grace_until: Dict[int, float] = {}       # pid -> конец grace-окна (watch)
        self._fixed = 0                           # счётчик для телеметрии
        self._focus_fixed = 0
        self._hook_thread: Optional[threading.Thread] = None
        self._hook_tid: Optional[int] = None      # поток с насосом сообщений
        self._hook_quit_sent = False              # stop() отправил WM_QUIT
        self._hook_proc = (_WINEVENTPROC(self._on_win_event)
                           if _WINEVENTPROC is not None else None)

    # ---- реестр -----------------------------------------------------------
    def watch(self, pid: Optional[int], port: object = None,
              autostart: bool = True, identity_grace: bool = False) -> None:
        """Начать охранять окна браузера pid (идемпотентно).

        autostart=False — только зарегистрировать, поток не поднимать:
        используется в тестах решения (tick), чтобы фоновой тик не сработал
        раньше проверки.
        identity_grace=True — пометить запись свежей: первая плановая сверка
        «держи ли pid наш CDP-порт» отложится до EARLY_WATCH_GRACE_S от
        регистрации. Нужно ранней регистрации (watch_launcher_pid): Chromium
        занимает порт через секунды после CreateProcess (замер: 7-9 с; шторм
        из 8 браузеров — дольше), а окно может показаться раньше — без grace
        первый же тик выбрасывал живой pid («не держит порт») и показ никто не
        гасил (замер 2026-10-18, .tmp/gap_harness.py: окно висело 4152 мс).
        """
        if not pid or os.name != "nt":
            return
        with self._lock:
            self._pids[int(pid)] = str(port or "?")
            if identity_grace:
                # Поле значит «когда сверяли»; сверка идёт при
                # now - last >= IDENTITY_RECHECK_S (см. _drop_dead_browsers).
                # Сдвинутая метка = первая сверка не раньше
                # EARLY_WATCH_GRACE_S от регистрации. Запись под локом:
                # тик сторожа читает метку из другого потока.
                self._identity_checked[int(pid)] = (
                    time.monotonic() + EARLY_WATCH_GRACE_S - IDENTITY_RECHECK_S)
                # Конец grace-окна: пока не наступил, тик сверяет ОБРАЗ
                # процесса каждый тик до первой порт-сверки. Между сдвинутой
                # меткой и порт-сверкой иначе нет identity вообще (дыра
                # t0+30..t0+45, PROBE2 ревью 2026-10-18).
                self._grace_until[int(pid)] = (
                    time.monotonic() + EARLY_WATCH_GRACE_S)
        if autostart:
            self.start()

    def forget(self, pid: Optional[int]) -> None:
        if not pid:
            return
        with self._lock:
            self._pids.pop(int(pid), None)
            self._identity_checked.pop(int(pid), None)
            self._grace_until.pop(int(pid), None)

    def watched_pids(self) -> Dict[int, str]:
        with self._lock:
            return dict(self._pids)

    # ---- поток ------------------------------------------------------------
    def start(self) -> None:
        # Lock-guarded + restartable: racing start() calls (watch() with
        # autostart from several worker threads) must not spawn duplicate
        # threads. The lifecycle lock serializes start() against stop() so a
        # restart cannot resurrect the pair mid-stop (audit 2026-10-19).
        with self._lifecycle_lock:
            # A stop() whose join timed out can leave live survivors holding
            # their handles; join them boundedly BEFORE clearing _stop so a
            # survivor is not overwritten by a fresh spawn — an unreachable
            # old hook thread would leak forever (review 2026-10-19, pass 3).
            # Gated on _stop: only a stop() can leave it set, so a redundant
            # start() of the running singleton stays instant — joining live
            # threads unconditionally costs 10 s on the worker connect path
            # (review 2026-10-19, pass 4).
            if self._stop.is_set():
                cur = threading.current_thread()
                for th in (self._hook_thread, self._thread):
                    if th is not None and th is not cur and th.is_alive():
                        th.join(timeout=5.0)
            with self._lock:
                self._stop.clear()
                # Each thread is spawned by its own liveness: a dead hook
                # next to a live guard must not block the restart, and a
                # live handle is never overwritten (review 2026-10-19, pass 3).
                spawned = False
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(target=self._loop,
                                                    name="ozon-window-guard",
                                                    daemon=True)
                    self._thread.start()
                    spawned = True
                if self._hook_thread is None \
                        or not self._hook_thread.is_alive() \
                        or self._hook_quit_sent:
                    # A hook that survived stop() (join timeout) already has
                    # WM_QUIT in its queue: it is not a working pump anymore
                    # (NIT review 2026-10-19). Spawn a fresh one.
                    self._hook_quit_sent = False
                    # Хук показа окон: мгновенное гашение (см. _hook_loop).
                    # Отдельный поток: SetWinEventHook требует насос сообщений,
                    # а тик должен идти своей каденцией.
                    self._hook_thread = threading.Thread(
                        target=self._hook_loop,
                        name="ozon-window-guard-hook",
                        daemon=True)
                    self._hook_thread.start()
                    spawned = True
        if spawned:
            log.debug("window_policy: сторож окон стартовал")

    def stop(self) -> None:
        # Restartable: start() re-clears _stop before spawning. State is read
        # under self._lock; the lifecycle lock serializes the whole stop()
        # (including the bounded joins) against start() so a concurrent
        # restart cannot resurrect _stop or race the thread handles
        # (review 2026-10-19, pass 2). Joining the current thread would raise,
        # so a call from a guard thread skips only its own join.
        with self._lifecycle_lock:
            with self._lock:
                self._stop.set()
                hook_tid = self._hook_tid
                thread = self._thread
                hook_thread = self._hook_thread
            if hook_tid:
                posted = False
                try:
                    user32 = _user32()
                    if user32 is not None:
                        _setup_api(user32)
                        posted = bool(user32.PostThreadMessageW(
                            hook_tid, WM_QUIT, 0, 0))
                except Exception:
                    posted = False
                if posted:
                    # Remember the quit: a hook that survives the join has a
                    # WM_QUIT in its queue and must not be reused (start()
                    # spawns a fresh pump).
                    with self._lock:
                        self._hook_quit_sent = True
            cur = threading.current_thread()
            for th in (hook_thread, thread):
                if th is not None and th is not cur and th.is_alive():
                    th.join(timeout=5.0)

    # ---- WinEvent-хук показа ----------------------------------------------
    def _hook_loop(self) -> None:
        """Поток EVENT_OBJECT_SHOW: показалось окно — прячем сразу.

        WINEVENT_OUTOFCONTEXT: система кладёт событие в очередь ЭТОГО потока,
        колбэк вызывается при прокачке сообщений. GetMessageW блокирующий, но
        WM_QUIT (из stop()) его будит. Хук поднимает только сокет-фильтр:
        EI-подписок нет, лишнего CPU не тратит.
        """
        user32 = _user32()
        k32 = _kernel32()
        if user32 is None or k32 is None or _WINEVENTPROC is None:
            return
        _setup_api(user32)
        own_tid = int(k32.GetCurrentThreadId())
        msg = wintypes.MSG()
        try:
            # Насос очереди нужен ДО PostThreadMessage из stop().
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            with self._lock:
                self._hook_tid = own_tid
            hook = user32.SetWinEventHook(
                EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, None, self._hook_proc,
                0, 0, WINEVENT_OUTOFCONTEXT)
            if not hook:
                log.debug("window_policy: WinEvent-хук не встал — показы "
                          "гасятся только тиками (150 мс)")
                return
            if self._stop.is_set():
                # stop() пришёл раньше публикации _hook_tid: WM_QUIT не был
                # отправлен (очереди ещё не существовало) — выходим сами, иначе
                # поток зависнет в GetMessageW навсегда (находка ревью
                # 2026-10-17: утечка потока в 27 из 30 циклов start/stop).
                user32.UnhookWinEvent(hook)
                return
            log.debug("window_policy: WinEvent-хук показа окон активен "
                      "(мгновенное гашение)")
            try:
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
            finally:
                user32.UnhookWinEvent(hook)
        except Exception as e:
            log.debug(f"window_policy: hook err {type(e).__name__}: {e}")
        finally:
            with self._lock:
                if self._hook_tid == own_tid:
                    self._hook_tid = None   # не затираем tid нового потока
                    self._hook_quit_sent = False

    def _on_win_event(self, _hook, event, hwnd, id_object, id_child,
                      _event_thread, _event_ms) -> None:
        """Колбэк показа окна: скрыть немедленно, не дожидаясь тика."""
        try:
            if (event != EVENT_OBJECT_SHOW or id_object != OBJID_WINDOW
                    or id_child or not hwnd):
                return
            watched = self.watched_pids()
            if not watched:
                return
            user32 = _user32()
            if user32 is None:
                return
            _setup_api(user32)
            pid = _window_pid(user32, hwnd)
            if pid not in watched:
                return
            uv = user_visible()
            class_name = _window_class(user32, hwnd)
            rect = wintypes.RECT()
            width = height = 0
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                width = rect.right - rect.left
                height = rect.bottom - rect.top
            if not should_hide_show_event(pid, class_name, width, height,
                                          watched, uv):
                return
            if minimize_hwnd(int(hwnd)):
                with self._lock:
                    self._fixed += 1
                log.info(f"window_policy: окно pid={pid} показалось "
                         f"({width}x{height}, {class_name}) — прячу немедленно "
                         f"(WinEvent)")
        except Exception as e:
            log.debug(f"window_policy: hook cb err {type(e).__name__}: {e}")

    @property
    def alive(self) -> bool:
        return self._thread is not None and not self._stop.is_set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:      # сторож не имеет права ронять сбор
                log.debug(f"window_policy: tick err {type(e).__name__}: {e}")
            self._stop.wait(self.poll_s)

    # ---- решение ----------------------------------------------------------
    def _drop_dead_browsers(self) -> list:
        """Убрать из реестра мёртвые pid и те, что больше не держат наш порт.

        Правило проекта: трогать можно только процесс с нашим CDP-портом. Без
        этой проверки упавший воркер-браузер оставляет pid в реестре, Windows
        переиспользует pid — и `tick()` сворачивает уже ЧУЖОЕ окно (ревью
        2026-10-06). Живость проверяется каждый тик (дёшево, без WMI), полная
        сверка «процесс держит наш порт» — раз в IDENTITY_RECHECK_S (дёшево:
        сокет-таблица ядра, PowerShell — только fallback).
        В grace-окне (ранняя регистрация, порт ещё не привязан) каждый тик
        сверяется образ процесса (_grace_until): случай «Chrome умер,
        pid-реюз на чужой процесс» — иначе чужие окна сворачивались бы до 45 с
        (находки ревью 2026-10-18, раунды 2-3).
        """
        now = time.monotonic()
        stale = []
        for pid, port in self.watched_pids().items():
            if not _pid_alive(pid):
                stale.append((pid, "процесс мёртв"))
                continue
            with self._lock:
                last = self._identity_checked.get(pid, 0.0)
                grace_until = self._grace_until.get(pid, 0.0)
            if now < grace_until:
                # grace: порт ещё не сверен — identity держит ОБРАЗ процесса
                # (QueryFullProcessImageNameW, ~мс) каждый тик, а не только
                # первые 30 с: иначе реюз pid на чужой процесс свернул бы
                # чужие окна в хвосте grace-окна (PROBE2 ревью 2026-10-18).
                image = process_image_basename(pid)
                if image is not None \
                        and image.lower() not in _CHROME_IMAGE_NAMES:
                    stale.append((pid, f"не Chrome ({image})"))
                    continue
            if now - last < IDENTITY_RECHECK_S:
                continue
            owns = _pid_owns_port(pid, port)
            if owns is None:
                # Unverifiable identity is not proof of innocence: run the
                # cheap image check even beyond the grace window, otherwise a
                # pid reused by a foreign non-Chrome process keeps its window
                # suppressed (review 2026-10-19, pass 3).
                image = process_image_basename(pid)
                if image is not None \
                        and image.lower() not in _CHROME_IMAGE_NAMES:
                    stale.append((pid, f"не Chrome ({image})"))
                    continue
                # Both checks failed, the fallback PS-probe is slow: keep pid
                # and grace, but back off ~2 s so a sick socket table / WMI
                # does not spawn a probe every tick. The stamp counts from
                # the END of the probe: the probe itself can burn up to 20 s,
                # and a start-of-tick stamp would re-probe every tick
                # (review 2026-10-19, pass 2 + pass 3).
                with self._lock:
                    self._identity_checked[pid] = (
                        time.monotonic() - IDENTITY_RECHECK_S + 2.0)
                log.debug(f"window_policy: порт {port} не проверен для "
                          f"pid={pid} — оставляю под охраной")
                continue
            with self._lock:
                self._identity_checked[pid] = now
                self._grace_until.pop(pid, None)   # порт-сверка состоялась
            if owns is False:
                stale.append((pid, f"не держит порт {port}"))
        for pid, why in stale:
            self.forget(pid)   # снимает pids, метку и grace под локом
            log.info(f"window_policy: pid={pid} убран из охраны ({why}) — "
                     f"окна этого процесса не трогаем")
        return [p for p, _ in stale]

    def _uv_suspended(self) -> bool:
        """True while the tray asks to show windows; logs the transition once."""
        if not user_visible():
            return False
        if not self._uv_logged:
            self._uv_logged = True
            log.info("window_policy: окна развёрнуты пользователем (трей) — "
                     "сторож спит, окна не трогаю")
        return True

    def tick(self) -> dict:
        """Одна итерация: вернуть инвариант «окно скрыто, фокус у пользователя».

        Единственное исключение — флаг user_visible(): трей развернул окна для
        пользователя, и сторож не трогает ни окна, ни фокус, пока флаг стоит.
        The flag is re-checked during the tick: the tray can flip it after the
        initial check, and one hide would fight the user's explicit choice.

        Возвращает счётчики (для тестов и телеметрии).
        """
        if self._uv_suspended():
            return {"unminimized": 0, "focus_restored": 0}
        if self._uv_logged:
            self._uv_logged = False
            log.info("window_policy: флаг трея снят — сторож снова следит "
                     "за окнами")
        # Реестр чистим ПЕРВЫМ делом: упавший браузер оставляет pid в реестре, а
        # Windows переиспользует pid — дальше по нему сворачивалось бы уже чужое
        # окно. Пересобираем pids ПОСЛЕ чистки, иначе подставной pid успеет
        # попасть в обработку этого же тика.
        self._drop_dead_browsers()
        pids = self.watched_pids()
        if not pids:
            return {"unminimized": 0, "focus_restored": 0}
        unminimized = 0
        fg = foreground()
        for row in list_chrome_windows():
            if self._uv_suspended():
                return {"unminimized": unminimized, "focus_restored": 0}
            if row.pid not in pids:
                continue                       # чужой браузер — не трогаем
            if not row.visible:
                continue                       # окно уже скрыто
            if not row.is_main_window:
                continue                       # попап/инфобар — не наше окно
            # Включая iconic: свёрнутое, но ВИДИМОЕ окно — «мини-плитка-призрак»
            # над панелью задач (замер 2026-10-16), её тоже прячем. Невидимое
            # окно (IsWindowVisible=False) не трогаем: лишние ShowWindow —
            # мерцание рендера.
            if minimize_hwnd(row.hwnd):
                unminimized += 1
                with self._lock:
                    self._fixed += 1
                ghost = " («мини-плитка-призрак»)" if row.iconic else ""
                log.info(
                    f"window_policy: окно браузера pid={row.pid} "
                    f"(порт {pids[row.pid]}) было видимо{ghost} "
                    f"({row.width}x{row.height}) — прячу; "
                    f"заголовок {row.title[:40]!r}")
        focus_restored = 0
        if self._uv_suspended():
            return {"unminimized": unminimized, "focus_restored": 0}
        if fg is not None:
            if fg.pid in pids:
                if self._last_fg and window_exists(self._last_fg):
                    # Не чаще FOCUS_RETRY_S: если возврат не помог, ALT-trick
                    # не должен «стрелять» каждые 150 мс — это уже шум в
                    # пользовательском вводе, а не лечение.
                    now = time.monotonic()
                    if now - self._last_focus_try >= FOCUS_RETRY_S:
                        self._last_focus_try = now
                        if restore_focus(self._last_fg):
                            focus_restored = 1
                            self._focus_fixed += 1
                            log.info(
                                f"window_policy: фокус был у браузера "
                                f"pid={fg.pid} — вернул окно пользователя")
            else:
                self._last_fg = fg.hwnd
        return {"unminimized": unminimized, "focus_restored": focus_restored}

    @property
    def counters(self) -> dict:
        return {"windows_unminimized": self._fixed,
                "focus_restored": self._focus_fixed}

    # ---- фокус ------------------------------------------------------------
    def note_user_window(self) -> Optional[int]:
        """Запомнить окно пользователя ДО запуска браузера.

        Нужно, потому что Chromium крадёт фокус на старте (замер: foreground ==
        chrome через 0.3-0.4 с, окно при этом скрыто). Если запоминать окно
        только по тикам, то к моменту кражи последнее «пользовательское» окно
        уже некуда запомнить: все тики видят чужой pid — наш, и last_fg остаётся
        None (воспроизведено на живом замере: last_fg=None → фокус не вернулся).
        """
        fg = foreground()
        if fg is None:
            return self._last_fg
        pids = self.watched_pids()
        if fg.pid not in pids:
            self._last_fg = fg.hwnd
        return self._last_fg


_GUARD: Optional[WindowGuard] = None
_GUARD_LOCK = threading.Lock()


def enabled_by_env() -> bool:
    """Включён ли сторож. ПО УМОЛЧАНИЮ ВКЛЮЧЁН (мандат владельца 2026-10-16).

    Окна сбора «вылезают» сами (Chrome показывает окно через 30-40 с после
    старта) и мешают работе — сторож обязан их прятать. Раньше (просьба
    «трогай только свои», 2026-10-06) он был выключен; теперь выключение
    только явное: `OZON_WINDOW_GUARD=0` (или off/false/no). Сторож трогает
    ТОЛЬКО окна собственных браузеров коллектора (реестр Session) и молчит,
    пока стоит флаг user_visible() (пользователь развернул окна из трея).
    """
    flag = (os.environ.get("OZON_WINDOW_GUARD") or "").strip().lower()
    return flag not in {"0", "off", "false", "no"}


def guard() -> Optional[WindowGuard]:
    """Синглтон сторожа на процесс (None, только если явно выключен env).

    9-17 Session не должны плодить потоки, но и выключенный сторож не должен
    поднимать фоновый поток впустую.
    """
    if not enabled_by_env():
        return None
    global _GUARD
    with _GUARD_LOCK:
        if _GUARD is None:
            _GUARD = WindowGuard()
        return _GUARD


# ---- сплошной свип: наши Chromium вне реестров (сторожа-слои дырявые) ------

STRAY_GRACE_S = 45.0
_stray_first_seen: Dict[int, float] = {}     # порт → monotonic первого «CDP молчит»


def _our_profile_chromium_stat() -> tuple:
    """(ряды наших Chromium, сколько chrome.exe вообще увидел перечислитель).

    Ряды: (pid, token, port|None, is_main). Матч НЕ по порту и НЕ по реестрам
    (они и теряют процессы): профиль из `--user-data-dir` строго под базой
    профилей и имя каталога начинается с 'ozon-profile'.

    raw-счётчик нужен сентинелу: `process._win_iter_chromium_processes`
    глотает сбой PowerShell молча (пустой результат при исключении/rc≠0) —
    «успешный» пустой скан без счётчика отравил бы alien-кэш нашему свежему
    pid (review-skeptic 2026-10-17). raw>0 = перечислитель видел хоть один
    chrome.exe.
    """
    from . import process as proc
    base = os.path.normcase(os.path.normpath(proc.profile_base_dir()))
    out = []
    raw = 0
    for pid, argv, _ppid in proc._iter_chromium_processes():
        raw += 1
        data_dir = None
        port = None
        is_main = True
        for a in argv:
            if a.startswith("--user-data-dir="):
                data_dir = a.split("=", 1)[1].strip("\"'")
            elif a.startswith("--remote-debugging-port="):
                try:
                    port = int(a.split("=", 1)[1])
                except ValueError:
                    port = None
            elif a.startswith("--type="):
                is_main = False
        if not data_dir:
            continue
        name = os.path.basename(data_dir.rstrip("/\\"))
        if not name.startswith(proc.PROFILE_TOKEN):
            continue
        path = os.path.normcase(os.path.normpath(data_dir))
        if not path.startswith(base + os.sep):
            continue
        out.append((pid, name, port, is_main))
    return out, raw


def _our_profile_chromium() -> list:
    """Ряды наших Chromium (см. `_our_profile_chromium_stat`)."""
    return _our_profile_chromium_stat()[0]


def sweep_stray_browsers(*, grace_s: float = STRAY_GRACE_S,
                         now: Optional[float] = None) -> dict:
    """Сплошной обход: спрятать видимые окна наших Chromium, убить зависшие.

    Зачем шире WindowGuard: окна и процессы коллектора выпадали из всех
    реестров (воркер умирал вместе с реестром; identity-сверка «не держит
    порт» ошибочно отбрасывала ЖИВОГО pid; старый cdp_url смотрел на старую
    базу портов). Свип не зависит ни от реестров Session, ни от портовых
    матчей — только профиль под profile_base_dir().

    Два прохода:
    1) любое ВИДИМОЕ главное окно любого нашего Chromium — minimize_hwnd
       (SW_HIDE); работает даже для окон процессов вне реестров;
    2) main-процесс с `--remote-debugging-port`, чей CDP молчит дольше
       grace_s — убивается вместе с группой процессов своего профиля
       (identity-проверка kill_records, исключение PID-reuse).

    В режиме visible (флаг трея) не трогаем ничего. Проход по окнам уважает
    OZON_WINDOW_GUARD=0; уборка зависших — всегда (это мусор, а не окна).
    """
    res = {"ours": 0, "visible": 0, "hidden": 0, "mains": 0, "killed": 0,
           "killed_pids": [], "skipped": ""}
    if os.name != "nt":
        res["skipped"] = "non-windows"
        return res
    if user_visible():
        res["skipped"] = "user_visible"
        return res
    from . import process as proc
    ours = _our_profile_chromium()
    res["ours"] = len(ours)
    our_pids = {pid for pid, _t, _p, _m in ours}
    if enabled_by_env():
        for row in list_chrome_windows():
            if not (row.pid in our_pids and row.visible
                    and row.is_main_window):
                continue
            res["visible"] += 1
            if minimize_hwnd(row.hwnd):
                res["hidden"] += 1
                log.info(f"свип: спрятал окно pid={row.pid} hwnd={row.hwnd} "
                         f"'{row.title}' {row.width}x{row.height}")
    if now is None:
        now = time.monotonic()
    alive_ports = set()
    for pid, token, port, is_main in ours:
        if not is_main or not port:
            continue
        alive_ports.add(port)
        res["mains"] += 1
        if proc.cdp_responds(f"http://127.0.0.1:{port}", timeout_s=1.0):
            _stray_first_seen.pop(port, None)
            continue
        t0 = _stray_first_seen.setdefault(port, now)
        age = now - t0
        if age < grace_s:
            continue
        group = [p for p, tk, _pt, _m in ours if tk == token]
        records = []
        for p in group:
            ident = proc.process_identity(p)
            if ident is not None:
                records.append(proc.ProcessRecord(
                    launch_id=f"sweep:{token}", identity=ident))
        killed = proc.kill_records(records, wait_s=3.0)
        pids = [r.identity.pid for r in killed]
        res["killed"] += len(pids)
        res["killed_pids"].extend(pids)
        log.warning(f"свип: убит зависший браузер token={token} port={port} "
                    f"(CDP молчал {age:.0f}с): pids={pids}")
        _stray_first_seen.pop(port, None)
    for port in list(_stray_first_seen):
        if port not in alive_ports:
            _stray_first_seen.pop(port, None)
    return res


# ---- сентинел показов: глобальный WinEvent-хук + частый проход -------------
#
# Требование владельца 2026-10-17: в скрытом режиме окна сбора НЕ ДОЛЖНЫ
# появляться вообще (ни на старте воркера, ни при шторме перезапусков, ни у
# осиротевших браузеров). Слои-сторожа прячут в миллисекунды ТОЛЬКО свои
# зарегистрированные окна: щель «браузер стартовал — воркер ещё не подключил
# сторож» (нормализация геометрии при connect сама ПОКАЗЫВАЕТ окно) закрывает
# сентинел. sweep_stray_browsers ниже — задел на случай отказа от сентинела;
# в прод он НЕ подключён (review 2026-10-17), «каденс ~3 с» — не действующий
# механизм. Сентинел живёт в процессе демона и закрывает обе
# дыры сразу для всего рабочего стола:
#   - out-of-context WinEvent-хук на EVENT_OBJECT_SHOW: любое наше окно,
#     показавшееся на экране, гасится за миллисекунды (включая осиротевшие
#     браузеры, о которых воркер уже забыл);
#   - проход list_chrome_windows() каждые 200 мс: страховка к хуку (показы
#     без EVENT_OBJECT_SHOW: бесшовный показ, смена владельца).
# Чужих окон не трогаем: pid обязан быть нашим Chromium по профилю под
# profile_base_dir() (как в свипе); в режиме visible (флаг трея) молчим.
#
# Шторм-защита (находка ревью 2026-10-17): скан профилей — PowerShell
# Get-CimInstance, живой замер ~0.8 с. Без защиты каждое показавшееся ЧУЖОЕ
# окно (терминал, редактор, браузер пользователя) тянуло полный скан, а
# пустой результат кэша вообще не кэшировался. Слои: (1) префильтр по имени
# образа процесса (QueryFullProcessImageName, ~0.05 мс) — не-chrome.exe до
# скана не доходит; (2) пустой кэш живёт TTL как любой другой; (3) refresh
# не чаще _SENTINEL_REFRESH_MIN_S; (4) скан под single-flight: параллельный
# вызов получает текущий кэш, а не второй скан; (5) alien-кэш — чужой
# chrome.exe не пересканируется _SENTINEL_ALIEN_TTL_S; (6) метка «чужой» —
# только по скану, ЗАВЕРШИВШЕМУСЯ после показа (pids_ts ≥ t_call) И
# видевшему хоть один chrome.exe (scan_raw > 0): троттлинг и летящий скан
# отдают кэш, снятый ДО спавна браузера, а тихий отказ PowerShell (пустой
# результат при сбое перечислителя) не считается состоявшимся сканом —
# иначе наш свежий pid уходил бы в alien на 60 с (review-skeptic
# 2026-10-17); (7) тик пред-фильтрует кандидатов (имя процесса +
# alien-кэш) ДО скана и сам кладёт чужих chrome.exe в alien; (8) alien-кэш
# мутируется под своим локом (alien_lock) — запись из хука/тика не рвёт
# итерацию само-лечения в скане.

_SENTINEL: Optional[dict] = None
_SENTINEL_LOCK = threading.Lock()
_SENTINEL_PIDS_TTL_S = 2.0
_SENTINEL_REFRESH_MIN_S = 0.5     # минимальный интервал фактического скана
_SENTINEL_ALIEN_TTL_S = 60.0      # чужой chrome.exe: не пересканировать
_SENTINEL_ALIEN_MAX = 32
_CHROME_IMAGE_NAMES = ("chrome.exe", "chromium.exe")
_SENTINEL_TICK_S = 0.2
_WINEVENT_SKIPOWNPROCESS = 0x0002


def _keep_window_env() -> bool:
    """OZON_KEEP_WINDOW=1 — начальный режим; явный "0" из трея сильнее."""
    if user_hidden():
        return False
    return _proc.env_bool("OZON_KEEP_WINDOW")


def _hwnd_pid(hwnd: int) -> Optional[int]:
    try:
        import ctypes
        from ctypes import wintypes
        pid = wintypes.DWORD(0)
        ctypes.windll.user32.GetWindowThreadProcessId(
            wintypes.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value) or None
    except Exception:
        return None


def _sentinel_alien_prune(st: dict) -> None:
    """Убрать из alien-кэша записи старше TTL (вызывается по переполнению)."""
    now = time.monotonic()
    alien = st.get("alien") or {}
    for pid in [p for p, ts in alien.items()
                if now - ts >= _SENTINEL_ALIEN_TTL_S]:
        alien.pop(pid, None)


def _sentinel_pids(refresh: bool = False) -> Dict[int, str]:
    """Наши Chromium (pid → token); TTL-кэш, включая ПУСТОЙ результат.

    refresh=True — «свежие данные сейчас», но не чаще _SENTINEL_REFRESH_MIN_S
    (шторм-защита). Скан — под single-flight: если его уже делает другой
    поток, отдаём текущий кэш вместо второго скана (0.8 с × события всего
    рабочего стола — это блокировка хука и нагрузка на CPU).
    """
    st = _SENTINEL
    if st is None:
        return {}
    now = time.monotonic()
    ts = st.get("pids_ts", 0.0)
    if ts:
        if refresh:
            if now - st.get("scan_ts", 0.0) < _SENTINEL_REFRESH_MIN_S:
                return st["pids"]
        elif now - ts < _SENTINEL_PIDS_TTL_S:
            return st["pids"]
    elif now - st.get("scan_ts", 0.0) < _SENTINEL_REFRESH_MIN_S:
        # Успешного скана ещё не было (ts=0), но попытка была только что —
        # в т.ч. неудачная (упавший/пустой скан): не штормим PS-спавнами
        # каждое событие показа (review-skeptic 2026-10-17).
        return st["pids"]
    lock = st.get("lock")
    if lock is None or not lock.acquire(blocking=False):
        return st.get("pids", {})
    try:
        now = time.monotonic()
        ts = st.get("pids_ts", 0.0)
        if ts:
            if refresh:
                if now - st.get("scan_ts", 0.0) < _SENTINEL_REFRESH_MIN_S:
                    return st["pids"]
            elif now - ts < _SENTINEL_PIDS_TTL_S:
                return st["pids"]
        elif now - st.get("scan_ts", 0.0) < _SENTINEL_REFRESH_MIN_S:
            return st["pids"]
        st["scan_start"] = time.monotonic()
        try:
            rows, raw = _our_profile_chromium_stat()
        except Exception as e:  # noqa: BLE001 — сентинел не должен падать
            # Попытка была (scan_ts), но данных нет: pids_ts НЕ бампаем —
            # упавший скан не должен выглядеть свежим/успешным (находка
            # ревью 2026-10-17, раунд 2: битый PowerShell → наш pid уходил
            # в alien по «свежему» пустому кэшу).
            log.debug(f"сентинел: скан профилей не удался: {e}")
            st.setdefault("pids", {})
            st["scan_ts"] = time.monotonic()
            return st["pids"]
        if raw == 0:
            # Перечислитель не увидел НИ ОДНОГО chrome.exe, хотя событие
            # показа пришло от живого chrome.exe (префильтр по имени образа
            # уже пройден) — это признак ТИХОГО отказа PowerShell: process.
            # _win_iter_chromium_processes молча возвращает [] на сбое, и
            # пустой «успешный» кэш выглядел бы свежее события → наш pid
            # уходил в alien на 60 с (review-skeptic 2026-10-17). Скан не
            # состоялся: только scan_ts, кэш не трогаем.
            log.debug("сентинел: скан пуст при видимом chrome.exe — "
                      "перечислитель молчит, кэш не обновляем")
            st.setdefault("pids", {})
            st["scan_ts"] = time.monotonic()
            return st["pids"]
        st["pids"] = {pid: token for pid, token, _p, _m in rows}
        st["scan_raw"] = raw
        st["pids_ts"] = time.monotonic()
        st["scan_ts"] = st["pids_ts"]
        alien = st.get("alien")
        if alien:
            # Само-лечение: pid, найденный нашим свежим сканом, не чужой —
            # снимаем застрявшую метку (pid-reuse, гонка кэша).
            with st["alien_lock"]:
                for pid in [p for p in alien if p in st["pids"]]:
                    alien.pop(pid, None)
        return st["pids"]
    finally:
        lock.release()


def sentinel_on_show(hwnd: int, *, hide=None, pids=None,
                     uv: Optional[Callable[[], bool]] = None) -> bool:
    """Погасить показавшееся НАШЕ окно (вызов из WinEvent-хука)."""
    if os.name != "nt" or not enabled_by_env() or _keep_window_env():
        return False
    if (uv or user_visible)():
        return False
    pid = _hwnd_pid(int(hwnd))
    if pid is None:
        return False
    t_call = time.monotonic()
    if pids is None:
        # Дешёвый префильтр до скана профилей: показавшееся окно не-chrome
        # процесса не может быть нашим Chromium. Без него каждое чужое окно
        # тянуло полный PS-скан (~0.8 с) — шторм на живом столе.
        name = (process_image_basename(pid) or "").lower()
        if name and name not in _CHROME_IMAGE_NAMES:
            return False
        st = _SENTINEL
        if st is not None:
            seen = (st.get("alien") or {}).get(pid)
            if seen and time.monotonic() - seen < _SENTINEL_ALIEN_TTL_S:
                return False
        ours = _sentinel_pids()            # быстрый путь: TTL-кэш без скана
        if pid not in ours:
            ours = _sentinel_pids(refresh=True)   # скан, с троттлингом
            if pid not in ours:
                # Метка «чужой» — только по скану, ЗАВЕРШИВШЕМУСЯ после
                # показа (pids_ts ≥ t_call). Начало скана (scan_start) не
                # годится: летящий скан в другом потоке отдаёт СТАРЫЙ pids,
                # хотя scan_start уже ≥ t_call (находка ревью 2026-10-17,
                # раунд 2); троттлинг/упавший скан тоже не бампают pids_ts.
                trusted = (st is not None and
                           st.get("pids_ts", 0.0) >= t_call and
                           st.get("scan_raw", 0) > 0)
                if not name or not trusted:
                    return False
                alien = st.setdefault("alien", {})
                with st["alien_lock"]:
                    alien[pid] = time.monotonic()
                    if len(alien) > _SENTINEL_ALIEN_MAX:
                        _sentinel_alien_prune(st)
                return False
    else:
        ours = pids
        if pid not in ours:
            ours = _sentinel_pids(refresh=True)
            if pid not in ours:
                return False
    # Re-check right before hiding: the tray may have switched to visible
    # while we were scanning profiles (review 2026-10-19).
    if _keep_window_env() or (uv or user_visible)():
        return False
    ok = bool((hide or minimize_hwnd)(int(hwnd)))
    if ok:
        log.info(f"сентинел(хук): спрятал окно hwnd={int(hwnd)} pid={pid}")
    return ok


def sentinel_tick(*, hide=None, rows=None, pids=None,
                  uv: Optional[Callable[[], bool]] = None) -> dict:
    """Один быстрый проход: спрятать видимые главные окна наших Chromium."""
    res = {"ours": 0, "visible": 0, "hidden": 0, "skipped": ""}
    if os.name != "nt":
        res["skipped"] = "non-windows"
        return res
    if not enabled_by_env():
        res["skipped"] = "guard_off"
        return res
    if _keep_window_env():
        res["skipped"] = "keep_window"
        return res
    if (uv or user_visible)():
        res["skipped"] = "user_visible"
        return res
    hide = hide or minimize_hwnd
    t_obs = time.monotonic()
    row_list = rows if rows is not None else list_chrome_windows()
    cand = [r for r in row_list if r.visible and r.is_main_window]
    names: Dict[int, str] = {}
    if pids is None:
        if not cand:
            # Нечего решать — не сканируем профили (иначе скан каждые 2 с
            # даже при пустом рабочем столе; холостая нагрузка демона,
            # ревью 2026-10-17).
            return res
        # Пред-фильтр до скана (как в хуке): чужие chrome.exe из alien-кэша и
        # не-chrome процессы нашими быть не могут. Без него тик сканировал
        # профили каждые ~2 с, пока на экране висело ЛЮБОЕ окно chrome.exe —
        # например, личный Chrome владельца (находка ревью 2026-10-17).
        st = _SENTINEL
        alien = (st.get("alien") or {}) if st is not None else {}
        now = time.monotonic()
        need = []
        for r in cand:
            seen = alien.get(r.pid)
            if seen and now - seen < _SENTINEL_ALIEN_TTL_S:
                continue
            nm = (process_image_basename(r.pid) or "").lower()
            names[r.pid] = nm
            if nm and nm not in _CHROME_IMAGE_NAMES:
                continue
            need.append(r)
        cand = need
        if not cand:
            return res
    ours = pids if pids is not None else _sentinel_pids()
    res["ours"] = len(ours)
    for row in cand:
        if row.pid not in ours:
            # Чужой chrome.exe — в alien-кэш: следующие тики не сканируют
            # профили ради него. Метка — только если скан ЗАВЕРШИЛСЯ после
            # наблюдения окна (pids_ts ≥ t_obs) и перечислитель что-то видел
            # (scan_raw > 0 — тихий пустой скан PS метку не даёт, см.
            # MAJOR-1 review-skeptic 2026-10-17); иначе `ours` — устаревший
            # кэш, снятый до спавна нашего браузера, и свежий НАШ pid
            # застрянет «чужим» на 60 с (находка ревью 2026-10-17, раунд 2).
            # Без refresh: проход идёт каждые 200 мс, состав обновит TTL-кэш
            # сам (≤2 с); мгновенный путь — хук.
            if pids is None and st is not None \
                    and names.get(row.pid, "") in _CHROME_IMAGE_NAMES \
                    and st.get("pids_ts", 0.0) >= t_obs \
                    and st.get("scan_raw", 0) > 0:
                alien = st.setdefault("alien", {})
                with st["alien_lock"]:
                    alien[row.pid] = time.monotonic()
                    if len(alien) > _SENTINEL_ALIEN_MAX:
                        _sentinel_alien_prune(st)
            continue
        # Re-check per row: a scan can take ~0.8 s and the tray may have
        # flipped to visible mid-tick (review 2026-10-19).
        if _keep_window_env():
            res["skipped"] = "keep_window"
            break
        if (uv or user_visible)():
            res["skipped"] = "user_visible"
            break
        res["visible"] += 1
        if hide(row.hwnd):
            res["hidden"] += 1
            log.info(f"сентинел: спрятал окно pid={row.pid} hwnd={row.hwnd} "
                     f"'{row.title}' {row.width}x{row.height}")
    return res


def _sentinel_hook_loop(stop: threading.Event) -> None:
    """Поток хука: EVENT_OBJECT_SHOW со всего рабочего стола → sentinel_on_show."""
    import ctypes
    from ctypes import wintypes
    user32 = _user32()
    if user32 is None or _WINEVENTPROC is None:
        return
    # Типы объявляем ИМЕННО нашему дескриптору (канон _hook_loop): на
    # глобальном ctypes.windll.user32 restype по умолчанию c_int обрезал
    # HANDLE хука на x64 — UnhookWinEvent молча не срабатывал.
    _setup_api(user32)

    def _cb(_hook, event, hwnd, idobject, _idchild, _thread, _ms):
        try:
            if event == EVENT_OBJECT_SHOW and idobject == OBJID_WINDOW:
                sentinel_on_show(int(hwnd))
        except Exception as e:  # noqa: BLE001
            log.debug(f"сентинел(хук): {type(e).__name__}: {e}")

    cb = _WINEVENTPROC(_cb)
    hook = user32.SetWinEventHook(
        EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, None, cb, 0, 0,
        WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS)
    if not hook:
        log.warning("сентинел: SetWinEventHook не удался — только быстрый проход")
        return
    msg = wintypes.MSG()
    try:
        while not stop.is_set():
            got = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
            if got:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                stop.wait(0.05)
    finally:
        user32.UnhookWinEvent(hook)


def _sentinel_fast_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            sentinel_tick()
        except Exception as e:  # noqa: BLE001
            log.debug(f"сентинел(проход): {type(e).__name__}: {e}")
        stop.wait(_SENTINEL_TICK_S)


def watch_launcher_pid(pid, port) -> bool:
    """Взять под охрану окно браузера, только что запущенного лаунчером.

    Закрывает щель launch→connect (2-7 с), где окно некому прятать: WindowGuard
    регистрируется лишь в Session._sync_window_guard после connect, а у караула
    лаунчера есть слепые слоты (REST-проба порта). Живой замер 2026-10-18:
    окно владельца жило видимым ~4 с в этой щели (репро .tmp/gap_harness.py:
    hidden_ms=4014 на коде до фикса). Гейт — как у sentinel_start: hidden-режим
    и включённый сторож; видимый режим (трей/keep_window) не трогаем.
    """
    if _keep_window_env():
        log.debug("ранняя охрана не нужна: окна видимы (OZON_KEEP_WINDOW)")
        return False
    if not enabled_by_env():
        log.debug("ранняя охрана выключена: OZON_WINDOW_GUARD=off")
        return False
    g = guard()
    if g is None:
        log.debug("ранняя охрана не встала: guard()=None при включённом стороже (аномалия)")
        return False
    g.watch(pid, port, identity_grace=True)
    return True


def sentinel_start() -> bool:
    """Запустить сентинел в текущем процессе (идемпотентно). False — выключен.

    Причины False пишутся в лог: до 2026-10-18 выход был МОЛЧАЛИВЫМ, а
    стартовая строка терялась ещё и в слепой зоне лога демона (attach_file
    вызывался позже, внутри run_campaign) — в файле не оставалось ни одного
    упоминания сентинела при любом исходе.
    """
    global _SENTINEL
    if os.name != "nt":
        log.warning("сентинел показов НЕ запущен: не Windows")
        return False
    if not enabled_by_env():
        log.warning("сентинел показов НЕ запущен: OZON_WINDOW_GUARD выключен")
        return False
    if _keep_window_env():
        # Стартовый visible не повод не запускать сторожа: трей может явно
        # свернуть окна позже (флаг "0"), и тогда он обязан работать даже
        # если сам трей убьют. Хук и тик спят под _keep_window_env сами.
        log.info("сентинел показов: старт в visible — ждёт явного hidden из трея")
    with _SENTINEL_LOCK:
        if _SENTINEL is not None:
            return True
        stop = threading.Event()
        _SENTINEL = {"stop": stop, "pids": {}, "pids_ts": 0.0,
                     "scan_ts": 0.0, "scan_start": 0.0, "scan_raw": 0,
                     "alien": {}, "alien_lock": threading.Lock(),
                     "lock": threading.Lock()}
        threading.Thread(target=_sentinel_hook_loop, args=(stop,),
                         name="wp-sentinel-hook", daemon=True).start()
        threading.Thread(target=_sentinel_fast_loop, args=(stop,),
                         name="wp-sentinel-tick", daemon=True).start()
        log.info("сентинел показов запущен (хук + проход 200мс)")
        return True


def sentinel_stop() -> None:
    """Остановить сентинел (тесты / переиспользование процесса)."""
    global _SENTINEL
    with _SENTINEL_LOCK:
        if _SENTINEL is not None:
            _SENTINEL["stop"].set()
            _SENTINEL = None
