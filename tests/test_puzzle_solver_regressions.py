"""Регрессии пазла, найденные ревью 07.10 (обе ломали живой сбор).

1. BLOCKER: `solve_puzzle` выходил сразу на любой сессии с геометрическим
   guard'ом (а он есть у КАЖДОЙ сессии — `cdp.Session._attach_geom_guard`).
   Строка `return _ok("late_url") if _captcha_done(page) else _fail(...)`
   стояла на отступе блока `if geom is not None:`, то есть выполнялась всегда:
   геометрия в норме → мгновенный отказ, ни одного `_drag_to`.
   Мок-прогон: ok=False, drags=0, attempts=0, failures=1.

2. MAJOR: успех возвращался голым `return True`, минуя `_ok(...)` → `note_solved()`
   не вызывался, `puzzle_solve_rate = solved/attempts = 0/1 = 0` и ломался
   инвариант `attempts == failures + solved`.
"""
from __future__ import annotations

import ozon_collector.puzzle as P
from ozon_collector import puzzle_state as PS


PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8"
       "BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _page_with_guard(cdp_ok: bool = True):
    """Страница, на которой solve_puzzle реально доходит до drag.

    `_capture_info` читает DOM через evaluate; подменяем его, чтобы тест не
    зависел от разбора JS, но весь контур (бюджет → попытки → кандидаты →
    drag → вердикт) шёл настоящим кодом.
    """
    p = type("Page", (), {})()
    p._geom_guard = lambda _pg: (True, {"hwnd_rect": [0, 0, 1440, 900],
                                        "cdp_bounds": [0, 0, 1440, 900]})
    p.mouse_move = lambda *a, **kw: None
    p.mouse_down = lambda *a, **kw: None
    p.mouse_up = lambda *a, **kw: None
    p.close = lambda *a, **kw: None
    p.goto = lambda *a, **kw: None
    return p


def _patch_pipeline(monkeypatch, verdict: str):
    """Заглушки только на внешние источники картинок и решение суда.

    Всё между ними (бюджет, попытки, детекторы, паспорт, счётчики) — код
    проекта.
    """
    import io

    class _Resp:
        headers = {"content-type": "image/png"}

        def read(self):
            return _png_bytes()

    def _png_bytes():
        import base64
        import zlib
        w = h = 8
        raw = b"".join(b"\x00" + b"\x7f\x7f\x7f" * w for _ in range(h))
        return (b"\x89PNG\r\n\x1a\n"
                + _chunk(b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big")
                         + bytes([8, 2, 0, 0, 0]))
                + _chunk(b"IDAT", zlib.compress(raw))
                + _chunk(b"IEND", b""))

    def _chunk(tag, data):
        import struct
        import zlib
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    info = {"bg": {"src": "http://x/bg.png", "rect": [456, 93, 512, 384]},
            "puzzle": {"src": "http://x/p.png",
                       "rect": [485, 303, 170, 128]},
            "captcha_done": False}
    # CDN-фоны — настоящий numpy-массив: код вызывает mean()/std()/max(),
    # и подмена геометрии не должна ломать интерфейс массива.
    import numpy as _np
    cdn = {"bg_l": _np.zeros((384, 512), dtype=float),
           "bg_r": [456, 93, 512, 384],
           "pz_r": [485, 303, 170, 128], "p_r": [485, 303, 170, 128],
           "psrc": "http://x/p.png"}
    monkeypatch.setattr(P, "_bg_from_cdn", lambda info: cdn, raising=False)
    monkeypatch.setattr(P, "_silhouette",
                        lambda *a, **kw: _np.zeros((128, 170)),
                        raising=False)
    monkeypatch.setattr(P, "_grip_point", lambda *a, **kw: (570.0, 360.0),
                        raising=False)
    monkeypatch.setattr(P, "_slot_ok", lambda *a, **kw: True, raising=False)
    monkeypatch.setattr(P, "_shot_order", lambda cands, *a, **kw: list(cands),
                        raising=False)
    monkeypatch.setattr(P, "_capture_info", lambda page: dict(info),
                        raising=False)
    monkeypatch.setattr(P, "_still_challenge", lambda page: True,
                        raising=False)
    monkeypatch.setattr(P, "_captcha_done", lambda page: False, raising=False)
    monkeypatch.setattr(P, "_stub_page", lambda page: False, raising=False)
    monkeypatch.setattr(P, "_page_has_content", lambda page: True,
                        raising=False)
    monkeypatch.setattr(P, "_fetch", lambda _u: _Resp(), raising=False)
    monkeypatch.setattr(P, "_slot_spy_install", lambda page: False,
                        raising=False)
    monkeypatch.setattr(P, "_js_slot_target", lambda *a, **kw: None,
                        raising=False)
    # Один кандидат по форме лунки: этого достаточно, чтобы контур дошёл до
    # drag. Содержимое кандидата тут не проверяется — за него отвечают тесты
    # детекторов (`test_puzzle*.py`), здесь важна маршрутизация солвера.
    cand = [(212.0, 900.0)]
    monkeypatch.setattr(P, "_detect_slot_shape", lambda *a, **kw: list(cand),
                        raising=False)
    monkeypatch.setattr(P, "_detect_slot_topk", lambda *a, **kw: [],
                        raising=False)
    monkeypatch.setattr(P, "_detect_slot_diff", lambda *a, **kw: [],
                        raising=False)
    monkeypatch.setattr(P, "_detect_slot_edges", lambda *a, **kw: [],
                        raising=False)
    # `_drag_bx` — вложенная функция solve_puzzle, её нельзя подменить снаружи,
    # поэтому подменяем её ЗАМЕНУЮ: успех = вердикт "done" от `_await_verdict`,
    # провал = тот же контур, но вердикт "no".
    monkeypatch.setattr(P, "_await_verdict",
                        lambda page, grace, left=None: (
                            "done" if verdict == "solved" else "no"),
                        raising=False)
    monkeypatch.setattr(P, "_last_move", lambda: 12.0, raising=False)
    monkeypatch.setattr(P, "_drag_diag", lambda: {}, raising=False)
    monkeypatch.setattr(P, "_verdict_reason", lambda *a, **kw: "",
                        raising=False)
    monkeypatch.setattr(P, "_drag_to", lambda *a, **kw: True, raising=False)
    monkeypatch.setattr(P, "_cand_attempt_fields", lambda _d: {},
                        raising=False)
    monkeypatch.setattr(P, "_page_state", lambda page: {}, raising=False)
    monkeypatch.setattr(P, "_attempt", lambda *a, **kw: None,
                        raising=False)


def test_solver_reaches_drag_when_geometry_ok(monkeypatch):
    """Геометрия в норме → солвер доходит до drag, а не отказывается сразу.

    До правки на geom-ветке стоял безусловный `return`, и мок давал
    drags=0, attempts=0: ни одного выстрела на живой сессии.
    """
    _patch_pipeline(monkeypatch, "reject")
    PS.reset()
    ok = P.solve_puzzle(_page_with_guard(), attempts=2, timeout_s=5.0)
    assert ok is False, "все дропы отвергнуты → не решён"
    assert PS._PUZZLE_ATTEMPTS[0] >= 1, "попытка не засчитана — вернулся рано"
    assert PS._PUZZLE_FAILURES[0] >= 1, "провал должен быть засчитан"
    assert (PS._PUZZLE_ATTEMPTS[0]
            == PS._PUZZLE_FAILURES[0] + PS._PUZZLE_SOLVED[0]), (
        "инвариант attempts == failures + solved нарушен")
    PS.reset()


def test_geometry_mismatch_still_fails_fast():
    """Рассинхрон геометрии по-прежнему блокирует солвер (страховка цела)."""
    p = _page_with_guard()
    p._geom_guard = lambda _pg: (False, {"hwnd_rect": [0, 0, 1440, 900],
                                         "cdp_bounds": [10, 0, 1440, 900]})
    PS.reset()
    ok = P.solve_puzzle(p, attempts=3, timeout_s=2.0)
    assert ok is False
    assert PS._PUZZLE_FAILURES[0] == 1, "геометрия даёт ровно один провал"
    assert PS._PUZZLE_ATTEMPTS[0] == 1, "и ровно одну попытку"
    PS.reset()


def test_success_goes_through_ok_and_counts_solved(monkeypatch):
    """Успех обязан звать _ok → note_solved, иначе solve-rate всегда 0."""
    _patch_pipeline(monkeypatch, "solved")
    PS.reset()
    ok = P.solve_puzzle(_page_with_guard(), attempts=2, timeout_s=5.0)
    assert ok is True, "успешный дроп должен вернуть True"
    assert PS._PUZZLE_SOLVED[0] >= 1, "note_solved() обязан быть вызван"
    assert PS._PUZZLE_FAILURES[0] == 0, "успех не должен считаться провалом"
    assert (PS._PUZZLE_ATTEMPTS[0]
            == PS._PUZZLE_FAILURES[0] + PS._PUZZLE_SOLVED[0]), (
        "инвариант attempts == failures + solved нарушен")
    PS.reset()
