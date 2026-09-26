"""Живой случай для рейпера: тест «забывает» дочерний процесс.

Служит доказательством, что `pytest_unconfigure` в conftest действительно
убивает хвосты, а не только пишет отчёт. По умолчанию пропускается, чтобы не
мусорить в обычных прогонах; включается явно:

    KD_REAP_DEMO=1 python -m pytest tests/test_reap_demo.py -s

Ожидаемый вывод: тест зелёный, в конце `[Reaper:session-end] killed pid=...`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

DEMO_ENV = "KD_REAP_DEMO"


@pytest.mark.skipif(
    not os.environ.get(DEMO_ENV),
    reason=f"демо утечки: включается {DEMO_ENV}=1",
)
def test_leaks_child_process():
    """Поднимает ДЛИТЕЛЬНО ЖИВУЩИЙ python и НЕ гасит его — эмуляция утечки."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"])
    print(f"\n[Demo] запущен процесс-утечка pid={child.pid} (уснёт через 600с)")
    time.sleep(0.3)
    assert child.poll() is None
