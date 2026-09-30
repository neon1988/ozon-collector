# Карта проекта ozon-collector

Навигация сверху вниз: структура → поиск → модуль → документ → файл → функция.
Не читай весь `src/`.

## Что это

Сборщик данных о товарах Ozon: карточка, отзывы выбранного варианта, OCR инфографики →
`data/{pid}_raw.json` + markdown-отчёт. Два слоя: ядро (`cli.py`, `ozon_collector/`)
и автономный супервизор кампаний (`scripts/key_daemon.py`).

## Слои и документы

| Слой | Код | Документ |
|---|---|---|
| Ядро коллектора | `cli.py`, `ozon_collector/` | `architecture.md` §3–§9 |
| QA-гейт | `scripts/qa_raw.py` | `architecture.md` §10 |
| Супервизор кампаний | `scripts/key_daemon.py` | `key_daemon_architecture.md` |
| Логика завершения | демон, финализатор | `daemon_completion_logic.md` |
| Антибот и браузер | `browser.py`, `cdp.py`, `process.py` | `anti_bot_architecture.md` |

`architecture.md` — сводка на 2026-08-31 (collector v1.16.x, schema 2.0),
покрывает оба слоя. Остальные документы глубже по своей теме.

## Смежные документы

- `docs/architecture/invariants.md` — что нельзя нарушать (черновик, требует ревью)
- `docs/architecture/gotchas.md` — ловушки и неочевидное поведение (черновик, требует ревью)
- Формат `data/{pid}_raw.json` (schema 2.0) — `architecture.md` §5, §11 и код `qa_raw.py`.
  Отдельного `docs/schema.md` в репозитории нет.

## Проверка изменений

- Тесты: `python scripts/run_tests_guarded.py` (ядро + демон, ~209 тестов демона)
- Известный стабильный xfail ядра: `test_baseline_matches_source_raw` (дрейф baseline,
  помечен `xfail`, CI не блокирует). Не «чини» его, не перезаписывая baseline молча.
- QA данных: `python scripts/qa_raw.py --pids <pid>`

## Данные на диске

```
data/    {pid}_raw.json, {pid}/dom/*.html.gz, ocr_cache/, search_combined.json,
         campaigns/{campaign}/{progress,status,events.jsonl,combined_*.json}
docs/    отчёты *.md, schema.md, qa/qa_*.{md,json}, architecture/
logs/    ozon_collector.log(.1), {campaign}_daemon.log
```

Каталоги `data/`, `logs/`, `docs/qa/` в `.gitignore` — в репозитории их нет.
