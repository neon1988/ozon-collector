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
инвариант «окно браузера коллектора свёрнуто, фокус принадлежит пользователю».
Источник событий логируется, чтобы при следующем живом прогоне было видно, кто
именно показывает окно.

Что guard НЕ делает (намеренно):
- не трогает окна чужих браузеров: фильтр по pid, зарегистрированным Session
  с `browser_owner == "collector"`; чужой Chrome пользователя не сворачивается;
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
from typing import Dict, Iterable, NamedTuple, Optional

log = logging.getLogger(__name__)

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

# Пауза между попытками вернуть фокус. Нужна потому, что «лечение» фокуса
# опирается на синтетический Alt: если не помогло, повтор каждые 150 мс —
# это уже помеха пользователю, а не помощь.
FOCUS_RETRY_S = 3.0

# Как часто сверять, что охраняемый pid всё ещё держит наш CDP-порт. Живость
# проверяется каждый тик (дёшево), эта сверка поднимает PowerShell — поэтому
# редко.
IDENTITY_RECHECK_S = 15.0

# Ручное открытие окна пользователем уважается: окно браузера коллектора не
# надо сворачивать, если пользователь сам его открыл («восстановить» в
# контекстном меню таскбара). Признак ручного действия — свежий РЕАЛЬНЫЙ ввод:
# Windows не учитывает программные вызовы и SendInput в GetLastInputInfo,
# поэтому «Chromium сам показал» и «пользователь кликнул» различимы (замер
# 2026-10-06). Держим окно не дольше HOLD_S, иначе забытый открытый браузер
# останется висеть навсегда.
INPUT_FRESH_S = 1.5
try:
    HOLD_S = float(os.environ.get("OZON_WINDOW_HOLD_S", "300"))
except ValueError:          # «5m», «не число» → дефолт, а не ImportError
    HOLD_S = 300.0


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
    user32.EnumWindows.argtypes = [_ENUMPROC, L]
    user32.EnumWindows.restype = wintypes.BOOL
    k32 = _kernel32()
    if k32 is not None:
        k32.GetCurrentThreadId.argtypes = []
        k32.GetCurrentThreadId.restype = U
    _API_READY = True


_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                               wintypes.LPARAM) if os.name == "nt" else None


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
        delete_taskbar_tab(int(hwnd))
        return True
    except Exception as e:
        log.debug(f"window_policy: minimize err {type(e).__name__}: {e}")
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


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def last_input_age_s():
    """Секунд с последнего РЕАЛЬНОГО ввода пользователя (None — неизвестно).

    Ключевое измеренное свойство (2026-10-06): программные вызовы
    ShowWindow/SetForegroundWindow и SendInput НЕ обновляют GetLastInputInfo.
    Поэтому окно, «прыгнувшее» от Chromium, отличается от окна, открытого
    кликом пользователя, — без подмены фокуса и без хуков.
    """
    user32 = _user32()
    k32 = _kernel32()
    if user32 is None or k32 is None:
        return None
    _setup_api(user32)
    try:
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now_ms = k32.GetTickCount() & 0xFFFFFFFF
        return ((now_ms - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception as e:
        log.debug(f"window_policy: last_input_age err {type(e).__name__}: {e}")
        return None


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс (быстро: OpenProcess + код выхода, без WMI)."""
    k32 = _kernel32()
    if k32 is None:
        return False
    STILL_ACTIVE = 259
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32.OpenProcess.argtypes = [ctypes.c_uint, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                      ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                        int(pid))
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def _pid_owns_port(pid: int, port) -> bool:
    """Ровно ли этот PID держит CDP-порт (полная identity-сверка, дорогая).

    Требуется правилом проекта: трогать можно только процесс, который держит
    наш порт. Вызывается редко (раз в IDENTITY_RECHECK_S на каждый pid), потому
    что поднимает PowerShell.
    """
    import subprocess
    if not port:
        return False
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter "
             f"\"ProcessId={int(pid)}\").CommandLine"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20)
        cmdline = (out.stdout or "").strip()
    except Exception as e:
        log.debug(f"window_policy: cmdline не прочитан: {type(e).__name__}: {e}")
        return False
    return f"--remote-debugging-port={port}" in cmdline


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
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_fg: Optional[int] = None       # последнее «пользовательское» окно
        self._last_focus_try: float = 0.0
        self._holds: Dict[int, float] = {}           # hwnd -> момент открытия
        self._identity_checked: Dict[int, float] = {}  # pid -> когда сверяли порт
        self._fixed = 0                           # счётчик для телеметрии
        self._focus_fixed = 0

    # ---- реестр -----------------------------------------------------------
    def watch(self, pid: Optional[int], port: object = None,
              autostart: bool = True) -> None:
        """Начать охранять окна браузера pid (идемпотентно).

        autostart=False — только зарегистрировать, поток не поднимать:
        используется в тестах решения (tick), чтобы фоновой тик не сработал
        раньше проверки.
        """
        if not pid or os.name != "nt":
            return
        with self._lock:
            self._pids[int(pid)] = str(port or "?")
        if autostart:
            self.start()

    def forget(self, pid: Optional[int]) -> None:
        if not pid:
            return
        with self._lock:
            self._pids.pop(int(pid), None)
        self._identity_checked.pop(int(pid), None)

    def watched_pids(self) -> Dict[int, str]:
        with self._lock:
            return dict(self._pids)

    # ---- поток ------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop,
                                        name="ozon-window-guard", daemon=True)
        self._thread.start()
        log.debug("window_policy: сторож окон стартовал")

    def stop(self) -> None:
        self._stop.set()

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
        сверка «процесс держит наш порт» — раз в IDENTITY_RECHECK_S (дорого:
        поднимает PowerShell).
        """
        now = time.monotonic()
        stale = []
        for pid, port in self.watched_pids().items():
            if not _pid_alive(pid):
                stale.append((pid, "процесс мёртв"))
                continue
            last = self._identity_checked.get(pid, 0.0)
            if now - last < IDENTITY_RECHECK_S:
                continue
            self._identity_checked[pid] = now
            if not _pid_owns_port(pid, port):
                stale.append((pid, f"не держит порт {port}"))
        for pid, why in stale:
            self.forget(pid)
            self._identity_checked.pop(pid, None)
            log.info(f"window_policy: pid={pid} убран из охраны ({why}) — "
                     f"окна этого процесса не трогаем")
        return [p for p, _ in stale]

    def _user_opened(self, row, fg, age) -> bool:
        """Открыл ли окно пользователь сам (а не Chromium изнутри).

        Два условия, оба измерены:
        - свежий РЕАЛЬНЫЙ ввод (age <= INPUT_FRESH_S): программные ShowWindow
          и SendInput в GetLastInputInfo не попадают, значит клик по таскбару
          виден, а «прыжок» Chromium — нет;
        - фокус на НАШЕМ окне: если фокус у пользователя (он печатает/работает),
          а окно браузера просто выскочило, уважать его нельзя — свернём.
        """
        if age is None or age > INPUT_FRESH_S:
            return False
        if fg is not None and fg.hwnd != row.hwnd:
            return False
        if row.hwnd not in self._holds:
            log.info(f"window_policy: окно pid={row.pid} открыто пользователем "
                     f"(ввод {age:.2f}с назад) — не сворачиваю "
                     f"{HOLD_S:.0f}с, фокус не отбираю")
        self._holds.setdefault(row.hwnd, time.monotonic())
        return True

    def _hold_active_for(self, hwnd: int) -> bool:
        """Удерживается ли окно hwnd открытым пользователем (и не истекло)."""
        started = self._holds.get(hwnd)
        if started is None:
            return False
        if time.monotonic() - started > HOLD_S:
            self._holds.pop(hwnd, None)
            return False
        return True

    def _hold_active(self, row) -> bool:
        """Окно удерживается открытым пользователем (и ещё не истекло)."""
        return self._hold_active_for(row.hwnd)

    def tick(self) -> dict:
        """Одна итерация: вернуть инвариант «окно свёрнуто, фокус у пользователя».

        Исключение из инварианта — окно, открытое пользователем руками: оно не
        сворачивается HOLD_S секунд и не отбирает фокус.

        Возвращает счётчики (для тестов и телеметрии).
        """
        # Реестр чистим ПЕРВЫМ делом: упавший браузер оставляет pid в реестре, а
        # Windows переиспользует pid — дальше по нему сворачивалось бы уже чужое
        # окно. Пересобираем pids ПОСЛЕ чистки, иначе подставной pid успеет
        # попасть в обработку этого же тика.
        self._drop_dead_browsers()
        pids = self.watched_pids()
        if not pids:
            return {"unminimized": 0, "focus_restored": 0}
        unminimized = 0
        held = 0
        fg = foreground()
        age = last_input_age_s() if fg is not None and fg.pid in pids else None
        for row in list_chrome_windows():
            if row.pid not in pids:
                continue                       # чужой браузер — не трогаем
            if not row.visible or row.iconic:
                self._holds.pop(row.hwnd, None)   # окно скрыто/свёрнуто — забыть
                continue
            if not row.is_main_window:
                continue                       # попап/инфобар — не наше окно
            if self._hold_active(row) or self._user_opened(row, fg, age):
                held += 1
                continue
            if minimize_hwnd(row.hwnd):
                unminimized += 1
                self._fixed += 1
                log.info(
                    f"window_policy: окно браузера pid={row.pid} "
                    f"(порт {pids[row.pid]}) вышло из minimized "
                    f"({row.width}x{row.height}) — возвращаю в minimized; "
                    f"заголовок {row.title[:40]!r}")
        focus_restored = 0
        if fg is not None:
            # Удержание проверяем по КОНКРЕТНОМУ окну в фокусе, а не по
            # «словарь непуст»: удержание чужого окна не должно отменять возврат
            # фокуса для остальных браузеров процесса (их 9-17).
            if fg.pid in pids and not self._hold_active_for(fg.hwnd):
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
            elif fg.pid not in pids:
                self._last_fg = fg.hwnd
        out = {"unminimized": unminimized, "focus_restored": focus_restored}
        if held:
            out["user_held"] = held
        return out

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
    """Включён ли сторож. ПО УМОЛЧАНИЮ ВЫКЛЮЧЕН.

    Пользователь 2026-10-06: «Хватит трогать мои браузеры, сворачивать. Трогай
    только свои». Окна браузеров коллектора — его рабочие окна, он сам их
    открывает и сам ими пользуется, поэтому сторож по умолчанию не
    регистрирует ни одного pid и не делает ничего. Включается явно:
    `OZON_WINDOW_GUARD=1`.
    """
    return os.environ.get("OZON_WINDOW_GUARD", "0") == "1"


def guard() -> Optional[WindowGuard]:
    """Синглтон сторожа на процесс (None, если выключен).

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
