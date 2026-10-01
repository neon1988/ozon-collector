import json

import numpy as np
import pytest

from ozon_collector import puzzle
from ozon_collector import puzzle_drag


def test_is_puzzle_signatures():
    assert puzzle.is_puzzle("title:Сопоставьте пазл")
    assert puzzle.is_puzzle("двигая ползунок, соберите пазл")
    assert not puzzle.is_puzzle("title:Похоже, нет соединения")
    assert not puzzle.is_puzzle(None)
    assert not puzzle.is_puzzle("")


def test_failures_counter_reset():
    puzzle.reset()
    assert puzzle.failures() == 0
    assert puzzle.attempts() == 0
    puzzle._PUZZLE_FAILURES[0] = 3
    puzzle._note_attempt()
    assert puzzle.failures() == 3
    assert puzzle.attempts() == 1
    puzzle.reset()
    assert puzzle.failures() == 0
    assert puzzle.attempts() == 0


def test_solve_puzzle_no_bg_shot_fails_cleanly(monkeypatch):
    """Нет живого кадра фона → честный провал, без UnboundLocalError.

    Регресс: ветка звала classic-fallback (CDN template-match), тот делал
    слепой драг и возвращал True; после этого код попадал в блок
    `if not cands:` и строил f-string из bg_l/mask/yrow, которые в этой
    ветке не присвоены → UnboundLocalError. Лунка рисуется оверлеем только
    в живом кадре, поэтому без кадра решать нечем.
    """
    stages = []
    info = {"bg": {"src": "http://x/bg.png", "r": [0, 0, 100, 100]},
            "puzzle": {"src": "http://x/p.png", "r": [0, 0, 40, 40]}}
    monkeypatch.setattr(puzzle.time, "sleep", lambda s: None)
    monkeypatch.setattr(puzzle, "_capture_info", lambda page: info)
    monkeypatch.setattr(puzzle, "_captcha_done", lambda page: False)
    monkeypatch.setattr(puzzle, "_still_challenge", lambda page: True)
    monkeypatch.setattr(puzzle, "_bg_from_cdn", lambda info: None)
    monkeypatch.setattr(puzzle, "_shot_bg_clean", lambda page, i: None)
    monkeypatch.setattr(
        puzzle, "_solve_diag",
        lambda page, i, stage, **kw: stages.append(stage))
    puzzle.reset()
    ok = puzzle.solve_puzzle(_FakePage("puzzle"), attempts=1, timeout_s=0.0)
    assert ok is False
    assert "no_bg_shot" in stages          # дошли до нужной ветки
    assert puzzle.failures() == 1


def test_classic_template_match_path_is_gone():
    """Template-match по CDN удалён: для текущего формата он мёртв.

    Проверяем именно наличие символов — если кто-то вернёт classic-фолбэк,
    тест упадёт до запуска anti-bot кода.
    """
    for name in ("_solve_classic", "_find_notch", "_notch_match",
                 "_puzzle_piece_gone", "_log_slider", "_detect_slot"):
        assert not hasattr(puzzle, name), f"мёртвый символ снова в модуле: {name}"


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
    def __init__(self, sig, href=None):
        self.sig = sig
        self.href = href
        self.mouse = self
        self._down = False

    def evaluate(self, _js):
        if "location.href" in str(_js) or "location?.href" in str(_js):
            return self.href or ""
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
    cands, mode = puzzle._detect_slot_diff(live, cdn, mask, yrow=80, k=3)
    assert cands, "diff-детектор должен найти оверлей лунки"
    assert mode in ("strict", "local")
    assert abs(cands[0][0] - hole_x) <= 2


@pytest.mark.parametrize("hole_x", [140, 200, 260])
def test_detect_slot_diff_survives_moderate_brightness_shift(hole_x):
    """Небольшая яркостная разница рендера и CDN не должна глушить детектор."""
    live, cdn, mask = _make_diff_frames(hole_x)
    bright = np.clip(live + 12.0, 0, 255)
    cands, mode = puzzle._detect_slot_diff(bright, cdn, mask, yrow=80, k=3)
    assert cands, "умеренный яркостный сдвиг не должен ронять детектор"
    assert abs(cands[0][0] - hole_x) <= 2


def test_detect_slot_shape_uses_whole_band_not_guess_yrow():
    """Профиль строится по ВСЕМУ фону, поэтому yrow не должен влиять на x.

    Проверяем на реальном кадре корпуса, а не на синтетике: попытка сделать
    синтетический «фото-фон» выглядела правдоподобно, но физику задачи не
    воспроизводила (прямая грань +90 перебивала лунку +9, а в бою такого
    нет). Замер на 15 боевых кадрах: top-1 13/15 = 87% против 20-27% у
    колоночных детекторов.
    """
    import glob
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    corpus = os.path.join(os.path.dirname(here), ".tmp", "corpus")
    hits = hits_wrong_row = tot = 0
    for jp in sorted(glob.glob(os.path.join(corpus, "*.json"))):
        with open(jp, encoding="utf-8") as f:
            rec = json.load(f)
        bx = rec.get("bx_true")
        if bx is None:
            continue
        base = jp[:-5]
        try:
            bg = np.load(base + "_bg.npy").astype(float)
            mask = np.load(base + "_mask.npy")
        except Exception:
            continue
        tot += 1
        yrow = rec.get("yrow") or max(0, bg.shape[0] - mask.shape[0] - 2)
        c = puzzle._detect_slot_shape(bg, mask, yrow, k=1)
        if c and abs(c[0][0] - int(bx)) <= 6:
            hits += 1
        # контроль: с неверной строкой должно быть заметно хуже - именно на
        # этом эффекте (87% против 27%) нашла правка вызова в solve_puzzle
        c0 = puzzle._detect_slot_shape(bg, mask, 0, k=1)
        if c0 and abs(c0[0][0] - int(bx)) <= 6:
            hits_wrong_row += 1
    if tot == 0:
        pytest.skip("корпус не собран")
    assert hits / tot >= 0.8, f"shape-детектор дал {hits}/{tot}, ждали >=80%"
    assert hits > hits_wrong_row, (
        f"yrow должен влиять: с верной строкой {hits}/{tot}, "
        f"с нулевой {hits_wrong_row}/{tot}")


def test_shape_cands_take_precedence_in_solve_cands():
    """Кандидат по форме возглавляет порядок, маржа к нему не применяется."""
    mask = np.zeros((40, 40), dtype=bool)
    mask[8:32, 8:32] = True
    shape = [(200, 1.0)]
    edges = [(215, 9.0)]        # после -silL дало бы 215-10=205
    cands = puzzle._solve_cands(mask, edges, [], [], 10, shape_cands=shape)
    assert cands[0][0] == 200, "shape-кандидат должен быть первым и без маржи"


def test_solve_cands_without_shape_keeps_legacy_order():
    """Без shape-кандидатов поведение прежнее (регресс-страховка)."""
    mask = np.zeros((40, 40), dtype=bool)
    mask[8:32, 8:32] = True
    cands = puzzle._solve_cands(mask, [(215, 9.0)], [], [], 10)
    assert cands and cands[0][0] != 200


def test_detect_slot_diff_reports_gate_reason():
    mask = _HoleMask().nl
    cdn = np.full((200, 400), 128.0, dtype=np.float32)
    rng = np.random.default_rng(3)
    flat = cdn + rng.normal(0, 3, cdn.shape)     # шум: пика нет -> ratio
    cands, mode = puzzle._detect_slot_diff(flat, cdn, mask, yrow=80, k=3)
    assert cands == []
    assert mode in ("med", "ratio", "isolation", "none")
    cands, mode = puzzle._detect_slot_diff(cdn.copy(), cdn, mask, yrow=80, k=3)
    assert cands == []
    assert mode == "med"


def test_detect_slot_diff_still_rejects_pure_noise():
    """Устойчивый базис не должен пропускать мусор без изолированного пика."""
    rng = np.random.default_rng(7)
    cdn = np.full((200, 400), 128.0)
    live = cdn + rng.normal(0, 6, cdn.shape)
    mask = _HoleMask().nl
    cands, mode = puzzle._detect_slot_diff(live, cdn, mask, yrow=80, k=3)
    assert cands == [], "шум без пика обязан отсеяться"
    assert mode in ("ratio", "med", "isolation", "none")


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

    # _drag_to живёт в puzzle_drag: его глобальные имена (_find_slider,
    # _read_sl_piece, _still_challenge) резолвятся в модуле-drag, поэтому
    # патчим именно его, а не фасад puzzle.
    with patch.object(puzzle_drag, "_find_slider", lambda p: None), \
         patch.object(puzzle_drag, "_read_sl_piece", side_effect=_read), \
         patch.object(puzzle_drag, "_still_challenge", lambda p: True):
        res = puzzle._drag_to(page, target_vx=300.0, info=info, bx=200, score=1.0)
    kinds = [c[0] for c in calls]
    assert "down" in kinds and "up" in kinds
    assert any(k == "move" for k in kinds)


def test_detect_slot_diff_empty_when_frames_identical():
    mask = _HoleMask().nl
    cdn = np.full((200, 400), 128.0, dtype=np.float32)
    assert puzzle._detect_slot_diff(cdn.copy(), cdn, mask, yrow=80)[0] == []


def test_detect_slot_diff_empty_when_shot_mismatched():
    rng = np.random.default_rng(3)
    mask = _HoleMask().nl
    cdn = rng.integers(0, 40, (200, 400)).astype(np.float32)
    live = rng.integers(180, 245, (200, 400)).astype(np.float32)
    # высокий med (разные регионы) → гейт, не мусорный кандидат
    assert puzzle._detect_slot_diff(live, cdn, mask, yrow=80)[0] == []


def test_detect_slot_diff_negative_for_shape_mismatch():
    mask = _HoleMask().nl
    cdn = np.full((200, 400), 128.0, dtype=np.float32)
    live = np.full((180, 380), 128.0, dtype=np.float32)
    assert puzzle._detect_slot_diff(live, cdn, mask, yrow=80)[0] == []


def test_captcha_done_true_when_url_has_marker():
    page = _FakePage(sig="puzzle", href="https://ozon.ru/product/123/?&captchaDone=true")
    assert puzzle._captcha_done(page) is True


def test_captcha_done_false_without_marker():
    page = _FakePage(sig="puzzle", href="https://ozon.ru/product/123/")
    assert puzzle._captcha_done(page) is False


def test_captcha_done_false_on_evaluate_error():
    class _ErrPage(_FakePage):
        def evaluate(self, _js):
            raise RuntimeError("cdp gone")
    assert puzzle._captcha_done(_ErrPage("puzzle")) is False


def test_solve_puzzle_counts_attempts(monkeypatch):
    """Каждая попытка solve_puzzle попадает в счётчик (ADR §14.7).

    Инвариант для KPI: attempts == failures + solved.
    """
    monkeypatch.setattr(puzzle.time, "sleep", lambda s: None)
    monkeypatch.setattr(puzzle, "_capture_info", lambda page: None)
    monkeypatch.setattr(puzzle, "_captcha_done", lambda page: False)
    puzzle.reset()
    ok = puzzle.solve_puzzle(_FakePage("puzzle"), attempts=2, timeout_s=0.0)
    assert ok is False
    # пустой капча-стаб обрывает попытку сразу (не крутит attempts=2)
    assert puzzle.attempts() == 1
    assert puzzle.failures() == 1
    puzzle.solve_puzzle(_FakePage("puzzle"), attempts=2, timeout_s=0.0)
    assert puzzle.attempts() == 2
    assert puzzle.failures() == 2


def test_solve_puzzle_empty_capture_emits_on_event(monkeypatch):
    """Капча есть, а пазл не пришёл (capture пустой, captchaDone нет):
    on_event получает kind=empty — хантер пишет «заглушку пустой капчи»."""
    evs = []
    monkeypatch.setattr(puzzle.time, "sleep", lambda s: None)
    monkeypatch.setattr(puzzle, "_capture_info", lambda page: None)
    monkeypatch.setattr(puzzle, "_captcha_done", lambda page: False)
    ok = puzzle.solve_puzzle(_FakePage("puzzle"), attempts=1,
                             timeout_s=0.0, on_event=evs.append)
    assert ok is False
    assert evs and evs[0]["kind"] == "empty"
    assert "seconds_s" in evs[0]


def test_solve_puzzle_src_empty_quick_stub(monkeypatch):
    """Пустая капча (src='') с живым челленджем: on_event kind=empty reason=
    src_empty УЖЕ после ~3s (не 8s буфера) — ранний сигнал «заглушки»."""
    evs = []
    t = [1000.0]

    def _mono():
        t[0] += 1.0
        return t[0]

    monkeypatch.setattr(puzzle.time, "sleep", lambda s: None)
    monkeypatch.setattr(puzzle.time, "monotonic", _mono)
    monkeypatch.setattr(puzzle, "_capture_info",
                        lambda page: {"bg": {"src": ""}, "puzzle": {"src": ""}})
    monkeypatch.setattr(puzzle, "_captcha_done", lambda page: False)
    monkeypatch.setattr(puzzle, "_still_challenge", lambda page: True)
    ok = puzzle.solve_puzzle(_FakePage("puzzle"), attempts=1,
                             timeout_s=0.0, on_event=evs.append)
    assert ok is False
    assert evs and evs[0]["kind"] == "empty"
    assert evs[0].get("reason") == "src_empty"


def test_solve_puzzle_challenge_gone_stub(monkeypatch):
    """Челлендж исчез, а страница — «нет соединения» (stub-маркер в title/теле):
    on_event kind=empty reason=challenge_gone УЖЕ после ~3s, не 8s буфера."""
    evs = []
    t = [1000.0]

    def _mono():
        t[0] += 1.0
        return t[0]

    monkeypatch.setattr(puzzle.time, "sleep", lambda s: None)
    monkeypatch.setattr(puzzle.time, "monotonic", _mono)
    monkeypatch.setattr(puzzle, "_capture_info", lambda page: None)
    monkeypatch.setattr(puzzle, "_captcha_done", lambda page: False)
    monkeypatch.setattr(puzzle, "_still_challenge", lambda page: False)
    monkeypatch.setattr(
        puzzle, "_stub_page",
        lambda page: "нет соединения" in (page.sig or "").lower())
    evs = []
    ok = puzzle.solve_puzzle(_FakePage("Похоже, нет соединения"), attempts=1,
                             timeout_s=0.0, on_event=evs.append)
    assert ok is False
    assert evs and evs[0]["kind"] == "empty"
    assert evs[0].get("reason") == "challenge_gone"


# ── Вердикт снятия капты: URL-маркер ИЛИ снятие челленджа (2026-09-30) ──
#
# Регресс живой находки 2026-09-29: текущее поколение капчи (v10, кусочек
# без трека) после верного дропа снимает челлендж из DOM и перезапрашивает
# выдачу НА МЕСТЕ — навигации с captchaDone=true не происходит. Старый
# вердикт ждал только URL, поэтому 64 дропа, попавшие в лунку (|err|≤1px),
# были засчитаны провалом (успех 3), а кандидат ещё и 24с ждал мёртвый маркер.


def test_classify_accepts_url_marker():
    assert puzzle._classify(True, False, False) == "done"
    assert puzzle._classify(True, True, True) == "done"


def test_classify_accepts_challenge_gone_without_url_marker():
    """Челлендж снят, URL чист, страница не заглушка, контент есть — успех."""
    assert puzzle._classify(False, True, False, True) == "unblocked"


def test_classify_rejects_live_challenge_and_stub():
    assert puzzle._classify(False, False, False, True) == "no"   # челлендж жив
    assert puzzle._classify(False, True, True, True) == "no"     # подмена заглушкой


def test_classify_rejects_challenge_gone_but_no_content():
    """РЕГРЕСС 2026-09-30 (наблюдение с экрана): после верного дропа Ozon
    успевает снять пазл и тут же показать заглушку «Похоже, нет соединения».
    Челлендж исчез, заглушку _stub_page ещё не поймал (её текст мог ещё
    рендериться) — успехом засчитывать НЕЛЬЗЯ. Без проверки контента такие
    случаи давали ложный solved: хантер показывал 100% при реальном ~15%."""
    assert puzzle._classify(False, True, False, False) == "no"


def test_puzzle_verdict_resolves_module_globals(monkeypatch):
    """_verdict читает ГЛОБАЛЫ puzzle — патч тестов обязан влиять на бой."""
    page = _FakePage("puzzle")
    monkeypatch.setattr(puzzle, "_captcha_done", lambda p: False)
    monkeypatch.setattr(puzzle, "_still_challenge", lambda p: True)
    monkeypatch.setattr(puzzle, "_stub_page", lambda p: False)
    monkeypatch.setattr(puzzle, "_page_has_content", lambda p: True)
    assert puzzle._verdict(page) == "no"
    monkeypatch.setattr(puzzle, "_still_challenge", lambda p: False)
    assert puzzle._verdict(page) == "unblocked"
    # контента нет (страница-заглушка) — успех отменяется
    monkeypatch.setattr(puzzle, "_page_has_content", lambda p: False)
    assert puzzle._verdict(page) == "no"


def test_await_verdict_polls_until_challenge_gone(monkeypatch):
    """Вердикт появляется не мгновенно: поллинг дожидается снятия."""
    seq = [True, True, False]      # два поллинга ещё живой, третий — снят
    monkeypatch.setattr(puzzle, "_captcha_done", lambda p: False)
    monkeypatch.setattr(puzzle, "_stub_page", lambda p: False)
    monkeypatch.setattr(puzzle, "_page_has_content", lambda p: True)
    monkeypatch.setattr(puzzle, "_still_challenge", lambda p: seq.pop(0))
    monkeypatch.setattr(puzzle.time, "sleep", lambda s: None)
    assert puzzle._await_verdict(_FakePage("puzzle"), 5.0) == "unblocked"
    assert seq == []


def test_await_verdict_gives_up_on_stub(monkeypatch):
    """Заглушка вместо разблокировки — успехом не считается, ждём до дедлайна."""
    monkeypatch.setattr(puzzle, "_captcha_done", lambda p: False)
    monkeypatch.setattr(puzzle, "_still_challenge", lambda p: False)
    monkeypatch.setattr(puzzle, "_stub_page", lambda p: True)
    monkeypatch.setattr(puzzle, "_page_has_content", lambda p: False)
    monkeypatch.setattr(puzzle.time, "sleep", lambda s: None)
    t = [0.0]
    monkeypatch.setattr(puzzle.time, "monotonic",
                        lambda: (t.__setitem__(0, t[0] + 1.0), t[0])[1])
    assert puzzle._await_verdict(_FakePage("puzzle"), 5.0) == "no"


def test_drag_verdict_accepts_unblocked(monkeypatch):
    """Тот же вердикт в puzzle_drag: дроп без URL-маркера — успех."""
    page = _FakePage("puzzle")
    monkeypatch.setattr(puzzle_drag, "_captcha_done", lambda p: False)
    monkeypatch.setattr(puzzle_drag, "_still_challenge", lambda p: False)
    monkeypatch.setattr(puzzle_drag, "_stub_page", lambda p: False)
    monkeypatch.setattr(puzzle_drag, "_page_has_content", lambda p: True)
    assert puzzle_drag._verdict(page) == "unblocked"
    monkeypatch.setattr(puzzle_drag, "_stub_page", lambda p: True)
    assert puzzle_drag._verdict(page) == "no"
    monkeypatch.setattr(puzzle_drag, "_stub_page", lambda p: False)
    monkeypatch.setattr(puzzle_drag, "_page_has_content", lambda p: False)
    assert puzzle_drag._verdict(page) == "no"


def test_page_has_content_rejects_empty_href():
    """_FakePage отдаёт '' на запрос location.href — контента нет."""
    assert puzzle._page_has_content(_FakePage("puzzle")) is False


class _TextPage:
    """Страница с заданными title и текстом body — для _stub_page."""

    def __init__(self, title="", body=""):
        self._title = title
        self._body = body

    def evaluate(self, js, *a, **kw):
        return f"{self._title}||{self._body}"


def test_stub_page_ignores_words_that_live_captcha_uses():
    """РЕГРЕСС 2026-10-01: guard «сессия сожжена» отказывался двигать кусок.

    На живой капче Ozon в инструкции есть «Попробуйте сопоставить», а в
    разметке — «antibot». Старые маркеры _stub_page ловили это как заглушку,
    solve_puzzle возвращал False за 0.0s, и пазл стоял неподвижно: live6 —
    8 пазлов, 0 дропов, все reason=session_blocked.
    """
    page = _TextPage("Ozon", "Попробуйте сопоставить части картинки. antibot")
    assert puzzle._stub_page(page) is False


def test_stub_page_still_catches_real_stub():
    page = _TextPage("Ozon", "Похоже, нет соединения. Проверьте VPN.")
    assert puzzle._stub_page(page) is True


def test_stub_markers_are_distinctive():
    """В маркерах не должно быть слов, встречающихся на живой капче."""
    joined = " ".join(puzzle.STUB_MARKERS)
    for bad in ("попробуйте", "challenge", "antibot"):
        assert bad not in joined, f"маркер {bad!r} даёт ложные срабатывания"


def test_last_move_reports_zero_when_piece_stuck():
    """«Ползунок не двигается» должно быть видно как измеренный факт."""
    puzzle_drag._reset_last_move()
    assert puzzle_drag._last_move() is None
    puzzle_drag._LAST_MOVE[0] = 0.0
    assert puzzle_drag._last_move() == 0.0
    puzzle_drag._reset_last_move()


# ---- порядок выстрелов (2026-10-01) -----------------------------------------


def test_shot_order_top_first_with_micro_shifts():
    """Топ-кандидат с микросдвигами ±3 — весь порядок выстрелов.

    Остальные кластеры НЕ перебираются: каждый дроп с неверной целью вызывает
    перезагрузку пазла, а после нескольких промахов сессия сгорает в
    заглушку (наблюдение пользователя с экрана 2026-10-01).
    """
    cands = [(300, 9.0), (250, 5.0), (200, 4.0), (150, 3.0)]
    shots = [x for x, _ in puzzle._shot_order(cands)]
    assert shots == [300, 297, 303]
    assert 250 not in shots and 200 not in shots


def test_shot_order_dedups_and_respects_cap():
    cands = [(300, 9.0), (297, 8.0), (250, 5.0), (200, 4.0), (150, 3.0)]
    shots = [x for x, _ in puzzle._shot_order(cands)]
    assert len(shots) == len(set(shots)), "дублей быть не должно"
    assert len(shots) <= puzzle.SHOTS_MAX


def test_shot_order_stays_within_drop_budget():
    """Бюджет сессии должен РЕАЛЬНО ограничивать число дропов.

    Без него циклы дали бы SHOTS_MAX*PUZZLE_MAX_ATTEMPTS дропов на один пазл,
    и сессия сгорала бы в заглушку (наблюдение пользователя 2026-10-01).
    """
    cands = [(300, 9.0), (250, 5.0)]
    per_cycle = len(puzzle._shot_order(cands))
    uncapped = per_cycle * puzzle.PUZZLE_MAX_ATTEMPTS
    assert puzzle.DROP_BUDGET < uncapped, "бюджет обязан быть меньше полного"
    assert puzzle.DROP_BUDGET >= 1


def test_shot_order_keeps_scores_for_logging():
    cands = [(300, 9.0), (250, 5.0)]
    shots = puzzle._shot_order(cands)
    assert shots[0][1] == 9.0
    # все выстрелы несут score топ-кандидата: они его микросдвиги
    assert all(s == 9.0 for _, s in shots)


def test_shot_order_empty_cands():
    assert puzzle._shot_order([]) == []


def test_solve_puzzle_aborts_when_session_already_blocked(monkeypatch):
    """Заглушка в DOM при живом пазле — сессия сожжена, дропов быть не должно.

    Живой замер 2026-10-01: 6 из 8 дропов несли st_h1='Похоже, нет соединения'
    при живом пазле. Драгами это не лечится, только тратит попытки.
    """
    evs = []
    monkeypatch.setattr(puzzle.time, "sleep", lambda s: None)
    monkeypatch.setattr(puzzle, "_stub_page", lambda p: True)
    monkeypatch.setattr(puzzle, "_still_challenge", lambda p: True)
    monkeypatch.setattr(puzzle, "_captcha_done", lambda p: False)
    info = {"bg": {"src": "http://x/b.png", "r": [0, 0, 512, 384]},
            "puzzle": {"src": "http://x/p.png", "r": [0, 0, 141, 128]}}
    monkeypatch.setattr(puzzle, "_capture_info", lambda page: info)
    puzzle.reset()
    ok = puzzle.solve_puzzle(_FakePage("puzzle"), attempts=3,
                             timeout_s=0.0, on_event=evs.append)
    assert ok is False
    assert evs and evs[0]["kind"] == "blocked"
    # ни одного дропа: бюджет попыток не израсходован впустую
    assert puzzle.failures() == 1