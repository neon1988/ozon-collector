"""Регрессы разбора «пазл перестал доезжать» (живой лог 2026-10-07).

Каждый тест здесь падал бы ДО правки и падает на коде, который вернул
«слайдер не едет / солвер едет мимо / бюджет не считается». Числа взяты из
живого лога, а не выдуманы:

    solve cdn bg=(384, 512) piece=(128, 170) p_r=[485.44, 303.42, 170.24, 128]
         yrow=210 edges=[] diff=[] va=[293, 292, 82]
    solve cand bx=145 score=1000014.805 target_vx=601.0
    drag start target_vx=601.0 sl={'track': [472, 508.5, 480, 2],
         'handle': [472, 493.5, 40, 32]} grip=slider gx=492.0 gy=509.5
         max_hx=911.0
    drag try=0 r0={'s': 472, 'px': 485.44, 'ps': 23, 'ss': 0}
    drag calib timing: t_probe=12.34s t_r1=0.00s
    drag r1={'s': 512, 'px': 509.29, 'ps': 41.64, 'ss': 40} calib=40.0
         moved_px=23.86 k=0.596
    drag to dest=684.4 (err=-91.7 d_sl=153.7 calib=40.0)
    drag i=0 px=567.68 s=668.44 err=-33.32
    drag i=0 t_move=10.86s
    drag diag: t_read_s=0.0 t_move_s=23.2 probe_reads=3 iters=2
         final_err=-33.32 settled=False stop=deadline_micro k=0.596
         moved_px=23.86
    solve: ОТКАЗ — исчерпан бюджет времени (60с)

Из этих чисел следуют проверяемые утверждения:

* bx=145 и target_vx=601 — РАЗНЫЕ ВЕЩИ, и разница b_r[0]=456.0 согласована
  (bg 512px занимает 456..968, yrow=210 = 303.42-93.42). Значит «bx не по той
  оси» — не причина; чинить пересчёт цели не нужно.
* усиление связки мышь→кусок в одном заходе дало 23.86/40 = 0.596 на зонде и
  58.39/156.44 = 0.373 на большом ходе. Доводка держала 0.596 замороженным.
* при усилении 0.373 ошибка 33.32px требует 89px хода мыши; со старым
  потолком шага 12px это 8 итераций по ~11с = 88с при бюджете 60с.
* page.mouse.move стоил 0.73с на точку (12.34с / 17 точек), а page.evaluate
  отвечал за 0.00с — координата куска читалась с недоотрисованного кадра.
"""
import io

import numpy as np
import pytest

from ozon_collector import collect as C
from ozon_collector import ffsel_js
from ozon_collector import puzzle
from ozon_collector import puzzle_drag
from ozon_collector import puzzle_input as PI

# ---- числа живого лога, на которые опираются проверки ----------------------
BG_X = 456.0          # target_vx = b_r[0] + bx = 456 + 145 = 601
BX = 145
TARGET_VX = 601.0
PX0 = 485.44          # кусок до зонда
SLIDER0 = 472.0       # ход полосы на зонде
CALIB = 40.0          # длина зонда = _calib_step(115.56) = CALIB_MAX
MOVED_PROBE = 23.86   # ход куска на 40px мыши
SLIDER_BIG = 156.44   # ход полосы на большом шаге
MOVED_BIG = 58.39     # ход куска на том шаге
ERR_AFTER_BIG = -33.32
K_PROBE = MOVED_PROBE / CALIB          # 0.5965
K_BIG = MOVED_BIG / SLIDER_BIG         # 0.3731
STEP_SEC = 0.73                       # 12.34s на 17 точек зонда
ITER_S = 10.86                        # t_move на одной итерации доводки


# =============================================================================
# 1. Коэффициент мышь→кусок: перемер вместо заморозки
# =============================================================================

def test_gain_update_tracks_measured_pair_not_frozen_probe():
    """Ключевой дефект: k, замеренный зондом, нельзя тащить на всю доводку.

    Зонд дал 23.86px на 40px мыши (k=0.596). Большой ход дал 58.39px на
    156.44px (k=0.373) — в полтора раза меньше. Прежний код брал k из зонда
    один раз и считал по нему все шаги до конца захода.
    """
    k, updated = puzzle_drag._gain_update(K_PROBE, MOVED_BIG, SLIDER_BIG)
    assert updated is True
    assert k == pytest.approx(K_BIG, abs=0.005), (k, K_BIG)
    assert abs(k - K_PROBE) > 0.2, "коэффициент обязан уехать от замороженного"


def test_gain_update_keeps_previous_when_pair_is_noise():
    """Мелкие ходы (<1px куска, <0.5px мыши) не меряют коэффициент.

    Обновлять k по таким парам нельзя: getBoundingClientRect отдаёт доли
    пикселя, и коэффициент прыгал бы на каждом шаге.
    """
    for dpx, dm in ((0.9, 5.0), (8.0, 0.4), (0.0, 9.0), (0.0, 0.0)):
        k, updated = puzzle_drag._gain_update(0.5, dpx, dm)
        assert updated is False, (dpx, dm)
        assert k == 0.5


def test_step_mouse_is_mouse_displacement_sign_checked():
    """Шаг — это сдвиг МЫШИ, и его знак обратен знаку ошибки.

    err = px - target_vx; при err<0 кусок левее цели, значит двигаемся вперёд.
    Прежний код называл этот шаг d_sl, то есть сдвигом СЛАЙДЕРА, и подставлял
    его в координату мыши — масштаб домножался на связку мышь→слайдер.
    """
    assert puzzle_drag._step_mouse(-33.32, 0.3731, step_cap=1e6) == \
        pytest.approx(89.28, rel=1e-3)
    assert puzzle_drag._step_mouse(+33.32, 0.3731, step_cap=1e6) == \
        pytest.approx(-89.28, rel=1e-3)
    # в допуске — не двигаемся вовсе
    assert puzzle_drag._step_mouse(0.5, 0.3731) == 0.0
    assert puzzle_drag._step_mouse(-33.32, None) == 0.0


def _simulate(err, k_init, true_gain, iters, cap):
    """Прогон замкнутого контура на константном усилении (чистая модель)."""
    used = 0
    for _ in range(iters):
        step = puzzle_drag._step_mouse(err, k_init, step_cap=cap)
        if step == 0.0:
            break
        used += 1
        dpx = step * true_gain
        err += dpx
        k_init, _ = puzzle_drag._gain_update(k_init, dpx, step)
        if abs(err) <= puzzle_drag.ACC:
            break
    return used, abs(err)


def test_low_gain_converges_within_budget_where_old_cap_cannot():
    """Потолок шага: со старым 12px ошибка 33px не гасилась за БЮДЖЕТ.

    Числа из лога: после большого хода err=-33.32 при фактическом усилении
    0.373. Старый потолок давал 12*0.373 = 4.5px куска за итерацию, то есть
    нужно 8 итераций; при t_move=10.86s на итерацию это 87с против
    PUZZLE_BUDGET_S=60с — ровно тот `stop=deadline_micro` при iters=2.
    """
    old_used, old_err = _simulate(ERR_AFTER_BIG, K_PROBE, K_BIG, 30, 12.0)
    new_used, new_err = _simulate(ERR_AFTER_BIG, K_PROBE, K_BIG, 30,
                                  puzzle_drag.STEP_CAP_MOUSE)
    assert old_err <= puzzle_drag.ACC and old_used >= 8, (old_used, old_err)
    assert new_err <= puzzle_drag.ACC, new_err
    assert new_used <= 3, new_used
    # и по времени: старый вариант физически не влезал в бюджет
    assert old_used * ITER_S > puzzle.PUZZLE_BUDGET_S, old_used
    assert new_used * ITER_S < puzzle.PUZZLE_BUDGET_S, new_used
    # каждый шаг проверяется следующим замером — перелёт стоит итерации,
    # а не попытки
    assert puzzle_drag.STEP_CAP_MOUSE > 12.0


def test_step_cap_still_bounds_absurd_models():
    """Потолок нужен и при заведомо неверном коэффициенте."""
    assert puzzle_drag._step_mouse(-1000.0, 0.05) == \
        puzzle_drag.STEP_CAP_MOUSE
    assert puzzle_drag._step_mouse(+1000.0, 0.05) == \
        -puzzle_drag.STEP_CAP_MOUSE


# =============================================================================
# 2. «Кусок не двинулся» ≠ «коэффициент вне полосы»
# =============================================================================

def test_low_gain_is_usable_even_below_old_nominal_gate():
    """0.373 — живой коэффициент из лога, а не поломка захвата.

    Старая проверка `not (0.4 <= k <= 3.0)` объявляла поломкой хвата любой
    коэффициент вне полосы и уходила на ПОВТОРНЫЙ хват (три полных зонда по
    ~12с каждый), то есть сам съедал тот бюджет, ради которого повтор и был
    сделан.
    """
    assert puzzle_drag._k_usable(K_BIG) is True
    assert puzzle_drag._k_usable(K_PROBE) is True
    assert puzzle_drag._k_usable(0.1) is True
    assert puzzle_drag._k_usable(0.0) is False
    assert puzzle_drag._k_usable(None) is False
    assert puzzle_drag._k_usable(50.0) is False
    # номинальная полоса уже коэффициента — это про АНОМАЛИЮ, не про поломку
    assert puzzle_drag.K_NOMINAL[0] > K_BIG


class _GainPage:
    """Страница, где кусок едет за мышью с постоянным усилением `gain`."""

    def __init__(self, gain, px0, y0=509.5):
        self.gain = gain
        self.px = float(px0)
        self.mx = float(px0)
        self.my = float(y0)
        self.pressed = False
        self.events = []
        self._base = float(px0)
        self._mx0 = float(px0)

    @property
    def mouse(self):
        return self

    def move(self, x, y, steps=None):
        self.mx, self.my = float(x), float(y)
        if self.pressed:
            self.px = self._base + (self.mx - self._mx0) * self.gain
        return self

    def down(self):
        self.pressed = True
        self.events.append("down")
        self._base = self.px
        self._mx0 = self.mx
        return self

    def up(self):
        self.pressed = False
        self.events.append("up")
        return self

    def evaluate(self, js):
        return "raf" if "Promise.race" in str(js) else None


def _read_gain(page):
    return {"px": page.px, "s": page.mx, "ps": 10.0, "ss": 0.0}


def _drag_with_gain(gain, target=TARGET_VX, px0=PX0):
    """Прогнать _drag_to на странице с заданным усилением связки."""
    import ozon_collector.log as _log
    lines = []
    orig_warn, orig_dbg = _log.warning, _log.debug
    _log.warning = lambda m, extra=None: lines.append(str(m))
    _log.debug = lambda m, extra=None: lines.append(str(m))
    page = _GainPage(gain, px0)
    sl = {"track": [472.0, 508.5, 480.0, 2.0],
          "handle": [472.0, 493.5, 40.0, 32.0]}
    info = {"bg": {"r": [BG_X, 93.42, 512.0, 384.0]},
            "puzzle": {"r": [px0, 303.42, 170.24, 128.0]}}
    from unittest.mock import patch
    try:
        with patch.object(puzzle_drag, "_find_slider", lambda p: sl), \
             patch.object(puzzle_drag, "_read_sl_piece",
                          side_effect=lambda p: _read_gain(p)), \
             patch.object(puzzle_drag, "_await_verdict", lambda *a, **k: "no"), \
             patch.object(puzzle_drag, "_still_challenge", lambda p: True), \
             patch.object(puzzle_drag, "_captcha_done", lambda p: False), \
             patch.object(puzzle_drag, "_stub_page", lambda p: False), \
             patch.object(puzzle_drag, "_page_has_content", lambda p: True):
            res = puzzle_drag._drag_to(page, target_vx=target, info=info,
                                       bx=BX, score=1.0)
    finally:
        _log.warning, _log.debug = orig_warn, orig_dbg
    return res, page, puzzle_drag.last_drag_diag(), lines


def test_drag_with_low_gain_reaches_target_instead_of_claiming_grip_failure():
    """ГЛАВНЫЙ регресс: связка 0.373 обязана доводиться, а не отбрасываться.

    Тот же сценарий, что в логе: усиление 0.373, кусок на 485.44, цель 601.0.
    Прежний код трижды объявлял «мышь не зацепила цель» и выходил с
    grip_exhausted; теперь доводит и попадает в допуск.
    """
    res, page, diag, lines = _drag_with_gain(K_BIG)
    assert abs(page.px - TARGET_VX) <= puzzle_drag.ACC, page.px
    assert res is False or res is True        # дроп состоялся (вердикт no)
    assert not any("КУСОК НЕ ДВИНУЛСЯ" in m for m in lines), lines
    assert diag.get("stop") == "acc_ok", diag
    assert diag.get("k", 0) < puzzle_drag.K_NOMINAL[0], diag
    # кнопка отпущена и кусок реально доехал
    assert page.events[-1] == "up", page.events
    assert page.px > PX0 + 50


def test_drag_with_zero_gain_is_reported_as_grip_failure():
    """Усиление 0 — это по-настоящему поломка захвата, и она остаётся loud."""
    _res, page, diag, lines = _drag_with_gain(0.0)
    assert any("КУСОК НЕ ДВИНУЛСЯ" in m for m in lines), lines
    assert diag.get("stop") == "grip_exhausted", diag
    assert page.events.count("down") == 3, page.events
    assert page.events[-1] == "up", page.events


def test_sign_inverted_gain_is_not_treated_as_usable():
    """Коэффициент с обратным знаком — не «пригодный малый», а мусор.

    Отрицательный k = кусок поехал против мыши (отскок/резина). Доводка по
    нему поехала бы в ту же сторону, куда он и так убегает, то есть
    разошлась бы — поэтому такое измерение отбраковывается, а не
    подставляется в шаг.
    """
    assert puzzle_drag._k_usable(-0.5) is False
    assert puzzle_drag._step_mouse(-33.32, -0.5) == 0.0


# =============================================================================
# 3. Замер после кадра страницы
# =============================================================================

def test_settle_js_races_raf_against_timer():
    """У page.evaluate в Playwright НЕТ таймаута.

    Чистый двойной requestAnimationFrame на фоновой вкладке не резолвится
    никогда (rAF там не приходит), и один такой вызов вешает солвер навсегда.
    Поэтому гонка с setTimeout обязательна, и это проверяется константой, а не
    комментарием.
    """
    js = puzzle_drag._SETTLE_JS
    assert "requestAnimationFrame" in js
    assert "setTimeout" in js
    assert "Promise.race" in js
    assert str(puzzle_drag.SETTLE_TIMEOUT_MS) in js


def test_settle_reports_raf_timer_and_errors_without_raising():
    class _P:
        def __init__(self, v):
            self.v = v

        def evaluate(self, js):
            if isinstance(self.v, Exception):
                raise self.v
            return self.v

    assert puzzle_drag._settle(_P("raf")) == "raf"
    assert puzzle_drag._settle(_P(None)) == "timer"
    assert puzzle_drag._settle(_P(RuntimeError("cdp gone"))).startswith("err_")
    # бюджета нет — страницу не дёргаем вообще
    class _Boom:
        def evaluate(self, js):
            raise AssertionError("settle без бюджета дёргать нельзя")

    assert puzzle_drag._settle(_Boom(), 0.0) == "no_budget"


def test_read_settled_waits_for_frame_before_reading_position():
    """Порядок «кадр → чтение» — то, чего не было в живом логе.

    t_probe=12.34s на 17 точек при t_r1=0.00s означает: между последним
    mouse.move и чтением координаты нет ни одного кадра композитора, и
    getBoundingClientRect отдаёт положение с ПРОШЛОГО кадра. Отсюда
    moved_px=23.86 / 48.34 / -0.88 при одной и той же команде.
    """
    order = []

    class _P:
        def evaluate(self, js):
            order.append("settle")
            return "raf"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(puzzle_drag, "_read_sl_piece",
                   lambda p: (order.append("read"), {"px": 1.0})[1])
        puzzle_drag._read_settled(_P())
    assert order == ["settle", "read"], order


def test_read_settled_skips_frame_wait_when_budget_spent():
    """Бюджета нет — читаем как раньше: лучше неточный замер, чем отказ."""
    calls = []

    class _P:
        def evaluate(self, js):
            calls.append(js)
            return "raf"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(puzzle_drag, "_read_sl_piece", lambda p: {"px": 1.0})
        puzzle_drag._read_settled(_P(), deadline=puzzle_drag.time.monotonic() - 5.0)
    assert calls == [], calls


# =============================================================================
# 4. Кнопка отпускается на ЛЮБОМ выходе из захода (дефект 1)
# =============================================================================

class _DeadlineRuns:
    """Прогон _drag_to с дедлайном на N-м вызове часов (детерминированно)."""

    def __init__(self, budget_calls, gain=K_BIG):
        self.clock = [1000.0]
        self.calls = [0]
        self.budget = budget_calls
        self.page = _GainPage(gain, PX0)

    def _mono(self):
        self.calls[0] += 1
        self.clock[0] += 1.0
        return self.clock[0]

    def run(self):
        from unittest.mock import patch
        sl = {"track": [472.0, 508.5, 480.0, 2.0],
              "handle": [472.0, 493.5, 40.0, 32.0]}
        info = {"bg": {"r": [BG_X, 93.42, 512.0, 384.0]},
                "puzzle": {"r": [PX0, 303.42, 170.24, 128.0]}}
        self.calls[0] = 0
        with patch.object(puzzle_drag, "_find_slider", lambda p: sl), \
             patch.object(puzzle_drag, "_read_sl_piece",
                          side_effect=lambda p: _read_gain(self.page)), \
             patch.object(puzzle_drag, "_await_verdict",
                          lambda *a, **k: "no"), \
             patch.object(puzzle_drag, "_still_challenge", lambda p: True), \
             patch.object(puzzle_drag, "_captcha_done", lambda p: False), \
             patch.object(puzzle_drag, "_stub_page", lambda p: False), \
             patch.object(puzzle_drag, "_page_has_content", lambda p: True), \
             patch.object(puzzle_drag.time, "monotonic", self._mono), \
             patch.object(puzzle_drag.time, "sleep", lambda s: None):
            return puzzle_drag._drag_to(self.page, target_vx=TARGET_VX,
                                        info=info, bx=BX, score=1.0,
                                        deadline=1000.0 + self.budget)


def test_mouse_is_released_on_every_deadline_stop():
    """_finish обязан отпустить кнопку — это ЧАСТЬ КОНТРАКТА, а не страховка.

    _finish зовётся на deadline_before_pick / deadline_micro / deadline_nudge /
    grip_exhausted, и три из них срабатывают С ЗАЖАТОЙ кнопкой. Без
    mouse.up() следующий заход стартует поверх mousePressed, а страница
    получает mousedown без mouseup — в живом логе 2026-10-07 это дало
    `moved_px=-0.88`, то есть кусок «поехал назад» при команде вперёд.

    Проверка перебирает МНОГО точек дедлайна, потому что важно не «дойти до
    одного stop», а чтобы ЛЮБОЙ выход освобождал кнопку.
    """
    stops = set()
    for budget_calls in range(2, 60):
        run = _DeadlineRuns(budget_calls)
        run.run()
        page = run.page
        assert page.pressed is False, f"кнопка зажата (дедлайн={budget_calls})"
        assert page.events.count("down") == page.events.count("up"), \
            f"непарный down/up (дедлайн={budget_calls}): {page.events}"
        stops.add(puzzle_drag.last_drag_diag().get("stop"))
    assert any(s and s.startswith("deadline") for s in stops), stops
    assert "deadline_micro" in stops, stops


def test_deadline_micro_reports_measured_error():
    """На выходе по дедлайну паспорт обязан нести ИЗМЕРЕННУЮ ошибку."""
    run = _DeadlineRuns(12)
    run.run()
    diag = puzzle_drag.last_drag_diag()
    assert diag.get("stop") == "deadline_micro", diag
    assert diag.get("final_err") is not None, diag
    assert abs(diag["final_err"]) > puzzle_drag.ACC, diag
    assert diag.get("moved_px") is not None, diag


def test_mouse_up_swallows_cdp_error():
    class _P:
        class mouse:
            @staticmethod
            def up():
                raise RuntimeError("cdp gone")

    assert puzzle_drag._mouse_up(_P()) is False


# =============================================================================
# 5. Бюджет: длина траектории и доставка конечной координаты (дефект 3)
# =============================================================================

def test_fit_points_keeps_requested_length_without_budget():
    """Бюджета нет (точечный свип/тест) — форма остаётся случайной."""
    assert PI._fit_points(0.0, 120.0, 22) == 22
    assert PI._fit_points(-1.0, 120.0, 14) == 14


def test_fit_points_shrinks_when_mouse_move_is_slow():
    """0.73с на точку (12.34s / 17) — 22 точки не влезают ни в один заход.

    При 20с остатка и стоимости точки 0.73с доля PATH_BUDGET_SHARE даёт 9
    точек вместо 22; на здоровой вкладке (0.01с) — все, что просили.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(PI, "_MOVE_COST_S", [STEP_SEC])
        slow = PI._fit_points(20.0, 160.0, 22)
        assert slow < 22, slow
        assert slow >= PI.PATH_MIN_POINTS
        mp.setattr(PI, "_MOVE_COST_S", [0.01])
        fast = PI._fit_points(20.0, 160.0, 22)
    assert fast == 22, fast


def test_fit_points_never_below_minimum():
    """Даже при нулевом остатке команда должна быть отправлена."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(PI, "_MOVE_COST_S", [10.0])
        assert PI._fit_points(0.001, 400.0, 30) == PI.PATH_MIN_POINTS


def test_human_move_truncated_by_deadline_still_delivers_target():
    """Обрезанная траектория ОБЯЗАНА доставить конечную координату.

    Иначе кусок остаётся там, где его оставил последний промежуточный move, а
    доводка считает позицию по этой точке и «едет мимо» — то есть обрезка по
    бюджету тихо превращается в промах.
    """
    moves = []

    class _Mouse:
        def move(self, x, y):
            moves.append((x, y))

    class _P:
        mouse = _Mouse()

    pts = [(float(i), 0.0) for i in range(20)] + [(999.0, 0.0)]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(PI.time, "sleep", lambda s: None)
        # дедлайн уже прошёл: ни одна промежуточная точка не отправляется
        sent = PI._human_move(_P(), pts,
                              deadline=PI.time.monotonic() - 1.0)
    assert moves == [(999.0, 0.0)], moves
    assert sent == 1


def test_human_move_sends_every_point_when_budget_is_ample():
    """С дедлайном «за горизонтом» поведение прежнее: точка на mouse.move."""
    moves = []

    class _Mouse:
        def move(self, x, y):
            moves.append((x, y))

    class _P:
        mouse = _Mouse()

    pts = [(float(i), 0.0) for i in range(6)]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(PI.time, "sleep", lambda s: None)
        sent = PI._human_move(_P(), pts,
                              deadline=PI.time.monotonic() + 60.0)
    assert sent == len(pts) == 6, sent
    assert moves == pts


def test_human_move_measures_point_cost():
    """Стоимость точки нужна, чтобы следующая траектория была короче.

    Часы подменены: time.monotonic на Windows имеет шаг ~15.6 мс, и на реальном
    времени короткая пачка дала бы нулевую разницу.
    """
    moves = []

    class _Mouse:
        def move(self, x, y):
            moves.append((x, y))

    class _P:
        mouse = _Mouse()

    clock = [1000.0]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(PI, "_MOVE_COST_S", [0.0])
        mp.setattr(PI.time, "sleep", lambda s: None)
        mp.setattr(PI.time, "monotonic",
                   lambda: (clock.__setitem__(0, clock[0] + 0.05),
                            clock[0])[1])
        PI._human_move(_P(), [(0.0, 0.0), (10.0, 0.0)])
        # две точки, часы прошли 0.05с → 0.025с на точку
        assert PI.move_cost() == pytest.approx(0.025, abs=0.005), PI.move_cost()
        # вторая пачка усредняется, а не затирает оценку
        clock[0] = 1000.0
        PI._human_move(_P(), [(0.0, 0.0), (10.0, 0.0)])
        assert PI.move_cost() == pytest.approx(0.025, abs=0.005), PI.move_cost()


def test_await_verdict_is_clamped_by_remaining_budget():
    """Вердикт после дропа не может съесть больше остатка бюджета.

    В логе бюджет 60с был исчерпан на 55-й секунде, после чего ещё платились
    12s ожидания вердикта — и «бюджет 60с» в настройке не означал 60с.
    """
    import ozon_collector.log as _log
    orig = _log.warning
    _log.warning = lambda m, extra=None: None
    try:
        page = object()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(puzzle_drag, "_verdict", lambda p: "no")
            mp.setattr(puzzle_drag, "_still_challenge", lambda p: False)
            mp.setattr(puzzle_drag.time, "sleep", lambda s: None)
            clock = [0.0]

            def _mono():
                clock[0] += 1.0
                return clock[0]

            mp.setattr(puzzle_drag.time, "monotonic", _mono)
            st = puzzle_drag._await_verdict(page, 12.0, 1.2, left=0.5)
        assert st == "no"
        assert clock[0] <= 2.0, clock
    finally:
        _log.warning = orig


def test_left_reports_remaining_budget():
    import ozon_collector.puzzle_drag as pd
    assert pd._left(0.0) == 0.0
    assert pd._left(pd.time.monotonic() - 3.0) < 0.0
    assert pd._left(pd.time.monotonic() + 10.0) > 9.0


# =============================================================================
# 6. Бюджет дропов и инвариант KPI (дефект 2)
# =============================================================================

def _png_bytes(w, h):
    """Кусок с ПРОЗРАЧНОЙ маржой — иначе _mask_edges пуст (см. ниже).

    Силуэт строится из альфы CDN-PNG, а _mask_edges смотрит на разницу
    силуэта с его эрозией. У полностью непрозрачного прямоугольника эрозия
    совпадает с ним и контура нет — значит shape-детектор вернёт пусто, и
    тест проверит не то. Реальный кусок прозрачен по краям.
    """
    from PIL import Image
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = im.load()
    for x in range(w // 4, w - 2):
        for y in range(3, h - 3):
            px[x, y] = (200, 120, 60, 255)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _solve_env(monkeypatch, drag_results, forbid_margin=True):
    """Подготовить solve_puzzle: живой кандидат, драги — по списку исходов."""
    rng = np.random.default_rng(7)
    bg = rng.integers(0, 200, (384, 512)).astype(np.float32)
    info = {"bg": {"src": "http://x/bg.png", "r": [BG_X, 93.42, 512.0, 384.0]},
            "puzzle": {"src": "http://x/p.png",
                       "r": [PX0, 303.42, 170.24, 128.0]}}
    seq = list(drag_results)
    calls = {"drag": 0}

    def _drag(*a, **kw):
        i = min(calls["drag"], len(seq) - 1)
        calls["drag"] += 1
        return seq[i]

    monkeypatch.setattr(puzzle.time, "sleep", lambda s: None)
    monkeypatch.setattr(puzzle, "_capture_info", lambda page: info)
    monkeypatch.setattr(puzzle, "_bg_from_cdn",
                        lambda i: {"bg_l": bg, "bg_r": info["bg"]["r"],
                                   "pz_r": info["puzzle"]["r"]})
    monkeypatch.setattr(puzzle, "_fetch", lambda url: _png_bytes(64, 64))
    monkeypatch.setattr(puzzle, "_slot_spy_install", lambda page: False)
    monkeypatch.setattr(puzzle, "_drag_to", _drag)
    monkeypatch.setattr(puzzle, "_await_verdict", lambda *a, **k: "no")
    monkeypatch.setattr(puzzle, "_still_challenge", lambda page: True)
    monkeypatch.setattr(puzzle, "_captcha_done", lambda page: False)
    monkeypatch.setattr(puzzle, "_stub_page", lambda page: False)
    monkeypatch.setattr(puzzle, "_captcha_src_changed", lambda page, i: False)
    monkeypatch.setattr(puzzle, "_page_state", lambda page: {})
    monkeypatch.setattr(puzzle, "_last_move", lambda: 12.0)
    if forbid_margin:
        monkeypatch.setattr(
            puzzle, "_shot_piece_margin",
            lambda p, i: pytest.fail(
                "рендерная маржа не нужна, когда shape-детектор нашёл лунку: "
                "она стоит 2 скриншота и не может изменить cands[0]"))
    else:
        monkeypatch.setattr(puzzle, "_shot_piece_margin", lambda p, i: 11)
    return calls


class _SolvePage:
    def __init__(self):
        self.mouse = self

    def evaluate(self, js):
        return None

    def move(self, x, y, steps=None):
        return self

    def down(self):
        return self

    def up(self):
        return self


def test_rejected_drops_consume_drop_budget(monkeypatch):
    """Дефект 2: бюджет дропов сравнивался с глобальным счётчиком провалов.

    На возвратах "reject" из _drag_bx (а это и есть провальный дроп) счётчик
    НЕ рос, поэтому бюджет не срабатывал никогда; и наоборот, копился за всю
    сессию. Теперь лимит считает отказы за ОДИН пазл.
    """
    calls = _solve_env(monkeypatch, [False] * 20)
    evs = []
    puzzle.reset()
    ok = puzzle.solve_puzzle(_SolvePage(), attempts=3, timeout_s=0.0,
                             on_event=evs.append)
    assert ok is False
    budget = [e for e in evs if e.get("kind") == "budget"]
    assert budget, evs
    assert budget[0]["drops"] == puzzle.DROP_BUDGET, budget
    # ровно столько дропов успело состояться, сколько потрачено
    assert calls["drag"] == puzzle.DROP_BUDGET, calls


def test_drop_budget_is_per_solve_not_accumulated_over_session(monkeypatch):
    """Провал ПРЕДЫДУЩЕГО пазла не должен запрещать дропы на следующем.

    Прежний счётчик жил в _PUZZLE_FAILURES (сессионный), поэтому после 4
    провалов за сессию любой следующий пазл отказывался двигать кусок, даже
    имея полный бюджет времени.
    """
    puzzle.reset()
    puzzle._PUZZLE_FAILURES[0] = 99
    calls = _solve_env(monkeypatch, [False] * 20)
    evs = []
    ok = puzzle.solve_puzzle(_SolvePage(), attempts=3, timeout_s=0.0,
                             on_event=evs.append)
    assert ok is False
    assert calls["drag"] >= 1, "провал прошлого пазла отключил текущий"
    assert any(e.get("kind") == "budget" for e in evs), evs


def test_solve_puzzle_never_returns_none(monkeypatch):
    """solve_puzzle обязан возвращать bool, а не падать с конца функции.

    Хвост функции стоял на уровне ТЕЛА цикла попыток, а последний оператор в
    нём — `continue`. То есть на пути «все круги ушли на continue» функция
    доходила до конца и возвращала None: falsy, но не False, без failures += 1
    и без записи в паспорт. В логе это читалось как «пазл не решён за 0
    попыток» — при живом паузе с полным бюджетом времени.
    """
    _solve_env(monkeypatch, [False] * 20)
    puzzle.reset()
    ok = puzzle.solve_puzzle(_SolvePage(), attempts=2, timeout_s=0.0)
    assert ok is False, repr(ok)
    assert puzzle.attempts() == puzzle.failures() + puzzle.solved(), (
        puzzle.attempts(), puzzle.failures(), puzzle.solved())


def test_attempts_equal_failures_plus_solved_on_drop_budget_exit(monkeypatch):
    """Инвариант KPI на пути, который раньше его ломал."""
    _solve_env(monkeypatch, [False] * 20)
    puzzle.reset()
    puzzle.solve_puzzle(_SolvePage(), attempts=3, timeout_s=0.0)
    assert puzzle.attempts() == puzzle.failures() + puzzle.solved(), (
        puzzle.attempts(), puzzle.failures(), puzzle.solved())
    assert puzzle.failures() >= 1


def test_attempts_equal_failures_plus_solved_on_timeout_exit(monkeypatch):
    """Тот же инвариант на таймауте от _drag_bx (тоже не считался раньше)."""
    _solve_env(monkeypatch, [False] * 20)
    clock = [1000.0]
    monkeypatch.setattr(puzzle, "_still_challenge", lambda page: True)
    monkeypatch.setattr(puzzle.time, "monotonic",
                        lambda: (clock.__setitem__(0, clock[0] + 9.0),
                                 clock[0])[1])
    puzzle.reset()
    ok = puzzle.solve_puzzle(_SolvePage(), attempts=3, timeout_s=12.0)
    assert ok is False
    assert puzzle.attempts() == puzzle.failures() + puzzle.solved(), (
        puzzle.attempts(), puzzle.failures(), puzzle.solved())


def test_live_frame_capture_is_skipped_when_shape_found_slot(monkeypatch):
    """Живой кадр фона и рендерная маржа не платят за съёмку зря.

    _shot_bg_clean = 8 скриншотов (таймаут 5с каждый) + ~40 evaluate,
    _shot_piece_margin = ещё 2 скриншота. При shape-кандидате cands[0] =
    shape_cands[0] независимо от diff/edges (сдвиг 1e6 против шкалы ≤510), и
    _shot_order стреляет только в cands[0] и ±3 — то есть съёмка не может
    изменить ни одного выстрела, но съедает треть-половину PUZZLE_BUDGET_S.
    """
    shots = {"n": 0}

    def _shot(page, info):
        shots["n"] += 1
        return None

    _solve_env(monkeypatch, [False] * 20)
    monkeypatch.setattr(puzzle, "_shot_bg_clean", _shot)
    puzzle.reset()
    puzzle.solve_puzzle(_SolvePage(), attempts=1, timeout_s=0.0)
    assert shots["n"] == 0, "живой кадр снят вхолостую"


def test_live_frame_capture_still_happens_without_shape_candidate(monkeypatch):
    """Проверка, что ленивость НЕ отключает проверку: без shape кандидата
    кадр снимается и дифф-детекторы работают как раньше."""
    shots = {"n": 0}

    def _shot(page, info):
        shots["n"] += 1
        rng = np.random.default_rng(3)
        live = rng.integers(0, 255, (384, 512)).astype(np.float32)
        return {"bg_l": live, "b_r": [BG_X, 93.42, 512.0, 384.0],
                "pz_r": [PX0, 303.42, 170.24, 128.0], "orig": b""}

    _solve_env(monkeypatch, [False] * 20, forbid_margin=False)
    monkeypatch.setattr(puzzle, "_shot_bg_clean", _shot)
    monkeypatch.setattr(puzzle, "_detect_slot_shape", lambda *a, **k: [])
    monkeypatch.setattr(puzzle, "_solve_cands",
                        lambda mask, e, d, v, sil, shape_cands=None: [])
    puzzle.reset()
    ok = puzzle.solve_puzzle(_SolvePage(), attempts=1, timeout_s=0.0)
    assert ok is False
    assert shots["n"] >= 1, "без shape-детектора живой кадр обязателен"


# =============================================================================
# 7. target_vx против bx: оси согласованы, это НЕ причина провала (дефект 4 из
#    подсказки пользователя — проверяем, чтобы не «починили» не то)
# =============================================================================

def test_target_vx_is_bg_left_plus_slot_x():
    """target_vx = b_r[0] + bx — та же ось, что и у детектора.

    b_r[0]=456 при фоне 512px и piece_w=170: bg занимает 456..968, слот 145
    внутри фона → 601. bx=145 и target_vx=601 — не «разные оси», а локальная
    и экранная запись одной координаты. Расхождение величиной 456 — это и есть
    левый край фона во вьюпорте.
    """
    info = {"bg": {"r": [BG_X, 93.42, 512.0, 384.0]}}
    b_r = info["bg"]["r"]
    assert b_r[0] + BX == pytest.approx(TARGET_VX)
    assert puzzle._slot_ok(BX, 512, 170) is True


def test_required_travel_fits_slider_range():
    """115.56px хода куска помещаются в ход слайдера (440px) с запасом.

    То есть «кусок не может туда доехать из-за геометрии» — тоже не причина.
    """
    need = TARGET_VX - PX0
    assert need == pytest.approx(115.56, abs=0.01)
    slider_travel = (472.0 + 480.0 - 40.0 - 1.0) - 472.0
    assert need / K_BIG < slider_travel, (need, K_BIG, slider_travel)


# =============================================================================
# 8. Маркеры шагов прокрутки: дефект 4 из списка ревью
# =============================================================================

@pytest.mark.parametrize("js,marker", ((C.SCROLL_STEP_UP_JS, "step-up"),
                                       (C.SCROLL_STEP_DOWN_JS, "step-down")))
def test_scroll_step_js_is_recognised_as_function(js, marker):
    """Ведущий `/*step-up*/` ломал ffsel_js.js_is_function.

    Строка уходила в execute_script как выражение, возвращала объект-функцию и
    НЕ скроллила: на Firefox-ветке (FakePage) top-up не добирал отзывы молча.
    Маркер при этом должен остаться в строке — по нему идёт идентификация.
    """
    assert ffsel_js.js_is_function(js) is True
    assert marker in js


def test_scroll_step_js_begins_with_arrow_function():
    """Прямая проверка формы: обрезка и strip() видят `()` первым символом."""
    for js in (C.SCROLL_STEP_UP_JS, C.SCROLL_STEP_DOWN_JS):
        assert js.strip().startswith("("), repr(js[:40])