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
from . import collect as C
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


def collect_search(sess, url: str, limit: int, gather_cfg: dict,
                   query: Optional[str] = None) -> dict:
    """Собирает до limit товаров из выдачи url в одной CDP-сессии.

    gather_cfg — базовый конфиг для run_gather (pid/url подставляются на
    каждый товар). Ошибка отдельного товара не прерывает цикл.
    Возвращает combined-объект (схема 2.0, аддитивно):
      search{url,query}, found, limit, collected, products[{pid,ok,error,...}]
    """
    sess.goto(url)
    links = _collect_links(sess, limit)

    found = len(links)
    selected = links[:limit]
    total = len(selected)
    print(f"[search] найдено {found} товаров в выдаче, собираю {total}")

    products = []
    collected = 0
    for i, item in enumerate(selected, 1):
        pid = item["pid"]
        print(f"[search] товар {i}/{total}: pid={pid} ({item['url']})")
        cfg = dict(gather_cfg)
        cfg["pid"] = pid
        cfg["url"] = item["url"]
        res = run_gather(cfg, sess=sess)
        products.append(res)
        if res["ok"]:
            collected += 1
            n = len((res.get("raw") or {}).get("reviews", []))
            print(f"[search] товар {i}/{total}: ok · cache={res['cache']} "
                  f"· ocr={res['ocr']} · отзывов {n}")
        else:
            print(f"[search] товар {i}/{total}: fail · {res['error']}")

    return {
        "schema_version": SCHEMA_VERSION,
        "collector_version": VERSION,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "search": {"url": url, "query": query},
        "found": found,
        "limit": limit,
        "collected": collected,
        "products": products,
    }
