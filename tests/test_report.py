"""Тесты дедупа повторяющихся ответов продавца при рендере отчёта."""
from __future__ import annotations

from ozon_collector.report import (
    _dedup_key,
    _group_comments,
    _render_comments,
    _review_block,
    _review_text_sig,
    _top_reviews,
    render,
    render_search_digest,
)

MARK = "таких же ответ"  # общий кусок метки счётчика


def _c(text, author="Продавец", reply_to="Иван"):
    return {"author": author, "reply_to": reply_to,
            "date": "2026-08-01", "text": text, "like": 0, "dislike": 0}


def _review(author, comments, text="отзыв", stars=5, variant=None, stale=False):
    r = {"author": author, "date": "2026-08-01", "stars": stars,
         "text": text, "comments": comments}
    if variant is not None:
        r["variant"] = variant
    if stale:
        r["stale"] = True
    return r


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


def test_render_comments_ozon_author_hides_text():
    """Комментарий от «OZON» (шаблонный текст о возврате) — с заглушкой."""
    lines = _render_comments([_c("Благодарим за отзыв, возможен возврат", author="OZON")])
    txt = "\n".join(lines)
    assert "_(текст скрыт)_" in txt
    assert "Благодарим за отзыв" not in txt
    # заголовок (автор) и дата сохраняются
    assert "OZON" in txt
    assert "2026-08-01" in txt


def test_render_comments_ozon_author_empty_text_shows_placeholder():
    """У Пустого комментария «OZON» тоже заглушка, не пустая строка."""
    lines = _render_comments([_c("", author="OZON")])
    txt = "\n".join(lines)
    assert "_(текст скрыт)_" in txt


def test_render_comments_polzovatel_ozon_keeps_text():
    """Автор «Пользователь OZON» (не точный «OZON») текст НЕ скрывает."""
    lines = _render_comments([_c("комментарий пользователя", author="Пользователь OZON")])
    txt = "\n".join(lines)
    assert "комментарий пользователя" in txt
    assert "скрыт" not in txt


def test_render_comments_technozone_keeps_text():
    """Продавец, чьё имя лишь содержит OZON (TechnoZone), текст НЕ скрывает."""
    lines = _render_comments([_c("Наш ответ", author="TechnoZone")])
    txt = "\n".join(lines)
    assert "Наш ответ" in txt
    assert "скрыт" not in txt


def test_render_comments_regular_author_keeps_text():
    """Обычный автор по-прежнему выводит текст и не получает заглушку."""
    lines = _render_comments([_c("обычный текст", author="Иван")])
    txt = "\n".join(lines)
    assert "обычный текст" in txt
    assert "скрыт" not in txt


def test_review_text_sig_stale_placeholder_is_empty():
    """Stale-пустышка «Отзыв устарел» (текст = буква + имя) — незначима."""
    assert not _review_text_sig(_review(
        "Ольга Б.", [], text="О\nОльга Б.\n\nОтзыв устарел"))


def test_review_text_sig_stale_placeholder_with_content_is_significant():
    """«Отзыв устарел» + реальный текст — всё ещё значим."""
    assert _review_text_sig(_review(
        "Пользователь OZON", [],
        text="Отзыв устарел\nУпаковка разорвана испорчена. Товар повреждён."))


def test_top_reviews_drops_stale_placeholders():
    """Пустышки уровня «Отзыв устарел» не попадают в топ."""
    top = _top_reviews([
        _review("Ольга Б.", [], text="О\nОльга Б.\n\nОтзыв устарел",
                stars=1),
        _review("Иван", [], text="Отличный товар", stars=5),
    ], 10)
    assert [r["author"] for r in top] == ["Иван"]


def test_review_block_preserves_old_behavior_when_dedup_off():
    lines = _review_block(_review("Автор", [_c("Привет!"), _c("Привет!")]))
    assert "Привет!" in "\n".join(lines)
    assert "пропущено" not in "\n".join(lines)


def test_review_block_stale_hides_stars():
    """stale-отзыв рендерится без звёздной строки (маркер Ozon «Отзыв устарел»)."""
    lines = _review_block(_review("Автор", [], stars=4, stale=True))
    text = "\n".join(lines)
    assert "★" not in text
    assert "☆" not in text
    assert "Автор" in text
    assert "отзыв" in text


def test_review_block_normal_shows_stars():
    lines = _review_block(_review("Автор", [], stars=3))
    text = "\n".join(lines)
    assert "★" in text


def test_variant_in_header_dedups_from_reviews():
    """Одинаковый вариант выносится в шапку один раз и не дублируется в отзывах."""
    md = render(_raw([
        _review("A", [], variant="Цвет товара: черный"),
        _review("B", [], variant="Цвет товара: черный"),
    ]))
    assert md.count("Вариант: Цвет товара: черный") == 1
    assert md.count("Цвет товара: черный") == 1
    assert "| Цвет товара" not in md


def test_variant_in_header_kept_when_differ():
    """Разные варианты остаются в строке каждого отзыва."""
    md = render(_raw([
        _review("A", [], variant="Цвет товара: черный"),
        _review("B", [], variant="Цвет товара: белый"),
    ]))
    assert "Вариант: Цвет товара: черный" not in md
    assert md.count("Цвет товара: черный") == 1
    assert md.count("Цвет товара: белый") == 1


def test_review_block_aspects_join_single_line():
    """Аспекты варианта выводятся одной ·-строкой, без пустых."""
    lines = _review_block(_review(
        "Автор", [], variant="Цвет товара: черный\nВнешний вид:Великолепный\n"
        "Комфорт:Удобно\n\n"))
    text = "\n".join(lines)
    assert "\nКомфорт:" not in text, "Комфорт должен быть в строке аспектов, не отдельной"
    assert "Внешний вид:Великолепный · Комфорт:Удобно" in text
    assert not text.rstrip().endswith("·")
    assert not text.rstrip().endswith(":")


def test_review_block_aspects_empty_no_line():
    """Без аспектов вариант-строка не выводится."""
    lines = _review_block(_review("Автор", [], variant="Цвет товара: черный"))
    text = "\n".join(lines)
    assert "Внешний вид" not in text
    assert "\nЦвет товара" not in text


def test_norm_nbsp_in_review_text():
    """Неразрывные пробелы в тексте отзыва заменяются на обычные."""
    r = _review("Автор", [])
    r["text"] = "Очень\u00a0хороший\u00a0товар"
    md = render(_raw([r]))
    assert "Очень\u00a0хороший\u00a0товар" not in md
    assert "Очень хороший товар" in md


def test_strip_html_in_review_text():
    """HTML-теги и коды вырезаются из текста отзыва."""
    r = _review("Автор", [])
    r["text"] = "Звук <b>отличный</b> &amp; бас глубокий<br>ещё строка"
    md = render(_raw([r]))
    assert "<b>" not in md
    assert "<br>" not in md
    assert "&amp;" not in md
    assert "Звук отличный & бас глубокий" in md


def test_strip_html_in_comment_text():
    """HTML-теги вырезаются из текста комментария."""
    r = _review("Автор", [_c("Спасибо <em>за отзыв</em>!", author="Продавец")])
    md = render(_raw([r]))
    assert "<em>" not in md
    assert "Спасибо за отзыв!" in md


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


def test_render_card_rating_when_no_variant_sample():
    """Без нейтральной выборки (нет отзывов) рейтинг карточки выводится с
    пометкой «нет нейтральной выборки»; rating_source не показывается.
    АКСТЫ из customer_opinion при отсутствии text не выводятся."""
    raw = _raw([])
    raw["product"]["rating"] = 4.9
    raw["product"]["rating_count"] = 3045
    raw["product"]["rating_source"] = "webSingleProductScore"
    raw["product"]["customer_opinion"] = {
        "recommend_percent": 92,
        "stars": {"5": "81", "4": "12", "3": "4", "2": "2", "1": "1"},
    }
    md = render(raw)
    assert ("Рейтинг карточки Ozon: 4.900 / 5 (3045 оценок) "
            "_(нет нейтральной выборки)_") in md
    assert "webSingleProductScore" not in md
    assert "По мнению покупателей" not in md
    assert "92% рекомендуют" not in md


def test_customer_aspects_rendered():
    """АКСТЫ из «По мнению покупателей» выводятся в карточке, мусор отбрасывается."""
    raw = _raw([])
    raw["product"]["customer_opinion"] = {
        "text": "По мнению покупателей\nКачество звука: хорошее\n"
                "Комфорт при использовании: удобно\nВнешний вид: великолепный\n"
                "Подобрали для вас\n27 167 ₽\nSony ..."
    }
    md = render(raw)
    assert "По мнению покупателей:" in md
    assert "Качество звука: хорошее" in md
    assert "Комфорт при использовании: удобно" in md
    assert "Внешний вид: великолепный" in md
    assert "Подобрали для вас" not in md
    assert "Sony" not in md
    assert "По мнению покупателей\n" not in md


def test_customer_aspects_no_text_omitted():
    """customer_opinion без text и процентов — строка сводки не выводится."""
    raw = _raw([])
    raw["product"]["customer_opinion"] = {"recommend_percent": None, "stars": {}}
    md = render(raw)
    assert "По мнению покупателей" not in md


def test_customer_opinion_percents_rendered():
    """% рекомендуют выводится, разбивка «Оценки: ★…» (все варианты) — НЕТ."""
    raw = _raw([])
    raw["product"]["customer_opinion"] = {
        "recommend_percent": 92,
        "stars": {"1": 0, "2": 6.66667, "3": 0, "4": 0, "5": 93.3333},
        "text": "По мнению покупателей\nКачество звука: хорошее\nПодобрали для вас",
    }
    md = render(raw)
    assert "Рекомендуют: 92% покупателей" in md
    assert "Качество звука: хорошее" in md
    assert "Подобрали для вас" not in md
    assert "Оценки:" not in md
    assert "★5:" not in md
    assert "93%" not in md


def test_render_no_rating_opinion_lines():
    """Без рейтинга/opinion в raw — строки не выводятся, карточка цела."""
    md = render(_raw([]))
    assert "Рейтинг" not in md
    assert "По мнению покупателей" not in md
    assert "Средний рейтинг по отзывам" not in md


def test_render_avg_review_rating():
    """Средний балл по нейтральной выборке собранных отзывов (без score_asc)."""
    raw = _raw([
        _review("A", [], stars=5),
        _review("B", [], stars=4),
        _review("C", [], stars=3),
        _review("D", [], stars=3),
    ])
    md = render(raw)
    assert ("Рейтинг по собранным отзывам (без «сначала низкие»): "
            "3.750 / 5 (4 оценки)") in md
    md2 = render(_raw([_review("A", [], stars=5)]))
    assert ("Рейтинг по собранным отзывам (без «сначала низкие»): "
            "5.000 / 5 (1 оценка)") in md2


def _combined(products):
    return {
        "schema_version": "2.0", "collector_version": "1.11.0",
        "collected_at_utc": "2026-08-08T00:00:00+00:00",
        "search": {"url": "https://www.ozon.ru/search/?text=x", "query": "x"},
        "found": 2, "limit": 2, "collected": 2, "products": products,
    }


def test_collapse_newlines_removes_blank_lines():
    from ozon_collector.report import _collapse_newlines
    txt = "Строка1\n\n\nСтрока2\n\nСтрока3\n"
    assert _collapse_newlines(txt) == "Строка1\nСтрока2\nСтрока3"


def test_collapse_newlines_keeps_single_newlines():
    from ozon_collector.report import _collapse_newlines
    assert _collapse_newlines("а\nб") == "а\nб"
    assert _collapse_newlines("   ") == ""
    assert _collapse_newlines("") == ""
    # неразрывные/узкие юникод-пробелы заменяются обычными
    assert _collapse_newlines("Цена\u00a01\u2009234\u200a₽") == "Цена 1 234 ₽"
    assert "\u00a0" not in _collapse_newlines("a\u00a0b\u202fc\u2009d")


def test_render_collapses_blank_lines_in_review_text():
    from ozon_collector.report import render
    raw = _raw([_review("Лилия", [],
                        text="Здравствуйте!\n\nСпасибо за отзыв.\n\nКоманда Fiio",
                        stars=5)])
    md = render(raw, dedup_replies=False)
    # три фразы на отдельных строках без пустых строк между ними
    assert "Здравствуйте!\nСпасибо за отзыв.\nКоманда Fiio" in md
    # признаков исходных пустых (двойных) переводов строк не осталось в блоке текста
    assert "Спасибо за отзыв.\n\nКоманда" not in md


def test_render_description_replaces_nbsp(tmp_path=None):
    from ozon_collector.report import render
    raw = _raw([])
    raw["product"]["descriptions"] = ["Описание \u00a0с\u2009неразрывными\u200aпробелами\u00a0и\n\nпереносом"]
    md = render(raw, dedup_replies=False)
    assert "\u00a0" not in md
    assert "\u2009" not in md and "\u200a" not in md
    assert "с неразрывными пробелами и" in md
    # двойного переноса больше нет
    assert "и\n\nпереносом" not in md


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


def test_digest_excluded_section():
    """Исключённые по рейтингу не рендерятся полностью, а выносятся в секцию."""
    low = _ok_result("1")
    low["excluded"] = {"reason": "rating_below", "rating": 4.3,
                       "min_rating": 4.7}
    low["raw"]["product"]["title"] = "Товар Низкий"
    good = _ok_result("2")
    md = render_search_digest(_combined([low, good]), reviews_per_product=5)
    head, sep = md.split("## Исключено по рейтингу")
    # в основной части только полный render хорошего товара (нумерация 1)
    assert "Товар Низкий" not in head
    assert "## Товар 1: 2" in head
    assert "## Товар 2:" not in head
    # секция исключённых: pid, рейтинг, порог, заголовок товара
    assert "1 · рейтинг 4.3 (мин. 4.7): Товар Низкий" in sep
    assert "# Товар Низкий" not in sep  # полного render исключённого нет
    # строка в шапке про исключённые
    assert "Исключено по рейтингу: 1 товаров" in md


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


# ---- splitting по размеру файла (default 3 МиБ) ------------------------------

def _review_long(i):
    return _review(f"A{i}", [], text=("Текст отзыва " * 20) + str(i), stars=5)


def _ok_long(pid, n_reviews=80):
    reviews = [_review_long(i) for i in range(n_reviews)]
    return {"pid": pid, "ok": True, "error": None, "cache": "hit", "ocr": "off",
            "raw": _raw(reviews), "markdown": ""}


def _combined_big(n_prods, n_reviews=80):
    return _combined([_ok_long(str(i), n_reviews) for i in range(n_prods)])


def _read_sizes(paths):
    import os
    return [(p, os.path.getsize(p)) for p in paths]


def test_default_max_file_size_bytes_is_3_mib():
    from ozon_collector.report import DEFAULT_MAX_FILE_SIZE_BYTES
    assert DEFAULT_MAX_FILE_SIZE_BYTES == 3 * 1024 * 1024


def test_write_digest_no_split_when_within_limit(tmp_path):
    from ozon_collector.report import write_search_digest
    out = tmp_path / "d.md"
    written = write_search_digest(_combined_big(2), str(out))
    assert written == [str(out)]
    assert out.exists()
    assert not (tmp_path / "d.part2.md").exists()
    assert "Часть" not in out.read_text(encoding="utf-8")


def test_write_digest_default_splits_over_3_mib(tmp_path):
    # суммарный рендер заведомо больше 3 МиБ → должно быть >1 файл,
    # и каждый нормальный файл ≤ 3 МиБ по факту на диске.
    from ozon_collector.report import write_search_digest
    combined = _combined_big(60, n_reviews=100)
    out = tmp_path / "d.md"
    written = write_search_digest(combined, str(out))
    assert len(written) > 1
    sizes = dict(_read_sizes(written))
    for p, sz in sizes.items():
        assert sz <= 3 * 1024 * 1024, f"{p}: {sz}"


def test_write_digest_split_by_small_limit_respects_bytes(tmp_path):
    # маленький лимит → несколько частей, все нормальные файлы ≤ лимита на диске
    from ozon_collector.report import write_search_digest
    limit = 64 * 1024
    out = tmp_path / "d.md"
    written = write_search_digest(_combined_big(40, n_reviews=100), str(out),
                                  max_file_size_bytes=limit)
    assert len(written) > 1
    for p, sz in _read_sizes(written):
        assert sz <= limit, f"{p}: {sz}"


def test_write_digest_parts_numbered_with_n_matching_count(tmp_path):
    from ozon_collector.report import write_search_digest
    limit = 64 * 1024
    out = tmp_path / "d.md"
    written = write_search_digest(_combined_big(40, n_reviews=100), str(out),
                                  max_file_size_bytes=limit)
    n = len(written)
    for k, p in enumerate(written, start=1):
        text = open(p, encoding="utf-8").read()
        assert f"Часть {k} из {n}" in text


def test_write_digest_first_part_is_out_not_part1(tmp_path):
    import os
    from ozon_collector.report import write_search_digest
    limit = 64 * 1024
    out = tmp_path / "d.md"
    written = write_search_digest(_combined_big(40, n_reviews=100), str(out),
                                  max_file_size_bytes=limit)
    assert written[0] == str(out)
    assert not os.path.exists(str(tmp_path / "d.part1.md"))
    assert written[1] == str(tmp_path / "d.part2.md")


def test_write_digest_preserves_product_order(tmp_path):
    import re
    from ozon_collector.report import write_search_digest
    limit = 64 * 1024
    combined = _combined_big(30, n_reviews=100)
    out = tmp_path / "d.md"
    written = write_search_digest(combined, str(out), max_file_size_bytes=limit)
    seen = []
    for p in written:
        text = open(p, encoding="utf-8").read()
        seen.extend(re.findall(r"## Товар \d+: (\d+)", text))
    assert seen == [str(i) for i in range(30)]


def test_write_digest_single_huge_product_allowed_overflow(tmp_path):
    # один товар > лимита → свой файл, размер может превысить лимит (warning),
    # остальные файлы ≤ лимита
    from ozon_collector.report import write_search_digest
    limit = 8 * 1024
    huge = _ok_long("H", n_reviews=100)          # много отзывов → блок большой
    small = _ok_long("1", n_reviews=1)           # маленький блок
    combined = _combined([huge, small, _ok_long("2", n_reviews=1)])
    out = tmp_path / "d.md"
    written = write_search_digest(combined, str(out), max_file_size_bytes=limit)
    assert len(written) >= 2
    huge_paths = [p for p, sz in _read_sizes(written) if sz > limit]
    assert huge_paths, "должен быть файл-переросток для огромного товара"
    assert len(huge_paths) == 1
    # остальные нормальные файлы в лимите
    for p, sz in _read_sizes(written):
        if sz > limit:
            continue
        assert sz <= limit


def test_write_digest_two_huge_products_each_own_file(tmp_path):
    from ozon_collector.report import write_search_digest
    limit = 8 * 1024
    combined = _combined([_ok_long("H1", 100), _ok_long("H2", 100),
                          _ok_long("1", 1)])
    out = tmp_path / "d.md"
    written = write_search_digest(combined, str(out), max_file_size_bytes=limit)
    over = [p for p, sz in _read_sizes(written) if sz > limit]
    assert len(over) == 2


def test_write_digest_zero_or_negative_limit_no_split(tmp_path):
    from ozon_collector.report import write_search_digest
    out = tmp_path / "d.md"
    for limit in (0, -1):
        written = write_search_digest(_combined_big(5), str(out),
                                      max_file_size_bytes=limit)
        assert written == [str(out)]
    assert "Часть" not in out.read_text(encoding="utf-8")


def test_write_digest_empty_products_single_out(tmp_path):
    from ozon_collector.report import write_search_digest
    out = tmp_path / "d.md"
    written = write_search_digest(_combined([]), str(out),
                                  max_file_size_bytes=0)
    assert written == [str(out)]
    assert out.exists()


def test_write_digest_too_small_limit_does_not_create_empty_parts(tmp_path):
    # если отдельные товары не влезают в лимит — они не порождают пустых файлов;
    # каждый товар попадает в непустую часть, файлов ровно столько, сколько групп
    from ozon_collector.report import write_search_digest
    limit = 1024
    combined = _combined_big(20, n_reviews=100)
    out = tmp_path / "d.md"
    written = write_search_digest(combined, str(out), max_file_size_bytes=limit)
    assert written
    for p in written:
        assert "## Товар" in open(p, encoding="utf-8").read()


def test_old_split_api_names_removed_from_code():
    # гарантия: ни в коде, ни в скриптах не осталось старого API
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = ["max_products_per_file", "DEFAULT_MAX_PRODUCTS_PER_FILE"]
    found = []
    for dirpath, _dirs, files in os.walk(os.path.join(root, "ozon_collector")):
        for fn in files:
            if fn.endswith(".py"):
                _scan_file(os.path.join(dirpath, fn), bad, found)
    for fn in ("digest_from_raw.py", "key_daemon.py"):
        _scan_file(os.path.join(root, "scripts", fn), bad, found)
    assert not found, "\n".join(found)


def _scan_file(path, needles, out):
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if any(needle in line for needle in needles):
                out.append(f"{path}:{i}: {line.rstrip()}")


# ---- build_search_digest_combined: сборка combined из raw по pid -------------

def _write_raw(data_dir, pid, title, version=None, n_reviews=1):
    import json
    import os
    if version is None:
        from ozon_collector import VERSION
        version = VERSION
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "raw_fixtures", "shape_minimal.json"),
              encoding="utf-8") as f:
        raw = json.load(f)
    raw["collector_version"] = version
    raw["pid"] = pid
    raw["product_url"] = f"https://www.ozon.ru/product/{pid}"
    raw["reviews_url"] = f"https://www.ozon.ru/product/{pid}?tab=reviews"
    raw["product"] = dict(raw["product"], title=title)
    raw["product"]["characteristics"] = dict(
        raw["product"]["characteristics"], Артикул=pid)
    raw["reviews_collected"] = n_reviews
    with open(data_dir / f"{pid}_raw.json", "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)


def test_build_combined_ok_and_missing(tmp_path):
    from ozon_collector.report import build_search_digest_combined
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_raw(data_dir, "3627230434", "Xiaomi REDMI Buds 8 Active, черные")
    combined, table = build_search_digest_combined(
        ["3627230434", "9999999999"], str(data_dir))
    assert combined["found"] == 2
    assert combined["collected"] == 1
    prods = {p["pid"]: p for p in combined["products"]}
    assert prods["3627230434"]["ok"] is True
    assert prods["3627230434"]["cache"].startswith("raw:")
    assert prods["9999999999"]["ok"] is False
    assert "нет валидных данных" in prods["9999999999"]["error"]
    assert any("SKIP" in line for line in table)
    assert combined["search"]["query"].startswith("артикулы:")


def test_build_combined_query_override(tmp_path):
    from ozon_collector.report import build_search_digest_combined
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_raw(data_dir, "3627230434", "Xiaomi REDMI Buds 8 Active, черные")
    combined, _ = build_search_digest_combined(
        ["3627230434"], str(data_dir), query="тестовая выдача")
    assert combined["search"]["query"] == "тестовая выдача"


# ---- sort_digest_products -------------------------------------------------

def _sort_raw(stars, n_collected):
    reviews = [{"author": "a", "date": "2026-08-01", "stars": stars,
                "text": "отзыв", "comments": []} for _ in range(n_collected)]
    raw = _raw(reviews)
    raw["reviews_collected"] = n_collected
    return raw


def _sort_res(pid, *, ok=True, raw=None):
    return {"pid": pid, "ok": ok, "raw": raw if ok else None}


def test_sort_digest_products_best_avgxcount_first():
    # ключ сортировки — средний балл × число собранных отзывов (решение 2026-09-05):
    # 3×9=27 > 4×5=20 > 5×2=10, хотя средний балл у p2 выше всех
    from ozon_collector.report import sort_digest_products
    products = [
        _sort_res(1, raw=_sort_raw(4, 5)),  # 20
        _sort_res(2, raw=_sort_raw(5, 2)),  # 10 -> последний среди ok
        _sort_res(3, raw=_sort_raw(3, 9)),  # 27 -> первый
    ]
    sort_digest_products(products)
    assert [p["pid"] for p in products] == [3, 1, 2]


def test_sort_digest_products_avgxcount_grows_with_count():
    # при равном среднем балле больше отзывов → выше произведение avg×count
    from ozon_collector.report import sort_digest_products
    products = [
        _sort_res(1, raw=_sort_raw(4, 3)),  # 12
        _sort_res(2, raw=_sort_raw(4, 8)),  # 32 -> первый
        _sort_res(3, raw=_sort_raw(4, 5)),  # 20
    ]
    sort_digest_products(products)
    assert [p["pid"] for p in products] == [2, 3, 1]


def test_sort_digest_products_tie_break_by_pid():
    # одинаковое произведение avg×count (3×4 и 4×3) → tie-break по pid
    from ozon_collector.report import sort_digest_products
    products = [
        _sort_res(2, raw=_sort_raw(4, 3)),  # 12
        _sort_res(1, raw=_sort_raw(3, 4)),  # 12
    ]
    sort_digest_products(products)
    assert [p["pid"] for p in products] == [1, 2]


def test_sort_digest_products_errors_and_no_reviews_last():
    from ozon_collector.report import sort_digest_products
    products = [
        _sort_res(9, ok=False),      # ошибка — в самый конец
        _sort_res(1, raw=_sort_raw(4, 2)),
        _sort_res(2, ok=True, raw=None),  # ok, но без отзывов — ниже
    ]
    sort_digest_products(products)
    assert [p["pid"] for p in products] == [1, 2, 9]


def _raw_sorted(stars_neutral, n_neutral, stars_asc, n_asc,
                rating=None, rating_count=None):
    reviews = []
    for i in range(n_neutral):
        reviews.append({"author": "a", "stars": stars_neutral, "text": "отзыв",
                        "review_sorts": ["published_at_desc"]})
    for i in range(n_asc):
        reviews.append({"author": "a", "stars": stars_asc, "text": "отзыв",
                        "review_sorts": ["score_asc"]})
    raw = _raw(reviews)
    raw["reviews_collected"] = n_neutral + n_asc
    if rating is not None:
        raw["product"]["rating"] = rating
    if rating_count is not None:
        raw["product"]["rating_count"] = rating_count
    return raw


def test_sort_digest_products_ignores_score_asc():
    """Порция «сначала низкие оценки» не участвует ни в рейтинге, ни в балле
    сортировки: 5×10 нейтральных > 4×11 (несмотря на 90 добавленных единиц)."""
    from ozon_collector.report import sort_digest_products
    products = [
        _sort_res(1, raw=_raw_sorted(4, 11, 1, 0)),   # 4×11=44
        _sort_res(2, raw=_raw_sorted(5, 10, 1, 90)),  # 5×10=50 -> первый
    ]
    sort_digest_products(products)
    assert [p["pid"] for p in products] == [2, 1]


def test_sort_digest_products_official_fallback_no_multiplier():
    """Нет нейтральной выборки → рейтинг карточки без множителя (ниже имеющих
    рейтинг варианта, но выше товаров без рейтинга вовсе)."""
    from ozon_collector.report import sort_digest_products
    products = [
        _sort_res(2, raw=None),                              # без рейтинга
        _sort_res(3, ok=False),                              # ошибка — в конец
        _sort_res(1, raw=_raw_sorted(0, 0, 1, 5,
                                     rating=4.8, rating_count=3000)),
    ]
    sort_digest_products(products)
    assert [p["pid"] for p in products] == [1, 2, 3]


def test_variant_rating_excludes_score_asc_from_render():
    """В карточке рейтинг по нейтральным отзывам (без score_asc), а официальный
    рейтинг карточки выводится справочной строкой."""
    raw = _raw_sorted(5, 2, 1, 2, rating=4.9, rating_count=365142)
    md = render(raw)
    assert ("Рейтинг по собранным отзывам (без «сначала низкие»): "
            "5.000 / 5 (2 оценки)") in md
    assert "- Ozon (вся карточка): 4.900 / 5 · 365142 оценки" in md


def test_variant_rating_only_score_asc_falls_back_to_card():
    """Все отзывы из score_asc → рейтинга варианта нет, карточка с пометкой."""
    raw = _raw_sorted(0, 0, 1, 5, rating=4.2)
    md = render(raw)
    assert "Рейтинг по собранным отзывам" not in md
    assert ("Рейтинг карточки Ozon: 4.200 / 5 "
            "_(нет нейтральной выборки)_") in md


def test_digest_rating_variant_wins_over_official():
    """При наличии нейтральной выборки берётся вариант, даже если карточка выше."""
    from ozon_collector.report import digest_rating
    raw = _raw_sorted(3, 10, 1, 90, rating=4.9, rating_count=3000)
    rating, n, src = digest_rating(raw["product"], raw["reviews"])
    assert (rating, n, src) == (3.0, 10, "variant")


def test_official_rating_info_from_counts():
    """Полное распределение customer_opinion.counts точнее product.rating."""
    from ozon_collector.report import official_rating_info
    product = {
        "rating": 4.9,
        "rating_count": 365142,
        "customer_opinion": {"counts": {"5": 340899, "4": 19582,
                                        "3": 2271, "2": 820, "1": 1570}},
    }
    rating, count = official_rating_info(product)
    assert count == 365142
    assert abs(rating - 4.90) < 0.05


def test_official_rating_info_no_data():
    from ozon_collector.report import official_rating_info
    assert official_rating_info(None) == (None, None)
    assert official_rating_info({}) == (None, None)
    assert official_rating_info({"customer_opinion": {"stars": {}}}) == (None, None)


# ---- доля негативных оценок (только полное распределение Ozon) ---------------

def test_official_negative_share_from_counts():
    """Доля негатива (1–2★) берётся ТОЛЬКО из полного распределения
    customer_opinion.counts (генеральная совокупность), а не из собранных
    отзывов (score_asc смещает выборку)."""
    from ozon_collector.report import official_negative_share
    product = {
        "rating": 4.9,
        "rating_count": 500,
        "customer_opinion": {"counts": {"5": 100, "4": 100, "3": 100,
                                        "2": 100, "1": 100}},
    }
    share, total = official_negative_share(product)
    assert total == 500
    assert abs(share - 0.4) < 1e-9


def test_official_negative_share_ignores_rating_without_counts():
    """Без распределения counts долю не считаем: product.rating один не даёт
    доли негатива, и никакого процента по собранной выборке не выдумываем."""
    from ozon_collector.report import official_negative_share
    assert official_negative_share(None) == (None, None)
    assert official_negative_share({}) == (None, None)
    assert official_negative_share({"rating": 4.2, "rating_count": 3000}) == (None, None)
    assert official_negative_share({"customer_opinion": {"stars": {}}}) == (None, None)


def test_render_negative_share_line_from_counts():
    """В карточке выводится официальная доля негатива с базой «вся карточка Ozon»."""
    raw = _raw_sorted(5, 2, 1, 2)
    raw["product"]["customer_opinion"] = {
        "counts": {"5": 93, "4": 4, "3": 1, "2": 1, "1": 1},
    }
    md = render(raw)
    assert "- Негативных оценок (1–2★): 2.0% · по 100 оценкам (вся карточка Ozon)" in md


def test_render_no_negative_line_without_counts():
    """Распределения нет → строки о доле негатива нет вовсе (ни официальной, ни
    «по собранным»): никакой ложной статистики по смещённой выборке."""
    raw = _raw_sorted(5, 2, 1, 2, rating=4.9, rating_count=365142)
    md = render(raw)
    assert "Негативных оценок" not in md


# ---- разделение секций отзывов (обычная / диагностическая) ------------------

def _raw_split(neutral, diag, sorts=("published_at_desc", "score_asc")):
    """raw с двумя группами отзывов: neutral (без score_asc) и diag (с ним).
    Порядок в reviews сохраняется: нейтральные не перемешиваются с diag."""
    revs = [rev for stars, text in neutral
            for rev in [{"author": "a", "stars": stars, "text": text,
                         "review_sorts": ["published_at_desc"]}]]
    revs += [rev for stars, text in diag
             for rev in [{"author": "a", "stars": stars, "text": text,
                          "review_sorts": ["score_asc"]}]]
    raw = _raw(revs)
    raw["reviews_sorts"] = list(sorts)
    return raw


def test_render_reviews_sections_split():
    """Обычная выборка идёт первой, диагностическая — второй с предупреждением;
    каждого отзыва из своей группы попадает в свою секцию."""
    raw = _raw_split(neutral=[(5, "отличный"), (4, "хороший")],
                     diag=[(1, "брак"), (2, "комкается")])
    md = render(raw)
    assert "### Отзывы покупателей — обычная выборка (2 отзыва)" in md
    assert ("### Негативные отзывы — целенаправленный поиск проблем (2 отзыва)"
            in md)
    assert "долей недовольных покупателей" in md
    # порядок секций: обычная выше негативной
    assert md.index("обычная выборка") < md.index("Негативные отзывы")
    # группировка по секциям
    assert md.index("отличный") < md.index("брак")


def test_render_reviews_mixed_score_asc_goes_to_diagnostic():
    """Отзыв с метками ['published_at_desc','score_asc'] идёт в диагностическую
    секцию — ровно как исключается из рейтинга варианта (_is_score_asc).
    Инвариант: «обычная» выборка = выборка variant_rating_from."""
    reviews = [
        {"author": "a", "stars": 5, "text": "норм", "review_sorts": ["published_at_desc"]},
        {"author": "b", "stars": 1, "text": "очень плохо", "review_sorts": ["published_at_desc", "score_asc"]},
        {"author": "c", "stars": 5, "text": "ещё норм", "review_sorts": ["published_at_desc"]},
    ]
    raw = _raw(reviews)
    md = render(raw)
    # секции
    neutral_start = md.index("### Отзывы покупателей — обычная выборка")
    diag_start = md.index("### Негативные отзывы")
    assert "норм" in md[neutral_start:diag_start]
    assert "очень плохо" not in md[neutral_start:diag_start]
    assert "очень плохо" in md[diag_start:]
    # рейтинг варианта не учитывает второй отзыв
    from ozon_collector.report import digest_rating
    rating, n, src = digest_rating(raw["product"], reviews)
    assert (rating, n, src) == (5.0, 2, "variant")


def test_render_reviews_only_score_asc_single_section():
    """Все отзывы из score_asc → только диагностическая секция с предупреждением,
    «обычной» нет (рейтинг варианта недоступен — фолбэк карточки в разделе 1)."""
    raw = _raw_split(neutral=[], diag=[(1, "плохо"), (2, "так себе")])
    md = render(raw)
    assert "обычная выборка" not in md
    assert "### Негативные отзывы — целенаправленный поиск проблем (2 отзыва)" in md
    assert "долей недовольных покупателей" in md


def test_render_reviews_without_score_asc_single_section():
    """Без score_asc — одна секция «обычная выборка», диагностической нет."""
    raw = _raw_split(neutral=[(5, "отличный"), (4, "хороший")], diag=[])
    md = render(raw)
    assert "### Отзывы покупателей — обычная выборка (2 отзыва)" in md
    assert "Негативные отзывы" not in md


def test_render_reviews_sections_order_preserved():
    """Порядок отзывов внутри каждой секции = исходный порядок reviews
    (простая фильтрация, без пересортировки)."""
    raw = _raw_split(neutral=[(5, "ТОК-А"), (4, "ТОК-Б"), (5, "ТОК-В")],
                     diag=[(1, "ТОК-Г"), (2, "ТОК-Д"), (1, "ТОК-Е")])
    md = render(raw)
    n_start = md.index("### Отзывы покупателей — обычная выборка")
    d_start = md.index("### Негативные отзывы")
    n_block = md[n_start:d_start]
    d_block = md[d_start:]
    n_pos = [n_block.index(x) for x in ("ТОК-А", "ТОК-Б", "ТОК-В")]
    d_pos = [d_block.index(x) for x in ("ТОК-Г", "ТОК-Д", "ТОК-Е")]
    assert n_pos == sorted(n_pos)
    assert d_pos == sorted(d_pos)


def test_render_reviews_final_totals():
    """После обеих секций выводится итоговая суммарная строка по ВСЕМ собранным
    отзывам (обычная + диагностическая), с корректными счётчиками звёзд."""
    raw = _raw_split(neutral=[(5, "н1"), (4, "н2")],
                     diag=[(2, "д1"), (1, "д2")])
    raw["stars_full"] = {"total": 4, "counts": {1: 1, 2: 1, 3: 0, 4: 1, 5: 1}}
    md = render(raw)
    assert ("Итог по всем собранным отзывам: 4 отзыва · ★5: 1 · ★4: 1 · ★2: 1 · ★1: 1"
            in md)
    # пер-секционные счётчики тоже присутствуют
    assert "★5: 1 · ★4: 1" in md
    assert "★2: 1 · ★1: 1" in md


# ---- _top_reviews ---------------------------------------------------------

def _rv(helpful_yes=0, helpful_no=0, comments_count=0, publishedAt="0", text="текст"):
    return {"uuid": "u", "helpful_yes": helpful_yes, "helpful_no": helpful_no,
            "comments_count": comments_count, "publishedAt": publishedAt,
            "text": text}


def test_top_reviews_by_usefulness():
    """Приоритет — сумма полезности (yes+no), затем комментарии, затем новизна."""
    reviews = [
        _rv(helpful_yes=1, helpful_no=0),          # 1
        _rv(helpful_yes=5, helpful_no=5),          # 10
        _rv(helpful_yes=2, helpful_no=3),          # 5
    ]
    top = _top_reviews(reviews, 2)
    assert [r["helpful_yes"] + r["helpful_no"] for r in top] == [10, 5]


def test_top_reviews_excludes_no_text_when_text_exists():
    """Пустые (без текста) выкидываются из топа, если есть хоть один текстовый."""
    reviews = [
        _rv(helpful_yes=100, text=""),
        _rv(helpful_yes=1, text="с текстом"),
        _rv(helpful_yes=2, text=""),
    ]
    top = _top_reviews(reviews, 3)
    assert len(top) == 1
    assert top[0]["text"] == "с текстом"


def test_top_reviews_no_text_fills_when_all_empty():
    """Если все отзывы без текста — топаются как есть (иначе потеряем всё)."""
    reviews = [_rv(text=""), _rv(text="", helpful_yes=5)]
    top = _top_reviews(reviews, 1)
    assert len(top) == 1
    assert top[0]["helpful_yes"] == 5


def test_top_reviews_does_not_pad_to_n():
    """Пустышками до n НЕ добираем: значимых меньше n → вернётся сколько есть."""
    reviews = [_rv(helpful_yes=1, text="очень хороший звук"),
               _rv(helpful_yes=2, text="")]
    top = _top_reviews(reviews, 10)
    assert len(top) == 1
    assert top[0]["text"] == "очень хороший звук"


def test_top_reviews_excludes_name_only_text():
    """Отзыв с текстом = только имя автора (М\\nМаксим С.) — пустышка, в топ не идёт."""
    reviews = [
        {"uuid": "u1", "author": "Максим С.", "text": "М\nМаксим С.",
         "helpful_yes": 100, "comments_count": 0, "publishedAt": "0"},
        {"uuid": "u2", "author": "Иван Н.", "text": "Наушники шикарные, бас глубокий.",
         "helpful_yes": 1, "comments_count": 0, "publishedAt": "0"},
    ]
    top = _top_reviews(reviews, 10)
    assert [r["uuid"] for r in top] == ["u2"]


def test_review_text_sig_name_only():
    """Текст == имя автора (с/без первой буквы) → незначимый."""
    assert _review_text_sig({"author": "Максим С.", "text": "М\nМаксим С."}) is False
    assert _review_text_sig({"author": "Ильмир", "text": "Ильмир"}) is False
    assert _review_text_sig({"author": "Имя скрыто", "text": "А\nИмя скрыто"}) is False
    assert _review_text_sig({"author": "Андрей С.", "text": "Андрей С."}) is False


def test_review_text_sig_meaningful():
    """Реальный текст с пометкой автора → значимый."""
    assert _review_text_sig(
        {"author": "Максим С.", "text": "Максим С.\nОтличные наушники за свои деньги"}) is True
    assert _review_text_sig({"author": "Иван", "text": "Хорошее качество звука"}) is True
    assert _review_text_sig({"author": "", "text": "Нормально"}) is True
    assert _review_text_sig({"author": "", "text": ""}) is False


def test_top_reviews_stable_index_breaks_ties():
    """При равной метрике сохраняется исходный порядок."""
    a = _rv(helpful_yes=1, publishedAt="100", text="a")
    b = _rv(helpful_yes=1, publishedAt="100", text="b")
    assert [r["text"] for r in _top_reviews([a, b], 1)] == ["a"]


def test_top_reviews_lower_usefulness_newer_loses():
    """Новизна — только третичный ключ: более полезный старее побеждает."""
    reviews = [
        _rv(helpful_yes=0, publishedAt="9999999999", text="новый, но не полезный"),
        _rv(helpful_yes=3, publishedAt="1", text="старый, полезный"),
    ]
    top = _top_reviews(reviews, 1)
    assert top[0]["text"] == "старый, полезный"


def test_top_reviews_respects_n_and_returns_fewer():
    """n меньше числа отзывов → возвращается ровно n."""
    reviews = [_rv(i, text=f"t{i}") for i in range(10)]
    assert len(_top_reviews(reviews, 4)) == 4
    assert len(_top_reviews(reviews, 0)) == 0
    assert _top_reviews([], 5) == []
