---
name: ozon-api-prepass-dom-truth
description: >
  Отзывы Ozon через entrypoint-api берутся ТОЛЬКО как opt-in предсрез: DOM —
  источник истины, API лишь добавляет недостающие отзывы по uuid, пагинация
  следует только серверному nextPage, короткое замыкание DOM — лишь при доказанном
  покрытии rating_count. Use when: отзывы медленные, entrypoint-api, reviews_api,
  fetch_slice, nextPage, pageToken, ускорить сбор отзывов, 12-15 сек на товар,
  layout_page_index, Ozon reviews API.
---
# API-предсрез отзывов: ускоритель, а не замена DOM

## Контекст
- Замеры 2026-09-25 (pid 210502516, 257 707 отзывов, `tests/probe_reviews_api.py`,
  `tests/probe_reviews_req_shape.py`) на **анонимной** сессии:
  - `layout_page_index` без токенов страницы игнорируется — страницы 1–4 отдают
    одну и ту же четвёрку (те же uuid, что в DOM);
  - `nextPage`/`pageToken` пусты;
  - анонимному профилю отдаётся 4 отзыва суммарно — столько же, сколько даёт
    DOM-скролл. Срез **серверный**, транспорт его не обходит;
  - настоящая пагинация несёт `sh=` и `start_page_id=` (32-hex) внутри вложенного
    `url=`, токены живут только в конкретной загрузке страницы.
- Вывод: «12–15 с на товар → 1 с» не доказано. Это гипотеза для авторизованной
  сессии, поэтому дефолт — ВЫКЛ.

## Алгоритм (KISS)
1. Включение: `--reviews-api` / `OZON_REVIEWS_API=1` / `run_cfg.reviews_api`.
2. `reviews_api_enabled` + guards: variant-фильтр и мульти-сорт **отключают** путь
   (API не умеет ни то, ни другое).
3. `fetch_slice` → `normalize_review` (в DOM-форму, ключ `stars`).
4. `merge_reviews_api`: merge по `uuid`, **DOM всегда выигрывает**; счётчики
   `api_added`/`api_dupes`/`api_dropped` сразу показывают расхождение схем.
5. `api_slice_complete` = fetched >= доверенного `product.rating_count` →
   короткое замыкание DOM (одна страница, `dom_short_circuit: true`).
6. Телеметрия `raw["reviews_api"]` = `{enabled, used, reason, fetched, pages,
   complete, elapsed_ms, api_added, api_dupes, dom_short_circuit}`.

## Подводные камни
- ❌ Пагинация через `layout_page_index` без токенов: сервер игнорирует, страницы
  повторяются. Только `nextPage` из ответа.
- ❌ Синтетический «показун» в обход серверного курсора — тратит время, не даёт
  новых отзывов.
- ❌ Ошибка транспорта/парсинга как повод ронять товар: любое исключение → тихо
  выключить предсрез, собрать как раньше.
- ❌ Запись без `uuid`+текста+оценки в `raw`: мусор хуже отсутствия отзыва.
- ❌ Короткое замыкание DOM при неполном покрытии — тихая потеря отзывов (K4).
- ✅ Проба из контекста страницы (те же куки/заголовки/TLS): валидная сессия уже
  есть, перехват сети в Firefox недоступен.

## Где вшито
- `ozon_collector/ozon_fetch.py`: `normalize_review`, `api_plan`, `api_slice_complete`,
  `fetch_slice`, `_fetch_by_url`, `merge_reviews_api`.
- `ozon_collector/collect.py`: `reviews_api_enabled`, `_reviews_api_prepass`,
  `_collect_reviews_stage`, `raw["reviews_api"]`.
- `ozon_collector/gather.py`: `cfg.get("reviews_api")` → `C.collect`.
- CLI: `cli.py` (root `collect`/`gather`), `scripts/key_daemon.py`.

## Примеры
✅ API отдала 250 при rating_count 250 → DOM одна страница, `complete: true`.
✅ `nextPage` пуст → 1 страница, `complete: false`, DOM докручивает как раньше.
❌ variant-фильтр включён → `used: false, reason: "variant"`, DOM без изменений.
❌ API вернул 4 тех же uuid → `api_dupes: 4, api_added: 0` — видно, что путь
   бесполезен именно для этой сессии.

## Ссылки
- `D:\Users\Neon\Projects\ozon-collector\tests\test_reviews_api.py` (41 тест)
- `D:\Users\Neon\Projects\ozon-collector\docs\architecture\anti_bot_architecture.md` §16
- `D:\Users\Neon\Projects\ozon-collector\PROJECT_NOTES.md` — ревизия 2.8
