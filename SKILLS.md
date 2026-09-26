# Skills

Project skills used in this repo (copies from global `.myskills/skills/`).

| Skill | Что это | Триггеры |
|---|---|---|
| [`ozon-puzzle-render-margin`](.skills/ozon-puzzle-render-margin/SKILL.md) | Рендерная маржа кусочка silR (живой кадр) вместо CDN-альфы silL | «маржа кусочка», silR, рендер-маржа, edge плохо попадает |
| [`ozon-entity-class-canary`](.skills/ozon-entity-class-canary/SKILL.md) | Canary проверяет сущность того же класса: `--canary-url` → env → кампания → главная | «canary», CANARY_OK, canary_url, OZON_CANARY_URL, session_used вхолостую |
| [`ozon-api-prepass-dom-truth`](.skills/ozon-api-prepass-dom-truth/SKILL.md) | Отзывы через entrypoint-api только как opt-in предсрез; DOM — источник истины | «отзывы медленные», reviews_api, fetch_slice, nextPage, layout_page_index |
| [`ozon-minimized-window-frame`](.skills/ozon-minimized-window-frame/SKILL.md) | Однотонный кадр = свёрнутое окно; `bring_to_front` перед одним reshoot | «flat frame», std=0, однотонный кадр, bring_to_front, reshoot |
| [`ozon-parallel-per-worker-telemetry`](.skills/ozon-parallel-per-worker-telemetry/SKILL.md) | Per-worker challenges/rate до решения «свой IP на воркер» | «параллелизм», свой IP на воркер, challenge_rate_per_100, worker summary |
| [`ozon-target-url-validation`](.skills/ozon-target-url-validation/SKILL.md) | Fail-fast валидация `--url` (markdown-обёртки чинятся, не-URL → exit 2) + `EMPTY_SEARCH` вместо ложного `NORMAL_DONE discovered=0` | «Cannot navigate to invalid URL», discovered 0, EMPTY_SEARCH, URL поправлен, canary уехал на главную |
| [`powershell-file-traps`](.skills/powershell-file-traps/SKILL.md) | Ловушки PS 5.1 при правке файлов: `Set-Content -Encoding UTF8` добавляет BOM, а `open(f,"w").write(open(f).read())` обнуляет файл. Плюс контракт лаунчеров: fail loudly (ASCII-маркеры, exit 1), проверка СВОЕГО процесса вместо «порт отвечает», fail-fast по коду возврата, фиксированный путь лога | BOM, -Encoding UTF8, файл обнулился, io.open, marshal+dis, восстановление из .pyc, ложный OK, EXITED, fail-fast, ozon_launch_<port>.log |
| [`ozon-dead-browser-vs-dead-tab`](.skills/ozon-dead-browser-vs-dead-tab/SKILL.md) | Мёртвый браузер ≠ мёртвая вкладка: recycle пересоздаёт вкладку внутри ЖИВОГО браузера, при мёртвом Chromium он бессилен и жжёт session-бюджет (ложный SESSION_EXHAUSTED = soft block из-за локальной поломки). Лечение — респаун воркера со свежим браузером | TargetClosedError, kind=browser, CDP не отвечает, browser_dead, recycle не помогает, _needs_restart |
