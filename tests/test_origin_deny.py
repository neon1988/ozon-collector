"""Отказ origin (403/429/503) — это отказ СЕССИИ, а не бан адреса.

История файла: до 2026-10-02 он назывался test_ip_fastfail и закреплял
fast-fail «сетевой бан IP → abort run без лестницы». Живой замер опроверг
этот диагноз (лог демона prezervativy, 2026-10-02):
    16:03:11  тот же адрес → NORMAL_DONE (discovered 336, valid 10)
    16:13-17:24  21 отказ 403 подряд у всех 8 воркеров сразу
Пользователь в это время спокойно открывал Ozon в другом браузере и в
инкогнито. Бан адреса так не выглядит — значит отказ сессионный, и смена
сессии (new_tab/recycle) его снимает. Поэтому отказ origin больше НЕ
пропускает лестницу восстановления, а товары не теряются молча.

Ранее подтверждённый факт сохранён: HEAD и GET отвечают по-разному
(HEAD → 405/403, GET → 200), поэтому статус отказа подтверждается GET'ом —
см. BLOCK_PROBE_JS.
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
    assert S._origin_denied(_Sess([], status=429)) == 429
    assert S._origin_denied(_Sess([], status=503)) == 503


def test_captcha_status_200_is_not_origin_deny():
    """Капча = 200: отказом origin не считаем, лестница сохраняется."""
    assert S._origin_denied(_Sess([], status=200)) is None


def test_network_block_status_unknown_on_error():
    """Любая ошибка пробы → None (прежнее поведение, без ложных стопов)."""
    assert S._network_block_status(_Sess([], boom=True)) is None


def test_network_block_status_none_without_page():
    class _NoPage:
        page = None
    assert S._network_block_status(_NoPage()) is None


# ---- ложный бан: HEAD блок, но GET жив (2026-09-26) ------------------------

def test_head_403_with_live_get_is_not_origin_deny():
    """403 на HEAD при живом GET — не отказ origin.

    Ozon отвечает на HEAD 405/403 (WAF на метод), а GET отдаёт 200.
    """
    assert S._origin_denied(_Sess([], status=403, get_status=200)) is None
    assert S._network_block_status(_Sess([], status=403, get_status=200)) is None


def test_head_405_is_not_origin_deny():
    """Реальный ответ ozon.ru на HEAD — 405, отказом не считаем."""
    assert S._origin_denied(_Sess([], status=405, get_status=200)) is None


def test_origin_deny_confirmed_by_both_methods():
    """Отказ подтверждён обоими методами → фиксируется как факт."""
    for st in S.ORIGIN_DENY_STATUSES:
        assert S._origin_denied(_Sess([], status=st, get_status=st)) == st


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
    assert c["recovery"].get("origin_denied") is None, "ложный отказ засчитан"
    assert sess.new_pages == 1, "лестница не пропущена"
    assert len(recycled) == 1
    # лестница исчерпана честно (не потому что отказ origin), остаток aborted
    assert c["products"][0]["error_kind"] == "session_blocked"
    assert "origin" not in c["products"][0]["error"]


# ---- лестница --------------------------------------------------------------

def test_origin_deny_runs_full_ladder(monkeypatch):
    """ГЛАВНЫЙ баг 2026-10-02: 403 → отказ СЕССИИ, лестница обязана отработать.

    Прежде код делал fast-fail «сетевой бан IP → abort run»: ни new_tab, ни
    recycle, а весь остаток очереди aborted. Отказ при этом сессионный —
    смена сессии его снимает, поэтому товары терялись на ровном месте.
    """
    sess = _Sess(_links(2), status=403)
    recycled = []
    monkeypatch.setattr(S, "run_gather",
                        lambda cfg, sess=None: _challenge(cfg["pid"]))
    monkeypatch.setattr(S, "run_canary", lambda s, url=None: "ok")
    monkeypatch.setattr(S, "_recycle_browser",
                        lambda s, m, lid: recycled.append(lid))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u",
                       limit=2, gather_cfg={})
    # отказ зафиксирован как факт: проба честно срабатывает на каждом шаге
    # лестницы (товар → new_tab → recycle), а не один раз на прогон
    assert c["recovery"]["origin_denied"] == 3
    # но лестница отработала полностью
    assert sess.new_pages == 1, "new_tab пропущен при отказе origin"
    assert len(recycled) == 1, "recycle пропущен при отказе origin"
    # ложного abort run на первом товаре нет: session_blocked один, от лестницы
    assert c["products"][0]["error_kind"] == "session_blocked"
    # остаток aborted в known-наборе демона, причина — сессия, а не «IP»
    assert c["products"][1]["error_kind"] == "session_blocked"
    assert "ip" not in c["products"][1]["error"].lower()
    assert c["aborted"]["reason"] == "session_blocked"


def test_origin_deny_never_names_ip_ban(monkeypatch):
    """Ни один текст при отказе origin не называет бан адреса.

    Пользователь повторял это наблюдение много раз, и лог должен больше не
    утверждать то, что неверно: адрес обслуживается другими сессиями.
    """
    sess = _Sess(_links(2), status=403)
    monkeypatch.setattr(S, "run_gather",
                        lambda cfg, sess=None: _challenge(cfg["pid"]))
    monkeypatch.setattr(S, "run_canary", lambda s, url=None: "ok")
    monkeypatch.setattr(S, "_recycle_browser", lambda s, m, lid: None)
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u",
                       limit=2, gather_cfg={})
    blob = " ".join([str(c.get("aborted") or "")] +
                    [str(p.get("error") or "") for p in c["products"]])
    assert "ip" not in blob.lower(), blob
    assert "бан" not in blob.lower(), blob


def test_status_200_keeps_challenge_ladder(monkeypatch):
    """Капча (200) → лестница работает как раньше, origin_denied не растёт."""
    sess = _Sess(_links(1), status=200)
    recycled = []
    monkeypatch.setattr(S, "run_gather",
                        lambda cfg, sess=None: _challenge(cfg["pid"]))
    monkeypatch.setattr(S, "run_canary", lambda s, url=None: "ok")
    monkeypatch.setattr(S, "_recycle_browser",
                        lambda s, m, lid: recycled.append(lid))
    c = collect_search(sess, "https://www.ozon.ru/search/?text=u", limit=1, gather_cfg={})
    assert c["recovery"].get("origin_denied") is None
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
    assert c["recovery"].get("origin_denied") is None
    assert sess.new_pages == 1


# ---- canary ----------------------------------------------------------------

def test_canary_reports_origin_denied(monkeypatch):
    """Canary различает отказ origin и капчу (для логов/cooldown демона)."""
    sess = _Sess([], status=429)

    def _boom(page, where):
        raise B.AccessChallengeError("title:похоже, нет соединения")

    monkeypatch.setattr(C, "check_challenge", _boom)
    assert S.run_canary(sess) == S.CANARY_ORIGIN_DENY


def test_canary_reports_challenge_when_200(monkeypatch):
    sess = _Sess([], status=200)

    def _boom(page, where):
        raise B.AccessChallengeError("title:похоже, нет соединения")

    monkeypatch.setattr(C, "check_challenge", _boom)
    assert S.run_canary(sess) == S.CANARY_CHALLENGE
