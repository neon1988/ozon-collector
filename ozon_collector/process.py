"""Изолированный process layer: identity и убийство процессов Chromium.

Один модуль отвечает за ОДНО: физическую идентификацию и завершение процессов
нашего CDP-браузера. Вся логика recovery/классификации живёт выше (browser.py,
search.py) — здесь нет ни recycle, ни probe.

Ownership двухуровневый:
- ProcessIdentity (pid + start_key) — ФИЗИЧЕСКОЕ владение; переживает PID-reuse,
  потому что start_key (Linux: starttime из /proc/<pid>/stat, Windows: время
  создания процесса) у каждого инстанса свой.
- ProcessRecord (launch_id + identity + lifecycle) — ЛОГИЧЕСКОЕ владение:
  к какой попытке recovery относится процесс. Сам по себе launch_id не позволяет
  ничего убить — перед каждым kill происходит сверка identity.

Убийство (Linux): exit-detection через pidfd + select.poll (POLLIN), работает и
для НЕ child процессов; сигналы через os.kill(pidfd, sig) ПОСЛЕ capability-теста,
иначе честный fallback: повторная проверка start_key непосредственно перед
os.kill(pid, sig). waitpid(pidfd) и os.kill(fd) как PID не используются.
Убийство (Windows): OpenProcess-хэндл открывается после валидации identity и
используется ВМЕСТО PID (защита от PID-reuse), затем TerminateProcess.

Безопасность: никогда не убиваем процесс с несовпадающим start_key (PID-reuse) —
это чужой процесс, даже если launch_id совпадает.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
from urllib.parse import urlparse

if os.name == "nt":
    import ctypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                    ("dwHighDateTime", ctypes.c_uint32)]

PROFILE_TOKEN = "ozon-profile"


def profile_base_dir() -> str:
    r"""Базовая директория профилей Chromium (дефолты как в launch_chrome.*).

    Переопределяется env OZON_PROFILE_BASE. Windows: %LOCALAPPDATA%\opencode
    (совпадает с launch_chrome.ps1), Linux: /tmp/opencode (launch_chrome.sh).
    """
    base = os.environ.get("OZON_PROFILE_BASE")
    if base:
        return base
    if os.name == "nt":
        la = os.environ.get("LOCALAPPDATA",
                            os.path.expanduser(r"~\AppData\Local"))
        return os.path.join(la, "opencode")
    return "/tmp/opencode"


def profile_path(token: str) -> str:
    """Полный путь профиля по токену (token = имя каталога, напр. 'ozon-profile')."""
    return os.path.join(profile_base_dir(), token)


def worker_profile_token(worker_id: int) -> str:
    """Токен профиля воркера: 'ozon-profile-worker-{k}' (часть ownership-контракта)."""
    return f"ozon-profile-worker-{int(worker_id)}"


def worker_profile_path(worker_id: int) -> str:
    """Полный путь профиля воркера (для launch env PROFILE и preflight)."""
    return profile_path(worker_profile_token(worker_id))


_OZON_SESSION_COOKIES = {
    "__Secure-access-token", "__Secure-refresh-token",
    "__Secure-user-id", "rfuid", "xcid", "guest",
}


def _ozon_cookie_names(profile: str) -> set | None:
    """Имена ozon-cookies профиля (детекция «есть ли сессия Ozon»).

    Читает Default/Network/Cookies (Chrome ≥ v92). None — файла нет или
    не читается (занят другим процессом / повреждён).
    """
    p = os.path.join(profile, "Default", "Network", "Cookies")
    if not os.path.exists(p):
        return None
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1)
        try:
            rows = con.execute(
                "select name from cookies where host_key like '%ozon%'").fetchall()
            return {r[0] for r in rows}
        finally:
            con.close()
    except Exception:
        return None


def has_ozon_session(profile: str) -> bool:
    """Есть ли в профиле Ozon-сессия (login/identity-cookies из main)."""
    names = _ozon_cookie_names(profile)
    return bool(names and names & _OZON_SESSION_COOKIES)


def bootstrap_worker_profiles(worker_ids, main_profile: str | None = None,
                              main_alive_check=None, worker_alive_check=None,
                              src_path=None) -> dict:
    """Сеять Ozon-сессию (cookies) из main-профиля в холодные воркер-профили.

    Решение §14 плана Фазы B: холодные профили Ozon блочит анти-бот
    («Похоже, нет соединения»). Доказано эмпирически: копирование
    Local State (os_crypt-ключ) + Default/Network/Cookies из main-профиля
    снимает блок (воркер открывает страницы как человек).

    Обновление 2026-08-17: куки СИНХРОНИЗИРУЮТСЯ при КАЖДОМ вызове, а не
    только при первом сидинге. Просроченные куки воркера (после долгой
    паузы/смены сессии main) Ozon блочит анти-ботом; главный профиль
    проходит проверку, поэтому его куки всегда перезаписывают воркерские.

    Инварианты:
    - копирование ТОЛЬКО при остановленном main-браузере (иначе файлы заняты);
    - работающий воркер-браузер пропускается;
    - при отсутствии кук в main — пропуск (нечего сеять).

    Возвращает {worker_id: статус} для логов.
    """
    from .cdp import browser_alive as _ba
    if main_profile is None:
        main_profile = profile_path(PROFILE_TOKEN)
    if main_alive_check is None:
        main_alive_check = lambda: _ba("http://127.0.0.1:9222")
    if worker_alive_check is None:
        def _w(k):
            url = f"http://127.0.0.1:{9222 + int(k)}"
            return bool(browser_processes(url, worker_profile_token(k)))
        worker_alive_check = _w

    status = {}
    if main_alive_check():
        for k in worker_ids:
            status[int(k)] = "skipped:main_browser_running"
        return status

    src = src_path or main_profile
    src_cookies = os.path.join(src, "Default", "Network", "Cookies")
    src_cookies_legacy = os.path.join(src, "Default", "Cookies")
    src_local = os.path.join(src, "Local State")
    if not os.path.exists(src_cookies) and not os.path.exists(src_cookies_legacy):
        for k in worker_ids:
            status[int(k)] = "skipped:no_main_cookies"
        return status

    for k in worker_ids:
        k = int(k)
        wp = worker_profile_path(k)
        if worker_alive_check(k):
            status[k] = "skipped:worker_browser_running"
            continue
        had_session = has_ozon_session(wp)
        os.makedirs(os.path.join(wp, "Default", "Network"), exist_ok=True)
        copied = []
        for s, d in ((src_local, os.path.join(wp, "Local State")),
                     (src_cookies, os.path.join(wp, "Default", "Network", "Cookies")),
                     (src_cookies_legacy, os.path.join(wp, "Default", "Cookies"))):
            if os.path.exists(s):
                shutil.copy2(s, d)
                copied.append(os.path.basename(d))
        status[k] = (f"refreshed ({', '.join(copied)})" if had_session
                     else f"seeded ({', '.join(copied)})")
    return status

_PIDFD_SIGNAL_OK: bool | None = None

_WIN_PROCESS_QUERY = 0x1000          # PROCESS_QUERY_LIMITED_INFORMATION
_WIN_PROCESS_TERMINATE = 0x0001      # PROCESS_TERMINATE
_WIN_SYNCHRONIZE = 0x00100000
_WIN_WAIT_OBJECT_0 = 0x00000000


def cdp_port(cdp_url: str) -> int:
    """Порт remote-debugging из cdp_url (9222 по умолчанию)."""
    port = urlparse(cdp_url).port
    return port if port else 9222


# ---- ProcessIdentity / ProcessRecord --------------------------------------

class ProcessIdentity:
    """Физическая идентичность процесса: pid + стабильный start_key.

    start_key постоянен для инстанса и меняется при PID-reuse — этого достаточно,
    чтобы отличать «наш» процесс от случайного захватчика того же pid.
    """

    __slots__ = ("pid", "start_key")

    def __init__(self, pid: int, start_key: str):
        self.pid = int(pid)
        self.start_key = str(start_key)

    def __eq__(self, other):
        if not isinstance(other, ProcessIdentity):
            return NotImplemented
        return self.pid == other.pid and self.start_key == other.start_key

    def __hash__(self):
        return hash((self.pid, self.start_key))

    def __repr__(self):
        return f"ProcessIdentity(pid={self.pid}, start_key={self.start_key!r})"


class Lifecycle:
    CREATED = "created"
    ALIVE = "alive"
    CDP_READY = "cdp_ready"
    KILLED = "killed"
    VERIFIED_DEAD = "verified_dead"


_LIFECYCLE_ORDER = {
    Lifecycle.CREATED: 0,
    Lifecycle.ALIVE: 1,
    Lifecycle.CDP_READY: 2,
    Lifecycle.KILLED: 3,
    Lifecycle.VERIFIED_DEAD: 4,
}


class ProcessRecord:
    """Реестровая запись созданного нами Chromium (логическое владение).

    lifecycle строго монотонен вперёд:
    created → alive → cdp_ready → killed → verified_dead.
    partial=True — launch_and_wait упал, но процесс был создан и зарегистрирован.
    """

    __slots__ = ("launch_id", "identity", "lifecycle", "partial",
                 "spawned_at", "cdp_ready_at")

    def __init__(self, launch_id: str, identity: ProcessIdentity,
                 partial: bool = False):
        self.launch_id = launch_id
        self.identity = identity
        self.lifecycle = Lifecycle.CREATED
        self.partial = bool(partial)
        self.spawned_at = time.monotonic()
        self.cdp_ready_at: float | None = None

    def set_lifecycle(self, state: str):
        if _LIFECYCLE_ORDER[state] < _LIFECYCLE_ORDER[self.lifecycle]:
            raise ValueError(
                f"lifecycle не может идти назад: {self.lifecycle} → {state}")
        self.lifecycle = state
        if state == Lifecycle.CDP_READY:
            self.cdp_ready_at = time.monotonic()

    def __repr__(self):
        return (f"ProcessRecord(launch_id={self.launch_id!r}, "
                f"identity={self.identity!r}, lifecycle={self.lifecycle!r}, "
                f"partial={self.partial})")


# ---- identity: чтение start_key и проверка живости -------------------------

def _linux_proc_stat(pid: int) -> dict | None:
    """/proc/<pid>/stat → {state, starttime}. None если прочитать нельзя.

    Парсим «после последней )»: comm может содержать пробелы и скобки, поэтому
    отступаем от последней закрывающей скобки. Далее поля с 3-го (state):
    rest[0]=state, ..., rest[19]=starttime (поле 22).
    """
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8",
                  errors="replace") as f:
            data = f.read()
    except OSError:
        return None
    rparen = data.rfind(")")
    if rparen < 0:
        return None
    rest = data[rparen + 2:].split()
    if len(rest) < 20:
        return None
    return {"state": rest[0], "starttime": rest[19]}


def _win_open_process(pid: int, access: int):
    """OpenProcess. None при отказе (в т.ч. процесса нет или нет прав)."""
    h = ctypes.windll.kernel32.OpenProcess(access, False, int(pid))
    return h if h else None


def _win_close_handle(h):
    ctypes.windll.kernel32.CloseHandle(h)


def _win_creation_time(h) -> int | None:
    ct, et, kt, ut = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
    if not ctypes.windll.kernel32.GetProcessTimes(
            h, ctypes.byref(ct), ctypes.byref(et),
            ctypes.byref(kt), ctypes.byref(ut)):
        return None
    return (ct.dwHighDateTime << 32) | ct.dwLowDateTime


def _win_process_identity(pid: int) -> ProcessIdentity | None:
    h = _win_open_process(pid, _WIN_PROCESS_QUERY)
    if h is None:
        return None
    try:
        ct = _win_creation_time(h)
        if ct is None:
            return None
        return ProcessIdentity(pid=pid, start_key=str(ct))
    finally:
        _win_close_handle(h)


def _linux_process_identity(pid: int) -> ProcessIdentity | None:
    st = _linux_proc_stat(pid)
    if st is None:
        return None
    return ProcessIdentity(pid=pid, start_key=st["starttime"])


def process_identity(pid: int) -> ProcessIdentity | None:
    """ProcessIdentity по pid (физическое владение) или None, если нет/не читается."""
    if os.name == "nt":
        return _win_process_identity(pid)
    return _linux_process_identity(pid)


def _linux_identity_alive(identity: ProcessIdentity) -> bool:
    st = _linux_proc_stat(identity.pid)
    if st is None:
        return False
    if st["state"] == "Z":          # зомби — уже не процесс
        return False
    return st["starttime"] == identity.start_key


def _win_identity_alive(identity: ProcessIdentity) -> bool:
    h = _win_open_process(identity.pid, _WIN_PROCESS_QUERY | _WIN_SYNCHRONIZE)
    if h is None:
        return False
    try:
        # объект процесса может «жить» после TerminateProcess, пока открыты
        # хэндлы; завершённость проверяем сигналом хэндла, а не только временем
        if ctypes.windll.kernel32.WaitForSingleObject(h, 0) == _WIN_WAIT_OBJECT_0:
            return False
        ct = _win_creation_time(h)
        if ct is None:
            return False
        return str(ct) == identity.start_key
    finally:
        _win_close_handle(h)


def identity_alive(identity: ProcessIdentity) -> bool:
    """Жив ли ИМЕННО этот инстанс процесса (start_key совпадает).

    PID-reuse: тот же pid с другим start_key → False, это чужой процесс.
    """
    if os.name == "nt":
        return _win_identity_alive(identity)
    return _linux_identity_alive(identity)


def pid_alive(pid: int) -> bool:
    """Жив ли процесс с данным PID (без identity-проверки).

    Для watchdog'а воркера: проверяем, жив ли родитель (оркестратор). PID-reuse
    здесь не критичен (родитель умирает редко и его PID не переиспользуется
    мгновенно), а дешёвая OpenProcess-проверка на Windows не требует прав.
    """
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        # OpenProcess на недавно завершившийся процесс на Windows может
        # вернуть handle (пока не переиспользован) — ложноположительный «жив».
        # Надёжно: GetExitCodeProcess: у живого процесса код = STILL_ACTIVE
        # (259), у завершённого — код завершения (≠259).
        h = _win_open_process(pid, _WIN_PROCESS_QUERY)
        if h is None:
            return False
        try:
            exit_code = ctypes.c_uint32()
            if not ctypes.windll.kernel32.GetExitCodeProcess(h,
                                                             ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            _win_close_handle(h)
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8",
                  errors="replace") as f:
            data = f.read()
        return bool(data)
    except (OSError, ValueError):
        return False


# ---- RSS (диагностика воркеров; без новых зависимостей) -------------------

def rss_bytes(pid: int) -> int | None:
    """Resident Set Size процесса в байтах (диагностика) или None."""
    if os.name == "nt":
        return _win_rss_bytes(pid)
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


def _win_rss_bytes(pid: int) -> int | None:
    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_uint32),
                    ("PageFaultCount", ctypes.c_uint32),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    h = _win_open_process(pid, _WIN_PROCESS_QUERY)
    if h is None:
        return None
    try:
        pmc = _PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
                h, ctypes.byref(pmc), pmc.cb):
            return None
        return int(pmc.WorkingSetSize)
    finally:
        _win_close_handle(h)


# ---- поиск наших browser-процессов по profile + port ----------------------

def _tokenize_cmdline(s: str) -> list[str]:
    """Командная строка → аргументы, уважая двойные кавычки."""
    args: list[str] = []
    cur: list[str] = []
    in_q = False
    for c in s:
        if c == '"':
            in_q = not in_q
        elif c.isspace() and not in_q:
            if cur:
                args.append("".join(cur))
                cur = []
        else:
            cur.append(c)
    if cur:
        args.append("".join(cur))
    return args


def _matches_browser(args, port: int,
                     profile_token: str = PROFILE_TOKEN) -> bool:
    """Browser-процесс Chromium на нашем profile+port (не дочерний renderer).

    Дочерние процессы (renderer/gpu/utility) наследуют команду, но имеют
    --type=... — они исключаются. --user-data-dir считается нашим, если
    последний компонент пути == profile_token (по умолчанию PROFILE_TOKEN;
    для воркеров — 'ozon-profile-worker-{k}').
    """
    user_data_dir: str | None = None
    has_port = False
    has_type = False
    for a in args:
        if a == f"--remote-debugging-port={port}":
            has_port = True
        elif a.startswith("--user-data-dir="):
            user_data_dir = a.split("=", 1)[1].strip("\"'")
        elif a.startswith("--type="):
            has_type = True
    if not (has_port and user_data_dir):
        return False
    if has_type:
        return False
    name = os.path.basename(user_data_dir.rstrip("/\\"))
    return name == profile_token


def _linux_iter_chromium_processes():
    import glob

    for path in glob.glob("/proc/[0-9]*/cmdline"):
        pid = int(path.split("/")[2])
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue
        argv = [a.decode("utf-8", "replace")
                for a in data.split(b"\0") if a]
        if not argv:
            continue
        base = os.path.basename(argv[0]).lower()
        if "chrome" not in base and "chromium" not in base:
            continue
        ppid = _linux_ppid(pid)
        yield pid, argv, ppid


def _linux_ppid(pid: int) -> int | None:
    """Родительский PID процесса на Linux (из /proc/<pid>/stat)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        # формат: pid (comm) state ppid ... — comm может содержать пробелы/скобки
        rpar = raw.rfind(b")")
        if rpar < 0:
            return None
        fields = raw[rpar + 2:].split()
        return int(fields[1])
    except (ValueError, IndexError):
        return None


def _win_iter_chromium_processes():
    cmd = [
        "powershell", "-NoProfile", "-Command",
        ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
         "Select-Object ProcessId,ParentProcessId,CommandLine | "
         "ConvertTo-Json -Compress"),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception:
        return
    if out.returncode != 0 or not out.stdout.strip():
        return
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return
    items = data if isinstance(data, list) else [data]
    for it in items:
        pid = it.get("ProcessId")
        ppid = it.get("ParentProcessId")
        cmdline = it.get("CommandLine")
        if pid is None or not cmdline:
            continue
        yield int(pid), _tokenize_cmdline(cmdline), (
            int(ppid) if ppid is not None else None)


def _iter_chromium_processes():
    if os.name == "nt":
        yield from _win_iter_chromium_processes()
    else:
        yield from _linux_iter_chromium_processes()


def browser_processes(cdp_url: str = "http://127.0.0.1:9222",
                      profile_token: str = PROFILE_TOKEN) -> list[ProcessIdentity]:
    """Идентичности Browser-процессов Chromium на profile+port из cdp_url.

    Ownership identity = (port, profile_token): матчатся только процессы на
    данном remote-debugging-port И с данным profile_token в --user-data-dir.
    """
    port = cdp_port(cdp_url)
    out: list[ProcessIdentity] = []
    for pid, argv, _ppid in _iter_chromium_processes():
        if not _matches_browser(argv, port, profile_token=profile_token):
            continue
        ident = process_identity(pid)
        if ident is not None:
            out.append(ident)
    return out


def _matches_profile_token(args, profile_token: str) -> bool:
    """Chromium-процесс на нашем профиле (main или дочерний --type=...)."""
    user_data_dir: str | None = None
    for a in args:
        if a.startswith("--user-data-dir="):
            user_data_dir = a.split("=", 1)[1].strip("\"'")
    if not user_data_dir:
        return False
    name = os.path.basename(user_data_dir.rstrip("/\\"))
    return name == profile_token


def orphaned_worker_browser_processes(
        cdp_url: str, profile_token: str) -> list[ProcessIdentity]:
    """Все Chromium-процессы воркера на профиле, чей владелец мёртв.

    Воркер (python-процесс) запускает СВОЙ Chromium. Если воркер был убит
    принудительно (kill/Ctrl+C/таймаут), его `finally` не выполняется и
    браузер остаётся жить — это осиротевший зомби. Здесь такой Chromium
    идентифицируется по родителю: родитель main chrome-процесса обязан быть
    python-процессом воркера. Родитель мёртв/не python → осиротевший.

    Если main-браузер на (порт, профиль) осиротел — возвращаем ВСЕ
    chromium-процессы этого профиля (main + дочерние renderer/gpu/utility).
    Убить только main нельзя: дочерние переживают и Chrome перезапускает
    main. Вернувшийся список можно force_stop (это не чужие процессы —
    профиль наш, а владельца-воркера больше нет).
    """
    port = cdp_port(cdp_url)
    orphans: list[ProcessIdentity] = []
    for pid, argv, ppid in _iter_chromium_processes():
        if not _matches_browser(argv, port, profile_token=profile_token):
            continue
        if _parent_owner_alive(ppid):
            continue
        ident = process_identity(pid)
        if ident is not None:
            orphans.append(ident)
    if not orphans:
        return []
    # main осиротел → собрать ВЕСЬ дочерний лес этого браузера
    # (renderer/gpu/utility/crashpad-handler наследуют --user-data-dir, но не
    # порт; их PPID-цепочка уходит к осиротевшему main). Дочерние процессы
    # своего профиля и ДРУГОГО живого браузера не трогаем.
    descendant_pids: set[int] = set()
    frontier = {i.pid for i in orphans}
    while frontier:
        nxt: set[int] = set()
        for pid, argv, ppid in _iter_chromium_processes():
            if not _matches_profile_token(argv, profile_token):
                continue
            if pid in descendant_pids or pid in frontier:
                continue
            if ppid in frontier:
                descendant_pids.add(pid)
                nxt.add(pid)
        frontier = nxt
    out = list(orphans)
    for pid in sorted(descendant_pids):
        ident = process_identity(pid)
        if ident is not None:
            out.append(ident)
    return out


def _parent_owner_alive(ppid: int | None) -> bool:
    """Жив ли родительский python-процесс воркера (по PPID).

    None (PPID не определился) трактуем как «родителя нет» → осиротевший
    (консервативно для preflight: лишний force_stop безвреден, а вот
    заблокированный запуск из-за зомби — вреден).
    """
    if ppid is None:
        return False
    try:
        if os.name == "nt":
            # Недостаточно проверить «есть ли процесс»: PID-reuse может дать
            # чужой живой процесс с тем же PID. Имя процесса обязано быть
            # python/pythonw (воркер-процесс). Читаем через WMI по PPID.
            cmd = [
                "powershell", "-NoProfile", "-Command",
                (f"(Get-CimInstance Win32_Process -Filter "
                 f"\"ProcessId={int(ppid)}\").Name"),
            ]
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=15)
            if out.returncode != 0 or not out.stdout.strip():
                return False
            name = out.stdout.strip().lower()
            return name.startswith("python")
        else:
            # Linux: жив ли python-процесс с данным PPID (родитель воркера)
            try:
                with open(f"/proc/{int(ppid)}/comm", "rb") as f:
                    comm = f.read().decode("utf-8", "replace").strip().lower()
            except OSError:
                return False
            return comm.startswith("python")
    except Exception:
        return False


# ---- Linux: pidfd (exit detection + сигналы) -------------------------------

def _pidfd_open(pid: int) -> int | None:
    if not hasattr(os, "pidfd_open"):
        return None
    try:
        return os.pidfd_open(pid)
    except OSError:
        return None


def _pidfd_signal_supported() -> bool:
    """Capability-тест: os.kill(pidfd, sig) работает на этой машине.

    Проверяем на собственном pidfd сигналом 0 (проверка без доставки).
    Кэшируется на процесс. При TypeError/OSError — не поддерживается.
    """
    global _PIDFD_SIGNAL_OK
    if _PIDFD_SIGNAL_OK is None:
        if not hasattr(os, "pidfd_open"):
            _PIDFD_SIGNAL_OK = False
        else:
            fd = None
            try:
                fd = os.pidfd_open(os.getpid())
            except OSError:
                _PIDFD_SIGNAL_OK = False
            else:
                try:
                    os.kill(fd, 0)
                    _PIDFD_SIGNAL_OK = True
                except (TypeError, ValueError, OSError):
                    _PIDFD_SIGNAL_OK = False
                finally:
                    os.close(fd)
    return _PIDFD_SIGNAL_OK


def _signal_process(identity: ProcessIdentity, sig: int):
    """Отправить сигнал инстансу identity (никогда не чужому pid).

    Основной путь: os.kill(pidfd, sig) — pidfd жёстко привязан к процессу.
    Fallback (pidfd-сигналы недоступны): повторная проверка start_key
    непосредственно перед os.kill(pid, sig) — остаточное окно документировано.
    """
    if _pidfd_signal_supported():
        fd = _pidfd_open(identity.pid)
        if fd is not None:
            try:
                os.kill(fd, sig)
                return
            except OSError:
                pass  # процесс уже исчез (ESRCH) — ничего делать не нужно
            finally:
                os.close(fd)
    if identity_alive(identity):
        os.kill(identity.pid, sig)


def _linux_wait_exit(identity: ProcessIdentity, wait_s: float) -> bool:
    """Дождаться завершения инстанса до wait_s. True — процесс исчез.

    Первично: select.poll() по pidfd (POLLIN — процесс завершён). Работает и для
    НЕ child процессов. После события identity перепроверяется — факт POLLIN сам
    по себе не считается доказательством смерти именно нашего инстанса.
    Fallback без pidfd: опрос identity.
    """
    deadline = time.monotonic() + wait_s
    fd = _pidfd_open(identity.pid)
    if fd is None:
        while time.monotonic() < deadline:
            if not identity_alive(identity):
                return True
            time.sleep(0.1)
        return not identity_alive(identity)

    import select

    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    try:
        while time.monotonic() < deadline:
            if not identity_alive(identity):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            events = poller.poll(min(int(remaining * 1000), 200))
            if events and (events[0][1] & (select.POLLIN | select.POLLHUP
                                           | select.POLLERR)):
                if not identity_alive(identity):
                    return True
                # событие было, но наш инстанс всё ещё жив — ждём дальше
        return not identity_alive(identity)
    finally:
        try:
            poller.unregister(fd)
        except OSError:
            pass
        os.close(fd)


def _linux_kill(identity: ProcessIdentity, wait_s: float = 3.0) -> bool:
    if not identity_alive(identity):
        return True
    _signal_process(identity, signal.SIGTERM)
    if _linux_wait_exit(identity, wait_s):
        return True
    if identity_alive(identity):
        _signal_process(identity, signal.SIGKILL)
        return _linux_wait_exit(identity, min(wait_s, 2.0))
    return True


# ---- Windows: handle-based kill -------------------------------------------

def _win_handle_matching(identity: ProcessIdentity):
    """OpenProcess ВМЕСТО PID: хэндл только если создание совпадает с identity.

    Хэндл «закрепляет» процесс: после PID-reuse к чужому процессу не обращаемся.
    """
    h = _win_open_process(identity.pid, _WIN_PROCESS_QUERY
                          | _WIN_PROCESS_TERMINATE | _WIN_SYNCHRONIZE)
    if h is None:
        return None
    ct = _win_creation_time(h)
    if ct is None or str(ct) != identity.start_key:
        _win_close_handle(h)
        return None
    return h


def _win_wait_handle(h, wait_s: float) -> bool:
    rc = ctypes.windll.kernel32.WaitForSingleObject(h, int(wait_s * 1000))
    return rc == _WIN_WAIT_OBJECT_0


def _win_kill(identity: ProcessIdentity, wait_s: float = 3.0) -> bool:
    h = _win_handle_matching(identity)
    if h is None:
        return True  # нет процесса или PID переиспользован — убивать нечего
    try:
        if not ctypes.windll.kernel32.TerminateProcess(h, 1):
            return not identity_alive(identity)
        if _win_wait_handle(h, wait_s):
            return True
        return not identity_alive(identity)
    finally:
        _win_close_handle(h)


# ---- публичное API убийства -----------------------------------------------

def wait_exit(identity: ProcessIdentity, wait_s: float = 3.0) -> bool:
    """Дождаться завершения инстанса до wait_s. True — инстанс исчез."""
    if os.name == "nt":
        h = _win_handle_matching(identity)
        if h is None:
            return True
        try:
            return _win_wait_handle(h, wait_s)
        finally:
            _win_close_handle(h)
    return _linux_wait_exit(identity, wait_s)


def kill_records(records, wait_s: float = 3.0) -> list[ProcessRecord]:
    """Best-effort завершение процессов записей records (только наших).

    Перед каждым kill — проверка identity (PID-reuse → не трогаем чужой).
    Убитые записи переводятся в killed → verified_dead и возвращаются.
    """
    killed: list[ProcessRecord] = []
    for rec in records:
        if not identity_alive(rec.identity):
            rec.set_lifecycle(Lifecycle.KILLED)
            rec.set_lifecycle(Lifecycle.VERIFIED_DEAD)
            killed.append(rec)
            continue
        gone = (_win_kill(rec.identity, wait_s) if os.name == "nt"
                else _linux_kill(rec.identity, wait_s))
        if gone:
            rec.set_lifecycle(Lifecycle.KILLED)
            rec.set_lifecycle(Lifecycle.VERIFIED_DEAD)
            killed.append(rec)
    return killed
