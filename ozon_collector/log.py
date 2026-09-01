"""Файловый логгер прогона (для последующей диагностики).

Консольный вывод НЕ меняется: log.info/warning/error печатают в stdout ровно
как прежние print (на них держатся CLI-UX и capsys-тесты), а параллельно
дублируют сообщение в файл. log.debug пишет ТОЛЬКО в файл — подробные шаги
парсинга, которые засоряли бы консоль.

Файл: logs/ozon_collector.log (перезаписывается каждый запуск, предыдущий
сохраняется как .log.1). Путь через env OZON_COLLECTOR_LOG. setup() вызывается
в cli.main(); без него модуль работает как чистый print (тесты ничего в файл
не пишут).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_DEFAULT_LOG = "logs/ozon_collector.log"
_log_path = Path(os.environ.get("OZON_COLLECTOR_LOG", _DEFAULT_LOG))

_fh = None
_worker_prefix = ""


def _project_root() -> Path:
    """Корень проекта: каталог ozon_collector/ родителя."""
    return Path(__file__).resolve().parent.parent


def _resolve_path() -> Path:
    p = _log_path
    if not p.is_absolute():
        p = _project_root() / p
    return p


def _rotate(path: Path):
    """Ротация: .log → .log.1 (сохранить предыдущий прогон)."""
    old = path.with_suffix(path.suffix + ".1")
    if path.exists():
        try:
            if old.exists():
                old.unlink()
            path.rename(old)
        except OSError:
            pass


def _reconfigure_streams():
    """Windows-консоль по умолчанию cp1251: юникодные символы (→ и др.)
    в print падают с UnicodeEncodeError и роняют процесс при редиректе вывода
    в файл. Переключаем stdout/stderr на UTF-8 с безопасным fallback."""
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass


def setup():
    """Открыть файл лога (идемпотентно). Вызывается при старте CLI."""
    global _fh
    if _fh is not None:
        return
    _reconfigure_streams()
    _reconfigure_streams()
    path = _resolve_path()
    _rotate(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _fh = open(path, "w", encoding="utf-8")
    _write_file("INFO", "log", f"log start pid={os.getpid()} at "
                 f"{time.strftime('%Y-%m-%d %H:%M:%S')}")


def set_prefix(prefix: str):
    """Установить префикс всех строк лога/консоли (напр. '[worker=1 pid=123] ').

    Процесс-локальный глобал: воркер ставит свой префикс после spawn, не влияя
    на parent (отдельный процесс) и на других воркеров.
    """
    global _worker_prefix
    _worker_prefix = prefix or ""


def attach_file(path, mode: str = "a", prefix: str = ""):
    """Присоединить файл лога воркера (БЕЗ ротации/перезаписи).

    Используется воркерами ВМЕСТО setup(): setup() делает ротацию (w-режим) и
    может затереть чужой лог. Здесь файл открывается в заданном режиме (по
    умолчанию "a" — append), плюс ставится префикс [worker=k pid=N].
    """
    global _fh
    if prefix:
        set_prefix(prefix)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _fh = open(p, mode, encoding="utf-8")
    _write_file("INFO", "log", f"attach_file start pid={os.getpid()} at "
                 f"{time.strftime('%Y-%m-%d %H:%M:%S')}")


def _write_file(level: str, module: str, msg: str, extra: dict | None = None):
    if _fh is None:
        return
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {level:7s} [{module}] {_worker_prefix}{msg}"
    if extra:
        line += " " + json.dumps(extra, ensure_ascii=False, default=str)
    try:
        _fh.write(line + "\n")
        _fh.flush()
    except OSError:
        pass


def _caller_module() -> str:
    """Имя модуля-отправителя (фрейм вызывающего)."""
    try:
        f = sys._getframe(2)
        name = f.f_globals.get("__name__", "?")
        return name.rsplit(".", 1)[-1]
    except Exception:
        return "?"


def _emit(level: str, msg: str, extra: dict | None, to_stderr: bool):
    _write_file(level, _caller_module(), msg, extra)
    out = _worker_prefix + msg
    if to_stderr:
        print(out, file=sys.stderr)
    else:
        print(out)


def info(msg: str, extra: dict | None = None):
    """Печать в stdout + запись в файл (эквивалент прежнего print)."""
    _emit("INFO", msg, extra, to_stderr=False)


def warning(msg: str, extra: dict | None = None):
    """Печать в stdout + запись в файл (как прежний print)."""
    _emit("WARNING", msg, extra, to_stderr=False)


def error(msg: str, extra: dict | None = None, stderr: bool = False):
    """Печать (stdout по умолчанию, stderr=True для CLI-ошибок) + файл."""
    _emit("ERROR", msg, extra, to_stderr=stderr)


def debug(msg: str, extra: dict | None = None):
    """Только в файл (подробный шаг парсинга, без вывода в консоль)."""
    _write_file("DEBUG", _caller_module(), msg, extra)
