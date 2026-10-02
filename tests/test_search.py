"""Тесты поиска: SEARCH_JS на фикстурах, лимиты, partial failure,
collect_search (одна сессия, ошибки), render_search_digest."""
from __future__ import annotations

import gzip
import os

from ozon_collector.search import (
    SEARCH_JS, search_url, collect_search, _recovery_meta,
    CollectionInterrupted, _collect_links, _collect_links_with_recovery)
from ozon_collector import browser as B
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
        self.new_pages = 0
        self.cdp_url = "http://127.0.0.1:9222"
        self._managed = 1
        self._page_url = "https://www.ozon.ru/product/1"

    @property
    def page(self):
        if getattr(self, "_page_override", None) is not None:
            return self._page_override
        class _P:
            url = self._page_url
            def title(self):
                return "Ozon"
            def evaluate(self, js, *args, **kwargs):
                return ""
        return _P()

    @page.setter
    def page(self, value):
        self._page_override = value

    def collector_tabs(self):
        return self._managed

    def goto(self, url, wait_ms=None):
        self.gotos += 1

    def eval(self, js):
        return self._links

    def scroll(self, by=600):
        self.scrolls += 1

    def wait(self, ms=1000):
        pass

    def new_page(self, close_current=True):
        self.new_pages += 1


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


def _ok_rating(pid, rating):
    res = _ok_result(pid)
    res["raw"]["product"]["rating"] = rating
    return res


def test_collect_search_min_rating_excludes(monkeypatch):
    """--min-rating: собранный товар с рейтингом ниже порога → excluded.

    raw остаётся собранным (ok=True), но из collected/дайджеста исключается.
    Рейтинг == порогу (4.7) и None (не извлечён) не исключаются."""
    links = [{"pid": str(i), "url": f"https://www.ozon.ru/product/{i}"}
             for i in range(1, 5)]
    sess = _FakeSess(links)
    ratings = {"1": 4.5, "2": 4.8, "3": None, "4": 4.7}

    def fake(cfg, sess=None):
        return _ok_rating(cfg["pid"], ratings[cfg["pid"]])

    monkeypatch.setattr("ozon_collector.search.run_gather", fake)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5,
                       gather_cfg={"min_rating": 4.7})
    assert c["collected"] == 3
    assert c["excluded"] == 1
    assert c["min_rating"] == 4.7
    assert all(p["ok"] for p in c["products"])
    ex = [p for p in c["products"] if p.get("excluded")]
    assert [p["pid"] for p in ex] == ["1"]
    assert ex[0]["excluded"] == {"reason": "rating_below", "rating": 4.5,
                                 "min_rating": 4.7}


def test_collect_search_min_rating_disabled(monkeypatch):
    """Без min_rating в gather_cfg фильтр выключен (даже при низком рейтинге)."""
    sess = _FakeSess([{"pid": "1", "url": "u1"}])
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_rating(cfg["pid"], 3.0))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
    assert c["collected"] == 1
    assert c["excluded"] == 0
    assert "min_rating" not in c


def test_collect_search_found_less_than_limit(monkeypatch):
    links = [{"pid": str(i), "url": f"https://www.ozon.ru/product/{i}"}
             for i in range(1, 4)]
    sess = _FakeSess(links)
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
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
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
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
        collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
    assert sess.gotos == 3  # warmup (главная + категория) + одна докрутка (одна CDP-сессия)


def test_collect_search_progress_output(monkeypatch, capsys):
    links = [{"pid": str(i), "url": f"https://www.ozon.ru/product/{i}"}
             for i in range(1, 4)]
    sess = _FakeSess(links)
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
    out = capsys.readouterr().out
    assert "найдено 3 товаров в выдаче, собираю 3" in out
    assert "товар 1/3: pid=1" in out
    assert "товар 3/3: ok" in out


# ---- pids-режим (явный список артикулов вместо докрутки выдачи) ------------

def test_collect_search_pids_skips_link_scan(monkeypatch):
    """pids-режим: выдача не сканируется (ноль скроллов), собираются ТОЛЬКО
    перечисленные артикулы, found = len(pids)."""
    links = [{"pid": "1", "url": "u1"}, {"pid": "2", "url": "u2"}]
    sess = _FakeSess(links)

    def fake_run_gather(cfg, sess=None):
        sess.called_pids.append(cfg["pid"])
        return _ok_result(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=x",
                       limit=5, gather_cfg={}, query="x",
                       pids=["999", "777"])
    assert sess.scrolls == 0                    # докрутки нет
    assert sess.gotos == 0
    assert c["found"] == 2
    assert c["collected"] == 2
    pids = [p["pid"] for p in c["products"]]
    assert sorted(pids) == ["777", "999"]


def test_collect_search_pids_uses_product_url(monkeypatch):
    """pids-режим: URL карточки строится как /product/{pid}."""
    sess = _FakeSess([])
    seen = {}

    def fake_run_gather(cfg, sess=None):
        seen["url"] = cfg.get("url")
        return _ok_result(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={}, pids=["1234567"])
    assert seen["url"] == "https://www.ozon.ru/product/1234567"


def test_collect_search_pids_respects_limit(monkeypatch):
    """pids-режим: limit обрезает сбор, но found = полный список."""
    sess = _FakeSess([])
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2, gather_cfg={},
                       pids=["1", "2", "3"])
    assert c["found"] == 3
    assert c["collected"] == 2
    assert len(c["products"]) == 2


# ---- recovery (R1 recycle / R2 retry) --------------------------------------

def _recyclable_fail(pid, error="net::ERR_ABORTED: The frame was detached"):
    return {
        "pid": pid, "ok": False, "error": f"Error: {error}",
        "raw": None, "markdown": "", "cache": "miss", "ocr": "off",
        "recyclable": True, "error_kind": "renderer_failure",
        "error_probe_state": "renderer_failure", "error_signature": True,
    }


def _challenge_fail(pid, error="анти-бот (product_page): title:похоже, нет соединения"):
    return {
        "pid": pid, "ok": False, "error": error,
        "raw": None, "markdown": "", "cache": "miss", "ocr": "off",
        "recyclable": True, "error_kind": "access_challenge",
        "error_probe_state": None, "error_signature": False,
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
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
    assert len(recycled) == 1             # ровно один recycle
    assert recycled[0] == "search-1-r1"
    assert c["recovery"]["recycles"] == 1
    assert c["recovery"]["events"][0]["kind"] == "renderer_failure"
    ok = [p for p in c["products"] if p["ok"]]
    assert len(ok) == 2
    assert ok[0]["recycle_attempts"] == 2
    assert ok[1]["recycle_attempts"] == 1


def _browser_fail(pid, error="TargetClosedError: Page.goto: Target page, "
                              "context or browser has been closed"):
    """kind=browser: Playwright потерял цель (в т.ч. умер сам Chromium)."""
    return {
        "pid": pid, "ok": False, "error": f"Error: {error}",
        "raw": None, "markdown": "", "cache": "miss", "ocr": "off",
        "recyclable": True, "error_kind": "browser",
        "error_probe_state": None, "error_signature": False,
    }


def _dead_cdp(monkeypatch):
    """CDP воркера не отвечает (браузер убит/упал)."""
    monkeypatch.setattr("ozon_collector.search.browser_alive",
                        lambda url: False)
    monkeypatch.setattr("ozon_collector.search.time.sleep",
                        lambda *_a, **_k: None)


def test_collect_search_browser_dead_no_recycle(monkeypatch):
    """Браузер мёртв: recycle НЕ тратится (нечего пересоздавать).

    Инцидент 2026-09-26: мёртвый CDP + recycle-бюджет = SESSION_EXHAUSTED на
    каждом товаре (soft block из-за локальной поломки, Ozon ни при чём).
    """
    links = [{"pid": "1", "url": "u1"}]
    sess = _FakeSess(links)
    recycled = []
    _dead_cdp(monkeypatch)
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _browser_fail(cfg["pid"]))
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id: recycled.append(
                            launch_id))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=1,
                       gather_cfg={})
    assert recycled == []                       # recycle не вызывался
    assert c["recovery"]["recycles"] == 0       # и не посчитан
    assert c["recovery"]["browser_dead"] == 1
    p = c["products"][0]
    assert p["ok"] is False
    assert p["error_kind"] == "browser_dead"
    assert p["failure_type"] == "infra_failure"  # НЕ session_failure
    assert "нужен перезапуск браузера" in p["error"]


def test_collect_search_browser_dead_not_a_block(monkeypatch):
    """browser_dead — локальная поломка, НЕ блок: block_kind не выставляем."""
    links = [{"pid": str(i), "url": f"u{i}"} for i in range(1, 3)]
    sess = _FakeSess(links)
    _dead_cdp(monkeypatch)
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _browser_fail(cfg["pid"]))
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id: None)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2,
                       gather_cfg={})
    assert "block_kind" not in c["recovery"]
    assert c["recovery"].get("session_exhausted", 0) == 0
    assert c["recovery"].get("session_blocked", 0) == 0
    # run НЕ прерван: оба товара отработали штатно (каждый со своим browser_dead)
    assert c.get("aborted") is None
    assert [p["error_kind"] for p in c["products"]] == ["browser_dead"] * 2
    assert c["recovery"]["browser_dead"] == 2


def test_collect_search_browser_dead_keeps_budget(monkeypatch):
    """Мёртвый браузер НЕ жжёт recycle-бюджет (в отличие от живого)."""
    _budget(monkeypatch, challenge=10, new_tab=10, browser_recycle=1)
    links = [{"pid": str(i), "url": f"u{i}"} for i in range(1, 3)]
    sess = _FakeSess(links)
    _dead_cdp(monkeypatch)
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _browser_fail(cfg["pid"]))
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id: None)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2,
                       gather_cfg={})
    # browser_recycle=1, 2 товара: живой браузер ушёл бы в SESSION_EXHAUSTED,
    # мёртвый — не потратил бюджет вовсе (session_used, не лимиты).
    assert c["recovery"]["session_budget"]["browser_recycle"] == 1
    assert c["recovery"]["session_used"].get("browser_recycle", 0) == 0
    assert c["recovery"].get("session_exhausted", 0) == 0
    assert c.get("aborted") is None


def test_collect_search_live_browser_still_recycles(monkeypatch):
    """Регрессия: живой CDP + renderer_failure → recycle по-старому (R1/R2)."""
    links = [{"pid": "1", "url": "u1"}]
    sess = _FakeSess(links)
    recycled = []
    calls = {}

    def fake_run_gather(cfg, sess=None):
        if "retried" not in calls:
            calls["retried"] = True
            return _recyclable_fail(cfg["pid"])   # renderer_failure
        return _ok_result(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.browser_alive",
                        lambda url: True)
    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id: recycled.append(
                            launch_id))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=1,
                       gather_cfg={})
    assert recycled == ["search-1-r1"]
    assert c["products"][0]["ok"] is True
    assert "browser_dead" not in c["recovery"]


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
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
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
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={},
                       max_recycles=2)
    assert len(recycled) == 3              # 2 recycle + 1 preventive (stability)
    assert c["recovery"]["recycles"] == 3
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
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
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
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
    preventive = [r for r in recycled if "preventive" in r]
    assert len(preventive) == 2                 # после 2-го и 4-го товара
    assert c["recovery"]["recycles"] == 2
    kinds = [e["kind"] for e in c["recovery"]["events"]]
    assert kinds == ["preventive", "preventive"]
    assert c["collected"] == 5


# ---- SESSION_BLOCKED (ADR §14.2: лестница одного challenge-инцидента) -------

def test_collect_search_challenge_storm_aborts(monkeypatch):
    """Персистентный challenge → лестница (new_tab×1 → recycle×1) исчерпана →
    SESSION_BLOCKED, остаток aborted (session_failure, без штрафа PID).

    Никакого агрессивного retry-loop: лестница не крутится, после исчерпания
    run останавливается, remaining помечается error_kind=session_blocked,
    combined.aborted."""
    links = [{"pid": str(i), "url": f"u{i}"} for i in range(1, 6)]
    sess = _FakeSess(links)
    recycled = []
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _challenge_fail(cfg["pid"]))
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id:
                        recycled.append(launch_id))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
    assert len(recycled) == 1               # 1 recycle (лестница инцидента)
    assert sess.new_pages == 1              # 1 new_tab (лестница инцидента)
    kinds = [p["error_kind"] for p in c["products"]]
    assert kinds == ["session_blocked"] * 5
    assert all(p["failure_type"] == "session_failure" for p in c["products"])
    assert all(p["recyclable"] is False for p in c["products"])
    assert all("cooldown" in p["error"] for p in c["products"][1:])
    assert c["aborted"]["reason"] == "session_blocked"
    assert c["collected"] == 0


def test_collect_search_pause_between_products(monkeypatch):
    """gather_cfg['pause_s'] > 0 → time.sleep вызывается между товарами,
    но не после последнего (нет бесполезного ожидания)."""
    import time
    links = [{"pid": f"{i}", "url": f"u{i}"} for i in range(1, 4)]
    sess = _FakeSess(links)
    monkeypatch.setenv("OZON_ACTIVE_WARMUP", "0")
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    sleeps = []
    monkeypatch.setattr("ozon_collector.search.time.sleep",
                        lambda s: sleeps.append(s))
    collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=3,
                   gather_cfg={"pause_s": 2.5})
    assert sleeps == [2.5, 2.5]   # 3 товара → 2 паузы


def test_collect_search_pause_zero_no_sleep(monkeypatch):
    """pause_s отсутствует/0 → time.sleep не вызывается вовсе."""
    import time
    links = [{"pid": f"{i}", "url": f"u{i}"} for i in range(1, 3)]
    sess = _FakeSess(links)
    monkeypatch.setenv("OZON_ACTIVE_WARMUP", "0")
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    sleeps = []
    monkeypatch.setattr("ozon_collector.search.time.sleep",
                        lambda s: sleeps.append(s))
    collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2, gather_cfg={})
    assert sleeps == []


def test_collect_search_warmup_challenges_telemetry(monkeypatch):
    """Прогрев пишет warmup_challenges в recovery и НЕ тратит session_used."""
    import ozon_collector.search as _S
    links = [{"pid": "1", "url": "u1"}]
    sess = _FakeSess(links)
    monkeypatch.setenv("OZON_ACTIVE_WARMUP", "0")
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))

    def _challenge(page, where, reload_on_fab=True):
        if where == "warmup_home":
            raise B.AccessChallengeError(f"challenge ({where}): title:captcha")
        return None

    monkeypatch.setattr(_S.C, "check_challenge", _challenge)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=1, gather_cfg={})
    assert c["recovery"]["warmup_challenges"] == 1
    assert c["recovery"]["warmup_entry"] == "home_category"
    assert c["recovery"]["session_used"] == {"challenge": 0, "new_tab": 0,
                                             "browser_recycle": 0}


def test_collect_search_challenge_transient_ok_continues(monkeypatch):
    """Transient challenge (снят new_tab) → товар ok, прогон продолжается,
    SESSION_BLOCKED не наступает."""
    links = [{"pid": str(i), "url": f"u{i}"} for i in range(1, 3)]
    sess = _FakeSess(links)
    seen = set()

    def fake_run_gather(cfg, sess=None):
        if cfg["pid"] not in seen:
            seen.add(cfg["pid"])
            return _challenge_fail(cfg["pid"])
        return _ok_result(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id: None)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2, gather_cfg={})
    assert "aborted" not in c
    assert c["collected"] == 2
    assert c["recovery"].get("session_blocked") is None
    # оба товара собраны через new_tab-снятие (tab_observed)
    assert all(p["observed_scope"] == "tab_observed" for p in c["products"])


def test_collect_search_challenge_ladder_not_a_loop(monkeypatch):
    """Лестница одного инцидента не крутится: после new_tab→recycle challenge
    → SESSION_BLOCKED (abort), а не бесконечный retry."""
    links = [{"pid": str(i), "url": f"u{i}"} for i in range(1, 2)]
    sess = _FakeSess(links)
    calls = {"n": 0}
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None:
                        calls.__setitem__("n", calls["n"] + 1) or
                        _challenge_fail(cfg["pid"]))
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id: None)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=1, gather_cfg={})
    # 1 + new_tab(1) + recycle(1) = 3 вызова; НЕ 4-й и НЕ бесконечный
    assert calls["n"] == 3
    assert c["products"][0]["error_kind"] == "session_blocked"
    assert c["products"][0]["observed_scope"] == "profile_observed"
    assert c["collected"] == 0


# ---- new_tab recovery (эксперимент 2026-08-17: FAB → новая вкладка) ----------

def test_collect_search_challenge_new_tab_recovers(monkeypatch):
    """access_challenge → recovery новой вкладкой: товар собирается БЕЗ recycle.

    Эксперимент: FAB залипает на навигации конкретной вкладки, а свежая
    вкладка проходит Qrator и разблокирует сессию (auth-куки)."""
    links = [{"pid": "1", "url": "u1"}, {"pid": "2", "url": "u2"}]
    sess = _FakeSess(links)
    recycled = []
    calls = {}

    def fake_run_gather(cfg, sess=None):
        if cfg["pid"] == "1" and "nt" not in calls:
            calls["nt"] = True
            return _challenge_fail(cfg["pid"])
        return _ok_result(cfg["pid"])

    def fake_recycle(sess, manager, launch_id):
        recycled.append(launch_id)

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser", fake_recycle)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
    assert recycled == []                    # recycle НЕ вызывался
    assert sess.new_pages == 1               # одна новая вкладка
    ok = [p for p in c["products"] if p["ok"]]
    assert len(ok) == 2
    p1 = [p for p in c["products"] if p["pid"] == "1"][0]
    assert p1["ok"] is True
    assert p1["new_tab_attempts"] == 1
    assert p1["recovery_kind"] == "new_tab"
    assert p1["recycle_attempts"] == 2       # 1 вызов + 1 повтор после вкладки
    assert c["recovery"]["new_tab_attempts"] == 1
    assert c["recovery"]["new_tab_successes"] == 1
    assert c["recovery"]["recycles"] == 0
    assert any(e["kind"] == "new_tab" for e in c["recovery"]["events"])


def test_collect_search_challenge_new_tab_fallback_recycle(monkeypatch):
    """Персистентный challenge: new_tab×1 → recycle×1 (лестница) → успех."""
    links = [{"pid": "1", "url": "u1"}]
    sess = _FakeSess(links)
    recycled = []
    calls = {"n": 0}

    def fake_run_gather(cfg, sess=None):
        calls["n"] += 1
        if calls["n"] == 3:                  # 1 + new_tab + recycle
            return _ok_result(cfg["pid"])
        return _challenge_fail(cfg["pid"])

    def fake_recycle(sess, manager, launch_id):
        recycled.append(launch_id)

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser", fake_recycle)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
    assert sess.new_pages == 1
    assert len(recycled) == 1
    p = c["products"][0]
    assert p["ok"] is True
    assert p["new_tab_attempts"] == 1
    assert p["recovery_kind"] == "recycle"   # последний recovery — recycle
    assert p["recycle_attempts"] == 3        # 1 + new_tab + recycle
    assert p["observed_scope"] == "browser_observed"
    assert c["recovery"]["new_tab_attempts"] == 1
    assert c["recovery"]["new_tab_successes"] == 0
    assert c["recovery"]["recycles"] == 1
    kinds = [e["kind"] for e in c["recovery"]["events"]]
    assert kinds == ["new_tab", "recycle"]


def test_collect_search_challenge_new_tab_max_then_recycle_limit(monkeypatch):
    """Лестница исчерпана: new_tab×1 + recycle×1 на товар → SESSION_BLOCKED,
    остаток aborted (session_failure)."""
    links = [{"pid": str(i), "url": f"u{i}"} for i in range(1, 3)]
    sess = _FakeSess(links)
    recycled = []
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _challenge_fail(cfg["pid"]))
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id:
                        recycled.append(launch_id))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2, gather_cfg={})
    assert sess.new_pages == 1               # 1 new_tab (первый товар)
    assert len(recycled) == 1                # 1 recycle (первый товар)
    p0 = c["products"][0]
    assert p0["ok"] is False
    assert p0["error_kind"] == "session_blocked"
    assert p0["failure_type"] == "session_failure"
    assert p0["observed_scope"] == "profile_observed"
    assert p0["new_tab_attempts"] == 1
    assert p0["recycle_attempts"] == 3       # 1 + new_tab + recycle
    # остаток aborted (session_failure, не штрафуем PID)
    assert c["products"][1]["error_kind"] == "session_blocked"
    assert c["aborted"]["reason"] == "session_blocked"
    assert c["recovery"]["new_tab_attempts"] == 1
    assert c["recovery"]["recycles"] == 1


def test_collect_search_challenge_new_tab_failure_marks_product(monkeypatch):
    """new_page сам упал → ошибка дополняется new_tab_failed, без recycle."""
    links = [{"pid": "1", "url": "u1"}]
    sess = _FakeSess(links)
    recycled = []

    def _boom(*args, **kwargs):
        raise RuntimeError("CDP target closed")

    sess.new_page = _boom

    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _challenge_fail(cfg["pid"]))
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id:
                        recycled.append(launch_id))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
    assert recycled == []
    p = c["products"][0]
    assert p["ok"] is False
    assert "new_tab_failed" in p["error"]
    assert p["recyclable"] is False
    assert p["new_tab_attempts"] == 1
    assert c["recovery"]["new_tab_attempts"] == 1
    assert c["recovery"]["recycles"] == 0


# ---- этап 3: session budget (ADR §14.4) --------------------------------------

def _budget(monkeypatch, **over):
    b = {"challenge": 3, "new_tab": 3, "browser_recycle": 2, "wall_clock_s": 0}
    b.update(over)
    monkeypatch.setattr("ozon_collector.search.SESSION_BUDGET_DEFAULT", b)
    return b


def test_session_budget_challenge_exhausted(monkeypatch):
    """challenge-budget исчерпан → SESSION_EXHAUSTED (abort run)."""
    _budget(monkeypatch, challenge=1, new_tab=10, browser_recycle=10)
    links = [{"pid": str(i), "url": f"u{i}"} for i in range(1, 3)]
    sess = _FakeSess(links)
    seen = set()

    def fake_run_gather(cfg, sess=None):
        if cfg["pid"] not in seen:     # transient: снимается new_tab
            seen.add(cfg["pid"])
            return _challenge_fail(cfg["pid"])
        return _ok_result(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id: None)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2, gather_cfg={})
    # товар1: challenge(1) → new_tab → ok; товар2: challenge(2 > 1) → exhausted
    assert c["products"][0]["ok"] is True
    assert c["products"][1]["error_kind"] == "session_exhausted"
    assert c["products"][1]["failure_type"] == "session_failure"
    assert c["aborted"]["reason"] == "session_exhausted"


def test_session_budget_new_tab_exhausted(monkeypatch):
    """new_tab-budget исчерпан → SESSION_EXHAUSTED, recycle-бюджет не тратится."""
    _budget(monkeypatch, challenge=10, new_tab=1, browser_recycle=10)
    links = [{"pid": str(i), "url": f"u{i}"} for i in range(1, 3)]
    sess = _FakeSess(links)
    seen = set()

    def fake_run_gather(cfg, sess=None):
        if cfg["pid"] not in seen:
            seen.add(cfg["pid"])
            return _challenge_fail(cfg["pid"])
        return _ok_result(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id: None)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2, gather_cfg={})
    assert c["products"][0]["ok"] is True
    assert c["products"][1]["error_kind"] == "session_exhausted"
    assert c["recovery"]["session_used"]["new_tab"] == 2  # 1 + попытка на 2-м
    assert c["recovery"]["session_used"]["browser_recycle"] == 0


def test_session_budget_wall_clock_param_override(monkeypatch):
    """session_budget-параметр ПЕРЕКРЫВАЕТ SESSION_BUDGET_DEFAULT (ревизия 2.6).

    Передаём свой бюджет с challenge=1 → 2-й challenge даёт SESSION_EXHAUSTED,
    хотя глобальный SESSION_BUDGET_DEFAULT.challenge=3 (не тронут). Это
    доказывает, что override-словарь используется, а не дефолт.
    """
    links = [{"pid": str(i), "url": f"u{i}"} for i in range(1, 3)]
    sess = _FakeSess(links)
    seen = set()

    def fake_run_gather(cfg, sess=None):
        if cfg["pid"] not in seen:
            seen.add(cfg["pid"])
            return _challenge_fail(cfg["pid"])
        return _ok_result(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id: None)
    override = {"challenge": 1, "new_tab": 10, "browser_recycle": 10,
                "wall_clock_s": 0}
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2, gather_cfg={},
                       session_budget=dict(override))
    assert c["recovery"]["session_budget"]["challenge"] == 1
    assert c["products"][0]["ok"] is True          # challenge(1) → new_tab → ok
    assert c["products"][1]["error_kind"] == "session_exhausted"  # 2-й challenge
    assert c["aborted"]["reason"] == "session_exhausted"
    assert c["kpi"]["session_exhausted"] >= 1


def test_kpi_and_fingerprint_in_combined(monkeypatch):
    """combined содержит KPI-агрегаты и fingerprint (ревизии 2.6/2.7)."""
    links = [{"pid": "1", "url": "u1"}, {"pid": "2", "url": "u2"}]
    sess = _FakeSess(links)
    sess.fingerprint = {"ua": "Mozilla/5.0", "webdriver": False,
                        "tz": "Europe/Moscow", "vw": 1080, "vh": 720}

    def fake_run_gather(cfg, sess=None):
        return _ok_result(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2, gather_cfg={})
    kpi = c["kpi"]
    assert kpi["total_challenges"] == 0
    assert kpi["total_recoveries"] == 0
    assert kpi["success_rate"] == 1.0
    assert c["fingerprint"]["webdriver"] is False
    assert c["fingerprint"]["ua"] == "Mozilla/5.0"


def test_kpi_challenge_recovery_counts(monkeypatch):
    """KPI считает challenge и recovery-действия (ADR §14.4/§16)."""
    _budget(monkeypatch, challenge=5, new_tab=5, browser_recycle=5)
    links = [{"pid": "1", "url": "u1"}, {"pid": "2", "url": "u2"}]
    sess = _FakeSess(links)
    seen = set()

    def fake_run_gather(cfg, sess=None):
        if cfg["pid"] not in seen:
            seen.add(cfg["pid"])
            return _challenge_fail(cfg["pid"])
        return _ok_result(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id: None)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2, gather_cfg={})
    # 2 товара, оба с 1 challenge → new_tab recovery
    assert c["kpi"]["total_challenges"] == 2
    assert c["kpi"]["total_recoveries"] >= 2  # new_tab_attempts
    assert c["kpi"]["success_rate"] == 1.0
    assert c["kpi"]["challenge_rate_per_100"] == 100.0
    assert c["kpi"]["puzzle_attempts"] == 0
    assert c["kpi"]["puzzle_solve_rate"] is None


def test_kpi_puzzle_solve_rate(monkeypatch):
    """KPI отдаёт solve-rate пазла: попытки/решения (ADR §14.7)."""
    links = [{"pid": "1", "url": "u1"}]
    sess = _FakeSess(links)
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    monkeypatch.setattr("ozon_collector.puzzle.attempts", lambda: 5)
    monkeypatch.setattr("ozon_collector.puzzle.failures", lambda: 2)
    # solved считается ЯВНО с 2026-10-02 (раньше выводился как attempts-failures)
    monkeypatch.setattr("ozon_collector.puzzle.solved", lambda: 3)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=1, gather_cfg={})
    assert c["recovery"]["puzzle"] == {"attempts": 5, "failures": 2, "solved": 3}
    assert c["kpi"]["puzzle_attempts"] == 5
    assert c["kpi"]["puzzle_solved"] == 3
    assert c["kpi"]["puzzle_solve_rate"] == 0.6
    assert c["kpi"]["challenge_rate_per_100"] == 0.0


# ---- этап 7: recycle по browser stability (ADR §14.5) ------------------------

def test_preventive_recycle_by_renderer_errors(monkeypatch):
    """Накопленные renderer-ошибки → preventive recycle (stability), НЕ по
    challenge. Challenge не вызывает stability-recycle."""
    monkeypatch.setattr("ozon_collector.search.run_canary",
                        lambda s, url=None: "ok")
    links = [{"pid": str(i), "url": f"u{i}"} for i in range(1, 3)]
    sess = _FakeSess(links)
    recycled = []

    def fake_run_gather(cfg, sess=None):
        # renderer_failure на обоих товарах (после recycle fail остаётся)
        return _recyclable_fail(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id:
                        recycled.append(launch_id))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2, gather_cfg={},
                       max_recycles=1)
    # 2 товара × 1 recycle (max_recycles) + preventive (renderer_errors>=2)
    assert len(recycled) >= 3
    preventive = [e for e in c["recovery"]["events"]
                  if e["kind"] == "preventive"]
    assert len(preventive) >= 1             # stability-recycle случился
    # access_stats НЕ растут от renderer (раздельно)
    assert c["recovery"]["access_stats"]["challenges"] == 0


# ---- этап 4: canary (ADR §14.2) ---------------------------------------------

def test_canary_ok_after_recycle_continues(monkeypatch):
    """Canary OK после recycle-recovery → прогон продолжается."""
    monkeypatch.setattr("ozon_collector.search.run_canary",
                        lambda sess, url=None: "ok")
    links = [{"pid": "1", "url": "u1"}]
    sess = _FakeSess(links)
    recycled = []
    calls = {"n": 0}

    def _gather(cfg, sess=None):
        calls["n"] += 1
        if calls["n"] == 3:              # 3-й вызов — после new_tab+recycle → ok
            return _ok_result(cfg["pid"])
        return _challenge_fail(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.run_gather", _gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id:
                        recycled.append(launch_id))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2, gather_cfg={})
    # товар: challenge→new_tab(challenge)→recycle→ok; canary OK → без aborted
    assert c["collected"] == 1
    assert "aborted" not in c
    assert c["recovery"].get("canary_fails") is None
    assert c["products"][0]["ok"] is True
    assert len(recycled) == 1


def test_canary_challenge_blocks_session(monkeypatch):
    """Canary CHALLENGE после recycle-recovery → остаток SESSION_BLOCKED, но уже
    собранный товар НЕ штрафуется (raw/ok сохраняется, ADR §14.1)."""
    monkeypatch.setattr("ozon_collector.search.run_canary",
                        lambda sess, url=None: "challenge")
    links = [{"pid": str(i), "url": f"u{i}"} for i in range(1, 3)]
    sess = _FakeSess(links)
    recycled = []
    calls = {"n": 0}

    def _gather(cfg, sess=None):
        calls["n"] += 1
        if calls["n"] == 3:              # после new_tab+recycle — ok
            return _ok_result(cfg["pid"])
        return _challenge_fail(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.run_gather", _gather)
    monkeypatch.setattr("ozon_collector.search._recycle_browser",
                        lambda sess, manager, launch_id:
                        recycled.append(launch_id))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2, gather_cfg={})
    # товар1 собран (ok=True) через recycle; canary CHALLENGE → abort остатка
    assert c["products"][0]["ok"] is True
    assert c["products"][0]["raw"] is not None
    # товар2 (ещё не начат) aborted как session_blocked
    assert c["products"][1]["error_kind"] == "session_blocked"
    assert c["products"][1]["failure_type"] == "session_failure"
    assert c["recovery"]["canary_fails"] == 1
    assert c["aborted"]["reason"] == "session_blocked"


# ---- warmup: challenge на главной → новая вкладка (ревизия 2.6) -------------

def test_warmup_home_challenge_switches_tab(monkeypatch):
    """Challenge на главной при warmup → новая вкладка ДО категории (не тащим
    challenge на первый PID)."""
    import ozon_collector.search as _S
    sess = _FakeSess([{"pid": "1", "url": "u1"}])

    def _challenge_page(page, where, reload_on_fab=True):
        raise B.AccessChallengeError(f"анти-бот ({where}): title:captcha")

    monkeypatch.setattr(_S.C, "check_challenge", _challenge_page)
    _S._warmup_to_category(sess, "https://www.ozon.ru/category/c")
    # главная (1) + категория (1) = 2 goto; плюс new_page при challenge
    assert sess.gotos == 2
    assert sess.new_pages == 1


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
                           scroll_by=1500, wait_ms=1000, quiet_after=3,
                           quiet_wait_ms=2500,
                           stop_event=None, force_event=None):
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
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
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
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=50, gather_cfg={})
    assert c["found"] == 32
    assert c["collected"] == 32
    assert len(c["products"]) == 32
    # 3 скролла с ростом + 2 тихие со скроллом; на 3-й тихой цикл выходит
    # (quiet_after=3) — скролла на ней уже нет
    assert sess.scrolls == 5


def test_collect_search_found_full_selected_limit(monkeypatch):
    """found = вся выдача, но собираем только limit товаров."""
    steps = [_growing_links(8), _growing_links(16), _growing_links(32),
             _growing_links(32), _growing_links(32)]
    sess = _GrowingSess(steps)
    monkeypatch.setattr("ozon_collector.search.run_gather",
                        lambda cfg, sess=None: _ok_result(cfg["pid"]))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
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
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=50, gather_cfg={})
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
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
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
    assert "## Задание для LLM" not in md
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


# ---- этап 0: телеметрия recovery (ADR §14.6) --------------------------------

def test_recovery_meta_telemetry_fields(monkeypatch):
    """_recovery_meta обогащает событие: signature, last_url, tabs, duration."""
    monkeypatch.setattr("ozon_collector.search.browser_tabs_count",
                        lambda cdp_url: None)  # /json/list недоступен
    sess = _FakeSess([{"pid": "1", "url": "u1"}])
    ev = _recovery_meta(sess, {"pid": "1", "attempt": 2, "kind": "new_tab"},
                        duration=1.25, products_since_recovery=7,
                        error="Error: анти-бот (product_page): title:captcha")
    assert ev["signature"] == "Error: анти-бот (product_page): title:captcha"
    assert ev["last_url"] == "https://www.ozon.ru/product/1"
    assert ev["browser_tabs"] is None        # /json/list недоступен в тесте
    assert ev["collector_tabs"] == 1
    assert ev["duration_ms"] == 1250
    assert ev["products_since_recovery"] == 7
    # базовые поля события сохранены
    assert ev["kind"] == "new_tab" and ev["attempt"] == 2


def test_collect_search_challenge_event_has_telemetry(monkeypatch):
    """access_challenge → new_tab-событие с телеметрией (Этап 0)."""
    monkeypatch.setattr("ozon_collector.search.browser_tabs_count",
                        lambda cdp_url: None)
    links = [{"pid": "1", "url": "u1"}]
    sess = _FakeSess(links)

    def fake_run_gather(cfg, sess=None):
        return _challenge_fail(cfg["pid"])

    monkeypatch.setattr("ozon_collector.search.run_gather", fake_run_gather)
    # при первом new_tab сессия восстанавливается → второй вызов успешен
    sess._managed = 2
    # owner=external: recycle НЕ перезапускает процесс браузера (ADR §14.1) —
    # иначе тест доходит до BrowserProcessManager.restart() и поднимает
    # настоящий Chrome на 9222 (живой случай 2026-09-26, PID 5084).
    sess.browser_owner = "external"
    sess.ensure_single_worker_tab = lambda: None
    sess.reconnect = lambda: None
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=5, gather_cfg={})
    ev = c["recovery"]["events"][0]
    assert ev["kind"] == "new_tab"
    assert ev["signature"]  # текст ошибки challenge
    assert "last_url" in ev
    assert "browser_tabs" in ev
    assert ev["collector_tabs"] == 2
    assert "duration_ms" in ev
    assert ev["products_since_recovery"] == 0


# ---- этап 1: ownership (_recycle_browser, ADR §14.1) -------------------------

def test_recycle_browser_external_no_kill(monkeypatch):
    """external-браузер: recycle НЕ перезапускает процесс, только вкладку."""
    import ozon_collector.search as _S
    sess = _FakeSess([{"pid": "1", "url": "u1"}])
    sess.browser_owner = "external"
    ensure_calls = []
    sess.ensure_single_worker_tab = lambda: ensure_calls.append("ensure") or None
    manager = type("M", (), {"profile_token": "ozon-profile"})()
    restarts = []
    monkeypatch.setattr(_S.P, "browser_processes", lambda *a, **k: [])
    # подменяем внутренние шаги, чтобы увидеть, что restart НЕ зовётся
    class FakeManager:
        profile_token = "ozon-profile"
        def restart(self, old, launch_id):
            restarts.append(launch_id)
    _S._recycle_browser(sess, FakeManager(), "ext-r1")
    assert ensure_calls == ["ensure"]     # external → только новая вкладка
    assert restarts == []                 # restart/kill НЕ вызван


def test_recycle_browser_collector_restarts(monkeypatch):
    """collector-браузер: recycle перезапускает процесс (restart + reconnect)."""
    import ozon_collector.search as _S
    sess = _FakeSess([{"pid": "1", "url": "u1"}])
    sess.browser_owner = "collector"
    restarts = []
    reconnects = []
    sess.reconnect = lambda: reconnects.append("reconnect") or None

    class FakeManager:
        profile_token = "ozon-profile"
        def restart(self, old, launch_id):
            restarts.append(launch_id)

    monkeypatch.setattr(_S.P, "browser_processes",
                        lambda cdp_url, profile_token: [("ident",)])
    _S._recycle_browser(sess, FakeManager(), "c-r1")
    assert restarts == ["c-r1"]
    assert reconnects == ["reconnect"]


# ---- Кооперативная остановка докрутки ссылок (Ctrl+C) ------------------------

class _InterruptSess(_FakeSess):
    """Выдача, которая сама поднимает событие после первого eval (для partial)."""
    def __init__(self, links, event, on_eval):
        super().__init__(links)
        self._event = event
        self._on_eval = on_eval

    def eval(self, js):
        res = super().eval(js)
        self._on_eval()
        return res


def test_collect_links_graceful_stop_returns_partial_links():
    """stop_event на k-й итерации → CollectionInterrupted(forced=False) с
    частично найденными ссылками (не теряем прогресс докрутки)."""
    import threading as _t
    stop = _t.Event()
    collected = []
    # 2 ссылки появляются на первом eval, потом событие поднимается ДО след. скролла
    step = {"n": 0}
    def _snap():
        step["n"] += 1
        if step["n"] == 1:
            stop.set()
    sess = _InterruptSess([{"pid": "111", "url": "u111"},
                           {"pid": "222", "url": "u222"}], stop, _snap)
    try:
        _collect_links(sess, limit=10, stop_event=stop)
    except CollectionInterrupted as exc:
        collected = exc.links
        assert not exc.forced
    else:
        raise AssertionError("ожидали CollectionInterrupted (graceful)")
    pids = [l["pid"] for l in collected]
    assert pids == ["111", "222"]     # частичные ссылки сохранены


def test_collect_links_force_stop_raises_forced():
    """force_event → CollectionInterrupted(forced=True) — emergency (не graceful)."""
    import threading as _t
    force = _t.Event()
    force.set()
    sess = _FakeSess([{"pid": "111", "url": "u111"}])
    try:
        _collect_links(sess, limit=10, force_event=force)
    except CollectionInterrupted as exc:
        assert exc.forced
    else:
        raise AssertionError("ожидали CollectionInterrupted (forced)")


def test_collect_links_force_beats_stop():
    """Оба события установлены → побеждает force (FORCE > STOP)."""
    import threading as _t
    stop = _t.Event()
    force = _t.Event()
    stop.set()
    force.set()
    sess = _FakeSess([{"pid": "111", "url": "u111"}])
    try:
        _collect_links(sess, limit=10, stop_event=stop, force_event=force)
    except CollectionInterrupted as exc:
        assert exc.forced
    else:
        raise AssertionError("ожидали CollectionInterrupted (forced)")


# ---- недобор выдачи: тишина догрузки ≠ конец списка (регресс 2026-10-02) -----

class _ScriptedSess:
    """Выдача отдаёт порциями: тишина на 2 итерации, потом снова рост.

    Сценарий взят из живого лога: прогон 17:29 умел продолжить после тихой
    итерации (248/0 248/0 256/1 264/0), а прогон 18:22 на той же тишине
    выходил и объявлял 72 товара вместо 320+.
    """

    def __init__(self, script):
        self._script = list(script)   # список списков pid на каждой итерации
        self._i = 0
        self.waits = []
        self.scrolls = 0

    def eval(self, js):
        batch = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return [{"pid": p, "url": "u" + p} for p in batch]

    def scroll(self, by=1500):
        self.scrolls += 1

    def wait(self, ms=1000):
        self.waits.append(ms)


def test_collect_links_survives_two_quiet_iterations():
    """Две тихие итерации подряд — НЕ конец выдачи: докрутка продолжается."""
    sess = _ScriptedSess([
        ["1", "2"],                    # iter 0: 2
        ["3", "4"],                    # iter 1: 4
        ["3", "4"],                    # iter 2: тишина (stagnant=1)
        ["3", "4"],                    # iter 3: тишина (stagnant=2) — НЕ выход
        ["5"],                         # iter 4: рост возобновился
        ["5", "6"],                    # iter 5: 6
    ])
    got = _collect_links(sess, limit=100, scan_limit=100, max_scrolls=10)
    assert [g["pid"] for g in got] == ["1", "2", "3", "4", "5", "6"]


def test_collect_links_first_quiet_iteration_waits_longer():
    """Первая тихая итерация — двойная пауза (ленивая догрузка), дальше обычная."""
    sess = _ScriptedSess([["1"], ["1"], ["1"], ["1"], ["1"], ["1"], ["1"]])
    _collect_links(sess, limit=100, scan_limit=100, max_scrolls=10,
                   wait_ms=1000, quiet_wait_ms=2500)
    assert 2500 in sess.waits, sess.waits
    assert sess.waits[-1] == 1000, sess.waits


def test_collect_links_stops_after_three_quiet_iterations():
    """Три тихие подряд — правда конец: выход, товары не теряются."""
    sess = _ScriptedSess([["1"], ["1"], ["1"], ["1"], ["1"], ["1"], ["1"],
                         ["1"]])
    got = _collect_links(sess, limit=100, scan_limit=100, max_scrolls=10)
    assert [g["pid"] for g in got] == ["1"]


def test_collect_links_logs_exit_reason(monkeypatch):
    """Причина выхода в логе обязательна: иначе «вышли рано» неотличимо от
    «выдача кончилась», и повторяющийся недобор не виден (жалоба пользователя).

    Логи проекта своя обёртка (ozon_collector.log пишет в stdout/файл, а не
    через logging), поэтому подменяем log.info, а не caplog."""
    import ozon_collector.log as _log
    lines = []
    monkeypatch.setattr(_log, "info", lambda msg, extra=None: lines.append(msg))
    sess = _ScriptedSess([["1"], ["1"], ["1"], ["1"], ["1"], ["1"], ["1"],
                         ["1"]])
    _collect_links(sess, limit=100, scan_limit=100, max_scrolls=10)
    text = "\n".join(lines)
    assert "причина выхода" in text, text
    assert "quiet_after" in text, text


def test_collect_links_logs_scan_limit_exit_reason(monkeypatch):
    import ozon_collector.log as _log
    lines = []
    monkeypatch.setattr(_log, "info", lambda msg, extra=None: lines.append(msg))
    sess = _ScriptedSess([["1", "2"], ["3", "4"], ["5", "6"]])
    _collect_links(sess, limit=6, scan_limit=6, max_scrolls=10)
    text = "\n".join(lines)
    assert "scan_limit" in text, text


def test_collect_links_with_recovery_stop_blocks_retry(monkeypatch):
    """stop_event между попытками recovery → НЕ запускаем следующий recycle и
    не повторяем goto: пробрасываем CollectionInterrupted (не endless retry)."""
    import threading as _t
    import ozon_collector.search as _S
    stop = _t.Event()
    stop.set()                       # стоп активен до входа в контур recovery

    class _Sess:
        def goto(self, url, wait_ms=None):
            pass
        @property
        def page(self):
            return None

    recycled = []
    monkeypatch.setattr(_S, "_recycle_browser",
                        lambda sess, manager, la: recycled.append(la))
    try:
        _collect_links_with_recovery(_Sess(), manager=None, url="u",
                                     limit=5, max_recycles=2,
                                     recovery={"events": []},
                                     stop_event=stop)
    except CollectionInterrupted as exc:
        assert not exc.forced
    else:
        raise AssertionError("ожидали CollectionInterrupted (graceful)")
    assert recycled == []            # повторная recovery НЕ запускалась


# ---- ревизия 2.9 P0: невалидный URL не доходит до Page.goto -----------------

def test_collect_search_rejects_junk_url():
    """Мусорный URL = ValueError ДО warmup/goto, а не 'goto warn' + 0 товаров.

    Реальный баг 2026-09-26: [текст](url) → Page.goto 'invalid URL' →
    выдача 0 → run NORMAL_DONE discovered=0 (тихая потеря суток).
    """
    import pytest
    sess = _FakeSess([{"pid": "1", "url": "u1"}])
    with pytest.raises(ValueError):
        collect_search(sess, "ozon категория шумы", limit=1, gather_cfg={})


def test_collect_search_repairs_markdown_url(monkeypatch):
    """Markdown-ссылка чинится, и canary получает ЧИСТЫЙ URL (rev 2.8 §10)."""
    import ozon_collector.search as _S
    got = {}
    monkeypatch.setattr(_S, "_warmup_to_category",
                        lambda s, u, recovery=None: got.setdefault("url", u))
    monkeypatch.setattr(_S, "_collect_links_with_recovery",
                        lambda s, m, u, lim, mr, rec: (got.update(links=u)
                                                       or []))
    sess = _FakeSess([{"pid": "1", "url": "u1"}])
    c = collect_search(sess,
                       "[кат](https://www.ozon.ru/category/j-10/?x=1)",
                       limit=1, gather_cfg={})
    assert got["url"] == "https://www.ozon.ru/category/j-10/?x=1"
    assert got["links"] == "https://www.ozon.ru/category/j-10/?x=1"
    assert c["search"]["url"] == "https://www.ozon.ru/category/j-10/?x=1"
    assert c["recovery"]["canary_url"].startswith("https://www.ozon.ru/category/")


def test_search_marks_grid_empty_flag(monkeypatch):
    """Пустая сетка помечает recovery.search_grid_empty (демон → EMPTY_SEARCH)."""
    import ozon_collector.search as _S
    links = [{"pid": "1", "url": "https://www.ozon.ru/product/1/"}]
    sess = _FakeSess(links)
    monkeypatch.setenv("OZON_ACTIVE_WARMUP", "0")
    monkeypatch.setattr(_S, "_feed_appeared", lambda s: False)
    monkeypatch.setattr(_S.C, "detect_challenge", lambda p: None)
    c = _S.collect_search(sess, "https://www.ozon.ru/category/z-1/",
                          limit=1, gather_cfg={})
    assert c["found"] == 0
    assert c["recovery"]["search_grid_empty"] is True
