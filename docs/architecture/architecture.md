# Архитектура ozon-collector

> Единая сводка устройства проекта (актуально на 2026-08-31, collector v1.16.x,
> schema 2.0). Покрывает оба слоя: **ядро коллектора** (`ozon_collector/` +
> `cli.py`) и **автономный супервизор кампаний** (`scripts/key_daemon.py`).
> Источники: README.md, код, PROJECT_NOTES.md, `docs/key_daemon_architecture.md`.

---

## 1. Задача и фундаментальные принципы

Проект собирает **максимально полные данные о товарах Ozon**: карточка
(цена, рейтинг, характеристики, описание, галерея), **отзывы именно
выбранного варианта товара**, OCR инфографики — и складывает это в
машиночитаемый `data/{pid}_raw.json` + человекочитаемый markdown-отчёт.
Есть два режима работы:

- **Разовый сбор** — `gather`/`collect` по конкретному товару или `search`
  по выдаче категории/запроса.
- **Автономная кампания** — `scripts/key_daemon.py` сам ведёт весь цикл:
  итеративный сбор через `search`, контроль качества, пересбор брака,
  финальный QA — до `target` **чистых** PID.

Три незыблемых правила:

1. **«Собранное = Этот вариант товара»** — если у товара есть варианты
   (цвет/размер), включается нативный фильтр Ozon, отзывы чужих вариантов
   отбрасываются. Собранное число должно совпадать с `variant_count`
   (с допуском на стену логина — см. §5).
2. **Никакого headless/API** — Ozon FAB блокирует автоматических клиентов.
   Единственный путь — живой видимый Chromium с реальным профилем,
   управление через CDP (`http://127.0.0.1:9222`).
3. **Data Quality Gate** — после каждого прогона `scripts/qa_raw.py`
   проверяет целостность данных; FAIL считается браком, тихий пропуск брака
   исключён (инварианты K4 в демоне).

---

## 2. Общая картина (два слоя)

```
┌─────────────────────────────────────────────────────────────────────┐
│  СЛОЙ 2: Автономный супервизор кампаний                            │
│  scripts/key_daemon.py                                              │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  итеративный цикл: search → combined → QA (K4) → valid/failed│   │
│  │  rescan брака → добор → target+reserve → финальный QA        │   │
│  │  состояние: data/campaigns/{campaign}/progress.json          │   │
│  └──────────────────────────────────────────────────────────────┘   │
│            │ вызывает                                              │
│            ▼                                                       │
│  СЛОЙ 1: Ядро коллектора                                          │
│  cli.py ──► ozon_collector/ (search/gather/collect/ocr/report)    │
│  │                                                                 │
│  └─► data/{pid}_raw.json, docs/*.md, logs/*.log                    │
└─────────────────────────────────────────────────────────────────────┘
        │
        ▼
scripts/qa_raw.py  ← QA-гейт (офлайн проверка raw; машинный вывод [qa] …)
```

Ключевая идея разделения: **слой 1 собирает и не оценивает**, **слой 2
контролирует и решает**. Демон читает результат каждого прогона из
машиночитаемого `combined.json` (не из текста лога), гоняет кандидатов через
`qa_raw --pids ... --no-report` и ведёт собственный манифест качества.

---

## 3. Модули ядра (`ozon_collector/`)

| Модуль | Роль |
|---|---|
| `cli.py` | Команды: `collect`, `ocr`, `report`, `gather`; флаги; вызов модулей. Поисковый `search` — не CLI-подкоманда, а модуль `search.py` (его вызывает демон) |
| `cdp.py` | `Session`: подключение к Chromium, `goto/evaluate/reload`, watchdog |
| `collect.py` | Ядро: JS-экстракторы карточки/отзывов, скролл-converge, топ-ап виртуализации, фильтры варианта, правила брака |
| `browser.py` | Классификация ошибок (`classify_error`), детект антибота, recycle-политика |
| `process.py` | Запуск/убийство Chromium со сверкой identity процесса (PID-reuse) |
| `gather.py` | Оркестрация товара: кэш → collect → ocr → отчёт; raise брака |
| `search.py` | Кампания: докрутка выдачи, перебор товаров, recovery, combined-json |
| `cache.py` | TTL-кэш raw по `collected_at_utc`, инвалидация по версии коллектора |
| `ocr.py` | tesseract rus + cv2-предобработка, кэш по md5 |
| `report.py` | Markdown из raw, дедуп шаблонных ответов продавца |
| `parallel.py` | Опциональные воркеры со своими браузерами (`--parallel`) |
| `log.py` | Файловый лог `logs/ozon_collector.log` с ротацией; debug только в файл |

Плюс `scripts/qa_raw.py` (QA-гейт), `scripts/launch_chrome.ps1/.sh`,
`scripts/digest_from_raw.py`, `scripts/dom_snapshot.py`,
`scripts/key_daemon.py` (супервизор, §12).

---

## 4. Браузерный слой

- **Движок по умолчанию — Chromium** (`browser.py:DEFAULT_BROWSER_KIND = "chrome"`).
  Ozon отдаёт заглушку «похоже, нет соединения» на первом `goto` любой сессии
  geckodriver/Marionette; webdriver-флаг причиной не является (Chromium с
  `--enable-automation`, `webdriver=true`, проходит). Замеры и исключённые
  причины — в докстринге `browser.py::browser_kind` и `PROJECT_NOTES.md`.
  Firefox-ветка (`cdp.py::_connect_firefox`, `ffsel.py`) — только отладка профилей.
- Chromium запускается скриптом с CDP-портом 9222 и отдельным профилем
  (`%LOCALAPPDATA%\opencode\ozon-profile`): там куки и однажды
  подтверждённая 18+ самопроверка возраста.
- `Session(cdp_url)` берёт `ctx.pages[0]` — одна вкладка на весь прогон;
  сторонние зонды во время прогона вкладку коллектора не трогают.
- Если браузер закрыт — авто-запуск (`OZON_AUTO_LAUNCH=0` отключает).
- Windows: порт 9333 в зарезервированном диапазоне 9255–9354 (WSAEACCES) —
  альтернативные браузеры на **9444**. Системный Chrome через
  `launch_chrome.ps1` работает (подтверждено пробами 2026-09-25); при желании
  зафиксировать версию — `scripts/setup_bundled_chrome.py` + `OZON_CHROME_PATH`.
  Сборка `ms-playwright/chromium` через launch нестабильна — не использовать.
- `process.py` сверяет identity процесса (PID + cmdline + браузер-маркер),
  чтобы не убить чужой процесс при переиспользовании PID.

---

## 5. Пайплайн одного товара (`collect`)

1. **Карточка**: `goto(product_url)` → пауза ~6с → `check_challenge()`
   (детект антибот-заглушки → `AccessChallengeError`) → `_pass_age_gate()`
   (модалка «Подтвердите возраст»; кука сохраняется в профиле) →
   `PRODUCT_JS`: title, цена из `webPrice`, рейтинг + `rating_source`,
   `rating_count`, характеристики, описание, галерея.
2. **Затворы пустоты** (до всякой прокрутки): явный `rating_count == 0`;
   явный `variant_count == 0`; счётчиков нет вовсе + на странице отзывов 0
   карточек (двойная проверка с паузой) → `stop_reason=no_cards_no_count_hint`.
   Экономия ~70–90с на пустом товаре, убирает ложные recycle браузера.
3. **Режим варианта**: `_activate_variant_mode()` кликает тумблер «Этот
   вариант товара» (или URL-параметр `reviewsVariantMode=1`), читает
   `variant_count`; диагностика → `raw.reviews_variant_mode`.
4. **Converge-скролл** (`_scroll_to_converge`): шаги прокрутки, счётчик
   уникальных uuid; плато из 6 стабильных итераций = конец списка.
   0 карточек → до 2 релоадов (антифлейк медленного рендера).
5. **Экстракция**: `REVIEWS_JS` снимает DOM: `uuid, author, date, variant,
   stars, text, helpful`. Карточки без текста сохраняются; отдельный тип
   `rating_only` не вводился — основная масса `stars=0` это потерянные
   звёзды / архивная разметка (>~365 дней), не отдельные «оценки без текста».
6. **Top-up виртуализации** (`_topup_virtualized`): Ozon размонтирует
   верхние карточки при прокрутке; двухфазный объезд (подъём к вершине,
   затем спуск), слить через `_merge_topup` (обновление полей / добавление
   пропавших).
7. **Мульти-сортировки** (`--sorts`, дефолт `published_at_desc,score_asc`):
   проход на сортировку (`?sort=`), объединение по uuid (`review_sort`,
   `review_sorts`). Оптимизации: вариант ≤100 собран целиком первой
   сортировкой → остальные пропускаются; ноль отзывов → ранний break.
8. **Сохранение**: `data/{pid}_raw.json` (schema 2.0, `incomplete`/
   `missing_fields`), DOM-снимок `data/{pid}/dom/{дата}.html.gz`.

**Правило брака** `variant_review_problems()`:
- vc≤100: недобор допускается в пределах допуска 3%
  (`max(1, round(vc*0.03))`) — стена логина на границе плавает ~96–99;
- явный ноль варианта (used=true) — не брак;
- vc>100: требуется ≥90 и равномерность сортировок (≥70% от лучшей);
- used=false с плохим reason (click_error/no_effect/timeout/disabled) —
  брак фильтра варианта.

Нарушение → `VariantShortError` → товар несобран (raw удаляется).

---

## 6. Стена логина — главное ограничение

Без авторизации Ozon отдаёт максимум ~96–100 первых отзывов списка.
vc≤100 — собираем почти целиком; vc>100 — режим `WALL_LIMITED`: ~90+
из разных сортировок (разные подмножества пересекаются, объединение полнее).

**`WALL_LIMITED` не является браком** — это ожидаемое ограничение
неавторизованного Ozon при vc>100.

---

## 7. OCR и отчёт

- `ocr`: галерея/инфографика → tesseract rus (tessdata_best
  автоскачивается), предобработка cv2 (gray→upscale 2000px→CLAHE),
  кэш `data/ocr_cache/` по md5 параметров; `--threads`, `--no-preproc`.
  По умолчанию OCR **выключен** (`--ocr` включает).
- `report`: markdown; дедуп шаблонных ответов продавца (ключ:
  автор + первая фраза; сырые данные не изменяются).

---

## 8. Кампания (`search`) — слой 1

1. URL выдачи открывается, сетка результатов докручивается, пока
   прибавляются товары или не набрано `max(limit,50)`; рекомендации
   исключаются.
2. Для каждого кандидата — `run_gather`: кэш (TTL `--max-age`, статусы
   miss/hit/off/stale; инвалидация по версии коллектора), клиентский
   фильтр `--min-rating`.
3. **`--pids A,B`**: режим точечного сбора конкретных артикулов **без
   докрутки выдачи** (используется демоном для rescan). `--limit`
   автоматически = `len(pids)`.
4. Итоги: `found` / `collected` / `excluded`; combined-json
   `data/search_combined.json` (телеметрия каждого товара: error_kind,
   recycle_attempts, cache/ocr); markdown-дайджест `--out` (+ part2).
5. **`--out-raw`**: combined-json пишется **всегда**, когда задан (включая
   challenge-storm с полем `aborted`) — это контракт для супервизора.

---

## 9. Устойчивость (слой 1)

- Watchdog живости вкладки/браузера.
- Классификация ошибок: browser / renderer_failure / access_challenge /
  structure / variant_short / target / unknown; recyclable-типы →
  **recycle браузера** (kill с identity-сверкой → запуск нового →
  переподключение → повтор товара, лимит 2 попытки).
- **Challenge-storm** в CLI: 3 AccessChallengeError подряд →
  самопрерывание прогона (поле `aborted` в combined). Демон поверх CLI
  трактует это как **короткую фиксированную паузу** (`--challenge-pause`),
  а не длинный растущий cooldown.
- Preventive recycle после каждых ~200 товаров.
- Файловый лог с ротацией; debug-слой восстанавливает хронологию сбоя.
- Эксплуатация: запуск через Start-Process переживает таймаут шелла;
  проверять состояние по файлам/логам, а не только по pid.

---

## 10. QA-гейт (`scripts/qa_raw.py`)

Вердикты по товару:
- **FAIL** — BAD_JSON, дубликаты uuid, CHALLENGE_PAGE, OVERCOUNT,
  GARBAGE_TEXT, FUTURE_DATE, STARS_OVER_HISTOGRAM;
- **WARN** — UNDER_TOLERANCE (>допуск при FULL), ZERO_REVIEWS,
  SHORT_TOTAL, STARS_MISSING (>5%), DUP_TEXT, EMPTY_TEXT, SORT_IMBALANCE,
  FALLBACK_OFFICIAL_COUNT;
- **INFO** — LOW_COVERAGE у стены, NO_OFFICIAL_COUNT, а также коды
  UNAUTH_WALL / AUTH_UNKNOWN / AUTH_NOT_MEASURED (причина недобора у
  сессионного среза — см. ниже).

`collection_mode`: FULL (vc≤100) / WALL_LIMITED (>100) / UNKNOWN.
Exit code 1 только при FAIL (`--strict` эскалирует WARN). Допуск FULL =
`round(official × 0.03)`. Отчёты: `docs/qa/qa_<ts>.{md,json}` + `_latest.*`.

**Машинный вывод** для супервизора:
- JSON-контракт: `qa_raw --report-json <path>` пишет `{summary, products:
  [{pid, verdict, collection_mode, collected, issues:[{code,severity}]}]}` —
  демон читает именно его (stdout — только для человека);
- сводная строка `[qa] товаров: N | OK a | WARN b | FAIL c | WALL_LIMITED k`
  — для консоли/отладки, не для машинного парсинга.

### Сессионный срез отзывов и видимость авторизации

Ozon режет выдачу отзывов у неавторизованного профиля, и величина среза зависит
от сессии и товара, а не от способа сбора. Замер на одном товаре
(pid `210502516`, 257 707 отзывов, `tests/probe_reviews_api.py` и
`tests/probe_reviews_req_shape.py`, 2026-09-25):

| путь | результат |
|---|---|
| DOM, `/reviews/pdp-part`, 16 итераций скролла | 4 уникальных отзыва, высота документа перестаёт расти, кнопки «Дальше» нет |
| DOM, карточка товара, скролл до `webListReviews` | те же 4 отзыва |
| JSON API `entrypoint-api`, `layout_page_index` 1..4 | те же 4 uuid, `nextPage`/`pageToken` пусты, `paging.total` = 257 707 |
| тот же товар 2026-08-29 (сырой raw) | 97 отзывов на сортировку `published_at_desc` + 90 на `score_asc` |

Вывод: срез серверный и общий для DOM и API, транспорт его не обходит. Плюс
настоящая пагинация требует токенов страницы (`sh`, `start_page_id`), без них
`layout_page_index` игнорируется — синтезировать пагинацию «похожим URL» нельзя.

`collect.detect_auth_state()` после age-gate пишет `raw.session.auth`:
`{logged_in: true|false|null, source: cookie|dom|ambiguous|unknown,
cookie_present: true|false|null}`. Кука `_ozon_sat` (`context.cookies()` для
Playwright, `get_cookies()` для Selenium) авторитетна; DOM-признаки
(«Войти» / «Мой Ozon») — фолбэк, когда куки недоступны; любая ошибка даёт
`unknown`, детектор не бросает исключений.

Дальше состояние видно в двух местах: QA-метрика `auth_logged_in` + INFO-код
причины в `qa_raw`, и агрегат `session_auth` (`checked/logged_in/anonymous/
unknown`) в `status.json` демона. Коды INFO **не меняют вердикт**: иначе
валидные pid уехали бы в массовый rescan. Практический смысл — анонимный прогон
виден сразу, а не после тысяч отзывов: при срезе в 4 отзыва сбор товара с
`variant_count` в сотни тысяч бессмыслен, и это дешёвый повод остановиться.

---

## 11. Данные на диске

```
data/   {pid}_raw.json, {pid}/dom/*.html.gz, gallery_{pid}/, ocr_cache/,
        search_combined.json, campaigns/{campaign}/progress.json,
        campaigns/{campaign}/combined_{run_id}.json, campaigns/{campaign}/status.json,
        campaigns/{campaign}/events.jsonl, campaigns/{campaign}/runs/{run_id}.json,
        key_daemon.pid
docs/   *.md отчёты, schema.md, qa/qa_*.{md,json}, architecture.md,
        key_daemon_architecture.md
logs/   ozon_collector.log(.1), {campaign}_daemon.log
```

---

## 12. Автономный супервизор кампаний (`scripts/key_daemon.py`) — слой 2

Полное описание — в `docs/key_daemon_architecture.md`. Кратко:

**Цель кампании** — накопить `target` **ЧИСТЫХ** PID (прошедших QA без FAIL)
и зафиксировать это финальным QA по immutable-снимку.

**Единственный источник состояния** — `data/campaigns/{campaign}/progress.json`
(атомарная запись; при resume конфиг берётся из `run_cfg` манифеста, а не из
новых аргументов CLI).

**Главный цикл:**
```
проверки выхода → cooldown → выбор режима (full/rescan) → cli.py search
→ классификация (NORMAL_DONE/CHALLENGE_STORM/CRASH/UNKNOWN_FAILURE)
→ QA новых PID (K4) + reconcile → реакция на статус → target? → save
```

**Инварианты K4** (достоверность QA): `products == len(batch)`,
`ok+warn+fail == products`, при `fail>0` per_pid покрывает все PID.
Нарушение → `NEEDS_ATTENTION` (терминально). K4 проверяется **и в живом QA,
и в финальном QA финализатора**.

**Источник истины (П1)** — наличие canonical raw: `valid_pids` держит только
пиды с существующим `data/{pid}_raw.json` и актуальным вердиктом OK/WARN
(mtime-инвалидация при изменении/удалении файла).

**Резерв**: полный прогон собирает `target + reserve_count` товаров
(discovery-лимит), чтобы valid превысил target и резервный пул
`campaign_reserve_pids` реально сформировался. При финальном QA брак из
snapshot заменяется чистыми из резерва (`replacement_pids`).

**Rescan**: `cli.py search --pids <failed> --out-raw ...` — точечный пересбор
бракованных PID (контракт подтверждён в `cli.py`, покрыт тестами), бюджет
`max_rescans` на PID → далее `permanently_failed`. Детерминированный брак
(повторный идентичный набор issues FAIL) уходит в permanent сразу, не сжигая
бюджет rescan.

**Итоговые статусы**: `COMPLETED` / `COMPLETED_MIN` (мягкие остановки) /
`ABANDONED` / `NEEDS_ATTENTION`. `NEEDS_ATTENTION` терминален и никогда не
перезаписывается финализатором.

---

## 13. Тесты

Офлайн-юниты (без браузера/сети):
- ядро: HTML.gz фикстуры карточек/отзывов (PRODUCT_JS / REVIEWS_JS /
  VARIANT_MODE), top-up (FakePage, маркеры `step-up`/`step-down`), затворы
  пустоты, правила брака, кэш, ocr, parallel, process, report, search;
- демон (`test_key_daemon.py` + `test_cli.py`): **209 тестов** — классификация,
  K4 (живой QA + финализатор), П1, mtime, детерминизм, immutable snapshot,
  validate_new_campaign, сброс счётчиков, rescan, детерминированный брак,
  JSON-контракт, PidLock, resume-конфиг.

Постоянный известный фейл ядра — `test_baseline_matches_source_raw`
(падает и на чистой HEAD, дрейф baseline; помечен `xfail` с причиной,
не блокирует CI).
`tests/smoke_cdp.py` — живой CDP опционально.

---

## 14. Как запускать

Разовый сбор товара:
```bash
python3 cli.py gather --pid 550332874 --sorts published_at_desc,score_desc,score_asc --out docs/report.md
```

Кампания под демоном:
```bash
python scripts/key_daemon.py --campaign prezervativy \
    --url "https://www.ozon.ru/category/..." --limit 300 --min-valid 200 \
    --out docs/prezervativy.md --max-age 7d --min-reviews 40 --parallel 1 \
    --max-no-progress-runs 3 --max-rescans 2 --max-total-hours 12 \
    --reserve-count 30 --challenge-pause 60 --notify toast
```

Браузер: `sh scripts/launch_chrome.sh` (Linux) /
`powershell -ExecutionPolicy Bypass -File scripts/launch_chrome.ps1` (Windows).