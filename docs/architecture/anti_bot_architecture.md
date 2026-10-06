# Ozon Collector — архитектура обхода защиты от ботов

> Принцип из миссии проекта: «Ozon защищён антиботом (FAB) и разрешается
> использовать любые способы для выполнения цели качественного сбора информации».
> При этом **качество сбора первично**: данные обязаны на 100% соответствовать
> странице товара (K4), а страница-заглушка (FAB/captcha) НИКОГДА не проходит
> в результат как успех. Обход защищает процесс сбора, но не подменяет данные.

**Архитектурная позиция (после ревизии):** цель — не «скрыть всё» набором
стелс-приёмов, а построить **устойчивый сборщик**, который умеет (а) отличить
заглушку от товара, (б) определить *масштаб* ограничения по наблюдаемым
сигналам, (в) корректно выдержать cooldown и (г) продолжить с валидной
сессией. Маскировка — лишь один из инструментов, не гарантия.

**Статус:** документ описывает РЕАЛИЗОВАННОЕ состояние (этапы 0–8 завершены,
2026-08-31; ревизия 2.8 добавила entity-class canary, opt-in API-предсрез
отзывов и per-worker телеметрию параллелизма — 2026-09-26; ревизия 2.9 —
fail-fast валидация целевого URL + `EMPTY_SEARCH`, Page Visibility телеметрия,
недетерминированный активный прогрев, overshoot-драг, per-worker proxy
capability и soft/hard-эскалация — 2026-09-26). Механизмы ниже присутствуют
в коде и покрыты тестами; их **влияние на блокировки не измерено** — см.
метки `[HYPOTHESIS]` и §18 Backlog.

**Как читать:** §§1–2 — модель ограничений и карта слоёв; §§3–14 — детальный
разбор каждого механизма (что делает, как, зачем, когда срабатывает); §15 —
сквозной поток одного товара; §16 — что сознательно не делаем; §17 — файлы;
§18 — legacy и backlog. Ниже — короткий «слой-клик» (как это всё складывается).

### Эпистемические метки (ревизия 2.9)

Каждое утверждение о поведении Ozon/антибота помечено — иначе документ
превращается в набор непроверяемых мифов, которые потом принимаются за факт
и правят код:

| Метка | Значение | Требование |
|---|---|---|
| `[FACT]` | Проверено измерением в контролируемом A/B или воспроизведено в тестах | есть команда/тест, повторяющий замер |
| `[OBSERVATION]` | Повторяющаяся эмпирическая корреляция **без** доказанного механизма | есть частота/выборка, механизм не утверждается |
| `[HYPOTHESIS]` | Предполагаемый внутренний механизм WAF/площадки | явно назван как гипотеза, не влияет на K4-гейты |
| `[UNKNOWN]` | Состояние, неустановимое извне | не используется как основание для решений |

Правило: непомеченное утверждение о поведении внешней системы читается как
`[OBSERVATION]`. Гипотеза **никогда** не может поднять уровень доверия к данным
(влияет только на тактику обхода, не на `guard`/K4).

---

## 1. Бан-модель Ozon: что мы знаем и чего НЕ знаем

### Что знаем по наблюдениям

- **FAB (Front-end Anti-Bot) / Qrator оценивают сессию по Trust Score**, а не
  по одному флагу. В расчёт идут: fingerprint JS (navigator-свойства, WebGL,
  canvas), поведение (клики, скролл, тайминги, фокус вкладки), tampering-
  детекция, плотность запросов, состояние профиля (история, куки, TLS-session).
- **FAB залипает на навигации конкретной вкладки.** Повторы/reload в той же
  вкладке не помогают; свежая вкладка проходит через Qrator, получает
  auth-куки и разблокирует сессию. Это — основа recovery `new_tab`
  (подтверждено экспериментом 2026-08-17).
- **Прямой холодный вход на категорию провоцирует challenge.** Тёплый вход
  через главную (`_warmup_to_category`) снижает его частоту.
- **Headless / `--enable-automation` детектируются почти сразу** —
  `navigator.webdriver === true` и десятки признаков.

### Чего НЕ знаем (важно, чтобы не делать ложных выводов)

- **Точный scope бана извне не определяется.** «Несколько challenge подряд»
  — это *session degraded / вероятно ограничение доступа*, а не доказанный
  «бан IP/аккаунта». Архитектура фиксирует `observed_scope` как **наблюдение**,
  а не как диагноз.
- **То, что вручную браузер работает, а демон получает challenge, НЕ доказывает
  бан на IP.** Это сигнал о разнице ручной и CDP-сессии (см. §3).
- **Полное «пережигание» профиля контрпродуктивно.** Свежий профиль без
  истории/кук/Qrator-tracking имеет МИНИМАЛЬНЫЙ Trust Score и провоцирует
  первичный JS-челлендж. Поэтому профиль НЕ удаляется: он переводится в
  cooldown (см. §11).

---

## 2. Слои защиты (обзор)

| Слой | Задача | Реализация |
|------|--------|-----------|
| L1 Профиль/запуск | Живой Chrome без automation-флагов, отдельный профиль | `scripts/launch_chrome.{ps1,sh}`, `process.py` |
| L2 Маскировка | Убрать *очевидные* признаки автоматизации | `launch_chrome`, `cdp.py` |
| L3 Поведение | Тёплый вход, паузы, контроль нагрузки | `search.py::_warmup_to_category` |
| L4 Детект | Отличить заглушку от товара (критично для K4) | `collect.py`, `gather.py` |
| L5 Recovery | FSM «один инцидент»: reload/new_tab/recycle → SESSION_BLOCKED | `search.py::_collect_shard` |
| L6 Storm/backoff | Адаптивная пауза, profile cooldown, NEEDS_ATTENTION | `key_daemon.py` |
| L7 Управление | Session budget, canary, browser stability, ownership | `search.py`, `cdp.py` |

### Карта «кто решает что» (слой-клик)

- **L1–L2** запускают и маскируют браузер — решают *как выглядит сессия*.
- **L3** решает *как входить* (тёплый вход, не бить в холодную категорию).
- **L4** решает *что мы видим* (товар или заглушка) — это фундамент K4.
- **L5** решает *что делаем с заглушкой* (лестница recovery, строго ограничена).
- **L7** решает *сколько раз за run можно восстанавливаться* (budget), *чист ли
  браузер* (canary/stability), *чьим браузером можно управлять* (ownership).
- **L6** решает *паузу между run'ами* (adaptive cooldown, profile cooldown).

Важно: L4–L7 работают **наблюдаемо** (по сигналам страницы и телеметрии), а
L1–L3 — **проактивно** (сделать сессию «живой»). Ни один слой не «угадывает»
бан — все основаны на фактах (маркеры, метрики, счётчики).

---

## 2.1. Жизненный цикл: от демона до результата

Полный цикл одной «волны» сбора:

```
[Демон key_daemon.py]
   │  читает challenge_control/profile_cooldown (persistent)
   │  эффективный cooldown = max(cooldown_until, profile_cooldown.until)
   │  если не истёк → спит (backoff), ждёт
   │
   ▼  старт run (по кампании, напр. prezervativy / naushniki)
[search.py]
   │  для каждого воркера (свой профиль + порт, K воркеров)
   │
   ├── [L1] авто-запуск браузера (если закрыт) или attach (CDP)
   │        → Session(browser_owner=collector, свой профиль ozon-profile-worker-k)
   ├── [L3] _warmup_to_category: главная → категория (тёплый вход)
   ├── [L5/L7] _collect_shard по каждому PID:
   │     goto → check_challenge(reload×3)
   │        ├─ ok      → собрать карточку/отзывы/галерею (K4)
   │        └─ challenge → FSM: new_tab×1 → recycle×1 → SESSION_BLOCKED
   │                        + used{challenge,new_tab,browser_recycle}
   │                        + canary ТОЛЬКО после recycle
   │                        + observed_scope (profile/tab/browser)
   └── результат → combined/raw (статус на диск)
        │
        ▼
[Демон] _absorb_recovery_telemetry: обновить progress.json/status.json
        если SESSION_BLOCKED/EXHAUSTED → storm_streak++ → adaptive cooldown
        если repeated_challenge/profile_observed → profile_cooldown
```

Ключевой принцип разделения: **решение «что делать с сессией» (L5–L7) — в
search.py (внутри run), а решение «когда снова пробовать» (L6) — в демоне
(между run'ами)**. Это позволяет не сжигать профиль внутри одного run и не
молотить без остановки при устойчивом бане.

---

## 3. Диагностика: ручная сессия против CDP-сессии демона

Симптом «вручную открываю — работает, бот — challenge» — это **данные для
сравнения**. Что сравнивается:

- **Что собирает телеметрия демона (§12):** recovery-метрики каждой сессии —
  `signature`, `last_url`, `browser_tabs`, `collector_tabs`, `duration_ms`,
  `products_since_recovery`, `observed_scope`, counts (challenge/recycle).
- **Базовый fingerprint (реализовано, ревизия 2.7):** при первом `goto`
  каждой сессии `cdp.Session._capture_fingerprint_once` снимает однократный
  снимок — `ua`, `webdriver`, `languages`, `platform`,
  `hardwareConcurrency`, `deviceMemory`, `chrome`, `tz`, `lang`, viewport
  (`vw`/`vh`), screen (`sw`/`sh`/`colorDepth`). Пишется в `combined.fingerprint`
  и в `status.json` (`recovery_telemetry.fingerprint`). Это только наблюдение,
  не влияет на поведение; служит для быстрого diff manual vs daemon
  (напр. прилип ли `navigator.webdriver`, совпадает ли viewport).
- **Полный fingerprint-probe** (WebGL canvas, cookie, глубокие Client-Hints) —
  отдельная утилита `scripts/fingerprint_probe.py`, остаётся P2 (см. §18).
  Базовый снимок закрывает ОСНОВНЫЕ гипотезы «вручную работает, бот —
  challenge», но НЕ замеряет CDP-аномалии (см. §5).

То есть: по recovery-метрикам видно поведение (где challenge), по базовому
fingerprint — грубый отпечаток сессии; полный probe — в backlog.

---

## 4. L1 — Запуск живого Chromium и профиль

`scripts/launch_chrome.ps1` / `.sh`:

- **Не передаётся `--enable-automation`** — иначе `navigator.webdriver === true`
  и Ozon блочит сразу.
- **Не ставится `--no-sandbox`** (сильный сигнал automation; системному Chrome
  не нужен).
- **Не ставится `--disable-blink-features=AutomationControlled`** — флаг НЕ
  нужен: без `--enable-automation` и при явном (не нулевом) порту CDP
  `navigator.webdriver` нативно `false`. [FACT]
- **Не ставится init-script с подменой `navigator.webdriver`** — такого кода в
  проекте нет, и он намеренно не добавляется: JS-подмена геттера оставляет
  непативный дескриптор (`getOwnPropertyDescriptor` отдаёт функцию без
  `[native code]`) и сама становится сигналом. [HYPOTHESIS, см. §5]
- **Гигиена UI:** `--disable-infobars`, `--no-first-run`,
  `--no-default-browser-check`, `--disable-session-crashed-bubble`,
  `--hide-crash-restore-bubble`.
- **Приоритет системного Chrome над Playwright-chromium** (`Find-Chrome`):
  реальный отпечаток блочится реже. Если системный Chrome не отдаёт
  `--remote-debugging-port` (редкие сборки/политики) — fallback на
  Playwright chromium; в этом случае fingerprint хуже (отмечено в скрипте).
- **Свой профиль:** `--user-data-dir=%LOCALAPPDATA%\opencode\ozon-profile`
  (воркеры — `ozon-profile-worker-{k}`).
- **Маркер ownership:** скрипты пишут файл `.ozon_collector_launched` в профиль —
  по нему `Session` отличает «наш» запуск (collector) от ручного (external).
- **Скрытый запуск и фокус:** окно создаётся скрытым (SW_HIDE), затем
  свёрнутым без активации (SW_SHOWMINNOACTIVE + WS_EX_NOACTIVATE + HWND_TOPMOST).
  Фокус возвращается предыдущему окну силами лаунчера (см. оговорку ниже).
- **Оговорка, измеренная 2026-10-06:** «фокус возвращается» выполняется не
  всегда. Когда фокус уже сидит на окне Chromium, ни `AttachThreadInput`
  (возвращает 0), ни `SwitchToThisWindow`, ни `SetForegroundWindow` из
  PowerShell не переводят фокус назад; работает только синтетический Alt.
- **Сторож окон отключён по умолчанию.** `ozon_collector/window_policy.py`
  умеет возвращать окно коллектора в minimized (0.06 с) и возвращать фокус,
  но включается только через `OZON_WINDOW_GUARD=1`: окна браузеров коллектора —
  рабочие окна пользователя, он сам их открывает и сворачивает
  (требование 2026-10-06: «трогай только свои»).

### Окна и фокус — что измерено 2026-10-06

Замерял Win32-монитор (EnumWindows каждые 20 мс, 4 серии по 60-120 с).

| Наблюдение | Статус |
|---|---|
| Chromium крадёт фокус на старте даже при SW_HIDE (`foreground == chrome` через 0.3-0.4 с, окно `IsWindowVisible == false`) | [FACT] |
| Окно браузера коллектора само выходит из minimized в normal и забирает фокус через ~30-40 с после старта; живёт в normal ~1.4 с | [FACT], воспроизведено 2 раза |
| Воспроизводится и без кода коллектора (только `launch_chrome.ps1`), и не лечится ни SW_HIDE, ни `ShowWindow(SW_SHOWMINNOACTIVE)`, ни `SetTopmost`, ни чистым профилем, ни одним браузером вместо трёх | [FACT] |
| Источник — внутри Chromium/Windows, не в одной строке нашего кода | [FACT] (ограничение вывода) |
| `ozon_collector/window_policy.py` (сторож, **выключен** без `OZON_WINDOW_GUARD=1`): окно в minimized за ~0.06 с, фокус возвращается ALT-trick'ом | [FACT] на живых прогонах 06.10; по умолчанию не применяется |
| `GetLastInputInfo` не учитывает ни программные `ShowWindow`, ни `SendInput` — ручное открытие окна отличается от самовсплытия | [FACT], замер 06.10 |
| Влияние режима окна на challenge rate (document.hidden) | [HYPOTHESIS], A/B не проводился |
- **Порт:** `--remote-debugging-port=9222` (воркеры `9222+k`), CDP на localhost.

**Авто-запуск:** `cdp.launch_and_wait` запускает браузер сам, если закрыт
(отключение — env `OZON_AUTO_LAUNCH=0`).

---

## 5. L2 — Маскировка автоматизации: её НЕТ, и это решение

**Ревизия 2.9 (2026-09-26):** этот раздел раньше описывал несуществующий код —
init-script `Object.defineProperty(navigator,'webdriver',{get:()=>undefined})` и
флаг `--disable-blink-features=AutomationControlled`. Ни того, ни другого в
проекте нет и никогда не было в боевом пути. Документ исправлен, а сам факт
зафиксирован в `tests/test_cdp_launch.py` (guard-тест: у новых страниц
`init_scripts == []`, в args лаунчера нет `AutomationControlled`).

### Что происходит на самом деле

- Chrome запускается **без** `--enable-automation`, порт CDP явный (`9222+k`),
  не `0`. В этих условиях `navigator.webdriver` нативно `false`. [FACT]
- `Session._connect` **не** ставит ни одного init-script (`self._managed_pages`
  + `add_init_script` не используются). [FACT]
- Значение `navigator.webdriver` пишется в fingerprint-телеметрию (§3) как
  контроль: если оно вдруг `true` — значит браузер запущен с чужыми флагами. [FACT]

### Почему не добавляем JS-подмену (обоснованное решение, а не забывчивость)

- JS-подмена геттера через `Object.defineProperty` **оставляет цифровой
  отпечаток**: `Object.getOwnPropertyDescriptor(Navigator.prototype,
  'webdriver')` отдаёт функцию, у которой `toString()` не содержит
  `[native code]`. Для чужого антибота это читаемый маркер инъекции. [HYPOTHESIS]
- Механически «чистый» путь — нативный флаг браузера, а не подмена в JS: без
  `--enable-automation` геттер возвращает `false` сам. [FACT]
- Локальные измерения проекта (2026-09-24/25) **не поддержали** версию
  «webdriver-подмена — главный триггер»: Chromium CDP с нативным
  `navigator.webdriver === true` отдаёт нормальные страницы Ozon, а любая
  geckodriver/Marionette-сессия (stock Firefox, `webdriver=true`, подмены
  нет) получает заглушку «нет соединения» на первом же `goto`. То есть tell —
  в транспорте (Marionette), а не в значении флага. [FACT]
- Вывод: **сам факт `webdriver=true` — не главный триггер** [OBSERVATION];
  утверждение «достаточно убрать webdriver-подмену» — [UNKNOWN], оно не
  проверялось A/B на живых сессиях. Поэтому подмену не добавляем и не
  удаляем — её нет.

### Ограничение (важно)

- **Ограничение:** подмена `webdriver` — это compatibility measure, а НЕ
  anti-bot bypass. Qrator смотрит на много признаков. Поэтому маскировка
  минимальна и не разрастается в «стелс-пак» (canvas-noise, случайный UA,
  эмуляция всего подряд — намеренно НЕ делается, это красный flag).
- Почему не расширяем:
  - каждый новый «стелс-приём» сам по себе становится сигналом, если Qrator
    научился его детектить (раз STS/совпадения версий);
  - чем больше кастомизации, тем сложнее поддерживать и тем выше риск сломать
    легитимную навигацию;
  - реальная защита — это устойчивое recovery + бюджет + cooldown (L5–L7), а
    не погоня за «идеальным fingerprint».
- Не хранится и не создаётся никаких CDP-артефактов, которые могли бы
  палить автоматизацию сверх необходимого.

### Осознанный слепой угол: CDP-аномалии

Живой Chromium под CDP-протоколом сам по себе детектируем (атрибуты `cdc_*`,
вызовы `Runtime.enable`, отклики отладчика) — независимо от `navigator.webdriver`.
Это внутреннее свойство любого `connect_over_cdp` и НЕ закрывается эмуляцией.
Мы это НЕ отрицаем и НЕ пытаемся скрыть «стелс-паком» (см. §16) — вместо этого:

- базовая fingerprint-телеметрия (§3) даёт сигнал, если анрилистичный
  отпечаток совпадает с волнами challenge;
- реальная страховка — устойчивая лестница + бюджет + cooldown (L5–L7), а не
  погоня за невидимостью; система достраивает то, что не смогла маскировка.

Полноценный CDP-fingerprint-probe (замер `cdc_*`, отклонений протокола) —
backlog P2 (§18.5).

---

## 6. L3 — Поведение: тёплый вход и контроль нагрузки

`search.py::_warmup_to_category`:

- Прямой холодный вход на категорию провоцирует access_challenge. Поэтому
  сначала открывается главная `ozon.ru` (пауза на JS-челлендж/куки), затем в
  **той же вкладке** переход на категорию.
- **Challenge на главной НЕ тащится на первый PID (ревизия 2.6):** если
  `check_challenge` на главной снял challenge → переход на **свежую вкладку**
  (`new_page()`) ДО категории (FAB залипает на вкладке, как в FSM). Страница
  главной при этом помечается `is_invalid`, чтобы на неё не возвращаться. Так
  первый товар не «оплачивает» битый warmup.
- **Warmup new_tab — «бесплатный» (контракт, ревизия 2.7):** `sess.new_page()`
  из warmup НЕ расходует `session_used{new_tab,challenge,browser_recycle}` —
  у warmup собственный контур, не связанный с инцидент-счётчиками сбора
  товаров. То есть серия challenge на warmup (сколько бы вкладок ни создалось)
  никогда не подрезает бюджет первого товара. Ограничение только одно:
  `wall_clock_s` — это таймер ВСЕГО run (включая warmup), см. §18.
- Паузы после навигаций (`WARMUP_HOME_WAIT_MS=8000`, `WARMUP_CAT_WAIT_MS=8000` —
  на антибот-куки) умножаются на джиттер `0.75–1.35` (ревизия 2.9): два
  прогона подряд не ждут одинаковое число миллисекунд.
- **Активный прогрев** (ревизия 2.9, `OZON_ACTIVE_WARMUP`, по умолчанию ВКЛ):
  1–2 прокрутки и 2–3 движения мыши между навигациями. Пишет
  `recovery.warmup_activity` / `warmup_entry` / `warmup_challenges`.
  `[HYPOTHESIS]` влияние на блокировки не измерено; отключается флагом.
  Выключайте, если нужна строгая детерминированность пауз (например, в
  тестах про межтоварные паузы).
- Между товарами — `pause_s` (если задан), НЕ после последнего.

### Fail-fast целевого URL (ревизия 2.9, P0)

`net.normalize_target_url()` + `_validated_campaign_url()` в `search.py`:

- **Чинится** (с warning в лог): markdown-обёртка `[текст](url)`, «умные»
  кавычки, угловые скобки, хвостовая пунктуация, пробелы, URL внутри текста.
- **Отвергается** (`ValueError` → exit 2 в демоне, `SystemExit` в CLI):
  пусто, нет схемы http(s), `javascript:`, нет host, порт вне диапазона.
- Точки вызова: `key_daemon.main()` (`--url`, `--canary-url`),
  `key_daemon._inproc_collect()` (URL из `state.json` кампании — мусор,
  записанный до 2.9, иначе пережил бы рестарт демона), `collect_search()`,
  `cli.cmd_collect/cmd_gather`.
- Нормализация идёт **до** `resolve_canary_url()`: иначе canary получил бы
  сырой мусор и тихо уехал бы на главную вместо категории (ревизия 2.8).
- В `pids`/rescan-режиме URL не используется для навигации (служебная строка) —
  не валидируется.

`[FACT]` Живой случай 2026-09-26: в `--url` пришла markdown-ссылка
`[https://.../zvukoizolyatsiya-33302/?...](https://.../fonari-...-11061/?type=47720)`.
Раньше это давало `Page.goto: Cannot navigate to invalid URL` → 0 ссылок →
`status=NORMAL_DONE discovered=0`: демон числился «здоровым», а кампания часами
давала ноль. Тесты: `tests/test_net.py::TestNormalizeTargetUrl`,
`tests/test_key_daemon.py::TestDaemonUrlGuard`,
`tests/test_search.py::test_collect_search_rejects_junk_url`.

### EMPTY_SEARCH (ревизия 2.9)

Прогон завершился без падения, но сетка выдачи не отрисовалась
(`recovery.search_grid_empty`): раньше это давало `NORMAL_DONE discovered=0` —
ложный «успех». Теперь `classify_status()` отдаёт `EMPTY_SEARCH`: стагнация
считается, прогрессом — нет, причина видна в логе и `status.json`.
`CHALLENGE_STORM` имеет приоритет над `EMPTY_SEARCH` (это разные вещи).

### Зачем тёплый вход (механика)

Свежая сессия (новый профиль или cold-start) не имеет Qrator-куки
`isValid_*` / auth-токена. Прямой заход на глубокую страницу (карточку) без
«пути» выглядит как бот-заход: нет реферальной цепочки, нет истории кликов,
внезапная навигация в глубину. Холодный вход на категорию — самый быстрый
способ получить первичный JS-челлендж.

Тёплый вход эмулирует «живого пользователя»:
1. главная — ресурс низкой ценности, Qrator редко блокирует её сразу;
   при загрузке раздаются auth-куки/токены;
2. только после готовности кук — переход на категорию (уже «из сессии» с
   куками);
3. карточка — внутри уже прогретой сессии.

Результат измеряется не словами, а метриками: доля товаров, зашедших БЕЗ
challenge на первом `goto`. Если после тёплого входа стабильно идёт challenge
— значит, дело не в «холоде», и срабатывает L5/L6.

### Нагрузка (L3 vs L7)

Паузы между навигациями и `pause_s` — это **темп** (L3). Но «сколько раз за
run можно восстанавливаться» — это **бюджет** (L7). Темп снижает частоту
триггера; бюджет ограничивает ущерб, если триггер сработал. Их нельзя путать:
дикий темп без бюджета — не защита, а бюджет без темпа — быстрый «выжигание»
лимита восстановлений.

---

## 7. L4 — Детект челленджа (критично для K4)

`collect.py`:

- `CHALLENGE_TITLE_MARKERS` — маркеры `<title>`: «похоже, нет соединения»,
  «доступ ограничен», «подтвердите, что вы не робот», «captcha», ….
- `CHALLENGE_BODY_MARKERS` — маркеры DOM-текста: `fab_chlg_`, `fab_cp_`,
  «выключите vpn», «сопоставьте пазл», «двигая ползунок», ….
- `detect_challenge(page)` → сигнатура или `None`. **0 отзывов НЕ признак
  челленджа** — детектор полагается только на сигнатуры, чтобы не ронять
  реальные карточки без отзывов.
- `check_challenge(page, where, reload_on_fab=True)`: до `FAB_RELOAD_ATTEMPTS=3`
  перезагрузок с паузой 2.5с (простая FAB-заглушка снимается reload). Если
  остаётся — поднимает `AccessChallengeError` **и помечает `page.is_invalid=True`**.

`gather.py::_check_raw` (acceptance-gate): challenge-маркер в `product.title` →
`AccessChallengeError` (recyclable); битая структура → `StructureError`
(не recyclable). **Gate применяется и к кеш-хиту**: challenge никогда не
проходит в результат как успех.

Возрастная заглушка («Подтвердите возраст») детектится отдельным JS-пробом.

**INVALID-страница (K4):** после детекта challenge страница помечается
`is_invalid=True`. `Session._guard_check` бросает `AccessChallengeError` на
любой `eval/goto/scroll/wait` такой страницы — данные с заглушки физически
невозможно извлечь. Флаг снимается только на новой чистой вкладке
(`new_page` / `ensure_single_worker_tab` / `reconnect`).

### Почему детект — фундамент K4 (механика «двух заслонов»)

Достоверность данных (K4) держится на **двух независимых заслонах**:

1. **`detect_challenge` (до извлечения)** — по `<title>` и DOM-тексту.
   `FAB_RELOAD_ATTEMPTS=3` с паузой 2.5с: простая FAB-заглушка (не Qrator)
   снимается самим reload. Если после 3 reload'ов сигнатура осталась —
   это стойкий challenge, `AccessChallengeError` + `page.is_invalid=True`.
2. **acceptance-gate `gather._check_raw` (после извлечения)** — даже если
   challenge просочился каким-то путём в `product.title`, gate ловит маркер и
   превращает результат в ошибку (recyclable), а не в «успешный» товар.

Почему именно «0 отзывов» не считается challenge: детектор полагается только
на сигнатуры (title/markers), а не на пустоту. Реальная карточка без отзывов
не должна роняться — иначе мы бы теряли валидные данные. Отличить «нет
отзывов» от «заглушка» можно только по структуре DOM/маркерам, что и делает
детектор.

`page.is_invalid` — это «радиационная метка»: любой вызов `eval/goto/scroll`
на такой странице немедленно кидает `AccessChallengeError` до того, как код
успеет что-то извлечь. Это защита от «частичного чтения» заглушки. Метка
снимается только на новой чистой вкладке — т.е. «загрязнённая» страница
навсегда исключена из сбора.

### Однотонный кадр = не «нет лунок», а свёрнутое окно (L4, ревизия 2.8)

`_frame_painted` (std(grayscale) < `FLAT_STD`) отличает валидный PNG капчи от
однотонной заливки. Заливку отдаёт Chromium под Windows, когда окно свёрнуто или
перекрыто (особенно при старте через `SW_SHOWMINNOACTIVE`): буфер есть, размеры
верные, рисования нет. Пассивная пересъёмка после паузы это не лечит — она
повторяет ту же ошибку и сжигает лимит попыток.

Поэтому перед reshoot выполняется `_bring_to_front` (best-effort `page.bring_to_front()`,
best-effort — нет API/ошибка не ломают съёмку, причина пишется в
`_shot_bg_reason` как `front=ok` / `front_err=<Тип>`). Кадр после подъёма окна
переснимается один раз; если он всё ещё плоский — честный отказ, без молчаливых
кандидатов.

---

## 8. L5 — Recovery: FSM «один challenge-инцидент»

### Контракт (ADR §14.2)

**Один challenge-инцидент = ограниченная лестница, НЕ цикл:**

```
reload×3 (внутри check_challenge)
   ↓ (не снялось)
new_tab×1
   ↓ (не снялось)
recycle×1
   ↓ (не снялось)
SESSION_BLOCKED → abort run
```

- Лестница **не крутится** в цикле: после исчерпания — stop, никаких скрытых
  повторных попыток внутри того же run.
- После `run_stopped` — только демон (run-boundary): cooldown → новый run.

### Multi-incident vs per-run budget (важно не путать)

- **Per-incident** (один challenge на товаре): `reload×3 → new_tab×1 →
  recycle×1 → SESSION_BLOCKED`. Это про ОДИН инцидент, НЕ про весь run.
- **Per-run budget** (§9): `SESSION_BUDGET_DEFAULT {challenge: 3, new_tab: 3,
  browser_recycle: 2, wall_clock_s: 1800}`. За run можно пережить ДО N таких
  инцидентов (каждый со своей лестницей); при исчерпании — `SESSION_EXHAUSTED`
  (abort run).

То есть «один challenge» ≠ «весь run навсегда»: после успешного recovery на
одном товаре демон продолжает, пока не исчерпан бюджет сессии.

### Реализация (`search.py::_collect_shard`)

- Константы: `INCIDENT_NEW_TAB_MAX = 1`, `INCIDENT_RECYCLE_MAX = 1`.
- При `access_challenge`:
  1. `used["challenge"] += 1` (session budget, см. §9); если бюджет исчерпан →
     `SESSION_EXHAUSTED`.
  2. `incident_new_tab > 0` → `sess.new_page()`, `access_recovery=True`,
     `observed_scope`-кандидат `tab_observed`.
  3. иначе `incident_recycle > 0` → `_recycle_browser()` (для collector —
     restart+reconnect; для external — только `ensure_single_worker_tab`),
     кандидат `browser_observed`.
  4. иначе → `SESSION_BLOCKED` (abort run, остаток aborted).

### Разбор «один challenge-инцидент» на примере

На одном товаре `check_challenge` делает до 3 reload'ов. Допустим, не снялось:

- **Шаг 1 (reload×3):** `check_challenge` сам перезагрузил страницу 3 раза с
  паузой 2.5с. Простую FAB-заглушку это снимает. Не снялось →
  `AccessChallengeError`, `page.is_invalid=True`.
- **Шаг 2 (new_tab):** `incident_new_tab=1` → создаётся новая чистая вкладка
  (`new_page`), прыжок на товарный URL в ней. Это снимает FAB, который
  «залип» на старой вкладке. Снялось → товар собран, `observed_scope`
  кандидат `tab_observed`, canary **не запускается** (чистота доказана на
  товаре через `check_challenge`).
- **Шаг 3 (new_tab не помог):** тогда `incident_recycle=1` →
  `_recycle_browser()` (для collector — перезапуск+reconnect; для external —
  только `ensure_single_worker_tab`). Новая сессия → **canary** (главная) —
  проверка, что доступ вообще есть. Снялось → товар собран, кандидат
  `browser_observed`.
- **Шаг 4 (и recycle не помог):** лестница исчерпана → `SESSION_BLOCKED`
  (abort run, остаток aborted, `observed_scope=profile_observed`).

Ключевое: каждый шаг — это **другие счётчики бюджета** (used.challenge,
used.new_tab, used.browser_recycle по §9) и **другая метка observed_scope**. Так
по телеметрии видно, на каком уровне восстановилась сессия.

### `observed_scope` (ADR §14.3) — только наблюдение

- `session_blocked` (лестница исчерпана) → `profile_observed`;
- товар собран через `new_tab` → `tab_observed`;
- товар собран через `recycle` → `browser_observed`;
- иначе `unknown`.

Это **наблюдение, а не диагноз**: система не делает выводов «это profile ban»
и не принимает по нему деструктивных решений (профиль не удаляется).

### Типы сбоев (ADR §14.1) — не смешиваются

`Classification.failure_type` (browser.py):

| error_kind | failure_type | PID штрафуется? |
|---|---|---|
| `access_challenge` | `access_failure` | нет |
| `browser`, `renderer_failure`, `target` | `browser_failure` | нет |
| `structure`, `variant_short` | `product_failure` | да (retry/rescan товара) |
| `unknown` | `unknown` | — |

`run_gather` пишет `res["failure_type"]`. `SESSION_BLOCKED`/`SESSION_EXHAUSTED`
имеют `failure_type=session_failure` — PID не резканится из-за проблем доступа.

---

## 9. L7 — Session budget

`SESSION_BUDGET_DEFAULT = {challenge: 3, new_tab: 3, browser_recycle: 2, wall_clock_s: 1800}`.

- Лимиты на **весь запуск (run)**, а не на товар.
- `session_used {challenge, new_tab, browser_recycle}` инкрементируется при
  каждом действии; `wall_clock_s` — жёсткий таймер сессии.
- Исчерпание → `SESSION_EXHAUSTED` → abort run (остаток aborted, session_failure).
- `product_retries` (max_recycles) — **отдельно**, на товар: проблемы одного
  PID не сжигают бюджет всей сессии.
- **Canary НЕ расходует `session_used{challenge,new_tab,browser_recycle}`.**
  Его результат — только `canary_fails` (и, при canary-fail, остановка run с
  `session_failure`). Иначе canary после каждого recycle «тратил» бы и так
  скудный бюджет на проверку, а не на сбор. Это явно зафиксировано, чтобы не
  интерпретировать canary как восстановление уровня FSM.

### Таблица «событие → счётчики → состояние»

| Событие | `session_used` инкрементируется? | Итог/состояние |
|---|---|---|
| `check_challenge` → reload снял (transient) | нет | товар далее ok |
| access_challenge → new_tab снял | challenge+1, new_tab+1 | товар ok, `tab_observed` |
| access_challenge → recycle снял → canary ok | challenge+1, browser_recycle+1 | товар ok, `browser_observed` |
| access_challenge → лестница исчерпана | challenge+1 | `SESSION_BLOCKED` → abort run, `profile_observed` |
| challenge+1 ≥ budget.challenge | — (сработал до трат) | `SESSION_EXHAUSTED` → abort run |
| canary (после recycle) | **нет** | `CANARY_OK/CHALLENGE/ERROR` → `canary_fails` |

### Canary проверяет СУЩНОСТЬ ТОГО ЖЕ КЛАССА (ревизия 2.8)

**Проблема.** Каноничной была жёстко зашитая главная `https://www.ozon.ru/`.
Главная **мягче** карточки по лимитам: `CANARY_OK` на ней не значит, что
сессия видит `/product/`. Теневой бан на карточке главная не покажет — сессия
считалась восстановленной, `session_used` бюджет уходил впустую, и первый же
товар снова ловил challenge (тот же recycle по кругу).

**Правило.** Canary бьёт по сущности того же класса, на которой упало, иначе
проверка ничего не доказывает. Классы: `product` (`/product/`, жёстче всего) →
`category` (`/category/`, `/c/`) → `search` (`/search/`, `text=`) → `home`
(главная, мягче всех) → `custom`.

**Порядок выбора цели** (`search.resolve_canary_url`):

1. `--canary-url` (CLI демона, `run_cfg.canary_url` в манифесте);
2. env `OZON_CANARY_URL` (удобно для оператора: «поставь лёгкий товар»);
3. **URL текущей кампании** — дефолт: ровно та сущность, ради которой идём;
4. главная `https://www.ozon.ru/` — только fallback.

Конкретный pid в коде **не хардкодится**: он устареет и сам станет тяжёлым или
заблокированным (как у любой «безопасной» ссылки). Не-http аргумент
(например, `url="список из N артикулов"` в rescan-режиме демона) уровнем
цели не считается — молча уходим на следующий.

**Телеметрия.** Решение оркестратора логируется один раз
(`[canary] цель: <kind> (<url>)`), результат пишется в
`recovery.canary = {url, kind, result}`, агрегаты — `kpi.canary_fails` и
`kpi.ip_blocks` (canary-fail и бан IP — разные причины остановки). Canary
по-прежнему НЕ расходует `session_used` и не трогает PID.

**Per-worker вызовы.** `collect_parallel` решает цель ОДИН раз и передаёт её
воркерам в `task["canary_url"]` (через общий `_worker_task`), чтобы воркер и
его замена после респавна не проверяли разные сущности. `collect_search` кладёт
цель в `recovery["canary_url"]`.



### Пример расхода бюджета (пошагово)

`SESSION_BUDGET_DEFAULT {challenge: 3, new_tab: 3, browser_recycle: 2, wall_clock_s: 1800}`.

Счётчики `session_used{challenge,new_tab,browser_recycle}` инкрементируются на
КАЖДОМ действии. Ниже — пошагово, где видно каждое `+=1`:

| шаг | событие | challenge | new_tab | recycle | примечание |
|---|---|---|---|---|---|
| 1 | товар 1: ok на 1-м goto | 0 | 0 | 0 | ничего не тратим |
| 2 | товар 2: challenge→ | **1** | 0 | 0 | `used.challenge+=1` |
| 3 | → new_tab снял→ товар ok | 1 | **1** | 0 | `used.new_tab+=1` |
| 4 | товар 3: challenge→ | **2** | 1 | 0 | `+=1` |
| 5 | → new_tab снял (вторая new_tab) | 2 | **2** | 0 | `+=1` |
| 6 | товар 4: challenge→ | **3** | 2 | 0 | `+=1` (равно budget.challenge) |
| 7 | → recycle снял→ canary ok→ товар ok | 3 | 2 | **1** | `+=1` |
| 8 | товар 5: challenge→ | **4** | 2 | 1 | `+=1` → **4 > 3** |
| 9 | → `SESSION_EXHAUSTED` ДО трат new_tab/recycle | 4 | 2 | 1 | abort run |

Порядок в `_collect_shard`: сначала `used["challenge"] += 1`, и если уже
`> budget.challenge` → `SESSION_EXHAUSTED` ДО того, как тратить new_tab/recycle.

### SESSION_BLOCKED vs SESSION_EXHAUSTED — что реально делает код

Оба abort'ят run и оба в коде проставляют `session_blocked=True` → на уровне
товара `observed_scope="profile_observed"`, а демон классифицирует ОБА как
`CHALLENGE_STORM` (`classify_status`: любой `combined.aborted`). На текущий
момент **эскалация у них одинаковая** — это важное ограничение:

- **`SESSION_EXHAUSTED`** = «слишком много МЕЛКИХ challenge/действий за run» —
  инкремент счётчика превысил лимит бюджета. Это «усталость сессии», НЕ
  доказанный профильный бан.
- **`SESSION_BLOCKED`** = «ОДИН инцидент дожат до конца лестницы (new_tab и
  recycle не помогли)» — достовернее похоже на блок доступа.

Оператор различает их по `aborted.reason` и раздельным счётчикам
`recovery.session_blocked` / `recovery.session_exhausted` (они уже есть).

**Известное упрощение (честно, ревизия 2.7):** идея «EXHAUSTED → мягче
эскалация, не всегда `profile_observed`» из критики — осознанное НЕ реализовано.
Сейчас оба состояния дают одинаковую storm-эскалацию в демоне. Чистое
разделение эскалации (`EXHAUSTED` не всегда шторм/`profile_cooldown`) —
кандидат в backlog §18.6, т.к. требует аккуратной смены логики демона и тестов,
а не только документа.

### `wall_clock_s` — настраиваемый (ревизия 2.6)

`wall_clock_s` — жёсткий таймер сессии. Дефолт 1800 (30 мин); для длинных
одиночных `search` (`--limit > 100`) — параметризуется через
`--session-budget-wall-clock` (см. §18.3) и хранится в `run_cfg`. Это НЕ
жёсткая константа: одиночный прогон с большим `--limit` не должен получать
`SESSION_EXHAUSTED` без возможности настроить.

---

## 10. Canary после recovery (L5/L7)

`run_canary(sess, url=None)`:

- **цель выбирается по классу сущности**, а не жёстко главная: `--canary-url` →
  `OZON_CANARY_URL` → URL текущей кампании → `https://www.ozon.ru/`
  (`search.resolve_canary_url`, детали и телеметрия — §9);
- `goto` → `check_challenge` + минимальная DOM-проверка;
- состояния `CANARY_OK / CANARY_CHALLENGE / CANARY_ERROR / CANARY_IP_BLOCKED`;
- пишет только в логи/`recovery.canary`/счётчики `canary_fails`+`ip_blocks`,
  НЕ в raw и НЕ в метрики сбора.

**Когда:** только после **recycle по доступу** (новая сессия — чистота НЕ
доказана), НЕ после new_tab (challenge уже снят на товарном URL и товар собран
через `check_challenge` — canary избыточен, лишний заход на Qrator) и НЕ после
профилактического recycle по стабильности.

**Поведение при canary-fail (контракт, ревизия 2.7):** canary-fail →
**`SESSION_BLOCKED` немедленно** (abort run, остаток aborted, session_failure) —
дальнейший сбор останавливается, но уже собранный товар сохраняется
(ok=True, raw на месте); session_failure не штрафует PID (ADR §14.1).
`canary_fails` — отдельный счётчик телеметрии: НЕ расходует `session_used` и
НЕ влияет на бюджет напрямую, но является сигналом для демона (повторяющиеся
canary-fail после recovery указывают на системную блокировку уровня входа,
а не на товар).

**Историческая слабость (ревизия 2.2 → закрыта в 2.8).** Canary на главной
мягче по лимитам, чем карточки: `CANARY_OK` на главной не гарантирует, что
карточки доступны (теневой бан на *товары* главная может не показать) → сессия
считалась восстановленной, бюджет `SESSION_BUDGET` уходил вхолостую, первый же
товар снова ловил challenge. **Реализовано (2.8):** каноничная цель — сущность
того же класса, на которой упало; главная осталась только fallback'ом, а
конкретный pid не хардкодится (устаревает и сам становится тяжёлым) — его задаёт
оператор через `--canary-url`/`OZON_CANARY_URL` либо подставляется URL кампании.
Теперь `CANARY_OK` означает «сессия видит то, ради чего мы здесь».

**Важно:** canary не расходует `session_used` (§9) — у него собственный счётчик
`canary_fails`.

---

## 11. L6 — Adaptive cooldown и Profile cooldown (демон)

### Adaptive cooldown (`key_daemon.py`)

`challenge_control {storm_streak, total_storms, last_storm_at, cooldown_until,
last_signature}` в progress.json — **persistent** (переживает рестарт демона).

- `CHALLENGE_COOLDOWN_STEPS_S = [60, 300, 900]`: storm#1→1м, #2→5м, #3→15м.
- `CHALLENGE_JITTER_FRAC = 0.2` — джиттер ±20% (в тестах — `KD_RANDOM_SEED`).
- `storm_streak` растёт ТОЛЬКО после полностью исчерпанного инцидента
  (SESSION_BLOCKED → `combined.aborted`), не после каждого transient.
- Сброс `storm_streak` при прогрессе (new_disc/new_valid).
- При `storm_streak > len(steps)` → **NEEDS_ATTENTION** (не молотить).

### Profile cooldown

`profile_cooldown {until, reason, challenge_count, last_signature}` — отдельно
от storm (чтобы видеть, почему демон стоит). Заполняется при
`repeated_challenge`.

**Честное уточнение (ревизия 2.7):** `profile_cooldown.until` на текущий
момент = **то же значение**, что и storm `cooldown_until` — это НЕ независимый
таймер с собственной длительностью, а **ярлык причины** на общей временной
метке. Он существует для диагностики (status.json: «почему стоит → repeated
challenge»), а не ради отдельного второго таймера. Поэтому
`max(storm, profile)` всегда тривиален. Если позднее потребуется отдельная
продолжительность (напр. профиль остывает дольше, чем storm-пауза) — это
осознанное расширение, в текущем коде НЕ реализовано.

### Точный триггер `profile_cooldown` (по коду)

`profile_cooldown` заполняется в том же блоке, что и storm-эскалация —
срабатывает при статусе прогона `CHALLENGE_STORM` (`combined.aborted` есть,
т.е. хотя бы один воркер исчерпал session/FSM). Конкретика:

- `reason = "repeated_challenge"`, `until = cooldown_until` (та же эскалация),
  `challenge_count = total_storms`, `last_signature = last_challenge_signature`.
- С учётом агрегации (§2.1/§11 ниже) он ставится ТОЛЬКО при **ФУЛЛ-шторме**
  (`parallel==1` или `all_workers==True`), а не при partial.
- `until` берётся из эффективного cooldown = `max(storm, profile)`.

То есть «repeated_challenge» = «прогон(ы) завершился аборт'ом из-за
persistent access», а не «одиночный transient challenge за run».

### Приоритет cooldown (ревизия 2.4)

Эффективный cooldown в цикле демона = **максимум** из `cooldown_until`
(storm-эскалация) и `profile_cooldown.until`. Оба блокируют старт следующего
run; когда оба истекли — можно продолжать (при необходимости через canary).
`profile_cooldown` — признак ФУЛЛ-шторма (см. «Точный триггер» выше), а не
каждого transient.

### Агрегация storm при параллелизме (ADR §14.6, ревизия 2.5)

Шторм **одного** воркера ≠ шторм всей кампании. `parallel.py::_storm_summary`
кладёт в `combined["aborted"]` поля `workers_stormed[]` и `all_workers`
(`len(storms) == parallel`). Демон эскалирует storm/profile cooldown по этому
правилу:

- **ФУЛЛ-шторм** (`parallel==1` или `all_workers==True`) → `storm_streak++`,
  `profile_cooldown` ставится, пауза по шкале `[60,300,900]`, при переборе —
  `NEEDS_ATTENTION`. Означает «Ozon душит вход целиком».
- **Частичный шторм** (`all_workers==False`) → короткая фиксированная пауза
  `CHALLENGE_PARTIAL_PAUSE_S=20с`, **БЕЗ** эскалации `storm_streak` и БЕЗ
  `profile_cooldown`. Остальные воркеры прогрессируют; один упершийся воркер
  не должен сжигать cooldown всей кампании.

Это закрывает риск «любой воркер → +1 streak» (он выжигал бы кампанию по
частям). Уже собранные воркерами товары сохраняются; пауза короткая.

**Хронический partial storm (ревизия 2.7):** риск фиксированных 20с — при
хроническом состоянии «1 из N воркеров всегда штормит» кампания вечно слегка
тормозит, не эскалируя. Это осознанный компромисс (anti-stampede важнее), но
имеет смысл soft-сигнал: **N частичных штормов подряд** → предупреждение в
`status.json`/events (`partial_storm_seq`), БЕЗ полной эскалации. Это лёгкое
решение для оператора, не меняющее агрегацию ФУЛЛ/partial. (Отдельная задача
— см. §18.6.)

### Прочие backoff

- CRASH → `BACKOFF_DELAYS_S[0]` (5 мин).
- UNKNOWN_FAILURE → транзиентный retry до `--max-transient-retries=3` с
  паузой `TRANSIENT_RETRY_BACKOFF_S=4`.

### Как демон решает «ждать или идти дальше» (механика)

В цикле демона решение выглядит так:

1. Читает `cooldown_until` (storm-эскалация) и `profile_cooldown.until`.
2. **Эффективный cooldown = max(обоих).** Если `now < effective` — спит
   (журналирует причину: `storm` или `profile`), не трогая браузер.
3. Когда оба истекли — запускает следующий run. При необходимости первой
   проверкой идёт canary (если была profile-пауза) — дешёвая проверка, что
   сессия «отлежалась» и теперь чистая.

**Осторожно с canary после cooldown (ревизия 2.7):** canary сейчас на главной
`ozon.ru` (§10), а главная мягче лимитов и НЕ гарантирует доступ к карточкам.
Значит canary-ok после profile-паузы может создать **ложное чувство «можно
идти»** — воркер идёт на товар и снова ловит challenge, сжигая `SESSION_BUDGET`.
Пока canary не переведён на лёгкий товар/категорию (P1, §18.4), трактуем
canary-ok как «сессия жива на уровне входа», а не «карточки доступны».

Логика `storm_streak`: растёт ТОЛЬКО после полностью исчерпанного инцидента
(`SESSION_BLOCKED` → `combined.aborted`), т.к. именно он означает «эта волна
упёрлась в стену». Отдельный transient challenge (`reload` сработал) — это
норма, не повод для эскалации. Шкала `[60,300,900]` секунд: каждая НЕУДАЧНАЯ
волна (после полного блока) увеличивает паузу, уменьшая давление и давая
scope «остыть». Прогресс (новые найденные/валидные товары) сбрасывает
`storm_streak` — система «верит», что ограничение ушло.

`NEEDS_ATTENTION` — это не «стоп навсегда», а сигнал человеку: демон дошёл до
потолка шкалы и не должен молотить вслепую. Дальше либо ждать дольше, либо
сменить вход (профиль/IP), либо пересмотреть кампанию.

---

## 12. Телеметрия и наблюдаемость

Каждое recovery-событие обогащается `_recovery_meta`:
`signature, last_url, browser_tabs, collector_tabs, duration_ms,
products_since_recovery`.

- `browser_tabs` — внешний снимок через HTTP `/json/list` (диагностика).
- `collector_tabs` — собственный набор managed-страниц Session (источник
  истины для ownership, см. §13).

`key_daemon._absorb_recovery_telemetry` переносит метрики из combined в
progress; `status.json` содержит `browser_tabs, collector_tabs, last_page_url,
last_challenge_signature, challenge_control, profile_cooldown`.

### Как читать телеметрию (наблюдателю)

- **`observed_scope`** — где восстановилась сессия: `profile_observed` (лестница
  до конца — доступ похож на «заблокирован для этого профиля»), `tab_observed`
  (достаточно новой вкладки), `browser_observed` (понадобился перезапуск
  браузера), `unknown`. Смотреть распределение по run: если много
  `profile_observed` подряд → ждём cooldown/меняем вход, а не молотим.
- **`products_since_recovery`** — сколько товаров собрано с последнего recovery.
  Большое значение = сессия «отлежалась» и работает; малое и растущее число
  challenge = давление растёт.
- **`browser_tabs` vs `collector_tabs`** — расхождение означает «чужие» вкладки
  (external-браузер) или утечку вкладок (нужен ownership-checks, §13).
- **`challenge_control / profile_cooldown`** — почему демон стоит: `storm` из-за
  серии полных блоков, или `profile` из-за признаков «профильного» ограничения.
  Причина — в поле `reason`, а не в догадках.

### Per-worker телеметрия параллелизма (ревизия 2.8)

Раньше challenges были только СУММОЙ по пулу — по отчёту нельзя было понять,
вредит ли параллелизм именно этому IP/профилю или это общий шторм. Теперь
`combined.parallel.workers[k]` отдаёт:

- `challenges` — access-challenges конкретного воркера;
- `challenge_rate_per_100` — на 100 товаров этого воркера
  (`null`, если товаров не было);
- `recovery.canary = {url, kind, result}` — какая сущность проверялась и что
  ответила (kind — из `classify_canary_url`).

Агрегаты в `combined.kpi` дополнены `canary_fails` и `ip_blocks` (canary-fail и
бан IP — разные причины остановки run, смешивать их нельзя).

**Как пользоваться:** A/B-замер `parallel=1` против `parallel=2` на одинаковой
кампании и сравнение `challenge_rate_per_100` по воркерам. Прокси на воркер по
мерерам добавляется только после такого замера — раньше это было бы
оптимизацией по гипотезе, а не по данным.

Это чтение — основа для KPI обхода (§18 backlog): доля run без SESSION_BLOCKED,
median `products_since_recovery`, распределение `observed_scope`.

---

## 13. Ownership и управление вкладками (ADR §14.1)

- `Session.browser_owner ∈ {external, collector}` определяется по маркеру
  `.ozon_collector_launched` (или факту авто-запуска). Поля: `browser_pid`,
  `browser_profile`, `browser_started_at`, `ownership_token`.
- **external:** только attach / CDP-disconnect. Никаких kill/recycle/close
  (даже в `finally`). При external `Session` создаёт СВОЮ вкладку
  (`ctx.new_page()`), не трогая `ctx.pages[0]` пользователя.
- **collector:** полный lifecycle (restart + reconnect).
- `ensure_single_worker_tab()`: безопасный порядок (create → init_script →
  rebind → close только свои managed → assert `collector_tabs==1`); при ошибке
  создания новой вкладки старая не уничтожается. Вызов — вне critical section
  collect (evaluate/scroll/topup).
- `_recycle_browser` для external — только `ensure_single_worker_tab` (без kill).

### Почему ownership так важен (механика)

`browser_owner ∈ {external, collector}` определяется по маркеру
`.ozon_collector_launched` в профиле: если профиль поднят скриптами
`launch_chrome.{ps1,sh}` (или авто-запуском) — это «наш» браузер, и мы можем
им полностью управлять (restart/recycle/close). Если же маркера нет — браузер
поднят пользователем вручную (external), и мы НИКОГДА его не трогаем
kill/recycle/close, даже в `finally`/очистке.

Причина: убийство «чужого» процесса браузера:
1. портит пользователю ручную сессию (вкладки, форма, незакрытые данные);
2. ломает сам принцип «ручной браузер = другой отпечаток, чем бот» — если
   бот убьёт ручной Chrome, пользователь уже не сможет вручную проверить,
   работает ли сайт (ключевой инструмент диагностики, §3);
3. риск collateral-ущерба при параллельных процессах.

Поэтому у external-браузера recovery ограничено безопасным набором:
`ensure_single_worker_tab` (создать/выбрать СВОЮ managed-вкладку, никогда не
закрывая `ctx.pages[0]` пользователя). Т.е. бот «живёт» в своей вкладке внутри
чужого браузера, не разрушая его.

### External: лестница access и ограничение контекста (ревизия 2.7)

**Ограничение контекста:** для external `_recycle_browser` ≡
`ensure_single_worker_tab` — recycle на external означает «открыть/выбрать ещё
одну managed-вкладку в ТОМ ЖЕ браузерном контексте». Такая вкладка наследует
заблокированные куки / испорченный Trust Score / сессионные маркеры Qrator,
поэтому recycle на external НЕ способен вывести сессию из блокировки уровня
КОНТЕКСТА/IP.

Тем не менее шаг recycle на external **выполняется** (как и у collector),
потому что эксперимент 2026-08-17 показал: свежая вкладка может пройти Qrator
и получить свежие auth-куки даже в чужом контексте — это реальный шанс снять
заглушку на товаре. Это ОТДЕЛЬНОЕ создание вкладки (не повтор того же
объекта), поэтому оно не запрещено «одним инцидентом».

Ключевое (что отличает от collector и что важно осознавать):

- Лестница: `reload×3` (внутри `check_challenge`) → `new_tab×1` →
  `recycle×1`(=ещё одна свежая вкладка) → `SESSION_BLOCKED`. Та же, что у
  collector, БЕЗ перезапуска процесса.
- Если и после recycle (вторая свежая вкладка) challenge остался — это почти
  наверняка блок уровня КОНТЕКСТА/IP, и recycle (лямбда ещё одной вкладки)
  его не снимет. Дальше `SESSION_BLOCKED` → демон (§11), а НЕ бесконечные
  повторные вкладки — «один инцидент, без цикла» соблюдается.
- Попытки на external расходуют `browser_recycle` из бюджета так же, как у
  collector, поэтому бесконечный recycle невозможен (упирается в бюджет).

Итого: для external нет перезапуска процесса, но порядок шагов тот же; главное
предупреждение — recycle не снимает контекстные блокировки, и после него
следует осознанный `SESSION_BLOCKED`.

### Поведение при потере external-браузера (ревизия 2.5)

Если пользователь **закрыл** ручной Chrome (или CDP-соединение оборвалось) во
время работы с external-браузером:

1. Соединение `Session` помечается как утраченное → `browser_failure`.
2. **Никакого авто-перезапуска чужого браузера** — мы не имеем права поднимать
   пользовательский процесс заново (он может даже не хотеть его снова).
3. run останавливается (`SESSION_BLOCKED`/abort), а демон решает: если профиль
   external более недоступен — переключиться на свой `collector`-профиль или
   приостановиться с понятным статусом. Повтор ждать, пока пользователь сам
   откроет браузер (или не сменится вход).
4. Уже собранное не теряется — save points до момента обрыва остаются в raw.

Это согласуется с §13 (не ломать пользовательскую среду) и предотвращает
«бешеный рестарт» чужого процесса.

---

## 14. Browser stability: recycle по стабильности, а не по challenge (ADR §14.5)

- Метрики разделены: `browser_stats {renderer_errors}` и
  `access_stats {challenges}`.
- Preventive recycle — ТОЛЬКО по browser stability:
  - `renderer_errors ≥ RENDERER_RECYCLE_THRESHOLD (=2)`, или
  - `since_recycle ≥ RECYCLE_EVERY (=40)` — safety-ceiling.
- Challenge **никогда** не вызывает stability-recycle.
- После recycle `renderer_errors` сбрасываются (свежий браузер — чистые метрики).

| Причина | Может вызвать recycle |
|---|---|
| renderer crash / OOM | ✅ |
| CDP disconnect / browser dead | ✅ |
| слишком старая сессия (ceiling) | ✅ |
| challenge / challenge streak | ❌ |

### Почему challenge не вызывает stability-recycle (механика)

Два разных цикла восстановления, которые НЕЛЬЗЯ смешивать:

- **Access-recovery (L5)** — реагирует на challenge. Это про *доступ*: снять
  заглушку (new_tab/recycle) и продолжить. Считается в `access_stats`.
- **Stability-recycle (§14)** — реагирует на *техническую деградацию браузера*
  (renderer crash, OOM, CDP disconnect, старая сессия). Считается в
  `browser_stats`.

Если бы challenge сам вызывал stability-recycle, мы бы **теряли диагностику**:
не могли бы отличить «сайт режет доступ» от «браузер сломался». А главное —
challenge-реакция уже регламентирована FSM-лестницей и бюджетом (L5+L7), и
добавление второго пути recovery сделало бы поведение непредсказуемым
(сколько раз на самом деле можно восстановиться за run?).

Поэтому: challenge решается строго в FSM (L5), технический сбой браузера —
по stability-логике, и у каждого — свои счётчики, свои пороги, своя
телеметрия. Это держит восстановление предсказуемым и наблюдаемым.

---

## 15. Сквозной поток (сбор одного товара)

```
goto(url) → check_challenge(reload×3)          # L4 + L5.1
   ↓ AccessChallengeError
page.is_invalid=True                            # K4: извлечение заблокировано
new_tab ×1 → ok?  → следующий товар             # L5.2 (canary не нужен:
   ↓ не снялось                                 #   чистота доказана на товаре)
recycle ×1 (по owner) → canary → ok?           # L5.3 + L10 (новая сессия)
   ↓ не снялось
SESSION_BLOCKED → abort run                     # демон: adaptive cooldown
   ↓ storm_streak>max
NEEDS_ATTENTION (persistent challenge)
```

Примечание: после успешного canary/recovery товар, с которого снят challenge,
остаётся собранным (raw на месте); challenge-товар не «добирается» в этом же
run — следующий PID идёт вперёд (демон при надобности доберёт через
`search --pids` в следующем run).

---

## 16. Чего НЕ делаем (анти-паттерны)

- ❌ Headless (классический, `--headless=old`) / `--enable-automation` /
  Playwright `launch()` вместо CDP.
- ❌ `--no-sandbox` (сигнал automation).
- ❌ Бесконечный retry при challenge — лестница строго ограничена.
- ❌ Принимать заглушку за товар (INVALID + acceptance-gate, K4).
- ❌ Полное удаление `user-data-dir` при challenge (холодный профиль хуже).
- ❌ Массовый «стелс-пак» (canvas-noise, случайный UA, эмуляция всего подряд).
- ❌ Автоподбор тактики обхода по одному `observed_scope`/одной сессии —
  масштаб определяется только по агрегатам (§11), иначе одно событие
  запускает каскад деструктивных решений.
- ❌ Работать с «чужим» процессом браузера (ownership violation).
- ❌ Подмена данных / «дорисовка» отзывов — противоречит K4 и миссии.

> **Про `--headless=new` (Chrome 112+, ревизия 2.5):** в текущей версии
> headless НЕ используется (анти-паттерн оставлен для изоляции от классического
> headless и отладочных артефактов). Полноценный UI-режим даёт идентичный
> fingerprint и корректный рендер. Как возможный эксперимент — вынесено в
> backlog (§18.6), т.к. требует живого A/B-замера, а не только документа.

### Отзывы через Ozon JSON API — только как ускоритель (ревизия 2.8)

`ozon_fetch.py` бьёт `entrypoint-api.bx` прямо из контекста страницы (те же
куки/заголовки/TLS, сессия валидна — перехват сети в Firefox недоступен). Модуль
был написан, но НЕ подключался: замеры 2026-09-25 (pid 210502516, 257 707
отзывов, `tests/probe_reviews_api.py`, `tests/probe_reviews_req_shape.py`)
показали, что на **анонимной** сессии:

- `layout_page_index` без токенов страницы игнорируется (страницы 1–4 отдают одну
  и ту же четвёрку отзывов — те же uuid, что и в DOM);
- `nextPage`/`pageToken` пусты, курсора пагинации нет;
- анонимный профиль получает 4 отзыва суммарно — столько же, сколько отдаёт
  DOM-скролл: срез **серверный**, транспорт его не обходит;
- настоящая пагинация несёт `sh=` и `start_page_id=` (32-hex) внутри вложенного
  `url=`, а токены живут только в конкретной загрузке страницы.

Значит «12–15 с на товар → 1 с» — не доказанный факт, а гипотеза для
авторизованной сессии. Поэтому подключение сделано **self-validating и
opt-in** (`--reviews-api` / `OZON_REVIEWS_API=1`, дефолт — выключено):

1. **DOM — источник истины.** `merge_reviews_api` НИКОГДА не перезаписывает
   отзыв, уже полученный из DOM, и мержит по `uuid` (счётчики `api_added` /
   `api_dupes` / `api_dropped` — видно расхождение схем сразу).
2. **Отказоустойчивость по умолчанию.** Любая ошибка транспорта, неизвестная
   форма поля или отсутствие ответа → предсрез молча выключается, товар
   собирается как раньше. `normalize_review` отбрасывает запись, если не нашёл
   ни `uuid`, ни текст, ни оценку: мусор в `raw` хуже отсутствия отзыва.
3. **Короткое замыкание DOM только при доказанном покрытии.**
   `api_slice_complete` истинно, когда API вернул не меньше доверенного
   `product.rating_count`; тогда глубина DOM ограничивается одной страницей
   (в raw попадает `dom_short_circuit: true`). Мульти-сорт (`--sorts`) и
   вариант-фильтр (`--variant`) этот путь отключают — API не умеет ни то, ни
   другое (нативный фильтр «Этот вариант» требует перезагрузки страницы).
4. **Пагинация только по `nextPage`.** Синтетический `layout_page_index` без
   токенов, по замерам, просто повторяет первую страницу — такой «показун» лишь
   тратит время.
5. **Телеметрия в raw:** `raw["reviews_api"] = {enabled, used, reason, fetched,
   pages, complete, elapsed_ms, api_added, api_dupes, dom_short_circuit}` —
   по готовому raw видно, пробовали ли API и помог ли он, без догадок.

Итог: API-путь даёт выигрыш там, где сервер реально отдаёт пагинацию
(авторизованная сессия, `nextPage` непустой), и остаётся бесплатным no-op там,
где её нет. Побочный эффект метрики — дешёвый способ проверить гипотезу «сколько
реально видно анонимному профилю» без минутного скролла.

---

## 17. Связанные файлы

| Файл | Роль |
|------|------|
| `scripts/launch_chrome.ps1` / `.sh` | Запуск живого Chrome, профиль, маркер ownership, фокус |
| `ozon_collector/cdp.py` | Session, CDP, new_page, watchdog, INVALID-guard, browser_tabs/collector_tabs |
| `ozon_collector/browser.py` | Классификация ошибок, failure_type, watchdog |
| `ozon_collector/collect.py` | Детект/сброс челленджа, INVALID-пометка |
| `ozon_collector/gather.py` | Acceptance-gate (K4), failure_type в res |
| `ozon_collector/search.py` | FSM-лестница, session budget, canary (выбор цели по классу сущности), stability, observed_scope |
| `ozon_collector/parallel.py` | Воркеры: профили/порты, `_worker_task` (canary_url), per-worker KPI |
| `ozon_collector/ozon_fetch.py` | Отзывы через entrypoint-api: `fetch_slice`, `normalize_review`, `merge_reviews_api` (opt-in) |
| `ozon_collector/puzzle_capture.py` | Кадры пазла: flat-frame детект + `bring_to_front` перед reshoot |
| `ozon_collector/process.py` | Identity, kill/restart, профили |
| `scripts/key_daemon.py` | Супервизор: adaptive cooldown, profile cooldown, NEEDS_ATTENTION |

---

## 18. Статусы, legacy и backlog

### Статус докрутки выдачи (ревизия 2.5)

`_collect_links_with_recovery` **приведена к той же FSM-лестнице**, что и сбор
товаров: `access_challenge → new_tab×1 → recycle×1 → raise (SESSION_BLOCKED)`.
Константа `NEW_TAB_ATTEMPTS=2` удалена; `INCIDENT_NEW_TAB_MAX/INCIDENT_RECYCLE_MAX`
общие с `_collect_shard`. Это закрывает старое несоответствие ADR-лестнице.
(Реализовано в ревизии по фидбеку; см. архив журнала
`docs/archive/PROJECT_NOTES_2026-10-06.md`.)

**Докрутка и session budget (ревизия 2.7):** докрутка выдачи НЕ расходует
`session_used{challenge,new_tab,browser_recycle}` — она использует собственные
счётчики инцидента (`incident_new_tab`/`incident_recycle`) и пишет только в
`recovery.new_tab_attempts`/`recovery.recycles`, но НЕ в бюджет сбора товаров.
То есть скан выдачи не «съедает» бюджет, зарезервированный на карточки.
Единственное общее поле — `wall_clock_s`: это таймер ВСЕГО run, и `started_ts`
включает время прогрева + скана выдачи, поэтому при очень долгой докрутке
таймер может подойти к концу ещё до первого товара — очевидный, но осознанный
компромисс (таймер на разрыв зависших сессий).

### Legacy

1. **`--challenge-pause`** остаётся в run_cfg как **deprecated** (не влияет на
   storm-реакцию, которая теперь adaptive). Не удалён, чтобы не ломать
   существующие манифесты; при желании маппится на минимальную ступень.
2. **pre-existing pytest hang** при совместном прогоне файлов с playwright
   browser-fixture (key_daemon+search/browser) — артефакт teardown браузера,
   не связан с архитектурой; по отдельности всё зелёное.
3. **`wall_clock_s=1800`** — жёсткий таймер одиночной сессии. Для длинных
   одиночных прогонов (`--limit > 100`) теперь **параметризуется** через
   `--session-budget-wall-clock` в CLI (маппится на `session_budget.wall_clock_s`,
   работает и для одиночного `search`, и для воркеров `--parallel`).
   Дефолт остаётся в `SESSION_BUDGET_DEFAULT` (см. §9).

### Backlog (по приоритету)

4. **[P1] ~~Canary на лёгкий товар/категорию~~ — ЗАКРЫТО (ревизия 2.8).**
   Canary проверяет сущность того же класса: `--canary-url` / `OZON_CANARY_URL`
   → URL кампании → главная; класс и результат пишутся в телеметрию
   (`recovery.canary`, `kpi.canary_fails`/`ip_blocks`). См. §9.
5. **[P2] Полный Fingerprint-probe** (`scripts/fingerprint_probe.py`) — diff
   manual vs daemon по WebGL/cookies/глубоким Client-Hints. Базовый снимок
   (UA/viewport/timezone/webdriver, §3) уже собирается в телеметрию и покрывает
   основные гипотезы; полный probe закрывает оставшиеся (CDP-аномалии §5).
6. **[P2] KPI обхода — расширение.** Базовый KPI уже реализован в
   `status.json`/`combined` (ревизия 2.7):
   `total_challenges`, `total_recoveries`, `success_rate`,
   `products_since_recovery` (`recovery_telemetry.kpi`). Осталось P2:
   `block_rate` (доля run без SESSION_BLOCKED), медианный
   `products_since_recovery`, распределение `observed_scope`, soft-сигнал
   «хронический partial storm» (§11). Телеметрия собирает всё необходимое;
   отсутствуют именно агрегации.
7. **[P3] Пул профилей с прогревом / ротацией.** Сейчас перегретый профиль
   уходит в cooldown, и воркер ждёт. Пул позволил бы воркеру мгновенно взять
   свободный профиль и не простаивать. Требует пересмотра ownership и
   жизненного цикла профилей — отдельная большая работа, НЕ включается слепо
   (профиль без истории с минимальным Trust Score провоцирует первичный
   challenge, см. §1).
8. **[P3] `--headless=new` как эксперимент** (см. §16) — требует живого
   A/B-замера fingerprint и рендера, а не только документа.
9. **[P3] Рандомизация точки входа** (L3). Частично закрыто в 2.9: джиттер
   пауз прогрева, активный прогрев, easing/overshoot/дрейф в `puzzle_input`.
   Не сделано: разнообразие цепочки входа — `warmup_entry` пока всегда
   `home_category`. Варианты (поиск/категория/товар из главной) остаются
   `[HYPOTHESIS]`: монетизируются только живым замером, и подделывать
   `Referer`/историю переходов нельзя без доказанного эффекта.
10. **[P2] Replay-датасет траекторий.** Сравнить «какая траектория
    бот-дружелюбнее» нельзя без живых A/B. До их появления любая
    «улучшенная» траектория — `[HYPOTHESIS]`, а не `[FACT]`.
11. **[P2] Окружение видимости.** `document.hidden` / `visibilityState` /
    `hasFocus` / `outerWidth`×`outerHeight` теперь пишутся в
    fingerprint-телеметрию (`cdp._capture_fingerprint_once`), но вкладка **не**
    переключается в foreground. Виртуальный рабочий стол (Windows) и Xvfb
    (Linux, `DISPLAY` в `launch_chrome.sh`) — задокументированные варианты,
    эффект не измерен. Внимание: `--window-mode hidden` стартует браузер
    скрытым и может ухудшить ситуацию — тогда как `OZON_KEEP_WINDOW=1` после
    запуска показывает окно (см. §5, ограничения ревизии 2.9).
12. **[P2] Per-worker proxy.** Capability готов (`net.proxy_url_for_worker`,
    `OZON_PROXY_URL`/`OZON_PROXY_URLS`, masked `worker_spec.proxy`), **по
    умолчанию выключен**. Включать только после A/B `parallel=1 без прокси`
    vs `parallel=2 с прокси` (метрики: challenge rate, discovery/run).
    Секреты не публикуются (`scheme://user:***@host:port`). Подводные камни:
    Chromium не применяет логин/пароль из URL для socks5 (нужен PAC/
    расширение), DNS резолвится на стороне Chrome для части схем, fingerprint
    не меняется при смене IP (частичный mismatch).
13. **[P2] Различение «пустой категории» и «не отрисовалось».** `EMPTY_SEARCH`
    закрывает молчание, но не различает «категория реально пуста» и «страница
    не загрузилась». Нужен признак по сетке/маркерам (сейчас —
    `search_grid_empty` + текст лога).

### Проверка гипотез ревизии 2.9 (НЕ сделано)

Каждый новый механизм снабжён телеметрией, но **ни один не измерен в живом
A/B** (запрещено в текущем контексте). Что и как проверять:

| Механизм | Телеметрия | Метрика успеха |
|---|---|---|
| Page Visibility / window-mode | `fingerprint.hidden/visibility/focused` | challenge rate: visible vs hidden (N≥20 прогонов) |
| Активный прогрев + джиттер | `recovery.warmup_activity/warmup_challenges` | challenge rate прогрева, время до первого товара |
| Overshoot-дрейф | пазл-метрики (решено/время) | доля решённых пазлов, время решения |
| Per-worker proxy | `worker_spec.proxy` (masked) | challenge/IP-block rate, discovery на run |
| soft vs hard эскалация | `aborted.block_kind`, `storm_streak` | ложные карантины: soft-серия → реальный бан? |
| `EMPTY_SEARCH` | `recovery.search_grid_empty` | доля ложных «нулевых» прогонов |

### Параллелизм (ревизия 2.5)

- **FSM-лестница и session budget — per browser session** (у каждого воркера
  свой).
- **`challenge_control` в демоне — per campaign**, но с агрегацией по
  `all_workers` (§11): только ФУЛЛ-шторм эскалирует `storm_streak`/
  `profile_cooldown`. Частичный шторм — короткая пауза без эскалации, чтобы
  один воркер не сжигал cooldown всей кампании. Реализовано в коде и тестах
  (ADR §14.6).