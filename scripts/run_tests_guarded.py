"""Запуск pytest под внешним watchdog с гарантированным возвратом управления.

Проблема: playwright/chromium в отдельных случаях виснут навсегда в naтивнои
блокировке (мертвыи коннект, teardown фикстуры). pytest-timeout покрывает только
фазу call и не прерывает нативное чтение драивера. Эта обёртка гарантирует: нет
превышен hard timeout —— дерево процессов (pytest + chromium + node) убивается,
а shell обязательно возвращается.

Использование:
    python scripts/run_tests_guarded.py tests
    python scripts/run_tests_guarded.py tests/test_reviews.py -- -k markup
    python scripts/run_tests_guarded.py --timeout 600 tests
    python scripts/run_tests_guarded.py --timeout 0 tests      # отключить watchdog

Коды возврата:
    0   — pytest завершился успешно
    N   — pytest завершился с кодом N (сохранён как есть)
    124 — сработал watchdog: hard timeout превышен, процесс убит
"""
from __future__ import annotations

import os
import subprocess
import sys
import time


DEFAULT_TIMEOUT_SEC = 300
TIMEOUT_EXIT_CODE = 124


def _ignore_console_ctrl_c() -> None:
    """Windows: перевести процесс в штатный режим «игнорировать Ctrl+C/Ctrl+Break»
    (SetConsoleCtrlHandler(NULL, TRUE)).

    Без этого headless Chromium при teardown иногда рассылает в консольную
    группу ложное console-control событие, из-за чего драйвер Playwright (node)
    и весь прогон падают. Экспериментально подтверждено: подходит только
    NULL-режим; ручные хендлеры (даже с return True) НЕ защищают драйвер."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)
    except Exception:
        pass


def _kill_tree(pid: int) -> None:
    """Убить дерево процесса pytest. Windows: taskkill /F /T (обязательно /T,
    иначе chromium/node останутся висеть); POSIX: SIGKILL самому pytest."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _parse_args(argv: list[str]) -> tuple[list[str], int]:
    """Разделить --timeout N/--timeout=N на стороне watchdog, остальное — pytest."""
    args = []
    timeout = DEFAULT_TIMEOUT_SEC
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--timeout":
            i += 1
            if i >= len(argv):
                raise SystemExit("--timeout требует значение (число секунд, 0 = отключить)")
            timeout = int(argv[i])
        elif arg.startswith("--timeout="):
            timeout = int(arg.split("=", 1)[1])
        else:
            args.append(arg)
        i += 1
    if not args:
        args = ["tests"]
    return args, timeout


def main() -> int:
    _ignore_console_ctrl_c()
    pytest_args, timeout_sec = _parse_args(sys.argv[1:])
    cmd = [sys.executable, "-u", "-m", "pytest"] + pytest_args

    print(f"[Watchdog] exec: {' '.join(cmd)}")
    print(f"[Watchdog] timeout: {timeout_sec if timeout_sec else 'disabled'} sec", flush=True)

    process = subprocess.Popen(cmd)
    start = time.time()

    try:
        while True:
            code = process.poll()
            if code is not None:
                print(f"\n[Watchdog] pytest finished, exit code {code}", flush=True)
                return code
            elapsed = time.time() - start
            if timeout_sec and elapsed > timeout_sec:
                print(
                    f"\n[Watchdog] HARD TIMEOUT ({timeout_sec}s) exceeded. "
                    f"Killing process tree {process.pid}...",
                    flush=True,
                )
                _kill_tree(process.pid)
                # Сознательно НЕ ждём process.wait() бесконечно: после taskkill
                # /T процесс умирает, но блокирующий wait может сам зависнуть.
                time.sleep(3.0)
                print(f"[Watchdog] KILLED after {timeout_sec}s (exit {TIMEOUT_EXIT_CODE})", flush=True)
                return TIMEOUT_EXIT_CODE
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[Watchdog] interrupted, killing pytest tree...", flush=True)
        _kill_tree(process.pid if process.poll() is None else -1)
        return 130
    finally:
        if process.poll() is None:
            _kill_tree(process.pid)


if __name__ == "__main__":
    raise SystemExit(main())