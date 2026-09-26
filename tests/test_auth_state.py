"""Детектор авторизации профиля: collect.detect_auth_state + QA-коды + status.json.

Мотивация: незалогиненный Ozon отдаёт максимум ~96-100 отзывов (WALL_LIMITED).
Без явного маркера в raw этот потолок неотличим от недобора/сбоя скролла.

Инвариант, который тут главный: новые коды — только INFO, вердикт остаётся OK.
Иначе уже валидные pid уехали бы в брак и демон запустил бы массовый rescan.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ozon_collector import collect as C
from scripts.qa_raw import qa_product

NOW = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()


class _Ctx:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self):
        return list(self._cookies)


class _Page:
    """Минимальный page: context.cookies() (Playwright) или get_cookies() (Selenium)."""

    def __init__(self, cookies=None, dom=None, selenium=False,
                 raise_eval=False, raise_cookies=False):
        self._cookies = cookies
        self._dom = dom
        self._selenium = selenium
        self._raise_eval = raise_eval
        self._raise_cookies = raise_cookies
        if not selenium:
            self.context = _Ctx(self._get_cookies())

    def _get_cookies(self):
        if self._raise_cookies:
            raise RuntimeError("no CDP access")
        return self._cookies or []

    def get_cookies(self):
        return self._get_cookies()

    def evaluate(self, js, *a, **kw):
        if self._raise_eval:
            raise RuntimeError("evaluate boom")
        return self._dom


SAT = [{"name": "_ozon_sat", "value": "opaque"}]
ANON = [{"name": "sessionId", "value": "x"}]
DOM_AUTH = {"hasLogin": False, "hasAccount": True}
DOM_ANON = {"hasLogin": True, "hasAccount": False}
DOM_BOTH = {"hasLogin": True, "hasAccount": True}


def test_auth_cookie_present_is_authoritative():
    st = C.detect_auth_state(_Page(cookies=SAT, dom=DOM_AUTH))
    assert st == {"logged_in": True, "source": "cookie", "cookie_present": True}


def test_auth_cookie_absent_means_anonymous_even_if_dom_says_account():
    # Кука авторитетнее DOM: разлогиненный профиль может держать «Мой Ozon»
    # в разметке, но _ozon_sat уже протух — потолок ~100 отзывов вернётся.
    st = C.detect_auth_state(_Page(cookies=ANON, dom=DOM_AUTH))
    assert st["logged_in"] is False
    assert st["source"] == "cookie"


def test_auth_dom_fallback_when_cookies_unavailable():
    st = C.detect_auth_state(_Page(dom=DOM_AUTH, raise_cookies=True,
                                   selenium=True))
    assert st["logged_in"] is True
    assert st["source"] == "dom"
    assert st["cookie_present"] is None


def test_auth_dom_fallback_detects_anonymous():
    st = C.detect_auth_state(_Page(dom=DOM_ANON, raise_cookies=True,
                                   selenium=True))
    assert st["logged_in"] is False
    assert st["source"] == "dom"


def test_auth_ambiguous_dom_keeps_unknown():
    st = C.detect_auth_state(_Page(dom=DOM_BOTH, raise_cookies=True,
                                   selenium=True))
    assert st["logged_in"] is None
    assert st["source"] == "ambiguous"


def test_auth_never_raises_and_reports_unknown():
    st = C.detect_auth_state(_Page(dom=None, raise_eval=True,
                                   raise_cookies=True, selenium=True))
    assert st == {"logged_in": None, "source": "unknown", "cookie_present": None}


def test_auth_uses_selenium_get_cookies():
    st = C.detect_auth_state(_Page(cookies=SAT, dom=DOM_AUTH, selenium=True))
    assert st["logged_in"] is True
    assert st["source"] == "cookie"


# ---- QA: только INFO, вердикт не меняется ------------------------------------

def _rev(i):
    return {"uuid": f"u{i}", "author": "Иван", "date": "19 августа 2026",
            "variant": "Спрей", "stars": 5, "text": f"Отличный спрей {i}",
            "helpful_yes": 0, "comments": []}


def _raw(vc, collected, auth):
    return {
        "pid": "42",
        "schema_version": "2.0",
        "collector_version": "1.16.0",
        "collected_at_utc": NOW,
        "product": {"title": "Спрей", "rating": 4.8, "rating_count": vc,
                    "price_text": "156 ₽", "customer_opinion": {}},
        "reviews": [_rev(i) for i in range(collected)],
        "reviews_collected": collected,
        "reviews_sorts": ["published_at_desc"],
        "reviews_variant_mode": {"used": True, "reason": "click_ok",
                                 "variant_count": vc, "total_count": vc,
                                 "pagination_type": "infinite_scroll"},
        "pages": [],
        "session": {"auth": auth},
    }


def _codes(rep):
    return {i["code"] for i in rep["issues"]}


def test_qa_wall_limited_anonymous_gets_info_not_browserkill():
    rep = qa_product(_raw(vc=300, collected=97,
                          auth={"logged_in": False, "source": "cookie",
                                "cookie_present": False}), "42")
    assert rep["collection_mode"] == "WALL_LIMITED"
    assert "UNAUTH_WALL" in _codes(rep)
    # Критично: потолок неавторизованной сессии — не брак, иначе демон
    # отправит валидные pid в rescan и потеряет уже собранное.
    assert rep["verdict"] == "OK"
    assert rep["metrics"]["auth_logged_in"] is False


def test_qa_wall_limited_auth_unknown_reported():
    rep = qa_product(_raw(vc=300, collected=90,
                          auth={"logged_in": None, "source": "unknown"}), "42")
    assert "AUTH_UNKNOWN" in _codes(rep)
    assert rep["verdict"] == "OK"
    assert rep["metrics"]["auth_source"] == "unknown"


def test_qa_full_mode_authed_raises_nothing():
    rep = qa_product(_raw(vc=20, collected=18,
                          auth={"logged_in": True, "source": "cookie"}), "42")
    assert "UNAUTH_WALL" not in _codes(rep)
    assert "AUTH_UNKNOWN" not in _codes(rep)
    assert rep["metrics"]["auth_logged_in"] is True


def test_qa_legacy_raw_without_session_is_backward_compatible():
    # Старые raw (до детектора) не имеют session → потолок остаётся
    # невыясненным, но помечается отдельно от «измерили и не определили».
    # Вердикт обязан остаться OK, иначе весь архив уехал бы в брак.
    raw = _raw(vc=300, collected=90, auth=None)
    raw.pop("session")
    rep = qa_product(raw, "42")
    assert "UNAUTH_WALL" not in _codes(rep)
    assert "AUTH_UNKNOWN" not in _codes(rep)
    assert "AUTH_NOT_MEASURED" in _codes(rep)
    assert rep["metrics"]["auth_logged_in"] is None
    assert rep["verdict"] == "OK"
