"""Механика драга кусочка пазла: чтение позиции, калибровка k, доведение.

_drag_to — один «заход»: снять актуальную позицию куска, применить
калибровочный сдвиг k*delta (кусок физически отстаёт от мыши), довести
|err| <= допуска и отпустить; при неудаче — свайп ±1px и повтор.
Состояние челленджа (готово ли, надо ли перезагружать) — здесь НЕ решается,
это делает solve_puzzle; отсюда вызываются _captcha_done/_confirm_solved.

Перенесено из puzzle.py без изменения кода (механический сплит).

Примечание по сложности: _drag_to — живой FSM драга (234 строки, cn выше
ориентира 15) и НЕ расчленён дальше намеренно: у нас нет live-корпуса капч,
а механическое дробление обратной связи «замер → шаг → доводка» без прогонов
реально повышает риск антибот-регрессии. Вынесенные чистые функции
_calib_step/_k_from_probe покрыты юнит-тестами (tests/test_puzzle_drag.py).
"""

import os
import random
import time
from typing import Optional

from ozon_collector import log

from .puzzle_dom import (_await_solved, _captcha_done, _classify,  # noqa: F401
                          _find_slider, _page_has_content, _still_challenge,
                          _stub_page)
from .puzzle_input import (_human_click, _human_drag, _human_move,
                           _human_path)
from .puzzle_state import _attempt

# Диагностика последнего захода драга (заполняется _drag_to, читает паспорт в
# solve_puzzle). Зачем модульный хвост, а не возврат кортежа: контракт _drag_to
# проверяется юнит-тестами как bool, и менять его ради телеметрии значило бы
# трогать FSM, который мы сознательно не дробим (см. докстринг модуля).
#
# Содержимое подобрано под разбор инцидентов: сколько времени заняло чтение
# позиции против ввода, сколько шагов дошло до допуска, что сработало раньше —
# допуск, дедлайн или исчезновение куска. По живому логу 2026-10-02 видно, что
# без этих чисел провал выглядел как «verdict=no» и разбор стоил часа: 11 с на
# шаг читалось как проблема детектора, хотя промах был 0.07 px.
_LAST_DRAG_DIAG: dict = {}


def last_drag_diag() -> dict:
    """Диагностика последнего захода _drag_to (пусто, если драга не было)."""
    return dict(_LAST_DRAG_DIAG)

# Калибровочный зонд (измерение k = фактический сдвиг куска / сдвиг мыши).
# Раньше зонд был фиксированным +40px: когда лунка рядом с кусочком, зонд
# уезжал ЗА цель (перешагивал слот), т.е. deliberate overshoot мимо слота —
# и тратил попытку впустую. Теперь длина зонда — функция расстояния до цели.
CALIB_MAX = 40.0   # потолок = прежнее поведение для дальних целей
CALIB_MIN = 6.0    # меньше 6px k не измерить: getBoundingClientRect даёт ~1px
CALIB_GUARD = 2.0  # не подходить к цели ближе, чем на 2px

# Пауза после отпускания куска до вердикта «снято/не снято». Приёмка приходит
# быстро (перезапрос выдачи на месте), 2.5s хватает; дальше решает вызывающий
# (solve_puzzle ждёт ещё _SOLVED_GRACE_S). Прежний _confirm_solved ждал
# 6x1s ТОЛЬКО URL-маркер captchaDone=true, которого текущее поколение капчи
# не ставит, — верные дропы возвращали okc=False и жегли время.
DROP_VERDICT_S = 2.5

# Простой непрерывный драг вместо возвратно-точного FSM (см.
# _simple_drag_once). Флагом, чтобы A/B на живых прогонах.
_SIMPLE_DRAG = os.environ.get("OZON_PUZZLE_SIMPLE_DRAG", "0") == "1"

# Последний замер фактического сдвига цели за drag, в px (None = не сдвинулась).
# Решатель читает его через _last_move(), чтобы отличать «мышь не зацепила
# кусок» (поломка) от «кусок доехал, но лунка не та» (промах детектора).
# Наблюдение пользователя 2026-10-01: пазл стоит и не двигается — по логу
# без этого поля нельзя понять, движок ли это или захват.
_LAST_MOVE: list = [None]


def _last_move():
    """Фактический сдвиг цели в последнем drag, px (None — не сдвинулась)."""
    return _LAST_MOVE[0]


def _reset_last_move():
    _LAST_MOVE[0] = None


def _verdict(page) -> str:
    """Вердикт снятия капты: 'done' | 'unblocked' | 'no'.

    Признаки резолвятся через глобалы ЭТОГО модуля, чтобы юнит-тесты
    подменяли puzzle_drag._captcha_done / puzzle_drag._still_challenge.
    """
    return _classify(_captcha_done(page), not _still_challenge(page),
                     _stub_page(page), _page_has_content(page))


def _await_verdict(page, seconds: float = DROP_VERDICT_S,
                   reject_after: float = 1.2) -> str:
    """Вердикт после отпускания: полный grace только если челлендж исчез.

    Если челлендж жив — дроп отвергнут, хватит reject_after (иначе каждый
    выстрел стоит полные секунды и свип не укладывается в таймаут solve).
    """
    t0 = time.monotonic()
    while True:
        st = _verdict(page)
        if st != "no":
            return st
        if _still_challenge(page):
            if time.monotonic() - t0 >= reject_after:
                return "no"
        elif time.monotonic() - t0 >= seconds:
            return "no"
        time.sleep(0.3)


def _calib_step(dist: float) -> float:
    """Длина калибровочного зонда по расстоянию до цели (px).

    0.0 → зондировать нечем (цель вплотную), калибровка пропускается и работает
    замкнутый контур по фактическому px. Иначе половина расстояния в границах
    [CALIB_MIN, CALIB_MAX] с отступом CALIB_GUARD от цели.
    """
    if dist - CALIB_GUARD < CALIB_MIN:
        return 0.0
    return min(CALIB_MAX, max(CALIB_MIN, dist * 0.5), dist - CALIB_GUARD)


def _k_from_probe(r0: dict, r1, calib: float, grip_mode: str) -> Optional[float]:
    """k = сдвиг куска / сдвиг мыши по двум замерам (чистая функция).

    None → кусочек не поехал (хват не удался), вызывающий берёт новую точку
    хвата. slider: делим на фактический ход слайдера (там k<1 из-за лага
    отрисовки), piece: мышь тянет кусок 1:1, делим на calib.
    """
    if not r1 or r1.get("px") is None:
        return None
    dpx = (r1.get("px") or 0) - (r0.get("px") or 0)
    if abs(dpx) <= 1.0:
        return None
    if grip_mode == "slider":
        dsl = (r1.get("s") or 0) - (r0.get("s") or 0)
        return dpx / dsl if dsl > 0.01 else 1.0
    return dpx / calib


def _read_sl_piece(page):
    """(slider.x, piece.viewport_x) или None.

    Позиция слайдера читается из getBoundingClientRect (надёжно при любой
    реализации: css-класс, transform или inline style). Ищем кусочек и слайдер
    ГЛОБАЛЬНО (не только внутри #captcha): после реролла капча пересобирает
    DOM (img может оказаться вне прежнего контейнера/id), а классы Ozon часто
    пустые — поэтому матчим и по id='puzzle', и по классу.

    Раньше только #captcha img + class /puzzle/: после неудачного дропа DOM
    пересобирается и _read_sl_piece давал px=None → ветка no_piece считала
    «пазл снят» (диагноз 2026-09-22: систематические ложные okc=True)."""
    try:
        return page.evaluate("""() => {
          const pz = [...document.querySelectorAll('img')].find(
            el => el.id === 'puzzle' || /puzzle/.test(el.className||''));
          let px=null, ps=null;
          if (pz) { const r=pz.getBoundingClientRect();
                    if (r.width>0 && r.height>0) px=r.x;
                    const v=parseFloat(pz.style.left); if(!isNaN(v)) ps=v; }
          let s=null, ss=null;
          const sl = document.querySelector('#slider') ||
            [...document.querySelectorAll('[class*=slider],[class*=slided],[class*=handler]')]
               .find(el => { const r=el.getBoundingClientRect();
                             return r.width>0 && r.height>0; });
          if (sl) { const r=sl.getBoundingClientRect();
                    if (r.width>0 && r.height>0) s=r.x;
                    const v=parseFloat(sl.style.left); if(!isNaN(v)) ss=v; }
          return {s, px, ps, ss};
        }""")
    except Exception:
        return None


def _wait_piece(page, deadline: float = 0.0, tries: int = 5) -> Optional[dict]:
    """Прочитать кусочек с коротким поллингом появления.

    После неудачного дропа Ozon РЕРОЛЛИТ капчу: DOM пересобирается, и
    img#puzzle заново инжектится через 0.3-2s (подобно первичной загрузке
    captcha_v10.js). Мгновенное чтение в этот момент даёт px=None, который
    прежний код трактовал как «пазл снят» — ложный успех. Здесь делаем
    N повторов с паузой; вернулось dict с px — кусок снова на месте."""
    r0 = _read_sl_piece(page)
    if r0 and r0.get("px") is not None:
        return r0
    for _ in range(tries):
        if deadline and time.monotonic() > deadline:
            break
        time.sleep(0.35)
        r0 = _read_sl_piece(page)
        if r0 and r0.get("px") is not None:
            return r0
    return None


def _drag_to(page, target_vx: float, info, bx, score,
             grip: Optional[tuple] = None,
             deadline: float = 0.0) -> bool:
    """Драг кусочка к target_vx с жёсткой обратной связью по реальному px.

    Самое слабое звено живой реализации — точность попадания: если детектор
    дал верную лунку, но кусочек не встал в слот, пазл остаётся (валидировано
    2026-09-17: один и тот же пазл решён в 13:16 и не решён в 18:58 при том же
    кандидате). Здесь на каждой микро-коррекции читается фактическое position
    кусочка (getBoundingClientRect), и шаг строится не по модели, а по реальной
    ошибке; финальный вердикт — «challenge снят» (_still_challenge == False),
    а не «кусочек исчез»: после неудачного дропа DOM пазла часто пересобирается
    (ложное «gone»), поэтому решает только фактическое снятие challenge.

    Новое поколение (нет трека/слайдера в DOM): перетаскиваем САМ кусочек
    (img.puzzle). Хват — в точке внутри силуэта (_grip_point), иначе mousedown
    в прозрачной зоне бочонка «не везёт» кусок. Вертикаль держим исходную.
    Все перемещения мыши — человеческие траектории (_human_path/_human_move),
    анти-бот палит равномерные линейные move (телепорт + идеальная прямая).
    """
    ACC = 0.9
    _LAST_MOVE[0] = None
    if deadline and time.monotonic() > deadline:
        return False
    try:
        page.evaluate("""() => {
          const c=document.querySelector('#captcha');
          if (c) c.scrollIntoView({block:'center', behavior:'instant'});
          return true; }""")
    except Exception:
        pass
    sl = _find_slider(page)
    bg_r = (info.get("bg") or {}).get("r")
    pz_r = (info.get("puzzle") or {}).get("r")
    if sl:
        track, handle = sl["track"], sl["handle"]
        max_hx = track[0] + track[2] - handle[2] - 1
        gx = handle[0] + handle[2] / 2.0
        gy = handle[1] + handle[3] / 2.0
        grip_mode = "slider"
    elif bg_r and pz_r and len(bg_r) == 4 and len(pz_r) == 4:
        # новый формат: тянем сам кусочек; трек = прямоугольник фона
        track = list(bg_r)
        max_hx = track[0] + track[2] - pz_r[2] - 1
        gx = pz_r[0] + pz_r[2] / 2.0
        gy = pz_r[1] + pz_r[3] / 2.0
        # сместим хват в непрозрачную зону силуэта (центр бокса часто прозрачный)
        if grip:
            ogx, ogy = grip
            gx = pz_r[0] + ogx
            gy = pz_r[1] + ogy
        grip_mode = "piece"
    else:
        _attempt("drag", note="no_anchor", bx=bx, score=score,
                 target_vx=round(target_vx, 1))
        return False
    log.debug(f"drag start target_vx={target_vx:.1f} bx={bx} sl={sl} "
              f"grip={grip_mode} gx={gx:.1f} gy={gy:.1f} max_hx={max_hx:.1f} "
              f"bg_r={bg_r} pz_r={pz_r}")
    rng_x = random.uniform(-6, 6)
    # Шум цели — только визуальный (прикладываем к move dest), а проверки
    # ACC/err считаем по честной target_vx: иначе ±2.5px шум делает
    # недостижимым допуск 0.9px даже для идеального детектора.
    move_noise = random.uniform(-2.5, 2.5)

    # Накопитель диагностики захода (см. _LAST_DRAG_DIAG в шапке модуля).
    # Пишется по ходу и сбрасывается на каждом входе в _drag_to, чтобы паспорт
    # в solve_puzzle описывал ИМЕННО последний заход, а не заход из прошлого
    # кандидата. Держим и время чтения позиции, и время ввода по отдельности:
    # по живому логу 2026-10-02 именно это отличает «evaluate встал» от
    # «ввод встал», а без разделения провал выглядел как проблема детектора.
    _t_drag = time.monotonic()
    _diag = {"t_read_s": 0.0, "t_move_s": 0.0, "probe_reads": 0,
             "iters": 0, "final_err": None, "settled": False,
             "stop": None, "k": None, "moved_px": None, "grip": grip_mode,
             "target_vx": round(target_vx, 1), "bx": bx,
             "simple": bool(_SIMPLE_DRAG)}
    _LAST_DRAG_DIAG.clear()
    _LAST_DRAG_DIAG.update(_diag)

    def _finish(**kw) -> bool:
        """Закрыть заход: дописать факты в паспорт и отдать результат провала."""
        _diag.update(kw)
        _diag["t_total_s"] = round(time.monotonic() - _t_drag, 2)
        for _n in ("t_read_s", "t_move_s"):
            _diag[_n] = round(_diag[_n], 2)
        _LAST_DRAG_DIAG.clear()
        _LAST_DRAG_DIAG.update(_diag)
        log.debug("drag diag: " + " ".join(f"{_k}={_v}"
                                           for _k, _v in _diag.items()))
        return False

    def _confirm_solved():
        # Пазл мог решиться, но страница медленно перестраивается: выдача
        # открывается после снятия челленджа, и DOM ещё секунды показывает
        # остатки пазла. Успех = ЛЮБОЙ из двух признаков: переход url на
        # captchaDone=true (старое поколение) ИЛИ снятие челленджа при реальном
        # контенте на странице (текущее поколение v10 — перезапрос выдачи на
        # месте). Прежние 6x1s ждали только URL-маркер и почти всегда истекали
        # впустую: заменены на _await_verdict с тем же вердиктом.
        return _await_verdict(page) != "no"

    def _drop_and_confirm(cur0: float) -> bool:
        # Кнопка ещё зажата на куске, доведённом к цели. Отпускаем и сразу
        # спрашиваем вердикт у обоих признаков снятия.
        page.mouse.up()
        return _await_verdict(page) != "no"

    def _simple_drag_once() -> bool:
        """ОДИН непрерывный человеческий драг: без зонда и без до-крутки.

        Гипотеза (наблюдение пользователя с экрана 2026-10-01): «если
        начинаешь двигать ползунок, но потом его бросаешь, происходит
        перезагрузка пазла». Текущий _drag_to делает ровно то, о чём говорит
        наблюдение: mousedown, затем калибровочный зонд ВПЕРЁД на 6-40px,
        потом возврат к цели, потом до 18 итераций микрокоррекции с паузами,
        и только затем отпуск. Для живой капчи это выглядит как «начал тянуть
        и бросил» - и она перезагружается.

        Здесь: одно mousedown, один _human_drag до цели, короткая пауза,
        mouse.up. Позиция куска заранее известна (r0.px), к цели едем
        напрямую, поэтому калибровать k не нужно.
        """
        r = _read_sl_piece(page)
        if not r or r.get("px") is None:
            r = _wait_piece(page, deadline=deadline, tries=3)
        if not r or r.get("px") is None:
            _attempt("drag", note="simple_no_piece", bx=bx, score=score,
                     target_vx=round(target_vx, 1))
            return _await_verdict(page) != "no"
        px0 = r["px"]
        _LAST_MOVE[0] = None
        _human_click(page, gx + rng_x, gy, hold=random.uniform(0.08, 0.18))
        time.sleep(random.uniform(0.05, 0.12))
        dest = max(track[0] + 1, min(max_hx, gx + (target_vx - px0)))
        _shape = _human_drag(page, gx + rng_x, gy, dest, gy, bound=max_hx)
        time.sleep(random.uniform(0.2, 0.45))
        r2 = _read_sl_piece(page)
        if r2 and r2.get("px") is not None:
            _LAST_MOVE[0] = round(r2["px"] - px0, 2)
        err = round((r2.get("px") if r2 and r2.get("px") is not None
                     else px0) - target_vx, 2)
        _attempt("drag", note="simple_end", bx=bx, score=score,
                 target_vx=round(target_vx, 1), err=err, shape=_shape,
                 moved_px=_LAST_MOVE[0], grip=grip_mode, k=1.0)
        log.debug(f"drag simple px0={px0} dest={dest:.1f} err={err} "
                  f"moved={_LAST_MOVE[0]}")
        return _drop_and_confirm(dest)

    last = {}
    for _try in range(3):
        if deadline and time.monotonic() > deadline:
            return _finish(stop="deadline_before_pick")
        _t0_read = time.monotonic()
        r0 = _read_sl_piece(page)
        _diag["t_read_s"] += time.monotonic() - _t0_read
        _diag["probe_reads"] += 1
        log.debug(f"drag try={_try} gx={gx:.1f} gy={gy:.1f} r0={r0} "
                  f"t_read={time.monotonic() - _t0_read:.2f}s")
        if not r0 or r0.get("px") is None:
            # Кусок мог исчезнуть на пересборке капчи после неудачного дропа:
            # img#puzzle грузится заново через 0.3-2s. Поллинг; если не
            # появился — это НЕ «пазл снят», а только реальное снятие
            # челленджа даёт True (2026-09-22: ложные okc=True из no_piece
            # раздували успехи и жечь свип по устаревшим кандидатам).
            r0 = _wait_piece(page, deadline=deadline)
            if r0 is None:
                _attempt("drag", note="no_piece", bx=bx, score=score,
                         target_vx=round(target_vx, 1))
                return _await_verdict(page) != "no"
        if _SIMPLE_DRAG:
            # Один непрерывный драг: гипотеза, что возвратно-точный FSM
            # (зонд вперёд + до 18 микрокоррекций) Ozon читает как
            # «начал тянуть и бросил» и перезагружает пазл (наблюдение
            # пользователя 2026-10-01). Включается флагом, чтобы можно было
            # A/B сравнить на живых прогонах.
            _solved = _simple_drag_once()
            if _solved:
                return True
            page.mouse.up()
            return False
        # подвод мыши к точке хвата (визуально-естественно) затем нажим
        _human_click(page, gx + rng_x, gy, hold=random.uniform(0.06, 0.16))
        time.sleep(random.uniform(0.05, 0.12))
        # Калибровочный зонд по расстоянию до цели (НЕ фиксированные 40px):
        # близкая лунка раньше перешагивалась зондом. Длина здесь не влияет на
        # точность big-move: d_sl считается от ФАКТИЧЕСКОГО px после зонда,
        # т.е. dest = gx + calib + (target - px_now)/k — probe-agnostic.
        calib = _calib_step(abs(target_vx - (r0.get("px") or 0)))
        k = 1.0
        r1 = r0
        if calib > 0.0:
            # калибровочный сдвиг человеческой траекторией
            _t_probe = time.monotonic()
            _human_move(page, _human_path(gx + rng_x, gy, gx + calib + rng_x, gy,
                                          n=16, arc=3.0))
            time.sleep(random.uniform(0.1, 0.22))
            _t_probe = time.monotonic() - _t_probe
            _t_r1 = time.monotonic()
            r1 = _read_sl_piece(page)
            _t_r1 = time.monotonic() - _t_r1
            _diag["t_move_s"] += _t_probe
            _diag["t_read_s"] += _t_r1
            _diag["probe_reads"] += 1
            k = _k_from_probe(r0, r1, calib, grip_mode)
            _diag["k"] = None if k is None else round(float(k), 3)
            log.debug(f"drag calib timing: t_probe={_t_probe:.2f}s "
                      f"t_r1={_t_r1:.2f}s")
        else:
            # зона для зонда нет (цель вплотную): k неизвестен → берём
            # консервативную 1.0 (недооценка скорее, чем перелёт) и отдаём
            # доводку замкнутому контуру, который меряет реальный px каждый шаг.
            log.debug(f"drag calib skipped dist="
                      f"{abs(target_vx - (r0.get('px') or 0)):.1f} k=1.0")
        # ЖИВОСТЬ ДРАГА. Если после mousedown + калибровочного зонда кусок/ручка
        # не сдвинулись (|dpx| <= 1px) — мышь не зацепила цель, и никакая
        # доводка это не спасёт. Это НЕ метрика, а поломка, поэтому она видна
        # в логе голосом WARNING и отдельной записью no_move с фактическим
        # сдвигом: иначе «пазл стоит, ничего не происходит» неотличимо от
        # «мышь едет, но лунка не та» (наблюдение пользователя 2026-10-01).
        _moved = None
        if r1 and r1.get("px") is not None and r0.get("px") is not None:
            _moved = round(r1["px"] - r0["px"], 2)
        _LAST_MOVE[0] = _moved
        _diag["moved_px"] = _moved
        log.debug(f"drag r1={r1} calib={calib} moved_px={_moved} k={k}")
        if k is None or not (0.4 <= k <= 3.0):
            page.mouse.up()
            time.sleep(random.uniform(0.3, 0.5))
            _no_move = _moved is None or abs(_moved) <= 1.0
            if _no_move:
                log.warning(f"drag: КУСОК НЕ ДВИНУЛСЯ (moved_px={_moved}, "
                            f"calib={calib:.1f}, grip={grip_mode} @"
                            f"({gx:.0f},{gy:.0f}) bx={bx}) — мышь не зацепила "
                            f"цель, доводка невозможна")
                _attempt("drag", note="no_move", bx=bx, score=score,
                         target_vx=round(target_vx, 1), calib=round(calib, 1),
                         moved_px=_moved, gx=round(gx, 1), gy=round(gy, 1),
                         grip=grip_mode)
            _attempt("drag", note="grip_fail", bx=bx, score=score,
                     target_vx=round(target_vx, 1), k=k, grip=grip_mode,
                     moved_px=_moved, no_move=_no_move)
            rng_x = random.uniform(-9, 9)
            if grip_mode == "piece":
                # При повторном хвате НЕ рандом: читаем живую непрозрачную точку
                # куска через canvas (точка с alpha>80 в css-боксе). CDN-маска
                # может не совпасть с рендером после перерисовки куска (grip_fail),
                # canvas-точка — это реальное изломанное положение на экране.
                off = _piece_grip_offset(page)
                if off:
                    nx, ny = off
                    gx = min(pz_r[0] + pz_r[2], max(pz_r[0], pz_r[0] + nx))
                    gy = min(pz_r[1] + pz_r[3], max(pz_r[1], pz_r[1] + ny))
                else:
                    gx = min(pz_r[0] + pz_r[2], max(pz_r[0], gx + random.uniform(-12, 12)))
                    gy = min(pz_r[1] + pz_r[3], max(pz_r[1], gy + random.uniform(-8, 8)))
            continue  # хват не удался — пробуем другую точку
        px_now = r1.get("px") or r0.get("px")
        err = px_now - target_vx
        if abs(err) <= ACC:
            time.sleep(random.uniform(0.15, 0.3))
            _solved = _drop_and_confirm(gx + calib + rng_x)
            _attempt("drag", note="acc_ok", bx=bx, score=score,
                     target_vx=round(target_vx, 1), k=k, calib=calib,
                     settled=True, solved=_solved, grip=grip_mode)
            return _solved
        # большой сдвиг в ожидаемую позицию (остаточная ошибка докручивается)
        d_sl = -err / k
        dest = max(track[0] + 1, min(max_hx, gx + calib + d_sl + move_noise))
        log.debug(f"drag to dest={dest:.1f} (err={err:.1f} d_sl={d_sl:.1f} "
                  f"calib={calib})")
        _shape = _human_drag(page, gx + calib + rng_x, gy, dest, gy,
                             bound=max_hx)
        log.debug(f"drag shape={_shape}")
        time.sleep(random.uniform(0.15, 0.3))
        cur = dest
        settled = False
        iters = 0
        for _i in range(18):
            iters = _i + 1
            _diag["iters"] = iters
            if deadline and time.monotonic() > deadline:
                return _finish(stop="deadline_micro",
                               final_err=round(err, 2) if last.get("px")
                               is not None else None)
            _t_read = time.monotonic()
            rr = _read_sl_piece(page)
            _t_read = time.monotonic() - _t_read
            _diag["t_read_s"] += _t_read
            _diag["probe_reads"] += 1
            if not rr or rr.get("px") is None:
                log.debug(f"drag i={_i} piece gone")
                settled = True
                _diag["stop"] = "piece_gone"
                break
            last = rr
            err = (rr.get("px") or 0) - target_vx
            _diag["final_err"] = round(err, 2)
            log.debug(f"drag i={_i} px={rr.get('px')} s={rr.get('s')} "
                      f"err={err:+.2f} t_read={_t_read:.2f}s")
            if abs(err) <= ACC:
                settled = True
                _diag["stop"] = "acc_ok"
                break
            step_px = -err / k if k else -err
            step_px = max(-12.0, min(12.0, step_px))
            _dest = max(track[0] + 1, min(max_hx, cur + step_px))
            if abs(_dest - cur) < 0.3:
                _dest = cur  # больше не двигаем — мертвая зона
            _t_move = time.monotonic()
            _human_move(page, _human_path(cur, gy, _dest, gy,
                                          n=10, arc=1.2))
            # ТАЙМИНГ (2026-10-02): живой лог показал 11 с между соседними
            # шагами при норме 0.4 с, то есть CDP-вызовы висят в 25 раз
            # дольше. Разделение read/move отвечает на вопрос, что именно
            # тормозит, а по догадке править нельзя.
            _t_move = time.monotonic() - _t_move
            _diag["t_move_s"] += _t_move
            log.debug(f"drag i={_i} t_move={_t_move:.2f}s "
                      f"t_step={_t_read + _t_move:.2f}s")
            time.sleep(random.uniform(0.1, 0.24))
            cur = _dest
        else:
            _diag["stop"] = _diag.get("stop") or "iters_exhausted"
        _diag["settled"] = settled
        log.debug(f"drag settle={settled} cur={cur:.1f}")
        cur2 = cur
        if last.get("px") is not None:
            _e = (last.get("px") or 0) - target_vx
            for _n in range(random.randint(1, 3)):
                if deadline and time.monotonic() > deadline:
                    return _finish(stop="deadline_nudge",
                                   final_err=round(_e, 2))
                if abs(_e) <= 0.4:
                    break
                _sd = (-_e / k) if k else (-_e if abs(_e) < 5 else -4.0)
                _sd = max(-4.0, min(4.0, _sd))
                _nc = max(track[0] + 1, min(max_hx, cur2 + _sd))
                _t_move = time.monotonic()
                _human_move(page, _human_path(cur2, gy, _nc, gy, n=6, arc=0.8))
                _diag["t_move_s"] += time.monotonic() - _t_move
                time.sleep(random.uniform(0.18, 0.45))
                cur2 = _nc
                _t_read = time.monotonic()
                rr2 = _read_sl_piece(page)
                _diag["t_read_s"] += time.monotonic() - _t_read
                _diag["probe_reads"] += 1
                if not rr2 or rr2.get("px") is None:
                    break
                _e = (rr2.get("px") or 0) - target_vx
                _diag["final_err"] = round(_e, 2)
                last = rr2
                settled = True
        time.sleep(random.uniform(0.25, 0.4))
        _solved = _drop_and_confirm(cur2)
        _err_last = None
        if last.get("px") is not None:
            _err_last = round((last.get("px") or 0) - target_vx, 1)
        _diag["settled"] = settled
        _diag["final_err"] = _err_last
        _diag["solved"] = bool(_solved)
        _diag["stop"] = _diag.get("stop") or ("settle_end" if _solved
                                              else "dropped_not_solved")
        _attempt("drag", note="settle_end", bx=bx, score=score,
                 target_vx=round(target_vx, 1), k=k, calib=calib,
                 settled=settled,
                 solved=_solved, err=_err_last, cur=round(cur, 1),
                 iters=iters, px=last.get("px"), s=last.get("s"),
                 ps=last.get("ps"), ss=last.get("ss"), grip=grip_mode)
        if _solved:
            _diag["t_total_s"] = round(time.monotonic() - _t_drag, 2)
            for _n in ("t_read_s", "t_move_s"):
                _diag[_n] = round(_diag[_n], 2)
            _LAST_DRAG_DIAG.clear()
            _LAST_DRAG_DIAG.update(_diag)
        else:
            _finish()
        return _solved
    try:
        page.mouse.up()
    except Exception:
        pass
    _attempt("drag", note="grip_exhausted", bx=bx, score=score,
             target_vx=round(target_vx, 1))
    return _finish(stop="grip_exhausted")


def _piece_grip_offset(page):
    """Смещение хвата внутри силуэта кусочка (локально к css-боксу)."""
    try:
        return page.evaluate("""() => {
          const cap=document.querySelector('#captcha');
          const img=cap? [...cap.querySelectorAll('img')].find(
            el=>/puzzle/.test(el.className||'')) : null;
          if (!img) return null;
          const r=img.getBoundingClientRect();
          if (r.width<=0) return null;
          const can=document.createElement('canvas');
          can.width=Math.max(1,Math.floor(r.width));
          can.height=Math.max(1,Math.floor(r.height));
          const g=can.getContext('2d');
          try { g.drawImage(img,0,0,can.width,can.height); }
          catch(e){ return null; }
          let d;
          try { d=g.getImageData(0,0,can.width,can.height).data; }
          catch(e){ return null; }
          let sx=0, sy=0, n=0;
          for(let i=0;i<d.length;i+=4){
            if(d[i+3]>80){ sx+= (i/4)%can.width; sy+= Math.floor((i/4)/can.width); n++; }
          }
          if(!n) return null;
          return [sx/n, Math.min(can.height*0.8, Math.max(can.height*0.35, (sy/n)*0.85))];
        }""")
    except Exception:
        return None
