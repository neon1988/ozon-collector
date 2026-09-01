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