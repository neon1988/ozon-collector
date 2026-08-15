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
import signal
import subprocess
import time
from urllib.parse import urlparse

if os.name == "nt":
    import ctypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                    ("dwHighDateTime", ctypes.c_uint32)]

PROFILE_TOKEN = "ozon-profile"

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


def _matches_browser(args, port: int) -> bool:
    """Browser-процесс Chromium на нашем profile+port (не дочерний renderer).

    Дочерние процессы (renderer/gpu/utility) наследуют команду, но имеют
    --type=... — они исключаются. --user-data-dir считается нашим, если
    последний компонент пути == PROFILE_TOKEN.
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
    return name == PROFILE_TOKEN


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
        yield pid, argv


def _win_iter_chromium_processes():
    cmd = [
        "powershell", "-NoProfile", "-Command",
        ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
         "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"),
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
        cmdline = it.get("CommandLine")
        if pid is None or not cmdline:
            continue
        yield int(pid), _tokenize_cmdline(cmdline)


def _iter_chromium_processes():
    if os.name == "nt":
        yield from _win_iter_chromium_processes()
    else:
        yield from _linux_iter_chromium_processes()


def browser_processes(cdp_url: str = "http://127.0.0.1:9222") -> list[ProcessIdentity]:
    """Идентичности Browser-процессов Chromium на profile+port из cdp_url."""
    port = cdp_port(cdp_url)
    out: list[ProcessIdentity] = []
    for pid, argv in _iter_chromium_processes():
        if not _matches_browser(argv, port):
            continue
        ident = process_identity(pid)
        if ident is not None:
            out.append(ident)
    return out


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
