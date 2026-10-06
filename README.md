# Ozon Collector

> ### Миссия
> Собирать **максимально полные и верные данные** о товарах с Ozon (карточка,
> информация отзывов выбранного варианта, OCR галереи и т.д.) и превращать их
> в готовый markdown-отчёт.
>
> Три принципа:
> - **Достоверность.** Собранная информация на 100% соответствует тому, что
>   реально есть на странице товара. Отзывы собираем только для выбранного
>   варианта товара.
> - **Качество важнее скорости.** Сбор ведём настолько быстро, насколько это
>   возможно, но скорость не должна снижать качество.
> - **Обход защиты от ботов.** Ozon защищён антиботом (FAB) и разрешается 
>   использовать любые способы для выполнения цели качественного сбора информации.

> ⚠️ Ozon FAB блокирует любых автоматических клиентов (включая headless). Единственный
> рабочий путь — живой Chromium на реальном дисплее + подключение по CDP
> (`scripts/launch_chrome.sh` на Linux, `scripts/launch_chrome.ps1` на Windows).
> Если браузер закрыт, `collect`/`gather`/`search`
> запускают его автоматически; отключить: `OZON_AUTO_LAUNCH=0`.
>
> Полная архитектура обхода антибота — `docs/architecture/anti_bot_architecture.md`.

## Ревизия 2.9 — что нужно знать оператору

**URL кампании проверяется на входе.** Markdown-ссылка, «умные» кавычки и
хвостовая пунктуация чинятся автоматически (в лог падает warning «URL поправлен»);
не-URL (`опечатка`, `javascript:...`, пусто) → ошибка с кодом 2 **до** запуска
браузера. Раньше такой URL доходил до `Page.goto` и давал «нулевые» прогоны со
статусом `NORMAL_DONE`.

**`EMPTY_SEARCH`** — новый статус прогона: браузер жив, сетка выдачи не
отрисовалась, товаров 0. Считается стагнацией (оператору видно в логе и
`status.json`), а не успехом.

Переменные окружения ревизии 2.9:

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `OZON_ACTIVE_WARMUP` | `1` | Активный прогрев (scroll + mousemove). `0` — только навигации и паузы |
| `OZON_PROXY_URL` | — | Прокси для worker'ов (в логах маскируется). **Выключено по умолчанию, включать после A/B** |
| `OZON_PROXY_URLS` | — | Список прокси через запятую: worker-1 берёт первый, worker-2 второй и т.д. |
| `OZON_KEEP_WINDOW` | `0` | Показать окно браузера после запуска (на сервере без дисплея используйте Xvfb + `DISPLAY`) |
| `OZON_CANARY_URL` | — | Переопределить canary-цель (приоритет: `--canary-url` → env → URL кампании → главная) |

Диагностика:

- `fingerprint` в логах теперь содержит `hidden` / `visibility` / `focused` —
  по ним видно, в каком состоянии вкладка (данные только, принудительного
  foreground нет).
- `recovery` в прогонах: `warmup_activity`, `warmup_challenges`,
  `search_grid_empty`, `canary`, `block_kind` (`soft`/`hard`).

Все новые механизмы покрыты тестами, но **влияние на блокировки не измерено
живым A/B** — см. таблицу «Проверка гипотез ревизии 2.9» в
`docs/architecture/anti_bot_architecture.md`.

- **Диагностика автозапуска браузера.** Каждый запуск лаунчера пишет лог в
  `%TEMP%\ozon_launch_<порт>.log` (перезаписывается при каждом старте, после
  неудачи остаётся как улика). Если демон пишет «CDP-браузер не поднялся», в
  сообщении уже есть: путь к логу, код возврата лаунчера и его вывод. Маркеры
  лаунчера — ASCII и читаются без искажений:

  | Маркер | Значит |
  |---|---|
  | `LAUNCHER-ERROR: port N ALREADY responds` | порт держал старый Chromium — освободите порт |
  | `LAUNCHER-ERROR: Chromium pid=N EXITED` | процесс умер сразу; чаще всего профиль уже открыт другим Chrome |
  | `LAUNCHER-DIAG: chrome=… profile=… port=… proxy=…` | что именно запускалось (пароль прокси маскирован) |

  Проверка вручную: `powershell -ExecutionPolicy Bypass -File scripts\launch_chrome.ps1`
  (код возврата 0 — успех, 1 — отказ с причиной). Путь к логу можно переопределить
  через `OZON_LAUNCH_LOG_DIR` (используется тестами).

## Установка

```bash
pip3 install -r requirements.txt
python3 -m playwright install chromium
sudo apt install tesseract-ocr tesseract-ocr-rus   # для OCR
```

Windows:

```powershell
pip install -r requirements.txt
python -m playwright install chromium
# tesseract: winget install UB-Mannheim.TesseractOCR + язык rus
```

OCR v1.7.0 (опционально):

- `opencv-python` — консервативная предобработка (grayscale → upscale до
  2000px → CLAHE). Без cv2 OCR работает как раньше (`preprocess: none`).
- Локальные `tessdata_best` модели скачиваются автоматически при первом
  `ocr` в `data/tessdata_best/` (без apt); при недоступности сети — системный
  tessdata.

## Автономный браузер (Chrome-for-Testing / Edge)

Коллектор работает на любом Chromium-браузере, а не только на системном Chrome.
Если системный Chrome обновляется сам / недоступен / не хочется зависеть от
Google Update — поставь закреплённый Chrome-for-Testing (не обновляется сам,
версия меняется только явным перезапуском скрипта):

```powershell
python scripts/setup_bundled_chrome.py     # скачивает chrome.exe и печатает export
$env:OZON_CHROME_PATH = "<путь из вывода>"
```

Так же годится любой Chromium-путь (каталог или exe) — Chrome-for-Testing, Edge:

```powershell
$env:OZON_CHROME_PATH = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
```

`OZON_CHROME_PATH` пробрасывается в launch-скрипты как `CHROME`, автономный
браузер использует тот же anti-automation паттерн (без `--enable-automation`).
Без переменной — по умолчанию системный Chrome. **Движок по умолчанию в сборке —
Chromium** (`chrome`, см. `OZON_BROWSER`; явно задать другой: `--browser
firefox` или `OZON_BROWSER=firefox`).

## Firefox (отладочная ветка, НЕ для боевого сбора)

Firefox — **stock Firefox через Selenium + geckodriver (Marionette)**, не
Playwright: Playwright патчит Firefox (канал Juggler) и Ozon это видит.
`geckodriver` должен быть в PATH, каталог профиля создаётся **заранее** —
geckodriver не создаёт его сам (`Failed to set preferences`).

> ⚠️ **Firefox заблокирован целиком (замеры 2026-09-25).** Ozon отдаёт заглушку
> «Похоже, нет соединения» на первом же `goto` **любой** marionette-сессии:
> тёплый профиль, пустой профиль, без user prefs, с включёнными
> OCSP/CRLite/CRL — заглушка 6.6–8с. Chromium CDP на том же IP проходит
> (19.7с; 13.6с даже с `--enable-automation`, `webdriver=true`).
>
> Что замеры **доказали**: webdriver-флаг причиной **не является**; исключены
> профиль, prefs, locale, геометрия окна, ресурс-блокировка, тайминги,
> OCSP/CRL, IP и его репутация, cookies (инкогнито вручную на том же IP
> открывается). Отделить tell уровня marionette-протокола от правила
> «automation + UA Firefox» текущими инструментами нельзя: Firefox без
> marionette не скриптуется. Поэтому `firefox` — только для отладки профилей,
> боевой `gather`/`search`/`key_daemon` идёт на `chrome`.

```powershell
$env:OZON_BROWSER = "firefox"                # вариант 1: env
python cli.py --browser firefox gather ...   # вариант 2: флаг
```

Что осталось рабочим в firefox-ветке (для отладки):

- Профиль — persistent `{token}.firefox` (`OZON_PROFILE_BASE`), куки живут
  между запусками, как и в Chrome.
- Окно **не сворачивается**: свёрнутое окно Firefox не рендерит lazy-блоки
  (IntersectionObserver не срабатывает) — список отзывов остаётся пустым. Окно
  разворачивается без активации фокуса (`WS_EX_NOACTIVATE`), чтобы не мешать
  работе за компьютером. `OZON_KEEP_WINDOW=1` — развернуть и показать явно.
- Перехват сети не используется: комментарии берутся из DOM. Селекторы
  Playwright-псевдокласс (`:visible`) разворачиваются в XPath, `has_text`
  локатора поддержан.
- Навигация не ждёт `load`: geckodriver возвращает управление на
  DOMContentLoaded (`pageLoadStrategy=eager`), иначе страницы отзывов Ozon висят
  до таймаута и отдают недогидрированную оболочку.
- Короткий адрес `/product/{pid}` без slug не подгружает блок отзывов — перед
  сбором collect берёт `<link rel="canonical">` и переходит на полный URL.
- Параллельный сбор для firefox разрешён: каждый воркер = отдельный процесс со
  своим Firefox+geckodriver и профилем, живость — по процессу + heartbeat
  (без CDP).
- Окна Firefox управляются нативно (PID + класс `MozillaWindowClass`), рестарт
  броузерной подсистемы = kill процессов профиля + reconnect.

Опечатка в `OZON_BROWSER` (не `chrome`/`firefox`) — `ValueError` на старте.

Замечания по сборкам:
- Предпочитай Chrome-for-Testing (stable, `setup_bundled_chrome.py`) или Edge.
  Playwright-сборка chromium (`%LOCALAPPDATA%\ms-playwright\chromium-*\chrome-win*\chrome.exe`) через `launch_chrome` работает нестабильно — браузер может стартовать и сразу умереть; лучше не использовать.
- `storage.googleapis.com/.../last-known-good-versions-with-downloads.json` иногда
  отдаёт 404 — скрипт автоматически пробует зеркало `googlechromelabs.github.io`.

## Быстрый старт

Браузер запускается автоматически при первом обращении (если закрыт), затем
весь пайплайн одним вызовом:

```bash
python3 cli.py gather --pid 550332874 --sorts published_at_desc,score_desc,score_asc --out docs/digests/report.md
```

Ручной запуск браузера (не обязателен, полезен при отладке):

```bash
sh scripts/launch_chrome.sh          # Linux: живой Chromium + CDP :9222 (см. выше)
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/launch_chrome.ps1
```

Три типовых сценария:

```bash
# 1) Быстро: только отзывы варианта + карточка (без OCR)
python3 cli.py collect --pid 550332874 --sorts published_at_desc,score_asc

# 2) Полный пайплайн с OCR галереи → отчёт в файл
python3 cli.py gather --pid 550332874 --out docs/digests/report.md

# 3) Отчёт из уже собранного raw.json (офлайн, без браузера)
python3 cli.py report --raw data/550332874_raw.json --out docs/digests/report.md
```

ID товара = цифры из URL (`ozon.ru/product/550332874`) либо `--url <полный URL>`.
Результат: `data/{pid}_raw.json` + `docs/digests/report.md`.

## Пайплайн

Низкоуровневые команды (для отладки):

```
collect → ocr → report
```

```bash
# 1) Браузер (не обязателен — запускается автоматически при необходимости)
sh scripts/launch_chrome.sh            # Linux; Windows: launch_chrome.ps1

# 2) Сбор: карточка + отзывы варианта (CDP) → data/{pid}_raw.json + DOM-снимок
python3 cli.py collect --pid 3627230434 --variant 'Цвет товара: черный' --category headphones

# 3) OCR галереи и инфографики (tesseract rus) → обновляет raw
python3 cli.py ocr --raw data/3627230434_raw.json

# 3a) OCR с настройкой параллелизма и предобработкой (опции v1.7.0)
python3 cli.py ocr --raw data/3627230434_raw.json --threads 8      # воркеры OCR (по умолч. 8)
python3 cli.py ocr --raw data/3627230434_raw.json --no-preproc     # без cv2 (gray/upscale/CLAHE)

# 4) Markdown-отчёт (без --out — в stdout)
python3 cli.py report --raw data/3627230434_raw.json --out docs/digests/headphones_xiaomi_redmi_buds8.md
```

Пользовательский shortcut — вся цепочка одним вызовом:

```bash
# отчёт в stdout (удобно для | less / > file.md)
python3 cli.py gather --pid 3627230434 --variant 'Цвет товара: черный'

# сохранение в файл
python3 cli.py gather --pid 3627230434 --variant 'Цвет товара: черный' --out docs/digests/xiaomi.md

# быстрый прогон без OCR (без tesseract)
python3 cli.py gather --pid 3627230434 --no-ocr
```

`--variant` — либо габариты в порядке **ДЛИНА × ШИРИНА × ВЫСОТА** (например
 `200x90x6`), либо **ключ:значение** (например `Цвет товара: черный` — для техники
 по цвету). Без `--variant` собираются все отзывы.

По умолчанию коллектор включает **нативный фильтр Ozon «Этот вариант товара»**
(`webReviewsVariantMode`): если у товара есть варианты, фильтр активируется
(клик/URL-параметр `reviewsVariantMode=1`) и собираются только отзывы выбранного
варианта. `--no-variant-mode` отключает фильтр (собираются все отзывы).
Диагностика — в `raw.reviews_variant_mode` (used, variant_count, total_count,
reviews_dropped_by_client_filter); в отчёте выводится строка
«Фильтр Ozon: „Этот вариант товара“ (2697 из 5153)».

> ⚠️ **ПРАВИЛО (обязательно, 2026-08-20): ВСЕГДА собирать отзывы именно
> выбранного варианта товара («Этот вариант товара»).** Если у товара есть
> варианты (цвет/размер/габариты) — фильтр варианта включается АВТОМАТИЧЕСКИ,
> `--no-variant-mode` НЕ использовать для дайджестов. Проверка результата:
> `raw.reviews_variant_mode.used == true`, `reason == click_ok/url_param/
> already_active`, `variant_count` — сколько отзывов у варианта, и число
> собранных отзывов должно совпадать с `variant_count` (не меньше; если
> `variant_count` больше собранного и разница не объясняется ограничением
> «доступно только после регистрации» — сбор был неполный, пересобирать).
> Собранные отзывы — это данные именно выбранного варианта, а не всего товара.

**Сессионный срез отзывов и авторизация.** Неавторизованный профиль Ozon режет
выдачу, и величина среза зависит от сессии и товара, а не от способа сбора: на
одном и том же pid `210502516` (257 707 отзывов) 2026-08-29 отдавали 97 отзывов
на сортировку, а 2026-09-25 анонимная сессия отдаёт 4 — одинаково через DOM и
через JSON API. Такой недобор помечается в QA как `WALL_LIMITED`, а причина пишется
в `raw.session.auth`: `logged_in: true|false|null`,
`source: cookie|dom|ambiguous|unknown`. Куки `_ozon_sat` считаются авторитетными,
DOM («Войти» / «Мой Ozon») — фолбэком. У данных, собранных до появления детектора,
поля нет — QA тогда пишет `AUTH_NOT_MEASURED`. Коды `UNAUTH_WALL` /
`AUTH_UNKNOWN` / `AUTH_NOT_MEASURED` имеют уровень INFO и **не меняют вердикт**:
собранное — значит валидно, а не «пересобрать заново». Сводка по кампании —
`status.json` → `session_auth`.

`--category` сохраняется в `product.category` и выводится в отчёте.
`--no-dom` отключает DOM-снимок. `--out` задаёт путь raw.json (по умолчанию
`data/{pid}_raw.json`); у `report`/`gather` `--out` — путь markdown-отчёта.

### Сбор в нескольких сортировках отзывов (`--sorts`, v1.10.0)

По умолчанию собирается одна сортировка — «новые и полезные». Чтобы собрать
отзывы в нескольких сортировках сразу, передайте их через запятую:

```bash
python3 cli.py collect --pid 3627230434 --sorts published_at_desc,score_desc,score_asc
python3 cli.py gather --pid 3627230434 --sorts published_at_desc,score_desc,score_asc --no-ocr
```

Допустимые значения: `published_at_desc` («новые и полезные», дефолт),
`score_desc` («сначала высокие оценки»), `score_asc` («сначала низкие оценки»).
Для каждой сортировки делается отдельный проход (URL-параметр `?sort=`),
затем отзывы объединяются в один список без дубликатов по `uuid`.

- У каждого отзыва появляются поля `review_sort` (сортировка первого появления)
  и `review_sorts` (все сортировки, где отзыв встретился).
- В raw пишется `reviews_sorts` — запрошенный список.
- В отчёте — строка «Сортировки: новые и полезные (N) · сначала высокие оценки (M) …».
- Без `--sorts` поведение прежнее: отзывы без тегов, `reviews_sorts` отсутствует.

## Справка по командам и опциям

```
ozon-collector {collect,ocr,report,gather} [опции]
```

Поисковый сбор (`search`) — **не CLI-подкоманда**: это модуль
`ozon_collector/search.py`, который вызывается демоном in-process
(`scripts/key_daemon.py::_inproc_collect` → `parallel.collect_parallel`).
Отдельный поисковый CLI в сборке нет.

### `collect` — сбор карточки + отзывов варианта (CDP)

| Опция | Назначение | По умолчанию |
|-------|-----------|--------------|
| `--pid ID` | ID товара Ozon (`ozon.ru/product/550332874` → `550332874`) | — |
| `--url URL` | полный URL товара (альтернатива `--pid`) | — |
| `--variant SPEC` | фильтр отзывов варианта: габариты `200x90x6` или `Цвет:белый` | все отзывы |
| `--reviews-url URL` | URL страницы отзывов | `?tab=reviews` |
| `--max-pages N` | лимит страниц при постраничной пагинации | авто |
| `--min-reviews N` | минимум отзывов на странице (infinite scroll) | 40 |
| `--category CAT` | категория → `product.category`, выводится в отчёте | — |
| `--sorts LIST` | сортировки через запятую (см. выше) | одна: «новые и полезные» |
| `--no-variant-mode` | не включать нативный фильтр «Этот вариант товара» | фильтр включён |
| `--no-dom` | не сохранять DOM-снимок (`data/{pid}/dom/…`) | сохранять |
| `--out PATH` | путь raw.json | `data/{pid}_raw.json` |

### `ocr` — OCR галереи/инфографики (обновляет raw.json)

| Опция | Назначение | По умолчанию |
|-------|-----------|--------------|
| `--raw PATH` | путь к raw.json (обязательный) | — |
| `--pid ID` | переопределить pid (для поиска галереи) | из raw |
| `--gallery-dir DIR` | каталог скачанных изображений | `data/gallery_{pid}/` |
| `--threads N` | число процессов OCR | 8 |
| `--no-preproc` | без cv2-предобработки (gray/upscale/CLAHE) | предобработка вкл. |

### `report` — markdown-отчёт из raw.json

| Опция | Назначение | По умолчанию |
|-------|-----------|--------------|
| `--raw PATH` | путь к raw.json (обязательный) | — |
| `--out PATH` | путь к markdown-файлу | stdout |
| `--no-dedup-replies` | не скрывать повторяющиеся ответы продавца (см. ниже) | дедуп включён |

### `gather` — collect → ocr → report одним вызовом

Принимает все опции `collect`, плюс:

| Опция | Назначение | По умолчанию |
|-------|-----------|--------------|
| `--out PATH` | путь markdown-отчёта | stdout |
| `--out-raw PATH` | путь raw.json | `data/{pid}_raw.json` |
| `--gallery-dir DIR` | каталог галереи | `data/gallery_{pid}/` |
| `--threads N` | воркеры OCR | 8 |
| `--no-preproc` | без cv2-предобработки | предобработка вкл. |
| `--no-ocr` | пропустить OCR (быстрый прогон без tesseract) | OCR выполняется |
| `--max-age AGE` | TTL сырого кеша (см. ниже), `0` = не читать кеш | `1d` |
| `--no-dedup-replies` | не скрывать повторяющиеся ответы продавца (см. ниже) | дедуп включён |
| `--overwrite` | перезаписывать существующий `data/{pid}_raw.json`; иначе новый прогон → `data/{pid}_raw.<ts>.<i>.json` | выкл |

### `search` — поисковый сбор выдачи для кампаний (v1.11.0, модуль, не CLI)

Собирает до `limit` товаров из выдачи поиска/категории Ozon через пайплайн
`gather` и складывает combined-json + markdown-дайджест. Живой код входа —
`ozon_collector/search.py` (`collect_search`) и
`ozon_collector/parallel.py` (`collect_parallel`, воркеры); вызывает их демон
(`scripts/key_daemon.py`, режимы full/rescan), а не CLI. Аргументы — из
манифеста кампании: `run_cfg` демона → `gather_cfg` (см. `key_daemon.py:1013`).

Запуск поиска вручную — только через демон (он ведёт учёт, QA и cooldown):

```bash
python scripts/key_daemon.py --url "https://www.ozon.ru/category/..." \
    --limit 300 --min-valid 200
```

**Докрутка выдачи:** Ozon рендерит в DOM лишь первые карточки, остальные
подгружаются при прокрутке (`infiniteVirtualPaginator`). `search` прокручивает
выдачу, пока в сетке результатов (`tileGridDesktop`) прибавляются товары или
пока не найдено `scan_limit = max(limit, 50)` товаров. Блоки рекомендаций
(«Вы смотрели», «Возможно, вам понравится» — виджеты `island`/`skuGrid`) в
выборку не попадают: товары берутся только из сетки результатов. `found`
(= «Найдено товаров в выдаче» в digest) — все найденные уникальные товары,
собирается при этом `min(found, limit)`.

Эквивалентные параметры задаются флагами демона (кампания = поисковый прогон):
`--url` (выдача/категория), `--limit` (сколько товаров собрать),
`--min-reviews`, `--max-age` (TTL кеша raw), `--parallel` (1..2; выше —
`OZON_PARALLEL_MAX`), `--out` (markdown-дайджест кампании). Полный список —
`python scripts/key_daemon.py --help`.

Поисковый прогон открывает **одну CDP-сессию** на весь прогон. Combined-файл
(`combined_{run_id}.json` в каталоге кампании) пишется всегда и содержит:
`{schema_version, collector_version, collected_at_utc, search {url, query},
found, limit, collected, recovery, products[{pid, ok, error, error_kind,
recycle_attempts, raw, markdown, cache, ocr}]}`. `found` — уникальные товары,
найденные на выдаче; `collected` — сколько удалось собрать до `limit`. Это
контракт, который читает демон (не текст лога).

### Recovery после краша браузера (v1.12.0)

Длительный поисковый прогон (кампания) может упереться в OOM-краш renderer'а
Chromium (типовой сценарий: `net::ERR_ABORTED: The frame was detached from the
DOM`). Коллектор классифицирует ошибку сбора (независимый health probe по
OS-identity процесса + CDP HTTP `/json/version` + состоянию target'а) и:

- **recyclable** (`browser` / `renderer_failure`) → **R1**: браузер
  перезапускается (`BrowserProcessManager`: kill старых процессов с сверкой
  identity против PID-reuse → проверка, что не осталось чужих → запуск нового
  Chromium через `scripts/launch_chrome`) → сессия переподключается →
  **R2**: товар собирается заново. До `max_recycles` (внутренний лимит 2)
  на товар.
- **non-recyclable** (`target` / `unknown`) → ошибка товара фиксируется, цикл
  продолжается на следующем товаре (крах вкладки без краха браузера не
  требует перезапуска Chromium).

Телеметрия: у каждого товара `error_kind`/`error_probe_state`/
`recycle_attempts`; в combined — `recovery{recycles, events[{pid, attempt,
kind}]}`. В консоли видно `[search] товар N/M: renderer_failure → recycle
браузера (попытка 1/2)`.

`browser_dead` (инфраструктура, не soft block): если CDP не отвечает **и**
процесса браузера на паре (debug-порт, профиль) нет, товар не уходит в recycle —
восстанавливать нечего, а recycle только жёг бы session-бюджет до
`SESSION_EXHAUSTED`. Такой товар даёт `error_kind=browser_dead`,
`failure_type=infra_failure`, и оркестратор респаунит воркера со свежим
браузером (`parallel._needs_restart`). Проверка смерти — живой OS-запрос
(`process.browser_processes`), а не снимок `Session.browser_pid`: снимок
протухает при рестарте без `reconnect` и отсутствует у внешнего браузера.
У чужого браузера (`browser_owner=external`) смерть недоказуема — там
`browser_dead` не выводится (ADR §14.1).

### Canary по классу сущности + API-предсрез отзывов (ревизия 2.8)

**Canary.** После recycle «по доступу» сессия проверяется не всегда на главной:
цель выбирается по классу сущности — `--canary-url` → env `OZON_CANARY_URL` →
URL кампании → `https://www.ozon.ru/`. Главная мягче карточки по лимитам, и
`CANARY_OK` на ней не доказывал доступ к `/product/`, из-за чего бюджет сессии
уходил вхолостую. Класс цели и результат пишутся в
`recovery.canary={url, kind, result}` и в агрегаты `kpi.canary_fails` /
`kpi.ip_blocks`; воркерам цель уходит в `task["canary_url"]`, чтобы замена
упавшего воркера не проверяла другую сущность. Флаг демона: `--canary-url URL`.

**Отзывы через Ozon API.** По умолчанию выключено. Включается `--reviews-api`
(у `collect`/`gather` и у демона) или `OZON_REVIEWS_API=1`; результат
фиксируется в `run_cfg.reviews_api` манифеста. Поведение:

- DOM — источник истины: API добавляет только те отзывы, которых в DOM нет
  (сверка по `uuid`, при конфликте выигрывает DOM);
- пагинация — только по серверному `nextPage` (синтетический `layout_page_index`
  без токенов сервер игнорирует);
- DOM-докрутка сокращается до одной страницы, только если API вернул не меньше
  доверенного `rating_count` товара;
- вариант-фильтр и мульти-сорт этот путь отключают;
- любая ошибка API не ломает сбор — предсрез просто выключается на этот товар.

Диагностика без разбора логов: `raw["reviews_api"]` в combined/манифесте —
`{enabled, used, reason, fetched, pages, complete, api_added, api_dupes,
dom_short_circuit}`. Подробности и замеры (почему это opt-in) — в
[`docs/architecture/anti_bot_architecture.md`](docs/architecture/anti_bot_architecture.md)
§7, §9, §10, §12, §16.

### Прогресс-логи

`search` пишет прогресс по каждому товару (`товар N/5: pid=…` + итог), а
`run_gather` (используется и `search`, и одиночным `gather`) — стадии внутри
товара с причиной по кешу:

```
[search] найдено 32 товаров в выдаче, собираю 5
[search] товар 1/5: pid=1674716363 (https://www.ozon.ru/product/1674716363)
[gather] pid=1674716363: cache=miss (old_version 1.5.0 < 1.11.0)
[gather] pid=1674716363: collect — 2 отзывов
[gather] pid=1674716363: ocr=ran (5 картинок)
[search] товар 1/5: ok · cache=miss · ocr=ran · отзывов 2
[search] товар 2/5: pid=1674716607 (…)
```

Так всегда видно, на каком товаре процесс находится и что именно происходит
(проверка кеша → сбор → OCR → отчёт), а не «немое ожидание».

### Файловый лог прогона (`--log`)

Помимо консоли, каждая команда автоматически пишет подробный лог в файл
`logs/ozon_collector.log` (папка `logs/` создаётся сама при первом запуске).
Путь меняется глобальным `--log <path>` или env `OZON_COLLECTOR_LOG`. При
каждом запуске лог ротируется: текущий сохраняется как
`logs/ozon_collector.log.1`, пишется новый.

- `info`/`warning`/`error` дублируют консольный вывод (то же сообщение, +
  время, уровень, модуль);
- `debug` пишется **только в файл**: итерации докрутки выдачи и
  converge/pagination, каждый `goto`/`scroll`/`eval`, переходы watchdog
  (`alive → transient → dead`), recycles браузера — по этому логу можно
  восстановить, что происходило во время длинного прогона.

Пример строки файла:

```
2026-08-09 18:00:01,123 INFO  [search] товар 1/32: pid=1674716363 {"url": "https://www.ozon.ru/product/1674716363"}
2026-08-09 18:00:02,010 DEBUG [cdp] goto: https://www.ozon.ru/product/1674716363 → 340ms
```

Запуск (лог и так пишется по умолчанию; `--log` — если нужен отдельный файл):

```bash
python scripts/key_daemon.py --url "https://www.ozon.ru/category/..." --limit 5
python cli.py --log logs/run1.log gather --pid 550332874
```

### Кеш сырых данных (`--max-age`, v1.11.0)

`gather` и `search` кешируют собранные `raw.json` в `data/{pid}_raw.json`.
Перед сбором проверяется кеш: если файл есть, `collected_at_utc` свежий
(возраст ≤ TTL), `incomplete != true`, `schema_version` совместима **и версия
коллектора совпадает с текущей** — товар берётся из кеша, повторный живой
сбор не делается. Это защищает перезапуск после падения: свежие товары не
пересобираются.

- `--max-age 1d` (дефолт): кеш читается, если не старше суток.
- `--max-age 0`: кеш не читается вообще (всегда живой сбор, но raw всё равно
  перезаписывается).
- Формат: целое число + суффикс `d`/`h`/`m` (например `12h`, `6h`).

**Инвалидация по версии коллектора:** если `raw.json` собран версией **старее**
текущей или `collector_version` отсутствует — кеш не используется (причина
`old_version`), данные пересобираются: старые сборки могли не иметь полей
(`reviews_variant_mode`, `pagination_type`) или содержать ошибки экстракторов.
Если версия в кеше **новее** текущей — причина `newer_version`, кеш тоже не
берётся (защита от запуска старого кода поверх данных новой версии). В логе
причина видна явно: `cache=miss (old_version 1.6.0 < 1.11.0)`. Старые файлы не
удаляются — они остаются как архив и перезаписываются свежими при следующем
успешном сборе.

При cache-hit OCR выполняется только если в кеше нет `gallery_ocr` (картинки
перекачиваются заново и raw перезаписывается). Статусы в отчёте: `cache: hit`
(из кеша) / `miss` (живой сбор) / `off` (`--max-age 0`) / `stale` (кеш старше
TTL — пересбор); `ocr: hit` (в кеше уже был) / `ran` (выполнен при hit) /
`off`.

### Дедуп повторяющихся ответов продавца (`--no-dedup-replies`, v1.11.0)

Продавцы на Ozon часто отвечают на каждый отзыв шаблонным текстом, отличающимся
только именем покупателя («Иван, спасибо за заказ и отзыв!», «Пётр, спасибо за
заказ и отзыв!»). `report`, `gather` и `search` по умолчанию скрывают такие
дубли при рендере Markdown: из каждой группы одинаковых ответов одного продавца
оставляется только первый, к нему дописывается метка
`_…и ещё N таких же ответов продавца пропущено_`. Сырые данные `data/*_raw.json`
не изменяются — дедуп работает только на уровне отчёта.

- **Ключ группы:** `(author, первая фраза)` — совпадение должно быть по автору
  (продавцу) и первой фразе ответа (до `.`, `!`, `?` или перевода строки).
  Ведущее обращение к покупателю («Иван, …») вырезается, регистр и пробелы
  нормализуются.
- `--no-dedup-replies` полностью отключает фильтр: все ответы выводятся без
  меток, как в `raw.json`.

На реальном товаре MAX WAX Chain Wax (PID `550332874`, 270 комментариев,
170 от продавца) дедуп сократил ответы продавца с 170 до 138 (17 групп с дублями).

## Кампании (scripts/key_daemon.py)

`key_daemon` — демон полного сбора: цель по числу валидных товаров,
реконсиляция с QA, rescan чистильщиков, восстановление после challenge/crash,
resume при повторном запуске. Имена кампаний — `data/campaigns/<имя>/`.

**Имя кампании можно не задавать** — оно генерируется из `--url`
(автоименование, контракт фиксирован тестами):

```bash
# достаточно одного --url; --campaign/--out при этом не нужны
python scripts/key_daemon.py --url "https://www.ozon.ru/search/?text=палатка 4 местная" \
    --limit 300 --min-valid 200
```

Приоритет источника имени:
`text=` в query → сегмент категории (`/category/<slug>-<id>/`, числовой ID
отбрасывается) → `campaign_<дата_время>`. Имя — детерминированный транслит
(макс. 50 символов, `len<=50`, без завершающего `_`); `--out` по умолчанию
`docs/digests/{имя}.md`.

Явные флаги работают строго как раньше (автоименование НЕ вызывается):

```bash
python scripts/key_daemon.py --campaign prezervativy \
    --url "<URL>" --limit 300 --out docs/digests/prezervativy.md ...
```

> ⚠️ **`text=` используется ТОЛЬКО для имени кампании, никогда для фильтра
> названий `--match-title`** (поисковый запрос ≠ название товара; инцидент
> podarok_five из `docs/archive/PROJECT_NOTES_2026-10-06.md`).

Если прогон упал на финальной фазе (главный профиль занят, CDP не поднялся, Ctrl+C после сбора), сырьё уже лежит в `data/*_raw*.json`, а дайджеста нет — повторный сбор ради файла не нужен, отрендерить его из кеша без сети и браузера:

```
python scripts/digest_from_cache.py <campaign>   # напр. fonari_dlya_velosipedov
python scripts/digest_from_cache.py <campaign> --dry-run   # только показать pid и путь
```

Берёт PID из `data/campaigns/<campaign>/progress.json` (`valid_pids`) и пишет `progress["out"]`. Манифест не меняет, в сеть не ходит.

## Данные

```
data/
  ├── {pid}_raw.json                  # результат collect (+ ocr после шага 3)
  ├── {pid}/dom/{YYYY-MM-DD}.html.gz  # DOM-снимок страницы товара (для тестов/аудита)
  ├── gallery_{pid}/                  # скачанные изображения галереи (создаёт ocr)
  ├── ocr_cache/                      # кэш OCR: {md5}_{lang}_{psm}_{preproc}_{tessdata}_schema{N}.json
  └── search_combined.json            # combined raw всех товаров (search --out-raw)
docs/
  ├── architecture/                       # архитектурные документы (ADRs, схема)
  ├── digests/{slug}.md                   # готовые дайджесты кампаний (по умолчанию)
  └── qa/                                 # QA-отчёты (json + md: qa_latest.*)
```

## Тесты

```bash
python3 -m pytest tests/            # офлайн: экстракторы + форма raw на сохранённых фикстурах
python3 tests/smoke_cdp.py          # живой CDP: ключевые поля + two-page (A→B)
python3 tests/smoke_cdp.py --compare # + сверка с tests/expected_snapshots.json (свежесть фикстур)
```

- `tests/product_pages/*.html.gz` — снимки карточек (xiaomi/roborock/neoclima/topper):
  проверяют `PRODUCT_JS` (в т.ч. `rating_source` из допустимых виджетов).
- `tests/review_pages/*.html.gz` — снимки страницы отзывов (`xiaomi` — режим
  «Все отзывы», `xiaomi_variant` — активен «Этот вариант товара»): проверяют
  `REVIEWS_JS` (uuid/текст/звёзды/вариант/автор/дата), детектор `VARIANT_MODE_JS`
  и поведение `_activate_variant_mode` (skip клика при активном фильтре,
  graceful-фолбэк при неудаче).
- `tests/raw_fixtures/shape_minimal.json` — эталон формы raw.json; `test_raw_shape.py`
  ловит «collect работает, report падает из-за отсутствующего ключа».
- smoke-тест ходит на живые страницы (single + two-page), двухстраничная проверка —
  регрессия бага «CDP оставался на предыдущей странице».

## Схема raw.json

См. **`docs/schema.md`** (полная схема с примерами). Кратко:
`collector_version`, `schema_version`, `collected_at_utc`, `product_url`,
`reviews_url`, `pid`, `incomplete`/`missing_fields` (если цена/рейтинг не
извлечены), `target_variant`, `product` (title, `rating` + `rating_source` —
откуда взят рейтинг: webProductScore / webSingleProductScore /
webReviewProductScore / fallback_text, `rating_count`, `price` `{current, old,
bank, confidence, source}` из виджета webPrice, `characteristics`, `descriptions`,
`gallery`, `infographics`), `reviews[]` (uuid, author, date, variant, stars, text,
helpful; при `--sorts` — review_sort/review_sorts), `reviews_sorts` (при `--sorts`),
`review_pages`, `artifacts.dom_snapshot` (путь к DOM-снимку),
`gallery_ocr`/`gallery_stats` (после `ocr`, в записях — `url` изображения).

Схема — **`schema_version: "2.0"`**, расширяется аддитивно (старые raw читаются
без миграции), `collector_version` в шапке raw. `search --out-raw` — отдельный
combined-контейнер (см. `search`), каждый товар внутри — обычный raw той же
схемы.

## Важные ограничения

- Ozon: сортировка отзывов через URL (`?sort=`) работает (см. `--sorts`, v1.10.0);
  при единичном сборе — по умолчанию «новые и полезные».
- Выборка свежих отзывов смещена к негативу — для общего рейтинга доверять виджету
  `webSingleProductScore`, не выборке.
- Живой сбор (`collect`/`ocr`) запускать только по явному запросу; `report` и тесты
  работают офлайн по сохранённому raw.json.
