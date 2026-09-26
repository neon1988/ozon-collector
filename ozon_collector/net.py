"""Сетевой контур: per-worker прокси (ревизия 2.9, ADR §18.7).

Зачем: 12 воркеров с одного IP — один общий фактор отказа. [HYPOTHESIS]
Qrator/FAB может резать частоту выдачи антибот-токенов на IP или /24, и
блокировка одного воркера снижает доверие ко всем остальным (эффект
«challenge_storm» по цепочке). Механизм не проверен — поэтому это НЕ дефолт,
а измеримая capability: включил `OZON_PROXY_URLS`, получил A/B по
`challenge_rate_per_100` (ADR §14.7), выключил — вернулся к baseline.

Контракт:
- `OZON_PROXY_URLS` — список через запятую/точку с запятой, индекс 1 = worker-1.
  Несовпадающий по длине список — warning и хвост воркеров идёт без прокси;
  это НЕ повод падать (иначе один лишний элемент ломает весь сбор).
- `OZON_PROXY_URL` — один прокси на всех воркеров (удобно для A/B «весь трафик
  через один IP»).
- Только `http`/`https`/`socks5`/`socks5h`, обязателен host:port.
- Креденшелы никогда не попадают в логи/телеметрию: наружу отдаётся
  `scheme://***@host:port` (`mask_proxy`).
"""

from __future__ import annotations

import os
import re
import urllib.parse

PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
_WORKER_RE = re.compile(r"worker-(\d+)")


def validate_proxy_url(url: str) -> str:
    """Проверить прокси-URL; вернуть нормализованный. Иначе ValueError.

    Нормализация: схема в нижний регистр, пробелы обрезаны, хвостовой `/`
    убран (Chrome не любит `http://host:port/`).
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("proxy url пуст")
    parsed = urllib.parse.urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in PROXY_SCHEMES:
        raise ValueError(f"схема {scheme or '?'!r} не поддерживается "
                         f"({', '.join(PROXY_SCHEMES)})")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("нет host")
    if not parsed.port:
        raise ValueError("нет порта (ожидается host:port)")
    netloc = f"{host}:{parsed.port}"
    if parsed.username:
        netloc = (f"{urllib.parse.quote(parsed.username, safe='')}:"
                  f"***@{netloc}")
    return urllib.parse.urlunsplit((scheme, netloc, "", "", ""))


def has_credentials(url: str) -> bool:
    try:
        return bool(urllib.parse.urlsplit(url).username)
    except Exception:
        return False


def mask_proxy(url: str | None) -> str | None:
    """Лог/телеметрия-версия: креденшелы заменены на `***`."""
    if not url:
        return None
    try:
        return validate_proxy_url(url)
    except ValueError:
        return "<invalid-proxy>"


def proxy_url_for_worker(worker_id: int) -> str | None:
    """Прокси для воркера k (1-based). None = без прокси (дефолт)."""
    worker_id = int(worker_id)
    raw = (os.environ.get("OZON_PROXY_URL") or "").strip()
    if raw:
        return raw
    lst = _proxy_list()
    if not lst:
        return None
    if worker_id > len(lst):
        return None
    return lst[worker_id - 1] or None


def _proxy_list() -> list:
    raw = os.environ.get("OZON_PROXY_URLS") or ""
    if not raw.strip():
        return []
    return [p.strip() for p in re.split(r"[,;\s]+", raw) if p.strip()]


def proxy_for_profile_token(profile_token: str | None) -> str | None:
    """Прокси по токену профиля: 'ozon-profile-worker-3' → прокси 3."""
    k = worker_index_from_profile_token(profile_token)
    return proxy_url_for_worker(k) if k else None


def worker_index_from_profile_token(profile_token: str | None) -> int | None:
    """Индекс воркера из токена профиля; None для главного профиля/мусора."""
    if not profile_token:
        return None
    m = _WORKER_RE.search(str(profile_token))
    return int(m.group(1)) if m else None


def chrome_proxy_arg(url: str) -> str:
    """Аргумент Chrome для прокси. Валидирует URL (fail-fast на опечатке).

    ВНИМАНИЕ: Chrome не применяет логин/пароль из URL для socks5 (нужен PAC
    или расширение), для http/https — применяет. `has_credentials` +
    предупреждение вызывающий код логирует, чтобы оператор не удивлялся
    «прокси есть, а трафик идёт мимо».
    """
    validate_proxy_url(url)
    return f"--proxy-server={url.strip().rstrip('/')}"


# ---------------------------------------------------------------------------
# Санитизация целевого URL (ревизия 2.9 — P0 hotfix)
# ---------------------------------------------------------------------------
# Живой случай 2026-09-26: в демон ушёл URL, скопированный из markdown:
#   [https://www.ozon.ru/category/zvukoizolyatsiya-33302/?...](https://www.ozon.ru/category/fonari-...-11061/?type=47720)
# Page.goto → "Cannot navigate to invalid URL", выдача 0 товаров, прогоны
# NORMAL_DONE с 0 discovered — молчаливая потеря времени. Поэтому URL
# нормализуется и валидируется НА ВХОДЕ (CLI/демон), а не «предупреждение
# в логе» на середине сбора.

_MD_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s]+)>?\s*\)")
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'\)\]]+")
_TRIM = " \t\r\n “”„«»\"'`"


def normalize_target_url(url: str) -> str:
    """Привести целевой URL к виду, пригодному для навигации.

    Что делает:
    - снимает markdown-обёртку `[текст](url)` (в т.ч. с пробелами/углами);
    - обрезает пробелы, «умные» кавычки, угловые скобки;
    - если URL не начинается с http(s) — достаёт первый http(s)-токен из
      текста (кейс «текст, потом ссылка»);
    - убирает хвостовую пунктуацию `. , ; : ) ]` (кейс копирования из
      сообщения/чата);
    - требует схему http/https и непустой host.

    Иначе ValueError с понятным текстом: молча пропускать нельзя — это
    стоит прогонов и «здоровых» NORMAL_DONE с нулём данных.
    """
    raw = (url or "").strip()
    for ch in _TRIM:
        raw = raw.strip(ch)
    if not raw:
        raise ValueError("URL цели пуст")
    m = _MD_LINK.fullmatch(raw)
    if m:
        raw = m.group(1).strip()
    else:
        m2 = _MD_LINK.search(raw)
        if m2:
            # markdown-ссылка в середине строки — берём её адрес
            raw = m2.group(1).strip()
    if not raw.lower().startswith(("http://", "https://")):
        m3 = _URL_IN_TEXT.search(raw)
        if m3:
            raw = m3.group(0)
    raw = raw.rstrip(".,;:)]}")
    parsed = urllib.parse.urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"не http(s) URL: {raw[:120]!r}")
    if not parsed.hostname:
        raise ValueError(f"нет host в URL: {raw[:120]!r}")
    if parsed.port is not None and not (0 < parsed.port < 65536):
        raise ValueError(f"порт вне диапазона: {parsed.port}")
    return urllib.parse.urlunsplit(
        (scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def is_valid_target_url(url: str) -> bool:
    try:
        normalize_target_url(url)
        return True
    except ValueError:
        return False
