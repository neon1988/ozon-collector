"""Canary-цель: сущность того же класса, а не всегда главная (ревизия 2.8).

Критика: canary всегда ходил на https://www.ozon.ru/ — а главная МЯГЧЕ
карточки по лимитам. CANARY_OK на главной не значит, что сессия видит
/product/, поэтому каноничная проверка могла «пропустить» упавшую сессию, и
бюджет вхолостую уходил в первый товар.
"""
import inspect
import logging
import os
import pathlib
from unittest.mock import patch

import pytest

from ozon_collector import parallel as P
from ozon_collector import search as S


class TestClassify:
    @pytest.mark.parametrize("url,kind", [
        ("https://www.ozon.ru/", "home"),
        ("https://www.ozon.ru", "home"),
        ("", "home"),
        ("https://www.ozon.ru/product/konnect-15-123456789/", "product"),
        ("https://www.ozon.ru/product/123456789/", "product"),
        ("https://www.ozon.ru/category/15821-telefony-15502/",
         "category"),
        ("https://www.ozon.ru/search/?text=iphone&from_global=true",
         "search"),
        ("https://www.ozon.ru/brand/apple-15721/", "custom"),
    ])
    def test_kinds(self, url, kind):
        assert S.classify_canary_url(url) == kind


class TestResolve:
    def test_preferred_wins(self):
        u, k = S.resolve_canary_url("https://www.ozon.ru/product/a-111111/")
        assert k == "product" and u.endswith("111111/")

    def test_env_used_when_no_preferred(self):
        with patch.dict(os.environ, {"OZON_CANARY_URL":
                                     "https://www.ozon.ru/category/15821/"}):
            u, k = S.resolve_canary_url(None)
        assert k == "category"

    def test_home_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            assert S.resolve_canary_url(None) == (S.CANARY_URL_HOME, "home")

    def test_non_http_skipped(self):
        """rescan демона передаёт url='список из N артикулов' — не цель."""
        with patch.dict(os.environ, {}, clear=True):
            u, k = S.resolve_canary_url("список из 5 артикулов")
        assert (u, k) == (S.CANARY_URL_HOME, "home")

    def test_env_beats_home_but_loses_to_preferred(self):
        with patch.dict(os.environ, {"OZON_CANARY_URL": "https://www.ozon.ru/category/9/"}):
            u, k = S.resolve_canary_url("https://www.ozon.ru/product/x-222222/")
        assert k == "product"

    def test_env_beats_campaign(self):
        """Ключевой инвариант: env — флаг оператора, campaign его НЕ перекрывает.

        Регресс: campaign-URL передавался в preferred, и OZON_CANARY_URL
        молча игнорировался — «поставь лёгкую цель» не работало.
        """
        env_url = "https://www.ozon.ru/category/15821/"
        with patch.dict(os.environ, {"OZON_CANARY_URL": env_url}):
            u, k = S.resolve_canary_url(None, campaign="https://www.ozon.ru/product/z-999999/")
        assert (u, k) == (env_url, "category")

    def test_campaign_used_when_no_preferred_no_env(self):
        campaign = "https://www.ozon.ru/product/z-999999/"
        with patch.dict(os.environ, {}, clear=True):
            u, k = S.resolve_canary_url(None, campaign=campaign)
        assert (u, k) == (campaign, "product")

    def test_preferred_beats_env_and_campaign(self):
        with patch.dict(os.environ, {"OZON_CANARY_URL": "https://www.ozon.ru/category/9/"}):
            u, k = S.resolve_canary_url("https://www.ozon.ru/product/a-111111/",
                                        campaign="https://www.ozon.ru/category/15821/")
        assert k == "product"

    def test_campaign_non_http_falls_to_home(self):
        with patch.dict(os.environ, {}, clear=True):
            u, k = S.resolve_canary_url(None, campaign="список из 5 артикулов")
        assert (u, k) == (S.CANARY_URL_HOME, "home")


class _FakeSess:
    def __init__(self, raise_=None):
        self.visited = []
        self.raise_ = raise_

    def goto(self, url, wait_ms=0):
        self.visited.append(url)
        if self.raise_:
            raise self.raise_

    page = None


def test_run_canary_hits_resolved_url():
    sess = _FakeSess()
    assert S.run_canary(sess, "https://www.ozon.ru/product/p-111111/") == "ok"
    assert sess.visited == ["https://www.ozon.ru/product/p-111111/"]


def test_run_canary_challenge_on_product():
    from ozon_collector import browser as B
    sess = _FakeSess(raise_=B.AccessChallengeError("challenge"))
    with patch.object(S, "_ip_blocked", lambda s: None):
        assert S.run_canary(sess, "https://www.ozon.ru/product/p-1/") \
            == S.CANARY_CHALLENGE


def test_run_canary_ip_blocked():
    from ozon_collector import browser as B
    sess = _FakeSess(raise_=B.AccessChallengeError("blocked"))
    with patch.object(S, "_ip_blocked", lambda s: 403):
        assert S.run_canary(sess, "https://www.ozon.ru/category/1/") \
            == S.CANARY_IP_BLOCKED


class TestWorkerTaskCanary:
    def test_task_carries_canary_url(self):
        task = P._worker_task([], {"scroll_jitter_seed": 5}, 2, None,
                             "log.log", canary_url="https://ozon.ru/product/x-1/")
        assert task["canary_url"] == "https://ozon.ru/product/x-1/"
        assert task["gather_cfg"] == {"scroll_jitter_seed": 5}

    def test_task_defaults_none(self):
        assert P._worker_task([], {}, 2, None, None)["canary_url"] is None

    def test_respawn_forwards_canary(self):
        """Замена упавшего воркера не должна терять canary-цель.

        Раньше task собирался в двух местах; если в _respawn забыть поле,
        замена молча проверяла главную вместо сущности кампании.
        """
        seen = {}

        def _fake_spawn(spec, task, on_event=None):
            seen.update(task)
            return P.WorkerContext(spec=spec, proc=None, conn=None)

        wctx = P.WorkerContext(spec={"worker_id": 1}, proc=None, conn=None)
        wctx.shard_indexes = []
        wctx.restarts = 0
        with patch.object(P, "_cleanup_worker", lambda w: None), \
             patch.object(P, "_wait_port_free", lambda s, timeout_s=8.0: None), \
             patch.object(P, "_start_stagger_s", lambda n: 0.0), \
             patch.object(P, "_spawn_ctx", _fake_spawn):
            P._respawn(wctx, [], {}, {}, 2, None, None, None,
                       dynamic=True, canary_url="https://ozon.ru/product/y-2/")
        assert seen["canary_url"] == "https://ozon.ru/product/y-2/"


class TestCampaignFallbackPriority:
    """Интеграция: реальные call sites, не только сам resolver.

    Резолвер покрыт отдельно; здесь важно, что оркестраторы передают campaign
    ОТДЕЛЬНЫМ аргументом. Иначе OZON_CANARY_URL снова перекрывается URL выдачи
    (регресс, найденный при сверке кода с docs 2.8).
    """

    def test_collect_search_env_wins_over_campaign_url(self):
        env_url = "https://www.ozon.ru/category/15821/"
        seen = {}

        def _fake_shard(sess, manager, items, gather_cfg, max_recycles,
                        recovery, **kw):
            seen["recovery"] = recovery
            return []

        with patch.dict(os.environ, {"OZON_CANARY_URL": env_url}):
            with patch.object(S, "_collect_shard", _fake_shard), \
                 patch.object(S, "_warmup_to_category", lambda *a, **kw: None), \
                 patch.object(S, "_collect_links_with_recovery",
                              lambda *a, **kw: []):
                S.collect_search(
                    sess=None, url="https://www.ozon.ru/search/?text=iphone",
                    limit=1, gather_cfg={})
        assert seen["recovery"]["canary_url"] == env_url

    def test_collect_search_campaign_when_no_env(self):
        campaign = "https://www.ozon.ru/search/?text=iphone"
        seen = {}

        def _fake_shard(sess, manager, items, gather_cfg, max_recycles,
                        recovery, **kw):
            seen["recovery"] = recovery
            return []

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(S, "_collect_shard", _fake_shard), \
                 patch.object(S, "_warmup_to_category", lambda *a, **kw: None), \
                 patch.object(S, "_collect_links_with_recovery",
                              lambda *a, **kw: []):
                S.collect_search(
                    sess=None, url=campaign, limit=1, gather_cfg={})
        assert seen["recovery"]["canary_url"] == campaign

    @pytest.mark.parametrize("module", ["search.py", "parallel.py"])
    def test_callsites_pass_campaign_separately(self, module):
        """Сторож регрессии формы вызова: campaign=url, не `canary_url or url`.

        поведенческий покрыт выше для collect_search; здесь ловится сама форма
        аргумента в обоих оркестраторах (дешёвый guard на plumbing).
        """
        root = pathlib.Path(inspect.getfile(S)).parent
        src = (root / module).read_text(encoding="utf-8")
        assert "resolve_canary_url(canary_url or url)" not in src
        assert "resolve_canary_url(canary_url, campaign=url)" in src \
            or "S.resolve_canary_url(canary_url, campaign=url)" in src


class TestKpiCanaryAndPerWorker:
    def _payload(self, challenges, canary_fails=0, ip_blocks=0):
        return {"recovery_metrics": {
            "access_stats": {"challenges": challenges},
            "canary_fails": canary_fails, "ip_blocks": ip_blocks,
            "recycles": 0, "new_tab_attempts": 0}}

    def test_kpi_sums_canary_fails_and_ip_blocks(self):
        kpi = P._kpi_summary([self._payload(1, 2, 1), self._payload(3)],
                             attempted=10)
        assert kpi["canary_fails"] == 2
        assert kpi["ip_blocks"] == 1
        assert kpi["total_challenges"] == 4
        assert kpi["challenge_rate_per_100"] == 40.0

    def test_worker_summary_reports_challenges(self):
        wctx = P.WorkerContext(spec={"worker_id": 2, "port": 9223,
                                     "profile_token": "t"}, proc=None,
                               conn=None)
        s = P._worker_summary(wctx, {
            "processed_indexes": [1, 2, 3, 4], "failed_indexes": [5],
            "recovery_metrics": {"access_stats": {"challenges": 1}}})
        assert s["challenges"] == 1
        assert s["challenge_rate_per_100"] == 20.0

    def test_worker_summary_handles_empty_worker(self):
        wctx = P.WorkerContext(spec={"worker_id": 1, "port": 9222,
                                     "profile_token": "t"}, proc=None,
                               conn=None)
        s = P._worker_summary(wctx, {})
        assert s["challenges"] == 0
        assert s["challenge_rate_per_100"] is None
