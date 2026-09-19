"""Сбор данных с поисковой выдачи Ozon: ссылки карточек → gather → digest.

search — тонкий оркестратор поверх существующего пайплайна (gather.run_gather):
извлекает ссылки товаров из сетки результатов выдачи (/search/ или /category/),
дедуплицирует по pid, собирает каждый товар в ОДНОЙ CDP-сессии, результат —
структурированный combined-объект для render_search_digest и --out-raw.

Выдача докручивается прокруткой (infiniteVirtualPaginator): товары берутся ТОЛЬКО
из виджета tileGridDesktop; блоки рекомендаций («Вы смотрели» island, «Возможно,
вам понравится» skuGrid) в выборку не попадают.
"""
from __future__ import annotations

import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

from . import VERSION, SCHEMA_VERSION
from . import browser as B
from . import collect as C
from . import log
from . import process as P
from . import puzzle as _puzzle
from .cdp import browser_tabs_count
from .gather import run_gather
from .report import digest_rating, sort_digest_products

SEARCH_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  for (const g of document.querySelectorAll('[data-widget="tileGridDesktop"]')) {
    for (const a of g.querySelectorAll('a[href*="/product/"]')) {
      const href = a.getAttribute('href') || '';
      if (!href.startsWith('/product/') && !/^https?:\/\/(www\.)?ozon\.ru\/product\//.test(href)) {
        continue;
      }
      const h = href.split('?')[0].split('#')[0].replace(/\/+$/, '');
      const m = h.match(/(?:-(\d+)|\/(\d+))$/);
      if (!m) continue;
      const pid = m[1] || m[2];
      if (!pid || pid.length < 5 || seen.has(pid)) continue;
      seen.add(pid);
      out.push({ pid, url: 'https://www.ozon.ru/product/' + pid });
    }
  }
  return out;
}
"""


class CollectionInterrupted(Exception):
    """Кооперативная остановка докрутки выдачи пользователем (Ctrl+C в демоне).

    Наследует Exception и пробрасывается из _collect_links /
    _collect_links_with_recovery вверх до демона. Ключевой маркер — флаг `forced`:

      forced=False (STOP):  первый Ctrl+C — graceful; часть уже найденных ссылок
                           (self.links) НЕ должна теряться, их сохраняем в
                           discovered_pids, сбор воркеров НЕ запускаем.
      forced=True  (FORCE): второй Ctrl+C — экстренный выход; низкий уровень НЕ
                           зовёт os._exit(), а лишь сигнализирует — решение о
                           физическом завершении принимает lifecycle демона.

    Низкоуровневые search.py/parallel.py никогда не вызывают os._exit() — только
    поднимают/пробрасывают это исключение.
    """
    def __init__(self, links=None, forced: bool = False):
        super().__init__("collection interrupted (forced=%s)" % forced)
        self.links = list(links) if links else []
        self.forced = bool(forced)


def search_url(query: str) -> str:
    """URL выдачи по текстовому запросу."""
    q = urllib.parse.quote(query)
    return f"https://www.ozon.ru/search/?text={q}&from_global=true"


# Сколько ждём, пока после челленджа реально отрисуется выдача (товары в
# tileGridDesktop), прежде чем решить, что это подмена, а не настоящая страница.
FEED_APPEAR_TRIES = 6
FEED_APPEAR_WAIT_MS = 1500


def _feed_appeared(sess, tries: int = FEED_APPEAR_TRIES,
                   wait_ms: int = FEED_APPEAR_WAIT_MS) -> bool:
    """True, если в выдаче РЕАЛЬНО есть хоть один товар (tileGridDesktop).

    Проверяет текущую страницу (без скроллов) до tries попыток с паузой —
    даёт JS-рендеру сетки время. False — выдача пустая ИЛИ страница-заглушка.
    """
    for _ in range(max(1, tries)):
        try:
            if sess.eval(SEARCH_JS):
                return True
        except Exception:
            pass
        time.sleep(wait_ms / 1000.0)
    return False


def _collect_links(sess, limit: int, scan_limit: Optional[int] = None,
                   max_scrolls: int = 40, scroll_by: int = 1500,
                   wait_ms: int = 1000, quiet_after: int = 2,
                   stop_event=None, force_event=None) -> list[dict]:
    """Докручивает выдачу (tileGridDesktop), пока прибавляются товары.

    Возвращает ВСЕ найденные уникальные товары (found) — НЕ ограничиваясь
    limit: найденное количество нужно для честного «Найдено товаров в выдаче:
    N». Ограничение (selected = found[:limit]) применяет collect_search.

    Алгоритм на каждой итерации: scroll → wait → eval → dedup. Стоп:
    - набрано scan_limit товаров (по умолчанию max(limit, 50) — чтобы не
      сканировать гигантские каталоги ради счётчика);
    - quiet_after скроллов подряд без роста (товары закончились);
    - упёрлись в max_scrolls (предохранитель).

    seen копим в Python: virtual-лист может размонтировать ушедшие вверх
    карточки, и они не должны потеряться.

    stop_event/force_event (threading.Event, от демона) — кооперативная
    остановка Ctrl+C. Приоритет: FORCE > STOP. На границе каждой итерации:
      stop_event  → CollectionInterrupted(links, forced=False) — graceful,
                    возвращаем частично найденные ссылки, чтобы сохранить их
                    в discovered_pids и не потерять прогресс докрутки;
      force_event → CollectionInterrupted(links, forced=True) — emergency.
    Уже идущий блокирующий CDP-вызов (scroll/eval) не прерывается polling-флагом,
    но задержка ограничена одной итерацией / таймаутом сессии.
    """
    if scan_limit is None:
        scan_limit = max(limit, 50)
    seen = {}
    stagnant = 0
    prev = 0
    for i in range(max_scrolls + 1):
        if force_event is not None and force_event.is_set():
            raise CollectionInterrupted(
                [{"pid": p, "url": u} for p, u in seen.items()], forced=True)
        if stop_event is not None and stop_event.is_set():
            log.info("[stop] прерывание докрутки выдачи (Ctrl+C) — сохраняю "
                     f"частично найденные ссылки ({len(seen)})")
            raise CollectionInterrupted(
                [{"pid": p, "url": u} for p, u in seen.items()], forced=False)
        for item in sess.eval(SEARCH_JS):
            seen.setdefault(item["pid"], item["url"])
        log.debug(f"links iter {i}: seen={len(seen)} stagnant={stagnant}")
        if len(seen) >= scan_limit or i >= max_scrolls:
            break
        if len(seen) == prev:
            stagnant += 1
            if stagnant >= quiet_after:
                break
        else:
            stagnant = 0
        prev = len(seen)
        sess.scroll(scroll_by)
        sess.wait(wait_ms)
    return [{"pid": p, "url": u} for p, u in seen.items()]


def _recycle_browser(sess, manager, launch_id: str):
    """Recovery R1: восстановление сессии браузера по ownership-контракту.

    browser_owner == "external" (браузер запущен НЕ нами): перезапуск чужого
    процесса ЗАПРЕЩЁН (ADR §14.1) — только ensure_single_worker_tab (новая
    managed-вкладка, чужие вкладки не трогаем).
    browser_owner == "collector" (наш процесс): manager.restart убивает только
    matching-процессы (identity-сверка на (порт, профиль)), проверяет отсутствие
    чужих, запускает новый процесс и дожидается CDP; затем сессия reconnect.
    """
    owner = getattr(sess, "browser_owner", "collector")
    if owner == "external":
        log.warning(f"[search] recycle: external-браузер — не перезапускаю, "
                    f"новая вкладка ({launch_id})")
        sess.ensure_single_worker_tab()
        return
    old = [P.ProcessRecord(launch_id, ident)
           for ident in P.browser_processes(
               sess.cdp_url, profile_token=manager.profile_token)]
    manager.restart(old, launch_id)
    sess.reconnect()


# Профилактический recycle (ADR §14.5): recycle ТОЛЬКО по browser stability
# (OOM/краши renderer/CDP/старая сессия), НЕ по challenge (access отдельно).
# RECYCLE_EVERY — safety-ceiling (максимум товаров между recycle); основной
# триггер — накопленные renderer-ошибки (RENDERER_RECYCLE_THRESHOLD).
RECYCLE_EVERY = 40
RENDERER_RECYCLE_THRESHOLD = 2

# Первый recovery для access_challenge — НОВАЯ ВКЛАДКА (эксперимент 2026-08-17):
# FAB залипает на навигации конкретной вкладки (повторы 10с/15с/reload в той же
# вкладке НЕ помогают), а свежая вкладка проходит через Qrator, получает auth-куки
# и разблокирует всю сессию.

# FSM «один challenge-инцидент» (ADR §14.2): лестница НЕ крутится в цикле.
# reload×3 уже внутри collect.check_challenge; здесь только new_tab×1 → recycle×1
# → SESSION_BLOCKED (abort run). После исчерпания — никаких скрытых повторов.
INCIDENT_NEW_TAB_MAX = 1
INCIDENT_RECYCLE_MAX = 1

# Session budget (ADR §14.4): лимиты на ВЕСЬ запуск (run), а не на товар.
# challenge — сколько инцидентов; new_tab/recycle — сколько recovery-действий;
# wall_clock_s — жёсткий таймер сессии. product_retries отдельно (на товар).
# Исчерпание → SESSION_EXHAUSTED → abort run (не продолжать молотить).
SESSION_BUDGET_DEFAULT = {
    "challenge": 3,
    "new_tab": 3,
    "browser_recycle": 2,
    "wall_clock_s": 1800,
}

# Canary (ADR §14.2, этап 4): после recovery-событий доступа проверяем, что
# сессия снова получает нормальную страницу, ПРЕЖДЕ чем тратить реальный PID.
# Фиксированный безопасный URL, только check_challenge + минимальная DOM-проверка.
CANARY_URL = "https://www.ozon.ru/"
CANARY_OK = "ok"
CANARY_CHALLENGE = "challenge"
CANARY_ERROR = "error"


def run_canary(sess) -> str:
    """Canary-проба сессии: возвращает CANARY_OK/CANARY_CHALLENGE/CANARY_ERROR.

    Никогда не пишет raw и не трогает PID/recycle-бюджеты (ADR §14.5):
    canary-fail — системный сбой сессии, отдельный счётчик canary_fails.
    """
    try:
        sess.goto(CANARY_URL, wait_ms=1500)
        page = getattr(sess, "page", None)
        if page is not None:
            C.check_challenge(page, "canary")
        return CANARY_OK
    except B.AccessChallengeError:
        return CANARY_CHALLENGE
    except Exception:
        return CANARY_ERROR

# Порог рейтинга по умолчанию: товары с product.rating < DEFAULT_MIN_RATING
# исключаются из дайджеста (res["excluded"]), но raw остаётся собранным.
DEFAULT_MIN_RATING = 4.7

# SESSION_BLOCKED (ADR §14.2): challenge не снят после лестницы одного
# инцидента → run останавливается, остаток aborted (session_failure), НЕ
# штрафуем PID. Агрессивный retry-loop бесполезен и усугубляет.


def _recovery_meta(sess, ev: dict, duration: float | None = None,
                   products_since_recovery: int | None = None,
                   error: str | None = None) -> dict:
    """Обогащает recovery-событие телеметрией (ADR §14.6, этап 0).

    Добавляет: signature (текст ошибки/сигнатура), last_url (текущая страница),
    browser_tabs (внешний снимок /json/list), collector_tabs (страницы
    коллектора), duration (мс, если передано), products_since_recovery.
    НЕ меняет поведение — только наблюдение. Сбои телеметрии не роняют событие.
    """
    out = dict(ev)
    if error:
        out["signature"] = error[:160]
    try:
        page = getattr(sess, "page", None)
        if page is not None:
            out["last_url"] = page.url
    except Exception:
        pass
    try:
        out["browser_tabs"] = browser_tabs_count(sess.cdp_url)
    except Exception:
        out["browser_tabs"] = None
    try:
        out["collector_tabs"] = sess.collector_tabs()
    except Exception:
        out["collector_tabs"] = None
    if duration is not None:
        out["duration_ms"] = int(duration * 1000)
    if products_since_recovery is not None:
        out["products_since_recovery"] = products_since_recovery
    return out


def _collect_links_with_recovery(sess, manager, url: str, limit: int,
                                 max_recycles: int, recovery: dict,
                                 stop_event=None, force_event=None):
    """Докрутка выдачи с recovery по FSM-лестнице (ADR §14.2).

    Единый контур с _collect_shard: access_challenge → new_tab×1 → recycle×1 →
    raise (SESSION_BLOCKED на уровне вызывающего). Прочие recyclable
    (renderer_failure/browser) → recycle до max_recycles. Зависание renderer'а
    при докрутке не роняет весь search traceback'ом — перезапуск и повтор.
    Возвращает найденные ссылки; после исчерпания — пробрасывает ошибку.
    Телеметрия пишется в recovery (pid='links').

    stop_event/force_event (от демона): на верху каждой итерации цикла И перед
    каждой recovery-операцией (new_tab/_recycle_browser/reconnect) проверяем их,
    чтобы после Ctrl+C НЕ запускать повторный recovery/reconnect/scroll (тот
    самый «recycle→reconnect→новая попытка» цикл из проблемы). CollectionInterrupted
    из _collect_links пробрасывается как есть.
    """
    attempts = 0
    incident_new_tab = INCIDENT_NEW_TAB_MAX
    incident_recycle = INCIDENT_RECYCLE_MAX
    while True:
        if force_event is not None and force_event.is_set():
            raise CollectionInterrupted(forced=True)
        if stop_event is not None and stop_event.is_set():
            log.info("[stop] прерывание докрутки выдачи до повторной попытки "
                     "(Ctrl+C)")
            raise CollectionInterrupted(forced=False)
        attempt_t0 = time.time()
        attempts += 1
        try:
            sess.goto(url)
            if getattr(sess, "page", None) is not None:
                C.check_challenge(sess.page, "search_page")
            # Выдача должна РЕАЛЬНО появиться (tileGridDesktop с товарами), а не
            # «пазл снят, но всё ещё бот-заглушка/подмена». Пустая сетка после
            # челленджа — это НЕ успех: не начинаем сбор товаров.
            if not _feed_appeared(sess):
                try:
                    sig = C.detect_challenge(getattr(sess, "page", None))
                except Exception:
                    sig = None
                if sig:
                    raise B.AccessChallengeError(
                        f"после челленджа выдача не появилась: {sig}")
                log.warning("[search] выдача не отрисовалась (сетка пуста без "
                            "бот-маркеров) — возвращаю 0 товаров")
                return []
            return _collect_links(sess, limit, stop_event=stop_event,
                                  force_event=force_event)
        except Exception as e:
            if isinstance(e, CollectionInterrupted):
                raise
            cls = B.classify_error(e, sess=sess)
            kind = cls.error_kind or "unknown"
            # access_challenge → FSM-лестница (FAB залипает на вкладке):
            if kind == "access_challenge" and incident_new_tab > 0:
                incident_new_tab -= 1
                nt_used = INCIDENT_NEW_TAB_MAX - incident_new_tab
                log.warning(f"[search] докрутка выдачи: access_challenge → "
                            f"новая вкладка ({nt_used}/{INCIDENT_NEW_TAB_MAX})",
                            extra={"pid": "links"})
                recovery["new_tab_attempts"] = (
                    recovery.get("new_tab_attempts", 0) + 1)
                recovery["events"].append(_recovery_meta(
                    sess, {"pid": "links", "attempt": attempts, "kind": "new_tab"},
                    duration=time.time() - attempt_t0))
                try:
                    sess.new_page()
                except Exception as ne:
                    log.error(f"[search] докрутка выдачи: new_tab FAILED: "
                              f"{type(ne).__name__}: {ne}", extra={"pid": "links"})
                    raise
                continue
            if kind == "access_challenge" and incident_recycle > 0:
                incident_recycle -= 1
                rc_used = INCIDENT_RECYCLE_MAX - incident_recycle
                log.warning(f"[search] докрутка выдачи: access_challenge → "
                            f"recycle браузера ({rc_used}/{INCIDENT_RECYCLE_MAX})",
                            extra={"pid": "links"})
                try:
                    _recycle_browser(sess, manager, f"search-links-cr{rc_used}")
                except Exception as re:
                    raise RuntimeError(
                        f"recycle при докрутке выдачи FAILED: {re}") from e
                recovery["recycles"] += 1
                recovery["events"].append(_recovery_meta(
                    sess, {"pid": "links", "attempt": attempts, "kind": "recycle"},
                    duration=time.time() - attempt_t0))
                continue
            if not cls.should_recycle or attempts > max_recycles:
                raise
            kind = cls.error_kind or "unknown"
            log.warning(f"[search] докрутка выдачи: {kind} -> recycle браузера "
                        f"(попытка {attempts}/{max_recycles})",
                        extra={"signature": cls.signature,
                               "probe": cls.probe_state})
            try:
                _recycle_browser(sess, manager, f"search-links-r{attempts}")
            except Exception as re:
                raise RuntimeError(
                    f"recycle при докрутке выдачи FAILED: {re}") from e
            recovery["recycles"] += 1
            recovery["events"].append(_recovery_meta(
                sess, {"pid": "links", "attempt": attempts, "kind": kind},
                duration=time.time() - attempt_t0))


WARMUP_HOME_URL = "https://www.ozon.ru/"
# Пауза после главной и после категории (мс) — Ozon ставит антибот-куки при
# «человеческом» заходе и посте уже на целевую категорию не выдаёт challenge.
WARMUP_HOME_WAIT_MS = 8000
WARMUP_CAT_WAIT_MS = 8000


def _warmup_to_category(sess, cat_url: str) -> None:
    """Тёплый вход в категорию «как человек» в той же вкладке.

    Прямой холодный переход на целевую категорию с пустой сессии провоцирует
    access_challenge (FAB) у Ozon. Чтобы снять его, сначала открываем главную
    ozon.ru (даём 8с на JS-челлендж/антибот-куки), затем в ТОЙ ЖЕ вкладке
    переходим на категорию. Если challenge на главной — НЕ тащим его на первый
    PID: переходим на свежую вкладку (FAB залипает на вкладке), как в FSM."""
    try:
        sess.goto(WARMUP_HOME_URL, wait_ms=WARMUP_HOME_WAIT_MS)
    except Exception as e:
        log.warning(f"[search] warmup главная warn: {type(e).__name__}: {e}")
    try:
        page = getattr(sess, "page", None)
        if page is not None:
            C.check_challenge(page, "warmup_home", reload_on_fab=True)
    except B.AccessChallengeError as e:
        log.warning(f"[search] warmup главная → challenge, новая вкладка: {e}")
        try:
            sess.new_page()
        except Exception as ne:
            log.warning(f"[search] warmup new_tab warn: {type(ne).__name__}: {ne}")
    try:
        sess.goto(cat_url, wait_ms=WARMUP_CAT_WAIT_MS)
    except Exception as e:
        log.warning(f"[search] warmup категория warn: {type(e).__name__}: {e}")


def _collect_shard(sess, manager, items, gather_cfg: dict,
                   max_recycles: int, recovery: dict,
                   on_recovery=None, on_progress=None,
                   label: str = "search") -> list[dict]:
    """Per-product recovery-цикл над списком (index, pid, url) в одной сессии.

    ОДИН код для collect_search (main) и воркеров (parallel._worker_main):
    recovery access_challenge → FSM-лестница одного инцидента (new_tab×1 →
    recycle×1 → SESSION_BLOCKED), прочие recyclable (browser/renderer) → recycle
    через переданный manager (identity-сверка на (порт, профиль)) + preventive
    recycle по browser stability (RECYCLE_EVERY/RENDERER_RECYCLE_THRESHOLD).
    Ошибка отдельного товара не прерывает цикл.

    Возвращает list[dict] результатов (res run_gather + recycle_attempts +
    new_tab_attempts + recovery_kind), строго в порядке items. Телеметрия
    recovery пишется в recovery (recycles/events/new_tab_attempts/
    new_tab_successes); on_recovery — колбэк события (воркер → pipe
    телеметрия), None для main. on_progress — колбэк после каждого товара:
    on_progress(products, n, total) (частичные результаты для периодической
    передачи воркером, None для main).
    """
    products: list[dict] = []
    collected = 0
    since_recycle = 0
    since_recovery = 0
    total = len(items)
    _puzzle.reset()
    budget = recovery.get("session_budget") or SESSION_BUDGET_DEFAULT
    used = recovery.setdefault("session_used", {"challenge": 0, "new_tab": 0,
                                                 "browser_recycle": 0})
    started_ts = recovery.get("session_started_ts", time.time())

    for n, (idx, pid, url) in enumerate(items, 1):
        # жёсткий таймер сессии (ADR §14.4): wall_clock_s исчерпан → abort
        if budget.get("wall_clock_s") and \
                (time.time() - started_ts) >= budget["wall_clock_s"]:
            log.error(f"[{label}] session budget: wall_clock исчерпан → "
                      f"SESSION_EXHAUSTED", extra={"pid": pid})
            for _m, (_i2, pid2, _u2) in enumerate(items[n - 1:], n):
                products.append({
                    "pid": pid2, "ok": False,
                    "error": "aborted: session budget exhausted",
                    "error_kind": "session_exhausted",
                    "failure_type": "session_failure",
                    "raw": None, "markdown": "", "cache": "off",
                    "ocr": "off", "recyclable": False,
                    "recycle_attempts": 0,
                })
            return products
        tag = f"[{label}] товар {n}/{total}"
        log.info(f"{tag}: pid={pid} ({url})", extra={"url": url})
        cfg = dict(gather_cfg)
        cfg["pid"] = pid
        cfg["url"] = url
        attempts = 0
        recycles_done = 0
        incident_new_tab = INCIDENT_NEW_TAB_MAX
        incident_recycle = INCIDENT_RECYCLE_MAX
        session_blocked = False
        access_recovery = False
        last_recovery = None
        while True:
            attempt_t0 = time.time()
            res = run_gather(cfg, sess=sess)
            attempts += 1
            if res["ok"] or not res.get("recyclable"):
                break
            kind = res.get("error_kind") or "unknown"
            bstats = recovery.setdefault("browser_stats", {"renderer_errors": 0})
            astats = recovery.setdefault("access_stats", {"challenges": 0})
            if kind == "renderer_failure" or kind == "browser":
                bstats["renderer_errors"] = bstats.get("renderer_errors", 0) + 1
            if kind == "access_challenge":
                astats["challenges"] = astats.get("challenges", 0) + 1

            # ---- FSM «один challenge-инцидент» (ADR §14.2) ----
            # access_challenge → лестница: reload×3 (внутри check_challenge) →
            # new_tab×1 → recycle×1 → SESSION_BLOCKED (abort run). Лестница НЕ
            # крутится в цикле; после исчерпания нет скрытых повторов.
            if kind == "access_challenge":
                # session budget: инцидент challenge тратит бюджет сессии
                used["challenge"] = used.get("challenge", 0) + 1
                if budget.get("challenge") and \
                        used["challenge"] > budget["challenge"]:
                    res["error_kind"] = "session_exhausted"
                    res["failure_type"] = "session_failure"
                    res["error"] = "session blocked: challenge budget исчерпан"
                    res["recyclable"] = False
                    session_blocked = True
                    log.error(f"{tag}: session budget challenge исчерпан → "
                              f"SESSION_EXHAUSTED", extra={"pid": pid})
                    recovery.setdefault("session_exhausted", 0)
                    recovery["session_exhausted"] += 1
                    break
                if incident_new_tab > 0:
                    used["new_tab"] = used.get("new_tab", 0) + 1
                    if budget.get("new_tab") and \
                            used["new_tab"] > budget["new_tab"]:
                        res["error_kind"] = "session_exhausted"
                        res["failure_type"] = "session_failure"
                        res["error"] = "session blocked: new_tab budget исчерпан"
                        res["recyclable"] = False
                        session_blocked = True
                        log.error(f"{tag}: session budget new_tab исчерпан → "
                                  f"SESSION_EXHAUSTED", extra={"pid": pid})
                        recovery.setdefault("session_exhausted", 0)
                        recovery["session_exhausted"] += 1
                        break
                    incident_new_tab -= 1
                    nt_used = INCIDENT_NEW_TAB_MAX - incident_new_tab
                    last_recovery = "new_tab"
                    access_recovery = True
                    recovery.setdefault("new_tab_successes", 0)
                    recovery["new_tab_attempts"] = (
                        recovery.get("new_tab_attempts", 0) + 1)
                    log.warning(f"{tag}: access_challenge → новая вкладка "
                                f"({nt_used}/{INCIDENT_NEW_TAB_MAX})",
                                extra={"pid": pid})
                    ev = _recovery_meta(
                        sess, {"pid": pid, "attempt": attempts, "kind": "new_tab"},
                        duration=time.time() - attempt_t0,
                        products_since_recovery=since_recovery,
                        error=res.get("error"))
                    recovery["events"].append(ev)
                    since_recovery = 0
                    if on_recovery is not None:
                        on_recovery(ev)
                    try:
                        sess.new_page()
                    except Exception as e:
                        log.error(f"{tag}: new_tab FAILED: "
                                  f"{type(e).__name__}: {e}", extra={"pid": pid})
                        res["error"] = f"{res['error']}; new_tab_failed: {e}"
                        res["recyclable"] = False
                        break
                    continue
                if incident_recycle > 0:
                    used["browser_recycle"] = used.get("browser_recycle", 0) + 1
                    if budget.get("browser_recycle") and \
                            used["browser_recycle"] > budget["browser_recycle"]:
                        res["error_kind"] = "session_exhausted"
                        res["failure_type"] = "session_failure"
                        res["error"] = "session blocked: recycle budget исчерпан"
                        res["recyclable"] = False
                        session_blocked = True
                        log.error(f"{tag}: session budget recycle исчерпан → "
                                  f"SESSION_EXHAUSTED", extra={"pid": pid})
                        recovery.setdefault("session_exhausted", 0)
                        recovery["session_exhausted"] += 1
                        break
                    incident_recycle -= 1
                    rc_used = INCIDENT_RECYCLE_MAX - incident_recycle
                    last_recovery = "recycle"
                    access_recovery = True
                    log.warning(f"{tag}: access_challenge → recycle браузера "
                                f"({rc_used}/{INCIDENT_RECYCLE_MAX})",
                                extra={"pid": pid})
                    try:
                        _recycle_browser(sess, manager,
                                         f"{label}-{pid}-challenge-r{rc_used}")
                    except Exception as e:
                        log.error(f"{tag}: recycle FAILED: "
                                  f"{type(e).__name__}: {e}", extra={"pid": pid})
                        res["error"] = f"{res['error']}; recycle_failed: {e}"
                        res["recyclable"] = False
                        break
                    recovery["recycles"] += 1
                    ev = _recovery_meta(
                        sess, {"pid": pid, "attempt": attempts, "kind": "recycle"},
                        duration=time.time() - attempt_t0,
                        products_since_recovery=since_recovery,
                        error=res.get("error"))
                    recovery["events"].append(ev)
                    since_recovery = 0
                    if on_recovery is not None:
                        on_recovery(ev)
                    continue
                # лестница исчерпана → SESSION_BLOCKED (session_failure, не product)
                res["error_kind"] = "session_blocked"
                res["failure_type"] = "session_failure"
                res["error"] = "session blocked: challenge не снят после лестницы"
                res["recyclable"] = False
                session_blocked = True
                log.error(f"{tag}: challenge не снят после лестницы → "
                          f"SESSION_BLOCKED (abort run)", extra={"pid": pid})
                recovery.setdefault("session_blocked", 0)
                recovery["session_blocked"] += 1
                if _puzzle.failures():
                    # пазл решался и не снят — помечаем шторм как puzzle_persistent:
                    # родитель (parallel) вернёт combined.aborted с all_workers/
                    # workers_stormed, демон отличит нерешаемый пазл от «голого»
                    # challenge (перезапуск/cooldown-логика строится по признаку).
                    recovery.setdefault("challenge_storm", {})
                    recovery["challenge_storm"]["puzzle_persistent"] = True
                    recovery["challenge_storm"]["puzzle_failures"] = \
                        _puzzle.failures()
                    recovery["challenge_storm"]["streak"] = \
                        recovery.get("session_blocked", 0)
                break

            # ---- прочие recyclable (browser/renderer/variant_short) ----
            # browser_failure → recycle (R1/R2) по max_recycles; НЕ трогает
            # FSM-лестницу challenge.
            if recycles_done >= max_recycles:
                break
            used["browser_recycle"] = used.get("browser_recycle", 0) + 1
            if budget.get("browser_recycle") and \
                    used["browser_recycle"] > budget["browser_recycle"]:
                res["error_kind"] = "session_exhausted"
                res["failure_type"] = "session_failure"
                res["error"] = "session blocked: recycle budget исчерпан"
                res["recyclable"] = False
                session_blocked = True
                log.error(f"{tag}: session budget recycle исчерпан → "
                          f"SESSION_EXHAUSTED", extra={"pid": pid})
                recovery.setdefault("session_exhausted", 0)
                recovery["session_exhausted"] += 1
                break
            recycles_done += 1
            last_recovery = "recycle"
            log.warning(f"{tag}: {kind} → recycle браузера "
                        f"(попытка {recycles_done}/{max_recycles})",
                        extra={"pid": pid, "probe": res.get("error_probe_state"),
                               "signature": res.get("error_signature")})
            try:
                _recycle_browser(sess, manager, f"{label}-{pid}-r{recycles_done}")
            except Exception as e:
                log.error(f"{tag}: recycle FAILED: {type(e).__name__}: {e}",
                      extra={"pid": pid})
                res["error"] = f"{res['error']}; recycle_failed: {e}"
                res["recyclable"] = False
                break
            recovery["recycles"] += 1
            ev = _recovery_meta(
                sess, {"pid": pid, "attempt": attempts, "kind": kind},
                duration=time.time() - attempt_t0,
                products_since_recovery=since_recovery,
                error=res.get("error"))
            recovery["events"].append(ev)
            since_recovery = 0
            if on_recovery is not None:
                on_recovery(ev)
        res["recycle_attempts"] = attempts
        nt_used = INCIDENT_NEW_TAB_MAX - incident_new_tab
        res["new_tab_attempts"] = nt_used
        if last_recovery is not None:
            res["recovery_kind"] = last_recovery
        if res["ok"] and last_recovery == "new_tab":
            recovery["new_tab_successes"] = (
                recovery.get("new_tab_successes", 0) + 1)
        # observed_scope (ADR §14.3): наблюдение, не вывод о блоке
        if session_blocked:
            res["observed_scope"] = "profile_observed"
        elif res["ok"] and last_recovery == "new_tab":
            res["observed_scope"] = "tab_observed"
        elif res["ok"] and last_recovery == "recycle":
            res["observed_scope"] = "browser_observed"
        elif last_recovery is not None:
            res["observed_scope"] = "unknown"
        products.append(res)
        if session_blocked:
            # SESSION_BLOCKED / SESSION_EXHAUSTED → abort run: остаток aborted
            # (session_failure), НЕ штрафуем PID (rescan не делаем).
            abort_kind = (res.get("error_kind")
                          if res.get("error_kind") == "session_exhausted"
                          else "session_blocked")
            abort_msg = ("aborted: session blocked (cooldown)"
                         if abort_kind == "session_blocked"
                         else "aborted: session budget exhausted")
            for _m, (_i2, pid2, _u2) in enumerate(items[n:], n + 1):
                products.append({
                    "pid": pid2, "ok": False,
                    "error": abort_msg,
                    "error_kind": abort_kind,
                    "failure_type": "session_failure",
                    "raw": None, "markdown": "", "cache": "off",
                    "ocr": "off", "recyclable": False,
                    "recycle_attempts": 0,
                })
            break
        mr = gather_cfg.get("min_rating")
        if res["ok"] and mr is not None:
            rating, _n, _src = digest_rating(
                (res.get("raw") or {}).get("product") or {},
                (res.get("raw") or {}).get("reviews"))
            if rating is not None and rating < mr:
                res["excluded"] = {
                    "reason": "rating_below",
                    "rating": rating,
                    "min_rating": mr,
                }
        if res["ok"]:
            collected += 1
            n_rev = len((res.get("raw") or {}).get("reviews", []))
            excl = ""
            if res.get("excluded"):
                e = res["excluded"]
                excl = f" · исключён по рейтингу {e['rating']} < {e['min_rating']}"
            log.info(f"{tag}: ok · cache={res['cache']} · ocr={res['ocr']} "
                     f"· отзывов {n_rev}{excl}",
                     extra={"pid": pid})
        else:
            log.warning(f"{tag}: fail · {res['error']}",
                        extra={"pid": pid})
# Canary после RECYCLE по доступу (ADR §14.2, этап 4): новый браузер — чистота
        # сессии НЕ доказана, проверяем перед следующим PID. После new_tab challenge
        # уже снят на товарном URL и товар собран (check_challenge прошёл) — canary
        # избыточен (не трогаем Qrator лишний раз). НЕ после профилактического recycle.
        if sess is not None and res["ok"] and last_recovery == "recycle" and access_recovery:
            canary = run_canary(sess)
            if canary != CANARY_OK:
                canary_fails = recovery.setdefault("canary_fails", 0) + 1
                recovery["canary_fails"] = canary_fails
                log.error(f"{tag}: canary={canary} после recovery → "
                          f"SESSION_BLOCKED (abort run)", extra={"pid": pid})
                # Текущий товар УЖЕ собран валидно (acceptance-gate пройден) —
                # НЕ перезаписываем его (ADR §14.1: session_failure не штрафует
                # PID). Останавливаем только ДАЛЬНЕЙШИЙ сбор.
                for _m, (_i2, pid2, _u2) in enumerate(items[n:], n + 1):
                    products.append({
                        "pid": pid2, "ok": False,
                        "error": "aborted: canary challenge (cooldown)",
                        "error_kind": "session_blocked",
                        "failure_type": "session_failure",
                        "raw": None, "markdown": "", "cache": "off",
                        "ocr": "off", "recyclable": False,
                        "recycle_attempts": 0,
                    })
                recovery.setdefault("session_blocked", 0)
                recovery["session_blocked"] += 1
                break
        since_recycle += 1
        since_recovery += 1
        # Recycle по browser stability (ADR §14.5): renderer-ошибки (OOM/краши)
        # или safety-ceiling RECYCLE_EVERY. НЕ по challenge (access отдельно).
        bstats = recovery.setdefault("browser_stats", {"renderer_errors": 0})
        if sess is not None and (
                since_recycle >= RECYCLE_EVERY or
                bstats.get("renderer_errors", 0) >= RENDERER_RECYCLE_THRESHOLD):
            try:
                _recycle_browser(sess, manager, f"{label}-preventive-{n}")
            except Exception as e:
                log.error(f"{tag}: preventive recycle FAILED: "
                          f"{type(e).__name__}: {e}", extra={"pid": pid})
            else:
                since_recycle = 0
                bstats["renderer_errors"] = 0   # свежий браузер — чистые метрики
                recovery["browser_stats"] = bstats
                recovery["recycles"] += 1
                ev = _recovery_meta(
                    sess, {"pid": pid, "attempt": 0, "kind": "preventive"},
                    products_since_recovery=since_recovery)
                recovery["events"].append(ev)
                since_recovery = 0
                if on_recovery is not None:
                    on_recovery(ev)
                log.info(f"preventive recycle (stability) после {n} товаров",
                         extra={"pid": pid})
        if on_progress is not None:
            try:
                on_progress(products, n, total)
            except Exception as e:
                log.warning(f"on_progress warn: {type(e).__name__}: {e}")
        pause_s = gather_cfg.get("pause_s") or 0
        if pause_s > 0 and n < total:
            time.sleep(pause_s)
    return products


def _links_from_pids(pids: list) -> list:
    """Ссылки-товары из явного списка артикулов (общий для collect_search
    и collect_parallel)."""
    return [{"pid": str(p), "url": f"https://www.ozon.ru/product/{p}"}
            for p in pids]


def _filter_skip_pids(links: list, skip_pids: list | None) -> list:
    """Отфильтровать уже обработанные артикулы (не пересобирать заново).

    Общий для collect_search и collect_parallel. Возвращает links без ссылок
    на skip_pids (с логом о числе пропущенных).
    """
    if not skip_pids:
        return links
    skip = set(str(p) for p in skip_pids)
    before = len(links)
    out = [it for it in links if str(it["pid"]) not in skip]
    if len(out) < before:
        log.info(f"[search] skip_pids: пропущено {before - len(out)} "
                 f"уже обработанных (осталось {len(out)})")
    return out


def collect_search(sess, url: str, limit: int, gather_cfg: dict,
                   query: Optional[str] = None,
                   max_recycles: int = 2,
                   pids: Optional[list] = None,
                   skip_pids: Optional[list] = None,
                   session_budget: Optional[dict] = None) -> dict:
    """Собирает до limit товаров из выдачи url в одной CDP-сессии.

    gather_cfg — базовый конфиг для run_gather (pid/url подставляются на
    каждый товар). Ошибка отдельного товара не прерывает цикл.

    pids — явный список артикулов вместо докрутки выдачи: выдача не
    сканируется, товары берутся ТОЛЬКО из списка (found = len(pids), url
    игнорируется). Режим для «собрать/обновить конкретные товары».

    skip_pids — список уже обработанных артикулов, которые НАДО пропустить
    в full-режиме (не пересобирать): выдача сканируется, но ссылки на эти
    PID исключаются. Совместно с pids не используется (pids — явный режим).

    Recovery (ADR §14.2): access_challenge → FSM «один инцидент»: reload×3
    (внутри check_challenge) → new_tab×1 → recycle×1 → SESSION_BLOCKED (abort
    run). Лестница не крутится в цикле. Прочие recyclable-ошибки (browser /
    renderer_failure) → recycle (R1/R2) до max_recycles. Телеметрия recovery
    пишется в combined["recovery"] (recycles/events/new_tab_attempts/...).

    Возвращает combined-объект (схема 2.0, аддитивно):
      search{url,query}, found, limit, collected,
      recovery{recycles, events[]},
      products[{pid,ok,error,error_kind,error_probe_state,recycle_attempts,
                new_tab_attempts,recovery_kind,...}]
    """
    recovery = {"recycles": 0, "events": [],
                "session_budget": dict(session_budget or SESSION_BUDGET_DEFAULT),
                "session_used": {"challenge": 0, "new_tab": 0,
                                 "browser_recycle": 0},
                "session_started_ts": time.time(),
                "browser_stats": {"renderer_errors": 0},
                "access_stats": {"challenges": 0},
                "browser_tabs": browser_tabs_count(
                    getattr(sess, "cdp_url", "http://127.0.0.1:9222"))}
    manager = B.BrowserProcessManager(
        cdp_url=getattr(sess, "cdp_url", "http://127.0.0.1:9222"))
    if pids:
        links = _links_from_pids(pids)
    else:
        _warmup_to_category(sess, url)
        links = _collect_links_with_recovery(
            sess, manager, url, limit, max_recycles, recovery)

    found = len(links)
    # skip_pids: уже обработанные артикулы пропускаем (не пересобираем заново).
    # Позволяет full-run добрать только новые/FAIL товары, не пролистывая
    # уже собранные с cache=hit.
    links = _filter_skip_pids(links, skip_pids)
    selected = links[:limit]
    items = [(i, it["pid"], it["url"]) for i, it in enumerate(selected)]
    total = len(items)
    log.info(f"[search] найдено {found} товаров в выдаче, собираю {total}")

    products = _collect_shard(sess, manager, items, gather_cfg,
                              max_recycles, recovery, label="search")
    sort_digest_products(products)
    collected = sum(1 for p in products
                    if p["ok"] and not p.get("excluded"))
    excluded = sum(1 for p in products if p.get("excluded"))

    combined = {
        "schema_version": SCHEMA_VERSION,
        "collector_version": VERSION,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "search": {"url": url, "query": query},
        "found": found,
        "limit": limit,
        "collected": collected,
        "excluded": excluded,
        "recovery": recovery,
        "products": products,
    }
    mr = gather_cfg.get("min_rating")
    if mr is not None:
        combined["min_rating"] = mr
    astats = recovery.get("access_stats") or {}
    combined["kpi"] = {
        "total_challenges": astats.get("challenges", 0),
        "total_recoveries": (recovery.get("recycles", 0)
                             + recovery.get("new_tab_attempts", 0)),
        "products_since_recovery": recovery.get("products_since_recovery"),
        "success_rate": (round(collected / total, 3) if total else None),
    }
    fp = getattr(sess, "fingerprint", None)
    if fp:
        combined["fingerprint"] = fp
    if recovery.get("session_blocked") or recovery.get("session_exhausted"):
        combined["aborted"] = {
            "reason": ("session_blocked" if recovery.get("session_blocked")
                       else "session_exhausted"),
            "count": recovery.get("session_blocked")
                     or recovery.get("session_exhausted"),
        }
    return combined
