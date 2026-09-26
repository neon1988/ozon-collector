---
name: ozon-minimized-window-frame
description: >
  Однотонный кадр (std≈0) при валидных размерах в Chromium = не «лунок нет», а
  буфер НЕПЕРЕРИСОВАННОГО окна (свёрнуто/перекрыто, старт через SW_SHOWMINNOACTIVE).
  Пассивная пересъёмка это не лечит; лечит best-effort page.bring_to_front() перед
  ОДНИМ reshoot. Use when: flat frame, std=0, однотонный кадр, пустая капча,
  bring_to_front, reshoot кадра, окно свёрнуто, _frame_painted, FLAT_STD.
---
# Однотонный кадр = свёрнутое окно, а не пустая капча

## Контекст
- `puzzle_capture._frame_painted` отличает валидный PNG капчи от заливки по
  `std(grayscale) < FLAT_STD`. Размеры при этом корректные — буфер есть.
- Заливку отдаёт Chromium под Windows, когда окно свёрнуто или перекрыто (особенно
  после старта через `SW_SHOWMINNOACTIVE`): рисования не было, но буфер валиден.

## Алгоритм (KISS)
1. Кадр плоский → **не** ждать и не переснимать вхолостую.
2. `_bring_to_front(page, reason_box=None)`: best-effort `page.bring_to_front()`,
   исключения глушатся, причина пишется в `_shot_bg_reason` как
   `front=ok` / `front_err=<Тип>`.
3. Один reshoot. Кадр валидный → идём дальше; всё ещё плоский → честный отказ,
   без молчаливых кандидатов.
4. Кадр валидный с первого раза — окно НЕ трогаем (bring_to_front не нужен).

## Подводные камни
- ❌ Считать flat-frame признаком «капча без лунок» и искать кандидаты.
- ❌ Blind reshoot в цикле: он повторяет ту же ошибку и сжигает лимит попыток.
- ❌ Необработанное исключение от `bring_to_front` — оно не должно ломать съёмку
  (у части адаптеров метода нет вообще).
- ✅ Причина reshoot всегда в `_shot_bg_reason` — по логу видно, было ли окно
  поднято или метод не сработал.

## Где вшито
- `ozon_collector/puzzle_capture.py`: `_bring_to_front` + вызов перед reshoot
  после `_frame_painted() == False`.

## Примеры
✅ std=0 → `front=ok` → reshoot std=41 → кандидаты валидны.
✅ std=0 → `front_err=AttributeError` (нет метода) → reshoot как раньше, без падения.
❌ 3 reshoot подряд без подъёма окна — лимит попыток потрачен впустую.

## Ссылки
- `D:\Users\Neon\Projects\ozon-collector\tests\test_puzzle_capture.py`
- `D:\Users\Neon\Projects\ozon-collector\docs\architecture\anti_bot_architecture.md` §7
