"""Тесты challenge-guard (анти-бот-заглушки Ozon: FAB, captcha).

Без живого Chromium: fake-страницы с title()/evaluate() эмулируют
либо челлендж-страницу («Похоже, нет соединения», fab_chlg_ и т.п.),
либо обычную карточку (в т.ч. с 0 отзывов — НЕ челлендж).
"""
from __future__ import annotations

import pytest

from ozon_collector import browser as B
from ozon_collector import collect as collect_mod
from ozon_collector.collect import (
    collect_product,
    check_challenge,
    detect_challenge,
)


class _Page:
    """Fake CDP-страница: title + body-текст для evaluate."""

    def __init__(self, title="", body=""):
        self._title = title
        self._body = body

    def title(self, *a, **kw):
        return self._title

    def evaluate(self, js, *args, **kwargs):
        return self._body


class _BrokenEval:
    """title доступен, evaluate падает (например, навигация)."""

    def __init__(self, title=""):
        self._title = title

    def title(self, *a, **kw):
        return self._title

    def evaluate(self, *a, **kw):
        raise RuntimeError("Execution context was destroyed")


class _ThinPage(_Page):
    """Страница без маркеров капчи, но не отрисовавшаяся.

    Реальный JS `_CHALLENGE_LOOKS_JS` возвращает булево «похоже на челлендж»
    (нет h1, текста <300 символов), тогда как `detect_challenge` просит
    innerText и получает пустую строку — то есть маркеров капчи нет.
    """

    def __init__(self, title="Ozon", body=""):
        super().__init__(title=title, body=body)

    def evaluate(self, js, *args, **kwargs):
        if "location.href" in js:      # _CHALLENGE_LOOKS_JS → boolean
            return True
        return self._body


def _product_page(product):
    class _Eval:
        def evaluate(self, js, *a, **kw):
            return product

    return _Eval()


# ---- detect_challenge -------------------------------------------------------

def test_detect_challenge_fab_title():
    page = _Page(title="Похоже, нет соединения")
    assert detect_challenge(page) == "title:похоже, нет соединения"


def test_detect_challenge_access_denied_title():
    page = _Page(title="Доступ ограничен")
    assert detect_challenge(page) == "title:доступ ограничен"


def test_detect_challenge_dom_marker():
    page = _Page(title="Сайт Ozon",
                 body="Произошла ошибка fab_chlg_20260815 ...")
    assert detect_challenge(page) == "dom:fab_chlg_"


def test_detect_challenge_normal_page_none():
    page = _Page(title="Робот-пылесос Xiaomi X",
                 body="Отзывы покупателей...")
    assert detect_challenge(page) is None


def test_detect_challenge_zero_reviews_is_not_challenge():
    """0 отзывов на обычной карточке НЕ должно считаться челленджем."""
    page = _Page(title="Товар без отзывов", body="Нет отзывов")
    assert detect_challenge(page) is None


def test_detect_challenge_evaluate_failure_none():
    assert detect_challenge(_BrokenEval("обычный заголовок")) is None


# ---- check_challenge --------------------------------------------------------

def test_check_challenge_raises_access_challenge():
    page = _Page(title="Похоже, нет соединения")
    with pytest.raises(B.AccessChallengeError, match="похоже, нет соединения"):
        check_challenge(page, "product_page")


def test_check_challenge_error_carries_puzzle_summary_and_no_ip_claim():
    """Требование пользователя (2026-10-02): в отказе НЕ должно быть лжи.

    Заглушка Ozon — это отказ СЕССИИ, а не бан по адресу: живой замер показал,
    что в 16:03 тот же адрес отдал NORMAL_DONE (discovered 336, valid 10), а в
    17:22 403 получили все 8 воркеров разом, при том что Ozon из другого
    браузера открывался нормально. Ложный «бан IP» стоил нам 21 строки в логе за
    прогон и выбрасывания товаров без попытки смены сессии.

    Поэтому сообщение об отказе обязано: называть наблюдаемый факт, указывать
    лечение (смена сессии) и НЕ утверждать бан по IP.
    """
    import ozon_collector.puzzle as puzzle_mod
    page = _Page(title="Похоже, нет соединения")
    with pytest.raises(B.AccessChallengeError) as ei:
        check_challenge(page, "product_page")
    msg = str(ei.value)
    assert "не бан по IP" in msg, msg
    assert "new_tab" in msg, "сообщение должно называть лечение (смену сессии)"
    assert "пазлов:" in msg, f"в отказе нет сводки по пазлам: {msg}"


def test_puzzle_timeout_skips_stub_wait(monkeypatch):
    """Зависшая вкладка (бюджет пазла исчерпан) НЕ ждёт заглушку.

    Живой замер 2026-10-02: провал шёл 178 с, потом код ждал бы ещё 12 с
    (_await_challenge_sig) и делал до трёх FAB-reload в ТОЙ ЖЕ зависшей вкладке,
    а новая вкладка решала ту же капчу за 10 с. При исчерпанном бюджете
    ожидание бессмысленно — товар надо отдать в лестницу (new_tab → recycle).
    """
    import ozon_collector.collect as C
    import ozon_collector.puzzle as P
    from ozon_collector import browser as B

    waited = []
    reloads = []
    # Первый вызов _challenge_sig открывает ветку пазла, дальше пауз нет
    sig_seq = iter(["puzzle", None, None, None, None, None, None, None])

    monkeypatch.setattr(P, "is_puzzle", lambda sig: True)
    monkeypatch.setattr(P, "looks_like_puzzle", lambda page: True)
    monkeypatch.setattr(P, "solve_puzzle", lambda page, **kw: False)
    monkeypatch.setattr(P, "note_storm", lambda: None)
    monkeypatch.setattr(P, "last_was_timeout", lambda: True)
    monkeypatch.setattr(C, "_puzzle", P)
    monkeypatch.setattr(C, "_await_challenge_sig",
                        lambda page, seconds=None: waited.append(1) or None)
    monkeypatch.setattr(C, "_reload_fn",
                        lambda page: reloads.append(1), raising=False)
    monkeypatch.setattr(C, "_challenge_sig",
                        lambda page: next(sig_seq, None))

    page = _Page(title="Antibot")
    with pytest.raises(B.AccessChallengeError) as ei:
        C.check_challenge(page, "product_page")
    assert not waited, "зависшая вкладка не должна ждать заглушку"
    assert not reloads, "зависшая вкладка не должна делать FAB-reload"
    assert "бюджет" in str(ei.value), str(ei.value)
    assert "не бан по IP" in str(ei.value), str(ei.value)


def test_puzzle_miss_still_waits_stub(monkeypatch):
    """Обычный промах (бюджет НЕ исчерпан) по-прежнему ждёт заглушку.

    Разделять эти случаи обязательно: при промахе капча жива и FAB-reload её
    снимает, а отказ сюрать на лестницу зрядно потратил бы её бюджет.
    """
    import ozon_collector.collect as C
    import ozon_collector.puzzle as P
    from ozon_collector import browser as B

    waited = []
    sig_seq = iter(["puzzle", None, None, None, None, None, None, None])

    monkeypatch.setattr(P, "is_puzzle", lambda sig: True)
    monkeypatch.setattr(P, "looks_like_puzzle", lambda page: True)
    monkeypatch.setattr(P, "solve_puzzle", lambda page, **kw: False)
    monkeypatch.setattr(P, "note_storm", lambda: None)
    monkeypatch.setattr(P, "last_was_timeout", lambda: False)
    monkeypatch.setattr(C, "_puzzle", P)
    monkeypatch.setattr(C, "_await_challenge_sig",
                        lambda page, seconds=None: waited.append(1) or None)
    monkeypatch.setattr(C, "_challenge_sig",
                        lambda page: next(sig_seq, None))
    monkeypatch.setattr(C, "_looks_like_challenge", lambda page: True)

    page = _Page(title="Antibot")
    with pytest.raises(B.AccessChallengeError):
        C.check_challenge(page, "product_page")
    assert waited, "при промахе заглушку ждать обязаны"


def test_check_challenge_marks_page_invalid():
    """ADR §14.1: после детекта challenge страница помечается INVALID (K4)."""
    page = _Page(title="Похоже, нет соединения")
    try:
        check_challenge(page, "product_page")
    except B.AccessChallengeError:
        pass
    assert page.is_invalid is True


def test_check_challenge_passes_on_normal_page():
    check_challenge(_Page(title="Робот-пылесос Xiaomi X"), "product_page")


def test_check_challenge_raises_on_unrendered_page(monkeypatch):
    """Страница без маркеров капчи, но без h1/контента — тоже отказ.

    Замер 2026-09-25: после холодного старта профиля Ozon отдал пустой
    документ (widgets=0, h=805). Раньше такой случай проходил молча, и
    collect писал пустой raw, тратя слот кампании.
    """
    monkeypatch.setattr(collect_mod, "CHALLENGE_RECHECKS", 0)
    page = _ThinPage()
    with pytest.raises(B.AccessChallengeError, match="не отрисована"):
        check_challenge(page, "product_page")


def test_check_challenge_unrendered_marks_page_invalid(monkeypatch):
    """Неразрендеренная страница блокируется для evaluate (ADR §14.1)."""
    monkeypatch.setattr(collect_mod, "CHALLENGE_RECHECKS", 0)
    page = _ThinPage()
    with pytest.raises(B.AccessChallengeError):
        check_challenge(page, "product_page")
    assert page.is_invalid is True


def test_check_challenge_rechecks_then_raises_when_stays_thin(monkeypatch):
    """Переспросы не помогли — отказ после них, а не пустая карточка."""
    sleeps = []
    monkeypatch.setattr(collect_mod, "CHALLENGE_RECHECKS", 3)
    monkeypatch.setattr(collect_mod.time, "sleep", lambda s: sleeps.append(s))
    page = _ThinPage()
    with pytest.raises(B.AccessChallengeError, match="не отрисована"):
        check_challenge(page, "product_page")
    assert len(sleeps) == 3


# ---- поздняя заглушка после пазла -------------------------------------------


class _LateStubPage(_Page):
    """Пазл снят, страница тонкая, заглушка вылезает позже.

    Реализует наблюдение с экрана 2026-09-30: кусок встал в лунку, пазл
    исчез, страница осталась тонкой (нет h1/текста), и только спустя
    `delay` опросов проявилась заглушка «нет соединения». До этого маркеров
    в title/body нет, поэтому сигнатура пустая.
    """

    def __init__(self, delay=3, title="Похоже, нет соединения"):
        super().__init__(title="Ozon", body="")
        self._delay = delay
        self._n = 0
        self._stub_title = title
        self.reloads = 0

    def evaluate(self, js, *a, **kw):
        if "location.href" in js:      # _CHALLENGE_LOOKS_JS → boolean
            return True                # страница ТОНКАЯ (контента нет)
        return self._body

    def _maybe_stub(self):
        self._n += 1
        if self._n > self._delay:
            self._title = self._stub_title
            self._body = "Похоже, нет соединения. Проверьте VPN."

    def title(self, *a, **kw):
        self._maybe_stub()
        return self._title

    def reload(self, *a, **kw):
        self.reloads += 1
        self._n = -10 ** 6      # после reload страница чистая
        self._title = "Ozon"
        self._body = ""


# Оригинальная сигнатура — до monkeypatch, иначе патчем сами себя.
_REAL_SIG = collect_mod._challenge_sig


def test_await_challenge_sig_waits_for_late_stub(monkeypatch):
    """Заглушка, вылезшая поздно, ДОЛЖНА находиться ожиданием.

    Регресс 2026-09-30: разовый _challenge_sig после solve_puzzle давал None,
    ветка FAB-reload была зацеплена за `if sig` → range(0) → страница висела
    с заглушкой и не перезагружалась (наблюдение с экрана пользователя).
    """
    monkeypatch.setattr(collect_mod.time, "sleep", lambda s: None)
    page = _LateStubPage(delay=3)
    t = [0.0]
    monkeypatch.setattr(collect_mod.time, "monotonic",
                        lambda: (t.__setitem__(0, t[0] + 1.0), t[0])[1])
    sig = collect_mod._await_challenge_sig(page, 12.0)
    assert sig, "поздняя заглушка обязана быть обнаружена"


class _ProductishPage(_Page):
    """Нормальная карточка: h1 есть, текста много, маркеров капчи нет."""

    def __init__(self):
        super().__init__(title="Робот-пылесос Xiaomi X - купить на OZON",
                         body="x" * 400)

    def evaluate(self, js, *a, **kw):
        if "location.href" in js:      # _CHALLENGE_LOOKS_JS
            return False               # страница НЕ тонкая
        return self._body


def test_await_challenge_sig_skips_wait_when_page_has_content(monkeypatch):
    """Есть h1 и текст — успех, polling не тратит паузу."""
    sleeps = []
    monkeypatch.setattr(collect_mod.time, "sleep", lambda s: sleeps.append(s))
    page = _ProductishPage()
    assert collect_mod._await_challenge_sig(page, 12.0) is None
    assert sleeps == []


def test_check_challenge_reloads_after_solve_when_late_stub(monkeypatch):
    """Полный путь: пазл «решён» → поздно вылезла заглушка → reload.

    Раньше check_challenge после успешного solve_puzzle делал `return`, не
    проверяя страницу, а FAB-reload не срабатывал из-за пустой сигнатуры.
    """
    monkeypatch.setattr(collect_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(collect_mod, "FAB_RELOAD_ATTEMPTS", 1)
    monkeypatch.setattr(collect_mod, "fab_reload_delay_s", lambda: 0.0)
    t = [0.0]
    monkeypatch.setattr(collect_mod.time, "monotonic",
                        lambda: (t.__setitem__(0, t[0] + 1.0), t[0])[1])
    monkeypatch.setattr(collect_mod, "_challenge_sig", _REAL_SIG)
    monkeypatch.setattr(collect_mod._puzzle, "solve_puzzle", lambda page: True)
    monkeypatch.setattr(collect_mod._puzzle, "is_puzzle", lambda s: True)
    monkeypatch.setattr(collect_mod._puzzle, "looks_like_puzzle",
                        lambda page: True)
    page = _LateStubPage(delay=2)
    with pytest.raises(B.AccessChallengeError):
        check_challenge(page, "product_page")
    assert page.reloads >= 1, "после поздней заглушки страница обязана перезагрузиться"


# ---- collect_product --------------------------------------------------------

def test_collect_product_challenge_title_raises():
    product = {"title": "Похоже, нет соединения", "rating": None}
    with pytest.raises(B.AccessChallengeError):
        collect_product(_product_page(product), "https://www.ozon.ru/product/12345")


def test_collect_product_normal_title_ok():
    product = {"title": "Робот-пылесос Xiaomi X", "rating": 4.8}
    out = collect_product(_product_page(product),
                          "https://www.ozon.ru/product/12345", category="Техника")
    assert out["title"] == "Робот-пылесос Xiaomi X"
    assert out["category"] == "Техника"


def test_collect_product_none_raises_runtime():
    with pytest.raises(RuntimeError):
        collect_product(_product_page(None), "https://www.ozon.ru/product/12345")
