"""Тесты политики окон (ozon_collector/window_policy.py).

Проверяют РЕШЕНИЕ, а не «не упало»: подменяются модульные функции Win32
(list_chrome_windows/foreground/minimize_hwnd/restore_focus/window_exists),
поэтому тест идёт без живого Windows и без браузера.

Требование, которое здесь защищается (мандат владельца 2026-10-16): окна
браузеров коллектора не должны всплывать на экран и не должны забирать
фокус. Исключение ровно одно — флаг data/window_visible.flag, который ставит
трей («Развернуть окна сбора»): пока флаг есть, сторож спит. Чужие окна
(браузер пользователя, другой Chrome), попапы того же Chrome и невидимые
окна — не трогаются вовсе.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from ozon_collector import window_policy as wp


def test_user32_memoized_single_instance():
    """_user32() мемоизирован: типы Win32 объявляются один раз на процесс.

    Регрессия ревью 07.10: типы объявлялись на первом экземпляре user32, а
    каждый вызов создавал новый → x64-обрезка hwnd со второго тика."""
    if wp._user32() is None:
        pytest.skip("user32 недоступен (не Windows)")
    assert wp._user32() is wp._user32()


class FakeWin32:
    """Подмена Win32-слоя window_policy.

    Identity-сверка pid (живость процесса + «держит ли он наш CDP-порт»)
    подменяется по умолчанию на успешную: без этого любой тест с несуществующим
    pid выкидывал бы его из реестра на первом же тике. Тесты на чистку реестра
    подменяют эти две функции сами.
    """

    def __init__(self, windows=None, fg=None, fg_exists=True):
        self.windows = list(windows or [])
        self.fg = fg
        self.fg_exists = fg_exists
        self.minimized: list[int] = []
        self.focus_calls: list[int] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(wp, "list_chrome_windows",
                            lambda: list(self.windows))
        monkeypatch.setattr(wp, "foreground", lambda: self.fg)
        monkeypatch.setattr(wp, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(wp, "_pid_owns_port", lambda pid, port: True)

        def _min(hwnd):
            self.minimized.append(hwnd)
            for w in self.windows:
                if w.hwnd == hwnd:
                    self.windows[self.windows.index(w)] = w._replace(iconic=True)
            return True
        monkeypatch.setattr(wp, "minimize_hwnd", _min)
        monkeypatch.setattr(wp, "window_exists", lambda h: self.fg_exists)
        monkeypatch.setattr(wp, "restore_focus",
                            lambda h: (self.focus_calls.append(h), True)[1])
        return self


def _win(hwnd, pid, visible=True, iconic=False, title="about:blank",
         width=1440, height=753):
    return wp.WindowRow(hwnd=hwnd, pid=pid, visible=visible, iconic=iconic,
                        title=title, width=width, height=height)


def test_unminimized_own_window_is_minimized_back(monkeypatch):
    """Окно нашего браузера вышло из minimized → сторож возвращает его."""
    fake = FakeWin32(windows=[_win(11, 500, iconic=False)]).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    out = g.tick()
    assert fake.minimized == [11], "окно должно быть свёрнуто обратно"
    assert out["unminimized"] == 1


def test_visible_iconic_ghost_strip_is_hidden(monkeypatch):
    """Видимое свёрнутое окно — «мини-плитка-призрак» — прячется как обычное.

    Живой замер 2026-10-16: SW_MINIMIZE/SW_SHOWMINNOACTIVE дают vis=True
    icon=True (полоска с крестиком над панелью задач) и кадров не отдают.
    Невидимое окно (IsWindowVisible=False) не трогаем — см.
    test_hidden_window_is_not_minimized.
    """
    fake = FakeWin32(windows=[_win(11, 500, iconic=True)]).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    out = g.tick()
    assert fake.minimized == [11]
    assert out["unminimized"] == 1


def test_foreign_browser_window_never_touched(monkeypatch):
    """Чужой Chrome пользователя не сворачивается — только наш pid."""
    fake = FakeWin32(windows=[_win(21, 999, iconic=False),
                              _win(22, 500, iconic=False)]).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g.tick()
    assert fake.minimized == [22]


def test_popup_of_same_browser_is_not_minimized(monkeypatch):
    """Внутреннее окно того же Chrome (мелочное/инфобар) не сворачиваем.

    Наш браузер сам открывает небольшие окна; их «мигание» не то, что мешает
    пользователю, а сворачивание ломает их жизненный цикл.
    """
    fake = FakeWin32(windows=[_win(31, 500, iconic=False, title="Сохранить пароль?",
                                   width=420, height=220)]).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g.tick()
    assert fake.minimized == []


def test_main_window_detection_by_size():
    """Порог отделяет главное окно браузера от попапов (1440x900 vs 420x220)."""
    assert _win(1, 1).is_main_window
    assert not _win(1, 1, width=320, height=240).is_main_window


def test_hidden_window_is_not_minimized(monkeypatch):
    """Скрытое с рождения окно (IsWindowVisible=False) не показываем и не трогаем."""
    fake = FakeWin32(windows=[_win(41, 500, visible=False, iconic=False)]
                     ).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    out = g.tick()
    assert fake.minimized == []
    assert out["unminimized"] == 0


def test_focus_stolen_by_our_browser_is_returned(monkeypatch):
    """Фокус ушёл на наш браузер → вернуть окно пользователя (2 тика: запомнить, потом вернуть)."""
    fake = FakeWin32(windows=[_win(51, 500, iconic=True)],
                     fg=_win(77, 1234, title="Мой редактор")).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g.tick()                       # запомнили чужое окно 77
    fake.fg = _win(52, 500, title="about:blank")   # фокус украден нашим Chrome
    out = g.tick()
    assert fake.focus_calls == [77], "фокус должен вернуться окну пользователя"
    assert out["focus_restored"] == 1


def test_focus_left_alone_when_user_window_in_front(monkeypatch):
    """Фокус у окна пользователя — не вмешиваемся (иначе воруем фокус сами)."""
    fake = FakeWin32(windows=[_win(61, 500, iconic=True)],
                     fg=_win(88, 1234)).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g.tick()
    g.tick()
    assert fake.focus_calls == []


def test_note_user_window_before_browser_start(monkeypatch):
    """Окно пользователя запоминается ДО старта браузера.

    Живой замер: Chromium крадёт фокус сразу при старте, поэтому к первому тику
    все окна в фокусе — наши; без note_user_window() last_fg остаётся None и
    фокус не возвращается никогда (воспроизведено: last_fg=None).
    """
    fake = FakeWin32(windows=[], fg=_win(123, 4242, title="Мой редактор")
                     ).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    assert g.note_user_window() == 123
    # теперь фокус украден нашим браузером
    fake.fg = _win(124, 500)
    out = g.tick()
    assert fake.focus_calls == [123]
    assert out["focus_restored"] == 1


def test_note_user_window_ignores_own_browser(monkeypatch):
    """Если в момент запоминания фокус уже на нашем браузере — не запоминаем
    (иначе вернём фокус самому себе)."""
    fake = FakeWin32(windows=[], fg=_win(124, 500)).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    assert g.note_user_window() is None
    out = g.tick()
    assert fake.focus_calls == []


def test_connect_order_does_not_remember_own_window(monkeypatch):
    """Порядок в `Session._connect` не должен запомнить собственное окно.

    Реальный порядок: сперва pid ещё НЕ зарегистрирован (браузер только что
    запущен и уже украл фокус), и лишь потом регистрируется pid. Если бы
    `note_user_window()` вызывался в этой точке, гвард запомнил бы собственное
    окно браузера и потом «возвращал» фокус самому себе (ревью 2026-10-06).
    Сценарий моделируется напрямую: pid=500 уже в фокусе, pid в реестре пуст.
    """
    fake = FakeWin32(windows=[], fg=_win(124, 500)).install(monkeypatch)
    g = wp.WindowGuard()
    g.note_user_window()                     # pid ещё НЕ зарегистрирован
    assert g._last_fg == 124
    # а вот с регистрацией — фильтр работает:
    g2 = wp.WindowGuard()
    g2.watch(500, "9222", autostart=False)
    g2.note_user_window()
    assert g2._last_fg is None


def test_note_user_window_respects_registry_when_browser_already_live(monkeypatch):
    """Браузер уже был жив, pid зарегистрирован, фокус на нём → не запоминаем.

    Это вторая точка вызова в `_connect` (после `_sync_window_guard`).
    """
    fake = FakeWin32(windows=[], fg=_win(124, 500)).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    assert g.note_user_window() is None
    fake.fg = _win(125, 700)
    g.note_user_window()
    assert g._last_fg == 125


def test_no_restore_when_previous_window_gone(monkeypatch):
    """Окно пользователя закрылось (hwnd невалиден) — фокус не трогаем."""
    fake = FakeWin32(windows=[_win(71, 500, iconic=True)],
                     fg=_win(99, 1234), fg_exists=False).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g.tick()
    fake.fg = _win(72, 500)
    out = g.tick()
    assert fake.focus_calls == []
    assert out["focus_restored"] == 0


def test_forget_pid_stops_protection(monkeypatch):
    """После forget() окно браузера больше не охраняется (видимый режим/external)."""
    fake = FakeWin32(windows=[_win(81, 500, iconic=False)]).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g.forget(500)
    out = g.tick()
    assert fake.minimized == []
    assert out == {"unminimized": 0, "focus_restored": 0}


def test_tick_without_registry_is_noop(monkeypatch):
    """Ничего не зарегистрировано — тик не трогает никаких окон."""
    fake = FakeWin32(windows=[_win(91, 500, iconic=False)]).install(monkeypatch)
    g = wp.WindowGuard()
    assert g.tick() == {"unminimized": 0, "focus_restored": 0}
    assert fake.minimized == []


def test_watch_is_idempotent_and_counts(monkeypatch):
    """Повторный watch того же pid не плодит записи и не дублирует работу."""
    fake = FakeWin32(windows=[_win(101, 500, iconic=False)]).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g.watch(500, "9222", autostart=False)
    assert g.watched_pids() == {500: "9222"}
    out = g.tick()
    assert fake.minimized == [101]
    assert out["unminimized"] == 1
    assert g.counters["windows_unminimized"] == 1


def test_event_is_logged_for_measurement(monkeypatch, caplog):
    """Каждое срабатывание попадает в лог: по нему видно источник в бою."""
    FakeWin32(windows=[_win(111, 500, iconic=False, title="about:blank")]
              ).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    with caplog.at_level("INFO", logger="ozon_collector.window_policy"):
        g.tick()
    assert "было видимо" in caplog.text
    assert "9222" in caplog.text


def test_events_mirror_to_project_log(monkeypatch, caplog):
    """Записи сторожа дублируются в проектный лог: daemon-лог был слеп к
    событиям сторожа — жалобу «окна открываются» нечем было проверить
    (разбор 2026-10-16)."""
    seen = []
    monkeypatch.setattr(wp.plog, "debug",
                        lambda msg, extra=None: seen.append(("debug", msg)))
    monkeypatch.setattr(wp.plog, "warning",
                        lambda msg, extra=None: seen.append(("warning", msg)))
    with caplog.at_level("DEBUG", logger="ozon_collector.window_policy"):
        wp.log.info("window_policy: тестовое событие")
        wp.log.warning("window_policy: тестовое предупреждение")
    assert ("debug", "window_policy: тестовое событие") in seen
    assert ("warning", "window_policy: тестовое предупреждение") in seen


@pytest.mark.skipif(wp.os.name != "nt", reason="Win32: нужен Windows")
def test_real_win32_enumeration_is_read_only():
    """Живой Win32: только ПЕРЕЧИСЛЕНИЕ окон, без единого действия над ними.

    Сюда НЕльзя звать `minimize_hwnd`/`restore_focus`: перечисление возвращает
    ВСЕ окна Chrome на машине, включая пользовательские, и такой тест сворачивал
    чужие окна при каждом прогоне (требование пользователя 2026-10-06: «трогай
    только свои»). Реальные вызовы проверяются только для pid из `watch()`
    (см. тесты с `FakeWin32`).
    """
    rows = wp.list_chrome_windows()
    assert isinstance(rows, list)
    for r in rows:
        assert isinstance(r.hwnd, int) and isinstance(r.pid, int)
        assert isinstance(r.visible, bool) and isinstance(r.iconic, bool)


def test_focus_retry_is_rate_limited(monkeypatch):
    """Попытка вернуть фокус не чаще FOCUS_RETRY_S: синтетический Alt не
    должен «стрелять» каждый тик, если возврат не удаётся."""
    fake = FakeWin32(windows=[_win(131, 500, iconic=True)],
                     fg=_win(132, 1234)).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g.tick()                                   # запомнили окно пользователя
    fake.fg = _win(133, 500)                   # фокус у нашего браузера
    calls = {"n": 0}

    def _fail_restore(_h):
        calls["n"] += 1
        return False
    monkeypatch.setattr(wp, "restore_focus", _fail_restore)
    g.tick()
    assert calls["n"] == 1
    g.tick()                                   # сразу после — должен пропустить
    g.tick()
    assert calls["n"] == 1, "повторные попытки должны быть ограничены паузой"
    g._last_focus_try -= wp.FOCUS_RETRY_S + 0.1
    g.tick()
    assert calls["n"] == 2, "после паузы попытка повторяется"


def test_self_shown_window_is_hidden_without_flag(monkeypatch):
    """Всплытие без флага трея (Chromium сам показал окно) — прячем."""
    fake = FakeWin32(windows=[_win(141, 500, iconic=False)],
                     fg=_win(141, 500)).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    out = g.tick()
    assert fake.minimized == [141]
    assert out["unminimized"] == 1


def test_user_visible_flag_makes_guard_sleep(monkeypatch):
    """Пользователь развернул окна из трея (флаг) — сторож не трогает ничего.

    Именно это требование владельца: трей «Развернуть окна сбора» показывает
    окна для работы; сторож (Python в демоне и воркерах) видит флаг
    data/window_visible.flag и спит, пока флаг стоит.
    """
    fake = FakeWin32(windows=[_win(151, 500, iconic=False)],
                     fg=_win(151, 500)).install(monkeypatch)
    monkeypatch.setattr(wp, "user_visible", lambda: True)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    out = g.tick()
    assert fake.minimized == [], "при флаге окна пользователя не трогаем"
    assert out == {"unminimized": 0, "focus_restored": 0}


def test_flag_removed_and_windows_are_hidden_again(monkeypatch):
    """Флаг снят («Свернуть окна сбора») — сторож снова прячет окна."""
    fake = FakeWin32(windows=[_win(161, 500, iconic=False)]).install(monkeypatch)
    flag = {"on": True}
    monkeypatch.setattr(wp, "user_visible", lambda: flag["on"])
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    assert g.tick()["unminimized"] == 0
    flag["on"] = False
    out = g.tick()
    assert fake.minimized == [161]
    assert out["unminimized"] == 1


def test_popup_while_focus_elsewhere_is_hidden(monkeypatch):
    """Окно выскочило, фокус у пользователя (другое приложение) — прячем."""
    fake = FakeWin32(windows=[_win(171, 500, iconic=False)],
                     fg=_win(172, 4242)).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    out = g.tick()
    assert fake.minimized == [171]
    assert out["focus_restored"] == 0


def test_flag_makes_guard_leave_focus_alone(monkeypatch):
    """При флаге трея сторож не вмешивается и в фокус."""
    fake = FakeWin32(windows=[_win(181, 500, iconic=False)],
                     fg=_win(181, 500)).install(monkeypatch)
    monkeypatch.setattr(wp, "user_visible", lambda: True)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g._last_fg = 182
    g.tick()
    assert fake.focus_calls == [], "фокус при флаге не трогаем"


def test_user_visible_state_is_logged_once(monkeypatch, caplog):
    """«Сторож спит» пишется в лог один раз, а не каждый тик 150 мс."""
    FakeWin32(windows=[]).install(monkeypatch)
    monkeypatch.setattr(wp, "user_visible", lambda: True)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    with caplog.at_level("INFO", logger="ozon_collector.window_policy"):
        g.tick()
        g.tick()
    assert caplog.text.count("сторож спит") == 1


def test_singleton_guard_is_one_per_process():
    """9-17 Session не должны плодить потоки: сторож один на процесс."""
    assert wp.guard() is wp.guard()


# ---- интеграция с Session ---------------------------------------------------

def _fake_session(keep_window=False, owner="collector", kind="chrome", pid=777):
    return SimpleNamespace(keep_window=keep_window, browser_owner=owner,
                           kind=kind,
                           browser_pid=SimpleNamespace(pid=pid) if pid else None,
                           cdp_url="http://127.0.0.1:9222")


def _guard_with(monkeypatch, g=None):
    """Подменённый гвард БЕЗ фонового потока.

    Поток нужен для проверки `tick` вживую, но в тестах реестра он мешает:
    `_drop_dead_browsers` выкидывает несуществующий pid (777/500) и к моменту
    проверки реестр оказывался пустым — тест падал в полном прогоне и проходил
    изолированно (гонка). Поток проверяется отдельно в
    `test_guard_thread_starts_once_and_stops`.
    """
    g = g or wp.WindowGuard()
    monkeypatch.setattr(g, "start", lambda: None)
    monkeypatch.setattr(wp, "guard", lambda: g)
    return g


def test_guard_enabled_by_default(monkeypatch):
    """ПО УМОЛЧАНИЮ сторож ВКЛЮЧЁН.

    Мандат владельца 2026-10-16: окна сбора «вылезают» сами и мешают работе —
    сторож обязан их прятать. Выключение — только явное (OZON_WINDOW_GUARD=0).
    """
    monkeypatch.delenv("OZON_WINDOW_GUARD", raising=False)
    assert wp.enabled_by_env() is True
    assert wp.guard() is wp.guard()


def test_guard_disabled_only_by_explicit_env(monkeypatch):
    """Выключение — только явное значение (0/off/false/no)."""
    for val in ("0", "off", "false", "no", " OFF "):
        monkeypatch.setenv("OZON_WINDOW_GUARD", val)
        assert wp.enabled_by_env() is False, f"{val!r} должен выключать сторожа"
    monkeypatch.setenv("OZON_WINDOW_GUARD", "1")
    assert wp.enabled_by_env() is True


def test_session_does_not_touch_windows_when_guard_disabled(monkeypatch):
    """Выключенный сторож — Session не вызывает его методов вовсе."""
    from ozon_collector import cdp
    monkeypatch.setenv("OZON_WINDOW_GUARD", "0")

    class Boom(wp.WindowGuard):
        def watch(self, *a, **k):
            raise AssertionError("сторож не должен регистрировать pid")

        def forget(self, *a, **k):
            raise AssertionError("сторож не должен трогать реестр")

    # Подменяем сам factory: он обязан вернуть None (сторож выключен) и НЕ
    # создавать WindowGuard — тогда даже случайная регистрация упала бы.
    monkeypatch.setattr(wp, "WindowGuard", Boom)
    monkeypatch.setattr(wp, "_GUARD", None)
    assert wp.guard() is None
    cdp.Session._sync_window_guard(_fake_session(), " (t)", enforce_hidden=True)


def test_session_hidden_collector_window_is_guarded(monkeypatch):
    """hidden + свой браузер → окно коллектора под защитой сторожа."""
    from ozon_collector import cdp
    g = _guard_with(monkeypatch)
    cdp.Session._sync_window_guard(_fake_session(), " (t)", enforce_hidden=True)
    assert g.watched_pids() == {777: "9222"}


def test_session_visible_mode_not_guarded(monkeypatch):
    """--window-mode visible: окно должно быть видно, сторож его не трогает."""
    from ozon_collector import cdp
    g = _guard_with(monkeypatch)
    sess = _fake_session(keep_window=True)
    cdp.Session._sync_window_guard(sess, " (t)", enforce_hidden=True)
    assert g.watched_pids() == {}
    cdp.Session._sync_window_guard(sess, " (t)", enforce_hidden=False)
    assert g.watched_pids() == {}


def test_session_keep_window_yields_to_tray_hidden(monkeypatch, tmp_path):
    """Явный "0" из трея сильнее keep_window и в Session-стороже.

    --window-mode visible задаёт стартовый режим; после «Свернуть» окно
    обязано снова попасть под охрану, иначе при падении трея оно останется
    видимым (MAJOR ревью 2026-10-19).
    """
    from ozon_collector import cdp
    flag = tmp_path / "window_visible.flag"
    flag.write_text("0", encoding="utf-8")
    monkeypatch.setattr(wp, "USER_VISIBLE_FLAG", flag)
    monkeypatch.setenv("OZON_KEEP_WINDOW", "1")
    g = _guard_with(monkeypatch)
    sess = _fake_session(keep_window=True)
    cdp.Session._sync_window_guard(sess, " (t)", enforce_hidden=True)
    assert g.watched_pids() == {777: "9222"}


def test_apply_window_policy_honors_tray_hidden(monkeypatch, tmp_path):
    """keep_window must not resurrect windows after the tray wrote "0"."""
    from ozon_collector import cdp
    flag = tmp_path / "window_visible.flag"
    flag.write_text("0", encoding="utf-8")
    monkeypatch.setattr(wp, "USER_VISIBLE_FLAG", flag)
    monkeypatch.setenv("OZON_KEEP_WINDOW", "1")
    calls = []
    monkeypatch.setattr(cdp.Session, "_keep_window_visible",
                        lambda self: calls.append("keep"))
    monkeypatch.setattr(cdp.Session, "_minimize_window",
                        lambda self: calls.append("min"))
    monkeypatch.setattr(
        cdp.Session, "_sync_window_guard",
        lambda self, why="", enforce_hidden=False:
        calls.append(("sync", enforce_hidden)))
    sess = cdp.Session.__new__(cdp.Session)
    sess.keep_window = True
    sess.kind = "chrome"
    cdp.Session._apply_window_policy(sess, " (t)")
    assert calls == ["min", ("sync", True)], calls


def test_apply_window_policy_tray_visible_beats_env_hidden(monkeypatch, tmp_path):
    """env hidden + tray "1": the tray is authoritative, window stays visible."""
    from ozon_collector import cdp
    flag = tmp_path / "window_visible.flag"
    flag.write_text("1", encoding="utf-8")
    monkeypatch.setattr(wp, "USER_VISIBLE_FLAG", flag)
    monkeypatch.setenv("OZON_KEEP_WINDOW", "0")
    calls = []
    monkeypatch.setattr(cdp.Session, "_keep_window_visible",
                        lambda self: calls.append("keep"))
    monkeypatch.setattr(cdp.Session, "_minimize_window",
                        lambda self: calls.append("min"))
    monkeypatch.setattr(
        cdp.Session, "_sync_window_guard",
        lambda self, why="", enforce_hidden=False:
        calls.append(("sync", enforce_hidden)))
    sess = cdp.Session.__new__(cdp.Session)
    sess.keep_window = False
    sess.kind = "chrome"
    cdp.Session._apply_window_policy(sess, " (t)")
    assert calls == ["keep", ("sync", False)], calls


def test_apply_window_policy_never_touches_external(monkeypatch, tmp_path):
    """Tray "1" must not show/move the user's own Chrome (external owner)."""
    from ozon_collector import cdp
    flag = tmp_path / "window_visible.flag"
    flag.write_text("1", encoding="utf-8")
    monkeypatch.setattr(wp, "USER_VISIBLE_FLAG", flag)
    monkeypatch.setenv("OZON_KEEP_WINDOW", "1")
    calls = []
    monkeypatch.setattr(cdp.Session, "_keep_window_visible",
                        lambda self: calls.append("keep"))
    monkeypatch.setattr(cdp.Session, "_minimize_window",
                        lambda self: calls.append("min"))
    monkeypatch.setattr(
        cdp.Session, "_sync_window_guard",
        lambda self, why="", enforce_hidden=False:
        calls.append(("sync", enforce_hidden)))
    sess = cdp.Session.__new__(cdp.Session)
    sess.keep_window = True
    sess.kind = "chrome"
    sess.browser_owner = "external"
    cdp.Session._apply_window_policy(sess, " (t)")
    assert calls == [("sync", False)], calls


def test_geometry_guard_never_normalizes_external(monkeypatch):
    """External Chrome desync must not trigger Browser.setWindowBounds."""
    from ozon_collector import cdp
    calls = []
    monkeypatch.setattr(
        cdp.Session, "_assert_geometry_ok",
        lambda self, page, tolerance=3.0: (False, {"hwnd_rect": [0, 0, 1, 1]}))
    monkeypatch.setattr(cdp.Session, "_normalize_window_geometry",
                        lambda self, *a, **k: calls.append("norm"))
    sess = cdp.Session.__new__(cdp.Session)
    sess.browser_owner = "external"
    ok, m = cdp.Session._geometry_guard(sess, object())
    assert ok is True, "внешнее окно не должно блокировать пазл"
    assert calls == [], "normalize тронул чужое окно"
    assert m.get("skipped") == "external", m


def test_sync_window_guard_tray_visible_not_watched(monkeypatch, tmp_path):
    """Tray "1" + env hidden must not register the window for hiding."""
    from ozon_collector import cdp
    flag = tmp_path / "window_visible.flag"
    flag.write_text("1", encoding="utf-8")
    monkeypatch.setattr(wp, "USER_VISIBLE_FLAG", flag)
    monkeypatch.setenv("OZON_KEEP_WINDOW", "0")
    g = _guard_with(monkeypatch)
    cdp.Session._sync_window_guard(_fake_session(keep_window=False), " (t)",
                                   enforce_hidden=True)
    assert g.watched_pids() == {}


def test_minimize_window_skips_native_when_tray_visible(monkeypatch, tmp_path):
    """Direct minimize path obeys the tray flag too (review 2026-10-19)."""
    from ozon_collector import cdp
    flag = tmp_path / "window_visible.flag"
    flag.write_text("1", encoding="utf-8")
    monkeypatch.setattr(wp, "USER_VISIBLE_FLAG", flag)
    calls = []
    monkeypatch.setattr(cdp.Session, "_sync_window_guard", lambda *a, **k: None)
    monkeypatch.setattr(cdp.Session, "_normalize_window_geometry",
                        lambda self, *a, **k: False)
    monkeypatch.setattr(cdp.Session, "_minimize_window_native",
                        lambda self, *a, **k: calls.append("native"))
    sess = cdp.Session.__new__(cdp.Session)
    sess.kind = "chrome"
    sess.browser_owner = "collector"
    cdp.Session._minimize_window(sess)
    assert calls == [], "окно скрыто вопреки явному visible из трея"


def test_apply_window_policy_firefox_hidden_skips_restore(monkeypatch, tmp_path):
    """Debug Firefox must not restore its window after an explicit hidden."""
    from ozon_collector import cdp
    flag = tmp_path / "window_visible.flag"
    flag.write_text("0", encoding="utf-8")
    monkeypatch.setattr(wp, "USER_VISIBLE_FLAG", flag)
    calls = []
    monkeypatch.setattr(cdp.Session, "_restore_window_native",
                        lambda self, **kw: calls.append("restore"))
    monkeypatch.setattr(cdp.Session, "_sync_window_guard", lambda self, *a, **k: None)
    sess = cdp.Session.__new__(cdp.Session)
    sess.keep_window = True
    sess.kind = "firefox"
    cdp.Session._apply_window_policy(sess, " (t)")
    assert calls == [], "firefox развернулся вопреки явному hidden"


def test_session_external_browser_not_guarded(monkeypatch):
    """Чужой Chrome пользователя (owner=external) не сворачиваем никогда."""
    from ozon_collector import cdp
    g = _guard_with(monkeypatch)
    cdp.Session._sync_window_guard(_fake_session(owner="external"), " (t)",
                                   enforce_hidden=True)
    assert g.watched_pids() == {}


def test_session_firefox_not_guarded(monkeypatch):
    """Firefox сворачивается по другой политике (restore без фокуса)."""
    from ozon_collector import cdp
    g = _guard_with(monkeypatch)
    cdp.Session._sync_window_guard(_fake_session(kind="firefox"), " (t)",
                                   enforce_hidden=True)
    assert g.watched_pids() == {}


def test_session_without_pid_is_safe(monkeypatch):
    """Нет pid (окно ещё не найдено) — регистрация не падает."""
    from ozon_collector import cdp
    g = _guard_with(monkeypatch)
    cdp.Session._sync_window_guard(_fake_session(pid=None), " (t)",
                                   enforce_hidden=True)
    assert g.watched_pids() == {}


def test_dead_browser_pid_is_dropped_before_touching_windows(monkeypatch):
    """Мёртвый pid выкидывается из реестра ДО обработки окон.

    Без этого Windows переиспользует pid упавшего воркер-браузера, и гвард
    сворачивает уже чужое окно (ревью 2026-10-06). Подмены ставятся ДО install:
    `FakeWin32.install` подменяет обе функции успешными.
    """
    # окно pid=500 («умерший» браузер коллектора) и pid=900 (чужой pid,
    # который Windows отдала другому процессу)
    fake = FakeWin32(windows=[_win(11, 500, iconic=False),
                              _win(12, 900, iconic=False)])
    fake.install(monkeypatch)
    # install подставляет успешную identity-сверку; переопределяем ПОСЛЕ неё
    monkeypatch.setattr(wp, "_pid_alive", lambda pid: pid != 500)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g.tick()
    assert 500 not in g.watched_pids(), "мёртвый pid обязан уйти из реестра"
    assert fake.minimized == [], "пид вне реестра не сворачиваем вовсе"
    # а если pid 900 тоже наш (живой браузер коллектора) — тогда сворачиваются
    # оба его окна (hwnd 12 и 13), а не окно упавшего pid 500
    fake.windows.append(_win(13, 900, iconic=False))
    g.watch(900, "9223", autostart=False)
    g.tick()
    assert fake.minimized == [12, 13], "сворачиваются окна живых наших pid"
    assert 11 not in fake.minimized, "окно упавшего pid не трогаем"


def test_pid_that_stopped_owning_port_is_dropped(monkeypatch):
    """Процесс жив, но CDP-порт больше не его → тоже убираем (identity)."""
    fake = FakeWin32(windows=[_win(13, 500, iconic=False)])
    fake.install(monkeypatch)
    monkeypatch.setattr(wp, "_pid_owns_port", lambda pid, port: False)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g.tick()
    assert g.watched_pids() == {}
    assert fake.minimized == []


def test_unknown_port_ownership_keeps_pid_watched(monkeypatch):
    """Unverifiable identity (both checks failed) must NOT evict the pid.

    Audit regression 2026-10-19: _pid_owns_port returned False when the check
    could not be performed, so a live collector browser fell out of the watch
    list and its flashing window stayed on screen.
    """
    fake = FakeWin32(windows=[_win(13, 500, iconic=False)])
    fake.install(monkeypatch)
    monkeypatch.setattr(wp, "_pid_owns_port", lambda pid, port: None)
    # Детерминизм: на хосте с читаемым pid 500 image-check выкинул бы pid до
    # probe, и ассерт ниже стал бы вакуумным (review 2026-10-19, pass 4).
    monkeypatch.setattr(wp, "process_image_basename", lambda pid: None)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g.tick()
    assert 500 in g.watched_pids(), \
        "неизвестная identity выкинула живой pid из охраны"
    assert fake.minimized == [13], "живой pid продолжает охраняться и гасится"


def test_unknown_ownership_backoff_counts_from_probe_end(monkeypatch):
    """Backoff после недоказуемой identity — от конца probe, не от начала тика.

    Fallback PS-probe штатно может жечь ~20 с (sick WMI); если штамп ставить
    по `now` начала тика, к следующему тику `now - last` уже превышает
    IDENTITY_RECHECK_S и probe повторяется каждый тик — backoff не работает
    (review 2026-10-19, pass 3).
    """
    calls = {"n": 0}
    clock = [1000.0]

    def _owns(pid, port):
        calls["n"] += 1
        clock[0] += 21.0           # имитация 20-с таймаута PS-probe
        return None

    FakeWin32(windows=[_win(13, 500, iconic=False)]).install(monkeypatch)
    monkeypatch.setattr(wp, "_pid_owns_port", _owns)
    monkeypatch.setattr(wp, "process_image_basename", lambda pid: None)
    monkeypatch.setattr(wp.time, "monotonic", lambda: clock[0])
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g.tick()
    assert calls["n"] == 1
    clock[0] += 0.2                # следующий тик сразу после probe
    g.tick()
    assert calls["n"] == 1, \
        "probe повторился сразу после медленного — backoff считался не от конца"
    clock[0] += 2.1                # backoff истёк — probe обязан повториться
    g.tick()
    assert calls["n"] == 2, \
        "backoff навсегда: probe не повторился после ~2 с"


def test_unknown_ownership_drops_foreign_image(monkeypatch):
    """Неизвестная identity + чужой образ → pid уходит из охраны.

    Порт-сверка недоступна, но дешёвый образ процесса решает: pid переиспользован
    не-Chrome процессом, окна которого гасить нельзя (ревью 2026-10-19, pass 5).
    """
    fake = FakeWin32(windows=[_win(13, 500, iconic=False)])
    fake.install(monkeypatch)
    monkeypatch.setattr(wp, "_pid_owns_port", lambda pid, port: None)
    monkeypatch.setattr(wp, "process_image_basename", lambda pid: "notepad.exe")
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g._grace_until[500] = 0.0      # grace истёк — решает только None-ветка
    g.tick()
    assert 500 not in g.watched_pids(), \
        "чужой образ при неизвестной identity остался под охраной"
    assert fake.minimized == [], "окно чужого процесса не сворачиваем"


def test_identity_recheck_is_throttled(monkeypatch):
    """Полная сверка порта — раз в IDENTITY_RECHECK_S, не каждый тик.

    Она читает сокет-таблицу ядра (с 2026-10-17 дёшево), но 150-мс тик не
    должен дёргать её без нужды: pid-реюзы редки.
    """
    calls = {"n": 0}

    def _owns(pid, port):
        calls["n"] += 1
        return True
    FakeWin32(windows=[]).install(monkeypatch)
    monkeypatch.setattr(wp, "_pid_owns_port", _owns)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g.tick()
    first = calls["n"]
    assert first == 1, "первый тик сверяет identity один раз"
    g.tick()
    g.tick()
    assert calls["n"] == first, "сверка порта не должна идти каждый тик"
    g._identity_checked[500] -= (wp.IDENTITY_RECHECK_S + 1)
    g.tick()
    assert calls["n"] == first + 1, "после паузы сверка повторяется"


def test_early_watch_grace_keeps_pid_until_identity_recheck(monkeypatch):
    """Ранняя регистрация (watch_launcher_pid) не выбрасывается первым тиком.

    Chromium занимает CDP-порт через секунды после CreateProcess, а окно может
    показаться раньше: мгновенная сверка identity выбрасывала живой pid
    («не держит порт») и показ никто не гасил — живой замер 2026-10-18,
    .tmp/gap_harness.py: окно висело 4152 мс. С grace-отметкой первый тик пид
    не трогает; мёртвый процесс и так отсекает _pid_alive.
    """
    fake = FakeWin32(windows=[_win(13, 500, iconic=False)])
    fake.install(monkeypatch)
    checked = []
    monkeypatch.setattr(wp, "_pid_owns_port",
                        lambda pid, port: checked.append(pid) or False)
    # Герметично: символ процесса — chrome (в grace каждый тик сверяет образ,
    # а pid 500 на живой машине мог бы существовать с чужим образом).
    monkeypatch.setattr(wp, "process_image_basename",
                        lambda pid: "chrome.exe")
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False, identity_grace=True)
    g.tick()
    assert 500 in g.watched_pids(), "живой pid с grace обязан остаться"
    assert checked == [], "в grace-окне сверка порта не запускается"
    assert fake.minimized == [13], "окно обязано сворачиваться в том же тике"
    # Середина grace-окна: за 15-й меткой порт-сверка НЕ запускается —
    # регресс «grace сжался до IDENTITY_RECHECK_S» ловится именно здесь
    # (PROBE1 ревью 2026-10-18, раунд 3).
    g._identity_checked[500] -= (wp.IDENTITY_RECHECK_S + 1)
    g.tick()
    assert 500 in g.watched_pids(), "grace держится и после 15-й секунды"
    assert checked == [], "порт-сверка до EARLY_WATCH_GRACE_S запрещена"
    g._identity_checked[500] -= (wp.EARLY_WATCH_GRACE_S + 1)
    g.tick()
    assert 500 not in g.watched_pids(), "после grace сверка обязана вернуться"


def test_early_watch_grace_drops_pid_reused_by_other_process(monkeypatch):
    """В grace pid, переиспользованный чужим процессом, обязан выпасть.

    До привязки порта идентичность держит только образ процесса: иначе смерть
    Chrome + реюз pid на чужой процесс свернули бы чужие окна до порт-сверки
    (находка ревью 2026-10-18).
    """
    fake = FakeWin32(windows=[_win(14, 501, iconic=False)])
    fake.install(monkeypatch)
    monkeypatch.setattr(wp, "process_image_basename",
                        lambda pid: "notepad.exe")
    g = wp.WindowGuard()
    g.watch(501, "9222", autostart=False, identity_grace=True)
    g.tick()
    assert 501 not in g.watched_pids(), "чужой образ обязан снять охрану"
    assert fake.minimized == [], "чужие окна не трогаем"


def test_early_watch_grace_tail_checks_image(monkeypatch):
    """Хвост grace (порт-сверка ещё не наступила): образ всё ещё сверяется.

    Дыра t0+30..t0+45: сдвинутая метка уже в прошлом, порт-сверка ещё не
    наступила — без _grace_until этот интервал был без identity вовсе
    (PROBE2 ревью 2026-10-18, раунд 3).
    """
    fake = FakeWin32(windows=[_win(15, 502, iconic=False)])
    fake.install(monkeypatch)
    monkeypatch.setattr(wp, "process_image_basename", lambda pid: "notepad.exe")
    g = wp.WindowGuard()
    g.watch(502, "9222", autostart=False, identity_grace=True)
    # Состояние на t0+31: grace-сдвиг метки уже прошёл (метка на секунду в
    # прошлом), порт-сверка (now - last >= 15) ещё не наступила
    g._identity_checked[502] -= (wp.EARLY_WATCH_GRACE_S - 14)
    g.tick()
    assert 502 not in g.watched_pids(), \
        "в хвосте grace образ обязан снимать охрану"
    assert fake.minimized == [], "чужие окна не трогаем"


def test_watch_launcher_pid_gates_hidden_mode(monkeypatch, caplog):
    """Ранняя охрана: hidden+сторож → watch с identity_grace; иначе — нет."""
    monkeypatch.setenv("OZON_WINDOW_GUARD", "1")
    monkeypatch.delenv("OZON_KEEP_WINDOW", raising=False)
    g = _guard_with(monkeypatch)
    calls = []
    monkeypatch.setattr(g, "watch",
                        lambda pid, port, **kw: calls.append((pid, port, kw)))
    assert wp.watch_launcher_pid(1234, 9222) is True, \
        "hidden-режим обязан регистрировать раннего сторожа"
    assert calls == [(1234, 9222, {"identity_grace": True})], \
        "ранний watch обязан идти с identity_grace (порт ещё не занят)"
    monkeypatch.setenv("OZON_KEEP_WINDOW", "1")
    with caplog.at_level("DEBUG", logger="ozon_collector.window_policy"):
        assert wp.watch_launcher_pid(1234, 9222) is False and len(calls) == 1, \
            "visible-режим не трогаем (окно должно остаться видимым)"
    assert "видимы" in caplog.text, "причина отказа обязана попасть в лог"
    monkeypatch.delenv("OZON_KEEP_WINDOW", raising=False)
    monkeypatch.setenv("OZON_WINDOW_GUARD", "0")
    assert wp.watch_launcher_pid(1234, 9222) is False and len(calls) == 1, \
        "выключенный сторож не регистрируем"


@pytest.mark.skipif(wp.os.name != "nt", reason="Windows-only (iphlpapi)")
def test_port_owner_pids_finds_listener():
    """Сокет-таблица ядра отдаёт pid держателя LISTEN-сокета (без WMI).

    На этом пути стоит identity-сверка сторожа: раньше каждый вызов поднимал
    PowerShell (~0.5-1 с), и первый тик с показом окна опаздывал — живой замер
    2026-10-17: вспышка 0.73 с. Проверка живая: реальный сокет этого процесса.
    """
    import os as _os
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        owners = wp.port_owner_pids(port)
        assert owners is not None, \
            "сокет-таблица недоступна — identity уйдёт на медленный fallback"
        assert _os.getpid() in owners, "свой LISTEN-сокет обязан быть в таблице"
    finally:
        s.close()


def test_pid_owns_port_prefers_socket_table(monkeypatch):
    """Быстрый путь identity: порт держит ровно этот pid (сокет-таблица)."""
    monkeypatch.setattr(wp, "port_owner_pids", lambda port: {42})
    assert wp._pid_owns_port(42, "9222") is True
    assert wp._pid_owns_port(7, "9222") is False


def test_pid_owns_port_falls_back_to_cmdline(monkeypatch):
    """Таблица недоступна → исторический fallback по командной строке."""
    monkeypatch.setattr(wp, "port_owner_pids", lambda port: None)
    called = []
    monkeypatch.setattr(
        wp, "_pid_owns_port_via_cmdline",
        lambda pid, port: (called.append((pid, port)), True)[1])
    assert wp._pid_owns_port(42, "9222") is True
    assert called == [(42, "9222")]


def test_pid_owns_port_none_when_unverifiable(monkeypatch):
    """Both checks unavailable -> None ("unknown"), never False ("not owning")."""
    monkeypatch.setattr(wp, "port_owner_pids", lambda port: None)
    monkeypatch.setattr(wp, "_pid_owns_port_via_cmdline",
                        lambda pid, port: None)
    assert wp._pid_owns_port(42, "9222") is None


def test_should_hide_show_event_matrix():
    """Решение хука показа: фильтр тот же, что у тика (наши окна, не попапы)."""
    base = dict(pid=500, class_name="Chrome_WidgetWin_1", width=1440,
                height=900, watched={500: "9222"})
    assert wp.should_hide_show_event(**base, uv=False) is True
    assert wp.should_hide_show_event(**base, uv=True) is False, \
        "пользователь развернул окна из трея — хук обязан молчать"
    assert wp.should_hide_show_event(**{**base, "pid": 999}, uv=False) is False
    assert wp.should_hide_show_event(
        **{**base, "class_name": "MozillaWindowClass"}, uv=False) is False
    assert wp.should_hide_show_event(
        **{**base, "width": 200, "height": 40}, uv=False) is False, \
        "мелкий попап/инфобар не трогаем"


def test_show_event_callback_hides_watched_window(monkeypatch):
    """EVENT_OBJECT_SHOW нашего окна гасится колбэком сразу, не тиком.

    Замер 2026-10-17: Chrome показывает окно сам при создании вкладки через
    CDP (`/json/new`) — тик 150 мс на это опаздывает.
    """
    hidden = []

    def _fake_min(hwnd):
        hidden.append(hwnd)
        return True

    class _U32:
        def GetWindowRect(self, hwnd, ref):
            rect = ref._obj
            rect.left = 0
            rect.top = 0
            rect.right = 1440
            rect.bottom = 900
            return True

    monkeypatch.setattr(wp, "minimize_hwnd", _fake_min)
    monkeypatch.setattr(wp, "_setup_api", lambda u: None)
    monkeypatch.setattr(wp, "_user32", lambda: _U32())
    monkeypatch.setattr(wp, "_window_pid", lambda u, h: 500)
    monkeypatch.setattr(wp, "_window_class", lambda u, h: "Chrome_WidgetWin_1")
    monkeypatch.setattr(wp, "user_visible", lambda: False)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g._on_win_event(None, wp.EVENT_OBJECT_SHOW, 1234, wp.OBJID_WINDOW, 0, 0, 0)
    assert hidden == [1234], "показавшееся окно нашего pid обязано прятаться"
    monkeypatch.setattr(wp, "_window_pid", lambda u, h: 999)
    g._on_win_event(None, wp.EVENT_OBJECT_SHOW, 1235, wp.OBJID_WINDOW, 0, 0, 0)
    assert hidden == [1234], "чужой pid хук не трогает"
    g._on_win_event(None, 0x8003, 1236, wp.OBJID_WINDOW, 0, 0, 0)
    assert hidden == [1234], "не-show событие игнорируется"


@pytest.mark.skipif(wp.os.name != "nt", reason="поток хука: нужен Windows")
def test_event_hook_thread_starts_and_stops():
    """Поток хука не течёт: стартует со сторожем, гаснет по WM_QUIT.

    Висящий поток — заведённый инцидент проекта (awk.exe, см. AGENTS.md);
    поэтому start() обязан поднимать насос ровно один, а stop() — будить его.
    """
    import time
    g = wp.WindowGuard(poll_s=0.01)
    g.start()
    hook_thread = g._hook_thread
    assert hook_thread is not None and hook_thread.is_alive()
    deadline = time.monotonic() + 3.0
    while g._hook_tid is None and time.monotonic() < deadline:
        time.sleep(0.02)
    g.stop()
    hook_thread.join(timeout=5)
    assert not hook_thread.is_alive(), "поток хука не остановился по WM_QUIT"
    g._thread.join(timeout=5)


@pytest.mark.skipif(wp.os.name != "nt", reason="поток хука: нужен Windows")
def test_event_hook_stop_immediately_after_start():
    """stop() сразу после start() не оставляет висящий поток-насос.

    Гонка: stop() успевал до публикации _hook_tid, WM_QUIT отправить было
    некуда, и поток навсегда зависал в GetMessageW (находка ревью 2026-10-17:
    утечка в 27 из 30 циклов start/stop). Фикс — самопроверка _stop сразу
    после SetWinEventHook, до входа в насос.
    """
    for _ in range(10):
        g = wp.WindowGuard(poll_s=0.01)
        g.start()
        g.stop()
        g._hook_thread.join(timeout=5)
        g._thread.join(timeout=5)
        assert not g._hook_thread.is_alive(), \
            "поток хука пережил stop() — гонка публикации _hook_tid вернулась"


@pytest.mark.skipif(wp.os.name != "nt", reason="поток хука: нужен Windows")
def test_event_hook_restart_after_stop():
    """stop() -> start() spawns a fresh pair of guard threads (restartable).

    Audit regression 2026-10-19: the second start() early-returned on the
    stale _thread (never reset, _stop never re-cleared), so the guard silently
    stayed down after any stop/start cycle.
    """
    g = wp.WindowGuard(poll_s=0.01)
    g.start()
    first = g._thread
    g.stop()
    assert not first.is_alive(), "первый поток обязан завершиться по stop()"
    g.start()
    try:
        assert g._thread is not None and g._thread.is_alive()
        assert g._thread is not first
        assert g._hook_thread is not None and g._hook_thread.is_alive()
    finally:
        g.stop()


@pytest.mark.skipif(wp.os.name != "nt", reason="kernel32: нужен Windows")
def test_process_image_basename_reads_real_process():
    """Живой тест: имя образа процесса читается через QueryFullProcessImageNameW.

    Ошибка в ctypes-сигнатуре видна только на живом вызове: фильтр по имени в
    _chrome_pids_for_port вернул бы пусто и свернул бы не то окно.
    """
    name = wp.process_image_basename(wp.os.getpid())
    assert name, "имя образа текущего процесса не прочиталось"
    assert name.lower().startswith("python"), f"неожиданное имя: {name}"


def test_focus_return_works_with_two_watched_browsers(monkeypatch):
    """Оба всплывших окна прячутся, фокус возвращается пользователю.

    Регрессия ревью 2026-10-06 (переписана под модель «флаг вместо удержаний»,
    2026-10-16): удержание/показ одного окна не должно ломать возврат фокуса
    для остальных браузеров процесса (их 9-17).
    """
    fake = FakeWin32(windows=[_win(21, 500, iconic=False),
                              _win(22, 600, iconic=False)],
                     fg=_win(21, 500)).install(monkeypatch)
    g = wp.WindowGuard()
    g.watch(500, "9222", autostart=False)
    g.watch(600, "9223", autostart=False)
    g._last_fg = 99
    out = g.tick()
    assert fake.minimized == [21, 22]
    assert out["focus_restored"] == 1
    assert fake.focus_calls == [99]


def test_flag_file_roundtrip(monkeypatch, tmp_path):
    """user_visible() читает флаг, clear_user_visible() снимает (идемпотентно).

    Флаг — единственный источник истины «пользователь смотрит окна»; залипший
    файл от жёстко убитого трея снимает clear_user_visible() при старте сбора.
    """
    flag = tmp_path / "window_visible.flag"
    monkeypatch.setattr(wp, "USER_VISIBLE_FLAG", flag)
    assert wp.user_visible() is False
    flag.write_text("1", encoding="utf-8")
    assert wp.user_visible() is True
    wp.clear_user_visible()
    assert wp.user_visible() is False
    wp.clear_user_visible()      # повторный вызов — не исключение


def test_hidden_flag_overrides_keep_window_env(monkeypatch, tmp_path):
    """Explicit tray "0" beats OZON_KEEP_WINDOW after start (owner 2026-10-19).

    The console/env sets the initial mode; the tray is authoritative after
    that. Without this the env=1 passed to workers would resurrect windows the
    owner hid in the tray.
    """
    flag = tmp_path / "window_visible.flag"
    monkeypatch.setattr(wp, "USER_VISIBLE_FLAG", flag)
    monkeypatch.setenv("OZON_KEEP_WINDOW", "1")
    flag.write_text("0", encoding="utf-8")
    assert wp.user_visible() is False
    assert wp.user_hidden() is True
    assert wp._keep_window_env() is False
    # PS 5.1 Set-Content -Encoding UTF8 writes a BOM: it must not turn the
    # explicit "1" into "no opinion".
    flag.write_text("\ufeff1", encoding="utf-8")
    assert wp.user_hidden() is False
    assert wp.user_visible() is True
    assert wp._keep_window_env() is True


def test_empty_visible_flag_means_no_opinion(monkeypatch, tmp_path):
    """An interrupted flag write must not mean visible (review 2026-10-19)."""
    flag = tmp_path / "window_visible.flag"
    flag.write_text("", encoding="utf-8")
    monkeypatch.setattr(wp, "USER_VISIBLE_FLAG", flag)
    monkeypatch.setenv("OZON_KEEP_WINDOW", "1")
    assert wp.user_visible() is False
    assert wp.user_hidden() is False
    assert wp._keep_window_env() is True


@pytest.mark.skipif(wp.os.name != "nt", reason="hook: нужен Windows")
def test_hook_is_replaced_after_quit_was_posted():
    """A stuck hook must not survive into a restart (NIT review 2026-10-19).

    stop() posts WM_QUIT; if the thread outlives the join, is_alive() stays
    True and the old start() would keep it — the guard would then run without
    instant window hiding. start() must replace it.
    """
    g = wp.WindowGuard(poll_s=0.01)
    blocker = threading.Event()

    def stuck():
        blocker.wait(10)

    t = threading.Thread(target=stuck, daemon=True)
    t.start()
    g._hook_thread = t
    g._hook_quit_sent = True
    g.start()
    try:
        assert g._hook_thread is not t, \
            "хук с отправленным WM_QUIT не пересоздан"
        assert g._hook_quit_sent is False
    finally:
        blocker.set()
        g.stop()
        t.join(timeout=5)


def test_user_visible_flag_lives_in_data_dir():
    """Флаг — общий контракт трей↔Python: data/window_visible.flag.

    Трей (PowerShell) и сторож (Python) обязаны указывать на один файл; смена
    пути здесь = сломанная синхронизация, поэтому путь фиксируется тестом.
    """
    assert wp.USER_VISIBLE_FLAG.name == "window_visible.flag"
    assert wp.USER_VISIBLE_FLAG.parent.name == "data"


@pytest.mark.skipif(wp.os.name != "nt", reason="поток: нужен Windows")
def test_guard_thread_starts_once_and_stops():
    """Поток сторожа поднимается один раз и останавливается (висящий поток
    в проекте — уже заведённый инцидент, см. AGENTS.md про awk.exe)."""
    g = wp.WindowGuard(poll_s=0.01)
    g.watch(4242, "9222")
    assert g.alive
    thread = g._thread
    g.start()
    assert g._thread is thread, "повторный start не должен плодить поток"
    g.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not g.alive


def _make_probe_hwnd():
    """Реальное top-level окно (без показа) — hwnd для живых Win32-тестов.

    Заглушка user32 здесь не нужна: проводку DeleteTab проверяем на настоящем
    окне, иначе тест проверял бы пустоту.
    """
    import ctypes
    user32 = ctypes.windll.user32
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.CreateWindowExW.argtypes = [
        ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    return user32.CreateWindowExW(0, "STATIC", "taskbar-delete-probe",
                                  0x80000000, 0, 0, 40, 40,
                                  None, None, None, None)


def _destroy_probe_hwnd(hwnd):
    import ctypes
    ctypes.windll.user32.DestroyWindow(ctypes.c_void_p(hwnd))


@pytest.mark.skipif(wp.os.name != "nt", reason="Windows-only (COM ITaskbarList)")
def test_delete_taskbar_tab_real_com_pipeline():
    """DeleteTab на реальном hwnd: ctypes-COM обязан вернуть S_OK.

    Регрессия 2026-10-16: кнопка панели задач не снимается сменой
    WS_EX_TOOLWINDOW — лечится только ITaskbarList::DeleteTab."""
    hwnd = _make_probe_hwnd()
    assert hwnd, "не удалось создать тестовое окно"
    try:
        assert wp.delete_taskbar_tab(int(hwnd)) is True, \
            "ITaskbarList::DeleteTab не отработал (COM/vtable сломан)"
    finally:
        _destroy_probe_hwnd(hwnd)


@pytest.mark.skipif(wp.os.name != "nt", reason="Windows-only")
def test_minimize_hwnd_wires_taskbar_delete(monkeypatch):
    """Сворачивание окна обязано снять кнопку панели задач (проводка)."""
    recorded = []
    monkeypatch.setattr(wp, "delete_taskbar_tab", recorded.append)
    hwnd = _make_probe_hwnd()
    assert hwnd
    try:
        assert wp.minimize_hwnd(int(hwnd)) is True
        assert recorded == [int(hwnd)], "кнопка не снята при minimize_hwnd"
    finally:
        _destroy_probe_hwnd(hwnd)


def test_minimize_hwnd_uses_sw_hide_not_sw_minimize(monkeypatch):
    """Прячем окно через SW_HIDE (0), а не SW_MINIMIZE (6).

    Живой замер 2026-10-16: свёрнутое TOOLWINDOW-окно Windows рисует над
    панелью задач «мини-плитку-призрак» с крестиком и не отдаёт кадры;
    скрытое окно кадры отдаёт.
    """
    calls = []

    class _FakeUser32:
        def GetWindowLongW(self, hwnd, idx):
            return 0

        def SetWindowLongW(self, hwnd, idx, val):
            return 0

        def ShowWindow(self, hwnd, cmd):
            calls.append(cmd)
            return True

    monkeypatch.setattr(wp, "_user32", lambda: _FakeUser32())
    monkeypatch.setattr(wp, "_setup_api", lambda user32: None)
    monkeypatch.setattr(wp, "delete_taskbar_tab", lambda hwnd: True)
    assert wp.minimize_hwnd(0x1234) is True
    assert calls == [0], \
        f"minimize_hwnd послал ShowWindow(cmd={calls}), нужен только SW_HIDE=0"


def test_show_hwnd_restores_with_show_window_flag(monkeypatch):
    """Show path: SW_RESTORE (9) + SWP_SHOWWINDOW (0x0040), no activation.

    A window hidden with SW_HIDE at start is shown only by SW_RESTORE +
    SWP_SHOWWINDOW; NOACTIVATE keeps the user focus; SetWindowPos targets
    TOPMOST (-1); the taskbar button is removed explicitly
    (audit 2026-10-18, D2).
    """
    show_calls = []
    pos_calls = []

    class _FakeUser32:
        def GetWindowLongW(self, hwnd, idx):
            return 0

        def SetWindowLongW(self, hwnd, idx, val):
            return 0

        def ShowWindow(self, hwnd, cmd):
            show_calls.append(cmd)
            return True

        def SetWindowPos(self, hwnd, insert_after, x, y, cx, cy, flags):
            pos_calls.append((insert_after, flags))
            return True

    deleted = []
    monkeypatch.setattr(wp, "_user32", lambda: _FakeUser32())
    monkeypatch.setattr(wp, "_setup_api", lambda user32: None)
    monkeypatch.setattr(wp, "delete_taskbar_tab", deleted.append)
    assert wp.show_hwnd(0x1234) is True
    assert show_calls == [9], \
        f"show_hwnd послал ShowWindow(cmd={show_calls}), нужен SW_RESTORE=9"
    assert pos_calls == [(-1, 0x0002 | 0x0001 | 0x0010 | 0x0040)], \
        "SetWindowPos: TOPMOST + NOMOVE|NOSIZE|NOACTIVATE|SHOWWINDOW"
    assert deleted == [0x1234], "кнопка панели задач не снята при показе"


class _ParkUser32:
    """Minimal user32 fake: only the calls park/bring need."""

    def __init__(self):
        self.events = []

    def GetWindowLongW(self, hwnd, idx):
        return 0

    def SetWindowLongW(self, hwnd, idx, val):
        return 0

    def ShowWindow(self, hwnd, cmd):
        self.events.append(("show", cmd))
        return True

    def SetWindowPos(self, hwnd, insert_after, x, y, cx, cy, flags):
        self.events.append(("pos", x, y, flags))
        return True


def test_minimize_hwnd_parks_window_offscreen(monkeypatch):
    """SW_HIDE parks the window beyond the screen edge: Chrome's show is invisible.

    Chrome shows its window by itself (start/new tab/puzzle) and suppression
    arrives milliseconds late — an empty grey rectangle flashes (live
    measurement 2026-10-18: show->hide 30 ms). Beyond the virtual desktop edge
    it is invisible; the CDP model follows the position and screenshots keep
    working (live stand). Parking goes AFTER the hide.
    """
    fake = _ParkUser32()
    monkeypatch.setattr(wp, "_user32", lambda: fake)
    monkeypatch.setattr(wp, "_setup_api", lambda user32: None)
    monkeypatch.setattr(wp, "delete_taskbar_tab", lambda hwnd: True)
    monkeypatch.setattr(wp, "_virtual_screen", lambda user32: (0, 0, 1920, 1080))
    monkeypatch.setattr(wp, "_window_rect", lambda user32, hwnd:
                        (431, 62, 1440, 900))
    assert wp.minimize_hwnd(0x1234) is True
    assert ("show", 0) in fake.events, "окно не скрыто (SW_HIDE)"
    pos = [e for e in fake.events if e[0] == "pos"]
    assert pos == [("pos", -wp.PARK_MARGIN - 1440, wp.ONSCREEN_MARGIN,
                    wp._SWP_NOSIZE | wp._SWP_NOZORDER | wp._SWP_NOACTIVATE)], \
        f"окно не запарковано за краем экрана: {pos}"
    assert fake.events.index(("show", 0)) < fake.events.index(pos[0]), \
        "парковка обязана идти ПОСЛЕ скрытия (иначе окно мелькнёт при переносе)"


def test_minimize_hwnd_skips_park_when_already_offscreen(monkeypatch):
    """An already parked window is not moved again (no flash, no extra calls)."""
    fake = _ParkUser32()
    monkeypatch.setattr(wp, "_user32", lambda: fake)
    monkeypatch.setattr(wp, "_setup_api", lambda user32: None)
    monkeypatch.setattr(wp, "delete_taskbar_tab", lambda hwnd: True)
    monkeypatch.setattr(wp, "_virtual_screen", lambda user32: (0, 0, 1920, 1080))
    monkeypatch.setattr(wp, "_window_rect", lambda user32, hwnd:
                        (-wp.PARK_MARGIN - 1440, wp.ONSCREEN_MARGIN, 1440, 900))
    assert wp.minimize_hwnd(0x1234) is True
    assert [e for e in fake.events if e[0] == "pos"] == []


def test_show_hwnd_brings_parked_window_onscreen(monkeypatch):
    """Showing a parked window must first bring it back to the desktop.

    Otherwise the tray ("Развернуть окна сбора") and keep_window would show a
    window beyond the screen edge — the user would see nothing.
    """
    fake = _ParkUser32()
    monkeypatch.setattr(wp, "_user32", lambda: fake)
    monkeypatch.setattr(wp, "_setup_api", lambda user32: None)
    monkeypatch.setattr(wp, "delete_taskbar_tab", lambda hwnd: True)
    monkeypatch.setattr(wp, "_virtual_screen", lambda user32: (0, 0, 1920, 1080))
    monkeypatch.setattr(wp, "_window_rect", lambda user32, hwnd:
                        (-wp.PARK_MARGIN - 1440, wp.ONSCREEN_MARGIN, 1440, 900))
    assert wp.show_hwnd(0x1234) is True
    pos = [e for e in fake.events if e[0] == "pos"]
    assert pos == [
        ("pos", wp.ONSCREEN_MARGIN, wp.ONSCREEN_MARGIN,
         wp._SWP_NOSIZE | wp._SWP_NOZORDER | wp._SWP_NOACTIVATE),
        ("pos", 0, 0, 0x0002 | 0x0001 | 0x0010 | 0x0040),
    ], f"показ: сначала на экран, затем TOPMOST без сдвига; получено {pos}"
    assert fake.events.index(pos[0]) < fake.events.index(("show", 9)), \
        "вернуть на экран нужно ДО SW_RESTORE"


def test_cdp_minimize_native_delegates_to_canon():
    """The CDP hide path must call the window_policy.minimize_hwnd canon.

    The single SW_HIDE implementation lives in window_policy (its own test:
    test_minimize_hwnd_uses_sw_hide_not_sw_minimize). Duplicating it in cdp
    already caused two SW_MINIMIZE regressions: a minimized TOOLWINDOW window
    is drawn by Windows as a ghost tile and stops delivering frames (live
    measurement 2026-10-16) — now there is a single implementation
    (audit 2026-10-18, D2).
    """
    import inspect

    from ozon_collector import cdp as C

    src = inspect.getsource(C.Session._minimize_window_native)
    assert "minimize_hwnd" in src, "cdp не зовёт канон скрытия"
    assert "user32.ShowWindow" not in src, \
        "реализация скрытия вернулась в cdp — канон должен быть один"
    assert "SW_MINIMIZE = 6" not in src, "вернулась константа свёртывания"


def test_cdp_restore_native_delegates_to_canon():
    """The CDP show path must call the window_policy.show_hwnd canon.

    The single SW_RESTORE + SetWindowPos implementation lives in
    window_policy (audit 2026-10-18, D2).
    """
    import inspect

    from ozon_collector import cdp as C

    src = inspect.getsource(C.Session._restore_window_native)
    assert "show_hwnd" in src, "cdp не зовёт канон показа"
    assert "user32.SetWindowPos" not in src \
        and "user32.ShowWindow" not in src, \
        "реализация показа вернулась в cdp — канон должен быть один"


@pytest.mark.skipif(wp.os.name != "nt", reason="Windows-only")
def test_cdp_minimize_native_wires_taskbar_delete(monkeypatch):
    """CDP-путь свёртывания шлёт DeleteTab для найденного hwnd."""
    from ozon_collector import cdp as C
    recorded = []
    monkeypatch.setattr(C, "_window_for_pid", lambda *a, **kw: 0x1234)
    monkeypatch.setattr(wp, "delete_taskbar_tab", recorded.append)
    C.Session._minimize_window_native(pids=[4242])
    assert recorded == [0x1234], "CDP-свёртывание не сняло кнопку"


@pytest.mark.skipif(wp.os.name != "nt", reason="Windows-only")
def test_cdp_restore_native_wires_taskbar_delete(monkeypatch):
    """CDP-путь показа (reconnect/restore/OZON_KEEP_WINDOW) снимает кнопку."""
    from ozon_collector import cdp as C
    recorded = []
    monkeypatch.setattr(C, "_window_for_pid", lambda *a, **kw: 0x1234)
    monkeypatch.setattr(wp, "delete_taskbar_tab", recorded.append)
    C.Session._restore_window_native(pids=[4242])
    assert recorded == [0x1234], "restore не снял кнопку панели задач"


# ---- сентинел показов: в скрытом режиме окна сбора не появляются вообще ----
# (требование владельца 2026-10-17)

class TestSentinel:
    @pytest.fixture(autouse=True)
    def _guard_on(self, monkeypatch):
        # conftest глушит сторожа (setdefault OZON_WINDOW_GUARD=0): для тестов
        # сентинела включаем явно, «выключенные» тесты ставят 0 сами.
        monkeypatch.setenv("OZON_WINDOW_GUARD", "1")
        monkeypatch.delenv("OZON_KEEP_WINDOW", raising=False)

    def _rows(self):
        return [
            _win(101, 500, visible=True),                      # наше видимое главное
            _win(102, 501, visible=True, width=420, height=220),  # наш попап
            _win(103, 502, visible=False),                     # наше скрытое
            _win(104, 999, visible=True),                      # чужой Chrome
        ]

    _OURS = {500: "ozon-profile", 501: "ozon-profile", 502: "ozon-profile"}

    @staticmethod
    def _record(hidden):
        def _hide(hwnd):
            hidden.append(hwnd)
            return True
        return _hide

    def test_tick_hides_only_own_visible_main(self):
        hidden = []
        res = wp.sentinel_tick(hide=self._record(hidden), rows=self._rows(),
                               pids=dict(self._OURS), uv=lambda: False)
        assert hidden == [101], \
            f"спрятать обязан только 101 (наше видимое главное), а не {hidden}"
        assert res["hidden"] == 1 and res["visible"] == 1

    def test_tick_respects_user_visible(self):
        hidden = []
        res = wp.sentinel_tick(hide=self._record(hidden), rows=self._rows(),
                               pids=dict(self._OURS), uv=lambda: True)
        assert hidden == [] and res["skipped"] == "user_visible"

    def test_tick_off_when_guard_disabled(self, monkeypatch):
        monkeypatch.setenv("OZON_WINDOW_GUARD", "0")
        hidden = []
        res = wp.sentinel_tick(hide=self._record(hidden), rows=self._rows(),
                               pids=dict(self._OURS), uv=lambda: False)
        assert hidden == [] and res["skipped"] == "guard_off"

    def test_tick_off_with_keep_window(self, monkeypatch):
        monkeypatch.setenv("OZON_KEEP_WINDOW", "1")
        hidden = []
        res = wp.sentinel_tick(hide=self._record(hidden), rows=self._rows(),
                               pids=dict(self._OURS), uv=lambda: False)
        assert hidden == [] and res["skipped"] == "keep_window", \
            "visible-режим (env) обязан отличаться от выключенного гейта"

    def test_sentinel_start_logs_reason(self, monkeypatch, caplog):
        """False у sentinel_start обязан быть с причиной в логе.

        До 2026-10-18 выход был молчаливым, а стартовая строка терялась в
        слепой зоне логов демона (attach_file вызывался позже, в
        run_campaign) — в logs/*_daemon.log ноль упоминаний сентинела при
        любом исходе (замер 18.10).
        """
        import logging
        monkeypatch.setenv("OZON_WINDOW_GUARD", "0")
        monkeypatch.delenv("OZON_KEEP_WINDOW", raising=False)
        with caplog.at_level(logging.WARNING,
                             logger="ozon_collector.window_policy"):
            assert wp.sentinel_start() is False
        assert any("НЕ запущен" in r.getMessage() for r in caplog.records), \
            "выключенный сторож обязан объяснить отказ в логе"
        monkeypatch.setenv("OZON_WINDOW_GUARD", "1")
        monkeypatch.setenv("OZON_KEEP_WINDOW", "1")
        monkeypatch.setattr(wp, "sentinel_tick", lambda **kw: {"stub": True})
        caplog.clear()
        with caplog.at_level(logging.INFO,
                             logger="ozon_collector.window_policy"):
            try:
                assert wp.sentinel_start() is True, \
                    ("стартовый visible не отменяет старт сторожа: явный "
                     "hidden из трея обязан сработать даже без трея")
            finally:
                wp.sentinel_stop()
        assert any("visible" in r.getMessage() for r in caplog.records), \
            "visible-режим обязан логировать, что сторож ждёт явного hidden"

    def test_tick_rechecks_user_visible_before_hide(self):
        """A flip to visible during the scan cancels the hide (review 2026-10-19)."""
        state = {"n": 0}

        def uv():
            state["n"] += 1
            return state["n"] > 1

        hidden = []
        res = wp.sentinel_tick(hide=self._record(hidden), rows=self._rows(),
                               pids=dict(self._OURS), uv=uv)
        assert hidden == [], "тик спрятал окно после переключения в visible"
        assert res["skipped"] == "user_visible", res

    def test_on_show_rechecks_user_visible_before_hide(self, monkeypatch):
        """The hook re-checks visible after its profile scan (review 2026-10-19)."""
        monkeypatch.setattr(wp, "_hwnd_pid", lambda hwnd: 500)
        state = {"n": 0}

        def uv():
            state["n"] += 1
            return state["n"] > 1

        hidden = []
        ok = wp.sentinel_on_show(0x4040, hide=self._record(hidden),
                                 pids=dict(self._OURS), uv=uv)
        assert ok is False and hidden == [], \
            "хук спрятал окно после переключения в visible"

    def test_on_show_hides_our_window(self, monkeypatch):
        hidden = []
        monkeypatch.setattr(wp, "_hwnd_pid", lambda hwnd: 500)
        ok = wp.sentinel_on_show(0x1010, hide=self._record(hidden),
                                 pids=dict(self._OURS), uv=lambda: False)
        assert ok is True and hidden == [0x1010]

    def test_on_show_ignores_foreign_window(self, monkeypatch):
        hidden = []
        monkeypatch.setattr(wp, "_hwnd_pid", lambda hwnd: 999)
        ok = wp.sentinel_on_show(0x2020, hide=self._record(hidden),
                                 pids=dict(self._OURS), uv=lambda: False)
        assert ok is False and hidden == [], "чужое окно трогать нельзя"

    def test_on_show_skips_in_user_visible(self, monkeypatch):
        hidden = []
        monkeypatch.setattr(wp, "_hwnd_pid", lambda hwnd: 500)
        ok = wp.sentinel_on_show(0x3030, hide=self._record(hidden),
                                 pids=dict(self._OURS), uv=lambda: True)
        assert ok is False and hidden == [], \
            "в режиме visible пользователь смотрит окна — не прячем"

    @pytest.mark.skipif(wp.os.name != "nt", reason="Windows-only")
    def test_start_is_idempotent_and_stops(self, monkeypatch):
        monkeypatch.setattr(wp, "sentinel_tick", lambda **kw: {"stub": True})
        try:
            assert wp.sentinel_start() is True
            assert wp.sentinel_start() is True, "повторный старт идемпотентен"
        finally:
            wp.sentinel_stop()
        assert wp._SENTINEL is None

    def test_start_off_when_guard_disabled(self, monkeypatch):
        monkeypatch.setenv("OZON_WINDOW_GUARD", "0")
        assert wp.sentinel_start() is False

    # ---- шторм-защита скана профилей (находка ревью 2026-10-17) ----------

    @staticmethod
    def _fresh_state():
        return {"stop": None, "pids": {}, "pids_ts": 0.0, "scan_ts": 0.0,
                "scan_start": 0.0, "scan_raw": 0, "alien": {},
                "alien_lock": threading.Lock(), "lock": threading.Lock()}

    def test_empty_pids_cache_lives_ttl(self, monkeypatch):
        """Пустой результат скана — тоже кэш: не скан на каждый вызов."""
        calls = {"n": 0}

        def _scan():
            calls["n"] += 1
            return [], 1

        monkeypatch.setattr(wp, "_our_profile_chromium_stat", _scan)
        monkeypatch.setattr(wp, "_SENTINEL", self._fresh_state())
        assert wp._sentinel_pids() == {}
        assert wp._sentinel_pids() == {}
        assert calls["n"] == 1, \
            "пустой кэш обязан жить TTL, а не сканировать каждый вызов"

    def test_refresh_throttled(self, monkeypatch):
        """refresh=True не чаще min-интервала: show-события не штормят скан."""
        calls = {"n": 0}

        def _scan():
            calls["n"] += 1
            return [], 1

        monkeypatch.setattr(wp, "_our_profile_chromium_stat", _scan)
        monkeypatch.setattr(wp, "_SENTINEL", self._fresh_state())
        assert wp._sentinel_pids(refresh=True) == {}
        assert wp._sentinel_pids(refresh=True) == {}
        assert calls["n"] == 1, "два refresh подряд — один скан"

    def test_on_show_prefilter_no_scan_for_foreign_app(self, monkeypatch):
        """Не-chrome окно чужого приложения до скана профилей не доходит."""
        scans = {"n": 0}
        monkeypatch.setattr(wp, "_hwnd_pid", lambda hwnd: 999)
        monkeypatch.setattr(wp, "process_image_basename",
                            lambda pid: "explorer.exe")
        monkeypatch.setattr(
            wp, "_sentinel_pids",
            lambda refresh=False: scans.__setitem__("n", scans["n"] + 1) or {})
        ok = wp.sentinel_on_show(0x4040, hide=lambda h: True,
                                 uv=lambda: False)
        assert ok is False and scans["n"] == 0

    def test_on_show_alien_chrome_cached(self, monkeypatch):
        """Чужой chrome.exe кэшируется (alien): повторный показ без скана.

        Здесь работает НАСТОЯЩИЙ _sentinel_pids (со сканом-заглушкой): метка
        чужака ставится только по скану, начавшемуся после показа события.
        """
        scans = {"n": 0}

        def _scan():
            scans["n"] += 1
            return [], 1

        monkeypatch.setattr(wp, "_our_profile_chromium_stat", _scan)
        monkeypatch.setattr(wp, "_hwnd_pid", lambda hwnd: 777)
        monkeypatch.setattr(wp, "process_image_basename",
                            lambda pid: "chrome.exe")
        monkeypatch.setattr(wp, "_SENTINEL", self._fresh_state())
        assert wp.sentinel_on_show(0x5050, hide=lambda h: True,
                                   uv=lambda: False) is False
        n_after_first = scans["n"]
        assert n_after_first >= 1, "первый показ обязан дойти до скана"
        assert 777 in wp._SENTINEL["alien"], \
            "скан начался после события — метке чужака доверяем"
        # Кэш «протух»: без alien-пути второй показ обязан был бы сканировать
        # (иначе assertion ниже зелёный и без проверяемого кода).
        wp._SENTINEL.update(pids_ts=0.0, scan_ts=0.0, scan_start=0.0)
        assert wp.sentinel_on_show(0x5051, hide=lambda h: True,
                                   uv=lambda: False) is False
        assert scans["n"] == n_after_first, \
            "чужой chrome.exe кэшируется (alien): повторный показ без скана"

    def test_on_show_throttled_refresh_does_not_poison_alien(
            self, monkeypatch):
        """Троттлинг refresh не метит НАШ свежий pid чужим по старому скану.

        Регресс ревью 2026-10-17: скан был ДО спавна браузера, refresh пойман
        троттлингом (или уже летит в другом потоке) — кэш отдаёт данные без
        нашего pid, и метка «чужой» слепила бы хук на 60 с.
        """
        scans = {"n": 0}

        def _scan():
            scans["n"] += 1
            return [], 1

        monkeypatch.setattr(wp, "_our_profile_chromium_stat", _scan)
        monkeypatch.setattr(wp, "_hwnd_pid", lambda hwnd: 777)
        monkeypatch.setattr(wp, "process_image_basename",
                            lambda pid: "chrome.exe")
        st = self._fresh_state()
        old = time.monotonic() - 0.3          # скан 0.3 с назад — ДО показа
        st.update(pids_ts=old, scan_ts=old, scan_start=old)
        monkeypatch.setattr(wp, "_SENTINEL", st)
        assert wp.sentinel_on_show(0x6060, hide=lambda h: True,
                                   uv=lambda: False) is False
        assert scans["n"] == 0, \
            "fast path и refresh — из кэша (TTL + троттлинг), без скана"
        assert 777 not in (st.get("alien") or {}), \
            "скан, начавшийся ДО показа, — не доказательство чужеродности"

    def test_tick_foreign_chrome_cached_no_repeat_scan(self, monkeypatch):
        """Тик: чужой chrome.exe уходит в alien — повторный скан не нужен.

        Регресс ревью 2026-10-17: тик сканировал профили каждые ~2 с, пока
        на экране висело ЛЮБОЕ чужое окно chrome.exe (личный Chrome).
        """
        scans = {"n": 0}

        def _scan():
            scans["n"] += 1
            return [], 1

        monkeypatch.setattr(wp, "_our_profile_chromium_stat", _scan)
        monkeypatch.setattr(wp, "process_image_basename",
                            lambda pid: "chrome.exe")
        monkeypatch.setattr(wp, "_SENTINEL", self._fresh_state())
        rows = [_win(104, 999, visible=True)]          # чужой Chrome
        hide = self._record([])
        res = wp.sentinel_tick(hide=hide, rows=rows, uv=lambda: False)
        assert res["hidden"] == 0 and res["visible"] == 0
        assert scans["n"] == 1 and 999 in wp._SENTINEL["alien"]
        # Кэш «протух»: без alien-пути второй тик обязан был бы сканировать
        wp._SENTINEL.update(pids_ts=0.0, scan_ts=0.0, scan_start=0.0)
        res = wp.sentinel_tick(hide=hide, rows=rows, uv=lambda: False)
        assert res["hidden"] == 0
        assert scans["n"] == 1, \
            "чужой chrome.exe из alien-кэша — без повторного скана"

    def test_tick_stale_cache_does_not_poison_our_pid(self, monkeypatch):
        """Тик не метит свежий НАШ pid чужим по кэшу, снятому до его спавна.

        Регресс ревью 2026-10-17 (раунд 2): `ours` из ещё свежего TTL-кэша
        не содержит только что заспавненный браузер, а тик метил его alien
        на 60 с — оба слоя сентинела слепли, окно оставалось видимым.
        """
        scans = {"n": 0}

        def _scan():
            scans["n"] += 1
            return [], 1

        monkeypatch.setattr(wp, "_our_profile_chromium_stat", _scan)
        monkeypatch.setattr(wp, "process_image_basename",
                            lambda pid: "chrome.exe")
        st = self._fresh_state()
        old = time.monotonic() - 0.3     # кэш свеж по TTL, снят ДО спавна
        st.update(pids_ts=old, scan_ts=old, scan_start=old)
        monkeypatch.setattr(wp, "_SENTINEL", st)
        res = wp.sentinel_tick(hide=self._record([]),
                               rows=[_win(101, 500, visible=True)],
                               uv=lambda: False)
        assert scans["n"] == 0, "TTL-кэш свежий — скана нет"
        assert 500 not in (st.get("alien") or {}), \
            "кэш старше наблюдения окна не доказывает чужеродность"

    def test_tick_prefilter_no_scan_for_foreign_app(self, monkeypatch):
        """Тик: не-chrome окно чужого приложения до скана профилей не доходит."""
        scans = {"n": 0}

        def _scan():
            scans["n"] += 1
            return [], 1

        monkeypatch.setattr(wp, "_our_profile_chromium_stat", _scan)
        monkeypatch.setattr(wp, "process_image_basename",
                            lambda pid: "explorer.exe")
        monkeypatch.setattr(wp, "_SENTINEL", self._fresh_state())
        res = wp.sentinel_tick(hide=self._record([]),
                               rows=[_win(105, 998, visible=True)],
                               uv=lambda: False)
        assert res["hidden"] == 0 and scans["n"] == 0

    def test_failed_scan_does_not_look_fresh(self, monkeypatch):
        """Упавший скан: бампается только scan_ts, pids_ts не врёт.

        Регресс ревью 2026-10-17 (раунд 2): исключение в скане оставляло
        «свежий успешный» пустой кэш — trusted-якорь пропускал метку alien
        для нашего pid по данным, которых не существует.
        """
        def _scan():
            raise RuntimeError("powershell битый")

        monkeypatch.setattr(wp, "_our_profile_chromium_stat", _scan)
        monkeypatch.setattr(wp, "process_image_basename",
                            lambda pid: "chrome.exe")
        st = self._fresh_state()
        monkeypatch.setattr(wp, "_SENTINEL", st)
        monkeypatch.setattr(wp, "_hwnd_pid", lambda hwnd: 777)
        assert wp.sentinel_on_show(0x7070, hide=lambda h: True,
                                   uv=lambda: False) is False
        assert st["scan_ts"] > 0.0, "попытка скана отмечена (троттлинг)"
        assert st.get("pids_ts", 0.0) == 0.0, \
            "упавший скан не должен выглядеть свежим успешным"
        assert 777 not in (st.get("alien") or {}), \
            "по несуществующим данным метку чужака ставить нельзя"

    def test_silent_empty_scan_does_not_poison_alien(self, monkeypatch):
        """Тихий пустой скан PS (raw=0) — не «состоявшийся»: метки чужака нет.

        `process._win_iter_chromium_processes` глотает сбой PowerShell молча;
        пустой результат проходил якорь pids_ts, и наш свежий pid уходил в
        alien на 60 с (MAJOR-1 review-skeptic 2026-10-17). Пустой список при
        видимом chrome.exe физически невозможен — доверия такому скану нет.
        """
        scans = {"n": 0}

        def _scan():
            scans["n"] += 1
            return [], 0

        monkeypatch.setattr(wp, "_our_profile_chromium_stat", _scan)
        monkeypatch.setattr(wp, "process_image_basename",
                            lambda pid: "chrome.exe")
        st = self._fresh_state()
        monkeypatch.setattr(wp, "_SENTINEL", st)
        monkeypatch.setattr(wp, "_hwnd_pid", lambda hwnd: 777)
        assert wp.sentinel_on_show(0x8080, hide=lambda h: True,
                                   uv=lambda: False) is False
        assert scans["n"] == 1, \
            "refresh после пустого скана — из кэша (троттлинг), не второй PS"
        assert st["scan_ts"] > 0.0, "попытка скана отмечена (троттлинг)"
        assert st.get("pids_ts", 0.0) == 0.0, \
            "пустой скан при видимом chrome.exe — не состоявшийся"
        assert 777 not in (st.get("alien") or {}), \
            "по тихо пустому скану метку чужака ставить нельзя"


# ---- свип окон-сирот: kill-путь (находка ревью 2026-10-17: был без тестов) --

class TestSweepStrayBrowsers:
    def _install(self, monkeypatch, ours, *, responds=False):
        """Подмена наружного мира свипа; возвращает счётчик вызовов kill."""
        import types

        from ozon_collector import process as P
        calls = {"records": None}
        monkeypatch.setattr(wp, "user_visible", lambda: False)
        monkeypatch.setattr(wp, "enabled_by_env", lambda: True)
        monkeypatch.setattr(wp, "list_chrome_windows", lambda: [])
        monkeypatch.setattr(wp, "_our_profile_chromium", lambda: list(ours))
        monkeypatch.setattr(wp, "_stray_first_seen", {})
        monkeypatch.setattr(P, "cdp_responds",
                            lambda url, timeout_s=1.0: responds)
        monkeypatch.setattr(P, "process_identity",
                            lambda pid: types.SimpleNamespace(pid=pid))

        def _kill(records, wait_s=3.0):
            calls["records"] = list(records)
            return [types.SimpleNamespace(identity=r.identity)
                    for r in records]

        monkeypatch.setattr(P, "kill_records", _kill)
        return calls

    def test_kill_path_identity_and_grace(self, monkeypatch):
        """CDP молчит дольше grace → kill только по ProcessRecord-identity."""
        ours = [(111, "ozon-profile", 9444, True)]
        calls = self._install(monkeypatch, ours, responds=False)
        assert wp.sweep_stray_browsers(grace_s=45.0, now=100.0)["killed"] == 0, \
            "первый проход лишь фиксирует «CDP молчит» (grace не вышел)"
        res = wp.sweep_stray_browsers(grace_s=45.0, now=200.0)
        assert res["killed"] == 1 and res["killed_pids"] == [111]
        recs = calls["records"]
        assert [(r.launch_id, r.identity.pid) for r in recs] == [
            ("sweep:ozon-profile", 111)], \
            "kill_records обязан получить ProcessRecord с launch_id и identity"
        assert wp._stray_first_seen == {}, "порт убран из first_seen после kill"

    def test_cdp_alive_no_kill_and_clears_seen(self, monkeypatch):
        ours = [(111, "ozon-profile", 9444, True)]
        calls = self._install(monkeypatch, ours, responds=True)
        assert wp.sweep_stray_browsers(grace_s=0.0, now=100.0)["killed"] == 0
        assert calls["records"] is None
        assert wp._stray_first_seen == {}

    def test_user_visible_skips_everything(self, monkeypatch):
        ours = [(111, "ozon-profile", 9444, True)]
        calls = self._install(monkeypatch, ours, responds=False)
        monkeypatch.setattr(wp, "user_visible", lambda: True)
        res = wp.sweep_stray_browsers(now=200.0)
        assert res["skipped"] == "user_visible" and res["killed"] == 0
        assert calls["records"] is None

    def test_window_pass_hides_only_own_visible_main(self, monkeypatch):
        ours = [(500, "ozon-profile", 9444, True)]
        self._install(monkeypatch, ours, responds=True)
        rows = [_win(101, 500, visible=True),
                _win(102, 500, visible=True, width=420, height=220),
                _win(104, 999, visible=True)]
        hidden = []
        monkeypatch.setattr(wp, "list_chrome_windows", lambda: rows)
        monkeypatch.setattr(wp, "minimize_hwnd",
                            lambda hwnd: (hidden.append(hwnd), True)[1])
        res = wp.sweep_stray_browsers(now=100.0)
        assert hidden == [101] and res["hidden"] == 1 and res["visible"] == 1, \
            "попап своего Chrome и чужое окно не трогаются"


# ---- D4/D5 (audit 2026-10-18): process.pid_alive/env_bool delegation -------

def test_pid_alive_delegates_to_process(monkeypatch):
    """D4: the single liveness implementation is process.pid_alive (guard
    2**24, isinstance); window_policy no longer duplicates OpenProcess."""
    seen = []

    def fake_alive(pid):
        seen.append(pid)
        return True

    monkeypatch.setattr(wp._proc, "pid_alive", fake_alive)
    assert wp._pid_alive(1234) is True
    assert seen == [1234]

    monkeypatch.undo()
    assert wp._pid_alive(2 ** 24) is False  # guard from the shared impl


def test_keep_window_env_uses_shared_env_bool(monkeypatch, tmp_path):
    """D5: OZON_KEEP_WINDOW=true/yes/on is recognized the same way as in
    launch_chrome.ps1 and cdp.Session (shared process.env_bool)."""
    # Hermetic: a real data/window_visible.flag ("0") on the dev machine must
    # not leak into the env decision.
    monkeypatch.setattr(wp, "USER_VISIBLE_FLAG", tmp_path / "absent.flag")
    monkeypatch.setenv("OZON_KEEP_WINDOW", "true")
    assert wp._keep_window_env() is True
    monkeypatch.setenv("OZON_KEEP_WINDOW", "0")
    assert wp._keep_window_env() is False
    monkeypatch.delenv("OZON_KEEP_WINDOW", raising=False)
    assert wp._keep_window_env() is False
