# Архитектура key_daemon.py

Автономный супервизор кампаний сбора Ozon. Это **один** долгоживущий Python-процесс,
который сам управляет всей кампанией: от запуска CLI до финального QA.

---

## 1. Зачем нужен

Раньше сбор вели руками: `cli.py search --limit 300` → получили товары → руками
проверили качество → нашли брак → снова `cli.py search --pids 123,456`. Это
трудоёмко и ненадёжно.

**key_daemon** делает всё это автоматически:
1. Запускает `cli.py search` итеративно
2. После каждого прогона проверяет качество (qa_raw)
3. Бракованные PID пересобирает (rescan)
4. При challenge-storm (FAB-заглушка) делает паузу и повторяет
5. При достижении цели — финальный QA с заменой брака из резерва
6. При исчерпании лимитов — корректно завершается

---

## 2. Файл-манифест: progress.json

Вся информация о кампании хранится в `data/campaigns/{campaign}/progress.json` —
это **единственный источник состояния**. Демон может умереть в любой момент и
продолжить с того же места (атомарная запись через temp + os.replace).

### Основные поля

```json
{
  "campaign": "prezervativy",
  "target_count": 300,         // цель: сколько ЧИСТЫХ PID нужно
  "min_valid": 200,            // минимум для частичного успеха
  "url": "...",                // URL категории
  "out": "docs/prezervativy.md",

  "run_cfg": {                 // ВСЕ параметры запуска — источник истины при resume
    "data_dir": "data",
    "max_age": "7d",
    "min_reviews": 40,
    "parallel": 1,
    "max_no_progress_runs": 3,
    "max_rescans": 2,
    "max_total_hours": 12.0,
    "reserve_count": 30,
    "challenge_pause": 60,
    "max_transient_retries": 3
  },
  "run_cfg_hash": "sha256...", // fingerprint run_cfg; ручная правка → run_cfg_modified

  "qa_version": "1.8",          // версия QA-логики (key_daemon.QA_VERSION); инвалидирует verdict_meta при изменении

  "campaign_identity": {        // идентичность кампании (защита от смены категории при resume)
    "url_sha256": "sha256...",
    "schema_version": "...",
    "collector_version": "..."
  },

  "started_at": "2026-08-31T10:00:00+00:00",  // фиксируется один раз, от него дедлайн

  "discovered_pids": ["123", "456", ...],  // все PID, когда-либо увиденные (порядок обнаружения)
  "valid_pids": ["123", "456", ...],       // прошли QA без FAIL (порядок = порядок подтверждения)
  "failed_pids": ["789"],                  // FAIL, подлежат rescan
  "permanently_failed_pids": ["012"],      // исчерпали max_rescans

  "verdicts": {"123": "OK", "456": "WARN", "789": "FAIL"},  // QA-вердикты
  "verdict_meta": {           // fingerprint QA-кэша пида (инвалидация вердикта)
    "123": {"verdict": "OK", "raw_mtime_ns": 1725100800123456789,
            "raw_sha256": "ab12...", "raw_size": 2048,
            "qa_version": "1.8", "qa_at": "2026-08-31T10:01:00+00:00"}
  },

  "running_rescans": {"789": 1},           // PID → число попыток rescan

  "runs": [/* история каждого прогона */],

  "cooldown_until": null,       // ISO-время до которого спать
  "challenge_seq": 0,           // сколько challenge подряд
  "no_progress_count": 0,       // сколько прогонов без прогресса

  // разделённые счётчики «нет прогресса» по типам
  "progress": {"new_discovered": 0, "new_valid": 0,
               "rescans_fixed": 0, "rescans_failed": 0},
  "stall": {"full_runs_without_discovery": 0, "full_runs_without_valid": 0,
            "challenge_runs": 0, "crash_runs": 0},

  "transient_failures": 0,   // счётчик транзиентных ретраев UNKNOWN_FAILURE
  "last_error": null,        // последнее человекочитаемое сообщение об ошибке
  "run_cfg_modified": false, // true, если run_cfg вручную поправлен после создания
  "runtime_state": "STARTING",// живой runtime-state (для status.json)

  "campaign_final_pids": [],    // immutable snapshot (target + reserve), фиксируется при достижении цели
  "campaign_reserve_pids": [],  // остаток резерва после финального QA
  "campaign_accepted_pids": [], // итоговые чистые PID
  "replacement_pids": [{"failed_pid": "789", "replaced_by": "300"}],

  "final_state": null,          // null | COMPLETED | COMPLETED_MIN | ABANDONED | NEEDS_ATTENTION
  "final_reason": null,         // time_limit | no_progress | target_reached | qa_parse_error | ...
  "final_note": null            // человекочитаемое описание
}
```

> **ВАЖНО про resume:** при перезапуске демон берёт конфигурацию из `run_cfg`
> (манифест), а **не** из новых аргументов CLI. Так лимиты/дедлайны не меняются
> между запусками, и кампания ведётся с теми же правилами.

---

## 3. Главный цикл: run_campaign()

```
┌─────────────────────────────────────────────────────────────┐
│                     ГЛАВНЫЙ ЦИКЛ                           │
│                                                             │
│  while True:                                                │
│    ┌─────────────────────────────────────────────────┐      │
│    │ 1. ПРОВЕРКИ ВЫХОДА                             │      │
│    │    - final_state в TERMINAL_STATES? → break     │      │
│    │    - время > max_total_hours? → time_limit      │      │
│    │    - no_progress_count >= max? → no_progress    │      │
│    └─────────────────────────────────────────────────┘      │
│                          ↓                                  │
│    ┌─────────────────────────────────────────────────┐      │
│    │ 2. COOLDOWN                                     │      │
│    │    cooldown_until в будущем? → sleep(N)          │      │
│    │    (не пересекает max_total_hours — дедлайн)    │      │
│    └─────────────────────────────────────────────────┘      │
│                          ↓                                  │
│    ┌─────────────────────────────────────────────────┐      │
│    │ 3. ВЫБОР РЕЖИМА                                │      │
│    │    есть failed + попытки < max_rescans?         │      │
│    │      → mode="rescan", targets=failed            │      │
│    │    иначе failed исчерпаны?                      │      │
│    │      → permanently_failed, mode="full"          │      │
│    └─────────────────────────────────────────────────┘      │
│                          ↓                                  │
│    ┌─────────────────────────────────────────────────┐      │
│    │ 4. CLI SEARCH                                  │      │
│    │    subprocess: cli.py search [--pids ...]       │      │
│    │    stdout → лог, combined_{run_id}.json         │      │
│    └─────────────────────────────────────────────────┘      │
│                          ↓                                  │
│    ┌─────────────────────────────────────────────────┐      │
│    │ 5. КЛАССИФИКАЦИЯ                               │      │
│    │    classify_status → NORMAL_DONE | CHALLENGE     │      │
│    │                      | CRASH | UNKNOWN_FAILURE   │      │
│    └─────────────────────────────────────────────────┘      │
│                          ↓                                  │
│    ┌─────────────────────────────────────────────────┐      │
│    │ 6. QA НОВЫХ PID                                │      │
│    │    classify_new_pids → OK/WARN/FAIL per pid     │      │
│    │    reconcile_validity → обновить valid/failed    │      │
│    │    ЕСТЬ прогресс (new_disc или new_valid)?      │      │
│    │      → СБРОС: no_progress_count = 0,           │      │
│    │               challenge_seq = 0                 │      │
│    └─────────────────────────────────────────────────┘      │
│                          ↓                                  │
│    ┌─────────────────────────────────────────────────┐      │
│    │ 7. РЕАКЦИЯ НА СТАТУС                           │      │
│    │    CHALLENGE → cooldown (challenge_pause сек)    │      │
│    │    CRASH → cooldown (300 сек)                   │      │
│    │    UNKNOWN_FAILURE → NEEDS_ATTENTION (стоп)     │      │
│    │    NORMAL_DONE → no_progress_count++ (если 0)   │      │
│    └─────────────────────────────────────────────────┘      │
│                          ↓                                  │
│    ┌─────────────────────────────────────────────────┐      │
│    │ 8. ДОСТИЖЕНИЕ ЦЕЛИ                             │      │
│    │    valid >= target? → snapshot+reserve          │      │
│    │    → save_json (immutable) → break              │      │
│    └─────────────────────────────────────────────────┘      │
│                          ↓                                  │
│    save_json(progress_path, progress)  ← атомарно          │
│                                                             │
└─────────────────────────────────────────────────────────────┘
                          ↓
              finalize_campaign()
```

---

## 4. QA-пайплайн (K4-инварианты)

### 4.1 Инварианты достоверности (K4)

Главное правило: **доверяем qa_raw только если сводка достоверна**.

```
1. products == len(batch)        ← QA обработало ровно столько, сколько передали
2. ok + warn + fail == products  ← сумма вердиктов совпадает с общим числом
3. если fail > 0 → per_pid ДОЛЖЕН покрывать ВСЕ PID батча
   (иначе fail-пид может получить fallback OK — тихий пропуск брака)
```

При нарушении любого → `parse_error=True` → **NEEDS_ATTENTION** (терминально).

> **Машинный контракт:** демон читает результат QA из **JSON-файла**
> (`qa_raw --report-json <path>`: `{summary, products:[{pid, verdict, issues}]}`),
> а не из текстового stdout. stdout парсится только как fallback для старых
> версий qa_raw. Это устраняет хрупкость regex-контракта.
> Из JSON берутся и **коды issues** — они позволяют детектировать
> детерминированный брак (см. §6 «rescan»).

> **ВАЖНО:** проверка K4 выполняется **в обоих местах**:
> - в `classify_new_pids` (живой QA после каждого прогона CLI);
> - в `_qa_clean` финализатора (финальный QA по snapshot).
> Раньше финализатор доверял `per_pid.get(pid, "OK")` без проверки — теперь
> недостоверная сводка на финале тоже уводит кампанию в NEEDS_ATTENTION.

### 4.2 Живой QA новых PID: classify_new_pids()

После каждого прогона демон берёт OK-пиды из `combined.json` и гоняет через
`qa_raw.py --pids ... --no-report`.

**mtime-инвалидация:** если `data/{pid}_raw.json` изменился после последнего QA
(по mtime), вердикт перепроверяется. Это ловит случай, когда rescan обновил raw,
но вердикт остался старым.

**Источник истины — П1 (наличие canonical raw).** Пиды без raw-файла вообще не
попадают в QA. И **важно**: `reconcile_validity` при каждом обновлении выбрасывает
из `valid_pids` пиды, у которых raw-файл **исчез** (удалён) — старый вердикт OK
не удерживает PID в valid. Если `data/{pid}_raw.json` удалён после QA — PID
автоматически перестаёт быть valid.

### 4.3 Финальный QA: finalize_campaign()

При завершении кампании (по любой причине) запускается финальный QA по snapshot.

**Путь `target_reached`:**
```
snapshot = target + reserve (резервные PID)   ← immutable, фиксируется СРАЗУ
pool = unique(snapshot + reserve)
clean, total_fail, parse_error = qa(pool)   ← финальный QA по всему пулу
if parse_error → NEEDS_ATTENTION            ← недостоверная сводка (K4)

clean_from_snapshot = snapshot ∩ clean
failed_in_snapshot = snapshot - clean
shortfall = target - len(clean_from_snapshot)
replacements = clean_reserve[:shortfall]

final_pids = clean_from_snapshot + replacements  ( ≤ target )
replacement_pids = [{failed_pid, replaced_by}]
```

Бракованные из snapshot заменяются чистыми из резерва.
Если после замен чистых < target → **ABANDONED** (COMPLETED_MIN для
target_reached недопустим — демон уже доказал, что мог выйти на target).

**Мягкие остановки (no_progress, time_limit, exhausted):**
```
snapshot = valid_pids[:target]
clean, total_fail, parse_error = qa(snapshot)
if parse_error → NEEDS_ATTENTION
clean ≥ target → COMPLETED
clean ≥ min_valid → COMPLETED_MIN
иначе → ABANDONED
```

### 4.4 Приоритет статусов (K1)

```
NEEDS_ATTENTION  >  COMPLETED  >  COMPLETED_MIN  >  ABANDONED
```

Это приоритет **защиты от перезаписи**, а не «лучший → худший»:
- `NEEDS_ATTENTION` — терминален, **никогда** не перезаписывается финализатором
  (он даже не прогоняет финальный QA — нет доверия к данным);
- остальные статусы выставляет **только финализатор по числам** (clean/target/min_valid);
- мягкие остановки — только `COMPLETED_MIN` (частичный успех);
- `COMPLETED_MIN` только при `FAIL == 0` и `clean ≥ min_valid`;
- при `target_reached` с `clean < target` → `ABANDONED` (не `COMPLETED_MIN`).

**Инварианты итоговых списков (для `COMPLETED`):**
```
len(campaign_accepted_pids) == target
set(campaign_accepted_pids) ⊆ set(campaign_final_pids) ∪ set(campaign_reserve_pids)
```
Для `COMPLETED_MIN`: `len(campaign_accepted_pids) >= min_valid`, каждый PID имеет
актуальный `OK/WARN`-вердикт и существует его raw-файл.

---

## 5. Классификация прогонов

| Статус CLI         | combined.json | exit_code | Значение                                   |
|--------------------|---------------|-----------|--------------------------------------------|
| NORMAL_DONE        | есть          | 0         | Успешный прогон                            |
| CHALLENGE_STORM    | aborted=true  | *         | FAB-заглушка (challenge)                   |
| CRASH              | нет           | ≠ 0       | Процесс упал, combined не записан          |
| UNKNOWN_FAILURE    | есть          | ≠ 0       | Непредвиденная ошибка → NEEDS_ATTENTION    |

### Challenge-storm

Challenge — это не бан, а FAB-заглушка Ozon. Демон:
1. Ставит `cooldown_until = now + challenge_pause` (фиксированная пауза, **не растёт**)
2. Увеличивает `challenge_seq`
3. Если прогресса нет → `no_progress_count++` (защита от вечного цикла)
4. Если прогресс есть → сбрасывает оба счётчика

### Стагнация

Если NORMAL_DONE не дал ни нового discovered, ни нового valid →
`no_progress_count++`. Выдача перетирается известными товарами, дальше греться
некуда — иначе демон крутился бы вечно при живой, но не растущей выдаче.

---

## 5.5 Проброс невыполненных товаров между прогонами (2026-10-06)

`discovered_pids` пополняется сразу после докрутки выдачи, до сбора. Поэтому
«известные пиды» нельзя целиком выкидывать из `skip_pids`: товары, до которых
воркеры не дошли (`SESSION_EXHAUSTED`, `BROWSER_DEAD`, конец прогона), выпадали
из работы навсегда — в логе было «пропущено 336 уже обработанных (осталось 0)»
при 66 `raw.json` на диске.

- `split_done_pending(progress)` → `(done, pending)`: «обработан» = valid / FAIL /
  permanently_failed / canonical raw на диске; `pending` возвращается в очередь.
- `pending_pids(progress)` и ключ `pending` в `summarize()` показывают недобор;
  строка статуса печатает «БЕЗ РЕЗУЛЬТАТА: N — вернутся в очередь».
- `pending_attempts` в состоянии ограничивает бесконечный повтор: после
  `PENDING_ATTEMPT_LIMIT` (3) попыток пид уходит в `permanently_failed_pids` и
  больше не берётся в работу. Счётчик сбрасывается сам: в нём остаются только
  пиды без результата, поэтому появление raw его обнуляет.
- `parallel.py` подставляет `pid: None` для индексов без результата (воркер упал
  после пачки) — такие пиды тоже не доходят до `discovered_pids`; починка в
  `merge_shard_results` не сделана, зафиксирована как открытая находка.

## 6. Rescan бракованных PID

```
failed_pids = [pid для pid в discovered если verdict == FAIL]
              - permanently_failed_pids

rescan:
  targets = failed_pids (попытки < max_rescans)
  cli.py search --pids targets --out-raw combined.json  ← пересбор конкретных PID
  → новый combined.json (поле combined.products)
  → сброс вердиктов целей (чтобы перепроверить)
  → classify_new_pids (пере-QA)
  → reconcile_validity (обновить valid/failed)

permanently_failed:
  если попытки >= max_rescans → permanently_failed_pids
  failed_pids = []
```

**Детерминированный брак (повторная причина):** демон запоминает набор кодов
issues для каждого FAIL (`fail_reason_seq`). Если после rescan пид снова FAIL
с **тем же набором причин** (2 раза подряд) — дальнейший пересбор бессмыслен:
PID сразу уходит в `permanently_failed`, не сжигая остаток бюджета `max_rescans`.
Если причина сменилась или пид стал OK/WARN — история сбрасывается.

> **Контракт `search --pids` (подтверждён по коду):** `cli.py search --pids A,B`
> существует (subcommand `search`, флаг `--pids`), передаёт список в
> `collect_search(..., pids=...)`, собирает только эти PID **без докрутки выдачи**
> и при `--out-raw` всегда пишет машиночитаемый `combined.json` с `combined.products`
> (покрыто тестами `test_collect_search_pids_*`). Демон в режиме `rescan` передаёт
> `--pids` + `--out-raw`, без `--url`/`--out` — это штатный путь, не выдуманный.

---

## 7. Защита от повторного запуска

```python
class PidLock:
    # data/key_daemon.pid — JSON {pid, campaign, started_at, start_cmdline}
    # acquire():
    #   если pid-файл есть:
    #     pid — int и процесс жив → return False (занято)
    #     pid битый (не int / нет поля) → трактуем как stale, перезаписываем
    #     процесс мёртв → перезаписываем
    # release(): удалить pid-файл (в finally)
```

Один демон — одна кампания. Повторный запуск блокируется. Битый pid-файл
(невалидный JSON, не-int pid) не роняет демон — он считается stale.

**Известное ограничение (PID-reuse):** проверка — только «PID жив» (`os.kill(pid, 0)`),
без сверки командной строки. Если демон убит жёстко (SIGKILL) и ОС переиспользовала
его PID для другого процесса, новый запуск может ошибочно счесть демон живым и
отказаться стартовать. Чтобы обойти: удалить `data/key_daemon.pid` вручную.
(Полноценная сверка cmdline без psutil — сложная кроссплатформенная задача, не в приоритете.)

---

## 8. Детерминизм: почему порядок PID важен

Раньше snapshot/reserve выбирались через `list(set(valid_pids))` — порядок `set()`
зависел от hash-функции Python (PYTHONHASHSEED). На resume с другим seed
snapshot выбирался **по-другому**, что делало кампанию невоспроизводимой.

Теперь порядок везде детерминированный:
- `discovered_pids` и `valid_pids` хранятся в **порядке обнаружения/подтверждения**
- snapshot = первые `target` из `valid_pids` в этом порядке
- reserve = следующие `reserve_count`

Тот же список входных данных → тот же snapshot, независимо от процесса/seed.

**Immutable snapshot:** при достижении `target` списки `campaign_final_pids`
и `campaign_reserve_pids` сразу сохраняются в манифест (до финального QA).
При resume они **не пересобираются** из текущего множества `valid` — фиксация
однажды, навсегда. Это гарантирует, что финальный QA проверяет ровно тот же
набор PID, что и был выбран в момент достижения цели.

---

## 9. Запуск

```bash
python scripts/key_daemon.py \
    --campaign prezervativy \
    --url "https://www.ozon.ru/category/prezervativy-14818/?..." \
    --limit 300 \
    --min-valid 200 \
    --out docs/prezervativy.md \
    --max-age 7d \
    --min-reviews 40 \
    --parallel 1 \
    --max-no-progress-runs 3 \
    --max-rescans 2 \
    --max-total-hours 12 \
    --reserve-count 30 \
    --challenge-pause 60 \
    --notify toast
```

### Параметры

| Параметр              | Дефолт | Описание                                          |
|-----------------------|--------|---------------------------------------------------|
| `--campaign`          | —      | Имя кампании (обязательный)                       |
| `--url`               | —      | URL категории                                     |
| `--limit`             | 300    | Целевое число PID                                 |
| `--min-valid`         | 1      | Минимум чистых для частичного успеха; **обязателен** для новой кампании: `1 ≤ min-valid < limit` |
| `--out`               | —      | Выходной файл (.md)                               |
| `--max-age`           | 7d     | Возраст отзывов                                   |
| `--min-reviews`       | 40     | Пред-фильтр выдачи по числу отзывов (0 = выключить) |
| `--parallel`          | 1      | Число воркеров                                    |
| `--max-no-progress-runs` | 3  | Лимит прогонов без прогресса                      |
| `--max-rescans`       | 2      | Макс. попыток пересбора бракованного PID           |
| `--max-total-hours`   | 12.0   | Жёсткий дедлайн (часы от started_at первого запуска) |
| `--reserve-count`     | 30     | Резервных PID сверх target                        |
| `--challenge-pause`   | 60     | Пауза (сек) при challenge (фиксированная)         |
| `--notify`            | off    | Уведомления: off / toast                          |
| `--reset`             | off    | Полный сброс существующей кампании: удаляет `data/campaigns/<name>` (progress.json, status.json, events.jsonl, runs/) и начинает сбор заново — требуется `--url`. Сырой кеш товаров (`data/*_raw*.json`) НЕ трогается |

> При resume параметры из CLI игнорируются в пользу сохранённых в `run_cfg`
> (кроме `--campaign`, который выбирает, какую кампанию возобновить).
>
> `--reset` — единственный способ «перезапустить» кампанию, уже завершившуюся
> терминальным состоянием: без него демон просто выходит на старте
> (`final_state` ∈ TERMINAL). Сброс выполняется только до загрузки манифеста,
> поэтому старые `discovered/valid/runs` не наследуются.

**Замечания к параметрам:**
- `--min-valid` — для новой кампании обязателен и валидируется (`1 ≤ min_valid < limit`).
  Значение `0` отклоняется: иначе мягкая остановка с 0 чистых давала бы бессмысленный
  `COMPLETED_MIN`.
- `--min-reviews` — **пред-фильтр** выдачи (`cli.py search` отсеивает товары с меньшим
  числом отзывов). Может сделать `target` недостижимым, если в категории мало «толстых»
  карточек. Для кампании можно задать `0` (собирать всех кандидатов, качество решает QA).
- `--max-total-hours` — жёсткий дедлайн от **первого** `started_at` (сохраняется в
  манифесте), включает все паузы/cooldown и перезапуски. Если демон долго простаивал
  между перезапусками и дедлайн уже прошёл — кампания сразу завершится
  (`time_limit`). Это осознанное поведение «стены», а не накопленное активное время.

---

## 10. Состояния кампании

| final_state       | Описание                                              | TERMINAL? |
|-------------------|-------------------------------------------------------|-----------|
| `null`            | Кампания работает                                     | нет       |
| `COMPLETED`       | len(accepted) == target, финальный QA по snapshot      | да        |
| `COMPLETED_MIN`   | min_valid ≤ accepted < target, мягкая остановка        | да        |
| `ABANDONED`       | accepted < min_valid, или target_reached без покрытия брака резервом | да |
| `NEEDS_ATTENTION` | Ошибка QA / CLI, нужен человек                        | да        |

### Причины завершения (final_reason)

| Причина              | Описание                                    |
|----------------------|---------------------------------------------|
| `target_reached`     | Достигнут target валидных PID                |
| `time_limit`         | Исчерпан max_total_hours                    |
| `no_progress`        | Исчерпан max_no_progress_runs               |
| `qa_parse_error`     | QA-сводка недостоверна (нарушены K4)        |
| `cli_not_found`      | Не запускается CLI                           |
| `unknown_failure`    | CLI упал с ненулевым кодом при наличии combined → NEEDS_ATTENTION |
| `exhausted`          | (фолбэк) мягкая остановка без конкретной причины — в норме не возникает, т.к. цикл всегда ставит причину до break |

---

## 11. Сигналы и graceful shutdown

- **SIGINT (Ctrl-C):** логирует, удаляет pid-файл, выход
- **SIGTERM:** (только Linux — на Windows в Python нет SIGTERM) аналогично SIGINT
- **KeyboardInterrupt:** ловится в `__main__`, удаляет pid-файл
- **finally в run_campaign:** `lock.release()` — pid-файл удаляется всегда

При обрыве прогресс НЕ теряется: каждый прогон завершается `save_json`.

> **Windows:** на Windows нет SIGTERM (есть `CTRL_C_EVENT`/`CTRL_BREAK_EVENT`).
> Корректный выход обеспечивается Ctrl+C (→ `KeyboardInterrupt`/SIGINT) и
> `lock.release()` в `finally`. Закрытие окна консоли тоже приводит к завершению
> процесса с последующей очисткой pid-файла при следующем «честном» выходе.

---

## 11.1 Кроссплатформенность

Демон работает и на Windows, и на Linux:

- **Toast-уведомления** — Windows-only (PowerShell + SystemSounds). На Linux
  `subprocess.run(powershell ...)` просто не находит программу и молча пропускается
  (best-effort, `try/except`). Никакого влияния на логику.
- **`wmic`** — удалён из кода (был единственным Windows-only вызовом). Поиск
  процессов по `wmic` не используется.
- **`os.kill(pid, 0)`** — кроссплатформенный (проверка живости процесса).
- **Пути/кодировки** — `Path` + `encoding="utf-8"` везде.

Зависимость `psutil` **намеренно не вводится** (см. раздел «Зависимости»).

---

## 12. Логирование и runtime-API

- Консоль: `2026-08-31 10:00:00 INFO     [run_1725100800] CLI (full) старт`
- Файл: `logs/{campaign}_daemon.log` (то же содержимое)
- **status.json** — обязательный runtime API для внешних потребителей:
  `data/campaigns/{campaign}/status.json` (state, current_mode, current_run,
  счётчики). Обновляется на каждом state-переходе через `_set_state`.
- **events.jsonl** — append-only лог всех state-переходов и событий:
  `data/campaigns/{campaign}/events.jsonl` (для аудита/дашборда).
- **run-журнал** — transactional-журнал стадий каждого прогона:
  `data/campaigns/{campaign}/runs/{run_id}.json` со стадиями
  `run_started / run_completed / qa_completed / state_committed`.
  При resume `_recover_interrupted_run` находит последний прогон без
  `state_committed` и повторно применяет discovery + QA + reconcile, чтобы
  не потерять только что собранный raw.

---

## 13. Зависимости

- `cli.py search` — основной сборщик (Playwright + CDP)
- `scripts/qa_raw.py` — QA-гейт (проверка raw-файлов)
- Python stdlib (subprocess, json, time, pathlib)
- **НЕ использует** requests, aiohttp, psutil
- Профиль Chrome: `%LOCALAPPDATA%\opencode\ozon-profile`, CDP 9222

---

## 14. Тесты

212 тестов в `tests/test_key_daemon.py` + `tests/test_cli.py`:

- **Классификация:** challenge_storm, normal_done, crash, unknown_failure
- **QA-пайплайн:** K4-инварианты (живой QA + финализатор),
  verdict_meta-инвалидация (fingerprint: raw_mtime_ns + raw_sha256 + qa_version,
  race-защита до/после QA), per_pid покрытие при fail>0,
  **JSON-контракт (_parse_qa_json_report)**
- **Finalize:** target_reached, no_progress, time_limit, замена из резерва,
  accepted_pids, **K4-parse_error → NEEDS_ATTENTION**, WARN → clean
- **П1 (валидность требует raw):** удаление raw → PID выходит из valid,
  сохраняется при наличии raw
- **Валидация кампании:** `validate_new_campaign` (0 < min_valid < target),
  новая кампания с min_valid=0 или без --url → SystemExit; resume с пустым url OK
- **Сброс счётчиков при прогрессе:** стагнация → прогресс → сброс no_progress_count
- **UNKNOWN_FAILURE → транзиентный retry** (до max_transient_retries, короткая
  пауза), затем → NEEDS_ATTENTION с `final_reason="unknown_failure"`
- **Run-journal recovery:** прерванный прогон (нет state_committed) повторно
  применяется при resume (discovery + QA + reconcile), committed — пропускается
- **Уникальность run_id** при быстрых последовательных прогонах (счётчик)
- **Детерминированный брак:** fail_reason_seq (счётчик повторной причины),
  пропуск бесполезного rescan
- **Детерминизм:** порядок valid_pids сохраняется, snapshot = порядок обнаружения,
  **immutable snapshot сохраняется в манифест до финального QA**
- **Resume:** конфиг берётся из манифеста, а не из аргументов
- **Инфраструктура:** PidLock (включая битый/не-int pid), _alive-гард от
  нереалистичных PID, save/load_json, batch_pids, normalize_pids
- **Интеграция:** полные циклы target→finalize, стагнация, резерв→брак

---

## 15. Итоговые файлы

| Файл                          | Описание                              |
|-------------------------------|---------------------------------------|
| `data/campaigns/{camp}/progress.json` | Манифест кампании              |
| `data/{pid}_raw.json`         | Собранные данные о товаре              |
| `data/key_daemon.pid`         | pid-файл (защита от повторного запуска)|
| `logs/{camp}_daemon.log`      | Лог демона                            |
| `data/campaigns/{camp}/status.json`   | Текущий статус (runtime API, state-переходы) |
| `data/campaigns/{camp}/events.jsonl`   | Append-only лог событий/state-переходов        |
| `data/campaigns/{camp}/runs/{run_id}.json` | Run-журнал стадий (recovery при resume)     |
| `data/campaigns/{camp}/combined_{run_id}.json` | Combined JSON каждого прогона |
| `docs/{camp}.md`              | Выходной отчёт                         |