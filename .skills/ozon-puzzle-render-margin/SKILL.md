---
name: ozon-puzzle-render-margin
description: >
  Рендерная маржа кусочка слайдер-пазла Ozon НЕ равна альфе CDN-PNG: браузер
  рисует кусок через object-fit, силуэт смещается относительно resize PNG в
  боксе, поэтому margin измеряется ТОЛЬКО по живому кадру (два скриншота
  clip=piece: видим + visibility:hidden, |diff| colmax, первый столбец > порога).
  Use when: маржа кусочка, silL, silR, рендер-маржа, margin куска, slider Ozon
  edge плохо попадает, candidates всегда мимо, CDN-альфа ненадёжна.
---
# Ozon puzzle: рендерная маржа (silR) вместо CDN-альфы (silL)

## Контекст
- Слайдер-пазл Ozon (abt-challenge): кусок 141x128 в css-боксе pz_r,
  фон 512x384. Цель драга — `edges - margin` (edges = левая грань лунки
  в живом кадре).
- **CDN-маржа `_sil_left(mask)` НЕДОСТОВЕРНА**: PNG-альфа resize в css-бокс
  даёт другой силуэт, чем object-fit в браузере. Доказано 2026-09-23:
  кадры 008 и 020 имеют ИДЕНТИЧНЫЕ PNG-маски (silL=10 обе), а рендерные
  маржи разные (3 и 10). То есть маржу из картинки не получить вообще.
- `silR = _shot_piece_margin` — измерение по живому кадру, работает 8/8
  (nedges-bx_true), топ-1 `_solve_cands` 19/19 на truth-кадрах.
- Модель не читает изображения — валидировать числами (json калб-кадров).

## Алгоритм (KISS)
1. `info["puzzle"]["r"]` → rect, `silR = _shot_piece_margin(page, info)`.
2. Два скриншота clip=rect: `vis` (кусок виден), hide `visibility:hidden`
   на img с class /puzzle/, 0.15s, `clean`. `|vis-clean|` colmax →
   `thr = max(16, 0.30*mx)`, первый столбец > thr = silR.
3. `sil_use = silR if silR is not None else silL` (fallback).
4. В боевой solve вызывать В try/except (лишний скрин не должен ломать
   решение), логировать silL/silR.
5. Порог mx<8 → None; скролл/вне вьюпорта может дать None — фолбэк.

## Подводные камни
- ❌ `page.evaluate(hide, timeout=5000)` — в Playwright `timeout=` есть только
  у locator.evaluate; у page.evaluate TypeError → молча None (ловится
  try/except). НЕ добавлять timeout в page.evaluate.
- ❌ Мерить маржу только в калб-захвате и думать, что боевой solve получил её.
- ✅ Хранить `cands` в json калб-кадра (топ-1 сравнивать по ним).
- Валидация: edges-e silR vs bx_true на solved кадрах (verdict==solved,
  bx_true есть).

## Где вшито
- `ozon_collector/puzzle.py`: `_shot_piece_margin` (:533), `_shot_piece_margin_rect`
  (:558), вызов в `solve_puzzle` (silR внутри try/except, fallback silL).
- `tools/puzzle_hunter.py` `_collect_calib`: записывает silL, silR, cands.

## Примеры
✅ 000.json bx=255 silR=16 e0=271 → 271-16=255 (HIT); silL=21 дал бы 250 (MISS).
✅ silR=12 при bx=237: 249-12=237.
❌ silL на группе 008/020: маска CDN одна, маржи 3 и 10 — PNG бесполезен.
❌ hopsключение silR в solve без try/except — срыв на проблемном куске.

## Ссылки
- `D:\Users\Neon\Projects\ozon-collector\ozon_collector\puzzle.py`
- Truth-кадры: `D:\Temp\opencode\captcha_calib{4,5,6,7}\*.json`
- Сверка: `scripts/eval_margin.py` (silR vs bx_true по калб-кадрам)