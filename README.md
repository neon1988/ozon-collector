# Ozon Collector

Однократный сбор максимально полной информации о товаре на Ozon: карточка
(цена, рейтинг, характеристики, описание, галерея), отзывы выбранного варианта,
OCR галереи/инфографики — и готовый markdown-отчёт. Панель мониторинга/сравнения
намеренно не входит (проект упрощён до разового сбора по запросу).

> ⚠️ Ozon FAB блокирует любых автоматических клиентов (включая headless). Единственный
> рабочий путь — живой Chromium на реальном дисплее + подключение по CDP
> (`scripts/launch_chrome.sh` на Linux, `scripts/launch_chrome.ps1` на Windows).
> Если браузер закрыт, `collect`/`gather`/`search`
> запускают его автоматически; отключить: `OZON_AUTO_LAUNCH=0`.

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

## Быстрый старт

Браузер запускается автоматически при первом обращении (если закрыт), затем
весь пайплайн одним вызовом:

```bash
python3 cli.py gather --pid 550332874 --sorts published_at_desc,score_desc,score_asc --out docs/report.md
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
python3 cli.py gather --pid 550332874 --out docs/report.md

# 3) Отчёт из уже собранного raw.json (офлайн, без браузера)
python3 cli.py report --raw data/550332874_raw.json --out docs/report.md
```

ID товара = цифры из URL (`ozon.ru/product/550332874`) либо `--url <полный URL>`.
Результат: `data/{pid}_raw.json` + `docs/report.md`.

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
python3 cli.py report --raw data/3627230434_raw.json --out docs/headphones_xiaomi_redmi_buds8.md
```

Пользовательский shortcut — вся цепочка одним вызовом:

```bash
# отчёт в stdout (удобно для | less / > file.md)
python3 cli.py gather --pid 3627230434 --variant 'Цвет товара: черный'

# сохранение в файл
python3 cli.py gather --pid 3627230434 --variant 'Цвет товара: черный' --out docs/xiaomi.md

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
ozon-collector {collect,ocr,report,gather,search} [опции]
```

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

### `search` — дайджест выдачи Ozon для LLM (v1.11.0)

Собирает до `--limit` товаров из выдачи поиска Ozon через тот же пайплайн
`gather` и выдаёт один markdown-digest для Gemini (выбор лучшего товара).

**Докрутка выдачи:** Ozon рендерит в DOM лишь первые карточки, остальные
подгружаются при прокрутке (`infiniteVirtualPaginator`). `search` прокручивает
выдачу, пока в сетке результатов (`tileGridDesktop`) прибавляются товары или
пока не найдено `scan_limit = max(limit, 50)` товаров. Блоки рекомендаций
(«Вы смотрели», «Возможно, вам понравится» — виджеты `island`/`skuGrid`) в
выборку не попадают: товары берутся только из сетки результатов. `found`
(= «Найдено товаров в выдаче» в digest) — все найденные уникальные товары,
собирается при этом `min(found, limit)`.

```bash
# по запросу (список первых N товаров выдачи)
python3 cli.py search --query "палатка 4 местная" --limit 5 --out docs/digest.md

# по URL выдачи (страница /search/ или /category/)
python3 cli.py search --url "https://www.ozon.ru/category/palatki-4-mestnye-9889/" --limit 3
```

| Опция | Назначение | По умолчанию |
|-------|-----------|--------------|
| `--query TEXT` | поисковый запрос (альтернатива `--url`) | — |
| `--url URL` | URL выдачи Ozon | — |
| `--limit N` | сколько товаров собрать | 5 |
| `--reviews-per-product N` | отзывов на товар в digest (`0` = все) | 15 |
| `--min-reviews N` | минимум отзывов на странице (как в `collect`) | 40 |
| `--max-pages N` | лимит страниц пагинации отзывов | 12 |
| `--out PATH` | путь markdown-digest | stdout |
| `--out-raw PATH` | путь combined raw (все товары) | `data/search_combined.json` |
| `--max-age AGE` | TTL сырого кеша товаров | `1d` |
| `--no-dedup-replies` | не скрывать повторяющиеся ответы продавца (см. ниже) | дедуп включён |

Переиспользуются все опции `gather` (`--variant`, `--category`, `--threads`,
`--no-preproc`, `--no-dom`, `--no-ocr`, `--no-variant-mode`, `--sorts`,
`--no-dedup-replies`).

`search` открывает **одну CDP-сессию** на весь прогон. В `--out-raw` пишется
combined-файл: `{schema_version, collector_version, collected_at_utc, search
{url, query}, found, limit, collected, products[{pid, ok, error, raw, markdown,
cache, ocr}]}`. `found` — уникальные товары, найденные на странице выдачи;
`collected` — сколько удалось собрать до `limit`.

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

## Данные

```
data/
  ├── {pid}_raw.json                  # результат collect (+ ocr после шага 3)
  ├── {pid}/dom/{YYYY-MM-DD}.html.gz  # DOM-снимок страницы товара (для тестов/аудита)
  ├── gallery_{pid}/                  # скачанные изображения галереи (создаёт ocr)
  ├── ocr_cache/                      # кэш OCR: {md5}_{lang}_{psm}_{preproc}_{tessdata}_schema{N}.json
  └── search_combined.json            # combined raw всех товаров (search --out-raw)
docs/
  ├── headphones_xiaomi_redmi_buds8.md  # готовый отчёт
  ├── schema.md                         # схема raw.json
  └── archive/                          # старые отчёты (эталонные примеры markdown)
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
