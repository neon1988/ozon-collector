import numpy as np
import pytest

from ozon_collector import puzzle


def test_is_puzzle_signatures():
    assert puzzle.is_puzzle("title:Сопоставьте пазл")
    assert puzzle.is_puzzle("двигая ползунок, соберите пазл")
    assert not puzzle.is_puzzle("title:Похоже, нет соединения")
    assert not puzzle.is_puzzle(None)
    assert not puzzle.is_puzzle("")


def test_failures_counter_reset():
    puzzle.reset()
    assert puzzle.failures() == 0
    puzzle._PUZZLE_FAILURES[0] = 3
    assert puzzle.failures() == 3
    puzzle.reset()
    assert puzzle.failures() == 0


@pytest.mark.parametrize("y", [30, 140, 180])
def test_find_notch_positive(y):
    rng = np.random.default_rng(42)
    bg = rng.integers(0, 256, (200, 300, 3), dtype=np.uint8)
    patch = rng.integers(0, 256, (20, 20, 3), dtype=np.uint8)
    bg[y:y + 20, 25:25 + 20] = patch
    piece = patch.copy()
    assert puzzle._find_notch(bg, piece) == 25


def test_find_notch_no_match():
    bg = np.random.default_rng(7).integers(0, 256, (60, 120, 3),
                                           dtype=np.uint8)
    piece = np.random.default_rng(9).integers(0, 256, (20, 20, 3),
                                              dtype=np.uint8)
    assert puzzle._find_notch(bg, piece) is not None


def test_find_notch_piece_larger_than_bg():
    bg = np.zeros((20, 20, 3), dtype=np.uint8)
    piece = np.zeros((40, 40, 3), dtype=np.uint8)
    assert puzzle._find_notch(bg, piece) is None


class _FakeLocator:
    def __init__(self, value):
        self._v = value

    def __call__(self, *a, **k):
        return self

    @property
    def first(self):
        return self

    def click(self):
        pass

    def press_sequentially(self, text, delay=0):
        pass


class _FakePage:
    def __init__(self, sig):
        self.sig = sig
        self.mouse = self
        self._down = False

    def evaluate(self, _js):
        return {"bg": None, "puzzle": None, "slider": None, "has": False}

    def locator(self, sel):
        return _FakeLocator(1)

    def get_by_role(self, role, name=None):
        return _FakeLocator(name)

    def move(self, x, y, steps=None):
        return x

    def down(self):
        self._down = True

    def up(self):
        self._down = False

    def reload(self, *a, **k):
        pass

    @property
    def is_invalid(self):
        return False

    @is_invalid.setter
    def is_invalid(self, v):
        pass


def test_check_challenge_solver_tried_for_puzzle():
    from ozon_collector import collect as C
    calls = []

    def _fail_solve(page):
        calls.append(1)
        return False

    puzzle.reset()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(C, "detect_challenge",
                   lambda page, **kw: "title:Сопоставьте пазл")
    monkey.setattr(C, "_puzzle", puzzle)
    monkey.setattr(puzzle, "solve_puzzle", _fail_solve)
    try:
        try:
            C.check_challenge(_FakePage("puzzle"), "product_page")
        except C.B.AccessChallengeError:
            pass
    finally:
        monkey.undo()
    assert calls  # решатель вызывался


def test_check_challenge_solver_success_skips_fab_reload():
    from ozon_collector import collect as C
    reload_calls = []
    monkey = pytest.MonkeyPatch()
    puzzle.reset()
    monkey.setattr(C, "detect_challenge",
                   lambda page, **kw: "title:Сопоставьте пазл")
    monkey.setattr(C, "_puzzle", puzzle)
    monkey.setattr(puzzle, "solve_puzzle", lambda page: True)
    monkey.setattr(C, "_reload_fn", lambda page: reload_calls.append(1))
    try:
        C.check_challenge(_FakePage("puzzle"), "product_page")
    finally:
        monkey.undo()
    assert not reload_calls  # перезагрузок не было
    assert puzzle.failures() == 0


class _FakePuzzlePage(_FakePage):
    """Страница с найденным слайдер-пазлом в DOM (bg + puzzle)."""

    def evaluate(self, _js):
        return {"bg": {"src": "http://x/bg.png", "r": [0, 0, 300, 100]},
                "puzzle": {"src": "http://x/p.png", "r": [0, 0, 40, 40]},
                "slider": {"r": [0, 0, 100, 20]},
                "has": True}


def test_looks_like_puzzle_detects_dom_elements():
    assert puzzle.looks_like_puzzle(_FakePuzzlePage("x"))


def test_looks_like_puzzle_false_without_bg():
    assert not puzzle.looks_like_puzzle(_FakePage("x"))


class _FakeSliderDomPage(_FakePage):
    """DOM как у живой капчи: bg(512x384), трек(480x2), ручка(40x32)."""

    def __init__(self):
        super().__init__("puzzle")
        self.return_track = [472, 509, 480, 2]
        self.return_handle = [472, 494, 40, 32]

    def evaluate(self, _js):
        return {"track": list(self.return_track),
                "handle": list(self.return_handle)}


def test_find_slider_returns_track_and_handle():
    assert puzzle._find_slider(_FakeSliderDomPage()) == \
        {"track": [472, 509, 480, 2], "handle": [472, 494, 40, 32]}


def test_find_slider_none_when_no_track():
    class _NoTrack(_FakePage):
        def evaluate(self, _js):
            return None

    assert puzzle._find_slider(_NoTrack("x")) is None


def test_check_challenge_solver_runs_for_antibot_captcha_with_dom_puzzle():
    """'antibot captcha' без 'пазл' в сигнатуре, но с пазлом в DOM → решаем."""
    from ozon_collector import collect as C
    calls = []
    monkey = pytest.MonkeyPatch()
    puzzle.reset()
    monkey.setattr(C, "detect_challenge",
                   lambda page, **kw: "title:antibot captcha")
    monkey.setattr(C, "_puzzle", puzzle)
    monkey.setattr(puzzle, "solve_puzzle", lambda page: calls.append(1) or False)
    try:
        try:
            C.check_challenge(_FakePuzzlePage("puzzle"), "product_page")
        except C.B.AccessChallengeError:
            pass
    finally:
        monkey.undo()
    assert calls  # решатель вызывался, несмотря на сигнатуру 'antibot captcha'


class _HoleMask:
    """Реальный bool-массив силуэта кусочка (как _silhouette): гребёнка."""

    H, W = 30, 50

    def __init__(self):
        m = np.zeros((self.H, self.W), dtype=bool)
        m[6:self.H - 4, 5:self.W - 5] = True
        self.shape = m.shape
        self.nl = m


def _make_diff_frames(hole_x, yrow=80, W=400, H=200):
    """cdn = ровный фон с лёгким шумом, live = фон + светлый оверлей лунки."""
    rng = np.random.default_rng(11)
    mask = _HoleMask().nl
    cdn = np.full((H, W), 128.0, dtype=np.float32)
    cdn += rng.integers(0, 9, (H, W))  # CDN-фон с лёгким шумом
    live = cdn.copy()
    live += rng.integers(-4, 5, (H, W))  # шум захвата кадра (независимый)
    mh, mw = mask.shape
    over = live[yrow + 6:yrow + mh - 4, hole_x + 5:hole_x + mw - 5]
    over[...] += 60.0
    live = np.clip(live, 0, 255)
    return live, cdn, mask


@pytest.mark.parametrize("hole_x", [140, 200, 260])
def test_detect_slot_diff_finds_hole(hole_x):
    live, cdn, mask = _make_diff_frames(hole_x)
    cands = puzzle._detect_slot_diff(live, cdn, mask, yrow=80, k=3)
    assert cands, "diff-детектор должен найти оверлей лунки"
    assert abs(cands[0][0] - hole_x) <= 2


@pytest.mark.parametrize("hole_x", [140, 200, 260])
def test_detect_slot_edges_finds_left_edge(hole_x):
    live, cdn, mask = _make_diff_frames(hole_x)
    cands = puzzle._detect_slot_edges(live, cdn, mask, yrow=80, k=3)
    assert cands, "edge-детектор должен найти левую грань лунки"
    # синтетика рисует лунку от hole_x+5 (over-слип) — допуск 6px
    assert abs(cands[0][0] - hole_x) <= 6


def test_detect_slot_edges_empty_when_identical():
    mask = _HoleMask().nl
    cdn = np.full((200, 400), 128.0, dtype=np.float32)
    assert puzzle._detect_slot_edges(cdn.copy(), cdn, mask, yrow=80) == []


def test_grip_point_inside_silhouette():
    m = _HoleMask().nl
    gx, gy = puzzle._grip_point(m)
    assert 0 <= gx < m.shape[1]
    assert 0 <= gy < m.shape[0]
    assert m[int(round(gy)), int(round(gx))], "хват должен быть в непрозрачной зоне"
    assert gy <= m.shape[0] * 0.85


def test_human_path_start_end_exact():
    pts = puzzle._human_path(10.0, 20.0, 300.0, 25.0, n=20)
    assert len(pts) == 21
    assert abs(pts[0][0] - 10.0) <= 1.0 and abs(pts[0][1] - 20.0) <= 1.0
    assert pts[-1] == (300.0, 25.0)


def test_human_path_has_jitter_not_linear():
    pts = puzzle._human_path(10.0, 20.0, 300.0, 20.0, n=30)
    delta = [(x2 - x1, y2 - y1) for (x1, y1), (x2, y2) in zip(pts, pts[1:])]
    # не все шаги одинаковые (не равномерная прямая) — есть ускорение/торможение
    steps = sorted({round(abs(dx), 1) for dx, _ in delta})
    assert len(steps) >= 4, "траектория должна быть неравномерной (ease)"


def test_drag_to_piece_mode_no_slider():
    """Новый формат (без трека): драг САМОГО кусочка, grip внутри силуэта."""
    from unittest.mock import patch
    calls = []

    class _Page:
        def evaluate(self, js):
            return None

        @property
        def mouse(self):
            return self

        def move(self, x, y, steps=None):
            calls.append(("move", x, y, steps))
            return self

        def down(self):
            calls.append(("down",))
            return self

        def up(self):
            calls.append(("up",))
            return self

    page = _Page()
    info = {"bg": {"r": [0, 0, 300, 100]},
            "puzzle": {"r": [10, 20, 40, 40]}}
    reads = {"n": 0}

    def _read(page):
        reads["n"] += 1
        if reads["n"] == 1:
            return {"px": 50.0, "s": None, "ps": 10, "ss": None}
        return {"px": 90.0, "s": None, "ps": 10, "ss": None}

    with patch.object(puzzle, "_find_slider", lambda p: None), \
         patch.object(puzzle, "_read_sl_piece", side_effect=_read):
        with patch.object(puzzle, "_still_challenge", lambda p: True):
            res = puzzle._drag_to(page, target_vx=300.0, info=info, bx=200, score=1.0)
    kinds = [c[0] for c in calls]
    assert "down" in kinds and "up" in kinds
    assert any(k == "move" for k in kinds)


def test_detect_slot_diff_empty_when_frames_identical():
    mask = _HoleMask().nl
    cdn = np.full((200, 400), 128.0, dtype=np.float32)
    assert puzzle._detect_slot_diff(cdn.copy(), cdn, mask, yrow=80) == []


def test_detect_slot_diff_empty_when_shot_mismatched():
    rng = np.random.default_rng(3)
    mask = _HoleMask().nl
    cdn = rng.integers(0, 40, (200, 400)).astype(np.float32)
    live = rng.integers(180, 245, (200, 400)).astype(np.float32)
    # высокий med (разные регионы) → гейт, не мусорный кандидат
    assert puzzle._detect_slot_diff(live, cdn, mask, yrow=80) == []


def test_detect_slot_diff_negative_for_shape_mismatch():
    mask = _HoleMask().nl
    cdn = np.full((200, 400), 128.0, dtype=np.float32)
    live = np.full((180, 380), 128.0, dtype=np.float32)
    assert puzzle._detect_slot_diff(live, cdn, mask, yrow=80) == []