# Логика поведения завершения кампании (key_daemon)

Файл: `scripts/key_daemon.py`. Цель этого документа — описать, КАК демон решает, когда
остановить кампанию. Полезно для ревью логики и поиска дефектов.

---

## Входные параметры (аргументы CLI → run_cfg)

| Аргумент | Дефолт | Смысл |
|---|---|---|
| `--limit` | 300 | **target** (= `target_count`). Цель по числу ВАЛИДНЫХ товаров. |
| `--min-valid` | (обязателен) | `min_valid`. Порог «частичного успеха»: если `min_valid <= valid < target` и выдача исчерпана → `COMPLETED_MIN`. |
| `--max-total-hours` | 12.0 | Жёсткий дедлайн. |
| `--max-no-progress-runs` | 3 | Лимит прогонов «без прогресса» подряд. |
| `--max-rescans` | 2 | Сколько раз пересобирать бракованный товар. |
| `--reserve-count` | 30 | Резервные valid PID сверх target для замены брака при финальном QA. |
| `--parallel` | 1 | Число воркеров. |
| `--min-reviews` | 40 | Критерий отбора (не passthrough). |

## Словарь статусов прогона (`classify_status`)

- `NORMAL_DONE` — прогон отработал штатно (`exit_code=0`, без абстрактного шторма).
- `CHALLENGE_STORM` — шторм капчи/пазла.
- `CRASH` — прогон упал.
- `UNKNOWN_FAILURE` — combined записан, но `exit_code != 0` (транзиентные ретраи).
- `SESSION_EXHAUSTED` — исчерпан session budget.

---

## Главный цикл (`while True`, строки 1190–1283)

Сначала проверяются **терминальные условия** (до запуска прогона):

```
if final_state в TERMINAL_STATES:        break          # уже завершено ранее
if (now - started) >= max_total_hours*3600:   break     # time_limit
if no_progress_count >= max_no_progress_runs: break    # no_progress
```

Затем обработка cooldown → выбор режима прогона (`full` / `rescan`) → запуск прогона.

### Выбор режима
- Если есть `pending` (failed-товары с `rescan < max_rescans`, причина не повторная) → `rescan`.
- Если есть `failed` без права на rescan → в `permanently_failed`.
- Иначе `full` — собираем выдачу целиком с запасом `discovery = target + reserve_count`.

---

## Пост-обработка каждого прогона (строки 1331–1420)

### 1. Подсчёт прогресса
```
new_disc  = разница(discovered_pids до/после)
new_valid = разница(valid_pids до/после)
```

### 2. Сброс общего счётчика при прогрессе (строки 1336–1342)
```
if new_disc or new_valid:
    no_progress_count = 0
    challenge_seq = 0
    storm_streak = 0
```
То есть **ЛЮБОЙ** новый обнаруженный товар (не обязательно валидный) сбрасывает `no_progress_count`.

### 3. Счётчики прогресса (строки 1357–1364)
`progress.new_discovered += len(new_disc)`, `progress.new_valid += len(new_valid)` — накопительные, для статистики.

### 4. Счётчики стагнации `stall` (строки 1366–1378)
```
if status == "NORMAL_DONE" and not new_disc and not new_valid:
    stall.full_runs_without_discovery += 1
    if mode == "full":
        stall.full_runs_without_valid += 1
if status == "CHALLENGE_STORM": stall.challenge_runs += 1
if status == "CRASH":            stall.crash_runs += 1
```

### 5. Инкремент `no_progress_count` (КЛЮЧЕВОЕ — строки 1396–1398, 1456–1458)
```
# только внутри веток:
if status == "CHALLENGE_STORM" and not new_disc and not new_valid:
    no_progress_count += 1
elif status == "CRASH":
    no_progress_count += 1
```

> ⚠️ **`NORMAL_DONE` НЕ инкрементирует `no_progress_count`.** Он увеличивает только
> `stall.full_runs_without_discovery` / `full_runs_without_valid`, которые в стоп-условии
> головного цикла (1198) НЕ участвуют.

---

## Терминальные состояния (`compute_final`, строки 379–388; финальный QA 1582–1731)

Итоговая категория вычисляется так:

```
valid = len(valid_pids)
if valid >= target:                          → COMPLETED
elif exhausted:                              → COMPLETED_MIN if valid >= min_valid
                                             → ABANDONED        if valid <  min_valid
elif valid < min_valid:                      → ABANDONED
else:                                        → None (продолжаем)
```

Параметр `exhausted` означает «выдача исчерпана». Он приходит из решений в
`run_campaign` (строки ~1505–1523, где после прогона без `found>0` решается остановка).

### Блок принятия решения после прогона (строки 1505–1523)
```
if not new_disc and not new_valid:
    no_progress_count += 1          # ДУБЛЬ? выяснить
```

и ветка про `found == 0` → выдача исчерпана (с пометкой «если found>0 — выдача не
исчерпана, продолжаем»).

### Условия мягкой остановки (строки 1700–1731)
```
snapshot = campaign_final_pids (или valid_pids)
clean = pids snapshot, прошедшие QA без FAIL
total_fail = число FAIL в снапшоте

if total_fail == 0 and valid >= target:      → COMPLETED (reason=target_reached)
elif total_fail == 0 and valid >= min_valid: → COMPLETED_MIN
else:                                        → ABANDONED
```

---

## Наблюдаемый дефект

**Отсутствует стоп-критерий «выдача исчерпана».**

Счётчик `stall.full_runs_without_discovery` (и `full_runs_without_valid`) пишется в
статус, но:
- НЕ читается в терминальных проверках головного цикла (1190–1201);
- не приводит к инкременту `no_progress_count` в ветке `NORMAL_DONE`.

Следствие: когда полный прогон штатно завершился, но новых товаров не дал
(`NORMAL_DONE`, `new_disc=0`, `new_valid=0`), демон НЕ видит исчерпания выдачи и
просто запускает следующий прогон. Остановка возможна только по:
- `time_limit` (max_total_hours),
- либо шторм/краш, дающие `no_progress_count`.

Дополнительно усугубляет: `no_progress_count` сбрасывается на 0 при ЛЮБОМ `new_disc`,
даже если новый товар не проходит критерии (`min_reviews`). В выдаче, где лупируем
невалидные товары, демон может «вечно» считать, что прогресс есть, и молотить до
дедлайна.

---

## Гипотезы для фикса (на рассмотрение ботам)

1. В ветке `NORMAL_DONE` без `new_disc`/`new_valid` инкремент `no_progress_count`
   (как это делают ветки STORM/CRASH). Тогда `max_no_progress_runs=3` сработает и
   даст `COMPLETED_MIN`/`ABANDONED` по `compute_final`.
2. Или ввести отдельный порог по `stall.full_runs_without_discovery >= N`
   (например, 2) с разрывом по `exhausted=True`.
3. Не сбрасывать `no_progress_count` на НЕвалидный `new_disc`: сбрасывать только на
   `new_valid` (или на discovery, который потом дал valid).
4. При `exhausted` явно вызывать `break` с финальным `COMPLETED_MIN`/`ABANDONED`.

> Все гипотезы требуют согласования с реальной семантикой `no_progress_count` и
> `max_no_progress_runs` в текущих тестах.