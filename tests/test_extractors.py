"""Тесты экстракторов PRODUCT_JS на сохранённых DOM-страницах Ozon.

Быстрый аналог живого сбора: страницы зафиксированы (tests/product_pages/*),
тест проверяет, что PRODUCT_JS извлекает ключевые поля текущей разметки.
"""
from __future__ import annotations

import pytest

FIXTURES = ["xiaomi", "roborock", "neoclima", "topper"]

SCORE_WIDGETS = {
    "webProductScore",
    "webSingleProductScore",
    "webReviewProductScore",
    "fallback_text",
}


@pytest.mark.parametrize("name", FIXTURES)
def test_extract_live_markup(evaluate, name):
    d = evaluate(name=name)
    assert isinstance(d, dict)

    rating = d.get("rating")
    assert isinstance(rating, (int, float)), f"{name}: rating={rating!r}"
    assert 0 < rating <= 5

    assert d.get("rating_source") in SCORE_WIDGETS, f"{name}: source={d.get('rating_source')!r}"

    count = d.get("rating_count")
    assert isinstance(count, (int, float)) and count > 0, f"{name}: count={count!r}"

    chars = d.get("characteristics") or {}
    assert len(chars) >= 10, f"{name}: chars={len(chars)}"
    assert "Артикул" in chars, f"{name}: нет Артикула"

    desc = d.get("descriptions") or []
    assert len(desc) > 0, f"{name}: descriptions пуст"

    price = d.get("price") or {}
    assert isinstance(price.get("current"), (int, float)) and price["current"] > 0, \
        f"{name}: price.current={price.get('current')!r}"

    gallery = d.get("gallery") or []
    assert len(gallery) > 0, f"{name}: gallery пуст"
    for u in gallery:
        assert "/rp-photo" not in u, f"{name}: фото отзыва в gallery: {u}"
        assert "/video-" not in u, f"{name}: видео-обложка в gallery: {u}"
        assert "/cms/" not in u, f"{name}: служебный файл в gallery: {u}"


def test_characteristics_not_culled_by_content_visibility(evaluate):
    """Regression for the xiaomi flake (2026-10-18): content-visibility wrapper.

    Chromium culls layout of a `content-visibility: auto` subtree that is far
    below the viewport; innerText then returns "" for it and characteristics()
    yielded {} while rating (outside the wrapper) stayed intact. Fixtures lift
    the culling via CV_UNSKIP_CSS, so this page must keep its characteristics.
    """
    html = (
        "<html><body>"
        "<div style='height:9000px'></div>"
        '<div data-widget="paginator" style="content-visibility:auto">'
        '<div data-widget="webCharacteristics">'
        "<dl><dt>Артикул</dt><dd>12345</dd></dl>"
        "<dl><dt>Бренд</dt><dd>TestBrand</dd></dl>"
        "</div></div></body></html>")
    d = evaluate(html=html)
    chars = d.get("characteristics") or {}
    assert chars.get("Артикул") == "12345", \
        f"culling обнулил characteristics: {chars!r}"
    assert chars.get("Бренд") == "TestBrand", \
        f"culling обнулил characteristics: {chars!r}"


def test_empty_page_no_crash(evaluate):
    d = evaluate(html="<html><body><p>товара нет</p></body></html>")
    assert isinstance(d, dict)
    assert d.get("rating") is None
    assert d.get("characteristics") in ({}, None)
    assert d.get("descriptions") in ([], None)


def test_gallery_only_product_widgets(evaluate):
    """gallery собирается только из виджетов карточки товара (webGallery,
    webDescription) — без фото отзывов (webListPhotos), чужих товаров
    (skuGrid/skuShelfGoods) и служебных файлов (header/footer/price)."""
    d = evaluate(html='''<html><body>
      <div data-widget="webGallery">
        <img src="https://ir.ozone.ru/s3/multimedia-1-7/wc50/9784223467.jpg">
        <img src="https://ir.ozone.ru/s3/video-60/01KJX11TBGE4H2ESQ9A3GHS1MV/cover/wc75/cover.jpg">
      </div>
      <div data-widget="webDescription">
        <img src="https://ir.ozone.ru/s3/multimedia-1-t/ww1200/9918301961.jpg">
      </div>
      <div data-widget="webListPhotos">
        <img src="https://ir.ozone.ru/s3/rp-photo-15/wc200/9708d718-2290-4561-98e9-4d84d6d592c6.jpeg">
      </div>
      <div data-widget="skuGrid">
        <img src="https://ir.ozone.ru/s3/multimedia-1-a/wc50/7777777777.jpg">
      </div>
      <div data-widget="skuShelfGoods">
        <img src="https://ir.ozone.ru/s3/multimedia-1-b/wc50/8888888888.jpg">
      </div>
      <div data-widget="webPrice">
        <img src="https://ir.ozone.ru/s3/cms/d2/t26/wc200/logo_ozon_1.png">
      </div>
    </body></html>''')
    g = d.get("gallery") or []
    urls = [u.replace("/wc50/", "/wc1000/").replace("/wc75/", "/wc1000/")
              .replace("/ww1200/", "/wc1000/") for u in g]
    assert "9784223467.jpg" in " ".join(urls), f"нет картинки webGallery: {g}"
    assert "9918301961.jpg" in " ".join(urls), f"нет картинки описания: {g}"
    for u in g:
        assert "/rp-photo" not in u, f"фото отзыва попало в gallery: {u}"
        assert "/video-" not in u, f"видео-обложка попала в gallery: {u}"
        assert "/cms/" not in u, f"служебный файл попал в gallery: {u}"
        assert "7777777777" not in u and "8888888888" not in u, \
            f"чужой товар попал в gallery: {u}"
    assert len(g) == 2, f"ожидал 2 (webGallery+desc), получил: {g}"


def test_review_zero_from_widget_state(evaluate):
    """«Отзывы о товаре 0»: state-JSON webReviewProductScore c reviewsCount=0
    → rating=0 и rating_count=0 (подтверждённый ноль, а не неопределённость)."""
    html = '''<html><body>
      <div id="state-webReviewProductScore-3132021-default-1"
           data-state="{&quot;score&quot;:null,&quot;reviewsCount&quot;:0,
                       &quot;totalScore&quot;:0,
                       &quot;constants&quot;:{&quot;emptyScoreText&quot;:&quot;Нет оценок&quot;,
                                            &quot;reviewsTextExist&quot;:&quot;0 отзывов&quot;}}"></div>
      <h1>Спрей для горла</h1>
    </body></html>'''
    d = evaluate(html=html)
    assert d.get("rating") == 0, f"rating={d.get('rating')!r}"
    assert d.get("rating_source") == "no_reviews_widget", \
        f"rating_source={d.get('rating_source')!r}"
    assert d.get("rating_count") == 0, f"rating_count={d.get('rating_count')!r}"


def test_review_zero_from_total_score_only(evaluate):
    """Если reviewsCount отсутствует, но totalScore=0 — тоже подтверждённый ноль."""
    html = '''<html><body>
      <div id="state-webReviewProductScore-1-default-1"
           data-state="{&quot;score&quot;:null,&quot;totalScore&quot;:0}"></div>
    </body></html>'''
    d = evaluate(html=html)
    assert d.get("rating") == 0
    assert d.get("rating_count") == 0


def test_review_state_with_count_not_zero_untouched(evaluate):
    """reviewsCount>0 → rating_count не приравнивается к нулю, обычный путь."""
    html = '''<html><body>
      <div id="state-webReviewProductScore-1-default-1"
           data-state="{&quot;reviewsCount&quot;:277,&quot;totalScore&quot;:0}"></div>
      <div data-widget="webSingleProductScore"><div>5.0/5</div></div>
    </body></html>'''
    d = evaluate(html=html)
    assert d.get("rating_count") is None or d.get("rating_count") != 0


def test_review_zero_absent_no_crash(evaluate):
    """Нет state-JSON виджета → reviewZero=false, без краша, обычное поведение."""
    d = evaluate(html="<html><body><p>товара нет</p></body></html>")
    assert d.get("rating") is None
    assert d.get("rating_count") is None


def test_customer_opinion_recommend_and_stars(evaluate):
    """«По мнению покупателей»: % рекомендуют + разбивка оценок по звёздам.
    Реальная разметка Ozon: строки «N звёзд», процент в style=width, кол-во в конце."""
    html = '''<html><body>
      <div data-widget="webReviewProductScore">
        <div class="yk0_25">
          <div class="ky2_25">
            <div class="ky3_25">5 звёзд</div>
            <div class="k2y_25"><div class="yk2_25"><div class="y2k_25" style="width: 87.9883%;"></div></div></div>
            <div class="k3y_25">4527</div>
          </div>
          <div class="ky2_25">
            <div class="ky3_25">4 звезды</div>
            <div class="k2y_25"><div class="yk2_25"><div class="y2k_25" style="width: 7.26919%;"></div></div></div>
            <div class="k3y_25">374</div>
          </div>
          <div class="ky2_25">
            <div class="ky3_25">3 звезды</div>
            <div class="k2y_25"><div class="yk2_25"><div class="y2k_25" style="width: 1.4966%;"></div></div></div>
            <div class="k3y_25">77</div>
          </div>
          <div class="ky2_25">
            <div class="ky3_25">2 звезды</div>
            <div class="k2y_25"><div class="yk2_25"><div class="y2k_25" style="width: 0.738581%;"></div></div></div>
            <div class="k3y_25">38</div>
          </div>
          <div class="ky2_25">
            <div class="ky3_25">1 звезда</div>
            <div class="k2y_25"><div class="yk2_25"><div class="y2k_25" style="width: 2.50729%;"></div></div></div>
            <div class="k3y_25">129</div>
          </div>
        </div>
      </div>
    </body></html>'''
    d = evaluate(html=html)
    op = d.get("customer_opinion") or {}
    stars = {int(k): v for k, v in (op.get("stars") or {}).items()}
    assert abs(stars.get(5) - 87.9883) < 0.001, f"5★={stars.get(5)!r}"
    assert abs(stars.get(4) - 7.26919) < 0.001, f"4★={stars.get(4)!r}"
    assert abs(stars.get(1) - 2.50729) < 0.001, f"1★={stars.get(1)!r}"
    counts = {int(k): v for k, v in (op.get("counts") or {}).items()}
    assert counts.get(5) == 4527, f"counts={counts!r}"
    assert counts.get(1) == 129, f"counts={counts!r}"


def test_customer_opinion_live_markup(evaluate):
    """Гистограмма извлекается с реальной сохранённой страницы товара."""
    d = evaluate(name="xiaomi")
    op = d.get("customer_opinion") or {}
    stars = {int(k): v for k, v in (op.get("stars") or {}).items()}
    assert len(stars) == 5, f"ожидал 5 звёзд, получил: {stars!r}"
    assert 0 < stars.get(5, 0) <= 100, f"5★ вне диапазона: {stars!r}"
    assert sum(stars.values()) >= 95, f"сумма процентов < 95: {stars!r}"


def test_customer_opinion_absent_when_no_widget(evaluate):
    """Нет виджета «По мнению покупателей» → customer_opinion пустой, без краша."""
    d = evaluate(html='<html><body><p>товара нет</p></body></html>')
    op = d.get("customer_opinion") or {}
    assert op.get("recommend_percent") is None
    assert not op.get("stars")
    assert not op.get("text")