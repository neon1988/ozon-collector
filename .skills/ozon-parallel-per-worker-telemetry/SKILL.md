---
name: ozon-parallel-per-worker-telemetry
description: >
  Прежде чем «давать воркеру свой IP», нужен замер: challenges считались только
  СУММОЙ по пулу, и по отчёту нельзя было понять, вредит ли параллелизм
  конкретному IP/профилю. Поэтому сначала per-worker телеметрия
  (challenges, challenge_rate_per_100, recovery.canary), потом A/B parallel=1 vs 2.
  Use when: параллелизм, воркер, свой IP на воркер, прокси на воркер, challenges
  по пулу, challenge_rate_per_100, worker summary, parallel=2 хуже, троттлинг.
---
# Per-worker телеметрия раньше, чем прокси на воркер

## Контекст
- Требование «каждому воркеру свой IP» звучит правильно, но это оптимизация по
  гипотезе: без замера нельзя отличить «вредит параллелизм этому IP» от «общий
  шторм на пуле».
- Раньше challenges были только суммой по пулу — в разрезе воркеров данных не было.

## Алгоритм (KISS)
1. `combined.parallel.workers[k]` отдаёт `challenges` и
   `challenge_rate_per_100` (на 100 товаров воркера; `null`, если товаров нет).
2. `recovery.canary = {url, kind, result}` — какая сущность проверялась.
3. Агрегаты: `kpi.canary_fails`, `kpi.ip_blocks` (разные причины остановки —
   не смешивать).
4. Замер: одинаковая кампания, `parallel=1` против `parallel=2`, сравнить
   `challenge_rate_per_100` по воркерам.
5. Прокси на воркер — только после этого замера.

## Подводные камни
- ❌ Прокси на воркер «на всякий случай»: новая переменная (профиль/порт/cookies)
  без данных о том, что именно вредно.
- ❌ Сравнивать absolute challenges при разном числе товаров — нужен rate.
- ❌ Пустой воркер с `rate = 0.0` вместо `null` — выглядит как «идеальный IP».
- ✅ `null` при отсутствии товаров — честный сигнал «данных нет».

## Где вшито
- `ozon_collector/parallel.py`: `_worker_summary` (challenges/rate),
  `_kpi_summary` (canary_fails/ip_blocks).

## Примеры
✅ воркер 1: 20 товаров, 2 challenge → rate 10.0; воркер 2: 3 товара, 0 → null.
❌ pool challenges = 2 → «всё нормально», а оба challenge были на одном воркере.

## Ссылки
- `D:\Users\Neon\Projects\ozon-collector\tests\test_canary_url.py` — TestKpiCanaryAndPerWorker
- `D:\Users\Neon\Projects\ozon-collector\docs\architecture\anti_bot_architecture.md` §12
