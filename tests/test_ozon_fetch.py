"""ozon_fetch: разбор ответа entrypoint-api с виджетом отзывов (без сети)."""
import json

from ozon_collector import ozon_fetch


class _FakePage:
    """Подмена page.evaluate: отдаёт заранее заданный ответ fetch."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def evaluate(self, js, *args, **kwargs):
        self.calls.append((js, args, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _payload(**widget):
    return {"widgetStates": {"webListReviews-1-reviewshelfpaginator-1":
                             json.dumps(widget)}}


class TestReviewsUrl:
    def test_contains_pid_and_index(self):
        url = ozon_fetch.reviews_url("1590289080", 2)
        assert "1590289080" in url
        assert "layout_page_index=2" in url
        assert url.startswith("https://www.ozon.ru/api/entrypoint-api.bx/")

    def test_index_defaults_to_first(self):
        assert "layout_page_index=1" in ozon_fetch.reviews_url("42")


class TestFetchJson:
    def test_parses_json_body(self):
        page = _FakePage({"s": 200, "t": '{"a": 1}'})
        assert ozon_fetch.fetch_json(page, "https://x/y") == {"a": 1}

    def test_credentials_included(self):
        page = _FakePage({"s": 200, "t": "{}"})
        ozon_fetch.fetch_json(page, "https://x/y")
        assert "credentials: 'include'" in page.calls[0][0]

    def test_non_200_gives_none(self):
        assert ozon_fetch.fetch_json(_FakePage({"s": 502, "t": "oops"}), "u") is None

    def test_fetch_error_gives_none(self):
        assert ozon_fetch.fetch_json(_FakePage({"err": "blocked"}), "u") is None

    def test_broken_json_gives_none(self):
        assert ozon_fetch.fetch_json(_FakePage({"s": 200, "t": "not-json"}), "u") is None

    def test_transport_exception_gives_none(self):
        assert ozon_fetch.fetch_json(_FakePage(RuntimeError("dead")), "u") is None


class _StrictPlaywrightPage:
    """Страница со строгой сигнатурой Playwright: evaluate(expression, arg).

    Chromium-движок (дефолт боевого сбора) не принимает ни лишних позиционных
    аргументов, ни keyword timeout=. Регрессия 2026-09-25: fetch_json звал
    evaluate(js, url, timeout_ms) и падал с TypeError вживую, а фейки с
    (*args, **kwargs) это скрывали.
    """

    def __init__(self, response):
        self.response = response
        self.calls = []

    def evaluate(self, expression, arg=None):
        self.calls.append((expression, arg))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class TestFetchJsonPlaywrightSignature:
    def test_single_arg_list_and_no_timeout_kwarg(self):
        page = _StrictPlaywrightPage({"s": 200, "t": '{"a": 1}'})
        assert ozon_fetch.fetch_json(page, "https://x/y", timeout_ms=5000) == {"a": 1}
        expr, arg = page.calls[0]
        assert isinstance(arg, list) and arg[0] == "https://x/y" and arg[1] == 5000

    def test_timeout_enforced_inside_js(self):
        page = _StrictPlaywrightPage({"s": 200, "t": "{}"})
        ozon_fetch.fetch_json(page, "u", timeout_ms=1234)
        expr = page.calls[0][0]
        assert "AbortController" in expr and "clearTimeout" in expr


class TestFindReviewsWidget:
    def test_parses_nested_json_string(self):
        w = ozon_fetch.find_reviews_widget(_payload(reviews=[{"id": 1}]))
        assert w["reviews"] == [{"id": 1}]

    def test_accepts_already_parsed_dict(self):
        payload = {"widgetStates": {"webListReviews-x": {"reviews": []}}}
        assert ozon_fetch.find_reviews_widget(payload) == {"reviews": []}

    def test_skips_other_widgets(self):
        payload = {"widgetStates": {"webSearch-1": "{}", "webListReviews-2": "{}"}}
        assert ozon_fetch.find_reviews_widget(payload) == {}

    def test_broken_widget_string_skipped(self):
        payload = {"widgetStates": {"webListReviews-1": "{{{", "webListReviews-2": "{}"}}
        assert ozon_fetch.find_reviews_widget(payload) == {}

    def test_missing_states(self):
        assert ozon_fetch.find_reviews_widget({}) is None
        assert ozon_fetch.find_reviews_widget(None) is None
        assert ozon_fetch.find_reviews_widget({"widgetStates": []}) is None


class TestFetchReviewsPage:
    def test_returns_reviews_and_paging(self):
        page = _FakePage({"s": 200, "t": json.dumps(_payload(
            reviews=[{"id": 1}], nextPage="https://n", pageToken="tok"))})
        out = ozon_fetch.fetch_reviews_page(page, "42")
        assert out["reviews"] == [{"id": 1}]
        assert out["next_page"] == "https://n"
        assert out["page_token"] == "tok"
        assert "layout_page_index=1" in page.calls[0][1][0][0]

    def test_missing_widget_gives_none(self):
        page = _FakePage({"s": 200, "t": '{"widgetStates": {}}'})
        assert ozon_fetch.fetch_reviews_page(page, "42") is None

    def test_empty_widget_defaults(self):
        page = _FakePage({"s": 200, "t": json.dumps(_payload())})
        out = ozon_fetch.fetch_reviews_page(page, "42")
        assert out["reviews"] == []
        assert out["next_page"] == ""
        assert out["page_token"] == ""
