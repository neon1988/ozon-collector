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

import urllib.parse
from datetime import datetime, timezone
from typing import Optional

from . import VERSION, SCHEMA_VERSION
from . import browser as B
from . import collect as C
from . import log
from . import process as P
from .gather import run_gather

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


def search_url(query: str) -> str:
    """URL выдачи по текстовому запросу."""
    q = urllib.parse.quote(query)
    return f"https://www.ozon.ru/search/?text={q}&from_global=true"


def _collect_links(sess, limit: int, scan_limit: Optional[int] = None,
                   max_scrolls: int = 40, scroll_by: int = 1500,
                   wait_ms: int = 1000, quiet_after: int = 2) -> list[dict]:
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
    """
    if scan_limit is None:
        scan_limit = max(limit, 50)
    seen = {}
    stagnant = 0
    prev = 0
    for i in range(max_scrolls + 1):
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
    """Recovery R1: перезапуск Chromium + переподключение CDP-сессии.

    manager — BrowserProcessManager (main или воркера): restart убивает только
    matching-процессы (identity-сверка на (порт, профиль) менеджера), проверяет
    отсутствие чужих, запускает новый процесс и дожидается CDP. Затем сессия
    переподключается к новому браузеру.
    """
    old = [P.ProcessRecord(launch_id, ident)
           for ident in P.browser_processes(
               sess.cdp_url, profile_token=manager.profile_token)]
    manager.restart(old, launch_id)
    sess.reconnect()


# Профилактический recycle: каждые RECYCLE_EVERY обработанных товаров браузер
# перезапускается принудительно, чтобы renderer не дорастал до OOM (длинный
# search держит десятки страниц в одной сессии, JS heap копится).
RECYCLE_EVERY = 40

# Первый recovery для access_challenge — НОВАЯ ВКЛАДКА (эксперимент 2026-08-17):
# FAB залипает на навигации конкретной вкладки (повторы 10с/15с/reload в той же
# вкладке НЕ помогают), а свежая вкладка проходит через Qrator, получает auth-куки
# и разблокирует всю сессию. Максимум NEW_TAB_ATTEMPTS попыток на товар, затем —
# существующий recycle (fallback, см. _collect_shard). Storm-лимит не меняется.
NEW_TAB_ATTEMPTS = 2

# Порог рейтинга по умолчанию: товары с product.rating < DEFAULT_MIN_RATING
# исключаются из дайджеста (res["excluded"]), но raw остаётся собранным.
DEFAULT_MIN_RATING = 4.7

# Challenge-шторм: если CHALLENGE_STORM_LIMIT товаров ПОДРЯД закончились
# access_challenge (после своих retry/recycle) — Ozon душит аккаунт/IP.
# Агрессивный retry-loop бесполезен и усугубляет: шард останавливается,
# остаток помечается aborted, в recovery пишется challenge_storm → combined
# получает aborted (CLI советует cooldown).
CHALLENGE_STORM_LIMIT = 3


def _collect_links_with_recovery(sess, manager, url: str, limit: int,
                                 max_recycles: int, recovery: dict):
    """Докрутка выдачи с recovery: goto + _collect_links, при recyclable-ошибке
    браузер перезапускается (R1) и докрутка повторяется (до max_recycles).

    Зависание renderer'а при докрутке выдачи (TimeoutError) не должно ронять
    весь search traceback'ом — только перезапустить браузер и попробовать снова.
    Возвращает найденные ссылки; после исчерпания попыток — пробрасывает
    последнюю ошибку. Телеметрия пишется в recovery (pid='links')."""
    attempts = 0
    while True:
        attempts += 1
        try:
            sess.goto(url)
            if getattr(sess, "page", None) is not None:
                C.check_challenge(sess.page, "search_page")
            return _collect_links(sess, limit)
        except Exception as e:
            cls = B.classify_error(e, sess=sess)
            if not cls.should_recycle or attempts > max_recycles:
                raise
            kind = cls.error_kind or "unknown"
            print(f"[search] докрутка выдачи: {kind} → recycle браузера "
                  f"(попытка {attempts}/{max_recycles})")
            log.warning(f"докрутка выдачи: {kind} → recycle браузера "
                        f"(попытка {attempts}/{max_recycles})",
                        extra={"signature": cls.signature,
                               "probe": cls.probe_state})
            try:
                _recycle_browser(sess, manager, f"search-links-r{attempts}")
            except Exception as re:
                raise RuntimeError(
                    f"recycle при докрутке выдачи FAILED: {re}") from e
            recovery["recycles"] += 1
            recovery["events"].append(
                {"pid": "links", "attempt": attempts, "kind": kind})


def _collect_shard(sess, manager, items, gather_cfg: dict,
                   max_recycles: int, recovery: dict,
                   on_recovery=None, label: str = "search") -> list[dict]:
    """Per-product recovery-цикл над списком (index, pid, url) в одной сессии.

    ОДИН код для collect_search (main) и воркеров (parallel._worker_main):
    recovery access_challenge → НОВАЯ ВКЛАДКА (до NEW_TAB_ATTEMPTS раз, сессия
    не перезапускается), затем R1/R2 recycle через переданный manager
    (identity-сверка на (порт, профиль) менеджера) + preventive recycle каждые
    RECYCLE_EVERY товаров. Ошибка отдельного товара не прерывает цикл.

    Возвращает list[dict] результатов (res run_gather + recycle_attempts +
    new_tab_attempts + recovery_kind), строго в порядке items. Телеметрия
    recovery пишется в recovery (recycles/events/new_tab_attempts/
    new_tab_successes); on_recovery — колбэк события (воркер → pipe
    телеметрия), None для main.
    """
    products: list[dict] = []
    collected = 0
    since_recycle = 0
    challenge_streak = 0
    total = len(items)
    for n, (idx, pid, url) in enumerate(items, 1):
        tag = f"[{label}] товар {n}/{total}"
        print(f"{tag}: pid={pid} ({url})")
        log.info(f"товар {n}/{total}: pid={pid}", extra={"url": url})
        cfg = dict(gather_cfg)
        cfg["pid"] = pid
        cfg["url"] = url
        attempts = 0
        recycles_done = 0
        new_tab_budget = NEW_TAB_ATTEMPTS
        last_recovery = None
        while True:
            res = run_gather(cfg, sess=sess)
            attempts += 1
            if res["ok"] or not res.get("recyclable"):
                break
            kind = res.get("error_kind") or "unknown"

            # 1) access_challenge → recovery НОВОЙ ВКЛАДКОЙ (до recycle):
            #    FAB залипает на вкладке, свежая навигация проходит и
            #    разблокирует сессию (auth-куки). Бюджет на товар —
            #    NEW_TAB_ATTEMPTS; recycle-бюджет (max_recycles) не тратится.
            if kind == "access_challenge" and new_tab_budget > 0:
                new_tab_budget -= 1
                nt_used = NEW_TAB_ATTEMPTS - new_tab_budget
                last_recovery = "new_tab"
                recovery.setdefault("new_tab_successes", 0)
                recovery["new_tab_attempts"] = (
                    recovery.get("new_tab_attempts", 0) + 1)
                print(f"{tag}: access_challenge → новая вкладка "
                      f"(попытка {nt_used}/{NEW_TAB_ATTEMPTS})")
                log.warning(f"товар {n}/{total}: access_challenge → новая "
                            f"вкладка (попытка {nt_used}/{NEW_TAB_ATTEMPTS})",
                            extra={"pid": pid})
                ev = {"pid": pid, "attempt": attempts, "kind": "new_tab"}
                recovery["events"].append(ev)
                if on_recovery is not None:
                    on_recovery(ev)
                try:
                    sess.new_page()
                except Exception as e:
                    print(f"{tag}: new_tab FAILED: {type(e).__name__}: {e}")
                    log.error(f"товар {n}/{total}: new_tab FAILED: "
                              f"{type(e).__name__}: {e}", extra={"pid": pid})
                    res["error"] = f"{res['error']}; new_tab_failed: {e}"
                    res["recyclable"] = False
                    break
                continue

            # 2) fallback: recycle браузера (существующий recovery R1/R2)
            if recycles_done >= max_recycles:
                break
            recycles_done += 1
            last_recovery = "recycle"
            print(f"{tag}: {kind} → recycle браузера "
                  f"(попытка {recycles_done}/{max_recycles})")
            log.warning(f"товар {n}/{total}: {kind} → recycle браузера "
                        f"(попытка {recycles_done}/{max_recycles})",
                        extra={"pid": pid, "probe": res.get("error_probe_state"),
                               "signature": res.get("error_signature")})
            try:
                _recycle_browser(sess, manager, f"{label}-{pid}-r{recycles_done}")
            except Exception as e:
                print(f"{tag}: recycle FAILED: {type(e).__name__}: {e}")
                log.error(f"товар {n}/{total}: recycle FAILED: "
                          f"{type(e).__name__}: {e}", extra={"pid": pid})
                res["error"] = f"{res['error']}; recycle_failed: {e}"
                res["recyclable"] = False
                break
            recovery["recycles"] += 1
            ev = {"pid": pid, "attempt": attempts, "kind": kind}
            recovery["events"].append(ev)
            if on_recovery is not None:
                on_recovery(ev)
        res["recycle_attempts"] = attempts
        nt_used = NEW_TAB_ATTEMPTS - new_tab_budget
        res["new_tab_attempts"] = nt_used
        if last_recovery is not None:
            res["recovery_kind"] = last_recovery
        if res["ok"] and last_recovery == "new_tab":
            recovery["new_tab_successes"] = (
                recovery.get("new_tab_successes", 0) + 1)
        mr = gather_cfg.get("min_rating")
        if res["ok"] and mr is not None:
            rating = (res.get("raw") or {}).get("product", {}).get("rating")
            if isinstance(rating, (int, float)) and rating < mr:
                res["excluded"] = {
                    "reason": "rating_below",
                    "rating": rating,
                    "min_rating": mr,
                }
        products.append(res)
        if res["ok"]:
            collected += 1
            n_rev = len((res.get("raw") or {}).get("reviews", []))
            excl = ""
            if res.get("excluded"):
                e = res["excluded"]
                excl = f" · исключён по рейтингу {e['rating']} < {e['min_rating']}"
            print(f"{tag}: ok · cache={res['cache']} · ocr={res['ocr']} "
                  f"· отзывов {n_rev}{excl}")
            log.info(f"товар {n}/{total}: ok · cache={res['cache']} "
                     f"· ocr={res['ocr']} · отзывов {n_rev}{excl}",
                     extra={"pid": pid})
        else:
            print(f"{tag}: fail · {res['error']}")
            log.warning(f"товар {n}/{total}: fail · {res['error']}",
                        extra={"pid": pid})
        if not res["ok"] and res.get("error_kind") == "access_challenge":
            challenge_streak += 1
        else:
            challenge_streak = 0
        if challenge_streak >= CHALLENGE_STORM_LIMIT:
            print(f"{tag}: {challenge_streak} access_challenge подряд — "
                  f"останавливаю прогон (challenge storm, cooldown)")
            log.error(f"товар {n}/{total}: challenge storm "
                      f"({challenge_streak} подряд) — останавливаю прогон",
                      extra={"pid": pid})
            recovery["challenge_storm"] = {
                "streak": challenge_streak,
                "remaining_aborted": total - n,
                "pid": pid,
            }
            for _m, (_i2, pid2, _u2) in enumerate(items[n:], n + 1):
                products.append({
                    "pid": pid2, "ok": False,
                    "error": "aborted: challenge storm (cooldown)",
                    "error_kind": "challenge_storm",
                    "raw": None, "markdown": "", "cache": "off",
                    "ocr": "off", "recyclable": False,
                    "recycle_attempts": 0,
                })
            break
        since_recycle += 1
        if since_recycle >= RECYCLE_EVERY:
            try:
                _recycle_browser(sess, manager, f"{label}-preventive-{n}")
            except Exception as e:
                print(f"{tag}: preventive recycle FAILED: "
                      f"{type(e).__name__}: {e}")
                log.error(f"товар {n}/{total}: preventive recycle FAILED: "
                          f"{type(e).__name__}: {e}", extra={"pid": pid})
            else:
                since_recycle = 0
                recovery["recycles"] += 1
                ev = {"pid": pid, "attempt": 0, "kind": "preventive"}
                recovery["events"].append(ev)
                if on_recovery is not None:
                    on_recovery(ev)
                log.info(f"preventive recycle после {n} товаров",
                         extra={"pid": pid})
    return products


def collect_search(sess, url: str, limit: int, gather_cfg: dict,
                   query: Optional[str] = None,
                   max_recycles: int = 2,
                   pids: Optional[list] = None) -> dict:
    """Собирает до limit товаров из выдачи url в одной CDP-сессии.

    gather_cfg — базовый конфиг для run_gather (pid/url подставляются на
    каждый товар). Ошибка отдельного товара не прерывает цикл.

    pids — явный список артикулов вместо докрутки выдачи: выдача не
    сканируется, товары берутся ТОЛЬКО из списка (found = len(pids), url
    игнорируется). Режим для «собрать/обновить конкретные товары».

    Recovery: access_challenge → НОВАЯ ВКЛАДКА (до NEW_TAB_ATTEMPTS, сессия не
    перезапускается); прочие recyclable-ошибки (browser / renderer_failure) →
    браузер перезапускается (R1: recycle) и товар собирается заново (R2: retry),
    до max_recycles раз на товар. Телеметрия recovery пишется в
    combined["recovery"] (recycles/events/new_tab_attempts/new_tab_successes).

    Возвращает combined-объект (схема 2.0, аддитивно):
      search{url,query}, found, limit, collected,
      recovery{recycles, events[]},
      products[{pid,ok,error,error_kind,error_probe_state,recycle_attempts,
                new_tab_attempts,recovery_kind,...}]
    """
    recovery = {"recycles": 0, "events": []}
    manager = B.BrowserProcessManager(
        cdp_url=getattr(sess, "cdp_url", "http://127.0.0.1:9222"))
    if pids:
        links = [{"pid": str(p), "url": f"https://www.ozon.ru/product/{p}"}
                 for p in pids]
    else:
        links = _collect_links_with_recovery(
            sess, manager, url, limit, max_recycles, recovery)

    found = len(links)
    selected = links[:limit]
    items = [(i, it["pid"], it["url"]) for i, it in enumerate(selected)]
    total = len(items)
    print(f"[search] найдено {found} товаров в выдаче, собираю {total}")
    log.info(f"найдено {found} товаров в выдаче, собираю {total}")

    products = _collect_shard(sess, manager, items, gather_cfg,
                              max_recycles, recovery, label="search")
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
    storm = recovery.get("challenge_storm")
    if storm:
        combined["aborted"] = {"reason": "challenge_storm", **storm}
    return combined
