"""Кеш собранных raw-данных: валидация, актуальность (TTL), совместимость.

Принципы:
- max_age_s == 0 означает «кеш выключен» — диск вообще не читается.
- Кеш-хит гарантирует: файл существует, raw полный (incomplete != True),
  collected_at_utc валиден и свеж, schema совместима, версия коллектора
  совпадает с текущей (старые сборки могли содержать ошибки сбора).
- Хит ищется среди ВСЕХ raw-файлов pid (канон + timestamp-файлы
  {pid}_raw.*.json): свежий прогон текущей версии не теряется, если канон
  занят старой версией (overwrite-защита Фазы B) — берётся лучший валидный.
- Причины промаха различаются для логов (reason), наружу маппятся в
  компактное miss/stale/hit.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone

from . import SCHEMA_VERSION, VERSION


def parse_max_age(s: str) -> int:
    """Парсит --max-age '1d'|'12h'|'6h'|'0' в секунды. '0' — кеш выключен.

    Допустимы только целые значения с одним суффиксом d/h/m. Дробные
    ('1.5d') и составные ('1h30m') — ValueError с понятным сообщением.
    """
    if not s:
        return 0
    v = s.strip().lower()
    if v == "0":
        return 0
    mult = {"d": 86400, "h": 3600, "m": 60}
    if v[-1] in mult and v[:-1].isdigit():
        return int(v[:-1]) * mult[v[-1]]
    raise ValueError(
        f"Неверный формат --max-age: '{s}'. "
        "Используйте '1d', '12h', '6h' или '0' (кеш выключен)."
    )


def _parse_utc(s: str) -> datetime | None:
    """Парсит ISO-дату в timezone-aware UTC. 'Z' и naive-даты → UTC.

    Возвращает None при битом значении (в т.ч. TypeError).
    """
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError, AttributeError):
        return None


def raw_path(pid: str, data_dir: str = "data") -> str:
    """Путь raw.json для pid: data/{pid}_raw.json."""
    return os.path.join(data_dir, f"{pid}_raw.json")


def atomic_write_text(path: str, text: str) -> None:
    """Атомарная запись текстового файла: tmp в той же директории + os.replace.

    Канонический файл всегда либо старый целиком, либо новый целиком —
    читатель никогда не увидит частично записанный файл.
    """
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=parent, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def atomic_write_json(path: str, raw: dict) -> None:
    """Атомарная запись JSON (с indent=2) — см. atomic_write_text."""
    atomic_write_text(path, json.dumps(raw, ensure_ascii=False, indent=2))


def raw_sha256(path: str) -> str | None:
    """SHA256 содержимого canonical raw, или None если файла нет/не читается.

    Fingerprint версии raw: более строгий, чем mtime. Используется для
    инвалидации QA-кэша и фиксации immutable snapshot'а (shared bytes).
    """
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def raw_save_path(pid: str, data_dir: str = "data",
                  overwrite: bool = False) -> str:
    """Путь для СОХРАНЕНИЯ raw.json с защитой от перезаписи (Фаза B).

    Канонический data/{pid}_raw.json перезаписывается только с
    overwrite=True. Иначе при существующем файле возвращается отдельный
    data/{pid}_raw.<ts>.<i>.json (новый прогон не трогает старый артефакт).
    """
    canon = os.path.join(data_dir, f"{pid}_raw.json")
    if overwrite or not os.path.exists(canon):
        return canon
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    for i in range(1, 10000):
        cand = os.path.join(data_dir, f"{pid}_raw.{ts}.{i}.json")
        if not os.path.exists(cand):
            return cand
    return cand


def _raw_candidates(pid: str, data_dir: str = "data") -> list[str]:
    """Существующие raw-файлы pid: канон + timestamp-файлы {pid}_raw.*.json.

    Канонический файл первым, остальные в алфавитном порядке имён. Это
    позволяет кешу видеть данные нового прогона даже когда канон занят
    старой версией коллектора (overwrite-защита, Фаза B).
    """
    canon = os.path.join(data_dir, f"{pid}_raw.json")
    out = [canon] if os.path.exists(canon) else []
    prefix = f"{pid}_raw."
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        return out
    for n in names:
        if n.startswith(prefix) and n.endswith(".json"):
            p = os.path.join(data_dir, n)
            if p not in out:
                out.append(p)
    return out


def _validate_raw(raw: dict, pid: str | None = None) -> str | None:
    """Причина непригодности raw для кеша; None — годен (полный, совместимый).

    Порядок проверок как в cache_check: incomplete → schema → версия →
    timestamp. pid — ожидаемый PID (необязательно): при передаче raw с другим
    pid (битый файл) тоже отклоняется. Негодные кандидаты не блокируют поиск
    лучшего файла.
    """
    if raw.get("incomplete"):
        return "incomplete"
    if pid is not None and str(raw.get("pid") or "") != str(pid):
        return "pid_mismatch"
    if raw.get("schema_version") != SCHEMA_VERSION:
        return "incompatible"
    vs = version_status(raw.get("collector_version"))
    if vs in ("old", "invalid"):
        return "old_version"
    if vs == "newer":
        return "newer_version"
    if not raw.get("collected_at_utc"):
        return "bad_timestamp"
    if _parse_utc(raw.get("collected_at_utc")) is None:
        return "bad_timestamp"
    return None


def _covers_sorts(raw: dict, sorts) -> bool:
    """Покрывает ли raw все запрошенные сортировки отзывов.

    raw["reviews_sorts"] — список сортировок, по которым собирался отзыв
    (None для старых сборок и для одиночного прохода «новые и полезные»).
    Если sorts запрошены (не None), кеш годен только когда raw покрывает
    их все (суперсет). Иначе старый raw без негативных (score_asc) отдавался
    бы как hit при новом дефолте sorts.
    """
    if not sorts:
        return True
    got = set(raw.get("reviews_sorts") or [])
    return got >= set(sorts)


def _best_raw(pid: str, data_dir: str = "data",
              sorts=None) -> tuple[dict | None, str]:
    """Лучший валидный raw среди канона и timestamp-файлов.

    Кандидаты ранжируются по (наличие отзывов, дата сбора, число отзывов):
    сначала — файлы с реальными данными (reviews_collected > 0): пустой/не
    дособранный stress-прогон (0 отзывов) не должен вытеснять полный raw
    только потому, что собран позже; среди файлов одного класса —
    свежайший, при равной дате — больший reviews_collected. Версия в ключе
    не участвует: _validate_raw уже допускает только текущую.

    sorts (необязательно) — запрошенные сортировки: кандидаты, НЕ покрывающие
    их (_covers_sorts), отбрасываются (причина "sorts_missing"). Возвращает
    (raw, reason): reason == "candidate" при валидном кандидате, иначе
    причина первой неудачной валидации (или "missing", если файлов нет).
    """
    best: dict | None = None
    best_key: tuple | None = None
    first_reason: str | None = None
    for path in _raw_candidates(pid, data_dir):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            continue
        reason = _validate_raw(raw, pid)
        if reason is not None:
            if first_reason is None:
                first_reason = reason
            continue
        if not _covers_sorts(raw, sorts):
            if first_reason is None:
                first_reason = "sorts_missing"
            continue
        dt = _parse_utc(raw.get("collected_at_utc"))
        reviews = int(raw.get("reviews_collected") or 0)
        key = (
            1 if reviews > 0 else 0,
            dt.timestamp() if dt is not None else 0.0,
            reviews,
        )
        if best is None or key > best_key:
            best, best_key = raw, key
    if best is None:
        return None, first_reason or "missing"
    return best, "candidate"


def _version_tuple(s) -> tuple[int, ...] | None:
    """'1.10.0' → (1, 10, 0). None при отсутствии или не-разборчивой строке.

    Численное сравнение важно: строковое '1.10.0' < '1.9.0' дало бы неверный
    результат для обычного лексикографического порядка.
    """
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        parts = s.strip().split(".")
        if not parts:
            return None
        tup = tuple(int(p) for p in parts)
    except ValueError:
        return None
    return tup if tup else None


def version_status(version) -> str:
    """Сравнивает версию кеша с текущей.

    Возвращает: 'ok' (совпадает) | 'old' (старее) | 'newer' (новее) |
    'invalid' (не указана или не парсится — считаем устаревшей).
    """
    cur = _version_tuple(VERSION)
    cached = _version_tuple(version)
    if cached is None:
        return "invalid"
    if cached > cur:
        return "newer"
    if cached < cur:
        return "old"
    return "ok"


def cache_is_compatible(raw: dict) -> bool:
    """Можно ли доверять кешу: schema совпадает И версия коллектора == текущей.

    collector_version обязательна: старые сборки (напр. 1.5.0) могли не иметь
    полей reviews_variant_mode / pagination_type или содержать ошибки
    экстракторов — их данные не используем как кеш, только пересобираем.
    """
    if raw.get("schema_version") != SCHEMA_VERSION:
        return False
    return version_status(raw.get("collector_version")) == "ok"


def cached_version(pid: str, data_dir: str = "data"):
    """Версия коллектора в кеше (для логов). None если файла нет или битый."""
    path = raw_path(pid, data_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("collector_version")
    except (OSError, ValueError):
        return None


def cache_check(pid: str, max_age_s: int,
                data_dir: str = "data",
                sorts=None) -> tuple[dict | None, str]:
    """Проверяет кеш raw (канон + timestamp-файлы). Возвращает (raw|None, reason).

    Ищет лучший валидный raw среди data/{pid}_raw.json и data/{pid}_raw.*.json
    (_best_raw): данные свежего прогона текущей версии не отбрасываются только
    потому, что канон занят старой версией коллектора.

    sorts (необязательно) — запрошенные сортировки отзывов: raw, собранный
    без них (напр. старый дефолт без score_asc), кешем не считается → "miss"
    и повторный сбор. Иначе канон вернёт raw без негативных отзывов.

    reason: off | missing | pid_mismatch | incomplete | incompatible |
            old_version | newer_version | bad_timestamp | sorts_missing |
            stale | hit
    """
    if max_age_s <= 0:
        return None, "off"

    raw, reason = _best_raw(pid, data_dir, sorts=sorts)
    if raw is None:
        return None, reason

    dt = _parse_utc(raw.get("collected_at_utc"))
    age_s = (datetime.now(timezone.utc) - dt).total_seconds()
    if age_s > max_age_s:
        return None, "stale"

    return raw, "hit"
