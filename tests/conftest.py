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
def _pin_browser_kind_chrome(monkeypatch):
    """Стабильное окружение движка для тест-свиты.

    Продакшн-дефолт OZON_BROWSER может меняться (сейчас = chrome), а CDP-тесты
    (test_cdp_launch/test_browser/test_search/...) жёстко завязаны на chromium.
    Firefox покрыт отдельно: test_cdp_firefox.py сам ставит OZON_BROWSER=firefox
    (monkeypatch перекрывает), а test_default_chrome снимает env через delenv.
    """
    monkeypatch.setenv("OZON_BROWSER", "chrome")


@pytest.fixture(autouse=True)
def _redirect_launch_logs(tmp_path, monkeypatch):
    """Логи автозапуска браузера — в tmp_path, а не в реальный %TEMP%.

    cdp._launch_browser / browser._launch_script пишут в один фиксированный
    файл на порт (ozon_launch_<port>.log). В тестах Popen почти всегда замотан,
    но путь всё равно создаётся — без override тесты захламляют настоящий
    %TEMP% (и на CI это грязно). Прод остаётся на %TEMP%.
    """
    logs = tmp_path / "launchlogs"
    logs.mkdir(exist_ok=True)
    monkeypatch.setenv("OZON_LAUNCH_LOG_DIR", str(logs))


ALLOW_LAUNCH_MARK = "allow_browser_launch"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{ALLOW_LAUNCH_MARK}: тесту разрешён реальный вызов "
        f"_launch_script (лаунчер/Popen замоканы в самом тесте)")


@pytest.fixture(autouse=True)
def _forbid_real_browser_launch(request, monkeypatch):
    """Запретить тестам поднимать НАСТОЯЩИЙ браузер.

    Живой случай 2026-09-26: `test_collect_search_challenge_event_has_telemetry`
    дошёл до `_recycle_browser` → `BrowserProcessManager.restart()` →
    `launch()` → лаунчер поднял реальный Chrome с профилем `ozon-profile` на
    9222 (PID 5084) прямо рядом с окнами демона. Тест был зелёным, а окно
    осталось жить после pytest — «браузер, который открылся во время тестов и
    не закрылся». Молчаливый хвост опаснее падения: его замечают только
    глазами. Поэтому непокрытый моком вызов лаунчера → fail с рецептом.
    """
    if request.node.get_closest_marker(ALLOW_LAUNCH_MARK):
        return
    from ozon_collector import browser as _B
    from ozon_collector import cdp as _C

    def _deny(*_a, **_kw):
        pytest.fail(
            "тест дёрнул реальный запуск браузера — после pytest останется "
            "окно Chrome на 9222. Замокай _recycle_browser/browser_processes/"
            "Session либо поставь маркер "
            f"{ALLOW_LAUNCH_MARK}, если лаунчер нужен по существу.",
            pytrace=False)

    monkeypatch.setattr(_B, "_launch_script", _deny)
    monkeypatch.setattr(_C, "_launch_browser", _deny)


@pytest.fixture(autouse=True)
def _assume_browser_alive(monkeypatch):
    """По умолчанию CDP воркера считается ЖИВЫМ: тесты не зависят от портов.

    search._cdp_dead() дёргает cdp.browser_alive() (реальный HTTP на
    localhost), чтобы отличить «сбой вкладки» (лечится recycle) от «браузер
    мёртв» (лечится перезапуском). Без этой фикстуры результат зависел бы от
    того, слушает ли кто-нибудь 127.0.0.1:9222 — на машине разработчика с
    живым демоном тесты про recycle проходили, на CI падали бы.

    Тесты про browser_dead переопределяют точечно:
        monkeypatch.setattr("ozon_collector.search.browser_alive",
                            lambda url: False)
    """
    monkeypatch.setattr("ozon_collector.search.browser_alive",
                        lambda url: True, raising=False)


@pytest.fixture(autouse=True)
def _redirect_log_dirs(tmp_path, monkeypatch):
    """Юнит-тесты не должны писать в боевой каталог logs/ проекта.

    Демон (orchestrator) и воркеры parallel используют единый модульный
    логгер ozon_collector/log.py, пишущий в LOGS_DIR/{campaign}_daemon.log
    (путь воркеру передаётся через task["worker_log"]). Перенаправляем всё во
    временный каталог, иначе прямоые тесты finalize_campaign/qa и параллельные
    тесты (FakeSession) захламляют реальный logs/ (как было: None_daemon.log,
    ozon_worker_*.log).
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
