"""Счётчики попыток пазла и журнал попыток (JSONL).

Состояние — процесс-локал (list как изменяемый синглтон), поэтому reset()
работает между тестами и воркерами. Журнал _attempt — машиночитаемый
пер-попыточный след для классификации fail-reason (detect/drag/token/UI),
пишется в $TMP, в репозиторий не попадает.

Инвариант KPI (ADR §14.7): failures() + solved == attempts().

Перенесено из puzzle.py без изменения кода (механический сплит).
"""

import json
import os
import tempfile
import time
from datetime import datetime

import numpy as np


# Сколько попыток решения даёт воркер на один инцидент пазла, прежде чем
# признать его нерешаемым (fallback → FAB-reload → FSM).
#
# 2026-09-22 live (2467 записей): drag идеален (445/517 с |err|≤0.9), но
# детектор даёт неверный bx в ~97% точных дропов → свип ±3 бесполезен
# (капча рероллится после каждого дропа). Успехи приходят из ПОВТОРНЫХ
# независимых заходов (свежий захват кадра → новые кандидаты): 2 захода
# давали ~15/492 ок. attempts=3 даёт ~1.5x шансов без риска свипа.
PUZZLE_MAX_ATTEMPTS = 3

# Процесс-локал (list как изменяемый синглтон, чтобы reset() работал в тестах).
_PUZZLE_FAILURES: list[int] = [0]
_PUZZLE_ATTEMPTS: list[int] = [0]
def reset() -> None:
    """Сбросить счётчик неудач (вызывается при старте сбора/воркера)."""
    _PUZZLE_FAILURES[0] = 0
    _PUZZLE_ATTEMPTS[0] = 0


def failures() -> int:
    """Число неудачных попыток решения пазла в текущем сборе."""
    return _PUZZLE_FAILURES[0]


def attempts() -> int:
    """Число попыток решения пазла (успешных + неудачных) в текущем сборе.

    Инвариант: failures() + solved == attempts(). Нужен для solve-rate в KPI
    (ADR §14.7): без него видно только «сорвалось N раз», но не «из скольких».
    """
    return _PUZZLE_ATTEMPTS[0]


def _note_attempt() -> None:
    _PUZZLE_ATTEMPTS[0] += 1


# Куда писать пер-попыточный JSONL (машиночитаемо, для классификации
# fail-reason: detect/drag/token/UI). Диагностика, НЕ в репозиторий.
_ATTEMPT_LOG_DIR = os.environ.get(
    "OZON_ATTEMPT_LOG", os.path.join(tempfile.gettempdir(), "puzzle_attempts"))


def _attempt(kind: str, **kw) -> None:
    """Дописать строку JSONL с попыткой решения (тихая, без исключений)."""

    def _js_default(o):
        if isinstance(o, np.floating):
            return round(float(o), 3)
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.bool_):
            return bool(o)
        return str(o)

    try:
        os.makedirs(_ATTEMPT_LOG_DIR, exist_ok=True)
        path = os.path.join(_ATTEMPT_LOG_DIR,
                            time.strftime("%Y%m%d") + ".jsonl")
        rec = {"ts": datetime.now().strftime("%H%M%S.%f")[:-3], "kind": kind}
        rec.update(kw)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False,
                               default=_js_default) + "\n")
    except Exception:
        pass
