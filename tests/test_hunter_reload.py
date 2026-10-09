"""Регрессионные тесты на обработку заглушек после решения пазла.

Сценарий (образец fab_cp_, html-скелет Antibot Captcha): после НЕУДАЧНОГО
решения Ozon рероллит капчу, и 1-2s страница висит «скелетом» (img#image и
img#puzzle с src="", has=false). Если reload'ить мгновенно — убьём живую
капчу; поэтому _reload_on_fab сначала ждёт ~2s, потом проверяет, что пазл
реально отрисован (looks_like_puzzle), и только если его нет — reload.
"""

import importlib.util
import io
import os
import time


def _load_hunter():
    """Загрузить tools/puzzle_hunter.py модулем (импортируется без side-эффектов)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "tools", "puzzle_hunter.py")
    spec = importlib.util.spec_from_file_location("puzzle_hunter", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestHunterPathsInProject:
    """Провокатор пишет сессию и журналы В ПРОЕКТ, а не в %LOCALAPPDATA%/%TEMP%.

    Было: профиль %LOCALAPPDATA%\\opencode\\ozon-profile-hunter (после переноса
    профилей в проект 07.10 не существует → hunter молча создавал новый
    холодный профиль = заново 18+ и холодная сессия) и attempts-журнал в
    системном temp. Оба пути обязаны быть внутри <проект>/.tmp.
    """

    def test_hunter_profile_inside_project_tmp(self):
        mod = _load_hunter()
        prof = mod.HUNTER_PROFILE.replace("/", os.sep).lower()
        assert ".tmp" in prof, f"профиль вне .tmp: {mod.HUNTER_PROFILE}"
        assert "localappdata" not in prof

    def test_attempts_dir_inside_project_tmp(self):
        mod = _load_hunter()
        d = mod._tmp_dir("puzzle_attempts").replace("/", os.sep).lower()
        want = os.path.join(root_of_project(), ".tmp").replace("/", os.sep).lower()
        assert d.startswith(want), f"{d} не внутри {want}"

    def test_profile_follows_ozon_profile_base(self, monkeypatch):
        mod = _load_hunter()
        base = os.path.join(root_of_project(), ".tmp", "profiles")
        monkeypatch.setenv("OZON_PROFILE_BASE", base)
        # константа вычислена при импорте — проверяем сам resolver
        got = os.path.join(mod.PR.profile_base_dir(), "ozon-profile-hunter")
        assert got.replace("/", os.sep).lower().startswith(base.replace("/", os.sep).lower())


def root_of_project() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeFactsPage:
    """Страница, отдающая СКРИПТ фактов DOM (как _STATUS_JS). Сценарий:
    каждый вызов evaluate → следующий факт; после конца скрипта — последний."""

    def __init__(self, script=None):
        self._script = list(script or [])
        self._i = 0
        self.reloads = 0

    def evaluate(self, _js):
        idx = min(self._i, len(self._script) - 1) if self._script else 0
        self._i += 1
        return dict(self._script[idx]) if self._script else {}

    def reload(self, *a, **k):
        self.reloads += 1


def _facts(**over):
    base = {"href": "https://www.ozon.ru/product/123456/",
            "ready": "complete", "title": "", "has": False,
            "shell": False, "loader": False, "sig": None, "product": False,
            "_reason": "loading_default"}
    base.update(over)
    return base


def _stub_sig():
    # сигнатура антibot-скелета (fab_cp_) из html-образца
    return "dom:Сопоставьте пазл, двигая ползунок (fab_cp_20260921090956)"


def test_failed_solve_shell_reloads_after_delay(monkeypatch):
    """Скелет держится >2s после провала: ждём delay, потом reload (до attempts).
    Реальные факты DOM читаются на КАЖДОЙ стадии и пишутся в журнал."""
    H = _load_hunter()
    # сценарий: after_solve=скелет, after_delay=скелет, после reload#1/#2 скелет
    page = _FakeFactsPage(script=[
        _facts(shell=True, sig=_stub_sig(), _reason="shell_stub"),
        _facts(shell=True, sig=_stub_sig(), _reason="shell_stub"),
        _facts(shell=True, sig=_stub_sig(), _reason="shell_stub"),
        _facts(shell=True, sig=_stub_sig(), _reason="shell_stub"),
    ])
    journal = io.StringIO()
    H._JOURNAL = journal
    sleeps = []
    monkeypatch.setattr(H.time, "sleep", lambda s: sleeps.append(s))

    assert H._reload_on_fab(page, pid="pid1", tab_idx=2,
                            delay_s=2.0, attempts=2,
                            verdict="failed") is True
    assert page.reloads == 2
    # пауза «пару секунд»: одна до проверки состояния + одна после каждого reload
    assert sleeps == [2.0, 2.0, 2.0]

    text = journal.getvalue()
    # что реально на странице — пишется всегда
    assert "POST_SOLVE_CHECK pid=pid1 tab=2 stage=after_solve verdict=failed" in text
    assert "POST_SOLVE_CHECK pid=pid1 tab=2 stage=after_delay" in text
    assert "POST_SOLVE_CHECK pid=pid1 tab=2 stage=after_reload_2" in text
    # заглушка поймана и снята reload'ом, каждое действие в журнале
    assert "STUB type=captcha_shell pid=pid1 tab=2" in text
    assert _stub_sig() in text
    assert "PAGE_RELOAD type=captcha_shell pid=pid1 tab=2 attempt=1" in text
    assert "PAGE_RELOAD type=captcha_shell pid=pid1 tab=2 attempt=2" in text
    assert "STUB_STILL_PRESENT pid=pid1 tab=2 after=exhausted" in text


def test_new_puzzle_after_failed_solve_no_reload(monkeypatch):
    """Пазл НЕ решился, но новый уже отрисовался (reroll): это не заглушка,
    reload НЕ делаем — убить свежий пазл нельзя."""
    H = _load_hunter()
    # after_solve: нет пазла (старый ушёл); after_delay: новый пазл отрисован
    page = _FakeFactsPage(script=[
        _facts(shell=True, sig=_stub_sig(), _reason="shell_stub"),
        _facts(has=True, sig="title:Сопоставьте пазл", _reason="puzzle_rendered"),
    ])
    journal = io.StringIO()
    H._JOURNAL = journal
    sleeps = []
    monkeypatch.setattr(H.time, "sleep", lambda s: sleeps.append(s))

    assert H._reload_on_fab(page, pid="pid1", tab_idx=0,
                            delay_s=2.0, verdict="failed") is False
    assert page.reloads == 0
    assert sleeps == [2.0]  # delay на реролл был, reload — нет
    text = journal.getvalue()
    assert "NEW_PUZZLE pid=pid1 tab=0 stage=reroll_after_delay" in text
    # заглушка была зафиксирована В МОМЕНТ появления (after_solve)
    assert "STUB type=captcha_shell pid=pid1 tab=0" in text
    assert "phase=after_solve" in text
    assert "PAGE_RELOAD" not in text  # реролл — reload не делаем


def test_stub_logged_immediately_when_check_fails(monkeypatch):
    """Заглушка появилась (loader=True в after_solve), но after_delay упал
    (context destroyed / страница уехала): STUB всё равно в журнале — не теряем."""
    H = _load_hunter()

    class _DieAfterFirst:
        """after_solve отдаёт факты, все последующие evaluate падают."""

        def __init__(self, facts):
            self._facts = facts
            self._i = 0
            self.reloads = 0

        def evaluate(self, _js):
            self._i += 1
            if self._i == 1:
                return dict(self._facts)
            raise RuntimeError("Execution context was destroyed")

        def reload(self, *a, **k):
            self.reloads += 1

    page = _DieAfterFirst(_facts(loader=True, _reason="loader_stub"))
    journal = io.StringIO()
    H._JOURNAL = journal
    monkeypatch.setattr(H.time, "sleep", lambda s: None)

    assert H._reload_on_fab(page, pid="p9", tab_idx=4,
                            delay_s=2.0, verdict="solved") is True
    text = journal.getvalue()
    assert "STUB type=loader_challenge pid=p9 tab=4" in text
    assert "phase=after_solve" in text
    assert "POST_SOLVE_CHECK pid=p9 tab=4 stage=after_solve verdict=solved" in text
    assert "POLL_ERROR pid=p9 tab=4" in text


def test_reroll_right_after_solve_no_wait(monkeypatch):
    """Новый пазл виден СРАЗУ после провала — даже delay на реролл не нужен."""
    H = _load_hunter()
    page = _FakeFactsPage(script=[
        _facts(has=True, sig="title:Сопоставьте пазл", _reason="puzzle_rendered"),
    ])
    journal = io.StringIO()
    H._JOURNAL = journal
    sleeps = []
    monkeypatch.setattr(H.time, "sleep", lambda s: sleeps.append(s))

    assert H._reload_on_fab(page, pid="pid1", tab_idx=0,
                            delay_s=2.0, verdict="failed") is False
    assert page.reloads == 0
    assert not sleeps  # факты видны сразу — никаких пауз
    assert "NEW_PUZZLE pid=pid1 tab=0 stage=reroll_after_solve" in \
        journal.getvalue()


def test_no_stub_normal_page_returns_false(monkeypatch):
    """Ни пазла, ни заглушки, ни маркера (товар/странное состояние):
    ничего не трогаем, reload не делаем, спим только на проверку."""
    H = _load_hunter()
    page = _FakeFactsPage(script=[
        _facts(product=True, _reason="product_visible"),
        _facts(product=True, _reason="product_visible"),
    ])
    journal = io.StringIO()
    H._JOURNAL = journal
    sleeps = []
    monkeypatch.setattr(H.time, "sleep", lambda s: sleeps.append(s))

    assert H._reload_on_fab(page, pid="pid1", tab_idx=0,
                            delay_s=2.0, verdict="solved") is False
    assert page.reloads == 0
    assert sleeps == [2.0]
    text = journal.getvalue()
    assert "stage=unknown_state" in text
    assert "STUB " not in text


def test_reload_loop_stops_when_challenge_disappears(monkeypatch):
    """После reload'а челлендж ушёл: stub_resolved challenge_gone, цикл вон."""
    H = _load_hunter()
    page = _FakeFactsPage(script=[
        _facts(shell=True, sig=_stub_sig(), _reason="shell_stub"),
        _facts(shell=True, sig=_stub_sig(), _reason="shell_stub"),
        _facts(product=True, _reason="product_visible"),  # после reload#1
    ])
    journal = io.StringIO()
    H._JOURNAL = journal
    monkeypatch.setattr(H.time, "sleep", lambda s: None)

    assert H._reload_on_fab(page, pid="pid1", tab_idx=0,
                            delay_s=2.0, attempts=5,
                            verdict="failed") is True
    assert page.reloads == 1  # второй reload не нужен — челлендж ушёл
    text = journal.getvalue()
    assert "STUB_RESOLVED pid=pid1 tab=0 attempt=1 after=challenge_gone" in text


def _status(info):
    base = {"href": "https://www.ozon.ru/product/123456/",
            "ready": "complete", "title": "", "has": False,
            "shell": False, "loader": False, "sig": None, "product": False}
    base.update(info)
    return base


class _FakeStatusPage:
    def __init__(self, info):
        self._info = info

    def evaluate(self, _js):
        return dict(self._info)


def test_nav_normal_when_product_visible_no_full_load(monkeypatch):
    """Товар виден (product=True) даже при readyState=loading —
    normal сразу, догрузку скриптов не ждём."""
    H = _load_hunter()
    page = _FakeStatusPage(_status({"ready": "loading", "product": True}))
    assert H._check_page_status(page, "123456", time.monotonic())[0] == "normal"


def test_nav_waits_when_product_widget_hidden(monkeypatch):
    """Товара ещё нет (product=False) и readyState=loading — НЕ normal, ждём."""
    H = _load_hunter()
    page = _FakeStatusPage(_status({"ready": "loading", "product": False}))
    assert H._check_page_status(page, "123456", time.monotonic())[0] == "loading"


def test_nav_fallback_ready_complete(monkeypatch):
    """Без виджета товара прежний fallback жив: readyState=complete + dwell."""
    H = _load_hunter()
    page = _FakeStatusPage(_status({"ready": "complete", "product": False}))
    assert H._check_page_status(
        page, "123456", time.monotonic() - 1.0)[0] == "normal"


def test_nav_normal_when_slug_url_product_visible(monkeypatch):
    """Слаг-URL /product/<slug>-123456/?sh=…: товар виден (product=True) при
    readyState=loading → normal сразу (раньше терялся строгим поиском
    '/product/123456')."""
    H = _load_hunter()
    info = _status({"href": "https://www.ozon.ru/product/topper-nikol-123456/?sh=x",
                    "ready": "loading", "product": True})
    page = _FakeStatusPage(info)
    st, sig, _ = H._check_page_status(page, "123456", time.monotonic())
    assert st == "normal"


def test_nav_search_redirect_detected(monkeypatch):
    """Ozon свёл товар на /search/?product_id=… — ждём status search_redirect
    (слот сбрасывается мгновенно, не долбя до tab_timeout)."""
    H = _load_hunter()
    info = _status({"href": "https://www.ozon.ru/search/?product_id=123456",
                    "ready": "complete", "product": False})
    page = _FakeStatusPage(info)
    st, sig, _ = H._check_page_status(page, "123456", time.monotonic())
    assert st == "search_redirect"


def test_nav_challenge_beats_product_marker():
    """Приоритет: реальный пазл (has) важнее маркера товара — не съедаем пазл."""
    H = _load_hunter()
    page = _FakeStatusPage(_status({
        "ready": "loading", "product": True, "has": True,
        "sig": "title:Сопоставьте пазл"}))
    assert H._check_page_status(page, "123456", time.monotonic())[0] == "puzzle"


class _Slot:
    def __init__(self, index=0, pid="p1", url="https://www.ozon.ru/product/p1/"):
        self.index = index
        self.pid = pid
        self.url = url
        self.nav_time = time.monotonic()
        self.stub_count = 0
        self.status_changes = 0
        self.last_status = None
        self.last_sig = None
        self.facts_fp = None
        self.last_log_t = 0.0


def test_log_page_poll_writes_reason_and_heartbeat(monkeypatch):
    """poll-журнал: смена статуса логируется с причиной; повторный waiting
    без 1s — тихо, потом heartbeat."""
    H = _load_hunter()
    journal = io.StringIO()
    H._JOURNAL = journal
    slot = _Slot()
    info = _status({"ready": "loading", "product": True})

    H._log_page_poll(slot, "loading", None,
                     dict(info, _reason="product_visible"))
    text = journal.getvalue()
    assert "POLL pid=p1" in text
    assert "st=loading" in text
    assert "product_visible" in text  # причина классификации в журнале

    # тот же waiting статус подряд — тихо (heartbeat ещё не наступил)
    H._log_page_poll(slot, "loading", None, info)
    H._log_page_poll(slot, "loading", None, info)
    # смена статуса — снова пишем
    H._log_page_poll(slot, "puzzle", "title:Сопоставьте пазл", info)
    text2 = journal.getvalue()
    assert text2.count("POLL ") == 2
    assert "st=puzzle" in text2


def test_check_status_poll_error_emits_event(monkeypatch):
    """Сбой evaluate логируется как poll_error и не роняет слот (loading)."""
    H = _load_hunter()
    journal = io.StringIO()
    H._JOURNAL = journal

    class _ErrPage:
        def evaluate(self, _js):
            raise RuntimeError("cdp gone")

    st, sig, info = H._check_page_status(_ErrPage(), "123456",
                                         time.monotonic(), tab=3)
    assert st == "loading"
    assert sig is None
    text = journal.getvalue()
    assert "POLL_ERROR pid=123456 tab=3" in text


def test_solve_puzzle_logs_wrong_solve_reroll(monkeypatch):
    """Солвер сообщает reroll (пазл решён неправильно, Ozon перезалил новый) —
    хантер пишет NEW_PUZZLE stage=wrong_solve в журнал (0 таких за прогон!)."""
    H = _load_hunter()
    journal = io.StringIO()
    H._JOURNAL = journal

    def _solve(page, timeout_s=0.0, on_event=None):
        if on_event is not None:
            on_event({"kind": "reroll", "ctx": "piece_drop"})
        return False

    monkeypatch.setattr(H.P, "reset", lambda: None)
    monkeypatch.setattr(H.P, "solve_puzzle", _solve)
    monkeypatch.setattr(H, "_reload_on_fab", lambda *a, **k: False)

    stats = {"puzzles": 0, "solved": 0, "failed": 0}
    H._solve_puzzle(object(), "pid99", 0, delay=0.1, stats=stats,
                    puzzles_seen=[], timeout_s=1.0, started=0.0,
                    product_wait_s=0.0)
    text = journal.getvalue()
    assert "SOLVE_START pid=pid99 tab=0" in text
    assert ("NEW_PUZZLE pid=pid99 tab=0 stage=wrong_solve "
            "verdict=reroll" in text)
    assert stats["puzzles"] == 1 and stats["failed"] == 1