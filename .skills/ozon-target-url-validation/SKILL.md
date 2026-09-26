---
name: ozon-target-url-validation
description: >
  Fail-fast валидация целевого URL Ozon (markdown-обёртки, кавычки, пунктуация)
  и статус EMPTY_SEARCH вместо ложного NORMAL_DONE discovered=0.
Триггеры: "Cannot navigate to invalid URL", "выдача пустая", "0 товаров",
"NORMAL_DONE discovered=0", "демон не собирает", "--url", "canary уехал на главную",
"EMPTY_SEARCH", "campaign url broken", "0 links found"
---

# Ozon: валидация целевого URL + EMPTY_SEARCH

## Симптом (живой случай 2026-09-26)

```
[cdp] goto warn: [https://www.ozon.ru/category/zvukoizolyatsiya-33302/?...](https://www.ozon.ru/category/fonari-...-11061/?type=47720)
      → Page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL
[search] выдача не отрисовалась (сетка пуста без бот-маркеров) — возвращаю 0 товаров
[run_...] status=NORMAL_DONE: discovered 0 valid 0
```

URL был скопирован из markdown. Демон при этом **выглядел здоровым**: прогоны
считались успешными, а кампания давала ноль часами. Хуже всего — молчание:
ни ошибки, ни бан-сигнала, просто пусто.

## Почему это не «мелочь»

1. Тихая потеря времени: `no_progress_count` рос, но статус врал.
2. Мусор попадал в `data/campaigns/<name>/state.json` и **переживал рестарт** —
   даже после исправления CLI демон читал бы старое значение.
3. Canary резолвился ДО нормализации → получал мусор → `classify_canary_url`
   возвращал `home`, и canary «проходил» на главной вместо категории
   (ровно тот баг, который чинили в 2.8).

## Решение (ревизия 2.9)

`ozon_collector/net.py::normalize_target_url(url)`:

| Вход | Результат |
|---|---|
| `[текст](https://ozon.ru/...)` | адрес из скобок |
| `“https://...”`, `<https://...>`, пробелы | чистый URL |
| `вот ссылка https://... (смотри)` | первый http(s)-токен |
| `https://...?x=1.` | без хвостовой пунктуации |
| `javascript:alert(1)`, `ozon категория`, `` | `ValueError` |
| `https:///path` (нет host) | `ValueError` |

Точки вызова (все — до навигации):
- `key_daemon.main()` → `--url`, `--canary-url` (exit 2 + подсказка в stderr)
- `key_daemon._inproc_collect()` → URL из `state.json`
- `search._validated_campaign_url()` → `collect_search()` (pids/rescan — не
  валидируется, там url служебный)
- `cli._target_url()` → `cmd_collect`, `cmd_gather`

Плюс `recovery.search_grid_empty` → `classify_status()` = `EMPTY_SEARCH`
(стагнация, не успех; `CHALLENGE_STORM` важнее).

## Правила

1. **Любой новый вход URL** (CLI, env, state.json, конфиг) обязан пройти через
   `net.normalize_target_url`. Не дублировать正则/regex в модулях.
2. Нормализация — **до** `resolve_canary_url`, всегда.
3. Тишина = баг: если прогон дал 0 товаров, у него должен быть явный статус.
4. Тест на каждый новый источник URL: markdown, мусор, pids-режим, state.json.

## Проверка

```powershell
python scripts/key_daemon.py --url "[кат](https://www.ozon.ru/category/x-1/)" --limit 1
# → warning «URL поправлен», имя кампании из категории
python scripts/key_daemon.py --url "просто текст"
# → key_daemon: неверный --url: не http(s) URL ... (exit 2)
```

Тесты: `tests/test_net.py::TestNormalizeTargetUrl`,
`tests/test_key_daemon.py::TestDaemonUrlGuard`,
`tests/test_search.py::test_collect_search_rejects_junk_url`,
`tests/test_key_daemon.py::TestEmptySearchStatus`.

## Связанные

- [[ozon-entity-class-canary]] — canary берёт URL кампании; мусор ломал его приоритет.
- [[powershell-file-traps]] — правки этих файлов только python-скриптами.
