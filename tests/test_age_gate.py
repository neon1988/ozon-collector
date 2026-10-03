# -*- coding: utf-8 -*-
"""Тесты прохождения возрастного гейта 18+ (_pass_age_gate).

Возрастной гейт Ozon («Подтвердите возраст») — модалка поверх карточки.
Обычный Playwright .click() падает, т.к. оверлей модалки
(<div class="a75_4_1-b3"> в vue-portal-target) перехватывает pointer events.
Рабочий путь — trusted JS-клик el.click() через element_handle().evaluate.
"""
import pytest

from ozon_collector import collect as C


class _Handle:
    """element_handle: JS-клик записывается, Playwright-аналогов нет."""

    def __init__(self, calls, on_click=None):
        self._calls = calls
        self._on_click = on_click

    def evaluate(self, expr, *a, **k):
        s = str(expr)
        if "getBoundingClientRect" in s:
            # чтение геометрии для реального клика мышью — это НЕ клик
            return {"x": 100.0, "y": 200.0, "w": 160.0, "h": 44.0}
        self._calls["js_click"] = self._calls.get("js_click", 0) + 1
        if self._on_click:
            self._on_click()
        return None


class _Button:
    """locator кнопки «Подтвердить»: enabled, отдаёт JS-хэндл."""

    def __init__(self, calls, on_click=None):
        self._calls = calls
        self._on_click = on_click

    def wait_for(self, state="visible", timeout=5000):
        return None

    def is_enabled(self):
        return True

    def element_handle(self):
        return _Handle(self._calls, self._on_click)

    def click(self, *a, **k):
        self._calls["pw_click"] = self._calls.get("pw_click", 0) + 1
        if self._on_click:
            self._on_click()


class _Input:
    def click(self, *a, **k):
        return None

    def press(self, *a, **k):
        return None

    def press_sequentially(self, text, delay=0):
        return None

    def type(self, text, delay=0):
        return None


class _First:
    def __init__(self, calls):
        self._calls = calls

    def click(self, *a, **k):
        return None

    def press_sequentially(self, text, delay=0):
        return None

    def count(self):
        return 1


class _Locator:
    def __init__(self, calls):
        self._calls = calls
        self.first = _First(calls)

    def click(self, *a, **k):
        return None


class _AgePage:
    """Страница с возрастным гейтом: probe=True, кнопка активна.

    Моделирует РЕАЛЬНУЮ картину: до клика модалка есть и виджетов нет, после
    клика модалка исчезает. Проверка результата (_age_gate_confirmed) появилась
    именно потому, что прежний код заявлял успех без проверки, а Ozon на части
    карточек модалку не убирал (наблюдение пользователя 2026-10-03).
    """

    def __init__(self, gate_after_click=False, date="01.01.1990"):
        self.calls = {}
        self._btn = _Button(self.calls, on_click=self._mark_clicked)
        self._inp = _Input()
        self._loc = _Locator(self.calls)
        self._clicked = False
        self.gate_after_click = gate_after_click
        self.date = date          # None = поле остаётся пустым (баг маски)
        self.mouse = _MouseSpy(self.calls, on_click=self._mark_clicked)

    def _mark_clicked(self):
        self._clicked = True

    def evaluate(self, expr, *a, **k):
        s = str(expr)
        if "userAdultModal" in s:
            # _AGE_GATE_CONFIRM_JS: разметка реальной модалки
            # [data-widget=userAdultModal] с полем даты и кнопкой
            return {"hasModal": True, "hasInput": True, "hasBtn": True,
                    "value": self.date if self.date else ""}
        if "Подтвердите возраст" in s and "gate:" in s:
            # _AGE_GATE_STATE_JS: dict{gate, ready}
            if self._clicked and not self.gate_after_click:
                return {"gate": False, "ready": True}
            return {"gate": True, "ready": False}
        if "Подтвердите возраст" in s:
            if self._clicked and not self.gate_after_click:
                return False
            return True
        return None

    def locator(self, sel, *a, **k):
        return self._loc

    def get_by_role(self, role, name=None, *a, **k):
        return self._btn

    def wait_for_timeout(self, ms):
        return None

    def query_selector(self, sel):
        return None


def test_pass_age_gate_fails_when_modal_stays(monkeypatch):
    """ГЛАВНЫЙ баг 2026-10-03: модалка осталась, а код рапортовал успех.

    Пользователь наблюдал вживую: «пишет, что возрастной гейт пройден, но на
    самом деле не пройден, страница так и зависает». Прежний код нажимал кнопку,
    ждал 1.5 с и БЕЗ ПРОВЕРКИ писал «гейт пройден» → возвращал True. След в
    данных: 483601244, 4818407500, 1701665910 дали 0 отзывов при формально
    успешном гейте.
    """
    lines = []
    import ozon_collector.log as _log
    monkeypatch.setattr(_log, "info", lambda m, extra=None: lines.append(str(m)))
    monkeypatch.setattr(_log, "warning",
                        lambda m, extra=None: lines.append(str(m)))

    page = _AgePage(gate_after_click=True)   # модалка НЕ уходит после клика
    ok = C._pass_age_gate(page)
    assert ok is False, "модалка осталась — гейт не пройден"
    clicks = page.calls.get("js_click", 0) + page.calls.get("mouse_clicks", 0)
    assert clicks >= 2, f"ожидалась повторная попытка: {page.calls}"
    blob = "\n".join(lines)
    assert "подтверждение не сработало" in blob, blob
    assert "модалка осталась на месте" in blob, blob
    assert "возрастной гейт 18+ пройден" not in blob, "лог не должен врать"
    assert "НЕ пройден" in blob, blob


def test_pass_age_gate_passes_when_widgets_appear(monkeypatch):
    """Гейт пройден, если модалка ещё в DOM, но виджеты товара появились.

    Ozon после подтверждения перерисовывает страницу, и текст модалки может
    ещё какое-то время оставаться — считать это провалом было бы ошибкой.
    """
    class _GateStaysButReady(_AgePage):
        def evaluate(self, expr, *a, **k):
            if "Подтвердите возраст" in str(expr) and "gate:" in str(expr):
                return {"gate": True, "ready": True}
            return super().evaluate(expr, *a, **k)

    page = _GateStaysButReady()
    assert C._pass_age_gate(page) is True


def test_age_gate_uses_modal_input_not_first_page_input():
    """Дата вводится в поле МОДАЛКИ, а не в первый input страницы.

    Разметка реальной модалки (DOM-снимок 2026-09-08):
        [data-widget="userAdultModal"] > div[name=birthdate][mask=##.##.####]
          > ... > input
    В шапке Ozon всегда есть поисковая строка, и прежний `input:visible` .first
    уводил дату туда — кнопка подтверждала пустое значение, модалка не уходила.
    """
    asked = []

    class _Page:
        def __init__(self):
            self.calls = {}
            self._btn = _Button(self.calls, on_click=self._mark)
            self._clicked = False
            self.mouse = _MouseSpy(self.calls, on_click=self._mark)

        def _mark(self):
            self._clicked = True

        def evaluate(self, expr, *a, **k):
            s = str(expr)
            if "userAdultModal" in s:
                return {"hasModal": True, "hasInput": True, "hasBtn": True,
                        "value": "01.01.1990"}
            if "Подтвердите возраст" in s and "gate:" in s:
                return {"gate": not self._clicked, "ready": self._clicked}
            if "Подтвердите возраст" in s:
                return True
            return None

        def locator(self, sel, *a, **k):
            asked.append(sel)
            return _Locator(self.calls)

        def get_by_role(self, role, name=None, *a, **k):
            return self._btn

        def wait_for_timeout(self, ms):
            return None

        def query_selector(self, sel):
            return None

    page = _Page()
    assert C._pass_age_gate(page) is True
    assert asked, "поля ввода вообще не искали"
    assert asked[0] == '[data-widget="userAdultModal"] input', asked
    assert 'input:visible' not in asked[:1], \
        "первым должен идти якорь модалки, а не первый input страницы"


class _MouseSpy:
    """page.mouse: настоящий (доверенный) клик через CDP."""

    def __init__(self, calls, on_click=None):
        self._calls = calls
        self._on_click = on_click

    def click(self, x, y, **k):
        self._calls["mouse_clicks"] = self._calls.get("mouse_clicks", 0) + 1
        self._calls["mouse_click"] = (round(float(x)), round(float(y)))
        if self._on_click:
            self._on_click()


def test_age_gate_confirms_with_real_mouse_click():
    """Подтверждение — НАСТОЯЩИМ кликом мышью по координатам кнопки.

    Замер 2026-10-03: JS-dispatch pointerdown/mousedown/... даёт НЕДОВЕРЕННОЕ
    событие, и Vue-обработчик Ozon его игнорирует — в логе было 20 случаев
    «подтверждение не сработало, модалка осталась на месте» при заполненном поле.
    Доверенное событие даёт только клик через CDP (page.mouse.click) — тем же
    путём, что работает перетаскивание пазла.
    """
    page = _AgePage()
    assert C._pass_age_gate(page) is True
    assert "mouse_click" in page.calls, \
        "подтверждение должно идти реальным кликом мышью, не JS-событием"
    assert page.calls.get("js_click", 0) == 0, \
        "JS-dispatch больше не основной путь подтверждения"


def test_pass_age_gate_avoids_playwright_click():
    """Подтверждение НЕ идёт обычным Playwright .click().

    Исторически: .click() падал на перехвате pointer events оверлеем модалки,
    поэтому сначала перешли на JS-dispatch, а теперь (замер 2026-10-03) на
    page.mouse.click — он доверенный и проходит через тот же CDP, что и
    перетаскивание пазла. Playwright .click() остаётся неиспользованным.
    """
    page = _AgePage()
    assert C._pass_age_gate(page) is True
    assert page.calls.get("pw_click", 0) == 0, \
        "обычный Playwright click всё ещё вызывается"
    assert "mouse_click" in page.calls, "ожидается доверенный клик мышью"


def test_pass_age_gate_skips_when_no_gate(monkeypatch):
    """Нет модалки «Подтвердите возраст» → пропускаем (False)."""
    class _NoGate:
        def evaluate(self, expr, *a, **k):
            return False

    assert C._pass_age_gate(_NoGate()) is False


def test_pass_age_gate_waits_for_late_modal(monkeypatch):
    """Модалка 18+ появляется ПОЗЖЕ: без ожидания гейт не проходился.

    Живой замер 2026-10-03: на карточках 4859784413 и 1639540706 гейт не
    проходился, страница отзывов оставалась пустой (stop_reason
    no_cards_no_count_hint, 0 отзывов, у карточки не было даже цены и
    рейтинга), хотя текст «Подтвердите возраст» в DOM присутствовал. Проба была
    однократной, сразу после загрузки, а модалка рендерится позже.
    """
    calls = {"waits": 0, "probes": 0}

    class _LateGate(_AgePage):
        def evaluate(self, expr, *a, **k):
            s = str(expr)
            if "userAdultModal" in s:
                # поле даты появляется вместе с модалкой
                return {"hasModal": True, "hasInput": True, "hasBtn": True,
                        "value": self.date}
            if "Подтвердите возраст" in s and "gate:" in s:
                # после клина подтверждение сработало: модалка ушла
                return {"gate": bool(self._clicked),
                        "ready": bool(self._clicked)}
            if "Подтвердите возраст" in s:
                calls["probes"] += 1
                # модалка появляется только с третьей пробы
                return calls["probes"] >= 3
            # страница не готова: виджетов нет
            return False

        def wait_for_timeout(self, ms):
            calls["waits"] += 1

    page = _LateGate()
    assert C._pass_age_gate(page) is True, "модалка должна была догрузиться"
    assert calls["probes"] >= 3, calls
    assert calls["waits"] >= 2, calls


def test_pass_age_gate_does_not_wait_when_page_ready(monkeypatch):
    """Готовая карточка без модалки: НЕ ждём (лишние секунды на 336 товаров)."""
    calls = {"waits": 0}

    class _ReadyNoGate:
        def evaluate(self, expr, *a, **k):
            if "Подтвердите возраст" in expr:
                return False
            return True          # страница отрисовалась

        def wait_for_timeout(self, ms):
            calls["waits"] += 1

    assert C._pass_age_gate(_ReadyNoGate()) is False
    assert calls["waits"] == 0, f"на готовой странице ждать нельзя: {calls}"


def test_pass_age_gate_warns_when_unrendered_without_modal(monkeypatch):
    """Страница не отрисовалась и модалки нет → предупреждение, а не молчание.

    Молчаливый False стоил нам карточек: по логу было видно только «0 отзывов»,
    и причина (гейт не догрузился) не была видна нигде.
    """
    lines = []
    import ozon_collector.log as _log
    monkeypatch.setattr(_log, "warning",
                        lambda m, extra=None: lines.append(str(m)))

    class _Unrendered:
        def evaluate(self, expr, *a, **k):
            return False

        def wait_for_timeout(self, ms):
            return None

    assert C._pass_age_gate(_Unrendered(), wait_ms=300) is False
    assert any("модалка 18+ не появилась" in m for m in lines), lines


def test_pass_age_gate_false_when_button_disabled():
    """Кнопка disabled → гейт не пройден (False)."""
    class _DisabledBtn:
        def wait_for(self, **k):
            return None

        def is_enabled(self):
            return False

    class _Page:
        def __init__(self):
            self._btn = _DisabledBtn()

        def evaluate(self, expr, *a, **k):
            return True

        def locator(self, *a, **k):
            return self

        @property
        def first(self):
            return self

        def click(self, *a, **k):
            return None

        def press_sequentially(self, *a, **k):
            return None

        def get_by_role(self, *a, **k):
            return self._btn

        def wait_for_timeout(self, ms):
            return None

        def query_selector(self, sel):
            return None

    assert C._pass_age_gate(_Page()) is False