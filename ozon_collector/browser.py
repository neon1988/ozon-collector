"""Браузерный слой: probe здоровья, классификация ошибок, restart/stop Chromium.

Зависит только от process.py (identity, kill) и от duck-typed Session
(атрибуты cdp_url, browser_pid, page). Сама Session живёт в cdp.py.
Recovery-автомата (R1/R2, retry) — в search.py; здесь строительные блоки.

Классификация (policy):
  browser          -> recycle
  renderer_failure -> recycle
  target           -> no recycle
  unknown          -> no recycle

probe: BROWSER_DEAD / BROWSER_UNHEALTHY / RENDERER_FAILURE / TARGET_LOST /
       HEALTHY / UNKNOWN. BROWSER_DEAD выводится только когда доказуемо нет
       процесса на (порт, профиль) и нет нашего инстанса в снимке.
       BROWSER_UNHEALTHY (процесс жив, но независимый CDP HTTP-проб не
       отвечает, либо владение недоказуемо — external-браузер) НЕ считается
       смертью Chromium — до retries ретраев.
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
from typing import NamedTuple
from urllib.parse import urlparse

from . import launchlog as _LL
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


class AccessChallengeError(Exception):
    """Ozon отдал анти-бот/челлендж-страницу (FAB и т.п.) вместо контента.

    Отдельный error_kind (access_challenge): такое НЕ должно считаться
    успешным сбором — иначе в результат попадает страница-заглушка
    («Похоже, нет соединения», 0 отзывов) как будто товар собран.
    """


class StructureError(Exception):
    """Структура собранного raw не похожа на карточку товара (acceptance-gate).

    Не recyclable: повторная попытка с перезапуском браузера не исправит
    неполные/чужие/битые данные (product пуст, pid не совпал, reviews не
    список). Челлендж-title в product.title обрабатывается отдельно
    (AccessChallengeError) — он recyclable, retry с recycle.
    """


class VariantShortError(Exception):
    """Собрано меньше отзывов, чем обещает variant_count (брак по правилу
    «собранное = Этот вариант товара (N)»).

    Recyclable: короткий сбор обычно значит залипшую ленивую догрузку или
    подвисший renderer — recycle браузера + повтор сбора исправляет.
    """


def _launch_script_name() -> str:
    return "launch_chrome.ps1" if os.name == "nt" else "launch_chrome.sh"


def _launch_script_path() -> Path:
    return (Path(__file__).resolve().parent.parent / "scripts"
            / _launch_script_name())


def _chrome_override() -> str | None:
    """Путь к автономному Chromium для launch-скриптов, либо None.

    OZON_CHROME_PATH → CHROME в scripts/launch_chrome.ps1/.sh. Допустим путь
    к chrome.exe ИЛИ к каталогу с chrome.exe (Chrome-for-Testing, Edge — любой
    Chromium-ский браузер). Даёт независимость от системного Chrome и его
    апдейтов (Google Update): автономный Chrome никогда не обновляется сам.
    """
    return os.environ.get("OZON_CHROME_PATH") or None


BROWSER_KINDS = ("chrome", "firefox")
DEFAULT_BROWSER_KIND = "chrome"


def browser_kind() -> str:
    r"""Движок браузера: 'chrome' (CDP) | 'firefox' (geckodriver/Selenium).

    Дефолт = chrome с 2026-09-25. Причина смены: Ozon отдаёт заглушку «похоже,
    нет соединения» на ПЕРВОМ же goto ЛЮБОЙ сессии geckodriver/Marionette, то
    есть блокируется сам транспорт автоматизации Firefox, а не отдельные его
    признаки. Firefox-бэкенд для боевого сбора непригоден; ветка остаётся для
    отладки профилей. Рабочий транспорт — Chromium через scripts/launch_chrome
    (--remote-debugging-port; --enable-automation не обязателен, см. Session
    в cdp.py).

    Живые замеры 2026-09-25 (один IP, ozon.ru, по одному запросу, gobot 1.0):

      Firefox 156 + geckodriver, профиль ozon-profile       → заглушка 8с
      Firefox 156 + geckodriver, ПУСТОЙ профиль .v2         → заглушка 8с
      Firefox 156 + geckodriver, пустой профиль, без prefs  → заглушка 7с
      Firefox 156 + geckodriver, пустой профиль, OCSP/CRLite→ заглушка 6.6с
        (во всех четырёх navigator.webdriver === true)
      Chromium, launch_chrome.ps1, свежий user-data-dir     → ✅ 19.7с
        (navigator.webdriver === false)
      Chromium + --enable-automation (webdriver=true)       → ✅ 13.6с
      Firefox инкогнито вручную (контроль оператора)         → ✅ открывается

    Что замеры ДОКАЗЫВАЮТ:
      - webdriver-флаг НЕ является причиной: Chromium с webdriver=true
        проходит, Firefox с webdriver=true нет;
      - исключены как причины: профиль (тёплый/пустой), user prefs, locale
        (ru-RU в обоих), геометрия окна/экрана, headless/свёрнуто, блокировка
        ресурсов, человеческие тайминги, OCSP/CRL/CRLite;
      - не IP, не репутация IP, не cookie: тот же IP в инкогнито вручную
        открывается;
      - не локализуемо глубже текущими инструментами: Firefox без marionette
        не скриптуется, поэтому разделить «marionette-уровневый tell» и
        «правило automation+UA Firefox» нечем. Лечится сменой транспорта.

    Значение вне {chrome, firefox} — ValueError: лучше явно упасть на опечатке
    конфига, чем молча работать на другом движке.
    """
    kind = os.environ.get("OZON_BROWSER", DEFAULT_BROWSER_KIND).strip().lower()
    if kind not in BROWSER_KINDS:
        raise ValueError(
            f"OZON_BROWSER: ожидается {'|'.join(BROWSER_KINDS)}, получено {kind!r}")
    return kind


def env_browser_override() -> str | None:
    """Явно заданный OZON_BROWSER (None = дефолт, пользователь не выбирал).

    Нужно, чтобы отличать «chrome, потому что это дефолт» от осознанного
    «OZON_BROWSER=firefox». Для совместимых с движком решений (см.
    resolve_browser_for_parallel) дефолт можно мягко заменять, явный — не трогать.
    """
    return os.environ.get("OZON_BROWSER") or None


def resolve_browser_for_parallel() -> str:
    """Движок для parallel/daemon-режима.

    До 2026-09-24 firefox в parallel блокировался: owner-контракт воркеров
    жил на CDP-портах Chromium (browser_alive через /json/version, restart,
    окна). Firefox не даёт CDP-сетевого конца вообще — поэтому фолбекали на
    chrome. С 2026-09-24 firefox-воркеры в parallel работают БЕЗ CDP: каждый
    воркер = отдельный процесс со своим stock Firefox + geckodriver/Selenium
    (см. cdp.Session._connect_firefox), живость оркестратор оценивает по
    процессу воркера + heartbeat (не по CDP), окна не двигает (нативная
    геометрия).

    С 2026-09-25 дефолт = chrome: Ozon отдаёт заглушку ЛЮБОЙ сессии
    geckodriver/Marionette, при этом Chromium CDP с тем же webdriver=true
    проходит (см. browser_kind). Firefox-ветка остаётся для отладки, но
    боевой parallel/daemon должен идти на chrome.
    """
    return browser_kind()



class LaunchRun(NamedTuple):
    """Результат запуска лаунчера: путь лога + процесс (для fail-fast)."""

    path: str
    proc: subprocess.Popen


def _apply_worker_proxy_env(env: dict, profile_token: str | None) -> None:
    """PROXY env для per-worker прокси (net.py): 1 профиль = 1 IP.

    Общий хелпер launch-путей (browser._launch_script — recycle через
    BrowserProcessManager, и cdp._launch_browser — первичный запуск): раньше
    блок жил только в cdp, и recycle воркера поднимал его БЕЗ прокси, нарушая
    изоляцию 1 профиль = 1 IP (аудит 2026-10-15). Дефолт — OFF: нет env →
    нет переменной PROXY в лаунчере → Chromium идёт напрямую.
    """
    from . import net
    k = net.worker_index_from_profile_token(profile_token)
    if not k:
        return
    proxy = net.proxy_url_for_worker(k)
    if not proxy:
        return
    try:
        net.chrome_proxy_arg(proxy)
    except ValueError as e:
        log.warning(f"[launch] worker-{k}: некорректный прокси "
                    f"({type(e).__name__}: {e}) — запуск без прокси")
        return
    env["PROXY"] = proxy
    log.info(f"[launch] worker-{k}: прокси {net.mask_proxy(proxy)}"
             + (" [ creds в URL: Chrome применит]"
                if net.has_credentials(proxy)
                and not proxy.lower().startswith("socks5")
                else " [ creds в socks5-URL Chrome игнорирует — нужен PAC]"
                if net.has_credentials(proxy) else ""))


def _launch_script(cdp_url: str, profile_path: str | None = None,
                   profile_token: str | None = None,
                   script_path: Path | None = None,
                   script_name: str | None = None) -> LaunchRun:
    """Запуск живого Chromium через scripts/launch_chrome (порт из cdp_url).

    profile_path — полный путь --user-data-dir (для воркеров передаёт PROFILE
    env в launch_chrome.ps1/.sh; None = дефолтный профиль скрипта).
    profile_token — токен профиля воркера: задаёт per-worker PROXY (1 профиль =
    1 IP), тот же контракт, что в cdp._launch_browser.
    CHROME env (из OZON_CHROME_PATH или уже заданной CHROME) выбирает
    исполняемый файл браузера: системный Chrome по умолчанию, либо автономный
    Chrome-for-Testing/Edge (см. scripts/setup_bundled_chrome.py).
    script_path/script_name — injection point for the cdp._launch_browser
    alias: it passes ITS helpers so monkeypatched cdp._launch_script_* names
    keep working in tests (None → this module's own helpers).
    """
    script = script_path if script_path is not None else _launch_script_path()
    name = script_name if script_name is not None else _launch_script_name()
    if not script.exists():
        hint = ("powershell -ExecutionPolicy Bypass -File "
                "scripts/launch_chrome.ps1" if os.name == "nt"
                else "sh scripts/launch_chrome.sh")
        raise RuntimeError(
            f"{name} не найден: {script}\n"
            f"Запустите браузер вручную: {hint}")
    env = dict(os.environ)
    env["PORT"] = str(P.cdp_port(cdp_url))
    if profile_path:
        env["PROFILE"] = profile_path
    chrome = _chrome_override()
    if chrome:
        env["CHROME"] = chrome
    _apply_worker_proxy_env(env, profile_token)
    cmd = (["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)]
           if os.name == "nt" else [str(script)])
    # Вывод лаунчера пишем в temp-файл, а не в DEVNULL: если Chromium не
    # поднялся, ProcessManager.launch печатает этот вывод в лог (ревизия 2.9).
    logf = _LL.launcher_log_path(P.cdp_port(cdp_url))
    try:
        with open(logf, "wb") as fh:     # truncate: лог всегда от THIS запуска
            # CREATE_NO_WINDOW (Windows): the parent has no console (daemon
            # started from a shortcut/scheduler) — without the flag Windows
            # would open a new terminal window for the launcher on every
            # collection start. Same approach as the tray
            # (key_daemon._start_tray_companion); cdp._launch_browser delegates
            # here since audit 2026-10-18 (D1), so this is the only Popen.
            kw = {}
            if os.name == "nt":
                kw["creationflags"] = getattr(
                    subprocess, "CREATE_NO_WINDOW", 0x08000000)
            proc = subprocess.Popen(cmd, env=env,
                                    stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=subprocess.STDOUT, **kw)
    except OSError as e:
        raise RuntimeError(f"не удалось запустить {name}: "
                           f"{type(e).__name__}: {e}") from e
    return LaunchRun(logf, proc)


def _launch_log_tail(path: str | None, limit: int = 12) -> str:
    """Хвост вывода лаунчера (диагностика «CDP не поднялся»)."""
    return _LL.read_log_tail(path, limit)


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
    окончательный вердикт даёт classify_error (контрольная проба
    wait_for_function с таймаутом).
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
        return self.error_kind in ("browser", "renderer_failure",
                                   "access_challenge", "variant_short")

    @property
    def failure_type(self) -> str:
        """Тип сбоя для разделения product/session/browser/access (ADR §14.1).

        product_failure — проблема товара (структура/variant_short): штрафует
          PID (rescan/retry товара).
        browser_failure — краш renderer/CDP: НЕ штрафует PID (recycle браузера).
        access_failure — challenge/капча: НЕ штрафует PID, идёт по FSM-лестнице.
        unknown — не классифицировано.
        """
        if self.error_kind in ("access_challenge",):
            return "access_failure"
        if self.error_kind in ("browser", "renderer_failure", "target"):
            return "browser_failure"
        if self.error_kind in ("structure", "variant_short"):
            return "product_failure"
        return "unknown"


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

    def os_process_alive(self, cdp_url: str, profile_token: str | None = None,
                         kind: str | None = None) -> bool:
        """Жив ли процесс браузера на (порт, профиль) — живой OS-запрос.

        Единственный источник истины для «браузер мёртв»:
          * снимок `sess.browser_pid` протухает (рестарт manager'ом без reconnect)
            и его нет у external-браузера / Firefox-private / при гонке перечисления
            сразу после launch → probe не может отличить смерть от молчания CDP;
          * CDP HTTP молчит и у ЖИВОГО браузера (transient), поэтому один только
            HTTP-сигнал death не доказывает (та же логика, что в watchdog).

        fail-safe: не смогли перечислить процессы → считаем живыми, чтобы никогда
        не объявить смерть по недостоверному сигналу.
        """
        token = profile_token or P.PROFILE_TOKEN
        if (kind or "chrome") == "firefox":
            try:
                return bool(P.firefox_browser_processes(token))
            except Exception:
                return True
        try:
            return bool(P.browser_processes(cdp_url, profile_token=token))
        except Exception:
            return True

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
        """Health probe. Возвращает (ProbeState, http_attempts).

        Порядок сигналов: снимок процесса (бесплатно) → CDP HTTP → живой
        OS-запрос. Смерть браузера выводится ТОЛЬКО когда нет процесса на
        (порт, профиль) И нет нашего инстанса в снимке, то есть когда доказуемо
        не просто «CDP молчит», а «процесса нет».

        `browser_owner == "external"` (ADR §14.1): профиль не наш, в перечислении
        его процесса не будет НИКОГДА, поэтому для чужого браузера смерть
        недоказуема — судим только по молчанию CDP (BROWSER_UNHEALTHY).
        """
        ident = getattr(sess, "browser_pid", None)
        ident_alive = ident is not None and P.identity_alive(ident)
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
            owner = getattr(sess, "browser_owner", "collector")
            if owner != "external" and not ident_alive and not \
                    self.os_process_alive(sess.cdp_url,
                                          getattr(sess, "profile_token", None),
                                          getattr(sess, "kind", "chrome")):
                log.warning("[probe] CDP молчит и процесса браузера на "
                            "(порт, профиль) нет → BROWSER_DEAD",
                            extra={"cdp": sess.cdp_url,
                                   "snapshot_pid": getattr(ident, "pid", None),
                                   "owner": owner})
                return ProbeState.BROWSER_DEAD, attempts
            # процесс жив (или владение недоказуемо) → CDP-молчание транзиентно
            return ProbeState.BROWSER_UNHEALTHY, attempts
        state = self.page_target_state(sess)
        if state == "gone":
            return (ProbeState.RENDERER_FAILURE if signature
                    else ProbeState.TARGET_LOST), attempts
        if state == "alive":
            return ProbeState.HEALTHY, attempts
        return ProbeState.UNKNOWN, attempts


HANG_PROBE_TIMEOUT_MS = 10000
HANG_PROBE_POLL_MS = 100


def _classify_hang(sess, signature: bool) -> Classification:
    """TimeoutError: операция не вернулась за таймаут (подозрение на hang).

    Отличить «долго, но живо» от «renderer завис» контрольным пробником
    на живой странице: если страница отвечает — это временная
    задержка (unknown, НЕ recyclable); если пробник не отвечает за
    HANG_PROBE_TIMEOUT_MS или кидает renderer-ошибку — renderer завис/упал
    (recyclable).

    Проба ОБЯЗАНА быть с таймаутом: у `page.evaluate` в Playwright таймаута
    нет, и на зависшем renderer'е он блокирует главный поток навсегда
    (живой случай 2026-11-06: 38 минут, Ctrl+C не срабатывал).
    `wait_for_function` ограничен драйвером; polling не «raf», иначе на
    фоновой/скрытой вкладке кадры не приходят и проба врёт.
    """
    page = getattr(sess, "page", None)
    probe = getattr(page, "wait_for_function", None) if page is not None else None
    if probe is None:
        # Нет ограниченного API — лучше «не знаем», чем вечный evaluate:
        # решение всё равно recyclable только при доказанном hang.
        return Classification("unknown", signature=signature)
    try:
        # JSHandle намеренно не dispose: dispose — протокольный вызов без
        # таймаута, то есть ещё один шанс навсегда встать на зависшем
        # renderer'е. Один boolean-handle освобождается с контекстом страницы.
        probe("() => true", timeout=HANG_PROBE_TIMEOUT_MS,
              polling=HANG_PROBE_POLL_MS)
    except Exception:
        return Classification("renderer_failure", probe_state="renderer_hang",
                              signature=signature)
    return Classification("unknown", signature=signature)


def classify_error(exc, sess=None) -> Classification:
    """Классифицирует исключение сбора товара -> Classification.

    sess — сессия с cdp_url/browser_pid/page; None -> «не знаем» (unknown).
    error_kind — источник для policy; probe_state/probe_attempts — телеметрия.
    """
    signature = has_renderer_signature(str(exc))
    if isinstance(exc, BrowserRecoveryError):
        return Classification("browser", probe_state="watchdog",
                              signature=signature)
    if isinstance(exc, AccessChallengeError):
        return Classification("access_challenge", signature=signature)
    if isinstance(exc, StructureError):
        return Classification("structure", signature=signature)
    if isinstance(exc, VariantShortError):
        return Classification("variant_short", signature=signature)
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
                 grace_s: float = 4.0, probe: BrowserProbe | None = None,
                 profile_token: str = P.PROFILE_TOKEN,
                 kind: str | None = None):
        self.cdp_url = cdp_url
        self.profile_token = profile_token
        # None = движок из env (browser_kind); явный kind имеет приоритет.
        self.kind = kind or browser_kind()
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

        Firefox (kind='firefox'): CDP HTTP не существует — живость = есть ли
        firefox-процесс на нашем профиле (geckodriver/Selenium).

        Живой OS-запрос общий с BrowserProbe.probe (os_process_alive) — одна
        реализация «процесса на (порт, профиль) нет», чтобы probe и watchdog
        не разошлись в критерии смерти.
        """
        if self.kind == "firefox":
            # CDP у firefox не существует: не тратим http_timeout на не-CDP порт
            return self.probe.os_process_alive(self.cdp_url, self.profile_token,
                                               self.kind)
        if self.probe.cdp_http_ok(self.cdp_url):
            return True
        return self.probe.os_process_alive(self.cdp_url, self.profile_token,
                                            self.kind)


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
                 profile_token: str = P.PROFILE_TOKEN,
                 launch_wait_s: float = 8.0, kill_wait_s: float = 3.0,
                 capture_interval: float = 0.2):
        self.cdp_url = cdp_url
        self.profile_token = profile_token
        self.launch_wait_s = launch_wait_s
        self.kill_wait_s = kill_wait_s
        self.capture_interval = capture_interval
        self.profile_path = P.profile_path(profile_token)

    def _matching(self) -> list:
        """Matching-процессы на нашем (порт, profile_token)."""
        return P.browser_processes(self.cdp_url,
                                   profile_token=self.profile_token)

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
        run = _launch_script(self.cdp_url, profile_path=self.profile_path,
                             profile_token=self.profile_token)
        if run is not None:
            # Recycle path (restart -> launch) used to leave the new pid
            # unguarded until _await_launch returned: the window could flash
            # in the gap (audit 2026-10-19). Hand it to the early watcher
            # right now; late import — cdp imports browser at module level.
            from . import cdp as _cdp
            _cdp._watch_launcher_window(self.cdp_url, run)
        return self._await_launch(launch_id, wait_s, run)

    def _await_launch(self, launch_id: str, wait_s: float,
                      run: "LaunchRun | None") -> "ProcessRecord | None":
        logf = getattr(run, "path", run)
        record = None
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if record is None:
                matches = self._matching()
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
            if run is not None and run.proc.poll() is not None:
                break       # лаунчер завершился: он уже знает результат
            time.sleep(self.capture_interval)
        if record is None:
            matches = self._matching()
            if matches:
                if len(matches) > 1:
                    raise BrowserOwnershipError(
                        f"два и более matching процесса на {self.cdp_url}: "
                        f"{matches} (launch_id={launch_id})")
                record = P.ProcessRecord(launch_id, matches[0])
                record.set_lifecycle(P.Lifecycle.ALIVE)
        if record is not None:
            record.partial = record.lifecycle != P.Lifecycle.CDP_READY
        if record is None or record.partial:
            code = run.proc.poll() if run is not None else None
            exit_note = ("лаунчер ещё работает" if code is None
                         else f"код возврата лаунчера={code}")
            log.warning(
                f"[browser] CDP не готов после {wait_s:.0f}с на {self.cdp_url} "
                f"(process={'нет' if record is None else record.lifecycle}; "
                f"{exit_note})"
                + f"\nвывод {_launch_script_name()} ({logf}):"
                + _launch_log_tail(logf))
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
        matching = self._matching()
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
        matching = self._matching()
        owned_ids = {rec.identity for rec in owned}
        for ident in matching:
            if ident not in owned_ids:
                raise BrowserOwnershipError(
                    f"чужой/неизвестный Chromium на {self.cdp_url}: {ident}; "
                    "ownership violation — требуется ручное вмешательство")
        P.kill_records(owned, wait_s=wait_s)
