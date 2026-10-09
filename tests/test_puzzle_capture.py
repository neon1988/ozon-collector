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


# ---------------------------------------------------------------------------
# ОКНО БРАУЗЕРА (живой замер 2026-10-08)
# ---------------------------------------------------------------------------
# Свёрнутое окно Chromium под Windows не отдаёт кадры композитора: page.screenshot
# виснет до таймаута (6с, три раза подряд), captureScreenshot(fromSurface=false)
# отдал 210с и плоскую заливку std=0.0. На normal — 0.05с и std 32.
# Именно это, а не «вкладка зависла», съедало PUZZLE_BUDGET_S=60 восемью
# съёмками по 5с до первого драга (боевой лог 07.10: drops=0, таймаут 60с).

class _FakeCDP:
    def __init__(self, state="minimized", fails=False):
        self.state = state
        self.fails = fails
        self.calls = []

    def send(self, method, params=None):
        self.calls.append(method)
        if self.fails:
            raise RuntimeError("cdp down")
        if method == "Browser.getWindowForTarget":
            # Состояние окна лежит ВНУТРИ bounds — верхнего windowState в
            # ответе нет (проверено живым замером: верхний уровень = null).
            return {"windowId": 7,
                    "bounds": {"windowState": self.state,
                               "width": 1440, "height": 900}}
        if method == "Browser.setWindowBounds":
            self.state = (params.get("bounds") or {}).get("windowState", self.state)
            return {}
        raise AssertionError(f"unexpected CDP call {method}")


class _FakeContext:
    def __init__(self, cdp_obj):
        self._cdp = cdp_obj

    def new_cdp_session(self, _page):
        return self._cdp


class _FakeWinPage:
    """Страница, умеющая только CDP-сессию (окно) и скриншот."""

    def __init__(self, cdp_obj):
        self.context = _FakeContext(cdp_obj)
        self.shots = 0
        self.n_front = 0

    def bring_to_front(self):
        self.n_front += 1

    def screenshot(self, timeout=None, **kw):
        self.shots += 1
        return b"png"


def _win_page(state="minimized", fails=False):
    cdp_obj = _FakeCDP(state=state, fails=fails)
    return _FakeWinPage(cdp_obj), cdp_obj


def test_ensure_render_window_restores_minimized():
    page, cdp_obj = _win_page("minimized")
    with patch.object(puzzle_capture.time, "sleep", lambda _s: None):
        assert puzzle_capture.ensure_render_window(page) is True
    assert cdp_obj.state == "normal"
    assert "Browser.setWindowBounds" in cdp_obj.calls


def test_ensure_render_window_noop_when_normal():
    page, cdp_obj = _win_page("normal")
    assert puzzle_capture.ensure_render_window(page) is False
    assert "Browser.setWindowBounds" not in cdp_obj.calls


def test_ensure_render_window_reads_state_from_bounds():
    """Регрессия: верхнего windowState в ответе нет, состояние в bounds."""
    cdp_obj = _FakeCDP(state="minimized")
    page = _FakeWinPage(cdp_obj)
    with patch.object(puzzle_capture.time, "sleep", lambda _s: None):
        puzzle_capture.ensure_render_window(page)
    assert cdp_obj.state == "normal", "состояние прочитано не из bounds"


def test_ensure_render_window_survives_cdp_error():
    page, _cdp_obj = _win_page("minimized", fails=True)
    with patch.object(puzzle_capture.time, "sleep", lambda _s: None):
        assert puzzle_capture.ensure_render_window(page) is False


def test_ensure_render_window_without_cdp_transport():
    """Страница без context.new_cdp_session (Firefox/старый драйвер) — не исключение."""
    class _NoCtx:
        def screenshot(self, timeout=None, **kw):
            return b"png"

    page = _NoCtx()
    assert puzzle_capture.ensure_render_window(page) is False
    assert puzzle_capture.render_window_state(page) == ""


def test_ensure_render_window_can_be_disabled(monkeypatch):
    monkeypatch.setenv("OZON_PUZZLE_RESTORE_WINDOW", "0")
    page, cdp_obj = _win_page("minimized")
    assert puzzle_capture.ensure_render_window(page) is False
    assert cdp_obj.calls == []


def test_page_shot_restores_window_before_capture():
    """Единая точка съёмки сначала возвращает окно, потом снимает."""
    page, cdp_obj = _win_page("minimized")
    with patch.object(puzzle_capture.time, "sleep", lambda _s: None):
        assert puzzle_capture._page_shot(page, animations="disabled") == b"png"
    assert cdp_obj.state == "normal"
    assert page.shots == 1


def test_bring_to_front_records_window_restore():
    reason = [""]
    page, _cdp_obj = _win_page("minimized")
    with patch.object(puzzle_capture.time, "sleep", lambda _s: None):
        puzzle_capture._bring_to_front(page, reason)
    assert "win=restored" in reason[0]
    assert page.n_front == 1


class TestSolvedSummaryOrder:
    """Строка лога не должна врать о счётчике solved.

    Живой прогон 2026-10-08: на каждом успехе печаталось «пазл снят за 1/3
    попытки; попыток=1 решено=0» — log.info стоял ДО _ok(), а solved
    инкрементит именно _ok. Метрика в отчёте была верной, врала строка лога,
    но её и читают при разборе прогонов.
    """

    def test_summary_line_shows_solved_one(self, monkeypatch):
        from ozon_collector import puzzle as P
        from ozon_collector import puzzle_state as PS
        from tests.test_puzzle_solver_regressions import (_page_with_guard,
                                                          _patch_pipeline)

        _patch_pipeline(monkeypatch, "solved")
        PS.reset()
        lines = []
        monkeypatch.setattr(P.log, "info", lambda m: lines.append(str(m)))

        assert P.solve_puzzle(_page_with_guard(), attempts=1, timeout_s=5.0) is True
        assert PS.solved() == 1, "note_solved() обязан быть вызван"
        itog = [ln for ln in lines if "ИТОГ" in ln]
        assert itog, f"нет строки ИТОГ в логе: {lines}"
        assert "решено=0" not in itog[0], (
            f"строка успеха показывает решено=0: {itog[0]}")
        assert "решено=1" in itog[0], itog[0]
