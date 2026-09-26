"""Быстрый выход при сетевом бане IP (critique #3).

Капча приходит со статусом 200 и снимается новой вкладкой/recycle. Жёсткий
бан (403/429/503) лестницей НЕ лечится — раньше воркер тратил на него
reload×3 + new_tab + recycle (~15-20 мин) и всё равно уходил в SESSION_BLOCKED.
Теперь HEAD из текущей вкладки отличает эти случаи до траты бюджета.

Уточнение 2026-09-26 (замер scripts/probe_head.py на живом ozon.ru): HEAD и GET
отвечают по-разному (HEAD → 405/403, GET → 200), то есть 403 на HEAD не доказывает
бан IP. Поэтому блок-статус подтверждается GET'ом — см. BLOCK_PROBE_JS.
"""

from ozon_collector import browser as B
from ozon_collector import collect as C
from ozon_collector import search as S
from ozon_collector.search import collect_search


class _Page:
    """Страница: evaluate отдаёт статусы HEAD и GET (или падает)."""

    def __init__(self, status=200, boom=False, get_status=None):
        self.status = status
        # GET по умолчанию повторяет HEAD — так ведёт себя обычный origin.
        # Для ложного бана передают get_status=200 при status=403.
        self.get_status = status if get_status is None else get_status
        self.boom = boom
        self.probes = 0

    def evaluate(self, js, *args, **kwargs):
        if self.boom:
            raise RuntimeError("cdp gone")
        if "method: 'HEAD'" in js:
            return {"head": self.status, "get": self.get_status}
        return ""

    def title(self):
        return "Ozon"


class _Sess:
    def __init__(self, links, status=200, boom=False, get_status=None):
        self._links = links
        self.cdp_url = "http://127.0.0.1:9222"
        self._page = _Page(status, boom, get_status)
        self.gotos = 0
        self.new_pages = 0
        self.scrolls = 0

    @property
    def page(self):
        return self._page

    @page.setter
    def page(self, value):
        self._page = value

    def collector_tabs(self):
        return 1

    def goto(self, url, wait_ms=None):
        self.gotos += 1

    def eval(self, js):
        return self._links

    def scroll(self, by=600):
        self.scrolls += 1

    def wait(self, ms=1000):
        pass

    def new_page(self, close_current=True):
        self.new_pages += 1


def _challenge(pid):
    return {
        "pid": pid, "ok": False,
        "error": "анти-бот (product_page): title:похоже, нет соединения",
        "raw": None, "markdown": "", "cache": "miss", "ocr": "off",
        "recyclable": True, "error_kind": "access_challenge",
        "error_probe_state": None, "error_signature": False,
    }


def _links(n=2):
    return [{"pid": str(i), "url": f"u{i}"} for i in range(1, n + 1)]


# ---- проба статуса ---------------------------------------------------------

def test_network_block_status_reads_head():
    assert S._network_block_status(_Sess([], status=403)) == 403
    assert S._ip_blocked(_Sess([], status=429)) == 429
    assert S._ip_blocked(_Sess([], status=503)) == 503


def test_captcha_status_200_is_not_ip_block():
    """Капча = 200: баном не считаем, лестница сохраняется."""
    assert S._ip_blocked(_Sess([], status=200)) is None


def test_network_block_status_unknown_on_error():
    """Любая ошибка пробы → None (прежнее поведение, без ложных стопов)."""
    assert S._network_block_status(_Sess([], boom=True)) is None


def test_network_block_status_none_without_page():
    class _NoPage:
        page = None
    assert S._network_block_status(_NoPage()) is None


# ---- ложный бан: HEAD блок, но GET жив (2026-09-26) ------------------------

def test_head_403_with_live_get_is_not_ip_block():
    """ГЛАВНЫЙ баг: 403 на HEAD при живом GET — не бан IP.

    Ozon отвечает на HEAD 405/403 (WAF на метод), а GET отдаёт 200. Старый код
    принимал 403 за бан и делал abort run, выбрасывая остаток без recycle.
    """
    assert S._ip_blocked(_Sess([], status=403, get_status=200)) is None
    assert S._network_block_status(_Sess([], status=403, get_status=200)) is None


def test_head_405_is_not_ip_block():
    """Реальный ответ ozon.ru на HEAD — 405, баном не считаем."""
    assert S._ip_blocked(_Sess([], status=405, get_status=200)) is None


def test_real_ip_block_confirmed_by_both_methods():
    """Настоящий бан: оба метода отвечают блок-статусом → fast-fail работает."""
    for st in S.IP_BLOCK_STATUSES:
        assert S._ip_blocked(_Sess([], status=st, get_status=st)) == st


def test_challenge_403_on_head_keeps_ladder(monkeypatch):
    """challenge + 403 на HEAD → лестница продолжается, abort run не делается."""
    sess = _Sess(_links(1), status=403, get_status=200)
    recycled = []
    monkeypatch.setattr(S, "run_gather",
                        lambda cfg, sess=None: _challenge(cfg["pid"]))
    monkeypatch.setattr(S, "run_canary", lambda s, url=None: "ok")
    monkeypatch.setattr(S, "_recycle_browser",
                        lambda s, m, lid: recycled.append(lid))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u",
                       limit=1, gather_cfg={})
    assert c["recovery"].get("ip_blocks") is None, "ложный засчитан как бан"
    assert sess.new_pages == 1, "лестница не пропущена"
    assert len(recycled) == 1
    # лестница исчерпана честно (не потому что ip_blocked), остаток aborted
    assert c["products"][0]["error_kind"] == "session_blocked"
    assert "ip blocked" not in c["products"][0]["error"]
    assert "403" not in c["products"][0]["error"]


# ---- лестница --------------------------------------------------------------

def test_ip_block_skips_ladder(monkeypatch):
    """403 на origin → сразу SESSION_BLOCKED: ни new_tab, ни recycle."""
    sess = _Sess(_links(2), status=403)
    recycled = []
    monkeypatch.setattr(S, "run_gather",
                        lambda cfg, sess=None: _challenge(cfg["pid"]))
    monkeypatch.setattr(S, "_recycle_browser",
                        lambda s, m, lid: recycled.append(lid))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=2, gather_cfg={})
    assert recycled == [], "recycle при бане IP не имеет смысла"
    assert sess.new_pages == 0, "new_tab при бане IP не имеет смысла"
    assert c["recovery"]["ip_blocks"] == 1
    assert c["recovery"]["session_blocked"] == 1
    assert c["products"][0]["error_kind"] == "ip_blocked"
    assert "http 403" in c["products"][0]["error"]
    # остаток aborted в Known-наборе демона, но с причиной в тексте
    assert c["products"][1]["error_kind"] == "session_blocked"
    assert "ip blocked" in c["products"][1]["error"]
    assert c["aborted"]["reason"] == "session_blocked"


def test_status_200_keeps_challenge_ladder(monkeypatch):
    """Капча (200) → лестница работает как раньше, ip_blocks не растёт."""
    sess = _Sess(_links(1), status=200)
    recycled = []
    monkeypatch.setattr(S, "run_gather",
                        lambda cfg, sess=None: _challenge(cfg["pid"]))
    monkeypatch.setattr(S, "run_canary", lambda s, url=None: "ok")
    monkeypatch.setattr(S, "_recycle_browser",
                        lambda s, m, lid: recycled.append(lid))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=1, gather_cfg={})
    assert c["recovery"].get("ip_blocks") is None
    assert sess.new_pages == 1, "капча лечится новой вкладкой"
    assert len(recycled) == 1, "лестница не пропущена"
    assert c["products"][0]["error_kind"] == "session_blocked"


def test_probe_error_keeps_challenge_ladder(monkeypatch):
    """Проба упала (нет evaluate) → лишних стопов нет, лестница как раньше."""
    sess = _Sess(_links(1), boom=True)
    monkeypatch.setattr(S, "run_gather",
                        lambda cfg, sess=None: _challenge(cfg["pid"]))
    monkeypatch.setattr(S, "run_canary", lambda s, url=None: "ok")
    monkeypatch.setattr(S, "_recycle_browser", lambda s, m, lid: None)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=1, gather_cfg={})
    assert c["recovery"].get("ip_blocks") is None
    assert sess.new_pages == 1


# ---- canary ----------------------------------------------------------------

def test_canary_reports_ip_blocked(monkeypatch):
    """Canary различает бан IP и капчу (для логов/cooldown демона)."""
    sess = _Sess([], status=429)

    def _boom(page, where):
        raise B.AccessChallengeError("title:похоже, нет соединения")

    monkeypatch.setattr(C, "check_challenge", _boom)
    assert S.run_canary(sess) == S.CANARY_IP_BLOCKED


def test_canary_reports_challenge_when_200(monkeypatch):
    sess = _Sess([], status=200)

    def _boom(page, where):
        raise B.AccessChallengeError("title:похоже, нет соединения")

    monkeypatch.setattr(C, "check_challenge", _boom)
    assert S.run_canary(sess) == S.CANARY_CHALLENGE
