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
    from types import SimpleNamespace
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


def test_identity_recheck_is_throttled(monkeypatch):
    """Полная сверка порта — раз в IDENTITY_RECHECK_S, не каждый тик.

    Она поднимает PowerShell, поэтому 150-мс тик не должен её дёргать.
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


def test_cdp_minimize_native_hides_not_minimizes():
    """CDP-путь скрывает окно (SW_HIDE=0), а не сворачивает (SW_MINIMIZE=6).

    Свёрнутое TOOLWINDOW-окно рисует «мини-плитку-призрак» над панелью задач
    и не отдаёт кадры — пазл на нём виснет (живой замер 2026-10-16).
    """
    import inspect

    from ozon_collector import cdp as C

    src = inspect.getsource(C.Session._minimize_window_native)
    assert "SW_HIDE = 0" in src
    assert "ShowWindow(hwnd, SW_HIDE)" in src
    assert "SW_MINIMIZE = 6" not in src, "вернулась константа свёртывания"
    assert "ShowWindow(hwnd, SW_MINIMIZE)" not in src, \
        "CDP-путь снова сворачивает окно вместо скрытия"


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
