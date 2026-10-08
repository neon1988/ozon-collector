"""Негативные отзывы не должны выпадать из дайджеста (регрессия 2026-10-06).

`_top_reviews` ранжировал всё по полезности, без квоты на диагностическую
порцию `score_asc`. При 150 популярных пятёрках с голосами «полезно» и 50
диагностических отзывах в срез 100 попадали только пятёрки: секция
«Негативные отзывы» не печаталась вовсе, хотя в строке итогов честно стояло
«★1: 50». Оператор получал дайджест без единого плохого отзыва.

Живой браузер не поднимается: проверяется чистая функция `_top_reviews` и
рендер `render_search_digest` на синтетическом raw.
"""
from __future__ import annotations

import ozon_collector.report as R


def _rev(stars, text, sort, helpful=0, idx=0):
    return {
        "stars": stars,
        "text": text,
        "review_sort": sort,
        "review_sorts": [sort],
        "helpful_yes": helpful,
        "helpful_no": 0,
        "comments_count": 0,
        "publishedAt": f"2026-01-{(idx % 28) + 1:02d}T00:00:00",
    }


def test_diagnostic_reviews_survive_the_cut():
    """50 негативных не исчезают при 150 популярных пятёрках."""
    reviews = [_rev(5, f"нормально {i}", "published_at_desc", helpful=10, idx=i)
               for i in range(150)]
    reviews += [_rev(1, f"плохо {i}", "score_asc", helpful=0, idx=i)
                for i in range(50)]
    top = R._top_reviews(reviews, 100)
    diag_left = [r for r in top if R._is_score_asc(r)]
    assert diag_left, "в срезе не осталось ни одного диагностического отзыва"
    assert len(diag_left) == 33, "квота должна быть треть отзыва (100 // 3)"
    assert len(top) == 100, "срез всё равно полный"


def test_quota_neutralises_expensive_five_stars():
    """Ровно та картина из ревью: 150×5★ + 50×1★, срез 100."""
    reviews = [_rev(5, f"хорошо {i}", "published_at_desc", helpful=10, idx=i)
               for i in range(150)]
    reviews += [_rev(1, f"ужасно {i}", "score_asc", helpful=0, idx=i)
                for i in range(50)]
    top = R._top_reviews(reviews, 100)
    stars = [r["stars"] for r in top]
    assert 1 in stars, "в дайджесте не осталось ни одной единицы"
    assert 5 in stars, "обычная выборка должна сохраниться тоже"


def test_no_diagnostic_reviews_means_pure_usefulness():
    """Если диагностической порции нет — поведение прежнее, по полезности."""
    reviews = [_rev(5, f"норм {i}", "published_at_desc", helpful=10 - i, idx=i)
               for i in range(50)]
    top = R._top_reviews(reviews, 10)
    assert len(top) == 10
    assert [r["helpful_yes"] for r in top] == list(range(10, 0, -1))


def test_cut_not_exceeded_when_diag_is_small():
    """Диагностических мало — не раздуваем срез сверх n."""
    reviews = [_rev(5, f"хорошо {i}", "published_at_desc", helpful=10, idx=i)
               for i in range(20)]
    reviews += [_rev(1, "плохо", "score_asc", helpful=0, idx=99)]
    top = R._top_reviews(reviews, 5)
    assert len(top) == 5
    assert any(R._is_score_asc(r) for r in top), "единственный плохой не потерян"


def test_all_reviews_fit_returns_everything():
    reviews = [_rev(5, f"ок {i}", "published_at_desc", helpful=1, idx=i)
               for i in range(3)]
    reviews.append(_rev(1, "плохо", "score_asc", idx=9))
    assert len(R._top_reviews(reviews, 100)) == 4


def test_digest_prints_negative_section_when_cut_kept_diag():
    """Секция «Негативные отзывы» появляется в самом дайджесте."""
    reviews = [_rev(5, f"хорошо {i}", "published_at_desc", helpful=10, idx=i)
               for i in range(40)]
    reviews += [_rev(1, f"плохо {i}", "score_asc", helpful=0, idx=i)
                for i in range(10)]
    raw = {
        "pid": "1",
        "product": {"title": "товар", "url": "https://www.ozon.ru/product/1/"},
        "reviews": R._top_reviews(reviews, 10),
        "reviews_variant_mode": {"used": True, "variant_count": 50,
                                 "total_count": 50, "matched_variant": 50},
    }
    md = R.render(raw)
    assert "Негативные отзывы" in md, "в дайджесте нет секции негативных отзывов"
    assert "★1:" in md, "в итогах нет единиц — диагностика не дошла до отчёта"