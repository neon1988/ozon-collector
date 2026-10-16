"""Тесты механики драга: калибровочный зонд по расстоянию до цели.

Критика #1: фиксированный зонд +40px перешагивал цель, когда лунка рядом с
кусочком. Зонд теперь = половина расстояния (в границах), либо 0 — тогда
калибровка пропускается и работает замкнутый контур по фактическому px.
"""

import pytest

from ozon_collector import puzzle
from ozon_collector import puzzle_drag
from ozon_collector import puzzle_input


def test_calib_step_scales_with_distance():
    """Зонд = половина расстояния в [CALIB_MIN, CALIB_MAX], 0 если места нет."""
    assert puzzle_drag._calib_step(200.0) == 40.0     # дальняя цель — как было
    assert puzzle_drag._calib_step(80.0) == 40.0      # потолок
    assert puzzle_drag._calib_step(40.0) == 20.0
    assert puzzle_drag._calib_step(20.0) == 10.0
    assert puzzle_drag._calib_step(12.0) == 6.0       # минимум измеримого
    assert puzzle_drag._calib_step(7.0) == 0.0        # места для зонда нет
    assert puzzle_drag._calib_step(0.0) == 0.0


def test_calib_step_never_overshoots_target():
    """Инвариант: зонд никогда не заходит на цель."""
    for d in [0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 8.0, 12.0, 20.0, 40.0, 79.0]:
        c = puzzle_drag._calib_step(d)
        assert c <= max(0.0, d - puzzle_drag.CALIB_GUARD) + 1e-9
        assert c <= puzzle_drag.CALIB_MAX


def test_k_from_probe_piece_mode_one_to_one():
    """piece: мышь тянет кусок 1:1 → k = dpx/calib (не /40)."""
    r0 = {"px": 50.0, "s": None}
    r1 = {"px": 58.0, "s": None}
    assert puzzle_drag._k_from_probe(r0, r1, 8.0, "piece") == pytest.approx(1.0)
    # лаг отрисовки куска (меньше мыши) — k<1, и это ловится
    assert puzzle_drag._k_from_probe(r0, {"px": 54.0, "s": None},
                                     8.0, "piece") == pytest.approx(0.5)


def test_k_from_probe_slider_divides_by_MOUSE_travel():
    """slider: знаменатель — ИЗВЕСТНЫЙ сдвиг МЫШИ, а не ход слайдера.

    Разбор прогона 2026-10-07 (слайдер перестал доезжать). Доводка управляет
    МЫШЬЮ: dest в _drag_to — координата мыши. Значит коэффициент, который
    гасит ошибку куска, обязан быть полным «мышь→кусок». Прежняя схема
    считала k = «кусок/слайдер» и подставляла его в координату мыши, то есть
    домножала ещё на связку мышь→слайдер, которая в живой капче плавает.

    Числа из живого лога: 40px мыши → слайдер +40, кусок +23.86. Старая схема
    дала бы 23.86/40 = 0.596 здесь случайно (dsl==calib), но на большом ходе
    156px слайдера кусок ушёл на 58.4 → 0.373, и доводка с замороженным 0.596
    считала шаг 55.9px, обрезала до 12 и получала 4.5px хода куска.
    """
    r0 = {"px": 10.0, "s": 100.0}
    r1 = {"px": 20.0, "s": 125.0}
    assert puzzle_drag._k_from_probe(r0, r1, 40.0, "slider") == pytest.approx(0.25)
    # мышь и слайдер разошлись (dsl=25 при ходе мыши 40) — коэффициент
    # обязан считаться по мыши, иначе ошибка масштаба теряется
    r1b = {"px": 20.0, "s": 125.0}
    assert puzzle_drag._k_from_probe(r0, r1b, 20.0, "slider") == pytest.approx(0.5)


def test_k_from_probe_grip_fail_returns_none():
    """Кусок не поехал / нет замера → None = хват не удался (retry)."""
    r0 = {"px": 50.0, "s": 0.0}
    assert puzzle_drag._k_from_probe(r0, {"px": 50.5, "s": 40.0},
                                     40.0, "piece") is None
    assert puzzle_drag._k_from_probe(r0, {"px": None, "s": 40.0},
                                     40.0, "piece") is None
    assert puzzle_drag._k_from_probe(r0, None, 40.0, "piece") is None


def test_k_from_probe_slider_without_travel_warns(caplog, monkeypatch):
    """Полоса слайдера не поехала — коэффициент ВСЁ РАВНО полный, но молчать нельзя.

    Почему не None и не 1.0: `s` больше не входит в формулу коэффициента
    (см. test_k_from_probe_slider_divides_by_MOUSE_travel), возвращать None
    означало бы объявить поломкой захвата связку, которая жива. Расхождение
    «мышь едет, полоса не читается» остаётся аномалией, о ней нужен голос в
    логе — иначе рассинхрон масштабов снова придётся выводить из разбора.
    """
    import ozon_collector.log as _log
    lines = []
    monkeypatch.setattr(_log, "warning",
                        lambda m, extra=None: lines.append(str(m)))
    r0 = {"px": 10.0, "s": 100.0}
    r1 = {"px": 30.0, "s": 100.0}      # кусок +20, полоса 0
    k = puzzle_drag._k_from_probe(r0, r1, 40.0, "slider")
    assert k == pytest.approx(0.5), k
    assert any("полоса слайдера не читается" in m for m in lines), lines


def test_k_from_probe_slider_missing_s_coord_is_loud():
    """s=None в обоих замерах (координата не читается) — тоже предупреждение."""
    import ozon_collector.log as _log
    lines = []
    orig = _log.warning
    _log.warning = lambda m, extra=None: lines.append(str(m))
    try:
        r0 = {"px": 10.0, "s": None}
        r1 = {"px": 30.0, "s": None}
        assert puzzle_drag._k_from_probe(r0, r1, 40.0, "slider") == \
            pytest.approx(0.5)
    finally:
        _log.warning = orig
    assert any("полоса слайдера не читается" in m for m in lines), lines


def _recording_drag(target_vx, px_seq):
    """Прогнать _drag_to с записью траекторий; вернуть (paths, result)."""
    from unittest.mock import patch
    paths = []
    real_path = puzzle_drag._human_path

    def _rec(x0, y0, x1, y1, **kw):
        pts = real_path(x0, y0, x1, y1, **kw)
        paths.append(((x0, y0), (pts[-1][0], pts[-1][1])))
        return pts

    reads = {"n": 0}

    def _read(_page):
        i = reads["n"]
        reads["n"] += 1
        px = px_seq[min(i, len(px_seq) - 1)]
        return {"px": float(px), "s": None, "ps": 10, "ss": None}

    class _Page:
        def evaluate(self, _js):
            return None

        @property
        def mouse(self):
            return self

        def move(self, _x, _y, steps=None):
            return self

        def down(self):
            return self

        def up(self):
            return self

    info = {"bg": {"r": [0, 0, 300, 100]},
            "puzzle": {"r": [10, 20, 40, 40]}}
    # _human_click/_human_move лежат в puzzle_input и дёргают _human_path
    # СВОЕГО модуля, а _drag_to — из puzzle_drag: патчим оба namespace'а,
    # иначе в запись не попадает траектория подвода/нажатия.
    with patch.object(puzzle_drag, "_find_slider", lambda p: None), \
         patch.object(puzzle_drag, "_read_sl_piece", side_effect=_read), \
         patch.object(puzzle_drag, "_still_challenge", lambda p: True), \
         patch.object(puzzle_drag, "_captcha_done", lambda p: False), \
         patch.object(puzzle_drag, "_human_path", side_effect=_rec), \
         patch.object(puzzle_input, "_human_path", side_effect=_rec), \
         patch.object(puzzle_drag.time, "sleep", lambda _s: None):
        res = puzzle._drag_to(_Page(), target_vx=target_vx, info=info,
                              bx=200, score=1.0)
    return paths, res


def test_drag_diag_filled_on_failure():
    """Паспорт заполняется даже когда заход ПРОВАЛИЛСЯ.

    Зачем: паспорт читается в solve_puzzle при разборе причины отказа. Если он
    заполняется только на успехе, то провал (тот случай, который и разбирают)
    останется без фактов — то есть без t_read/t_move/финальной ошибки.
    Живой замер 2026-10-02: провал шёл 178 с, и по логу не было видно ни
    сколько времени заняло чтение позиции, ни ввода.
    """
    # кусок стабильно НЕ доезжает до цели → доводка исчерпывает итерации
    paths, res = _recording_drag(900.0, [500.0, 500.0, 500.0])
    d = puzzle_drag.last_drag_diag()
    assert d, "паспорт пуст — причина провала останется без фактов"
    assert d.get("stop"), d
    assert "t_total_s" in d, d
    assert "t_read_s" in d and "t_move_s" in d, d
    assert d.get("probe_reads", 0) >= 1, d
    assert d.get("bx") == 200 and d.get("target_vx") == 900.0, d
    # iters может остаться 0, если до микрокоррекций не дошли (напр. кусок не
    # зацепили мышью) — тогда stop описывает именно эту ветку
    assert d.get("iters", 0) >= 0, d


def test_drag_probe_is_half_distance_for_near_target():
    """Близкая цель: зонд НЕ 40px, а ~половины расстояния (без перелёта)."""
    dist = 10.0
    paths, _ = _recording_drag(50.0 + dist, [50.0, 50.0 + 6.0, 60.0])
    calib_move = paths[1]              # 0=подвод к хвату, 1=зонд, 2=доводка
    dx = calib_move[1][0] - calib_move[0][0]
    assert dx == pytest.approx(puzzle_drag._calib_step(dist))
    assert dx < puzzle_drag.CALIB_MAX


def test_drag_probe_keeps_legacy_40px_for_far_target():
    """Дальняя цель (≥80px): поведение прежнее — зонд 40px (регрессия-гейт)."""
    paths, _ = _recording_drag(50.0 + 200.0, [50.0, 90.0, 250.0])
    calib_move = paths[1]
    dx = calib_move[1][0] - calib_move[0][0]
    assert dx == pytest.approx(40.0)


def test_drag_skips_probe_when_target_adjacent():
    """Цель вплотную (|err| <= ACC): зонд не делается вовсе, только дроп."""
    paths, _ = _recording_drag(100.5, [100.0, 100.0, 100.5])
    assert len(paths) == 1             # только подвод к хвату, зонда нет


def test_drag_no_probe_uses_conservative_k_and_settles():
    """Без зонда k=1.0 и доезд по фактическому px (недооценка, не перелёт)."""
    from unittest.mock import patch
    reads = {"n": 0}
    seen = []

    def _read(_page):
        i = reads["n"]
        reads["n"] += 1
        # цель 60, кусок 50: первый шаг без зонда = +1.5, дальше конвергенция
        px = [50.0, 50.0, 51.5, 60.0][min(i, 3)]
        seen.append(px)
        return {"px": px, "s": None, "ps": 10, "ss": None}

    class _Page:
        def evaluate(self, _js):
            return None

        @property
        def mouse(self):
            return self

        def move(self, _x, _y, steps=None):
            return self

        def down(self):
            return self

        def up(self):
            return self

    info = {"bg": {"r": [0, 0, 300, 100]},
            "puzzle": {"r": [10, 20, 40, 40]}}
    with patch.object(puzzle_drag, "_find_slider", lambda p: None), \
         patch.object(puzzle_drag, "_read_sl_piece", side_effect=_read), \
         patch.object(puzzle_drag, "_still_challenge", lambda p: True), \
         patch.object(puzzle_drag, "_captcha_done", lambda p: False), \
         patch.object(puzzle_drag.time, "sleep", lambda _s: None):
        puzzle._drag_to(_Page(), target_vx=60.0, info=info, bx=200, score=1.0)
    # r0 прочитан до расчёта dist, дальше — измерение/доводка
    assert seen[0] == 50.0
    assert max(seen) <= 60.0          # кусок не уехал за цель


def test_drag_releases_button_when_engine_raises():
    """Исключение в середине драга не оставляет кнопку зажатой.

    Аудит 2026-10-15: без try/finally обрыв CDP (evaluate/mouse) вылетал
    наружу с mousePressed, и следующий заход стартовал поверх зажатой
    кнопки. Тест воспроизводит падение на чтении позиции после хвата.
    """
    from unittest.mock import patch

    up_calls = []

    class _Mouse:
        def move(self, _x, _y, steps=None):
            return self

        def down(self):
            return self

        def up(self):
            up_calls.append(1)
            return self

    class _Page:
        mouse = _Mouse()

        def evaluate(self, _js):
            return None

    def _boom(*_a, **_kw):
        raise RuntimeError("cdp died mid-drag")

    info = {"bg": {"r": [0, 0, 300, 100]},
            "puzzle": {"r": [10, 20, 40, 40]}}
    with patch.object(puzzle_drag, "_find_slider", lambda p: None), \
         patch.object(puzzle_drag, "_read_sl_piece",
                      lambda p: {"px": 50.0, "s": None, "ps": 10,
                                 "ss": None}), \
         patch.object(puzzle_drag, "_read_settled", side_effect=_boom), \
         patch.object(puzzle_drag, "_SIMPLE_DRAG", False), \
         patch.object(puzzle_drag.time, "sleep", lambda _s: None), \
         pytest.raises(RuntimeError, match="cdp died mid-drag"):
        puzzle._drag_to(_Page(), target_vx=60.0, info=info, bx=200, score=1.0)
    assert up_calls == [1], "кнопка обязана быть отпущена в finally"
