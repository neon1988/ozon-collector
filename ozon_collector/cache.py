"""Кеш собранных raw-данных: валидация, актуальность (TTL), совместимость.

Принципы:
- max_age_s == 0 означает «кеш выключен» — диск вообще не читается.
- Кеш-хит гарантирует: файл существует, raw полный (incomplete != True),
  collected_at_utc валиден и свеж, schema совместима, версия коллектора
  совпадает с текущей (старые сборки могли содержать ошибки сбора).
- Причины промаха различаются для логов (reason), наружу маппятся в
  компактное miss/stale/hit.
"""
from __future__ import annotations

import json
import os
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
                data_dir: str = "data") -> tuple[dict | None, str]:
    """Проверяет кеш {pid}_raw.json. Возвращает (raw|None, reason).

    reason: off | missing | incomplete | incompatible | old_version |
            newer_version | bad_timestamp | stale | hit
    """
    if max_age_s <= 0:
        return None, "off"

    path = raw_path(pid, data_dir)
    if not os.path.exists(path):
        return None, "missing"

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None, "missing"

    if raw.get("incomplete"):
        return None, "incomplete"

    if raw.get("schema_version") != SCHEMA_VERSION:
        return None, "incompatible"

    vs = version_status(raw.get("collector_version"))
    if vs in ("old", "invalid"):
        return None, "old_version"
    if vs == "newer":
        return None, "newer_version"

    collected_at = raw.get("collected_at_utc")
    if not collected_at:
        return None, "bad_timestamp"
    dt = _parse_utc(collected_at)
    if dt is None:
        return None, "bad_timestamp"

    age_s = (datetime.now(timezone.utc) - dt).total_seconds()
    if age_s > max_age_s:
        return None, "stale"

    return raw, "hit"
