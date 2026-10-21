"""Тесты экстрактора REVIEWS_JS на сохранённой странице отзывов Ozon.

Фикстуры tests/review_pages/*.html.gz — живые снимки страницы отзывов
(?tab=reviews + прокрутка до появления карточек), снятые браузером по CDP:
  xiaomi          — режим «Все отзывы» (смешанная лента);
  xiaomi_variant  — активен нативный фильтр «Этот вариант товара» (2697).
Отзывы — самая динамичная часть Ozon, поэтому контракт проверяется отдельно
от PRODUCT_JS (tests/test_extractors.py).
"""
from __future__ import annotations

import json
import re

import pytest

from ozon_collector.collect import _activate_variant_mode  # noqa: E402
from ozon_collector.collect import EXPAND_REVIEWS_JS, REVIEWS_JS  # noqa: E402
import ozon_collector.collect as collect_mod


def test_reviews_tab_url_does_not_duplicate_query():
    url = collect_mod._reviews_tab_url(
        None, "https://www.ozon.ru/product/123/",
        "https://www.ozon.ru/product/test-123/?at=abc", "123")
    assert url == "https://www.ozon.ru/product/test-123/?at=abc&tab=reviews"


def test_reviews_tab_url_rejects_foreign_pid():
    with pytest.raises(RuntimeError, match="PID mismatch"):
        collect_mod._reviews_tab_url(
            None, "https://www.ozon.ru/product/123/",
            "https://www.ozon.ru/product/test-999/", "123")


def test_reviews_tab_url_accepts_relative_path():
    url = collect_mod._reviews_tab_url(
        None, "https://www.ozon.ru/product/123/",
        "/product/test-123/?at=abc", "123")
    assert url == "https://www.ozon.ru/product/test-123/?at=abc&tab=reviews"


def test_reviews_tab_url_rejects_foreign_host_before_navigation():
    class _Page:
        def goto(self, *a, **k):
            raise AssertionError("must not navigate to a foreign host")

    with pytest.raises(RuntimeError, match="origin mismatch"):
        collect_mod._reviews_tab_url(
            _Page(), "https://www.ozon.ru/product/123/",
            "https://evil.example/product/123/reviews/", "123")


def test_reviews_tab_url_rejects_pid_with_extra_path_segments():
    with pytest.raises(RuntimeError, match="PID mismatch"):
        collect_mod._reviews_tab_url(
            None, "https://www.ozon.ru/product/123/",
            "https://www.ozon.ru/product/456/reviews/", "123")


def test_reviews_tab_url_rejects_short_foreign_pid_before_navigation():
    class _Page:
        def goto(self, *a, **k):
            raise AssertionError("must not navigate to a foreign short URL")

    with pytest.raises(RuntimeError, match="PID mismatch"):
        collect_mod._reviews_tab_url(
            _Page(), "https://www.ozon.ru/product/123/",
            "https://www.ozon.ru/product/456", "123")


def test_review_extract_live_markup(evaluate_reviews):
    d = evaluate_reviews(name="xiaomi")
    assert d["ok"] is True
    assert d["totalText"]
    reviews = d["reviews"]
    assert len(reviews) > 0
    for r in reviews:
        assert r.get("uuid"), f"uuid отсутствует: {r}"
        assert r.get("text"), f"text пуст: {r.get('uuid')}"
        assert isinstance(r.get("stars"), int), f"stars не число: {r.get('uuid')}"
        assert 1 <= r["stars"] <= 5, f"stars вне диапазона: {r}"
        assert "variant" in r, f"нет variant: {r.get('uuid')}"
        assert r.get("author"), f"нет author: {r.get('uuid')}"
        assert r.get("date"), f"нет date: {r.get('uuid')}"


def test_reviews_fields_are_strings(evaluate_reviews):
    d = evaluate_reviews(name="xiaomi")
    for r in d["reviews"]:
        assert isinstance(r["author"], str)
        assert isinstance(r["text"], str)
        assert isinstance(r["date"], str)
        assert isinstance(r["variant"], str)


def test_empty_review_page_no_crash(evaluate_reviews):
    d = evaluate_reviews(html="<html><body><p>отзывов нет</p></body></html>")
    assert d["ok"] is False
    assert d["reviews"] == []
    assert d["pages"] == 1


# ---- VARIANT_MODE_JS: детектор нативного фильтра «Этот вариант товара» ------

def test_variant_mode_detector_live_markup(evaluate_variant_mode):
    d = evaluate_variant_mode(name="xiaomi_variant")
    assert d["found"] is True
    assert d["variant_present"] is True
    assert d["active"] == "Этот вариант товара"
    assert isinstance(d["variant_count"], int) and d["variant_count"] > 0
    assert d["total_count"] is None or d["total_count"] >= d["variant_count"]
    assert any("Все отзывы" in o for o in d["options"]), d["options"]
    assert any(o.startswith("Этот вариант товара") for o in d["options"]), d["options"]


def test_variant_mode_detector_all_reviews_mode(evaluate_variant_mode):
    d = evaluate_variant_mode(name="xiaomi")
    assert d["found"] is True
    assert d["variant_present"] is True
    assert d["active"] == "Все отзывы"
    assert isinstance(d["variant_count"], int) and d["variant_count"] > 0


def test_variant_mode_detector_no_toggle(evaluate_variant_mode):
    d = evaluate_variant_mode(html="<html><body><p>отзывов нет</p></body></html>")
    assert d["found"] is False
    assert d["variant_present"] is False
    assert d["active"] is None


def test_variant_mode_detector_no_variant_option(evaluate_variant_mode):
    d = evaluate_variant_mode(
        html='<html><body><div data-widget="webReviewsVariantMode">'
             '<div tabindex="0">Все отзывы</div></div></body></html>'
    )
    assert d["found"] is True
    assert d["variant_present"] is False
    assert d["variant_count"] is None


# ---- _activate_variant_mode: поведение включения фильтра --------------------

def test_activate_skips_click_when_already_active(variant_mode_page):
    page = variant_mode_page("xiaomi_variant")
    vm = _activate_variant_mode(page)
    assert vm["used"] is True
    assert vm["clicked"] is False
    assert vm["reason"] == "already_active"
    assert vm["variant_present"] is True
    assert isinstance(vm["variant_count"], int)


def test_activate_graceful_fallback_on_static_dom(variant_mode_page):
    """В статичном DOM без React клик не меняет активность — сбор не должен
    падать: used=False (фильтр фактически не активен), reason=click_no_effect."""
    page = variant_mode_page("xiaomi")
    vm = _activate_variant_mode(page)
    assert vm["used"] is False
    assert vm["clicked"] is True
    assert vm["reason"] in ("click_no_effect", "click_ok")
    assert vm["variant_present"] is True


def test_activate_no_toggle_returns_used_false(variant_mode_page):
    page = variant_mode_page("xiaomi")
    # временно скрываем виджет, имитируя товар без тумблера
    page.evaluate(
        """() => {
          const w = document.querySelector('[data-widget="webReviewsVariantMode"]');
          if (w) w.remove();
        }"""
    )
    vm = _activate_variant_mode(page)
    assert vm["used"] is False
    assert vm["clicked"] is False
    assert vm["reason"] == "no_widget"


class _VariantModeFakePage:
    """Страница с управляемым VARIANT_MODE_JS для ветвей _activate_variant_mode:
    initial — первый снапшот, on_click — эффект клика по опции, evaluate_errors —
    какие evaluate бросать (для веток warn/click_error)."""

    def __init__(self, initial, url="https://www.ozon.ru/product/x-123/?tab=reviews",
                 on_click=None, evaluate_errors=None):
        self.snap = initial
        self.url = url
        self.on_click = on_click
        self.evaluate_errors = evaluate_errors or {}
        self.click_calls = 0

    def evaluate(self, js, *args, **kwargs):
        for probe, exc in self.evaluate_errors.items():
            if probe in str(js):
                raise exc
        if js is collect_mod.VARIANT_MODE_JS:
            return self.snap
        self.click_calls += 1
        if self.on_click:
            self.on_click(self)
        return None

    def wait_for_timeout(self, ms):
        pass


_SNAP_WITH_OPTION = {
    "found": True, "variant_present": True, "variant_count": 5,
    "active": "Все отзывы", "total_count": 100,
}


def test_activate_url_param_without_dom_signal_clicks():
    """URL-параметр без подтверждения DOM — не доказательство применения
    фильтра: виджет показывает активными «Все отзывы», клик обязателен
    (ревью 2026-10-15; раньше used=True по строке URL обходил I1)."""
    def _apply(self):
        self.snap = {**self.snap, "active": "Этот вариант товара"}

    page = _VariantModeFakePage(
        _SNAP_WITH_OPTION,
        url="https://www.ozon.ru/product/x-123/?tab=reviews&reviewsVariantMode=1",
        on_click=_apply,
    )
    vm = _activate_variant_mode(page)
    assert vm["used"] is True
    assert vm["clicked"] is True
    assert vm["reason"] == "click_ok"
    assert page.click_calls == 1


def test_activate_url_param_conflict_with_dom_fails_closed():
    """URL-параметр есть, DOM показывает другой активный фильтр, клик не
    сработал → used=False; слепо доверять URL нельзя."""
    page = _VariantModeFakePage(
        _SNAP_WITH_OPTION,
        url="https://www.ozon.ru/product/x-123/?tab=reviews&reviewsVariantMode=1",
    )
    vm = _activate_variant_mode(page, timeout_s=0.2)
    assert vm["used"] is False
    assert vm["reason"] == "click_no_effect"


def test_activate_click_ok():
    """Клик по опции применился (active сменился) → click_ok, used=True."""
    def _apply(self):
        self.snap = {**self.snap, "active": "Этот вариант товара"}

    page = _VariantModeFakePage(_SNAP_WITH_OPTION, on_click=_apply)
    vm = _activate_variant_mode(page, timeout_s=1.0)
    assert vm["used"] is True
    assert vm["clicked"] is True
    assert vm["reason"] == "click_ok"
    assert page.click_calls == 1
    assert vm["variant_count"] == 5


def test_activate_click_no_effect():
    """Клик не применился (активность не сменилась) → click_no_effect, used=False,
    сбор продолжается без нативного фильтра."""
    page = _VariantModeFakePage(_SNAP_WITH_OPTION)
    vm = _activate_variant_mode(page, timeout_s=0.2)
    assert vm["used"] is False
    assert vm["clicked"] is True
    assert vm["reason"] == "click_no_effect"


def test_activate_click_error():
    """Ошибка на клике → click_error (used=False), прогон не падает."""
    page = _VariantModeFakePage(
        _SNAP_WITH_OPTION,
        evaluate_errors={"target) target.click": RuntimeError("click boom")},
    )
    vm = _activate_variant_mode(page)
    assert vm["used"] is False
    assert vm["clicked"] is True
    assert vm["reason"] == "click_error"


def test_activate_evaluate_warn_falls_back_to_no_widget():
    """Ошибка на первом evaluate VARIANT_MODE_JS (не renderer) → warn и no_widget."""
    page = _VariantModeFakePage(
        _SNAP_WITH_OPTION,
        evaluate_errors={"const totalEl = document.querySelector": RuntimeError("boom")},
    )
    vm = _activate_variant_mode(page)
    assert vm["reason"] == "no_widget"
    assert vm["used"] is False


# ---- Вариантная лента однородна (режим «Этот вариант товара») ---------------

def test_variant_scoped_reviews_uniform(evaluate_reviews):
    """В режиме «Этот вариант товара» лента однородна по первому аспекту
    (цвету); под-аспекты (Качество звука и т.п.) могут различаться."""
    d = evaluate_reviews(name="xiaomi_variant")
    reviews = d["reviews"]
    assert len(reviews) > 0
    first_aspects = {
        (r.get("variant", "") or "").splitlines()[0].strip()
        for r in reviews
    }
    assert first_aspects, "нет variant-полей"
    assert len(first_aspects) == 1, f"лента не однородна: {first_aspects}"
    assert "черный" in next(iter(first_aspects))


# ---- Guard: нативный фильтр активен, но отзывы варианта не собраны ----------

class _FakePage:
    """Минимальная страница для collect_reviews: REVIEWS_JS отдаёт заданные данные,
    остальные методы — noop (активатор/скролл/пагинация замоканы в тестах)."""

    def __init__(self, reviews_data):
        self.reviews_data = reviews_data
        self.url = "https://www.ozon.ru/product/test-123/?tab=reviews"

    def goto(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js, *args, **kwargs):
        if "location.href" in js:
            # _CHALLENGE_LOOKS_JS: страница с отзывами отрисована →
            # «не похоже на челлендж» (иначе check_challenge ждёт переспросы).
            return False
        return self.reviews_data


class TestCanonicalProductUrl:
    """Короткий /product/{pid} не гидратирует отзывы — нужен canonical со slug."""

    class _Page:
        def __init__(self, canon):
            self.canon = canon
            self.visited = []
            self.url = "https://www.ozon.ru/product/1590289080"

        def evaluate(self, js, *args, **kwargs):
            assert "canonical" in js
            return self.canon

        def goto(self, url, **kwargs):
            self.visited.append(url)
            self.url = url

    def test_short_url_goes_to_canonical(self):
        page = self._Page("https://www.ozon.ru/product/porolon-1590289080/")
        out = collect_mod._canonical_product_url(
            page, "https://www.ozon.ru/product/1590289080")
        assert out.endswith("/product/porolon-1590289080/")
        assert page.visited == [out]

    def test_slug_url_left_untouched(self):
        url = "https://www.ozon.ru/product/porolon-1590289080/?tab=reviews"
        page = self._Page("https://www.ozon.ru/product/other-1/")
        assert collect_mod._canonical_product_url(page, url) == url
        assert page.visited == []

    def test_missing_canonical_keeps_url(self):
        url = "https://www.ozon.ru/product/1590289080"
        page = self._Page(None)
        assert collect_mod._canonical_product_url(page, url) == url
        assert page.visited == []

    def test_evaluate_error_keeps_url(self):
        url = "https://www.ozon.ru/product/1590289080"

        class _Boom(self._Page):
            def evaluate(self, js, *args, **kwargs):
                raise RuntimeError("detached")

        page = _Boom(None)
        assert collect_mod._canonical_product_url(page, url) == url
        assert page.visited == []

    def test_goto_error_keeps_original_url(self):
        class _Fail(self._Page):
            def goto(self, url, **kwargs):
                raise RuntimeError("net error")

        page = _Fail("https://www.ozon.ru/product/porolon-1590289080/")
        url = "https://www.ozon.ru/product/1590289080"
        assert collect_mod._canonical_product_url(page, url) == url


def _patch_variant_collect(monkeypatch, used=True, reason="click_ok"):
    """Мокает активатор (used=used), скролл и пагинацию для collect_reviews."""
    monkeypatch.setattr(
        collect_mod, "_activate_variant_mode",
        lambda page: {
            "used": used, "clicked": True, "variant_present": True,
            "variant_count": 5, "active": "Этот вариант товара",
            "total_count": 100, "reason": reason,
        },
    )
    monkeypatch.setattr(
        collect_mod, "_scroll_to_converge",
        lambda page, target_min=40, telemetry=None: (5, False),
    )
    monkeypatch.setattr(
        collect_mod, "_detect_pagination",
        lambda page, grew=True: "next_button",
    )
    monkeypatch.setattr(collect_mod, "_next_page", lambda page: None)


def test_collect_warns_when_native_active_but_zero_matches(monkeypatch, capsys):
    """Нативный фильтр активен (used=True), но ни один отзыв не совпал с
    вариантом — должен появиться warning (в stdout и variant_mode), без падения."""
    _patch_variant_collect(monkeypatch, used=True)
    page = _FakePage({
        "ok": True,
        "reviews": [{
            "uuid": "u1", "text": "Отзыв", "stars": 5,
            "variant": "Цвет товара: черный", "author": "A", "date": "1 авг",
        }],
    })
    reviews, _, _, vm = collect_mod.collect_reviews(
        page, page.url, {"Цвет": "белый"}, min_reviews=1,
    )
    assert reviews == []
    assert vm["used"] is True
    assert vm["reviews_matched_variant"] == 0
    assert vm["reviews_dropped_by_client_filter"] == 0
    assert vm["pagination_type"] == "next_button"
    assert vm["warning"] == "native_filter_active_no_matching_reviews"
    assert "native_filter_active_no_matching_reviews" in capsys.readouterr().out


def test_collect_no_warning_when_native_active_and_matches(monkeypatch, capsys):
    """Нативный фильтр активен и отзыв совпал с вариантом — warning отсутствует
    (регресс штатного v1.5 сценария)."""
    _patch_variant_collect(monkeypatch, used=True)
    page = _FakePage({
        "ok": True,
        "reviews": [{
            "uuid": "u1", "text": "Отзыв", "stars": 5,
            "variant": "Цвет товара: белый", "author": "A", "date": "1 авг",
        }],
    })
    reviews, _, _, vm = collect_mod.collect_reviews(
        page, page.url, {"Цвет": "белый"}, min_reviews=1,
    )
    assert len(reviews) == 1
    assert vm["reviews_matched_variant"] == 1
    assert vm["reviews_dropped_by_client_filter"] == 0
    assert vm["pagination_type"] == "next_button"
    assert "warning" not in vm
    assert "native_filter_active_no_matching_reviews" not in capsys.readouterr().out


def test_collect_writes_pagination_type_when_variant_mode_disabled(monkeypatch):
    """apply_variant_mode=False: pagination_type всё равно пишется в variant_mode."""
    _patch_variant_collect(monkeypatch, used=True)
    page = _FakePage({
        "ok": True,
        "reviews": [{
            "uuid": "u1", "text": "Отзыв", "stars": 5,
            "variant": "Цвет товара: белый", "author": "A", "date": "1 авг",
        }],
    })
    _, _, _, vm = collect_mod.collect_reviews(
        page, page.url, {}, min_reviews=1, apply_variant_mode=False,
    )
    assert vm["used"] is False
    assert vm["reason"] == "disabled"
    assert vm["pagination_type"] == "next_button"


# ---- Pagination-агностичный сбор (новые схемы разметки Ozon) ----------------

def test_review_extract_without_webListReviews(evaluate_reviews):
    """REVIEWS_JS извлекает отзывы по [data-review-uuid] даже без виджета
    webListReviews (новая разметка state-paginator / infinite scroll)."""
    d = evaluate_reviews(
        html='''<html><body>
          <div data-review-uuid="u1" publishedat="2026-01-15">
            <span class="tsCompactControl500Medium">Иван</span>
            <div>5 января 2026</div>
            <div>Хороший матрас, спим отлично</div>
            <svg class="a9"></svg><svg class="a9"></svg><svg class="a9"></svg>
            <svg class="a9"></svg><svg class="a9"></svg>
          </div>
        </body></html>'''
    )
    assert d["ok"] is True
    assert len(d["reviews"]) == 1
    r = d["reviews"][0]
    assert r["uuid"] == "u1"
    assert r["text"]
    assert r["stars"] == 5
    assert r["date"] == "5 января 2026"
    assert d["pages"] == 1
    assert d["totalText"] is None


def test_review_extract_stale_marker(evaluate_reviews):
    """REVIEWS_JS устанавливает stale=True/False по бейджу «Отзыв устарел»
    (DOM-маркер Ozon; классы хэшированы — ищем по title/тексту)."""
    html = '''<html><body>
      <div data-review-uuid="stale1" publishedat="2026-01-15">
        <span class="tsCompactControl500Medium">Людмила Л.</span>
        <div>8 июля 2026</div>
        <div title="Отзыв устарел" class="b5_7_3-a4 tsBodyControl300XSmall">Отзыв устарел</div>
        <div>Комфорт при использовании:Неудобно
          Старый отзыв, больше не актуален</div>
        <svg class="a9"></svg><svg class="a9"></svg><svg class="a9"></svg>
      </div>
      <div data-review-uuid="ok1" publishedat="2026-01-16">
        <span class="tsCompactControl500Medium">Иван</span>
        <div>9 января 2026</div>
        <div>Хороший товар</div>
        <svg class="a9"></svg><svg class="a9"></svg><svg class="a9"></svg>
        <svg class="a9"></svg><svg class="a9"></svg>
      </div>
    </body></html>'''
    d = evaluate_reviews(html=html)
    by_uuid = {r["uuid"]: r for r in d["reviews"]}
    assert by_uuid["stale1"]["stale"] is True
    assert by_uuid["ok1"]["stale"] is False


def _open_html(browser, html):
    page = browser.new_page()
    page.route("**/*", lambda route: route.abort())
    page.set_content(html, wait_until="domcontentloaded")
    return page


def test_next_page_state_paginator_scroll(browser):
    """fetchType=scroll (infinite scroll) → _next_page возвращает None:
    подгрузка обрабатывается _scroll_to_converge, а не переходом по ссылке."""
    html = ('<html><body><div id="state-paginator" '
            'data-state=\'{"nextPage":"/reviews/?layout_page_index=2","fetchType":"scroll","size":10}\'>'
            '</div></body></html>')
    page = _open_html(browser, html)
    try:
        assert collect_mod._next_page(page) is None
    finally:
        page.close()


def test_next_page_state_paginator_nextpage(browser):
    """state-paginator c fetchType != scroll → возвращает URL nextPage."""
    html = ('<html><body><div id="state-paginator" '
            'data-state=\'{"nextPage":"/reviews/?layout_page_index=2","fetchType":"page","size":10}\'>'
            '</div></body></html>')
    page = _open_html(browser, html)
    try:
        assert collect_mod._next_page(page) == \
            "https://www.ozon.ru/reviews/?layout_page_index=2"
    finally:
        page.close()


def test_next_page_legacy_dalshe(browser):
    """Старая разметка webListReviews (кнопка «Дальше») продолжает работать."""
    html = ('<html><body><div data-widget="webListReviews">'
            '<div></div><div></div>'
            '<div><a href="/reviews/?page=2">Дальше</a></div>'
            '</div></body></html>')
    page = _open_html(browser, html)
    try:
        assert collect_mod._next_page(page) == "https://www.ozon.ru/reviews/?page=2"
    finally:
        page.close()


def test_next_url_no_click_detects_without_side_effect(browser):
    """click=False только ОБНАРУЖИВАЕТ кнопочную пагинацию: '__CLICK__' без клика.

    Регрессия ревью 2026-10-15: старая детекция кликала «Дальше» как побочный
    эффект, страница уезжала на стр.2 до извлечения стр.1."""
    html = ('<html><body><div data-widget="webListReviews">'
            '<div></div><div></div>'
            '<div><button>Дальше</button></div>'
            '</div></body></html>')
    page = _open_html(browser, html)
    try:
        assert collect_mod.next_url(page, click=False) == "__CLICK__"
        assert page.url.startswith("about:")  # клика не было, навигации нет
    finally:
        page.close()


def test_next_page_click_in_place_returns_sentinel(browser):
    """Кнопочная пагинация: клик не меняет URL → сентинел '__IN_PLACE__'."""
    html = ('<html><body><div data-widget="webListReviews">'
            '<div></div><div></div>'
            '<div><button>Дальше</button></div>'
            '</div></body></html>')
    page = _open_html(browser, html)
    try:
        assert collect_mod._next_page(page) == "__IN_PLACE__"
    finally:
        page.close()


def test_next_page_fallback_layout_page_index(browser):
    """Нет ни state-paginator, ни webListReviews → fallback по layout_page_index."""
    html = '<html><body><a href="/reviews/?layout_page_index=3">3</a></body></html>'
    page = _open_html(browser, html)
    try:
        assert collect_mod._next_page(page) == \
            "https://www.ozon.ru/reviews/?layout_page_index=3"
    finally:
        page.close()


def _scroll_converge_page(cap):
    class _P:
        def __init__(self):
            self.n = 0
            self.scrolls = 0

        def evaluate(self, js, *args, **kwargs):
            if "data-review-uuid" in js:
                self.n += 1
                return [f"id{i}" for i in range(min(self.n, cap))]
            if "scrollBy" in js or "scrollTo" in js:
                self.scrolls += 1
                return None
            return None

        def wait_for_timeout(self, ms):
            pass

    return _P()


def test_scroll_to_converge_plateaus():
    """Прокрутка останавливается по плато (MAX_SCROLL_STABLE) и возвращает
    итоговое число уникальных отзывов без возобновления роста."""
    page = _scroll_converge_page(cap=8)
    count, resumed = collect_mod._scroll_to_converge(page, target_min=40)
    assert count == 8
    assert resumed is False


def test_scroll_to_converge_raises_on_timeout():
    """Зависший renderer (TimeoutError на evaluate) пробрасывается наверх —
    чтобы сработал recovery, а не молча собранный пустой товар."""
    from playwright.sync_api import TimeoutError
    page = _scroll_converge_page(cap=5)

    def _boom(js, *args, **kwargs):
        raise TimeoutError("Timeout 15000ms exceeded")

    page.evaluate = _boom
    with pytest.raises(TimeoutError):
        collect_mod._scroll_to_converge(page, target_min=40)


def test_scroll_to_converge_raises_on_frame_detached():
    """OOM-краш вкладки (frame was detached) пробрасывается наверх."""
    from playwright.sync_api import Error as PWError
    page = _scroll_converge_page(cap=5)

    def _boom(js, *args, **kwargs):
        raise PWError("net::ERR_ABORTED: The frame was detached from the DOM")

    page.evaluate = _boom
    with pytest.raises(PWError):
        collect_mod._scroll_to_converge(page, target_min=40)


def test_scroll_to_converge_swallows_plain_error():
    """Обычная (не renderer) ошибка evaluate не роняет прокрутку —
    прежнее поведение: сход по плато с 0 отзывов."""
    page = _scroll_converge_page(cap=5)

    def _boom(js, *args, **kwargs):
        raise ValueError("bad js")

    page.evaluate = _boom
    count, resumed = collect_mod._scroll_to_converge(page, target_min=40)
    assert count == 0
    assert resumed is False


def test_dedupe_reviews_uuid_and_fallback():
    """Дедуп по uuid; у отзывов без uuid — sha1(author+date+text[:120]),
    ключ не попадает в отзыв."""
    r1 = {"uuid": "u1", "author": "A", "date": "1 января 2026", "text": "x"}
    noid = {"author": "Б", "date": "2 января 2026", "text": "длинный текст " * 30}
    seen = set()
    out = collect_mod._dedupe_reviews([r1, dict(r1), noid, dict(noid)], seen)
    assert len(out) == 2
    assert out[0]["uuid"] == "u1"
    assert "uuid" not in out[1]
    assert len(seen) == 2


def test_detect_pagination_modes():
    class _P:
        def __init__(self, sp, legacy):
            self.sp = sp
            self.legacy = legacy

        def evaluate(self, js, *args, **kwargs):
            if "state-paginator" in js:
                return self.sp
            if "webListReviews" in js:
                return self.legacy
            return None

    assert collect_mod._detect_pagination(_P("scroll", None)) == "infinite_scroll"
    assert collect_mod._detect_pagination(_P("page", None)) == "next_button"
    assert collect_mod._detect_pagination(_P(None, "/reviews/?page=2")) == "next_button"
    assert collect_mod._detect_pagination(_P(None, None), grew=True) == "converged"
    assert collect_mod._detect_pagination(_P(None, None), grew=False) == "unknown"


# ---- EXPAND_REVIEWS_JS: раскрытие «Читать полностью» ------------------------

def test_expand_reviews_js_clicks_read_more(browser):
    """Клик по кнопке «Читать полностью» дораскрывает текст отзыва; без клика
    innerText усечён."""
    html = '''<html><body>
      <div data-review-uuid="u1" publishedat="2026-01-15">
        <span class="tsCompactControl500Medium">Иван</span>
        <div>5 января 2026</div>
        <div id="body1">
          Хороший матрас, спим отлично
          <button id="readmore1"
            onclick="document.getElementById('hidden1').style.display='block'; this.remove();">
            Читать полностью
          </button>
          <div id="hidden1" style="display:none"> Это точно тот матрас который я искал очень долго.</div>
        </div>
      </div>
      <div data-review-uuid="u2" publishedat="2026-01-16">
        <span class="tsCompactControl500Medium">Пётр</span>
        <div>6 января 2026</div>
        <div id="body2">
          Отзыв без кнопки
        </div>
      </div>
    </body></html>'''
    page = _open_html(browser, html)
    try:
        before = page.evaluate(REVIEWS_JS)
        r1 = next(r for r in before["reviews"] if r["uuid"] == "u1")
        assert "очень долго" not in r1["text"]

        n = page.evaluate(EXPAND_REVIEWS_JS)
        assert n == 1

        after = page.evaluate(REVIEWS_JS)
        r1 = next(r for r in after["reviews"] if r["uuid"] == "u1")
        assert "очень долго" in r1["text"]
        assert "Читать полностью" not in r1["text"]
        r2 = next(r for r in after["reviews"] if r["uuid"] == "u2")
        assert r2["text"] == "Отзыв без кнопки"
    finally:
        page.close()


def test_expand_reviews_js_strips_toggle_after_click(browser):
    """Текст, раскрытый после клика, не содержит хвостовой кнопки «Свернуть»."""
    html = '''<html><body>
      <div data-review-uuid="u1" publishedat="2026-01-15">
        <span class="tsCompactControl500Medium">Иван</span>
        <div>5 января 2026</div>
        <div>
          <button id="rm" onclick="this.closest('[data-review-uuid]').querySelector('#full').style.display='block'; this.remove()">
            Читать полностью
          </button>
          <div id="full" style="display:none">Полный текст отзыва после раскрытия.</div>
        </div>
      </div>
    </body></html>'''
    page = _open_html(browser, html)
    try:
        n = page.evaluate(EXPAND_REVIEWS_JS)
        assert n == 1
        after = page.evaluate(REVIEWS_JS)
        r = after["reviews"][0]
        assert "Полный текст отзыва после раскрытия." in r["text"]
        assert "Читать полностью" not in r["text"]
    finally:
        page.close()


# ---- EXPAND_COMMENTS_JS + комментарии к отзывам -----------------------------

_COMMENT_HTML = '''<html><body>
  <div class="lk0_25">
    <div data-review-uuid="rv1" publishedat="1772478395" class="jn0_25">
      <span class="tsCompactControl500Medium">Автор</span>
      <div class="mj3_25">1 января 2026</div>
      <a href="/product/smazka-1/">Объем, мл: 100, Название цвета: Белый</a>
      <div class="m4j_25">
        <div class="mj5_25">Отличный товар, но хотелось бы больше информации</div>
      </div>
      <div>Вам помог этот отзыв?</div>
      <button><div class="b25_9_0-a2 tsBodyControl400Small">Да 5</div></button>
      <button><div class="b25_9_0-a2 tsBodyControl400Small">Нет 2</div></button>
      <button id="cmbtn"><div class="tsBodyControl400Small">2 комментария</div></button>
    </div>
    <div class="k1l_25" id="comments1">
      <div data-comment-uuid="cm1" class="rpProduct_ac3">
        <div class="i7z_25">
          <span class="tsCompactControl500Medium">Продавец</span>
          <a class="rpProduct_c4a">в ответ Автор</a>
        </div>
        <div class="rpProduct_a4c">2 января 2026</div>
        <div class="jm_25"><span class="rpProduct_ca5">Благодарим за отзыв, подробности в личных сообщениях.</span></div>
        <button><div class="b25_9_0-a2 tsBodyControl400Small">3</div></button>
        <button><div class="b25_9_0-a2 tsBodyControl400Small">1</div></button>
      </div>
      <div data-comment-uuid="cm2" class="rpProduct_ac3">
        <div class="i7z_25">
          <span class="tsCompactControl500Medium">Покупатель</span>
        </div>
        <div class="rpProduct_a4c">3 января 2026</div>
        <div class="jm_25"><span class="rpProduct_ca5">Согласен с продавцом.</span></div>
        <button><div class="b25_9_0-a2 tsBodyControl400Small">0</div></button>
        <button><div class="b25_9_0-a2 tsBodyControl400Small">0</div></button>
      </div>
    </div>
  </div>
  <div class="lk0_25">
    <div data-review-uuid="rv2" publishedat="1772478396" class="jn0_25">
      <span class="tsCompactControl500Medium">Пётр</span>
      <div class="mj3_25">4 января 2026</div>
      <div class="m4j_25">
        <div class="mj5_25">Отзыв без комментариев</div>
      </div>
    </div>
  </div>
</body></html>'''


def test_reviews_parse_comments(evaluate_reviews):
    """Комментарии извлекаются из блока рядом с отзывом с полями uuid/автор/
    reply_to/дата/текст и голосованием."""
    d = evaluate_reviews(html=_COMMENT_HTML)
    assert d["ok"] is True
    rv1 = next(r for r in d["reviews"] if r["uuid"] == "rv1")
    assert rv1["helpful_yes"] == 5
    assert rv1["helpful_no"] == 2
    assert rv1["comments_count"] == 2
    comments = rv1["comments"]
    assert len(comments) == 2
    c1, c2 = comments
    assert c1["uuid"] == "cm1"
    assert c1["author"] == "Продавец"
    assert c1["reply_to"] == "Автор"
    assert c1["date"] == "2 января 2026"
    assert c1["text"].startswith("Благодарим за отзыв")
    assert c1["like"] == 3
    assert c1["dislike"] == 1
    assert c2["uuid"] == "cm2"
    assert c2["reply_to"] == ""
    assert c2["like"] == 0
    assert c2["dislike"] == 0
    rv2 = next(r for r in d["reviews"] if r["uuid"] == "rv2")
    assert rv2["comments"] == []


def test_review_text_excludes_comments(evaluate_reviews):
    """Текст отзыва не должен включать тексты комментариев (они вне [data-review-uuid])."""
    d = evaluate_reviews(html=_COMMENT_HTML)
    rv1 = next(r for r in d["reviews"] if r["uuid"] == "rv1")
    assert "Отличный товар, но хотелось бы больше информации" in rv1["text"]
    assert "Благодарим за отзыв" not in rv1["text"]
    assert "Согласен с продавцом" not in rv1["text"]


def test_expand_comments_trusted_clicks(browser):
    """_expand_review_comments раскрывает комментарии trusted-кликами:
    маркирует карточки JS-ом, затем кликает через Playwright locator (React
    игнорирует untrusted dispatchEvent). Кнопки «0 комментариев» не трогает."""
    html = '''<html><body>
      <div data-review-uuid="u1">
        <button id="cm2" onclick="document.getElementById('c2').style.display='block'; this.remove()">
          <div class="tsBodyControl400Small">2 комментария</div>
        </button>
        <div id="c2" style="display:none">два</div>
      </div>
      <div data-review-uuid="u2">
        <button><div class="tsBodyControl400Small">0 комментариев</div></button>
      </div>
    </body></html>'''
    page = _open_html(browser, html)
    try:
        n = collect_mod._expand_review_comments(page)
        assert n == 1
        assert page.locator("#c2").evaluate("el => el.style.display") == "block"
        # карточка u2 (0 комментариев) не помечена и не кликнута
        assert page.evaluate(
            "() => document.querySelector('[data-review-uuid=\"u2\"]')"
            ".hasAttribute('data-comments-expanded')"
        ) is False
    finally:
        page.close()


def test_is_sticky_intercept():
    """_is_sticky_intercept отличает перекрытие sticky-блоком/нестабильность
    элемента от прочих ошибок: сообщения «intercepts pointer events» и
    «element is not stable» должны давать True (это НЕ renderer_failure —
    сбор должен продолжаться, а не падать)."""
    assert collect_mod._is_sticky_intercept(
        Exception("Timeout 4000ms exceeded. ... subtree intercepts pointer events ...")
    ) is True
    assert collect_mod._is_sticky_intercept(
        Exception("Timeout 4000ms exceeded. ... element is not stable ...")
    ) is True
    assert collect_mod._is_sticky_intercept(
        Exception("Timeout 30000ms exceeded")
    ) is False
    assert collect_mod._is_sticky_intercept(
        Exception("Element is not attached to the DOM")
    ) is False


def test_expand_comments_sticky_fallback(browser):
    """Кнопка «N комментария» перекрыта fixed-оверлеем: trusted-клик Playwright
    таймаутит («intercepts pointer events»), fallback el.click() раскрывает
    комментарий, а сам прогон НЕ падает (не классифицируется как renderer_failure)."""
    html = '''<html><head><style>
      #overlay { position: fixed; inset: 0; z-index: 100; background: transparent; }
    </style></head><body>
      <div id="overlay"></div>
      <div data-review-uuid="u1">
        <button id="cm1" onclick="document.getElementById('c1').style.display='block'; this.remove()">
          <div class="tsBodyControl400Small">2 комментария</div>
        </button>
        <div id="c1" style="display:none">два</div>
      </div>
    </body></html>'''
    page = _open_html(browser, html)
    try:
        n = collect_mod._expand_review_comments(page)
        assert n == 1
        assert page.locator("#c1").evaluate("el => el.style.display") == "block"
        assert page.locator("#cm1").count() == 0
    finally:
        page.close()


def test_expand_comments_timeout_falls_back_to_js(browser, monkeypatch):
    """Trusted-клик Playwright таймаутит (не sticky, просто медленный рендер) —
    JS-fallback el.click() раскрывает комментарий, а прогон НЕ падает как
    renderer_failure (если сам JS-fallback жив)."""
    from playwright.sync_api import TimeoutError as PWTimeout
    calls = {"js_clicks": 0, "trusted": 0}
    marked = [1]

    class _FakeHandle:
        def evaluate(self, *a, **kw):
            calls["js_clicks"] += 1
            marked[0] = 0   # карточка помечена data-comments-clicked

        def scroll_into_view_if_needed(self, *a, **kw):
            pass

        def click(self, *a, **kw):
            calls["trusted"] += 1
            raise PWTimeout("Timeout 8000ms exceeded")

    class _FakeLocator:
        def __getattr__(self, name):
            return lambda *a, **kw: None

        @property
        def first(self):
            return _FakeLocator()

        def count(self):
            return marked[0]

        def element_handle(self):
            return _FakeHandle()

    class _FakePage:
        def locator(self, *a, **kw):
            return _FakeLocator()

        def evaluate(self, js, *a, **kw):
            if js is collect_mod.MARK_COMMENT_CARDS_JS:
                return marked[0]
            return 0

        def wait_for_timeout(self, ms):
            pass

    page = _FakePage()
    n = collect_mod._expand_review_comments(page)
    assert n == 1
    assert calls["trusted"] == 1
    assert calls["js_clicks"] >= 1


class _RecordingPage:
    """Страница, записывающая URL каждого goto; REVIEWS_JS отдаёт данные по URL.

    Отзывы могут различаться между сортировками: словарь reviews_by_url
    ключуется полным URL (с параметром sort). Прочие JS возвращают 0 — без
    раскрытия текстов/комментариев (активатор/скролл/пагинация замоканы)."""

    def __init__(self, reviews_by_url):
        self.reviews_by_url = reviews_by_url
        self.gotos = []
        self.url = "https://www.ozon.ru/product/test-123/?tab=reviews"

    def goto(self, url, *args, **kwargs):
        self.gotos.append(url)

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js, *args, **kwargs):
        if js is not collect_mod.REVIEWS_JS:
            return 0
        cur = self.gotos[-1] if self.gotos else self.url
        return self.reviews_by_url.get(cur, {"ok": True, "reviews": []})


def _rev(uuid, text):
    return {"uuid": uuid, "text": text, "stars": 5,
            "variant": "Цвет товара: белый", "author": "A", "date": "1 авг"}


def test_sort_url_unit():
    base = "https://www.ozon.ru/product/123/?tab=reviews"
    assert collect_mod._sort_url(base, "score_desc") == base + "&sort=score_desc"
    assert collect_mod._sort_url(base, "score_asc") == base + "&sort=score_asc"
    assert collect_mod._sort_url(base, None) == base
    assert collect_mod._sort_url(base + "&sort=score_asc", "score_desc") == base + "&sort=score_desc"
    assert collect_mod._sort_url(base + "&sort=score_desc", None) == base
    assert collect_mod._sort_url(base, "published_at_desc") == base + "&sort=published_at_desc"


def test_collect_reviews_multi_sort_urls_and_merge(monkeypatch):
    """3 сортировки: per-sort URL с параметром sort, отзывы объединяются,
    у каждого review_sort (первое появление) и review_sorts (все сортировки)."""
    _patch_variant_collect(monkeypatch, used=True)
    base = "https://www.ozon.ru/product/test-123/?tab=reviews"
    d_url = base + "&sort=score_desc"
    a_url = base + "&sort=score_asc"
    page = _RecordingPage({
        base: {"ok": True, "reviews": [_rev("u1", "общий")]},
        d_url: {"ok": True, "reviews": [_rev("u1", "общий")]},
        a_url: {"ok": True, "reviews": [_rev("u1", "общий")]},
    })
    reviews, _, page_log, vm = collect_mod.collect_reviews(
        page, page.url, {"Цвет": "белый"}, min_reviews=1,
        sorts=["published_at_desc", "score_desc", "score_asc"],
    )
    assert len(reviews) == 1
    r = reviews[0]
    assert r["review_sort"] == "published_at_desc"
    assert r["review_sorts"] == ["published_at_desc", "score_desc", "score_asc"]
    assert page.gotos == [base, d_url, a_url]
    assert len(page_log) == 3
    assert {e["sort"] for e in page_log} == {"published_at_desc", "score_desc", "score_asc"}
    assert all(e["ok"] is True for e in page_log)
    assert vm["used"] is True
    assert vm["reviews_matched_variant"] == 1
    assert vm["reviews_dropped_by_client_filter"] == 0


def test_collect_reviews_multi_sort_dedup_by_uuid(monkeypatch):
    """Отзыв, встречающийся в нескольких сортировках, — одна запись;
    сортировки, где его нет, не пишутся в review_sorts."""
    _patch_variant_collect(monkeypatch, used=True)
    base = "https://www.ozon.ru/product/test-123/?tab=reviews"
    d_url = base + "&sort=score_desc"
    a_url = base + "&sort=score_asc"
    page = _RecordingPage({
        base: {"ok": True, "reviews": [_rev("u1", "общий"), _rev("u2", "только дефолт")]},
        d_url: {"ok": True, "reviews": [_rev("u1", "общий"), _rev("u3", "только score_desc")]},
        a_url: {"ok": True, "reviews": [_rev("u4", "только score_asc")]},
    })
    reviews, *_ = collect_mod.collect_reviews(
        page, page.url, {"Цвет": "белый"}, min_reviews=1,
        sorts=["published_at_desc", "score_desc", "score_asc"],
    )
    assert [r["uuid"] for r in reviews] == ["u1", "u2", "u3", "u4"]
    by = {r["uuid"]: r for r in reviews}
    assert by["u1"]["review_sort"] == "published_at_desc"
    assert by["u1"]["review_sorts"] == ["published_at_desc", "score_desc"]
    assert by["u2"]["review_sorts"] == ["published_at_desc"]
    assert by["u3"]["review_sorts"] == ["score_desc"]
    assert by["u4"]["review_sorts"] == ["score_asc"]


def test_collect_reviews_unknown_sort_raises(monkeypatch):
    """Неизвестная сортировка — ValueError, без единого goto."""
    _patch_variant_collect(monkeypatch, used=True)
    page = _RecordingPage({})
    with pytest.raises(ValueError):
        collect_mod.collect_reviews(page, page.url, {}, min_reviews=1, sorts=["nonsense"])
    assert page.gotos == []


def test_collect_reviews_small_variant_skips_remaining_sorts(monkeypatch):
    """vc=5 <= 100 и первый проход собрал все 5 — score_asc не запускается:
    тот же набор в другом порядке, второй проход не нужен."""
    _patch_variant_collect(monkeypatch, used=True)
    base = "https://www.ozon.ru/product/test-123/?tab=reviews"
    page = _RecordingPage({
        base: {"ok": True,
               "reviews": [_rev(f"u{i}", "общий") for i in range(5)]},
    })
    reviews, _, page_log, vm = collect_mod.collect_reviews(
        page, page.url, {"Цвет": "белый"}, min_reviews=1,
        sorts=["published_at_desc", "score_asc"],
    )
    assert page.gotos == [base]
    assert len(reviews) == 5
    assert len(page_log) == 1
    assert vm["variant_count"] == 5


def test_collect_reviews_incomplete_small_variant_runs_all_sorts(monkeypatch):
    """vc=5, но первый проход собрал только 3 — вторая сортировка нужна
    (докрутить недостающее), пропуск НЕ срабатывает."""
    _patch_variant_collect(monkeypatch, used=True)
    base = "https://www.ozon.ru/product/test-123/?tab=reviews"
    a_url = base + "&sort=score_asc"
    page = _RecordingPage({
        base: {"ok": True,
               "reviews": [_rev(f"u{i}", "общий") for i in range(3)]},
        a_url: {"ok": True,
                "reviews": [_rev(f"u{i}", "общий") for i in range(5)]},
    })
    reviews, *_ = collect_mod.collect_reviews(
        page, page.url, {"Цвет": "белый"}, min_reviews=1,
        sorts=["published_at_desc", "score_asc"],
    )
    assert page.gotos == [base, a_url]
    assert len(reviews) == 5


def test_collect_reviews_legacy_no_sort_tagging(monkeypatch):
    """sorts=None (legacy): без полей review_sort/review_sorts, URL без
    sort-параметра передаётся как есть."""
    _patch_variant_collect(monkeypatch, used=True)
    base_url = "https://www.ozon.ru/product/test-123/?tab=reviews"
    page = _RecordingPage({base_url: {"ok": True, "reviews": [_rev("u1", "текст")]}})
    reviews, *_ = collect_mod.collect_reviews(page, base_url, {"Цвет": "белый"}, min_reviews=1)
    assert len(reviews) == 1
    r = reviews[0]
    assert "review_sort" not in r
    assert "review_sorts" not in r
    assert page.gotos == [base_url]


# ---- Phase A: jitter-паузы (детерминированный seed) ------------------------

def test_scroll_jitter_deterministic_seed():
    """Одинаковый seed → одинаковая последовательность пауз (воспроизводимость)."""
    collect_mod.set_scroll_jitter_seed(12345)
    a = [collect_mod.scroll_pause_ms() for _ in range(20)]
    collect_mod.set_scroll_jitter_seed(12345)
    b = [collect_mod.scroll_pause_ms() for _ in range(20)]
    assert a == b
    for v in a:
        assert 340 <= v <= 460  # 400 ± 15% (SCROLL_PAUSE_MS: speed-тест 1500/400/6, 2026-08-16)


def test_scroll_jitter_range_zero():
    """jitter=0 — пауза ровно равна базе (без вариации)."""
    collect_mod.set_scroll_jitter_seed(7)
    assert collect_mod.scroll_pause_ms(base=1000, jitter=0.0) == 1000


def test_scroll_jitter_effective_seed_self_set():
    """Без заданного seed эффективный seed self-set'ится один раз и стабилен."""
    collect_mod.set_scroll_jitter_seed(None)
    s1 = collect_mod.effective_scroll_jitter_seed()
    assert isinstance(s1, int) and s1 > 0
    assert collect_mod.effective_scroll_jitter_seed() == s1


def test_fab_reload_delay_jitter():
    """Пауза FAB-reload джиттерится ±15% и детерминирована seed'ом (ADR §14.7).

    Три фиксированных 2.5с подряд — регулярный retry-сигнал; jitter ломает
    ритм, seed сохраняет воспроизводимость.
    """
    collect_mod.set_scroll_jitter_seed(4242)
    a = [collect_mod.fab_reload_delay_s() for _ in range(20)]
    collect_mod.set_scroll_jitter_seed(4242)
    b = [collect_mod.fab_reload_delay_s() for _ in range(20)]
    assert a == b
    assert len(set(a)) > 1                      # джиттер реально варьирует
    for v in a:
        assert 2.125 <= v <= 2.875              # 2500 мс ±15%


def test_fab_reload_delay_jitter_zero():
    """jitter=0 → ровно базовая пауза (escape hatch для отладки)."""
    collect_mod.set_scroll_jitter_seed(1)
    assert collect_mod.fab_reload_delay_s(jitter=0.0) == 2.5


# ---- Phase A: телеметрия сходимости ----------------------------------------

def test_scroll_to_converge_telemetry():
    """Телеметрия плато: scroll_count, стабилизация, прогрессия, докрутка к низу."""
    page = _scroll_converge_page(cap=8)
    tel = {}
    count, resumed = collect_mod._scroll_to_converge(
        page, target_min=40, telemetry=tel)
    assert count == 8
    assert resumed is False
    assert tel["scroll_count"] >= 1
    assert tel["last_growth_iteration"] >= 1
    assert tel["stable_iterations"] >= tel["last_growth_iteration"]
    assert tel["stable_rounds"] == collect_mod.MAX_SCROLL_STABLE
    assert tel["bottom_passes"] == collect_mod.MAX_SCROLL_BOTTOM
    assert tel["progression"] and tel["progression"][-1] == 8
    assert tel["scroll_wait_seconds"] > 0
    assert tel["bottom_growth"] is False


# ---- Speed-тест: защита от регрессии констант (1500/400/6) -----------------

def test_scroll_constants_regression():
    """Speed-тест 2026-08-16 зафиксировал 1500/400/6 (missing=0 на 2 товарах,
    −58..80% времени). Шаг 2000px/400ms ПОКАЗАТЕЛЬНО НЕБЕЗОПАСЕН: на живом Ozon
    он пропускает lazy-load DOM-чанк (missing=9 из 96) при выигрыше лишь ~6%
    времени. Поэтому рост шага — регрессия, а не ускорение:
    константы не должны произвольно «ускоряться» в обход новой матрицы A/B."""
    assert collect_mod.SCROLL_STEP_PX <= 1500, (
        f"SCROLL_STEP_PX={collect_mod.SCROLL_STEP_PX}: шаг 2000px теряет отзывы "
        f"(lazy-load, missing=9 из 96 на speed-тесте 2026-08-16)"
    )
    assert collect_mod.SCROLL_PAUSE_MS <= 400, (
        f"SCROLL_PAUSE_MS={collect_mod.SCROLL_PAUSE_MS}: ускорение ниже 400ms не "
        f"проверялось матрицей (максимум A/B — 400ms, missing=0)"
    )
    assert collect_mod.MAX_SCROLL_STABLE <= 6, (
        f"MAX_SCROLL_STABLE={collect_mod.MAX_SCROLL_STABLE}: ниже 6 плато без "
        f"роста не проверялось (A/B максимум — 6, missing=0)"
    )


# ---- Вспомогательные функции: parse_variant, pid_from_url, _t_utc ------------

def test_parse_variant_dimensions():
    """Габариты '200x90x6' → {'Длина_см':200,'Ширина_см':90,'Высота_см':6}
    (порядок ДЛИНА x ШИРИНА x ВЫСОТА), включая разные разделители x/х/* и
    пробелы вокруг них."""
    assert collect_mod.parse_variant("200x90x6") == {
        "Длина_см": 200, "Ширина_см": 90, "Высота_см": 6,
    }
    assert collect_mod.parse_variant(" 200 x 90 х 6 ") == {
        "Длина_см": 200, "Ширина_см": 90, "Высота_см": 6,
    }
    assert collect_mod.parse_variant("200*90*6") == {
        "Длина_см": 200, "Ширина_см": 90, "Высота_см": 6,
    }


def test_parse_variant_key_value_pairs():
    """Пары 'ключ:значение' через ';' и ','; пробелы обрезаются; пустые
    ключ/значение пропускаются (но если не осталось ни одной пары — ValueError);
    габаритная часть перед ';' игнорируется."""
    assert collect_mod.parse_variant("Цвет: белый-прозрачный") == {
        "Цвет": "белый-прозрачный",
    }
    got = collect_mod.parse_variant("Цвет: белый; Тип: наушники, Комплект: 2 шт")
    assert got == {"Цвет": "белый", "Тип": "наушники", "Комплект": "2 шт"}
    assert collect_mod.parse_variant("200x90x6; Цвет:белый") == {"Цвет": "белый"}
    assert collect_mod.parse_variant("   ") == {}


def test_parse_variant_bad_raises():
    for bad in ("200x90", "цвет", "Цвет:", ":белый", "xX*"):
        with pytest.raises(ValueError):
            collect_mod.parse_variant(bad)


def test_pid_from_url_variants():
    """pid из любых форм URL товара: slug, query, hash, trailing slash;
    не-товарные URL → 'unknown'."""
    cases = [
        ("https://www.ozon.ru/product/3627230434", "3627230434"),
        ("https://www.ozon.ru/product/xiaomi-redmi-buds-8-3627230434/",
         "3627230434"),
        ("/product/3627230434/?tab=reviews", "3627230434"),
        ("https://www.ozon.ru/product/2000-1234567/", "1234567"),
        ("https://ozon.ru/product/1234567#reviews", "1234567"),
        ("https://www.ozon.ru/product/1234567/reviews/", "1234567"),
        ("https://www.ozon.ru/product/slug-1234567/anything", "1234567"),
    ]
    for url, pid in cases:
        assert collect_mod.pid_from_url(url) == pid, url


def test_pid_from_url_unknown():
    assert collect_mod.pid_from_url("") == "unknown"
    assert collect_mod.pid_from_url("abc") == "unknown"
    assert collect_mod.pid_from_url("https://www.ozon.ru/category/phones/") == "unknown"


def test_t_utc_iso_with_offset():
    import datetime
    s = collect_mod._t_utc()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$", s), s
    dt = datetime.datetime.fromisoformat(s)
    assert dt.tzinfo is not None
    assert dt.utcoffset() == datetime.timedelta(0)


# ---- _parse_comment_api / _apply_api_comments --------------------------------

def test_parse_comment_api_normal():
    """Парсинг тела ответа rpGetCommentsByReviewUuid: корректный JSON."""
    body = json.dumps({
        "cellTrackingInfo": {"0": {"type": "comment"}},
        "comments": [
            {
                "uuid": "c1",
                "comment": "Спасибо за отзыв!",
                "publishedAtFormatted": "26 сентября 2025",
                "createdAt": 1727318400,
                "author": {"firstName": "Максим"},
                "usefulness": {"useful": 3, "unuseful": 0},
                "parentUuid": "",
                "rootParentUuid": "review-uuid-1",
                "isDeleted": False,
            },
            {
                "uuid": "c2",
                "comment": "Рады, что понравилось!",
                "publishedAtFormatted": "27 сентября 2025",
                "createdAt": 1727404800,
                "author": {"firstName": "", "clientOfficial": {"brandName": "М22"}},
                "usefulness": {"useful": 1, "unuseful": 0},
                "parentUuid": "c1",
                "rootParentUuid": "review-uuid-1",
                "isDeleted": False,
            },
        ],
    }).encode()
    comments, reason = collect_mod._parse_comment_api(body)
    assert reason == ""
    assert len(comments) == 2
    c1, c2 = comments
    assert c1["uuid"] == "c1"
    assert c1["author"] == "Максим"
    assert c1["reply_to"] == ""  # reply_to не заполняется парсером (корневой)
    assert c1["date"] == "26 сентября 2025"
    assert c1["text"] == "Спасибо за отзыв!"
    assert c1["like"] == 3
    assert c1["dislike"] == 0
    assert c2["author"] == "М22"
    assert c2["text"] == "Рады, что понравилось!"


def test_parse_comment_api_deleted_skipped():
    """isDeleted=True пропускается парсером."""
    body = json.dumps({"comments": [
        {"uuid": "c1", "comment": "ok", "isDeleted": True},
        {"uuid": "c2", "comment": "живой", "isDeleted": False},
    ]}).encode()
    comments, _ = collect_mod._parse_comment_api(body)
    assert len(comments) == 1
    assert comments[0]["uuid"] == "c2"


def test_parse_comment_api_empty_text_skipped():
    """Комментарий с пустым/пробельным текстом пропускается."""
    body = json.dumps({"comments": [
        {"uuid": "c1", "comment": "   ", "publishedAtFormatted": "1"},
        {"uuid": "c2", "comment": "нормальный", "publishedAtFormatted": "2"},
    ]}).encode()
    comments, _ = collect_mod._parse_comment_api(body)
    assert len(comments) == 1
    assert comments[0]["text"] == "нормальный"


def test_parse_comment_api_invalid_json():
    """Битый JSON → (None, reason)."""
    comments, reason = collect_mod._parse_comment_api(b"not json at all")
    assert comments is None
    assert "json" in reason


def test_parse_comment_api_no_comments_key():
    """Нет ключа comments в payload → (None, reason)."""
    body = json.dumps({"cellTrackingInfo": {}}).encode()
    comments, reason = collect_mod._parse_comment_api(body)
    assert comments is None
    assert "no-comments-list" in reason


def test_parse_comment_api_empty_list():
    """Пустой список comments → (None, reason)."""
    body = json.dumps({"comments": []}).encode()
    comments, reason = collect_mod._parse_comment_api(body)
    assert comments is None


def test_parse_comment_api_giant_body():
    """Ответ > MAX_RESPONSE_BYTES → (None, reason), без парсинга."""
    big = b"x" * (collect_mod.MAX_RESPONSE_BYTES + 1)
    comments, reason = collect_mod._parse_comment_api(big)
    assert comments is None
    assert "body>=" in reason


def test_parse_comment_api_reply_to_always_empty():
    """По контракту B0 API-комментарии — корневые ответы продавца
    (parentUuid пустой), поэтому reply_to всегда пустой (в отличие от DOM)."""
    body = json.dumps({"comments": [
        {"uuid": "p1", "comment": "Родительский", "author": {"firstName": "Иван"},
         "usefulness": {}, "publishedAtFormatted": "1"},
        {"uuid": "c1", "comment": "Ответ", "author": {"firstName": "Мария"},
         "usefulness": {}, "publishedAtFormatted": "2"},
    ]}).encode()
    comments, _ = collect_mod._parse_comment_api(body)
    assert len(comments) == 2
    assert comments[0]["reply_to"] == ""
    assert comments[1]["reply_to"] == ""


def test_apply_api_comments_replaces_dom():
    """API-комментарии перезаписывают DOM, если API непуст."""
    reviews = [
        {"uuid": "rv1", "comments": [{"text": "DOM1"}], "author": "Автор"},
        {"uuid": "rv2", "comments": [], "author": "Другой"},
    ]
    api = {
        "rv1": [{"uuid": "a1", "text": "API1"}],
        "rv2": [{"uuid": "a2", "text": "API2"}],
    }
    n = collect_mod._apply_api_comments(reviews, api)
    assert n == 2
    assert reviews[0]["comments_source"] == "api"
    assert reviews[0]["comments"][0]["text"] == "API1"
    assert reviews[1]["comments_source"] == "api"
    assert reviews[1]["comments"][0]["text"] == "API2"


def test_apply_api_comments_fallback_dom_when_api_empty():
    """Пустой API-ответ (люч [''] отзыву) → DOM остаётся."""
    reviews = [
        {"uuid": "rv1", "comments": [{"text": "DOM1"}]},
    ]
    n = collect_mod._apply_api_comments(reviews, {})
    assert n == 0
    assert reviews[0]["comments_source"] == "dom"
    assert reviews[0]["comments"][0]["text"] == "DOM1"


def test_apply_api_comments_skips_uuidless():
    """Отзыв без uuid не трогается."""
    reviews = [{"comments": [{"text": "DOM"}]}]
    n = collect_mod._apply_api_comments(reviews, {"rv1": [{"text": "X"}]})
    assert n == 0


# ---- _expand_comments_snapshot: Firefox-ветка без перехвата сети ----------------


class _PageNoCapture:
    """Страница без перехвата сети (Firefox/Selenium)."""

    network_capture = False

    def __init__(self):
        self.listeners = []
        self.remove_calls = []

    def on(self, event, cb):
        self.listeners.append((event, cb))

    def remove_listener(self, event, cb):
        self.remove_calls.append(event)

    def wait_for_timeout(self, ms):
        pass


def test_comments_snapshot_no_capture_skips_listener(monkeypatch):
    """Firefox: перехват не регистрируется, комментарии берутся из DOM."""
    page = _PageNoCapture()
    calls = []
    monkeypatch.setattr(collect_mod, "_expand_review_comments",
                        lambda p, max_passes=4: calls.append(max_passes) or 7)
    monkeypatch.setattr(collect_mod, "_touch_watchdog", lambda: None)
    n_clicks, by_review = collect_mod._expand_comments_snapshot(page)
    assert n_clicks == 7
    assert by_review == {}
    assert page.listeners == [], "перехват сети не должен регистрироваться"
    assert calls == [4]


def test_comments_snapshot_with_capture_registers_listener(monkeypatch):
    """Chrome-путь не сломали: слушатель response регистрируется и снимается."""
    class _Page(_PageNoCapture):
        network_capture = True

        class _Resp:
            url = ""
            request = type("R", (), {"post_data": None})()
            body = staticmethod(lambda: b"")

        def evaluate(self, js, *a, **kw):
            return 0

    page = _Page()
    monkeypatch.setattr(collect_mod, "_expand_review_comments",
                        lambda p, max_passes=4: 0)
    monkeypatch.setattr(collect_mod, "_touch_watchdog", lambda: None)
    n_clicks, by_review = collect_mod._expand_comments_snapshot(page)
    assert n_clicks == 0
    assert by_review == {}
    assert [e for e, _ in page.listeners] == ["response"]
    assert page.remove_calls == ["response"]


def test_parse_comment_api_brutally_long_text():
    """Текст > 20000 символов обрезается."""
    long_text = "x" * 25000
    body = json.dumps({"comments": [
        {"uuid": "c1", "comment": long_text, "publishedAtFormatted": "1",
         "usefulness": {}},
    ]}).encode()
    comments, _ = collect_mod._parse_comment_api(body)
    assert len(comments[0]["text"]) == 20000


class _FakeCommentResp:
    """Фейк Playwright-ответа: url/body/post_data для перехвата API."""
    def __init__(self, url, body=b"", post_data=""):
        self.url = url
        self._body = body
        self.request = type("Req", (), {"post_data": post_data})()

    def body(self):
        return self._body


class _FakeCommentPage:
    """Фейк page для _expand_comments_snapshot: принимает response-listener
    и позволяет эмулировать приход API-ответа во время кликов."""
    def __init__(self):
        self.listeners = {"response": []}
        self.sleep_ms = 0

    def on(self, event, cb):
        self.listeners.setdefault(event, []).append(cb)

    def remove_listener(self, event, cb):
        if cb in self.listeners.get(event, []):
            self.listeners[event].remove(cb)

    def wait_for_timeout(self, ms):
        self.sleep_ms += ms

    def emit_resp(self, resp):
        for cb in list(self.listeners.get("response", [])):
            cb(resp)


def test_expand_comments_snapshot_captures_api(monkeypatch):
    """_expand_comments_snapshot подхватывает API-ответ и привязывает его
    к отзыву по reviewUuid из post_data; слушатель снимается в finally."""
    page = _FakeCommentPage()
    monkeypatch.setattr(collect_mod, "_expand_review_comments", lambda *a, **k: 2)
    api_body = json.dumps({"comments": [
        {"uuid": "c1", "comment": "Ответ продавца",
         "publishedAtFormatted": "1 сентября 2026", "author": {"firstName": "М22"},
         "usefulness": {"useful": 1, "unuseful": 0}},
    ]}).encode()
    n, by_review = collect_mod._expand_comments_snapshot(page)
    assert n == 2
    assert page.listeners["response"] == []  # снят после возврата
    # эмулируем приход API именно во время подписки:
    page = _FakeCommentPage()
    monkeypatch.setattr(collect_mod, "_expand_review_comments",
                        lambda *a, **k: page.emit_resp(_FakeCommentResp(
                            url="https://www.ozon.ru/api/composer-api.bx/_action/rpGetCommentsByReviewUuid",
                            body=api_body,
                            post_data=json.dumps({"reviewUuid": "rv-uuid"}))))
    n, by_review = collect_mod._expand_comments_snapshot(page)
    assert "rv-uuid" in by_review
    assert by_review["rv-uuid"][0]["text"] == "Ответ продавца"
    assert page.listeners["response"] == []


def test_expand_comments_snapshot_skips_unrelated_resp():
    """Не относящиеся к API url не перехватываются."""
    page = _FakeCommentPage()
    page.emit_resp(_FakeCommentResp(url="https://www.ozon.ru/product/123", body=b"x",
                                    post_data=json.dumps({"reviewUuid": "x"})))
    bio = json.dumps({"comments": [{"uuid": "c1", "comment": "т"}]}).encode()
    n, by_review = collect_mod._expand_comments_snapshot(page)
    assert by_review == {}
    assert n == 0
