"""Тесты съёмки кадров: однотонный (неотрисованный) кадр ≠ пустой кадр.

Критика #2: валидный PNG от свёрнутого/минимизированного окна декодируется и
проходит проверку размеров, но std(grayscale)≈0 — детекторы читали его как
«лунки нет» и попытка тихо сгорала. Теперь есть диагностика + одна пересъёмка.
"""

import io
from unittest.mock import patch

import numpy as np

from ozon_collector import puzzle_capture


def _png(arr) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def _flat(w=60, h=40, val=255) -> np.ndarray:
    return np.full((h, w), val, dtype=np.uint8)


def _textured(w=60, h=40) -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.integers(0, 256, (h, w), dtype=np.uint8)


def test_frame_painted_rejects_flat():
    ok, why = puzzle_capture._frame_painted(_flat())
    assert ok is False
    assert why.startswith("flat_frame")


def test_frame_painted_accepts_photo():
    ok, why = puzzle_capture._frame_painted(_textured())
    assert ok is True
    assert why.startswith("std=")


def test_frame_painted_rejects_empty():
    assert puzzle_capture._frame_painted(None)[0] is False
    assert puzzle_capture._frame_painted(np.zeros((0, 0)))[0] is False


class _FakePage:
    """Страница с заранее заданными PNG-кадрами по клипу."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.n_shot = 0
        self.n_eval = 0
        self.n_front = 0
        self.front_fails = False

    def evaluate(self, js):
        self.n_eval += 1
        i = self.n_eval
        if i <= 2:
            return 100.0                 # scrollIntoView + чтение y (стабильно)
        if i == 3:
            return [True, 100, "u"]      # eager-src проверка (результат не важен)
        if i == 4:
            return True                  # img.complete && naturalWidth>0
        return None                      # hide/show/scroll

    def bring_to_front(self):
        self.n_front += 1
        if self.front_fails:
            raise RuntimeError("no such target")

    def screenshot(self, clip=None, timeout=None, animations=None):
        self.n_shot += 1
        f = self._frames[min(self.n_shot - 1, len(self._frames) - 1)]
        if f is None:
            raise RuntimeError("screenshot failed")
        return f


def _info():
    return {"bg": {"r": [0, 0, 60, 40]},
            "puzzle": {"r": [10, 10, 20, 20]},
            "has": True}


def _run(page):
    with patch.object(puzzle_capture, "_capture_info", lambda p: _info()), \
         patch.object(puzzle_capture.time, "sleep", lambda _s: None):
        return puzzle_capture._shot_bg_clean(page, _info())


def test_shot_bg_clean_reshoots_flat_frame():
    """Первый кадр однотонный → пересъёмка, кадр принят, причина в диагностике."""
    page = _FakePage([_png(_flat()), _png(_textured())])
    shot = _run(page)
    assert shot is not None
    assert page.n_shot == 2, "нужна ровно одна пересъёмка"
    why = puzzle_capture._shot_bg_reason()
    assert "flat_frame" in why
    assert "reshoot=std=" in why
    assert "ok_sz=(60x40)" in why
    # orig соответствует принятому кадру (не пустому)
    assert shot["orig"] == _png(_textured())


def test_shot_bg_clean_fails_when_reshoot_also_flat():
    """Оба кадра однотонные → честный None (без молчаливых кандидатов)."""
    page = _FakePage([_png(_flat())])
    assert _run(page) is None
    why = puzzle_capture._shot_bg_reason()
    assert "flat_frame" in why
    assert "reshoot=flat_frame" in why


def test_shot_bg_clean_accepts_good_frame_without_reshoot():
    """Нормальный кадр — без лишней пересъёмки (без регрессии по времени)."""
    good = _png(_textured())
    page = _FakePage([good])
    shot = _run(page)
    assert shot is not None
    assert page.n_shot == 1
    assert "flat_frame" not in puzzle_capture._shot_bg_reason()
    assert page.n_front == 0, "окно трогать незачем — кадр валидный"
    assert shot["orig"] == good


def test_flat_frame_reshoot_raises_window_first():
    """Свёрнутое окно отдаёт однотонный буфер: перед reshoot поднимаем окно.

    Иначе пересъёмка повторяет ту же ошибку, тратит лимит попыток и упирается
    в таймаут (критика: minimized + SW_SHOWMINNOACTIVE → ретраи).
    """
    page = _FakePage([_png(_flat()), _png(_textured())])
    shot = _run(page)
    assert shot is not None
    assert page.n_front == 1
    assert "front=ok" in puzzle_capture._shot_bg_reason()


def test_front_failure_does_not_break_reshoot():
    """bring_to_front — best-effort: нет API/ошибка не должны ронять съёмку."""
    page = _FakePage([_png(_flat()), _png(_textured())])
    page.front_fails = True
    shot = _run(page)
    assert shot is not None
    assert page.n_shot == 2
    assert "front_err=RuntimeError" in puzzle_capture._shot_bg_reason()


def test_front_failure_without_api_attribute():
    """Страница без bring_to_front (старый драйвер) — не исключение."""
    class _NoFront(_FakePage):
        def __getattribute__(self, name):
            if name == "bring_to_front":
                raise AttributeError("no such attribute")
            return object.__getattribute__(self, name)

    page = _NoFront([_png(_flat()), _png(_textured())])
    shot = _run(page)
    assert shot is not None
    assert "front_err=AttributeError" in puzzle_capture._shot_bg_reason()
