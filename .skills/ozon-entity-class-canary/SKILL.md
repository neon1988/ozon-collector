---
name: ozon-entity-class-canary
description: >
  Проверка «живости» сессии в обходном контуре Ozon проверяет СУЩНОСТЬ ТОГО ЖЕ
  КЛАССА, на которой сессия упала: /product/ жёстче главной, и CANARY_OK на
  главной не доказывает доступ карточек — бюджет session_used уходит вхолостую.
  Порядок цели: --canary-url → env OZON_CANARY_URL → URL кампании → главная.
  Use when: canary, CANARY_OK, проверка сессии, canary_url, OZON_CANARY_URL,
  canary фолбэк, session_used вхолостую, recycle не помогает, бан после recovery,
  какой URL проверять.
---
# Canary по классу сущности (не «главная всегда»)

## Контекст
- Главная `https://www.ozon.ru/` — **самая мягкая** цель: `CANARY_OK` на ней
  не доказывает, что сессия видит `/product/`. Теневой бан на карточках главная
  не показывает → сессия считалась восстановленной, первый товар снова ловил
  challenge, recycle уходил по кругу (тот же budget вхолостую).
- Классы по строгости: `product` (`/product/`) → `category` (`/category/`,`/c/`)
  → `search` (`/search/`,`text=`) → `home` → `custom`.
- Canary **не расходует** `session_used{challenge,new_tab,browser_recycle}` и не
  трогает PID: свой счётчик `canary_fails` + `ip_blocks`.

## Алгоритм (KISS)
1. `resolve_canary_url(preferred, campaign)` — порядок строго:
   `preferred` (явный `--canary-url` / уже разрешённая цель) →
   `OZON_CANARY_URL` (флаг оператора: «поставь лёгкий товар») →
   `campaign` (URL текущей выдачи) → `CANARY_URL_HOME`.
2. Кандидат годен только если `http(s)://`; не-http (`url="список из N
   артикулов"` в rescan демона) молча пропускается, **не** уходит в `goto`.
3. Класс пишется в телеметрию: `recovery.canary = {url, kind, result}` +
   `kpi.canary_fails` / `kpi.ip_blocks` (canary-fail и бан IP — разные причины).
4. Оркестратор решает цель ОДИН раз и кладёт в `task["canary_url"]`
   (через общий `_worker_task`), чтобы респавн не проверял другую сущность.
5. Вызывается только после recycle **по доступу**; после new_tab избыточно
   (challenge уже снят на товарном URL).

## Подводные камни
- ❌ **campaign-URL, переданный в `preferred`, перекрывает env.** Именно так
  выглядел баг: `resolve_canary_url(canary_url or url)` — оператор ставил
  `OZON_CANARY_URL`, а проверялась выдача. Отсюда правило: campaign отдельным
  аргументом, не через `or`.
- ❌ Хардкодить pid «безопасного» товара: он устаревает и сам становится тяжёлым
  или забаненным (любая «постоянная» ссылка деградирует в ту же проблему).
- ❌ Считать `CANARY_OK` доказательством доступа к карточкам без проверки класса.
- ❌ Считать canary восстановлением уровня FSM — у него собственный счётчик.
- ✅ Сторож формы вызова: тест ловит возврат к `canary_url or url` в обоих
  оркестраторах (search.py, parallel.py).

## Где вшито
- `ozon_collector/search.py`: `classify_canary_url`, `resolve_canary_url`,
  `run_canary(sess, url=None)`, `collect_search(canary_url=...)` → `recovery`.
- `ozon_collector/parallel.py`: `_worker_task(canary_url=...)`, `collect_parallel`,
  `_respawn`, `_await_workers`, `_kpi_summary`, `_worker_summary`.
- `scripts/key_daemon.py`: `--canary-url` → `run_cfg.canary_url` манифеста.

## Примеры
✅ `OZON_CANARY_URL=<category>` + кампания `/search/?text=iphone` → category.
✅ без env и без флага, кампания `/product/...` → product (тот же класс, что упал).
❌ rescan демона: url=`список из 5 артикулов` → fallback главная, kind=home.
❌ `resolve_canary_url(canary_url or url)` — env мёртв, регресс.

## Ссылки
- `D:\Users\Neon\Projects\ozon-collector\docs\architecture\anti_bot_architecture.md` §9, §10
- `D:\Users\Neon\Projects\ozon-collector\tests\test_canary_url.py` (30 тестов)
- `D:\Users\Neon\Projects\ozon-collector\PROJECT_NOTES.md` — ревизия 2.8
