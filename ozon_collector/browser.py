"""Браузерный слой: probe здоровья, классификация ошибок, restart/stop Chromium.

Зависит только от process.py (identity, kill) и от duck-typed Session
(атрибуты cdp_url, browser_identity, page). Сама Session живёт в cdp.py.
Recovery-автомата (R1/R2, retry) — в search.py; здесь строительные блоки.

Классификация (policy):
  browser          -> recycle
  renderer_failure -> recycle
  target           -> no recycle
  unknown          -> no recycle

probe: BROWSER_DEAD / BROWSER_UNHEALTHY / RENDERER_FAILURE / TARGET_LOST /
       HEALTHY / UNKNOWN. BROWSER_UNHEALTHY (identity жив, но независимый CDP
       HTTP-проб не отвечает) НЕ считается смертью Chromium — до retries ретраев.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import log
from . import process as P


class ProbeState:
    BROWSER_DEAD = "browser_dead"
    BROWSER_UNHEALTHY = "browser_unhealthy"
    RENDERER_FAILURE = "renderer_failure"
    TARGET_LOST = "target_lost"
    HEALTHY = "healthy"
    UNKNOWN = "unknown"


class BrowserRecoveryError(Exception):
    """Восстановимый сбой браузера — допустим повтор restart/connect."""


class BrowserOwnershipError(Exception):
    """Нарушение ownership (чужой/неизвестный процесс, matching>1).

    Убивать нельзя, продолжать нельзя — требуется ручное вмешательство.
    """


def _launch_script_name() -> str:
    return "launch_chrome.ps1" if os.name == "nt" else "launch_chrome.sh"


def _launch_script_path() -> Path:
    return (Path(__file__).resolve().parent.parent / "scripts"
            / _launch_script_name())


def _launch_script(cdp_url: str):
    """Запуск живого Chromium через scripts/launch_chrome (порт из cdp_url)."""
    script = _launch_script_path()
    if not script.exists():
        hint = ("powershell -ExecutionPolicy Bypass -File "
                "scripts/launch_chrome.ps1" if os.name == "nt"
                else "sh scripts/launch_chrome.sh")
        raise RuntimeError(
            f"{_launch_script_name()} не найден: {script}\n"
            f"Запустите браузер вручную: {hint}")
    env = dict(os.environ)
    env["PORT"] = str(P.cdp_port(cdp_url))
    cmd = (["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)]
           if os.name == "nt" else [str(script)])
    subprocess.Popen(cmd, env=env,
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


# ---- классификация ---------------------------------------------------------

def has_renderer_signature(msg: str) -> bool:
    """Сигнатуры подтверждённого renderer-отказа (наш наблюдённый OOM-сценарий).

    НЕ является доказательством OOM — только признак для классификации.
    Покрывает: детач frame (net::ERR_ABORTED), «renderer crashed», OOM
    («Out of memory», «Aw, Snap!») и гибель JS-контекста вкладки.
    """
    m = msg.lower()
    if "net::err_aborted" in m and "frame was detached" in m:
        return True
    if "renderer" in m and "crashed" in m:
        return True
    if "out of memory" in m:
        return True
    if "aw, snap" in m or "aw snap" in m:
        return True
    if "execution context was destroyed" in m:
        return True
    if "cannot find context with specified id" in m:
        return True
    if "frame was detached" in m and "crashed" in m:
        return True
    return False


def is_renderer_failure(exc) -> bool:
    """True, если исключение говорит о сломанном/зависшем renderer.

    Такие исключения НЕЛЬЗЯ проглатывать ниже по стеку (scroll/expand/goto):
    их надо пробрасывать наверх, чтобы сработал recovery (R1 restart + R2 retry).
    TimeoutError трактуется как подозрение на hang (операция не вернулась);
    окончательный вердикт даёт classify_error (контрольный evaluate).
    """
    from playwright.sync_api import Error as PWError
    if not isinstance(exc, PWError):
        return False
    msg = str(exc)
    if has_renderer_signature(msg):
        return True
    if type(exc).__name__ == "TimeoutError":
        return True
    if ("Browser has been closed" in msg
            or "Target page, context or browser has been closed" in msg):
        return True
    return False


@dataclass
class Classification:
    error_kind: str
    probe_state: str | None = None
    probe_attempts: int = 0
    signature: bool = False

    @property
    def should_recycle(self) -> bool:
        return self.error_kind in ("browser", "renderer_failure")


class BrowserProbe:
    """Независимый health probe: OS identity + CDP HTTP + состояние target.

    CDP HTTP-проб (GET /json/version) не зависит от Playwright-транспорта:
    сломанный transport сам по себе НЕ превращает живой Chromium в BROWSER_DEAD.
    """

    def __init__(self, retries: int = 3, interval: float = 0.5,
                 http_timeout: float = 2.0):
        self.retries = retries
        self.interval = interval
        self.http_timeout = http_timeout

    def cdp_http_ok(self, cdp_url: str) -> bool:
        """Независимый CDP HTTP-проб: /json/version отвечает и отдаёт Browser."""
        try:
            with urllib.request.urlopen(cdp_url + "/json/version",
                                        timeout=self.http_timeout) as r:
                data = json.loads(r.read().decode())
                return bool(data.get("Browser"))
        except Exception:
            return False

    def json_list_targets(self, cdp_url: str):
        """/json/list -> список target'ов, или None при недоступности."""
        try:
            with urllib.request.urlopen(cdp_url + "/json/list",
                                        timeout=self.http_timeout) as r:
                data = json.loads(r.read().decode())
                return data if isinstance(data, list) else []
        except Exception:
            return None

    def page_target_state(self, sess) -> str | None:
        """Состояние target сессии: 'alive' | 'gone' | None (неизвестно)."""
        page = getattr(sess, "page", None)
        if page is None:
            return None
        try:
            closed = page.is_closed()
        except Exception:
            return None                    # transport сломан — определить нельзя
        if closed:
            return "gone"
        targets = self.json_list_targets(sess.cdp_url)
        if targets is None:
            return "alive"                 # HTTP жив, списка нет — доверяем page
        page_targets = [t for t in targets if t.get("type") == "page"]
        if not page_targets:
            return "gone"                  # браузер жив, но страниц нет
        return "alive"

    def probe(self, sess, signature: bool) -> tuple[str, int]:
        """Health probe. Возвращает (ProbeState, http_attempts)."""
        ident = getattr(sess, "browser_identity", None)
        if ident is None or not P.identity_alive(ident):
            return ProbeState.BROWSER_DEAD, 0
        attempts = 0
        cdp_ok = False
        for attempt in range(self.retries + 1):
            attempts += 1
            if self.cdp_http_ok(sess.cdp_url):
                cdp_ok = True
                break
            if attempt < self.retries:
                time.sleep(self.interval)
        if not cdp_ok:
            return ProbeState.BROWSER_UNHEALTHY, attempts
        state = self.page_target_state(sess)
        if state == "gone":
            return (ProbeState.RENDERER_FAILURE if signature
                    else ProbeState.TARGET_LOST), attempts
        if state == "alive":
            return ProbeState.HEALTHY, attempts
        return ProbeState.UNKNOWN, attempts


def _classify_hang(sess, signature: bool) -> Classification:
    """TimeoutError: операция не вернулась за таймаут (подозрение на hang).

    Отличить «долго, но живо» от «renderer завис» контрольным evaluate
    на живой странице: если страница отвечает на 1+1 — это временная
    задержка (unknown, НЕ recyclable); если evaluate сам не возвращается
    или кидает renderer-ошибку — renderer завис/упал (recyclable).
    """
    page = getattr(sess, "page", None)
    if page is None:
        return Classification("unknown", signature=signature)
    try:
        page.evaluate("1+1", timeout=4000)
    except Exception:
        return Classification("renderer_failure", probe_state="renderer_hang",
                              signature=signature)
    return Classification("unknown", signature=signature)


def classify_error(exc, sess=None) -> Classification:
    """Классифицирует исключение сбора товара -> Classification.

    sess — сессия с cdp_url/browser_identity/page; None -> «не знаем» (unknown).
    error_kind — источник для policy; probe_state/probe_attempts — телеметрия.
    """
    signature = has_renderer_signature(str(exc))
    if isinstance(exc, BrowserRecoveryError):
        return Classification("browser", probe_state="watchdog",
                              signature=signature)
    from playwright.sync_api import Error as PWError
    if not isinstance(exc, PWError):
        return Classification("unknown", signature=signature)
    name = type(exc).__name__
    msg = str(exc)
    if ("Browser has been closed" in msg
            or "Target page, context or browser has been closed" in msg):
        return Classification("browser", signature=signature)
    if name == "TimeoutError":
        return _classify_hang(sess, signature)
    if sess is None:
        return Classification("unknown", signature=signature)
    state, attempts = BrowserProbe().probe(sess, signature)
    kind = {
        ProbeState.BROWSER_DEAD: "browser",
        ProbeState.BROWSER_UNHEALTHY: "browser",
        ProbeState.RENDERER_FAILURE: "renderer_failure",
        ProbeState.TARGET_LOST: "target",
        ProbeState.HEALTHY: "unknown",
        ProbeState.UNKNOWN: "unknown",
    }[state]
    return Classification(kind, probe_state=state, probe_attempts=attempts,
                          signature=signature)


# ---- фоновый сторож здоровья --------------------------------------------------

class BrowserWatchdog:
    """Фоновый сторож Chromium для длинных прогонов (search).

    Daemon-поток опрашивает независимый CDP HTTP-проб (/json/version) каждые
    poll_interval секунд. Смерть браузера подтверждается двумя сигналами:
    CDP HTTP не отвечает И процесса Chromium на нашем profile+port больше нет
    (browser_processes). Если процесс жив, но CDP временно молчит — это транзиент
    (BROWSER_UNHEALTHY), и grace_s гасит ложные срабатывания: флаг «мёртв»
    выставляется только после непрерывного bad-окна длиной grace_s.

    GuardedPage/Session зовут check() перед каждой операцией: мёртвый браузер →
    BrowserRecoveryError сразу (fail-fast), без ожидания зависших операций.
    Playwright из фонового потока НЕ трогается (только HTTP + OS), поэтому
    поток безопасен при работе main-потока с page.
    """

    def __init__(self, cdp_url: str, poll_interval: float = 2.0,
                 grace_s: float = 4.0, probe: BrowserProbe | None = None):
        self.cdp_url = cdp_url
        self.poll_interval = poll_interval
        self.grace_s = grace_s
        self.probe = probe or BrowserProbe(http_timeout=1.0)
        self._lock = threading.Lock()
        self._dead = False
        self._dead_since: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        """Запустить фоновый поток (идемпотентно)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="ozon-watchdog", daemon=True)
        self._thread.start()
        log.debug(f"watchdog: поток стартовал ({self.cdp_url})")

    def stop(self):
        """Остановить поток (финально; после stop инстанс не переиспользуется)."""
        self._stop.set()
        log.debug(f"watchdog: поток остановлен ({self.cdp_url})")

    @property
    def alive(self) -> bool:
        with self._lock:
            return not self._dead

    def check(self):
        """Fail-fast: мёртвый браузер → BrowserRecoveryError (recyclable).

        Проверка стоит в начале каждой операции (eval/scroll/wait/goto), чтобы
        после смерти браузера следующие операции падали мгновенно, а не ждали
        свой таймаут по одной.
        """
        if not self.alive:
            log.warning(f"watchdog: блокировка операции — браузер мёртв "
                        f"({self.cdp_url})")
            raise BrowserRecoveryError(
                f"watchdog: Chromium мёртв на {self.cdp_url}")

    def _loop(self):
        while not self._stop.is_set():
            self._report(self._sample())
            self._stop.wait(self.poll_interval)

    def _report(self, ok: bool):
        """Debounce: bad-сэмплы копятся до grace_s, потом флаг «мёртв»."""
        now = time.monotonic()
        with self._lock:
            prev_dead = self._dead
            prev_since = self._dead_since
            if ok:
                self._dead = False
                self._dead_since = None
            elif self._dead_since is None:
                self._dead_since = now
            elif now - self._dead_since >= self.grace_s:
                self._dead = True
            if self._dead_since is not None and prev_since is None and not self._dead:
                log.debug(f"watchdog: {self.cdp_url} → probe bad "
                          f"(transient, grace={self.grace_s}s)")
            if prev_dead != self._dead:
                state = "dead" if self._dead else "alive"
                log.info(f"watchdog: {self.cdp_url} → {state}",
                         extra={"bad_since": self._dead_since})

    def _sample(self) -> bool:
        """Здоров ли браузер: CDP HTTP отвечает (основной сигнал, дёшево).

        CDP молчит → проверяем OS-уровень: процесса на profile+port нет —
        браузер реально мёртв (False); процесс ещё жив — транзиент (True),
        флаг «мёртв» не ставится (это BROWSER_UNHEALTHY, не смерть).
        """
        if self.probe.cdp_http_ok(self.cdp_url):
            return True
        return bool(P.browser_processes(self.cdp_url))


# ---- управление процессом браузера -----------------------------------------

@dataclass
class RestartResult:
    old: list = field(default_factory=list)
    killed: list = field(default_factory=list)
    created: P.ProcessRecord | None = None
    owned: list = field(default_factory=list)


class BrowserProcessManager:
    """Запуск / перезапуск / остановка нашего CDP-Chromium (профиль+порт).

    Каждый процесс, созданный любой попыткой launch, регистрируется сразу
    после появления (даже если CDP так и не поднялся) — частично созданный
    Chromium не теряется из ownership-графа.
    """

    def __init__(self, cdp_url: str = "http://127.0.0.1:9222",
                 launch_wait_s: float = 8.0, kill_wait_s: float = 3.0,
                 capture_interval: float = 0.2):
        self.cdp_url = cdp_url
        self.launch_wait_s = launch_wait_s
        self.kill_wait_s = kill_wait_s
        self.capture_interval = capture_interval

    def port_state(self) -> str:
        """Диагностика: 'listening' | 'not_listening' (TCP connect к порту).

        Предпроверка/телеметрия, НЕ инвариант и не доказательство возможности
        bind — единственный источник истины — сам launch.
        """
        host = urlparse(self.cdp_url).hostname or "127.0.0.1"
        port = P.cdp_port(self.cdp_url)
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return "listening"
        except OSError:
            return "not_listening"

    def launch(self, launch_id: str,
               wait_s: float | None = None) -> P.ProcessRecord | None:
        """Запуск Chromium + захват identity нового процесса.

        Возвращает ProcessRecord (alive -> cdp_ready при успехе) либо None,
        если процесс так и не появился (полный провал запуска). При появлении
        процесса без готовности CDP record возвращается с partial=True.
        matching>1 -> BrowserOwnershipError.
        """
        wait_s = wait_s or self.launch_wait_s
        _launch_script(self.cdp_url)
        record = None
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if record is None:
                matches = P.browser_processes(self.cdp_url)
                if matches:
                    if len(matches) > 1:
                        raise BrowserOwnershipError(
                            f"два и более matching процесса на {self.cdp_url}: "
                            f"{matches} (launch_id={launch_id})")
                    record = P.ProcessRecord(launch_id, matches[0])
                    record.set_lifecycle(P.Lifecycle.ALIVE)
            if record is not None and self._cdp_ready():
                record.set_lifecycle(P.Lifecycle.CDP_READY)
                return record
            time.sleep(self.capture_interval)
        if record is None:
            matches = P.browser_processes(self.cdp_url)
            if matches:
                if len(matches) > 1:
                    raise BrowserOwnershipError(
                        f"два и более matching процесса на {self.cdp_url}: "
                        f"{matches} (launch_id={launch_id})")
                record = P.ProcessRecord(launch_id, matches[0])
                record.set_lifecycle(P.Lifecycle.ALIVE)
        if record is not None:
            record.partial = record.lifecycle != P.Lifecycle.CDP_READY
        return record

    def _cdp_ready(self) -> bool:
        return BrowserProbe(http_timeout=1.0).cdp_http_ok(self.cdp_url)

    def _wait_browser_stopped(self, records,
                              wait_s: float | None = None) -> bool:
        """Все ли записи подтверждены мёртвыми (сперва best-effort kill)."""
        wait_s = wait_s or self.kill_wait_s
        P.kill_records(records, wait_s=wait_s)
        return all(r.lifecycle == P.Lifecycle.VERIFIED_DEAD for r in records)

    def _assert_browser_stopped(self, records):
        if not self._wait_browser_stopped(records):
            raise BrowserOwnershipError(
                "не удалось подтвердить остановку старых процессов Chromium "
                f"на {self.cdp_url}")

    def restart(self, old_records, launch_id: str) -> RestartResult:
        """Одна попытка restart: kill old -> проверка чужих -> запуск.

        Старые записи обязаны уйти в verified_dead, иначе ownership violation.
        После остановки не должно остаться matching-процессов (чужие -> violation).
        """
        self._assert_browser_stopped(old_records)
        matching = P.browser_processes(self.cdp_url)
        if matching:
            raise BrowserOwnershipError(
                f"после остановки старых процессов на {self.cdp_url} найден "
                f"чужой Chromium: {matching}")
        record = self.launch(launch_id)
        if record is None:
            raise BrowserRecoveryError(
                f"Chromium не появился после запуска (launch_id={launch_id})")
        return RestartResult(old=list(old_records),
                             killed=[r for r in old_records
                                     if r.lifecycle == P.Lifecycle.VERIFIED_DEAD],
                             created=record, owned=[record])

    def force_stop(self, owned, wait_s: float | None = None):
        """Убить только owned-процессы (сверка identity в process.kill_records).

        Чужой matching-процесс -> BrowserOwnershipError, ничего не убиваем.
        """
        wait_s = wait_s or self.kill_wait_s
        matching = P.browser_processes(self.cdp_url)
        owned_ids = {rec.identity for rec in owned}
        for ident in matching:
            if ident not in owned_ids:
                raise BrowserOwnershipError(
                    f"чужой/неизвестный Chromium на {self.cdp_url}: {ident}; "
                    "ownership violation — требуется ручное вмешательство")
        P.kill_records(owned, wait_s=wait_s)
