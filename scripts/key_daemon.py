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
        --url "<URL>" --limit 300 --out docs/digests/prezervativy.md \
        --max-age 7d --min-reviews 40 --parallel 1 \
        --min-valid 200 --max-no-progress-runs 3 --max-rescans 2 \
        --max-total-hours 12 --notify toast

    # --campaign и --out можно не указывать: имя генерируется из text= в --url
    # (транслит, ~50 символов; иначе из категории, иначе campaign_<дата_время>),
    # а out — docs/digests/{campaign}.md. text= используется ТОЛЬКО для имени,
    # для title-фильтра никогда.
    python scripts/key_daemon.py --url "https://www.ozon.ru/search/?text=..."
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ozon_collector import log
from ozon_collector import net
from ozon_collector.browser import AccessChallengeError
from ozon_collector.process import command_line_processes, kill_identities
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
# Взят ли pid-лок ЭТИМ процессом: finally/force сносят pid-файл только когда
# он наш. Иначе выход второго демона («уже запущен» → SystemExit) удалял бы
# pid-файл ЖИВОГО первого (ревью 2026-10-15).
_PID_LOCK_HELD = False

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
# Инкрементальный чекпоинт прогона (2026-09-26). Манифест пишется только на
# границах прогона, а прогон с 8 воркерами идёт часами: падение демона теряло
# весь собранный прогресс, resume начинал с нуля (живой случай fonari_ 1ч20м
# прогресс = 336 pid, в манифесте 0). Чекпоинт пишет inflight.json и НЕ трогает
# progress.json — иначе гонка с главным потоком. 0 = выключить (тесты).
CHECKPOINT_EVERY_S = int(os.environ.get("KD_CHECKPOINT_EVERY", "60") or 60)
INFLIGHT_NAME = "inflight.json"
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
QA_VERSION = "1.8"

# Сколько раз товар из выдачи может вернуться в очередь без результата, прежде
# чем признаётся безнадёжным. Обратная сторона починки «discovered ≠ собранные»:
# без счётчика не собираемый товар пересобирался бы каждый прогон (ревью
# 2026-10-06). Три попытки — потому что сбои бывают разовыми: SESSION_EXHAUSTED,
# BROWSER_DEAD, конец прогона.
PENDING_ATTEMPT_LIMIT = 3


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

def _workers_all_fatal(combined: Optional[dict]) -> bool:
    """ВСЕ воркеры прогона умерли (fatal_error у каждого)? None — нет данных.

    Воркеры пишут fatal_error при падении и исчерпании рестартов (а оркестратор
    завершается с exit 0). Без этой проверки мёртвый пул классифицировался
    NORMAL_DONE и тихо «худел» без cooldown (ревью 2026-10-15). Условие «все»,
    а не «любой»: штатный респавн одного воркера — не авария прогона, его
    fatal_error остаётся в workers как телеметрия.
    """
    workers = ((combined or {}).get("parallel") or {}).get("workers") or []
    if not workers:
        return False
    return all(isinstance(w, dict) and w.get("fatal_error") for w in workers)


def classify_status(exit_code: int, combined: Optional[dict]) -> str:
    """Классифицирует итог одного прогона сбора (по combined + exit-коду, не по логу).

    EMPTY_SEARCH (ревизия 2.9): прогон не упал, но сетка выдачи не отрисовалась
    (recovery.search_grid_empty) → 0 ссылок → 0 товаров. Это НЕ «успех»:
    раньше такой прогон писался NORMAL_DONE и в status.json выглядел здоровым.
    Считается стагнацией (как NORMAL_DONE без прогресса), но не прогрессом,
    поэтому явная метка видна оператору и в логе, и в status.json.
    """
    if combined and "aborted" in combined:
        return "CHALLENGE_STORM"
    if _workers_all_fatal(combined):
        # Смерть всего пула — не NORMAL_DONE (у оркестратора exit 0): CRASH
        # даёт короткий cooldown и запись в stall.crash_runs вместо тихого
        # «успеха» с потерянными товарами.
        return "CRASH"
    if exit_code == 0:
        if (combined or {}).get("recovery", {}).get("search_grid_empty"):
            return "EMPTY_SEARCH"
        return "NORMAL_DONE"
    if combined is None:
        return "CRASH"
    return "UNKNOWN_FAILURE"


def _storm_escalates(aborted: dict, parallel: int) -> tuple[bool, bool]:
    """(all_stormed, escalate) — пора ли жечь cooldown кампании (ревизия 2.9).

    Вынесено из run_campaign, чтобы правило было тестируемым и не расходилось
    с логикой soft/hard:
      * all_stormed — шторм у ВСЕХ воркеров (или одиночный прогон parallel=1);
      * escalate — all_stormed И вид блока hard. soft (SESSION_EXHAUSTED) не
        эскалирует даже при шторме всех воркеров: эскалация зарезервирована
        для настоящих блокировок, иначе «усталая» сессия выжигает
        profile_cooldown и Storm-Streak на ровном месте.
    """
    all_stormed = bool(parallel == 1 or aborted.get("all_workers"))
    soft = _storm_block_kind(aborted) == "soft"
    return all_stormed, (all_stormed and not soft)


def _storm_block_kind(aborted: dict) -> str:
    """soft | hard — вид блокировки прогона (ADR §14.6, ревизия 2.9).

    hard (по умолчанию, в т.ч. при нехватке данных): SESSION_BLOCKED,
    отказ origin (403/429/503) не снятый лестницей, challenge не снят после
    лестницы, нерешённый пазл.
    soft: SESSION_EXHAUSTED — бюджет сессии исчерпан (усталость от серии
    проверок, а не доказанный бан). Разница в реакции: soft → короткая пауза
    БЕЗ profile_cooldown и БЕЗ роста storm_streak; hard → adaptive-карантин.

    Правило асимметрии: любое сомнение трактуется как hard — в прошлом
    «мягкая» трактовка маскировала бы настоящий бан до растения серии.
    """
    if aborted.get("block_kind") in ("soft", "hard"):
        return aborted["block_kind"]
    if aborted.get("reason") == "session_exhausted":
        return "soft"
    if aborted.get("puzzle_persistent"):
        return "hard"
    return "hard"


def _storm_label(signature: Optional[str], aborted: dict) -> str:
    """Причина шторма для status.json/тоста/лога (оператор должен видеть ЧТО вернула Ozon).

    Приоритет: нерешённый пазл (recovery.challenge_storm.puzzle_persistent из
    search.py) → сигнатура страницы (title:..., http_status) → список
    штормивших воркеров → честное «неизвестно». Без этого пазл-штормы давали
    в last_error «сигнатура неизвестна» — диагноз невозможен.
    """
    if aborted.get("puzzle_persistent"):
        n = aborted.get("puzzle_failures")
        return (f"puzzle_storm: пазл не решён ({n} неудач)"
                if n else "puzzle_storm: пазл не решён")
    if signature:
        return signature
    workers = aborted.get("workers_stormed")
    if workers:
        return f"access_challenge (воркеры {workers})"
    return "access_challenge (сигнатура неизвестна)"


def extract_discovered_pids(combined: Optional[dict]) -> list[str]:
    """PID, обнаруженные выдачей этого прогона (combined.products, все записи).

    PID нормализуются (снимают URL-обёртку и хвосты), чтобы дальнейшие
    сравнения/словари были консистентны независимо от формата записи.
    Также сливает combined.discovered_pids (заполняется при graceful-стопе
    докрутки ссылок в collect_parallel: products пуст, но ссылки найдены).

    Берём ВСЮ запись, у которой опознан pid, независимо от ok/lost/excluded:
    товар без результата (lost=True из merge_shard_results) обязан попасть в
    discovered_pids, иначе split_done_pending не вернёт его в очередь и он
    исчезнет из кампании навсегда. «pid is not None» здесь — условие
    ОПОЗНАНИЯ записи, а не признак успеха; успех живёт только в
    extract_ok_pids.
    """
    if not combined:
        return []
    pids = [p["pid"] for p in (combined.get("products") or [])
            if p.get("pid") is not None]
    pids += list(combined.get("discovered_pids") or [])
    return normalize_pids(pids)


def extract_ok_pids(combined: Optional[dict]) -> list[str]:
    """PID, реально собранные без исключения (потенциально валидные).

    lost-записи (потерянный товар без результата) сюда НЕ попадают никогда:
    у них ok=False (и вдобавок про явный запрет по lost) — иначе товар без
    собранных отзывов уехал бы в valid_pids, что ломает инвариант I1/I5.
    """
    if not combined:
        return []
    return normalize_pids(p["pid"] for p in (combined.get("products") or [])
                          if p.get("ok") and not p.get("excluded")
                          and not p.get("lost"))


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


def raw_pids_since(data_dir: str, since_ts: float) -> list[str]:
    """Каноничные pid, чей raw появился/обновился не раньше since_ts.

    Источник истины тот же, что у has_raw, — canonical raw на диске. Бэкапы
    ({pid}_raw.<stamp>.N.json) и мусор (gallery_*, не цифра) отсекаются
    префиксом `isdigit`, поэтому регекс не нужен. since_ts <= 0 → все pid.
    """
    d = Path(data_dir)
    if not d.is_dir():
        return []
    out = []
    try:
        for f in d.glob("*_raw.json"):
            pid = f.name[: -len("_raw.json")]
            if not pid.isdigit():
                continue
            if since_ts and f.stat().st_mtime < since_ts:
                continue
            out.append(pid)
    except OSError:
        return []
    return normalize_pids(out)


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
                     url: str, out: str, name_source: str = "explicit",
                     name_source_value: Optional[str] = None,
                     **run_cfg) -> dict:
    return {
        "campaign": campaign,
        "name_source": name_source,
        "name_source_value": name_source_value,
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
            "last_full_storm_at": None,   # epoch фулл-шторма → hold пересева
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


def _start_checkpoint(camp_dir, data_dir: str, every_s: float,
                      campaign: Optional[str] = None) -> dict:
    """Фоновый чекпоинт прогресса на время блокирующего сбора.

    Живёт в отдельном потоке и пишет ТОЛЬКО свой снапшот inflight.json —
    progress.json остаётся собственностью главного потока (иначе read-modify-
    write гонка: главный поток мутирует манифест в сотнях мест). Снапшот
    мержится в манифест на границе прогона и при следующем старте демона
    (resume), поэтому падение не обнуляет часы работы.

    Вердикты/QA здесь НЕ делаются (это спавн qa_raw на сотни pid каждые 60с):
    у восстановленного pid есть canonical raw, поэтому вердикт и valid
    пересчитаются в classify_new_pids без браузера.

    handle: {"path", "thread", "pids", "stop"} — pids для лога/тестов.
    """
    path = Path(camp_dir) / INFLIGHT_NAME
    handle = {"path": path, "thread": None, "pids": [], "stop": None,
              "error": None}
    if not every_s or every_s <= 0:
        return handle

    stop = threading.Event()
    handle["stop"] = stop
    since = time.time()
    seen: set[str] = set()

    def _loop() -> None:
        while not stop.wait(every_s):
            try:
                fresh = [p for p in raw_pids_since(data_dir, since)
                         if p not in seen]
                if not fresh:
                    continue
                seen.update(fresh)
                handle["pids"] = normalize_pids(handle["pids"] + fresh)
                save_json(path, {
                    "campaign": campaign, "since": since,
                    "checkpoint_at": now_utc_iso(), "pids": handle["pids"],
                })
                log.info(f"[checkpoint] raw на диске: +{len(fresh)} pid "
                         f"(в inflight {len(handle['pids'])})")
            except Exception as exc:  # чекпоинт не имеет права убить демон
                handle["error"] = repr(exc)
                log.warning(f"[checkpoint] сбой (продолжаю без чекпоинта): {exc}")

    t = threading.Thread(target=_loop, name="kd-checkpoint", daemon=True)
    t.start()
    handle["thread"] = t
    return handle


def stop_checkpoint(handle: dict) -> None:
    """Остановить чекпоинт и дождаться конца итерации (best-effort)."""
    if not handle:
        return
    stop = handle.get("stop")
    if stop is not None:
        stop.set()
    t = handle.get("thread")
    if t is not None and t.is_alive():
        t.join(timeout=5.0)


def _merge_inflight(progress: dict, path) -> list[str]:
    """Влить pid из inflight.json в discovered_pids манифеста.

    Возвращает pid, которых раньше в манифесте не было. Без файла/битого
    файла — no-op: потеря чекпоинта не должна ломать демон (манифест —
    источник истины).
    """
    snap = load_json(path)
    if not snap:
        return []
    pids = normalize_pids(snap.get("pids") or [])
    if not pids:
        return []
    known = set(progress.get("discovered_pids", []))
    fresh = [p for p in pids if p not in known]
    if fresh:
        progress["discovered_pids"] = normalize_pids(
            list(progress.get("discovered_pids", [])) + fresh)
    return fresh


def _reconcile_inflight(progress: dict, path, data_dir: str,
                        qa_fn: Optional[Callable[[list[str]], dict]] = None,
                        ctx: str = "") -> list[str]:
    """Влить inflight.json в манифест И сразу поставить pid вердикты.

    Просто добавить pid в discovered НЕЛЬЗЯ: следующий прогон внесёт его в
    skip_pids, но ни в valid, ни в failed он не попадёт — вердикт ставит
    classify_new_pids по ok_pids прогона, а не по discovered. Товар зависнет
    «известным, но невалидным» навсегда, и цель кампании недостижима. Поэтому
    новые pid сразу получают QA: браузер не нужен, canonical raw уже на диске.
    """
    fresh = _merge_inflight(progress, path)
    if not fresh:
        return []
    checked = classify_new_pids(progress, fresh, qa_fn=qa_fn)
    if checked.get("parse_error"):
        log.error(f"{ctx} inflight: QA-сводка недостоверна — вердикты не "
                  f"применены ({len(fresh)} pid ждут следующего цикла)")
        return fresh
    reconcile_validity(progress, checked, data_dir)
    return fresh


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


def _run_data_dir(progress: dict) -> Optional[str]:
    """Run's data_dir from run_cfg (single source of truth for resume).

    Callers must not trust CLI defaults: split_done_pending/pending_pids read
    raw files from here (audit 2026-10-19 — calls passed no data_dir, so a
    non-default run dir would look in the wrong place).
    """
    return (progress.get("run_cfg") or {}).get("data_dir")


def summarize(progress: dict) -> dict:
    return {
        "campaign": progress.get("campaign"),
        "target": progress.get("target_count"),
        "min_valid": progress.get("min_valid"),
        "valid": len(set(progress.get("valid_pids", []))),
        "discovered": len(set(progress.get("discovered_pids", []))),
        "failed": len(set(progress.get("failed_pids", []))),
        "permanently_failed": len(set(progress.get("permanently_failed_pids", []))),
        "pending": len(pending_pids(progress, _run_data_dir(progress))),
        "runs": len(progress.get("runs", [])),
        "final_state": progress.get("final_state"),
        "final_reason": progress.get("final_reason"),
    }


def split_done_pending(progress: dict,
                       data_dir: str | None = None) -> tuple[set, list]:
    """Разделить пиды выдачи на «обработанные» и «без результата».

    `done` — по ним есть результат: вердикт (valid / FAIL / permanently_failed)
    либо canonical raw на диске. `pending` — найдены в выдаче, но результата
    нет: их обязано вернуть в очередку следующего прогона.

    Почему это отдельная функция (регрессия 2026-10-06, жалоба пользователя
    «прошёл, а собрал мало»): `discovered_pids` пополняется сразу после
    докрутки выдачи, ДО сбора. Раньше весь discovered попадал в skip_pids, и
    товары, до которых воркеры не дошли (SESSION_EXHAUSTED / BROWSER_DEAD /
    конец прогона), выкидались из очереди навсегда: в логе было «пропущено 336
    уже обработанных (осталось 0)», пока на диске лежало 66 raw.json.
    """
    d = data_dir or str(DATA_DIR)
    discovered = set(progress.get("discovered_pids") or [])
    # done шире discovered: valid/FAIL/permanently_failed могут быть в состоянии
    # от прошлых прогонов, их тоже нельзя пересобирать.
    done = set(progress.get("valid_pids", [])) \
        | set(progress.get("failed_pids", [])) \
        | set(progress.get("permanently_failed_pids", []))
    done |= {p for p in discovered if has_raw(d, str(p))}
    return done, sorted(discovered - done)


def pending_pids(progress: dict, data_dir: str | None = None) -> list[str]:
    """Товары из выдачи, по которым НЕТ результата (ни raw, ни вердикта).

    Их число показывает, что прогон «нормально завершился», а собрал не всё.
    """
    return split_done_pending(progress, data_dir)[1]


# ---------------------------------------------------------------------------
# инфраструктура процесса
# ---------------------------------------------------------------------------

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _alive_win(pid: int) -> bool:
    """Живость процесса в Windows без TerminateProcess.

    os.kill(pid, 0) здесь НЕ безобидный запрос: CPython на Windows вызывает
    TerminateProcess(handle, 0), то есть УБИВАЕТ процесс. Замер 2026-09-25:
    на этом pytest убивал сам себя в тестах pidlock (обрыв прогона на ~42%),
    а демон при проверке pid-файла убил бы предыдущий живой инстанс вместо
    отказа запускаться. Поэтому OpenProcess + GetExitCodeProcess."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION,
                                       False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception as e:
        log.debug(f"_alive_win({pid}): {type(e).__name__}: {e}")
        return False


def _alive(pid: int) -> bool:
    """Проверка живости процесса (best-effort).

    Защита от нереалистичных PID: с огромным/нулевым/отрицательным PID
    запрос может зависнуть или дать ложный результат. PID реальных
    процессов на Windows/Linux практически никогда не выходят за 2^24.
    """
    if not isinstance(pid, int) or pid <= 0 or pid >= 2 ** 24:
        return False
    if os.name == "nt":
        return _alive_win(pid)
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
        """Занять лок АТОМАРНО: O_CREAT|O_EXCL вместо is_file + save_json.

        Старая схема давала гонку: два демона видели stale-файл, оба снимали
        его, оба записывали себя — защита «от повторного запуска» пропускала
        второго (ревью 2026-10-15).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):    # вторая попытка — после снятия stale-файла
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                data = load_json(self.path)
                if data is None:
                    # The winner created the file with O_EXCL and writes the
                    # JSON a tick later. Treating the empty-file window as
                    # stale would let a second daemon unlink a live lock and
                    # acquire it (audit 2026-10-18, B4). Re-read briefly before
                    # declaring the file stale.
                    for _try in range(4):
                        time.sleep(0.05)
                        data = load_json(self.path)
                        if data is not None:
                            break
                pid = data.get("pid") if data else None
                # pid обязан быть int; иначе файл битый → трактуем как stale
                if isinstance(pid, int) and _alive(pid):
                    return False
                self.path.unlink(missing_ok=True)
                continue
            except OSError:
                return False
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({
                    "pid": os.getpid(), "campaign": campaign,
                    "started_at": now_utc_iso(),
                    "start_cmdline": " ".join(sys.argv),
                }, f, ensure_ascii=False, indent=2)
            return True
        return False

    def release(self) -> None:
        # Снимаем только СВОЙ pid-файл: чужой демон мог атомарно занять лок
        # после нашей проигранной гонки — release не имеет права снести его.
        data = load_json(self.path)
        if data and data.get("pid") != os.getpid():
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def _drop_pid_file() -> None:
    """Снять pid-файл выхода: только если лок держим МЫ (_PID_LOCK_HELD)."""
    if not _PID_LOCK_HELD:
        return
    try:
        (DATA_DIR / "key_daemon.pid").unlink(missing_ok=True)
    except OSError:
        pass


# ---- трей-пульт окон сбора ------------------------------------------------
# Трей — часть сбора, а не резидент (просьба владельца 2026-10-16): демон
# поднимает его на старте и передаёт -WatchPid, трей сам выходит в течение
# ~2с после смерти демона (watchdog внутри скрипта по cmdline) — иконка не
# может «зависнуть» даже при taskkill. Ручной standalone-запуск скрипта
# остался для отладки (-List/-Show/-Hide).

TRAY_SCRIPT = ROOT / "scripts" / "collector_tray.ps1"
_TRAY_CMD_TOKEN = "collector_tray.ps1"
_TRAY_KILL_WAIT_S = 2.0


def _stale_tray_processes() -> list:
    """Identity треев прошлых запусков (powershell/pwsh + маркер в cmdline)."""
    idents = []
    for name in ("powershell", "pwsh"):
        idents.extend(command_line_processes(name, _TRAY_CMD_TOKEN))
    return idents


def _kill_stale_trays() -> list[int]:
    """Снять треи прошлых запусков — только после сверки identity."""
    return kill_identities(_stale_tray_processes(), wait_s=_TRAY_KILL_WAIT_S)


def _start_tray_companion(disabled: bool = False):
    """Поднять трей-пульт, привязанный к этому процессу демона. None — не поднят.

    Спавн отключён при: --no-tray, env OZON_TRAY=0, не-Windows, pytest
    (PYTEST_CURRENT_TEST: тесты зовут main() in-process, GUI там не нужен).
    Старые треи (в т.ч. запущенные руками) сначала снимаются по identity —
    иначе после «трей живёт со сбором» на панели копилась бы вторая иконка.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    if os.name == "nt":
        # Снимаем старые треи ДО сброса флага: иначе живой трей прошлого
        # прогона увидит отсутствие флага и вернёт окна по своему env
        # (MINOR ревью 2026-10-19).
        try:
            gone = _kill_stale_trays()
            if gone:
                log.info(f"трей: снят прошлый экземпляр (pid={gone})")
        except Exception as exc:
            # Трей — удобство, а не часть сбора: его проблемы не роняют кампанию.
            log.warning(f"трей: чистка прошлых экземпляров не удалась: {exc}")
    # Залипший флаг «пользователь смотрит окна» (трей убили taskkill'ом, не
    # через «Выход») блокировал бы сторож на весь прогон: снимаем на каждом
    # старте сбора, даже когда трей отключён (--no-tray) — иначе старый "0"
    # отменял бы --window-mode visible.
    try:
        from ozon_collector import window_policy
        window_policy.clear_user_visible()
    except Exception as exc:
        log.warning(f"трей: сброс флага user-visible не удался: {exc}")
    if disabled or os.name != "nt":
        return None
    flag = (os.environ.get("OZON_TRAY") or "").strip().lower()
    if flag in {"0", "off", "false", "no"}:
        return None
    if not TRAY_SCRIPT.exists():
        log.warning(f"трей: скрипт не найден: {TRAY_SCRIPT}")
        return None
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(TRAY_SCRIPT), "-WatchPid", str(os.getpid())]
    try:
        # CREATE_NO_WINDOW: у трея скрытая консоль, виден только значок.
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    except OSError as exc:
        log.warning(f"трей: powershell не запустился: {exc}")
        return None
    log.info(f"трей: пульт окон сбора запущен (powershell pid={proc.pid}, "
             f"watch_pid={os.getpid()})")
    return proc


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
        "session_auth": progress.get("session_auth"),
        "qa": progress.get("qa"),
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
        # origin_denied и canary_fails добавлены в список 2026-10-02: без них
        # метрика отказа origin (403/429/503) и число неотвечающих canary-проб
        # вычислялись в combined.kpi и ТЕРЯЛИСЬ на финише — в progress.json
        # попадали только challenges/recoveries/пазлы. Именно эти два числа
        # нужны, чтобы отличить «много отказов origin» от «много капч».
        for k in ("total_challenges", "total_recoveries", "session_blocked",
                  "session_exhausted", "puzzle_storms",
                  "puzzle_attempts", "puzzle_solved",
                  "origin_denied", "canary_fails"):
            v = kpi.get(k)
            if v is not None:
                acc[k] = (acc.get(k, 0) or 0) + int(v)
        if kpi.get("products_since_recovery") is not None:
            acc["products_since_recovery"] = kpi["products_since_recovery"]
        last_sr = kpi.get("success_rate")
        if last_sr is not None:
            acc["last_success_rate"] = last_sr
        # Производные rate — «последний раунд» + кумулятивный solve-rate
        # (тренд: solve-rate падает → детектор/драг-геометрия деградировали).
        if kpi.get("puzzle_solve_rate") is not None:
            acc["last_puzzle_solve_rate"] = kpi["puzzle_solve_rate"]
        pa = int(acc.get("puzzle_attempts") or 0)
        if pa:
            acc["puzzle_solve_rate"] = round(
                int(acc.get("puzzle_solved") or 0) / pa, 3)
        if kpi.get("challenge_rate_per_100") is not None:
            acc["last_challenge_rate_per_100"] = kpi["challenge_rate_per_100"]
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


def _absorb_session_auth(progress: dict, combined: Optional[dict]) -> None:
    """Сводит состояние авторизации профиля по товарам прогона.

    Неавторизованный Ozon отдаёт максимум ~96-100 отзывов (WALL_LIMITED).
    Сводим session.auth из combined → progress["session_auth"], его читает
    status.json: видно, объясняется ли потолок неавторизованной сессией.
    Только наблюдаемость — на решение демона не влияет.
    """
    if not combined:
        return
    logged_in = anonymous = unknown = 0
    for p in (combined.get("products") or []):
        auth = (p.get("raw") or {}).get("session") or {}
        state = (auth.get("auth") or {}).get("logged_in")
        if state is True:
            logged_in += 1
        elif state is False:
            anonymous += 1
        else:
            unknown += 1
    total = logged_in + anonymous + unknown
    if not total:
        return
    progress["session_auth"] = {
        "checked": total,
        "logged_in": logged_in,
        "anonymous": anonymous,
        "unknown": unknown,
        "updated_at_utc": now_utc_iso(),
    }


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
    if not isinstance(payload, dict) or "summary" not in payload \
            or "products" not in payload:
        return None
    s = payload["summary"]
    if not isinstance(s, dict):
        # Broken/partial qa_raw JSON: fall back to stdout parsing instead of
        # raising AttributeError below (audit 2026-10-18, B2 follow-up).
        return None
    parsed = {
        "products": s.get("products", 0),
        "ok": s.get("ok", 0),
        "warn": s.get("warn", 0),
        "fail": s.get("fail", 0),
        "wall_limited": s.get("wall_limited_count", 0),
    }
    try:
        parsed["per_pid"] = {r["pid"]: r["verdict"]
                             for r in payload["products"]}
        # причины FAIL/WARN (коды issues) — для детекта детерминированного брака
        parsed["per_pid_issues"] = {
            r["pid"]: [i["code"] for i in r["issues"]]
            for r in payload["products"]
        }
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        # Contract violation in a products row (partial/evolved qa_raw JSON):
        # return None so run_campaign falls back to stdout parsing instead of
        # crashing with KeyError (audit 2026-10-18, B2).
        log.warning(f"_parse_qa_json_report: битая строка products "
                    f"({type(exc).__name__}: {exc}) → fallback на stdout")
        return None
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
                             encoding="utf-8", timeout=900,
                             creationflags=(getattr(subprocess,
                                                    "CREATE_NO_WINDOW",
                                                    0x08000000)
                                            if os.name == "nt" else 0))
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
    data_dir = _run_data_dir(progress) or DEFAULT_DATA_DIR
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
        _force_exit_guard()   # 2nd Ctrl+C: do not wait for the rest of batches
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

    batch_size — размер выдачи воркеру (1 = «грузчик берёт один товар»,
    решение владельца). Берётся ИЗ run_cfg манифеста: при resume конфиг — это
    манифест, а не аргументы текущего запуска (инвариант 4). Поле в кампаниях,
    созданных до появления ключа, отсутствует → дефолт 1.
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
        "skip_reviews": cfg.get("skip_reviews", False),
        # Размер выдачи воркеру. Кламп вниз единицей: batch_size<=0 в
        # parallel._take_batch даёт пустую выдачу при непустой очереди →
        # воркер получает 'none' и прогон тихо собирает 0 товаров.
        "batch_size": max(1, int(cfg.get("batch_size") or 1)),
        # Быстрый API-предсрез отзывов (ревизия 2.8): выключен по умолчанию,
        # DOM остаётся источником истины. None → env OZON_REVIEWS_API.
        "reviews_api": cfg.get("reviews_api"),
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
        # P0: URL лежит в состоянии кампании, поэтому мусор, попавший туда
        # ДО 2.9, пережил бы рестарт демона. Чиним/отвергаем на загрузке.
        try:
            url = net.normalize_target_url(progress["url"])
        except ValueError as e:
            log.error("[key_daemon] URL кампании в state.json невалиден: "
                      f"{e}. Перезадай --url или удали "
                      f"data/campaigns/{progress.get('campaign', '?')}/state.json")
            raise SystemExit(2)
        # Пропускать ТОЛЬКО реально обработанные пиды, а не весь discovered.
        # Регрессия 2026-10-06 (жалоба пользователя «прошёл, а собрал мало»):
        # discovered пополняется до сбора, и товары, до которых воркеры не
        # дошли (SESSION_EXHAUSTED / BROWSER_DEAD / конец прогона), выкидались
        # из очереди навсегда — в логе «пропущено 336 уже обработанных (осталось
        # 0)» при 66 raw.json на диске. Разбор — в split_done_pending.
        _done, _retry = split_done_pending(progress, _run_data_dir(progress))
        skip_pids = sorted(_done) or None
        # Счётчик попыток для товаров без результата. Без него починка
        # «discovered ≠ собранные» превратилась бы в свой антипаттерн: товар,
        # который невозможно собрать (удалённая карточка, вечная капча на этом
        # PID), брался бы в работу на каждом прогоне, пока хватает лимита
        # no_progress. После N попыток — в permanently_failed_pids (ревью
        # 2026-10-06). Счётчик сам сбрасывается: остаются только те, у кого
        # результата так и нет.
        _attempts = dict(progress.get("pending_attempts") or {})
        for _p in _retry:
            _attempts[_p] = _attempts.get(_p, 0) + 1
        _give_up = {p for p, n in _attempts.items()
                    if n >= PENDING_ATTEMPT_LIMIT}
        if _give_up:
            progress["permanently_failed_pids"] = normalize_pids(
                set(progress.get("permanently_failed_pids", [])) | _give_up)
            log.warning(
                f"[key_daemon] {len(_give_up)} товаров без результата после "
                f"{PENDING_ATTEMPT_LIMIT} попыток — в безнадёжные: "
                f"{', '.join(sorted(_give_up)[:12])}"
                f"{'...' if len(_give_up) > 12 else ''}")
        progress["pending_attempts"] = {
            p: n for p, n in _attempts.items() if p not in _give_up}
        skip_pids = sorted(set(skip_pids or ()) | _give_up) or None
        _retry = [p for p in _retry if p not in _give_up]
        if _retry:
            log.info(f"[key_daemon] в очередь возвращено {len(_retry)} пидов из "
                     f"выдачи без результата (были пропущены как "
                     f"'обработанные'): {', '.join(_retry[:12])}"
                     f"{'...' if len(_retry) > 12 else ''}")
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
    # ADR §14.7: после фулл-шторма прогретые воркер-профили не пересеиваем
    # (иначе испорченная сессия main тиражируется на весь пул).
    _storm_at = (progress.get("profile_cooldown") or {}).get(
        "last_full_storm_at")
    combined = collect_parallel(
        url, limit=limit, gather_cfg=gather_cfg, parallel=parallel,
        query=None, max_recycles=2, join_timeout_s=43200.0,
        pids=pids or None, skip_pids=skip_pids, no_bootstrap=False,
        bootstrap_post_storm_at=_storm_at,
        session_budget=session_budget, on_event=_on_event,
        max_worker_restarts=max_restarts, worker_log=worker_log,
        stop_event=_STOP_EVENT, force_event=_FORCE_EXIT,
        canary_url=cfg.get("canary_url") or None,
        skip_no_reviews=cfg.get("skip_no_reviews", True))

    combined_file = CAMPAIGNS_DIR / camp / f"combined_{run_id}.json"
    combined_file.parent.mkdir(parents=True, exist_ok=True)
    if combined is not None:
        # save_json (tmp + os.replace): читатель (status/replay/QA) никогда
        # не увидит полузаписанный combined при крэше между write и flush.
        save_json(combined_file, combined)
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

def _commit_run_results(progress, progress_path, run_id, mode, targets,
                        combined, exit_code, qa_fn, data_dir, camp_dir,
                        before_disc, before_valid):
    """Пост-обработка прогона (аудит 2026-10-15, #13).

    Граница прогона: вливает pid, накопленные чекпоинтом (late-reconcile),
    классифицирует статус, проверяет QA новых кандидатов, обновляет
    счётчики runs/progress/stall и печатает честный итог «без результата».
    Возвращает (status, new_disc, new_valid, new_checked) — их ждут реакции
    по статусу в run_campaign; None — результат терминален (Ctrl+C или
    недостоверная QA-сводка), цикл кампании должен выйти.
    """

    # Граница прогона: вливаем pid, накопленные чекпоинтом. Нужно ДО
    # подсчёта new_disc/new_valid и до save_json, иначе товары,
    # собранные в последние минуты прогона (или упавшей веткой:
    # UNKNOWN_FAILURE/CHALLENGE_STORM с пустым combined), не попадут
    # в манифест и следующий прогон соберёт их заново.
    _late = _reconcile_inflight(progress, camp_dir / INFLIGHT_NAME,
                                data_dir, qa_fn=qa_fn, ctx=f"[{run_id}]")
    if _late:
        log.info(f"[{run_id}] inflight → discovered: +{len(_late)} pid")

    status = classify_status(exit_code, combined)
    _journal_stage(progress, run_id, "run_completed",
                   {"status": status, "exit_code": exit_code})
    _absorb_recovery_telemetry(progress, combined)
    _absorb_session_auth(progress, combined)

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
        return None

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
        # Сбрасываем вердикт ТОЛЬКО у реально пере-QA-нутых пидов:
        # прогон мог не вернуть ни одного raw (пустая выдача или
        # шторм до QA) — сброс вердикта у НЕперепроверенных пидов
        # «забывал» брак, и пид выпадал из failed без повторного QA
        # (ревью 2026-10-15).
        rechecked = set(ok_pids) & set(targets)
        vdict = dict(progress.get("verdicts", {}))
        vmeta = dict(progress.get("verdict_meta", {}))
        for p in rechecked:
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
        return None
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
    if status in ("NORMAL_DONE", "EMPTY_SEARCH") \
            and not new_disc and not new_valid:
        # выдача перетёрлась известными — стагнация discovery/valid
        stall["full_runs_without_discovery"] = \
            stall.get("full_runs_without_discovery", 0) + 1
        if mode == "full":
            stall["full_runs_without_valid"] = \
                stall.get("full_runs_without_valid", 0) + 1
    # Счётчики серий — только ПОДРЯД идущие сбои (ADR §14.5/§14.6).
    # Раньше transient_failures и storm_streak были lifetime-счётчиками:
    # 2 шторма вчера + 2 сегодня (или 2 UNKNOWN_FAILURE до обеда + 2
    # после) давали NEEDS_ATTENTION ПОСЛЕ ЧАСОВ нормальной работы, т.е.
    # 12-часовая кампания была обречена на смерть по календарю.
    if status != "UNKNOWN_FAILURE" and progress.get("transient_failures"):
        log.info(f"серия UNKNOWN_FAILURE прервана ({status}) → "
                 f"transient_failures сброшен")
        progress["transient_failures"] = 0
    if status == "CHALLENGE_STORM":
        stall["challenge_runs"] = stall.get("challenge_runs", 0) + 1
    elif status == "NORMAL_DONE" and (new_disc or new_valid):
        cc = progress.get("challenge_control") or {}
        if cc.get("storm_streak"):
            log.info(f"прогресс ({len(new_disc)}+{len(new_valid)}) → "
                     f"storm_streak сброшен")
            cc["storm_streak"] = 0
            progress["challenge_control"] = cc
    if status == "CRASH":
        stall["crash_runs"] = stall.get("crash_runs", 0) + 1
    progress["stall"] = stall

    # Честный итог: NORMAL_DONE означает «воркеры отработали», а не
    # «собрано всё». Показываем, сколько товаров из выдачи осталось
    # без результата — иначе «нормально завершил» выглядит как успех
    # (жалоба 2026-10-06: три запуска подряд 21 → 37 → 58 valid, а демон
    # рапортовал нормальное завершение).
    _pending = pending_pids(progress, _run_data_dir(progress))
    log.info(f"[{run_id}] status={status}: discovered "
        f"{len(progress.get('discovered_pids', []))} "
        f"valid {len(progress.get('valid_pids', []))} "
        f"(new_disc={len(new_disc)}, new_valid={len(new_valid)})"
        + (f" БЕЗ РЕЗУЛЬТАТА: {len(_pending)} — вернутся в очередь"
           if _pending else " без результата: 0"))
    return status, new_disc, new_valid, new_checked


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
                 compact: bool = False,
                 skip_reviews: bool = False,
                 include_no_reviews: bool = False,
                 canary_url: Optional[str] = None,
                 reviews_api: bool = False,
                 batch_size: Optional[int] = None,
                 name_source: str = "explicit",
                 name_source_value: Optional[str] = None,
                 qa_fn: Optional[Callable[[list[str]], dict]] = None) -> dict:
    lock = PidLock(DATA_DIR / "key_daemon.pid")
    if not lock.acquire(campaign):
        raise SystemExit("key_daemon уже запущен (pid-файл)")
    global _PID_LOCK_HELD
    _PID_LOCK_HELD = True

    # Единый логгер демона: оркестратор — владелец файла кампании и ротации.
    # Воркеры (collect_parallel) получают тот же путь через task["worker_log"]
    # и пишут в него в append-режиме без ротации.
    # attach идемпотентен ПО ПУТИ: main() подключает тот же файл РАНЬШЕ (до
    # apply_window_mode/sentinel_start) — до 2026-10-18 стартовые строки
    # терялись в слепой зоне (файл не подключён — log.py::_write_file роняет
    # строку), и в логе демона не было ни одного упоминания сентинела.
    # Сверка именно с путём, а не has_file(): глобальный _fh мог остаться от
    # другой кампании или прошлого теста — has_file()-guard пропускал attach,
    # файл не создавался (3 интеграционных теста падали FileNotFoundError,
    # 2026-10-18).
    _log_path = LOGS_DIR / f"{campaign}_daemon.log"
    if log.current_path() != str(_log_path):
        log.attach_file(_log_path, mode="a",
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
        window_mode=window_mode,
        skip_reviews=skip_reviews,
        include_no_reviews=include_no_reviews,
        canary_url=canary_url,
        reviews_api=reviews_api,
        name_source=name_source,
        name_source_value=name_source_value)

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
                if name_source != "explicit":
                    hint = (f"имя '{campaign}' сгенерировано из --url "
                            f"(source={name_source}); конфликт с существующей "
                            f"кампанией — укажите --campaign явно")
                else:
                    hint = (f"Новая категория → используйте --campaign другое "
                            f"имя или --force-new для принудительного "
                            f"пересоздания.")
                raise SystemExit(
                    f"key_daemon: кампания '{campaign}' уже существует с URL, "
                    f"отличным от переданного --url (url_sha256 не совпадает). "
                    f"{hint}")
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

    # Краткий дайджест (--compact): флаг фиксируется при создании кампании,
    # при resume берётся из манифеста (источник истины) — CLI не переопределяет
    # поведение между запусками. Для кампаний, созданных до появления опции
    # (поля в манифесте нет), — fallback на текущий аргумент CLI: используется
    # только в этом запуске (передаётся в _write_digest отдельным параметром),
    # манифест НЕ перезаписывается — CLI не становится постоянной настройкой
    # старой кампании.
    if fresh:
        progress["compact_digest"] = bool(compact)
    else:
        compact = bool(progress.get("compact_digest", compact))

    # Быстрая карточка без отзывов (--no-reviews): фиксируется в run_cfg при
    # создании кампании; при resume run_cfg читается СТРОГО из манифеста ниже
    # (источник истины) — режим между запусками не переключается, смена — --reset.
    # Для старых кампаний (поля в run_cfg нет) fallback на текущий CLI-аргумент
    # только в этом запуске: манифест не перезаписывается.
    if fresh:
        progress.setdefault("run_cfg", {})["skip_reviews"] = bool(skip_reviews)
    else:
        skip_reviews = bool(progress.get("run_cfg", {}).get(
            "skip_reviews", skip_reviews))

    # Пропуск товаров без отзывов (решение владельца 2026-10-07): видно по
    # плитке выдачи, страницу не открываем. По умолчанию ВКЛ — по решению
    # владельца; отключается --include-no-reviews. Так же, как skip_reviews,
    # при resume берётся из манифеста (источник истины).
    if fresh:
        progress.setdefault("run_cfg", {})["skip_no_reviews"] = (
            not include_no_reviews)
        skip_no_reviews = not include_no_reviews
    else:
        skip_no_reviews = bool(progress.get("run_cfg", {}).get(
            "skip_no_reviews", not include_no_reviews))
    if not skip_no_reviews:
        log.info("[key_daemon] ВНИМАНИЕ: собираю и товары без отзывов "
                 "(include_no_reviews)")

    # Размер выдачи воркеру (--batch-size, по умолчанию 1 товар за раз —
    # решение владельца). Фиксируется в run_cfg при создании кампании; при
    # resume берётся ИЗ МАНИФЕСТА (источник истины, инвариант 4). Явно
    # переданный --batch-size синхронизируется в run_cfg, как window_mode и
    # session_budget_wall_clock: это разовый параметр планировщика, а не
    # идентичность кампании, и без записи он терялся бы при следующем запуске.
    if fresh:
        progress.setdefault("run_cfg", {})["batch_size"] = max(1, int(
            batch_size or 1))
    elif batch_size is not None:
        rcfg = progress.setdefault("run_cfg", {})
        rcfg["batch_size"] = max(1, int(batch_size))

    try:
        # При resume конфиг берём из манифеста (источник истины), а не из текущих
        # аргументов запуска — чтобы лимиты/дедлайны не менялись между запусками.
        rcfg = progress.get("run_cfg", {})
        max_total_hours = rcfg.get("max_total_hours", max_total_hours)
        max_no_progress_runs = rcfg.get("max_no_progress_runs",
                                        max_no_progress_runs)
        max_rescans = rcfg.get("max_rescans", max_rescans)
        reserve_count = rcfg.get("reserve_count", reserve_count)
        # target_count и parallel — тоже из манифеста (инвариант 4), а не из
        # аргументов запуска: иначе стоп-условие цикла (valid >= target), срез
        # snapshot/reserve и шторм-эскалация зависели бы от --limit/--parallel
        # текущего запуска. CLI-дефолт --limit 300 на resume молча уводил
        # кампанию в долгий добор «до 300» (аудит 2026-10-15).
        target = int(progress.get("target_count", target))
        parallel = max(1, int(rcfg.get("parallel", parallel)))

        # session_budget_wall_clock: если передан аргументом запуска, синхронизируем
        # в run_cfg (позволяет менять лимит run между запусками, сохраняя
        # остальные лимиты из манифеста).
        if session_budget_wall_clock is not None:
            rcfg["session_budget_wall_clock"] = session_budget_wall_clock
            progress["run_cfg"] = rcfg
        if window_mode is not None:
            rcfg["window_mode"] = window_mode
            progress["run_cfg"] = rcfg
        # canary-url/reviews-api пишем только когда заданы: иначе run_cfg_hash
        # существующей кампании изменился бы и на ровном месте пометил манифест
        # как отредактированный вручную.
        if canary_url:
            rcfg["canary_url"] = canary_url
            progress["run_cfg"] = rcfg
        if reviews_api:
            rcfg["reviews_api"] = True
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
        # Resume после падения: pid, собранные ПОСЛЕ последнего сохранения
        # манифеста, лежат в inflight.json (чекпоинт пишет их во время
        # прогона). Возвращаем в discovered — иначе те же товары собираются
        # заново (часы браузерного времени впустую).
        _recovered = _reconcile_inflight(progress, camp_dir / INFLIGHT_NAME,
                                        data_dir, qa_fn=qa_fn, ctx="resume:")
        if _recovered:
            log.warning(f"resume: восстановлено {len(_recovered)} pid из "
                        f"{INFLIGHT_NAME} (манифест их не видел)")
            _emit_event(progress, "INFLIGHT_RECOVERED", pids=len(_recovered))
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
                # Штормы тоже инкрементят no_progress (единый предохранитель от
                # бесконечного кружения), но ДИАГНОЗ должен быть честным: если
                # последние прогоны — ФУЛЛ-штормы (storm_streak>0), причина не
                # «выдача перетёрлась», а блок Ozon. Иначе оператор читает
                # final_reason=no_progress при challenge_control.total_storms=3
                # и ищет проблему в выдаче, а не в анти-боте (живой случай
                # zvukoizolyatsiya 2026-09-25 давал ровно эту путаницу).
                # Частичные штормы (storm_streak==0) диагноз не меняют: там
                # блок одного воркера, а не кампании (ADR §14.6).
                cc_final = progress.get("challenge_control") or {}
                if cc_final.get("storm_streak"):
                    progress["final_reason"] = "challenge_storm"
                    progress["final_note"] = (
                        f"блок Ozon: {cc_final['storm_streak']} шторм(ов) подряд, "
                        f"лимит no-progress ({max_no_progress_runs}) исчерпан")
                else:
                    progress["final_reason"] = "no_progress"
                    progress["final_note"] = "лимит no-progress подряд исчерпан"
                log.warning(f"no-progress лимит → {progress['final_reason']}: "
                            f"{progress['final_note']}")
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
            checkpoint = _start_checkpoint(
                camp_dir, data_dir, CHECKPOINT_EVERY_S, campaign=campaign)
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
            except AccessChallengeError as exc:
                # Блок на СТРАНИЦЕ ВЫДАЧИ (main-сессия, search_page): воркеры
                # ещё не спавнились, combined не пишется → classify_status видел
                # только exit_code=1 и давал UNKNOWN_FAILURE, а демон молотил
                # заблокированную сессию транзиентными ретраями по 4с (живой
                # случай 2026-09-25, zvukoizolyatsiya: 4 прогона за 3 минуты,
                # FAB-reload ×3 → new_tab → recycle → всё то же «нет соединения»).
                # Помечаем как CHALLENGE_STORM: включается adaptive cooldown
                # 1/5/15 мин + эскалация NEEDS_ATTENTION(challenge_storm).
                sig = str(exc).split(": ", 1)[-1]
                log.error(f"in-process сбор: анти-бот на выдаче ({sig}) → "
                          f"CHALLENGE_STORM (cooldown вместо ретраев)")
                combined = {
                    "products": [], "found": 0,
                    "aborted": {"reason": "challenge_storm",
                                "where": "search_page", "signature": sig,
                                "all_workers": True},
                    "kpi": {"total_challenges": 1},
                }
                exit_code = 1
            except Exception as exc:
                log.error(f"in-process сбор упал: {exc}")
                # combined «пуст, но есть» + exit!=0 → UNKNOWN_FAILURE: прежнее
                # поведение subprocess (combined записан, код ненулевой) →
                # транзиентные ретраи → NEEDS_ATTENTION, а не тихий ABANDONED.
                combined = {"products": [], "found": 0, "inproc_crash": str(exc)}
                exit_code = 1
            finally:
                stop_checkpoint(checkpoint)
            committed = _commit_run_results(
                progress, progress_path, run_id, mode, targets, combined,
                exit_code, qa_fn, data_dir, camp_dir, before_disc, before_valid)
            if committed is None:
                break
            status, new_disc, new_valid, new_checked = committed

            # ---- реакция по статусу ----
            if status == "CHALLENGE_STORM":
                progress["challenge_seq"] = progress.get("challenge_seq", 0) + 1
                # Агрегация storm (ADR §14.6): шторм ОДНОГО воркера ≠ шторм всей
                # кампании. Только ФУЛЛ-шторм (parallel==1, либо ВСЕ воркеры
                # упёрлись) эскалирует storm_streak/profile_cooldown. Частичный
                # шторм (часть воркеров прогрессирует) — короткая пауза, БЕЗ
                # эскалации, чтобы один воркер не сжигал cooldown кампании.
                aborted = (combined or {}).get("aborted") or {}
                block_kind = _storm_block_kind(aborted)
                soft = block_kind == "soft"
                # soft (SESSION_EXHAUSTED) НЕ эскалирует, даже если штормнули все
                # воркеры: эскалация зарезервирована для hard-блокировок.
                all_stormed, escalate = _storm_escalates(aborted, parallel)
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
                    "last_challenge_signature") or aborted.get("signature")
                label = _storm_label(signature, aborted)
                cc = progress.get("challenge_control") or {}
                pause_s = CHALLENGE_PARTIAL_PAUSE_S
                if escalate:
                    cc = _bump_challenge_control(progress, signature)
                    pause_s = _challenge_cooldown_seconds(progress)
                    # ОДНА adaptive-пауза на оба cooldown'а (ADR §14.4/§14.5).
                    # Раньше cc/progress cooldown_until ставились по adaptive
                    # значению, а pc["until"] — по ПОСЛЕДНЕМУ шагу (15м), и цикл
                    # брал max(пауз) → лестница 1/5/15 не работала (всегда 15м
                    # уже с первого шторма, NEEDS_ATTENTION через ~час молотья).
                    until = (datetime.now(timezone.utc) +
                              timedelta(seconds=pause_s)).isoformat(
                                  timespec="seconds")
                    cc["cooldown_until"] = until
                    if not _NO_COOLDOWN:
                        pc = progress.get("profile_cooldown") or {}
                        pc["until"] = until
                        pc["reason"] = "repeated_challenge"
                        pc["challenge_count"] = cc.get("total_storms", 0)
                        pc["last_signature"] = signature
                        pc["last_full_storm_at"] = time.time()
                        progress["profile_cooldown"] = pc
                # Эскалация: при длинной серии штормов — NEEDS_ATTENTION
                # (не молотить), иначе adaptive пауза с джиттером.
                # KD_NO_COOLDOWN: не останавливаем кампанию, продолжаем обходить.
                max_storm = len(CHALLENGE_COOLDOWN_STEPS_S)
                if (not _NO_COOLDOWN and escalate
                        and cc.get("storm_streak", 0) > max_storm):
                    progress["final_state"] = "NEEDS_ATTENTION"
                    progress["final_reason"] = "challenge_storm"
                    progress["final_note"] = (
                        f"persistent challenge ({cc['storm_streak']} штормов)")
                    # last_error = причина для status.json: без него оператор
                    # видит только «persistent challenge» и не знает, ЧТО вернула
                    # Ozon (живой случай: FAB «Похоже, нет соединения» на
                    # странице выдачи, а в status.json было пусто).
                    progress["last_error"] = (
                        f"persistent challenge: {label} "
                        f"({cc['storm_streak']} штормов подряд, "
                        f"всего {cc.get('total_storms', 0)})")
                    log.error(f"persistent challenge → NEEDS_ATTENTION "
                        f"(storm_streak={cc['storm_streak']}, {label})")
                    _set_state(progress, "NEEDS_ATTENTION", "CHALLENGE_STORM",
                               run_id=run_id, storm_streak=cc["storm_streak"])
                    notify(progress, "NEEDS_ATTENTION: persistent challenge — "
                           f"{label[:60]}")
                    save_json(progress_path, progress)
                    break
                # pause_s уже посчитан выше (adaptive для ФУЛЛ-шторма,
                # CHALLENGE_PARTIAL_PAUSE_S для частичного) — не пересчитываем,
                # иначе второй вызов _challenge_cooldown_seconds дал бы другой
                # джиттер и cooldown_until разошёлся бы с profile_cooldown.
                if _NO_COOLDOWN:
                    pause_s = 1
                progress["cooldown_until"] = (
                    datetime.now(timezone.utc) +
                    timedelta(seconds=pause_s)).isoformat(timespec="seconds")
                scope = ("soft:SESSION_EXHAUSTED" if soft else
                         ("ФУЛЛ" if all_stormed else "частичный"))
                log.warning(f"challenge detected → пауза {int(pause_s)}с "
                    f"(kind={block_kind}, {scope}, "
                    f"storm_streak={cc['storm_streak']})"
                    f"{' ' + str(stormed_workers) if stormed_workers else ''}, "
                    f"challenge_seq={progress['challenge_seq']}")
                _set_state(progress, "COOLDOWN_CHALLENGE", "CHALLENGE_STORM",
                           run_id=run_id, pause=int(pause_s),
                           storm_streak=cc['storm_streak'],
                           block_kind=block_kind,
                           challenge_seq=progress['challenge_seq'])
                progress["last_error"] = (
                    f"challenge_storm ({scope}): "
                    f"{label} | streak={cc['storm_streak']}, "
                    f"всего={cc.get('total_storms', 0)}")
                notify(progress, f"challenge: пауза {int(pause_s)}с "
                       f"(kind={block_kind}, {scope}) — "
                       f"{label[:60]}")

            elif status == "CRASH":
                progress["no_progress_count"] = progress.get(
                    "no_progress_count", 0) + 1
                # короткий cooldown, чтобы не зациклить мгновенные краши без паузы
                crash_pause = 1 if _NO_COOLDOWN else BACKOFF_DELAYS_S[0]
                progress["cooldown_until"] = (
                    datetime.now(timezone.utc) +
                    timedelta(seconds=crash_pause)).isoformat(
                        timespec="seconds")
                log.warning(f"crash сбора (условия CRASH: exit!=0 без "
                    f"combined или весь пул воркеров fatal), exit="
                    f"{exit_code}, cooldown {crash_pause}с")
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

            elif status in ("NORMAL_DONE", "EMPTY_SEARCH"):
                # Стагнация: прогон завершился, но не дал ни одного нового пида
                # (ни новых discovery, ни новых valid). Неважно, был ли found>0 —
                # выдача перетирается известными товарами, дальше греться некуда.
                # Иначе при живой выдаче без роста демон крутился бы вечно.
                if status == "EMPTY_SEARCH":
                    # Пустая сетка ≠ перетёртая выдача: это проблема страницы
                    # (не отрисовалась), а не отсутствие новых товаров.
                    log.warning("прогон без товаров: сетка выдачи не "
                                "отрисовалась (EMPTY_SEARCH) — считаю "
                                "стагнацией, но это чинится проверкой URL/прокси")
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
            _write_digest(progress, data_dir, compact=compact)
        notify(progress, msg)
        return progress

    except CollectionInterrupted:
        # Graceful/forced Ctrl+C keeps its existing semantics (partial save /
        # emergency shutdown) — decisions were made in the loop above; this
        # exception must still propagate out of run_campaign as before.
        raise
    except Exception as exc:
        # Post-processing crash (QA timeout, malformed QA JSON, digest render)
        # must not kill the daemon process with a traceback: mark the campaign
        # NEEDS_ATTENTION so it stays resumable (audit 2026-10-18, B1).
        # SystemExit/KeyboardInterrupt are BaseException and propagate as-is.
        log.error(f"кампания: необработанная ошибка прогона: "
                  f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        try:
            progress["final_state"] = "NEEDS_ATTENTION"
            progress["final_reason"] = f"unhandled_{type(exc).__name__}"
            progress["final_note"] = str(exc)
            notify(progress, f"NEEDS_ATTENTION: {type(exc).__name__}: {exc}")
            save_json(progress_path, progress)
        except Exception:
            # The fallback itself must never escape: the daemon has to survive.
            pass
        return progress

    finally:
        lock.release()


def _write_digest(progress: dict, data_dir: str,
                  compact: Optional[bool] = None) -> None:
    """Markdown-дайджест собранной выдачи (по финальным accepted PID).

    Рендерит из лучших валидных raw на каждый pid (report.build_search_digest_combined
    + write_search_digest) в progress["out"]. Ничего не делает, если accepted
    PID пустые (например, ABANDONED без данных).

    compact: флаг краткого дайджеста. Если None — берётся из манифеста
    (progress["compact_digest"]); так resume у старых кампаний (без поля) может
    передать fallback-значение из CLI без записи в манифест.
    """
    from ozon_collector.report import (build_search_digest_combined,
                                       write_search_digest,
                                       write_search_digest_compact)
    pids = list(progress.get("campaign_accepted_pids")
                or progress.get("campaign_final_pids") or [])
    if not pids:
        return
    out = progress.get("out") or f"docs/digests/{progress['campaign']}.md"
    combined, _table = build_search_digest_combined(pids, data_dir)
    written = write_search_digest(combined, out)
    for path in written:
        log.info(f"дайджест сохранён: {path}")
    compact_flag = (compact if compact is not None
                    else bool(progress.get("compact_digest")))
    if compact_flag:
        root, ext = os.path.splitext(out)
        compact_out = f"{root}_compact{ext}"
        written = write_search_digest_compact(combined, compact_out, full_out=out)
        for path in written:
            log.info(f"краткий дайджест сохранён: {path}")


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
            _force_exit_guard()   # 2nd Ctrl+C: do not wait for the rest
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
                               "warn": qa.get("warn", 0),
                               "wall_limited": qa.get("wall_limited",
                                                    qa.get("wall_limited_count", 0))})
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
                          "checked_pids": len(pool),
                          "wall_limited": sum(b.get("wall_limited", 0)
                                              for b in qa_batches)}
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
                      "checked_pids": len(snapshot),
                      "wall_limited": sum(b.get("wall_limited", 0)
                                          for b in qa_batches)}
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
    # Мягкая остановка не значит «выдача выбрана целиком»: если часть товаров
    # из выдачи так и не получила результата, это входит в итог (жалоба
    # 2026-10-06: кампания отчиталась о нормальном завершении, не собрав даже
    # пятой части товаров).
    _pending_final = pending_pids(progress, _run_data_dir(progress))
    if _pending_final:
        _note = progress.get("final_note") or ""
        _extra = (f"без результата {len(_pending_final)} товаров из выдачи "
                  f"(вернутся в очередь при новом запуске)")
        progress["final_note"] = f"{_note}; {_extra}" if _note else _extra
        log.warning(f"итог: {_extra}")
    save_json(progress_path, progress)


# --- Автогенерация имени кампании из --url (naming, НЕ фильтр) ---------------
# Простая детерминированная транслитерация кириллицы в латиницу. Контракт:
# одинаковый вход всегда даёт одинаковое имя (имя кампании = стабильный slug).
# Управляется "контрактом" (фиксировано в тестах): ь/ъ удаляются, ё->e, й->y,
# х->kh, ц->ts, ч->ch, ш->sh, щ->shch. Приоритет источника имени:
#   text= в query -> сегмент категории (/category/<slug>-<id>/, ID отбрасывается)
#   -> campaign_<дата_время>. Явный --campaign всегда главнее и автоименование
#   НЕ вызывается. ВАЖНЫЙ ИНВАРИАНТ: text= используется ТОЛЬКО для имени —
#   для --match-title (фильтра названий) он никогда не подхватывается
#   (инцидент podarok_five: авто-фильтр отсекал всю выдачу).
_SLUG_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def _slugify(text: str, max_len: int = 50) -> Optional[str]:
    """Нормализация произвольной строки в устойчивый латинский slug.

    lower -> транслит -> всё кроме [a-z0-9_] -> '_' -> схлопывание '_' ->
    strip '_' -> обрезка до max_len (без завершающего '_').
    Возвращает None для пустого/состоящего только из недопустимых символов.
    """
    slug = re.sub(r"[^a-z0-9_]+", "_",
                  text.lower().translate(_SLUG_TRANSLIT)).strip("_")
    slug = re.sub(r"_+", "_", slug)[:max_len].rstrip("_")
    return slug or None


def campaign_from_url(url: str) -> tuple[Optional[str], str, Optional[str]]:
    """Извлечь имя кампании (slug) из Ozon URL поиска/категории.

    Возвращает (имя, name_source, name_source_value), где name_source:
      'url_text'    — имя из параметра text= (value=исходный текст)
      'url_category'— имя из сегмента категории без числового ID
      'timestamp'   — fallback campaign_<дата_время> (имя всегда непустое)
    """
    default = f"campaign_{datetime.now().strftime('%Y%m%d_%H%M')}"
    if not url:
        return default, "timestamp", None
    try:
        url = net.normalize_target_url(url)
    except ValueError:
        pass      # main() уже провалидировал; здесь защита при вызове из кода
    parsed = urlparse(url)
    # 1. text= (parse_qs уже декодирует percent-encoding и '+' — повторный
    #    unquote не нужен). Непустой после strip() и _slugify -> источник.
    qs = parse_qs(parsed.query)
    texts = qs.get("text")
    if texts:
        source_text = (texts[0] or "").strip()
        slug = _slugify(source_text) if source_text else None
        if slug:
            return slug, "url_text", source_text
    # 2. /category/<slug>-<id>/ ... — сегмент категории, числовой ID
    #    отбрасывается (mashinki-radioupravlyaemye-7151 -> mashinki_...).
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "category":
        seg = re.sub(r"-\d+$", "", parts[1])
        slug = _slugify(seg)
        if slug:
            return slug, "url_category", parts[1]
    # 3. Ничего полезного не извлеклось.
    return default, "timestamp", None


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="key-daemon", description=__doc__)
    ap.add_argument("--campaign", default=None,
                    help="имя кампании (каталог data/campaigns/<имя>). "
                         "Не задан — генерируется из text= в --url "
                         "(транслит, ~50 символов), иначе из сегмента "
                         "категории, иначе campaign_<дата_время>. "
                         "Влияет ТОЛЬКО на имя: text= для title-фильтра "
                         "никогда не используется")
    ap.add_argument("--url", default=None)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--min-valid", type=int, default=1,
                    help="минимум чистых PID для частичного успеха "
                         "(1 <= min_valid < --limit; для новых кампаний обязателен)")
    ap.add_argument("--out", default=None,
                    help="путь Markdown-дайджеста итога кампании "
                         "(по умолчанию docs/digests/{campaign}.md)")
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
    ap.add_argument("--no-tray", action="store_true",
                    help="не поднимать трей-пульт окон сбора. По умолчанию "
                         "трей стартует вместе с демоном и сам закрывается "
                         "вместе с ним (watchdog по pid); env OZON_TRAY=0 — то же")
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
                         "(по умолчанию создаётся в --out); отключает и "
                         "полный, и краткий (--compact) дайджесты")
    ap.add_argument("--compact", action="store_true",
                    help="дополнительно создать краткий Markdown-дайджест "
                         "({root}_compact.md рядом с --out): карточка товара + "
                         "цена + характеристики + итог звёзд, БЕЗ текстов "
                         "отзывов/описаний/OCR. --no-digest отключает оба файла.")
    ap.add_argument("--no-reviews", action="store_true",
                    help="быстрый режим: собирать ТОЛЬКО карточку товара, "
                         "отзывы не собираются (raw.reviews_skipped=true). "
                         "Вариант-рейтинг недоступен. Ортогонально --compact: "
                         "--no-reviews --compact — самый быстрый обзор. Флаг "
                         "фиксируется при создании кампании в манифесте "
                         "(run_cfg.skip_reviews); при resume взётся только из "
                         "манифеста, смена режима — через --reset.")
    ap.add_argument("--include-no-reviews", action="store_true",
                    help="собирать и товары БЕЗ отзывов/оценки. По умолчанию "
                         "такие товары отбрасываются по плитке выдачи, без "
                         "захода на страницу (заход стоит минуты, а отзывов "
                         "всё равно нет) — решение владельца 2026-10-07. "
                         "Флаг фиксируется при создании кампании "
                         "(run_cfg.skip_no_reviews); при resume берётся из "
                         "манифеста, смена режима — через --reset.")
    ap.add_argument("--batch-size", type=int, default=None, metavar="N",
                    help="сколько товаров воркер берёт за одну раздачу "
                         "(дефолт 1 — «грузчик берёт один товар»). Значение "
                         "фиксируется в run_cfg.batch_size при создании "
                         "кампании; при resume без флага берётся из манифеста, "
                         "с флагом — синхронизируется в манифест (как "
                         "--window-mode).")
    ap.add_argument("--canary-url", default=None, metavar="URL",
                    help="цель canary-пробы после access-recovery. По умолчанию "
                         "берётся URL текущей кампании (та же сущность, на "
                         "которой упали). Переопределяется env OZON_CANARY_URL. "
                         "Главная ozon.ru мягче карточки и может давать ложный "
                         "ok — указывай /product/ или /category/.")
    ap.add_argument("--reviews-api", action="store_true",
                    help="быстрый предсрез отзывов через Ozon JSON API "
                         "(ozon_fetch), в 10-100 раз быстрее DOM-скролла. "
                         "DOM остаётся источником истины: API лишь дополняет "
                         "список и не докручивает страницы, пока не докажет "
                         "покрытие rating_count. Off по умолчанию; env "
                         "OZON_REVIEWS_API=1 — тот же переключатель.")
    return ap.parse_args(argv)


def _emergency_shutdown(progress_path: Optional[Path] = None,
                        force: str = ""):
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


def _force_exit_guard() -> None:
    """FORCE (2nd Ctrl+C) during long QA phases: exit immediately.

    QA batches run for minutes; without a check inside the batch loops the
    force semantics ("exit now") waited for the whole phase to finish (audit
    2026-10-19). _emergency_shutdown never returns (os._exit).
    """
    if _FORCE_EXIT.is_set():
        _emergency_shutdown(force="qa")


def _validated_url(args, attr: str, flag: Optional[str] = None) -> str:
    """Нормализовать URL аргумента, warn при починке, ValueError при мусоре.

    Починка (markdown-обёртка, хвостовая пунктуация, пробелы) разрешена
    молча-но-громко: оператор видит в логе, ЧТО реально открыли. Не-URL
    (опечатка, 'javascript:', пусто) → ValueError → exit 2 до старта.
    """
    raw = getattr(args, attr, None)
    if not raw:
        return raw
    fixed = net.normalize_target_url(raw)
    if fixed != raw.strip():
        log.warning(f"[key_daemon] URL {flag or '--url'} поправлен: "
                 f"{raw.strip()[:90]!r} → {fixed[:90]!r}")
    return fixed


def apply_window_mode(window_mode: Optional[str]) -> None:
    """CLI --window-mode → env OZON_KEEP_WINDOW; None — режим не трогаем.

    Два канала режима окон: трей («Развернуть окна сбора») пишет флаг
    data/window_visible.flag, который key_daemon сбрасывает при старте сбора;
    CLI-режим идёт отдельным каналом — env, чтобы флаг-файл остался при
    трее. visible → OZON_KEEP_WINDOW=1 (сентинел и сторожа молчат: окна
    видимы, как обещает справка --window-mode); hidden → переменная пуста.
    Дочерние процессы (воркеры collect_parallel, cdp-сторожа, лаунчеры)
    наследуют env. Находка ревью 2026-10-17: раньше --window-mode visible
    только писался в run_cfg манифеста, а окна всё равно прятались.
    """
    if window_mode == "visible":
        os.environ["OZON_KEEP_WINDOW"] = "1"
        log.info("режим окна: visible (--window-mode) → OZON_KEEP_WINDOW=1")
    elif window_mode == "hidden":
        os.environ["OZON_KEEP_WINDOW"] = ""
        log.info("режим окна: hidden (--window-mode) → OZON_KEEP_WINDOW снят")


def window_mode_from_manifest(campaign: str) -> Optional[str]:
    """Режим окна resume-кампании из run_cfg манифеста (инвариант 4).

    Находка ревью 2026-10-17: при resume без --window-mode in-process
    воркеры получают keep_window=True из run_cfg видимой кампании, а env
    демона пуст — сентинел гасил их окна вопреки режиму кампании.
    """
    try:
        prog = load_json(CAMPAIGNS_DIR / campaign / "progress.json")
    except Exception:
        return None
    if not isinstance(prog, dict):
        return None
    rc = prog.get("run_cfg")
    if not isinstance(rc, dict):
        return None
    mode = rc.get("window_mode")
    return mode if mode in ("visible", "hidden") else None


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
    # P0 (ревизия 2.9): URL цели валидируется ДО прогона. Раньше markdown-ссылка
    # вида [текст](url) доходила до Page.goto -> 'Cannot navigate to invalid URL',
    # выдача 0 товаров и прогоны NORMAL_DONE с discovered=0 (тихая потеря
    # времени и ложное 'успешно').
    try:
        args.url = _validated_url(args, "url")
    except ValueError as e:
        print(f"key_daemon: неверный --url: {e}", file=sys.stderr)
        print("Подсказка: нужен чистый http(s)-адрес Ozon, например "
              "https://www.ozon.ru/category/.../ "
              "или https://www.ozon.ru/search/?text=...",
              file=sys.stderr)
        return 2
    if args.canary_url:
        try:
            args.canary_url = _validated_url(args, "canary_url", "--canary-url")
        except ValueError as e:
            print(f"key_daemon: неверный --canary-url: {e}", file=sys.stderr)
            return 2
    if args.campaign:
        campaign, name_source, name_source_value = args.campaign, "explicit", None
    else:
        campaign, name_source, name_source_value = campaign_from_url(args.url or "")
    # Лог — ДО трея, apply_window_mode и sentinel_start: до 2026-10-18 attach
    # жил только в run_campaign (ниже), и все строки старта («режим окна»,
    # «сентинел запущен», «сентинел НЕ запущен: …») пропадали — _write_file
    # при неподключённом файле молча роняет строку. run_campaign повторно не
    # аттачит, если путь совпал (log.current_path()), ротация та же — 5 МБ.
    # Дубликат запуска дописывает эти стартовые строки в лог живой кампании до
    # падения на PidLock — ожидаемая диагностика, не молчание.
    log.attach_file(LOGS_DIR / f"{campaign}_daemon.log", mode="a",
                    rotate_max_bytes=5 * 1024 * 1024)
    log.set_context("orchestrator")
    # CLI-режим окна ДО трея и сентинела: они и воркеры читают env. Без
    # --window-mode при resume режим берётся из run_cfg манифеста
    # (инвариант 4): иначе visible-кампания после resume получает
    # keep_window воркерам из run_cfg, а env демона пуст — сентинел гасит
    # их окна вопреки режиму кампании (находка ревью 2026-10-17).
    # Эффективный режим уходит и в run_campaign: при --reset на visible-
    # кампании старый манифест удаляется, и новый обязан сохранить режим
    # (иначе следующий resume снова спрячет окна — находка раунда 2).
    eff_window_mode = args.window_mode
    if eff_window_mode is None:
        eff_window_mode = window_mode_from_manifest(campaign)
    apply_window_mode(eff_window_mode)
    # Трей-пульт: рождается вместе со сбором, сам выходит после смерти демона
    # (см. _start_tray_companion) — отдельный резидент больше не нужен.
    _start_tray_companion(disabled=args.no_tray)
    # Сентинел показов: в скрытом режиме ни одно окно сбора не должно
    # появиться на экране (требование владельца 2026-10-17) — глобальный
    # WinEvent-хук + проход по окнам каждые 200 мс, живёт в процессе демона.
    try:
        from ozon_collector import window_policy as _window_policy
        _window_policy.sentinel_start()
    except Exception as exc:  # noqa: BLE001 — сентинел не роняет кампанию
        log.warning(f"сентинел показов не запустился: {exc}")
    out = args.out or f"docs/digests/{campaign}.md"
    progress = run_campaign(
        campaign=campaign, url=args.url or "", target=args.limit,
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
        window_mode=eff_window_mode,
        match_title=args.match_title,
        write_digest=not args.no_digest,
        compact=args.compact,
        skip_reviews=args.no_reviews,
        canary_url=args.canary_url,
        reviews_api=args.reviews_api,
        batch_size=args.batch_size,
        name_source=name_source,
        name_source_value=name_source_value)
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
            _drop_pid_file()
            import os as _os
            _os._exit(1)
        raise
    finally:
        _drop_pid_file()
