"""Защита тестов от молчаливого запуска настоящего браузера (2026-09-26).

Живой случай: `test_collect_search_challenge_event_has_telemetry` дошёл до
`_recycle_browser` → `BrowserProcessManager.restart()` → лаунчер поднял
реальный Chrome с профилем `ozon-profile` на 9222. Тест зелёный, окно осталось
на рабочем столе после pytest. Автофикстура `_forbid_real_browser_launch`
(conftest) превращает такой вызов в fail — здесь проверяем, что она работает.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_autouse_guard_blocks_launch_script():
    """Без маркера реальный вызов лаунчера обязан падать, а не стартовать."""
    import ozon_collector.browser as B

    with pytest.raises(pytest.fail.Exception) as ei:
        B._launch_script("http://127.0.0.1:9222", profile_path=r"C:\prof\ozon")
    assert "реальный запуск браузера" in str(ei.value)


def test_autouse_guard_blocks_cdp_launch():
    import ozon_collector.cdp as C

    with pytest.raises(pytest.fail.Exception):
        C._launch_browser("http://127.0.0.1:9222")


@pytest.mark.allow_browser_launch
def test_marker_opts_out_of_guard(monkeypatch, tmp_path):
    """С маркером лаунчер вызывается (Popen и скрипт здесь замоканы)."""
    import subprocess
    from unittest import mock

    import ozon_collector.browser as B

    script = tmp_path / "launch_chrome.ps1"
    script.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(B, "_launch_script_path", lambda: script)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: mock.Mock())
    B._launch_script("http://127.0.0.1:9277", profile_path=r"C:\prof\ozon")
