"""Тесты поиска: SEARCH_JS на фикстурах, лимиты, partial failure,
collect_search (одна сессия, ошибки), render_search_digest."""
from __future__ import annotations

import gzip
import os

from ozon_collector.search import SEARCH_JS, search_url, collect_search
from ozon_collector import report as R

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    with gzip.open(os.path.join(TESTS_DIR, "search_pages", f"{name}.html.gz"),
                   "rt", encoding="utf-8") as f:
        return f.read()


# ---- SEARCH_JS -------------------------------------------------------------

def test_search_js_extracts_and_dedups(browser):
    page = browser.new_page()
    page.route("**/*", lambda route: route.abort())
    try:
        page.set_content(_load("search"), wait_until="domcontentloaded")
        links = page.evaluate(SEARCH_JS)
    finally:
        page.close()
    pids = [l["pid"] for l in links]
    assert len(pids) >= 20
    assert len(set(pids)) == len(pids)  # дедуп
    for l in links:
        assert l["pid"].isdigit()
        assert l["pid"].startswith(l["url"].rsplit("/", 1)[1])


def test_search_js_works_on_category(browser):
    page = browser.new_page()
    page.route("**/*", lambda route: route.abort())
    try:
        page.set_content(_load("category"), wait_until="domcontentloaded")
        links = page.evaluate(SEARCH_JS)
    finally:
        page.close()
    assert len(links) >= 20


def test_search_js_ignores_non_product_links(browser):
    html = ('<div data-widget="tileGridDesktop">'
            '<a href="https://www.ozon.ru/brand/some-12345">бренд</a>'
            '<a href="https://www.ozon.ru/product/123">короткий pid</a>'
            '<a href="https://other.ru/product/99999999">чужой домен</a>'
            '</div>')
    page = browser.new_page()
    page.route("**/*", lambda route: route.abort())
    try:
        page.set_content(html, wait_until="domcontentloaded")
        links = page.evaluate(SEARCH_JS)
    finally:
        page.close()
    assert links == []


def test_search_js_ignores_recommendation_blocks(browser):
    """Вне tileGridDesktop (skuGrid/island — рекомендации) ссылки не берём."""
    html = ('<div data-widget="tileGridDesktop">'
            '<a href="/product/tovar-11111111">товар 1</a>'
            '</div>'
            '<div data-widget="skuGrid">'
            '<a href="/product/recom-22222222">возможно понравится</a>'
            '</div>'
            '<div data-widget="island">'
            '<a href="/product/viewed-33333333">вы смотрели</a>'
            '</div>')
    page = browser.new_page()
    page.route("**/*", lambda route: route.abort())
    try:
        page.set_content(html, wait_until="domcontentloaded")
        links = page.evaluate(SEARCH_JS)
    finally:
        page.close()
    assert [l["pid"] for l in links] == ["11111111"]


# ---- search_url ------------------------------------------------------------

def test_search_url():
    assert "text=%D0%BF%D0%B0%D0%BB%D0%B0%D1%82%D0%BA%D0%B0" in search_url("палатка")
    assert "from_global=true" in search_url("палатка")


# ---- collect_search --------------------------------------------------------

class _FakeSess:
    """Плоская выдача: eval всегда возвращает один набор ссылок."""
    def __init__(self, links):
        self._links = links
        self.gotos = 0
        self.scrolls = 0
        self.called_pids = []

    def goto(self, url, wait_ms=None):
        self.gotos += 1

    def eval(self, js):
        return self._links

    def scroll(self, by=600):
        self.scrolls += 1

    def wait(self, ms=1000):
        pass


class _GrowingSess:
    """Выдача растёт при скролле: steps[0] — нач. DOM, steps[k] — после k скроллов."""
    def __init__(self, steps):
        self._steps = steps
        self.gotos = 0
        self.scrolls = 0

    def goto(self, url, wait_ms=None):
        self.gotos += 1

    def eval(self, js):
        return self._steps[min(self.scrolls, len(self._steps) - 1)]

    def scroll(self, by=600):
        self.scrolls += 1

    def wait(self, ms=1000):
        pass


def _ok_result(pid):
    return {
        "pid": pid, "ok": True, "error": None,
        "raw": {"pid": pid, "reviews_collected": 3,
                "product": {"title": f"Товар {pid}"}},
        "markdown": f"# Товар {pid}\n", "cache": "hit", "ocr": "off",
    }


def test_collect_search_limit(monkeypatch):
    links = [{"pid": f"{i}", "url": f"https://www.ozon.ru/product/{i}"}
             for i in range(1, 11)]
    sess = _FakeSess(links)

    def fake_run_gather(cfg, sess=None):
        sess.called_pids.append(cfg["pid"])
        return _ok_result(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=x",
                       limit=5, gather_cfg={}, query="x")
    assert c["found"] == 10
    assert c["limit"] == 5
    assert c["collected"] == 5
    assert len(c["products"]) == 5
    assert sess.called_pids == [str(i) for i in range(1, 6)]


def test_collect_search_found_less_than_limit(monkeypatch):
    links = [{"pid": str(i), "url": f"https://www.ozon.ru/product/{i}"}
             for i in range(1, 4)]
    sess = _FakeSess(links)
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    c = collect_search(sess, "u", limit=5, gather_cfg={})
    assert c["found"] == 3
    assert c["collected"] == 3
    assert len(c["products"]) == 3


def test_collect_search_partial_failure(monkeypatch):
    links = [{"pid": str(i), "url": f"https://www.ozon.ru/product/{i}"}
             for i in range(1, 4)]
    sess = _FakeSess(links)

    def fake(cfg, sess=None):
        if cfg["pid"] == "2":
            return {"pid": "2", "ok": False, "error": "RuntimeError: boom",
                    "raw": None, "markdown": "", "cache": "miss", "ocr": "off"}
        return _ok_result(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.run_gather", fake)
    c = collect_search(sess, "u", limit=5, gather_cfg={})
    assert c["collected"] == 2
    failed = [p for p in c["products"] if not p["ok"]]
    assert len(failed) == 1
    assert failed[0]["pid"] == "2"
    assert "boom" in failed[0]["error"]


def test_collect_search_one_session():
    sess = _FakeSess([{"pid": "1", "url": "u1"}, {"pid": "2", "url": "u2"}])
    # без monkeypatch: используем заглушку run_gather через monkeypatch ниже
    from unittest import mock
    with mock.patch("ozon_collector.search.run_gather",
                    side_effect=lambda cfg, sess=None: _ok_result(cfg["pid"])):
        collect_search(sess, "u", limit=5, gather_cfg={})
    assert sess.gotos == 1  # ровно один goto (одна CDP-сессия)


def test_collect_search_progress_output(monkeypatch, capsys):
    links = [{"pid": str(i), "url": f"https://www.ozon.ru/product/{i}"}
             for i in range(1, 4)]
    sess = _FakeSess(links)
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    collect_search(sess, "u", limit=5, gather_cfg={})
    out = capsys.readouterr().out
    assert "найдено 3 товаров в выдаче, собираю 3" in out
    assert "товар 1/3: pid=1" in out
    assert "товар 3/3: ok" in out


# ---- recovery (R1 recycle / R2 retry) --------------------------------------

def _recyclable_fail(pid, error="net::ERR_ABORTED: The frame was detached"):
    return {
        "pid": pid, "ok": False, "error": f"Error: {error}",
        "raw": None, "markdown": "", "cache": "miss", "ocr": "off",
        "recyclable": True, "error_kind": "renderer_failure",
        "error_probe_state": "renderer_failure", "error_signature": True,
    }


def test_collect_search_recycle_and_retry(monkeypatch):
    """R1+R2: recyclable-ошибка → recycle браузера → повторный сбор успешен."""
    links = [{"pid": "1", "url": "u1"}, {"pid": "2", "url": "u2"}]
    sess = _FakeSess(links)
    recycled = []
    calls = {}

    def fake_run_gather(cfg, sess=None):
        if cfg["pid"] == "1" and "retried" not in calls:
            calls["retried"] = True
            return _recyclable_fail(cfg["pid"])
        return _ok_result(cfg["pid"])

    def fake_recycle(sess, manager, launch_id):
        recycled.append(launch_id)

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser", fake_recycle)
    c = collect_search(sess, "u", limit=5, gather_cfg={})
    assert len(recycled) == 1             # ровно один recycle
    assert recycled[0] == "search-1-r1"
    assert c["recovery"]["recycles"] == 1
    assert c["recovery"]["events"][0]["kind"] == "renderer_failure"
    ok = [p for p in c["products"] if p["ok"]]
    assert len(ok) == 2
    assert ok[0]["recycle_attempts"] == 2
    assert ok[1]["recycle_attempts"] == 1


def test_collect_search_no_recycle_for_non_recyclable(monkeypatch):
    """Не-recyclable ошибка (target/unknown) → без recycle, сразу fail."""
    links = [{"pid": "1", "url": "u1"}]
    sess = _FakeSess(links)
    recycled = []

    def fake_run_gather(cfg, sess=None):
        res = _recyclable_fail(cfg["pid"])
        res["recyclable"] = False
        res["error_kind"] = "target"
        return res

    def fake_recycle(sess, manager, launch_id):
        recycled.append(launch_id)

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser", fake_recycle)
    c = collect_search(sess, "u", limit=5, gather_cfg={})
    assert recycled == []
    assert c["recovery"]["recycles"] == 0
    assert c["products"][0]["ok"] is False
    assert c["products"][0]["recycle_attempts"] == 1


def test_collect_search_recycle_until_success_or_limit(monkeypatch):
    """Лимит max_recycles: после N рециклов ошибка остаётся как есть."""
    links = [{"pid": "1", "url": "u1"}]
    sess = _FakeSess(links)
    recycled = []

    def fake_run_gather(cfg, sess=None):
        return _recyclable_fail(cfg["pid"])

    def fake_recycle(sess, manager, launch_id):
        recycled.append(launch_id)

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser", fake_recycle)
    c = collect_search(sess, "u", limit=5, gather_cfg={},
                       max_recycles=2)
    assert len(recycled) == 2              # 2 попытки рецикла
    assert c["recovery"]["recycles"] == 2
    assert c["products"][0]["ok"] is False
    assert c["products"][0]["recycle_attempts"] == 3


def test_collect_search_recycle_failure_marks_product(monkeypatch):
    """recycle сам упал → ошибка товара дополняется recycle_failed."""
    links = [{"pid": "1", "url": "u1"}]
    sess = _FakeSess(links)

    def fake_run_gather(cfg, sess=None):
        return _recyclable_fail(cfg["pid"])

    def fake_recycle(sess, manager, launch_id):
        raise RuntimeError("BrowserOwnershipError: чужой Chromium")

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser", fake_recycle)
    c = collect_search(sess, "u", limit=5, gather_cfg={})
    assert c["recovery"]["recycles"] == 0
    p = c["products"][0]
    assert p["ok"] is False
    assert "recycle_failed" in p["error"]
    assert p["recyclable"] is False


def test_collect_search_preventive_recycle(monkeypatch):
    """Профилактический recycle каждые RECYCLE_EVERY товаров (OOM-превенция):
    браузер перезапускается принудительно, чтобы renderer не дорастал до OOM."""
    links = [{"pid": str(i), "url": f"u{i}"} for i in range(1, 6)]
    sess = _FakeSess(links)
    recycled = []
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id:
                        recycled.append(launch_id))
    monkeypatch.setattr("ozon_collector.search.RECYCLE_EVERY", 2)
    c = collect_search(sess, "u", limit=5, gather_cfg={})
    preventive = [r for r in recycled if "preventive" in r]
    assert len(preventive) == 2                 # после 2-го и 4-го товара
    assert c["recovery"]["recycles"] == 2
    kinds = [e["kind"] for e in c["recovery"]["events"]]
    assert kinds == ["preventive", "preventive"]
    assert c["collected"] == 5


class _HangPage:
    """Зависший renderer: evaluate не возвращается (TimeoutError)."""

    def evaluate(self, *args, **kwargs):
        from playwright.sync_api import TimeoutError
        raise TimeoutError("Timeout 4000ms exceeded")


def test_collect_search_links_recovery(monkeypatch):
    """Докрутка выдачи: TimeoutError (завис renderer) → recycle → повтор успешен.

    Зависание при сборе ссылок выдачи не должно ронять весь search traceback'ом."""
    links = [{"pid": "1", "url": "u1"}]
    sess = _FakeSess(links)
    sess.page = _HangPage()
    recycled = []
    calls = {"n": 0}

    def fake_collect_links(sess, limit, scan_limit=None, max_scrolls=40,
                           scroll_by=1500, wait_ms=1000, quiet_after=2):
        calls["n"] += 1
        if calls["n"] == 1:
            from playwright.sync_api import TimeoutError
            raise TimeoutError("Timeout 15000ms exceeded")
        return links

    def fake_recycle(sess, manager, launch_id):
        recycled.append(launch_id)

    monkeypatch.setattr("ozon_collector.search._collect_links", fake_collect_links)
    monkeypatch.setattr("ozon_collector.search._recycle_browser", fake_recycle)
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    c = collect_search(sess, "u", limit=5, gather_cfg={})
    assert calls["n"] == 2                        # первый провалился, второй — успех
    assert len(recycled) == 1
    assert recycled[0] == "search-links-r1"
    assert c["recovery"]["recycles"] == 1
    assert c["recovery"]["events"][0]["pid"] == "links"
    assert c["recovery"]["events"][0]["kind"] == "renderer_failure"
    assert c["collected"] == 1


def _growing_links(n, start=0):
    return [{"pid": str(start + i), "url": f"https://www.ozon.ru/product/{start + i}"}
            for i in range(1, n + 1)]


def test_collect_search_scrolls_until_stagnation(monkeypatch):
    """Докрутка до стабилизации: found = вся выдача (32), а не только DOM-старт."""
    steps = [_growing_links(8), _growing_links(16), _growing_links(32),
             _growing_links(32), _growing_links(32)]
    sess = _GrowingSess(steps)
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    c = collect_search(sess, "u", limit=50, gather_cfg={})
    assert c["found"] == 32
    assert c["collected"] == 32
    assert len(c["products"]) == 32
    assert sess.scrolls == 4  # 2 скролла без роста → стоп


def test_collect_search_found_full_selected_limit(monkeypatch):
    """found = вся выдача, но собираем только limit товаров."""
    steps = [_growing_links(8), _growing_links(16), _growing_links(32),
             _growing_links(32), _growing_links(32)]
    sess = _GrowingSess(steps)
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    c = collect_search(sess, "u", limit=5, gather_cfg={})
    assert c["found"] == 32      # найдено всё, что в выдаче
    assert c["limit"] == 5
    assert c["collected"] == 5   # собрано только 5
    assert len(c["products"]) == 5


def test_collect_search_dedup_across_scrolls(monkeypatch):
    """Дедуп по pid между скроллами (виртуализация + пересечения)."""
    steps = [_growing_links(3, 1), _growing_links(4, 2), _growing_links(5, 4),
             _growing_links(5, 4), _growing_links(5, 4)]
    sess = _GrowingSess(steps)
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    c = collect_search(sess, "u", limit=50, gather_cfg={})
    assert c["found"] == 8
    pids = [p["pid"] for p in c["products"]]
    assert len(set(pids)) == len(pids)


def test_collect_search_stops_at_scan_limit(monkeypatch):
    """scan_limit (max(limit,50)) останавливает докрутку на больших выдачах."""
    steps = [_growing_links(8), _growing_links(16), _growing_links(32),
             _growing_links(64), _growing_links(128)]
    sess = _GrowingSess(steps)
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    c = collect_search(sess, "u", limit=5, gather_cfg={})
    assert c["found"] >= 50  # остановились на scan_limit, не дошли до стагнации
    assert sess.scrolls == 3


# ---- render_search_digest --------------------------------------------------

def _combined(products):
    return {
        "schema_version": "2.0", "collector_version": "1.11.0",
        "collected_at_utc": "2026-08-08T00:00:00+00:00",
        "search": {"url": "https://www.ozon.ru/search/?text=x", "query": "x"},
        "found": 3, "limit": 2, "collected": 2, "products": products,
    }


def test_render_search_digest_format():
    ok = _ok_result("42")
    ok["raw"]["reviews"] = [
        {"author": f"Автор{i}", "text": f"текст {i}", "stars": 5}
        for i in range(20)
    ]
    bad = {"pid": "7", "ok": False, "error": "TimeoutError: нет сети",
           "raw": None, "markdown": "", "cache": "miss", "ocr": "off"}
    md = R.render_search_digest(_combined([ok, bad]), reviews_per_product=5)
    assert "Дайджест выдачи Ozon" in md
    assert "найдено товаров в выдаче" in md.lower()
    assert "⚠ Не удалось собрать: TimeoutError: нет сети" in md
    assert "## Задание для LLM" in md
    # обрезка отзывов: до 5 (Автор0..Автор4 есть, Автор5.. нет)
    assert "Автор0" in md
    assert "Автор5" not in md
    assert "текст 19" not in md


def test_render_search_digest_no_truncate_if_zero():
    ok = _ok_result("42")
    ok["raw"]["reviews"] = [
        {"author": f"Автор{i}", "text": f"текст {i}", "stars": 5}
        for i in range(3)
    ]
    md = R.render_search_digest(_combined([ok]), reviews_per_product=0)
    assert "Автор2" in md
