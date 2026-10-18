# Аудит 2026-10-17: почему окно появляется в hidden и что исправлено

Проверка: `ALL GREEN (syntax + cli + tests)` — 1734 passed, 6 skipped, 1 xfailed
(286.40s). Живой замер W-слоя: окно видимо **0.018 с** против 0.731 с до фикса.

## 1. Отчёт: критические находки

### 1.1. Первопричина вспышки окна в hidden (доказано замером)

Chrome **сам показывает окно** при создании вкладки/target через CDP
(`PUT /json/new`, `Target.createTarget`, `ctx.new_page()`): в 10-мс пробнике
событие показа приходит через ~6 мс после запроса, rect 1440x900.

- Флаг `--no-startup-window` не спасает — окно создаётся и показывается при
  первой вкладке (vis=True), отброшен.
- Спонтанный показ через 30-40 с — тоже Chrome/Windows, не наш код.
- Щели, через которые показ жил до 0.73 с:
  1. лаунчер завершался сразу после ответа CDP-порта, а `guard.watch` демона
     регистрировался позже (поиск `browser_pid` через CIM ~1 с + connect, на
     8 воркеров дольше);
  2. первый tick сторожа блокировался PowerShell-identity (~0.5-1 с на вызов);
  3. native minimize в cdp шёл по pid из **wmic** (0.5-2 с);
  4. тик сторожа 150 мс сам по себе опаздывает на показ при new_page.

### 1.2. MAJOR: гонка stop() и потока WinEvent-хука (исправлено)

`stop()` мог прийти раньше публикации `_hook_tid`: WM_QUIT отправлять некуда,
`_hook_loop` уходил в `GetMessageW` навсегда — поток утекал (замер ревью: 27 из
30 циклов start/stop). Фикс: самопроверка `self._stop` сразу после
`SetWinEventHook`, до входа в насос + регресс-тест
`test_event_hook_stop_immediately_after_start`.

### 1.3. MAJOR: караул лаунчера боролся бы с треем (исправлено)

Duty-цикл пере-скрывал окно каждые 50 мс, не глядя на
`data/window_visible.flag` (трей: «Развернуть окна сбора») и `OZON_KEEP_WINDOW`.
В visible-режиме лаунчер 10 секунд боролся бы с пользователем. Фикс: оба цикла
скрытия молчат, если окна хотят видимыми; duty выходит из цикла сразу.

### 1.4. MINOR (все исправлены)

- `GetExtendedTcpTable`: ретрай при ERROR_INSUFFICIENT_BUFFER (таблица растёт
  между запросом размера и заполнением) — иначе identity падала на медленный
  PowerShell.
- `_chrome_pids_for_port`: пустая таблица → fallback на wmic (аномалия LISTEN,
  но терять держателя порта нельзя).
- `WindowGuard._fixed`: инкремент под lock (колбэк хука + tick — два потока).
- `.env.example`: добавлен `OZON_LAUNCHER_DUTY_S`; невалидное значение — warning.
- Живой тест `process_image_basename` (ошибка ctypes-сигнатуры видна только на
  живом вызове).

### 1.5. Открытые вопросы (сознательно не тронуты)

- **D1**: `ensure_render_window` гейтит OS-пробу по CDP-state
  (`puzzle_capture.py:113`), при state="normal" проба слепа. Поведение
  зафиксировано тестом `test_ensure_render_window_noop_when_normal` (интент
  прошлого решения). Гипотеза «self-show даёт state=normal» **не подтверждена
  замером**; щель закрыта слоями W+T. Менять гейт — отдельное решение с живым
  замером state при self-show.
- **D2/D4**: `OZON_KEEP_WINDOW` на уровне User пуст, `data/window_visible.flag`
  отсутствует — на этой машине не сработали.

## 2. Улучшенный код (что вошло в фикс)

Канон проекта — русские комментарии: контракт-тесты грепают русские строки
(«окно PID $targetPid скрыто» и др.), полный перевод на английский их сломает.
Поэтому код/комментарии — в стиле проекта, отчёт (этот файл) — русский.

- `ozon_collector/window_policy.py`: константы EVENT_OBJECT_SHOW/OBJID_WINDOW/
  WINEVENT_OUTOFCONTEXT/WM_QUIT, `_WINEVENTPROC`, декларации `_setup_api` +
  `SetWinEventHook/UnhookWinEvent/GetMessageW/PeekMessageW/...`;
  `port_owner_pids()` (ctypes `GetExtendedTcpTable`, ~1 мс вместо 0.5-1 с);
  `process_image_basename()` (QueryFullProcessImageNameW);
  `_pid_owns_port` fast path + `_pid_owns_port_via_cmdline` fallback;
  `should_hide_show_event()` (чистая матрица решения);
  `WindowGuard._hook_loop/_on_win_event` (гашение показа колбэком,
  лог «прячу немедленно (WinEvent)»); фиксы 1.2-1.4.
- `ozon_collector/cdp.py`: `_chrome_pids_for_port` через сокет-таблицу ядра +
  фильтр chrome.exe, `_chrome_pids_for_port_wmic` — исторический fallback.
- `scripts/launch_chrome.ps1`: duty-караул
  (`OZON_LAUNCHER_DUTY_S`, дефолт 10 с, re-hide каждые 50 мс) + проверки
  flag/keep_window в обоих циклах скрытия.
- Тесты: `test_window_policy.py` (+6), `test_cdp_launch.py` (+2),
  `test_launch_scripts.py` (+1 контракт ps1).

## 3. Ключевые изменения (список)

1. Показ окна гасится WinEvent-хуком мгновенно (замер: 0.018 с суммарной
   видимости при new_page; раньше 0.731 с).
2. Лаунчер остаётся караульным до регистрации сторожа демона (щаель
   «CDP готов → watch» закрыта).
3. Identity браузера — сокет-таблица ядра вместо PowerShell/wmic (конец
   блокировок тика на 0.5-2 с).
4. Караул и сторож уважают visible-режим (трей/keep_window).
5. Утечка потока хука устранена + регресс-тест.
6. `--no-startup-window` проверен и отброшен (не помогает, замер).

## Проверки

- `python -m pytest tests/test_window_policy.py tests/test_cdp_launch.py
  tests/test_launch_scripts.py tests/test_puzzle_capture.py` — 155 passed.
- `scripts\verify.ps1` — ALL GREEN (1734 passed, 6 skipped, 1 xfailed).
- Живой репро `.tmp/repro_owner.py` — видимость 18 мс, лог хука.
- Флейк `test_extract_live_markup[xiaomi]` (был в долге) — одиночно зелёный,
  в полном прогоне зелёный; не связан с фиксом.

## Дополнение 2026-10-17 (вечер): код-аудит трёх субагентов (только чтение)

Повод — жалоба владельца «серые окна с крестиком» (`.tmp/1.png`, 11:37:48;
`.tmp/2.png`, 16.10 16:34:11; пиксельно идентичны в области окон, 0.00%).
Все 8 сторожей в оба момента зарегистрированы (логи), показов/«прячу» нет —
окна НЕ из watched-pid. Находки:

1. **Живые браузеры вне всех реестров.** Сторож живёт в процессе воркера:
   смерть воркера = реестр умирает молча (нет «убран из охраны»). Preflight
   чистит сирот только по текущим портам (`parallel.py:241-252`); identity
   строгая — порт+токен (`process.py:766-790`); браузеры со старой базой 922x
   после сдвига 922x→944x (16.10 ~11:00, лог «база сдвинулась») не матчатся
   ничем. `Session.close()` браузер не гасит намеренно (`cdp.py:1503`);
   `_drop_dead_browsers` может выкинуть ЖИВОЙ pid («не держит порт» —
   `window_policy.py:899-905`; живой кейс `plastik_dlya_3d_printera_daemon.log`
   pid=51076), до следующего connect/new_page окно никто не гасит. Шторм
   (66 стартов сторожа за день, смерть браузеров за 2-4 мин) плодит сирот.
2. **Пустая поверхность окна (внешний фенотип).** Окно, скрытое в момент
   старта, нативный occlusion-трекер Chrome помечает occluded; показ даёт
   пустое окно (#202020 + caption) без кадров навсегда; SW_HIDE/SW_SHOW/
   minimize/restore не лечат. Источники: openai/codex#42547,
   chromiumembedded/cef#3638 (лечит `--disable-features=
   CalculateNativeWinOcclusion` — флаг отклонён 10-08 по отпечатку, см.
   «Правка 7» current.md). НЕ проверено вживую; если подтвердится — питает
   «серую рамку».
3. **Смежные щели (доказано кодом/замером):** `setWindowBounds(normal)`
   показывает скрытое окно (`puzzle_capture.py:99-102`), в hidden зовётся из
   `_normalize_window_geometry` (`cdp.py:952-957`, пауза до 2 с) и
   `_geometry_guard` (:1038) — в логах рассинхрона 0; зазор
   `ctx.new_page()`→watch (`cdp.py:713→724`); показ без EVENT_OBJECT_SHOW
   закрывается только тиком (≤150 мс); окна <400x300 не гасятся сознательно.

Статус: правки по находкам не делались (следующая сессия), живое
воспроизведение невозможно (коллектор остановлен, владелец за ПК).
