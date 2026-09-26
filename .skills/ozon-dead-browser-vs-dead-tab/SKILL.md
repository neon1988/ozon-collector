---
name: ozon-dead-browser-vs-dead-tab
description: >
  Отличать «умер браузер» от «умерла вкладка»: recycle пересоздаёт вкладку
  внутри ЖИВОГО браузера, поэтому при мёртвом Chromium он бессилен и только жжёт
  session-бюджет, доводя товар до ложного SESSION_EXHAUSTED (soft block из-за
  локальной поломки). Лечение — перезапуск воркера со свежим браузером.
  Use when: TargetClosedError, kind=browser, CDP не отвечает, browser dead,
  SESSION_EXHAUSTED на каждом товаре, воркер без браузера, recycle не помогает,
  browser_dead, _needs_restart не срабатывает.
---
# Мёртвый браузер ≠ мёртвая вкладка: recycle тут бессилен

## Контекст
`search.collect_search()` на `kind=browser` / `kind=renderer_failure` всегда
делал `recycle` (пересоздать вкладку/контекст) и тратил `browser_recycle` из
session-бюджета. Это верно, **только если браузер жив**.

Если Chromium упал или его убили (cleanup, OOM, краш):
- вкладку пересоздавать нечем — Playwright уже отдал `TargetClosedError`;
- recycle падает или возвращает тот же мёртвый контекст;
- бюджет уходит в `SESSION_EXHAUSTED` → `block_kind=soft` → пауза/cooldown
  демона **из-за локальной поломки**, Ozon ни при чём;
- воркер при этом выходит «чисто» (`exitcode 0`, без `fatal_error`), поэтому
  `_needs_restart` не срабатывает: в динамическом цикле оркестратора воркер
  берёт следующую пачку и снова сыпет `SESSION_EXHAUSTED` до конца прогона.

Симптом в логе — один воркер, у остальных всё хорошо:
```
goto renderer_failure: https://www.ozon.ru/product/… → TargetClosedError: Page.goto…
gather: error: TargetClosedError (kind=browser, recyclable=True)
session budget recycle исчерпан → SESSION_EXHAUSTED      # на КАЖДЫЙ товар
```

## Алгоритм
1. **Разделяй на уровне отказа.** `kind=browser` + `browser_alive(cdp_url) == False`
   → `browser_dead` (инфраструктура), иначе → обычный recycle.
   Проверку смерти делай **дважды через паузу** (`time.sleep(0.5)`): один
   неудачный HTTP-ответ на localhost (таймаут 3с) не должен превращаться в
   «браузер мёртв».
2. **Не жги бюджет.** Ветка `browser_dead` идёт ДО `used["browser_recycle"] += 1`:
   `error_kind=browser_dead`, `failure_type=infra_failure`, `recyclable=False`,
   `recovery["browser_dead"] += 1`, `break`.
3. **Не ставь block-метку.** `session_blocked` НЕ поднимается: нет ни
   `block_kind`, ни `session_exhausted`, ни `aborted` — иначе демон начислит
   cooldown на пустом месте.
4. **Лечи на правильном уровне.** `parallel._needs_restart()` возвращает True при
   `recovery_metrics["browser_dead"]` (рядом с `challenge_storm`), даже при
   `exitcode 0`: `_respawn()` чистит мёртвый Chromium, ждёт свободный порт и
   поднимает воркера со свежим браузером. Лимит `max_worker_restarts` (по
   умолчанию 2) защищает от зомби-цикла.
5. **Тесты не зависят от живых портов.** `browser_alive` — реальный HTTP, поэтому
   autouse-фикстура в `tests/conftest.py` подменяет его на `True`; тесты про
   `browser_dead` переопределяют точечно на `False`.
6. **Смерть суди по живому OS-запросу, не по снимку.** `Session.browser_pid`
   — это снимок на момент `_connect()`: он протухает, если браузер перезапустили
   без `reconnect()` (тогда `identity_alive` даёт ложную смерть → restарт
   ЗДОРОВОГО браузера), и его нет у external-браузера / Firefox-private / при
   гонке перечисления сразу после launch (тогда смерть не видна вовсе и
   телеметрия врёт `browser_unhealthy`). Один критерий на probe и watchdog —
   `BrowserProbe.os_process_alive()`: `process.browser_processes` по паре
   (debug-порт, профиль). Ошибка перечисления → считаем живыми (fail-safe).
   У `browser_owner=external` профиль не наш, процесса в списке не будет
   НИКОГДА → `BROWSER_DEAD` недоказуем, наружу только `BROWSER_UNHEALTHY`
   (ADR §14.1).

## Примеры
✅ Мёртвый CDP: `error_kind=browser_dead`, `failure_type=infra_failure`,
   `recycles == 0`, `session_used["browser_recycle"] == 0`, `block_kind` нет,
   воркер респаунится со свежим браузером.
✅ Живой CDP + `renderer_failure`: recycle как раньше (`R1/R2` по `max_recycles`),
   canary после recycle.
❌ recycle при мёртвом CDP: 2 товара × `recycle budget исчерпан` →
   `SESSION_EXHAUSTED` (soft) + пауза демона + потерянный шард.
❌ Перезапуск воркера по `fatal_error`/exitcode: воркер ведь выходит «чисто» —
   без `recovery_metrics` он не перезапустится никогда.
❌ `identity_alive(sess.browser_pid) == False` как признак смерти: снимок
   протухает → рестарт живого браузера; его нет → смерть не видна.
❌ `BROWSER_DEAD` для `browser_owner=external`: чужой процесс в перечислении
   по нашему профилю не появится никогда → это ложная смерть.

## Связанные
- `search._cdp_dead()` — хелпер (двойная проверка смерти CDP).
- `parallel._needs_restart()` — триггер респауна (`browser_dead`).
- `browser.BrowserProbe.os_process_alive()` — общий критерий смерти для
  `probe()` и `BrowserWatchdog._sample()`.
- `tests/test_browser.py::test_probe_*` (6 кейсов: dead без снимка, external,
  протухший снимок, транзиент при живом процессе, fail-safe перечисления,
  firefox-ветка) и
  `tests/test_search.py::test_collect_search_browser_dead_*` (4 кейса),
  `tests/test_parallel_restart.py::test_needs_restart_browser_dead_*` (3 кейса).
- `PROJECT_NOTES.md` → «P0: мёртвый браузер воркера = SESSION_EXHAUSTED на каждом товаре»
  и «BrowserProbe: смерть по живому OS-запросу».
- Скилл `powershell-file-traps` → «Launchers: fail loudly» (как не убить браузер
  воркера при уборке: проверять `ParentProcessId`, а не имя профиля).
