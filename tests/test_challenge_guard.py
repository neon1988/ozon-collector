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
