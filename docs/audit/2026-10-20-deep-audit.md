# Глубокий аудит ozon-collector — 2026-10-20

Объём: весь репозиторий (Python, PowerShell, bash, JS-экстракторы, тесты).
Цель: найти и починить критические дефекты, укрепить ключевые модули и
оставить проверяемый след каждого изменения.

Проверка на момент отчёта:

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1
ALL GREEN — 1906 passed, 6 skipped, 1 xfailed (301.67 s)
```

Живой сбор карточки/кампании после правок не запускался: «Не проверено
вживую: полный collect на реальной кампании».

---

## Часть 1. Критические баги (P1)

### P1-1. Кэш отдавал отзывы другого варианта товара

- Где: `ozon_collector/cache.py:178` (`_covers_variant`), `:206` (`_best_raw`),
  `ozon_collector/parallel.py`, `ozon_collector/gather.py`.
- Симптом: при повторном сборе кэш мог вернуть `raw.json` любого варианта того
  же PID. Инвариант I1 («собранное = отзывы именно этого варианта») нарушался
  молча.
- Причина: кэш проверял только PID/свежесть и не сверял `target_variant`,
  `reviews_variant_mode` и `variant_count`.
- Фикс: `_covers_variant` требует точного `stored == target_variant` для
  явного варианта и отвергает variant-slice для запроса всего товара. Для
  пустого target raw с нативным фильтром (`used=True`) считается
  представлением товара (его `variant_count` — активный вариант, а не
  `rating_count`); `variant_count=0` принимается только при
  `no_reviews_confirmed` и отсутствии отзывов. Иначе `variant_mismatch`.
  `collect`/`gather` передают `parse_variant(...)`; `variant_review_problems`
  считает `vc=0 + reviews` браком (`zero_variant_but_reviews`).
- Тесты: `tests/test_cache.py`, `tests/test_gather.py`.

### P1-2. Rescan не обновлял canonical `raw.json`

- Где: `scripts/key_daemon.py`, функция повторного поиска после неудачного
  сбора.
- Симптом: повторный поиск писал в новый файл, но `canonical` оставался
  старым; QA и отчёт читали устаревшие данные.
- Фикс: rescan идёт с `max_age_s=0` и `overwrite=True`.
- Тест: `tests/test_key_daemon.py`.

### P1-3. `disabled` считался допустимым исходом варианта

- Где: `ozon_collector/collect.py:111` (`VM_BAD_REASONS`), `:114`
  (`variant_review_problems`), `ozon_collector/gather.py`.
- Симптом: отзывы с `reviews_variant_mode.used=False, reason="disabled"`
  попадали в `vm_bad`, но не считались браком; I1 снова обходился.
- Фикс: `disabled` исключён из `VM_BAD_REASONS` и получает отдельный
  `filter_disabled`. Ослабление разрешено только для `no_variant_mode`.
- Тесты: `tests/test_gather.py` (strict + cache-hit), `tests/test_collect*`.

### P1-4. `reviews_url` мог увести на чужой товар или хост

- Где: `ozon_collector/collect.py:2161` (`_require_same_origin`), `:2173`
  (`_reviews_tab_url`), `:2316` (`pid_from_url`).
- Симптомы:
  - ссылка `.../product/456/reviews/` давала `pid_from_url = "unknown"`, и
    проверка «PID mismatch» пропускалась; отзывы чужого товара писались в raw
    запрошенного PID;
  - `https://evil.example/product/123/reviews/` проходил до навигации;
  - наивная склейка давала `...?at=...?tab=reviews`.
- Фикс: PID извлекается из сегмента `/product/<slug>-<id>` в любом месте пути;
  `reviews_url` приводится к absolute через `urljoin(product_url, ...)`,
  проверяется same-origin, затем строгий PID; `unknown` больше не пропуск.
- Тесты: `tests/test_reviews.py` (foreign host, `/reviews/`, trailing path,
  relative path).

### P1-5. Очистка профиля могла убить чужой браузер

- Где: `ozon_collector/parallel.py` (`_stop_main_browser`,
  `_release_profile_children`), `ozon_collector/cdp.py:357`
  (`_free_stale_profile`).
- Симптом: `browser_owner="external"` не учитывался; очистка после кампании
  или неудачного запуска убивала главный процесс, который коллектор не
  запускал.
- Фикс: external никогда не останавливается; после ожидания освобождения
  профиля убиваются только helpers, main не трогается. Если появился чужой
  main — очистка прекращается, marker снимается.
- Тесты: `tests/test_parallel.py`, `tests/test_cdp_launch.py`.

### P1-6. Stale/legacy marker объявлял ручной Chrome «нашим»

- Где: `ozon_collector/cdp.py:1659` (`_launch_marker_pid`), `:683`
  (`Session._connect`).
- Симптомы:
  - маркер умершего запуска с живым PID (PID reuse) давал ownership;
  - helper PID в маркере мог авторизовать kill main;
  - `_connect` не сверял marker PID с браузером на `cdp_url`.
- Фикс: marker — JSON `{pid, started_at}`; wall-clock старт процесса
  (`process_start_epoch`, Windows FILETIME / Linux btime) сверяется с
  `started_at` односторонне: процесс не позже marker+2 с и не раньше
  marker−60 с, поэтому recycled PID отвергается. `launch_chrome.ps1` пишет
  marker сразу после PID с фактическим `StartTime` (не временем записи) и
  обновляет его после готовности CDP; `launch_chrome.sh` пишет marker после
  проверки exe. Legacy timestamp-only маркеры не принимаются. `_connect`
  требует marker PID == `_browser_pid(cdp_url, profile)`; иначе `external`.
  `_free_stale_profile` требует marker PID среди holders; helper-marker не
  авторизует main.
- Тесты: `tests/test_cdp_launch.py` (PID reuse, stale, missing started_at,
  marker/main mismatch, helper marker, future marker).

### P1-7. `INTERRUPTED` не снимался при resume

- Где: `scripts/key_daemon.py`.
- Симптом: после Ctrl+C/аварии кампания навсегда оставалась `INTERRUPTED`,
  resume не стартовал.
- Фикс: `_clear_interrupted_state` вызывается на пути запуска; битый
  `progress.json` теперь fail-fast, а не тихий сброс.
- Тесты: `tests/test_key_daemon.py`.

### P1-8. QA зависел от CWD и терял PID в batch

- Где: `scripts/key_daemon.py` (`run_qa_batch`), `scripts/qa_raw.py`.
- Симптомы:
  - `--data-dir` относительный → QA читал не тот каталог;
  - исчезнувший raw выпадал из `products`, `products != len(batch)`, демон
    считал весь batch `parse_error`;
  - `--since` мог скрыть запрошенный PID.
- Фикс: абсолютный `data_dir` + `cwd=ROOT`; missing PID получает broken-row;
  новый файл после discovery досканируется; `--since` + `--pids` запрещены.
- Тесты: `tests/test_qa_raw.py`.

### P1-9. `reviews_url` с PID в конце пути обходил защиту

Найден на втором ревью: `pid_from_url` смотрел только конец URL. Закрыт в
P1-4 (regex по `/product/` + same-origin). Отдельный тест:
`tests/test_reviews.py::test_reviews_tab_url_rejects_pid_with_extra_path_segments`.

---

## Часть 2. Улучшенные ключевые модули

### `ozon_collector/cache.py`

- `_covers_variant` — единственная точка проверки варианта.
- `_best_raw` возвращает причину (`hit`, `variant_mismatch`, `stale`) вместо
  неявного «первый файл».
- Комментарии объясняют, почему допуск 3% и минимум 1.

### `ozon_collector/collect.py`

- `VM_BAD_REASONS` и `variant_review_problems` разделяют «плохой клик» и
  «фильтр выключен».
- `_require_same_origin` / `_reviews_tab_url` — граница доверия к внешнему URL.
- `pid_from_url` — строгий разбор сегмента товара.
- `_finalize_variant_mode`, seed сортировок, type guards.
- Логирование решений без ложного `out=`.

### `ozon_collector/process.py`

- `profile_process_roles` — main/helper разделение.
- `process_start_epoch` — привязка marker к инстансу процесса.
- `profile_processes` docstring больше не обещает ownership.

### `ozon_collector/cdp.py`

- `_launch_marker_pid` — ownership по PID + времени старта.
- `_free_stale_profile` — marker-membership, helper-only cleanup, сохранение
  marker при выжившем main.
- `Session._connect` — сверка marker PID с main на порту; external не трогаем.
- Закрытие Playwright при ошибке инициализации.
- `_ozon_browser_owner`, window policy для external.

### `ozon_collector/parallel.py`

- `_stop_main_browser(owner)` уважает external.
- `_release_profile_children`: helpers только после исчезновения main; чужой
  main неприкосновенен.
- `_clear_launch_marker` после полного освобождения.

### `scripts/key_daemon.py`

- Rescan с `overwrite=True`.
- `_clear_interrupted_state`.
- Fail-fast на битом `progress.json`.
- `_run_sort_key`, `validate_campaign_name` (legacy-имена разрешены,
  Windows-reserved/точка/пробел в конце — нет).
- QA batch: `--data-dir` absolute + `cwd=ROOT`.

### `scripts/qa_raw.py`

- Batch-контракт `products == len(batch)`.
- Повторный scan появившегося файла.
- Broken-row для отсутствующего PID.
- `--since` + `--pids` несовместимы.

### `scripts/launch_chrome.ps1` / `scripts/launch_chrome.sh`

- Marker пишется после старта, а не до него.
- PS1: marker при `ready` или живом PID с молчащим CDP; listener проверяется
  по `127.0.0.1`; делегированный запуск не перезаписывает marker.
- SH: pgrep по точному basename профиля, не подстроке (worker-профили).
- Кавычки аргументов и отсутствие `--enable-automation`.

### `ozon_collector/window_policy.py`, `puzzle_capture.py`

- `_uv_suspended` + повторная проверка режима.
- External browser не показывается и не трогается.

---

## Часть 3. Полный список изменений и обоснований

### Код

| Файл | Что изменено | Зачем |
|---|---|---|
| `ozon_collector/cache.py` | `_covers_variant`, `_best_raw`, reason | I1: вариант в кэше |
| `ozon_collector/collect.py` | `VM_BAD_REASONS`, `variant_review_problems`, URL-проверки, `pid_from_url`, sort seed, type guards, finalize, логи | I1 и безопасность URL |
| `ozon_collector/gather.py` | `allow_filter_disabled` только для `no_variant_mode` | I1 |
| `ozon_collector/process.py` | `profile_process_roles`, `process_start_epoch`, docstring | main/helper, PID reuse |
| `ozon_collector/cdp.py` | marker, `_free_stale_profile`, `_connect`, Playwright cleanup | ownership и утечки |
| `ozon_collector/parallel.py` | external stop, helper-only cleanup, marker | чужой браузер |
| `ozon_collector/puzzle_capture.py` | не поднимать external | окно пользователя |
| `ozon_collector/window_policy.py` | `_uv_suspended`, recheck | режимы окон |
| `cli.py` | `_parse_sorts`, убран ложный `out=` | корректный разбор/лог |
| `scripts/key_daemon.py` | rescan, INTERRUPTED, corrupt progress, sort, campaign names, QA cwd | resume и batch |
| `scripts/qa_raw.py` | missing/duplicate rows, `--since`+`--pids` | batch-контракт |
| `scripts/run_tests_guarded.py` | guard против `kill(-1)` | безопасность reaper |
| `scripts/launch_chrome.ps1` | marker после старта, listener, quoting | ownership |
| `scripts/launch_chrome.sh` | marker после старта, `/proc/exe`, точный pgrep | ownership |
| `.gitignore` | `.commandcode/` | гигиена |

### Тесты

Добавлены/обновлены:

- `tests/test_cache.py` — вариант в кэше.
- `tests/test_gather.py` — strict `disabled`, cache-hit.
- `tests/test_cdp_launch.py` — stale/legacy/PID-reuse marker, helper marker,
  marker/main mismatch, dead-main cleanup, foreign main.
- `tests/test_parallel.py` — external, helper-only release, crash cleanup.
- `tests/test_qa_raw.py` — missing PID row, `--since` + `--pids`.
- `tests/test_key_daemon.py` — legacy campaign names, reserved names.
- `tests/test_reviews.py` — foreign host, `/reviews/`, relative URL.
- `tests/test_launch_scripts.py` — marker для живого Chrome с молчащим CDP,
  точный pgrep.
- `tests/test_window_policy.py`, `tests/test_puzzle_capture.py` — режимы окон.
- `tests/test_parallel_batch_one.py` — batch merge.

### Проверки

| Когда | Команда | Результат |
|---|---|---|
| базовая | полный verify до правок | 1847 passed, 6 skipped, 1 xfailed |
| после волны 1 | focused | 683 / 143 / 89 passed |
| после волны 2 | focused | 351 passed |
| после волны 2 | полный verify | 1873 passed |
| после URL/marker | focused | 347 passed |
| после URL/marker | полный verify | 1883 passed |
| после launcher-fix | focused | 322 / 185 passed |
| после ownership/recycle | focused | 106 / 245 / 290 / 104 passed |
| после cache/launcher | focused | 337 passed |
| финал | полный verify | **1906 passed, 6 skipped, 1 xfailed (301.67 s)** |

### Ревью

- Раунд 1: 2 MAJOR (тупик очистки профиля, утечка I1 через кэш), 4 MINOR,
  4 NIT — закрыты.
- Раунд 2: 1 BLOCKER (legacy marker), 3 MAJOR (kill всех holders, marker на
  произвольного держателя, потеря QA-строк), 1 MINOR — закрыты.
- Раунд 3: 1 MAJOR (PS1 не метил живой Chrome с молчащим CDP), 4 MINOR, 2 NIT
  — закрыты; targeted 185/322 passed, полный verify 1886.
- Раунд 4: 1 BLOCKER (recycle до marker), 1 MAJOR (`_stop_main_browser`
  передавал token вместо пути) — закрыты; полный verify 1896.
- Раунд 5: 1 BLOCKER (PID, стартовавший после marker), 1 MAJOR (reconnect не
  подтверждал ownership), MINOR — закрыты; полный verify 1900.
- Раунд 6: 1 BLOCKER (cache принимал срез варианта за весь товар), 2 MAJOR
  (PS1 marker после 210-с цикла; потеря `RestartResult`), MINOR — закрыты;
  по замечаниям кэш ослаблен до `stored`-контракта + confirmed-zero,
  `_stop_main_browser` различает missing/dead marker, PS1 пишет ранний marker.
  Финальный focused 667, полный verify 1906.
- Открытые MINOR/NIT после раунда 6 (follow-up): promote при `os.replace`
  failure, stale marker с reused PID в PS1 (закрыт), Linux `.sh` без marker
  предупреждает, `docs`-нумерация строк.

### Что осталось

- Живой сценарий: короткий collect/QA на реальной кампании (проверить
  marker, окна, характеристики).
- Linux `launch_chrome.sh` вживую не запускался.
- `docs/tasks/current.md` / `state.json` обновлены на 15-й заход.
