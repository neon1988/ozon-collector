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

# Windows: headless Chromium в teardown иногда шлёт в консольную группу ложное
# console-control событие (CTRL_C/CTRL_BREAK), из-за чего драйвер Playwright
# (node) падает и весь прогон рассыпается. Штатный способ изолировать процесс —
# перевести его в режим «игнорировать Ctrl+C»: SetConsoleCtrlHandler(NULL, TRUE).
# Экспериментально подтверждено: подходит только NULL-режим; ручные хендлеры
# (даже с return True) драйвер НЕ защищают.
if sys.platform == "win32":
    import ctypes

    try:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)
    except Exception:
        pass

from ozon_collector.collect import PRODUCT_JS, REVIEWS_JS, VARIANT_MODE_JS  # noqa: E402

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture(autouse=True)
def _redirect_log_dirs(tmp_path, monkeypatch):
    """Юнит-тесты не должны писать в боевой каталог logs/ проекта.

    key_daemon.log() пишет в LOGS_DIR/{campaign}_daemon.log, а воркеры parallel
    используют WORKER_LOG_DIR → всё это перенаправляем во временный каталог.
    Иначе прямоые тесты finalize_campaign/qa и параллельные тесты (FakeSession)
    захламляют реальный logs/ (как было: None_daemon.log, ozon_worker_*.log).
    """
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    import scripts.key_daemon as k
    monkeypatch.setattr(k, "LOGS_DIR", logs)
    from ozon_collector import parallel as parallel_mod
    monkeypatch.setattr(parallel_mod, "WORKER_LOG_DIR", str(logs))



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
    """Общий headless-Chromium на всю сессию. Никакого авто-перезапуска:
    restart in-process ненадёжен (у Playwright один глобальный event loop) и
    раньше вешал teardown фикстуры навсегда. Если драйвер умирает посередине —
    последующие evaluate-тесты быстро падают с "Connection closed", а не виснут.
    Закрытие только best-effort: если close()/stop() зависнут на мёртвом
    коннекте, их снимет внешний watchdog (scripts/run_tests_guarded.py)."""
    pw = sync_playwright().start()
    b = None
    try:
        b = pw.chromium.launch(headless=True)
        yield b
    finally:
        if b is not None:
            try:
                b.close()
            except BaseException:
                pass
        try:
            pw.stop()
        except BaseException:
            pass


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
