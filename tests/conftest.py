"""Фикстуры pytest: живой chromium (headless) + сохранённые DOM-страницы Ozon.

Тесты извлекают данные через тот же PRODUCT_JS, что и боевой collect(),
но на локальном DOM (gzip-дамп), без сети и без CDP. Сеть блокируется
маршрутом, чтобы тесты были быстрыми и герметичными (SSR-разметка остаётся
статичной, гидратация не выполняется).
"""
from __future__ import annotations

import gzip
import os
import sys

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ozon_collector.collect import PRODUCT_JS, REVIEWS_JS, VARIANT_MODE_JS  # noqa: E402

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES = {
    "xiaomi": os.path.join(TESTS_DIR, "product_pages", "xiaomi.html.gz"),
    "roborock": os.path.join(TESTS_DIR, "product_pages", "roborock.html.gz"),
    "neoclima": os.path.join(TESTS_DIR, "product_pages", "neoclima.html.gz"),
    "topper": os.path.join(TESTS_DIR, "product_pages", "topper.html.gz"),
}

REVIEW_FIXTURES = {
    "xiaomi": os.path.join(TESTS_DIR, "review_pages", "xiaomi.html.gz"),
    "xiaomi_variant": os.path.join(TESTS_DIR, "review_pages", "xiaomi_variant.html.gz"),
}


def load_html(name: str) -> str:
    with gzip.open(FIXTURES[name], "rt", encoding="utf-8") as f:
        return f.read()


def load_review_html(name: str) -> str:
    with gzip.open(REVIEW_FIXTURES[name], "rt", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def evaluate(browser):
    """evaluate(name=...) — извлечь данные PRODUCT_JS с фикстуры;
    evaluate(html=...) — из произвольного HTML (негативные тесты)."""

    def _eval(name: str | None = None, html: str | None = None) -> dict:
        if html is None:
            html = load_html(name)
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())
        try:
            page.set_content(html, wait_until="domcontentloaded")
            return page.evaluate(PRODUCT_JS)
        finally:
            page.close()

    return _eval


@pytest.fixture
def evaluate_reviews(browser):
    """evaluate_reviews(name=...) — извлечь данные REVIEWS_JS с review-фикстуры;
    evaluate_reviews(html=...) — из произвольного HTML (негативные тесты)."""

    def _eval(name: str | None = None, html: str | None = None) -> dict:
        if html is None:
            html = load_review_html(name)
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())
        try:
            page.set_content(html, wait_until="domcontentloaded")
            return page.evaluate(REVIEWS_JS)
        finally:
            page.close()

    return _eval


@pytest.fixture
def evaluate_variant_mode(browser):
    """evaluate_variant_mode(name=...) — данные VARIANT_MODE_JS с review-фикстуры;
    evaluate_variant_mode(html=...) — из произвольного HTML (негативные тесты)."""

    def _eval(name: str | None = None, html: str | None = None) -> dict:
        if html is None:
            html = load_review_html(name)
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())
        try:
            page.set_content(html, wait_until="domcontentloaded")
            return page.evaluate(VARIANT_MODE_JS)
        finally:
            page.close()

    return _eval


@pytest.fixture
def variant_mode_page(browser):
    """variant_mode_page(name=...) — открытая страница с фикстурой для вызова
    _activate_variant_mode(page); закрывается в teardown фикстуры."""

    def _open(name: str):
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())
        page.set_content(load_review_html(name), wait_until="domcontentloaded")
        return page

    pages = []

    def _factory(name: str):
        p = _open(name)
        pages.append(p)
        return p

    yield _factory
    for p in pages:
        try:
            p.close()
        except Exception:
            pass
