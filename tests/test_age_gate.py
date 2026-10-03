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

    def __init__(self, calls):
        self._calls = calls

    def evaluate(self, expr, *a, **k):
        self._calls["js_click"] = self._calls.get("js_click", 0) + 1
        return None


class _Button:
    """locator кнопки «Подтвердить»: enabled, отдаёт JS-хэндл."""

    def __init__(self, calls):
        self._calls = calls

    def wait_for(self, state="visible", timeout=5000):
        return None

    def is_enabled(self):
        return True

    def element_handle(self):
        return _Handle(self._calls)

    def click(self, *a, **k):
        self._calls["pw_click"] = self._calls.get("pw_click", 0) + 1


class _Input:
    def click(self, *a, **k):
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


class _Locator:
    def __init__(self, calls):
        self._calls = calls
        self.first = _First(calls)

    def click(self, *a, **k):
        return None


class _AgePage:
    """Страница с возрастным гейтом: probe=True, кнопка активна."""

    def __init__(self):
        self.calls = {}
        self._btn = _Button(self.calls)
        self._inp = _Input()
        self._loc = _Locator(self.calls)

    def evaluate(self, expr, *a, **k):
        if "Подтвердите возраст" in str(expr):
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


def test_pass_age_gate_uses_js_click():
    """Подтверждение возраста идёт JS el.click() (не Playwright click)."""
    page = _AgePage()
    assert C._pass_age_gate(page) is True
    assert page.calls.get("js_click", 0) >= 1
    assert page.calls.get("pw_click", 0) == 0


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
            if "Подтвердите возраст" in expr:
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