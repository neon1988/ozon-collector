"""Opt-in API-предсрез отзывов: нормализация, self-validation, безопасный merge.

Критика: «API не подключён — 12-15 с на товар можно срезать до 1 с». Проверка
показала: на анонимной сессии API отдаёт те же 4 отзыва, что и DOM, и без
токенов страницы (layout_page_index) пагинация не идёт. Поэтому модуль
включён как УСКОРИТЕЛЬ-следствие, а не как источник истины: DOM главный,
API только дополняет и не имеет права докручивать/заменять сбор, пока
покрытие не доказано счётчиком rating_count.
"""
import json
from unittest.mock import patch

import pytest

from ozon_collector import collect as C
from ozon_collector import ozon_fetch


def _api_payload(reviews, next_page="", page_token=""):
    return {"s": 200, "t": json.dumps({"widgetStates": {
        "webListReviews-1-reviewshelfpaginator-1": json.dumps(
            {"reviews": reviews, "nextPage": next_page,
             "pageToken": page_token})}})}


class _FakePage:
    """Страница, отдающая заранее заданные ответы fetch по порядку."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def evaluate(self, js, arg=None):
        self.urls.append(arg[0] if isinstance(arg, list) else arg)
        res = self.responses.pop(0) if self.responses else {"s": 500, "t": ""}
        if isinstance(res, Exception):
            raise res
        return res


class TestNormalizeReview:
    def test_nested_author_object(self):
        rec = ozon_fetch.normalize_review(
            {"uuid": "u1", "author": {"name": "Иван"},
             "text": "норм", "rating": 5, "createdAt": "1 мая"})
        assert rec["uuid"] == "u1"
        assert rec["author"] == "Иван"
        assert rec["stars"] == 5, "отчёт читает stars, не rating"
        assert rec["date"] == "1 мая"
        assert rec["source"] == "api"

    def test_numeric_id_becomes_string(self):
        assert ozon_fetch.normalize_review({"id": 77, "text": "t"})["uuid"] == "77"

    def test_alternative_field_names(self):
        rec = ozon_fetch.normalize_review(
            {"id": "x", "reviewText": "текст", "score": 3})
        assert rec["text"] == "текст" and rec["stars"] == 3

    def test_no_uuid_dropped(self):
        assert ozon_fetch.normalize_review({"text": "без id"}) is None

    def test_unknown_shape_dropped(self):
        """Мусор в raw хуже отсутствия отзыва: fallback на DOM всегда есть."""
        assert ozon_fetch.normalize_review({"uuid": "u", "foo": "bar"}) is None
        assert ozon_fetch.normalize_review("строка") is None
        assert ozon_fetch.normalize_review(None) is None


class TestApiPlan:
    def test_allowed_without_variant(self):
        ok, why = ozon_fetch.api_plan({})
        assert ok is True and why == "ok"

    def test_refused_for_variant(self):
        """API не умеет фильтр «Этот вариант» — отзыв чужого варианта попадёт."""
        ok, why = ozon_fetch.api_plan({"Цвет": "белый"})
        assert ok is False and "variant_filter" in why

    def test_empty_variant_values_ignored(self):
        assert ozon_fetch.api_plan({"Цвет": ""})[0] is True

    def test_no_dom_extras_reason(self):
        ok, why = ozon_fetch.api_plan(None, need_dom_extras=False)
        assert ok is True and why == "no_dom_extras"


class TestApiSliceComplete:
    def test_complete_when_covering_count(self):
        assert ozon_fetch.api_slice_complete(4, 4) is True
        assert ozon_fetch.api_slice_complete(5, 4) is True

    def test_not_complete_below_count(self):
        assert ozon_fetch.api_slice_complete(4, 370) is False

    def test_unknown_count_never_complete(self):
        assert ozon_fetch.api_slice_complete(10, None) is False
        assert ozon_fetch.api_slice_complete(10, 0) is False


class TestFetchSlice:
    def test_single_page_normalizes(self):
        page = _FakePage([_api_payload([{"uuid": "a", "text": "x", "rating": 5}])])
        out = ozon_fetch.fetch_slice(page, "42")
        assert out["fetched"] == 1 and out["pages"] == 1
        assert out["reviews"][0]["uuid"] == "a"
        assert out["next_page_seen"] is False

    def test_follows_next_page_url(self):
        page = _FakePage([
            _api_payload([{"uuid": "a", "text": "x"}], next_page="https://n/p2"),
            _api_payload([{"uuid": "b", "text": "y"}]),
        ])
        out = ozon_fetch.fetch_slice(page, "42", max_pages=2)
        assert out["fetched"] == 2 and out["next_page_seen"] is True
        assert page.urls[1] == "https://n/p2", (
            "вторую страницу берём только по URL из nextPage, а не по "
            "синтетическому layout_page_index (он игнорируется сервером)")

    def test_dedupes_across_pages(self):
        page = _FakePage([
            _api_payload([{"uuid": "a", "text": "x"}], next_page="https://n/p2"),
            _api_payload([{"uuid": "a", "text": "x"}]),
        ])
        assert ozon_fetch.fetch_slice(page, "42", max_pages=2)["fetched"] == 1

    def test_transport_failure_is_reported(self):
        page = _FakePage([{"s": 502, "t": ""}])
        out = ozon_fetch.fetch_slice(page, "42")
        assert out["fetched"] == 0 and out["error"] == "fetch_failed"

    def test_unknown_shape_yields_empty_not_garbage(self):
        page = _FakePage([_api_payload([{"foo": 1}, "мусор"])])
        assert ozon_fetch.fetch_slice(page, "42")["fetched"] == 0


class TestMerge:
    def test_api_adds_only_missing(self):
        dom = [{"uuid": "a", "text": "из DOM"}]
        api = [{"uuid": "a", "text": "из API"}, {"uuid": "b", "text": "новый"}]
        merged, stats = ozon_fetch.merge_reviews_api(dom, api)
        assert [r["uuid"] for r in merged] == ["a", "b"]
        assert merged[0]["text"] == "из DOM", "DOM — источник истины"
        assert stats == {"api_added": 1, "api_dupes": 1, "api_dropped": 0,
                         "api_input": 2}

    def test_empty_dom_takes_all_api(self):
        merged, stats = ozon_fetch.merge_reviews_api([], [{"uuid": "a"}])
        assert len(merged) == 1 and stats["api_added"] == 1

    def test_no_api_noop(self):
        merged, stats = ozon_fetch.merge_reviews_api([{"uuid": "a"}], [])
        assert len(merged) == 1 and stats["api_added"] == 0

    def test_records_without_uuid_counted_dropped(self):
        merged, stats = ozon_fetch.merge_reviews_api([], [{"text": "без id"}])
        assert merged == [] and stats["api_dropped"] == 1


class TestReviewsApiEnabled:
    def test_default_off(self):
        with patch.dict("os.environ", {}, clear=True):
            assert C.reviews_api_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "YES", "on"])
    def test_env_truthy(self, val):
        with patch.dict("os.environ", {C.REVIEWS_API_ENV: val}, clear=True):
            assert C.reviews_api_enabled() is True

    def test_explicit_flag_wins_over_env(self):
        with patch.dict("os.environ", {C.REVIEWS_API_ENV: "1"}, clear=True):
            assert C.reviews_api_enabled(False) is False


class TestPrepass:
    def test_disabled_does_nothing(self):
        tel = C._reviews_api_prepass(_FakePage([]), "42", {}, 370, None, False)
        assert tel["used"] is False and tel["reason"] == "disabled"
        assert tel["reviews"] == []

    def test_no_pid_skips(self):
        tel = C._reviews_api_prepass(_FakePage([]), "unknown", {}, 370, None,
                                     True)
        assert tel["reason"] == "no_pid" and tel["used"] is False

    def test_variant_blocks_api(self):
        tel = C._reviews_api_prepass(_FakePage([]), "42", {"Цвет": "белый"},
                                     370, None, True)
        assert tel["used"] is False
        assert "variant_filter" in tel["reason"]

    def test_partial_when_below_rating_count(self):
        page = _FakePage([_api_payload([{"uuid": "a", "text": "x"}])])
        tel = C._reviews_api_prepass(page, "42", {}, 370, None, True)
        assert tel["used"] is True and tel["fetched"] == 1
        assert tel["complete"] is False and tel["reason"] == "partial"
        assert tel["elapsed_ms"] >= 0

    def test_complete_when_covers_rating_count(self):
        page = _FakePage([_api_payload([{"uuid": "a", "text": "x"}])])
        tel = C._reviews_api_prepass(page, "42", {}, 1, None, True)
        assert tel["complete"] is True and tel["reason"] == "complete"

    def test_multi_sort_never_short_circuits(self):
        """Мульти-сорт даёт разные выборки — API их не заменяет."""
        page = _FakePage([_api_payload([{"uuid": "a", "text": "x"}])])
        tel = C._reviews_api_prepass(page, "42", {}, 1,
                                     ["published_at_desc"], True)
        assert tel["complete"] is False

    def test_error_is_swallowed(self):
        """Ошибка транспорта не должна ронять товар — DOM остаётся путём."""
        page = _FakePage([RuntimeError("page gone")])
        tel = C._reviews_api_prepass(page, "42", {}, 370, None, True)
        assert tel["used"] is False and tel["fetched"] == 0
        assert tel["reason"] in ("fetch_failed",) or tel["reason"].startswith(
            "error:")

    def test_unexpected_error_in_wrapper_swallowed(self):
        with patch.object(ozon_fetch, "fetch_slice",
                          side_effect=ValueError("boom")):
            tel = C._reviews_api_prepass(_FakePage([]), "42", {}, 370, None,
                                         True)
        assert tel["used"] is False
        assert tel["reason"] == "error:ValueError"
        assert tel["reviews"] == []


class _FakeSess:
    page = object()


class _StageRecorder:
    """Подмена collect_reviews: запоминает глубину докрутки и отдаёт DOM-список."""

    def __init__(self, dom):
        self.dom = dom
        self.max_pages = None

    def __call__(self, page, rurl, target_variant, max_pages=12, **kw):
        self.max_pages = max_pages
        return list(self.dom), 1, [{"page": 1}], {"used": True, "reason": "dom"}


def _stage(recorder, pid="42", rating_count=370, sorts=None, api=True,
           api_reviews=None):
    from unittest.mock import patch as _patch
    with _patch.object(C, "collect_reviews", recorder), \
         _patch.object(ozon_fetch, "fetch_slice",
                       return_value={"reviews": api_reviews or [],
                                     "pages": 1, "fetched": len(api_reviews or []),
                                     "next_page_seen": False,
                                     "complete_url": "", "error": ""}):
        return C._collect_reviews_stage(
            _FakeSess(), "https://ozon.ru/p/42/?tab=reviews", {}, rating_count,
            pid, 12, 40, True, sorts, {}, False, api)


class TestReviewsStage:
    def test_off_by_default_keeps_full_dom_depth(self):
        rec = _StageRecorder([{"uuid": "d1", "text": "dom"}])
        reviews, pages, _pl, _vm, tel = _stage(rec, api=False)
        assert rec.max_pages == 12, "без API докручиваем как раньше"
        assert tel["used"] is False
        assert [r["uuid"] for r in reviews] == ["d1"]

    def test_partial_api_does_not_limit_dom(self):
        rec = _StageRecorder([{"uuid": "d1"}])
        _reviews, _p, _pl, _vm, tel = _stage(
            rec, rating_count=370, api_reviews=[{"uuid": "a", "text": "x"}])
        assert rec.max_pages == 12, "покрытие не доказано — DOM докручиваем полностью"
        assert tel["api_added"] == 1

    def test_proven_coverage_limits_dom_to_one_page(self):
        rec = _StageRecorder([{"uuid": "d1"}])
        _reviews, _p, _pl, _vm, tel = _stage(
            rec, rating_count=1, api_reviews=[{"uuid": "a", "text": "x"}])
        assert rec.max_pages == 1
        assert tel["dom_short_circuit"] is True

    def test_stage_multi_sort_no_dom_short_circuit(self):
        rec = _StageRecorder([{"uuid": "d1"}])
        _reviews, _p, _pl, _vm, tel = _stage(
            rec, rating_count=1, sorts=["score_asc"],
            api_reviews=[{"uuid": "a", "text": "x"}])
        assert rec.max_pages == 12
        assert tel["dom_short_circuit"] is False

    def test_dom_wins_on_conflict(self):
        rec = _StageRecorder([{"uuid": "a", "text": "из DOM"}])
        reviews, _p, _pl, _vm, tel = _stage(
            rec, rating_count=1, api_reviews=[{"uuid": "a", "text": "из API"}])
        assert reviews[0]["text"] == "из DOM"
        assert tel["api_dupes"] == 1 and tel["api_added"] == 0

    def test_telemetry_reports_timing_and_counts(self):
        rec = _StageRecorder([{"uuid": "d1"}])
        _r, _p, _pl, _vm, tel = _stage(
            rec, rating_count=2, api_reviews=[{"uuid": "a"}, {"uuid": "b"}])
        for key in ("enabled", "used", "fetched", "pages", "complete",
                    "reason", "elapsed_ms", "api_added", "dom_short_circuit"):
            assert key in tel, f"в телеметрии нет {key}"
        assert tel["fetched"] == 2
