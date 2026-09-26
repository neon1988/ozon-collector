"""Уборка процессов, оставшихся после pytest.

Живой случай 2026-09-26: полный прогон оставлял за собой осиротевшие
`node.exe` (пульты Playwright) и `python.exe` (воркеры `multiprocessing`
из интеграционных тестов key_daemon). Окна браузера при этом закрывались,
но пульты и воркеры висели часами. Молчащий мусор — тоже мусор.

Два механизма:

1. `reap_own_processes()` — вызывается из conftest в конце сессии. Убивает
   ТОЛЬКО потомков текущего процесса pytest (проверка по цепочке
   pid -> ppid). На Windows `th32ParentProcessID` не переписывается при
   смерти родителя, поэтому осиротевший пульт с ppid=мертвый_воркер
   корректно находится обходом дерева вверх. Живой демон (запущен до
   прогона, другой pid) в это множество не попадает по построению.

2. `sweep_stale()` — вызывается из `scripts/run_tests_guarded.py` ДО и ПОСЛЕ
   прогона. Ловит хвосты прошлых упавших прогонов: у них родитель мёртв,
   поэтому построить дерево уже нельзя, и вместо этого проверяется
   сигнатура командной строки (Playwright-драйвер / браузер с профилем из
   pytest-tmp / лаунчер с pytest-профилем / spawn-воркер multiprocessing).

Обе точки отключаются переменной окружения `KD_KEEP_TEST_PROCS=1`
(и `KD_PROTECT_PIDS` — доп. список pid, которые нельзя убивать никогда).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

Snapshot = Mapping[int, "ProcInfo"]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DAEMON_CACHE: set[int] | None = None
_LOG_PATH = os.path.join(_REPO_ROOT, "logs", "test_reap.log")

#: Процессы, которые вообще могут быть «хвостом тестов». Сузкий список —
#: чтобы не опрашивать командную строку у всей системы.
ORPHAN_CANDIDATE_NAMES = frozenset({
    "node.exe", "python.exe", "pythonw.exe", "pwsh.exe", "powershell.exe",
    "chrome.exe", "chrome-headless-shell.exe", "firefox.exe",
    "geckodriver.exe", "msedge.exe",
})

#: Подстроки в командной строке, по которым процесс опознаётся как тестовый.
STALE_CMDLINE_MARKERS = (
    "ms-playwright",
    "playwright\\driver",
    "playwright/driver",
    "pytest-of-",
    "spawn_main",
    "resource_tracker",
)

#: Подстроки для `launch_chrome.ps1` — лаунчер тестов, у которого родитель мёртв.
LAUNCHER_MARKER = "launch_chrome"


@dataclass(frozen=True)
class ProcInfo:
    """Минимальная запись о процессе: pid, родитель, имя исполняемого файла."""

    pid: int
    ppid: int
    name: str


# --------------------------------------------------------------------------
# снимок процессов
# --------------------------------------------------------------------------
def snapshot() -> Snapshot:
    """Снимок всех процессов: pid -> (ppid, имя). Не падает, если API недоступен."""
    if sys.platform == "win32":
        return _snapshot_win32()
    return _snapshot_proc()


def _snapshot_win32() -> Snapshot:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # SNAPPROCESS
    if snap is None or snap == -1:
        return {}
    out: dict[int, ProcInfo] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return {}
        while True:
            out[int(entry.th32ProcessID)] = ProcInfo(
                pid=int(entry.th32ProcessID),
                ppid=int(entry.th32ParentProcessID),
                name=entry.szExeFile,
            )
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snap)
    return out


def _snapshot_proc() -> Snapshot:
    """POSIX-фолбэк: /proc/<pid>/stat (поле 4 = ppid)."""
    out: dict[int, ProcInfo] = {}
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                fields = fh.read().rsplit(b")", 1)[-1].split()
            out[pid] = ProcInfo(pid=pid, ppid=int(fields[1]), name=fields[0].decode())
        except (OSError, IndexError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------
# чистая логика отбора (тестируется без реальных процессов)
# --------------------------------------------------------------------------
def children_map(snap: Snapshot) -> dict[int, set[int]]:
    """ppid -> множество детей."""
    out: dict[int, set[int]] = {}
    for info in snap.values():
        out.setdefault(info.ppid, set()).add(info.pid)
    return out


def descendants(snap: Snapshot, roots: Iterable[int]) -> set[int]:
    """Все потомки roots (не включая сами roots). Обход вниз по дереву."""
    kids = children_map(snap)
    seen: set[int] = set()
    stack = [r for r in roots if r in snap]
    while stack:
        for child in kids.get(stack.pop(), ()):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def top_level(owned: Iterable[int], snap: Snapshot) -> list[int]:
    """Только «вершины» поддерева: убивать их /T, иначе дети переживут."""
    owned_set = set(owned)
    roots = []
    for pid in owned_set:
        info = snap.get(pid)
        if info is None or info.ppid not in owned_set:
            roots.append(pid)
    return sorted(roots)


def plan_reap(snap: Snapshot, root_pid: int,
              protected: Iterable[int] = ()) -> list[ProcInfo]:
    """Что именно убить: живые потомки pytest, кроме защищённых pid.

    `protected` защищает не только сам pid, но и всё его поддерево: иначе
    «защищённый» воркер уехал бы, а его node-драйвер нет.
    """
    keep = set(protected) | {root_pid, os.getpid()}
    keep |= descendants(snap, protected)
    return [snap[pid] for pid in
            top_level(descendants(snap, [root_pid]) - keep, snap)
            if pid in snap]


def is_orphan(snap: Snapshot, info: ProcInfo) -> bool:
    """Родитель мёртв (или PID не существует) — кандидат на «сироту»."""
    return info.ppid not in snap


def is_stale_test_process(snap: Snapshot, info: ProcInfo,
                          cmd: str = "") -> bool:
    """Сирота + тестовая сигнатура в командной строке = хвост прошлого прогона."""
    if not is_orphan(snap, info):
        return False
    if info.name not in ORPHAN_CANDIDATE_NAMES:
        return False
    low = cmd.lower()
    if any(marker in low for marker in STALE_CMDLINE_MARKERS):
        return True
    return LAUNCHER_MARKER in low and "pytest" in low


# --------------------------------------------------------------------------
# командные строки кандидатов
# --------------------------------------------------------------------------
def cmdlines(pids: Sequence[int]) -> dict[int, str]:
    """Командные строки для узкого набора pid (один вызов PowerShell)."""
    pids = [p for p in pids if p > 0]
    if not pids:
        return {}
    if sys.platform != "win32":
        return _cmdlines_proc(pids)
    ids = ",".join(str(p) for p in pids)
    script = (
        f"Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.ProcessId -in @({ids}) }} | "
        f"ForEach-Object {{ \"$($_.ProcessId)`t$($_.CommandLine)\" }}"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    result: dict[int, str] = {}
    for line in out.splitlines():
        pid, _, cmd = line.partition("\t")
        if pid.strip().isdigit():
            result[int(pid)] = cmd.strip()
    return result


def _cmdlines_proc(pids: Sequence[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                out[pid] = fh.read().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            continue
    return out


# --------------------------------------------------------------------------
# убийство
# --------------------------------------------------------------------------
def kill_tree(pid: int) -> bool:
    """Убить процесс и его потомков. Windows: taskkill /F /T (без /T дети живы)."""
    if pid <= 0 or pid == os.getpid():
        return False
    if sys.platform == "win32":
        res = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        return res.returncode == 0
    try:
        os.kill(pid, 9)
        return True
    except OSError:
        return False


def protected_pids() -> set[int]:
    """Pid, которые нельзя убивать НИКОГДА: демон + явный список из env.

    Источники:
      - `KD_PROTECT_PIDS=59144,2222` — ручной список;
      - `data/key_daemon.pid` (JSON с полем `pid`) — живой демон. Pid
        дополнительно сверяется с текущей командной строкой процесса: если pid
        уже переиспользован другой программой, защита снимается (иначе мы бы
        «защищали» чужой процесс, а демон умер бы).
    """
    raw = os.environ.get("KD_PROTECT_PIDS", "")
    out: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.add(int(chunk))
    out |= _daemon_pid()
    return out


def _daemon_pid() -> set[int]:
    """Pid демона из pid-файла, только если процесс действительно он."""
    global _DAEMON_CACHE
    if _DAEMON_CACHE is not None:
        return _DAEMON_CACHE
    import json

    path = os.path.join(_REPO_ROOT, "data", "key_daemon.pid")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            pid = int(json.load(fh)["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return set()
    if pid <= 0 or pid not in snapshot():
        return set()
    if "key_daemon.py" not in cmdlines([pid]).get(pid, "").lower():
        return set()
    _DAEMON_CACHE = {pid}
    return _DAEMON_CACHE


def _disabled() -> bool:
    return os.environ.get("KD_KEEP_TEST_PROCS", "").strip() not in ("", "0", "false")


def _log(lines: Sequence[str]) -> None:
    for line in lines:
        print(f"[Reaper] {line}", flush=True)


# --------------------------------------------------------------------------
# публичные точки входа
# --------------------------------------------------------------------------
def reap_own_processes(root_pid: int | None = None) -> list[str]:
    """Убить всех живых потомков pytest. Вызывается в конце сессии тестов."""
    if _disabled():
        return ["отключено (KD_KEEP_TEST_PROCS)"]
    root = root_pid or os.getpid()
    snap = snapshot()
    victims = plan_reap(snap, root, protected_pids())
    log = []
    for info in victims:
        ok = kill_tree(info.pid)
        log.append(
            f"{'killed' if ok else 'FAIL  '} pid={info.pid} "
            f"name={info.name} ppid={info.ppid} причина=потомок pytest (root={root})"
        )
    after = snapshot()
    left = sorted(descendants(after, [root]) - protected_pids())
    if left:
        log.append(f"ВНИМАНИЕ: после уборки живы потомки pytest: {left}")
    log.append(f"итог: убрано {sum(1 for line in log if line.startswith('killed'))} "
               f"из {len(victims)} кандидатов")
    return log


def sweep_stale() -> list[str]:
    """Убрать хвосты ПРОШЛЫХ прогонов: сироты с тестовой сигнатурой."""
    if _disabled():
        return ["отключено (KD_KEEP_TEST_PROCS)"]
    snap = snapshot()
    protect = protected_pids()
    orphans = [i for i in snap.values()
               if i.pid not in protect and is_orphan(snap, i)
               and i.name in ORPHAN_CANDIDATE_NAMES]
    cmds = cmdlines([i.pid for i in orphans])
    log = []
    for info in orphans:
        cmd = cmds.get(info.pid, "")
        if not is_stale_test_process(snap, info, cmd):
            continue
        ok = kill_tree(info.pid)
        log.append(
            f"{'killed' if ok else 'FAIL  '} pid={info.pid} "
            f"name={info.name} ppid={info.ppid} причина=сирота прошлого прогона "
            f"cmd={cmd[:90]}"
        )
    if log:
        log.insert(0, f"найдено сирот-кандидатов: {len(orphans)}")
    else:
        log.append("сирот не найдено — чисто")
    return log


def run_and_log(action: str, lines: Sequence[str]) -> None:
    """Отчёт уборки: в stderr + в logs/test_reap.log (stderr глушит pytest capture).

    Файл нужен ещё и потому, что session-teardown выполняется при включённом
    capture: напечатанный в финале отчёт pytest не показывает, а терять
    сведения об убитых процессах нельзя.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    for line in lines:
        print(f"[Reaper:{action}] {line}", file=sys.stderr, flush=True)
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(f"{stamp} [{action}] {line}\n")
    except OSError:
        pass
