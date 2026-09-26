"""Отзывы Ozon через JSON API прямо со страницы (без перехвата сети).

Почему так: перехват сети в Firefox недоступен (network.getData не
поддерживается, addRequestHandler вешает страницу — см. FirefoxPage.on), но
сам Ozon отдаёт те же отзывы обычным GET, который доступен из контекста
страницы: куки/заголовки/TLS те же, сессия валидна.

Формат ответа (замер ff_widget_shape.py, 2026-09-25):
    payload["widgetStates"]["webListReviews-<id>-reviewshelfpaginator-<n>"]
        = JSON-СТРОКА (нужен второй json.loads), внутри reviews, nextPage,
        pageToken, products, productScore, filters, sortings.

В отзыве ключ id — `uuid`.

ЧТО НЕ РАБОТАЕТ на анонимной сессии (замер 2026-09-25, pid 210502516,
257 707 отзывов — tests/probe_reviews_api.py, tests/probe_reviews_req_shape.py):

  * `layout_page_index` без токенов страницы игнорируется — на 1, 2, 3, 4
    приходит одна и та же четвёрка отзывов (те же uuid, что и в DOM);
  * `nextPage` и `pageToken` в ответе пусты, курсора пагинации нет;
  * анонимный профиль получает 4 отзыва суммарно, столько же показывает и
    DOM-скролл: срез серверный, транспорт его не обходит;
  * настоящая пагинация страницы несёт `sh=` и `start_page_id=` (32-hex) в
    параметрах вложенного `url=` — без них индекс бессмыслен, а токены живут
    только в конкретной загрузке страницы.

Вывод: модуль — не ускоритель покрытия, а способ снять те же 4 отзыва за
0,02–0,12 с вместо минут скролла. Полноценная пагинация имеет смысл только
на авторизованной сессии (проверка — `collect.detect_auth_state`), поэтому
в боевой `collect` модуль не подключён.

ПОДКЛЮЧЕНИЕ (ревизия 2.8, opt-in): модуль всё же включён в боевой сбор, но
НЕ как источник истины, а как быстрый предсрез отзывов:
  * выключено по умолчанию (--reviews-api / OZON_REVIEWS_API=1);
  * DOM остаётся источником истины: merge_reviews_api() НИКОГДА не
    перезаписывает отзыв, который уже есть из DOM, и не добавляет отзыв,
    если для этой позиции DOM ждал пагинации (см. api_plan);
  * пагинация идёт ТОЛЬКО по URL из nextPage (layout_page_index без токенов
    не работает — см. замеры выше), поэтому анонимная сессия получит те же 4;
  * api_slice_complete() разрешает не докручивать DOM-страницы, только если
    API вернул не меньше доверенного product.rating_count — тогда покрытие
    серверно доказано, а не предположено.
Нормализация отзыва (normalize_review) консервативная: неизвестная форма
поля → отзыв отбрасывается (None), а не пишется в raw как есть.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from . import log

ENTRYPOINT = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2"
REVIEWS_WIDGET_PREFIX = "webListReviews"

# Поля API-отзыва, которые пробуем как источник текста/оценки. Порядок не
# важен: берётся первое непустое. Неизвестное имя → запись отбрасывается.
_TEXT_KEYS = ("text", "reviewText", "body", "content")
_AUTHOR_KEYS = ("authorName", "author", "userName", "nickname")
_DATE_KEYS = ("publishedAtFormatted", "createdAt", "date", "publishedAt")
_RATING_KEYS = ("rating", "score", "stars")


def reviews_url(pid: str, page_index: int = 1) -> str:
    """URL первой страницы отзывов товара."""
    return "%s?url=/product/%s/reviews/pdp-part?layout_container=reviewshelfpaginator" \
           "&layout_page_index=%s" % (ENTRYPOINT, pid, page_index)


def fetch_json(page, url: str, timeout_ms: int = 60000) -> Optional[Any]:
    """GET url из контекста страницы через fetch; вернуть распарсенный JSON.

    Работает одинаково на Playwright и Selenium: page.evaluate разруливает
    промисы в обоих. None при сетевой/парсинговой ошибке (вызывающий решает
    fallback).

    Таймаут задаётся через AbortController в JS, а не аргументом evaluate:
    Playwright принимает evaluate(expression, arg) — ровно один аргумент и без
    timeout=, так что передача (url, timeout_ms) падала с TypeError на
    Chromium (живая проба 2026-09-25, tests/probe_reviews_api.py). Selenium-
    обёртка ffsel.evaluate kwargs держит, но полагаться на это нельзя.
    """
    js = "([url, ms]) => {" \
         "  const ctl = new AbortController();" \
         "  const timer = setTimeout(() => ctl.abort(), ms);" \
         "  return fetch(url, {credentials: 'include', signal: ctl.signal})" \
         "    .then(r => r.text().then(txt => ({s: r.status, t: txt})))" \
         "    .catch(e => ({err: String(e)}))" \
         "    .finally(() => clearTimeout(timer));" \
         "}"
    try:
        res = page.evaluate(js, [url, timeout_ms])
    except Exception as e:
        log.warning(f"[reviews-api] transport error: {type(e).__name__}: {e}")
        return None
    if not isinstance(res, dict):
        return None
    if res.get("err"):
        log.warning(f"[reviews-api] fetch error: {str(res['err'])[:120]}")
        return None
    status = int(res.get("s") or 0)
    body = res.get("t") or ""
    if status != 200:
        log.warning(f"[reviews-api] HTTP {status}, body {len(body)}b")
        return None
    try:
        return json.loads(body)
    except Exception as e:
        log.warning(f"[reviews-api] not JSON: {type(e).__name__}: {e}")
        return None


def find_reviews_widget(payload: Any) -> Optional[dict]:
    """Достать распарсенный виджет отзывов из ответа entrypoint-api.

    Ищем первый ключ widgetStates с префиксом webListReviews; значение — строка,
    поэтому json.loads обязателен. Отсутствие/кривое значение → None.
    """
    if not isinstance(payload, dict):
        return None
    states = payload.get("widgetStates")
    if not isinstance(states, dict):
        return None
    for key, raw in states.items():
        if not str(key).startswith(REVIEWS_WIDGET_PREFIX):
            continue
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                log.debug(f"[reviews-api] widget {key}: не JSON")
                continue
        if isinstance(raw, dict):
            return raw
    return None


def fetch_reviews_page(page, pid: str, page_index: int = 1,
                       timeout_ms: int = 60000) -> Optional[dict]:
    """Отзывы товара: payload виджета, next_page (URL) и page_token."""
    payload = fetch_json(page, reviews_url(pid, page_index), timeout_ms)
    widget = find_reviews_widget(payload)
    if widget is None:
        log.debug(f"[reviews-api] pid={pid} page={page_index}: виджет не найден")
        return None
    return {
        "widget": widget,
        "reviews": widget.get("reviews") or [],
        "next_page": (widget.get("nextPage") or "").strip(),
        "page_token": (widget.get("pageToken") or "").strip(),
    }


def _first_str(raw: dict, keys: tuple) -> str:
    for k in keys:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):          # author: {"name": ...}
            inner = v.get("name") or v.get("firstName")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return ""


def _first_int(raw: dict, keys: tuple) -> Optional[int]:
    for k in keys:
        v = raw.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
    return None


def normalize_review(raw: Any) -> Optional[dict]:
    """API-отзыв → форма DOM-отзыва (uuid/author/date/text/rating).

    Возвращает None, если не нашли ни uuid, ни текст, ни оценку: мусор в raw
    хуже отсутствия отзыва, а fallback на DOM всегда доступен.
    """
    if not isinstance(raw, dict):
        return None
    uuid = raw.get("uuid") or raw.get("id") or raw.get("reviewId")
    if isinstance(uuid, int):
        uuid = str(uuid)
    if not isinstance(uuid, str) or not uuid.strip():
        return None
    text = _first_str(raw, _TEXT_KEYS)
    rating = _first_int(raw, _RATING_KEYS)
    if not text and rating is None:
        return None
    return {
        "uuid": uuid.strip(),
        "author": _first_str(raw, _AUTHOR_KEYS),
        "date": _first_str(raw, _DATE_KEYS),
        "text": text,
        # Поле называется stars, а не rating: отчёт/digest читает r["stars"]
        # во всех местах, и запись с "rating" выглядела бы как отзыв без оценки.
        "stars": rating,
        "helpful_yes": _first_int(raw, ("helpfulYes", "likes", "positive")) or 0,
        "helpful_no": _first_int(raw, ("helpfulNo", "dislikes", "negative")) or 0,
        "source": "api",
    }


def api_plan(target_variant: Optional[dict] = None,
             need_dom_extras: bool = True) -> tuple[bool, str]:
    """Допустим ли API-предсрез в этом сборе — (да, причина).

    Отказоустойчиво по умолчанию: спорные случаи уходят в DOM, а не в API.

      * variant-фильтр (--variant-*: Цвет/Размер/Габариты) API не умеет —
        отзыв другого варианта попадёт в товар. Применяем нативный фильтр
        Ozon «Этот вариант» нельзя без перезагрузки страницы → отказ;
      * если DOM сортируется по нескольким sort-проходам или нужны
        DOM-only поля (photos/replies/стабильность страниц) — API лишь
        дополняет, но сортировку/стабильность не заменяет (need_dom_extras).
    """
    if target_variant:
        keys = [k for k, v in (target_variant or {}).items() if v]
        if keys:
            return False, f"variant_filter({','.join(keys[:3])})"
    return True, "ok" if need_dom_extras else "no_dom_extras"


def api_slice_complete(fetched: int, expected_total: Optional[int]) -> bool:
    """Покрытие серверно доказано (API отдал не меньше rating_count)?"""
    if not expected_total or expected_total <= 0:
        return False
    return int(fetched) >= int(expected_total)


def fetch_slice(page, pid: str, max_pages: int = 1,
                timeout_ms: int = 60000) -> dict:
    """Быстрый предсрез отзывов через API: список + телеметрия.

    Пагинация ТОЛЬКО по nextPage из ответа: синтетический layout_page_index
    без токенов страницы, по замерам 2026-09-25, возвращает ту же первую
    страницу — такой «показун» лишь тратит время.

    Возвращает dict {reviews, pages, fetched, next_page_seen, complete_url,
    error}. reviews — нормализованные записи (normalize_review отбросил
    неизвестную форму), поэтому fetched может быть меньше сырого ответа.
    """
    out = {"reviews": [], "pages": 0, "fetched": 0, "next_page_seen": False,
           "complete_url": "", "error": ""}
    seen: set[str] = set()
    url = reviews_url(pid, 1)
    for _page_no in range(max(1, int(max_pages))):
        data = fetch_reviews_page(page, pid, 1, timeout_ms) if _page_no == 0 \
            else _fetch_by_url(page, url, timeout_ms)
        if data is None:
            out["error"] = out["error"] or "fetch_failed"
            break
        out["pages"] += 1
        for raw in data.get("reviews") or []:
            rec = normalize_review(raw)
            if rec is None or rec["uuid"] in seen:
                continue
            seen.add(rec["uuid"])
            out["reviews"].append(rec)
        nxt = data.get("next_page") or ""
        if not nxt:
            break
        out["next_page_seen"] = True
        out["complete_url"] = nxt
        url = nxt
    out["fetched"] = len(out["reviews"])
    return out


def _fetch_by_url(page, url: str, timeout_ms: int) -> Optional[dict]:
    """Страница пагинации по готовому URL (его отдал сервер в nextPage)."""
    payload = fetch_json(page, url, timeout_ms)
    widget = find_reviews_widget(payload)
    if widget is None:
        log.debug(f"[reviews-api] nextPage: виджет не найден")
        return None
    return {
        "widget": widget,
        "reviews": widget.get("reviews") or [],
        "next_page": (widget.get("nextPage") or "").strip(),
        "page_token": (widget.get("pageToken") or "").strip(),
    }


def merge_reviews_api(dom_reviews: list, api_reviews: list) -> tuple[list, dict]:
    """Дополнить DOM-список отзывами API. DOM всегда побеждает.

    Счётчики: api_added — сколько записей реально добавили, api_dupes — сколько
    API-отзывов уже были в DOM (это норма при согласованных источниках и
    главный признак расхождения схемы, если api_dupes==0 при api_reviews>0).
    """
    seen = set()
    for r in dom_reviews or []:
        uid = (r or {}).get("uuid")
        if uid:
            seen.add(uid)
    added, dupes, dropped = [], 0, 0
    for r in api_reviews or []:
        uid = (r or {}).get("uuid")
        if not uid:
            dropped += 1
            continue
        if uid in seen:
            dupes += 1
            continue
        seen.add(uid)
        added.append(r)
    stats = {"api_added": len(added), "api_dupes": dupes,
             "api_dropped": dropped, "api_input": len(api_reviews or [])}
    return list(dom_reviews or []) + added, stats
