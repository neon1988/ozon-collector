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