"""Тесты challenge-guard (анти-бот-заглушки Ozon: FAB, captcha).

Без живого Chromium: fake-страницы с title()/evaluate() эмулируют
либо челлендж-страницу («Похоже, нет соединения», fab_chlg_ и т.п.),
либо обычную карточку (в т.ч. с 0 отзывов — НЕ челлендж).
"""
from __future__ import annotations

import pytest

from ozon_collector import browser as B
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

    def title(self):
        return self._title

    def evaluate(self, js, *args, **kwargs):
        return self._body


class _BrokenEval:
    """title доступен, evaluate падает (например, навигация)."""

    def __init__(self, title=""):
        self._title = title

    def title(self):
        return self._title

    def evaluate(self, *a, **kw):
        raise RuntimeError("Execution context was destroyed")


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
