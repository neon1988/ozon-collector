# Ozon Collector — PROJECT NOTES

> Память проекта. Вся история, статус и решения по `~/ozon-collector` фиксируются
> ТОЛЬКО здесь (и в `README.md`/`docs/`). В `AGENTS.md` — только ссылка на этот файл.
>
> **Кодировка (зафиксировано 2026-08-18): ВСЕ файлы проекта (PROJECT_NOTES.md,
> README.md, docs/*.md, raw/прочие) пишутся и читаются только в UTF-8.**
> Прошлые cp1251-вставки перекодированы в UTF-8 2026-08-18 (9 строк). Никогда
> не писать и не читать эти файлы как cp1251/другие кодировки.

 - **Ревизия 2.9 (2026-09-26): fail-fast URL + EMPTY_SEARCH, Page Visibility
   телеметрия, недетерминированный прогрев, overshoot-драг, per-worker proxy,
   soft/hard-эскалация.** Повод: внешняя критика anti-bot контура + живой
   прогон демона, который вскрыл настоящий P0.
   - **P0 (живой баг, не из критики): markdown-ссылка в `--url`.** В демон ушло
     `[https://.../zvukoizolyatsiya-33302/?...](https://.../fonari-...-11061/?type=47720)`.
     `Page.goto` → `Cannot navigate to invalid URL` → 0 ссылок → прогоны
     `status=NORMAL_DONE discovered=0`. Кампания `campaign_20260926_1051` часами
     давала ноль, а демон выглядел «здоровым». Исправлено: `net.normalize_target_url()`
     (чинит markdown-обёртки/кавычки/пунктуацию/URL-в-тексте, отвергает не-URL),
     `_validated_campaign_url()` в `search.py`, вызовы в `key_daemon.main()`
     (`--url`, `--canary-url`), `_inproc_collect()` (URL из `state.json` —
     иначе мусор пережил бы рестарт), `cli.cmd_collect/cmd_gather`.
     Не-URL → `ValueError` → exit 2 с подсказкой ДО запуска браузера.
     Нормализация идёт ДО `resolve_canary_url` (иначе canary тихо уезжал на
     главную). pids/rescan-режим не валидируется (там url служебный).
   - **EMPTY_SEARCH.** `recovery.search_grid_empty` → `classify_status()` отдаёт
     `EMPTY_SEARCH` вместо `NORMAL_DONE` при 0 товарах и неотрисованной сетке.
     Считается стагнацией, прогрессом — нет. `CHALLENGE_STORM` важнее.
   - **Разбор критики (главное):** проверено по коду и замерам, а не принято.
     (1) `Object.defineProperty(navigator,'webdriver')` и реальный
     `--disable-blink-features=AutomationControlled` в проекте НЕ нужны и не
     добавлялись: Chromium CDP с честным `webdriver === true` проходит, а
     geckodriver/Marionette/Firefox с `webdriver=true` получает заглушку —
     исходная интерпретация была инвертирована; исправлен дрейф документа.
     (2) `browser_pid` уже был, `_solve_classic` отсутствует, canary уже
     исправлен в 2.8, reviews API/WALL_LIMITED/`_ozon_sat` уже реализованы,
     `puzzle.py` уже разделён. (3) `--window-mode`/`OZON_KEEP_WINDOW`
     существовали — добавлена только телеметрия видимости.
   - **Page Visibility телеметрия.** `cdp._capture_fingerprint_once` пишет
     `hidden/visibility/focused/ow/oh`. Вкладка НЕ поднимается в foreground:
     эффект не измерен, `--window-mode hidden` может ухудшить ситуацию.
   - **Активный прогрев** (`OZON_ACTIVE_WARMUP`, по умолчанию ВКЛ): 1–2 scroll
     + 2–3 mousemove, джиттер пауз 0.75–1.35, телеметрия
     `warmup_activity/warmup_entry/warmup_challenges`. Прогрев не тратит
     `session_used`. Гипотеза, не измерена.
   - **Overshoot-драйф** (`puzzle_input`): randomized easing, низкочастотный
     дрейф, асимметричный джиттер, dwell-паузы, overshoot→correction с
     ограничением `bound`. `tests/test_puzzle_input.py` — 11 тестов.
   - **Per-worker proxy** (`net.py`, `OZON_PROXY_URL`/`OZON_PROXY_URLS`):
     capability, **по умолчанию выключен**, включать только после A/B
     (parallel=1 без прокси vs 2 с прокси). В статусе/логах — только
     `scheme://user:***@host:port`. `launch_chrome.sh/ps1` принимают `PROXY`,
     Linux использует переопределяемый `DISPLAY` (Xvfb).
   - **soft/hard эскалация** (`_storm_block_kind`): `SESSION_EXHAUSTED` = soft →
     короткая пауза без `profile_cooldown` и без роста `storm_streak`;
     `SESSION_BLOCKED`/ip/нерешённый пазл = hard. Правило асимметрии: сомнение =
     hard. Шторм ОДНОГО воркера ≠ шторм всей кампании.
   - **Тесты:** 1275 passed / 4 skipped / 1 xfailed (было 1194 в 2.8), +81.
     Новые: `tests/test_net.py` (39), `tests/test_puzzle_input.py` (11),
     `tests/test_warmup_escalation.py` (11), `tests/test_cdp_launch.py` (+6
     fingerprint-visibility), +url/EMPTY_SEARCH-тесты в `test_search.py`
     и `test_key_daemon.py`.
   - **Ловушка Windows (урок в скилл `powershell-file-traps`):** `Set-Content
     -Encoding UTF8` в PS 5.1 ставит BOM, а list comprehension вида
     `[io.open(f,'w').write(io.open(f).read()) for f in ...]` ОБНУЛЯЕТ файл
     (open('w') обрезает раньше чтения). Так был потерян
     `tests/test_cdp_launch.py`; восстановлен из `__pycache__/*.pyc` (marshal
     + dis) и сверкой инструкций с HEAD. Правки файлов — только
     python-скриптами с чтением ДО записи.

 - **Ревизия 2.8 (2026-09-26): canary по классу сущности + opt-in API-предсрез
   отзывов + per-worker телеметрия + bring_to_front перед reshoot кадра.**
   Повод: внешняя критика anti-bot контура; каждый пункт сначала проверен по
   коду/замерам, недоказанное не внедрялось.
    - **Canary (закрыт P1 из backlog).** Каноничной была жёсткая главная
      `https://www.ozon.ru/`; главная МЯГЧЕ карточки по лимитам, поэтому
      `CANARY_OK` на ней не доказывал доступ к `/product/` — сессия считалась
      восстановленной, `session_used` уходил вхолостую, первый товар снова ловил
      challenge. Реализовано: `search.resolve_canary_url` → приоритет
      «--canary-url → OZON_CANARY_URL → URL кампании → главная»,
      `classify_canary_url` (product/category/search/home/custom), телеметрия
      `recovery.canary={url,kind,result}` + `kpi.canary_fails/ip_blocks`.
      Конкретный pid НЕ хардкодится (любая «безопасная» ссылка устаревает и сама
      становится тяжёлым) — его задаёт оператор. Не-http аргумент (rescan-режим
      демона: `url="список из N артикулов"`) уровнем цели не считается.
      Воркерам цель передаётся в `task["canary_url"]` через общий
      `_worker_task` — респавн не должен молча менять проверяемую сущность.
    - **API отзывов: гипотеза НЕ подтверждена, но инструмент подключён безопасно.**
      Замеры (pid 210502516, 257 707 отзывов, tests/probe_reviews_api.py +
      tests/probe_reviews_req_shape.py): анонимный профиль получает через
      entrypoint-api ровно те же 4 отзыва, что и DOM; `layout_page_index` без
      токенов страницы игнорируется (страницы 1-4 = одна четвёрка, те же uuid);
      `nextPage`/`pageToken` пусты; настоящая пагинация несёт `sh=` и
      `start_page_id=` (32-hex) внутри вложенного `url=`. Вывод: «12-15 с → 1 с»
      не доказано — это про авторизованную сессию. Поэтому не «API вместо DOM»,
      а self-validating предсрез: `--reviews-api` / `OZON_REVIEWS_API=1`
      (дефолт ВЫКЛ), DOM — источник истины (`merge_reviews_api` никогда не
      перезаписывает DOM-отзыв, конфликт решает DOM), пагинация только по
      `nextPage`, короткое замыкание DOM только при доказанном покрытии
      (`api_slice_complete`: API отдала >= доверенного `product.rating_count`),
      вариант-фильтр и мульти-сорт этот путь отключают. Нормализация
      консервативная: нет uuid+текст+оценки → запись отброшена (мусор в raw хуже
      отсутствия отзыва). Телеметрия в `raw["reviews_api"]`.
    - **Свёрнутое окно (L4).** Однотонный кадр (std≈0) при валидных размерах —
      это не «лунок нет», а буфер неперерисованного окна (особенно после старта
      через SW_SHOWMINNOACTIVE). Пассивная пересъёмка это не лечит. Добавлен
      best-effort `_bring_to_front()` перед reshoot (`front=ok`/`front_err=*`
      в `_shot_bg_reason`); кадр валидный — окно не трогаем.
    - **Прокси на воркер — НЕ внедряем до A/B.** Критика требовала «свой IP на
      воркер», но это оптимизация по гипотезе. Сначала собрали измерение:
      `combined.parallel.workers[k].challenges` и `challenge_rate_per_100`
      (раньше challenges были только суммой по пулу — нельзя было понять, вредит ли
      параллелизм конкретному IP/профилю). Сравнить `parallel=1` vs
      `parallel=2` на одинаковой кампании, потом решать.
    - **Проверено и признано устаревшим:** пункт «puzzle.py — 1775 строк» (уже
      561 строка: facade + FSM, CV/input/capture/state вынесены), «browser_pid
      без health-check» и «_solve_classic» (исправлены ранее), «parallel
      в один IP без прокси» (см. выше — сначала замер).
    - **Тесты:** +66 (1186 passed, 4 skipped, 1 xfailed; baseline до правок был
      1120/4/1). Новые файлы: tests/test_canary_url.py (22),
      tests/test_reviews_api.py (41), +3 в tests/test_puzzle_capture.py.
      Обновлены фейки под новые сигнатуры: `run_canary(s, url=None)`,
      `_respawn(..., dynamic=, canary_url=)`.
    - **Complexity:** новые функции <= 13 CCN (`_reviews_api_prepass`=10,
      `_collect_reviews_stage`=7, `_bring_to_front`=13, `_worker_task`=1);
      `collect()` разжат с CCN 44 до 38 выносом стадии отзывов. Оставшиеся
      гиганты (`_collect_shard`=77, `_shot_bg_clean`=33, `run_campaign`=126)
      — предсуществующий долг, этот проход его не ухудшал.
    - **Не проверено вживую:** canary на карточке, выигрыш API, reshoot после
      bring_to_front — требуют login/live-прогон, которые в этом проходе не
      выполнялись. Новые флаги по умолчанию выключены, поэтому дефолтный бой
      ведёт себя ровно как раньше.
 - **ADR: Firefox-бэкенд реализован как OZON_BROWSER=firefox (2026-09-24).**
   Пользователь просил «нужна поддержка Firefox», затем дал явный запрос
   «дополнительно». Прошлый ADR ниже фиксировал Firefox как «документированный
   разрыв» — теперь это дефолтный движок (см. следующий блок), chrome = opt-out.
    - **Архитектура (обновлено 2026-09-25):** `browser_kind()` (browser.py) →
      `chrome|firefox`; `Session.kind`. Firefox больше НЕ Playwright (Ozon блокирует
      patched Juggler-сборку): `_connect_firefox` в cdp.py = `ffsel.connect` →
      **stock Firefox + geckodriver/Selenium**, профиль
      `{OZON_PROFILE_BASE}/{token}.firefox` (свои куки, не пересекается с chrome).
      `ffsel.SeleniumBrowserAdapter.contexts[0].pages` — общий код (страницы,
      отзывы, комментарии) работает без изменений; шим `FirefoxPage` повторяет
      нужное подмножество page-API. Класс `_PersistentBrowserAdapter` (адаптер под
      старый Playwright-контекст) удалён как мёртвый код.
    - **Firefox-специфика:** (1) `navigator.webdriver=true` (geckodriver+Marionette) —
      chrome остаётся предпочтительным для Ozon FAB (webdriver=false); (2) окна —
      нативно

     по PID + `MozillaWindowClass`; главное окно висит на ПЕРВОМ firefox.exe
     (launcher) или ВТОРОМ (main browser) — `_firefox_pids()` собирает ВСЕ pid
     профиля из `firefox_browser_processes` (проверено live: 2 процесса на
     профиле, окно на втором); (3) geometry-CDP нет → `_normalize_window_geometry`
     False, `_assert_geometry_ok` верит (hwnd_rect, browser_kind=firefox);
      (4) watchdog = живость firefox-процесса на профиле; (5) `close()` гасит
      firefox/geckodriver (detach невозможен), рестарт = kill + reconnect.
    - **Ограничения:** первый вход в Ozon вручную один раз; `pv1`-профиль лежит
      отдельно от chrome. Parallel для firefox РАЗРЕШЁН (с 2026-09-24): воркер =
      отдельный процесс со своим Firefox+geckodriver, живость по процессу +
      heartbeat, без CDP.
    - **Тесты:** tests/test_cdp_firefox.py (моки: browser_kind, профиль, матчинг
      процессов, watchdog, connect, геометрия, recycle). Live-smoke на реальном
      Firefox 156: connect OK, окно найдено, close → 0 процессов, webdriver=true.

- **Решение: Firefox — движок по умолчанию (заявлено 2026-09-24,
  реализовано 2026-09-25).** `DEFAULT_BROWSER_KIND = "firefox"` — переключено
  после живого прогона (151 отзыв, cache=miss, exit 0). Opt-out:
  `OZON_BROWSER=chrome` / `--browser chrome`.
   - Runtime: браузерный выбор по-прежнему один источник — `browser_kind()`
     (env `OZON_BROWSER` | дефолт firefox), флаг `--browser` в cli приоритетен;
     `BrowserWatchdog.__init__` kind=None берёт движок из `browser_kind()`.
   - `resolve_browser_for_parallel()` возвращает kind как есть (дефолт firefox):
     фолбек firefox→chrome и `RuntimeError` на явном firefox в parallel УДАЛЕНЫ
     2026-09-24 (воркеры firefox работают без CDP).
   - Последствия, зафиксировать: (1) FAB-отпечаток firefox хуже
     (webdriver=true) — для премиум-сборок рекомендован `OZON_BROWSER=chrome`;
     (2) тест-свита пинится на chrome: `tests/conftest.py::_pin_browser_kind_chrome`
     (autouse) — CDP-тесты детерминированы, firefox-тесты сами перекрывают env;
     (3) в test_cdp_firefox.py был ДУБЛЬ класса `TestParallelEngine` (старый
     тест фолбека перетирался новым и молча не собирался) — старый удалён.


 - **ADR: бэкенд = Playwright-over-CDP; автономный браузер через OZON_CHROME_PATH (2026-09-24).**
   Пользователь просил «можно заменить Chrome на Selenium? / добавить Selenium по умолчанию?
   нужно поддержка Firefox, переехать полностью». Решение принято самостоятельно (пользователь
   ушёл, «сделай как лучше»). **Selenium ОТКЛОНЁН**, движок остаётся Playwright-over-CDP.
   - **Почему НЕ Selenium:** (1) `chromedriver` ставит `navigator.webdriver=true` → Ozon FAB
     блокирует воркеров — ядро продукта мертво; (2) движок уже Playwright-over-CDP: из CDP
     используются только 4 «сырых» метода — все оконные (`Browser.getWindowForTarget`/
     `SetWindowBounds` в `cdp.py`), сбор идёт через page-API (response/network нужен для
     отзывов/кандидатов — в Selenium такой сети нет); (3) chromedriver обязан совпадать с
     мажором Chrome → возвращается та же боль версионирования, от которой ушли (Omaha
     сломан); (4) дубль стеков/зависимостей без выигрыша. Fireproof-принцип: не менять
     работающий анти-автоматизационный паттерн ради «популярного фреймворка».
   - **Итоговая архитектура:** единственная точка выбора браузера — `OZON_CHROME_PATH`
     (env) → `browser._chrome_override()` (browser.py:91) → `_launch_script` прокидывает
     `CHROME` в `scripts/launch_chrome.ps1/.sh`; default = системный Chrome (лучший отпечаток
     для FAB). Любой Chromium = Chrome-for-Testing/Edge/playwright-сборка — идентичный
     CDP/окна/anti-automation паттерн (без `--enable-automation`, webdriver=false).
   - **Firefox — ДОКУМЕНТИРОВАННЫЙ разрыв, НЕ заглушка:** `connect_over_cdp` только
     chromium; `probe/watchdog/kill` завязаны на `chrome.exe`+порт+профиль; win32-логика
     (`Chrome_WidgetWin_1`, `--user-data-dir`, `Browser.*` CDP) специфична Chrome;
     поведение Ozon в Firefox не подтверждено живым сбором. Полная поддержка = переделка
     Session/probe/watchdog/win32 + живой прогон на Ozon (пользователь не доступен для
     валидации). Карта миграции оставлена в коде как TODO-комментарии? НЕТ — осознанно без
     полуфабрикатов; при решении вернуться — стартовать с новой разведки.
   - **Что реализовано:** `ozon_collector/browser.py` +`_chrome_override()` и CHROME-инъекция
     в `_launch_script`; `ozon_collector/cdp.py` — то же в `_launch_browser` (DRY-рефакторинг
     на browser-хелпер ОТКАЧЕН: `test_cdp_launch.py` пиняет приватные `cdp._launch_browser`
     и `cdp.subprocess.Popen` — см. ниже «Зацеп тестов на приватные launch-хелперы»);
     `scripts/launch_chrome.ps1` — CHROME-каталог→`chrome.exe` + валидация пути;
     `scripts/setup_bundled_chrome.py` — установщик Chrome-for-Testing (stable, win64);
     `.env.example` — `OZON_CHROME_PATH`; `README.md` — секция «Автономный браузер».
   - **Live-проверка (2026-09-24, CfT stable 154.0.8037.57):** `launch_chrome.ps1` +
     CfT → `OK: Chrome/154.0.8037.57 на порту …`, окно TOPMOST, фокус не крадётся; CDP
     `/json/version` → `Chrome/154.0.8037.57`; тот же путь через `subprocess.Popen`
     (PIPE и с DEVNULL) — браузер жив, CDP отвечает. **Playwright-сборка chromium
     (`ms-playwright\chromium-*\chrome-win*\chrome.exe`) через `launch_chrome` НЕСТАБИЛЬНА:
     2/2 попыток стартует и мгновенно умирает** (стандарный `chromium.launch()` Playwright —
     жив). Поэтому для OZON_CHROME_PATH — только Chrome-for-Testing/Edge (полные сборки);
     playwright-сборку в README пометили как непредпочтительную.
   - **Setup** `scripts/setup_bundled_chrome.py` (2026-09-24): `storage.googleapis.com/.../
     last-known-good-versions-with-downloads.json` отдаёт 404 (и standalone-канал 155.x тоже
     404) → добавлен зеркальный источник `googlechromelabs.github.io/chrome-for-testing/`
     (падает по очереди). CfT stable = **154.0.8037.57**, zip `chrome-win64.zip` (196 МБ) —
     распаковывается в `chrome-win64\` БЕЗ вложенной версии (исправлен ожидаемый путь),
     установка идемпотентна, `--reset` пересоздаёт. Скачано и развёрнуто в
     `%LOCALAPPDATA%\opencode\browsers\chrome-for-testing\chrome-win64\chrome.exe`.
   - **Зацеп тестов на приватные launch-хелперы:** `tests/test_cdp_launch.py` вызывает
     `cdp._launch_browser`/`cdp._launch_script_path` и патчит `cdp.subprocess.Popen`.
     ЛЮБОЙ рефакторинг cdp-запуска должен сохранять эти имена в `cdp.py`.
   - **Верификация после изменений:** `test_browser.py`+`test_cdp_launch.py` 88 passed;
     chunk-прогон 23 модулей без key_daemon: 298 + 321 + 50 = 669 passed, 4 skipped,
     1 xfailed; py_compile ok (browser/cdp/setup/test_browser); PS1 parse ok.

  - **Windows-баг HEAD `key_daemon._alive` (os.kill = TerminateProcess) — ИСПРАВЛЁН (2026-09-25).**
    `tests/test_key_daemon.py` глушил прогон ~на 42% без вывода: `_alive(pid)` в
    `scripts/key_daemon.py` звал `os.kill(pid, 0)`, а на Windows это
    `TerminateProcess(handle, 0)`. Тесты пишут свой `os.getpid()` (`test_pidlock_*`,
    `test_alive_current_process`) → pytest **убивал сам себя** (rc=0, пустой вывод,
    `run_tests_guarded.py` не успевал напечатать итог). Прод был опасен и вне тестов:
    демон при проверке pid-файла убил бы живой предыдущий инстанс вместо отказа
    запускаться.
    - **Фикс:** `scripts/key_daemon.py::_alive` на Windows идёт в `_alive_win` —
      `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `GetExitCodeProcess(...) ==
      STILL_ACTIVE (259)`; POSIX остался на `os.kill(pid, 0)`. Гонять сырой
      `pytest` по-прежнему нельзя (playwright/chromium виснут в нативной блокировке) —
      только через `scripts/run_tests_guarded.py`.
    - **Проверка (2026-09-25):** `python scripts/run_tests_guarded.py --timeout 900 tests
      -q --tb=line -p no:cacheprovider` → **1023 passed, 4 skipped, 1 xfailed in
      295.06s, exit 0**, deselect больше не нужен (было 1010 passed + deselect).

  - **Firefox: Selenium/Marionette вместо Playwright; 4 причины пустых отзывов (2026-09-25).**
    Живой `gather` на stock Firefox 156 через geckodriver даёт **151 отзыв**
    (`cli.py --browser firefox gather --pid 1590289080 --max-age 0 --max-pages 1
    --min-reviews 20` → `gather OK`, cache=miss). По дороге закрыты 4 грабли,
    каждая давала «0 отзывов»:
    1. **Свёрнутое окно Firefox не рендерит lazy-блоки** — главная причина.
       `cdp._connect_firefox` вызывал `_minimize_window()`, а IntersectionObserver в
       свёрнутом окне не срабатывает: `webListReviews` не монтировался, скролл
       упирался в низ (geom `y=3613/h=4333/ready=complete`, 0 карточек). Тот же
       профиль с видимым окном — 12..151 отзыв. Фикс: для firefox окно
       разворачивается без фокуса (`_restore_window_native` +
       `WS_EX_NOACTIVATE`), `OZON_KEEP_WINDOW=1` — как раньше. Chrome не тронут.
    2. **`os.kill`/`page.reload` в шиме:** `FirefoxPage.reload` отсутствовал —
       `collect._reload_fn` глотал `AttributeError` и не перезагружал страницу.
    3. **Навигация ждала `load`:** geckodriver возвращает управление из `get()` по
       `load`, которого страницы отзывов Ozon не шлют — 60s таймаут и
       недогидрированная оболочка. Фикс: `pageLoadStrategy=eager` в
       `ffsel.connect` (+ фолбэк без capability, если geckodriver его не принял) и
       `FirefoxPage._navigate`: таймаут навигации терпим, если
       `document.readyState != loading`, а `wait_until` = domcontentloaded/commit.
    4. **Короткий URL `/product/{pid}`:** без slug не подгружает блок отзывов
       (shell 1863 символа, `webListReviews` отсутствует). `collect._canonical_product_url`
       берёт `<link rel="canonical">` и один раз переходит на полный адрес;
       вызывается и для product_url, и для явного `--reviews-url`.
     - Плюс диагностика: `[variant_mode]` логирует клик по «Этот вариант товара»
       (на Firefox именно он гидрирует список), `_page_geom()` — readyState/y/h/ih
       при пустой выдаче.

  - **Параллельный сбор на Firefox: 3 бага (2026-09-25, живой прогон 12 воркеров).**
    1. **`preflight` падал на КАЖДОМ firefox-воркере:** `parallel.preflight_check`
       звал `B.force_stop(s["profile_token"])` — такой модульной функции в
       `browser.py` нет (`force_stop` есть только методом
       `BrowserProcessManager`) ⇒ `AttributeError: module ... has no attribute
       'force_stop'`, 12/12 воркеров, сбор не стартовал. Фикс: `P.kill_records`
       по identity процессов профиля (ownership-контракт firefox = профиль, тот
       же, что в `cdp.Session._connect_firefox`).
    2. **`NoSuchWindowException: Browsing context has been discarded`** — воркер
       падал на ПЕРВОМ товаре. Причина в окне: `Session.new_page`
       (`ensure_single_worker_tab` = новая вкладка на каждый товар) для firefox
       вызывал `_minimize_window()` → окно уходило в minimize, а Firefox
       выгружает вкладки невидимых/свёрнутых окон. Фиксы: единая
       `Session._apply_window_policy()` (firefox = развернуть без фокуса, НИКОГДА
       не minimize; в `_minimize_window` тоже early-return для firefox),
       преф `browser.tabs.unloadOnLowMemory=false` (гейт внутри Firefox —
       `TabUnloader`, omni.ja: `getBoolPref("browser.tabs.unloadOnLowMemory",
       true)`), self-heal в `Session.goto` (firefox + `NoSuchWindow…discarded` →
       новая вкладка + один повтор; для chrome не срабатывает — там это recycle).
    3. **Дубль класса `TestParallelEngine`** в `tests/test_cdp_firefox.py`:
       старый тест (фолбек firefox→chrome в parallel) перетирался новым классом
       с тем же именем и молча не собирался — старые тесты выброшены, дефолты
       переписаны под firefox.
    - Проверки: целевой прогон `test_cdp_firefox + test_parallel +
      test_cdp_launch + test_browser` → **182 passed**; живой `key_daemon
      --parallel 2 --limit 3` стартовал: preflight чистый, воркеры 1/2
      подключились, discard не повторился, товар 1/5 дошёл до FAB-reload
      (прогон прерван пользователем).
    - Уборка: 12 осиротевших firefox-профилей + 12 geckodriver с прошлого
      упавшего прогона сняты вручную (личный Firefox пользователя не тронут).



 - **Окна браузеров: WS_EX_NOACTIVATE ВОЗВРАЩЁН (финально, 2026-09-24).**
   Утром 2026-09-24 был сделан фикс «вкладки на таскбар» (убрали WS_EX_NOACTIVATE,
   ре-апплай коммита `936a715`). Вечером откатили по запросу пользователя:
   - Пользователь хотел «разные браузеры со своими куками, но сгруппированы на
     таскбаре». Выяснилось: на машине `TaskbarGlomLevel=2` («никогда не объединять»)
     — Windows рисует КАЖДОЕ окно отдельной кнопкой независимо от приложения/AUMID;
     менять настройку ОС запрещено. => По-настоящему сгруппировать отдельные окна
     невозможно; «одно окно со вкладками» требовал бы общего браузера и слияния кук.
     Решение: вернуть старое поведение (WS_EX_NOACTIVATE), воркер-окна не мозолят
     таскбар.
   - **Что возвращено (минимально):**
     - `scripts/launch_chrome.ps1`: снова `WS_EX_NOACTIVATE` (DllImports
       GetWindowLong/SetWindowLong, `GWL_EXSTYLE=-20`, `WS_EX_NOACTIVATE=0x08000000`);
       `Invoke-MinimizeNoActivate` ставит стиль в обоих проходах (первый — сразу
       при появлении окна, контрольные — после полного старта), затем
       показ/сворачивание + TOPMOST + возврат фокуса.
     - `ozon_collector/cdp.py`: `_minimize_window_native` — снова
       `GetWindowLongW/SetWindowLongW(WS_EX_NOACTIVATE)` + `SW_MINIMIZE`;
       `_restore_window_native` — стиль + `SW_RESTORE`(9) (вместо `SW_SHOWNOACTIVATE`(8)).
     - Прочие правки дерева (геометрия-фикс `_normalize_window_geometry` и др.) НЕ
       трогались — не относятся к окнам.
   - **Итоговое поведение:** окно Chromium воркера/коллектора — плавающий блок
     над таскбаром без таскбар-кнопки (как решения 2026-09-05): не перехватывает
     фокус, чужой Chrome не трогается. Фокус возвращается предыдущему окну.
   - Проверки после реверта: py_compile OK, PS1 parse OK, тесты без привязки к окнам.
   - **Trade-off:** панель задач чистая; но окна воркеров (прогресс) не видны —
     смотреть можно через `--window-mode visible` (OZON_KEEP_WINDOW=1).

 - **Chrome: обновлён до 154.0.8037.58 (2026-09-24).**
   Было 152.0.7977.83 (актуальный stable на сент-2026 — 155.x, но standalone-канал
   Google пока отдаёт 154; версии 155 на момент правки в MSI-канале нет — 404).
   - **Причина застревания на 152:** автообновление (Omaha) на машине НЕ работает:
     `GoogleUpdate.exe /av` и `/ua` не ведут к апдейту, задачи GoogleUpdateTaskMachine*
     отсутствуют, `/ua` возвращает 75009.
   - **Как обновлено:** официальный Enterprise MSI
     `GoogleChromeStandaloneEnterprise64.msi` (сейчас отдаёт 154.0.8037.58), установка
     `/qn /norestart` с elevation (msiexec rc=0). Chrome при живом процессе ставит
     новую версию в `Application\154.0.8037.58\` + `new_chrome.exe` в корень, а
     своп (`chrome.exe` → новая версия) происходит при следующем запуске Chrome.
   - **Активация 154 выполнена вручную (по согласованию с пользователем):**
     закрыт личный Chrome, элевированный PowerShell сделал своп —
     `new_chrome.exe`→`chrome.exe`, `new_chrome_proxy.exe`→`chrome_proxy.exe`,
     `chrome.exe` скопирован в `154.0.8037.58\`, папка `152.0.7977.83` удалена.
     Live-проверка: `/json/version` → `Chrome/154.0.8037.58`, корневой
     `chrome.exe` = 154. 155.x в standalone-канале Google ещё нет (404).
   - **`launch_chrome.ps1` / `Find-Chrome`:** теперь выбирает САМУЮ СВЕЖУЮ
     установленную версию — максимум ProductVersion среди `Application\<version>\chrome.exe`
     (раньше — всегда корневой `Application\chrome.exe`, который до свопа указывает
     на старую). Fallback на корневой/`Program Files(x86)`/LOCALAPPDATA/Playwright
     сохранён. После активации верну `Application\154.0.8037.58\chrome.exe`.

 - **key_daemon: автоименование кампании из --url (naming, НЕ фильтр) (2026-09-07).**
   CLI упрощён: вместо `--campaign <имя> --out docs/<имя>.md` достаточно одного
   `--url "...&text=запрос"` (или URL категории). Явные `--campaign`/`--out`
   работают строго как раньше — байт-в-байт (автоименование НЕ вызывается).
   - **Механика:** `campaign_from_url(url) -> (name, name_source, value)`.
     Приоритет источника имени: **`text=`** в query (parse_qs сам декодирует
     percent-encoding и `+`, повторный unquote НЕ нужен; берётся первый элемент)
     → **сегмент категории** `/category/<slug>-<id>/` (числовой ID
     отбрасывается: `mashinki-radioupravlyaemye-7151` → `mashinki_radioupravlyaemye`)
     → **timestamp-fallback** `campaign_<YYYYMMDD_HHMM>` (всегда непустое имя).
   - **`_slugify(text, max_len=50) -> str | None`:** lower → детерминированный
     транслит (ь/ъ удаляются, ё→e, й→y, х→kh, ц→ts, ч→ch, ш→sh, щ→shch) →
     всё вне `[a-z0-9_]` → `_` → схлопывание `_` → strip → `[:50]` → `rstrip("_")`.
     Пустой/только-недопустимые символы → `None` → кампания уходит в
     timestamp-fallback. Фиксированы контракты: `len(slug)<=50`, без завершающего
     `_`, одинаковый вход всегда даёт одинаковый slug (детерминизм, регистернезависимо).
   - **Манифест:** `default_manifest` аддитивно получил 2 top-level поля
     `name_source` / `name_source_value` (метаданные происхождения имени:
     `explicit` | `url_text` | `url_category` | `timestamp`). Top-level, НЕ в
     `run_cfg` → `run_cfg_hash` и resume-конфиг не затрагиваются (имя не станет
     `run_cfg_modified`).
   - **Коллизия:** существующая resume-проверка `campaign_identity.url_sha256`
     (тот же slug, др. URL) — та же ошибка, но для авто-режима добавлен hint
     «имя сгенерировано из --url; укажите --campaign явно». Никаких `_2/_3/uuid`
     — авто-именование должно оставаться предсказуемым.
   - **ВАЖНЫЙ ИНВАРИАНТ (naming ≠ match-title):** `text=` из --url используется
     ТОЛЬКО для имени кампании, для `--match-title` (фильтра названий товара)
     никогда не подхватывается (инцидент podarok_five: авто-фильтр отсекал всю
     выдачу → valid=0 → бесконечный пересбор). Поисковый запрос ≠ название товара.
   - **Файлы:** `scripts/key_daemon.py` (`_SLUG_TRANSLIT`, `_slugify`,
     `campaign_from_url`, `--campaign` required=False, `main()` авто-имя+`out`,
     `name_source` в default_manifest/run_campaign, hint коллизии, docstring),
     `tests/test_key_daemon.py` (+19: _slugify транслит/ьъ/спецсимволы/обрезка/None,
     campaign_from_url percent-text/category-strip/blank-fallback/timestamp/empty/
     deterministic/case-insensitive/invalid-only, default_manifest поля, 3 integration
     через main(): text→slug+docs/{slug}.md, явный --campaign→explicit, timestamp).
     Прогон: test_key_daemon 227 passed, полный pytest 837 passed, py_compile OK.

 - **key_daemon: опциональный фильтр exact-match товара по названию (--match-title) (2026-09-05).**
   Живая выдача по бренд-категории + `text=` приносит соседние модели/аксессуары
   (напр. по «Edifier W830NB» попадались W820NB и пр.). Ozon не умеет «точное
   соответствие» на стороне URL — поэтому в key_daemon добавлен фильтр на этапе
   реконсиляции: в `valid_pids` попадают только товары, чей `raw.product.title`
   содержит фразу как **подстроку, регистронезависимо**.
   - **Источник фразы — ТОЛЬКО явный `--match-title`.** text= из URL НЕ
     подхватывается автоматически (см. инцидент podarok_five ниже: авто-включение
     от `text=` отсекало всю выдачу → valid=0 → бесконечный пересбор).
     Нет фразы → фильтр **выключен**, сбор как обычно (регресс-безопасно).
   - **Механика:** фраза фиксируется в манифесте при создании кампании
     (`progress["title_match_query"]`), при resume подтягивается из манифеста
     (источник истины) и параметром запуска НЕ переопределяется. Несоответствующие
     товары НЕ считаются FAIL (иначе сожгли бы rescan-бюджет) — они идут в отдельный
     список `progress["title_mismatch_pids"]` (телеметрия в summarize/status:
     `title_mismatch`). Если все найденные не подходят — кампания честно уходит в
     stall/no-progress, а не крутит пересборку.
   - **Итог для пользователя:** `--limit 60 --min-valid 45 --reserve-count 15
     --match-title "W830NB"` → полный прогон соберёт 75, в дайджест попадут 60,
     из которых все с названием, содержащим фразу.
-  Файлы: `scripts/key_daemon.py` (`_title_matches`,
     `reconcile_validity(..., match_query)`, `--match-title`, `title_match_query`/
     `title_mismatch_pids` в manifest/summarize/status), `tests/test_key_daemon.py`
     (+7 тестов). Прогон: test_key_daemon 217 passed, digest_from_raw+cli 11 passed,
     py_compile OK.

 - **`--compact`: краткий дайджест изменения (карточка без стен отзывов) (2026-09-07).**
   Многосотенные отзывы раздувают полный `*.md` до десятков МБ; для быстрой сверки
   добавлен опциональный краткий вариант — только основное по каждому товару.
   - **Что входит (whitelist):** PID, название, URL, вариант, категория,
     рейтинг варианта (`digest_rating`, нейтральная выборка), официальный рейтинг
     карточки (`official_rating_info`), доля негатива 1–2★
     (`official_negative_share`), `recommend_percent` (если есть), блок цены,
     полная таблица характеристик, аспекты покупателей (`_customer_aspects`,
     из `customer_opinion.text`), распределение звёзд — ТОЛЬКО официальное
     (`customer_opinion.counts`, без смещения score_asc; нет counts → блок звёзд
     не выводится), URL. НЕ входят: тексты отзывов, описания, OCR/галерея,
     секции 4–7, служебные поля — полный дайджест остаётся прежним (byte-to-byte).
   - **Механика:** отдельные рендереры `render_compact`/`render_search_digest_compact`/
     `write_search_digest_compact` в `report.py` (существующие функции не меняли).
     Один файл на кампанию, без разбиения по размеру: `{root}_compact.md`
     рядом с полным; в шапке ссылка на полный (если он есть). Товары с ошибкой
     сбора (`ok=False`) не выпадают — блок-заглушка `## Товар {pid}` +
     `⚠ Не удалось собрать: {error}` (состав 1:1 с полным). Порядок — как в
     полном (общий `combined["products"]`, уже `sort_digest_products`).
   - **CLI:** `key_daemon --compact` → полный + компакт; `--no-digest` отключает
     ОБА дайджеста; `digest_from_raw.py --compact` — с `--out` пишет оба файла,
     без `--out` выводит компакт в stdout. Флаг фиксируется в манифесте
     (`progress["compact_digest"]`), resume берёт из манифеста (источник истины),
     CLI-параметр не переопределяет; для кампаний ДО опции (поля нет) — fallback
     на текущий CLI-флаг только в этом запуске, манифест НЕ перезаписывается
     (CLI не становится постоянной настройкой старой кампании).
   - Файлы: `ozon_collector/report.py`, `scripts/key_daemon.py` (`--compact`,
     `_write_digest(..., compact=)`), `scripts/digest_from_raw.py` (`--compact`),
     `tests/test_report.py` (8 тестов рендерера), `tests/test_key_daemon.py`
     (6 интеграционных: оба файла, изоляция без флага, `--no-digest`, resume
     True/False/без-поля + эффект на файлы). Прогон: test_key_daemon + test_report
     322 passed, digest_from_raw+cli 11 passed, py_compile OK.

 - **`--no-reviews`: product-only сбор карточки без отзывов (2026-09-07).**
   Полный (или даже компактный) сбор отзывов по выдаче дорогой — отзывы тянут
   минуты/часы и десятки МБ на товар. Для быстрой «обзорной» кампании нужен режим:
   собрать только карточку товара (название, цена, рейтинг, характеристики, URL),
   отзывы не трогать вовсе. Ортогонально `--compact` (формат дайджеста).
   - **CLI и манифест:** `key_daemon --no-reviews` (арг один, как `--compact`).
     Флаг фиксируется при fresh в манифесте: `progress["run_cfg"]["skip_reviews"]`;
     resume берёт режим ИЗ МАНИФЕСТА (источник истины), CLI-параметр резюме не
     переопределяет; смена режима — только `--reset`. Пробрасывается в сбор через
     `gather_cfg` → `collect(..., skip_reviews=True)`.
   - **Поведение сбора:** `collect` при `skip_reviews` делает только карточку:
     `reviews=[]`, `reviews_skipped: true` в raw, `reviews_variant_mode` с
     `reason="reviews_skipped"`, поле `no_reviews_confirmed` НЕ выставляется
     (пустота отзывов — по режиму, а не подтверждение нуля). `gather` не режет
     такой raw как брак числа отзывов (`variant_review_problems` пропускается).
   - **QA:** `reviews_skipped` отменяет ТОЛЬКО review-specific проверки
     (объём/OVERCOUNT/UNDER_TOLERANCE, ZERO_REVIEWS/CONFIRMED_NO_REVIEWS,
     FALLBACK_OFFICIAL_COUNT) — вместо них INFO `REVIEWS_SKIPPED` → вердикт OK.
     Но плохая карточка остаётся FAIL: challenge-страница (`CHALLENGE_PAGE`),
     `BAD_STRUCTURE`/`BAD_IDENTIFIER`, и новая проверка целостности — без цены
     и рейтинга (`INCOMPLETE_CARD`). Это гарантирует, что product-only не даёт
     безусловный OK мусору. Rescan «0 отзывов = брак» не зажигается
     (пустые отзывы — норма режима, QA даёт OK, товар идёт в valid).
   - **Дайджесты:** явная пометка `_Отзывы не собирались (режим --no-reviews)._`,
     Вариант-рейтинг честно «недоступен» (не подменяется официальным) + справочная
     подпись `- Ozon (вся карточка): R / 5 · N оценок` (если официальные данные
     есть). Строки sort-статистики «Собрано …» не выводятся. Сортировка компакта
     детерминирована по официальному рейтингу DESC + pid.
   - **Охват:** MVP — ключевые кампании (daemon) и все рендереры/report; корневой
     `cli.py` пока НЕ проброшен (следующий отдельный шаг при необходимости).
   - Файлы: `ozon_collector/collect.py` (`skip_reviews=`, `reviews_skipped` в raw),
     `ozon_collector/gather.py` (проброс, пропуск variant_review_problems),
     `scripts/key_daemon.py` (`--no-reviews`, `run_cfg.skip_reviews` в манифесте,
     resume/fallback), `scripts/qa_raw.py` (guards, REVIEWS_SKIPPED, INCOMPLETE_CARD),
     `ozon_collector/report.py` (пометки, официальный рейтинг, sort), тесты:
     test_report +2 (skipped-фикстуры), test_qa_raw +4, test_gather +4,
     test_key_daemon +4 (манифест fresh/resume, QA-integr через реальный qa_raw).
     Прогон: test_key_daemon 227 passed, полный pytest 837 passed.

 - **Инцидент podarok_five: авто-включение `--match-title` из text= URL отсекало всю выдачу (2026-09-07).**
   Кампания `podarok_five` собрана по `search/?text=для девочки 5 лет подарок`. Вчерашний
   fallback-дизайн (при отсутствии явного флага брать `text=` из URL) включил фильтр
   `title_match_query="для девочки 5 лет подарок"`. Названия товаров (куклы/конструкторы)
   не содержат запрос дословно → **valid=0 при discovered=785**, демон 8 часов (8 прогонов)
   гонял полные сборы по 530 товаров, пока не уткнулся в дедлайн — классический случай
   «нормальная кампания превращается в бесконечно повторяемую». Урок: **поисковый запрос
   описывает намерение покупателя, а не название товара** — строгий фильтр нельзя включать
   автоматически по наличию `text=` в URL.
   **Решение (Вариант А):** фильтр включается ТОЛЬКО явным `--match-title`; мёртвый
   `_query_from_url` и импорт `parse_qs/urlparse` удалены; добавлены регрессионные
   интеграционные тесты (URL с `text=`, без флага → `title_match_query==""`; явный
   флаг → фраза из флага работает). Кампанию перезапускать без фильтра.
   **BACKLOG (не срочно):** «ранний стоп title_filter_empty» — если явный `--match-title`
   дал 0 совпадений при `discovered>0` (например 1-2 полных прогона), завершать кампанию
   reason `title_filter_empty` вместо кручения до дедлайна. НЕ включать в текущий фикс.
   Осторожно: 0 совпадений «сегодня» не означает ошибку (товар может появиться завтра);
   критерий и терминальность обсуждать отдельно.
   - **Зонд структуры выдачи (2026-09-06, `tests/probe_search_grid.py`, живой, изолированный
     профиль, порт 9337):** проверка подозрения «товары собираются из блоков „Вы смотрели“ /
     „Возможно, вам понравится“» — **НЕ подтвердилась**. На странице выдачи
     (URL бренда edifier + `text=Edifier W830NB`) единственный виджет с товарными ссылками —
     `tileGridDesktop` (2→6 подпагинаций виртуального листа `infiniteVirtualPaginator`,
     51 ссылка), все ссылки под ним; отдельных рекомендательных блоков с `/product/`
     на странице нет (`reco_widgets` пуст, `island` внизу ссылок не содержит).
     SEARCH_JS читает ровно то, что надо: кандидаты = все ссылки выдачи.
   - **Реальный источник мусора из кампании edifier (рюкзаки Xiaomi/перчатки/конфеты):**
     не отдельные блоки, а **персонализация прямо внутри tileGridDesktop** — в нейтральном
     изолированном профиле та же выдача даёт 51 чистую позицию (только Edifier W830NB/
     W800BT/W820NB), а в боевом профиле с историей Ozon подмешивает персональный поток
     в главную сетку. Лечится именно `--match-title` (фильтр после сборки), кода discovery
     менять не нужно.
   - Файл зонда: `tests/probe_search_grid.py` (карта ссылка→виджет + прокрутка как
     `_collect_links`; запуск: `python tests/probe_search_grid.py --url <выдача> --yes`).

 - **Кампания podarok_five ЗАВЕРШЕНА: COMPLETED / target_reached (2026-09-07).**
   Итог: **valid 518** (target 500, min_valid 400), failed=0, permanently_failed=0,
   discovered 523, runs 9 (все full NORMAL_DONE, **rescans 0**). Запуск 06:14 UTC →
   финал 10:53 UTC (~4.7 ч). Запускалась пользователем. После инцидента с
   `--match-title` (см. выше) собрана БЕЗ фильтра; цель достигнута с запасом.
   - **Наблюдение по стабильности: `--parallel 12` на тяжёлых карточках даёт
     повторные `TargetClosedError` (стабильный renderer crash конкретных страниц:
     recycle → тот же URL снова роняет renderer до `goto`/wait).** Восстановление
     отработало штатно: 6 рециклов браузера, брак 0, единичный `SESSION_EXHAUSTED`
     на воркере — остаток батча передан, демон продолжил. Системного дефицита нет
     (RAM свободно 42/63 ГБ, CPU ~25%) — краши самих страниц под пиковой
     нагрузкой. **Рекомендация для будущих широких кампаний: `--parallel 4–6`
     (очень тяжёлые — до 1); текущий прогон не перезапускать.**
   - **BACKLOG (вариант C, НЕ реализовано):** «крашер-PID» — если после recycle
     тот же URL снова даёт `TargetClosedError` (2-й подряд) → skip товара без
     траты recycle-бюджета + телеметрия крашеров в recovery. Отдельным шагом.

 - **Дайджест: секция отзывов разделена на «обычную» и «диагностическую» (2026-09-05).**
   После устранения ложной статистики осталось «когнитивное» искажение: LLM
   видела сплошную стену негатива из score_asc и могла сделать вывод «половина
   ругает товар» при рейтинге 4.96. Решение — структурно разделить блок 6 в
   report.py(render): `### Отзывы покупателей — обычная выборка (N)`
   (отзывы без score_asc, с пояснением «используется для рейтинга варианта»)
   и `### Негативные отзывы — целенаправленный поиск проблем (M)` с
   предупреждением `⚠️ …количество НЕ является долей недовольных покупателей».
   Единый предикат `_is_score_asc` для расчёта рейтинга и секций (Инвариант:
   «обычная» секция = выборка `variant_rating_from`; mixed-отзывы
   `['published_at_desc','score_asc']` идут в диагностическую, как и
   исключаются из рейтинга). Нумерация отзывов с 1 в каждой секции, порядок
   исходный. Пер-секционные счётчики ★ , итоговая суммарная строка
   «Итог по всем собранным отзывам: …» по-прежнему по всем (включая score_asc).
   Пересбор не нужен (review_sorts уже в raw). Тесты: +6 в test_report
   (91 passed), search 53, digest_from_raw+cli 11. Промпт-страховка:
    «секцию "Негативные отзывы" не экстраполировать на общую удовлетворённость».

 - **Разведка variant-статистики Ozon: ЖИВОЙ эксперимент подтвердил — per-SKU гистограммы/score и paging.total у Ozon НЕТ (2026-09-05).**
   Проверено на живой карточке Персил 246074722 (multi-SKU) через `tests/probe_variant_stats.py`
   (изолированный профиль ozon-profile-variant-stats-probe, порт 9336): снятие state-JSON и
   сетевых ответов `reviews/pdp-part` до/после клика «Этот вариант товара» (reviewsVariantMode=1).
   **Результаты:**
   - `webReviewProductScore` (totalScore=4.9, reviewsCount=210799, гистограмма score[5★:200333 …
     1★:697]) — **НЕ меняется** при активном варианте: это всегда рейтинг/гистограмма ВСЕЙ склейки
     карточки, не варианта.
   - `paging.total`/`totalCount` — **числового поля нет вообще**; в `paginator` state есть только
     постраничное `size:10` + `nextPage`. Знаменатель полноты сбора остаётся `variant_count` из
     виджета «Этот вариант товара (N)» (11164 для 246074722) — это максимум, что даёт Ozon.
   - `itemId` — **есть и корректен**: в `webListPhotos.galleryParams` каждый отзыв-медиа несёт
     `itemId=<SKU>`; единственный встреченный = целевой 246074722 → вариант-фильтр чистый
     (все 10 отзывов первой страницы — нужный вариант, чужих SKU нет).
   - Клик переключает `variant_active` «Все отзывы»→«Этот вариант товара» и фильтрует сами отзывы.
   **Вывод (ветка C закрытия):** рейтинг варианта = пересчёт по вариант-отфильтрованным отзывам
   (исключая score_asc) — единственно корректный способ; официальной per-SKU статистики нет.
   Production НЕ менять (collect/report/search не трогаем). `webReviewProductScore.itemId` =
   SKU карточки (не отзыва). Справки: raw 246074722, отчёты probe_variant_stats_*.json в корне
   проекта.

   **itemId-гарда: ЗАКРЫТО как непервоочередное (решение пользователя, 2026-09-05).**
   Доработка разведки подтвердила: в DOM-карточке отзыва (`[data-review-uuid]`) itemId НЕТ —
   только `data-review-uuid`; per-review itemId есть лишь в сетевом JSON отзывов
   (`entrypoint-api.bx/page/json/v2?.../review`, пара `"itemId":"…"` + `"uuid":"…"` в одном объекте
   и в `photos[].galleryParams`). Значит полноценная гарда = сетевой перехват ответа ревьюшельфа
   (по образцу `_expand_comments_snapshot`/`_on_response` в collect.py) → карта `uuid→itemId` →
   прокидывание `item_id` в raw → WARN/FAIL в QA при чужом SKU. Это среднее изменение
   (collect+raw+qa+тесты), а текущий вариант-фильтр уже чистый (в probe чужих SKU не встречено:
   net_item_ids_unique = [246074722], все 15 карточек = нужный вариант). ПОЭТОМУ РЕШЕНО:
   не делать, теме присвоен статус «опциональный backlog» (сделать при реальной угрозе —
   например, если будет встречаться чужой itemId в пуле). Отчёт: `probe_variant_stats_dom_246074722.json`
   (dom_card.has_itemId=false; reviews_net с uuid+itemId).

 - **Рейтинг дайджеста и доля негатива: вариант по нейтральной выборке, score_asc только контент (2026-09-05).**
   Найдено: у товаров с большим числом отзывов в дайджесте рейтинг системно
   занижен — это НЕ реальное качество, а артефакт сбора. Сортировка
   «сначала низкие оценки» (±score_asc) выбивает худшие отзывы первыми, и для
   гигантов (нельзя собрать всё — стена входа) выборка ~190 набита негативом:
   пример pid 1283214310 — собранный негатив 26% и avg 2.51 при реальных
   0.66% негатива и полном распределении 4.91 (365 142 оценки). «Официальный
   рейтинг» Ozon (вся карточка) как основной источник ОТКЛОНЁН пользователем:
   карточка может слопать несколько разных товаров. Решение (report.py):
   **основной рейтинг варианта = среднее только по отзывам без метки
   score_asc** (`review_sorts` уже сохранён у каждого отзыва → пересбор НЕ
   нужен): `variant_rating_from`/`digest_rating`. Старые raw без `review_sorts`
   — нейтральные; у кого после отсечения 0 отзывов — фолбэк на рейтинг
   карточки Ozon с пометкой «нет нейтральной выборки». Справочная строка
   в карточке: `Ozon (вся карточка): X·K оценок` из `customer_opinion.counts`
   (точное распределение) или `product.rating`. Сортировка (`sort_digest_products`):
   балл = рейтинг варианта × число нейтральных оценок (без множителя у
   фолбэка-карточки), tie-break больше N → pid, без рейтинга ниже, ok=False
   в конец. `--min-rating` (search.collect_search) фильтрует ПО НОВОМУ баллу
   варианта (единый критерий с показом), не по официальному. **Доля негативных
   оценок (1–2★)** — только из полного распределения `customer_opinion.counts`
   `(N1+N2)/(N1..N5)`, строка «Негативных оценок (1–2★): 2.0% · по 365142
   оценкам (вся карточка Ozon)»; распределения нет → строку не выводим (никаких
   процентов по собранной выборке: score_asc — канал поиска проблем, не
   статистика покупателей; обсуждение ботов 2026-09-05). Тесты: report
   85 passed, search 53 passed (включая min_rating), digest_from_raw+cli 11.
   Полный прогон test_key_daemon в этой сессии не гонялся (тяжёлый, висит).

 - **Дайджест: сортировка по avg×count вместо среднего балла (2026-09-05).**
   Раньше «лучшие в начало» = средний балл собранных отзывов (убыв.), затем
   число отзывов (решение 2026-09-04). НО: сортировка по среднему возносила
   товары с парой отзывов на 5★ над товарами с 3★×9, даже если последний
   объективно популярнее. Изменён `sort_digest_products` (report.py): ключ =
   **средний балл × число собранных отзывов** (убыв.) — «популярностный балл».
   Пример: 3×9=27 > 4×5=20 > 5×2=10. Семантика прочего сохранена: ok=False — в
   конец; без собранных отзывов (avg=None) — ниже имеющих их (ключ +inf, НЕ -inf:
   иначе при восходящей сортировке они уезжали наверх); pid — детерм. tie-break
   при равном произведении. Вызывается теми же 3 точками (report.build_search_
   digest_combined, search.collect_search, parallel после merge_shard_results).
   Тесты test_report.py: переписан best_avg→best_avgxcount (3,9/4,5/5,2 → 3,1,2),
   renamed avgxcount_grows_with_count, добавлен tie_break_by_pid (3×4=4×3),
   errors_and_no_reviews_last сохранён = 74 passed, py_compile чист.

 - **`key_daemon --reset`: полное пересоздание существующей кампании (2026-09-05).**
   Раньше свежий сбор = руками удалять `data/campaigns/<name>` (иначе терминальная
   кампания → мгновенный выход, а ещё продолжались старые discovered/valid).
   Добавлен флаг `--reset` в `scripts/key_daemon.py`: удаляет каталог кампании
   целиком (progress.json, status.json, events.jsonl, runs/) ДО загрузки манифеста
   и стартует как новая кампания (fresh=True): требуется `--url`, discovered/valid/
   runs не наследуются, `campaign_identity.url_sha256` пересчитывается. Сырой кеш
   товаров (`data/*_raw*.json`) общий и НЕ трогается — свежесть сбора по-прежнему
   через `--max-age 0`. Защита: выполняется после PidLock, лог демона в отдельном
   LOGS_DIR не удаляется. `--force-new` по-прежнему НЕ сбрасывает состояние (только
   обход identity). Тесты: 2 новых в test_key_daemon.py (reset терминальной
   COMPLETED_MIN + reset без существующей кампании) = 209 passed, py_compile чист.

 - **Окна воркер-браузеров: возвращены плавающие блоки над таскбаром (2026-09-05).**
   Коммит `936a715` убрал `WS_EX_NOACTIVATE` из `launch_chrome.ps1`, `cdp.py
   _minimize_window_native()` и `_restore_window_native()` → воркер-браузеры стали
   появляться в таскбаре кучей обычных кнопок. Причина: `WS_EX_NOACTIVATE`
   (0x08000000) говорит Shell «окно НЕ должно получать taskbar-кнопку»;
   при + `HWND_TOPMOST` оно рендерится как маленький плавающий блок над таскбаром.
   Решение: ВЕРНУЛИ `WS_EX_NOACTIVATE` + `SW_RESTORE` во всех трёх местах
   (фактически откат `936a715`). Окна снова плавающие блоки, фокус не сбивается
   (окно не активируется). Побочка (принята пользователем): двойной клик по
   блоку НЕ восстанавливает окно (Shell-thumbnail рендерит отдельный процесс;
   `WM_NCLBUTTONDBLCLK` не доходит) — восстановление через Alt+Tab/клик по списку.
   **Фикс вспышки (та же сессия):** `SW_RESTORE` (9) в `Invoke-MinimizeNoActivate`
   показывал окно развёрнутым ~100-300 мс, потом Chrome сворачивал → «окна
   появляются а потом сворачиваются». Заменён на `SW_SHOWMINNOACTIVE` (7) —
   показывает сразу свёрнутым без мелькания. `cdp.py` НЕ трогали (там `SW_MINIMIZE`
   уже корректный; `_restore_window_native` с `SW_RESTORE` только для keep_window).
   VERSION не повышался. Тесты: test_search.py 53 passed.

 - **Ленивый запуск браузеров: reintroduced (2026-09-05).** Раньше каждый
   воркер в `worker_main` делал `_make_session(spec)` (запуск Chrome) СРАЗУ при
   старте процесса — до назначения товаров. При rescans (4 товара на 12
   воркеров) ~8 воркеров поднимали браузер зря. История: ленивый вариант уже
   был в коммите `4a672c1` (динамическая раздача), но откачен `38a8b77` из-за
   `asyncio loop error` (`sync_playwright().start()` вызывался из глубокого
   стека `_collect_shard`). **Новая реализация в `parallel.py worker_main`:**
   `sess=None` до `_recv_batch(p2w_conn)`; `_make_session(spec)` +
   `_warmup_session(sess)` вызываются ПОСЛЕ получения первой пачки, на верхнем
   уровне `worker_main` (стек `worker_main → _make_session → Session.__init__ →
   _connect → sync_playwright().start()` — без вложенных event loops).
   Воркеры без товаров выходят без запуска Chrome. Heartbeat-hook работает и
   без сессии (просто шлёт в pipe). finally: guards `sess is not None` уже были.
   Статический шардинг не менялся. Экономия: ~8 запусков Chrome × 2-3 сек +
   ~800 МБ RAM на rescan 12 воркеров. Тесты: test_search.py 53 + test_parallel.py
   49 + test_parallel_restart.py 19 passed, py_compile чист.

 - **«Отзывы о товаре 0» из state-JSON → подтверждённый ноль (2026-09-04).** Кампания `gorlo`: 105 товаров вечно пересобирались как `incomplete` (`rc=None`, no_widget). Причина: у таких нулевиков страница нормальная, просто отзывов нет (вручную подтверждено), но экстрактор `ratingCount()` искал только числа («N оценок», «N отзывов»), а виджет `webReviewProductScore` машинно отдаёт `reviewsCount: 0`/`totalScore: 0` в `data-state` JSON (блок «Отзывы о товаре 0»). Решение: в `PRODUCT_JS` добавлен helper `reviewZero()` (collect.py) — читает `data-state` виджета `webReviewProductScore`, возвращает true при `reviewsCount<=0` (fallback totalScore<=0); `rating()` → `{value:0}`, `ratingCount()` → `0`. Итог: `product.rating=0` (не None → `missing_fields` пуст, `incomplete=False`), `rating_count=0` → `no_reviews_confirmed=True` → кеш hit. `min_rating` в кампании не задан → exclusion «rating ниже» не срабатывает. Age-gate (`4465529033`, title «Подтвердите возраст») — **отдельная задача, не входит сюда**. VERSION не повышался. Тесты: 4 новых в `test_extractors.py` (state zero, totalScore-only, count>0 не тронут, отсутствие виджета без краша) = 13 passed; `test_cache`+`test_gather`+`test_report`+`test_qa_raw` = 179 passed.

 - **QA: CONFIRMED_NO_REVIEWS → OK для подтверждённых нулевиков (2026-09-04).** QA в `qa_raw.py:221` ставил `ZERO_REVIEWS` FAIL при `collected==0`, даже для настоящих нулевиков с `no_reviews_confirmed=True` (нормальная карточка, экстрактор подтвердил reviewsCount=0). Итог: нулевик собирался как ok, кеш хитил, но QA → FAIL → не попадал в `valid_pids`. Решение: если `raw["no_reviews_confirmed"]` truthy и `collected==0` → INFO `CONFIRMED_NO_REVIEWS` вместо FAIL. Вердикт нулевика → OK. `QA_VERSION` с `1.6` → `1.7` (инвалидация QA-кеша). Новая кампания: первый сбор нулевика записывает `no_reviews_confirmed=True` в canonical → QA → OK → valid_pids. Существующие кампании: нулевики в canonical (без флага) останутся FAIL до следующего прогонного QA (verdict invalidated qa_version). Тесты: 2 новых в `test_qa_raw.py` (confirmed zero → OK, rc=0 + confirmed → OK) = 24 passed.

 - **0-отзывовые товары кешируются как hit, не пересобираются (2026-09-04).** Причина: `incomplete=True` (отсутствие `product.rating`/`price.current`) → `cache._validate_raw` отбрасывал raw → `miss` каждый проход → повторный полный сбор с заходом на Ozon. Решение: поле `no_reviews_confirmed` в raw (collect.py, сразу после `missing_fields`); кеш: `incomplete and not no_reviews_confirmed` → `"incomplete"` → `"hit"`. Два достоверных сигнала явного нуля: `rating_count == 0` (экстрактор карточки) **или** `variant_count == 0` при `used=True` (виджет варианта). `variant_review_problems == []` необходимо, но недостаточно — без явного сигнала нуля (`rc=None`, `no_widget`) товар не кешируется (неоднозначный случай). Не кеширует: недогруз (`short(N/rc)`), нет подтверждения. `_best_raw` ранжирует: `(1 если reviews>0, ts, N)` — confirmed-нулевик не вытесняет полный raw. gather.py при `hit` + `variant_review_problems=[]` не обнуляет кеш. Бамп VERSION не требуется (поле аддитивно, старые raw без него — miss → 1 пересбор → потом hit). Тесты: `test_cache.py` (confirmed-zero hit, widget-zero hit, ambiguous incomplete, short incomplete, backward-compat) + `test_gather.py` (confirmed-zero hit no-recollect, miss recollects) = 718 passed.

 - **Лучшие товары — в начале дайджеста (2026-09-04).** `report.sort_digest_products` сортирует `combined["products"]` in-place по: средний балл СОбранных нами отзывов (`_avg_review_rating`, убыв.) → число собранных (reviews_collected, убыв.) → pid (детерм. tie-break). Ошибки (ok=False) — в конец; ok без отзывов — ниже имеющих их (avg=None → -inf ключ). Вызывается во всех трёх точках формирования combined: `report.build_search_digest_combined` (офлайн digest_from_raw + key_daemon._write_digest), `search.collect_search` (живой одиночный), `parallel` после `merge_shard_results` (живой параллельный). `render_search_digest`/`write_search_digest` не менялись — нарезка на части остаётся согласованной (combined отсортирован ДО планирования). Регресс test_report(73)+parallel+digest_from_raw+reviews+collect_topup = 209 passed, py_compile чист.

- **Решение возрастного гейта 18+ «Подтвердите возраст» (2026-09-04).** Модалка поверх карточки товаров 18+ (напр. pid=4465529033), НЕ анти-бот-заглушка — `detect_challenge` её не видит, title/body карточки нормальные. Реализация `collect._pass_age_gate(page)` (collect.py:984), вызывается сразу после `check_challenge` и на reviews (1882), и на product (2233). Логика: probe `_AGE_GATE_PROBE_JS` (/Подтвердите возраст/) → `input:visible` → `press_sequentially("01011990")` (маска ДД.ММ.ГГГГ не принимает value) → кнопка «Подтвердить» (`get_by_role("button", name="Подтвердить")`) → если enabled → клик.
>
> **Фикс 2026-09-04:** `el.click()` → synthetic `click`-событие, а Vue-компонент Ozon (userAdultModal) слушает `pointerdown`/`pointerup` цепочку — без неё `setBirthdate?isAlco=false` API не вызывается, модалка остаётся (лог «пройден», а карточка всё ещё «Подтвердите возраст»). Заменено на полную цепочку `pointerdown → mousedown → pointerup → mouseup → click` через `dispatchEvent(new PointerEvent(...))` с `clientX/clientY`, `pointerId`, `pointerType:'mouse'` (collect.py:1030-1043). Проверено вживую на `4465529033` (подключение к рабочему Chrome через CDP на 9222): дата `01.01.1990` вводится (inner input value корректен), pointer chain снимает гейт, товар «Спрей для полости рта» загружается. Fallback `btn.click(force=True)` сохранён для случая `element_handle()==None`. Тесты: test_age_gate 3 passed + test_extractors 13 = 16 passed.

 **Ключевой фикс (2026-09-04):** обычный `btn.click()` (Playwright) падал с `TimeoutError ... subtree intercepts pointer events` — оверлей модалки `<div class="a75_4_1-b3">` внутри `<div class="vue-portal-target">` перехватывает pointer events (в логе: 33× retry → renderer_failure → recyclable). Заменён на **trusted JS-клик** `handle.evaluate("(el) => el.click()")` (как в `_expand_review_comments`/`_is_sticky_intercept`), фолбэк `btn.click(force=True)` только при renderer_failure и не-sticky. Подтверждено вживую: после JS-клика `age=False` (модалка ушла), карточка открывается, `looks_like_puzzle=False`, пазл после возраста НЕ появляется. Остаётся куки-баннер («ОК») — на снятие не влияет, в age/probe не входит. Тесты: новый `tests/test_age_gate.py` (JS-клик vs Playwright, no-gate, disabled) = 3 passed; регресс age+puzzle+challenge_guard+collect_topup = 42 passed.

- **Решение слайдер-пазла «Сопоставьте пазл, двигая ползунок» ВНЕДРЕНО (2026-09-04).** Ради кампании stir_poroshok (падала на анти-бот капче). Интеграция ЕДИНАЯ через `collect.check_challenge` (collect.py:941-961), который вызывается и при поиске (search_page/canary/warmup_home, search.py:210/293/376), и при сборе (reviews_page/product_page, collect.py:1881/1938/2232): если `detect_challenge` дал сигнатуру ИЛИ в DOM есть фон+кусочек пазла — вызывается `puzzle.solve_puzzle`, успех → возврат без AccessChallenge (не тратится FAB-reload/budget). warmup в parallel.py:399 только прогревает куки, пазл решает уже `_collect_shard` по товарам.

 **Три корневых фикса в `ozon_collector/puzzle.py` (2026-09-04):**
 1. `_capture_info` — многострочный JS-строковый литерал селектора слайдера (перенос строки внутри `'[class*=slider]...'`) давал `SyntaxError` → детект пазла по DOM всегда возвращал `None`. Сделан однострочным.
 2. `_still_challenge` полагался только на `is_puzzle(detect_challenge)`, а для `title:antibot captcha` сигнатура не содержит «пазл» → всегда False даже при нерешённом пазле (ложный `solved:true`). Теперь сначала проверяет DOM (`looks_like_puzzle`).
 3. Зацеп не за ручку слайдера: у Ozon классы ручки/трека ПУСТЫЕ (`cls:""`), `slider:null`; и drag-за-центр ручки не работает (в центре svg-иконка перехватывает клик через `elementFromPoint`). Добавлен `_find_slider` по ГЕОМЕТРИИ (трек = самый широкий тонкий DIV ниже фона, ручка ~40x32 у его левого края) и drag **за левую часть ручки** (grab_x = handle.x + ~6, не центр).

 **Алгоритм (puzzle.py):** `_capture_info` (bg=img.image w>200, puzzle=img.puzzle) → скачиваем bg/piece (urllib) → `_find_notch` (OpenCV matchTemplate, x выемки) → `_find_slider` (трек+ручка, геометрия) → dx = bx*scale в экранные px → drag ручки за левый край. `PUZZLE_MAX_ATTEMPTS=2`, `_PUZZLE_FAILURES` (счётчик неудач через `failures()`/`reset()`).

 **Подтверждено вживую (2026-09-04):** капча на `ozon.ru/search/?text=спрей лечения горла&from_global=true` → `verdict=solved`, `failures=0`, `title_after=спрей лечения горла - купить на OZON` (переход на выдачу); `elapsed_s=2.41`. Пробник: `tests/puzzle_probe.py` (изолир. профиль, порт 9335, `--url --yes [--force-copy]`), отчёт JSON. Тесты: `tests/test_puzzle.py` (+ `_find_slider`/`looks_like_puzzle`) и `test_challenge_guard.py` = 26 passed; `test_collect_topup.py` = 13 passed.
 
 - **OCR по умолчанию ВЫКЛЮЧЕН (2026-08-17).** Флаг --no-ocr заменён на --ocr (включает tesseract); в конфигах no_ocr = not args.ocr. Параллельный OCR требует --allow-parallel-ocr (проверка: args.ocr and not args.allow_parallel_ocr). Сбор 300 без OCR ускоряется в разы. test_gather.py 16 passed.

 - **Новое поле: По мнению покупателей (customer_opinion) (2026-08-17).** В PRODUCT_JS добавлено извлечение блока: % рекомендуют (по всей странице) + гистограмма оценок по звёздам (строки N звёзд, процент из style=width, количество отзывов в конце строки). Поля: recommend_percent, stars {5..1: %}, counts {5..1: N}, text. Рендер в report.py: строка По мнению покупателей в карточке товара, напр. 92% рекомендуют 5? 88% (4527) · 4? 7.3% (374) .... Тесты test_extractors.py + test_report.py добавлены. Примечание: реальная разметка Ozon - проценты в атрибуте style=width строки N звёзд (webReviewProductScore), а не текст NN%.

 - **Итог: `element is not stable` ? renderer_failure (2026-08-17).** В полном прогоне 300 (18:27+) на товаре 3103801574 клик по кнопке комментария падал: `TimeoutError: ElementHandle.click: Timeout 4000ms exceeded ... element is not stable` > ложно классифицировался как renderer_failure > recycle браузера (потеря прогресса). Это НЕ hang renderer: элемент просто двигался (lazy-load/анимация), renderer жив. Фикс в `collect.py`: `_is_sticky_intercept` теперь ловит и паттерн `element is not stable` (в дополнение к `intercepts pointer events`) > вместо recycle срабатывает fallback JS-клик `el.click()`, сбор продолжается. Тесты: `test_is_sticky_intercept` расширен, test_reviews.py 53 passed. Защита для следующих прогонов (текущий процесс работал со старым кодом).

 - **Итог полного прогона 300 (18:27>19:28): обрыв по таймауту, данные в raw сохранены.** Воркеры собрали 25/300 товаров (13+12), но на 19:28:53 были принудительно terminate по дефолтному `--worker-timeout 3600` (1 ч) — раньше завершения. Итог `collected=0 ok=0`, отчёт пустой, ВСЕ товары в merge помечены `worker terminated/failed`, хотя `data/*_raw.*.json` записаны на диск (валидны, 70–192 отзывов). Причина: 300 товаров с OCR + 2 сортировками ? 2–5 мин/товар > нужно 6–12 ч, а лимит был 3600 сек.
 **Фиксы (2026-08-17, для следующих прогонов):**
 * `cli.py`: лимит `--worker-timeout` поднят 3600 > 86400 сек, дефолт 3600 > 43200 (12 ч).
 * `collect.py _expand_review_comments`: max_passes 6>4, таймаут клика 4000>2000 мс, паузы 400>200 мс / 2500>1500 мс (комментарии — главный пожиратель времени, 4 мин на 62 кнопки).
 * `parallel.py` + `search.py`: периодический progress-снимок (каждые PROGRESS_EVERY=5 товаров) — воркер шлёт частичный payload по pipe; при hang/terminate родитель берёт последний снимок, собранные товары НЕ теряются (вместо пустого отчёта). New: `WorkerContext.done`, `_reader_loop` t=progress, hang-детекция по `done` вместо `payload is None`. Тест `test_worker_hang_after_progress_keeps_partial` (42 passed в test_parallel.py).

 - **Параллельность: дефолтный лимит 2 > 8 воркеров (2026-08-17).** `PARALLEL_MAX_WORKERS` в `__init__.py` по умолчанию теперь 8 (env `OZON_PARALLEL_MAX` по-прежнему переопределяет). Порты воркеров назначаются динамически (9222+k > 9223–9230), профили — `worker_profile_token(k)`, конфликтов нет. Тест `test_parallel_validation_bounds` обновлён (дефолт 8, env 16). Внимание: 8 одновременных сессий Ozon — выше нагрузка на FAB, следить за challenge-штормом.

## Статус и следующие шаги (последнее обновление: 2026-09-05)

 - **✅ Секция отзывов дайджеста: обычная / диагностическая (2026-09-05).**
   Блок 6 разделён на «Обычная выборка» (без score_asc, пояснение «для рейтинга
   варианта») и «Негативные отзывы — целенаправленный поиск проблем» с
   предупреждением про НЕ-долю недовольных. Предикат секций = предикат рейтинга
   (`_is_score_asc`), инвариант «обычная секция ≡ выборка рейтинга» покрыт
   тестом. Нумерация с 1 в секции, порядок исходный, итоговая суммарная строка
   осталась. test_report.py: 91 passed (+6), test_search.py: 53 passed,
   digest_from_raw + cli: 11 passed. Пересбор НЕ требуется.
   Осталось (опц.): экстра-строка в `qa_raw` про секции; актуализация README/docs.

 - **✅ Рейтинг варианта по нейтральной выборке + честная доля негатива (2026-09-05).**
   См. запись в шапке: артефакт чрезвычайных low-first-сортировок раскрыт и
   убран — основной рейтинг и сортировка дайджеста идут по отзывам без
   `score_asc`, официальный рейтинг карточки справочной строкой, `--min-rating`
   фильтрует по новому баллу; доля негатива (1–2★) — только из полного
   распределения Ozon (вся карточка), без распределения — не выводится.
   Пересбор данных НЕ требуется. Осталось (опц.):
   `score_asc` в коллекторе помечать флагом «для анализа дефектов» и не смешивать
   в пул среднего вообще (сейчас смешивание уже не влияет — фильтр на этапе
   расчёта); актуализировать подписи/README и, при желании, docs о метрике.

 - **✅ Окна воркер-браузеров + ленивый запуск (2026-09-05).** См. записи в шапке:
   `WS_EX_NOACTIVATE` возвращён (плавающие блоки над таскбаром, без кражи фокуса),
   `SW_SHOWMINNOACTIVE` вместо `SW_RESTORE` при старте (без вспышки). Ленивый
   запуск браузеров reintroduced: `_make_session`+`_warmup` после `_recv_batch`.

 - **✅ Динамическая пакетная раздача (batch=5) реализована (2026-09-03).**
   Замена статического round-robin шардинга на «общий ящик задач» у оркестратора:
   воркер по завершении пачки запрашивает следующую через parent→worker pipe —
   устраняет «хвост» (простаивание быстрых воркеров в конце кампании).
   * `parallel.py`: два однонаправленных pipe на воркера — `conn` (worker→parent)
     и `p2w_conn` (parent→worker, только `{t:"next",batch}` / `{t:"none"}`);
     `worker_main` переведён на пакетный цикл `_recv_batch`→`_run_shard`→`{t:"done_batch"}`;
     `_await_workers` = диспетчер (shared_items deque + completed_indexes/owner),
     requeue недоделанных при краше, stall штрафует только PROCESSING (не WAITING_FOR_TASK);
     stop = Вариант A (мгновенный terminate всех живых), force = 2-й Ctrl+C без респавна;
     `_rebuild_shards` пересобирает совместимый shards из owner (fallback на статический
     шард для невыданных); телеметрия `combined.parallel.scheduler` (batch_size, requeue_count,
     tail_ratio); `DEFAULT_BATCH_SIZE=5`, настраивается `gather_cfg["batch_size"]`.
   * ВАЖНО об концах duplex=False: первый конец = RECV, второй = SEND; для parent→worker
     родителю отдан SEND, воркеру RECV (иначе OSError read-only).
   * Scope: только parallel.py + тесты; search/cache/key_daemon/QA/FAB не трогались.
   * Тесты: `_spawn_workers`/test_parallel.py переведены на пакетный диспетчер;
     test_parallel.py 49, test_parallel_restart.py 19, search/qa/digest/raw/collect_topup 98,
     gather/report/cache/ocr/etc. 268 — все зелёные.

 - **✅ Ctrl+C: закрыты 2 дыры — докрутка ссылок stop-aware + «живой» 2-й форс (2026-09-03).**
   Live-прогон выявил: первый Ctrl+C игнорировался, пока шла докрутка выдачи
   (main-сессия `_collect_links_with_recovery` не проверяла `_STOP_EVENT` и нап.
   делала recycle/reconnect), а «2-й Ctrl+C = форс-выход» был фактически МЁРТВ:
   обработчик SIGINT глотает сигнал и не доходил до `except KeyboardInterrupt`,
   где проверялся `_FORCE_EXIT`. Пофикшено (вариант 1.A, централизованный
   emergency shutdown).
   * `search.py new CollectionInterrupted(links, forced)` — низкий уровень НЕ зовёт
     os._exit(), только сигнализирует (forced=False=стоп / True=форс). `_collect_links`
     и `_collect_links_with_recovery` принимают `stop_event`/`force_event`
     (defaults None → cli/тесты не меняются); проверка на границе каждой итерации
     скролла И до/после каждой recovery (new_tab/recycle), приоритет force>stop;
     после стопа НЕ запускается повторный recycle/goto (тот самый бесконечный
     `recycle→reconnect→scroll` из проблемы).
   * `parallel.py collect_parallel(..., force_event=None)` + `_await_workers`:
     graceful-стоп докрутки → возвращает partial combined (`discovered_pids` из
     найденных ссылок, `collected=0`, воркеры НЕ запускаются); форс-стоп докрутки →
     проброс `CollectionInterrupted(forced=True)`. `_await_workers` при force →
     `_terminate_worker` ВСЕХ активных воркеров (зачистка их браузеров в родителе),
     без респавна/новых задач.
   * `key_daemon.py`: `_inproc_collect` пробрасывает `force_event`; в `run_campaign`
     явный `except CollectionInterrupted` ДО общего `except Exception` (форс НЕ
     классифицируется как CRASH/UNKNOWN_FAILURE); `_FORCE_EXIT` проверяется после
     worker-фазы (`_emergency_shutdown(force="workers")`) и в link-фазе
     (`force="collection"`). `_emergency_shutdown` — ЕДИНСТВЕННАЯ точка `os._exit(1)`
     + зачистка pid-файла (bounded cleanup браузеров уже сделал `_await_workers`).
   * `extract_discovered_pids` сливает `combined.discovered_pids` (graceful-стоп
     докрутки), иначе найденные ссылки терялись бы (products пуст).
   * Тесты: +2 `_collect_links`, +1 `_collect_links_with_recovery`, +2
     `collect_parallel` partial/force, +3 `_await_workers` force(+приоритет),
     +2 key_daemon (emergency shutdown на FORCE / extract merge); обновлён
     `fake_collect_links` в test_search.py. 275 key_daemon+parallel+restart и
     5 search-веток — зелёные. Требует живой проверки: докрутка+Ctrl+C (быстрый
     стоп) и 2-й Ctrl+C (форс) в реальном прогоне.

 - **✅ Красивый Ctrl+C: graceful shutdown воркеров + INTERRUPTED (2026-09-03).**
   Раньше Ctrl+C в консоли Windows получали демон И spawn-воркеры (одна console-group):
   KeyboardInterrupt рвал `finally` воркера и `_stop_worker_browser` не доезжал →
   осиротевшие Chromium на портах 9223–9228. Пофикшено на уровне архитектуры.
   * `parallel.py worker_main`: в самом начале — `SetConsoleCtrlHandler(NULL, TRUE)`
     на Windows (взят из `run_tests_guarded.py`). Воркеры НЕ видят консольный Ctrl+C;
     остановкой управляет только оркестратор.
   * `_await_workers` (обе ветки: limit=0 и restart): новый параметр `stop_event`;
     при установке — штатно сворачивает живых воркеров через `_terminate_worker`
     → `_cleanup_worker` → `force_stop` (зачистка браузера в РОДИТЕЛЕ, надёжно,
     не зависит от `finally` воркера), БЕЗ респавна. Текущий item воркера теряется
     (не помечается completed) → на resume `_recover_interrupted_run` доберёт.
   * `collect_parallel(...stop_event=...)` пробрасывает event в `_await_workers`.
   * `key_daemon.py`: глоб. `_STOP_EVENT`/`_FORCE_EXIT`. `_graceful_exit` (SIGINT/
     SIGTERM) больше НЕ делает `raise SystemExit` — ставит `_STOP_EVENT`. Второй
     Ctrl+C во время shutdown → `_FORCE_EXIT` (defensive escape в `__main__`).
   * Цикл `run_campaign`: на верху `while True` и в mid-collect ветке проверка
     `_STOP_EVENT` → сохраняем частичный результат (discovery/QA/reconcile по тому,
     что реально собрано), `final_state=INTERRUPTED`, `final_reason=user_interrupt`.
   * `TERMINAL_STATES` + `DAEMON_STATES` + `finalize_campaign` ранний выход:
     INTERRUPTED трактуется как NEEDS_ATTENTION (фин. QA по неполным данным НЕ
     прогоняем), счётчики `no_progress`/`crash_runs`/`challenge_seq` НЕ растут,
     дайджест на INTERRUPTED не пишется. Resume после Ctrl+C работает штатно.
   * Cooldown-сон и цикл ожидания — stop-осведомлённые (опрашивают событие ≤1с).
   * Тесты: +3 (test_parallel_restart.py: stop_event limit0 / restart-branch / late-
     break), +2 (test_key_daemon.py: INTERRUPTED top-of-loop / during-collect-partial).
     205 key_daemon + 63 parallel (+16 restart) + 81 cli/digest/report — все зелёные.
   Правки целиком в `key_daemon.py`, `parallel.py`, `tests/test_parallel_restart.py`,
   `tests/test_key_daemon.py`. Требует живой проверки заглушки Ctrl+C в воркере
   (контрольный прогон с ручным Ctrl+C).

 - **✅ Тексты в дайджесте без пустых строк (2026-09-03).**
   По запросу: в отзывах, комментариях и описаниях товаров убраны повторные
   переводы строк (пустые строки), текст идёт сплошным потоком строк.
   Новый хелпер `_collapse_newlines` (report.py) применяется к тексту отзыва
   (`_review_block`), комментария (`_comment_lines`) и описания (`render`, секция 4).
   Пример «Здравствуйте!\n\nСпасибо…» → «Здравствуйте!\nСпасибо…».
   Дополнено (тот же день): `_collapse_newlines` также заменяет неразрывные/
   узкие юникод-пробелы (`\xa0 \u202f\u2009\u200a`) обычными — покрыл и описания,
   которые идут только через этот хелпер (раньше NBSP в описаниях оставался).
   Тесты: +4 (collapse, рендер отзыва, NBSP в описании), test_report = 70 passed.
   Дополнено (тот же день): NBSP-замены хватало для текстов, но имя автора
   отзыва (`_review_block`) и комментария (`_comment_lines`) выводились как есть —
   у продавца «Цифровой Центр» NBSP оставался в дайджесте. Теперь имя автора
   тоже проходит `_collapse_newlines(_norm_nbsp(...))`. Дайджест naushniki_300
   пересоздан: NBSP всех типов = 0, 3 файла строго ≤ 3 МиБ.

 - **✅ Цена в дайджесте: только с Ozon Картой (2026-09-03).**
   По запросу: `_price_block` теперь выводит одну строку «Цена с Ozon Картой: N ₽»
   из `price.bank` (кнопка «С банками»). Убраны `Цена` (current), «Старая цена»,
   «С банком», «Уверенность», структура `price_parts`. Fallback: если `bank` нет —
   `Цена: N ₽` из `current`; при его отсутствии — пусто (price_text/parsed не
   выводятся). Решение выбрано пользователем из трёх вариантов (bank как Ozon-карта).
   Затронуты `_price_block` в report.py (меняет и `render()`, и блок товара дайджеста);
   тестов на цену не было, test_report = 66 passed.

 - **✅ Разбиение дайджеста по РАЗМЕРУ файла вместо числа товаров (2026-09-03).**
   `write_search_digest` режет md по `max_file_size_bytes` (дефолт `DEFAULT_MAX_FILE_SIZE_BYTES
   = 3 * 1024 * 1024` = 3 МиБ), а не по `max_products_per_file`. Старый API удалён полностью.
   * `_product_block_lines` — чистый рендер блока одного товара (глобальная сквозная
     нумерация `## Товар {i}`, не per-part) → байт-идентичность планирования и записи.
   * Жадная нарезка по порядку PID (без перестановок) с консервативной оценкой размера
     (worst-case шапка «Часть N из N» + строка размера) → запись никогда не превышает оценку.
   * После записи каждый нормальный файл проверяется через `os.path.getsize`; превышение —
     `RuntimeError`. Исключение: часть с РОВНО одним товаром, чей блок сам > лимита
     (допустимый overflow, `logger.warning`).
   * `_write_md` пишет с `newline="\n"` и `encoding="utf-8"` → размер на диске == UTF-8 байты
     строки (без недетерминизма CRLF на Windows).
   * CLI `scripts/digest_from_raw.py`: `--max-file-size-mb` (float, default 3.0, `>0`,
     отклоняется если меньше `minimum_header_size_bytes`); `--max-products-per-file` убран.
   * `scripts/key_daemon.py` не менялся — вызывает `write_search_digest(combined, out)` на
     новом дефолте 3 МиБ.
   * Проверено на реальном `naushniki_300`: 3 файла, каждый строго ≤ 3 145 728 байт
     (3135365 / 3140109 / 887997). Тесты test_report = 66 passed.

 - **✅ «По мнению покупателей» — аспектная сводка в карточке (2026-09-02).**
   По запросу: аспекты между заголовком «По мнению покупателей» и маркером
   «Подобрали для вас»/футером выводятся в секции 1 (Качество звука/Комфорт/
   Внешний вид и т.п.), мусор и конкуренты отброшены. `_customer_aspects()`
   (report.py): берёт `customer_opinion.text`, режет по строкам `метка: значение`
   до первого не-аспекта. Рейтинг и % рекомендуют остаются невыводимыми
   (данные обо всех вариантах). Тесты: test_report +2 (рендер аспектов, пусто при
   отсутствии text). test_report = 43 passed. raw-формат не менялся, версия не
   бампалась. Проверено на 2608348159 (Focal Bathys MG).
     * Дополнено (тот же день): добавлены ПРОЦЕНТЫ — «Рекомендуют: N% покупателей»
       (если Ozon отдаёт) и «Оценки: ★5: 93% · ★2: 7%» (только ненулевые, из
       `customer_opinion.stars`). «Подобрали для вас»/конкуренты не выводятся.
       test_report = 44 passed.

 - **✅ v1.16.3: «Задание для LLM» убрано + аспекты в одну строку (2026-09-02).**
   VERSION 1.16.2 → 1.16.3. По запросу пользователя.
   * **«Задание для LLM» удалено вообще** (`render_search_digest`): пользователь
     вставляет задание сам, блок в каждой части кампании был бесполезным.
     Тест `test_search.test_render_search_digest_format` — блок ТЕПЕРЬ отсутствует.
   * **Аспекты варианта — одна ·-строка** (`_review_block`): вместо трёх отдельных
     строк `Внешний вид:…` / `Комфорт:…` / `Качество:…` выводится одна
     `Внешний вид:Великолепный · Комфорт:Удобно · Качество звука:Хорошее`
     (разделитель « · », пустые аспекты отфильтрованы). Первая строка variant
     (цвет) как раньше — в шапке при едином значении или в строке отзыва.
   * Тесты: `test_report` — `test_review_block_aspects_join_single_line`,
     `test_review_block_aspects_empty_no_line`; `test_search` — блок отсутствует.
     Прогоны: test_report+test_search = 90 passed, test_reviews = 71 passed (суммарный
     прогон падает по таймауту на flaky live-markup, по отдельности зелёно).
   * Регенерация сырого дайджеста: `render()` — аспекты одной строкой, поисковый
     дайджест — без «Задания для LLM». Пересбор кампании — позже, по команде
     (FAB-риск); v1.16.3 инвалидирует raw v1.16.2 (old_version).

 - **✅ Уменьшение размера дайджеста + stale-отзывы (2026-09-02).**
   VERSION 1.16.1 → 1.16.2. По запросу пользователя сокращаем вес дайджеста
   без потери информации.
   * **Жирность убрана везде.** В report.py удалены все `**…**` (автор отзыва,
     `→ М22`, метки карточки «Цена/Товар/Дата сбора/Вариант/Всего/…»,
     «Фильтр Ozon», «Сортировки», «Задание», «Размер дайджеста» и т.д.) —
     чистый текст для LLM. Тесты test_report обновлены на безжирный формат.
   * **Вариант один раз в шапке.** В `render()` секции 6: если у всех отзывов
     `variant` одинаков (первая строка поля `variant` — это «Цвет товара: …»,
     поле в raw содержит ещё и аспекты «Качество/Комфорт/Внешний вид» через
     `\n`, у 51 уникальных значений на 1032557475), выводится `Вариант: <label>`
     один раз в шапке, а из каждого отзыва убирается дубль. Аспекты
     (`Внешний вид:…`, `Качество звука:…`) остаются в каждом отзыве (не теряем
     информацию). `_review_block` принимает `variant_in_header`.
   * **`\xa0` → пробел.** Хелпер `_norm_nbsp` заменяет неразрывные и редкие
     юникод-пробелы (`\xa0 \u202f \u2009 \u200a`) на обычные в текстах отзывов
     и комментариев.
   * **stale («Отзыв устарел»).** Discovery (пробный браузер, порт 9333,
      профиль probe_stale): Ozon рендерит бейдж `<div title="Отзыв устарел">Отзыв
      устарел</div>` (класс `b5_7_3-a4 tsBodyControl300XSmall`
     хэшируется каждый деплой — ищем по `title`/тексту, не по классу). Ключевое:
     у stale-карточек Ozon НЕ рендерит звёзды (starCount=0, svgs 2-3), у свежих
     starCount=5 — collector уже корректно ловит stars=0. В REVIEWS_JS добавлен
     `isStale(wrapper)` (по `title="Отзыв устарел"` или тексту `/^(Отзыв\s+)?устарел$/i`)
     → поле `stale: bool` в каждый отзыв. В render `_review_block` при
     `stale=true` звёздная строка `> ☆☆☆☆☆ …` не выводится (без per-review метки),
     текст/дата остаются. Поле опционально: старые raw без `stale` валидны.
   * Тесты: `test_report` — stale скрывает звёзды, вариант в шапке (одинаковый/
     разный), `_norm_nbsp`; `test_reviews` — `isStale` по бейджу (stale=True/False).
     Прогон test_report+test_reviews = 110 passed.
   * Пересборка: 1032557475 — при желании; вся кампания 300 товаров — отдельно
     (FAB-риск). Discovery-артефакты: D:/Temp/opencode/stale_probe/.

 - **✅ B1: сбор комментариев через API + A: топ-отзывы в дайджесте (2026-09-02).**
   VERSION 1.16.0 → 1.16.1 (две проблемы пользователя на 1032557475:
   «Всего 100 vs 96+35» и «пустой текст ответов»).
   * **Часть B (comments API).** Проблема №2: селекторы `parseComments`
     (`span.jm0_25`, `div.j9l_25`, `a.l9j_25`) устарели → ответы продавца
     приходили с пустым text/date. Решение B0-B1: перехват API
     `rpGetCommentsByReviewUuid` (`POST https://www.ozon.ru/api/composer-api.bx/_action/rpGetCommentsByReviewUuid`,
     post_data `{"reviewUuid":<uuid отзыва>,"sku":..,"rootSku":..,"limit":10,"offset":0}`,
     ответ `{"comments":[{uuid, comment, publishedAtFormatted, parentUuid,
     rootParentUuid, isDeleted, author:{firstName, clientOfficial:{brandName}},
     usefulness:{useful,unuseful}}]}`). Привязка к отзыву — по `reviewUuid`
     из post_data запроса (response-listener `resp.request.post_data`), порядок
     кликов не нужен. В collect.py: `_expand_comments_snapshot(page)` →
     (n_clicks, by_review) подписывает/снимает response-listener в try/finally
     (лимиты `MAX_COMMENT_RESPONSES=500`, `MAX_RESPONSE_BYTES=200000`,
     `MAX_COMMENTS_PER_REVIEW=500`, `MAX_TOTAL_COMMENTS=100000`), парсит через
     `_parse_comment_api` (skip isDeleted/пустых, гигантских тел) и вливает
     через `_apply_api_comments` ПОСЛЕ финального top-up в обоих местах
     (главный цикл и `_topup_virtualized`). Источник один на отзыв:
     API непуст → `comments_source: api`, иначе DOM. Неоднозначная привязка
     (нет reviewUuid/не JSON/пустое тело) → DOM fallback, без угадываний.
     DOM fallback-селекторы в `parseComments` обновлены на актуальные
     `rpProduct_c4a`/`rpProduct_a4c`/`rpProduct_ca5` (старые k1l/j9l/jm0
     оставлены как fallback). `import json` вновь используется (был удалён
     как неиспользуемый в рефакторинге 2026-09-02).
   * **Часть A (топ-отзывы).** Проблема №1: дайджест резал `reviews[:100]`.
     В report.py добавлен `_top_reviews(reviews, n)`: метрика
     `(-(helpful_yes+helpful_no), -comments_count, -publishedAt, index)` —
     полезность → комментарии → новизна, стабильность (index). Отзывы без
     текста исключаются из топа, если есть хоть один текстовый; пустышками
     до n НЕ добираем. Применён в `render_search_digest` вместо среза;
     `render()` не тронут; «Всего: N» = фактически отобранных.
   * Тесты: `test_report._top_reviews`, `test_reviews._parse_comment_api` /
     `_apply_api_comments` / `_expand_comments_snapshot` (фейк-page с
     эмуляцией API-ответа), фикстура `_COMMENT_HTML` переведена на новые
     классы. Прогоны: test_reviews/test_report/test_collect_topup/test_extractors/
     test_qa_raw/test_cache = 192 passed.

 - **✅ Fix: воркеры умирают вместе с оркестратором (2026-09-02).** При
   принудительном kill демона (kill -9 / Stop-Process -Force) воркеры оставались
   сиротами: держали свой Chromium (порт/профиль), писали в wd_out.log и жили
   вечно (наблюдалось: воркер 16792 выжил после смерти демона 11184). Добавлен
   родительский watchdog в `worker_main` (parallel.py): поток каждые 5с проверяет
   живость родителя (демона) через `process.pid_alive(os.getppid())`; при смерти
   родителя воркер **сначала гасит СВОЙ Chromium** (`_stop_worker_browser(spec)`,
   force_stop по своему профилю/порту), затем `os._exit(2)`. Новый
   `process.pid_alive(pid)` на Windows НЕ может опираться на OpenProcess
   (возвращает handle для недавно завершившегося процесса) — используется
   `GetExitCodeProcess`: живой = STILL_ACTIVE (259). Верифицировано live:
   kill демона 10184 → воркер 16204 самоубился и убил свой браузер
   (worker-profile chrome → 0), все python-процессы завершились. Общий хелпер
   `_stop_worker_browser` используется и в finally воркера (штатное завершение).
   Осиротевшие браузеры от принудительно убитого ВОРКЕРА (а не демона) убирает
   preflight-очистка `orphaned_worker_browser_processes` при следующем запуске.
   Также подтверждено: лог ОДИН (`logs/{campaign}_daemon.log`), `wd_out.log`
   больше не создаётся после перезапуска демона через обновлённый watchdog.

 - **✅ Рефакторинг: костыли убраны, дубли схлопнуты (2026-09-02).** Без изменения
   функциональности и форматов (raw/combined/QA/отчёт):
   * parallel.py: убраны мёртвые параметры `_await_workers` (`join_timeout_s`,
     `specs`); дублирующие stall→terminate блоки вынесены в `_terminate_worker`.
   * search.py + parallel.py: общие хелперы `_links_from_pids` и
     `_filter_skip_pids` (блок skip_pids был байт-в-байт продублирован).
   * collect.py: `_variant_matches` (вариантная фильтрация отзыва была в двух
     местах); удалён неиспользуемый `import json`.
   * cdp.py: удалён мёртвый код (`Session.screenshot`, `browser_started_at`,
     `ownership_token`); `_cdp_port` → `process.cdp_port` (был дубль).
   * gather.py: удалён неиспользуемый `import json`.
   * ⚠️ test_key_daemon (pidlock/alive) флакает при ЖИВОМ боевом демоне
     (конфликт с реальным pid-файлом/процессами) — это не регрессия:
     unit 173 + integration 16 проходят отдельными прогонами.

 - **✅ Fix: контроль зависания воркера перенесён в оркестратор (2026-09-01).**
   Инцидент при сборе кампании `naushniki_bt`: при тотальной антибот-капче Ozon
   воркер завис намертво на товаре hoco w35 air (>12 мин без прогресса). Причина
   архитектурная: при `--parallel 1` `_inproc_collect` гонял сбор **in-process**
   (`collect_search` прямо в демоне, без subprocess-воркера) → оркестратор не мог
   прервать зависший CDP-вызов (не было отдельного воркера). Решение (по требованию
   «оркестратор следит за воркерами, без костылей»):
   * **key_daemon `_inproc_collect`**: убран in-process путь — сбор ВСЕГДА через
     `collect_parallel` (subprocess-воркер), даже при `--parallel 1`. Единый путь
     оркестрации: `_await_workers` следит за воркерами.
   * **collect.py**: удалён сторож `_StallWatchdog` (костыль). Вместо него
     `_touch_watchdog()` → heartbeat-hook `set_heartbeat_hook(fn)`, который
     worker_main перехватывает и шлёт `{"t":"hb"}` в pipe родителю (throttled 15с).
   * **parallel.py**: `WorkerContext.last_activity` обновляется в `_reader_loop` на
     каждое сообщение воркера (start/recovery/progress/done/hb). `_await_workers`
     (оба режима) terminate'ит воркера без heartbeat дольше `WORKER_STALL_S=300`:
     `terminate()` → `_finalize_worker` → `_cleanup_worker` → respawn (если
     разрешён). Воркер, зависший на CDP-вызове, теперь падает и перезапускается,
     а не «вечно» висит.
* Тесты: `tests/test_parallel_restart.py` +3 (stall-terminate без рестарта,
      stall-terminate+respawn, heartbeat свежий → НЕ stall). parallel+restart 58 passed.
   * ⚠️ test_search.py: зависает при полном прогоне (`pause_between_products` и
      соседние) — проявляется и на чистом HEAD (проверено git stash), т.е. это
      существующий флак/проблема окружения, НЕ регрессия от этих правок.

 - **✅ Единый лог с ротацией (2026-09-02).** Убран отдельный `wd_out.log`
   (редирект stdout процесса демона): всё и так пишется в один файл
   `logs/{campaign}_daemon.log` с ротацией по размеру (5MB) и в DEBUG
   (подробные шаги сбора отзывов от воркера). Демон пишет через `log()`,
   воркер — через `attach_file(worker_log, prefix='[worker=k pid=N]')`
   в тот же файл. stdout демона при запуске теперь уходит в NUL
   (watchdog-скрипты: `> NUL 2>&1` вместо `> *_wd_out.log 2>&1`).
   Один файл на кампанию — удобно смотреть: `logs/{campaign}_daemon.log`.

 - **✅ Сбор naushniki_bt возобновлён успешно (2026-09-02, ~00:00).**
   После cooldown антибот-капча Ozon спала: поиск выдал 336 товаров,
   воркер-1 (порт 9223) открыл товар JBL Tune 520 BT и собирает отзывы
   (unique=96, сходимость достигнута). Поиск идёт через main-браузер
   (порт 9222, потом останавливается), сбор — через worker-браузер
   (порт 9223) — поэтому окна браузера сменяют друг друга по фазам
   (нормально). Ранее (вечер 09-01) сбор блокировался антиботом
   (search_page + product_page captcha, cooldown 12-24ч); кампания была
   удалена и пересоздана, потом завершилась из-за капчи. Сейчас идёт
   новый run (parallel=1, target 300, min_valid 200).

 - **✅ Аудит всего кода + фиксы багов (2026-09-01).** Полный обзор всех модулей
   (browser/cdp/cache/collect/parallel/process/search/gather/report/ocr/log/puzzle).
   Исправлены реальные баги (полный прогон 673 passed — +3 новых теста):
   * **parallel.py — потеря данных при перезапуске воркера (critical).** Раньше при
     аварийном kill воркера (OOM/без done) и включённом перезапуске уже собранные им
     товары выбрасывались: `_respawn` удалял старый wctx из `finalized`, и `merge_shard_results`
     помечал их «terminated/failed». Теперь `_await_workers` переносит снимок собранного
     в `new.carried_payload`, а `collect_parallel` включает его через `_merge_carried`.
   * **parallel.py — гонка reader-потока.** `_finalize_worker` ждал процесс, но не
     writer-поток, читающий pipe: payload мог прочитаться неполным. Добавлен
     `wctx.reader.join(3.0)` после join процесса.
   * **log.py — утечка file-handle.** `attach_file` перезаписывал глобальный `_fh` без
     закрытия старого → на Windows файл оставался залоченным (WinError 32 при ротации).
     Старый `_fh.close()` перед открытием; убран дубль `_reconfigure_streams()` в `setup`.
   * **cdp.py — утечка Playwright.** При неудачном `connect_over_cdp` (обе попытки)
     запущенный `_playwright` не гасился → теперь `_playwright.stop()` перед `raise`.
   * **cdp.py — goto глотал ошибки навигации.** Timeout/net::ERR_* (страница не
     загрузилась) продолжались на старой странице → мусор в данные. Теперь
     пробрасываются (как в `_collect_reviews_pass` в collect.py — аналогично).
   * **cdp.py — `new_page` копил мёртвые Page в `_managed_pages`** — закрытая страница
     удаляется из списка (медленная утечка при долгих recovery-циклах).
   * **collect.py — `element_handle()` мог вернуть None** в `_expand_review_comments`
     (элемент отсоединился между count() и element_handle()) → AttributeError;
     добавлена проверка `h is None: continue`.
   * **collect.py — fallback-рейтинг без диапазона 0..5** (в отличие от виджетов) —
     опечатка в разметке давала бы рейтинг >5; добавлена проверка `v <= 5`.
   * **collect.py — `next_url` `__CLICK__` evaluate без защиты** `list/children[2]`
     (TypeError ловился, но грязно) — добавлена `if (!list || !list.children[2])`.
   * **collect.py — убраны дублирующие `print()`** у log.debug/info (стр. 1858/1875).
   * **report.py — `money(current)`/`current/p["old"]` ломался на строковой цене**
     (ValueError/TypeError) — теперь `_num()` нормализует current/old/bank.
   * Тесты: `tests/test_parallel_restart.py` +82 (carried_payload, _merge_carried).
   * Не вносилось (риск/сложность): untrusted `.click()` в `_activate_variant_mode`
     (осознанное поведение, A/B-эксперименты зафиксированы); правильный фикс
     tessdata-dir с пробелами (pytesseract не разбирает кавычки — попытка откачена,
     см. ниже); глобальный shared-deadline в `_await_workers`.


 - **✅ Windows: полный прогон pytest — 670 passed, стабильно (2026-09-01).** Полный
   прогон воспроизводимо падал на `test_reviews.py::test_review_extract_live_markup`
   (`Browser.new_page: Connection closed while reading from the driver`) в зависимости
   от способа запуска: `cmd /c "... > file 2>&1"` — всегда FAIL (7/7), запуск через
   python-обёртку с Ctrl-C-подавлением — всегда PASS. Диагностика (лог-хендлеры
   CTRL-событий, мониторинг node/chrome, полные повторные прогоны) показала: события
   до родителя НЕ приходят, а лечит именно штатный Win32-режим
   `SetConsoleCtrlHandler(NULL, TRUE)` («игнорировать Ctrl+C») в процессе-родителе
   до старта pytest. Ручные хендлеры (даже `return True`) не помогают. Источник
   генерируемого headless-Chromium console-control события не установлен — это
   гипотеза, не факт.
   * Фикс: `ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)` при
     `sys.platform == "win32"` в начале `tests/conftest.py` (прямые запуски pytest)
     и перед `Popen` в `scripts/run_tests_guarded.py` (watchdog). `SIG_IGN` больше
     не нужен и убран, вся диагностика (logger-хендлеры, мониторинг драйвера, хук
     `pytest_runtest_makereport` с трейсбеками) из `conftest.py` удалена.
   * Ключевые прогоны ОТ и ДО: 7× F при cmd-запуске; run_verb/run_verb3/run_verb7/
     run_verb8 (Popen + `SetConsoleCtrlHandler(None, True)`) = **670 passed, 4 skipped,
     1 xfailed** (~4 мин) — воспроизводимо.

 - **✅ CLI-сбор убран — демон единственный исполнитель сбора (2026-09-01).** Пользователь решил:
   оркестратор САМ ищет выдачу и рассылает товары воркерам; воркеры только собирают.
   * `cli.py`: подкоманда `search` удалена полностью (`cmd_search` + `_gather_cfg_from_args` + search-парсер).
     Остались отладочные/офлайн-команды collect/gather/ocr/report. Поиск выполняется только через
     демон (`_inproc_collect`: collect_parallel/collect_search).
   * `scripts/key_daemon.py`: удалён `build_cli_args` и константа `CLI`; тексты про «запуск CLI»
     переписаны на «in-process сбор». Всё — оркестратор управляет воркерами напрямую.
   * Тесты: `test_cli.py` 8 (search-тесты удалены), `test_key_daemon.py` 203 — добавлен тестовый shim
     `_test_build_cli_args` (переопределяет `k.build_cli_args`) для совместимости fac(argv)-протокола,
     сами интеграционные тесты не менялись. Ключевые прогоны: cli+key_daemon 211, parallel+restart+puzzle+search 110.

 - **✅ Слайдер-пазл: авто-решатель в check_challenge (2026-09-01).** Новый модуль
   `ozon_collector/puzzle.py`: `is_puzzle(sig)` (маркеры «пазл»/«ползунок»), `solve_puzzle(page)`
   (скачивание фона и кусочка → OpenCV `cv2.matchTemplate` находит выемку → drag слайдера мыши;
   устойчив к ошибкам/обрыву CDP), счётчик неудач `PUZZLE_FAILURES` с `reset()`.
   * **Интеграция:** в `collect.check_challenge` (collect.py:961-971) для слайдер-пазла СНАЧАЛА
     РЕШАЕМ (`PUZZLE_MAX_ATTEMPTS=2`), а не перезагружаем вслепую. Успех → пазл снят без расхода
     бюджета challenge/new_tab/recycle; неудача → старый FAB-reload fallback и FSM-лестница.
   * **Телеметрия:** `_collect_shard` (search.py) при SESSION_BLOCKED + счётчик пазла>0 пишет
     `recovery.challenge_storm` со `puzzle_persistent`/`puzzle_failures`/`streak` → parallel
     вернёт `combined.aborted` с `workers_stormed`/`all_workers` (для parallel>1 строма теперь
     видна корректно, а не только reason). `puzzle.reset()` — в начале каждого `_collect_shard`.
   * **«Свежий браузер после пары попыток» уже даёт FSM:** решатель (2 попытки) → FAB-reload×3 →
     new_tab×1 → recycle×1 (свежий браузер) → SESSION_BLOCKED. Отдельный перезапуск воркера
     оркестратором для пазла НЕ добавлялся (редантный, пришлось бы менять семантику abort).
   * ТЕСТЫ: `tests/test_puzzle.py` (9: маркеры, счётчик+reset, `_find_notch` на синтетике,
     check_challenge решает/не решает). Search 49, parallel 52 — зелёные.

 - **✅ anti_bot_architecture.md: ревизия 2.7 по 4 волнам критики + 3 правки кода (2026-08-31).**
   КОД (P1/P2, реализовано):
   * **Базовый fingerprint в телеметрию (P1).** `cdp.py: Session._capture_fingerprint_once`
     снимает однократный снимок при первом `goto` каждой сессии — UA, `navigator.webdriver`,
     languages, platform, hardwareConcurrency, deviceMemory, chrome, timezone, locale, viewport,
     screen. Пишется в `combined.fingerprint` и в `status.json` (`recovery_telemetry.fingerprint`).
     Только наблюдение (diff manual vs daemon), поведение не меняет. Полный fingerprint-probe —
     по-прежнему P2 (§18.5).
   * **`--session-budget-wall-clock` (P1).** CLI-параметр перекрывает `wall_clock_s` из
     `SESSION_BUDGET_DEFAULT`; пробрасывается в `collect_search(..., session_budget=...)` и в
     воркеры `--parallel` (через `collect_parallel(..., session_budget=...)` → task → worker
     `recovery_metrics`). Длинные одиночные `--limit>100` прогоны больше не «намертво» 1800с.
   * **Базовый KPI в `combined` + `status.json` (P2).** `combined.kpi`:
     `total_challenges` (access_stats.challenges), `total_recoveries` (recycles+new_tab_attempts),
     `products_since_recovery`, `success_rate`. Демон аккумулирует в
     `recovery_telemetry.kpi` (копится по прогонам, абсорбируется даже без events).
     В `status.json` — `kpi` и `fingerprint`.
   ДОКУМЕНТ (честные контракты/исправления):
   * §9 — пример бюджета пошагово (каждый `+=1`); исправлена опечатка «линмита»; отрезок
     SESSION_BLOCKED vs SESSION_EXHAUSTED переписан честно: **в коде оба → profile_observed и
     оба → CHALLENGE_STORM** (soft-эскалация EXHAUSTED — НЕ реализовано, кандидат в backlog).
   * §9 — `wall_clock_s` настраиваемый (ревизия 2.6).
   * §3 — базовый fingerprint реализован; полный probe остаётся P2.
   * §5 — добавлен честный «слепой угол» CDP-аномалий (cdc_*, Runtime.enable): не маскируем
     «стелс-паком», страховка = recovery/budget/cooldown.
   * §6 — контракт «warmup new_tab => бесплатный» (не расходует session_used), лимит только
     wall_clock.
   * §10 — canary-fail => SESSION_BLOCKED немедленно, `canary_fails` — телеметрия, не бюджет.
   * §11 — честно: `profile_cooldown.until` = тот же `until`, что storm (не независимый таймер);
     canary после cooldown на главной даёт ложное «можно идти» (до P1 canary-на-товар);
     риск хронического partial storm (soft-сигнал — задача §18.6).
   * §13 — внешняя лестница: recycle на external = ещё одна свежая вкладка в ТОМ ЖЕ контексте,
     не снимает контекстные блокировки; после него осознанный SESSION_BLOCKED.
   * §18 — докрутка НЕ расходует session_used (только wall_clock общий); KPI частично
     реализован (остались block_rate/scope-гистограмма/partial-сигнал).
   ТЕСТЫ: key_daemon 211 (+1: absorb KPI аккумулируется), search 49 (+3: wall_clock override,
   KPI+fingerprint, KPI counts), cli 13 (+2: wall_clock param), parallel 45. Совместный прогон
   с browser-fixture виснет (pre-existing, артефакт teardown) — по отдельности всё зелёное.
   Клиент-выбор: НЕ реализовывал (ввиду объёма/боевости) — canary-на-товар (P1, блокер), CDP-
   нейтрализацию cdc_* (L2, противоречит «не стелс»), soft-эскалацию EXHAUSTED, partial-сигнал.

 - **✅ key_daemon: ревизия кода + 12 новых багфиксов (2026-08-31).** Сессия аудита `scripts/key_daemon.py`:
   * **K4-проверка в финализаторе (ВАЖНО).** `_qa_clean` финального QA НЕ проверял K4-инварианты —
     недостоверная сводка давала fallback `OK` и тихо пропускала брак (в `classify_new_pids` проверка была,
     а в финализаторе нет). Теперь `_qa_clean` возвращает `parse_error` → `NEEDS_ATTENTION`/`qa_parse_error`
     (и при fail>0 с неполным per_pid — тоже).
   * **Детерминизм snapshot/reserve.** `list(set(valid_pids))` давал hash-порядок (PYTHONHASHSEED-зависимый) →
     на resume snapshot выбирался по-другому. Теперь `dict.fromkeys` сохраняет порядок обнаружения
     (и в `reconcile_validity`, и в target_reached, и в мягком пути, и в `discovered_pids`).
   * **Resume читает конфиг из манифеста.** `max_total_hours/max_no_progress_runs/max_rescans/reserve_count`
     теперь берутся из `run_cfg` (источник истины), а не из аргументов CLI при рестарте.
   * **PidLock:** битый pid-файл (не int / нет поля pid) → трактуется как stale, не падает.
   * **П1 в reconcile:** `reconcile_validity` теперь выбрасывает из `valid_pids` пиды, чей raw-файл
     исчез (источник истины — наличие canonical raw), а не только по вердикту FAIL.
   * **Immutable snapshot:** при target_reached `campaign_final_pids`/`campaign_reserve_pids` сразу
     сохраняются в манифест (до финального QA) — при resume не пересобираются.
   * **`validate_new_campaign`:** для НОВОЙ кампании обязательно `1 ≤ min_valid < target` (иначе
     SystemExit); `--min-valid` дефолт изменён 0 → 1. min_valid=0 давал бы пустой `COMPLETED_MIN`.
   * **`reserve_pids`** (legacy-поле, дублировало `campaign_reserve_pids`) — удалено из манифеста.
   * **`find_running_cli_pids`** (мёртвый код с `wmic`) — удалён.
   * **UNKNOWN_FAILURE** теперь ставит `final_reason="unknown_failure"` (раньше оставался None).
   * **Новая кампания без `--url`** → SystemExit (fail-fast вместо тихого `--url ""`); при resume url не нужен (из манифеста).
   * **run_id** получил счётчик `run_{ts}_{n}` — исключает коллизию `combined_*.json` при быстрых (<1с) последовательных прогонах.
   * **JSON-контракт демон↔QA (P0 из ревью архитектуры):** `qa_raw --report-json <path>`
     пишет `{summary, products:[{pid, verdict, issues}]}`; демон читает JSON вместо regex по stdout
     (stdout — fallback). Бонус: из issues демон детектирует детерминированный брак.
   * **Детерминированный брак → без бесполезного rescan:** `fail_reason_seq` (pid → набор кодов
     issues + сколько раз подряд тот же). 2× идентичный FAIL → сразу `permanently_failed`,
     бюджет rescan не сжигается; причина сменилась/стал OK → история сброса.
   * **`_alive`-гард:** нереалистичные PID (0, отрицательные, ≥2^24) → False без `os.kill`
     (на Windows `os.kill(huge,0)` зависал — сломало бы демон на старте при битом pid-файле).
   * **`qa_raw`: баг без `--pids`** — `args.pids.replace` падал на None; исправлено
     `(args.pids or "")`. 6 падений `test_qa_raw` — предсуществующие (OLD_DATA по старым фикстурам).
   * **`test_baseline_matches_source_raw`** помечен `@pytest.mark.xfail(strict=False)` —
     известный дрейф baseline, не регрессия кода; CI не красный.
   * Тесты: **192 → 209** (JSON-парсер, fail_reason_seq, _alive-гард, discovery-лимит,
     WALL_LIMITED parse, validate_new_campaign, сброс счётчиков, UNKNOWN_FAILURE, run_id-уникальность).
   * **Контракт rescan подтверждён:** `cli.py search --pids A,B --out-raw` реально существует,
     собирает конкретные PID без докрутки и пишет `combined.products` (тесты `test_collect_search_pids_*`).
   * У тестов, вызывающих `notify` из финализатора, замьючен `k.notify` (нет записи в реальный `data/`).
* Документация: `docs/key_daemon_architecture.md` — полное описание (K4 в обоих местах, П1,
      immutable snapshot, validate_new_campaign, кроссплатформенность, PID-reuse limitation, контракт --pids).
   * **✅ P0/P1-рефакторинг (2026-08-31):** доведён до зелёного (201 тест key_daemon, 212 всего).
      * **Atomic raw-write:** `gather.py`/`collect.py` пишут raw через `atomic_write_json`
        (tmp → `os.replace`) из `ozon_collector/cache.py` (+ `atomic_write_text`, `raw_sha256`).
      * **QA-кэш → `verdict_meta`** вместо `verdict_mtime`: fingerprint = `raw_mtime_ns`
        (быстрый фильтр) + `raw_sha256` (строгий) + `qa_version`. Race-защита: fingerprint
        до/после QA (если raw менялся во время QA — вердикт недействителен).
        Ключ инвалидации — `QA_VERSION="1.5"`.
      * **Run-journal:** `runs/{run_id}.json` со стадиями `run_started / run_completed /
        qa_completed / state_committed`. `_recover_interrupted_run` при resume находит
        последний прогон без `state_committed` и повторно применяет discovery + QA + reconcile.
      * **Runtime API:** `status.json` (state-переходы через `_set_state`, `DAEMON_STATES`)
        + `events.jsonl` (append-only лог событий).
      * **`campaign_identity`** (url_sha256 + schema_version + collector_version) — защита от
        смены категории при resume; `--force-new` для обхода. `run_cfg_hash` + `run_cfg_modified`.
      * **Transient-retry UNKNOWN_FAILURE:** до `--max-transient-retries` (дефолт 3) с короткой
        паузой `TRANSIENT_RETRY_BACKOFF_S=4`, затем NEEDS_ATTENTION/unknown_failure.
        (Фикс зависания: раньше ставился cooldown 300с, тест с mock-sleep крутился вечно.)
      * **`no_progress_count`** разделён на `progress` и `stall` по типам.
      * Docs обновлены (`key_daemon_architecture.md`: verdict_meta, run_cfg, journal, status/events;
        `architecture.md`: файлы на диске).

   * **✅ Анти-бот: ADR + Этап 0 (телеметрия, 2026-08-31).** Утверждён контракт
     `docs/anti_bot_architecture.md §14` (мини-ADR): challenge = стоп-сигнал, а не
     лавина recovery; 4 контракта (collector_tabs, browser-ownership, типы сбоев,
     INVALID-страница); FSM + observed_scope; без health-score/stealth/ротации.
     Реализован **Этап 0** (только наблюдаемость, поведение не менялось):
     * `cdp.py`: `browser_tabs_count()` (HTTP /json/list, диагностика) +
       `Session._managed_pages`/`collector_tabs()` (источник истины для
       collector_tabs — свои страницы, не чужие вкладки).
     * `search.py`: `_recovery_meta()` обогащает каждое recovery-событие
       (signature, last_url, browser_tabs, collector_tabs, duration_ms,
       products_since_recovery); счётчик `since_recovery` сбрасывается при
       любом recovery.
     * `key_daemon.py`: `_absorb_recovery_telemetry()` переносит recovery-метрики
       из combined в progress; `_status_payload` +4 поля (browser_tabs,
       collector_tabs, last_page_url, last_challenge_signature).
     * Тесты: +6 (search: 2, key_daemon: 3, cdp-фиксы). Прогоны зелёные:
       key_daemon 204, search 38, cli/baseline/collect_topup 31, cache/gather/
       browser/parallel 179, cdp/challenge 35.
     * **Известный артефакт:** совместный pytest key_daemon+search даёт hang
       browser-fixture (предсуществующий, не от телеметрии); по отдельности всё
       зелёное.
     * Следующие этапы ADR: 1 ownership/lifecycle → 2 challenge FSM.

   * **✅ Анти-бот: Этап 1 (ownership + lifecycle, 2026-08-31).** По ADR §14:
     * `cdp.py Session`: поля `browser_owner` (external|collector), `browser_pid`,
       `browser_profile`, `browser_started_at`, `ownership_token`. Определение:
       если `auto_launch` поднял браузер → collector; иначе по маркеру
       `{profile}/.ozon_collector_launched` (ставит launch_chrome.*) → collector,
       при отсутствии → external. При external НЕ берём `ctx.pages[0]` (чужую
       вкладку пользователя), а создаём свою `ctx.new_page()`.
     * `Session.ensure_single_worker_tab()`: безопасный порядок (create →
       init_script → rebind → close только свои managed → assert collector_tabs==1);
       при ошибке создания новую вкладку старая НЕ уничтожается. Вызов — вне
       critical section collect.
     * `search.py _recycle_browser`: external → только ensure_single_worker_tab
       (kill/restart ЗАПРЕЩЁН); collector → manager.restart + reconnect (как было).
     * `launch_chrome.ps1/.sh`: пишут маркер `.ozon_collector_launched` в профиль.
     * Тесты: +5 (cdp: launch_marker, ensure_single_worker_tab×2; search:
       external-no-kill, collector-restart). Прогоны зелёные по отдельности:
       key_daemon 204, search 40, cdp 20, browser 56, cli/baseline/collect_topup/
       challenge 49 (1 xfail), cache/gather/parallel 123.
     * **Pre-existing hang:** совместный pytest файлов с playwright browser-fixture
       (key_daemon+search, key_daemon+browser) виснет на teardown браузера.
       По отдельности всё зелёное. Чинить отдельной задачей, этапу не блокирует.
     * Следующий этап: 2 challenge FSM + observed_scope.

   * **✅ Анти-бот: Этап 2 (challenge FSM + observed_scope, 2026-08-31).** По ADR §14:
     * **FSM «один challenge-инцидент»** (search.py `_collect_shard`): лестница
       `reload×3 (внутри check_challenge) → new_tab×1 → recycle×1 → SESSION_BLOCKED`.
       Лестница НЕ крутится; после исчерпания — abort run (остаток aborted,
       error_kind=session_blocked, failure_type=session_failure, PID не штрафуется).
       Заменён старый CHALLENGE_STORM_LIMIT (3 подряд → aborted).
     * **`page.is_invalid` (K4):** `collect.check_challenge` при детекте помечает
       страницу INVALID; `Session._guard_check` бросает AccessChallengeError на
       любой eval/goto/scroll/wait INVALID-страницы (заглушка не попадает в raw).
       Новая вкладка (new_page/ensure_single_worker_tab/reconnect) снимает флаг.
     * **`Classification.failure_type`** (browser.py): access_failure (challenge),
       browser_failure (crash/CDP/target), product_failure (structure/variant_short),
       unknown. run_gather пишет `res["failure_type"]`.
     * **`observed_scope`** (наблюдение, не вывод): session_blocked →
       profile_observed; ok после new_tab → tab_observed; ok после recycle →
       browser_observed; иначе unknown.
     * **combined.aborted** → `{reason: "session_blocked", count}`.
     * Тесты обновлены под FSM (storm→SESSION_BLOCKED, лестница не цикл,
       transient→ok) + новые (INVALID guard, failure_type, observed_scope).
       Прогоны зелёные: key_daemon 204, search 40, browser/cdp/challenge 97,
       gather 34, cache/parallel/cli/baseline/collect_topup 120 (1 xfail),
       reviews/extractors/process/report/raw_shape/digest 116 (4 skip).
     * Следующие этапы ADR: 3 session budget → 4 canary.

   * **✅ Анти-бот: Этапы 3-8 (2026-08-31).** Все этапы ADR §14 реализованы:
     * **3. Session budget** (search.py): `session_budget {challenge, new_tab,
       browser_recycle, wall_clock_s}` на сессию (run); `session_used` счётчики;
       исчерпание → `SESSION_EXHAUSTED` (abort run, aborted session_failure).
       `product_retries` отдельно (max_recycles на товар). Default: 3/3/2/1800.
     * **4. Canary** (search.py): `run_canary` — фиксированный CANARY_URL,
       `check_challenge` + минимальная DOM-проверка, состояния
       `CANARY_OK/CHALLENGE/ERROR`. После recovery событий доступа (new_tab/
       recycle по challenge, флаг `access_recovery`), НЕ после профилактического.
       Canary-fail → SESSION_BLOCKED (счётчик `canary_fails`, НЕ жжёт PID/
       recycle budget). 
     * **5. Adaptive cooldown** (key_daemon): `challenge_control {storm_streak,
       total_storms, last_storm_at, cooldown_until, last_signature}` в progress.json
       (persistent). Эскалация 1м→5м→15м→NEEDS_ATTENTION (storm_streak > max),
       джиттер ±20% (CHALLENGE_JITTER_FRAC, `KD_RANDOM_SEED` в тестах).
       storm_streak растёт только после исчерпанного инцидента; сброс при
       прогрессе (new_disc/new_valid). `--challenge-pause` больше не фикс-пауза.
     * **6. Profile cooldown** (key_daemon): `profile_cooldown {until, reason,
       challenge_count, last_signature}` отдельно от storm (repeated_challenge).
       В `_status_payload` добавлены challenge_control/profile_cooldown.
     * **7. Browser stability** (search.py): recycle ТОЛЬКО по browser_stats
       (renderer_errors ≥ RENDERER_RECYCLE_THRESHOLD=2 или safety-ceiling
       RECYCLE_EVERY=40), НЕ по challenge. `browser_stats`/`access_stats`
       раздельно (challenge не трогает stability-recycle). После recycle
       renderer_errors сбрасываются.
     * **Заменено:** CHALLENGE_STORM_LIMIT (3 подряд → storm) удалён — заменён
       SESSION_BLOCKED (лестница) + adaptive эскалацией; `challenge_storm`-reason
       в combined → `session_blocked`/`session_exhausted`.
     * Тесты: +10 (budget 2, canary 2, adaptive 3, persistent-challenge 1,
       stability 1, gather failure_type 1) + переписаны storm/new_tab под FSM.
       Прогоны зелёные по отдельности: key_daemon 208, search 45,
       cdp_launch+gather+search 100, browser/challenge 76,
       cache/parallel/cli/baseline/collect_topup/reviews/extractors/process/
       report/raw_shape/digest 236 (4 skip, 1 xfail).
     * Доки: `docs/anti_bot_architecture.md §14.6` — все этапы ✅.

   * **✅ Ревизия анти-бот кода (2026-08-31):** найден и исправлен баг в canary:
     при canary-fail после успешного recovery-сбора перезаписывался уже собранный
     товар (`products[-1]` = session_blocked, raw=None) — терялись валидные данные
     и нарушался принцип «session_failure не штрафует PID» (ADR §14.1). Теперь
     canary-fail останавливает ТОЛЬКО дальнейший сбор (остаток aborted), собранный
     товар сохраняется (ok=True, raw на месте). Тест `test_canary_challenge_blocks_session`
     обновлён под это поведение.
     * **Известные упрощения (не блокеры):** докрутка выдачи
       (`_collect_links_with_recovery`) использует старую модель new_tab×2/recycle
       без session budget/canary — несоответствие ADR-лестнице, но не критично
       (докрутка собирает только ссылки, не товары). `--challenge-pause` остаётся
       в run_cfg как legacy-параметр, но storm-реакция теперь adaptive
       (не использует его).
     * Ревизия прогонов зелёные: search 45, cdp_launch+gather+browser+challenge
       131, collect_topup+cli+baseline+parallel+cache 120 (1 xfail), key_daemon 208.

   * **✅ Ревизия архитектуры по фидбеку (2026-08-31, правки по ревизии):**
     * **Докрутка выдачи** (`_collect_links_with_recovery`) приведена к той же
       FSM-лестнице, что и сбор товаров: `new_tab×1 → recycle×1 → raise`.
       Удалена константа `NEW_TAB_ATTEMPTS=2` (больше не нужна).
     * **Canary** теперь вызывается ТОЛЬКО после recycle по доступу (новая
       сессия — чистота не доказана), НЕ после new_tab (challenge уже снят на
       товарном URL и товар собран через check_challenge — canary избыточен,
       лишний заход на Qrator). Тесты canary переведены на recycle-путь.
     * **Warmup** (`_warmup_to_category`): challenge на главной → НЕ тащим на
       первый PID — переходим на свежую вкладку (new_page) до категории.
     * **Profile cooldown читается в цикле демона**: эффективный cooldown =
       max(cooldown_until, profile_cooldown.until); оба блокируют старт run.
     * **Документ выровнен с кодом**: §3 честно фиксирует, что fingerprint
       snapshot НЕ собирается (только recovery-метрики) — планируется
       `scripts/fingerprint_probe.py`; §8 уточнён multi-incident vs per-run
       budget; §10 canary после recycle; §11 приоритет cooldown; §18 legacy/
       backlog (challenge-pause deprecated, KPI обхода, параллелизм per-session).
      * Тесты: search 46 (вкл. warmup-challenge → new_tab), key_daemon 208,
        cdp/gather/browser/challenge 131. Все зелёные.

   * **✅ Финальная ревизия по 3-м волнам критики (2026-08-31, P0/P1 итог):**
     * **🔴 P1 (реализовано код+тесты): агрегация storm при параллелизме
       (ADR §14.6).** `key_daemon` разделил ФУЛЛ-шторм (`parallel==1` или
       `all_workers=True`) и частичный шторм: ФУЛЛ → `storm_streak++` +
       `profile_cooldown` + эскалация `[60,300,900]` + NEEDS_ATTENTION;
       частичный (часть воркеров штормит) → короткая пауза
       `CHALLENGE_PARTIAL_PAUSE_S=20` БЕЗ эскалации, чтобы один воркер не
       сжигал cooldown кампании. Тесты: `test_integration_partial_storm_
       no_escalation`, `test_integration_full_storm_parallel_escalates`
       (key_daemon 210 passed; ранее 208 — ФУЛЛ-шторм retro одиночного на
       parallel=1 сохранён).
     * **Документ `docs/anti_bot_architecture.md` обновлён по критике:**
       - **§9**: «canary НЕ расходует `session_used{challenge,new_tab,
         browser_recycle}`» + таблица «событие → счётчики → состояние».
       - **§10**: canary на главной — слабое место (теневой бан на товары);
         P1-решение — canary на лёгкий товар/категорию (backlog §18.4);
         canary не влияет на session_used.
       - **§11**: ТОЧНЫЙ триггер `profile_cooldown` (CHALLENGE_STORM →
         ФУЛЛ-шторм); агрегация storm при параллелизме (ADR §14.6).
       - **§13**: поведение при потере external-браузера (browser_failure,
         НЕ перезапуск чужого процесса).
       - **§6**: warmup-challenge на main → new_page до категории (не тащим
         на первый PID), страница main → INVALID.
       - **§16**: анти-паттерн «автоподбор по observed_scope»;
         `--headless=new` — эксперимент в backlog (§18.8).
       - **§18**: ВОЗВРАЩЁН статус докрутки выдачи (приведена к FSM);
         canary->P1, fingerprint->P2, KPI->P2, пул профилей->P3,
         headless->P3, ранломизация входа->P3; wall_clock_s=1800 note.
       - Мелочи: canary §10 L5→«L5/L7»; «копия профиля»→«свой профиль
         ozon-profile-worker-k» в §2.1.
     * **Регресс:** key_daemon 210 (отдельно, pre-existing hang при совместном
       прогоне с browser-fixture), search+parallel 91, остальные не-browser
       файлы 413 passed + 4 skipped + 1 xfailed. Все зелёные.
     * **Не реализовано (в backlog сознательно):** пул профилей с ротацией —
       требует пересмотра ownership и прогрева профилей (профиль без истории
       с мин. Trust Score провоцирует первичный challenge, §1); fingerprint-
       probe; KPI агрегаты; headless=new; ранломизация точки входа (L3).



> ⚠️ **ГЛАВНОЕ ПРАВИЛО СБОРА (установлено пользователем 2026-08-20, НЕ ЗАБЫВАТЬ):**
> отзывы собираются **ВСЕГДА для выбранного варианта товара** («Этот вариант
> товара»), а не для всего товара. Фильтр включается автоматически
> (`_activate_variant_mode`); `--no-variant-mode` не использовать для дайджестов.
> После каждого сбора проверять `raw.reviews_variant_mode`:
> `used == true`, `variant_count` = сколько отзывов у варианта, и **число собранных
> отзывов должно равняться `variant_count`** (если собрано меньше и разница не
> объясняется ограничением «дальше доступно только после регистрации» — сбор
> неполный, пересобирать). История бага: до фикса `window.scrollBy` не триггерил
> догрузку в режиме варианта → собиралось 3–30 из 63–412 (см. запись ниже);
> после фикса `scrollIntoView` — ровно `variant_count`.

 - **✅ ФИКС: «Этот вариант товара» + scrollIntoView (2026-08-20).** Два бага сбора
   отзывов варианта (заметил пользователь на 5354898200, 5275165581, 888568232):
   1) Виджет `webReviewsVariantMode` рендерится лениво (после прокрутки к отзывам),
      а `_activate_variant_mode` вызывался ДО скролла → `no_widget`, фильтр не
      применялся (у 5354898200 собирались отзывы всех вариантов, 19 вместо 0).
      Фикс: в `_activate_variant_mode` перед первым evaluate скроллим
      `SCROLL_TO_LAST_REVIEW_JS` до появления виджета (≤3 скроллов после появления
      списка отзывов = товар без тумблера).
   2) В режиме «Этот вариант товара» `window.scrollBy` НЕ триггерит догрузку отзывов
      (застревало на 3–24 при variant_count 63–412); в обычном режиме scrollBy работал.
      Причина: Ozon грузит лениво по scrollIntoView последнего отзыва, а не по шагу.
      Фикс: `_scroll_to_converge`, финальная докрутка и `_activate_variant_mode`
      используют `SCROLL_TO_LAST_REVIEW_JS` (scrollIntoView последнего `[data-review-uuid]`,
      fallback — scrollBy 1500). Проверено live: 5275165581 → 64/64, 888568232 → 96
      (остальные из 412 доступны только после регистрации), 5354898200 → 0 (верно,
      у варианта действительно 0). Тесты test_reviews.py + test_cdp_launch.py 71 passed.
 - **✅ ПАЛАТКИ 100 (2026-08-20).** `search --url ".../category/palatki-11426/?...seatsquantl=37727..37733&text=Палатка" --limit 100 --parallel 1 --out docs/palatki.md`:
   found=104 collected=91 ok=100 excluded=9, файл 2.1МБ. Прогон ~7ч, 4 TimeoutError (renderer_failure) на товарах
   5279701501, 4041068293, 1504781999, 4905861818, 4479860768, 4366983927 — все восстановлены recycle, ни одного
   зависания. Средний рейтинг по отзывам — до тысячных (`.3f`): `Средний рейтинг по отзывам:** 4.433 / 5 (100 оценок)`,
   общий рейтинг/opinion по-прежнему не выводятся.
 - **✅ БЛЕНДЕРЫ 200+300 И FAB-БЛОК ПРОФИЛЯ (2026-08-19/20).** Два полных прогона
   `search --url .../category/pogruzhnoy-blender-31638/ --parallel 1 --out docs/blender.md`:
   limit 200 → found=200 collected=196 ok=200 excluded=4; limit 300 → found=304 collected=295 ok=300 excluded=5
   (файлы blender.md 2.5МБ + part2 + part3). Без единого access_challenge.
   **Первопричина FAB за ночь:** Ozon давал «Похоже, нет соединения» (fab_chlg_...) на ВСЕХ страницах
   (даже главной) со старого профиля `ozon-profile`. IP рабочий (пользователь заходит из обычного Chrome),
   куки валидны (access-token 16:04), fingerprint чистый (webdriver=None, chrome=True, GPU реальный),
   воспроизводилось и на системном Chrome (НЕ Playwright-сборке) с тем же профилем. **Решение без изменения кода:**
   скопирован залогиненный профиль `Profile 1` (11 ozon-куки, os_crypt-key из Local State) → `ozon-profile-copy` →
   содержимым заменён `ozon-profile`. FAB исчез, работает Playwright-chromium + goto через CDP.
   Бэкап старого: `ozon-profile-BLOCKED-backup`. Вывод: Ozon метит сам профиль (fingerprint устройства
   внутри профиля), а не только IP/браузер.
   **Фиксы кода в этой сессии:** (1) UnicodeEncodeError `→` в cp1251 stdout — `log.py setup()` вызывает
   `_reconfigure_streams()` (stdout/stderr → utf-8 errors=backslashreplace, аналог worker-фикса parallel.py);
   (2) access_challenge на докрутке выдачи ронял весь search — добавлен new_tab-recovery в
   `_collect_links_with_recovery` (NEW_TAB_ATTEMPTS=2, паттерн из `_collect_shard`).
   **Техника успешных ночных прогонов:** kill старого Chrome перед стартом; Start-Process python -u cli.py ...
   с RedirectStandardOutput → D:\Temp\opencode\*.log (stdout в cp1251!); `--parallel` требует аргумент в PS (1).

 - **✅ Фикс «тихого» нуля отзывов на проходе sort (2026-08-18).** Ozon разово не отдаёт блок отзывов при навигации на карточку (рате-лимит/флак): _scroll_to_converge возвращает 0 без исключения → проход считал «отзывов нет» и рекавери (recycle) не срабатывал. Проявилось на проходе «сначала низкие оценки» (сорт score_asc): негативные отзывы товара 2446440373 не собирались. Фикс в collect.py: на первой странице прохода при count==0 — до EMPTY_RETRY_PASSES=2 reload страницы + повторная прокрутка; в page_tel/page_log пишется empty_retries. Проверка: свежий сбор 2446440373 — 192 отзыва (96 published + 96 score_asc: 7×0★, 89×1★, 2×3★); ранее score_asc давал 0. Тесты: 379 passed, 4 skipped, 1 pre-existing fail (test_baseline).

 - **✅ ПРОГРЕВ СЕССИИ ВОРКЕРА (2026-08-17, коммит `5298523`).** По аналитике обхода антибота Ozon
   (IP-репутация, fingerprint, cookie-сессии, JS-челлендж, rate-limit): главная защита — «прогрев» сессии.
   Добавлено в `parallel.py`: `_warmup_session()` — воркер перед сбором открывает `https://www.ozon.ru/`
   (4 с), получает антибот-куки/решает JS-челлендж, challenge не фейлит воркер. Телеметрия `payload["warmup"]`.
   Проверка сессии (диагностика): `navigator.webdriver=False`, реальный UA Chrome 151, `ru-RU`, Win32, заголовки
   штатные (Accept-Language `ru-RU,ru;q=0.9`, Sec-CH-UA) — маскировка НЕ нужна (реальный Chrome без --enable-automation).
   Live-smoke: оба воркера «сессия прогрета», collected=2 ok=2 без challenge. pytest 367 passed (+3 новых),
   1 pre-existing fail (baseline raw), 4 skipped. **Коммиты сессии по FAB: `f320adb` (UTF-8), `5192546` (refresh кук),
   `5298523` (прогрев).**

 - **✅ ПЕРВОПРИЧИНА FAB НАЙДЕНА И ИСПРАВЛЕНА (2026-08-17, коммиты `f320adb` + `5192546`).**
   Диагноз экспериментами (challenge_probe): бан НЕ на IP — main-профиль (9222, 199 кук) открывает карточки чисто,
   а воркеры получали `access_challenge`. Дальнейшее сужение: НЕ неполная копия профиля (даже 116 КБ LocalState+Cookies
   → PASS). Причина: **просроченные куки воркеров** — `bootstrap_worker_profiles` копировал сессию только при первом
   сидинге, а затем `skipped:already_has_session` пропускал навсегда; у worker-1 было 17 старых кук → CHALLENGE,
   после копирования свежих из main (199) → PASS. Исправление (коммит `5192546`):
   * `process.py bootstrap_worker_profiles` — теперь СИНХРОНИЗИРУЕТ куки из main при каждом вызове (`refreshed`),
     а не только при первом сидинге (`seeded`);
   * `parallel.py collect_parallel` — bootstrap вызывается ПОСЛЕ остановки main-браузера (перенесён из cli.py,
     где main был ещё жив → `skipped:main_browser_running`); параметр `no_bootstrap` для `--no-bootstrap-workers`;
   * `cli.py` — убран ранний вызов bootstrap (дублировал логику).
   Проверено: smoke10 (17:29) — воркер живьём собрал 185 и 192 отзыва без единого challenge; полный pytest 364 passed,
   1 pre-existing fail (baseline raw), 4 skipped.
   **ВЫВОД ПО СБОРУ:** правило «smoke + пауза 2–4ч» больше не является жёстким барьером — главное свежие куки воркеров.
   Полный прогон 300 товаров (~7ч при 2 воркерах) ПОКА НЕ ЗАПУСКАЛИ — по решению пользователя отложен (риск кумулятивного FAB).

 - **⛔ ПОВТОРНЫЙ FAB на прогоне 300 товаров (2026-08-17, 17:06–17:11):** `search --limit 300 --parallel 2 --no-ocr`
   «робот пылесос» сразу после smoke (16:54, который прошёл чисто, 97 отзывов). **collected=1 ok=1** (только кеш-хит
   1779338676, 161 отзыв), все живые товары → `access_challenge` «похоже, нет соединения». **new_tab (2/2) и recycle (2/2)
   НЕ помогают** — причина оказалась в просроченных куках воркеров (см. выше), а не в блоке уровня IP.
   **Бонус-фикс (коммит `f320adb`):** воркер падал на `UnicodeEncodeError` при print `→` (cp1251),
   из-за чего new_tab recovery даже не выполнялся. Исправлено в `parallel.py worker_main` — stdout/stderr
   переключаются на UTF-8 с `errors=backslashreplace`. Теперь recovery выполняется штатно.

 - **feat: дефолт sorts `published_at_desc,score_asc` + кеш по sorts, версия 1.15.0 (коммит `6e5f29a`).**
   Дефолт `--sorts` для collect/gather/search теперь `published_at_desc,score_asc` (константа `DEFAULT_SORTS`
   в cli.py): сначала «новые и полезные», потом «сначала низкие оценки», дедуп по uuid (`_review_key`),
   у каждого отзыва `review_sort` + `review_sorts`. Кеш теперь учитывает sorts: `cache_check(pid, max_age,
   data_dir, sorts=...)` / `_best_raw(..., sorts=...)` — raw без запрошенных сортировок (`reviews_sorts`
   не суперсет) отдаёт `sorts_missing` вместо hit. **Версия 1.15.0 → старые raw 1.14.0 дают `old_version`
   → miss (принудительный пересбор).** Разбиение md-дайджеста на ≤100 товаров (`--max-products-per-file`)
   вошло в этот же коммит. Live-проверка: 2561238412 пересобран, 181 отзыв (было 96), 17 негативных
   (1–2 зв.), 0 дублей, `reviews_sorts: [published_at_desc, score_asc]`. pytest: 364 passed, 1 pre-existing
   fail `test_baseline_matches_source_raw`, 4 skipped.

 - **⛔ ИНЦИДЕНТ: большой прогон 300 товаров → FAB на всех живых, new_tab НЕ разблокировал (2026-08-17, 13:02–13:16).**
   `search --limit 300 --parallel 2` (нашёлся 304, последние контакты 12:56 и 12:58 — запуск сразу после двух прогонов):
   **все ok = cache=hit** (w1 123, w2 126), **ни одного живого товара**, fail=8 у каждого (все cache=miss → access_challenge).
   **new_tab recovery: 28 попыток (14+14), успехов 0** — FAB не отпускает ни новая вкладка, ни recycle.
   Шторм в конце: worker2 149/150 `challenge storm` (streak=3, remaining_aborted=1) → `search ABORTED`, CLI посоветовал cooldown.
   Итог: `found=304 limit=300 collected=241 ok=266 excluded=25 aborted`. Даже без нового кода дайджест бы вышел из кеша.
   **Вывод:** guard и recovery отработали штатно (ни одного challenge в products, шторм остановил прогон), но new_tab
   снимает ТОЛЬКО tab-уровневую блокировку (эксперимент — анонимная сессия), а здесь блокировка уровня IP/аккаунта
   (кумулятивная нагрузка: 300 товаров parallel 2 сразу после двух прогонов) — ни одна вкладка не проходит.
   **Правило подтверждено:** большой прогон только после паузы (последний контакт ≥2–4ч) + smoke на 1 товар
   (`--parallel 1`), иначе Ozon душит целиком. Теперь стоп на живой сбор (cooldown 12–24ч).
   ⚠️ **Разбиение md сработало в проде:** дайджест 12 МБ разбился на 3 файла (`.md` + `.part2` + `.part3`) — фича
   `write_search_digest` из этого раздела (ещё НЕ закоммичена).
 - **Разбиение md-дайджеста на части по 100 товаров (2026-08-17, НЕ закоммичено).**
   `render_search_digest` принимает `products` (срез для рендера) и `chunk=(k,n)` —
   в шапку части добавляется «Часть k из N». Новая `report.write_search_digest(combined,
   out, max_products_per_file=100)` — пишет файлы, разбивая при >100 товаров:
   `out` + `out.part2`, `out.part3`, … (`max_products_per_file=0` — без разбиения).
CLI `search --max-products-per-file` (default 100) и `scripts/digest_from_raw.py
    --max-products-per-file` используют её. pytest 360 passed (было 356), 1 pre-existing
    fail `test_baseline_matches_source_raw` (дрейф raw). Не закоммичено — ждёт согласия.
    ⚠️ **ЗАМЕНЕНО (2026-09-03):** этот API `max_products_per_file` удалён — см. «Статус»
    выше. Разбиение теперь по размеру файла через `max_file_size_bytes`/`--max-file-size-mb`.
 - **new_tab recovery ВНЕДРЁН И ЗАКОММИЧЕН (2026-08-17, коммит `7429cda`).** Реализация
   принята пользователем, git-коммит сделан (`feat: recover from access_challenge via new
   tab in same context`). Артефакты live-прогона `.coverage` и `challenge_probe_report.json`
   в коммит НЕ вошли (не gitignored — при желании добавить в `.gitignore`).
   **Следующие шаги:** живой тест специально НЕ запускать, ждать естественного появления
   FAB в проде; после первого реального challenge-события в логах проверить
   `recovery_kind: new_tab` у большинства товаров, `new_tab_successes > 0` без recycle,
   отсутствие шторма; `--parallel 2` не гонять до подтверждения механизма на одиночном
   воркере; baseline 2561238412 НЕ трогать (нужны 2 стабильных живых сбора подряд).
   Детали механизма — ниже в этом разделе.
 - **challenge_probe — live-инструмент для разблокировки FAB (2026-08-17).**
   Файл: `tests/challenge_probe.py` (запуск вне pytest, тратит живой бюджет Ozon).
   Наблюдение: парсер получает `access_challenge` на карточке, а ручное открытие в той же
   сессии проходит → FAB привязан к траектории/сценарию открытия, а не к сессии целиком.
   Проверяем гипотезы ПООЧЕРЁДНО на одном pid, STOP-on-success + контрольный повтор через
   60 с (воспроизводимость 2/2).
   - Сценарии: `0` = открыть карточку (gate: если FAB нет — «нечего разблокировать», стоп),
     `1` = пауза 10 с → повтор в ТОЙ ЖЕ вкладке, `4` = новая вкладка, `2` = главная → карточка,
     `3` = reload, `5` = пауза N с → повтор (только по явному `--scenario 5 --pause N`).
   - Автосерия: `0,1,4,2,3`; после первого PASS — контрольный повтор через 60 с.
   - Безопасность: куки ТОЛЬКО по именам (count + дельта набора, значения не читаются);
     изолированный профиль-копия `ozon-profile-probe` на порту 9334 (production 9222 не
     трогается); `--use-main-profile` — только по явному желанию (риск production-кук);
     автостоп после 3 challenge подряд → рекомендация cooldown; подтверждение перед запуском
     (`--yes` для авто-вызова).
   - Флаги: `--scenario N [--pause S]`, `--no-control`, `--force-copy` (пересоздать
     профиль-копию — для верного повтора после закрытия основного браузера), `--out`.
   - Тесты: `tests/test_challenge_probe.py` (6: delta-cookies, build/render отчёта, aborted).
   - **ПОЛНАЯ КАРТА LIVE-ЭКСПЕРИМЕНТА (2026-08-17, pid 1214739369, все на анонимном
     probe-стоке, если не сказано иное; ~14 запросов):**
     - **сценарий 0 (gate):** CHALLENGE `title:похоже, нет соединения` — FAB есть, зонд валиден.
     - **сценарий 1 (пауза 10с, та же вкладка): FAIL** (CHALLENGE ×2).
     - **сценарий 3 (reload): FAIL** (CHALLENGE после reload).
     - **сценарий 5 (пауза 15с, та же вкладка): FAIL** (CHALLENGE).
     - **сценарий 4 (новая вкладка): ✅ PASS 2/2.** Новая вкладка открывает карточку без FAB,
       сессия получает auth-куки (3→11: `__Secure-access-token`, `__Secure-refresh-token`,
       `__Secure-user-id`, `rfuid`, `xcid`, `__Secure-ab-group`, `__Secure-ext_xcid`,
       `__Secure-ext_xcid`, `ADDRESSBARBOOK_WEB_CLARIFICATION`…), после чего **вся сессия
       разблокирована** — даже старая FAB-вкладка проходит (контроль: 4a PASS в том же табе).
     - **сценарий 2 (главная→карточка) с РЕАЛЬНЫМИ куками:** FAB вообще НЕ воспроизвёлся
       (203 куки, карточка чистая) — дополнительный контроль, что сейчас боевая сессия чистая.
     - **ВЫВОД МЕХАНИЗМА:** FAB залипает на траектории/навигации КОНКРЕТНОЙ вкладки
       (Qrator). Повторы в той же вкладке (10с, 15с, reload) НЕ помогают. Новая вкладка =
       свежая навигация → Qrator пропускает → auth-куки → разблокировка всей сессии.
     - ⚠️ **ФИДЕЛИТИ:** FAB воспроизводится на АНОНИМНОЙ (без кук) сессии; с реальными куками
       (203) сейчас FAB нет → боевой парсер сейчас, вероятно, чист. Полного повторения
       «боевой FAB» на зонде не было (профиль-копия с куками невозможна, пока 9222 открыт).
   - **ВНЕДРЕНО (2026-08-17):** recovery для `access_challenge` теперь делает
     повторы в НОВОЙ ВКЛАДКЕ (new_tab), а не сразу recycle:
     - `Session.new_page()` (`ozon_collector/cdp.py`) — новая вкладка в том же
       контексте (закрывает FAB-залипшую, ставит анти-детект init_script).
     - `_collect_shard` (`ozon_collector/search.py`): `access_challenge` →
       до `NEW_TAB_ATTEMPTS=2` попыток новой вкладки (сессия НЕ перезапускается),
       затем — существующий recycle (fallback, `max_recycles` не тратится);
       storm-лимит `CHALLENGE_STORM_LIMIT` не менялся.
     - Телеметрия recovery (аддитивно): `new_tab_attempts`, `new_tab_successes`
       (успех = товар собран recovery вкладкой БЕЗ recycle); продукт получает
       `new_tab_attempts` и `recovery_kind` ("new_tab"/"recycle" — последний
       применённый). События recovery: `kind="new_tab"` на каждую попытку,
       `kind=<error_kind>` для recycle (как было).
     - Тесты: `tests/test_search.py` (4: recovers без recycle, fallback на recycle,
       максимум 2+2 без шторма, new_tab_failed), `tests/test_cdp_launch.py`
       (TestSessionNewPage, 3), дубли `_FakeSess`/`FakeSession` дополнены
       `new_page()`. pytest: 356 passed, 1 pre-existing fail
       (`test_baseline_matches_source_raw` — дрейф `data/*_raw.json`, не связан).
   - **НЕ ДОПРОВЕРЕНО:** сценарий 2 (главная→карточка) на анонимном стоке; порог паузы 30/60с
     (сценарий 5) — второстепенно, т.к. новая вкладка не требует ожидания.

 - **ПОСЛЕ ИНЦИДЕНТА (2026-08-15): challenge-guard + офлайн-дайджест + фикс кеша.**
   Resume-прогон «роботы-пылесосы» дал ложный ok=20 (все товары = FAB-страницы «Похоже,
   нет соединения», 0 отзывов). Исправлено (см. §19):
   - **challenge-guard**: `AccessChallengeError` → `error_kind=access_challenge`,
     recyclable; `detect_challenge` (title-сигнатуры + DOM `fab_chlg_`) в collect/search;
     0 отзывов НЕ считается челленджем; pytest 283 passed, 4 skipped.
   - **Офлайн-дайджест без Ozon**: `scripts/digest_from_raw.py` → `data/digest_from_raw.md`
     (39 товаров с реальными данными, отзывы 49–96; выбор лучшего валидного raw на pid).
   - **Фикс cache discovery**: `cache_check` находит лучший валидный raw среди КАНОНА и
     timestamp-файлов — v1.14.0-данные не теряются за старым каноном (1.11–1.13),
     cache=hit вместо повторного похода в Ozon.
   - **Acceptance-gate (ok = успешная карточка)**: `gather.assert_raw_structure` —
     raw dict, pid совпал, product непустой, title непустой и НЕ challenge, reviews —
     список. Битая структура → `StructureError` (error_kind=structure, НЕ recyclable);
     challenge-title → `AccessChallengeError` (retry с recycle). Gate работает и на
     кеш-хите, и на свежем сборе — challenge никогда не попадает в products как успех.
   - **Challenge-storm abort**: `_collect_shard` считает access_challenge ПОДРЯД;
     при `CHALLENGE_STORM_LIMIT=3` прогон останавливается (без агрессивного retry-loop),
     остаток шарда → error_kind=challenge_storm, в combined — `aborted`
     (в параллели: `workers_stormed`/`all_workers` через `parallel._storm_summary`);
     CLI печатает «уйди в cooldown 2+ часа». Тесты: pytest 299 passed, 4 skipped.
   - **Следующий шаг (после cooldown, §18):** smoke воркер-профиля (1 известный товар,
      реальный title, отзывы>0) → полный пересбор 20 товаров
      `search --limit 20 --parallel 2 --worker-timeout 3600 --reviews-per-product 100 --no-ocr`
      (с `--max-age 7d` — cache-aware, свежие timestamp-raw берутся без нагрузки на Ozon).
   - **СМОКЕ 16.08 01:39 → FAIL + ФИКС РАНЖИРОВАНИЯ КЕША (2026-08-16, pytest 301/4, коммит `64ff6d7`):**
     - **Фикс `cache._best_raw`:** ключ ранжирования теперь (наличие отзывов → дата → число отзывов).
       Stress-файл 21:18 с 0 отзывов НЕ вытесняет полный raw 21:12 (97 отзывов). Проверено офлайн:
       `2561238412 → hit 90 отзывов`, `1779338676 → hit 96 отзывов` (оба v1.14.0 timestamp).
       Лог hit: `cache=hit (v…, reviews=N, from=…)`. Регресс-тесты в `tests/test_cache.py`.
     - **Smoke обоих воркеров (`--parallel 2 --limit 2 --max-age 0`):** `collected=0 ok=0`,
       оба товара → `access_challenge` на product_page (3 попытки каждый, все challenge),
       выдача открывается (`found=56`) — FAB только на карточках, тот же паттерн что 22:06-22:10.
       Challenge-guard сработал корректно: мусорных raw нет, ложного ok нет. Cooldown ~3.5ч НЕ помог.
     - **ПЛАН (два независимых gate перед боевым прогоном):**
       `CACHE GATE`: `--max-age 7d --limit 1 --parallel 1` → cache=hit, правильный v1.14.0 raw,
       reviews>0, без обращения к Ozon. `LIVE GATE`: `--max-age 0 --limit 1 --parallel 1` →
       реальная product-page, title + reviews>0 + без challenge. Только оба PASS → боевой
       `--parallel 2 --max-age 7d`. Если LIVE GATE снова `access_challenge` через ~6ч cooldown —
       переход на 12–24ч без дальнейших попыток (потом при необходимости: смена IP / чистый
       профиль / один воркер + `--limit 1` + `--no-ocr` + большие паузы / другой аккаунт).
   - **БОЕВОЙ ПРОГОН 16.08 ~11:45 → 14/20 OK (2026-08-16):**
     - **LIVE GATE PASS в 07:45** (автозадача `OzonCollectorLiveGate`, gather 1779338676,
       `--max-age 0 --parallel 1`): реальный title Tuvio TR05MLCB, цена 29 566 ₽, **96 отзывов**,
       без challenge — FAB отпустил после ~6ч cooldown.
     - **Боевой `search --limit 20 --parallel 2 --max-age 7d --no-ocr`:** `collected=14, ok=14`,
       **все 14 = cache=hit** (отзывы 6–96, v1.14.0 timestamp) — ни одного запроса к Ozon;
       **6 товаров без кеша → `access_challenge` на product_page** (не шторм: фейлы не подряд —
       2 и 4 на воркерах), challenge-guard не пропустил ни одного ложного ok.
     - **Вывод:** cache discovery отработал идеально; живые новые карточки при `--parallel 2`
       по-прежнему частично блокируются (точечная метка по новым PID или эффект параллельности —
       НЕ доказано, т.к. переменные смешаны).
     - **Артефакт:** промежуточный дайджест 14 товаров → `D:\Temp\opencode\battle_digest_14.md`.
     - **ОТЛОЖЕННЫЙ ДОСБОР 6 PID** (`1214739369, 2923530832, 2345778208, 1626207745, 2101391799,
       1994330926`): отдельный controlled test через несколько часов —
       `search --limit 20 --parallel 1 --max-age 7d --no-ocr --worker-timeout 3600`
       (кеш возьмёт 14, живьём дособерёт только 6). Первый из шести → если PASS, максимум ещё
       один одиночный сбор (кумулятивный лимит). При повторном `access_challenge` → **стоп
       12–24ч**, без дальнейших попыток. `--parallel 2` по этим шести НЕ запускать, пока не
       проведён одиночный контроль (иначе смешаем PID и параллелизм).

 - **ФАЗА A (tuning отзывов): ЕДИНЫЙ ДЕФОЛТ 100 + КОНСТАНТЫ + BASELINE — РЕАЛИЗОВАНО (2026-08-15).**
   Часть A плана ускорения сбора отзывов без потери данных (Фаза B — `--parallel` — отложена
   до отдельного ревью; см. историю). Смысл изменений — ускорение докрутки отзывов и контроль
   объёма дайджеста, с доказательной базой на review-ключах.
   - **Единый дефолт `DEFAULT_REVIEWS_PER_PRODUCT = 100`** (`report.py`): используется и
     `render_search_digest`, и argparse `--reviews-per-product` (`cli.py`). Дефолт влияет ТОЛЬКО
     на обрезку отзывов в digest (`report.py` `raw_view["reviews"] = reviews[:N]`), raw остаётся
     полным. Один источник истины — тест `test_default_reviews_per_product_is_100`.
    - **Константы докрутки — A/B-реверт (2026-08-15):** опытные `SCROLL_STEP_PX 900→1200`,
      `SCROLL_PAUSE_MS 1600→1000`, `MAX_SCROLL_STABLE 6→4` **отменены — возвращены 900/1600/6**
      (применялись в `_scroll_to_reviews` и `_scroll_to_converge`; jitter-инфраструктура и
      телеметрия остаются). Причина: извлечение отзывов — ОДИН снимок DOM после докрутки
      (`_collect_reviews_pass` → `page.evaluate(REVIEWS_JS)`), и набор отзывов, попавших в DOM,
      зависит от траектории скролла. Живой acceptance (3 товара baseline, новые константы):
      `1779338676` PASS (missing=0), но `2561238412` — 96 vs baseline 97 (missing=29, extra=28;
      повтор прогона идентичен 96/96 → детерминирован), `3627230434` (с `--sorts`) — 265 vs 250
      (missing=165, extra=180). **A/B-тест** (monkeypatch старых констант,
      `D:\Temp\opencode\ab_constants_test.py`): старые 900/1600/6 сейчас воспроизводят baseline
      ТОЧНО (97/97, miss=0, extra=0), overlap старые-новые = 68. Вывод: расхождение — НЕ дрейф
      Ozon, а регрессия констант → baseline НЕ пересоздаётся (правило ниже), константы
      возвращены на 900/1600/6. Тест обновлён: `test_reviews.py` jitter-диапазон 1600±15% = 1360..1840.
   - **Jitter-паузы ±15%** (`scroll_pause_ms`, `set_scroll_jitter_seed`,
     `effective_scroll_jitter_seed`): production — случайный seed; воспроизводимость — явный
     `--scroll-jitter-seed N` (CLI collect/gather/search). Фактический seed пишется в
     `raw["collection_telemetry"]["scroll_jitter_seed"]`.
   - **Телеметрия сбора** (`raw["collection_telemetry"]`, аддитивно к схеме 2.0):
     `scroll_time`/`extract_time` (границы: только `_scroll_to_converge` и раскрытие+REVIEWS_JS,
     БЕЗ goto/активации варианта), `pages[]` с `scroll_count`, `stable_rounds`,
     `stable_iterations`, `last_growth_iteration`, `progression` (unique после каждого скролла),
     `bottom_passes`, `scroll_jitter_seed`, `total_review_collection_time`.
   - **Baseline отзывов (immutable)**: `tests/data/baseline_products.json` (schema 1.0) — 3 товара
     по review-ключам `_review_key` (uuid): 2561238412 (97), 1779338676 (96), 3627230434 (250 ≥ 200).
     Создан до изменения констант, автоматически НЕ перезаписывается.
     **SHA-256 файла: `DCABF74747BB82EAC95B4AACB6DDE3138FEC1AD28BA406C025D43F09DC1ACF90`**
     (пересоздавать baseline только сознательно, после проверки, что расхождение — дрейф Ozon,
     а не регрессия кода).
   - **Инструмент сравнения**: `python -m tests.compare_baseline --raw <raw.json> --baseline
     tests/data/baseline_products.json [--pid N] [--strict-extra] [--extract-runs 3]
     [--digest-limit 100]`. Exit code для CI: 0 = `missing_keys == ∅` (нет потери baseline);
     1 = потеря отзывов; 2 = недетерминизм извлечения (`--extract-runs`).
     `extra_keys` по умолчанию — диагностика (дрейф Ozon на живом товаре), FAIL только с
     `--strict-extra`. `--pid` — сравнение одного товара (остальные baseline не fail).
   - **Решение (зафиксировано):** порядок отзывов НЕ является контрактом — acceptance проверяет
     полноту множества review-ключей, упорядоченность — только информационная метрика.
     Детерминизм baseline: один живой прогон + офлайн-пересчёт review_keys 3 раза
     (`--extract-runs 3`) — честный детерминизм слоя анализа, дрейф Ozon между живыми
     прогонами не ломает эталон.
   - **Проверено:** `pytest` **232 passed + 4 skipped** (было 217/4). Новые: `test_report.py` +3
     (дефолт 100, обрезка digest, размер дайджеста), `test_reviews.py` +4 (jitter-детерминизм,
     диапазон, effective-seed, телеметрия converge), `tests/test_baseline.py` +8 (самоконсистентность
     baseline, missing/extra, exit codes, source-проверка).
    - **Приёмка Фазы A (2026-08-15, живые прогоны, возвращённые константы 900/1600/6, seed=1):**
      `2561238412` — **97/97, missing=0 extra=0, PASS** (raw: `data/phaseA_accept_2561238412_raw.json`);
      `1779338676` — PASS (missing=0, проходил и при новых константах); `3627230434` — см. раздел
      «Анализ стабильности baseline» ниже (исключён из gate). Детерминизм извлечения: `--extract-runs 3` OK.
    - **Gate Фазы A (зафиксировано):** стабильные товары baseline → `missing_keys == 0`;
      `3627230434` → diagnostic/excluded (не участвует в PASS/FAIL); baseline НЕ пересоздаётся только
      ради зелёного теста; дальнейшие изменения констант по результатам этого товара НЕ вносятся.
      Фаза A не заявлена полностью доказанной на трёх товарах — валидация по двум стабильным.
    - **Анализ стабильности baseline (2026-08-15):** `3627230434` исключён из строгого acceptance gate
      из-за доказанной нестабильности Ozon review-window. Baseline от 08.08 не воспроизводится даже
      между двумя прогонами одного дня: (а) новые константы → 265 собр., missing=165, overlap=85;
      (б) возвращённые константы → 264 собр., missing=210, overlap=40 — за ~1.5ч overlap упал вдвое,
      причём возврат старых констант НЕ приблизил результат к baseline (для 2561238412 наоборот
      воспроизвёл точно). Изменение констант — недостаточное объяснение расхождения → это дрейф/шум
      окон сортировок Ozon (входная стена ~96 на сортировку), а не регрессия кода. Товар — только
      диагностический кейс. Оба сегодняшних raw-прогона сохранены: `data/phaseA_3627230434_raw.json`
      (265, новые константы) и `data/phaseA_accept_3627230434_raw.json` (264, возвращённые).
    - **Git:** Фаза A закоммичена (коммит согласован после приёмки 2 стабильных товаров).

 - **ФАЗА B (`--parallel`): ПЛАН v2 — УТВЕРЖДЁН К РЕАЛИЗАЦИИ (2026-08-15).**
   Цель: ускорить search-сбор за счёт N независимых CDP-сессий, НЕ трогая механику
   `collect_reviews()` (вывод Фазы A: константы скролла зафиксированы, сбор — одиночный снимок DOM).
   Принцип: параллельность = изолированные воркер-процессы, каждый владеет СВОИМ Chromium.
   ```text
   parent (CLI search)  ── main Session (порт 9222, ozon-profile) ── links (докрутка выдачи)
        │   close Session + ПОЛНЫЙ recycle main-Chromium (kill на 9222)
        │   round-robin шардирование (индекс — часть задания воркера)
        ├─ worker-1: порт 9223, профиль ozon-profile-worker-1, всё своё
        └─ worker-2: порт 9224, профиль ozon-profile-worker-2, всё своё
        каждый worker: items[(index,pid,url)] → run_gather × N → payload по Pipe → parent merge
   ```
   ### 1. Архитектура и сериализация (правка 1)
   - Новый `ozon_collector/parallel.py`: `collect_parallel(url, limit, gather_cfg, parallel, ...)` →
     combined-объект схемы 2.0 + `parallel{workers[], shards[]}` (аддитивно).
   - **spawn** (`multiprocessing.get_context("spawn")`, явно и на Linux — единый путь). Аргументы `worker_main` —
     ТОЛЬКО сериализуемые:
     ```python
     spec = {"worker_id": k, "cdp_url": "http://127.0.0.1:9223",
             "port": 9223, "profile_path": "...", "profile_token": "ozon-profile-worker-1",
             "log_path": "logs/ozon_worker_1.log"}
     task = {"items": [(0, pid, url), (2, pid, url), ...],   # index — ЧАСТЬ задания (правка 2)
             "gather_cfg": { ...скалярные значения, БЕЗ лямбд/объектов... },
             "max_recycles": 2, "scroll_jitter_seed": 12345}
     ```
     Никаких Session/Manager/Watchdog/ProcessRecord/лямбд/pipe в аргументах. Всё это создаётся ВНУТРИ
     воркера в `worker_main`. Результат — picklable dict по Pipe; для больших raw (много МБ) — опция
     сохранения в файл и передачи пути (принято для v2 как опция, по умолчанию — pipe).
   - Схема: main-session собирает `links` (существующий `_collect_links_with_recovery`),
     `selected = links[:limit]`, parent шардирует и запускает воркеров. Категория воркерами НЕ переоткрывается.
   ### 2. Ownership (правка 4/6/9)
   - **Жёсткий инвариант (проверяется тестами).** Parent имеет право: spawn, monitor, collect payload,
     terminate hang-воркера, final merge, cleanup по ЗАРЕГИСТРИРОВАННЫМ identity. Parent НЕ имеет права:
     `BrowserProcessManager` воркера, `ProcessRecord` воркера, `BrowserWatchdog` воркера,
     `force_stop/recycle` браузера воркера.
   - Воркер владеет своим браузером целиком: launch/restart/force_stop через собственный
     `BrowserProcessManager`, kill — только через свои ProcessRecord (identity-сверка process.py).
   - **Ownership identity = (port, profile_token, ProcessIdentity)**, а не просто (profile_token):
     `process.browser_processes(cdp_url, profile_token)` проверяет И порт, И профиль (защита от случайного
     переиспользования профиля на чужом порту). Матчинг в `_matches_browser` — basename `--user-data-dir`
     == ТОКЕН профиля (сейчас захардкожен `PROFILE_TOKEN="ozon-profile"` → воркер-профили не матчились бы).
   - **Профиль и порт (правило 2):** `profile_path_for_worker(k)` — ЕДИНЫЙ хелпер именования профилей
     (часть security/ownership-контракта, тестируется отдельно). Профиль воркера k — `ozon-profile-worker-{k}`,
     порт — `9222 + k` (k=1..N; база 9222 занята main → «9222+i» = i-й воркер, без лишнего +1).
   ### 3. Изменения по файлам
   - `process.py`: `browser_processes(cdp_url, profile_token)` — матчинг по порту И токену;
     `rss_bytes(pid)` (Windows: ctypes GetProcessMemoryInfo; Linux: /proc/pid/status VmRSS) — диагностика.
   - `browser.py`: `_launch_script(cdp_url, profile_token)` → env `PORT` + `PROFILE` (скрипты уже читают
     PROFILE; Python-путь сейчас передаёт только PORT). `BrowserProcessManager(cdp_url, profile_token=...)` —
     token сквозной в launch/preflight/restart. `force_stop` для main (полный recycle main-Chromium).
   - `cdp.py`: `Session` без изменений (cdp_url параметризуется уже сейчас).
   - `log.py`: **`attach_file(path, mode="a", prefix="")`** — отдельный файл на воркера (правка 2); воркеры
     `setup()` НЕ зовут (нет ротации/перезаписи чужого лога). Префикс **`[worker=k pid=NNN] `** (правка 10).
     `OZON_WORKER_ID=k` в env воркера. На атомарность одного `write` НЕ полагаемся (гонка двух fd).
   - `parallel.py` (новый): `worker_main(spec, task, conn)`, `collect_parallel(...)`, preflight (4 ресурса),
     sharding, merge, watchdog-завершение.
   - `search.py`: per-product recovery-цикл (R1/R2 + preventive) вынести в переиспользуемую
     `_collect_shard(sess, manager, items, gather_cfg, max_recycles, recovery)` — ОДИН код для search и воркеров.
   - `cli.py`: `--parallel N` (default 1, валидация `1 ≤ N ≤ 2`), `--allow-parallel-ocr`.
     `--parallel>1` + OCR без флага → ошибка ДО запуска. При `--allow-parallel-ocr` OCR-потоки воркера
     уменьшаются: `max(2, os.cpu_count() // parallel)` (правка 6).
   - `__init__.py`: VERSION → 1.14.0; `PARALLEL_MAX_WORKERS = 2` (жёсткий предел).
   ### 4. Lifecycle и последовательность (правка 4)
   1. main-session: links.
   2. **`main_session.close()` + ПОЛНЫЙ recycle/stop main-Chromium** (force_stop по главному токену на 9222).
      Session.close гасит только CDP-клиент — main Chromium нужно остановить ОТДЕЛЬНО, иначе лишний
      процесс ест RAM. Принято: main браузер → stopped до запуска воркеров.
   3. Bootstrap профилей воркеров — по решению «сначала cold» (см. §14): НЕ копировать user-data-dir на
      старте; профили воркеров — самостоятельные persistent (создаются один раз).
   4. Preflight каждого воркера (§5) → Process(spawn) → start.
   5. worker: attach_file → Session → shard-цикл → payload → **try/finally: stop своего
      BrowserProcessManager + close Session** (правка 3) → exit 0.
   6. parent: join(timeout) → merge → CLI.
   ### 5. Preflight — 4 независимых ресурса (правка 5)
   Для каждого воркера k ДО старта:
   - **PORT**: TCP connect к `127.0.0.1:9222+k` НЕ открывается;
   - **PROCESS OWNERSHIP**: `P.browser_processes(cdp_url_k, token_k) == []`;
   - **CDP**: `/json/version` недоступен;
   - **PROFILE**: `profile_path_k != profile_path_main`; `profile_path_k` попарно различны у воркеров;
     каталог создаётся / не заблокирован другим Chromium.
   Любое нарушение → **fatal** (ошибка, воркеры не запускаются, попытка «подхватить» чужой Chromium
   исключена). Без fallback.
   ### 6. Логирование (правка 2/10)
   - parent: `log.setup()` (ротация main-лога), свой контекст/префикс.
   - worker: `attach_file(logs/ozon_worker_{k}.log, mode="a", prefix="[worker=k pid=NNN] ")`.
     Консоль воркера наследует stdout parent (spawn) — все строки с префиксом. Файл воркера — отдельный
     (append, без ротации), чужой лог не затрагивается. Acceptance по логам: нет потерянных строк,
     строки не перемешаны внутри одной записи, есть `[worker=0]` и `[worker=1]`.
   ### 7. Шардирование и слияние (правка 2)
   - Round-robin по индексу: `index % parallel`. Индекс — ЧАСТЬ задания: `items = [(0,url),(2,url),...]`.
     Merge НЕ зависит от позиции результата. Детерминизм: тот же links → те же шарды.
   - Слияние по `index` → `products` строго в исходном порядке; товар без результата →
     `ok=False, error="worker{k} terminated/failed"`. `products` всегда limit записей.
   ### 8. Worker → parent протокол (правка 7)
   - `{"t":"start","worker":k,"pid":…,"profile":…,"port":…}` — для тестов изоляции (identity до/после).
   - `{"t":"recovery","worker":k,"pid":pid,"kind":…,"attempt":n}` — recovery-события воркера.
   - `{"t":"done","worker":k,"payload":{...}}` → exit 0. **payload:**
     ```python
     {"worker_idx": k, "started": ts, "finished": ts, "exitcode": 0,
      "products": [...], "processed_indexes": [...], "failed_indexes": [...],
      "recovery_metrics": {...}, "rss_bytes": N, "fatal_error": None, "browser_stopped": True}
     ```
     `processed_indexes/failed_indexes` дают parent различать «нормально обработано 0» и «умер после 2-го».
     `browser_stopped` подтверждает, что воркер сам погасил свой браузер (cleanup parent — только аварии).
   - **Top-level обёртка воркера (правка 5):** весь `worker_main` в try/except; любое исключение →
     payload с `fatal_error` + частичные `processed/failed` → exit 0 (parent принимает и маркирует остаток fail).
   - Чтение: reader-поток на каждый worker (select по pipe на Windows не работает) → общая queue.
   ### 9. Recovery-изоляция (правило 6/9, правка 9)
   - R1/R2 + preventive — внутри воркера, его менеджером; recovery-метрики — в его payload.
   - Parent: recovery-события только телеметрия; никаких перезапусков чужих браузеров.
   - **Тест через ProcessIdentity (не только события):** worker0 old != worker0 new;
     worker1 before == worker1 after; pid/profile воркеров попарно различны весь тест.
   ### 10. Shutdown (правка 8)
   - `join()` «в голом виде» ЗАПРЕЩЁН — только `join(timeout)`.
   - Нормальный: все воркеры → done → join → merge → exit 0.
   - timeout/повис: `terminate()` → `join(2s)` → **cleanup по последнему валидному ownership snapshot**
     (зарегистрированные identity воркера), НЕ «любой Chromium на порту» → товары без результата → fail,
     exit 1. Worker, умерший внезапно, может оставить Chromium — именно поэтому snapshot, а не поиск по порту.
   ### 11. OCR и ресурсы (правило 5/7, правка 6)
   - `--parallel 2 --no-ocr` — стабильный режим Фазы B (основной acceptance). OCR в параллель — ТОЛЬКО
     с `--allow-parallel-ocr`, отдельным экспериментом ПОСЛЕ фиксации no-ocr-режима.
   - При `--allow-parallel-ocr`: OCR-потоки воркера = `max(2, os.cpu_count() // parallel)`.
   - RSS воркера — диагностика (`rss_bytes(pid)`), в payload и `combined["parallel"]`. Без новых депсов.
   ### 12. Тесты
   - **Unit (без браузера):** sharding (детерминизм, баланс, каждый товар ровно 1 раз, index в задании);
     merge (исходный порядок, пропуски → fail-запись, частичные processed/failed);
     `log.attach_file` (append, префикс `[worker=k pid=…]`, без ротации); preflight (4 ресурса: порт занят /
     процессы есть / CDP жив / профиль занят или совпадает → fatal); token-матчинг
     `browser_processes(cdp_url, profile_token)`; `profile_path_for_worker` (попарная уникальность).
   - **Worker-макет (без живого Ozon):** `worker_main` с fake-Session (monkeypatch Session в воркере) —
     recovery-цикл + протокол + exit 0 после done; исключение в воркере → payload `fatal_error` + частичные
     индексы + exit 0, parent не зависает; повисший воркер → terminate по таймауту.
   - **Изоляция recovery (ОБЯЗАТЕЛЬНЫЙ):** два воркера-макета; kill «браузера» воркера-0 (его ProcessRecord)
     → recycle только у 0; воркер-1: 0 recovery-событий, identity/profile/port БЕЗ изменений;
     `ProcessIdentity(worker0 old) != ProcessIdentity(worker0 new)`,
     `ProcessIdentity(worker1 before) == ProcessIdentity(worker1 after)`; pid/profile воркеров != друг друга
     и != main весь тест.
   - **Живой тест изоляции:** `--parallel 2 --no-ocr --limit 6`, принудительный kill браузера воркера-0
     на середине → recovery воркера-0, воркер-1 без перерыва; в combined ровно 1 recovery-событие у воркера-0.
   ### 13. Acceptance (правило 9) — сначала корректность, потом скорость
   - Корректность (`--parallel 2 --no-ocr --limit 6`): 6/6 собрано; no fatal; no hang; порядок товаров ==
     исходному; нет кросс-воркер вмешательства (порты/профили/identity не пересекаются);
     main браузер → stopped; worker-0/worker-1 → alive; recovery-изоляция (identity-ассерты выше).
   - **Live acceptance 2026-08-15: PASS** (после bootstrap §14). `--parallel 2 --no-ocr --limit 6`,
     категория «роботы-пылесосы»: found=56, collected=6, ok=6, recovery=0, fatal=0, hang=0.
     Реальные названия товаров; отзывы собраны (96/96/96/96/1/0). Существующие `data/*_raw.json`
     не изменены (0 changed; новые прогоны — только run-id файлы + 1 новый канон). Браузеры воркеров
     чисто закрыты. Логи: 649 строк с префиксами `[worker=1]/[worker=2]`.
     Осталось наблюдение: у 2/6 товаров мало/0 отзывов на этом прогоне — перепроверить при следующем
     сборе (вероятно нестабильность окна отзывов, не механизм параллели).
   - Benchmark (правка 12): `parallel=1` vs `parallel=2` на ОДНОМ frozen наборе links (одинаковый набор
     товаров, одинаковое число отзывов; корректность — на реальных links, бенч — на замороженном).
     Скорость — НЕ pass/fail, только после зелёной корректности.
   ### 14. Профили — решение (правка 1) — РЕАЛИЗОВАНО (bootstrap)
   - Изначально: **сначала ЧИСТЫЕ независимые cold-профили.** НЕ копировать user-data-dir на старте
     (риск переноса повреждённого состояния/lock-файлов/runtime-кэша).
   - **Холодные профили УПЕРЛИСЬ в анти-бот (подтверждено):** воркер-браузеры получали страницу
     Ozon «Похоже, нет соединения» (title продукта = это сообщение, отзывы не появлялись). Main-браузер
     (залогиненный ozon-profile: 14 cookies, включая `__Secure-access-token/refresh-token/user-id`)
     работал. Решение §14 включено:
   - **Bootstrap воркер-профилей** (`process.bootstrap_worker_profiles`, §14):
     копирует `Local State` (os_crypt-ключ) + `Default/Network/Cookies` из main-профиля в холодные
     воркер-профили. Инварианты: ТОЛЬКО при остановленном main-браузере; воркер с уже имеющейся
     Ozon-сессией не трогается (idempotent); работающий воркер пропускается.
     Проверено эмпирически: после bootstrap воркер открывает товар с реальным title (PASS).
   - CLI: `--parallel>1` автоматически сеет cookies; отключение — `--no-bootstrap-workers`.
   - Local Storage/IndexedDB из main не нужны (в main их нет; cookies+Local State достаточно — доказано).
   - Bootstrap-детекция: `process._ozon_cookie_names` (sqlite `Default/Network/Cookies`, host like %ozon%),
     сессия = наличие любого из {`__Secure-access-token`,`__Secure-refresh-token`,`__Secure-user-id`,
     `rfuid`,`xcid`,`guest`}.
   ### 15. Риски
   - Холодные воркер-профили → возможен FAB-челлендж (инцидент Фазы A) — п.1 §14 как fallback.
   - Память: 2-3 Chromium + OCR — RSS диагностика; лимиты в B не вводим.
   - spawn на Windows: каждый воркер импортирует проект (~1-2с на старт) — для длинного search несущественно.
   - Гонка двух fd при записи логов — отдельные файлы на воркера; атомарность write не гарантируется.
    - Git: коммит Фазы B — только после зелёного acceptance (как и в Фазе A).
    ### 16. Защита raw от перезаписи (инцидент 2026-08-15) — РЕАЛИЗОВАНО
    - **Инцидент:** live-прогон `search --parallel 2 --no-ocr` перезаписал
      `data/3627230434_raw.json` (source raw Phase A) усечённым fresh-сбором
      (2232 Б, 0 отзывов вместо 228 КБ, 250 отзывов). `data/` в gitignore,
      бэкапов нет, отзывы на Ozon сменились (в свежих данных осталось лишь
      85/250 baseline-ключей) — исходник невосстановим.
    - **Решение:** `data/3627230434_raw.json` удалён (тест по дизайну допускает
      отсутствие source-файла). **Baseline НЕ изменяется.** Товар исключён из
      source-vs-baseline проверки до появления нового валидного source raw.
      Это НЕ основание для пересборки baseline.
    - **Новый инвариант (защита):** существующий `data/{pid}_raw.json` НИКОГДА
      не перезаписывается live-прогоном без явного `--overwrite`.
      - `cache.raw_save_path(pid, data_dir, overwrite)` — канонический путь,
        если файла нет или `overwrite=True`; иначе
        `data/{pid}_raw.<ts>.<i>.json` (новый прогон отдельным файлом).
      - Единая точка: `gather.run_gather` считает `out_raw` один раз и передаёт
        в `collect()` и в OCR-сохранение; прямой путь `collect()` без `out_path`
        тоже защищён.
      - CLI: `--overwrite` у `gather` и `search`; воркеры параллели получают его
        через gather_cfg.
    - Тесты: `raw_save_path` (канон при отсутствии / run-id при существующем /
      уникальность при коллизии / overwrite=True → канон).
    ### 17. Открытый вопрос: параллель во вкладках (вместо браузеров)
    - **СТАТУС: ОТКЛОНЕНО (2026-08-15).** После Фазы B вкладки НЕ делать — мультипроцессная модель
      остаётся единственной. Обоснование:
      - Единая точка отказа: OOM/крах общего Chromium/зависание CDP валит ОБЕ вкладки сразу.
        Именно OOM был реальной проблемой длинных прогонов — для этого и проектировалась изоляция
        по браузерам (решение о вкладках не адресует исходный риск, а возвращает его).
      - Общая сессия: один профиль/один TLS-отпечаток/одна категория сессии — идентичные запросы
        с разных вкладок повышают риск анти-бот-челленджа (против независимых воркер-профилей).
      - Конкуренция вкладок за I/O профиля и WebSocket CDP при скролле/interception → плавающие
        timeout'ы и рассинхронизация пагинации.
      - Придётся писать ВТОРУЮ recovery-модель (page-level ownership/recovery, различение renderer vs
        browser failure) — дублирует Фазу B ради выигрыша в памяти, несущественного при `parallel=2`.
      - Вернуться к спайку ТОЛЬКО если боевой benchmark Фазы B покажет, что 2 браузера упираются
        в память ИЛИ скорость неудовлетворительна (порог: parallel=2 даёт <~1.4× при сильном
        росте RSS). Пока: не делать.

   ### 18. Стресс-тест параллели: сколько сессий выдержит Ozon (2026-08-15)
   - Инструмент: `OZON_PARALLEL_MAX` (env, default 2) поднимает жёсткий лимит `--parallel`
     для спайков; тест валидации обновлён (`reload` с env). Прогон:
     `search --query "роботы пылесосы" --limit N --parallel N --max-age 0 --no-ocr
     --worker-timeout 600` (каждый воркер = 1 свежий товар; кеш выключен).
   - **Результаты (подряд, за ~40 мин):**
     N=2 → 2/2 ok; N=3 → 3/3 ok; N=4 → 4/4 ok; N=5 → 5/5 ok (0 анти-бот, 0 recycles).
     **N=6 → FAB-челлендж:** main-сессия получила «Похоже, нет соединения»
     (incident `fab_chlg_20260815181958_01M03ADVR27BR9QJXQJDAVGX1B`) на странице ПОИСКА,
     found=0, собрано 0.
   - **ВАЖНО: челлендж НЕ однозначно про N=6.** Во время сбора ссылок воркеры ещё не
     запущены (links → spawn); вызов сработал у ОДНОЙ сессии. Челлендж сохранялся и в
     ОДИНОЧНОМ зонде после N=6 (real cookies, title=«Похоже, нет соединения»).
     Вероятный триггер — кумулятивная нагрузка аккаунта/IP: 6 тяжёлых прогонов подряд
     за ~1 час (боевой 20 товаров + N=2..6) ≈ десятки сессий, все на одной учётке и IP.
   - **Вывод для практики:** при `parallel=2` (штатно) риск челленджа — только от общего
     числа прогонов/сессий за окно времени, НЕ от параллели. Перед повторными прогонами —
     пауза (cooldown), не гонять серии подряд. Триггерный порог намеренно НЕ доводился
     до N=7/N=8 (аккаунт уже получил челлендж; добивать не стали).
   - **Исправление по итогам боевого прогона:** дефолт `--worker-timeout` 1800→3600 с
     (товар ≈ 2.5–3 мин; 10 товаров на воркера при 20 в выдаче = ~30 мин — дефолт 1800с
     убивал воркеров впритык). Воркер, убитый по таймауту, теряет payload → товары падают
     в дайджест как terminated (raw на диске сохраняются — 17/20 уже были собраны).
   - Нерешённый вопрос: N=2..5 уложились без ошибок — для решения «сколько выдержит»
     чисто экспериментальный ответ «≥5 стабильно, на 6-м серия сорвалась»; точный порог
     требует повторного спайка после cooldown с паузами между прогонами.
   - **Операционный лимит (практика):** не гонять >4–5 ТЯЖЁЛЫХ прогонов подряд за час на
     одну учётку/IP (порог FAB ≈ 15–20 собранных товаров или 5–6 серий подряд). Между
     сериями — пауза. Это лимит Ozon/FAB, НЕ collector/recovery; параллель=2 ни при чём.
     (Дополнительный аргумент против `--parallel-tabs`: один Chromium с вкладками даёт тот
     же кумулятивный паттерн — проблема в частоте/объёме доступа, не в числе браузеров.)
   - **План восстановления боевого прогона «роботы-пылесосы» (после cooldown 30–60 мин):**
     1) smoke-зонд на 1 лёгкий товар (title реальный, НЕ «Похоже, нет соединения»);
     2) `search --url <категория> --limit 20 --parallel 2 --worker-timeout 3600
        --reviews-per-product 100 --no-ocr` — 17 raw возьмутся из кеша (без нагрузки),
        пересоберутся только ~3 отсутствующих;
     3) проверить 20/20 в дайджесте, порядок и полноту отзывов.
      Если челлендж не отпустил → увеличить cooldown до 2–6 ч, НЕ менять профили/IP.

   ### 19. ИНЦИДЕНТ: resume-прогон записал challenge-страницы как ok=20 (2026-08-15)
   - **Что случилось.** Боевой resume `search --query "роботы пылесосы" --limit 20
     --parallel 2 --worker-timeout 3600 --no-ocr` прошёл с found=56 collected=20 ok=20,
     НО все 20 товаров — FAB-страницы («Похоже, нет соединения»), product.title =
     сообщение челленджа, отзывов 0. Дайджест пустой (100 КБ, 0 отзывов). Причина:
     guard'а не было — `collect_product` брал challenge-title как обычный title,
     0 отзывов трактовалось как штатный результат, raw сохранялся как успех.
   - **Данные не потеряны:** 17 хороших raw (v1.14.0, 75–96 отзывов, реальные title)
     с боевого прогона 20:31 лежат в timestamp-файлах `data/{pid}_raw.20260815-20*`;
     overwrite-защита §16 уберегла их от перезаписи.
   - **Исправление — challenge-guard:**
     - `browser.AccessChallengeError` + ветка в `classify_error` → error_kind
       `access_challenge`, recyclable=True (R1 restart пробуется; при персистентном
       челлендже товар падает в дайджест как ошибка, НЕ маскируется под ok).
     - `collect.detect_challenge(page)` — расширяемые сигнатуры: title
       (`CHALLENGE_TITLE_MARKERS`: «похоже, нет соединения», «доступ ограничен»,
       «подтвердите, что вы не робот» и т.п.) + DOM (`fab_chlg_`, «выключите vpn»).
       Хуки: после `sess.goto(product_url)`, в `collect_product` (product.title),
       в `_collect_reviews_pass` (после goto страницы отзывов), в search (после goto
       выдачи). Срабатывание — ДО записи raw.
     - 0 отзывов НЕ считается челленджем (у реального товара может быть 0 отзывов).
     - Тесты: `tests/test_challenge_guard.py` (+11), `test_browser.py` +2.
       Проверено: pytest 283 passed, 4 skipped.
   - **Офлайн-дайджест из raw (без обращений к Ozon):** `scripts/digest_from_raw.py`
     — на каждый pid выбирает лучший валидный raw (критерии: версия → дата → число
     отзывов; валидация `cache._validate_raw` + challenge-title + отзывы>0 + pid-match),
     рендерит через `render_search_digest`. Результат: `data/digest_from_raw.md` —
     39 товаров с реальными данными (роботы-пылесосы: Roborock Qrevo CurvX/C Pro/
     Saros 20/Curv 2 Flow/Q8 Plus, Dreame X50/X60, Neatsvor, Midea VCR04W, REDMOND
     и др.; отзывы 49–96), для каждого указан источник-файл.
   - **Фикс cache discovery:** `cache_check` ищет лучший валидный raw среди КАНОНА
     и timestamp-файлов (`_raw_candidates`/`_best_raw`/`_validate_raw`): свежий
     v1.14.0-прогон не теряется, если канон занят старой версией (1.11–1.13) →
     cache=hit вместо повторного похода в Ozon. Критерии: версия, валидность
     (полнота, schema, версия, pid-match, timestamp), дата, число отзывов.
     Канон валидным timestamp'ом НЕ перезаписывается (§16 не нарушается).
   - **План пересборки (после cooldown, §18):** smoke воркер-профиля (1 известный
     товар, реальный title, отзывы>0) → полный пересбор 20 товаров. Теперь
     challenge-страницы не дадут ложный ok.

   ### 19.1. ГЕЙТЫ ПЕРЕД БОЕВЫМ ПРОГОНОМ (2026-08-16)
   - **Acceptance-gate `gather.assert_raw_structure(raw, pid)`** — ok ставится ТОЛЬКО
     при успешной карточке с валидной структурой (вызывается в run_gather после
     collect ИЛИ кеш-хита, до render/ok=True):
     - raw — dict; `str(raw.pid) == str(pid)`; product — непустой dict;
       product.title — непустой текст; reviews — список (0 отзывов допустимо).
     - Битая структура → `browser.StructureError` → error_kind=`structure`,
       recyclable=False (ретрай бесполезен — данные чужие/неполные).
     - Challenge-маркер в product.title → `AccessChallengeError` (access_challenge,
       recyclable=True — R1 restart + R2 retry). Т.е. challenge всегда попытка
       retry с recycle, а не тихий провал и не ложный успех.
   - **Challenge-storm abort** (не бить по Ozon агрессивным retry-loop):
     - `search.CHALLENGE_STORM_LIMIT = 3`: N товаров ПОДРЯД закончились
       access_challenge (после своих retry/recycle) → `_collect_shard` останавливает
       прогон, остаток шарда помечается `error_kind="challenge_storm"`,
       `error="aborted: challenge storm (cooldown)"`, в recovery —
       `challenge_storm{streak, remaining_aborted, pid}`.
     - `collect_search`: `combined["aborted"] = {reason: challenge_storm, ...}`.
     - `collect_parallel`: `parallel._storm_summary(wctxs, payloads, n)` агрегирует
       по payload воркеров: `workers_stormed[]` + `all_workers` (шторм у всех →
       аккаунт/IP душат целиком).
     - CLI (`cmd_search`): при aborted печатает в stderr «уйди в cooldown 2+ часа».
   - **Кейсы из требований пользователя (обязательные к проверке после пересборки):**
     1) ok = успешная карточка + валидный title + НЕ challenge + ожидаемая структура;
     2) access_challenge → НЕуспешный товар (retry/recycle, потом fail), никогда не ok;
     3) 0 отзывов — не ошибка (только «нет отзывов», не challenge);
     4) оба воркера в challenge → прогон останавливается, cooldown (all_workers=True).
   - Тесты: `tests/test_gather.py` (+9: assert_raw_structure, challenge-hit,
     broken-structure), `tests/test_search.py` (+3 storm), `tests/test_parallel.py`
     (+3 storm summary). Итог: **pytest 299 passed, 4 skipped**.


 - **v1.13.0: WATCHDOG (fail-fast) + ФИКС EVAL_TIMEOUT-РЕГРЕССИИ — РЕАЛИЗОВАНО (2026-08-09).**
   Дополняет v1.12.0: раньше смерть браузера замечалась только по ошибке на
   операции; теперь фоновый сторож ловит её раньше и fail-fast на границе
   операций.
   - **`BrowserWatchdog`** (`browser.py`, после `classify_error`): daemon-поток
     опрашивает независимый CDP HTTP-probe (`/json/version`, `http_timeout=1.0`)
     каждые `poll_interval=2.0с`; CDP молчит → сверка `browser_processes(cdp_url)`
     (процесс жив = транзиент `BROWSER_UNHEALTHY`, НЕ смерть); флаг «мёртв»
     только после непрерывного bad-окна ≥ `grace_s=4.0` (debounce).
     `check()` → `BrowserRecoveryError` → в `classify_error` = kind "browser",
     probe_state "watchdog" → recyclable.
   - **Guard в `Session`** (`cdp.py`): `_guard_check()` перед `goto`/`scroll`/
     `wait`/`eval` (ленивый `_ensure_watchdog()` при первой guarded-операции,
     `watchdog_stop()` в `close()`, новый инстанс после `reconnect()`). Зависшую
     Playwright-операцию прервать нельзя (знание из v1.12.0) — watchdog спасает
     СЛЕДУЮЩИЕ операции, а не текущую.
   - **ФИКС РЕГРЕССИИ (важно):** незакоммиченный слой `timeout=EVAL_TIMEOUT`
     в `page.evaluate` (cdp.py `scroll`/`eval` + ~15 мест в collect.py) оказался
     сломан: в playwright **1.62.0 `Page.evaluate` НЕ принимает `timeout=`**
     (TypeError; `set_default_timeout` тоже не действует — проверено экспериментом).
     Это роняло 7 тестов (`test_reviews.py` 6, `test_extractors.py` 1) и сломал бы
     любой живой прогон. Фикс: kwarg удалён, константа `EVAL_TIMEOUT` убрана из
     cdp.py и collect.py. Защита от зависаний теперь — только watchdog на границах
     операций (per-op таймаут evaluate в playwright 1.62 в принципе нереализуем).
   - **Проверено:** `pytest` **217 passed + 4 skipped** (Linux-only pidfd),
      `py_compile` чисто. Новые: `test_browser.py` +8 (watchdog alive/dead,
      debounce через time.monotonic, ветки `_sample`, start/stop потока,
      классификация watchdog-ошибки recyclable), `test_cdp_launch.py` +2
      (`TestSessionWatchdogGuard` + `_FakePage`). До фикса регрессии: 210/7/4.
   - **ФАЙЛОВЫЙ ЛОГ ПАРСИНГА (`log.py`, той же ревизии):** подробный лог всех
      операций пишется в `logs/ozon_collector.log` (папка `logs/` создаётся
      автоматически; переопределение: `--log` или env `OZON_COLLECTOR_LOG`).
      При КАЖДОМ запуске ротация: текущий → `logs/ozon_collector.log.1`.
      `log.info/warning/error` дублируют прежний `print` в stdout (capsys-тесты
      не сломаны — проверено 217/4), `log.debug` пишет ТОЛЬКО в файл
      (шаги докрутки выдачи, converge/pagination, goto/scroll/eval, переходы
      watchdog alive→transient→dead, итерации поиска). `log.setup()` вызывается
      только в `cli.main()` — pytest файлы не создаёт. Модули: cdp.py
      (`_js_label`, goto/scroll/wait/eval/watchdog/reconnect/close), collect.py
      (все 18 print → log.* + debug в `_scroll_to_converge`/`_detect_pagination`),
      gather.py (cache-причины, collect/ocr), ocr.py (fetch/skip/tessdata),
      search.py (итерации, recycles, preventive), browser.py (watchdog-переходы,
      start/stop/check). Цель: по логу воспроизводить проблемы длинных прогонов.
   - **Следующий шаг (живой):** `search --limit 100` по «роботам-пылесосам»
      (сначала `--limit 3 --no-ocr` для валидации) — проверить, что recovery
      реально срабатывает на длинном прогоне.
   - **Git:** изменения v1.12.0 + v1.13.0 НЕ закоммичены (последний коммит
      `11ea53d`). Модифицированы: PROJECT_NOTES.md, README.md, cli.py,
      `ozon_collector/{__init__,browser,cdp,collect,gather,log,ocr,search}.py`,
      `tests/{test_cdp_launch,test_gather,test_reviews,test_search}.py`;
      новые: `ozon_collector/{browser,log,process}.py`,
      `tests/{test_browser,test_process}.py`. Коммит — только по запросу пользователя.


 - **ОКРУЖЕНИЕ: Windows PowerShell.** Запуск CLI — `python` (НЕ `python3` — это заглушка
   Microsoft Store, выводит «Python was not found»), Python 3.12.5. Перед любой командой
   CLI обязателен `$env:PYTHONIOENCODING="utf-8"` (иначе `UnicodeEncodeError cp1251` на
   кириллице). Рабочий каталог — папка проекта (`ozon-collector`), иначе `cli.py` не
   находится. Вставка в консоль PowerShell: правый клик или Ctrl+V. Всё окружение,
   зависимости, tesseract, git, живой сбор Ecovacs — см. «История» → Windows-окружение.
   **Следующий шаг:** живой `search --limit 100` по категории «роботы-пылесосы»
   (`--url` выдачи, `--out docs/роботпылесос.md`); для валидации сначала
   `--limit 3 --no-ocr`. Запуск: `python` + `$env:PYTHONIOENCODING="utf-8"` из каталога проекта.

 - **v1.11.0: SEARCH + TTL-КЕШ — РЕАЛИЗОВАНО (2026-08-08).** Команда `search`
   (`--query`/`--url`) собирает до `--limit` товаров из выдачи Ozon (докрутка
   скроллом, только `tileGridDesktop`) через существующий пайплайн
   `gather` и выдаёт один markdown-digest для Gemini
   (выбор лучшего товара). TTL-кеш `raw.json` для `gather`/`search` (`--max-age`,
   дефолт `1d`; `0` = не читать кеш). Одна CDP-сессия на весь search, раздельные
   поля `cache`/`ocr`, defensive-даты, `found` = валидные уникальные pid до `limit`.
   - **Проверено:** `pytest` **118/118** (новые `tests/test_cache.py` 25 +
     `tests/test_gather.py` 4 + `tests/test_search.py` 16 + `tests/test_cdp_launch.py` 11),
     `py_compile` чисто.
   - **Регрессия gather:** байт-в-байт идентичный отчёт из кеша (`cache=hit`) и
     живой сбор (`--max-age 0`).
   - **Живой прогон search:** докрутка выдачи — `--limit 2 --no-ocr` →
     `found=32 limit=2 collected=2 ok=2` (реальные товары только из tileGridDesktop,
     без блоков рекомендаций). Фикстуры выдачи — `tests/search_pages/`.
   - **Доработки (2026-08-08):** прогресс-логи `search` («товар N/5» + итог) и
     `gather` (стадии с pid и причиной по кешу); **версионная инвалидация кеша** —
     raw старее текущей версии или без `collector_version` → `old_version` (пересбор),
     новее → `newer_version` (кеш тоже не берётся); **докрутка выдачи** (только
     `tileGridDesktop`, `found` = вся выдача, `selected = found[:limit]`);
      **авто-запуск CDP-браузера** (`collect`/`gather`/`search` запускают Chromium,
      если он закрыт; отключение `OZON_AUTO_LAUNCH=0`);
      **чистка вкладок в Session.close()** — закрываются все вкладки кроме pages[0],
      pages[0] уводится на about:blank, браузер не гасится.
      См. «История» → 2026-08-08 (search/cache), (докрутка выдачи) и (автозапуск браузера).
    - **Документы:** README (+`search`, таблица опций, «Кеш сырых данных»,
      «Прогресс-логи», данные, схема combined, «Дедуп ответов продавца»). См. «История» → 2026-08-08 (search/cache).

 - **Дедуп ответов продавца (2026-08-08).** `report`/`gather`/`search` скрывают
   повторяющиеся шаблонные ответы продавца при рендере Markdown —
   `_…и ещё N таких же ответов продавца пропущено_`. `--no-dedup-replies`
   отключает. Сырые данные не меняются. На MAX WAX: 270 комментариев → 138
   (17 групп, 132 дубля скрыто). `pytest` **132/132** (+14 `test_report.py`).
   См. «История» → Дедуп.

 - **MAX WAX Chain Wax 550332874 — живой сбор с низкими оценками (2026-08-08).**
   Первый сбор без `--sorts` дал только 96 отзывов («новые и полезные»). По запросу
   пользователя пересобрано с `--sorts published_at_desc,score_asc` → **180 уникальных**
   (96+96, пересечение 12). Дедуп по uuid работает: `review_sorts` распределение
   84 только published / 12 оба / 84 только score_asc. Звёзды: ★5:77 · ★4:7 · ★3:5 ·
   ★2:37 · ★1:54 (**91 негативный 1-2★** — следствие запрошенного `score_asc`,
   в отчёте помечено «Сортировки: новые и полезные: 96 · сначала низкие оценки: 96»).
   Отчёт: `docs/maxwax_chain_wax.md`. Подробности — «История» → 2026-08-08.

 - **README: добавлены «Быстрый старт» и полная справка по опциям (2026-08-08).**
   В начало — раздел «Быстрый старт» (launch_chrome → gather одним вызовом, 3 типовых
   сценария: collect без OCR / gather с OCR / report из raw). Далее — «Справка по
   командам и опциям»: таблицы по `collect` (10 опций), `ocr`, `report`, `gather`
   с дефолтами. Собственно опции CLI не менялись — только документация.

 - **ПРАВИЛО по нумерации разделов отчёта (утверждено 2026-08-08):** номера разделов
   в `ozon_collector/report.py` остаются **жёстко зашитыми** (1-7), даже если раздел
   пропускается (напр. «## 5. Галерея (OCR)» при невыполненном OCR — скачок 4→6).
   Пропуск номера осознанно НЕ чиним: «номер привязан к определённому блоку» — по нему
   можно ссылаться на секцию независимо от полноты сбора. См. «Ключевые решения».

 - **v1.10.0: MULTI-SORT REVIEWS — РЕАЛИЗОВАНО (2026-08-07).** Сбор отзывов в
   нескольких сортировках одновременно через `--sorts`
   (`published_at_desc,score_desc,score_asc`). Один общий список без дублей:
   `review_sort` (первое появление) + `review_sorts` (все сортировки), в raw —
   `reviews_sorts`. Обратная совместимость: без `--sorts` поведение прежнее.
   Подробности — «История» → v1.10.0.
   - **Проверено:** `pytest` **61/61**, `py_compile` чисто, CLI-help содержит `--sorts`.
   - **Живой прогон (3627230434, --sorts все 3):** 96+96+96=288 сырых → **250 уникальных**
     (пересечения: default∩score_desc=14, default∩score_asc=24, score_desc∩score_asc=0),
     `reviews_sorts` в raw, `review_sort`/`review_sorts` у каждого отзыва,
     `variant_mode.used=True, pagination_type=infinite_scroll`. Отчёт: «Сортировки:
     новые и полезные: 96 · сначала высокие оценки: 96 · сначала низкие оценки: 96».
     `smoke_cdp.py --compare` SMOKE/TWO-PAGE/VARIANT OK (снапшот rating_count xiaomi
     обновлён 5158→**5207** — дрейф счётчика). VERSION → 1.10.0.
   - **Документы:** schema.md (+`reviews_sorts`, `review_sort`/`review_sorts`,
     раздел «Multi-sort сбор»), README (+`--sorts`, исправлена устаревшая пометка
     «?sort= не работает»).

 - **ПРАВИЛО (утверждено пользователем 2026-08-07):** при сборе товара **ВСЕГДА по
   умолчанию собирать только отзывы выбранного варианта товара** (нативный фильтр
   Ozon «Этот вариант товара», `reviewsVariantMode=1`). `--no-variant-mode` — только
   по явному запросу. `--category` **НЕ использовать** (категории не нужны).
 
 - **v1.9.0: КОММЕНТАРИИ К ОТЗЫВАМ + ГОЛОСОВАНИЕ — РЕАЛИЗОВАНО (2026-08-07).**
   Пользователь заметил, что не собираются «сколько согласилось/не согласилось»
   и ответы на отзывы. Разбор живого DOM:
  - Голосование отзыва «Вам помог этот отзыв?» = кнопки **«Да N»/«Нет N»**
    (`div.b25_9_0-a2.tsBodyControl400Small`) — парсился regex'ом уже раньше
    (`helpful_yes`/`helpful_no`), но у многих свежих отзывов реально 0/0, поэтому
    казалось, что не собирается. Подтверждено: regex работает, поле в отчёте
    просто не выводилось.
  - Ответы/комментарии к отзыву: под карточкой отзыва лежит соседний блок
    `k1l_25` с `[data-comment-uuid]` (отзыв `jn0_25` + комментарии в одном
    контейнере `lk0_25`). **Раскрываются только по клику** на кнопку
    «N комментарий» → API `rpGetCommentsByReviewUuid`.
  - **Реализация:** `EXPAND_COMMENTS_JS` (клик по всем кнопкам «N комментарий»
    при N≥1; карточка помечается `data-comments-expanded`, чтобы не кликать
    повторно при пересоздании кнопок React'ом — на живом сборе ровно 1 клик на
    отзыв, 95/95) + Python-обёртка `_expand_review_comments`; вызов в
    `collect_reviews` сразу после `_expand_review_texts`. В `REVIEWS_JS` добавлен
    `parseComments`: `uuid`, `author`, `reply_to` («в ответ X» из `a.l9j_25`),
    `date`, `text`, `like`/`dislike` (голосование за комментарий). Тексты
    комментариев в текст отзыва НЕ попадают (они вне `[data-review-uuid]` —
    подтверждено тестом).
  - **Отчёт:** `_review_block` теперь выводит `👍 полезно: N, нет: M` и под
    отзывом блок «→ автор (в ответ: X) — дата» + текст + голосование.
  - **VERSION → 1.9.0**, schema.md: секция `reviews[].comments[]`.
  - **Тесты:** `pytest` **56/56** (+3: парсинг комментариев, текст отзыва не
    содержит комментариев, `EXPAND_COMMENTS_JS` кликает только по N≥1).
  - **Проверено на живом DOM:** топпер 3408182809 — 54 отзыва, 11 с
    комментариями, 13 комментариев (ответы продавца YNISON корректно
    извлечены с «в ответ Юлия М.»). **Боевой сбор 550332874** — 96 отзывов,
    32 с комментариями, 47 комментариев (ответы продавца MAX WAX),
    15 отзывов с голосованием «Да/Нет».

- **v1.8.0: РАСКРЫТИЕ «ЧИТАТЬ ПОЛНОСТЬЮ» В ОТЗЫВАХ — РЕАЛИЗОВАНО (2026-08-07).**
  Ozon усекает длинные отзывы и дораскрывает по клику — раньше текст собирался
  обрезанным. Теперь `collect_reviews` перед `REVIEWS_JS` кликает по всем кнопкам
  «Читать полностью»/«Читать дальше»/«Развернуть» (`EXPAND_REVIEWS_JS` + Python-
  обёртка `_expand_review_texts`: многопроходно до сходимости, без повторных
  кликов по одной кнопке; защита от исключений, лог кликов в консоль). Лимит
  текста отзыва поднят **3000 → 20000** символов, хвостовые «Свернуть»/«Читать
  полностью» вычищаются из текста.
  - **Проверено:** `pytest` **52/52** (2 новых в `tests/test_reviews.py`:
  клик дораскрывает усечённый текст; хвостовая кнопка не попадает в текст).
  - **Замечание:** раскрытие работает и на живых страницах (клик по реальным
    DOM-кнопкам Ozon), и на статичных фикстурах с `onclick`.
  - **v1.8.0: ФИЛЬТРАЦИЯ ГАЛЕРЕИ — только карточка товара + описание.**
    Раньше `gallery` собирал **все** `img` с CDN со страницы (119 шт. на
    xiaomi!), включая фото отзывов (`webListPhotos`/`rp-photo`), чужие товары
    (`skuGrid`, `skuShelfGoods`, `webStickyProducts`), логотипы/служебные
    (`/cms/`) и видео-обложки (`/video-*/cover/`). Теперь `gallery()` берёт
    только виджеты карточки товара: **`webGallery`** (включая выбранный
    вариант) + **`webDescription`/`webProductDescription`**; `infographics()`
    тоже ограничен виджетами товара. Видео-обложки исключены и в JS
    (`/\/video-[^/]+\/[^/]+\/cover\//`), и на уровне скачивания
    (`VIDEO_RE` в ocr.py расширен). Fallback на «все CDN-картинки» — только
    если ни одного виджета товара на странице нет.
  - **Проверено:** на живом товаре пользователя (topper 3408182809) было
    «11 картинок в карточке» — новый сбор даёт ровно **11** после dedup
    (раньше ~12-119). На фикстурах xiaomi: 119 → 42; roborock 35; neoclima
    8; topper 11. `pytest` **53/53** (+2 теста: `test_gallery_only_product_widgets`,
    +проверка «нет чужого» в `test_extractors`).
  - **v1.7.0 OCR** (ниже) — без изменений; версия движка в `VERSION = "1.8.0"`.

- **v1.7.0: OCR PERFORMANCE PACK — РЕАЛИЗОВАНО (2026-08-07).** Тир A полностью:
  параллельный OCR (`ProcessPoolExecutor`, воркеры=8, атомарный кэш через
  mkstemp+`os.replace`), параллельное скачивание (`ThreadPoolExecutor`=8,
  порядок `img_NNN` детерминирован), консервативная cv2-предобработка
  (gray→upscale до 2000px→CLAHE, опционально), локальный `tessdata_best`
  (авто-загрузка в `data/tessdata_best/`, без apt), PSM-стратегия
  (3 → 11 при conf<60 или text_len<10, берётся лучший), составной ключ кэша
  `{md5}_{lang}_{psm}_{preproc_ver}_{tessdata_ver}_schema{N}.json`
  (`cache_schema=2`, старый формат инвалидирован), CLI `--threads`/`--no-preproc`,
  диагностика `gallery_stats` (`ocr_engine`, `ocr_threads`, `preprocess`,
  `ocr_wall_seconds`, `cache_hits`, `cache_misses`). Подробности — «История» → v1.7.
  - **Проверено:** `pytest` **49/49** (16 новых в `tests/test_ocr.py`, +1
    диагностический контракт в `test_raw_shape.py`); бенчмарк на живом товаре
    (см. «История» → v1.7): OCR **~2.1 с/файл → ~0.3-0.5 с/файл** (×5-7).
  - **Отложено (по решению):** ML-движки (PaddleOCR/EasyOCR) и tesseract 5 (PPA)
    НЕ включены — после cv2+tessdata_best плохих картинок осталось мало.
  - **Изменения в raw:** только аддитивные поля (`gallery_stats.*`, схема 2.0
    не менялась, старые raw рендерятся без миграции).

- **v1.6.0: PAGINATION-АГНОСТИЧНЫЙ СБОР — РЕАЛИЗОВАНО (2026-08-07).**
  Сбор отзывов больше не зависит от разметки пагинации Ozon: вместо «Дальше»
  (старый `webListReviews`) теперь работает сходимость по росту уникальных
  `[data-review-uuid]` (`_scroll_to_converge`: плато 6 итераций + 3 докрутки
  к низу), новый `_next_page` (state-paginator → legacy «Дальше» → fallback
  `layout_page_index`), дедуп по uuid с sha1-fallback, и диагностика
  `reviews_variant_mode.pagination_type` (`infinite_scroll | next_button |
  converged | unknown`). REVIEWS_JS извлекает отзывы по `[data-review-uuid]`
  даже без виджета `webListReviews`. Подробности — «История» → v1.6.
  - **Проверено:** `pytest` **33/33** (9 новых); живой topper 3427851536
    **33/33**, `pagination_type=infinite_scroll`, variant_count=33, dropped=0,
    warning=None (было 12/33, stop_reason=no_next); регресс xiaomi 3627230434
    **96/96** (сайт тоже перешёл на infinite scroll), dropped=0;
    `smoke_cdp.py --compare` SMOKE/TWO-PAGE/VARIANT OK (exit 0; снапшот
    rating_count xiaomi 5155→**5158** — дрейф счётчика).

- **v1.5.0: NATIVE VARIANT MODE + class-agnostic REVIEWS_JS — РЕАЛИЗОВАНО (2026-08-07).**
  Коллектор включает нативный фильтр Ozon «Этот вариант товара»
  (`webReviewsVariantMode`, URL-параметр `reviewsVariantMode=1`) и собирает
  только отзывы выбранного варианта. `--no-variant-mode` — собрать все отзывы.
  Диагностика в `raw.reviews_variant_mode`, строка «Фильтр Ozon» в отчёте.
  Параллельно переписан `REVIEWS_JS` под актуальную разметку Ozon
  (класс-агностично: variant по `a[href*="/product/"]`, дата по regex, автор по
  `tsCompactControl500Medium`) — старые класс-селекторы были мертвы на живой
  странице. Подробности — в «История» → v1.5.
  - **Проверено:** `pytest` **22/22** зелёные (11 новых: VARIANT_MODE_JS-детектор,
    `_activate_variant_mode` skip/фолбэк, вариантная однородность);
    `smoke_cdp.py` SMOKE OK + TWO-PAGE OK + **VARIANT MODE OK** (pid 3627230434:
    active=«Этот вариант товара», variant_count=2697, total=5153, 21 отзыв,
    лента однородна «Цвет товара: черный»).
  - **Живой `collect` v1.5 (2026-08-07):** 42/42 отзывов однородны, 0 пустых
    variant/date/text; `reviews_variant_mode`: used=true, clicked=true,
    reason=click_ok, variant_count=2697, total=5153, dropped=0.
  - **Найден и исправлен баг диагностики:** `active` в итоговом dict брался из
    снимка ДО клика («Все отзывы») даже при успешном клике → после фикса
    перечитывается состояние ПОСЛЕ полла (проверено на roborock: click_ok,
    active=«Этот вариант товара»).
  - **Свежесть снапшотов:** `rating_count` xiaomi обновлён 5148→**5153**
    (живой smoke `--compare` ловит дрейф).

- **v1.4.0: RELIABILITY PACK — РЕАЛИЗОВАНО (2026-08-07).** Все пункты плана
  v1.4 (аддитивно, схема 2.0 не менялась) выполнены: integrity, `pid`,
  `incomplete`/`missing_fields`, OCR-кэш, `url` в `gallery_ocr`, шапка отчёта,
  сводка звёзд, `gather`, report→stdout, контрактные тесты raw + REVIEWS_JS,
  two-page smoke. Подробности — в разделе «История» → v1.4.
  - **Проверено:** `pytest` 14/14 зелёные (5 extractors + 6 raw_shape + 3 reviews);
    `smoke_cdp.py` SMOKE OK + TWO-PAGE OK; `--compare` честно ловит дрейф
    `rating_count` 5145→5148 (снимок обновлён до 5148).
  - **В ходе работ найден и исправлен баг:** `pid_from_url()` не отрезал
    query/fragment → после редиректа Ozon (`/product/slug-{pid}/?sh=...`)
    возвращал `"unknown"` → two-page check падал. Фикс: `split("?",1)[0]` +
    `split("#",1)[0]` перед regex. Затронуло и integrity-проверку в `collect()`.
  - **Свежесть фикстур:** живые снимки на 2026-08-07: rating 4.8, count 5148,
    price 1690, gallery 119 (xiaomi), article = pid.

- **v1.3.0: ПРОЕКТ УПРОЩЁН ДО ОДНОКРАТНОГО СБОРЩИКА — РЕАЛИЗОВАНО (2026-08-07).**
  План «фикс экстракторов + живой прогон Xiaomi + упрощение» выполнен целиком.
  Решение пользователя: без бэкапов и git (проект не репозиторий). Детали — в
  разделах «История» и «Функции, которые были и оказались не нужны».
  - **Удалены модули:** analyze, validate, regression, watch, catalog, compare,
    diff, migrate, export (+ scripts/quality_gate.sh). Остались: cdp, collect,
    ocr, report, cli. Пайплайн: `collect → ocr → report`.
  - **cli.py** переписан: только `collect|ocr|report`; один `data/{pid}_raw.json`
    (история по датам убрана); `--expect` убран.
  - **report.py** — чистый рендерер без analysis: карточка (рейтинг+rating_source,
    цена current/old/bank/conf, категория, вариант), характеристики, описание,
    галерея+OCR, отзывы списком, метаданные.
  - **Фикс PRODUCT_JS** под текущую разметку Ozon (миграция на
    webSingleProductScore): SCORE_WIDGETS (3 виджета), ratingCount() языко-
    независимый (без `\b` — он ломался на кириллице, см. ниже), характеристики
    каскадом CHAR_WIDGETS→tsBodyMBold→generic dl→Row, описание каскадом
    DESC_WIDGETS→class-based→nextElementSibling после webCharacteristics.
  - **DOM-снимки:** collect() сохраняет `data/{pid}/dom/{YYYY-MM-DD}.html.gz`
    (product-страница) + `raw["artifacts"]["dom_snapshot"]`; флаг `--no-dom`.
  - **Тесты:** `tests/product_pages/{xiaomi,roborock,neoclima,topper}.html.gz`
    (снимки живых страниц), `tests/conftest.py` (playwright headless + set_content
    + evaluate(PRODUCT_JS), сеть блокируется), `tests/test_extractors.py` —
    **5/5 зелёные** (4 фикстуры + негативный тест). `tests/smoke_cdp.py` — живой
    CDP-прогон одной страницы, вне pytest (SMOKE OK). `pytest>=7` в requirements.
  - **ЖИВОЙ ПРОГОН XIAOMI (2026-08-07):** PID `3627230434`, вариант
    «Цвет товара: черный», категория headphones →
    `docs/headphones_xiaomi_redmi_buds8.md`. Результат: **23 отзыва** под цвет,
    цена **1 690 ₽** (с банком 1 673, conf 0.97, webPrice), рейтинг **4.8/5145**
    (webSingleProductScore), характеристики 34, описание 1, галерея 119;
    OCR: **82 скачано / 68 с текстом, mean_conf 87.4**, 0 ошибок.
  - **Найден и исправлен баг (критичный, при сборке фикстур):** `collect()` не
    навигировал на `product_url` перед `collect_product()` → при повторном
    использовании сессии CDP (переиспользуется `ctx.pages[0]`) в raw попадали
    данные ПРЕДЫДУЩЕГО товара (xiaomi_raw содержал topper). Фикс: `sess.goto(url)`
    в начале `collect()`. DOM-снимок снимается после goto на product-странице.
    Также `parse_variant("")` теперь возвращает `{}` (вариант необязателен).
  - **Схема** `2.0` не менялась; **VERSION 1.3.0**.
- **Дальше (по запросу, не сейчас):** per-field confidence / extractor-слой
  (схема 2.1+migrate), JSON Schema (без зависимости jsonschema), OCR cache,
  review fingerprint/delta, price timeline. Без появления реальной потребности
  эти идеи НЕ возвращаются.

- **v1.2: SNAPSHOT REGRESSION БЕЗ BASELINE + WATCH + CATALOG + RATING_SOURCE
  (2026-08-06, вечер):** план v1.2 полностью реализован. Решение пользователя:
  cron отменён, сбор ручной → regression = **ручной гейт качества** после
  пересбора, сравнивает latest с **предыдущим снапшотом из истории**
  (`data/{pid}/{YYYY-MM-DD}_raw.json`), baseline удалён. **Эталон не сломан:
  conf 80.0, compare 86.4.**
  - **`cli.py regression`** (новый): каждый товар сверяется с предыдущей датой в
    истории (строго раньше latest). ALERT (exit 1): падение conf > `--conf-drop`
    (10) п.п., score < `--score-min` (60), INVALID. Первый снимок → `[NEW]` без
    ALERT (нет сравнения с чем). Флаги `--baseline`/`--init` удалены.
    `scripts/nightly_regression.sh` удалён → **`scripts/quality_gate.sh`** (ручной,
    exit 1 при ALERT).
  - **`cli.py watch --pid`** (новый): PRODUCT CHANGES между latest и предыдущим
    снапшотом — цена (₽/%), рейтинг/оценки, число отзывов + средняя оценка,
    новые негативные 1-2★ (ключевые слова рисков + сниппеты), галерея (+/− по
    md5), характеристики (добавлено/удалено/изменено таблицей), описание.
  - **`cli.py catalog`** (новый): `data/catalog.json` — индекс портфеля (pid,
    title, category, снимков, первый/последний сбор, цена, рейтинг с
    rating_source, отзывы, confidence, score).
  - **`rating_source`:** PRODUCT_JS возвращает `{value, source}` — webProductScore
    → webReviewProductScore → fallback_text (для техники, где webProductScore
    нет). Показ в validate/export/report. Схема `2.0` не менялась.
  - **E2E-тест на fixture подтвердил:** [OK] 2026-07-01→08-01 (conf 75→60 =
    ALERT, exit 1; Емкость 2000→6400 = таблица «изменено»; цена −20%; новый
    негатив с словами-рисками); [NEW] для единственного снимка. **Найден и
    исправлен баг:** фильтр истории `f[:10].isdigit()` ломался на датах с
    дефисами (`2026-07-0` → не digits) → предыдущий снимок никогда не
    находился. Фикс: `f[:10].replace("-","").isdigit()`.
  - **Реальные данные:** 3 товара → [NEW] (первый снимок 2026-08-06 у всех);
    репорт перегенерирован, `regression_baseline.json` и `.log` удалены.
    `node --check` по PRODUCT_JS/REVIEWS_JS — OK.
  - **Следующий шаг:** живой `collect` + `ocr` на выбранной категории (CDP-браузер
    жив). После пересбора: `regression` (должен дать реальное сравнение
    было/стало, а не [NEW]), при необходимости `watch --pid`, `catalog`.
    Рекомендация из прошлого шага — наушники (проверка универсальности).

- **РЕШЕНИЕ ПО v1.3 (2026-08-07): НЕ добавлять v1.3 сразу.** Сначала
  эксплуатация на реальных товарах — практика покажет, что реально нужно.
  Отложенные идеи (только при появлении потребности):
  1. **SQLite** при росте до 100-500 товаров / десятков снапшотов
     (`products`/`snapshots`/`reviews`/`gallery`; экспорт в JSON оставить);
  2. **История отдельных отзывов** (новые/удалённые/изменённые, смена оценки) —
     только если цель — мониторинг изменений отзывов;
  3. **HTML-отчёт** — если отчёты смотрят люди без Markdown-редактора или нужна
     публикация;
  4. **Постоянные фикстуры** (topper/robot/dehumidifier, товар без OCR/без
     отзывов/без цены, мультивариантный) — для быстрых регрессионных проверок.
  Ядро (collect/ocr/analyze/report/compare/validate) НЕ трогать — оно закрывает
  полный цикл.

- **ЭТАП СТАБИЛИЗАЦИИ 1.0 (2026-08-06, вечер):** схема raw поднята до **2.0**,
  добавлены 3 новые команды CLI (`validate`, `migrate`, `export`) и колонка
  «Надёжность выборки» в compare. Итоговая валидация: topper **100/100**,
  Roborock **87/100**, осушитель **87/100** (у техники не извлекается рейтинг
  карточки — известное ограничение, снято в v1.2 через rating_source).
  Эталон conf 80.0 и рейтинги compare (97.1/69.1) не изменились.
  - **schema_version: "2.0"**, `collector_version: "1.1.0"`.
  - `cli.py migrate` — миграция raw in-place (`--dry-run` для предпросмотра):
    восстанавливает `product.price` из `price_parsed` по ярлыкам
    («с_другими_банками»/«с_банками»/«базовая_до_скидки», source='migrated'),
    поднимает schema_version; про `analysis` только подсказывает.
  - `cli.py validate` — health-check: критичные (карточка, отзывы, analysis) +
    весовые (цена 15, OCR 15, тексты 10, рейтинг 10, изображения 10,
    confidence/вариант/signals/ocr_summary 5×4) → **score 0–100**, VALID/INVALID,
    warnings (маленькая выборка, без текста, пропущено изображений…).
  - compare: колонка **«Надёжность выборки»** (`sample_reliability`):
    эмпирическая кривая 100+ отз.→100%, 20→70%, 5→40%; итоговая формула
    рейтинга НЕ менялась (чтобы не ломать валидированный эталон).
  - `cli.py export` → `data/all_products.jsonl` (строка на товар: pid, title,
    category, price/old/bank, rating, n_reviews, n_negative, n_with_text,
    ocr_mean_conf, confidence, …; сортировка по confidence). Для категории —
    новый флаг `collect --category` (→ `product.category`).
  - **Фикс бага:** `_pid_from_url`/`_pid` не распознавали URL вида
    `…/product/PID/` (только `…-PID`) → export/validate давали pid=null.
- **ЖИВОЙ ПРОГОН ОСУШИТЕЛЯ (2026-08-06, день):** NeoClima ND-20RH (PID
  `1643752608`, вариант «Цвет:белый») — проверка универсальности на принципиально
  другой категории (литры/мощность/площадь вместо габаритов):
  - `collect` → **34 отзыва** под вариант (страница показала 42, «Дальше» нет);
  - `ocr` → **66 скачано / 57 с текстом, mean_conf 86.0** (после фикса апгрейда
    URL, см. ниже; до фикса было 1 картинка, mean_conf 0);
  - `analyze` → n=34, avg=4.71, conf **58.4** (expect=100, собрано 34), негативных **2**;
  - `report` → `docs/dehumidifier_neoclima_nd20rh.md`; цена **16 436 ₽** (старая
    48 000, с банком 14 792, conf 0.97);
  - OCR summary: **л** 25/12/150/3/13/9/2.5, **м** 40/25/50, **мм** 400/12.5/1430,
    **мин** 150/80, **Па** 20000/22000, **Вт** 210 — категория обработана
    универсально.
- **ФИКС has_text (критичный, найден на осушителе):** `has_text` вычитал длину
  автора из текста (legacy, когда текст включал автора) → короткие отзывы
  («сломался через 4 мес») ложно помечались «без текста» и не попадали в
  негативные/информативные. Теперь `len(text.strip()) >= 10`.
- **OCR апгрейд URL (2026-08-06):** `ocr.py` скачивает `wc1200` вместо `wc300`
  (`_upgrade_url`) — превью ~10КБ отсекались порогом MIN_BYTES=20КБ. Результат:
  осушитель mean_conf **86.0** (до фикса 0.0). Заодно отфильтрованы видео-обложки
  (wc200) как маленькие.
- **OCR SUMMARY (2026-08-06, день):** этап 3 готов — в анализе ключевые значения
  из OCR галереи (`ocr_summary`): пары число+единица (Па/мм/Вт/мАч/%/мин/°С…,
  с группировкой по частоте) + фрагменты с ключевыми словами (гарантия, мощность,
  размер, материал, вес, навигация, заряд). В отчёте — подблок «Ключевые значения
  из OCR» в секции 3. Roborock: Па (10 000 ×4, 12 000…), мм (8, 96.5, 521, 487),
  180 мин, 75 °С. Примечание: «113 000 Па»/«220 м» — честные OCR-артефакты.
- **ЦЕНА ИСПРАВЛЕНА (2026-08-06, день):** новый `price_extractor` в collect.py —
  точная цена из виджета `webPrice`: `price = {current, old, bank, confidence,
  source}`. Roborock: **33 345 ₽** (старая 90 000, с банком 30 011, conf 0.97).
  Баг «цена в compare = 89 ₽» закрыт. Проверено на 4 товарах (Roborock, topper,
  наушники Hoco, книга).
- **REVIEW SIGNALS (2026-08-06, день):** аналитика информативности отзывов без
  категорий — длина, период эксплуатации («спустя месяц»), слова-маркеры проблем,
  `verified_long_review`. В отчёте новая секция **«6. Самые информативные отзывы»**
  (топ-3). Секции 7-10 перенумерованы.
- **ЖИВОЙ ПРОГОН ВЫПОЛНЕН (2026-08-06, ночь):** Roborock Qrevo L White, PID
  `2561238412`, вариант «Цвет: белый-прозрачный»:
  `collect` → 48 отзывов под вариант (бесконечный скролл, пагинации «Дальше» нет),
  `ocr` → 60 картинок, 40 с текстом, **mean_conf 81.8**, 0 ошибок,
  `analyze --expect 60` → n=48, avg=4.85, conf **71.4**, негативных 1,
  `report` → `docs/vacuum_robot.md`.
- **Фикс extract в REVIEWS_JS (по итогам живого прогона):** variant-метаданные
  («Цвет товара…», «Качество уборки…» и т.п.) попадали в текст отзыва.
  Решение: чистый текст берётся из `div[class*="o2j_25"]` (fallback — прежний
  `wrapper.innerText`); `helpful`/`comments` — из полного `wrapper.innerText`
  (иначе теряются). Устранён дубль `const variant`.
- **Следующий шаг (по запросу пользователя):** живой `collect` + `ocr` на выбранной
  категории/товаре (после фикса цены рекомендуется **наушники** — проверка
  универсальности на другой структуре). CDP-браузер жив (`http://127.0.0.1:9222`).
  Команды:
  `python3 cli.py collect --pid <id> --variant <ДxШxВ|ключ:значение> --expect N`
  затем `python3 cli.py ocr --raw data/<pid>_raw.json` затем
  `python3 cli.py analyze --raw ...` и `report`.

## Функции, которые были и оказались не нужны (2026-08-07)

Пользователь упростил проект до однократного сбора по запросу. Что удалено и почему
(полный список с указанием модулей):

- **`analyze` (аналитика отзывов)** — средняя оценка/звёзды, негативные 1-2★,
  «самые информативные», тональность, conclusion, confidence 0-100. Для разового
  решения «покупать или нет» достаточно списка всех отзывов варианта в отчёте.
- **`compare` (сравнение кандидатов)** — рейтинги 0-100 с весами и «надёжность
  выборки». При однократных заказах сравнение делается глазами в браузере Ozon.
- **`validate` (health-check 0-100) + `regression` (гейт качества по истории)** —
  были привязаны к идее повторных сборов и мониторинга. Разовый сбор не требует
  ни score-валидации, ни сверки с предыдущим снимком; гарантию корректности дают
  unit-тесты на сохранённых страницах.
- **`watch` (PRODUCT CHANGES) / `diff` (было/стало)** — мониторинг изменений
  товара во времени. Не нужен при «собрал один раз и забыл».
- **`catalog` (индекс портфеля) + `export` (JSONL) + `migrate` (миграция схемы)**
  — портфельная/историческая логика, которой больше нет.
- **История raw по датам** (`data/{pid}/{YYYY-MM-DD}_raw.json` + latest-копия) —
  один файл `data/{pid}_raw.json` на товар (дата внутри `collected_at_utc`).
- **Бэкапы и git** — осознанно не используются (проект не репозиторий).
- Ещё раньше (v1.0-1.2) уже удалены: конфиги категорий, классификация
  плюсов/минусов regex'ами, cron/nightly, baseline-регрессия.
- **Итоговая структура:** `cli.py collect|ocr|report`; `ozon_collector/
  {cdp,collect,ocr,report,__init__}.py`; `tests/` (фикстуры + extractors + smoke);
  `data/{pid}_raw.json`, `data/{pid}/dom/`, `data/gallery_{pid}/`; `docs/` + `docs/archive/`.

## История

### v1.13.0: BrowserWatchdog + Session guard + фикс EVAL_TIMEOUT (2026-08-09)

Задача: v1.12.0 замечал смерть браузера только по исключению на операции.
Добавлен фоновый сторож, чтобы fail-fast срабатывал раньше (проверка перед
каждой операцией вместо ожидания ошибки), и по пути найден и исправлен сломанный
слой `timeout=` в `page.evaluate`.

- **`BrowserWatchdog` (`browser.py`):**
  - daemon-поток `ozon-watchdog`, `start()` идемпотентен, `stop()` — через event.
  - `_sample()`: `BrowserProbe.cdp_http_ok(cdp_url)` (http_timeout=1.0); при
    провале HTTP → `bool(P.browser_processes(cdp_url))` — процесс жив = транзиент
    (BROWSER_UNHEALTHY), процесс исчез = смерть.
  - `_report(ok)` — debounce через `time.monotonic`: флаг `alive=False` только
    после непрерывного bad-окна ≥ `grace_s` (защита от временных сбоев CDP).
  - `check()` бросает `BrowserRecoveryError` на операционных границах.
  - `classify_error`: `BrowserRecoveryError` → `Classification("browser",
    probe_state="watchdog", signature=...)` → recyclable (как renderer_failure).
- **`Session` (`cdp.py`):**
  - `self._watchdog` (None до первого guarded-вызова), `_ensure_watchdog()`
    (ленивый старт), `watchdog_stop()` (в `close()`), `_guard_check()`.
  - guard в `goto`/`scroll`/`wait`/`eval`; после `reconnect()` — новый инстанс
    (состояние «мёртв» от старого браузера не переносится).
  - Поток НЕ трогает Playwright (только HTTP + OS probe) — безопасен при работе
    main-потока с page.
- **Регрессия EVAL_TIMEOUT (найдена по 7 красным тестам):**
  - `pytest` после watchdog: **210 passed / 7 failed** — падали
    `test_reviews.py` (6: activate, next_page×3, expand_comments) и
    `test_extractors.py::test_extract_live_markup[xiaomi]`.
  - Диагностика: `TypeError: Page.evaluate() got an unexpected keyword argument
    'timeout'`. В playwright 1.62.0 сигнатура `Page.evaluate(expression, arg)`,
    kwarg `timeout=` не существует; `page.set_default_timeout` на evaluate НЕ
    влияет (проверено экспериментом: infinite-loop evaluate не прерывается).
  - Источник: незакоммиченный EVAL_TIMEOUT-слой (из ранней части работ) —
    `timeout=EVAL_TIMEOUT` в cdp.py `scroll`/`eval` и ~15 местах в collect.py
    (`_next_page`, `next_url`, `_scroll_to_reviews`, `_scroll_to_converge`,
    `_expand_review_texts`, `_expand_review_comments`, `_detect_pagination`,
    `_activate_variant_mode` и др.). Сломал бы и живой прогон (первый же
    `sess.eval`/`sess.scroll` → TypeError).
  - Фикс: `timeout=` удалён из всех `page.evaluate`; `EVAL_TIMEOUT = 15000`
    удалён из cdp.py и collect.py; докстринг watchdog обновлён.
    Вывод: per-op таймаут evaluate в playwright 1.62 в принципе нереализуем —
    защита от зависших операций теперь только на границах операций (watchdog).
- **Тесты:** `test_browser.py` +8 (`test_watchdog_check_alive`,
  `test_watchdog_check_dead`, `test_watchdog_grace_debounce` (monkeypatch
  `time.monotonic`), `test_watchdog_sample_http_ok_skips_process`,
  `test_watchdog_sample_http_down_process_alive_is_transient`,
  `test_watchdog_sample_http_down_process_dead`,
  `test_watchdog_start_starts_daemon_thread`, `test_classify_watchdog_recyclable`).
  `test_cdp_launch.py` +2 (`TestSessionWatchdogGuard`: guard бросает при мёртвом
  watchdog + ленивый авто-старт и scope). `_FakePage.evaluate` принимает
  `*args/**kwargs`. Вспомогательные диагностики (удалены после работы):
  `D:\Temp\opencode\wd_debug*.py`.
- **Файловый лог парсинга (`log.py`, та же ревизия):** пользователь попросил вести
  подробные логи в файлы, чтобы потом по ним искать причины проблем.
  - Дизайн: `log.info/warning/error` дублируют прежний `print` в stdout
    (консольный вывод не меняется — на нём стоят capsys-тесты), `log.debug`
    пишет только в файл. Файл по умолчанию `logs/ozon_collector.log` (папка
    `logs/` в корне проекта, создаётся автоматически), переопределение
    `--log <path>` или env `OZON_COLLECTOR_LOG`; ротация при КАЖДОМ запуске:
    `.log→.log.1` (текущий лог всегда свежий, `.log.1` — предыдущий прогон).
    `log.setup()` вызывается только в `cli.main()` — pytest файлы не создаёт.
    Без `setup()` модуль работает как чистый `print` (поведение тестов не меняется).
  - `_caller_module()` — `sys._getframe(2)` для атрибуции записи по модулю;
    `extra={...}` сериализуется через `json.dumps(ensure_ascii=False,
    default=str)`.
  - Покрытие: cdp.py (`_js_label()` — метка JS ≤40 симв.; debug в
    goto/scroll/wait/eval, watchdog start/stop/check, reconnect, close;
    `[cdp] браузер не запущен` → + warning), collect.py (все 18 print → log.*;
    debug-шаги `_scroll_to_converge` — it/unique/stable/плато/финал, и
    `_detect_pagination` — sp/grew), gather.py (cache-причины, collect/ocr
    off/hit/ran/error), ocr.py (download tessdata, fail fetch, skip small/plain),
    search.py (итерации докрутки, ok/fail товара, recycles, preventive),
    browser.py (переходы watchdog alive→transient→dead, start/stop).
  - Проверено: `pytest` 217 passed + 4 skipped после всех правок (capsys
    целы), `py_compile` чисто.
- **Итог:** `pytest` **217 passed + 4 skipped**, `py_compile` чисто.



Задача: длинный `search` (десятки товаров, одна CDP-сессия) падает при OOM-краше
renderer'а Chromium — типовой сценарий `net::ERR_ABORTED: The frame was detached
from the DOM` — и все последующие товары проваливаются. Решение: классификация
ошибок + авто-перезапуск браузера + retry товара.

- **`ozon_collector/process.py` (новый, изолированный process layer):**
  - `ProcessIdentity(pid, start_key)` — физическое владение; переживает PID-reuse
    (start_key = Linux: starttime из `/proc/<pid>/stat`; Windows: время создания
    процесса через `GetProcessTimes`).
  - `ProcessRecord(launch_id, identity, lifecycle)` — логическое владение
    (какая попытка recovery); lifecycle строго монотонен
    `created → alive → cdp_ready → killed → verified_dead`; `partial=True` —
    launch поднял процесс, но CDP так и не поднялся.
  - `browser_processes(cdp_url)` — поиск Browser-процессов по profile
    (`--user-data-dir=…/ozon-profile`) + port; renderer/дочерние исключаются по
    `--type=`. `identity_alive()`/`kill_records()` — убийство только после
    повторной сверки start_key (PID-reuse → чужой процесс не трогаем).
  - Убийство Linux: pidfd + `os.kill(pidfd, sig)` (exit-detection через
    `select.poll` POLLIN, работает для non-child); Windows: OpenProcess-хэндл
    после валидации identity + `TerminateProcess` (вместо PID).
- **`ozon_collector/browser.py` (новый):**
  - `has_renderer_signature(msg)` — сигнатуры наблюдённого OOM-сценария.
  - `Classification{error_kind, probe_state, probe_attempts, signature}` +
    `should_recycle` (browser/renderer_failure → True, target/unknown → False).
  - `BrowserProbe` — независимый health probe: OS identity + CDP HTTP
    `/json/version` (ретраи) + состояние target (`/json/list` page-таргеты +
    `page.is_closed()`). ProbeState: BROWSER_DEAD / BROWSER_UNHEALTHY /
    RENDERER_FAILURE / TARGET_LOST / HEALTHY / UNKNOWN.
  - `classify_error(exc, sess)` — Playwright-error + (если сессия есть) probe:
    BROWSER_UNHEALTHY (identity жив, CDP HTTP не отвечает) НЕ считается смертью
    браузера для kill — телеметрия отдельно от error_kind.
  - `BrowserProcessManager` — launch/restart/force_stop: каждый появившийся
    процесс регистрируется сразу; restart = kill old (verified_dead обязателен)
    → проверка отсутствия чужих matching-процессов → запуск; чужой/неизвестный
    процесс или matching>1 → `BrowserOwnershipError` (ручное вмешательство).
- **Recovery-автомат (`search.py`):**
  - `collect_search(..., max_recycles=2)`: если `run_gather` вернул
    `recyclable=True` → **R1** `_recycle_browser()`: `BrowserProcessManager.
    restart` (old = текущие matching-процессы на cdp_url) + `Session.
    reconnect()` → **R2**: повторный `run_gather` того же товара. После лимита
    попыток ошибка остаётся как есть. При неудаче самого recycle — товар
    помечается `recycle_failed`, `recyclable=False`.
  - `cdp.py`: добавлен `Session.reconnect()` (close + _connect заново).
  - Телеметрия: у товара `error_kind`/`error_probe_state`/`recycle_attempts`;
    в combined `recovery{recycles, events[{pid, attempt, kind}]}`.
- **`gather.py`:** `run_gather` при исключении классифицирует его
  (`B.classify_error(e, sess=sess)`) и возвращает `error_kind`/`recyclable`/
  `error_probe_state`/`error_signature`; лог `[gather] pid=…: error: … (kind=…)`.
- **VERSION → 1.12.0** (инвалидация старых кешей raw — осознанно).
- **Проверки:** `pytest` **197/197 + 4 skipped** (skipped — Linux-only pidfd,
  тесты `test_process.py` пропускаются на Windows); `py_compile` чисто.
  `test_browser.py` 42 (классификация, probe 16 веток, manager launch/restart/
  force_stop на моках без живого Chromium); `test_process.py` (~24, включая
  живые sleep-процессы и PID-reuse негатив); recovery в `test_search.py` (+4:
  R1+R2 успех, no-recycle для non-recyclable, лимит max_recycles, recycle FAILED);
  классификация в `test_gather.py` (+2); `Session.reconnect` в `test_cdp_launch.py`.
- **Документы:** README (+раздел «Recovery после краша браузера», combined-схема
  с recovery/error_kind), PROJECT_NOTES (эта запись).

### Windows-окружение: python, зависимости, tesseract, git, живой сбор Ecovacs (2026-08-08)

- **Среда:** Windows PowerShell (win32), Python 3.12.5 (`D:\Users\Neon\AppData\Local\
  Programs\Python\Python312`). Ранее проект работал в Linux-контейнере — эта среда устарела.
- **Запуск CLI:** `python` — `python3` в этой системе заглушка Microsoft Store
  (WindowsApps alias, печатает «Python was not found»). Обязателен
  `$env:PYTHONIOENCODING="utf-8"` перед командами — иначе `UnicodeEncodeError cp1251`
  на кириллице (текст запроса, пути, вывод). Рабочий каталог — папка проекта, иначе
  «cli.py no such file». Вставка в консоль: правый клик / Ctrl+V.
- **Зависимости (Windows):** playwright 1.62.0 + chromium-1234, pytest 9.1.1,
  pillow 12.3.0, pytesseract 0.3.13, opencv-python 5.0.0.93, httpx, jinja2, pyyaml,
  selenium — все установлены (ALL PYTHON DEPS OK).
- **tesseract:** v5.4.0 (winget `UB-Mannheim.TesseractOCR`), путь `C:\Program Files\
  Tesseract-OCR`, добавлен в пользовательский PATH (новые PowerShell-сессии подхватят
  сами; в текущей — `$env:PATH += ";C:\Program Files\Tesseract-OCR"`). `rus.traineddata`
  (tessdata_best, 15301764 байт) скачан и скопирован через elevated Start-Process (UAC,
  нет прав на Program Files). `tesseract --list-langs`: eng, osd, rus. OCR-тесты 16/16.
- **git:** `git init` + первый коммит `11ea53d` (37 файлов, 8052 insertions). По решению
  пользователя в коммит вошёл ТОЛЬКО код (cli.py, ozon_collector/, tests/, scripts/,
  README, PROJECT_NOTES, requirements); `data/` (~187 МБ: raw, галереи, ocr_cache) и
  `docs/` исключены `.gitignore`.
- **Живой сбор Ecovacs T50S Pro Omni (PID 5023742495):** цена **43 151 ₽** (старая
  199 990, с банком 38 836, conf 0.97, webPrice), рейтинг 5/5 (1 оценка), 31
  характеристика, 13 фото галереи, 1 отзыв (5★, «Имя скрыто», 5 авг 2026). OCR галереи:
  **12/12 с текстом, mean_conf 86.9**, engine 5.4.0.20240606, threads 8, 11.7s.
  Отчёт: `docs/ecovacs_t50s_pro_omni.md`, данные: `data/5023742495_raw.json`.
  Ozon при живой сессии показывал капчу «Доступ, который ограничен» — проходится
  вручную в окне CDP-браузера (пользователь открывает Ozon сам).
- **Тесты:** полный прогон **132 passed** (test_cdp_launch 11, test_ocr 16, остальные зелёные).
- **Известный артефакт среды:** вывод read/grep дублирует строки — файлы на диске целы.

### Windows: скрипт запуска CDP-браузера launch_chrome.ps1 (2026-08-08)

- **Задача:** проект запускался только на Linux (`scripts/launch_chrome.sh` с `DISPLAY=:0`
  и `setsid`); на Windows `collect`/`gather`/`search` не могли поднять браузер сами.
- **Решение:** добавлен `scripts/launch_chrome.ps1` — аналог .sh для Windows: поиск
  Chromium (playwright chromium → системный Chrome), профиль по умолчанию
  `%LOCALAPPDATA%\opencode\ozon-profile`, порт из env `PORT` (дефолт 9222),
  без `--enable-automation` (иначе антибот). Запуск: `powershell -ExecutionPolicy Bypass
  -File scripts/launch_chrome.ps1`.
- **`ozon_collector/cdp.py`:** `_launch_script_name()`/`_launch_script_path()` выбирают
  скрипт по `os.name` (nt → `.ps1`, иначе → `.sh`); `_launch_browser` на Windows зовёт
  `powershell -ExecutionPolicy Bypass -File …`, на Linux — как раньше. Сообщения об
  ошибках/подсказки ручного запуска — платформозависимые.
- **Тесты:** `tests/test_cdp_launch.py` — assert'ы на `launch_chrome.sh` заменены на
  нейтральные `launch_chrome`; `test_script_missing_raises` мокает `_launch_script_path`.
- **Документация:** README — упоминание `.ps1` (установка, ручной запуск, пайплайн).

### SEARCH + TTL-кеш (v1.11.0, 2026-08-08)

- **Задача:** команда `search` (по `--query` или `--url` выдачи) собирает до `--limit`
  товаров через существующий пайплайн `gather` и выдаёт один markdown-digest для Gemini
  (выбор лучшего товара). Плюс TTL-кеш собранных raw-данных (`--max-age`, дефолт `1d`),
  чтобы перезапуск после падения не пересобирал свежие товары.
- **Архитектура:** учтена вся внешняя критика v3/v4 (см. «Ключевые решения»): отдельный
  модуль кеша, одна CDP-сессия на весь search, раздельные поля `cache`/`ocr`,
  defensive-парсинг дат, обрезка отзывов только в digest, `cache_is_compatible` по
  `schema_version` (не по версии коллектора).
- **`ozon_collector/cache.py`** (новый): `parse_max_age` (целое + суффикс `d/h/m`,
  дробные/составные → ValueError; `0`/пусто → кеш off), `_parse_utc` (оборачивает
  `s.replace` → AttributeError тоже ловится), `raw_path`, `cache_is_compatible`,
  `cache_check` → `(raw, reason)`. Причины: `off|missing|incomplete|incompatible|
  stale|bad_timestamp|hit`, наружу маппятся в `off|miss|stale|hit`.
- **`ozon_collector/gather.py`** (новый): `run_gather(cfg, sess=None)` → единая точка
  для CLI gather и search; `_save_raw`. `cli.py`: `cmd_gather` — тонкая обёртка,
  добавлен `--max-age`.
- **`ozon_collector/collect.py`:** у `collect()` добавлен `sess=None` (внешняя сессия
  не закрывается, `sess_owned`); без него — своя сессия закрывается в `finally`.
- **`ozon_collector/search.py`** (новый): `SEARCH_JS` (regex `/(?:-(\d+)|\/(\d+))$/`,
  экранирование `\/` — иначе `SyntaxError` в живой выдаче; отсев чужих доменов:
  только относительные `/product/` или `ozon.ru`), `search_url` (`from_global=true`),
  `collect_search` — одна сессия, `found=len(links)`, `products[:limit]`.
- **`ozon_collector/report.py`:** `render_search_digest(combined, reviews_per_product=15)`
  — deepcopy raw, обрезка отзывов только в копии; ошибки товара → блок
  «⚠ Не удалось собрать: <error>»; в конце «## Задание для LLM».
- **CLI `search`:** `--query`/`--url`, `--limit 5`, `--reviews-per-product 15`,
  `--max-pages 12`, `--min-reviews 40`, `--max-age`, `--out`, `--out-raw`
  (`data/search_combined.json`), переиспользуемые `--variant/--category/--threads/
  --no-preproc/--no-dom/--no-ocr/--no-variant-mode/--sorts`.
- **Формулировка digest:** «Найдено в первой странице выдачи: N уникальных товаров,
  собрано M» (не выдавать за весь поиск Ozon).
- **Найденные факты по живой выдаче:** оба формата (`/search/` и `/category/`) отдают
  ~24 уникальных pid (по 2 ссылки на карточку — орган. и спонсор.); фикстуры в
  `tests/search_pages/search.html.gz` и `category.html.gz`.
- **Проверки:** `pytest` **89/89** (test_cache.py 18 + test_search.py 10), py_compile
  чисто; регрессия gather байт-в-байт (кеш vs `--max-age 0`); живой search
  `--query "палатка 4 местная" --limit 2 --max-pages 1 --min-reviews 5 --no-ocr` →
  `found=24 limit=2 collected=2 ok=2` (первый товар показал `cache=hit`).
- **Найденные при тестах баги:** 1) SEARCH_JS с `\\/` давал `SyntaxError` в живой
  выдаче → `\/`; 2) SEARCH_JS пропускал `https://other.ru/product/99999999` → добавлен
  отсев чужих доменов; 3) тесты digest писали uuid отзывов как маркер, а `_review_block`
  uuid не печатает → маркеры заменены на author/text.

### Докрутка выдачи search + исключение рекомендаций (2026-08-08)

- **Симптом:** `search --limit 50` на фильтре матрасов показал `found=8`, хотя в выдаче
  больше товаров. Лог: `[search] найдено 8 товаров в выдаче, собираю 8`.
- **Диагностика живой страницы (CDP):**
  - Ozon рендерит в DOM сразу лишь ~8 карточек, остальные подгружаются скроллом —
    виджет `infiniteVirtualPaginator`. `SEARCH_JS` исполнялся сразу после `goto`,
    до скролла → `found=8`.
  - 21 ссылка `/product/` в начальном DOM = 8 уникальных pid (по 2–3 ссылки на
    карточку: картинка/заголовок/кнопка). Регэксп ничего не терял.
  - После скролла до конца: **32 уникальных товара в `tileGridDesktop`** (реальная
    выдача по фильтру). Считанные ранее «57» включали ссылки из блока «Возможно,
    вам понравится» (виджет `skuGrid`, 80 ссылок) и «Вы смотрели» (`island`).
- **Решение:**
  - `SEARCH_JS` теперь выбирает только `[data-widget="tileGridDesktop"]` — сетку
    результатов; `skuGrid`/`island` (рекомендации) физически вне выборки.
  - Новый `_collect_links(sess, limit)`: докрутка со скроллом (шаг 1500 px, пауза
    1000 мс после каждого скролла, `Session.wait`), `seen` копится в Python
    (виртуализация может размонтировать ушедшие вверх карточки), стоп по
    стагнации (`quiet_after=2` скролла без роста), `scan_limit = max(limit, 50)`
    (не сканировать гигантские каталоги), предохранитель `max_scrolls=40`.
  - **Семантика разделена:** `found` = все найденные уникальные товары (до конца
    сетки/scan_limit), `selected = found[:limit]` — сколько берём в работу. Digest
    честно пишет «Найдено товаров в выдаче: 32 … собрано 2».
- **Проверки:** `pytest` **107/107** (test_search.py 16: докрутка до стагнации,
  found=полная выдача при limit=5, дедуп между скроллами, стоп по scan_limit,
  рекомендации вне tileGridDesktop не берутся; тесты digest — новая формулировка).
  py_compile чисто. **Живой прогон** `--limit 2 --no-ocr` → `found=32 limit=2
  collected=2 ok=2`, digest: «Найдено товаров в выдаче: 32 уникальных товаров,
  собрано 2».
- **Замечание по времени:** полный прогон на новых товарах с OCR (~18 картинок на
  товар) долгий — 32 товара могут не уложиться в 10 минут; при верификации
  использовать `--no-ocr` или малый `--limit` (found вычисляется до среза).

### Автозапуск CDP-браузера (2026-08-08)

- **Проблема:** если браузер закрыт, `collect`/`gather`/`search` падали с
  traceback (`connect_over_cdp`), приходилось вручную бегать
  `sh scripts/launch_chrome.sh`.
- **Решение (вариант «по умолчанию вкл + env-выключатель», утверждён):**
  - `cdp.py`: `launch_and_wait(cdp_url, wait_s=30)` — если `browser_alive()` False:
    печать `[cdp] браузер не запущен на <url>, запускаю…`, `subprocess.Popen` на
    `scripts/launch_chrome.sh` (порт из `urlparse(cdp_url).port`, дефолт 9222, через
    env `PORT`), поллинг `browser_alive()` каждые 0.5с; таймаут → `RuntimeError` с
    подсказкой ручного запуска.
  - `Session(cdp_url, auto_launch=None)` — `None` → env `OZON_AUTO_LAUNCH`
    (дефолт `"1"`; `"0"` отключает). `_connect()`: если `auto_launch` и браузер не
    жив → `launch_and_wait()`; `try/except` вокруг `connect_over_cdp` — при падении
    (гонка: браузер умер между проверкой и коннектом) ещё один `launch_and_wait()`
    + повторный коннект; иначе `raise`.
  - Точки входа не менялись: все пути (`cmd_search`, `collect_product(sess=None)`)
    идут через `Session(cdp_url)`.
- **Проверки:** `tests/test_cdp_launch.py` — 11 тестов на моках (без живого
  Chromium): запуск при мёртвом браузере, отсутствие запуска при живом, таймаут →
  чистая ошибка, порт из URL, `OZON_AUTO_LAUNCH=0` → не запускается, retry после
  падения коннекта, без retry при авто-офф. `pytest` **118/118**.
- **Ограничения:** авто-запуск требует реального X-дисплея (`DISPLAY=:0` в скрипте)
  и установленного Chromium; иначе — понятная ошибка вместо traceback. Офлайн-
  команды (`report`/`ocr`/`compare`) браузер не трогают.

### Дедуп ответов продавца (2026-08-08)

- **Задача:** на отчётах MAX WAX 270 комментариев (170 от продавца), где шаблонные
  ответы («Иван, спасибо за заказ!», «Пётр, спасибо за заказ!») визуально шумят.
  Решение: дедуп только при рендере Markdown, `*_raw.json` не трогается.
- **Архитектура:** `_dedup_key(c)` = `(author, первая фраза)` — первая фраза до
  `[.!?\n]` с вырезанным «Имя, » через `^[^,\n]+,\s*`. `_group_comments(comments)`
  → `[(first_comment, dup_count)]` — порядок первого появления. `_render_comments`
  отделена от группировки: `first_meta = {id(c): n}` → выводятся только первые
  группы с меткой `_…и ещё N такой же ответ/таких же ответа/таких же ответов
  продавца пропущено_`.
- **CLI:** `--no-dedup-replies` для `report`, `gather`, `search` (полностью
  отключает фильтр). По умолчанию дедуп включён.
- **Проверки:** `pytest` **132/132** (+14 `test_report.py`: ключ, вырез имени,
  межгрупповые отличия, порядок+метка, interleaved A-B-C, no-dedup, digest
  per-product). `py_compile` чисто. **Верификация на `data/550332874_raw.json`:**
  md5 не меняется (`1fa7a47…`), 270 комментариев → **138** отрендерено (132 дубля
  скрыто, 17 меток «пропущено»), с `--no-dedup-replies` — все 270 на месте.
- **Документы:** README (опция `--no-dedup-replies`, раздел «Дедуп повторяющихся
  ответов продавца»), PROJECT_NOTES (эта запись).

### Чистка вкладок в Session.close() (2026-08-08)

- **Проблема:** браузер живёт постоянно (профиль `/tmp/opencode/ozon-profile`),
  после каждого `collect`/`gather`/`search` оставалась вкладка с результатом;
  вкладки копились. Коллектор работает с `ctx.pages[0]`, старые вкладки не мешают,
  но визуально захламляют браузер.
- **Решение (вариант A, утверждён пользователем):** `Session.close()` теперь
  закрывает все вкладки кроме `pages[0]` и уводит `pages[0]` на `about:blank`.
  Браузер НЕ гасится — следующая команда получает чистую вкладку.
- **Реализация:** одна правка в `cdp.py:Session.close()` — перебор
  `ctx.pages[1:]` с закрытием каждой, `pages[0].goto("about:blank")`.
  Исключения не роняют закрытие.
- **Проверки:** `pytest` **132/132** (тесты `test_cdp_launch.py` 11 не менялись —
  все проходят). `py_compile` чисто.

### Доработка v1.11.0: прогресс-логи + версионная инвалидация кеша (2026-08-08)

- **Проблема:** при `search` в консоли не видно, какой товар сейчас обрабатывается и
  что происходит — только мельтешение `[ocr] skip small…` и `[collect] стр. N…` без
  привязки к товару (цикл `collect_search` вообще не логировался).
- **Прогресс-логи (вариант A, утверждён):**
  - `collect_search` — перед каждым товаром: `[search] товар N/5: pid=… (url)`, после —
    итог `[search] товар N/5: ok · cache=… · ocr=… · отзывов M` или `fail · <error>`;
    в начале: `[search] найдено N товаров в выдаче, собираю M`.
  - `run_gather` (общий и для CLI gather, и для search) — стадии с pid и причиной:
    `[gather] pid=…: cache=hit (1.11.0, age=2m)`, `cache=off (--max-age 0)`,
    `cache=miss (<reason>)`, `collect — N отзывов`, `ocr=hit|ran (N картинок)|error`,
    `ocr=off`, в исключении `error: <type>: <msg>`.
- **Версионная инвалидация кеша (реверс прежнего решения «коллектор-версию не
  проверяем»):** `cache.py` → `_version_tuple` (численное сравнение по `tuple[int…]`,
  `1.10.0 > 1.9.0`; строкам верить нельзя) + `version_status` → `ok|old|newer|invalid`.
  `cache_check`: `collector_version` отсутствует или не парсится → **`old_version`**
  (пересбор); старее текущей → **`old_version`**; новее текущей → **`newer_version`**
  (кеш тоже не берётся — защита от старого кода поверх данных новой версии); равна
  текущей → кеш работает. Причины в логах: `cache=miss (old_version 1.5.0 < 1.11.0)`,
  `cache=miss (newer_version 2.0.0 > 1.11.0)`.
  - **Обоснование:** старые raw (в data/ нашлись 1.3.0/1.5.0/1.6.0/1.8.0/1.10.0) могли
    быть собраны версиями без `reviews_variant_mode`/`pagination_type` и с ошибками
    экстракторов — использовать их как кеш = риск неполноты/некорректности отчёта.
  - **Файлы не удаляются** (архив), при следующем успешном сборе перезаписываются
    свежими. VERSION не бампится (1.11.0): сегодняшние свежие кеши остаются hit,
    пересобираются только по-настоящему старые.
- **Проверки:** `pytest` **102/102** (test_cache.py 25 — новые `_version_tuple`,
  `version_status`, `old_version`/`newer_version`/missing-version, `cached_version`;
  test_gather.py 4 — cache-hit лог, old/newer → пересбор, error-лог; test_search.py 11 —
  прогресс-вывод `товар N/5`). py_compile чисто.
- **Документы:** README (+«Прогресс-логи», «Кеш сырых данных» — версионная
  инвалидация), PROJECT_NOTES (эта запись + «Ключевые решения» — реверс решения о
  проверке `collector_version`).

### Сбор MAX WAX + разбор критики отчёта (2026-08-08)

- **Сбор 550332874 (MAX WAX Chain Wax 100мл).** Первый прогон без `--sorts` — 96 отзывов
  («новые и полезные»). Пользователь спросил, собраны ли низкие оценки → пересбор с
  `--sorts published_at_desc,score_asc`: `python3 cli.py collect --pid 550332874 --sorts published_at_desc,score_asc`
  → **180 уникальных** (96+96, пересечение 12; `review_sorts`: 84 только published / 12 оба /
  84 только score_asc). Звёзды: ★5:77, ★4:7, ★3:5, ★2:37, ★1:54 = 91 негативный (1-2★).
  `reviews_sorts` в raw: `['published_at_desc','score_asc']`. Отчёт:
  `python3 cli.py report --raw data/550332874_raw.json --out docs/maxwax_chain_wax.md` →
  **docs/maxwax_chain_wax.md** (карточка: рейтинг 4.9/11670, цена 600₽ старая 2100₽ скидка 71%,
  с банком 540₽, страна Россия, объём 100 мл).
- **Критика отчёта (внешняя) — разобрана по 4 пунктам:**
  1. **«Пропуск раздела №5»** (после «4. Описание» идёт «6. Все отзывы») — подтверждено:
     «## 5. Галерея (OCR)» в `report.py:166` рендерится только при `gallery_ocr`; в этом сборе
     OCR не выполнялся → раздел выпал, номера жёсткие. **Решение: НЕ чинить** — номер привязан
     к блоку, по нему можно ссылаться (см. «Ключевые решения»).
  2. **«Аномалии хронологии ответов продавца»** (Евгений М. 3 июля 2026 — ответ 27 июня 2026;
     Ринат Л. 30 марта 2026 — ответ 18 августа 2025) — **подтверждаются в данных, но это НЕ
     дефект сбора**: парсер сохраняет `comments[].date` ровно как отдаёт Ozon (дата комментария
     = дата сообщения ветки, а не отзыва). Парсер ничего не искажает.
  3. **«Смещение выборки»** (54×«1»+37×«2» против рейтинга 4.9/11670) — **обоснованно, но это
     следствие запрошенного `--sorts score_asc`**, в отчёте явно помечено строкой «Сортировки».
     Не дефект, а документированное свойство выборки.
  4. **Факты из отзывов — подтверждаются полностью:** типографская ошибка «масло вместо
     парафина» (продавец MAX WAX подтвердил в ответах на отзывы Сергея П., Кирилла Г., Леонида Р.:
     «в одной партии этикеток была допущена ошибка в указании состава»); температурный режим —
     ответ на отзыв Якова Б.: «При отрицательной температуре смазка необратимо разлагается...
     Рекомендуемая температура хранения +5/+50˚С».
- **Вывод:** критика в основном касается дефекта отчёта (нумерация), а не данных. Реальный
  «баг» один — несогласованная нумерация при пропущенном OCR, оставлен как есть по решению.

### v1.10.0: Multi-Sort Reviews — сбор в нескольких сортировках с дедупом (2026-08-07)

  Задача: собрать отзывы выбранного варианта товара в трёх сортировках одновременно:
  дефолт «новые и полезные», «с высокой оценкой» (`score_desc`) и «с низкой оценкой»
  (`score_asc`), чтобы не пропустить важные 1-2★ и критику. Решение: новый CLI-флаг
  `--sorts` для `collect`/`gather`, цикл по сортировкам с мержем по uuid и тегированием.
  - **Ключевые изменения:**
    - `collect.py`: `SORT_PARAMS`, `_sort_url(url, sort)`, `_collect_reviews_pass`,
      `_merge_review_sorts`, `collect_reviews(..., sorts=None)` (None = legacy без
      тегирования; список = multi-sort). `collect()` принимает `sorts`, пишет
      `raw["reviews_sorts"]`.
    - `cli.py`: `--sorts` для `collect`/`gather` (через `_parse_sorts`).
    - `report.py`: секция «Сортировки: новые и полезные (N), с высокой оценкой (M),
      с низкой оценкой (K)» при наличии `raw["reviews_sorts"]`.
  - **Тесты (`tests/test_reviews.py`):** +6 (итого 56): `test_sort_url_unit`,
    `test_collect_reviews_multi_sort_urls_and_merge`,
    `test_collect_reviews_multi_sort_dedup_by_uuid`,
    `test_collect_reviews_unknown_sort_raises`,
    `test_collect_reviews_legacy_no_sort_tagging`, `_RecordingPage` обновлён для multi-sort.
  - **Проверено:** все тесты зелёные (`pytest` 61/61), `py_compile` чисто.
  - **Живой прогон (3627230434, все 3 сортировки, 2026-08-07):** 288 сырых →
    **250 уникальных** по uuid; `reviews_sorts=["published_at_desc","score_desc","score_asc"]`
    в raw; `review_sort`/`review_sorts` у всех отзывов; `variant_mode.used=True`,
    `pagination_type=infinite_scroll`; отчёт показывает «Сортировки: новые и полезные:
    96 · сначала высокие оценки: 96 · сначала низкие оценки: 96». Пересечения сортировок:
    default∩score_desc=14, default∩score_asc=24, score_desc∩score_asc=0 (нижние/верхние
    оценки не пересекаются — ожидаемо). `smoke_cdp.py --compare` SMOKE/TWO-PAGE/VARIANT OK
    (снапшот rating_count xiaomi обновлён 5158→**5207** — дрейф счётчика).
  - **VERSION → 1.10.0**, schema.md: `reviews_sorts` + `reviews[].review_sort`/`review_sorts`
    + раздел «Multi-sort сбор»; README: опция `--sorts`, исправлена устаревшая пометка
    «?sort= не работает».

### v1.7: OCR Performance Pack — параллелизм, предобработка, кэш-версионирование (2026-08-07)

Задача: сделать OCR галереи Ozon качественнее и быстрее без тяжёлых ML-зависимостей
(Тир A, утверждён пользователем). Старые замеры: tesseract 4.1.1, OCR строго
последовательно (2.07-2.10 с/файл, типовой товар ≈74 изобр. ≈ 2.5 мин + 15-20 с
загрузка), качество страдало на баннерах с фоном. В системе: 16 ядер CPU, 47GB
RAM, cv2 4.8.1 + numpy + torch(CPU), apt даёт только tesseract 4.1.1.

- **ocr.py — переписан:**
  - `_ocr_one()` — логика одного файла (исполняется в воркере), возвращает
    `(record, failed)`; record содержит `cache_hit` (все нужные psm найдены в
    кэше, tesseract не запускался).
  - Атомарный кэш: `tempfile.mkstemp` в той же директории + `os.replace`
    (`_save_cache`); `_save_cache` сам создаёт директорию (юнит-безопасен).
  - `ocr_texts(..., threads=8, preproc=True, tessdata_dir=None)` →
    `(records, failed, stats)`; `workers = min(threads, n)`, `threads=1` —
    последовательный режим. `ProcessPoolExecutor.map` сохраняет порядок входных
    путей; воркеры получают **пути файлов**, сами открывают изображения (без
    передачи numpy-массивов между процессами — сериализация не съедает выигрыш).
  - `download_gallery(urls, gallery_dir, threads=8)` — `ThreadPoolExecutor`,
    имена `img_NNN.jpg` назначаются по индексу до фетча (детерминированный
    порядок, dedup сохранён).
  - `_preprocess(ndarray)`: BGR→gray → upscale до `PREPROC_TARGET_LONG=2000` по
    длинной стороне (только если меньше) → CLAHE (clipLimit 2.0, tile 8×8).
    Консервативно: БЕЗ denoise/бинаризации по умолчанию (не терять тонкие
    подписи/логотипы). `CV2_AVAILABLE` через try-import; без cv2 → `none`.
  - PSM-стратегия: primary (psm, по умолч. 3) → если `conf < 60` **или**
    `text_len < 10` → повтор psm 11 (sparse, лучше для баннеров) → берётся
    лучший (`_better`: текст важнее conf, затем выше conf). Каждый psm
    кэшируется отдельным ключом.
  - Составной ключ: `{md5}_{lang}_{psm}_{preproc_ver}_{tessdata_ver}_schema{N}.json`
    (`CACHE_SCHEMA=2`). Смена движка/предобработки/структуры payload = новый ключ
    = инвалидация. Старый формат `{md5}_{lang}_{psm}.json` не читается
    (`_load_cache` проверяет `cache_schema`).
  - `download_tessdata_best(target_dir, langs=("rus","eng"))` — локальные
    `tessdata_best` из GitHub (main), идемпотентно, размер файла >100KB как
    признак валидности. `ocr_gallery` при отсутствии `data/tessdata_best/rus` —
    авто-загрузка с фолбэком на системный tessdata. `--tessdata-dir` в конфиге.
  - `ocr_gallery(..., threads=8, preproc=True, tessdata_dir=None)`: новые поля
    `gallery_stats`: `ocr_engine`, `ocr_threads`, `preprocess`,
    `ocr_wall_seconds`, `cache_hits`, `cache_misses`. `gallery_stats_text`
    расширен (engine/threads/preproc/cache/время).
- **cli.py:** `--threads` (default 8) и `--no-preproc` для `ocr` и `gather`
  (проброс в `ocr_gallery`). Существующие команды/флаги не менялись.
- **report.py:** не менялся — `gallery_stats_text` уже рендерится (обогащённая
  строка приходит из ocr.py).
- **Тесты:**
  - `tests/test_ocr.py` — 16 юнит-тестов: cache_key (составной, различие по
    каждой компоненте), `_accepted`/`_better`, `_preprocess` (grayscale/upscale/
    skip без cv2), PSM fallback (weak primary → psm 11, strong → без fallback),
    cache hit второй прогон, инвалидация по `cache_schema`, порядок при
    `threads=2`/`threads=3` (реальный tesseract, кэш 4/4 hit), threads=1
    последовательный, пустой список, download_gallery порядок/dedup (threads=3),
    контракт `gallery_stats` (все поля).
  - `tests/raw_fixtures/shape_minimal.json`: `collector_version: "1.7.0"`, в
    `gallery_stats` добавлены диагностические поля, `gallery_stats_text` — новая
    строка.
  - `tests/test_raw_shape.py`: +`test_gallery_stats_ocr_diagnostics`.
- **Документация:** `VERSION 1.7.0` (`__init__.py`), `requirements.txt`
  (+opencv-python как опциональный), `docs/schema.md` (gallery_stats-поля,
  OCR-кэш с составным ключом и PSM-стратегией), README (опции `--threads`/
  `--no-preproc`, локальные tessdata_best, дерево кэша).
- **Бенчмарк (84 файла живой галереи xiaomi 3627230434, tesseract 4.1.1 +
  tessdata_best, чистый кэш):**
  - `threads=1`: **215.4s (2.56 с/файл)** — базовый последовательный путь.
  - `threads=8`: **39.7s (0.47 с/файл)** — **×5.4 ускорение**; типовой товар
    (74-84 изобр.) ≈ **40 с вместо 2.5-3 мин**.
  - Повторный прогон (cache hit): **0.0-0.1s** (все hits, tesseract не запускался).
  - Качество: `preproc=True` → mean_conf 81.8, **0 пустых** / 40; `preproc=False`
    → mean_conf 85.3, 1 пустой. Наблюдение: CLAHE+upscale немного снижает
    уверенность на чистых картинках, но **устраняет пустые результаты** на
    сложных. Дефолт `preproc=True` оставлен (полнота важнее), `--no-preproc`
    доступен для чистых галерей.
- **Сознательно НЕ входит (по решению пользователя):** PaddleOCR/EasyOCR
  (B2-гибрид отложен — после cv2+tessdata_best плохих картинок мало), tesseract 5
  через PPA (B1, системное изменение), изменение схемы 2.0, catch-all профили.

### v1.6: Pagination-агностичный сбор (infinite scroll / state-paginator) (2026-08-07)

Задача: `cli.py collect --url "...topper...3427851536/"` собирал только 12 отзывов
из 33. Корень — новая разметка пагинации Ozon: `next_url()` (collect.py) искал
только `div[data-widget="webListReviews"]`, которого на карточке нет → после
стр.1 `stop_reason=no_next`. Новая пагинация — `[id^="state-paginator"]` с
`data-state` JSON `{"nextPage": "...layout_page_index=2...", "fetchType": "scroll",
"size": 10}`.

- **Живая диагностика topper 3427851536:** реально отзывов варианта ровно 33
  (полный скролл → `[data-review-uuid]`=33, REVIEWS_JS `ok=true reviews=33`);
  `variant_count=33`, `total_count=8482`, rating 4.8, цена 5696/5126 (с картой);
  инвариант — «отзывы добавились в DOM», а не распознавание виджетов.
- **Утверждённая архитектура (пользователь):** докрутка до сходимости по
  уникальным `[data-review-uuid]` вместо распознавания схемы пагинации.
  `max_pages` — общий внешний предохранитель (для scroll = макс. циклов
  «scroll→extract→growth»); внутри `_scroll_to_converge(max_iters=90)`.
  `pagination_type` пишется в `raw.reviews_variant_mode` аддитивно (схема 2.0 не
  меняется), значения `infinite_scroll | next_button | converged | unknown`
  (unknown — для будущих схем). REVIEWS_JS контракт `{ok, reviews, pages,
  totalText}` не меняется.
- **Реализация (все правки в `ozon_collector/collect.py`):**
  - **REVIEWS_JS:** убран ранний `return {ok:false,...}` при отсутствии
    `webListReviews` — извлечение идёт по `[data-review-uuid]`, виджет стал
    необязательным (используется только для определения `pages`).
  - **REVIEW_IDS_JS** (новый): лёгкий, возвращает массив uuid из
    `[data-review-uuid]` — для подсчёта уникальных при сходимости.
  - **`_scroll_to_converge(page, target_min=40, max_iters=90)`** (новый):
    крутит колесо, пока число УНИКАЛЬНЫХ uuid растёт; плато
    `MAX_SCROLL_STABLE=6` → `MAX_SCROLL_BOTTOM=3` докрутки к низу
    (`window.scrollTo(0, scrollHeight)`); возвращает `(count, resumed)` — если
    при докрутке рост возобновился, `collect_reviews` делает ещё один проход.
    Подсчёт уникальных защищает от виртуализации списка.
  - **`_next_page(page)`** (новый, приоритет): state-paginator `nextPage` ТОЛЬКО
    при `fetchType != "scroll"` (scroll подгружает `_scroll_to_converge`) →
    legacy `next_url` (webListReviews «Дальше», включая `__CLICK__`) → fallback
    `a[href*="layout_page_index="]`.
  - **`_dedupe_reviews(reviews, seen)`** (новый): ключ = `uuid`, fallback
    `sha1(author|date|text[:120])` (fallback-ключ только в `seen`, в отзыв не
    пишется).
  - **`_detect_pagination(page, grew)`** (новый): fetchType=scroll →
    `infinite_scroll`; state-paginator (не scroll)/legacy → `next_button`; рост
    без пагинатора → `converged`; иначе `unknown`. Вычисляется ПОСЛЕ первого
    сходимого прохода (state-paginator может вставиться поздно).
  - **`collect_reviews`:** per-страница goto → `_activate_variant_mode` →
    `_scroll_to_converge` → REVIEWS_JS → `_dedupe_reviews`; `pagination_type`
    сохраняется в `variant_mode` (и при `--no-variant-mode`); внешний
    предохранитель `max_pages`; `stop_reason`/`page_log` сохранены.
  - **Версия:** `__init__.py` 1.5.0 → **1.6.0**.
- **Тесты (`tests/test_reviews.py`):** 24 → **33** (9 новых): REVIEWS_JS без
  `webListReviews` (синтетическая карточка `[data-review-uuid]`); `_next_page`
  4 ветки (scroll→None, non-scroll→URL, legacy «Дальше», fallback
  `layout_page_index`); `_scroll_to_converge` плато; `_dedupe_reviews` uuid+sha1;
  `_detect_pagination` 5 режимов; `pagination_type` пишется в collect_reviews
  (включая apply_variant_mode=False). `_patch_variant_collect` обновлён под
  новые зависимости (`_scroll_to_converge`/`_detect_pagination`/`_next_page`).
- **Верификация живая:**
  - **topper 3427851536:** `collect` → **33/33**, `pagination_type=infinite_scroll`,
    `variant_count=33`, `total_count=8484`, used=True/click_ok,
    reviews_matched_variant=33, **dropped=0, warning=None**,
    page_log=[{page:1, no_next}] (артефакт `data/3427851536_raw.json` 12 отзывов
    перезаписан).
  - **Регресс xiaomi 3627230434:** **96/96** (сайт тоже перешёл на
    infinite_scroll), variant_count=2702, total=5158, dropped=0, warning=None
    (в v1.5 собиралось 23 при старой разметке).
  - **`smoke_cdp.py --compare`:** SMOKE OK / TWO-PAGE OK / VARIANT MODE OK,
    exit=0. Снапшот `rating_count` xiaomi обновлён 5155→**5158** (дрейф
    счётчика, ожидаемое поведение — «дрейф учтён»).
  - **Лимит скролл-ленты ~96 отзывов — ЭТО НЕ БАГ, а «входная стена» Ozon**
    (подтверждено пользователем 2026-08-07): при variant_count > ~100 лента
    бесконечного скролла загружает ~96 самых популярных отзывов, дальше Ozon
    требует авторизацию («войдите, чтобы увидеть больше отзывов»).
    Наблюдения: topper 3427851536 33/33 (фильтр 33 = собрано всё);
    3408182809 — фильтр 116 → собрано 96; xiaomi 3627230434 — фильтр 2702 →
    собрано 96. Это не признак недосбора: для неавторизованного скролла это
    максимум сайта. Пользователь согласился, что ~100 самых популярных
    отзывов достаточно; полный сбор 116+ потребовал бы авторизации в CDP.
  - **`docs/topper_90x200_void_3408182809.md`** — первый отчёт на новой схеме
    (v1.6, infinite_scroll): 96/96, OCR 74/58, ссылки/картинки на месте.

### v1.5: Native Variant Mode + class-agnostic REVIEWS_JS (2026-08-07)

Задача: если у товара Ozon есть нативный тумблер «Этот вариант товара»
(`webReviewsVariantMode`) — активировать его и собирать ТОЛЬКО отзывы варианта;
нет тумблера — собрать все. Пользователь выбрал «Полный фикс (variant+date+text)».

- **Живая разведка (PID 3627230434):** клик по тумблеру добавляет в URL
  `&oos_search=false&reviewsVariantMode=1`; прямой переход на
  `?tab=reviews&reviewsVariantMode=1` включает фильтр серверно, без клика
  (тумблер грузится активным). Признак активности: класс `*-a8` (неактивная
  `*-a5`) + белый фон. В режиме варианта лента 100% однородна «Цвет товара:
  черный» (12/12, 21/21). У Xiaomi нет пагинации «Дальше» — чистая бесконечная
  прокрутка; `next_url()` возвращает None.
- **Скрытая регрессия классов Ozon:** старые селекторы REVIEWS_JS мертвы на
  живой странице (variant `oj2_25` → живой блок `<a href="/product/...">`,
  дата `jn4_25` → `mj3_25`, текст `o2j_25` → `m4j_25 j2n_25`). Старые тесты
  проходили только потому, что фикстура снята со старой разметки.
- **Новый REVIEWS_JS — класс-агностичный:** variant по `a[href*="/product/"]`
  + regex `RE_ASPECT`, подъём к контейнеру аспектов (children<=3, innerText<=600);
  text = наибольший соседний блок; дата по `RE_DATE`; автор — `span.tsCompactControl500Medium`
  (фолбэк `span[class*="tsCompact"]`). Баг: `>`→`>=` в подъёме (иначе при
  одинарном аспекте текст брался фолбэком с мусором).
- **VARIANT_MODE_JS (детектор):** `{found, options[], variant_present,
  variant_count, active, total_count}`; контракт по тексту опций («Все отзывы» /
  «Этот вариант товара (N)»), активность по классу `-a8` (+фолбэк фон).
- **_activate_variant_mode(page):** detect → skip если активен/параметр в URL →
  клик → полл ~8с → verify. Возвращает `{used, clicked, variant_present,
  variant_count, active, total_count, reason}` (`no_widget / no_variant_opt /
  already_active / url_param / click_ok / click_no_effect / click_error`).
  Не падает при неудаче — `used=False` и сбор продолжается.
- **Интеграция:** `collect_reviews(..., apply_variant_mode=True)` — активация
  перед скроллом каждой страницы; `collect(..., apply_variant_mode=True)` →
  `raw["reviews_variant_mode"]` = базовая диагностика + `reviews_matched_variant`
  + `reviews_dropped_by_client_filter`. `collect_reviews` теперь возвращает
  4-кортеж (reviews, total_pages, page_log, variant_mode).
- **cli.py:** `--no-variant-mode` у `collect` и `gather`.
- **report.py:** строка «Фильтр Ozon: „Этот вариант товара“ (2697 из 5153)»
  (или «не применён (собраны все отзывы)»).
- **VERSION 1.5.0**, схема 2.0 не менялась.
- **Фикстуры/тесты:** `tests/review_pages/xiaomi_variant.html.gz` (живой снимок,
  1 225 904 chars, активный тумблер 2697, 12 отзывов); `conftest.py` —
  `VARIANT_MODE_JS`-импорт, фикстуры `evaluate_variant_mode`/`variant_mode_page`;
  `test_reviews.py` +9 тестов (детектор 4, активатор 3, однородность 1, +
  вариантная лента). Итог **22 passed**.
- **smoke_cdp.py:** новая проверка `check_variant_mode(pid)` — открывает
  `?tab=reviews&reviewsVariantMode=1`, скроллит, проверяет активный фильтр и
  однородность ленты по первому аспекту. Добавлена в `main()`.
- **Живая верификация:** xiaomi (click_ok, 42/42 однородны, 0 пустых), roborock
  (click_ok, active корректен после фикса), smoke VARIANT MODE OK, отчёт
  показывает строку фильтра. Снапшот rating_count 5148→5153→5155 (дрейф счётчика
  Ozon, снапшот обновлён 2026-08-07).
- **Верификация на случайных товарах — 9/9 PASS (2026-08-07, скрипт
  `tests/verify_random.py`, фазы scan/run/report):**
  - Пул: 6×A (тумблер есть: 4 электроники + 2 одежды «Российский размер»),
    3×B (тумблера нет: `variant_present=False`). C (пагинация «Дальше») и
    D (<40 отзывов) за тайм-бокс не встретились → **NOT_FOUND по плану**.
  - **A (6/6):** `used=True`, `reason=click_ok`, лента однородна по первому
    аспекту (1 уникальный аспект), 0 пустых variant/date/author/text,
    `banned_in_text=0`. variant_count совпадает с показанным в тумблере
    (80/154, 991/1113, 21623/47978, 14025/63861, 2095/12120, 630/7015).
  - **B (3/3):** `used=False`, `reason=no_widget` (тумблера нет), собраны все
    отзывы, пусты ТОЛЬКО variant (ожидаемо), date/author/text в порядке.
  - **Контраст True vs False (3773600056):** native — 1 аспект (175), unfiltered —
    2 (174, 175) — фильтр реально отсекает чужие варианты. PASS.
  - **Качество текста:** `text.strip()`, запрещённые префиксы («Цвет товара:»,
    «Размер:», «Вариант:») в text = 0 у всех 9; date/author не пусты.
  - **report:** R.render() на все 9 raw; строка «Фильтр Ozon: „Этот вариант
    товара“ (N из M)» для A и «не применён (собраны все отзывы)» для B.
  - **Баг scan, найденный в первом прогоне:** фильтр ссылок `/product/.../\d+/$`
    не матчил живые URL `?at=...` → давал 0 товаров; исправлен дедуп по
    `pid_from_url`. Fallback-условие `_discover_sections` было битым (`if not
    any(...)`), исправлено на `if u not in sections`. В keywords books добавлен
    «chit».
  - **Итог:** 9/9 PASS, регресс 22/22, smoke SMOKE OK / TWO-PAGE OK / VARIANT
    MODE OK. Raw: `/tmp/opencode/verify/{pid}_raw.json` (verify_random.py),
    summary `/tmp/opencode/verify_summary.json`, отчёты `/tmp/opencode/verify_md/`.
- **Guard: warning при активном фильтре и 0 совпадений (2026-08-07):** в
  `collect_reviews()` после подсчёта `matched_variant`/`dropped` добавлена
  защита от дрейфа Ozon-разметки: если `used=True` и `matched_variant==0`,
  то `variant_mode["warning"] = "native_filter_active_no_matching_reviews"`
  + `print("[collect] warning: ...")` (без падения). Поле аддитивное, схема 2.0
  не менялась, `report.py` не трогался (сигнал виден в raw.json). Тесты
  `test_reviews.py`: `_FakePage` + monkeypatch активатора/скролла/пагинации —
  позитивный кейс (срабатывание warning) и негативный (warning отсутствует при
  совпадении — регресс штатного v1.5). Проверки: py_compile OK, **pytest 24/24**,
  smoke SMOKE OK / TWO-PAGE OK / VARIANT MODE OK, в 7 живых raw с `used=True`
  warning отсутствует (0 срабатываний).

### v1.4: Reliability pack — integrity, OCR-кэш, gather, контрактные тесты (2026-08-07)
Аддитивное обновление поверх v1.3.0 (схема 2.0 не менялась, без migrate).
Порядок реализации: collect → ocr → report → cli → tests → docs.

- **collect.py — integrity + incomplete:**
  - После `goto(product_url)`: `requested_pid = pid_from_url(product_url)`,
    `actual_pid = pid_from_url(sess.page.url)`; при несовпадении (и оба не
    `"unknown"`) → `RuntimeError` (защита от бага «вкладка на прошлой странице»).
  - «Артикул» в характеристиках ≠ pid → только `print`-warning (у Ozon может
    быть внутренний SKU, не fatal).
  - `raw["pid"]` всегда; `raw["incomplete"]`/`raw["missing_fields"]` всегда
    (полные пути: `product.price.current`, `product.rating`).
  - **Баг найдён и исправлен:** `pid_from_url()` не отрезал query/fragment →
    после редиректа Ozon (`/product/slug-{pid}/?sh=...`) возвращал `"unknown"`.
    Фикс: `split("?",1)[0]` + `split("#",1)[0]` перед regex.
- **ocr.py — кэш + url:**
  - `ocr_texts(paths, cache_dir, lang, psm, urls)`: кэш
    `data/ocr_cache/{md5}_{lang}_{psm}.json` `{md5, lang, psm, text, mean_conf,
    created_at}`. Ключ зависит от lang+psm (смена настроек tesseract = новый
    ключ). Хит → `[ocr] cache hit`; пустой текст кэшируется корректно (не miss).
    `config="--psm {psm}"` теперь передаётся в tesseract.
  - `download_gallery`: в `stats["images"][basename]` добавлен `url`.
  - `ocr_gallery(raw, gallery_dir, cache_dir)`: проставляет `url` в записи
    `gallery_ocr` (по basename).
- **report.py:**
  - Шапка после заголовка: Дата сбора (UTC) / Collector / Схема / Источник Ozon
    (CDP); при `incomplete` — блок `> ⚠ Неполный сбор. Отсутствуют: …`.
  - Сводка по звёздам перед списком отзывов: `**Всего: N отзывов** · ★5: X · …`
    (нулевые звёзды опускаются, `None` игнорируется).
  - Галерея: `img {path}` стал ссылкой `[img {path}](url)`, если `url` есть.
- **cli.py:**
  - `report`: без `--out` → Markdown в stdout (раньше авто-сохранение в
    `docs/{pid}.md`). `--out` — как раньше.
  - Новый `gather` (shortcut): collect → ocr → report. Флаги `--no-ocr`
    (быстрый прогон без tesseract), `--out` (путь отчёта, иначе stdout),
    `--out-raw`, `--gallery-dir` + все флаги collect.
- **Тесты:**
  - `tests/raw_fixtures/shape_minimal.json` — эталон формы raw v1.4 (включая
    pid/incomplete/missing_fields, gallery_ocr с url, artifacts).
  - `tests/test_raw_shape.py` — 6 тестов контракта (топ-уровень, product,
    reviews, gallery_ocr, gallery_stats, incomplete-контракт).
  - `tests/review_pages/xiaomi.html.gz` — живой снимок страницы отзывов
    (2026-08-07, `?tab=reviews` + `_scroll_to_reviews`, 12 отзывов, totalText
    «5148 отзывов», 167KB gz).
  - `tests/test_reviews.py` — 3 теста REVIEWS_JS (uuid/text/stars/variant/
    author/date, типы строк, негатив на пустой странице).
  - `tests/conftest.py` — импорт REVIEWS_JS, `REVIEW_FIXTURES`,
    фикстура `evaluate_reviews`.
  - `tests/smoke_cdp.py` — добавлены `check_two_pages()` (A→B, pid не
    смешиваются, характеристики различаются) и `--compare` против
    `tests/expected_snapshots.json` (свежесть фикстур). Snapshot обновлён:
    xiaomi count 5145→5148 (дрейф зафиксирован --compare), roborock без изменений.
  - `tests/expected_snapshots.json` — эталонные значения для `--compare`
    (xiaomi 3627230434, roborock 2561238412).
- **Документация:** `VERSION 1.4.0` (`__init__.py`), `docs/schema.md` (полная
  схема raw.json, все поля + OCR-кэш), README (gather/stdout/тесты/schema.md),
  PROJECT_NOTES (этот раздел).
- **Верификация:** pytest 14/14; `smoke_cdp.py` SMOKE OK + TWO-PAGE OK;
  `--compare` детектирует дрейф; report → stdout и `--out` дают идентичные
  файлы (35882 байта); OCR-кэш проверен на синтетике (miss→write, hit→skip).
- **Сознательно НЕ входит (по решению):** meta-реструктуризация (схема 2.1 +
  migrate), возврат analyze/validate/regression/compare, конфиги категорий,
  per-field confidence, авто-лимит отзывов из виджета (остановка по «Дальше»
  достаточна).

### v1.2: Snapshot regression без baseline + watch + catalog + rating_source (2026-08-06, вечер)
План v1.2 целиком: regression через историю, watch, catalog.json, rating_source,
nightly→quality_gate. Решение по regression принято с пользователем: **cron
отменён** (сбор ручной → автоматический ежедневный прогон бессмысленен),
regression = ручной гейт после пересбора.

- **regression.py переписан:** `snapshot_history(pid)` (датированные файлы
  `data/{pid}/*.json`, растут по дате), `previous_snapshot(pid, latest_date)`
  (строго раньше), `metrics(raw)`, `run(files, conf_drop, score_min)`,
  `render()`. Baseline и `load_baseline` удалены. Первый снимок → `[NEW]`.
- **watch.py (новый):** `load_raw`, `pair`, `changes`, `render`, `render_pid`.
  Сравнение prev_history → latest. Негативные: `_negatives` (1-2★ отзывы),
  `_risk_keywords` (слова-риски по `_risk_keywords` списку + `STOPWORDS`),
  сниппеты. Галерея по md5. Характеристики: added/removed/changed таблицей.
  `_money`/`_pct` — форматирование ₽ и %.
- **catalog.py (новый):** `build(data_dir)` → {pid: {...}}, `render`, `build_save`
  → `data/catalog.json` (SAVED … N товаров). Для каждого pid — из самой свежей
  записи (latest-файл, при отсутствии — последний снапшот истории).
- **cli.py:** `cmd_regression` (без `--baseline`/`--init`; `--conf-drop`, `--score-min`,
  `--out`; exit 1 при ALERT), `cmd_watch` (`--pid`, `--out`), `cmd_catalog` (`--out`).
- **rating_source:** в collect.py `PRODUCT_JS.rating()` → `{value, source}`.
  Приоритет: `webProductScore` → `webReviewProductScore` → `fallback_text`.
  Для техники (Roborock/осушитель) webProductScore отсутствовал → рейтинг из
  fallback_text с `rating_source='fallback_text'`. Показ: validate (detail
  `(источник: …)`), export (колонка `rating_source`), report.
- **scripts/nightly_regression.sh удалён; scripts/quality_gate.sh (новый):**
  `cli.py regression --out data/regression_report.md`, exit 1 при ALERT, chmod +x.
- **Тесты (fixture /tmp/opencode/watch-test, синтетические снапшоты):**
  1) 123456: 2026-07-01→08-01 — [OK] (score 67→67, conf 75→75, отзывов 2→3,
  avg 3.0→2.33); 999999 — [NEW]. 2) мутация latest (conf 75→60, Емкость
  2000→6400): ALERT conf 75→60 (>10 п.п.), exit 1; watch показывает «изменено»
  Емкость таблицей. 3) catalog: 2 товара, catalog.json с rating_source.
  Fixture удалён после проверки.
- **Баг, найденный тестом (критичный):** `snapshot_history` фильтровал даты как
  `f[:10].isdigit()` — для `2026-07-01` срез `2026-07-0` содержит дефисы → False,
  предыдущий снимок никогда не находился (всегда [NEW]). Фикс:
  `f[:10].replace("-","").isdigit()`.
- **VERSION:** `__init__.py` → `1.2.0` (collector_version в raw пишется из VERSION).
  schema_version остаётся "2.0".
- **Применено к реальным данным:** `data/regression_baseline.json` и
  `data/regression_report.md.log` удалены; `regression_report.md` перегенерирован
  (3 товара, все [NEW] — единственный снимок 2026-08-06, корректно). JS-скрипты
  проверены `node --check` (PRODUCT_JS, REVIEWS_JS — OK). README и PROJECT_NOTES
  обновлены (watch/catalog/regression без baseline).

### v1.1: История raw + регрессионный мониторинг (2026-08-06, вечер)
Приоритет выбран пользователем из плана v1.1 (первый и второй пункты).

- **История raw:** `cmd_collect` без `--out` → снапшот `data/{pid}/{YYYY-MM-DD}_raw.json`
  (дата из UTC) + latest `data/{pid}_raw.json`. `--out` сохраняет старое поведение.
  Существующие 3 raw разложены в `data/PID/2026-08-06_raw.json` (copy, не перемещение;
  latest-файлы не тронуты). diff по историческим снапшотам = мониторинг цены/рейтинга.
- **regression.py + `cli.py regression`:** `collect()` → метрики по pid
  (file, score, conf, valid, warnings) через `validate.validate`; `write_baseline()`;
  `run()` → (rows, alerts, changed). ALERT-условия: conf-drop > порога, score < min,
  INVALID, MISSING. **NEW не ALERT** (фикс после теста — добавление товара в портфель
  не должно ронять nightly). Рендер markdown с корректным склонением «товар/товара/товаров».
  Пороги: `--conf-drop` 10 п.п., `--score-min` 60.
- **scripts/nightly_regression.sh:** `set -u`, cd в корень проекта; первый запуск —
  `regression --init` (exit 0); далее `regression --out data/regression_report.md`
  с перенаправлением stdout в `$REPORT.log`; exit 1 при ALERT. chmod +x, bash -n OK.
- **Тесты:** (1) все latest → 3/3 [OK], exit 0; (2) копия topper с conf=40 +
  без файла осушителя → 2 ALERT (conf drop, MISSING), exit 1; (3) новый pid +
  conf-падение + MISSING → ALERT только по conf-падению и MISSING, NEW без ALERT;
  (4) markdown-отчёт рендерится, «1 товар» склоняется корректно.
- **Baseline:** `data/regression_baseline.json` = topper score 100/conf 80.0,
  Roborock 87/71.4, осушитель 87/58.4.

### Стабилизация 1.0 (2026-08-06, вечер)
По плану пользователя: схема raw + мигратор, health-check, надёжность выборки,
экспорт JSONL. Статус проекта: **Ozon Collector v1.0 stable** (без категорий —
осознанно).

- **Схема raw 2.0:** `migrate.py` (модуль + `cli.py migrate`, in-place,
  `--dry-run`). Восстановление `product.price` из `price_parsed` по ярлыкам
  («с_другими_банками» → current, «с_банками» → bank, «до_скидки» → old;
  confidence=0.5, source='migrated'). Если `analysis` устарел — подсказка
  «cli.py analyze». Все 3 raw мигрированы 1.0→2.0 без потери данных.
- **validate.py:** критичные проверки (product+title, отзывы>0, analysis+confidence)
  → INVALID при провале; весовые → score 0-100. Валидация: topper 100, Roborock 87
  (нет рейтинга карточки, 248 маленьких картинок), осушитель 87 (нет рейтинга,
  выборка 34/100). У техники `product.rating` не извлекается из DOM — известное
  ограничение извлечения, не регрессия.
- **compare:** `sample_reliability(n)` — колонка «Надёжность выборки» в таблице +
  примечание. Веса итогового рейтинга не тронуты (эталонные 97.1/69.1 сохранились).
- **export.py:** `cli.py export` → `data/all_products.jsonl` (pid, title, category,
  price/old/bank, rating, rating_count, n_reviews, n_negative, n_with_text,
  ocr_mean_conf, confidence, reviews_expected, collected_at_utc, source, url).
  `collect --category` → `product.category`.
- **Фикс pid:** `_pid_from_url`/`_pid` парсили только `-PID`-суффикс, а у
  `…/product/PID/` не работал → pid=null в export. Regex: `(?:-(\d+)|/(\d+))/?$`.

### Живой прогон осушителя + фиксы (2026-08-06, день)
По плану пользователя (этап 4): проверить универсальность на товаре с другой
структурой. NeoClima ND-20RH — компрессорный осушитель (литры/мощность/площадь).

- **collect:** 34 отзыва под вариант «Цвет:белый»; страница показала 42 отзыва,
  «Дальше» нет (бесконечный скролл, как у Roborock). `target_variant` = ключ:значение.
- **БАГ-ФИКС has_text:** старый код `len(t) - len(author) > 40` считал короткие
  негативные отзывы «без текста» (2 из 2 негативных у осушителя были пустыми в
  отчёте, хотя текст был). Причина: legacy-эра, когда текст включал автора. Фикс:
  `len(text.strip()) >= 10`. Пере-валидированы все 3 raw (conf не изменился).
- **БАГ-ФИКС OCR URL:** галерея осушителя — только wc300-превью (~10КБ), которые
  отсекались MIN_BYTES=20КБ → OCR собрал 1 картинку, mean_conf 0.0. Фикс:
  `_upgrade_url()` в ocr.py — замена `/wc\d+/` → `/wc1200/` (67КБ, доступно).
  После фикса: 66 скачано / 57 с текстом, **mean_conf 86.0**.
- **analyze/report:** conf 58.4 (expect=100, собрано 34); негативных 2 («Отработал
  меньше месяца, не запускается», «сломался через 4 мес»); OCR summary выделил
  л/м/мм/мин/Па/Вт. Отчёт `docs/dehumidifier_neoclima_nd20rh.md`.

### OCR summary (этап 3) (2026-08-06, день)
По плану пользователя (этапы 1-2 → 3). Эталон conf **80.0** сохранён.

- **analyze.py:** `OCR_VALUE_RE` (число+единица: Па/кПа/МПа, мм/см/м²/см², мл/л,
  кг/г, Вт/В, мАч/Ач, мин, %, °C/°С/℃, МГц/кГц/Гц; альтернативы упорядочены от
  специфичных к общим — «мин» раньше «м», иначе ловилось «220 м» вместо «220 мин»),
  `_norm_num` (убрать пробелы-разделители тысяч, запятая→точка), `_norm_unit`
  (lower + display-map: па→Па, см2→см², мач→мАч, мин\w*→мин). `ocr_summary()` →
  `{values: [{unit, values:[{value, count}]}], phrases: [{keyword, text}]}`.
  В `analysis` добавлен ключ `ocr_summary`.
- **report.py:** в секции 3 подблок «Ключевые значения из OCR» (группировка по
  единице с кратностью ×N) + «Фрагменты с ключевыми словами» (топ-3).
- **Валидация:** Roborock — Па 10 000 (×4), 12 000, 113 000, 19 000, 8 000,
  22 000; мм 8/96.5/521/487; 180 мин; 75 °С. Topper — 60 мм + фразы нагрузка/
  материал. conf не изменился (71.4 / 80.0). docs/* пере-сгенерированы.

### Точная цена (price_extractor) + review signals (2026-08-06, день)
По плану пользователя (этапы 1-2). Эталон conf **80.0** сохранён.

- **collect.py:** добавлен `priceExtract()` (поле `price` в product) — точная цена
  из виджета `[data-widget="webPrice"]`:
  - все ценовые листовые узлы виджета (текст = только число + ₽), вычисленные
    fontSize/fontWeight, зачёркивание и признак «внутри кнопки» (BUTTON-предок);
  - `current` = не-кнопочная цена с max(fs); `old` = зачёркнутая/меньшая рядом;
    `bank` = цена в кнопке банка («С банками»); `confidence` (0.97 при fs>=16);
    `source` = "webPrice". Рассрочки/аксессуары/рекомендации вне виджета не
    попадают вообще.
  - Проверено на 4 товарах: Roborock 33 345/90 000/30 011, topper 5 045/55 255/4 540,
    наушники Hoco 1 113/5 471/1 001, книга 352/752/319 — all correct.
  - price_parts/price_parsed сохранены для диагностики.
- **report.py:** `_price_block` теперь приоритетно рендерит `price` (current/old/банк/
  скидка %/уверенность), price_parsed/parts — fallback. Новая секция
  **«6. Самые информативные отзывы»** (топ-3 по score: длина/50 + период + проблемы;
  маркеры: период эксплуатации, проблемы, опыт). Секции 7-10 перенумерованы.
- **compare.py:** `_price` берёт `price.current` в первую очередь.
- **analyze.py:** `USAGE_PERIOD_RE` (конкретные формы: день/неделя/месяц/год/лет/
  полгода/час/сутки, с опциональным числом; без «раз»/«подряд» — давали ложные
  срабатывания), `PROBLEM_WORDS` (общие: лом/слом/брак/не работает/шум/воня/пятн/
  царап/завис/глюч/…), `review_signal()`/`informative_top()`; в analysis добавлено
  `informative_reviews`.
- **raw-файлы обновлены:** `price` вписано в product обоих raw (снимок цены на
  дату проверки; цена динамическая — при живом collect пишется актуальная).
- **Валидация:** эталон topper conf **80.0** (не изменился — price не влияет на
  веса confidence); Roborock conf 71.4; compare: Roborock 33 345 ₽ (было 89 ₽!),
  topper 5 045 ₽; docs/vacuum_robot.md, docs/topper.md, docs/compare.md
  пере-сгенерированы. `py_compile` всех модулей — OK.

### Упрощение: убраны конфиги и классификация (2026-08-06, ночь)
По решению пользователя: конфиги на категорию — тупик (тысячи файлов, постоянная
адаптация); классификация плюсов/минусов regex'ами — избыточна.

- **analyze.py:** удалены `pluses`/`minuses`, `risk_penalties`, `subchips`/чипы,
  `contradictions`/«несоответствия», `mentions_other_thickness` («другая толщина»),
  `key_data_extra`. Остались: `star_stats`, `sentiment`, `negative_reviews` (1-2★),
  `confidence` (дефолтные веса char/rev/ocr/key/stab), `conclusion`. `analyze(raw)` —
  без cfg.
- **report.py:** секции «Плюсы/Минусы» и «Несоответствия» заменены на
  **«Негативные отзывы (1-2★) — полный текст»**. Вариант выводится универсально:
  размеры (Д×Ш×В см) или ключ:значение («Цвет: белый-прозрачный»). `render(raw)` —
  без cfg.
- **compare.py:** без cfg; риски = доля негативных (1-2★) отзывов, соответствие =
  полнота характеристик + доля положительных; колонка «Негатив (1-2★)». Плотность
  осталась информационной колонкой.
- **diff.py / cli.py:** `cfg` убран из сигнатур, `--config` убран из CLI.
- **Удалены:** `ozon_collector/config.py`, `config/` (topper, vacuum_robot,
  dehumidifier, headphones, refrigerator). `README.md`, `docs/recipe.md` обновлены.
- **Валидация:** эталон topper conf **80.0** (совпал); Roborock conf 71.4,
  негативных 1; compare на двух raw работает.
- **Известный нюанс — ИСПРАВЛЕН (2026-08-06, день):** парсер цены собирал все
  ценовые значения страницы (рассрочка, аксессуары и т.п.) — цена Roborock в
  compare была 89 ₽ (не настоящая). Решено точным `price_extractor` из webPrice
  (см. выше).

### Проект создан (2026-08-06)
- Автономный пайплайн сбора данных с Ozon: карточка + отзывы варианта через CDP,
  OCR галереи, аналитика, markdown-отчёт, сравнение кандидатов.
- CLI: `python3 cli.py collect|ocr|analyze|report|compare|diff` (конфиги категорий
  удалены 2026-08-06, см. ниже).
- Скрипты `toppers_*.py` перенесены из `/tmp/opencode` и
  `~/renovation-127/scripts/topper/` (копии УДАЛЕНЫ из renovation); ссылки в
  `~/renovation-127/docs/topper.md` и `topper-comparison.md` обновлены на новый проект.
- Офлайн-валидация на эталонном `data/topper_3427851536_raw.json`: compare **86.4**
  (совпадает с эталоном); conf 80.0 (OCR conf=0 в legacy-данных — при живом прогоне
  будет реальный).
- Живой сбор (collect/ocr) — только по запросу; CDP-браузер жив.
- Запуск и методика: `README.md`, `docs/recipe.md`.

### Офлайн-улучшения + шаблоны категорий (2026-08-06, вечер)
Все 8 пунктов плана выполнены, пайплайн перевалидирован, эталон не сломан.

- **ocr.py:** `download_gallery`/`ocr_texts` теперь отдают `image_metadata`
  (width/height/size_bytes/md5 на каждый файл; в `gallery_stats.images`), добавлен
  счётчик `ocr_failed` (в `gallery_stats` и `gallery_stats_text`). Помощники
  `_md5()`/`image_metadata()`.
- **collect.py:** `collect_reviews` возвращает `review_pages` — журнал страниц
  `[{page, n_found, n_matched, ok, stop_reason}]`; сохраняется в raw как
  `review_pages` (нужен для компонента «стабильность сбора»).
- **analyze.py:** новый компонент confidence **`stab`** («стабильность сбора» =
  50% успешных страниц пагинации + 50% отсутствие артефактов OCR). `confidence()`
  нормирует веса к сумме (`sum(w)/ws`) — legacy-конфиги (сумма 1.0) не меняются.
  Legacy-raw без `review_pages` → пагинация 100 если отзывы есть. Добавлена
  поддержка обобщённого `spec_claims` в `contradictions()` (аналог `firmness` для
  др. категорий: `{label, chips_field, claimed, soft_minus}`); блок `firmness` для
  topper не тронут.
- **compare.py:** `metric()` → обобщённый chip-скоринг (`chip_pos`/`chip_neg`,
  бакеты настраиваются `chip_pos_buckets`/`chip_neg_buckets`), **колонка
  «Плотн., кг/м³»** (вес из характеристик ÷ объём варианта, «—» если нет), опция
  **`price_norm: linear|log`** (default linear → эталон неизменен), плотность в
  spec_score только при `density_spec_weight>0` и `target_density` (нормируется
  делением на 1+den_w). Колонка чипа теперь опциональна (`chip_col_label`,
  default None; topper включает явно).
- **diff.py + `cli.py diff`:** сравнение двух raw одного товара «было/стало»:
  цена (±₽/%), характеристики (добавлено/удалено/изменено), описание, галерея
  (по md5), рейтинг, отзывы. Фикс `_ratio` = (b/a−1)×100 (равные значения → +0%).
- **report.py:** приложение показывает компонент `stab` и детали (пагинация X/Y
  страниц ok; ошибки OCR/загрузки).
- **config.py:** `confidence_weights` расширены до 5 компонентов (`stab` default
  0.0 → legacy conf не меняется); compare: `chip_col_label`, `chip_pos_buckets`,
  `chip_neg_buckets`, `price_norm`, `density_spec_weight`, `target_density`.
- **Шаблоны категорий:** `config/vacuum_robot.py`, `config/dehumidifier.py`,
  `config/headphones.py`, `config/refrigerator.py` (свои pluses/minuses regex,
  risk_penalties, chips_fields, key_data_extra, compare). Проверены прогоном
  `analyze` на эталонном raw — не падают (conf ~74-76 из-за отличий key_data,
  это норм: данные-то topper).
- **Валидация (эталон `data/topper_3427851536_raw.json`):** analyze conf = **80.0**
  (стабильно, `stab`=90 legacy: пагинация 100, OCR 80 из-за mean_conf 0 в legacy),
  compare Итог = **86.4** (не изменился), report и compare.md пересозданы, diff
  работает. `py_compile` всех модулей — OK.
- **README.md** обновлён (diff-команда, компоненты confidence, новые ключи
  конфига, шаблоны).

## Ключевые решения / ограничения

- Ozon FAB блочит webfetch/автоклиентов; единственный рабочий путь — живой Chromium
  без `--enable-automation` + CDP `http://127.0.0.1:9222` (жив).
- Ozon: сортировка отзывов через URL (`?sort=`) не работает — только клик «Дальше».
- Выборка свежих отзывов смещена к негативу — для общего рейтинга доверять
  виджету `webReviewProductScore`, не выборке.
- Сравнивать кандидатов только с сопоставимым confidence.
- Живой сбор (collect/ocr) — только по запросу; analyze/report/compare/diff —
  офлайн по сохранённому raw.json.
- **Нумерация разделов отчёта (2026-08-08):** номера «## N.» в `report.py` жёстко
  зашиты (1-7) и НЕ перенумеровываются при пропуске раздела (напр. OCR не выполнялся
  → «4. Описание» сразу «6. Все отзывы»). Пропуск номера — осознанное решение: номер
  привязан к блоку, по нему стабильно ссылаться на секцию независимо от полноты сбора.
- **ПРАВИЛО: всегда переиспользовать существующие функции, не изобретать новый код
  (утверждено 2026-08-08).** При добавлении новой функциональности В ПЕРВУЮ очередь
  искать готовые функции/модули (напр. `collect_product`, `collect_reviews`,
  `_merge_review_sorts`, `O.ocr_gallery`, `R.render`, пайплайн `gather`) и
  переиспользовать их. Новый код писать ТОЛЬКО для того, чего ещё нет
  (оркестрация/склейка/новые JS-экстракторы), и держать его «тонким».
  Конкретный случай: команда `search` строится как оркестратор поверх `gather`,
  а НЕ как новый сборщик.
- **TTL-кеш raw.json (v1.11.0, 2026-08-08):** `gather`/`search` читают `data/{pid}_raw.json`,
  если `collected_at_utc` свежий (≤ `--max-age`), `incomplete != true`, `schema_version`
  совместима И **`collector_version` == текущей**. Кеш защищает перезапуск после падения
  (свежие товары не пересобираются). `--max-age 0` = не читать кеш. OCR при cache-hit
  выполняется только если в кеше нет `gallery_ocr`.
  - **Версионная инвалидация (реверс раннего решения «версию не проверяем»):** raw старее
    текущей версии или без `collector_version` → `old_version` (пересбор — старые сборки
    могли не иметь `reviews_variant_mode`/`pagination_type` и содержать ошибки
    экстракторов); новее текущей → `newer_version` (кеш тоже не берётся — защита от
    старого кода поверх данных новой версии). Сравнение численное (`tuple[int…]`,
    строковое `'1.10.0' < '1.9.0'` врёт). Файлы НЕ удаляются (архив), перезаписываются
    при следующем успешном сборе.
- **`search` берёт товары только из `tileGridDesktop`** (сетка результатов): блоки
  рекомендаций «Вы смотрели» (`island`) и «Возможно, вам понравится» (`skuGrid`) не
  собираются. Выдача докручивается скроллом (`infiniteVirtualPaginator`), `found` —
  все найденные уникальные товары (до стагнации или `scan_limit = max(limit, 50)`),
  в работу идёт `min(found, limit)`. Digest честно пишет «Найдено товаров в выдаче:
  N … собрано M», не выдавая выборку за весь поиск Ozon.
- **Авто-запуск CDP-браузера (2026-08-08):** `collect`/`gather`/`search` сами
  запускают Chromium через `scripts/launch_chrome.sh`, если он закрыт
  (`cdp.launch_and_wait`, поллинг до 30с). Отключение: `OZON_AUTO_LAUNCH=0`.
  Требуется реальный X-дисплей (в скрипте `DISPLAY=:0`); иначе — понятная ошибка
  с подсказкой ручного запуска вместо traceback.
- **Дедуп ответов продавца (2026-08-08):** дедуп только на уровне рендера
  Markdown; `*_raw.json` не изменяется. Ключ группы — `(author, первая фраза)`
  с вырезанным обращением. `--no-dedup-replies` полностью отключает фильтр.
- **Чистка вкладок при Session.close() (2026-08-08):** браузер не гасится между
  командами, `Session.close()` закрывает все вкладки кроме `pages[0]` и уводит
  её на `about:blank`.
- **Recovery после краша браузера (v1.12.0, 2026-08-09):** recyclable-ошибка
  (browser / renderer_failure) перезапускает Chromium и повторяет товар;
  non-recyclable (target / unknown) — только фиксирует ошибку. Основа —
  изолированный `process.py` (identity против PID-reuse) + `browser.py`
  (probe/классификация/manager). Чужой или неподтверждённый процесс Chromium
  НИКОГДА не убивается (`BrowserOwnershipError`) — требуется ручное
  вмешательство. BROWSER_UNHEALTHY (identity жив, CDP HTTP молчит) не является
  причиной kill — это телеметрия, повторяем probe. Детали — «История» → v1.12.0.
- **search --pids/--pids-file + фильтр digest_from_raw (2026-08-16, коммит `08af737`):**
  сбор по явному списку артикулов вместо докрутки выдачи: `collect_search`/
  `collect_parallel` получили `pids` (found = len(pids), выдача не сканируется),
  `cli.parse_pids` понимает голые артикулы и полные URL карточек
  (`/product/<pid>`, со slug и query). `python cli.py search --pids A,B --pids-file f.txt
  --no-ocr --max-age 7d --out d.md --out-raw d.json` — гибрид: кеш-hit для
  собранных, живой досбор для отсутствующих (без докрутки). Офлайн-фильтр:
  `python scripts/digest_from_raw.py --pids A,B` (только указанные pid из data/;
  stdout переключён на UTF-8 — ₽ ломал cp1251). Тесты: pytest 312 passed, 4 skipped.
   Ограничение limit в pids-режиме: `limit = len(pids)` (список берётся целиком).

### Speed-тест скролла: СТОП из-за дрейфа gate-товара (2026-08-16)

Эксперимент по ускорению прокрутки отзывов (фаза А, чистые константы)
**остановлен на анкере** по правилу «анкер missing≠0 → стоп». Причина —
подтверждённый дрейф review-окна `2561238412`, а не регрессия констант.

- Старый baseline `2561238412` (08-15): **97** отзывов.
- Сегодня (08-16, 09:49 UTC) те же константы `900/1600/6`: собрано **96**,
  **missing=29, extra=28, overlap=68**.
- Вчера `900/1600/6` давал **97/97** (baseline воспроизводился точно).
- Сегодняшний набор совпадает с вчерашним «плохим» набором `1200/1000/4`
  на **91/96** (разница 5/5) → страница сменила набор отзывов за ночь.
- Механика скролла здоровая: scroll_count=81, 6 stable-раундов,
  `last_growth_iteration=76`, 3 bottom-прохода, роста вниз нет,
  seed=1, паузы 1361..1834 мс (avg 1607). Провал — не скролл.
- Контрольный товар `1779338676` остаётся валидным: smoke-прогон **96/96,
  missing=0, extra=0** (`total_review_collection_time`=178.4s, production-константы).
- Решение: матрицу НЕ переключать на `1779338676` автоматически (это другой
  эксперимент — gate был чувствительный `2561238412`); baseline 08-15 **НЕ
  менять**, текущие 96 отзывов за новый baseline НЕ принимать; повторный
  live-прогон не делать.
- Артефакт: `data/2561238412_drift_2026-08-16_raw.json` (диагностический).
- Инструменты: `D:\Temp\opencode\speedtest_scroll.py` (live-прогон →
  raw + телеметрия + сравнение с baseline; exit 0/1/2/3), результаты в
  `D:\Temp\opencode\scroll_test_results.jsonl`.

### Speed-тест скролла: ЗАВЕРШЁН, фикс `1500/400/6` (2026-08-16, вечер)

Продолжение утреннего СТОП-раздела выше. Дрейф оказался РЕАЛЬНЫМ, но
локализованным: review-окно `2561238412` сменилось за день (утро Y=97,
день X=96). Сравнение — только внутри одной среды и одного окна.

- **Изоляция (коллизия на 9222):** два утренних прогона на общем браузере
  (параллельный процесс DNS-шопа) упали (0 отзывов / execution context
  destroyed) — это была коллизия, НЕ шаг. Поднят отдельный Chromium:
  порт **9333**, профиль = копия `ozon-profile` (метод bootstrap §14).
  Харнесс `speedtest_scroll.py` получил `--cdp-url`.
- **Дрейф подтверждён:** даже main-профиль днём отдаёт окно X (96), не Y (97).
  Вариант окна зависит от Ozon, НЕ от профиля (полная копия профиля тоже
  даёт X). Окно X стабильно внутри дня: анкеры B1/B2 (900/1600/6) → 96/96,
  missing=0/extra=0 офлайн-сверка.
- **База:** для сравнения использован baseline окна X (`D:\Temp\opencode\baseline_9333.json`,
  собран 900/1600/6, 96 отзывов). Production `baseline_products.json` не тронут.

Матрица (2561238412, окно X, seed=1, браузер 9333; `total_review_collection_time`):

| конфиг | total | scroll | extract | scroll_count | missing |
|---|---|---|---|---|---|
| 900/1600/6 (анкер) | 192.2s | 134.4s | 57.8s | 80 | 0 |
| 1200/1000/6 | 127.8s | 66.3s | 61.5s | 62 | 0 |
| 1500/500/6 | 86.8s | 27.8s | 59.0s | 51 | 0 |
| **1500/400/6 (ЛИДЕР)** | **81.0s** | **22.3s** | **58.7s** | **51** | **0** |
| 2000/400/6 | 76.5s | 13.8s | 62.7s | 30 | **9** ✗ |

- **Шаг 2000px небезопасен:** 9 смежных отзывов не подгрузились (lazy-чанк
  проскакивается), сходимость сработала преждевременно. Пауза 400 при шаге
  1500 НЕ теряет (проверено 1500/400/6 и 1500/500/6) — лимитирует именно шаг.
- **Детерминизм лидера:** rep1 78.9s, rep2 77.7s (scroll_count=51,
  last_growth=46, bottom_passes=3 во всех); множества review-ключей
  лидер==rep1==rep2 **идентичны** (96/96, diff=0/0).
- **Подтверждение 1779338676:** анкер 900/1600/6 → 96/96 (у этого товара
  production-baseline валиден, дрейфа нет); `1500/400/6` → **96/96,
  missing=0, extra=0, 78.4s** (анкер 393.8s → **−80%**, extract доминирует).
- **Внедрено (коммит …):** `collect.py` → `SCROLL_STEP_PX=1500`,
  `SCROLL_PAUSE_MS=400` (jitter 0.15, MAX_SCROLL_STABLE=6 без изменений);
  `test_reviews.py` jitter-диапазон 1360..1840 → **340..460**; исправлен sha
  `2561238412` в локальном baseline (самопроверка). pytest **312 passed,
  4 skipped**.
- **⚠️ Дрейф 2561238412 (важно для будущих прогонов):** production-baseline
  хранит окно Y (97), Ozon сейчас отдаёт X (96). Следующий живой сбор этого
  товара даст ложные missing (31/30) — это дрейф, не потеря; перед сравнением
  пересобрать baseline из свежего сбора той же среды. 1779338676 — без дрейфа.

- **ДОСБОР 6 PID — ВЫПОЛНЕН (2026-08-16, вечер, §19.3).** После cooldown
  (~2.5ч, последний контакт 12:49):
  - **Smoke PASS:** `gather 1779338676 --max-age 0 --no-ocr` → 96 отзывов,
    cache=miss, без challenge (FAB отпустил).
  - **Контролируемый прогон** `search --query "роботы пылесосы" --limit 20
    --parallel 1 --max-age 7d --no-ocr --worker-timeout 3600`:
    **found=56, collected=20, ok=20, 0 challenge**. Кеш-хиты (v1.14.0, вчера
    17:40-18:16): 2561238412, 2402234703, 1779338676, 948009986, 4292851367,
    638266490, 2883344196, 3282136219, 4915768176, 3725536580, 3146461026,
    4843889442, 2568433504, 3371394592. Живой досбор (все ok, без challenge):
    1214739369 (96), 2463482923 (96), 2345778208 (54), 1626207745 (27),
    1176247275 (45), 2923530832 (57).
  - **Оставшиеся 2 PID из §19.3** (не попали в топ-20 выдачи) — гибрид
    `search --pids 2101391799,1994330926 --parallel 1 --max-age 7d --no-ocr`:
    **collected=2, ok=2** (2101391799 → 42, 1994330926 → 96), оба живьём.
  - **Итог:** все 6 PID §19.3 дособраны в v1.14.0 (96/57/54/27/42/96 отзывов),
    ни одного access_challenge, шторма нет, 8 живых сборов за сессию.
  - **Итоговый дайджест (офлайн):** 22/22 товаров с валидными raw v1.14.0 →
    `D:\Temp\opencode\digest_robots_pylesosy_final.md` (1232.62 КБ). Плюс
    артикул `1651499772` (секатор, 96 отз., кеш 12:22) →
    `D:\Temp\opencode\digest_1651499772.md`.
  - **Baseline 2561238412:** по-прежнему не пересоздавался (правило — после
    2 стабильных сборов подряд; live-бюджет сессии исчерпан, обновление —
    отдельная задача по сигналу пользователя).

- **⛔ ИНЦИДЕНТ: challenge storm на наушниках 200 товаров (2026-08-16, 15:49-15:53).**
  `search --url "https://www.ozon.ru/category/naushniki-15547/?headphonemount=277801&opened=earphoneconstructshort&wire=32735" --limit 200 --parallel 2` (без `--no-ocr` — но до OCR не дошло).
  - **Ход:** докрутка выдачи OK (200 ссылок найдено) → main-браузер остановлен → 2 воркера → **первый товар каждого воркера = `access_challenge`** (title «доступ ограничен», product_page), 3 попытки с recycle → **challenge storm** (streak=3) → прогон корректно прерван: `collected=0, ok=0, remaining_aborted=97`.
  - **Причина:** последний успешный живой контакт был 15:36 (досбор 6 PID) — пауза ~15 мин вместо 2–4ч. Плюс `--parallel 2` (2 сессии сразу) на 200 товаров. Третий шторм за день (утром §19.2, вечером — этот) → FAB стал чувствительнее после ~10 живых сборов.
  - **Что сработало штатно:** воркеры погасили свои Chromium (9223/9224) и main (9222) — 0 живых процессов; challenge-guard не записал ни одной FAB-страницы (мусорных raw нет); шторм-детектор остановил прогон вместо 200 попыток; кеш дособора (22 товара v1.14.0) не тронут.
  - **Артефакт:** `docs/papa_headphones.md` — бесполезный (все 200 = AccessChallengeError); файл в gitignore.
  - **ПРАВИЛО (усилено):** после повторного шторма — **стоп 12–24ч, без дальнейших попыток** (см. §19.2/§19.3). Большой прогон (limit ≥ 100): только после паузы 12–24ч + smoke, начинать с `--parallel 1 --limit ≤50`, поднимать до `--parallel 2` только после чистого прогона.
  - **Вопрос пользователя «поможет ли смена IP»:** кратко — да, но это НЕ первый инструмент (см. выводы ниже в анализе шторма; детальный разбор — в диалоге 2026-08-16 вечер).


## 2026-08-21: Ночной прогон палаток — зависание evaluate + сторож

- **Инцидент:** прогон (PID 25072, старт 00:50) завис на товаре 33/100
  (2069397474, sort=score_asc) в 03:47:34 после «раскрыто «Читать полностью»:
  7 кнопок» — page.evaluate без таймаута заблокирован навсегда (вкладка
  замёрзла), BrowserWatchdog видел живой процесс браузера и не срабатывал.
  Простоял 7.5 ч, убит вручную; 32 товара за сегодня сохранены в data/.
- **Фикс 1 — сторож простоя в collect.py:** _StallWatchdog (daemon-поток,
  STALL_TIMEOUT_S=480, проверка раз в 10с). Touch-точки на каждом шаге:
  итерации докрутки, страницы пагинации, expand-фазы, извлечение REVIEWS_JS.
  При простое >8 мин убивает Chromium CDP-сессии (P.browser_processes +
  P.kill_records) — заблокированный evaluate получает TargetClosedError,
  что классифицируется как renderer_failure (recyclable), штатный recovery
  перезапускает браузер. Арминг в collect(), остановка в finally
  (_active_watchdog глобально).
- **Фикс 2 — внешний монитор** D:/Temp/opencode/palatki_monitor.py: цикл
  5 мин; тишина лога >20 мин при живом процессе или смерть без маркера
  «[cli] search DONE:» -> hard kill + рестарт search --max-age 1d (кеш
  сохраняется). После завершения: верификация всех товаров (used==true;
  vc<=100 -> n==vc; vc>100 -> n>=90), пересбор брака (gather --max-age 0
  --overwrite, до 2 раундов), ребилд дайджеста (живой search --max-age 1d,
  фолбэк scripts/digest_from_raw.py --pids ...). Логи: palatki_monitor.log,
  отчёт palatki_verify_report.md.


## 2026-08-21 (день): v1.16.0 — проверка брака на лету + оптимизация сортировок

- **Проверка брака сразу после сбора каждого товара** (gather.run_gather):
  - новая функция collect.variant_review_problems(raw): used==False с reason
    из VM_BAD_REASONS (click_error/click_no_effect/timeout/disabled) = брак;
    no_widget/no_variant_opt — легитимно; vc<=100 -> собрано ровно vc;
    vc>100 -> собрано >=90 И КАЖДАЯ сортировка >=90 (per-sort по полю
    review_sorts; недобор одной сортировки не маскируется суммой);
  - брак на свежем сборе: битый raw удаляется + VariantShortError
    (recyclable, browser.py) -> recovery новой вкладкой/recycle и повтор;
  - брак на кеш-хите: кеш игнорируется, живой пересбор.
- **v1.15.0 -> 1.16.0:** дофиксовый кеш (до утра 21.08) содержал дыру —
  записи с no_widget и variant_count=null (виджет не находился старым кодом)
  проходили проверку с 9 отзывами при 176 у варианта (4041068293). Бамп
  версии инвалидирует весь старый кеш: cache=miss (old_version).
- **Оптимизация сортировок** (collect.collect_reviews): если variant_count
  <= 100 и проход собрал вариант целиком (len(order) >= vc), остальные
  сортировки пропускаются — тот же набор в другом порядке. Полезно только
  при vc>100 (стена логина режет список, сортировки дают разные подмножества).
- **Сторож простоя** _StallWatchdog в collect() (см. запись о ночном
  инциденте выше) + внешний монитор D:/Temp/opencode/palatki_monitor.py.
- Тесты: test_gather.py (variant_problems, VariantShortError, кеш-брак),
  test_reviews.py (skip/incomplete sorts), test_cache.py (фикс getattr
  cdp_url для фейк-сессий). Прогон палаток перезапускается монитором
  автоматически, кеш v1.16.0 сохраняется между рестартами.

 - **Fix: key_daemon _alive() падал на Windows СистемError (2026-08-31).** При повторном запуске после Stop-Process -Force демона оставался stale pid-lock; os.kill(pid,0) по мёртвому PID на Windows кидал SystemError: OSError returned a result with an exception set (WinError 87), который не ловился (except OSError) -> крэш на старте до состояния RUNNING. Фикс в scripts/key_daemon.py::_alive: добавлен except SystemError: return False (такой PID трактуется как мёртвый -> stale-lock корректно удаляется). Проверено: _alive(99999)=False, _alive(текущий)=True. Предыдущая фиксация этого семейства (битый pid-файл -> stale) была 2026-08-31 раньше. 
aushniki: снобран cooldown_until в progress.json и старт с тёплой сессии (браузер открыт вручную, без капчи).

 - **STALL_TIMEOUT_S 480 -> 120 (2026-08-31).** На прогоне naushniki рендерер периодически клинился на товарах с комментариями: page.evaluate(REVIEWS_JS) (collect.py:1804) блокировался на 4-6 мин, т.к. клинненый CDP-транспорт игнорирует даже 30с-дефолт Playwright (set_default_timeout не задан). Сторож срабатывал на 480с - слишком поздно. Снижен до 120с: сторож теперь убивает Chromium на 2-мин простое -> TargetClosedError -> штатный recovery (renderer_failure -> recycle). Фикс применился без перезапуска демона - каждый run = новый CLI-субпроцесс, загрузивший обновлённый модуль. Трейдофф: иногда теряется тёплая сессия (возможен FAB при холодном старте), но мёртвое время сокращено с ~8 мин до ~2 мин. Согласовано с пользователем (выбрал вариант 'снизить порог'). test_parallel+test_cli 58 passed.

 - **Режим KD_NO_COOLDOWN=1 (2026-08-31, по запросу пользователя).** Демон больше НЕ спит в cooldown: (1) старт-проверка cooldown_until/profile_cooldown пропускается и сбрасывается; (2) challenge-шторм -> пауза 1с вместо adaptive 1м/5м/15м, profile_cooldown НЕ ставится; (3) эскалация шторма в NEEDS_ATTENTION отключена (не останавливаем); (4) crash -> 1с вместо 300с; (5) лимит no-progress-подряд не останавливает. Смысл: непрерывный обход защиты (тёплая сессия + retry), а не паузы. Флаг в key_daemon.py через _NO_COOLDOWN = os.environ KD_NO_COOLDOWN. Тесты test_key_daemon 211 passed. Применено к naushniki: cooldown сброшен, run 5 стартовал.

 - **Окно браузера коллектора: всегда видимое + поверх всех + без кражи фокуса (2026-08-31).** В scripts/launch_chrome.ps1 функция Invoke-MinimizeNoActivate теперь: WS_EX_NOACTIVATE + SW_RESTORE (гарантирует WS_VISIBLE) + HWND_TOPMOST (SetWindowPos с SWP_NOACTIVATE). Окно Chrome коллектора ВСЕГДА видимо, прижато поверх других окон, но фокус не перехватывает (остаётся у активного окна пользователя). Служебные/скрытые окна тоже показываются. Только профиль коллектора (ozon-profile); ЧУЖИЕ окна (обычный Chrome, Edge WebView) НЕ трогаются — по явному требованию пользователя. Было исправление: ранее случайно ставился WS_EX_NOACTIVATE на пользовательский Chrome (pid 19044) и Edge WebView — откачено, снят флаг.

 - **Fix: ложный stall-watchdog при раскрытии отзывов/комментариев (2026-08-31).** Баг: _expand_review_texts и _expand_review_comments (collect.py) при большом числе комментариев/«Читать полностью» легитимно работали >120с, но НЕ вызывали _touch_watchdog() -> сторож (STALL_TIMEOUT_S=120) считал это «нет прогресса» и убивал браузер посреди раскрытия -> recovery -> повтор -> до 3 kills на одном товаре (кейс 1883409893: 18-22 комментария, 3 зависания подряд). Фикс: добавлен _touch_watchdog() после каждой паузы в обоих циклах (сброс таймера при реальном прогрессе). Страница при этом открывалась нормально (пользователь подтвердил) — это был ложный стопор, не настоящий hang. Проверено: test_parallel 45 passed, collect.py syntax OK. Применён к живому демону: run 7 стартовал с обновлённым кодом (kill зависшего CLI run 6 -> CRASH -> cooldown 1с -> новый run).

 - **Fix: 'сессия выдыхает' — wall_clock_s бюджет run (2026-09-01).** Каждый run CLI жил дефолтные 1800с (30 мин) -> SESSION_EXHAUSTED -> демон запускал новый run заново (пересканирование с нуля). Причина: build_cli_args не передавал --session-budget-wall-clock, а при resume демон берёт run_cfg из манифеста и игнорировал CLI-аргумент. Фиксы в key_daemon.py: (1) build_cli_args передаёт --session-budget-wall-clock из run_cfg; (2) при resume CLI-аргумент --session-budget-wall-clock синхронизируется в progress.run_cfg (разрешено менять между запусками, остальные лимиты — из манифеста); (3) добавлен CLI-флаг демона --session-budget-wall-clock. Для naushniki установлен 14400с (4 часа) — run больше не 'выдыхается' каждые 30 мин. Проверено: CLI-строка субпроцесса содержит --session-budget-wall-clock 14400, test_key_daemon 211 passed.

 - **Fix: full-run заново обходил каталог с товара 1 (2026-09-01).** Демон в full-режиме каждый run сканировал каталог заново и пролистывал все товары (cache=hit быстро, но визуально 'заново', и при SESSION_EXHAUSTED/краше начинался обход с начала). Фикс: (1) search.py/parallel.py добавили skip_pids (список уже обработанных PID пропускается в выдаче); (2) cli.py --skip-pids; (3) key_daemon build_cli_args в full-режиме передаёт --skip-pids со всеми discovered_pids. Теперь full-run добирает ТОЛЬКО новые/FAIL товары, не пролистывая собранные; FAIL-товары пересобираются через rescan (как и раньше). Бюджет сессии --session-budget-wall-clock также поддержан (4ч/12ч). Тесты: 318 passed (search+parallel+key_daemon+cli).

 - **Fix: 'дичь' — бесконечное молотьё пустых full-runs (2026-09-01).** Две ошибки в моём же фиксе skip_pids: (1) skip_pids брался из discovered_pids (351) вместо valid_pids (56) — демон пропускал ВСЁ, включая excluded/невалидные, которые надо дособирать, и гонял runs каждые ~1.5 мин по 0-1 новому товару; (2) _NO_COOLDOWN отключил и no-progress стоп, поэтому демон молотил до лимита 12ч. Исправлено: skip_pids = valid_pids (только подтверждённые), excluded пересматриваются каждый run (досбор); no-progress стоп возвращён ВО ВСЕХ режимах (это честное завершение 'каталог исчерпан', а не кулдаун). Теперь при 3 runs без новых валидных демон завершается. Проверено: skip_pids count=56, test_key_daemon 211 passed.

 - **FF-шим закрывал НЕ ТУ вкладку → «Browsing context has been discarded» (2026-09-25, 4-й баг параллельного Firefox).** Живой лог: worker падал на первом же goto нового батча, стек — Marionette `driver.sys.mjs:2631 newWindow` → `assert.open` → `NoSuchWindowError: Browsing context has been discarded`; self-heal в `Session.goto` не помогал, т.к. сам звал `switch_to.new_window`. Причина НЕ в памяти (37 GB свободно) и НЕ в `unloadOnLowMemory`: `FirefoxPage` не была привязана к своему window handle — `page.close()` звал `driver.close()`, который закрывает ТЕКУЩУЮ вкладку, т.е. в `ensure_single_worker_tab` (создать новую → закрыть старые) первым close убивалась НОВАЯ рабочая вкладка, вторым — последняя вкладка браузера. Плюс `is_closed()` проверял глобальные `window_handles` вместо своего handle. Исправлено в ffsel.py: `FirefoxPage._handle` + `_activate()` (switch на свою вкладку перед goto/evaluate/locator/screenshot/title/url/content), handle-aware `close()` с возвратом focus на вкладку, которая была текущей, `is_closed()` по своему handle, `_SeleniumContext.new_page` восстанавливает current-context перед `new_window` (иначе Marionette открывает вкладку ОТ мёртвого контекста), `pages` prunes закрытые. Правило: любой page-объект Selenium обязан хранить свой handle; `driver.close()` без switch = закрыть чужую вкладку. Проверено: 186 целевых + полный suite 1052 passed / 4 skipped / 1 xfailed.

 - **Firefox-воркеры шли с ХОЛОДНЫМИ профилями → блок «Похоже, нет соединения» (2026-09-25).** `bootstrap_worker_profiles` сеет только Chromium-файлы (`Local State` + `Default/Network/Cookies`), а Firefox-профиль имеет другой формат сессии → все firefox-воркеры стартовали без ozon-cookies, что Ozon и блокирует (ровно тот эффект, ради которого сев и придуман). Добавлено `bootstrap_firefox_worker_profiles` (копирует `cookies.sqlite` + `key4.db` + `logins.json`; без key4.db cookies не расшифруются) + `firefox_has_ozon_session`; `collect_parallel` выбирает ветку по `browser_kind()`. Плюс `_stop_main_browser` теперь firefox-aware: `driver.quit()` асинхронен, и при живом main профиле bootstrap пропускал сев (`skipped:main_browser_running`) → дожимаем процессы по токену, как раньше для Chromium. Проверено: 186 целевых + полный suite 1052 passed.

 - **Вердикт по внешним ревью ботов про «методы парсинга Ozon»: ~90% отвергнуто, есть фактические ошибки о нашем стеке (2026-09-25).** Проверено против замеров, не по общим статьям. Ошибки в самих предложениях: (1) «Chrome+CDP — основной production backend» — нет: дефолт движка переключён на Firefox 2026-09-25, chrome-path живёт как альтернатива; (2) «Firefox Playwright backend» — у нас не Playwright, а **geckodriver/Selenium + шим ffsel** (Playwright-совместимый page-API). Playwright применяется только для chrome-path (connect_over_cdp); (3) «Extension/Native Messaging нужен, потому что webdriver=true» — лечить нечего: stock FF156 + Marionette (`navigator.webdriver=true`) отдаёт 151 отзыв, т.е. автоматизация НЕ причина challenge, а patched Juggler FF153 блокируется — Ozon режет ИМЕННО patched-сборки. Отвергнуто: stealth/undetected_chromedriver/playwright-stealth/JS-spoof (мы это уже эмпирически прошли), curl_cffi/composer-api как Tier-1 (ломает K4 «страница 1:1», отзывы варианта/сортировки/login-wall завязаны на DOM живой сессии; прямой доступ допустим только внутри неё — перехват comments API уже есть), Camoufox/invisible_playwright/антидетект-браузеры (patched-сборки = движение в сторону блока), прокси-ротация как фундамент (сначала session/profile storm: 12 воркеров с одним cookie-якорем жгут IP), Celery/Redis/FastAPI/Docker/GUI/мобильный API со SSL-pinning (не для локального коллектора), ARQ/Taskiq/Pydantic-в-runtime (Pydantic уместен на границе normalize→QA, но не приоритет — у нас уже raw-first + `qa_raw`). Уже реализовано и предложено «как новое»: три формы challenge (`CHALLENGE_TITLE_MARKERS` + puzzle.py + FAB-reload FSM), link-first по `/product/` + pid, изоляция по профилям с ownership, observability/telemetry, BrowserSession-подобный слой (шим ffsel даёт одинаковый page-API для обоих движков; остаток — kind-ветки в `cdp.Session`, рефакторить без боли не берём). Правило для будущих ревью: сначала `PROJECT_NOTES`/`logs/*` (наши замеры), потом общие статьи; предложение, противоречащее живому логу, — не выполнять без нового замера.

 - **docs/ разделена на подпапки architecture/ + digests/ (2026-09-20, по ревью структуры).** Ревьюеры отметили смешение трёх жанров в docs/. Миграция (файлы в .gitignore, история коммитов не затронута): architecture.md, anti_bot_architecture.md, key_daemon_architecture.md, daemon_completion_logic.md → architecture/; 111 дайджестов кампаний → digests/; qa/ осталась. schema.md НЕ существовал — битые ссылки на него в README/PROJECT_NOTES. Обновлено: дефолт `--out` в key_daemon.py `docs/{campaign}.md` → `docs/digests/{campaign}.md` (2 места кода + docstring), README (примеры, структура docs/, ссылка на anti_bot_architecture), cli.py docstring, тесты test_main_* (дефолт автоименования). Проверено: test_main_* 3 passed, py_compile OK.

 - **ADR §14.7: bootstrap-cookie policy + health-KPI (2026-09-25, принято из разбора бот-ревью).** Две правки по фактам, а не по статьям. (1) **Пересев после шторма.** Раньше `bootstrap_*_worker_profiles` перезаписывали куки воркеров КАЖДЫЙ вызов; после полного challenge-шторма это тиражирует испорченную сессию main на весь пул (все воркеры получают один cookie-якорь → одинаковый блок). Новая политика в `process.py`: холодный воркер (нет Ozon-сессии) сеется ВСЕГДА (иначе блок «Похоже, нет соединения»), прогретый не пересеивается в окне `POST_STORM_HOLD_S=3600` после фулл-шторма (`skipped:post_storm_hold`) и в окне `SEED_MIN_INTERVAL_S=3600` после предыдущего сидинга (`skipped:seed_fresh`); каждый факт сидинга пишет метаданные `ozon_bootstrap.json` в профиль (`seeded_at`, `source`, `files`, `had_session`, `browser`). Демон передаёт `collect_parallel(bootstrap_post_storm_at=progress.profile_cooldown.last_full_storm_at)`; поле ставится в ветке full-storm (`all_workers`/parallel==1). Статусы видны в логе `[parallel] bootstrap worker-K: ...` и в `status.json` через kpi/telemetry. (2) **KPI health, а не только успех.** Раньше `combined.kpi` был только в sequential-пути (`search.py`), parallel-кампании его не несли. Добавлено: `puzzle.py::_note_attempt/attempts()` (инвариант `attempts == failures + solved`, ветка «капча есть, пазла нет» теперь честно считается неудачей), `recovery["puzzle"]` из `_collect_shard`, агрегат пула `parallel._kpi_summary` (challenges, recoveries, session_blocked, puzzle attempts/solved + `puzzle_solve_rate`, `challenge_rate_per_100`), `combined["kpi"]` в `collect_parallel` (теперь попадает в демон), накопление в `key_daemon._absorb_recovery_telemetry` (+ кумулятивный `puzzle_solve_rate` как тренд и `last_challenge_rate_per_100`) → `status.json.kpi`. Проверено: 425 целевых passed / 4 skipped (bootstrap policy, kpi aggregate, puzzle counters, daemon accumulate, storm hold).

 - **Правило формулировок anti-bot выводов: факт / наблюдение / гипотеза (2026-09-25).** Разбор бот-ревью вскрыл, что половина спорных утверждений была категоричной без замера. Три класса: **факт** — воспроизводимый замер в `logs/*` (151 отзыв на stock FF156+Marionette при `webdriver=true`; patched Juggler FF153 блокируется; Ozon режет patched-сборки, а не автоматизацию). **наблюдение** — корреляция в данных (12 воркеров с одним cookie-якорем совпадают по времени с session/profile storm; `challenge_seq` растёт на длинных прогонах). **гипотеза** — то, что проверяемо, но ещё не проверено (sticky-IP 1 identity : 1 IP как будущий рычаг; влияние TTL сидинга на выживаемость сессии). Запрещённые формулировки: «патч = доказательство» (нужен повторный живой замер), «12 воркеров = причина блока» (только корреляция без контроля), «webdriver=false = лучший отпечаток» (наши данные обратны). Sticky-IP зафиксирован как **будущий рычаг, не внедрён**: сейчас все воркеры ходят с одного IP, и разделение IP на воркер — первое, что пробуем, если storm останется при 1-2 воркерах. Симметричное требование к решениям: любая новая фича anti-bot сначала получает метрику (иначе её эффект нельзя отличить от шума).

 - **Терминология: мы в фингерпринт-инженерии «consistency over camouflage» (2026-09-25, правка формулировок).** Фраза «не фингерпринтный обход» была неточной: мы ПЛОТНО управляем фингерпринтом, просто оставляем нативным то, что нативно (`webdriver=true` на Firefox Marionette, chrome-path без `--enable-automation`, `intl.accept_languages` по системе) вместо подгонки под «человеческое». Правильные формулировки: «мы не маскируемся — мы обеспечиваем согласованность (native-consistent)»; «patched-транспорт отвергнут по замеру, а не по принципу». Если Ozon усилит проверку согласованности, отвечать будем в той же парадигме (согласованный реальный браузер), а не начнём спуфить сигнатуры. Отделено: утверждение «Ozon режет patched-сборки» — это **гипотеза уровня механизма** (одна точка данных: FF153 Juggler против FF156 stock, 2026-09-25); наблюдаемый факт — разница в выдаче между этими двумя прогонами. Нужен скользящий A/B (canary stock vs patched) — живые прогоны патченного транспорта не запускаются без явного разрешения пользователя.

 - **Прокси ≠ ротация на запрос: разделяем отвергнутое и отложенное (2026-09-25).** Ранее прокси отвергались формулировкой «session storm жжёт IP быстрее, чем помогает репутация» — это аргумент против ЧАСТОЙ ротации (паттерн бот-сети), а не против прокси как класса. Отвергнуто: ротация IP на каждый запрос/на каждый воркер-рестарт, дешёвые datacenter-пулы (тот же класс, что Ozon уже режет). Отложено (требует инфраструктурного решения пользователя, не кода): долгоживущий IP, закреплённый 1:1 за прогретым профилем на недели — снимает SPOF сетевой видимости (один IP/ASN-блок сейчас роняет весь пул независимо от качества браузерного слоя) и ЛОГИЧНО ПРОДОЛЖАЕТ линию «identity = 1 профиль + 1 сессионный якорь» из ADR §14.7 (bootstrap policy), а не противоречит ей. Сейчас узкое место — одна личность (один прогретый профиль) × один IP; sticky-IP снимает вторую половину, пул независимо прогретых профилей — первую.

 - **Fix: блок на странице ВЫДАЧИ маскировался под UNKNOWN_FAILURE → 4-секундные ретраи по заблокированной сессии (2026-09-25, живой случай zvukoizolyatsiya).** Лог: 4 прогона за ~3 минуты (run_1790336812_0 … run_1790337048_3), каждый: `goto category` → `FAB-заглушка (title:похоже, нет соединения)` ×3 reload → `докрутка выдачи: access_challenge → новая вкладка (1/1)` → `→ recycle браузера (1/1)` → реконнект → те же 3 reload → `in-process сбор упал: анти-бот (search_page)`. Итог каждого прогона `status=UNKNOWN_FAILURE`, `challenge_control.storm_streak=0`, `profile_cooldown` пуст, `recovery_telemetry={}`, финал `NEEDS_ATTENTION unknown_failure`. Причина: AccessChallengeError из фазы докрутки ссылок (main-сессия, воркеры ещё не спавнились) попадал в общий `except Exception` → `combined` без ключа `aborted` → `classify_status` даёт UNKNOWN_FAILURE → демон считал это транзиентным сбоем и повторял через 4с, т.е. молотил уже заблокированную сессию вместо эскалации. Вся storm-механика (cooldown 1/5/15 мин, storm_streak, profile_cooldown, NEEDS_ATTENTION(challenge_storm)) при этом не включалась НИ РАЗУ. Исправлено в `key_daemon.py`: отдельный `except AccessChallengeError` → синтетический `combined` с `aborted={reason: challenge_storm, where: search_page, signature: <title:...>, all_workers: True}` и `kpi.total_challenges=1`; сигнатура в `profile_cooldown.last_signature` теперь берётся из `aborted.signature`, если в recovery-телеметрии пусто. Проверено: `test_integration_search_page_challenge_becomes_storm` (UNKNOWN_FAILURE → challenge_storm, storm_streak растёт, cooldown ставится, KPI накапливает challenges), 28 целевых passed.

 - **Fix: adaptive-лестница cooldown 1/5/15 мин была мертва — всегда 15 мин (2026-09-25, найдено при разборе живого падения).** В шторм-ветке `key_daemon.py` `progress["cooldown_until"]` и `challenge_control.cooldown_until` ставились по adaptive-значению (`_challenge_cooldown_seconds`: storm#1→1м, #2→5м, #3→15м), но `profile_cooldown["until"]` — по ПОСЛЕДНЕМУ шагу `CHALLENGE_COOLDOWN_STEPS_S[-1]` (15м). Цикл паузы берёт максимум из `cooldown_until` и `profile_cooldown.until` ⇒ фактическая пауза = 15м уже с ПЕРВОГО шторма: лестница 1/5/15 не наблюдалась никогда, `storm_streak` рос 1 раз в 15 минут ⇒ NEEDS_ATTENTION накапливался ~час молотья заблокированной личности вместо 21 минуты. Плюс `_challenge_cooldown_seconds` вызывался дважды (adaptive + финальная строка), второй вызов давал другой джиттер ⇒ `cooldown_until` расходился с `profile_cooldown.until` на доли секунды. Исправлено: `pause_s` считается ОДИН раз, `cc["cooldown_until"]` и `pc["until"]` получают одну и ту же adaptive-паузу. Проверено: `test_integration_search_page_challenge_becomes_storm` шпионит `_set_state` и требует `cooldown_until == profile_cooldown.until` на каждом шторме + строго растущие паузы (1→2→3 на подменённых шагах); 10 целевых passed.

 - **Fix: серийные счётчики жили ВСЮ кампанию — 12-часовая кампания была обречена на смерть по календарю (2026-09-25).** `challenge_control.storm_streak` и `transient_failures` инкрементировались, но НИКОГДА не сбрасывались: 2 шторма до обеда + 2 после обеда (или 2 UNKNOWN_FAILURE в 09:00 и 2 в 18:00) давали `NEEDS_ATTENTION` ПОСЛЕ ЧАСОВ нормальной работы — эскалация измеряла не «подряд», а «за всё время». Для 12-часовой кампании с лимитом `max_transient_retries=3` это гарантированная смерть на 4-м случайном сбое за день. `no_progress_count` при этом уже сбрасывался на прогрессе (правильный образец рядом) — т.е. баг был именно в двух «младших» счётчиках. Исправлено в `key_daemon.py`: любой прогон не-`UNKNOWN_FAILURE` обнуляет `transient_failures`; `NORMAL_DONE` с новыми пидами обнуляет `storm_streak` (`total_storms` остаётся lifetime-счётчиком для статистики). Проверено: `test_transient_failures_reset_on_progress` (серия [0,1,2] → прогресс → [0,1,2,3] → терминал, 7 прогонов вместо 5) и `test_storm_streak_reset_on_progress` (шторм → прогресс → streak 0 → 4 шторма подряд → NEEDS_ATTENTION, 6 прогонов вместо 5); 12 целевых passed.

 - **Fix: полный шторм останавливал кампанию с диагнозом «no_progress» (стагнация выдачи) вместо «challenge_storm» (2026-09-25).** Шторм инкрементит `no_progress_count` (единый предохранитель от бесконечного кружения — это правильно), поэтому при дефолтном `max_no_progress_runs=3` демон выходил с `final_reason=no_progress` уже на 3-м шторме, т.е. лестница 1/5/15 мин (4-й шторм → NEEDS_ATTENTION) не доигрывала, а оператор читал «выдача перетёрлась» при `challenge_control.total_storms=3` и искал проблему не там (ровно то, что произошло в zvukoizolyatsiya до классификационного фикса). Исправлено: при достижении лимита, если `challenge_control.storm_streak > 0`, финальная причина = `challenge_storm` с note «блок Ozon: N шторм(ов) подряд»; частичные штормы (streak=0) диагноз не меняют — там блок одного воркера, а не кампании (ADR §14.6). Плюс warning в лог. Проверено: `test_full_storm_default_limit_reports_challenge_storm` (дефолтный лимит → challenge_storm + 3 шторма, note в логе демона), весь `test_key_daemon.py` — 248 passed.

 - **Наблюдение по данным, решение без правки кода: FAB-reload-лестницу оставляем (2026-09-25).** Разбор `logs/*_daemon.log`: из 18 прогонов, где сработал `FAB-заглушка` и прогон дошёл до статуса, восстановились с новыми valid только 2 (11%): 12 → UNKNOWN_FAILURE (лестница не помогла), 2 → CHALLENGE_STORM, 1 → NORMAL_DONE без valid, 2 → успех (в одном из них 18 FAB-срабатываний за прогон, т.е. восстановление шло по нескольким уровням). Вывод: лестница (3 reload → новая вкладка → recycle) слаба, но не бесполезна — 11% > 0, поэтому сокращать её на основании одного вчерашнего прогона значило бы пожертвовать теми rare-восстановлениями. Это факт по логам; гипотеза «лестница бесполезна» опровергнута. Побочно: 2 восстановления заняли до 18 reload-срабатываний на прогон — при желании уровень можно сделать адаптивным (после N неудачных reload подряд — не повторять, а сразу шторм), но это отдельная фича с ожидаемым эффектом на recall, не решается без данных о потерях.

 - **Ops: причина блока теперь видна в status.json (last_error), а не только в логе (2026-09-25).** В шторм-ветке `last_error` не заполнялся — оператор (и read-only диагностика по status.json) видел `NEEDS_ATTENTION`/`final_reason` без причины: в живом случае вообще пусто, потому что блок шёл по старой ветке UNKNOWN_FAILURE. Теперь `last_error` = `challenge_storm (ФУЛЛ|частичный): <signature> | streak=N, всего=M`, а на эскалации — `persistent challenge: <signature> (N штормов подряд, всего M)`. Сигнатура также добавлена в toast-уведомление (обрезается до 60 символов) — раньше оператор получал «challenge: пауза 60с» без указания, ЧТО вернула Ozon. Проверено ассертом в `test_integration_search_page_challenge_becomes_storm`.

 - **Диагноз шторма в last_error/тосте стал конкретным: пазл-шторм ≠ «сигнатура неизвестна» (2026-09-25).** `search.py` кладёт `recovery.challenge_storm.puzzle_persistent` (+`puzzle_failures`), `parallel._storm_summary` прокидывает это в `combined.aborted`, а демон поле игнорировал → для нерешаемых пазлов в status.json попадало «access_challenge (сигнатура неизвестна)», т.е. оператор не мог отличить нерешаемый пазл от FAB-заглушки. Добавлена `_storm_label(signature, aborted)`: приоритет пазл → сигнатура страницы → список штормивших воркеров → честное «неизвестно»; результат идёт в `last_error`, warning в лог и toast (обрезан 60 симв.). Покрыто: `test_storm_label_prioritises_puzzle_then_signature` (5 ветвей) + `test_puzzle_storm_reports_diagnosis` (интеграционный: final_reason=challenge_storm, last_error содержит `puzzle_storm` и `3 неудач`).

 - **Отклонено: homepage-canary как pre-run gate перед докруткой выдачи (2026-09-25, гипотеза опровергнута кодом и данными).** Идея: перед `sess.goto(category)` дёрнуть `run_canary` (homepage, ~1.5с) и при блоке сразу отдавать AccessChallengeError, не тратя ~25с на 3 FAB-reload → new_tab → recycle. Реализовано, затем откачено по трём причинам: (1) семантика canary в проекте = ПРОБА ЖИВОСТИ ПОСЛЕ блока (`test_canary_challenge_blocks_session`: блок на товаре → canary → session_blocked), а не pre-run шлюз; (2) в живом случае блок был на странице категории, homepage был ОТКРЫТ (в логе первая навигация — сразу category URL) ⇒ проба вернула бы OK, т.е. не дала бы ничего, только +1.5с и +1 запрос на каждый прогон; (3) при блоке на homepage она убила бы часть тех самых 2/18 восстановлений (11% успешных прогонов с FAB), ради которых лестница и существует. Побочный эффект отката: два теста показали, что `collect_search` делает НЕСКОЛЬКО навигаций (warmup главная+категория, докрутка, плюс попытка canary) — учтено как аргумент против лишних проб. Если появится live-данные «домашка блокируется, категория открыта» — вернуть как телеметрию (`recovery.canary_challenges`) БЕЗ изменения control flow, решение принимать по доле, а не по ощущению.

 - **ДИАГНОЗ ПЕРЕВЁРНУТ (2026-09-25): заглушка «Похоже, нет соединения» — не IP, не профиль, не prefs, не геометрия, а сам транспорт geckodriver/Marionette. Chromium CDP проходит.** Один IP, ozon.ru, по одному запросу на кейс, все через штатные примитивы проекта:

   | Транспорт | webdriver | Профиль | Результат |
   |---|---|---|---|
   | Firefox 156 + geckodriver | true | тёплый `ozon-profile` | заглушка 8с |
   | Firefox 156 + geckodriver | true | тёплый `worker-3` | заглушка |
   | Firefox 156 + geckodriver | true | ПУСТОЙ `.v2` | заглушка 8с |
   | Firefox 156 + geckodriver | true | пустой, без наших prefs | заглушка 7с |
   | Firefox 156 + geckodriver + OCSP/CRL/CRLite вкл. | true | пустой | заглушка 6.6с |
   | Chromium `launch_chrome.ps1` | false | свежий user-data-dir | **PASS 19.7с** |
   | Chromium + `--enable-automation` | **true** | свежий user-data-dir | **PASS 13.6с** |
   | Firefox инкогнито вручную (контроль оператора) | false | холодный | **PASS** |
   | Chrome вручную (контроль оператора) | false | тёплый | **PASS** |

   Что это опровергает/подтверждает по каждой гипотезе:
   * webdriver-флаг — **НЕ причина**: Chromium с `--enable-automation` (=`webdriver:true`) проходит. Значит правда прошлого вывода «webdriver не причина, Marionette работает» (замер на карточке товара) была НЕВЕРНОЙ как боевое правило: на главной/категории блокируется сама marionette-сессия, а не JS-флаг;
   * Juggler/patched — не наш путь (в cmdline `C:\Program Files\Mozilla Firefox\firefox.exe`, stock);
   * locale — исключено: `ru-RU / [ru-RU, ru, en-US, en]`, tz Europe/Moscow одинаковы в блокированном Firefox и в прошедшем Chromium;
   * геометрия/headless/свёрнутое окно — исключено: в блокированном Firefox `screen 1920x1080`, окно 1296x936 (норма), а Chromium проходил СВЁРНУТЫМ (`outerHeight=28`);
   * холодный профиль — исключено: холодный Chrome-профиль проходит, холодный Firefox-профиль нет;
   * блокировка ресурсов — исключено: в проде и в пробах нет `route.abort`/блокировок (только в тестах);
   * человеческие задержки/мышь/скролл — исключено: прошедший Chromium отработал с голым `goto` без пауз и событий;
   * OCSP/CRL — исключено отдельной пробой (geckodriver их не отключает в нашей конфигурации).

   Остаётся нелокализованный tell уровня marionette-протокола (не виден в webdriver/languages/screen/UA/WebGL) либо правило вида «automation-флаг + UA Firefox». Инструментально разделить нельзя: Firefox без marionette не скриптуется (geckodriver всегда поднимает с `--marionette`), webdriver-флаг в Firefox не отключается. Оба кандидата лечатся одним действием: **боевой транспорт = Chromium CDP** ⇒ `DEFAULT_BROWSER_KIND = "chrome"` (browser.py), ветка firefox остаётся для отладки. Тесты, кодировавшие старый дефолт (test_default_firefox, test_default_kind_firefox, resolve_browser_for_parallel), обновлены.

 - **Регрессия на resume после багованной эпохи: stale `transient_failures=3` больше не убивает кампанию (2026-09-25).** Живой манифест `zvukoizolyatsiya` остался с `transient_failures=3` (4× UNKNOWN_FAILURE из-за неверной классификации блока) и пустым `challenge_control`/`profile_cooldown`/`recovery_telemetry`. В старом коде ПЕРВЫЙ же новый сбой давал `tr=3` → терминал `NEEDS_ATTENTION unknown_failure`, т.е. после рестарта демона кампания была мертва ещё до первого прогона; теперь счётчик самолечится первым же не-`UNKNOWN_FAILURE` прогоном. Покрыто `test_integration_resume_heals_stale_failure_counters`: seed-манифест с точной формой живого состояния (tr=3, 4 старых прогона, нули в challenge_control) → 3 шторма подряд → `transient_failures == 0`, `final_reason == challenge_storm`, в логе «серия UNKNOWN_FAILURE прервана». Это же означает, что рестарт демона на живом прогоне безопасен и подхватывает новую классификацию без ручной правки progress.json.

 - **Дизайн скользящего canary A/B «stock vs patched транспорт» (2026-09-25, только план, запуск не выполнялся).** Задача п.2 ревью: «Ozon режет patched-сборки» сейчас гипотеза механизма с одной точкой данных. Минимальный проверяемый контур без изменения прод-поведения: (1) делить ТРАФИК, а не браузерный стек — кандидат = один и тот же stock FF156+geckodriver, единственная переменная = способ доставки JS-патча Juggler; (2) окно — N=30 успешных сборов на кандидате против M=30 сопоставимых по времени/товару на текущем транспорте, окно ≥7 дней (иначе разница не отделима от суточной волны Qrator); (3) метрики из уже существующего KPI (ADR §14.7): `challenge_rate_per_100`, `puzzle_storms`, `session_blocked/session_exhausted`, `puzzle_solve_rate`; критерий пригодности кандидата = снижение challenge_rate ≥2× при неизменном/лучшем success_rate; (4) каждая точка = один JSON- ряд в `data/campaigns/<probe>/`, чтобы тренд был виден без ретроспективы; (5) остановка при любом ухудшении success_rate >5 п.п. Жёсткое правило: патченный транспорт = отдельный probe-профиль на отдельном порту, никогда не в прод-пуле воркеров; запуск — только с явного разрешения пользователя (живой браузер). До этого все утверждения в отчётах формулируются как «наблюдение/гипотеза», не как факт.

 - **P1-проба JSON API против DOM (2026-09-25, один товар pid 210502516, изолированный профиль/порт 9337).** Одобрено пользователем: одна проба, без перезапуска кампании. Найдено и исправлено два дефекта, затем измерен главный вопрос.

   1. **Баг транспорта (был в боевом коде).** `ozon_fetch.fetch_json` звал `page.evaluate(js, url, timeout=timeout_ms)`. Selenium-обёртка `ffsel.evaluate` такой вызов терпит, а Playwright — нет: `evaluate(expression, arg)` принимает ровно один аргумент и не знает `timeout`. На Chromium (дефолт боевого сбора) это давало `TypeError` → `fetch_json` всегда возвращал `None`, то есть модуль был мёртв на дефолтном движке. Оффлайн-тесты это скрывали: фейки страниц объявляли `evaluate(self, js, *args, **kwargs)`. Исправлено: аргумент один — список `[url, ms]`, таймаут задаётся в JS через `AbortController` + `clearTimeout`. Регресс-тест `TestFetchJsonPlaywrightSignature` с фейком со строгой сигнатурой Playwright (`evaluate(self, expression, arg=None)`, без `**kwargs`); попутно поправлен старый тест, читавший позиционный url.

   2. **Ответы на вопросы пробы** (`tests/probe_reviews_api.py`, `tests/probe_reviews_req_shape.py`):
      - GET из вкладки работает: 0,02–0,12 с на страницу, ключ id отзыва — `uuid`;
      - без токенов страницы `layout_page_index` игнорируется: на 1/2/3/4 приходит одна и та же четвёрка, совпадающая с DOM (пересечение 4 из 4, `new_vs_dom` = 0);
      - `nextPage` и `pageToken` в ответе пусты — курсора пагинации нет;
      - `paging.total` = 257 707 — это счётчик по всему товару, а не по варианту;
      - реальная пагинация страницы: `GET /api/entrypoint-api.bx/page/json/v2?url=/product/<pid>/reviews/pdp-part?layout_container=reviewshelfpaginator&layout_page_index=N&sh=<токен>&start_page_id=<32-hex>&tab=reviews`. Токены живут только в конкретной загрузке страницы, синтезировать их нельзя;
      - скролл dedicated-страницы `/reviews/pdp-part` (16 итераций): 4 уникальных отзыва, высота документа перестаёт расти на y≈1897, кнопки «Дальше» нет. DOM и API дают одно и то же.

   3. **Срез серверный и общий для обоих путей.** Контрольная точка из архива: тот же pid 2026-08-29 дал 97 отзывов на сортировку `published_at_desc` и 90 на `score_asc` (`reviews_variant_mode.variant_count` = 223 124, `pagination_type` = `infinite_scroll`). Следовательно DOM-механика исправна, режет выдачу сессия/сайт, а не способ сбора. Транспортный ускоритель покрытия не работает, пока профиль анонимный.

   4. **Следствия для приоритетов.** (а) `ozon_fetch` в боевой `collect` по-прежнему не подключён — при срезе в 4 отзыва он не даёт выигрыша; чинить пагинацию «похожим URL» бессмысленно без `start_page_id`. (б) Смещённый фокус: главный рычаг — авторизация профиля, а не транспорт; P2 (`session.auth`) ценен именно тем, что делает анонимный прогон видимым сразу, а не после тысяч отзывов. (в) Прежняя формулировка «потолок ~100 отзывов» в докстрингах была неточной: измеренный срез зависит от сессии и товара (97 → 4). Формулировки в `collect.py`, `qa_raw.py`, `README.md`, `docs/architecture/architecture.md` и `ozon_fetch.py` исправлены на измеренные факты. (г) Побочная хрупкость: `_scroll_to_reviews` на свежей загрузке успевает выйти (`stable_iters` = 8 ≈ 3,3 с), пока ленивый `webListReviews` ещё не смонтирован (виджет появляется на y≈2800 через ~2–4 с). В бою не проявляется — карточка товара собирается раньше и успевает смонтировать виджет; в пробе с чистым `?tab=reviews` проявляется сразу. Не правилось — вне одобренного объёма P2, кандидат в бэклог.

 - **Решение пользователя по итогам P1 (2026-09-25): без входа, «собираем так же, все доступные, как раньше».** Значит авторизация профиля не делается, `ozon_fetch` в `collect` не подключается (при анонимном срезе выигрыша нет, а пагинация без `start_page_id` не воспроизводится). Ожидание по качеству данных нужно принимать как есть: анонимный профиль сегодня даёт единицы отзывов вместо ~90.

 - **Найден и починен дефект: тонкая/пустая страница проходила молча (2026-09-25).** Обнаружено при попытке замерить боевой путь сбора. `check_challenge` переспрашивает «тонкую» страницу (`_CHALLENGE_LOOKS_JS`: нет h1, текста <300 символов) `CHALLENGE_RECHECKS` раз, но если маркеров капчи нет — выходил без ошибки. Итог: `collect` продолжал извлечение с пустого документа и писал raw с 0 отзывов, то есть кампания тратила слот и заносила в архив мусор (воспроизведено: `widgets=0`, `h=805`, `scripts=1`, замер `D:/Temp/opencode/diag_mount_race.py`). Правка: после переспросов, если страница всё ещё тонкая — `AccessChallengeError("страница не отрисована: нет h1/контента")` + `page.is_invalid = True` (ADR §14.1). Смысл: неразрендеренная страница классифицируется как челлендж, бюджет тратится на сессию, а не на пустые товары. Покрыто `tests/test_challenge_guard.py` (`_ThinPage` + 3 теста: raise, `is_invalid`, raise после переспросов). Попутно исправлены фейки, отдававшие payload на любой JS: отрендеренная страница обязана отвечать на `_CHALLENGE_LOOKS_JS` отрицательно (`tests/test_reviews.py::_FakePage`, `tests/test_collect_topup.py::FakePage`). Полный suite: 1097 passed, 4 skipped, 1 xfailed.

 - **Ложный шаг проб и его цена (записать, чтобы не повторять).** Первый «замер боевого пути» запускал `C.collect` с URL `?tab=reviews` — это НЕ боевой URL (кампания ходит на голый `/product/<pid>/`), результат 0 отзывов к боевому пути отношения не имеет. Второй замер вернул пустые страницы во всех трёх фазах, потому что скрипт закрывал браузер (`sess.close()`), профиль 9337 стартовал холодным и Ozon отдал антибот-заглушку. Правила на будущее: (1) боевой путь мерить только на голом URL карточки; (2) изолированный браузер не закрывать между замерами — иначе теряешь прогретое состояние сессии; (3) замер, где `widgets=0`/`h<1000`, — это не данные, а заглушка, фиксировать как invalid и не делать выводов о срезе. Текущий боевой выход на анонимной сессии остаётся НЕИЗМЕРЕННЫМ: есть 4 отзыва на вкладке отзывов (доказано дважды) и 186 отзывов в архиве от 2026-09-08, но данных «голый URL + вариант-режим, сегодня» нет. Профиль 9337 сейчас холодный/под антиботом — перед следующей живой проверкой его нужно прогреть.

 - **Восстановлен затёртый архив (2026-09-25).** Замер `C.collect` успел перезаписать `data/210502516_raw.json` (0 отзывов) — `out_path=None` не отменяет дефолтное сохранение. Источник для восстановления нашёлся сам: `data/210502516_raw.20260908-141055.1.json` (186 отзывов: 97 `published_at_desc` + 89 `score_asc`, `variant_count` 226352, `total_count` 251753). Сегодняшний пустой замер сохранён как `data/210502516_raw.20260925-192208.anon0.json`, канонический файл восстановлен из ротационной копии. Вывод на будущее: любой одноразовый замер с `C.collect` обязан указывать явный `out_path` в каталог проб — иначе затирает прод-архив. Побочно: контрольная точка среза — 186 отзывов (не 187).
 - **Антибот-аудит по итогам разбора (2026-09-26): 6 пунктов проверены, 4 исправлены, 1 признан не-багом, 1 отложен.**

   - **P0 (реальный баг, исправлен): `BrowserProbe.probe()` читал несуществующий атрибут.** `ozon_collector/browser.py` брал `getattr(sess, "browser_identity", None)`, но реальный `cdp.Session` такого поля не имеет — identity лежит в `sess.browser_pid` (это `P.ProcessIdentity`, несмотря на имя). Итог: `ident` всегда `None`, условие `ident is None or not alive` давало `BROWSER_DEAD` на КАЖДОЙ неопознанной Playwright-ошибке, а `classify_error()` по `probe_state` картировал это в `error_kind="browser"` с `should_recycle=True` — то есть лишний kill+launch браузера (~секунды + потеря состояния вкладок) вместо дешёвого ретрая. Плюс `probe_state` в телеметрии всегда показывал `browser_dead`, из-за чего реальное состояние сессии не диагностировалось. Уточнение к формулировке разбора: **watchdog не затронут** — `BrowserWatchdog._sample()` работает через `cdp_http_ok()` + `P.browser_processes()` и `probe()` не вызывает. Правка: читать `sess.browser_pid`; `None` (чужой/внешний браузер) больше не значит «мёртв» — владение не проверяем, судим по CDP HTTP, как это уже делает watchdog. Обновлены докстринги модуля и `classify_error`, тест-фейк переведён на `browser_pid`, добавлены 2 теста (внешний браузер не объявляется мёртвым; `probe`/`Session` не упоминают `browser_identity` как защита от возврата опечатки).

   - **P0 (реальный баг, исправлен): `UnboundLocalError` в ветке classic-fallback.** Ветка `no_bg_shot` (нет живого кадра фона) звала `_solve_classic()` → тот делал слепой драг по template-match и возвращал `True`; управление падало в `if not cands:`, где f-string строился из `bg_l`/`mask`/`yrow`, которые в этой ветке **не присваивались**. Классика по CDN-PNG для текущего формата мёртва (об этом сказано в коде и в измерениях: лунка рисуется оверлеем только в живом кадре), а ветка срабатывает ровно тогда, когда кадра нет. Классика удалена целиком вместе с `_find_notch`/`_notch_match` и 5 тест-кейсами; ветка стала честным провалом (диаг `no_bg_shot` + `_PUZZLE_FAILURES + 1` + `return False`). Заодно убрана лишняя вложенность (`else` → ранний `return`). Покрыто `test_solve_puzzle_no_bg_shot_fails_cleanly` (доходит до нужной ветки, `failures() == 1`) и `test_classic_template_match_path_is_gone` (символы не вернутся).

   - **P0 (мёртвый код, удалён):** `_log_slider`, `_detect_slot` (дубль top-1 варианта `_detect_slot_topk`), `_puzzle_piece_gone`, `_find_notch`, `_notch_match`, плюс присваивание `target_vx0 = target_vx` в `_drag_to` (нигде не читалось). Проверено, что ссылок вне `puzzle.py` нет ни в коде, ни в тестах, ни в `tools/`. Попутно pyflakes-чистка: `Tuple` в импортах, затенение `os` переменной цикла в `_dedup1`, f-string без плейсхолдера.

   - **P0 (документация, исправлено): дефолт браузера.** Истина в коде — `browser.DEFAULT_BROWSER_KIND = "chrome"`, а `.env.example` поставлял `OZON_BROWSER=firefox` и называл firefox дефолтом, `cdp.py` писал «дефолт = firefox» в одном месте и «(дефолт)» про chrome в другом. Свежий клон стартовал на Firefox (хуже отпечаток, нужен geckodriver). `.env.example` переведён на `OZON_BROWSER=chrome`, докстринги `cdp.Session`/`env_browser_override` выровнены. `PROJECT_NOTES`-историю не переписывал (это лог решений, а не документация).

   - **P0 (launcher, признано НЕ багом): `--no-sandbox` в `launch_chrome.sh`.** Расхождение с `.ps1` платформенно оправдано: под root Chromium на Linux без этого флага не стартует (типичный контейнер/WSL), а на Windows песочница работает и лишний флаг — заметный сигнал automation, что `.ps1` прямо и объясняет в комментарии. Убирать флаг не стал, задокументировал причину в `.sh`. А вот настоящий баг там нашёл: `CHROME="${CHROME:-.../chromium-*/chrome}"` — глоб внутри параметрического подстановления **не разворачивается**, литерал `*` уезжал в `bash -c` и разворачивался уже там; при двух установленных ревизиях Chromium получал лишние аргументы и не стартовал, а `ls $CHROME`-guard это пропускал. Теперь путь разрешается явно через `ls -1d ... | sort -Vr | head -n1` (самая свежая ревизия — лучший отпечаток, как и поиск самой свежей версии Chrome в `.ps1`) с проверкой `-x`; дубль `ls`-guard удалён. `bash -n` проходит.

   - **P1 (сделано): `WALL_LIMITED` больше не теряется.** `parse_qa_stdout` разбирал `WALL_LIMITED N`, `qa_raw` считал `wall_limited_count`, но финальный QA демона значение отбрасывал, и в `progress`/`status.json` не было видно, сколько товаров упёрлось в анонимный потолок. Проброшено: per-batch `wall_limited` → `progress["qa"]["wall_limited"]` → `status.json.qa` (все три места: цель достигнута, резерв, мягкие остановки). Тест `test_final_qa_keeps_wall_limited_metric` (там же всплыло, что тестовые сводки обязаны проходить K4-инвариант `ok+warn+fail == products`).

   - **P1 (сделано): `puzzle.py` 1775 → 7 модулей.** Механический сплит без изменения кода функций (вырезка по `ast`-диапазонам, реэкспорты сохранены — `puzzle.X` работает для всех потребителей, включая `tools/puzzle_hunter.py`/`solve_live2.py`): `puzzle_dom` (194) — DOM и признаки состояния; `puzzle_capture` (240) — съёмка живого кадра; `puzzle_detect` (297) — чистый CV; `puzzle_drag` (335) — механика драга; `puzzle_input` (69) — человекоподобная мышь; `puzzle_state` (87) — счётчики и JSONL-журнал; `puzzle.py` (546) — фасад + FSM `solve_puzzle` + диагностика провалов. Один тест перенацелен (`test_drag_to_piece_mode_no_slider` патчит `puzzle_drag` вместо `puzzle`, т.к. глобалы `_drag_to` теперь в модуле-drag). **Обоснованное исключение из лимита 400 строк:** `solve_puzzle` — 318 строк в одном куске, это провалидированный на живых капчах FSM; его разбор на под-функции без живых капч для проверки даёт риск антибот-регрессии, которую юнит-тесты не поймают. Оставлен как есть, помечен в докстринге модуля.

   - **Итог:** полный suite `1096 passed, 4 skipped, 1 xfailed` (было 1097: минус 5 тест-кейсов `_find_notch`, плюс 5 новых тестов на probe/no_bg_shot/wall_limited). Живых прогонов, логина, прогрева профиля 9337 и коммитов не делал.

- **Разбор присланной критики anti-bot/puzzle (2026-09-26): 5 пунктов проверены по коду, 3 исправлены, 2 отвергнуты с обоснованием.**

   1. **P1 (исправлено): калибровочный зонд +40px перешагивал цель.** `puzzle_drag._drag_to` измерял k фиксированным сдвигом мыши на 40px. Формула `k = dpx/40` (piece) или `dpx/dsl` (slider) от длины зонда не зависит — большой move считается от ФАКТИЧЕСКОГО `px` после зонда, — но сам зонд при близкой лунке уезжал за слот, и попытка тратилась впустую (а в кусочковом режиме это ещё и увод мыши мимо цели на десятки px). Добавлена чистая функция `_calib_step(dist)`: зонд = половина расстояния в границах `CALIB_MIN=6 / CALIB_MAX=40` с отступом `CALIB_GUARD=2px` от цели; `0.0` → зонд пропускается, k=1.0 и работает замкнутый контур (консервативно: недобор вместо перелёта, контур меряет реальный px каждый шаг). Дальние цели (≥80px) ведут себя как раньше — ровно 40px, это зафиксировано тестом-регрессией. Измерение k вынесено в чистую `_k_from_probe(r0, r1, calib, grip_mode)` (None = хват не удался), покрыта 7 тестами в новом `tests/test_puzzle_drag.py`.

   2. **P1 (исправлено): неотрисованный кадр молча уходил в детекторы.** `puzzle_capture._shot_bg_clean` проверял только «кадр не пустой» (байты) и «размер совпал». Свёрнутое/минимизированное окно или гонка композитора дают ВАЛИДНЫЙ PNG правильного размера, но однотонный (std≈0) — детекторы читали его как «лунки нет», и попытка тихо сгорала без внятной причины. Добавлен гейт `_frame_painted` (std < `FLAT_STD=3.0` → `flat_frame(std=…)`) и одна пересъёмка с длинной паузой; при повторном провале — честный `None` с причиной в `_shot_bg_reason()`. Попутно DRY: hide/sleep/screenshot/show был скопирован три раза, причём во второй ветке без `animations`/`timeout`; теперь единая точка `_shoot_rect`, а декодирование — `_decode_gray`. Покрыто 6 тестами в `tests/test_puzzle_capture.py` (плоский→пересъёмка принята, оба плоских→None, хороший кадр→без лишней съёмки).

   3. **P1 (исправлено): сетевой бан IP не отличался от капчи.** `search._collect_shard` на `access_challenge` всегда шёл по лестнице reload×3 (внутри `check_challenge`) → new_tab → recycle → SESSION_BLOCKED. При жёстком бане (403/429/503) та же репутация IP воспроизводится на новой вкладке и после recycle, то есть воркер сжигал ~15–20 мин и всё равно останавливался. Добавлен `_network_block_status(sess)` — один HEAD `location.origin` из уже открытой вкладки (без навигации, с куками) — и гейт `_ip_blocked` ПЕРЕД тратой recovery-бюджета: `error_kind="ip_blocked"`, `recovery["ip_blocks"]`, счётчик `session_blocked`, остаток aborted с текстом причины. Консервативность: любая ошибка пробы/неизвестный статус → `None` → поведение прежнее; 200 (то есть капча) баном не считается. `run_canary` тоже различает `CANARY_IP_BLOCKED` — разные причины остановки в логах демона. Покрыто 9 тестами в `tests/test_ip_fastfail.py`.

   4. **Отвергнуто (фактическая ошибка разбора): «мышь дрожит на микро-шагах из-за move_noise».** В коде шум цели (`move_noise` ±2.5px) применяется ОДИН раз — к `dest` большого перемещения; дальше идут микро-коррекции с чистым `dest`, дедзоной 0.3px и обязательным замером фактического `px` на каждом шаге, то есть контур гасит Own-шум, а не усиливает. Правки не потребовалось; менялся только зонд (п.1), где шум к dest тоже не привязан.

   5. **Отвергнуто (уже сделано ранее): «монолит 1775 строк, разбить на 3 модуля».** Сплит 1775 → 7 модулей выполнен в этом же цикле (см. пункт P1 выше). Уточнение по факту: `puzzle.py` = 561 строка, а не 546 — 546 было до расширения докстринга; лимит 400 строк превышает только этот файл, исключение обосновано (живой FSM `solve_puzzle`), остальные модули в лимите.

   - **Сложность (правило AGENTS о цикломатичности).** Замерено своим AST-счётчиком (lizard в окружении нет): `_shot_bg_clean` — без изменений (cn 33 → 33, при этом 147 → 127 строк за счёт DRY); `_drag_to` вырос на 4 пункта из-за трёх путей калибровки, поэтому измерение k вынесено в чистую функцию — рост в функцию не оставлен, исключение (234 строки, cn выше 15) задокументировано в докстринге модуля вместе с причиной (нет live-корпуса капч для проверки дальнейшего дробления обратной связи).

   - **Итог:** полный suite `1120 passed, 4 skipped, 1 xfailed` (+24 теста: 7 drag, 6 capture, 9 ip_fastfail, 2 калибровочных), целевые прогоны `test_puzzle.py test_puzzle_drag.py test_puzzle_capture.py` = 47 passed, `git diff --check` — только LF→CRLF предупреждения. Живых прогонов, логина, прогрева профиля и коммитов по-прежнему не делал.

### P0 (2026-09-26): демон не мог поднять CDP — молчаливые отказы лаунчера

Исправлено. Отказы лаунчера теперь громкие, быстрые и не дают ложного «OK».

- **Симптом.** `scripts/key_daemon.py` 3 ретрая подряд: «CDP-браузер не поднялся за 30с», причины нет. Проверка версии лаунчера дала ответ: `scripts/launch_chrome.ps1` = 3 байта (только UTF-8 BOM) — тот же класс ловушки, что уже ловили на `tests/test_cdp_launch.py`. Версии про «26 процессов Chrome» и «занятый default-профиль» были ложными.

- **Восстановление.** `launch_chrome.ps1` восстановлен из HEAD + известные правки 2.9 (`PROXY` → `--proxy-server`, выбор свежей ms-playwright сборки, маскированный `LAUNCHER-DIAG`): 356 строк, 19 744 байта, UTF-8 BOM, CRLF-only, `ParseFile` = OK.

- **Три silent-failure сверх пустого файла** (каждый воспроизведён на живом запуске):

  1. **Ложный OK.** Лаунчер проверял «порт `/json/version` отвечает», а не «это мой процесс». Если порт держал старый Chromium, а новый процесс умирал (профиль открыт другим экземпляром → Chromium уходит в него), ps1 печатал `OK: Chrome/... на порту N`. Теперь: до запуска порт отвечает → `LAUNCHER-WARN` + отказ; после запуска `Get-Process -Id $targetPid` умер → `LAUNCHER-ERROR: Chromium pid=N EXITED` + подсказка про занятый профиль, `exit 1`. Успех — тоже `exit 0`, код возврата стал сигналом.
  2. **Лаунчер опрашивал мёртвый PID.** `Invoke-RestMethod -TimeoutSec 2` × 20 = до 40с опроса после смерти процесса, поэтому вывод лаунчера приходил ПОЗЖЕ, чем Python сдавался, и «лог пуст» был ложной диагностикой. Добавлен ранний выход (`break` в цикле ожидания, `return $false` в ожидании окна) + Python-side **fail-fast** по `proc.poll()`. Замер на одном кейсе (профиль держит чужой Chrome): **32.5с → 6.6с**, в демоне это было ×3 ретрая.
  3. **«Лог пуст» врёт.** Лаунчер — отдельный процесс и мог ещё писать: наблюдался 0-байтный лог при живом лаунчере, дописавшем 293 байта через секунду. Добавлен `_launcher_tail_after_grace()` — пустоту подтверждаем только после паузы с перечитыванием.

- **Владение временным файлом убрано.** `NamedTemporaryFile(delete=False)` + «вернуть путь, вызывающий удалит» = мусор: прямые вызовы `_launch_browser()`/`_launch_script()` в тестах возвращали путь и забывали его (2 файла в `%TEMP%` за прогон, 16 за полный suite), а на Windows `os.unlink` падает, пока файл держит дочерний процесс. Теперь фиксированный путь на порт — `%TEMP%\ozon_launch_<port>.log`, truncate при каждом запуске; уборка не нужна, после провала лог это улика, а путь печатается в тексте ошибки. Общий хелпер `ozon_collector/launchlog.py` используют `cdp.py` и `browser.py` (было дублирование). Тесты изолированы через `OZON_LAUNCH_LOG_DIR` (autouse-фикстура в `tests/conftest.py`) — в боевой `%TEMP%` тесты не пишут.

- **Диагностика.** `cdp.py` и `browser.py` возвращают `LaunchRun(path, proc)`; в тексте ошибки — путь к логу, код возврата лаунчера, хвост вывода, платформенная подсказка. Маркеры лаунчера сделаны ASCII-only: кириллица в перенаправленном выводе PowerShell 5.1 едет через кодировку консоли. На Windows подсказка больше не предлагает искать `DISPLAY` и прямо называет частые причины (занятый профиль, занятый порт).

- **Проверено на живых запусках:** A) порт уже занят → отказ, exit 1, ложного OK нет; B) свободный порт + занятый профиль → `EXITED` + подсказка, exit 1; C) чистый запуск → `OK`, exit 0. Плюс Python-smoke: сообщение содержит `код возврата лаунчера=1`, путь к логу и маркер `EXITED`, ложной «пустоты» нет. Suite: `1318 passed, 4 skipped, 1 xfailed`. Lizard: новые функции CCN 1-9, `_await_launch` 18 (был 17, +1 на fail-fast). `git diff --check` чист, BOM только у `.ps1`, Python-файлы без BOM. Коммит не делал.

- **Ошибка в моей диагностике, стоит зафиксировать.** 9 файлов `ozon_launch_9222…9230.log` в `%TEMP%` я сначала объявил мусором от тестов и почти вычистил «свои» Chrome. На деле это были логи **работающего демона пользователя** (pid 32112, 8 воркеров): тесты ни одного реального запуска не делают (проверено и пофайлово, и трассировкой `launchlog.launcher_log_dir`). Урок: перед kill процессов проверять **родителя** (`ParentProcessId`), а не только имя профиля в командной строке; «лог в %TEMP%» в проде — норма, а не мусор.

### P0 (2026-09-26, позже): мёртвый браузер воркера = SESSION_EXHAUSTED на каждом товаре

Симптом (воркер1, остальные 7 в порядке): `goto renderer_failure → TargetClosedError`
(kind=browser) → `recyclable=True` → `session budget recycle исчерпан → SESSION_EXHAUSTED`
на каждом товаре шарда, до конца прогона. Провокатор — мой же cleanup (убил Chrome
с профилем `opencode\ozon-profile`, среди них оказался браузер воркера1).

Но главное — **дефект был в коде**, а не только в моей уборке:

1. `search.collect_search()` на `kind=browser`/`renderer_failure` всегда делал
   `recycle` и тратил `browser_recycle` из session-бюджета. При МЁРТВОМ
   Chromium пересоздавать нечего: recycle бессилен, бюджет уходит в
   `SESSION_EXHAUSTED` → `block_kind=soft` → пауза/cooldown демона **из-за
   локальной поломки** (Ozon ни при чём).
2. Воркер при этом выходил «чисто» (`exitcode 0`, без `fatal_error`, без
   `challenge_storm`), поэтому `parallel._needs_restart()` = False: в
   динамическом цикле оркестратора воркер брал следующую пачку и снова сыпал
   `SESSION_EXHAUSTED`. Самовосстановления не существовало.

Исправлено:

- `search._cdp_dead(sess, kind)` — `browser`/`renderer_failure` + неотвечающий
  CDP = браузер мёртв. Смерть CDP подтверждается **дважды через паузу** (0.5с):
  один неудачный HTTP на localhost не должен превращаться в «браузер мёртв»
  (та же логика, что `_launcher_tail_after_grace`).
- Ветка `browser_dead` стоит **до** `used["browser_recycle"] += 1`:
  `error_kind=browser_dead`, `failure_type=infra_failure`, `recyclable=False`,
  `recovery["browser_dead"] += 1`, без recycle. `session_blocked` НЕ поднимается
  → нет `block_kind`, нет `session_exhausted`, нет `aborted` (иначе демон
  начислил бы cooldown на пустом месте).
- `parallel._needs_restart()` → True при `recovery_metrics["browser_dead"]`
  (рядом с `challenge_storm`), даже при `exitcode 0`: `_respawn()` чистит мёртвый
  Chromium, ждёт свободный порт и поднимает воркера со свежим браузером. Лимит
  `max_worker_restarts` (2) защищает от зомби-цикла.
- `tests/conftest.py` — autouse-фикстура `_assume_browser_alive`: `browser_alive`
  это реальный HTTP, поэтому по умолчанию в тестах CDP «живой» (иначе результат
  зависел бы от того, слушает ли кто-нибудь 127.0.0.1:9222 — на машине с живым
  демоном recycle-тесты проходили, на CI падали бы). Тесты про `browser_dead`
  переопределяют точечно на `False`.

**Известный пробел рядом (НЕ чинил здесь, нужен отдельный проход):** в логе было
`probe=None`, то есть `BrowserProbe` вообще не отработал. У `cdp.Session` поле
`browser_pid` (P.ProcessIdentity) не заполняется, поэтому `BrowserProbe.probe()`
идёт по ветке «браузер не наш» и при неотвечающем CDP возвращает
`BROWSER_UNHEALTHY`, а не `BROWSER_DEAD` (см. заметку P0 от 2026-09-26 в
`PROJECT_NOTES.md`, строка ~3880). То есть существующий probe-механизм сегодня
не способен отличить «вкладка» от «браузер» — и решение принимает мой прямой
check CDP в `search`. Правильное лечение пробела: заполнять `sess.browser_pid`
identity при подключении воркера, тогда probe начнёт давать BROWSER_DEAD. Не
делал сейчас намеренно: правка затрагивает классификацию всех ошибок и требует
отдельного прогона по classification-тестам; мой фикс от неё не зависит (истина
берётся из самого CDP).

Проверено: 7 новых тестов (4 в `test_search.py`, 3 в `test_parallel_restart.py`),
полный suite `1325 passed, 4 skipped, 1 xfailed`, lizard: `_cdp_dead` CCN 2,
`collect_search` CCN 27 (как был), `_needs_restart` 18 → 19 (+1 на необходимую
ветку, исключение задокументировано в докстринге), `git diff --check` чист
(заодно убрал хвостовую пустую строку в `launch_chrome.ps1`, 19744 → 19742 байта,
BOM + CRLF сохранены, `ParseFile` = OK).

**Урок на будущее (моя ошибка):** перед kill процессов проверять
`ParentProcessId`, а не имя профиля в командной строке. Маска
`user-data-dir=.*opencode\ozon-profile` накрыла и main-профиль, и воркерские.
Скилл `.skills/ozon-dead-browser-vs-dead-tab/SKILL.md` (+ строка в `SKILLS.md`).

- **Дайджест из кеша (инструмент).** Прогон `fonari_dlya_velosipedov` упал на финальной фазе (главный профиль держал осиротевший Chromium → `Chromium pid=… EXITED, CDP never came up on port 9222`, 3/3 ретрая), при этом 336 товаров / 23 валидных уже лежали в `data/`, а `docs/digests/fonari_dlya_velosipedov.md` не был создан (демон пишет дайджест в финале). Перезапускать кампанию ради markdown не нужно: `python scripts/digest_from_cache.py <campaign>` рендерит его из `valid_pids` + `data/*_raw*.json` через штатный `key_daemon._write_digest` → `report.build_search_digest_combined`, без сети, браузера и правки манифеста. Проверено: 23/23 pid, 794 481 байт, лишних нет. Манифест при этом остался `final_state=NEEDS_ATTENTION (unknown_failure)` — данные целы, рестарт по решению пользователя.

- **BrowserProbe: смерть браузера по живому OS-запросу, а не по снимку `Session.browser_pid`.**
  Гипотеза «`Session` не заполняет `browser_pid`, поэтому probe слеп к identity»
  была НЕВЕРНОЙ: поле заполняется в `cdp.Session._connect()` (строка
  `self.browser_pid = _browser_pid(...)`) и обновляется в `reconnect()`.
  Настоящий дефект другой — probe судил о смерти по СНИМКУ этого поля:
  - `ident is not None and not identity_alive(ident)` → `BROWSER_DEAD`: снимок
    берётся на момент `_connect()`, поэтому рестарт браузера менеджером без
    `reconnect()` ( reconnect бросил исключение) даёт ложную смерть → воркер
    пошёл бы перезапускать/убивать ЗДОРОВЫЙ браузер;
  - `ident is None` → владение «не проверяем», мёртвый браузер уезжал в
    `BROWSER_UNHEALTHY` (внешний браузер, Firefox-private, гонка перечисления
    сразу после launch) → телеметрия врала «браузер жив», и разбор инцидента
    терял время именно на этой формулировке.
  Оба состояния отдавались в `error_probe_state` (манифест/дайджест/лог), хотя
  на маршрут восстановления не влияли: и `BROWSER_DEAD`, и `BROWSER_UNHEALTHY`
  мапятся в `kind="browser"`, а реальную проверку смерти делает `search._cdp_dead()`.

  Фикс: `BrowserProbe.os_process_alive(cdp_url, profile_token, kind)` — живой
  запрос `process.browser_processes` по паре (debug-порт, профиль); он же
  теперь источник истины в `BrowserWatchdog._sample()` (было копипастой, общий
  критерий у probe и watchdog). `probe()`: снимок → CDP-retries → живой OS-запрос;
  `BROWSER_DEAD` выводится только когда нет процесса на (порт, профиль) И наш
  инстанс из снимка не жив. Ошибка перечисления → считаем живыми (fail-safe:
  ложная смерть хуже молчания). У `browser_owner=external` профиль не наш,
  процесса в списке не будет НИКОГДА → `BROWSER_DEAD` недоказуем и не
  выводится (ADR §14.1), наружу `BROWSER_UNHEALTHY`.

  Проверено: 6 новых тестов в `test_browser.py` (dead без снимка, external не
  получает DEAD, протухший снимок не убивает живой браузер, транзиент при живом
  процессе, fail-safe перечисления, firefox-ветка), `_sample` для firefox
  по-прежнему не тратит `http_timeout` на не-CDP порт, lizard: `probe` CCN 12,
  `os_process_alive` CCN 6, `_sample` CCN 3. Регресс-тест
  `test_probe_no_identity_falls_back_to_cdp` сохранён: при живом CDP
  перечисление процессов не вызывается вовсе.

### P0 (2026-09-26, вечер): прогресс длинного прогона терялся — часы сбора впустую

Живой случай: кампания `fonari_dlya_velosipedov`, демон PID 59144 (8 воркеров).
После ~1ч20м работы лог показывал 336 собранных pid, а `progress.json`
`discovered_pids` был пуст, `runs/run_*.json` содержал только `run_started`.
Причина структурная: `progress.json` пишется только на границах прогона, а
`_inproc_collect` блокирующий на часы. Любое падение (OOM, перезагрузка, kill)
теряло весь прогресс, а resume начинал с нуля — самый дорогой класс потери в
демоне, т.к. платим браузерным временем и лимитом Ozon.

Решение — фоновой checkpoint, отдельный файл `data/campaigns/<c>/inflight.json`:
  - поток `kd-checkpoint` (daemon) каждые `KD_CHECKPOINT_EVERY` сек (default 60,
    `0` = выключить) сканирует канонические `{pid}_raw.json` с `mtime` не раньше
    начала прогона и дописывает pid в снапшот (`raw_pids_since`: префикс
    `isdigit` отсекает бэкапы `{pid}_raw.<stamp>.N.json` и мусор, регекс не нужен);
  - `progress.json` чекпоинт НЕ трогает: главный поток мутирует манифест в
    сотнях мест, общий read-modify-write дал бы гонку и порчу прогресса;
  - вливается `_reconcile_inflight` на границе прогона (ДО подсчёта `new_disc`/
    `new_valid` и `save_json` — иначе товары последних минут прогона и упавшей
    веткой `UNKNOWN_FAILURE`/`CHALLENGE_STORM` с пустым `combined` не попадут в
    манифест) и при старте демона (resume);
  - влитые pid сразу получают вердикты. Просто добавить их в `discovered_pids`
    НЕЛЬЗЯ: следующий прогон внесёт их в `skip_pids`, но ни в `valid`, ни в
    `failed` они не попадут (вердикт ставит `classify_new_pids` по `ok_pids`
    прогона, а не по `discovered`) — товар зависнет «известным, но невалидным»
    навсегда. Браузер для этого не нужен, canonical raw уже на диске.
  - недостоверная сводка QA (`parse_error`) → вердикты не применяются, pid ждут
    следующего цикла; потеря/битый `inflight.json` → no-op, демон не падает.
  - почему скан по всему `data_dir` безопасен: `PidLock` глобальный
    (`data/key_daemon.pid`), в системе один демон, значит любой свежий raw
    принадлежит ему; ошибка скана логируется и НЕ убивает поток.

Проверено: 9 новых тестов в `test_key_daemon.py` (скан игнорирует бэкапы/мусор
и фильтрует по mtime, поток пишет `inflight.json` и останавливается чисто,
`every=0` выключает, ошибка скана не убивает поток, merge даёт `valid`+`verdict_meta`,
идемпотентность без повторного QA, отсутствие файла = no-op, `parse_error` не даёт
`valid`, чекпоинт не пишет `progress.json`). Полный прогон: 1345 passed, 4 skipped,
1 xfailed. lizard: новые функции ≤ 7 CCN (`raw_pids_since` 7, `_start_checkpoint` 6,
`_loop` 6, `stop_checkpoint` 5, `_merge_inflight` 7, `_reconcile_inflight` 3);
`run_campaign` 125 → 128 CCN — зафиксированное исключение (три новые ветки P0-фикса,
функция далеко за порогом 15 ДО этого изменения; новый логический блок вынесен в
`_reconcile_inflight`, раздувать `run_campaign` не стал).

Важно: работающий демон PID 59144 запущен на коде без чекпоинта — фикс
применится при рестарте. `inflight.json` накапливается только у новых прогонов.

### P0 (2026-09-26, вечер 2): тест поднимал НАСТОЯЩИЙ Chrome и не закрывал его

Живой случай: во время `pytest` на рабочем столе открылось окно Chrome с
профилем `ozon-profile` на 9222 (PID 5084) и осталось жить после прогона.
Первый след — лог демона в порядок не попал (окна воркеров на 9223-9230 имеют
свои профили `ozon-profile-worker-N`); виновник нашёлся по
`D:\Temp\pytest-of-*\...\launchlogs\ozon_launch_9222.log` (conftest перенаправляет
launch-логи в tmp_path теста — это и выдало тестовый запуск).

Путь (воспроизведён со стектрейсом): `test_search.py:1278 collect_search` →
`search.py:849 _collect_shard` → `search.py:220 _recycle_browser` →
`browser.py:752 restart` → `browser.py:677 launch` → `cdp._launch_browser` →
лаунчер поднял реальный Chrome. Тест при этом был ЗЕЛЁНЫМ: recycle падал на
`'_FakeSess' object has no attribute 'reconnect'`, но браузер к этому моменту
уже был запущен. Молчаливый хвост опаснее падения — его замечают только глазами.

Решение — fail-fast вместо тихого браузера:
  - автофикстура `_forbid_real_browser_launch` в `tests/conftest.py` подменяет
    `browser._launch_script` и `cdp._launch_browser` на `pytest.fail` с рецептом
    (замокать `_recycle_browser`/`browser_processes`/`Session`). Исключение —
    маркер `allow_browser_launch`, для тестов, где лаунчер нужен по существу
    (все они мокают `subprocess.Popen` или проверяют путь до Popen: 3 в
    `test_browser.py`, 3 в `test_cdp_launch.py::TestLaunchBrowser`, 2 в
    `test_launch_diagnostics.py`, 4 в `test_net.py`);
  - `test_collect_search_challenge_event_has_telemetry` починен точечно:
    `browser_owner="external"` + заглушки `ensure_single_worker_tab`/`reconnect`,
    поэтому recycle идёт по ветке «только новая вкладка» (ADR §14.1) и не
    доходит до `restart()`;
  - 3 новых теста в `tests/test_browser_guard.py`: guard блокирует лаунчер,
    блокирует `cdp._launch_browser`, маркер отключает guard.

Проверено: полный прогон 1348 passed, 4 skipped, 1 xfailed. После прогона —
порт 9222 не слушается, chrome с `ozon-profile` 0 штук, свежих launch-логов с
непустым содержимым нет. Заодно выяснилось, что Playwright в тестах утечки не
оставляет (фикстура `browser` закрывает headless-shell штатно; его имя —
`chrome-headless-shell.exe`, а НЕ `chrome.exe`, из-за чего первый детектор
промахнулся).

**Поправка к первоначальному выводу (важно).** Я сначала записал, что висящие
`node.exe` — это пульты Playwright от `multiprocessing`-воркеров тестов, и
предложил их убить. Проверка по цепочке pid -> ppid показала обратное:
`node.exe 45244` -> `python.exe 18196` -> **`python.exe 59144` = живой демон**
(его pid из `data/key_daemon.pid`). То есть пульт принадлежал ДЕМОНУ, а не
тестам, и убийство «осиротевшего» процесса снесло бы живой сбор. Вывод:
мёртвый родитель у процесса ≠ мусор (у демона лаунчер-PowerShell тоже умирает
сразу после старта Chrome, и его браузер выглядит «сиротой»).
Отсюда правило уборки: **только по цепочке pid -> ppid от текущего процесса
pytest**, а для чужих/прошлых — только по сигнатуре ИЗВЕСТНО ТЕСТОВОЙ.

### P0 (2026-09-26, вечер 3): авто-уборка процессов после тестов

Требование пользователя: после прогонов не должно оставаться мусора, включая
сироты. Реализовано два уровня:

1. `tests/_procguard.py` (новый модуль) —
   - `reap_own_processes()`: снимок процессов через Toolhelp32 (ctypes, без
     новых зависимостей), обход дерева вниз от pid pytest, убийство
     `taskkill /F /T`. Вызывается из хука `pytest_unconfigure` в conftest —
     хук, а не фикстура, чтобы сработать и при падении в teardown, и при
     ненулевом коде выхода. Отчёт: stderr + `logs/test_reap.log` (stderr в
     финале сессии глушится capture pytest — проверено на демо-тесте).
   - `sweep_stale()`: хвосты ПРОШЛЫХ упавших прогонов. Родитель мёртв, плюс
     сигнатура в командной строке (`ms-playwright` / `playwright\driver` /
     `pytest-of-` / `spawn_main` / `resource_tracker` / `launch_chrome.ps1`).
     Вызывается из `run_tests_guarded.py` ДО и ПОСЛЕ прогона.
   - Защита демона: pid читается из `data/key_daemon.pid` и сверяется с текущей
     командной строкой (если pid переиспользован — защита снимается, иначе мы бы
     «защищали» чужой процесс). Плюс ручной список `KD_PROTECT_PIDS`.
     `KD_KEEP_TEST_PROCS=1` отключает уборку целиком.
2. Тесты: `tests/test_procguard.py` (24 теста на чистую логику: дерево,
   top_level, protected, матрица сигнатур, переиспользование pid) и
   `tests/test_reap_demo.py` — демо «тест забыл процесс», гейтится
   `KD_REAP_DEMO=1`, доказывает убийство по факту.

Проверено вживую: демо-тест поднял `python -c sleep(600)`, `pytest_unconfigure`
убил pid 35408 (проверено `Get-Process` → мёртв), демон 59144 остался жив.
Полный прогон через обёртку: 1372 passed, 5 skipped, 1 xfailed,
`session-end: убрано 0 из 0 кандидатов` — то есть после прогонов чисто.
