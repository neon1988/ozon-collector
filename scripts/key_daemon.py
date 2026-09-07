"""Автономный супервизор кампаний сбора Ozon (key_daemon).

Долгоживущий Python-процесс, сам управляющий кампанией сбора:
итеративный дособор (САМ ищет выдачу и рассылает товары воркерам —
in-process через collect_parallel/collect_search, без subprocess cli.py),
контроль качества (qa_raw), recovery при challenge/crash (backoff +
cooldown + авто-resume), защита от зацикливания на одной выдаче,
контролируемый rescan бракованных PID.

Критерий завершения кампании — НЕ `collected` одного прогона, а рост числа
ВАЛИДНЫХ canonical raw (`data/{pid}_raw.json`, прошедших QA без FAIL) до target
при финальном QA по зафиксированному snapshot'у (FAIL==0).

Состояния кампании (final_state в progress.json):
  COMPLETED        — valid >= target и финальный QA FAIL == 0
  COMPLETED_MIN    — min_valid <= valid < target, выдача исчерпана (частичный успех)
  ABANDONED        — valid < min_valid (нехватка товаров / тех. отказ) или
                     no-progress-лимит
  NEEDS_ATTENTION  — непредвиденная ошибка; требует человека (терминально)
  INTERRUPTED      — остановлено пользователем (Ctrl+C): graceful shutdown,
                     прогресс сохранён, resume подхватит недособранное

Атомарно ведёт manifest data/campaigns/{campaign}/progress.json и
logs/{campaign}_daemon.log. Итог сбора читает из машиночитаемого combined JSON.

Использование:
    python scripts/key_daemon.py --campaign prezervativy \
        --url "<URL>" --limit 300 --out docs/prezervativy.md \
        --max-age 7d --min-reviews 40 --parallel 1 \
        --min-valid 200 --max-no-progress-runs 3 --max-rescans 2 \
        --max-total-hours 12 --notify toast
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ozon_collector import log
from ozon_collector.search import CollectionInterrupted

DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"
CAMPAIGNS_DIR = DATA_DIR / "campaigns"

# Режим "без кулдаунов" (KD_NO_COOLDOWN=1): демон НЕ спит в cooldown между
# прогонами, НЕ применяет adaptive паузу при challenge и НЕ эскалирует
# challenge-шторм в NEEDS_ATTENTION. Сбор идёт непрерывно, обходя защиту
# перезапуском/тёплой сессией. Включён по запросу пользователя 2026-08-31.
_NO_COOLDOWN = bool(os.environ.get("KD_NO_COOLDOWN"))

# Кооперативная остановка по Ctrl+C (2026-09-03). Демон — ЕДИНСТВЕННЫЙ, кто
# получает консольный CTRL_C_EVENT (воркеры глушат его через SetConsoleCtrlHandler
# в worker_main). Обработчик сигнала ставит STOP_EVENT, а collect_parallel —
# _await_workers штатно сворачивают воркеров (без респавна) и сохраняют прогресс;
# кампания завершается с final_state=INTERRUPTED (НЕ CRASH, счётчики сбоев не
# трогаем). Второй Ctrl+C во время shutdown — форс: поднимает _FORCE_EXIT, вместо
# кооперативного ожидания демон после короткой зачистки выходит принудительно.
_STOP_EVENT = threading.Event()
_FORCE_EXIT = threading.Event()

PYTHON = sys.executable
QA_RAW = str(ROOT / "scripts" / "qa_raw.py")

DEFAULT_DATA_DIR = str(DATA_DIR)
BACKOFF_DELAYS_S = [300, 900, 1800, 3600, 7200]  # 5м, 15м, 30м, 1ч, 2ч
# Adaptive cooldown при challenge (ADR §14.5): эскалация по числу подряд
# исчерпанных challenge-инцидентов (storm_streak). Не фиксированные 60с.
CHALLENGE_RETRY_DELAY_S = 60  # дефолт для НОВОГО шторма; переопределяется --challenge-pause
CHALLENGE_COOLDOWN_STEPS_S = [60, 300, 900]  # storm#1→1м, #2→5м, #3→15м
CHALLENGE_JITTER_FRAC = 0.2  # ±20% джиттер (в тестах seed/заморозка)
# Пауза при ЧАСТИЧНОМ шторме (parallel>1, штормила часть воркеров): короткая,
# БЕЗ эскалации storm_streak/profile_cooldown (ADR §14.6) — пока остальные
# воркеры прогрессируют, ждём фиксированный интервал, не сжигая cooldown кампании.
CHALLENGE_PARTIAL_PAUSE_S = 20
# Пауза между транзиентными ретраями UNKNOWN_FAILURE: короткая (не степпер),
# т.к. это временные сбои subprocess, а не устойчивый кризис счётчика.
TRANSIENT_RETRY_BACKOFF_S = 4
QA_BATCH_SIZE = 100
TERMINAL_STATES = {"COMPLETED", "COMPLETED_MIN", "ABANDONED", "NEEDS_ATTENTION",
                   "INTERRUPTED"}
OK_VERDICTS = {"OK", "WARN"}
# Формализованные состояния runtime-цикла демона (для status.json / state API),
# отдельно от TERMINAL_STATES (итог кампании). Внутренние фазы:
DAEMON_STATES = {
    "STARTING", "RUNNING_FULL", "RUNNING_RESCAN", "COOLDOWN_CHALLENGE",
    "COOLDOWN_CRASH", "FINALIZING", "COMPLETED", "COMPLETED_MIN",
    "ABANDONED", "NEEDS_ATTENTION", "INTERRUPTED",
}
# Версия QA-логики. Входит в ключ QA-кэша (verdict_meta.qa_version): улучшение
# QA (новый FAIL/WARN) инвалидирует старые вердикты даже если raw не менялся.
QA_VERSION = "1.7"


def _sha256_text(s: str) -> str:
    try:
        return hashlib.sha256((s or "").encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _collector_consts():
    """SCHEMA_VERSION + COLLECTOR_VERSION из пакета, lazy (без загрузки Ozon).

    Вынесено, чтобы импорт key_daemon (например, в тестах) не тянул тяжёлые
    зависимости коллектора. Возвращает (SCHEMA_VERSION, COLLECTOR_VERSION).
    """
    try:
        sys.path.insert(0, str(ROOT))
        from ozon_collector import SCHEMA_VERSION as _sv
        from ozon_collector import VERSION as _cv
        return _sv, _cv
    except Exception:
        return "2.0", "unknown"


_SCHEMA_VERSION, _COLLECTOR_VERSION = _collector_consts()
SCHEMA_VERSION = _SCHEMA_VERSION
COLLECTOR_VERSION = _COLLECTOR_VERSION


# ---------------------------------------------------------------------------
# чистые функции
# ---------------------------------------------------------------------------

def classify_status(exit_code: int, combined: Optional[dict]) -> str:
    """Классифицирует итог одного прогона сбора (по combined + exit-коду, не по логу)."""
    if combined and "aborted" in combined:
        return "CHALLENGE_STORM"
    if exit_code == 0:
        return "NORMAL_DONE"
    if combined is None:
        return "CRASH"
    return "UNKNOWN_FAILURE"


def extract_discovered_pids(combined: Optional[dict]) -> list[str]:
    """PID, обнаруженные выдачей этого прогона (combined.products, все записи).

    PID нормализуются (снимают URL-обёртку и хвосты), чтобы дальнейшие
    сравнения/словари были консистентны независимо от формата записи.
    Также сливает combined.discovered_pids (заполняется при graceful-стопе
    докрутки ссылок в collect_parallel: products пуст, но ссылки найдены).
    """
    if not combined:
        return []
    pids = [p["pid"] for p in (combined.get("products") or [])
            if p.get("pid") is not None]
    pids += list(combined.get("discovered_pids") or [])
    return normalize_pids(pids)


def extract_ok_pids(combined: Optional[dict]) -> list[str]:
    """PID, реально собранные без исключения (потенциально валидные)."""
    if not combined:
        return []
    return normalize_pids(p["pid"] for p in (combined.get("products") or [])
                          if p.get("ok") and not p.get("excluded"))


def normalize_pids(seq: Iterable[str]) -> list[str]:
    """Нормализует PID: снимает URL-обёртку, убирает дубли и пустые."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in seq:
        s = str(raw).strip()
        if not s:
            continue
        pid = s.split("product/")[-1] if "product/" in s else s
        pid = pid.split("?")[0].split("/")[0]
        if pid.isdigit() and pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def raw_pid_name(pid: str) -> str:
    return f"{pid}_raw.json"


def all_raw_pids(data_dir: str = DEFAULT_DATA_DIR) -> list[str]:
    """Каноничные pid, для которых есть data/{pid}_raw.json."""
    d = Path(data_dir)
    if not d.is_dir():
        return []
    return [f.name[: -len("_raw.json")] for f in sorted(d.glob("*_raw.json"))
            if f.name[: -len("_raw.json")].isdigit()]


def has_raw(data_dir: str, pid: str) -> bool:
    return (Path(data_dir) / raw_pid_name(pid)).is_file()


def _raw_mtime(data_dir: str, pid: str) -> Optional[float]:
    """st_mtime (float, секунды) canonical raw, или None если файла нет."""
    try:
        return (Path(data_dir) / raw_pid_name(pid)).stat().st_mtime
    except OSError:
        return None


def _sha256_file(path: Path) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def raw_fingerprint(data_dir: str, pid: str) -> Optional[dict]:
    """Fingerprint canonical raw на момент проверки, или None если файла нет.

    {raw_mtime_ns, raw_size, raw_sha256}. SHA256 — строгий идентификатор
    версии raw; mtime_ns — быстрый предварительный фильтр.
    """
    p = Path(data_dir) / raw_pid_name(pid)
    try:
        st = p.stat()
    except OSError:
        return None
    return {
        "raw_mtime_ns": st.st_mtime_ns,
        "raw_size": st.st_size,
        "raw_sha256": _sha256_file(p),
    }


def qa_cache_valid(data_dir: str, pid: str, meta) -> bool:
    """Актуален ли кешированный вердикт (verdict_meta) для текущего raw/QA.

    Алгоритм (рекомендация из ревизии):
      fp = fingerprint(raw)
      fp нет      → False (raw удалён — вердикт не держим)
      meta нет    → False
      qa_version≠ → False (QA изменился — перепроверить даже если raw тот же)
      mtime не изменился → True (быстрый фильтр, доверяем кешу)
      mtime изменился    → SHA256 == ? (строгий fingerprint после изменения)
    """
    fp = raw_fingerprint(data_dir, pid)
    if fp is None or not meta:
        return False
    if meta.get("qa_version") != QA_VERSION:
        return False
    if fp["raw_mtime_ns"] == meta.get("raw_mtime_ns"):
        return True
    return meta.get("raw_sha256") == fp["raw_sha256"]


def delta(before: Iterable[str], after: Iterable[str]) -> set:
    return set(after) - set(before)


def batch_pids(pids: Iterable[str], size: int = QA_BATCH_SIZE) -> list[list[str]]:
    items = list(dict.fromkeys(pids))
    return [items[i:i + size] for i in range(0, len(items), size)]


def backoff_delay(backoff_index: int) -> int:
    if backoff_index <= 0:
        return 0
    idx = min(int(backoff_index) - 1, len(BACKOFF_DELAYS_S) - 1)
    return BACKOFF_DELAYS_S[idx]


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_new_campaign(min_valid: int, target: int) -> None:
    """Валидация параметров НОВОЙ кампании (для resume manifest уже согласован).

    min_valid обязателен: min_valid=0 позволил бы COMPLETED_MIN даже с 0
    чистых — бессмысленный «частичный успех». min_valid >= target делает
    COMPLETED_MIN недостижимым (любая мягкая остановка → ABANDONED).
    """
    if not (1 <= min_valid < target):
        raise SystemExit(
            f"key_daemon: невалидный --min-valid {min_valid} — "
            f"нужно 1 <= min_valid < target({target})")


def default_manifest(campaign: str, target: int, min_valid: int,
                     url: str, out: str, **run_cfg) -> dict:
    return {
        "campaign": campaign,
        "target_count": target,
        "min_valid": min_valid,
        "url": url,
        "out": out,
        "qa_version": QA_VERSION,
        "campaign_identity": {
            "url_sha256": _sha256_text(url),
            "schema_version": SCHEMA_VERSION,
            "collector_version": COLLECTOR_VERSION,
        },
        "run_cfg": dict(run_cfg),
        "run_cfg_hash": "",
        "started_at": now_utc_iso(),
        "discovered_pids": [],
        "valid_pids": [],
        "failed_pids": [],
        "permanently_failed_pids": [],
        "replacement_pids": [],
        "title_mismatch_pids": [],   # товары, не прошедшие title-фильтр (не FAIL)
        "title_match_query": "",     # фраза title-фильтра (пусто = выключен)
        "verdicts": {},            # pid -> OK|WARN|FAIL (QA по новым на лету)
        "verdict_meta": {},        # pid -> {verdict, raw_mtime_ns, raw_sha256,
                                   #         raw_size, qa_version, qa_at}
        "running_rescans": {},     # pid -> число попыток rescan
        "fail_reason_seq": {},     # pid -> {"key": (коды issues), "count": сколько раз подряд тот же FAIL}
        "progress": {"new_discovered": 0, "new_valid": 0,
                     "rescans_fixed": 0, "rescans_failed": 0},
        "stall": {"full_runs_without_discovery": 0,
                  "full_runs_without_valid": 0,
                  "challenge_runs": 0, "crash_runs": 0},
        "runs": [],
        "cooldown_until": None,
        "challenge_seq": 0,
        "challenge_control": {   # ADR §14.4: шкала эскалации challenge (persistent)
            "storm_streak": 0, "total_storms": 0,
            "last_storm_at": None, "cooldown_until": None,
            "last_signature": None,
        },
        "profile_cooldown": {    # ADR §14.4: отдельно от storm (почему стоит)
            "until": None, "reason": None,
            "challenge_count": 0, "last_signature": None,
        },
        "no_progress_count": 0,
        "transient_failures": 0,
        "last_error": None,
        "run_cfg_modified": False,
        "runtime_state": "STARTING",
        "final_state": None,
        "final_reason": None,
        "final_note": None,
    }


def load_json(path) -> Optional[dict]:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_json(path, data) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(p))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def parse_qa_stdout(stdout: str) -> dict:
    """Парсит сводную строку qa_raw: '[qa] товаров: N | OK a | WARN b | FAIL c'."""
    out = {"products": 0, "ok": 0, "warn": 0, "fail": 0, "wall_limited": 0}
    for line in stdout.splitlines():
        ln = line.strip()
        if not ln.startswith("[qa] товаров:"):
            continue
        for part in ln.split("|"):
            part = part.strip()
            if part.startswith("[qa] товаров:"):
                tok = part.split(":")[-1].strip()
            else:
                tok = part.split()[-1] if part else ""
            for key, field in (("товаров:", "products"), ("OK", "ok"),
                               ("WARN", "warn"), ("FAIL", "fail"),
                               ("WALL_LIMITED", "wall_limited")):
                if part.startswith(key if key != "товаров:" else "[qa] товаров:"):
                    if tok.isdigit():
                        out[field] = int(tok)
    return out


def compute_final(progress: dict, exhausted: bool) -> str:
    """Итоговое состояние. exhausted=True — выдача исчерпана (есть min_valid)."""
    valid = len(set(progress.get("valid_pids", [])))
    target = progress["target_count"]
    min_valid = progress.get("min_valid", 0)
    if valid >= target:
        return "COMPLETED"
    if exhausted:
        return "COMPLETED_MIN" if valid >= min_valid else "ABANDONED"
    return "ABANDONED" if valid < min_valid else None


def summarize(progress: dict) -> dict:
    return {
        "campaign": progress.get("campaign"),
        "target": progress.get("target_count"),
        "min_valid": progress.get("min_valid"),
        "valid": len(set(progress.get("valid_pids", []))),
        "discovered": len(set(progress.get("discovered_pids", []))),
        "failed": len(set(progress.get("failed_pids", []))),
        "permanently_failed": len(set(progress.get("permanently_failed_pids", []))),
        "runs": len(progress.get("runs", [])),
        "final_state": progress.get("final_state"),
        "final_reason": progress.get("final_reason"),
    }


# ---------------------------------------------------------------------------
# инфраструктура процесса
# ---------------------------------------------------------------------------

def _alive(pid: int) -> bool:
    """Проверка живости процесса (best-effort).

    Защита от нереалистичных PID: os.kill(pid, 0) с огромным/нулевым/отрицательным
    PID на Windows может зависнуть или дать ложный результат. PID реальных
    процессов на Windows/Linux практически никогда не выходят за 2^24.
    """
    if not isinstance(pid, int) or pid <= 0 or pid >= 2 ** 24:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except SystemError:
        return False


class PidLock:
    """pid-файл демона — защита от повторного запуска."""

    def __init__(self, path: Path):
        self.path = path

    def acquire(self, campaign: str) -> bool:
        if self.path.is_file():
            data = load_json(self.path)
            pid = data.get("pid") if data else None
            # pid обязан быть int; иначе файл битый → трактуем как stale
            if isinstance(pid, int):
                if _alive(pid):
                    return False
            self.path.unlink(missing_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        save_json(self.path, {
            "pid": os.getpid(), "campaign": campaign,
            "started_at": now_utc_iso(),
            "start_cmdline": " ".join(sys.argv),
        })
        return True

    def release(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def _status_payload(progress: dict) -> dict:
    """Полный snapshot состояния кампании — runtime API для мониторинга.

    Пишется в status.json при каждом state-transition (см. _set_state).
    Это не «уведомление», а живое состояние процесса.
    """
    cfg = progress.get("run_cfg", {})
    started_ts = datetime.fromisoformat(progress["started_at"]).timestamp() \
        if progress.get("started_at") else None
    deadline = (started_ts + cfg.get("max_total_hours", 0) * 3600) \
        if started_ts else None
    return {
        "schema": 1,
        "campaign": progress.get("campaign"),
        "state": progress.get("runtime_state"),
        "updated_at_utc": now_utc_iso(),
        "started_at_utc": progress.get("started_at"),
        "deadline_at_utc": (datetime.fromtimestamp(deadline, timezone.utc)
                            .isoformat(timespec="seconds") if deadline else None),
        "target": progress.get("target_count"),
        "min_valid": progress.get("min_valid"),
        "valid": len(set(progress.get("valid_pids", []))),
        "discovered": len(set(progress.get("discovered_pids", []))),
        "failed": len(set(progress.get("failed_pids", []))),
        "permanently_failed": len(set(progress.get("permanently_failed_pids", []))),
        "reserve": len(progress.get("campaign_reserve_pids") or []),
        "title_match_query": progress.get("title_match_query") or "",
        "title_mismatch": len(set(progress.get("title_mismatch_pids", []))),
        "run": len(progress.get("runs", [])),
        "current_run": (progress.get("runs") or [None])[-1],
        "mode": progress.get("current_mode"),
        "cooldown_until": progress.get("cooldown_until"),
        "challenge_seq": progress.get("challenge_seq", 0),
        "challenge_control": progress.get("challenge_control"),
        "profile_cooldown": progress.get("profile_cooldown"),
        "no_progress_count": progress.get("no_progress_count", 0),
        "progress": progress.get("progress"),
        "stall": progress.get("stall"),
        "final_state": progress.get("final_state"),
        "final_reason": progress.get("final_reason"),
        "last_error": progress.get("last_error"),
        "running_rescans": progress.get("running_rescans") or {},
        "browser_tabs": (progress.get("recovery_telemetry") or {}).get("browser_tabs"),
        "collector_tabs": (progress.get("recovery_telemetry") or {}).get("collector_tabs"),
        "last_page_url": (progress.get("recovery_telemetry") or {}).get("last_page_url"),
        "last_challenge_signature": (progress.get("recovery_telemetry") or {})
            .get("last_challenge_signature"),
        "kpi": (progress.get("recovery_telemetry") or {}).get("kpi"),
        "fingerprint": (progress.get("recovery_telemetry") or {})
            .get("fingerprint"),
    }


def _write_status(progress: dict) -> None:
    camp = progress.get("campaign", "campaign")
    save_json(DATA_DIR / "campaigns" / camp / "status.json",
              _status_payload(progress))


def _emit_event(progress: dict, event: str, **kw) -> None:
    """Журнал событий events.jsonl — история для диагностики.

    Manifest (progress.json) — текущее состояние; events.jsonl — история.
    Пишется построчно (append), никогда не перезаписывается — расследование
    долгих кампаний не теряется.
    """
    camp = progress.get("campaign", "campaign")
    rec = {"ts": now_utc_iso(), "event": event, "campaign": camp}
    rec.update({k: v for k, v in kw.items() if v is not None})
    line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    try:
        p = DATA_DIR / "campaigns" / camp / "events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _journal_dir(progress: dict) -> Path:
    return CAMPAIGNS_DIR / progress.get("campaign", "campaign") / "runs"


def _journal_stage(progress: dict, run_id: str, stage: str,
                   data: Optional[dict] = None) -> None:
    """Записывает стадию прогона в run-журнал (transactional workflow).

    Файл runs/{run_id}.json хранит стадии run_started / run_completed /
    qa_completed / state_committed для этого прогона. При resume по
    отсутствию state_committed демон знает, что прогон не доcommit'ился и
    его следует осторожно продолжить (не потерять только что собранный raw).
    """
    try:
        d = _journal_dir(progress)
        d.mkdir(parents=True, exist_ok=True)
        fp = d / f"{run_id}.json"
        rec = load_json(fp) or {"run_id": run_id,
                                "campaign": progress.get("campaign")}
        stages = rec.setdefault("stages", {})
        if stage in ("run_started", "state_committed"):
            stages[stage] = now_utc_iso()
        else:
            stages[stage] = {**stages.get(stage, {}), "at": now_utc_iso(),
                             **(data or {})}
        rec["stages"] = stages
        save_json(fp, rec)
    except OSError:
        pass


def _absorb_recovery_telemetry(progress: dict, combined: Optional[dict]) -> None:
    """Переносит recovery-телеметрию сбора (combined.recovery) в progress.

    Этап 0 (ADR §14.6): только наблюдаемость, поведение не меняется. Из
    combined.recovery берём последние события (signature, last_url,
    browser_tabs, collector_tabs) и сохраняем в progress — их читает
    status.json (browser_tabs, last_challenge_signature, last_page_url).
    """
    if not combined:
        return
    rec = (combined.get("recovery") or {})
    meta = progress.setdefault("recovery_telemetry", {})
    kpi = combined.get("kpi")
    if kpi:
        acc = meta.setdefault("kpi", {})
        for k in ("total_challenges", "total_recoveries"):
            v = kpi.get(k)
            if v is not None:
                acc[k] = (acc.get(k, 0) or 0) + int(v)
        if kpi.get("products_since_recovery") is not None:
            acc["products_since_recovery"] = kpi["products_since_recovery"]
        last_sr = kpi.get("success_rate")
        if last_sr is not None:
            acc["last_success_rate"] = last_sr
    if combined.get("fingerprint"):
        meta["fingerprint"] = combined["fingerprint"]
    progress["recovery_telemetry"] = meta
    events = rec.get("events") or []
    if not events:
        return
    last = events[-1]
    for key, src in (("last_challenge_signature", "signature"),
                     ("last_page_url", "last_url"),
                     ("browser_tabs", "browser_tabs"),
                     ("collector_tabs", "collector_tabs")):
        if src in last:
            meta[key] = last[src]
    if rec.get("browser_tabs") is not None:
        meta["browser_tabs"] = rec.get("browser_tabs")
    progress["recovery_telemetry"] = meta


def _challenge_cooldown_seconds(progress: dict) -> float:
    """Adaptive пауза при challenge (ADR §14.5): эскалация по storm_streak.

    storm#1 → 1м, #2 → 5м, #3 → 15м, дальше → фиксировано 15м (демон при
    длинной эскалации уходит в NEEDS_ATTENTION отдельной логикой). Джиттер
    ±20% (CHALLENGE_JITTER_FRAC) — тесты замораживают через seed.
    """
    cc = progress.get("challenge_control") or {}
    streak = cc.get("storm_streak", 0)
    idx = min(max(streak - 1, 0), len(CHALLENGE_COOLDOWN_STEPS_S) - 1)
    base = CHALLENGE_COOLDOWN_STEPS_S[idx]
    base = min(base, CHALLENGE_COOLDOWN_STEPS_S[-1])
    jitter = base * CHALLENGE_JITTER_FRAC
    delay = base + _jitter_offset(jitter)
    return max(delay, 1.0)


def _jitter_offset(amplitude: float) -> float:
    """Случайное отклонение в [-amplitude, +amplitude]; детерминизм в тестах."""
    import random
    if os.environ.get("KD_RANDOM_SEED"):
        random.seed(os.environ["KD_RANDOM_SEED"])
    return random.uniform(-amplitude, amplitude)


def _bump_challenge_control(progress: dict, signature: str | None = None) -> dict:
    """Увеличивает challenge_control (storm_streak/total_storms) после
    исчерпанного challenge-инцидента (SESSION_BLOCKED). Persistent.
    """
    cc = progress.get("challenge_control") or {}
    cc["storm_streak"] = cc.get("storm_streak", 0) + 1
    cc["total_storms"] = cc.get("total_storms", 0) + 1
    cc["last_storm_at"] = now_utc_iso()
    tele = progress.get("recovery_telemetry") or {}
    cc["last_signature"] = signature or tele.get("last_challenge_signature")
    progress["challenge_control"] = cc
    return cc


def _recover_interrupted_run(progress: dict, data_dir: str,
                             qa_fn: Optional[Callable[[list[str]], dict]] = None):
    """Transactional recovery по run-журналу при resume.

    Если последний прогон НЕ дослат до state_committed (демон упал между
    run_started и коммитом вердиктов), fresh raw уже на диске, но его pid не
    отражены в манифесте — они были бы потеряны. Повторно применяем discovery +
    QA + reconcile по этому прогону (combined_{run_id}.json), затем помечаем
    state_committed. Ничего не делает, если журнала/данных прогона нет.
    """
    jdir = _journal_dir(progress)
    if not jdir.is_dir():
        return
    run_ids = sorted(
        (f.stem for f in jdir.glob("*.json") if f.name.endswith(".json")),
        key=lambda rid: rid)
    if not run_ids:
        return
    last = run_ids[-1]
    rec = load_json(jdir / f"{last}.json") or {}
    stages = rec.get("stages", {})
    if "run_started" not in stages or "state_committed" in stages:
        return

    combined = load_json(CAMPAIGNS_DIR / progress.get("campaign", "campaign")
                         / f"combined_{last}.json")
    if not combined:
        # прогон не успел ничего собрать — данных нет; фиксируем как committed,
        # чтобы не чинить каждый старт. Следующий цикл родит новый run_id.
        _journal_stage(progress, last, "state_committed",
                       {"recovered": True, "combined_missing": True})
        return

    discovered = extract_discovered_pids(combined)
    ok_pids = extract_ok_pids(combined)
    if discovered:
        progress["discovered_pids"] = normalize_pids(
            list(progress.get("discovered_pids", [])) + list(discovered))
    new_checked = classify_new_pids(progress, ok_pids, qa_fn=qa_fn)
    data_dir = data_dir or DEFAULT_DATA_DIR
    reconcile_validity(progress, new_checked, data_dir)
    _journal_stage(progress, last, "state_committed",
                   {"recovered": True, "new_valid": len(new_checked.get("valid_add", [])),
                    "checked": len(new_checked.get("checked", []))})
    log.info(f"recovery: прерванный прогон {last} восстановлен "
             f"(discovered={len(discovered)}, new_valid="
             f"{len(new_checked.get('valid_add', []))})")


def _set_state(progress: dict, state: str, event: str = None, **kw) -> None:
    """Формализованный state-transition: обновляет runtime_state, пишет
    status.json (обязательный runtime API) и лог события events.jsonl.

    state — одно из DAEMON_STATES. event — имя события для журнала
    (по умолчанию = state). kw — доп. поля события.
    """
    progress["runtime_state"] = state
    progress["current_mode"] = progress.get("current_mode")
    _write_status(progress)
    _emit_event(progress, event or state, **kw)


def notify(progress: dict, msg: str) -> None:
    """Уведомление: обновляет status.json (state-сообщение) + toast best-effort.

    Полный runtime-state пишется в _set_state; notify используется для
    значимых сообщений (паузы/ошибки/финал) и показывает message.
    """
    camp = progress.get("campaign", "campaign")
    payload = _status_payload(progress)
    payload["message"] = msg
    save_json(DATA_DIR / "campaigns" / camp / "status.json", payload)
    if progress.get("run_cfg", {}).get("notify") == "toast":
        try:
            kwargs = {"capture_output": True, "timeout": 10}
            if sys.platform == "win32":
                # Не разделять консоль с pytest: иначе PowerShell при выходе
                # шлёт CTRL_C_EVENT → KeyboardInterrupt в Python.
                kwargs["creationflags"] = getattr(
                    subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Write-Host '{msg.replace(chr(39), '')}' ; "
                 f"[System.Media.SystemSounds]::Beep.Play()"],
                **kwargs)
        except Exception:
            pass


def _parse_qa_json_report(path: str) -> Optional[dict]:
    """Читает машинный JSON-отчёт qa_raw (--report-json).

    Возвращает сводку в том же формате, что и parse_qa_stdout (+ per_pid),
    или None, если файла/контракта нет (тогда fallback — парсинг stdout).
    """
    payload = load_json(path)
    if not payload or "summary" not in payload or "products" not in payload:
        return None
    s = payload["summary"]
    parsed = {
        "products": s.get("products", 0),
        "ok": s.get("ok", 0),
        "warn": s.get("warn", 0),
        "fail": s.get("fail", 0),
        "wall_limited": s.get("wall_limited_count", 0),
    }
    parsed["per_pid"] = {r["pid"]: r["verdict"] for r in payload["products"]}
    # причины FAIL/WARN (коды issues) — для детекта детерминированного брака
    parsed["per_pid_issues"] = {
        r["pid"]: [i["code"] for i in r["issues"]]
        for r in payload["products"]
    }
    return parsed


def run_qa_batch(pids: Iterable[str]) -> dict:
    """Офлайн QA по батчу PID через qa_raw.py (без отчёта). Возвращает сводку.

    Машинный контракт — JSON-файл (--report-json), stdout парсится только как
    fallback для старых версий qa_raw.
    """
    report = None
    fd, report = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        cmd = [PYTHON, "-u", "-X", "utf8", QA_RAW, "--pids", ",".join(pids),
               "--report-json", report]
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", timeout=900)
        parsed = _parse_qa_json_report(report)
        if parsed is None:
            parsed = parse_qa_stdout(res.stdout)
            parsed["per_pid"] = _parse_qa_per_pid(res.stdout)
        parsed["exit_code"] = res.returncode
        return parsed
    finally:
        if os.path.exists(report):
            os.remove(report)


def qa_summary_consistent(qa_batch: dict, batch_size: int) -> bool:
    """Проверка инвариантов сводки qa_raw для батча (K4).

    Парсинг QA считается достоверным ТОЛЬКО если сводка обработала ровно
    `batch_size` товаров и сумма OK+WARN+FAIL совпадает с общим числом.
    Иначе per_pid/вердикты могут быть неполными — пропускать брак нельзя.
    """
    s = qa_batch
    total = s.get("products")
    if total is None or total != batch_size:
        return False
    return s.get("ok", 0) + s.get("warn", 0) + s.get("fail", 0) == total


def _parse_qa_per_pid(stdout: str) -> dict:
    """pid -> вердикт, из строк таблицы qa_raw ('pid  VERDICT ...')."""
    verdicts: dict[str, str] = {}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1] in OK_VERDICTS | {"FAIL", "INFO"}:
            verdicts[parts[0]] = parts[1]
    return verdicts


def classify_new_pids(progress: dict, candidates: Iterable[str],
                      qa_fn: Optional[Callable[[list[str]], dict]] = None) -> dict:
    """QA новых кандидатов → (valid_add, failed_add, verdicts).

    Обходит только те pid, у которых есть canonical raw и для которых ещё не
    определён актуальный вердикт. qa_fn инъекция для тестов; по умолчанию —
    subprocess QA.

    Актуальность вердикта (QA-кэш) определяется по verdict_meta (qa_cache_valid):
    fingerprint raw (mtime_ns быстрый фильтр + raw_sha256 строгий) и qa_version.
    Race-защита: fingerprint ПОСЛЕ QA должен совпадать с ДО QA (mtime_ns) — если
    raw менялся во время QA, вердикт недействителен и ждёт следующего цикла.

    Вердикт определяется по сводке [qa] товаров: (K4):
      - инварианты (total == len(batch), ok+warn+fail == total) ДОЛЖНЫ сходиться,
        иначе парсинг недостоверен → parse_error=True (демон уходит в
        NEEDS_ATTENTION, тихий пропуск брака исключён);
      - при fail==0 и неполном per_pid отсутствующие PID (легитимный случай
        SHOW_ALL_LIMIT) считаются OK;
      - при fail>0 per_pid ДОЛЖЕН покрывать ВСЕ PID батча, иначе fail-пид может
        получить fallback OK → parse_error=True.
    """
    qa_fn = qa_fn or run_qa_batch
    data_dir = progress.get("run_cfg", {}).get("data_dir", DEFAULT_DATA_DIR)
    base_verdicts = dict(progress.get("verdicts", {}))
    verdicts = dict(base_verdicts)
    verdict_meta = dict(progress.get("verdict_meta", {}))

    # Кому нужен QA: нет актуального вердикта (raw изменился / QA сменился /
    # вердикта не было). Игнорируем пиды без raw вообще.
    need = []
    for p in dict.fromkeys(candidates):
        if not has_raw(data_dir, p):
            continue
        if p in verdicts and qa_cache_valid(data_dir, p, verdict_meta.get(p)):
            continue  # вердикт актуален
        need.append(p)
    if not need:
        return {"valid_add": [], "failed_add": [], "verdicts": verdicts,
                "checked": [], "parse_error": False}

    valid_add: list[str] = []
    failed_add: list[str] = []
    checked: list[str] = []
    new_meta: dict[str, dict] = {}
    per_pid_issues: dict[str, list[str]] = {}
    for batch in batch_pids(need, QA_BATCH_SIZE):
        # fingerprint ДО QA — для race-детекта изменения raw во время чтения
        fp_before = {p: raw_fingerprint(data_dir, p) for p in batch}
        qa = qa_fn(batch)
        per_pid = qa.get("per_pid") or {}
        if qa.get("per_pid_issues"):
            per_pid_issues.update(qa["per_pid_issues"])
        if not qa_summary_consistent(qa, len(batch)):
            # при parse_error возвращаем ИСХОДНЫЕ вердикты (без частичных
            # накоплений уже обработанных батчей) — прогресс не мутируется.
            return {"valid_add": [], "failed_add": [],
                    "verdicts": dict(base_verdicts),
                    "checked": [], "parse_error": True}
        # инварианты сошлись. Если есть FAIL — per_pid ДОЛЖЕН покрывать ВСЕ
        # PID батча, иначе fail-пид может получить fallback OK (тихий пропуск).
        if qa.get("fail", 0) > 0:
            missing = [p for p in batch if p not in per_pid]
            if missing:
                return {"valid_add": [], "failed_add": [],
                        "verdicts": dict(base_verdicts),
                        "checked": [], "parse_error": True}
        now_iso = now_utc_iso()
        # fail==0 и неполный per_pid — легитимный SHOW_ALL_LIMIT: всё OK
        for pid in batch:
            # fingerprint ПОСЛЕ QA: если raw поменялся во время QA (mtime_ns
            # разошёлся) — вердикт недействителен (внешний процесс писал raw),
            # не фиксируем его, он пере-QA-нется в следующем цикле.
            fp = raw_fingerprint(data_dir, pid)
            if fp is None or fp["raw_mtime_ns"] != (fp_before.get(pid) or {}).get(
                    "raw_mtime_ns"):
                continue
            v = per_pid.get(pid, "OK")  # OK только при доказанных инвариантах
            verdicts[pid] = v
            checked.append(pid)
            new_meta[pid] = {
                "verdict": v, "raw_mtime_ns": fp["raw_mtime_ns"],
                "raw_sha256": fp["raw_sha256"], "raw_size": fp["raw_size"],
                "qa_version": QA_VERSION, "qa_at": now_iso,
            }
            if v in OK_VERDICTS:
                valid_add.append(pid)
            else:
                failed_add.append(pid)
    return {"valid_add": valid_add, "failed_add": failed_add,
            "verdicts": verdicts, "checked": checked, "parse_error": False,
            "new_meta": new_meta, "per_pid_issues": per_pid_issues}


# ---------------------------------------------------------------------------
# запуск сбора (in-process) и обновление manifest
# ---------------------------------------------------------------------------
def _inproc_gather_cfg(cfg: dict, max_age_s: int) -> dict:
    """gather_cfg для in-process сбора — дефолты прежнего CLI search.

    Демон запускал CLI без большинства опций → действовали дефолты CLI
    search-парсера; те же значения задаём здесь явно, чтобы не разъехаться.
    """
    return {
        "variant": "",
        "max_pages": 12,
        "min_reviews": cfg.get("min_reviews", 40),
        "category": None,
        "no_dom": False,
        "no_variant_mode": False,
        "sorts": ["published_at_desc", "score_asc"],  # DEFAULT_SORTS
        "max_age_s": max_age_s,
        "no_ocr": True,          # --ocr не передаётся демоном
        "threads": 8,
        "no_preproc": False,
        "dedup_replies": True,   # без --no-dedup-replies
        "overwrite": False,
        "min_rating": None,
        "window_mode": cfg.get("window_mode"),
        "scroll_jitter_seed": None,
    }


def _inproc_collect(progress: dict, run_id: str, mode: str,
                    targets=None) -> dict:
    """In-process сбор через collect_parallel/collect_search (без subprocess CLI).

    Эквивалент прежнего `cli.py search --out-raw combined_{run_id}.json`:
    возвращает combined и пишет файл на диск (его потребляют classify_status,
    QA, replay и status-API). Воркеры пишут в единый {campaign}_daemon.log
    (worker_log) и отдают live-телеметрию через on_event.
    """
    import sys as _sys
    if str(ROOT) not in _sys.path:
        _sys.path.insert(0, str(ROOT))
    from ozon_collector import cache as K
    from ozon_collector import search as S

    cfg = progress["run_cfg"]
    camp = progress["campaign"]
    parallel = max(1, int(cfg.get("parallel", 1)))
    max_age_s = K.parse_max_age(cfg.get("max_age", "7d"))
    gather_cfg = _inproc_gather_cfg(cfg, max_age_s)

    session_budget = None
    if cfg.get("session_budget_wall_clock"):
        session_budget = dict(S.SESSION_BUDGET_DEFAULT)
        session_budget["wall_clock_s"] = int(cfg["session_budget_wall_clock"])

    if mode == "rescan":
        pids = list(targets or [])
        limit = len(pids)
        # pids-режим: url не открывается, main-браузер не нужен
        url = f"список из {limit} артикулов"
        discovery = limit
        skip_pids = None
    else:
        pids = None
        url = progress["url"]
        skip_pids = (list(dict.fromkeys(progress.get("valid_pids", [])))
                     or None)
        # полный прогон собирает с запасом target + reserve_count (как в прежнем CLI)
        discovery = progress["target_count"] + int(cfg.get("reserve_count") or 0)
        limit = discovery

    worker_log = str(LOGS_DIR / f"{camp}_daemon.log")

    def _on_event(wctx, msg):
        k = getattr(wctx, "worker_idx", None)
        if isinstance(msg, dict):
            t = msg.get("t", "?")
            detail = msg.get("detail") or None
        else:
            t = msg
            detail = None
        log.info(f"worker{k} [{t}]" + (f" {detail}" if detail else ""))

    max_restarts = int(cfg.get("max_worker_restarts", 2))

    # Сбор ВСЕГДА через collect_parallel (subprocess-воркер), даже при
    # parallel=1: единый путь оркестрации, оркестратор (_await_workers)
    # следит за воркерами (heartbeat, stall-terminate, respawn). Без этого
    # при parallel=1 сбор шёл in-process в демоне и зависший CDP-вызов
    # не мог быть прерван оркестратором (не было отдельного воркера).
    from ozon_collector.parallel import collect_parallel
    combined = collect_parallel(
        url, limit=limit, gather_cfg=gather_cfg, parallel=parallel,
        query=None, max_recycles=2, join_timeout_s=43200.0,
        pids=pids or None, skip_pids=skip_pids, no_bootstrap=False,
        session_budget=session_budget, on_event=_on_event,
        max_worker_restarts=max_restarts, worker_log=worker_log,
        stop_event=_STOP_EVENT, force_event=_FORCE_EXIT)

    combined_file = CAMPAIGNS_DIR / camp / f"combined_{run_id}.json"
    combined_file.parent.mkdir(parents=True, exist_ok=True)
    if combined is not None:
        combined_file.write_text(
            json.dumps(combined, ensure_ascii=False, indent=2),
            encoding="utf-8")
    return combined


def _fail_reason_recurring(progress: dict, pid: str, threshold: int = 2) -> bool:
    """FAIL-причина пида повторилась ≥threshold раз подряд.

    Если брак детерминирован по данным (один и тот же набор issues после
    rescan), дальнейший пересбор бессмыслен — PID сразу идёт в permanent,
    не сжигая остаток бюджета max_rescans.
    """
    info = progress.get("fail_reason_seq", {}).get(pid)
    return bool(info and info.get("count", 0) >= threshold)


def _title_matches(data_dir: str, pid: str, match_query: str) -> bool:
    """True если title товара содержит фразу (регистронезависимо).

    raw отсутствует/битый или title пуст → False (такой товар не проходит
    фильтр, но и в title_mismatch не попадает — решает структура/QA).
    """
    raw = load_json(Path(data_dir) / raw_pid_name(pid))
    if not raw:
        return False
    title = (raw.get("product") or {}).get("title") or ""
    return match_query.lower() in title.lower()


def reconcile_validity(progress: dict, new_checked: dict,
                       data_dir: str,
                       match_query: Optional[str] = None) -> dict:
    """Обновляет valid/failed на основе вердиктов + источник истины (raw-файлы).

    Источник истины для валидности — наличие canonical raw (П1). Вердикт `OK`/`WARN`
    делает raw валидным; `FAIL` — бракованным; отсутствие raw — не-валид.

    match_query — опциональный фильтр точного соответствия названия товара
    (явный --match-title, НЕ автоматически из text= URL): товар валиден,
    только если `raw.product.title` содержит фразу как подстроку
    (без учёта регистра).
    Несоответствующие товары НЕ считаются FAIL (иначе зажгут rescan), а
    попадают в отдельный список title_mismatch_pids (телеметрия).
    Пустой/None match_query — фильтр выключен, поведение как обычно.
    """
    verdicts = dict(new_checked["verdicts"])
    discovered = set(progress.get("discovered_pids", []))
    if match_query is None:
        match_query = progress.get("title_match_query") or ""

    # valid: сохраняем ранее подтверждённые (в исходном порядке) + добавляем
    # вновь подтверждённые, но НЕ держим пиды с ТЕКУЩИМ вердиктом FAIL (защита
    # от накопленного брака — вердикт мог переписаться при rescan-перепроверке).
    # И НЕ держим пиды без canonical raw (источник истины — П1): если raw удалён
    # после QA, старый вердикт OK не должен удерживать PID в valid (mtime/наличие).
    # Порядок важен: snapshot/reserve (target_reached) выбираются по нему.
    valid = list(dict.fromkeys(progress.get("valid_pids", [])))
    seen = set(valid)
    for p in new_checked.get("valid_add", []):
        if p not in seen:
            valid.append(p)
            seen.add(p)
    valid = [p for p in valid
             if verdicts.get(p) != "FAIL" and has_raw(data_dir, p)]

    # title-фильтр (exact-match, регистронезависимо). Применяется ПОСЛЕ
    # QA-валидности: товар может быть OK по QA, но не соответствовать фразе.
    # Несоответствие НЕ является браком (failed) — пид просто не валиден
    # для этого поискового запроса и не раздувает rescan-бюджет.
    title_mismatch = list(
        dict.fromkeys(progress.get("title_mismatch_pids", [])) or [])
    if match_query:
        mismatch = []
        for p in valid:
            if not _title_matches(data_dir, p, match_query):
                mismatch.append(p)
        title_mismatch = list(dict.fromkeys(title_mismatch + mismatch))
        valid = [p for p in valid
                 if p not in set(mismatch)]
    progress["title_mismatch_pids"] = title_mismatch
    progress["valid_pids"] = valid

    # брак: пересчитываем из ТЕКУЩИХ вердиктов (не накапливаем) — если пид
    # после rescan стал валидным, его вердикт OK → он выходит из failed.
    failed = {pid for pid in discovered if verdicts.get(pid) == "FAIL"}
    progress["failed_pids"] = normalize_pids(failed)
    progress["verdicts"] = verdicts
    # фиксируем fingerprint canonical raw НА МОМЕНТ QA (verdict_meta) — для
    # инвалидации QA-кэша (qa_cache_valid). Кто проверился в этом цикле —
    # запоминаем; прочие не трогаем.
    if new_checked.get("new_meta"):
        vm = dict(progress.get("verdict_meta", {}))
        vm.update(new_checked["new_meta"])
        progress["verdict_meta"] = vm

    # история повторного брака: если пид снова FAIL с ТОЙ ЖЕ причиной (набор
    # issues), счётчик растёт → rescan пропускается (см. _fail_reason_recurring).
    seq = dict(progress.get("fail_reason_seq", {}))
    for pid, codes in (new_checked.get("per_pid_issues") or {}).items():
        if verdicts.get(pid) == "FAIL":
            key = tuple(sorted(codes)) or ("UNKNOWN",)
            prev = seq.get(pid)
            if prev and prev.get("key") == key:
                seq[pid] = {"key": key, "count": prev.get("count", 1) + 1}
            else:
                seq[pid] = {"key": key, "count": 1}
        else:
            seq.pop(pid, None)  # стал OK/WARN → история сбрасывается
    progress["fail_reason_seq"] = seq
    return progress


# ---------------------------------------------------------------------------
# главный цикл
# ---------------------------------------------------------------------------

def run_campaign(campaign: str, url: str, target: int, min_valid: int,
                 out: str, data_dir: str = DEFAULT_DATA_DIR,
                 max_age: str = "7d", min_reviews: int = 40,
                 parallel: int = 1, max_no_progress_runs: int = 3,
                 max_rescans: int = 2, max_total_hours: float = 12.0,
                 reserve_count: int = 30, notify_mode: str = "off",
                 challenge_pause: int = CHALLENGE_RETRY_DELAY_S,
                 force_new: bool = False, force_reset: bool = False,
                 max_transient_retries: int = 3,
                 max_worker_restarts: int = 2,
                 session_budget_wall_clock: Optional[int] = None,
                 window_mode: Optional[str] = None,
                 match_title: Optional[str] = None,
                 write_digest: bool = True,
                 qa_fn: Optional[Callable[[list[str]], dict]] = None) -> dict:
    lock = PidLock(DATA_DIR / "key_daemon.pid")
    if not lock.acquire(campaign):
        raise SystemExit("key_daemon уже запущен (pid-файл)")

    # Единый логгер демона: оркестратор — владелец файла кампании и ротации.
    # Воркеры (collect_parallel) получают тот же путь через task["worker_log"]
    # и пишут в него в append-режиме без ротации.
    log.attach_file(LOGS_DIR / f"{campaign}_daemon.log", mode="a",
                    rotate_max_bytes=5 * 1024 * 1024)
    log.set_context("orchestrator")

    camp_dir = CAMPAIGNS_DIR / campaign
    camp_dir.mkdir(parents=True, exist_ok=True)
    progress_path = camp_dir / "progress.json"

    # --reset: полный сброс существующей кампании. Каталог кампании удаляется
    # целиком (progress.json, status.json, events.jsonl, runs/), затем демон
    # стартует как НОВАЯ кампания (fresh=True) — с требованием --url и
    # пустыми discovered/valid. Сырой кеш товаров (data/*_raw*.json) общий
    # для всех кампаний и специально НЕ трогается.
    if force_reset and progress_path.is_file():
        log.warning(f"key_daemon: --reset: удаляю существующую кампанию "
                    f"'{campaign}' ({camp_dir}) — начинаю заново")
        shutil.rmtree(camp_dir, ignore_errors=True)
        camp_dir.mkdir(parents=True, exist_ok=True)
        progress_path = camp_dir / "progress.json"

    existing = load_json(progress_path)
    fresh = existing is None
    progress = existing or default_manifest(
        campaign, target, min_valid, url, out,
        data_dir=data_dir, max_age=max_age, min_reviews=min_reviews,
        parallel=parallel, max_no_progress_runs=max_no_progress_runs,
        max_rescans=max_rescans, max_total_hours=max_total_hours,
        reserve_count=reserve_count, notify=notify_mode,
        challenge_pause=challenge_pause,
        max_transient_retries=max_transient_retries,
        max_worker_restarts=max_worker_restarts,
        session_budget_wall_clock=session_budget_wall_clock,
        window_mode=window_mode)

    # Валидация для НОВОЙ кампании (при resume manifest уже согласован).
    if fresh:
        validate_new_campaign(min_valid, target)
        if not url:
            raise SystemExit(
                "key_daemon: для новой кампании нужен --url (или возобновите "
                "существующую кампанию)")
    else:
        # ---- resume-защита от изменения identity (campaign_identity) ----
        # URL/схема зафиксированы при создании кампании; менять их между
        # запусками нельзя (иначе демон молча продолжит другую категорию).
        ident = progress.get("campaign_identity") or {}
        if url and not force_new:
            url_hash = _sha256_text(url)
            stored_hash = ident.get("url_sha256")
            if stored_hash and stored_hash != url_hash:
                raise SystemExit(
                    f"key_daemon: кампания '{campaign}' уже существует с URL, "
                    f"отличным от переданного --url (url_sha256 не совпадает). "
                    f"Новая категория → используйте --campaign другое имя "
                    f"или --force-new для принудительного пересоздания.")
        if not force_new and ident.get("schema_version") \
                and ident.get("schema_version") != SCHEMA_VERSION:
            raise SystemExit(
                f"key_daemon: схема кампании {ident.get('schema_version')} != "
                f"текущей {SCHEMA_VERSION}. Обновите коллектор или --force-new.")

    # Фильтр точного соответствия названия товара запросу. Фраза берётся ТОЛЬКО
    # из явного --match-title — автоматически из text= URL НЕ подхватывается:
    # поисковый запрос описывает намерение покупателя, а не название товара
    # (инцидент podarok_five: авто-включение отсекало всю выдачу → valid=0 и
    # бесконечный пересбор). Пустая фраза → фильтр выключен (сбор как обычно).
    # Значение фиксируется в манифесте при создании кампании; при resume строго
    # берётся из манифеста (источник истины), параметр запуска не переопределяет.
    if fresh:
        progress["title_match_query"] = (match_title or "").strip()

    try:
        # При resume конфиг берём из манифеста (источник истины), а не из текущих
        # аргументов запуска — чтобы лимиты/дедлайны не менялись между запусками.
        rcfg = progress.get("run_cfg", {})
        max_total_hours = rcfg.get("max_total_hours", max_total_hours)
        max_no_progress_runs = rcfg.get("max_no_progress_runs",
                                        max_no_progress_runs)
        max_rescans = rcfg.get("max_rescans", max_rescans)
        reserve_count = rcfg.get("reserve_count", reserve_count)

        # session_budget_wall_clock: если передан аргументом запуска, синхронизируем
        # в run_cfg (позволяет менять лимит run между запусками, сохраняя
        # остальные лимиты из манифеста).
        if session_budget_wall_clock is not None:
            rcfg["session_budget_wall_clock"] = session_budget_wall_clock
            progress["run_cfg"] = rcfg
        if window_mode is not None:
            rcfg["window_mode"] = window_mode
            progress["run_cfg"] = rcfg

        # run_cfg_hash: фиксируем fingerprint конфигурации — ручное изменение
        # manifest'а (напр. max_total_hours) помечается, а не молча применяется.
        cfg_hash = _sha256_text(
            json.dumps(progress.get("run_cfg", {}), sort_keys=True,
                       ensure_ascii=False, default=str))
        stored_hash = progress.get("run_cfg_hash")
        if stored_hash and stored_hash != cfg_hash:
            progress["run_cfg_modified"] = True
            _emit_event(progress, "MANIFEST_RUN_CFG_MODIFIED")
        progress["run_cfg_hash"] = cfg_hash

        started_ts = datetime.fromisoformat(progress["started_at"]).timestamp()
        _set_state(progress, "STARTING", "DAEMON_STARTED",
                   fresh=fresh, target=target)
        _recover_interrupted_run(progress, data_dir, qa_fn=qa_fn)
        save_json(progress_path, progress)

        while True:
            # ---- терминальные и лимитные проверки ----
            # Кооперативный стоп по Ctrl+C: зафиксировали final_state=INTERRUPTED,
            # НЕ трогаем no_progress/crash-счётчики; цикл выходит, финализация
            # пропускается (см. finalize_campaign), прогресс сохранён ниже.
            if _STOP_EVENT.is_set():
                progress["final_state"] = "INTERRUPTED"
                progress["final_reason"] = "user_interrupt"
                progress["final_note"] = "остановлено пользователем (Ctrl+C)"
                log.warning("демон: пользователь остановил кампанию (Ctrl+C) → "
                            "INTERRUPTED")
                break
            if progress.get("final_state") and progress["final_state"] in TERMINAL_STATES:
                break
            if time.time() - started_ts >= max_total_hours * 3600:
                progress["final_reason"] = "time_limit"
                progress["final_note"] = f"лимит времени {max_total_hours}ч исчерпан"
                break
            if progress.get("no_progress_count", 0) >= max_no_progress_runs:
                progress["final_reason"] = "no_progress"
                progress["final_note"] = "лимит no-progress подряд исчерпан"
                break

            # ---- cooldown (не пересекает max-total-hours: жёсткий дедлайн) ----
            # Эффективный cooldown = максимум из storm-cooldown и profile_cooldown
            # (ADR §14.4): profile_cooldown.until блокирует старт следующего run.
            if not _NO_COOLDOWN:
                cu = progress.get("cooldown_until")
                pc_until = (progress.get("profile_cooldown") or {}).get("until")
                if pc_until:
                    if cu is None or datetime.fromisoformat(pc_until).timestamp() \
                            > datetime.fromisoformat(cu).timestamp():
                        cu = pc_until
                if cu:
                    remaining = (max_total_hours * 3600) - (time.time() - started_ts)
                    if remaining <= 0:
                        progress["final_reason"] = "time_limit"
                        progress["final_note"] = (f"лимит времени {max_total_hours}ч "
                                                  f"исчерпан (cooldown)")
                        break
                    wait = datetime.fromisoformat(cu).timestamp() - time.time()
                    if wait > 5:
                        sleep_for = min(wait, remaining)
                        log.info(f"cooldown: жду {int(sleep_for)}с")
                        # stop-осведомлённый sleep: опрашиваем Ctrl+C, чтобы
                        # долгий cooldown не блокировал graceful shutdown
                        _ended = time.monotonic()
                        _deadline = _ended + sleep_for
                        while time.monotonic() < _deadline:
                            if _STOP_EVENT.is_set():
                                break
                            time.sleep(min(1.0, _deadline - time.monotonic()))
                        # если проспали ровно до дедлайна — выходим по time_limit
                        if sleep_for >= remaining:
                            progress["final_reason"] = "time_limit"
                            progress["final_note"] = (f"лимит времени {max_total_hours}ч "
                                                      f"исчерпан (cooldown)")
                            break
                        continue
                    progress["cooldown_until"] = None
            else:
                # KD_NO_COOLDOWN: сбрасываем любой протухший cooldown сразу.
                progress["cooldown_until"] = None
                if (progress.get("profile_cooldown") or {}).get("until"):
                    progress["profile_cooldown"]["until"] = None

            # ---- выбрать режим прогона ----
            mode = "full"
            targets = None
            failed = [p for p in progress.get("failed_pids", [])
                      if p not in set(progress.get("permanently_failed_pids", []))]
            rescans = progress.get("running_rescans", {})
            pending = [p for p in failed
                       if rescans.get(p, 0) < max_rescans
                       and not _fail_reason_recurring(progress, p)]
            # брак с повторной идентичной причиной — пересбор бессмыслен
            recurring = [p for p in failed
                         if _fail_reason_recurring(progress, p)]
            if recurring:
                progress["permanently_failed_pids"] = normalize_pids(
                    set(progress.get("permanently_failed_pids", [])) | set(recurring))
                progress["failed_pids"] = [
                    p for p in progress.get("failed_pids", []) if p not in recurring]
                log.warning(f"детерминированный брак → permanently_failed "
                            f"(повторная причина): {len(recurring)}")
            if pending:
                mode = "rescan"
                targets = pending
                # фиксируем попытку rescan СЕЙЧАС за каждый пид (иначе счётчик
                # никогда не растёт и max_rescans не ограничивает повторения)
                for p in pending:
                    rescans[p] = rescans.get(p, 0) + 1
                progress["running_rescans"] = rescans
                attempts = [rescans[p] for p in pending[:10]]
                log.warning(f"rescan: {len(pending)} бракованных, попытки {attempts}"
                            f"{'...' if len(pending) > 10 else ''}")
            elif failed:
                progress["permanently_failed_pids"] = normalize_pids(
                    set(progress.get("permanently_failed_pids", [])) | set(failed))
                progress["failed_pids"] = []
                log.warning(f"бракованные переведены в permanently_failed: {len(failed)}")

            run_id = f"run_{int(time.time())}_{len(progress.get('runs', []))}"
            before_disc = set(progress.get("discovered_pids", []))
            before_valid = set(progress.get("valid_pids", []))
            progress["current_mode"] = mode
            progress["current_run"] = run_id
            _set_state(progress, "RUNNING_RESCAN" if mode == "rescan"
                       else "RUNNING_FULL", "RUN_STARTED",
                       run_id=run_id, mode=mode)
            _journal_stage(progress, run_id, "run_started")

            log.info(f"[{run_id}] in-process сбор ({mode}) старт")
            try:
                combined = _inproc_collect(progress, run_id, mode, targets)
                exit_code = 0
            except CollectionInterrupted as exc:
                # FORCE (2-й Ctrl+C) во время докрутки ссылок: воркеры не
                # спавнились, пользователь требует немедленного выхода.
                # НЕ классифицируем как CRASH/UNKNOWN_FAILURE — emergency exit.
                log.error("демон: принудительная остановка (2-й Ctrl+C) во время "
                          "сбора — экстренное завершение")
                _emergency_shutdown(progress_path, force="collection")
                # reachable только если _emergency_shutdown mocked/вернулся:
                # не даём упасть в unbound exit_code / CRASH-ветку ниже.
                raise
            except Exception as exc:
                log.error(f"in-process сбор упал: {exc}")
                # combined «пуст, но есть» + exit!=0 → UNKNOWN_FAILURE: прежнее
                # поведение subprocess (combined записан, код ненулевой) →
                # транзиентные ретраи → NEEDS_ATTENTION, а не тихий ABANDONED.
                combined = {"products": [], "found": 0, "inproc_crash": str(exc)}
                exit_code = 1

            status = classify_status(exit_code, combined)
            _journal_stage(progress, run_id, "run_completed",
                           {"status": status, "exit_code": exit_code})
            _absorb_recovery_telemetry(progress, combined)

            # FORCE (2-й Ctrl+C) в worker-фазе: _await_workers уже принудительно
            # завершил воркеров (зачистка их браузеров в родителе). Не уходим в
            # graceful INTERRUPTED/сохранение — экстренный выход (семантика force).
            if _FORCE_EXIT.is_set():
                _emergency_shutdown(progress_path, force="workers")

            # Ctrl+C во время сбора: collect_parallel вернулся с уже собранным
            # payload (воркеры сворачиваются). Сохраняем, что реально собрано
            # (discovery/QA/reconcile по частичным данным), НЕ трогая счётчики
            # статуса/шторма, и выходим на INTERRUPTED (обработается на верху
            # цикла). PROGRESS_EVERY=5 даёт payload даже у прерванных воркеров.
            if _STOP_EVENT.is_set():
                log.warning("демон: сбор прерван Ctrl+C — сохраняю частичный "
                            "результат этого прогона")
                discovered = extract_discovered_pids(combined)
                ok_pids = extract_ok_pids(combined)
                if discovered:
                    progress["discovered_pids"] = normalize_pids(
                        list(progress.get("discovered_pids", [])) + list(discovered))
                if ok_pids:
                    new_checked = classify_new_pids(progress, ok_pids, qa_fn=qa_fn)
                    reconcile_validity(progress, new_checked, data_dir)
                _journal_stage(progress, run_id, "state_committed",
                               {"interrupted": True})
                progress["final_state"] = "INTERRUPTED"
                progress["final_reason"] = "user_interrupt"
                progress["final_note"] = "остановлено пользователем (Ctrl+C)"
                save_json(progress_path, progress)
                break

            discovered = extract_discovered_pids(combined)
            ok_pids = extract_ok_pids(combined)
            # детерминированный порядок (нормализация дедуплицирует, сохраняя порядок)
            progress["discovered_pids"] = normalize_pids(
                list(progress.get("discovered_pids", [])) + list(discovered))

            # не собираем брак напрямую: новые кандидаты → QA
            # в rescan-режиме сбрасываем вердикты целей, чтобы их ПЕРЕпроверить
            # (иначе already-verdict не попадёт в need и raw после починки не
            # пере-QA-нётся — пид навсегда останется в failed)
            if mode == "rescan" and targets:
                vdict = dict(progress.get("verdicts", {}))
                vmeta = dict(progress.get("verdict_meta", {}))
                for p in targets:
                    vdict.pop(p, None)
                    vmeta.pop(p, None)
                progress["verdicts"] = vdict
                progress["verdict_meta"] = vmeta
            new_checked = classify_new_pids(progress, ok_pids, qa_fn=qa_fn)
            if new_checked.get("parse_error"):
                progress["final_state"] = "NEEDS_ATTENTION"
                progress["final_reason"] = "qa_parse_error"
                progress["final_note"] = "QA-сводка недостоверна (не сошлись инварианты)"
                notify(progress, "NEEDS_ATTENTION: QA-сводка недостоверна")
                log.error("QA parse_error → NEEDS_ATTENTION")
                save_json(progress_path, progress)
                break
            reconcile_validity(progress, new_checked, data_dir)

            new_disc = delta(before_disc, progress.get("discovered_pids", []))
            new_valid = delta(before_valid, progress.get("valid_pids", []))
            # Прогресс есть → сбрасываем ОБЩИЙ счётчик (шторм/краши больше не фон).
            # Иначе после серии штормов без прогресса, а затем успеха, no_progress
            # останется высоким → ложный ABANDONED.
            if new_disc or new_valid:
                progress["no_progress_count"] = 0
                progress["challenge_seq"] = 0
                # прогресс → шкала эскалации challenge сбрасывается
                cc = progress.get("challenge_control") or {}
                cc["storm_streak"] = 0
                progress["challenge_control"] = cc
            progress["runs"] = progress.get("runs", []) + [{
                "run_id": run_id, "mode": mode, "status": status,
                "exit_code": exit_code, "before_count": len(before_disc),
                "after_count": len(progress.get("discovered_pids", [])),
                "before_valid": len(before_valid),
                "after_valid": len(progress.get("valid_pids", [])),
                "new_discovered": sorted(new_disc),
                "new_valid": sorted(new_valid),
                "qa_checked": new_checked["checked"],
            }]

            # ---- разделённые счётчики прогресса и стагнации ----
            # progress — что дал этот прогон; stall — по ТИПУ отсутствия прогресса
            # (выдача кончилась / challenge / crash / какой-то прогресс был).
            prog = dict(progress.get("progress", {}))
            prog["new_discovered"] = prog.get("new_discovered", 0) + len(new_disc)
            prog["new_valid"] = prog.get("new_valid", 0) + len(new_valid)
            prog["rescans_fixed"] = prog.get("rescans_fixed", 0) + (
                len(new_valid) if mode == "rescan" else 0)
            prog["rescans_failed"] = prog.get("rescans_failed", 0) + (
                len(new_disc) - len(new_valid) if mode == "rescan" else 0)
            progress["progress"] = prog

            stall = dict(progress.get("stall", {}))
            if status == "NORMAL_DONE" and not new_disc and not new_valid:
                # выдача перетёрлась известными — стагнация discovery/valid
                stall["full_runs_without_discovery"] = \
                    stall.get("full_runs_without_discovery", 0) + 1
                if mode == "full":
                    stall["full_runs_without_valid"] = \
                        stall.get("full_runs_without_valid", 0) + 1
            if status == "CHALLENGE_STORM":
                stall["challenge_runs"] = stall.get("challenge_runs", 0) + 1
            if status == "CRASH":
                stall["crash_runs"] = stall.get("crash_runs", 0) + 1
            progress["stall"] = stall

            log.info(f"[{run_id}] status={status}: discovered "
                f"{len(progress.get('discovered_pids', []))} "
                f"valid {len(progress.get('valid_pids', []))} "
                f"(new_disc={len(new_disc)}, new_valid={len(new_valid)})")

            # ---- реакция по статусу ----
            if status == "CHALLENGE_STORM":
                progress["challenge_seq"] = progress.get("challenge_seq", 0) + 1
                # Агрегация storm (ADR §14.6): шторм ОДНОГО воркера ≠ шторм всей
                # кампании. Только ФУЛЛ-шторм (parallel==1, либо ВСЕ воркеры
                # упёрлись) эскалирует storm_streak/profile_cooldown. Частичный
                # шторм (часть воркеров прогрессирует) — короткая пауза, БЕЗ
                # эскалации, чтобы один воркер не сжигал cooldown кампании.
                aborted = (combined or {}).get("aborted") or {}
                all_stormed = bool(parallel == 1 or aborted.get("all_workers"))
                stormed_workers = aborted.get("workers_stormed") or []
                if not new_disc and not new_valid:
                    progress["no_progress_count"] = progress.get(
                        "no_progress_count", 0) + 1
                else:
                    progress["challenge_seq"] = 0
                # Adaptive cooldown (ADR §14.5): эскалация по storm_streak.
                # storm_streak растёт ТОЛЬКО после исчерпанного ФУЛЛ-шторма
                # (SESSION_BLOCKED → combined.aborted). 1м → 5м → 15м → ...
                signature = (progress.get("recovery_telemetry") or {}).get(
                    "last_challenge_signature")
                cc = progress.get("challenge_control") or {}
                if all_stormed:
                    cc = _bump_challenge_control(progress, signature)
                    cc["cooldown_until"] = (datetime.now(timezone.utc) +
                                            timedelta(seconds=CHALLENGE_COOLDOWN_STEPS_S[-1])).isoformat(
                                                timespec="seconds")
                    # profile_cooldown (ADR §14.4): отдельно от storm — почему стоит
                    if not _NO_COOLDOWN:
                        pc = progress.get("profile_cooldown") or {}
                        pc["until"] = cc["cooldown_until"]
                        pc["reason"] = "repeated_challenge"
                        pc["challenge_count"] = cc.get("total_storms", 0)
                        pc["last_signature"] = signature
                        progress["profile_cooldown"] = pc
                # Эскалация: при длинной серии штормов — NEEDS_ATTENTION
                # (не молотить), иначе adaptive пауза с джиттером.
                # KD_NO_COOLDOWN: не останавливаем кампанию, продолжаем обходить.
                max_storm = len(CHALLENGE_COOLDOWN_STEPS_S)
                if (not _NO_COOLDOWN and all_stormed
                        and cc.get("storm_streak", 0) > max_storm):
                    progress["final_state"] = "NEEDS_ATTENTION"
                    progress["final_reason"] = "challenge_storm"
                    progress["final_note"] = (
                        f"persistent challenge ({cc['storm_streak']} штормов)")
                    log.error(f"persistent challenge → NEEDS_ATTENTION "
                        f"(storm_streak={cc['storm_streak']})")
                    _set_state(progress, "NEEDS_ATTENTION", "CHALLENGE_STORM",
                               run_id=run_id, storm_streak=cc["storm_streak"])
                    notify(progress, "NEEDS_ATTENTION: persistent challenge")
                    save_json(progress_path, progress)
                    break
                pause_s = (1 if _NO_COOLDOWN else (
                    _challenge_cooldown_seconds(progress) if all_stormed
                    else CHALLENGE_PARTIAL_PAUSE_S))
                progress["cooldown_until"] = (
                    datetime.now(timezone.utc) +
                    timedelta(seconds=pause_s)).isoformat(timespec="seconds")
                log.warning(f"challenge detected → adaptive pause {int(pause_s)}с "
                    f"(storm_streak={cc['storm_streak']}) "
                    f"{'ФУЛЛ' if all_stormed else 'ЧАСТИЧНЫЙ'}"
                    f"{' ' + str(stormed_workers) if stormed_workers else ''}, "
                    f"challenge_seq={progress['challenge_seq']}")
                _set_state(progress, "COOLDOWN_CHALLENGE", "CHALLENGE_STORM",
                           run_id=run_id, pause=int(pause_s),
                           storm_streak=cc["storm_streak"],
                           challenge_seq=progress['challenge_seq'])
                notify(progress, f"challenge: пауза {int(pause_s)}с "
                       f"(storm #{cc['storm_streak']}, "
                       f"{'ФУЛЛ' if all_stormed else 'частичный'})")

            elif status == "CRASH":
                progress["no_progress_count"] = progress.get(
                    "no_progress_count", 0) + 1
                # короткий cooldown, чтобы не зациклить мгновенные краши без паузы
                crash_pause = 1 if _NO_COOLDOWN else BACKOFF_DELAYS_S[0]
                progress["cooldown_until"] = (
                    datetime.now(timezone.utc) +
                    timedelta(seconds=crash_pause)).isoformat(
                        timespec="seconds")
                log.warning(f"crash сбора (exit!=0, нет combined), cooldown "
                    f"{crash_pause}с")
                _set_state(progress, "COOLDOWN_CRASH", "CRASH",
                           run_id=run_id, exit_code=exit_code)

            elif status == "UNKNOWN_FAILURE":
                # Транзиентный retry: временные ошибки сбора (PermissionError,
                # IO, задержка combined, сбой отчёта) НЕ должны ронять 12-часовую
                # кампанию в NEEDS_ATTENTION. Retry с коротким cooldown до
                # max_transient_retries, затем — терминально. Малиформированный
                # combined/непредвиденное содержимое НЕ ретраится (см. выше).
                tr = progress.get("transient_failures", 0)
                max_tr = rcfg.get("max_transient_retries", 3)
                if tr < max_tr:
                    progress["transient_failures"] = tr + 1
                    progress["cooldown_until"] = (
                        datetime.now(timezone.utc) +
                        timedelta(seconds=TRANSIENT_RETRY_BACKOFF_S)).isoformat(
                            timespec="seconds")
                    progress["last_error"] = (
                        f"UNKNOWN_FAILURE (exit={exit_code}); retry "
                        f"{progress['transient_failures']}/{max_tr}")
                    log.warning(f"UNKNOWN_FAILURE → transient retry "
                        f"{progress['transient_failures']}/{max_tr}, пауза "
                        f"{TRANSIENT_RETRY_BACKOFF_S}с")
                    _set_state(progress, "COOLDOWN_CRASH", "TRANSIENT_RETRY",
                               run_id=run_id, attempt=progress["transient_failures"],
                               max_attempts=max_tr)
                else:
                    progress["final_state"] = "NEEDS_ATTENTION"
                    progress["final_reason"] = "unknown_failure"
                    progress["final_note"] = ("непредвиденная ошибка сбора после "
                                              f"{max_tr}+ транзиентных ретраев")
                    _set_state(progress, "NEEDS_ATTENTION", "FATAL",
                               run_id=run_id, reason="unknown_failure")
                    notify(progress, "NEEDS_ATTENTION: непредвиденная ошибка сбора")
                    save_json(progress_path, progress)
                    break

            elif status == "NORMAL_DONE":
                # Стагнация: прогон завершился, но не дал ни одного нового пида
                # (ни новых discovery, ни новых valid). Неважно, был ли found>0 —
                # выдача перетирается известными товарами, дальше греться некуда.
                # Иначе при живой выдаче без роста демон крутился бы вечно.
                if not new_disc and not new_valid:
                    progress["no_progress_count"] = progress.get(
                        "no_progress_count", 0) + 1

            # ---- достижение цели ----
            if len(set(progress.get("valid_pids", []))) >= target:
                # детерминированный порядок (dict.fromkeys сохраняет порядок
                # valid_pids, а set() давал hash-порядок → на resume snapshot
                # выбирался бы по-другому)
                ordered = list(dict.fromkeys(progress["valid_pids"]))
                snapshot = ordered[:target]
                reserve = ordered[target: target + reserve_count]
                progress["campaign_final_pids"] = snapshot
                progress["campaign_reserve_pids"] = reserve
                progress["final_reason"] = "target_reached"
                _journal_stage(progress, run_id, "qa_completed",
                               {"count": len(new_checked["checked"])})
                _journal_stage(progress, run_id, "state_committed")
                _set_state(progress, "FINALIZING", "TARGET_REACHED",
                           valid=len(set(progress["valid_pids"])))
                # фиксируем snapshot в манифесте СРАЗУ — при resume не
                # пересобираем его из текущего множества valid (immutable)
                save_json(progress_path, progress)
                break

            _journal_stage(progress, run_id, "qa_completed",
                           {"count": len(new_checked["checked"])})
            _journal_stage(progress, run_id, "state_committed")
            save_json(progress_path, progress)

        finalize_campaign(progress, progress_path, qa_fn=qa_fn)
        fs = progress.get("final_state")
        fr = progress.get("final_reason")
        msg = f"финал кампании: {fs}"
        if fs in ("COMPLETED_MIN", "ABANDONED") and fr:
            msg += f" / {fr}"
        _set_state(progress, fs, "FINALIZED", reason=fr)
        if write_digest and fs != "INTERRUPTED":
            # на INTERRUPTED данные неполные — дайджест не рендерим
            _write_digest(progress, data_dir)
        notify(progress, msg)
        return progress

    finally:
        lock.release()


def _write_digest(progress: dict, data_dir: str) -> None:
    """Markdown-дайджест собранной выдачи (по финальным accepted PID).

    Рендерит из лучших валидных raw на каждый pid (report.build_search_digest_combined
    + write_search_digest) в progress["out"]. Ничего не делает, если accepted
    PID пустые (например, ABANDONED без данных).
    """
    from ozon_collector.report import (build_search_digest_combined,
                                       write_search_digest)
    pids = list(progress.get("campaign_accepted_pids")
                or progress.get("campaign_final_pids") or [])
    if not pids:
        return
    out = progress.get("out") or f"docs/{progress['campaign']}.md"
    combined, _table = build_search_digest_combined(pids, data_dir)
    written = write_search_digest(combined, out)
    for path in written:
        log.info(f"дайджест сохранён: {path}")


def finalize_campaign(progress: dict, progress_path: Path,
                      qa_fn: Optional[Callable[[list[str]], dict]] = None) -> None:
    """Финальный QA по snapshot и выставление final_state (K1).

    Приоритет статусов:
      NEEDS_ATTENTION  — терминальный, финализатором НЕ перезаписывается и НЕ
                         прогоняет финальный QA (нет доверия к данным).
      COMPLETED        — набрано target ЧИСТЫХ pid (по финальному QA).
      COMPLETED_MIN    — min_valid <= чистых < target (мягкие остановки).
      ABANDONED        — чистых < min_valid.

    Для target_reached: snapshot = target + резерв (campaign_reserve_pids).
    Резерв ТОЖЕ проходит финальный QA (не по старым вердиктам). Бракованные
    из snapshot заменяются чистыми из резерва, пока не наберётся target чистых
    или не исчерпан резерв.
    """
    qa_fn = qa_fn or run_qa_batch
    target = progress["target_count"]
    min_valid = progress.get("min_valid", 0)
    reason = progress.get("final_reason")

    # NEEDS_ATTENTION / INTERRUPTED / уже решённая критическая остановка — не трогаем.
    # INTERRUPTED: стоп по Ctrl+C, данные неполные — финальный QA по snapshot
    # недостоверен, пропускаем (как NEEDS_ATTENTION), прогресс уже сохранён.
    if progress.get("final_state") in ("NEEDS_ATTENTION", "INTERRUPTED"):
        save_json(progress_path, progress)
        return

    def _qa_clean(pids: list[str]) -> tuple[list[str], int, list[dict], bool]:
        """Финальный QA по pids (батчами). Возвращает (чистые, total_fail, batch-лог, parse_error).

        parse_error=True при недостоверной сводке батча (нарушены K4-инварианты) —
        в этом случае вердикты НЕдостоверны (per_pid может быть неполным) и тихо
        принимать отсутствующих как OK нельзя → кампания уходит в NEEDS_ATTENTION.
        """
        clean: list[str] = []
        total_fail = 0
        qa_batches = []
        batches = batch_pids(pids, QA_BATCH_SIZE)
        n = len(batches)
        for bi, batch in enumerate(batches, 1):
            qa = qa_fn(batch)
            per_pid = qa.get("per_pid") or {}
            if not qa_summary_consistent(qa, len(batch)):
                log.error(f"финальный QA batch {bi}/{n}: недостоверная сводка "
                          f"(K4) → NEEDS_ATTENTION")
                return [], 0, [], True
            if qa.get("fail", 0) > 0:
                missing = [p for p in batch if p not in per_pid]
                if missing:
                    log.error(f"финальный QA batch {bi}/{n}: FAIL, но per_pid не "
                              f"покрывает все PID → NEEDS_ATTENTION")
                    return [], 0, [], True
            fails = qa.get("fail", 0)
            total_fail += fails
            qa_batches.append({"batch": bi, "size": len(batch),
                               "fail": fails, "ok": qa.get("ok", 0),
                               "warn": qa.get("warn", 0)})
            log.info(f"финальный QA batch {bi}/{n}: FAIL={fails}")
            for pid in batch:
                v = per_pid.get(pid, "OK")  # OK только при доказанных инвариантах
                if v in OK_VERDICTS:
                    clean.append(pid)
        return clean, total_fail, qa_batches, False

    def _attention_parse_error() -> None:
        """Терминальная остановка при недостоверном финальном QA."""
        progress["final_state"] = "NEEDS_ATTENTION"
        progress["final_reason"] = "qa_parse_error"
        progress["final_note"] = "финальный QA недостоверен (нарушены K4-инварианты)"
        notify(progress, "NEEDS_ATTENTION: финальный QA недостоверен")
        save_json(progress_path, progress)

    if reason == "target_reached":
        # ---- целевой путь: snapshot + резерв, замена брака из резерва ----
        snapshot = list(progress.get("campaign_final_pids") or [])
        reserve = list(progress.get("campaign_reserve_pids") or [])
        pool = list(dict.fromkeys(snapshot + reserve))
        clean, total_fail, qa_batches, parse_error = _qa_clean(pool)
        if parse_error:
            _attention_parse_error()
            return

        # бракованные pid в пуле (не прошли финальный QA)
        reserve_clean = [p for p in reserve if p in clean]

        # чистые из стартового снапшота; недобор до target докомплектовываем
        # чистыми из резерва.
        clean_from_snapshot = [p for p in snapshot if p in clean]
        failed_in_snapshot = [p for p in snapshot if p not in clean]
        shortfall = max(0, target - len(clean_from_snapshot))
        replacements = reserve_clean[:shortfall]
        final_snapshot = list(clean_from_snapshot) + replacements

        # replacement_pids — массив объектов {failed_pid, replaced_by}
        replacement_pairs = [
            {"failed_pid": failed_in_snapshot[i],
             "replaced_by": replacements[i]}
            for i in range(len(replacements))
        ]

        progress["campaign_final_pids"] = final_snapshot[:target]
        progress["campaign_accepted_pids"] = final_snapshot[:target]
        progress["campaign_reserve_pids"] = reserve_clean[len(replacements):]
        progress["replacement_pids"] = replacement_pairs
        progress["qa"] = {"batches": qa_batches, "total_fail": total_fail,
                          "checked_pids": len(pool)}
        n_clean = len(clean)

        # Цель — собрать target ЧИСТЫХ pid. Брак в snapshot заменяется чистыми
        # из резерва, поэтому решает именно число чистых в пуле, а не total_fail.
        # COMPLETED_MIN для target_reached НЕдопустим: демон уже доказал, что
        # пытался выйти на полный target, и недоукомплектация после замен —
        # это неудача финального восстановления → ABANDONED.
        if n_clean >= target:
            progress["final_state"] = "COMPLETED"
            progress["final_reason"] = reason
            progress["final_note"] = None
        else:
            progress["final_state"] = "ABANDONED"
            progress["final_reason"] = reason
            progress["final_note"] = (
                f"после финального QA и замен чистых {n_clean} < target {target}; "
                f"брак {total_fail} из {len(pool)}")
        save_json(progress_path, progress)
        return

    # ---- мягкие остановки (no_progress / time_limit / exhausted): без резерва ----
    snapshot = list(progress.get("campaign_final_pids") or list(
        dict.fromkeys(progress.get("valid_pids", [])))[:target])
    clean, total_fail, qa_batches, parse_error = _qa_clean(snapshot)
    if parse_error:
        _attention_parse_error()
        return
    progress["qa"] = {"batches": qa_batches, "total_fail": total_fail,
                      "checked_pids": len(snapshot)}
    progress["campaign_accepted_pids"] = clean[:target]

    valid = len(clean)
    if total_fail == 0 and valid >= target:
        progress["final_state"] = "COMPLETED"
        progress["final_reason"] = reason or "target_reached"
        progress["final_note"] = None
    elif total_fail == 0 and valid >= min_valid:
        progress["final_state"] = "COMPLETED_MIN"
        progress["final_reason"] = reason or "exhausted"
        progress["final_note"] = (
            f"мягкая остановка ({progress['final_reason']}): "
            f"target={target}, valid={valid} >= min_valid={min_valid}, QA FAIL=0")
    else:
        progress["final_state"] = "ABANDONED"
        progress["final_reason"] = reason or "exhausted"
        if total_fail > 0:
            progress["final_note"] = (
                f"после финального QA остаётся брак: FAIL={total_fail} из "
                f"{len(snapshot)}; valid={valid}")
        else:
            progress["final_note"] = (
                f"valid={valid} < min_valid={min_valid}, цель недостижима")
    save_json(progress_path, progress)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="key-daemon", description=__doc__)
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--url", default=None)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--min-valid", type=int, default=1,
                    help="минимум чистых PID для частичного успеха "
                         "(1 <= min_valid < --limit; для новых кампаний обязателен)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--max-age", default="7d")
    ap.add_argument("--min-reviews", type=int, default=40)
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--max-no-progress-runs", type=int, default=3)
    ap.add_argument("--max-rescans", type=int, default=2)
    ap.add_argument("--max-total-hours", type=float, default=12.0)
    ap.add_argument("--reserve-count", type=int, default=30,
                    help="число резервных valid PID сверх target для замены брака "
                         "при финальном QA (target_reached)")
    ap.add_argument("--challenge-pause", type=int, default=CHALLENGE_RETRY_DELAY_S,
                    help="пауза (сек) при challenge-storm перед следующей "
                         "попыткой (фиксированная, не растёт)")
    ap.add_argument("--notify", default="off", help="'off' | 'toast'")
    ap.add_argument("--force-new", action="store_true",
                    help="принудительно пересоздать кампанию, игнорируя "
                         "несовпадение identity (URL/схема) при resume")
    ap.add_argument("--reset", action="store_true",
                    help="полный сброс существующей кампании: удаляет каталог "
                         "data/campaigns/<name> (progress.json, status.json, "
                         "events.jsonl, runs/) и начинает сбор заново — "
                         "требуется --url. Сырой кеш товаров (data/*_raw*.json) "
                         "НЕ трогается.")
    ap.add_argument("--max-transient-retries", type=int, default=3,
                    help="сколько раз ретраить UNKNOWN_FAILURE (временные сбои "
                         "сбора) до перехода в NEEDS_ATTENTION")
    ap.add_argument("--max-worker-restarts", type=int, default=2,
                    help="сколько раз перезапускать упавшего воркера за один "
                         "run (0 — без перезапусков)")
    ap.add_argument("--session-budget-wall-clock", type=int, default=None,
                    help="лимит wall_clock (сек) одного прогона сбора (default 1800). "
                         "Поднять, чтобы run жил дольше без SESSION_EXHAUSTED")
    ap.add_argument("--window-mode", choices=["visible", "hidden"],
                    default=None,
                    help="режим окна браузера коллектора: visible — окно "
                         "развёрнуто и видимо наверху, hidden — свёрнуто "
                         "(по умолчанию из env OZON_KEEP_WINDOW=1 → visible)")
    ap.add_argument("--match-title", default=None,
                    help="фильтр точного соответствия названия товара "
                         "(регистронезависимая подстрока, применяется к "
                         "raw.product.title): в дайджест попадают только товары, "
                         "чей title содержит эту фразу. Включается ТОЛЬКО "
                         "явно; text= из --url автоматически НЕ подхватывается "
                         "(поисковый запрос ≠ название товара). Не задан — "
                         "фильтр выключен, сбор как обычно. Несоответствующие "
                         "товары НЕ считаются браком. Фильтр фиксируется при "
                         "создании кампании, при resume меняться не может.")
    ap.add_argument("--no-digest", action="store_true",
                    help="не создавать Markdown-дайджест итога кампании "
                         "(по умолчанию создаётся в --out)")
    return ap.parse_args(argv)


def _emergency_shutdown(progress_path: Path, force: str = ""):
    """ЕДИНСТВЕННАЯ точка физического (принудительного) завершения демона.

    Вызывается по 2-му Ctrl+C (FORCE) из точек, где замечен _FORCE_EXIT:
      - докрутка ссылок: CollectionInterrupted(forced=True) в _inproc_collect;
      - worker-фаза: _await_workers уже принудительно завершил воркеров
        (_terminate_worker → _cleanup_worker → force_stop их браузеров), затем
        этот хелпер вызывается из run_campaign при _FORCE_EXIT.is_set().

    os._exit() здесь — ПРАВИЛЬНЫЙ инструмент: минует graceful-финализацию
    (aca сематика force — потеря последних незаписанных данных приемлема,
    пользователь требует немедленного выхода). Низкоуровневые search/parallel
    НИКОГДА не зовут os._exit — только поднимают сигнал.
    """
    log.error(f"демон: emergency shutdown (force{('=' + force) if force else ''}) "
              f"— принудительный выход")
    try:
        pid_file = DATA_DIR / "key_daemon.pid"
        if pid_file.exists():
            pid_file.unlink(missing_ok=True)
    except OSError:
        pass
    import os as _os
    _os._exit(1)


def main(argv=None):
    # Перекодировать stdout/stderr в UTF-8 ДО argparse: иначе help/usage
    # (с кириллицей и стрелками) падают с UnicodeEncodeError в cp1251-консоли
    # (Windows), когда вывод редиректится в файл/pipe.
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
    args = parse_args(argv)
    out = args.out or f"docs/{args.campaign}.md"
    progress = run_campaign(
        campaign=args.campaign, url=args.url or "", target=args.limit,
        min_valid=args.min_valid, out=out, max_age=args.max_age,
        min_reviews=args.min_reviews, parallel=args.parallel,
        max_no_progress_runs=args.max_no_progress_runs,
        max_rescans=args.max_rescans,
        max_total_hours=args.max_total_hours,
        reserve_count=args.reserve_count, notify_mode=args.notify,
        challenge_pause=args.challenge_pause, force_new=args.force_new,
        force_reset=args.reset,
        max_transient_retries=args.max_transient_retries,
        max_worker_restarts=args.max_worker_restarts,
        session_budget_wall_clock=args.session_budget_wall_clock,
        window_mode=args.window_mode,
        match_title=args.match_title,
        write_digest=not args.no_digest)
    print(json.dumps(summarize(progress), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    import signal as _signal

    def _graceful_exit(signum, frame):
        """Ctrl+C / SIGTERM → кооперативная остановка (см. _STOP_EVENT выше).

        Первый сигнал: ставим _STOP_EVENT — collect_parallel/_await_workers штатно
        сворачивают воркеров (без респавна), прогресс сохраняется, кампания
        завершается с final_state=INTERRUPTED. Демон НЕ выходит здесь — он живёт,
        пока _await_workers не вернётся и run_campaign не запишет progress.
        Второй сигнал во время shutdown: _FORCE_EXIT — принудительный выход.
        """
        if _STOP_EVENT.is_set():
            # уже останавливаемся (первый Ctrl+C обработан) → эскалация
            log.warning(f"демон: повторный сигнал {signum} — форс-выход")
            _FORCE_EXIT.set()
            return
        log.warning(f"демон: получен сигнал {signum}, graceful shutdown "
                    f"(воркеры сворачиваются, прогресс сохраняется)")
        _STOP_EVENT.set()

    _signal.signal(_signal.SIGINT, _graceful_exit)
    try:
        _signal.signal(_signal.SIGTERM, _graceful_exit)
    except (OSError, AttributeError):
        pass  # SIGTERM недоступен на Windows
    try:
        main()
    except KeyboardInterrupt:
        # На случай, если сигнал-обработчик по какой-то причине не сработал
        # (например, interrupt прилетел в блок, где обработчик ещё не был
        # установлен). Ставим стоп-событие, чтобы ниже дождаться graceful
        # завершения активного сбора.
        log.warning("демон: KeyboardInterrupt — ставлю остановку")
        _STOP_EVENT.set()
        if _FORCE_EXIT.is_set():
            log.error("демон: форс-выход по второму прерыванию")
            try:
                (DATA_DIR / "key_daemon.pid").unlink(missing_ok=True)
            except OSError:
                pass
            import os as _os
            _os._exit(1)
        raise
    finally:
        try:
            (DATA_DIR / "key_daemon.pid").unlink(missing_ok=True)
        except OSError:
            pass
