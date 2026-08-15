"""Тесты дедупа повторяющихся ответов продавца при рендере отчёта."""
from __future__ import annotations

from ozon_collector.report import (
    _dedup_key,
    _group_comments,
    _render_comments,
    _review_block,
    render,
    render_search_digest,
)

MARK = "таких же ответ"  # общий кусок метки счётчика


def _c(text, author="Продавец", reply_to="Иван"):
    return {"author": author, "reply_to": reply_to,
            "date": "2026-08-01", "text": text, "like": 0, "dislike": 0}


def _review(author, comments, text="отзыв"):
    return {"author": author, "date": "2026-08-01", "stars": 5,
            "text": text, "comments": comments}


def _raw(reviews):
    return {
        "product": {"title": "Товар"},
        "collected_at_utc": "2026-08-08T00:00:00+00:00",
        "collector_version": "1.11.0",
        "schema_version": "2.0",
        "reviews": reviews,
        "reviews_collected": len(reviews),
    }


# ---- _dedup_key / _group_comments -----------------------------------------

def test_dedup_key_strips_name_and_normalizes():
    a = _c("Иван, спасибо за ваш заказ и отзыв!", reply_to="Иван")
    b = _c("Пётр,   спасибо за ваш заказ и отзыв!", reply_to="Пётр")
    assert _dedup_key(a) == _dedup_key(b) == ("Продавец", "спасибо за ваш заказ и отзыв")


def test_dedup_key_two_word_name():
    a = _c("Александр Петров, спасибо!")
    assert _dedup_key(a) == ("Продавец", "спасибо")


def test_dedup_key_different_first_sentence():
    a = _c("Спасибо за заказ!")
    b = _c("Очень жаль, что не подошло.")
    assert _dedup_key(a) != _dedup_key(b)


def test_dedup_key_different_author():
    a = _c("Спасибо!", author="MAX WAX")
    b = _c("Спасибо!", author="Roborock")
    assert _dedup_key(a) != _dedup_key(b)


def test_dedup_key_empty_text_is_none():
    assert _dedup_key(_c("  ")) is None
    assert _dedup_key(_c("")) is None


def test_group_comments_interleaved():
    # A, B, C с повтором группы A → метка должна быть у A, порядок A, B
    g = _group_comments([
        _c("Иван, спасибо за заказ!"),
        _c("Очень жаль, что не подошло."),
        _c("Пётр, спасибо за заказ!"),
    ])
    assert len(g) == 2
    assert g[0][1] == 1 and "спасибо" in g[0][0]["text"].lower()
    assert g[1][1] == 0 and "очень жаль" in g[1][0]["text"].lower()


def test_group_comments_keeps_first_occurrence():
    g = _group_comments([
        _c("Первая версия, спасибо!"),
        _c("Вторая версия, спасибо!"),  # та же первая фраза «спасибо!», но др. текст
    ])
    assert len(g) == 1
    assert "Первая" in g[0][0]["text"]
    assert g[0][1] == 1


# ---- _render_comments / _review_block --------------------------------------

def test_render_comments_dedup_label_next_to_first():
    comments = [
        _c("Иван, спасибо за заказ!"),
        _c("Очень жаль, что не подошло."),
        _c("Пётр, спасибо за заказ!"),
    ]
    first_meta = {id(c): n for c, n in _group_comments(comments)}
    lines = _render_comments(comments, first_meta)
    txt = "\n".join(lines)
    assert txt.count("спасибо за заказ") == 1
    assert "очень жаль" in txt.lower()
    assert "ещё 1 такой же ответ" in txt
    # метка должна идти сразу после первого «спасибо», а не после «очень жаль»
    i_spasibo = txt.index("спасибо за заказ")
    i_zhalko = txt.lower().index("очень жаль")
    i_mark = txt.index("ещё 1")
    assert i_spasibo < i_mark < i_zhalko


def test_render_comments_no_dedup():
    lines = _render_comments([
        _c("Иван, спасибо за заказ!"),
        _c("Пётр, спасибо за заказ!"),
    ])
    txt = "\n".join(lines)
    assert txt.count("спасибо за заказ") == 2
    assert "пропущено" not in txt


def test_review_block_preserves_old_behavior_when_dedup_off():
    lines = _review_block(_review("Автор", [_c("Привет!"), _c("Привет!")]))
    assert "Привет!" in "\n".join(lines)
    assert "пропущено" not in "\n".join(lines)


# ---- render / digest ---------------------------------------------------------

def test_render_dedup_across_reviews():
    md = render(_raw([
        _review("A", [_c("Иван, спасибо за заказ!")]),
        _review("B", [_c("Очень жаль!")]),
        _review("C", [_c("Пётр, спасибо за заказ!")]),
    ]))
    assert md.count("спасибо за заказ") == 1
    assert md.count("Очень жаль") == 1
    assert "ещё 1 такой же ответ" in md


def test_render_no_dedup_flag():
    md = render(_raw([
        _review("A", [_c("Иван, спасибо за заказ!")]),
        _review("B", [_c("Пётр, спасибо за заказ!")]),
    ]), dedup_replies=False)
    assert md.count("спасибо за заказ") == 2
    assert "пропущено" not in md


def _combined(products):
    return {
        "schema_version": "2.0", "collector_version": "1.11.0",
        "collected_at_utc": "2026-08-08T00:00:00+00:00",
        "search": {"url": "https://www.ozon.ru/search/?text=x", "query": "x"},
        "found": 2, "limit": 2, "collected": 2, "products": products,
    }


def _ok_result(pid, n_same=1):
    reviews = [_review("A", [_c("Иван, спасибо за заказ!")])]
    for i in range(1, n_same):
        reviews.append(_review(f"D{i}", [_c(f"Пётр{i}, спасибо за заказ!")]))
    reviews.append(_review("B", [_c("Очень жаль!")]))
    return {"pid": pid, "ok": True, "error": None, "cache": "hit", "ocr": "off",
            "raw": _raw(reviews), "markdown": ""}


def test_digest_dedup_per_product():
    # у каждого товара по 2 одинаковых ответа продавца → у обоих метки по 1
    combined = _combined([_ok_result("1", n_same=2), _ok_result("2", n_same=2)])
    md = render_search_digest(combined, reviews_per_product=5)
    assert md.count("ещё 1 такой же ответ") == 2


def test_digest_no_dedup_flag():
    combined = _combined([_ok_result("1", n_same=1)])
    md = render_search_digest(combined, reviews_per_product=5,
                              dedup_replies=False)
    assert md.count("спасибо за заказ") == 1  # всего один комментарий
    assert "пропущено" not in md


# ---- Phase A: единый дефолт 100 + размер дайджеста -------------------------

def test_default_reviews_per_product_is_100():
    """Единый источник дефолта: константа == 100 и используется рендером."""
    from ozon_collector.report import DEFAULT_REVIEWS_PER_PRODUCT
    assert DEFAULT_REVIEWS_PER_PRODUCT == 100
    assert render_search_digest.__defaults__[0] == DEFAULT_REVIEWS_PER_PRODUCT


def test_digest_default_caps_at_100():
    """Дефолт режет digest до 100 отзывов (raw в combined остаётся полным)."""
    ok = _ok_result("1")
    ok["raw"]["reviews"] = [
        {"author": f"Автор{i}", "date": "2026-08-01", "stars": 5,
         "text": f"текст {i}"} for i in range(250)
    ]
    md = render_search_digest(_combined([ok]))
    assert "Автор99" in md
    assert "Автор100" not in md
    # raw в combined не тронут (обрезка только в копии для дайджеста)
    assert len(_combined([ok])["products"][0]["raw"]["reviews"]) == 250


def test_digest_size_kb_line():
    """В дайджест добавляется строка размера (для контроля роста из-за 100)."""
    ok = _ok_result("1")
    ok["raw"]["reviews"] = [
        {"author": f"Автор{i}", "date": "2026-08-01", "stars": 5,
         "text": f"текст {i}"} for i in range(120)
    ]
    md = render_search_digest(_combined([ok]), reviews_per_product=100)
    assert "Размер дайджеста" in md
    assert "КБ" in md
