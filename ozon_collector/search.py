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


def _recycle_browser(sess, launch_id: str, max_launch_wait: float = 20.0):
    """Recovery R1: перезапуск Chromium + переподключение CDP-сессии.

    old_records — записи ТЕКУЩИХ matching-процессов на cdp_url сессии
    (наш браузер). BrowserProcessManager.restart убивает их (identity-сверка),
    проверяет отсутствие чужих, запускает новый процесс и дожидается CDP.
    Затем сессия переподключается к новому браузеру.
    """
    manager = B.BrowserProcessManager(cdp_url=sess.cdp_url)
    old = [P.ProcessRecord(launch_id, ident)
           for ident in P.browser_processes(sess.cdp_url)]
    manager.restart(old, launch_id)
    sess.reconnect()


# Профилактический recycle: каждые RECYCLE_EVERY обработанных товаров браузер
# перезапускается принудительно, чтобы renderer не дорастал до OOM (длинный
# search держит десятки страниц в одной сессии, JS heap копится).
RECYCLE_EVERY = 40


def _collect_links_with_recovery(sess, url: str, limit: int,
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
                _recycle_browser(sess, f"search-links-r{attempts}")
            except Exception as re:
                raise RuntimeError(
                    f"recycle при докрутке выдачи FAILED: {re}") from e
            recovery["recycles"] += 1
            recovery["events"].append(
                {"pid": "links", "attempt": attempts, "kind": kind})


def collect_search(sess, url: str, limit: int, gather_cfg: dict,
                   query: Optional[str] = None,
                   max_recycles: int = 2) -> dict:
    """Собирает до limit товаров из выдачи url в одной CDP-сессии.

    gather_cfg — базовый конфиг для run_gather (pid/url подставляются на
    каждый товар). Ошибка отдельного товара не прерывает цикл.

    Recovery (OOM-краш renderer'а): если run_gather вернул recyclable-ошибку
    (browser / renderer_failure) — браузер перезапускается (R1: recycle) и
    товар собирается заново (R2: retry), до max_recycles раз на товар.
    Телеметрия recovery пишется в combined["recovery"].

    Возвращает combined-объект (схема 2.0, аддитивно):
      search{url,query}, found, limit, collected,
      recovery{recycles, events[]},
      products[{pid,ok,error,error_kind,error_probe_state,recycle_attempts,...}]
    """
    recovery = {"recycles": 0, "events": []}
    links = _collect_links_with_recovery(
        sess, url, limit, max_recycles, recovery)

    found = len(links)
    selected = links[:limit]
    total = len(selected)
    print(f"[search] найдено {found} товаров в выдаче, собираю {total}")
    log.info(f"найдено {found} товаров в выдаче, собираю {total}")

    products = []
    collected = 0
    since_recycle = 0
    for i, item in enumerate(selected, 1):
        pid = item["pid"]
        print(f"[search] товар {i}/{total}: pid={pid} ({item['url']})")
        log.info(f"товар {i}/{total}: pid={pid}", extra={"url": item["url"]})
        cfg = dict(gather_cfg)
        cfg["pid"] = pid
        cfg["url"] = item["url"]
        attempts = 0
        while True:
            res = run_gather(cfg, sess=sess)
            attempts += 1
            if res["ok"] or not res.get("recyclable") or attempts > max_recycles:
                break
            kind = res.get("error_kind") or "unknown"
            print(f"[search] товар {i}/{total}: {kind} → recycle браузера "
                  f"(попытка {attempts}/{max_recycles})")
            log.warning(f"товар {i}/{total}: {kind} → recycle браузера "
                        f"(попытка {attempts}/{max_recycles})",
                        extra={"pid": pid, "probe": res.get("error_probe_state"),
                               "signature": res.get("error_signature")})
            try:
                _recycle_browser(sess, f"search-{pid}-r{attempts}")
            except Exception as e:
                print(f"[search] товар {i}/{total}: recycle FAILED: "
                      f"{type(e).__name__}: {e}")
                log.error(f"товар {i}/{total}: recycle FAILED: "
                          f"{type(e).__name__}: {e}", extra={"pid": pid})
                res["error"] = f"{res['error']}; recycle_failed: {e}"
                res["recyclable"] = False
                break
            recovery["recycles"] += 1
            recovery["events"].append(
                {"pid": pid, "attempt": attempts, "kind": kind})
        res["recycle_attempts"] = attempts
        products.append(res)
        if res["ok"]:
            collected += 1
            n = len((res.get("raw") or {}).get("reviews", []))
            print(f"[search] товар {i}/{total}: ok · cache={res['cache']} "
                  f"· ocr={res['ocr']} · отзывов {n}")
            log.info(f"товар {i}/{total}: ok · cache={res['cache']} "
                     f"· ocr={res['ocr']} · отзывов {n}", extra={"pid": pid})
        else:
            print(f"[search] товар {i}/{total}: fail · {res['error']}")
            log.warning(f"товар {i}/{total}: fail · {res['error']}",
                        extra={"pid": pid})
        since_recycle += 1
        if since_recycle >= RECYCLE_EVERY:
            try:
                _recycle_browser(sess, f"search-preventive-{i}")
            except Exception as e:
                print(f"[search] товар {i}/{total}: preventive recycle "
                      f"FAILED: {type(e).__name__}: {e}")
                log.error(f"товар {i}/{total}: preventive recycle FAILED: "
                          f"{type(e).__name__}: {e}", extra={"pid": pid})
            else:
                since_recycle = 0
                recovery["recycles"] += 1
                recovery["events"].append(
                    {"pid": pid, "attempt": 0, "kind": "preventive"})
                log.info(f"preventive recycle после {i} товаров",
                         extra={"pid": pid})

    return {
        "schema_version": SCHEMA_VERSION,
        "collector_version": VERSION,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "search": {"url": url, "query": query},
        "found": found,
        "limit": limit,
        "collected": collected,
        "recovery": recovery,
        "products": products,
    }
