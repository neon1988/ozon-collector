"""Тесты экстрактора REVIEWS_JS на сохранённой странице отзывов Ozon.

Фикстуры tests/review_pages/*.html.gz — живые снимки страницы отзывов
(?tab=reviews + прокрутка до появления карточек), снятые браузером по CDP:
  xiaomi          — режим «Все отзывы» (смешанная лента);
  xiaomi_variant  — активен нативный фильтр «Этот вариант товара» (2697).
Отзывы — самая динамичная часть Ozon, поэтому контракт проверяется отдельно
от PRODUCT_JS (tests/test_extractors.py).
"""
from __future__ import annotations

import pytest

from ozon_collector.collect import _activate_variant_mode  # noqa: E402
from ozon_collector.collect import EXPAND_REVIEWS_JS, REVIEWS_JS  # noqa: E402
import ozon_collector.collect as collect_mod


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
        return self.reviews_data


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
      <div data-comment-uuid="cm1" class="jl8_25">
        <div class="i7z_25">
          <span class="tsCompactControl500Medium">Продавец</span>
          <a class="l9j_25">в ответ Автор</a>
        </div>
        <div class="j9l_25">2 января 2026</div>
        <div class="jm_25"><span class="jm0_25">Благодарим за отзыв, подробности в личных сообщениях.</span></div>
        <button><div class="b25_9_0-a2 tsBodyControl400Small">3</div></button>
        <button><div class="b25_9_0-a2 tsBodyControl400Small">1</div></button>
      </div>
      <div data-comment-uuid="cm2" class="jl8_25">
        <div class="i7z_25">
          <span class="tsCompactControl500Medium">Покупатель</span>
        </div>
        <div class="j9l_25">3 января 2026</div>
        <div class="jm_25"><span class="jm0_25">Согласен с продавцом.</span></div>
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


# ---- Multi-sort: сбор отзывов в нескольких сортировках -----------------------

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
        assert 1360 <= v <= 1840  # 1600 ± 15% (SCROLL_PAUSE_MS после A/B-реверта)


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
