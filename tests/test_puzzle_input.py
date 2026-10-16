"""Биометрия траекторий мыши (ревизия 2.9, ADR §10.4).

Что проверяем:
- траектория заканчивается ровно в цели (overshoot отыгрывается назад —
  иначе сломается сверка позиции куска в puzzle_drag);
- параметры формы РАНДОМИЗИРУЮТСЯ между драгами (нет одной кривой);
- overshoot реально есть и не вылезает за границу дорожки (bound);
- микро-паузы (dwell) есть и не превращают движение в лестницу;
- y-траектория не идеально горизонтальна (нет «прямых» бота);
- обратная совместимость: _human_path для подводов/кликов не сломана.
"""
import random

from ozon_collector import puzzle_input as PI


def _xs(pts):
    return [p[0] for p in pts]


def test_drag_ends_exactly_at_target():
    for seed in range(25):
        rng = random.Random(seed)
        pts, _shape = PI._human_drag_path(0, 100, 180, 104, rng=rng)
        assert pts[-1] == (180, 104), seed


def test_shape_params_vary_between_drags():
    shapes = [PI._human_drag_path(0, 0, 150, 0, rng=random.Random(s))[1]
              for s in range(30)]
    assert len({s["n"] for s in shapes}) > 5
    assert len({round(s["ease_k"], 2) for s in shapes}) > 8
    assert len({s["arc"] for s in shapes}) > 5
    assert len({s["tremor"] for s in shapes}) > 5
    # ~15% драгов — с недолётом ( undershoot): не у всех людей рука
    # проезжает дальше цели, и наоборот.
    assert any(s["overshoot"] < 0 for s in shapes)
    assert any(s["overshoot"] > 0 for s in shapes)


def test_overshoot_present_and_bounded():
    over_x, saw_back = 0, 0
    for seed in range(30):
        rng = random.Random(seed)
        pts, shape = PI._human_drag_path(0, 0, 200, 0, rng=rng, bound=260)
        mx = max(_xs(pts))
        assert mx <= 260, "не вылезаем за правую границу дорожки"
        if shape["overshoot"] > 0:
            over_x += 1
            assert mx >= 199.0, "перелёт должен достигать цели"
        # после перелёта есть движение НАЗАД (к цели)
        if any(_xs(pts)[i] > _xs(pts)[i + 1] for i in range(len(pts) - 1)):
            saw_back += 1
    assert over_x >= 10 and saw_back >= 10


def test_overshoot_dropped_at_track_edge():
    """У самого края дорожки перелёта нет: превышать нечего."""
    for seed in range(20):
        pts, shape = PI._human_drag_path(0, 0, 200, 0,
                                         rng=random.Random(seed), bound=200.5)
        assert shape["overshoot"] <= 0.0
        assert max(_xs(pts)) <= 200.5


def test_path_is_not_perfectly_flat():
    """Даже «горизонтальный» драг имеет вертикальный микро-дрейф."""
    ys = set()
    for seed in range(15):
        pts, _ = PI._human_drag_path(0, 100, 200, 100, rng=random.Random(seed))
        ys.update(round(p[1], 2) for p in pts)
    assert len(ys) > 15


def test_holds_present_and_in_range():
    for seed in range(10):
        _pts, shape = PI._human_drag_path(0, 0, 200, 0,
                                          rng=random.Random(seed))
        holds = shape["holds"]
        assert holds, "нужны микро-паузы (dwell)"
        assert all(2 <= h < shape["n"] - 1 for h in holds)
        assert holds == sorted(holds)
        assert len(set(holds)) == len(holds)


def test_dwell_pauses_are_longer(monkeypatch):
    """В точках hold пауза длиннее обычных шагов."""
    slept = []

    class _Mouse:
        def move(self, x, y):
            pass

    class _Page:
        mouse = _Mouse()

    monkeypatch.setattr(PI.time, "sleep", lambda s: slept.append(s))
    pts = [(float(i), 0.0) for i in range(6)]
    PI._human_move(_Page(), pts, hold_ranges=(2, 4))
    assert len(slept) == 5
    assert slept[2] > 0.04 and slept[4] > 0.04
    assert max(slept[2], slept[4]) > max(slept[0], slept[1], slept[3])


def test_human_drag_emits_mouse_events(monkeypatch):
    moves = []

    class _Mouse:
        def move(self, x, y):
            moves.append((x, y))

    class _Page:
        mouse = _Mouse()

    monkeypatch.setattr(PI.time, "sleep", lambda s: None)
    shape = PI._human_drag(_Page(), 0, 0, 120, 0, rng=random.Random(7))
    assert moves[-1] == (120, 0)
    assert len(moves) == shape["n"] + 1 + 2


def test_zero_distance_drag_is_safe():
    pts, shape = PI._human_drag_path(10, 10, 10, 10, rng=random.Random(1))
    assert pts[-1] == (10, 10)
    assert shape["arc"] == 0.0 and shape["overshoot"] == 0.0


def test_legacy_human_path_still_works():
    """Подводы/клики продолжают использовать старый путь (без overshoot)."""
    pts = PI._human_path(0, 0, 30, 0, n=12, arc=2.0)
    assert len(pts) == 13
    assert pts[-1] == (30, 0)


def test_ease_monotonic_and_bounded():
    for k in (1.5, 2.0, 2.8):
        vals = [PI._ease(i / 20, k) for i in range(21)]
        assert vals[0] == 0.0 and vals[-1] == 1.0
        assert all(b >= a for a, b in zip(vals, vals[1:])), k


def test_drag_path_tiny_n_has_no_bogus_holds():
    """n=3..5 не роняют выбор dwell-точек (PATH_MIN_POINTS=3 достижим).

    Было: `max(1, n - 3)` при n=3 требовал сэмпл из пустого range(2, n-1) →
    ValueError прямо посреди драга (аудит 2026-10-15).
    """
    for n in (3, 4, 5):
        _pts, shape = PI._human_drag_path(0, 0, 60, 0, rng=random.Random(1),
                                          n=n)
        assert all(2 <= h <= n - 2 for h in shape["holds"])
    _pts, shape = PI._human_drag_path(0, 0, 60, 0, rng=random.Random(1), n=3)
    assert shape["holds"] == []
