"""Сбор карточки товара и отзывов варианта с Ozon через CDP.

Схема raw-файла (JSON, schema_version 2.0, аддитивно расширяется):
  collector_version, schema_version, collected_at_utc, product_url, reviews_url,
  pid, incomplete, missing_fields[] (полные пути, напр. 'product.rating'),
  target_variant {Длина_см, Ширина_см, Высота_см},
  product {title, rating, rating_source, rating_count, price_text, price_parts, price_parsed,
           price {current, old, bank, confidence, source},
           characteristics, gallery, descriptions, infographics},
   reviews[], reviews_collected, reviews_expected, reviews_sorts,
   reviews_variant_mode {used, clicked, variant_present, variant_count, active,
                         total_count, reason, reviews_matched_variant,
                         reviews_dropped_by_client_filter},
   pages, review_pages, artifacts {dom_snapshot}, source,
   session {auth {logged_in, source, cookie_present}}
   (при sorts: у каждого отзыва review_sort — сортировка первого появления,
    review_sorts — список всех сортировок, где отзыв встретился)

session.auth объясняет недобор у сессионного потолка Ozon: logged_in —
авторизован ли профиль (_ozon_sat в куках = True, source="cookie"), None если
определить не удалось (source="dom"/"ambiguous"/"unknown"). Потолок зависит от
сессии и товара, а не от метода сбора: измерено 97 отзывов на сортировку
(2026-08-29) против 4 (2026-09-25, анонимная сессия) на одном и том же товаре
(pid 210502516, 257 707 отзывов). У товаров, собранных до появления детектора,
поля нет.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import random as _random
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from . import VERSION, SCHEMA_VERSION, DEFAULT_CDP_URL
from . import browser as B
from . import cache as K
from . import log
from . import ozon_fetch
from . import puzzle as _puzzle
from .constants import CHALLENGE_TITLE_MARKERS, title_challenge_marker
from .cdp import Session

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.7922.34 Safari/537.36"

# ---------------------------------------------------------------------------
# Параметры докрутки отзывов (tuning Phase A: скорость без потери данных)
# ---------------------------------------------------------------------------
SCROLL_STEP_PX = 1500           # px за один скролл (speed-тест 2026-08-16: 1500/400/6, missing=0 на 2 товарах; шаг 2000 → пропуск отзывов)
SCROLL_PAUSE_MS = 400           # базовая пауза скролла, мс (speed-тест 2026-08-16: 1500/400/6; было 1600, ускорение ~58%)
SCROLL_PAUSE_JITTER = 0.15      # ±15% случайная вариация паузы (антибот)
MAX_SCROLL_STABLE = 6          # плато без роста → конец загрузки (было 4; A/B: см. PROJECT_NOTES)
MAX_SCROLL_BOTTOM = 3          # финальная докрутка к низу (не менялось)
MAX_TOPUP_STEPS = 40           # шагов top-up в каждой фазе (вверх/вниз)
# «Тихий» ноль отзывов: Ozon разово не отдаёт блок отзывов при навигации на
# карточку (rate-limit/флак), скролл возвращает 0 без исключения, и проход
# считает товар «без отзывов». Такой 0 на ПЕРВОЙ странице прохода — подозрителен:
# reload страницы даёт второй шанс (докрутка повторяется до RETRY_EMPTY_PASSES).
EMPTY_RETRY_PASSES = 2         # reload-попыток при 0 отзывов на стр. 1 (0 — выключено)
EMPTY_RETRY_WAIT_MS = 4000     # пауза после reload перед повторной прокруткой

# Heartbeat-хук сбора (2026-09-01): контроль зависания воркера перенесён в
# ОРКЕСТРАТОР (parallel._await_workers), а не локальный сторож-поток. Воркер
# сообщает о прогрессе через _touch_watchdog(), а worker_main (parallel.py)
# перехватывает это heartbeat-hook'ом и шлёт {"t":"hb"} в pipe родителю.
# Родитель по простою heartbeat-сообщений (> WORKER_STALL_S) terminate'ит
# зависший воркер и перезапускает его (recovery) — единый путь и для
# parallel=1, и для parallel>1 (без костыля со сторожем внутри collect).
_active_heartbeat = None        # hook (callable, throttled), ставит worker_main


def set_heartbeat_hook(fn):
    """Установить функцию-хук прогресса (воркер шлёт heartbeat в pipe)."""
    global _active_heartbeat
    _active_heartbeat = fn


def _touch_watchdog():
    """Отметить прогресс сбора (heartbeat для оркестратора)."""
    if _active_heartbeat is not None:
        try:
            _active_heartbeat()
        except Exception:
            pass


# Порог «полных» variant_count: до 100 отзывов Ozon отдаёт все и без логина,
# собранное должно совпасть точно; выше — без логина видно ~96-100, требуем >=90.
VARIANT_COUNT_FULL_LIMIT = 100
VARIANT_COUNT_MIN_LARGE = 90
# Допуск недобора при vc<=100: стена логина «плавает» (~96-99) и на границе
# vc≈100 может отрезать 1-3 последних карточки (пример: 4483627458, 98/100).
# Зеркалит TOLERANCE_RATIO из scripts/qa_raw.py (UNDER_TOLERANCE).
VARIANT_TOLERANCE_RATIO = 0.03

# Per-sort: стена логина плавает по сортировкам (на одном товаре наблюдали
# published_at_desc=96 и score_asc=89), поэтому абсолютный порог даёт ложный
# брак с разницей в 1 отзыв. Оборванный проход ловим относительно лучшей
# сортировки: < 70% от неё — пассива залипла на полпути (15-30 против 96).
SORT_DEVIATION_RATIO = 0.7

# used=False с этими reason — реальный сбой активации варианта (брак);
# no_widget/no_variant_opt — легитимно (тумблера нет, список и так варианта).
VM_BAD_REASONS = {"click_error", "click_no_effect", "timeout", "disabled"}


def variant_review_problems(raw) -> list:
    """Брак варианта/числа отзывов по правилу «собранное = Этот вариант (N)».

    Возвращает список проблем ([] = ок):
    - reviews_variant_mode.used == False с reason из VM_BAD_REASONS;
    - variant_count <= 100 и собрано меньше с учётом допуска 3%
      (стена логина на границе vc≈100 отрезает ~1-3 карточки; пример:
      4483627458 — собрано 98/100, в DOM без логина доступно 96);
    - явный variant_count=0 при used=True — вариант без отзывов, не брак
      (кейс 5575856356: rating_count=370 принадлежит другим вариантам);
    - variant_count > 100 и собрано < 90 (кап без логина ~96-100);
    - variant_count > 100 и сортировка собрала < 70% от лучшей (каждый
      проход докручивается до стены логина; стена плавает по сортировкам,
      поэтому сравнение относительное, а не по абсолютному порогу).
    Если тумблера варианта нет (variant_count=null) — товар одновариантный,
    все отзывы его: ориентир берётся из карточки товара (rating_count).
    """
    if not isinstance(raw, dict):
        return ["raw_not_dict"]
    vm = raw.get("reviews_variant_mode") or {}
    n = len(raw.get("reviews") or [])
    vc = vm.get("variant_count")
    if isinstance(vc, int) and vc == 0 and vm.get("used"):
        # Явный ноль от виджета варианта: у выбранного варианта отзывов нет.
        # Подставлять rating_count всего товара нельзя — это чужие отзывы
        # других вариантов (ложный short(0/370, cap~100), кейс 5575856356).
        return []
    if not (isinstance(vc, int) and vc > 0):
        # Нет виджета варианта -> ожидаемый объём = число отзывов товара.
        rc = (raw.get("product") or {}).get("rating_count")
        if isinstance(rc, int) and rc > 0:
            vc = rc
    problems = []
    if not vm.get("used"):
        reason = vm.get("reason")
        if reason in VM_BAD_REASONS:
            problems.append(f"filter_not_used(reason={reason})")
    if isinstance(vc, int) and vc > 0:
        if vc <= VARIANT_COUNT_FULL_LIMIT:
            tol = max(1, round(vc * VARIANT_TOLERANCE_RATIO))
            if n < vc - tol:
                problems.append(f"short({n}/{vc})")
        elif n < VARIANT_COUNT_MIN_LARGE:
            problems.append(f"short({n}/{vc}, cap~100)")
        if vc > VARIANT_COUNT_FULL_LIMIT:
            per_sort: dict = {}
            for r in raw.get("reviews") or []:
                if not isinstance(r, dict):
                    continue
                ss = r.get("review_sorts")
                keys = ss if isinstance(ss, list) else [r.get("review_sort")]
                for s in keys:
                    if s:
                        per_sort[s] = per_sort.get(s, 0) + 1
            if len(per_sort) > 1:
                best = max(per_sort.values())
                for s in sorted(per_sort):
                    if per_sort[s] < best * SORT_DEVIATION_RATIO:
                        problems.append(
                            f"short_sort({s}={per_sort[s]}, best={best}, "
                            f"min {int(best * SORT_DEVIATION_RATIO)})")
    return problems

_scroll_rng = _random.Random()
SCROLL_JITTER_SEED: int | None = None


def set_scroll_jitter_seed(seed: int | None):
    """Задать детерминированный seed jitter-пауз (None = случайный при 1-м использовании)."""
    global SCROLL_JITTER_SEED
    SCROLL_JITTER_SEED = int(seed) if seed is not None else None
    if SCROLL_JITTER_SEED is not None:
        _scroll_rng.seed(SCROLL_JITTER_SEED)


def effective_scroll_jitter_seed() -> int:
    """Фактический seed текущего прогона (self-seed при None — для логирования)."""
    if SCROLL_JITTER_SEED is None:
        set_scroll_jitter_seed(_random.randrange(1 << 32))
    return SCROLL_JITTER_SEED


def scroll_pause_ms(base: int = SCROLL_PAUSE_MS,
                    jitter: float = SCROLL_PAUSE_JITTER) -> int:
    """Пауза скролла с равномерным jitter ±jitter% (детерминированный seed)."""
    effective_scroll_jitter_seed()
    if jitter <= 0:
        return int(base)
    low = int(base * (1 - jitter))
    high = int(base * (1 + jitter))
    return _scroll_rng.randint(low, high)


def fab_reload_delay_s(base_ms: int | None = None,
                       jitter: float | None = None) -> float:
    """Пауза FAB-reload с джиттером ±jitter% (тот же seeded-RNG, что скролл).

    Три фиксированные паузы 2.5с подряд — регулярный сигнал retry, который
    ловится и без фингерпринтинга. Seed сохранён: тесты/replay детерминированы.
    """
    if base_ms is None:
        base_ms = FAB_RELOAD_DELAY_MS
    if jitter is None:
        jitter = FAB_RELOAD_JITTER
    return scroll_pause_ms(base=base_ms, jitter=jitter) / 1000.0

# ---------------------------------------------------------------------------
# Сортировки отзывов (URL-параметр sort=)
# ---------------------------------------------------------------------------
# У Ozon ровно три сортировки в выдаче отзывов:
#   published_at_desc — «новые и полезные» (по умолчанию, параметр не нужен);
#   score_desc        — «сначала высокие оценки»;
#   score_asc         — «сначала низкие оценки».
# Параметр подтверждён на живой странице: ?tab=reviews&sort=score_desc /
# &sort=score_asc меняют выдачу и сохраняются в URL при пагинации.
SORT_PARAMS = {
    "published_at_desc": None,   # без параметра — сортировка по умолчанию
    "score_desc": "score_desc",
    "score_asc": "score_asc",
}
SORT_LABELS = {
    "published_at_desc": "новые и полезные",
    "score_desc": "сначала высокие оценки",
    "score_asc": "сначала низкие оценки",
}


def _sort_url(url: str, sort: str | None) -> str:
    """Добавляет/убирает URL-параметр sort (идемпотентно).

    sort=None — убрать параметр (сортировка «новые и полезные», по умолчанию).
    sort='score_desc'/'score_asc' — установить соответствующий параметр."""
    u = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    if sort:
        q["sort"] = sort
    else:
        q.pop("sort", None)
    return urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(q)))

# ---------------------------------------------------------------------------
# Collector JS lives in ozon_collector/js/*.js (audit 2026-10-18, step 9):
# the literals inflated the module to ~3100 lines. The text is unchanged —
# each constant equals the exact file content; _JS_FILES is the registry
# checked by a contract test.
# ---------------------------------------------------------------------------
_JS_DIR = Path(__file__).resolve().parent / "js"

_JS_FILES = {
    "PRODUCT_JS": "product.js",
    "REVIEWS_JS": "reviews.js",
    "REVIEW_IDS_JS": "review_ids.js",
    "SCROLL_TO_LAST_REVIEW_JS": "scroll_to_last_review.js",
    "SCROLL_STEP_UP_JS": "scroll_step_up.js",
    "SCROLL_STEP_DOWN_JS": "scroll_step_down.js",
    "EXPAND_REVIEWS_JS": "expand_reviews.js",
    "MARK_COMMENT_CARDS_JS": "mark_comment_cards.js",
    "VARIANT_MODE_JS": "variant_mode.js",
    "_CHALLENGE_LOOKS_JS": "challenge_looks.js",
    "_AGE_GATE_PROBE_JS": "age_gate_probe.js",
    "_PAGE_READY_JS": "page_ready.js",
    "_AGE_GATE_STATE_JS": "age_gate_state.js",
    "_AGE_GATE_CONFIRM_JS": "age_gate_confirm.js",
    "_AUTH_DOM_JS": "auth_dom.js",
    "_PAGE_GEOM_JS": "page_geom.js",
    "_CARDS_COUNT_JS": "cards_count.js",
}


def _load_js(fname: str) -> str:
    """JS file text from ozon_collector/js (UTF-8, no BOM)."""
    return (_JS_DIR / fname).read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# JS-извлечение карточки товара (включая структурированную цену price_parts)
# ---------------------------------------------------------------------------
PRODUCT_JS = _load_js("product.js")

# ---------------------------------------------------------------------------
# JS-извлечение отзывов текущей страницы
# ---------------------------------------------------------------------------
REVIEWS_JS = _load_js("reviews.js")


REVIEW_IDS_JS = _load_js("review_ids.js")


SCROLL_TO_LAST_REVIEW_JS = _load_js("scroll_to_last_review.js")


# ВАЖНО: маркер `step-up`/`step-down` живёт ВНУТРИ тела функции, а не перед
# ней. ffsel_js.js_is_function определяет функцию по началу строки, и ведущий
# `/*step-up*/` ломал определение: строка уходила в execute_script как
# выражение, возвращала объект-функцию и НЕ скроллила — top-up на Firefox
# («FakePage») не добирал отзывы молча. Маркер при этом остаётся в строке
# (docs/architecture/architecture.md, FakePage-подмены и тесты ищут его
# подстрокой), так что идентификация не пострадала.
SCROLL_STEP_UP_JS = _load_js("scroll_step_up.js")


SCROLL_STEP_DOWN_JS = _load_js("scroll_step_down.js")


EXPAND_REVIEWS_JS = _load_js("expand_reviews.js")


MARK_COMMENT_CARDS_JS = _load_js("mark_comment_cards.js")


VARIANT_MODE_JS = _load_js("variant_mode.js")


def normalize_text(s: str) -> str:
    """Нормализация текста отзыва: регистр, ё, пробелы."""
    s = re.sub(r"\s+", " ", (s or "").strip())
    s = s.replace("ё", "е").replace("Ё", "Е")
    return s


_NUMERIC_VALUE_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _variant_value_matches(value: str, text: str) -> bool:
    """Значение варианта найдено в описании отзыва.

    Числовые значения матчатся по границам цифр: '200' не должно находиться
    в '1200' (ревью 2026-10-15 — подстрочный поиск молча пропускал чужой
    вариант), а '90' внутри '90x6' — должно. Текстовые (цвет и т.п.) —
    подстрокой, как раньше.
    """
    if _NUMERIC_VALUE_RE.fullmatch(value):
        return re.search(rf"(?<![\d.,]){re.escape(value)}(?![\d.,])",
                         text) is not None
    return value in text


def _variant_matches(target_variant: dict | None, review: dict) -> bool:
    """Совпадает ли отзыв с описанием варианта (все непустые значения).

    Общий предикат фильтрации отзывов варианта (topup-добор и основной
    проход) — раньше дублировался в двух местах.
    """
    text = normalize_text(review.get("variant", ""))
    for key, val in (target_variant or {}).items():
        if val and not _variant_value_matches(str(val), text):
            return False
    return True


def parse_variant(spec: str) -> dict:
    """Разбор описания варианта для фильтрации отзывов.

    Два формата:
      1. Габариты '200x90x6' -> {'Длина_см': 200, 'Ширина_см': 90, 'Высота_см': 6}
         (порядок ДЛИНА x ШИРИНА x ВЫСОТА, как в названии товара).
      2. Произвольные пары 'ключ:значение' (через ';' или ',') ->
         {'Цвет': 'белый-прозрачный'}. Значение ищется подстрокой в variant-поле
         отзыва (для техники варианты по цвету/комплектации, не по габаритам).
    """
    s = spec.strip()
    if not s:
        return {}
    m = re.match(r"^\s*(\d+)\s*[xхХX*]\s*(\d+)\s*[xхХX*]\s*(\d+)\s*$", s)
    if m:
        return {
            "Длина_см": int(m.group(1)),
            "Ширина_см": int(m.group(2)),
            "Высота_см": int(m.group(3)),
        }
    if ":" in s:
        out = {}
        for part in re.split(r"[;,]", s):
            if ":" not in part:
                continue
            k, _, v = part.partition(":")
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
        if out:
            return out
    raise ValueError(
        f"Не удалось разобрать variant={spec!r}: ожидается '200x90x6' "
        f"или 'Цвет:белый-прозрачный'"
    )


def _t_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


CHALLENGE_BODY_MARKERS = (
    "fab_chlg_",
    "fab_cp_",
    "выключите vpn",
    "сопоставьте пазл",
    "двигая ползунок",
)


def detect_challenge(page) -> str | None:
    """Сигнатура анти-бот-страницы Ozon (FAB/captcha) или None.

    Смотрим document.title (дёшево) и body-текст (маркеры DOM). 0 отзывов
    НЕ является признаком челленджа — детектор полагается только на эти
    сигнатуры, чтобы не ронять реальные карточки без отзывов.
    """
    try:
        # Без timeout=: Playwright title() его не принимает, TypeError глушился
        # голым except — детект челленджа по title был мёртв (ревью
        # 2026-10-15); ffsel.title(timeout=...) значение всё равно игнорирует.
        title = (page.title() or "").strip().lower()
    except Exception:
        title = ""
    m = title_challenge_marker(title)
    if m:
        return f"title:{m}"
    try:
        # Без timeout=: у Playwright evaluate(expression, arg) — timeout это
        # TypeError, который глушился и превращал детект в None (ревью
        # 2026-10-15).
        body = (page.evaluate(
            "() => (document.body ? document.body.innerText : '')") or "")
        body = body.lower()
    except Exception:
        return None
    for m in CHALLENGE_BODY_MARKERS:
        if m in body:
            return f"dom:{m}"
    return None


FAB_RELOAD_ATTEMPTS = 3
FAB_RELOAD_DELAY_MS = 2500
FAB_RELOAD_JITTER = 0.15       # ±15% к паузе reload (антибот-ритм retry)

# Капча v10 приходит ПОЗЖЕ основной страницы: сначала «каркас» (пустые
# img#image/img#puzzle, captcha_v10.js заполняет src через 0.3–2s), а то и
# вообще с редиректом ?__rr=1 после первичной загрузки. Однократная проверка
# «сразу после goto» пазл не видит → собирается мусор (капча как товар).
# Поэтому, если страница выглядит «подозрительно» (нет контента товара, URL с
# __rr/antibot/слайдер) — переспрашиваем пазл несколько раз с паузой.
CHALLENGE_RECHECKS = 4
CHALLENGE_RECHECK_DELAY_S = 1.5

_CHALLENGE_LOOKS_JS = _load_js("challenge_looks.js")


def _looks_like_challenge(page) -> bool:
    """Есть ли основание ждать появление капчи (переспросить позже).

    TRUE — страница пока «тонкая»: нет заголовка товара, мало текста, или
    URL уже содержит признаки челленджа. На такой странице пазл может
    прийти с задержкой — зовём переспросы. FALSE — это нормальная карточка
    (есть h1 и контент): повторный опрос не нужен, лишних пауз нет.
    """
    try:
        return bool(page.evaluate(_CHALLENGE_LOOKS_JS))
    except Exception:
        return False


def _reload_fn(page):
    """Перезагрузить страницу (FAB-заглушка снимается обычным reload)."""
    try:
        page.reload(wait_until="domcontentloaded", timeout=60000)
    except Exception:
        try:
            page.reload(timeout=60000)
        except Exception:
            return False
    return True


def _challenge_sig(page) -> str | None:
    """Сигнатура челленджа: detect_challenge (текст/mаркеры) или каркас капчи.

    Каркас пазла (is_captcha_shell) — сам по себе признак челленджа: Ozon
    только начал инжект пазла (src ещё пустые), но страница УЖЕ анти-бот.
    FAB-заглушка детектится detect_challenge по маркерам.
    """
    sig = detect_challenge(page)
    if sig:
        return sig
    if _puzzle.is_captcha_shell(page):
        return "shell:captcha_v10"
    return None


def _ensure_no_challenge(page, where: str) -> None:
    """Жёстко: страница ДОЛЖНА быть без челленджа (для post-extract).

    Никаких решений/релоадов: если на странице есть пазл/заглушка/каркас —
    данные карточки невалидны (название/цена могли быть извлечены с капчи),
    сбор нельзя продолжать. Бросаем AccessChallengeError → recyclable.
    """
    sig = _challenge_sig(page)
    if sig:
        raise B.AccessChallengeError(f"анти-бот после сбора ({where}): {sig}")


# Заглушка «Похоже, нет соединения» вылезает НЕ сразу: по наблюдению с экрана
# 2026-09-30 через ~30s после неверного дропа (кусок встал в лунку, пазл
# исчез, и только потом показалась заглушка). Разовый _challenge_sig сразу
# после solve_puzzle возвращает None — маркеры в title/body ещё не отрисованы,
# #captcha уже нет. Из-за этого ветка FAB-reload была зацеплена за `if sig`
# и не срабатывала: range(0), страница просто висела.
CHALLENGE_SETTLE_S = 12.0
CHALLENGE_SETTLE_STEP_S = 1.0


def _await_challenge_sig(page, seconds: float = CHALLENGE_SETTLE_S):
    """Дождаться ПОЗДНЕЙ заглушки, если страница выглядит заблокированной.

    Возвращает сигнатуру или None. Если страница отдала контент (есть h1 и
    текст) — возвращает None сразу, не тратя паузу: успех не нужно
    подтверждать polling'ом. Ожидание платится только когда страница тонкая.
    """
    if not _looks_like_challenge(page):
        return None
    t0 = time.monotonic()
    sig = _challenge_sig(page)
    while not sig and time.monotonic() - t0 < seconds:
        time.sleep(CHALLENGE_SETTLE_STEP_S)
        sig = _challenge_sig(page)
    return sig


def check_challenge(page, where: str, reload_on_fab: bool = True) -> None:
    """Поднять AccessChallengeError, если страница — анти-бот-заглушка.

    reload_on_fab=True: FAB-заглушка Ozon («Похоже, нет соединения») снимается
    простым обновлением страницы (подтверждено вручную: reload открывает Ozon).
    Делаем до FAB_RELOAD_ATTEMPTS перезагрузок с паузой; если FAB остаётся —
    бросаем AccessChallengeError.

    Капча v10 появляется с задержкой (каркас → инжект src через 0.3–2s, или
    редирект ?__rr=1 после загрузки). Если сразу челленджа нет, но страница
    выглядит «тонкой» (нет контента товара) — переспрашиваем до
    CHALLENGE_RECHECKS раз с паузой: пазл должен быть виден к моменту сбора,
    а не собран как товар. Нормальные карточки (есть h1/контент) дополнительных
    пауз не получают.

    Если после переспросов страница всё ещё «тонкая», но маркеров капчи нет —
    это неразрендеренная/заблокированная страница: AccessChallengeError, а не
    пустая карточка (иначе кампания собирает мусор и тратит слот).
    """
    sig = _challenge_sig(page)
    for _ in range(CHALLENGE_RECHECKS) if (not sig and _looks_like_challenge(page)) else range(0):
        time.sleep(CHALLENGE_RECHECK_DELAY_S)
        sig = _challenge_sig(page)
        if sig:
            break
    if sig and (_puzzle.is_puzzle(sig) or _puzzle.looks_like_puzzle(page)):
        # Слайдер-пазл (НЕ FAB-заглушка): сначала пытаемся РЕШИТЬ, а не
        # перезагружать вслепую. Успех → пазл снят, никакого AccessChallenge
        # (не тратим бюджет challenge/new_tab/recycle). DOM-детекция покрывает
        # случаи, когда сигнатура — общий 'antibot captcha', но на странице
        # реально есть фон+кусочек пазла.
        if _puzzle.solve_puzzle(page):
            # Пазл снят — но это ещё не доказательство, что доступ выдан.
            # Заглушка может вылезти ПОЗЖЕ (~30s, наблюдение 2026-09-30), и
            # раньше мы возвращались наружу сразу, не проверив страницу.
            late = _await_challenge_sig(page)
            if not late:
                log.warning(f"[collect] пазл решён ({where})")
                return
            sig = late
            log.warning(f"[collect] после решения пазла вылезла заглушка "
                        f"({late}, {where}) → reload")
        else:
            _puzzle.note_storm()
            log.warning(f"[collect] пазл не решён за {_puzzle.failures()} "
                        f"попыток ({where}) → ждём заглушку, FAB-reload | "
                        f"{_puzzle.puzzle_summary()}")
            if _puzzle.last_was_timeout():
                # Бюджет пазла исчерпан = вкладка ЗАВИСЛА, а не «капча сложная».
                # Ждать здесь заглушку (12 с _await_challenge_sig) и делать до
                # трёх FAB-reload в зависшей вкладке — чистая трата: живой замер
                # 2026-10-02 показал, что та же капча решилась за 10 с на новой
                # вкладке, а провал шёл 178 с. Отдаём товар в лестницу сразу, где
                # new_tab как раз и снимает проблему.
                try:
                    page.is_invalid = True
                except Exception:
                    pass
                log.warning(f"[collect] бюджет пазла исчерпан — не жду заглушку, "
                            f"ухожу в лестницу (new_tab → recycle); это отказ "
                            f"сессии, не бан по IP | "
                            f"{_puzzle.puzzle_summary()}")
                raise B.AccessChallengeError(
                    f"пазл исчерпал бюджет времени ({where}): вкладка зависла "
                    f"| {_puzzle.puzzle_summary()} | лечение: new_tab → recycle; "
                    f"это отказ сессии, не бан по IP")
            sig = _await_challenge_sig(page) or _challenge_sig(page)
    for _ in range(FAB_RELOAD_ATTEMPTS) if (sig and reload_on_fab) else range(0):
        log.warning(f"[collect] FAB-заглушка ({sig}) → reload для разблокировки")
        _reload_fn(page)
        time.sleep(fab_reload_delay_s())
        sig = _challenge_sig(page)
        if not sig:
            break
    if sig:
        # INVALID (ADR §14.1): страница-заглушка не годится для извлечения.
        # Любые evaluate/scroll/goto на ней блокируются (Session._guard_check).
        try:
            page.is_invalid = True
        except Exception:
            pass
        # ПРИЧИНА ОТКАЗА В ЛОГ — разбор инцидентов (2026-10-02). Заглушка Ozon
        # не означает бан по адресу: тот же адрес в 16:03 отдал NORMAL_DONE
        # (discovered 336, valid 10), а в 17:22 дал 403 у всех 8 воркеров сразу,
        # при том что Ozon из другого браузера открывался нормально. Это отказ
        # СЕССИИ, лечится сменой сессии (reload → new_tab → recycle). Поэтому в
        # сообщении пишем наблюдаемые факты, а не лозунг про IP.
        raise B.AccessChallengeError(
            f"анти-бот ({where}): {sig} | {_puzzle.puzzle_summary()} | "
            f"лечение: FAB-reload → new_tab → recycle; это отказ сессии, "
            f"не бан по IP")
    if _looks_like_challenge(page):
        # Маркеров капчи нет, но страница так и не отрисовалась: нет h1 и
        # почти нет текста даже после CHALLENGE_RECHECKS переспросов. Считать
        # с неё нечего — извлечение вернёт пустую карточку, а пустой raw
        # засоряет кампанию и жжёт слот (замер 2026-09-25: widgets=0, h=805).
        # Классифицируем как челлендж: бюджет тратится на сессию, а не на
        # мусорные товары.
        try:
            page.is_invalid = True
        except Exception:
            pass
        raise B.AccessChallengeError(f"страница не отрисована ({where}): нет h1/контента"
                                    f" | {_puzzle.puzzle_summary()}")


_AGE_GATE_PROBE_JS = _load_js("age_gate_probe.js")

# Готовность карточки: если виджеты цены/рейтинга/отзывов уже на месте, страница
# отрисовалась и модалки 18+ не будет — значит, ждать нечего.
_PAGE_READY_JS = _load_js("page_ready.js")

# Ждать появления модалки (замер 2026-10-03). Проба была ОДНОКРАТНОЙ, сразу
# после check_challenge, а модалка 18+ рендерится позже загрузки. Из-за этого
# на части карточек (2859784413/1639540706) проба возвращала False, гейт не
# проходился, и страница отзывов оставалась пустой: stop_reason
# no_cards_no_count_hint, 0 отзывов, у карточки не оказалось даже цены и
# рейтинга. При этом текст «Подтвердите возраст» в DOM присутствовал.
#
# Ждём ТОЛЬКО когда страница не отрисована: у нормальных карточек виджеты уже
# есть и ожидания не происходит, поэтому на 336 товарах лишних секунд нет.
_AGE_GATE_WAIT_MS = 2500
_AGE_GATE_POLL_MS = 250

# Проверка РЕЗУЛЬТАТА подтверждения. Прежде код нажимал кнопку, ждал 1.5 с и
# писал «возрастной гейт пройден» — НЕ ПРОВЕРЯЯ, ушла ли модалка (наблюдение
# пользователя 2026-10-03: «пишет, что возрастной гейт пройден, но на самом деле
# не пройден, страница так и зависает»). След в данных: карточки 483601244,
# 4818407500, 1701665916 дали 0 отзывов и incomplete=True при формально
# «успешном» гейте.
#
# Гейт считается пройденным, когда модалка УШЛА или на странице появились
# виджеты товара. Второе нужно потому, что после подтверждения Ozon перерисовывает
# страницу и текст модалки может ещё какое-то время оставаться в DOM.
_AGE_GATE_STATE_JS = _load_js("age_gate_state.js")

_AGE_GATE_CONFIRM_JS = _load_js("age_gate_confirm.js")

# Дата рождения в маске ##.##.#### — вводим с разделителями. Прежний ввод
# «01011990» без точек опирался на то, что маска расставит их сама; на части
# карточек этого не происходит, поле остаётся пустым, кнопка не подтверждает
# дату, модалка не уходит — и код рапортовал успех (до проверки результата).
_AGE_GATE_DATE = "01.01.1990"

_AGE_GATE_VERIFY_MS = 4000


def _age_gate_confirmed(page, wait_ms: int = _AGE_GATE_VERIFY_MS) -> tuple:
    """Ждёт фактического подтверждения гейта. Возвращает (ok, причина).

    ok=True только если модалка исчезла ИЛИ появились виджеты товара. Иначе False
    с наблюдаемым состоянием, чтобы разбор не гадал.
    """
    last = {"gate": True, "ready": False}
    deadline = time.monotonic() + wait_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            last = page.evaluate(_AGE_GATE_STATE_JS) or last
        except Exception:
            last = {"gate": True, "ready": False}
        if not last.get("gate") or last.get("ready"):
            return True, ("модалка ушла" if not last.get("gate")
                          else "виджеты товара появились")
        page.wait_for_timeout(_AGE_GATE_POLL_MS)
    if last.get("gate") and not last.get("ready"):
        return False, "модалка осталась на месте"
    return False, f"состояние неизвестно: {last}"


def _pass_age_gate(page, wait_ms: int = _AGE_GATE_WAIT_MS,
                   attempts: int = 2) -> bool:
    """Пройти модалку «Подтвердите возраст» (18+ категории).

    Маска даты ДД.ММ.ГГГГ не принимает программный value — вводим
    дату рождения посимвольно и жмём «Подтвердить». Возвращает True,
    если гейт был обнаружен и пройден.
    """
    try:
        if not page.evaluate(_AGE_GATE_PROBE_JS):
            # Модалки нет — но, возможно, она ещё не отрисовалась. Ждём только
            # если страница явно не готова (нет виджетов цены/рейтинга/отзывов):
            # иначе ждать бессмысленно и каждый товар получит лишние секунды.
            try:
                ready = bool(page.evaluate(_PAGE_READY_JS))
            except Exception:
                ready = True   # не смогли определить — не ждём
            if not ready and wait_ms > 0:
                deadline = time.monotonic() + wait_ms / 1000.0
                while time.monotonic() < deadline:
                    page.wait_for_timeout(_AGE_GATE_POLL_MS)
                    if page.evaluate(_AGE_GATE_PROBE_JS):
                        log.debug("[collect] age-gate: модалка 18+ догрузилась "
                                  f"через ждание ({wait_ms}мс бюджет)")
                        break
                else:
                    log.warning("[collect] age-gate: страница не отрисована и "
                                f"модалка 18+ не появилась за {wait_ms}мс — "
                                f"возможно, отзывы скрыты гейтом; иду дальше")
                    return False
            else:
                return False
        # Ввод идёт в поле МОДАЛКИ, а не в первый видимый input страницы: в шапке
        # Ozon всегда есть поисковая строка, и прежний `input:visible` .first
        # уводил дату туда. Разметка модалки (DOM-снимок 2026-09-08):
        #   [data-widget="userAdultModal"] > div[name=birthdate][mask=##.##.####]
        #     > ... > input
        #   > button: «Подтвердить»
        _MODAL_SEL = '[data-widget="userAdultModal"]'
        _field = None
        for _sel in (f"{_MODAL_SEL} input", f"{_MODAL_SEL} [contenteditable]",
                     "[name=birthdate]", "input:visible"):
            try:
                _loc = page.locator(_sel).first
                if _loc.count() > 0:
                    _field = _loc
                    break
            except Exception:
                continue
        if _field is None:
            log.warning("[collect] age-gate: модалка есть, но поле ввода не "
                        "найдено (селекторы: input/[contenteditable]/birthdate)")
            return False
        btn = page.get_by_role("button", name="Подтвердить")
        last_why = ""
        # Попыток две: первый клик может не сработать (Ozon слушает
        # pointerdown/up, а не click), и повтор с вводом заново часто проходит.
        for attempt in range(1, max(1, attempts) + 1):
            _field.click()
            # Очистить перед вводом: маска могла оставить прошлые символы, и
            # новая дата тогда не встанет в начало поля.
            try:
                _field.press("Control+a")
                _field.press("Delete")
            except Exception as e:
                if B.is_renderer_failure(e):
                    raise
            for _chunk in (_AGE_GATE_DATE, _AGE_GATE_DATE.replace(".", "")):
                try:
                    _field.press_sequentially(_chunk, delay=40)
                except Exception as e:
                    if B.is_renderer_failure(e):
                        raise
                    log.warning(f"[collect] age-gate press_sequentially warn: {e}")
                    _field.type(_chunk, delay=40)
                try:
                    _st = page.evaluate(_AGE_GATE_CONFIRM_JS) or {}
                    if _st.get("value"):
                        break
                except Exception:
                    pass
            # Поле должно содержать дату — иначе кнопка не подтвердит ничего.
            try:
                _st = page.evaluate(_AGE_GATE_CONFIRM_JS) or {}
            except Exception:
                _st = {}
            if not _st.get("value"):
                last_why = f"поле даты осталось пустым (селектор {_sel})"
                log.warning(f"[collect] age-gate: {last_why} "
                            f"(попытка {attempt}/{attempts})")
                continue
            try:
                btn.wait_for(state="visible", timeout=5000)
            except Exception as e:
                if B.is_renderer_failure(e):
                    raise
                log.warning(f"[collect] age-gate button wait warn: {e}")
            if not btn.is_enabled():
                last_why = "кнопка Подтвердить осталась disabled"
                log.warning(f"[collect] age-gate: {last_why} "
                            f"(попытка {attempt}/{attempts})")
                continue
            # ПОДТВЕРЖДЕНИЕ. Порядок важен и проверен на живых прогонах:
            #   1) реальный клик мышью по координатам кнопки (page.mouse.click) —
            #      это ДОВЕРЕННОЕ событие через CDP, его принимает Vue-обработчик
            #      Ozon;
            #   2) Enter в поле даты — дешёвый запасной путь;
            #   3) JS-dispatch pointerdown/mousedown/... — оставлен как последний,
            #      он даёт недоверенное событие и на части карточек не срабатывал
            #      вовсе (замер 2026-10-03: «подтверждение не сработало, модалка
            #      осталась на месте», поле при этом было заполнено).
            _clicked_ok = False
            try:
                _box = btn.element_handle()
                _rect = _box.evaluate(
                    """(el) => { const r = el.getBoundingClientRect();
                        return {x: r.x, y: r.y, w: r.width, h: r.height}; }"""
                ) if _box is not None else None
                if _rect and _rect.get("w") and _rect.get("h"):
                    page.mouse.click(_rect["x"] + _rect["w"] / 2.0,
                                     _rect["y"] + _rect["h"] / 2.0)
                    _clicked_ok = True
            except Exception as e:
                if B.is_renderer_failure(e):
                    raise
                log.debug(f"[collect] age-gate: реальный клик не удался: {e}")
            if not _clicked_ok:
                try:
                    _field.press("Enter")
                except Exception as e:
                    if B.is_renderer_failure(e):
                        raise
                    log.debug(f"[collect] age-gate: Enter не удался: {e}")
            ok, why = _age_gate_confirmed(page)
            if ok:
                log.info(f"[collect] возрастной гейт 18+ пройден "
                         f"(попытка {attempt}/{attempts}, {why})")
                return True
            last_why = why
            log.warning(f"[collect] age-gate: подтверждение не сработало "
                        f"(попытка {attempt}/{attempts}): {why}")
        log.warning(f"[collect] возрастной гейт 18+ НЕ пройден — {last_why}")
        return False
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        log.warning(f"[collect] age-gate warn: {e}")
        return False


_AUTH_COOKIE_NAME = "_ozon_sat"
_AUTH_DOM_JS = _load_js("auth_dom.js")


def _page_cookies(page) -> Optional[list]:
    """Куки текущей страницы: Playwright page.context.cookies() или
    Selenium get_cookies(). None = куки недоступны (не «пусто»)."""
    for getter in (lambda: page.context.cookies(), lambda: page.get_cookies()):
        try:
            return list(getter())
        except Exception:
            continue
    return None


def detect_auth_state(page) -> dict:
    r"""Состояние авторизации профиля: {logged_in, source, cookie_present}.

    Зачем: у неавторизованного профиля Ozon режет выдачу отзывов, и величина
    среза зависит от сессии и товара — на pid 210502516 (257 707 отзывов)
    2026-08-29 отдавали 97 отзывов на сортировку, а 2026-09-25 анонимная
    сессия отдаёт 4 (DOM и JSON API одинаково). Пока это не помечено в raw,
    недобор неотличим от сбоя скролла — и QA не может это объяснить.
    Источник 1 — кука _ozon_sat (HttpOnly, из JS не видна → читаем куки
    браузера). Источник 2 — DOM: аноним показывает «Войти» (/auth/),
    авторизованный — «Мой Ozon» (/my/). Ничего не сработало →
    logged_in=None, source="unknown".

    Никогда не бросает: детект не должен ронять сбор.
    """
    out = {"logged_in": None, "source": "unknown", "cookie_present": None}
    cookies = _page_cookies(page)
    if cookies is not None:
        names = {str(c.get("name") or "") for c in cookies if isinstance(c, dict)}
        out["cookie_present"] = _AUTH_COOKIE_NAME in names
    dom = None
    try:
        dom = page.evaluate(_AUTH_DOM_JS)
    except Exception as e:
        log.debug(f"[collect] auth dom probe warn: {type(e).__name__}")
    if isinstance(dom, dict):
        has_login = bool(dom.get("hasLogin"))
        has_account = bool(dom.get("hasAccount"))
        if out["cookie_present"] is not None:
            out["logged_in"] = out["cookie_present"]
            out["source"] = "cookie"
        elif has_account and not has_login:
            out["logged_in"] = True
            out["source"] = "dom"
        elif has_login and not has_account:
            out["logged_in"] = False
            out["source"] = "dom"
        elif has_account and has_login:
            out["source"] = "ambiguous"
    elif out["cookie_present"] is not None:
        out["logged_in"] = out["cookie_present"]
        out["source"] = "cookie"
    log.info(f"[collect] auth: logged_in={out['logged_in']} "
             f"source={out['source']} cookie_present={out['cookie_present']}")
    return out


def collect_product(page, product_url: str, category: str | None = None) -> dict:
    product = page.evaluate(PRODUCT_JS)
    if not product:
        raise RuntimeError("JS-извлечение карточки вернуло None (антибот?)")
    title = (product.get("title") or "").strip().lower()
    m = title_challenge_marker(title)
    if m:
        raise B.AccessChallengeError(f"анти-бот (product.title): {m}")
    if category:
        product["category"] = category
    return product


def _next_page(page) -> Optional[str]:
    """URL следующей страницы отзывов, '__IN_PLACE__' или None.

    Приоритет:
      1. state-paginator (data-state JSON) — только если fetchType != 'scroll'
         (scroll-подгрузка обрабатывается _scroll_to_converge);
      2. webListReviews — ссылка «Дальше» или кнопка-клик (старая разметка);
      3. fallback: любая ссылка с layout_page_index.
    Возвращает абсолютный URL; для кнопочной пагинации кликает «Дальше» сама
    (единственный клик за итерацию) и возвращает '__IN_PLACE__': страница
    обновилась на месте, вызывающий цикл обязан пропустить goto."""
    try:
        sp = page.evaluate(
            """() => {
              const el = document.querySelector('[id^="state-paginator"]');
              if (!el) return null;
              try {
                const d = JSON.parse(el.getAttribute('data-state'));
                if (d.fetchType === 'scroll') return null;
                return d.nextPage || null;
              } catch (e) { return null; }
            }""",
                )
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        sp = None
    if sp:
        if sp.startswith("/"):
            return "https://www.ozon.ru" + sp
        if sp.startswith("http"):
            return sp
    legacy = next_url(page, click=False)
    if legacy == "__CLICK__":
        # Кнопочная пагинация: страница обновляется НА МЕСТЕ. Кликаем здесь
        # (единственный клик за итерацию) и возвращаем сентинел — цикл сбора
        # пропускает goto, иначе страница откатилась бы на стр.1.
        raced = next_url(page, click=True)
        return raced or "__IN_PLACE__"
    if legacy:
        return legacy
    try:
        href = page.evaluate(
            """() => {
              const a = document.querySelector('a[href*="layout_page_index="]');
              return a ? a.getAttribute('href') : null;
            }""",
                )
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        href = None
    if href:
        if href.startswith("/"):
            return "https://www.ozon.ru" + href
        if href.startswith("http"):
            return href
    return None


def next_url(page, *, click: bool = True) -> Optional[str]:
    """URL следующей страницы отзывов или None.

    click=False — только обнаружение пагинации, без побочных эффектов: для
    кнопочной разметки («Дальше» без href) возвращается сентинел '__CLICK__'.
    Раньше детекция (_detect_pagination) кликала кнопку как побочный эффект —
    страница уезжала на стр.2 ДО извлечения стр.1 (ревью 2026-10-15).
    click=True — выполнить клик и дождаться обновления страницы на месте
    (возвращает None после клика)."""
    try:
        pag = page.evaluate(
            """() => {
              const list = document.querySelector('div[data-widget="webListReviews"]');
              if (!list || !list.children[2]) return null;
              const node = list.children[2];
              const a = node.querySelector('a[href]');
              if (a) return a.getAttribute('href');
              const m = String(node.innerText || '').match(/Дальше/);
              return m ? '__CLICK__' : null;
            }""",
                )
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        return None
    if not pag:
        return None
    if pag == "__CLICK__":
        if not click:
            return "__CLICK__"      # кнопка есть; клик — дело вызывающего
        try:
            page.evaluate(
                """() => {
                  const list = document.querySelector('div[data-widget="webListReviews"]');
                  if (!list || !list.children[2]) return null;
                  const node = list.children[2];
                  const btn = node.querySelector('button') || node;
                  btn.click();
                }""",
                )
            page.wait_for_timeout(6000)
            return None  # страница уже обновилась на месте
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            return None
    if pag.startswith("/"):
        return "https://www.ozon.ru" + pag
    if pag.startswith("http"):
        return pag
    return None


def _scroll_to_reviews(
    page, target_min: int = 40, max_iters: int = 90, stable_iters: int = 8,
) -> bool:
    """Прокручивает страницу, пока не наберутся отзывы или не появится пагинация.

    Ozon грузит отзывы лениво по мере прокрутки: часть товаров — пагинацией
    («Дальше» в list.children[2]), часть — бесконечным скроллом. Крутим колесо,
    пока не выполнится одно из условий: появилась пагинация «Дальше», число
    отзывов достигло target_min, либо count не растёт stable_iters итераций.
    Возвращает True, если отзывы найдены."""
    last_count, stale = -1, 0
    count = 0
    for _ in range(max_iters):
        try:
            count = page.evaluate(
                "() => document.querySelectorAll('[data-review-uuid]').length",
                )
            has_next = page.evaluate(
                """() => {
                  const list = document.querySelector('div[data-widget="webListReviews"]');
                  if (!list || !list.children[2]) return false;
                  return /Дальше/.test(list.children[2].innerText || '');
                }""",
                )
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            count, has_next = 0, False
        if count > 0 and has_next:
            break
        if count >= target_min:
            break
        if count == last_count:
            stale += 1
            if stale >= stable_iters:
                break
        else:
            stale = 0
        last_count = count
        try:
            page.evaluate(f"() => window.scrollBy(0, {SCROLL_STEP_PX})")
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            break
        page.wait_for_timeout(scroll_pause_ms())
    return count > 0


def _scroll_to_converge(page, target_min: int = 40, max_iters: int = 90,
                        telemetry: dict | None = None):
    """Докручивает страницу до сходимости числа уникальных отзывов.

    Ozon грузит отзывы лениво: классическая пагинация («Дальше»), бесконечный
    скролл (state-paginator fetchType=scroll) и любые будущие схемы. Единственный
    надёжный инвариант — рост числа уникальных [data-review-uuid]. Крутим колесо,
    пока число уникальных отзывов растёт; стабильность MAX_SCROLL_STABLE итераций
    → финальная докрутка к низу (MAX_SCROLL_BOTTOM раз) для ленивых чанков.
    Возвращает (count, resumed): число уникальных отзывов и признак того,
    что рост возобновился при финальной докрутке к низу.

    telemetry (dict, мутируется) — диагностика сходимости: scroll_count,
    scroll_wait_seconds, stable_rounds, stable_iterations, last_growth_iteration,
    progression (уникальные после каждой итерации), bottom_passes, bottom_growth.
    Аккумуляторный стиль: при повторном вызове на той же странице значения
    складываются (нужно для «возобновления роста» в _collect_reviews_pass).
    """
    def unique_count():
        try:
            ids = page.evaluate(REVIEW_IDS_JS)
            return len(set(ids)) if isinstance(ids, list) else 0
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            return 0

    tel = telemetry
    if tel is None:
        tel = {}
    tel.setdefault("scroll_count", 0)
    tel.setdefault("scroll_wait_seconds", 0.0)
    tel.setdefault("progression", [])

    last, stable = -1, 0
    stable_iterations = 0
    last_growth_iteration = 0
    for it in range(max_iters):
        _touch_watchdog()
        n = unique_count()
        tel["progression"].append(n)
        if n > last:
            stable = 0
            last_growth_iteration = it + 1
        else:
            stable += 1
        last = n
        log.debug(f"converge it={it}: unique={n} stable={stable}")
        if stable >= MAX_SCROLL_STABLE:
            stable_iterations = it + 1
            log.debug(f"converge done: stable плато {MAX_SCROLL_STABLE}, unique={n}")
            break
        try:
            page.evaluate(SCROLL_TO_LAST_REVIEW_JS)
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            log.warning(f"[collect] converge: скролл не удался: "
                        f"{type(e).__name__}: {e}")
            break
        tel["scroll_count"] += 1
        pause = scroll_pause_ms()
        tel["scroll_wait_seconds"] += pause / 1000.0
        page.wait_for_timeout(pause)
    resumed = False
    for _ in range(MAX_SCROLL_BOTTOM):
        _touch_watchdog()
        try:
            page.evaluate(SCROLL_TO_LAST_REVIEW_JS)
            pause = scroll_pause_ms()
            tel["scroll_wait_seconds"] += pause / 1000.0
            page.wait_for_timeout(pause)
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            break
        n = unique_count()
        if n > last:
            last = n
            resumed = True
            tel["progression"].append(n)
    tel["stable_rounds"] = stable
    tel["stable_iterations"] = stable_iterations
    tel["last_growth_iteration"] = last_growth_iteration
    tel["bottom_passes"] = tel.get("bottom_passes", 0) + MAX_SCROLL_BOTTOM
    tel["bottom_growth"] = tel.get("bottom_growth", False) or resumed
    log.debug(f"converge final: unique={last} resumed={resumed}")
    return last, resumed


def _expand_review_texts(page, max_passes: int = 8) -> int:
    """Кликает по всем кнопкам «Читать полностью»/«Читать дальше» в отзывах.

    Ozon усекает длинные отзывы и дораскрывает их по клику. Без клика
    REVIEWS_JS получает обрезанный innerText. Клик повторяется до сходимости
    (раскрытие может подгрузить ещё один уровень текста). Возвращает
    суммарное число кликов."""
    total = 0
    for _ in range(max_passes):
        try:
            n = page.evaluate(EXPAND_REVIEWS_JS) or 0
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            n = 0
        if not n:
            break
        total += n
        try:
            page.wait_for_timeout(800)
        except Exception:
            time.sleep(0.8)
        _touch_watchdog()  # долгий легитимный прогресс — сброс таймера сторожа
    return total


_COMMENT_BTN_RE = re.compile(r'[1-9]\d*\s*комментар', re.IGNORECASE)

# Перехват API комментариев к отзывам (rpGetCommentsByReviewUuid, B0 2026-09-02):
# API устойчив к смене hash-классов DOM и отдаёт текст/дату/голоса напрямую.
# POST json: {"reviewUuid": <uuid отзыва>, "sku": .., "rootSku": .., "limit":10,"offset":0}
# Ответ: {"cellTrackingInfo":{...},"comments":[{uuid, createdAt, comment,
#   publishedAt, publishedAtFormatted, parentUuid, rootParentUuid, isDeleted,
#   author:{firstName,clientOfficial:{brandName}}, usefulness:{useful,unuseful}}]}
# Привязка к отзыву: reviewUuid в post_data == review.uuid из DOM
# (rootParentUuid в ответе == тот же uuid отзыва).
_COMMENTS_API_URL_FRAGMENT = "/_action/rpGetCommentsByReviewUuid"
# Защитные пределы перехвата (на один проход сбора):
MAX_COMMENT_RESPONSES = 500      # максимум обработанных ответов API
MAX_RESPONSE_BYTES = 200_000     # тело ответа до response.json() (байт)
MAX_COMMENTS_PER_REVIEW = 500    # комментариев на один отзыв
MAX_TOTAL_COMMENTS = 100_000     # всех комментариев за сбор

_STICKY_INTERCEPT_HINT = "intercepts pointer events"
_UNSTABLE_ELEMENT_HINT = "element is not stable"


def _is_sticky_intercept(exc) -> bool:
    """True, если клик не прошёл из-за перекрытия/нестабильности элемента.

    Playwright сообщает об этом в тексте ошибки: «... subtree intercepts
    pointer events» или «... element is not stable» (элемент двигается:
    lazy-load, анимация, реклама). Это НЕ hang renderer: клик таймаутит,
    а renderer жив. Такую ошибку нельзя пробрасывать как renderer_failure —
    надо продолжить сбор (карточка уже помечена data-comments-clicked и
    больше не тронется)."""
    msg = str(exc)
    return _STICKY_INTERCEPT_HINT in msg or _UNSTABLE_ELEMENT_HINT in msg


def _expand_review_comments(page, max_passes: int = 4) -> int:
    """Раскрывает комментарии к отзывам («N комментарий»).

    Каждый клик дёргает API rpGetCommentsByReviewUuid и подгружает блок
    [data-comment-uuid] рядом с отзывом. MARK_COMMENT_CARDS_JS только помечает
    карточки с кнопкой, сами клики выполняет Playwright locator-кликом — это
    trusted-событие, которое React обрабатывает (A/B-эксперимент: dispatchEvent
    6/62, trusted-клики 15/16 и 31/31). Между кликами ждём асинхронную загрузку;
    повторный клик по одной карточке исключён маркером data-comments-clicked.
    Возвращает суммарное число кликов (0 — комментариев нет)."""
    total = 0
    for _ in range(max_passes):
        try:
            n = page.evaluate(MARK_COMMENT_CARDS_JS) or 0
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            n = 0
        if not n:
            break
        clicked = 0
        try:
            for _ in range(n):
                btn = page.locator(
                    '[data-comments-expanded]:not([data-comments-clicked]) button',
                    has_text=_COMMENT_BTN_RE,
                ).first
                if btn.count() == 0:
                    break
                h = btn.element_handle()
                if h is None:
                    # элемент отсоединился между count() и element_handle()
                    # (React-рендер/lazy-load) — пропускаем, не падаем
                    continue
                h.evaluate(
                    "(el) => { const c = el.closest('[data-review-uuid]');"
                    " if (c) c.setAttribute('data-comments-clicked', '1'); }"
                )
                try:
                    h.scroll_into_view_if_needed(timeout=5000)
                    h.click(timeout=8000)
                except Exception as e:
                    if B.is_renderer_failure(e) and not _is_sticky_intercept(e):
                        try:
                            h.evaluate("(el) => el.click()",
                                       timeout=8000)
                        except Exception as e2:
                            if B.is_renderer_failure(e2):
                                raise
                    else:
                        try:
                            h.evaluate("(el) => el.click()")
                        except Exception:
                            pass
                clicked += 1
                page.wait_for_timeout(300)
                _touch_watchdog()  # долгий прогресс — сброс таймера сторожа
        except Exception as e:
            if B.is_renderer_failure(e) and not _is_sticky_intercept(e):
                raise
            clicked = 0
        if not clicked:
            break
        total += clicked
        try:
            page.wait_for_timeout(2500)
        except Exception:
            time.sleep(2.5)
        _touch_watchdog()  # завершение прохода — сброс таймера сторожа
    return total


def _parse_comment_api(body: bytes) -> tuple[list | None, str]:
    """Разбирает тело ответа rpGetCommentsByReviewUuid в комментарии.

    Базовый API перехвачен в B0 (2026-09-02): комментарии приходят в fields,
    названная полями Ozon (comment, publishedAtFormatted, usefulness и пр.).
    Удалённые (isDeleted=true) и пустые комментарии пропускаются.
    Возвращает (comments, reason): comments — список dict в формате DOM-поля
    (uuid/author/reply_to/date/text/like/dislike, см. parseComments) либо None,
    если тело не JSON / не список comments (тогда остаётся DOM fallback);
    reason — краткая причина для лога (или "")."""
    if len(body) > MAX_RESPONSE_BYTES:
        return None, f"body>={MAX_RESPONSE_BYTES}B"
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception as e:
        return None, "json"
    if not isinstance(payload, dict):
        return None, "body-not-object"
    raw = payload.get("comments")
    if not isinstance(raw, list):
        return None, "no-comments-list"
    out = []
    for c in raw[:MAX_COMMENTS_PER_REVIEW]:
        if not isinstance(c, dict) or not c.get("uuid"):
            continue
        if c.get("isDeleted"):
            continue
        text = (c.get("comment") or "").strip()
        if not text:
            continue
        author = (c.get("author") or {})
        name = (author.get("firstName") or "").strip()
        if not name:
            brand = ((c.get("author") or {}).get("clientOfficial") or {})
            name = (brand.get("brandName") or "").strip()
        usefulness = (c.get("usefulness") or {})
        out.append({
            "uuid": c.get("uuid"),
            "author": name,
            "reply_to": "",
            "date": c.get("publishedAtFormatted") or "",
            "text": text[:20000],
            "like": int(usefulness.get("useful") or 0),
            "dislike": int(usefulness.get("unuseful") or 0),
        })
    return (out, "") if out else (None, "empty")


def _expand_comments_snapshot(page, max_passes: int = 4) -> tuple[int, dict]:
    """Раскрывает комментарии к отзывам, при возможности — через API.

    Основной путь (Chrome/Playwright): параллельно ловим ответы API
    rpGetCommentsByReviewUuid и привязываем их к отзыву по reviewUuid из
    post_data запроса (listener, см. _parse_comment_api). API-комментарии
    полнее DOM, поэтому заменяют их; остальные отзывы остаются с DOM.

    Firefox/Selenium: перехват сети недоступен (у page есть
    network_capture=False), поэтому сразу идём DOM-путём — он проверен живым
    прогоном: 15 trusted-кликов дали 15 узлов [data-comment-uuid].

    Возвращает (n_clicks, by_review): by_review — {review_uuid: [comments]}
    (пустой, если платформа не отвечала API). Отзывы с неоднозначной
    привязкой (нет reviewUuid / не JSON / пустое тело) остаются с DOM
    fallback: перехват просто их пропускает."""
    by_review: dict = {}
    n_resp = 0
    total_comments = 0

    if not getattr(page, "network_capture", True):
        log.debug("[comments-api] page без перехвата сети, DOM-комментарии")
        n_clicks = _expand_review_comments(page, max_passes=max_passes)
        try:
            _touch_watchdog()
        except Exception:
            pass
        return n_clicks, by_review

    def _on_response(resp):

        nonlocal n_resp, total_comments
        if _COMMENTS_API_URL_FRAGMENT not in (resp.url or ""):
            return
        if n_resp >= MAX_COMMENT_RESPONSES:
            log.warning(f"[comments-api] перехвачено ответов >= {MAX_COMMENT_RESPONSES}, "
                        f"пропускаю остальные")
            return
        n_resp += 1
        rid = ""
        try:
            post = resp.request.post_data or ""
            if post:
                data = json.loads(post)
                rid = (data.get("reviewUuid") or "").strip()
        except Exception:
            pass
        if not rid:
            return  # неоднозначно — остаётся DOM
        try:
            body = resp.body()
        except Exception:
            return  # тело недоступно (redirect/отмена) — остаётся DOM
        comments, reason = _parse_comment_api(body)
        if comments is None:
            log.debug(f"[comments-api] {rid}: пропущен ({reason})")
            return
        total_comments += len(comments)
        by_review[rid] = comments
        _touch_watchdog()  # один сброс на обработанный ответ

    try:
        page.on("response", _on_response)
    except Exception as e:
        log.warning(f"[comments-api] подписка warn: {e}")
        return 0, by_review
    try:
        n_clicks = _expand_review_comments(page, max_passes=max_passes)
        try:
            page.wait_for_timeout(1200)  # хвост API после последнего клика
        except Exception:
            time.sleep(1.2)
        _touch_watchdog()
    finally:
        page.remove_listener("response", _on_response)
    if by_review:
        log.info(f"[comments-api] перехвачено отзывов: {len(by_review)}, "
                 f"комментариев: {total_comments}")
    return n_clicks, by_review


def _apply_api_comments(reviews: list, api_comments: dict) -> int:
    """Вливает API-комментарии в отзывы (после финального top-up).

    Источник комментариев — один на отзыв: если API для отзыва дал
    непустые комментарии, они перезаписывают DOM-комментарии (API полнее:
    приходит с текстом/датами напрямую); иначе остаются DOM (fallback).
    Ставит на отзыв comments_source: api|dom — телеметрия происхождения.
    Возвращает число отзывов, где API заменил DOM."""
    replaced = 0
    for r in reviews:
        uid = r.get("uuid")
        api = api_comments.get(uid) if uid else None
        if not api or not isinstance(api, list) or not api:
            if r.get("comments"):
                r["comments_source"] = "dom"
            continue
        dom = r.get("comments") or []
        r["comments"] = api
        r["comments_source"] = "api"
        replaced += 1
        if dom:
            log.debug(f"[comments-api] {uid}: API заменил DOM "
                      f"({len(api)} vs {len(dom)})")
    return replaced



def _detect_pagination(page, grew: bool = True) -> str:
    """Определяет тип пагинации отзывов на текущей странице.

    Приоритет: state-paginator fetchType=scroll → infinite_scroll;
    state-paginator (не scroll) / webListReviews «Дальше» → next_button;
    рост без пагинатора → converged; иначе unknown."""
    try:
        sp = page.evaluate(
            """() => {
              const el = document.querySelector('[id^="state-paginator"]');
              if (!el) return null;
              try {
                const d = JSON.parse(el.getAttribute('data-state'));
                return d.fetchType || 'page';
              } catch (e) { return 'page'; }
            }""",
                )
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        sp = None
    if sp == "scroll":
        result = "infinite_scroll"
    elif sp is not None:
        result = "next_button"
    elif next_url(page, click=False):
        # click=False: детекция не имеет права кликать «Дальше» — клик здесь
        # уводил страницу на стр.2 до извлечения стр.1 (ревью 2026-10-15).
        result = "next_button"
    else:
        result = "converged" if grew else "unknown"
    log.debug(f"pagination detected: {result} (sp={sp!r}, grew={grew})")
    return result


def _review_key(r: dict) -> str:
    """Ключ дедупликации отзыва.

    data-review-uuid (основной); fallback — sha1 от (author + date + text[:120])
    на случай выдачи без uuid. Fallback-ключ используется только внутри
    дедупликации и не пишется в отзыв."""
    uid = r.get("uuid")
    if uid:
        return uid
    raw = f"{r.get('author') or ''}|{r.get('date') or ''}|{(r.get('text') or '')[:120]}"
    return "sha1:" + hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()


def _dedupe_reviews(reviews: list, seen: set) -> list:
    """Возвращает отзывы, отсутствующие в seen; добавляет новые ключи в seen."""
    out = []
    for r in reviews:
        uid = _review_key(r)
        if uid in seen:
            continue
        seen.add(uid)
        out.append(r)
    return out


def _merge_topup(data_reviews, seen: set, index: dict, reviews: list,
                 target_variant: dict) -> tuple[int, int]:
    """Сливает выдачу шага top-up.

    Три случая: (1) uid в seen и в index — уже собран, дозаполняем поля
    (текст длиннее, счётчики больше); (2) uid в seen, но НЕТ в index —
    карточка была видна converge при прокрутке, но размонтировалась к моменту
    экстракции: это и есть добираемый дефицит — добавляем; (3) uid новый.
    Добавление идёт через вариантный фильтр. Возвращает (added, refreshed)."""
    added = refreshed = 0
    for r in data_reviews or []:
        uid = _review_key(r)
        if uid in seen:
            dst = index.get(uid)
            if dst is None:
                pass  # seen-но-не-собран: добираем как новое ниже
            else:
                v = r.get("text")
                if v and len(v) > len(dst.get("text") or ""):
                    dst["text"] = v
                    refreshed += 1
                for k in ("helpful_yes", "helpful_no", "comments_count"):
                    v = r.get(k)
                    if isinstance(v, int) and v > (dst.get(k) or 0):
                        dst[k] = v
                        refreshed += 1
                vc = r.get("comments")
                if isinstance(vc, list) \
                        and len(vc) > len(dst.get("comments") or []):
                    dst["comments"] = vc
                    refreshed += 1
                continue
        else:
            seen.add(uid)
        ok_v = _variant_matches(target_variant, r)
        if ok_v:
            reviews.append(r)
            index[uid] = r
            added += 1
    return added, refreshed


def _topup_virtualized(page, target_count: int, seen: set, index: dict,
                       reviews: list, target_variant: dict, page_no: int,
                       max_steps: int | None = None) -> int:
    """Добирает карточки, размонтированные виртуализацией infinite-scroll.

    Converge считает uuid за всё время прокрутки, а REVIEWS_JS снимает DOM
    в один момент: в нижней позиции окна часть верхних карточек размонтирована
    (пример: converge unique=34, экстракция 30 -> VariantShortError).
    Фаза 1: подъём к началу списка БЕЗ досрочных стопов — пропавшие карточки
    сидят у вершины, лимит «без роста» рубил подъём на полпути.
    Фаза 2 (если дефицит остался): спуск вниз для срединных случаев.
    В конце дораскрытие текстов и финальный пересъём. Возвращает число
    добавленных отзывов."""
    steps = MAX_TOPUP_STEPS if max_steps is None else max_steps
    added_total = 0

    def _sweep(step_js: str, label: str) -> bool:
        nonlocal added_total
        for step in range(1, steps + 1):
            _touch_watchdog()
            try:
                edge = page.evaluate(step_js)
                page.wait_for_timeout(scroll_pause_ms())
                data = page.evaluate(REVIEWS_JS)
            except Exception as e:
                if B.is_renderer_failure(e):
                    raise
                log.warning(f"[collect] стр. {page_no}: top-up {label} "
                            f"шаг {step} warn: {e}")
                break
            added, _ = _merge_topup((data or {}).get("reviews"), seen, index,
                                    reviews, target_variant)
            added_total += added
            log.info(f"[collect] стр. {page_no}: top-up {label} шаг {step}: "
                     f"+{added}, собрано {len(reviews)}/{target_count}")
            if len(reviews) >= target_count:
                return True
            if edge:
                break
        return False

    done = _sweep(SCROLL_STEP_UP_JS, "вверх")
    if not done and len(reviews) < target_count:
        _sweep(SCROLL_STEP_DOWN_JS, "вниз")
    api_comments = {}
    try:
        _touch_watchdog()
        n_exp = _expand_review_texts(page)
        n_com, api_comments = _expand_comments_snapshot(page)
        data = page.evaluate(REVIEWS_JS) if (n_exp or n_com) else None
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        data = None
    if data and data.get("ok"):
        _, refreshed = _merge_topup(data.get("reviews"), seen, index,
                                    reviews, target_variant)
        if refreshed:
            log.info(f"[collect] стр. {page_no}: top-up дозаполнил полей: "
                     f"{refreshed}")
    n_api = _apply_api_comments(reviews, api_comments) if api_comments else 0
    if n_api:
        log.info(f"[collect] стр. {page_no}: API-комментарии заменили "
                 f"DOM у {n_api} отзывов")
    return added_total


def _activate_variant_mode(page, timeout_s: float = 8.0) -> dict:
    """Включает нативный фильтр «Этот вариант товара» (webReviewsVariantMode).

    Управление фильтром происходит серверно: URL-параметр reviewsVariantMode=1
    показывает тумблер активным. Клик по опции — только если параметра ещё нет.
    Виджет webReviewsVariantMode рендерится лениво (после прокрутки к отзывам),
    поэтому перед первым evaluate делается скролл, если виджет ещё не появился.
    Возвращает диагностику:
      used       — фильтр был активен в момент сбора (True/False)
      clicked    — был ли выполнен клик по «Этот вариант товара»
      variant_present, variant_count, active, total_count — из VARIANT_MODE_JS
      reason     — no_widget / url_param / click_ok / click_no_effect / timeout / disabled
    Не бросает исключений: неудача клика не должна ронять сбор."""
    snap = None
    try:
        snap = page.evaluate(VARIANT_MODE_JS)
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        log.warning(f"[variant_mode] evaluate warn: {e}")
    if not snap or not snap.get("found"):
        # Виджет рендерится после прокрутки к отзывам: скроллим до его появления,
        # но не более 3 скроллов после появления списка отзывов (товар без
        # тумблера — не тратим время на бесконечную прокрутку).
        scrolled_after_reviews = 0
        for _ in range(60):
            try:
                page.evaluate(SCROLL_TO_LAST_REVIEW_JS)
            except Exception as e:
                if B.is_renderer_failure(e):
                    raise
                log.warning(f"[variant_mode] скролл не удался: "
                            f"{type(e).__name__}: {e}")
                break
            page.wait_for_timeout(scroll_pause_ms())
            try:
                snap = page.evaluate(VARIANT_MODE_JS)
            except Exception as e:
                if B.is_renderer_failure(e):
                    raise
                snap = None
            if snap and snap.get("found"):
                break
            try:
                has_reviews = page.evaluate(
                    "() => !!document.querySelector('div[data-widget=\"webListReviews\"]')")
            except Exception:
                has_reviews = False
            if has_reviews:
                scrolled_after_reviews += 1
                if scrolled_after_reviews >= 3:
                    break
    if not snap or not snap.get("found"):
        return {"used": False, "clicked": False, "variant_present": False,
                "variant_count": None, "active": None, "total_count": None,
                "reason": "no_widget"}
    base = {k: snap.get(k) for k in ("variant_present", "variant_count", "active", "total_count")}
    if not snap.get("variant_present"):
        return {**base, "used": False, "clicked": False, "reason": "no_variant_opt"}
    if snap.get("active") == "Этот вариант товара":
        return {**base, "used": True, "clicked": False, "reason": "already_active"}
    try:
        url = page.url
    except Exception:
        url = ""
    if "reviewsVariantMode=1" in url:
        # URL-параметр — не доказательство, что фильтр применён: DOM мог
        # показывать другой активный вариант, а used=True по строке URL гасил
        # filter_not_used в обход инварианта I1 (ревью 2026-10-15). Если
        # фильтр реально включён, выше сработал already_active; иначе клик
        # ниже доведёт виджет до консистентного состояния.
        log.info("[variant_mode] reviewsVariantMode=1 в URL, но DOM не "
                 "подтвердил активный фильтр — кликаю «Этот вариант товара»")
    try:
        page.evaluate(
            """() => {
              const w = document.querySelector('[data-widget="webReviewsVariantMode"]');
              if (!w) return;
              const opts = Array.from(w.querySelectorAll('div[tabindex="0"]'));
              const target = opts.find((o) =>
                /^Этот вариант товара/.test((o.innerText || '').replace(/\\s+/g, ' ').trim())
              );
              if (target) target.click();
            }"""
        )
    except Exception as e:
        log.warning(f"[variant_mode] click warn: {e}")
        return {**base, "used": False, "clicked": True, "reason": "click_error"}
    deadline = time.monotonic() + timeout_s
    active = False
    while time.monotonic() < deadline:
        try:
            snap = page.evaluate(VARIANT_MODE_JS)
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            snap = None
        if snap and snap.get("active") == "Этот вариант товара":
            active = True
            break
        time.sleep(0.8)
    if not active:
        log.warning("[variant_mode] клик не применился, собираем без нативного фильтра")
        return {**base, "used": False, "clicked": True, "reason": "click_no_effect"}
    try:
        page.wait_for_timeout(2500)
    except Exception:
        time.sleep(2.5)
    final = dict(base)
    try:
        snap = page.evaluate(VARIANT_MODE_JS)
    except Exception:
        snap = None
    if snap:
        for k in ("variant_present", "variant_count", "active", "total_count"):
            if snap.get(k) is not None:
                final[k] = snap.get(k)
    return {**final, "used": True, "clicked": True, "reason": "click_ok"}


_CARDS_COUNT_JS = _load_js("cards_count.js")


def _count_review_cards(page):
    """Число карточек отзывов в DOM сейчас; None при ошибке evaluate."""
    try:
        return page.evaluate(_CARDS_COUNT_JS)
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        return None


_PAGE_GEOM_JS = _load_js("page_geom.js")


def _page_geom(page) -> dict:
    """Геометрия/состояние страницы — для диагностики пустого прокручивания."""
    try:
        g = page.evaluate(_PAGE_GEOM_JS)
        return g if isinstance(g, dict) else {}
    except Exception as e:
        if B.is_renderer_failure(e):
            raise
        return {"error": f"{type(e).__name__}: {e}"}


def _collect_reviews_pass(
    page, page_url: str, sort: str | None, target_variant: dict,
    max_pages: int = 12, min_reviews: int = 40, apply_variant_mode: bool = True,
    telemetry: dict | None = None, no_count_hint: bool = False,
) -> tuple[List[dict], int, List[dict], dict | None]:
    """Один проход сбора отзывов по одной сортировке (см. collect_reviews).

    Возвращает (reviews, total_pages, page_log, variant_mode): reviews уже
    профильтрованы по target_variant и дедуплицированы внутри прохода;
    variant_mode — сырая диагностика фильтра варианта (finalize делает
    collect_reviews на объединённом списке). В page_log каждая запись несёт
    поле 'sort' (сортировка прохода) для диагностики multi-sort сборки.

    telemetry (dict, мутируется) — аккумулятор диагностики: scroll_time
    (только _scroll_to_converge), extract_time (раскрытие текстов/комментариев
    + извлечение REVIEWS_JS; БЕЗ goto и активации варианта), pages[].
    """
    seen = set()
    reviews = []
    total_pages = 1
    page_log = []
    variant_mode: dict | None = None
    tel = telemetry
    if tel is None:
        tel = {}
    tel.setdefault("scroll_time", 0.0)
    tel.setdefault("extract_time", 0.0)
    tel.setdefault("pages", [])
    skip_goto = False
    for p in range(1, max_pages + 1):
        _touch_watchdog()
        if skip_goto:
            # Прошлая итерация кликнула «Дальше» и страница обновилась на
            # месте: goto тем же URL отбросил бы её на предыдущую страницу.
            skip_goto = False
        else:
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)
            except Exception as e:
                if B.is_renderer_failure(e):
                    raise
                name = type(e).__name__
                if name == "TimeoutError" or "net::ERR_" in str(e) \
                        or "Timeout" in str(e):
                    # навигация не удалась (таймаут/DNS/ERR) — продолжать
                    # разбор по старой/ошибочной странице нельзя: соберём
                    # чужой контент
                    raise
                log.warning(f"[collect] page {p} goto warn: {e}")
                page.wait_for_timeout(3000)
        check_challenge(page, f"reviews_page p{p}")
        _pass_age_gate(page)
        if apply_variant_mode:
            vm = _activate_variant_mode(page)
            n_cards = _count_review_cards(page)
            msg = (f"[variant_mode] p{p} sort={sort}: {vm} cards={n_cards}")
            if n_cards:
                log.debug(msg)
            else:
                log.info(msg + f" geom={_page_geom(page)}")
            if variant_mode is None or vm.get("used"):
                variant_mode = vm
            if vm.get("used") and vm.get("variant_count") == 0:
                # Виджет варианта явно говорит «Отзывы 0»: прокручивать
                # нечего — ни converge с релоадами, ни остальные сортировки.
                # Экономия ~70с на пустой вариант (кейсы 5274359430,
                # 5575856356).
                log.info(f"[collect] стр. {p}: у варианта 0 отзывов — "
                         f"пропускаю прокрутку (sort={sort})")
                page_log.append({"page": p, "n_found": 0, "n_matched": 0,
                                 "ok": True,
                                 "stop_reason": "variant_zero_reviews",
                                 "sort": sort})
                return [], 1, page_log, variant_mode
        if no_count_hint and p == 1:
            # Товар без явных счётчиков (rating_count/variant_count = None):
            # если на странице отзывов нет ни одной карточки (двойная
            # проверка с паузой), прокрутка и релоады бесполезны — кейс
            # 4551551032: нет ни рейтинга, ни виджета варианта, раздел пуст.
            n_cards = _count_review_cards(page)
            if n_cards == 0:
                page.wait_for_timeout(2500)
                n_cards = _count_review_cards(page)
            if n_cards == 0:
                log.info(f"[collect] стр. {p}: карточек нет, счётчиков нет "
                         f"— пропускаю проход (sort={sort})")
                page_log.append({"page": p, "n_found": 0, "n_matched": 0,
                                 "ok": True,
                                 "stop_reason": "no_cards_no_count_hint",
                                 "sort": sort})
                return [], 1, page_log, variant_mode
        page_tel: dict = {}
        _t0 = time.monotonic()
        empty_retries = 0
        while True:
            _touch_watchdog()
            count, resumed = _scroll_to_converge(
                page, target_min=min_reviews, telemetry=page_tel)
            if resumed:
                count, _ = _scroll_to_converge(
                    page, target_min=min_reviews, telemetry=page_tel)
            if count > 0 or p > 1 or empty_retries >= EMPTY_RETRY_PASSES:
                break
            empty_retries += 1
            log.warning(f"page {p}: 0 отзывов при прокрутке — "
                        f"reload ({empty_retries}/{EMPTY_RETRY_PASSES})")
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(EMPTY_RETRY_WAIT_MS)
            except Exception as e:
                if B.is_renderer_failure(e):
                    raise
                log.warning(f"page {p}: reload при пустой выдаче warn: {e}")
            check_challenge(page, f"reviews_page p{p} retry{empty_retries}")
        page_tel["count"] = count
        page_tel["resumed"] = resumed
        page_tel["empty_retries"] = empty_retries
        page_tel["page"] = p
        page_tel["sort"] = sort
        tel["scroll_time"] += time.monotonic() - _t0
        tel["pages"].append(page_tel)
        if count == 0:
            log.warning(f"[collect] page {p}: отзывы не появились при прокрутке, стоп")
            page_log.append({"page": p, "n_found": 0, "n_matched": 0, "ok": False,
                             "stop_reason": "no_scroll", "sort": sort,
                             "empty_retries": empty_retries})
            break
        if variant_mode is None:
            variant_mode = {}
        if "pagination_type" not in variant_mode:
            variant_mode["pagination_type"] = _detect_pagination(page, grew=count > 0)
        _et0 = time.monotonic()
        _touch_watchdog()
        try:
            n_expanded = _expand_review_texts(page)
            if n_expanded:
                log.info(f"[collect] стр. {p}: раскрыто «Читать полностью»: {n_expanded} кнопок")
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            log.warning(f"[collect] стр. {p}: раскрытие отзывов warn: {e}")
        api_comments = {}
        try:
            _touch_watchdog()
            n_comment, api_comments = _expand_comments_snapshot(page)
            if n_comment:
                log.info(f"[collect] стр. {p}: раскрыто комментариев к отзывам: {n_comment} кнопок")
        except Exception as e:
            if B.is_renderer_failure(e):
                raise
            log.warning(f"[collect] стр. {p}: раскрытие комментариев warn: {e}")
        _touch_watchdog()
        data = page.evaluate(REVIEWS_JS)
        tel["extract_time"] += time.monotonic() - _et0
        ok = bool(data and data.get("ok"))
        if not ok:
            log.warning(f"[collect] page {p}: пустая выдача, стоп")
            page_log.append({"page": p, "n_found": 0, "n_matched": 0, "ok": False,
                             "stop_reason": "empty", "sort": sort})
            break
        if data.get("totalText"):
            try:
                m = re.search(r"(\d+)\s*страниц", data["totalText"])
                if m:
                    total_pages = max(total_pages, int(m.group(1)))
            except Exception:
                pass
        matched_this = 0
        for r in _dedupe_reviews(data["reviews"], seen):
            if _variant_matches(target_variant, r):
                reviews.append(r)
                matched_this += 1
        log.debug(f"стр. {p}: всего на странице {len(data['reviews'])}, "
                  f"под вариант {len(reviews)} (виден {len(seen)})")
        # Виртуализация infinite-scroll: REVIEWS_JS видит только смонтированное
        # окно карточек, а converge насчитал count за всё время прокрутки.
        # Дефицит меряем по СОБРАННЫМ отзывам: seen после converge уже содержит
        # все uuid (в т.ч. размонтированные), поэтому len(seen) тут не годится.
        log.info(f"[collect][topup-check] стр. {p}: "
                 f"pt={(variant_mode or {}).get('pagination_type')!r} "
                 f"count={count} собрано={len(reviews)} видно={len(seen)}")
        if ((variant_mode or {}).get("pagination_type") == "infinite_scroll"
                and count > len(reviews)):
            idx0 = max(len(reviews) - matched_this, 0)
            pass_index = {_review_key(r): r for r in reviews[idx0:]}
            added = _topup_virtualized(page, count, seen, pass_index,
                                       reviews, target_variant, p)
            log.info(f"стр. {p}: top-up завершён: +{added} "
                     f"(собрано {len(reviews)}/{count})")
            if added:
                matched_this += added
                page_tel["topup_added"] = page_tel.get("topup_added", 0) + added
        n_api = _apply_api_comments(reviews, api_comments) if api_comments else 0
        if n_api:
            log.info(f"стр. {p}: API-комментарии заменили DOM у {n_api} отзывов")
        nxt = _next_page(page)
        if nxt == "__IN_PLACE__":
            # Кнопочная пагинация: клик сделан в _next_page, страница уже
            # несёт следующую порцию. Не выходим и не делаем goto.
            page_log.append({
                "page": p, "n_found": len(data["reviews"]), "n_matched": matched_this,
                "ok": True, "stop_reason": None, "sort": sort,
            })
            skip_goto = True
            continue
        if nxt and sort is not None:
            nxt = _sort_url(nxt, SORT_PARAMS.get(sort))
        if not nxt:
            log.warning(f"[collect] стр. {p}: пагинации нет, стоп")
            page_log.append({
                "page": p, "n_found": len(data["reviews"]), "n_matched": matched_this,
                "ok": True, "stop_reason": "no_next", "sort": sort,
            })
            break
        page_log.append({
            "page": p, "n_found": len(data["reviews"]), "n_matched": matched_this,
            "ok": True, "stop_reason": None, "sort": sort,
        })
        page_url = nxt
    return reviews, total_pages, page_log, variant_mode


def _merge_review_sorts(pass_reviews: list, merged: dict, order: list, sort: str) -> None:
    """Сливает отзывы одного прохода в объединённый список с дедупом по uuid.

    Один отзыв — одна запись: у первой записи проставляется review_sort
    (сортировка первого появления); при повторе в другой сортировке в
    существующую запись добавляется значение в review_sorts."""
    for r in pass_reviews:
        uid = _review_key(r)
        if uid in merged:
            if sort not in merged[uid]["review_sorts"]:
                merged[uid]["review_sorts"].append(sort)
        else:
            r["review_sort"] = sort
            r["review_sorts"] = [sort]
            merged[uid] = r
            order.append(uid)


def _finalize_variant_mode(variant_mode: dict | None, reviews: list,
                           apply_variant_mode: bool) -> dict:
    """Обогащает/собирает diagnostic variant_mode (см. collect_reviews)."""
    if apply_variant_mode:
        matched_variant = sum(1 for r in reviews if r.get("variant"))
        dropped = len(reviews) - matched_variant
        variant_mode = variant_mode or {
            "used": False, "clicked": False, "variant_present": False,
            "variant_count": None, "active": None, "total_count": None,
            "reason": "no_pages",
        }
        variant_mode["reviews_matched_variant"] = matched_variant
        variant_mode["reviews_dropped_by_client_filter"] = dropped
        if variant_mode.get("used") is True and matched_variant == 0:
            warning = "native_filter_active_no_matching_reviews"
            variant_mode["warning"] = warning
            log.warning(f"[collect] warning: {warning}")
        return variant_mode
    base = {"used": False, "variant_present": False, "active": None,
            "variant_count": None, "total_count": None,
            "reason": "disabled"}
    if variant_mode and variant_mode.get("pagination_type"):
        base["pagination_type"] = variant_mode["pagination_type"]
    return base


def _canonical_product_url(page, url: str) -> str:
    """Вернуть канонический URL карточки (со slug), если он есть.

    Короткий адрес `/product/{pid}` открывает недогидратированную оболочку:
    на живой проверке (2026-09-25, Firefox) `webListReviews` отсутствовал, тело
    страницы — 1863 символа, скролл давал 0 карточек, тогда как тот же товар
    по полному slug-URL отдавал 12 отзывов. Ozon публикует канонический адрес
    в `<link rel="canonical">`, поэтому берём его и один раз переходим.
    Ничего не делаем, если slug уже есть или каноника не читается."""
    if re.search(r"/product/[^/]*\d+[^/]*/", url or ""):
        return url
    try:
        canon = page.evaluate(
            "() => { const l = document.querySelector("
            "'link[rel=\"canonical\"]'); return l ? l.href : null; }",
        )
    except Exception:
        return url
    if not canon or canon == url:
        return url
    try:
        log.info(f"[collect] canonical URL: {canon}")
        page.goto(canon, timeout=60000)
    except Exception as e:
        log.warning(f"[collect] переход по canonical не удался: "
                    f"{type(e).__name__}: {e}")
        return url
    return canon


def collect_reviews(
    page, reviews_url: str, target_variant: dict, max_pages: int = 12,
    min_reviews: int = 40, apply_variant_mode: bool = True,
    sorts: Optional[List[str]] = None, telemetry: dict | None = None,
    no_count_hint: bool = False,
) -> tuple[List[dict], int, List[dict], dict]:
    """Собирает отзывы варианта. Возвращает (reviews, total_pages, page_log, variant_mode).

    Фильтр варианта: отзыв считается относящимся к варианту, если нормализованный
    текст его variant-поля содержит подстроки всех значений target_variant
    (для габаритов 'Длина_см' и т.п. — числа, для остальных — текст/значение).
    page_log — журнал страниц [{page, n_found, n_matched, ok, stop_reason, sort}],
    используется в компоненте confidence «стабильность сбора».

    apply_variant_mode=True — перед скроллом каждой страницы активировать
    нативный фильтр «Этот вариант товара» (webReviewsVariantMode); False —
    собрать все отзывы. variant_mode — диагностика фильтра (см. _activate_variant_mode).

    sorts=None — один проход без параметра sort (сортировка Ozon по умолчанию
    «новые и полезные»); отзывы не получают полей review_sort/review_sorts.
    Иначе — по одному проходу на каждую сортировку из SORT_PARAMS
    (published_at_desc / score_desc / score_asc), результаты объединяются в
    один список с дедупликацией по uuid: у каждого отзыва поле review_sort
    (сортировка первого появления) и review_sorts (все сортировки, где отзыв
    встретился). page_log — сквозной по всем сортировкам (с полем sort).

    telemetry (dict, мутируется) — аккумулятор диагностики сбора
    (scroll_time, extract_time, pages[]); см. _collect_reviews_pass.
    """
    tel = {} if telemetry is None else telemetry
    tel.setdefault("scroll_time", 0.0)
    tel.setdefault("extract_time", 0.0)
    tel.setdefault("pages", [])
    if sorts is None:
        reviews, total_pages, page_log, variant_mode = _collect_reviews_pass(
            page, reviews_url, None, target_variant,
            max_pages=max_pages, min_reviews=min_reviews,
            apply_variant_mode=apply_variant_mode, telemetry=tel,
            no_count_hint=no_count_hint,
        )
        variant_mode = _finalize_variant_mode(variant_mode, reviews, apply_variant_mode)
        if telemetry is not None:
            telemetry.update(tel)
        return reviews, total_pages, page_log, variant_mode

    unknown = [s for s in sorts if s not in SORT_PARAMS]
    if unknown:
        raise ValueError(
            f"Неизвестная сортировка {unknown[0]!r}; допустимо: {sorted(SORT_PARAMS)}"
        )
    merged: dict = {}
    order: list = []
    total_pages = 1
    page_log: list = []
    variant_mode: dict | None = None
    for sort in sorts:
        start_url = _sort_url(reviews_url, SORT_PARAMS[sort])
        log.info(f"[collect] сортировка «{SORT_LABELS.get(sort, sort)}» (sort={sort}): {start_url}")
        pass_reviews, tp, pl, vm = _collect_reviews_pass(
            page, start_url, sort, target_variant,
            max_pages=max_pages, min_reviews=min_reviews,
            apply_variant_mode=apply_variant_mode, telemetry=tel,
            no_count_hint=no_count_hint,
        )
        page_log.extend(pl)
        total_pages = max(total_pages, tp)
        if vm is not None and (
            variant_mode is None or (vm.get("used") and not variant_mode.get("used"))
        ):
            variant_mode = vm
        _merge_review_sorts(pass_reviews, merged, order, sort)
        log.info(f"[collect] сортировка «{SORT_LABELS.get(sort, sort)}»: "
                 f"собрано {len(pass_reviews)}, всего уникальных {len(order)}")
        # Оптимизация (2026-08-21): если вариант небольшой (variant_count <= 100)
        # и проход собрал его целиком, остальные сортировки вернут тот же
        # набор в другом порядке — пропускаем их (экономия ~минуты на товар
        # и меньше контакта с анти-ботом). Имеет смысл только когда список
        # обрезан стеной логина (vc > 100): там разные сортировки дают
        # разные подмножества.
        remaining = sorts[sorts.index(sort) + 1:]
        if remaining and not order and (
                no_count_hint
                or (variant_mode or {}).get("variant_count") == 0):
            log.info(f"[collect] отзывов у варианта нет — "
                     f"пропускаю сортировки: {remaining}")
            break
        vc_now = (variant_mode or {}).get("variant_count")
        if not isinstance(vc_now, int):
            # Фолбэк (2026-08-21): variant_count бывает None. Берём счётчик
            # converge последней страницы прохода — на небольших списках он
            # равен полному размеру выдачи. Граница 60 — заведомо ниже зоны
            # логин-стены (~100), где сортировки дают разные подмножества.
            pages_tel = tel.get("pages") or []
            c = (pages_tel[-1] or {}).get("count") if pages_tel else None
            if isinstance(c, int) and 0 < c <= 60:
                vc_now = c
        if (remaining and isinstance(vc_now, int)
                and 0 < vc_now <= VARIANT_COUNT_FULL_LIMIT
                and len(order) >= vc_now):
            log.info(f"[collect] вариант полный ({len(order)}/{vc_now}) — "
                     f"пропускаю сортировки: {remaining}")
            break
    reviews = [merged[uid] for uid in order]
    variant_mode = _finalize_variant_mode(variant_mode, reviews, apply_variant_mode)
    if telemetry is not None:
        telemetry.update(tel)
    return reviews, total_pages, page_log, variant_mode


def pid_from_url(url: str) -> str:
    url = (url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
    m = re.search(r"(?:-(\d+)|/(\d+))/?$", url)
    return (m.group(1) or m.group(2)) if m else "unknown"


REVIEWS_API_ENV = "OZON_REVIEWS_API"
_TRUE = ("1", "true", "yes", "on")


def reviews_api_enabled(flag: bool | None = None) -> bool:
    """Включён ли быстрый API-предсрез отзывов.

    По умолчанию ВЫКЛЮЧЕН (ревизия 2.8): модуль доказан только как способ снять
    те же 4 отзыва, что и DOM, за 0,1 с. Включение — явное (флаг/CLI/env), чтобы
    смена поведения боевого сбора не случилась сама.
    """
    if flag is not None:
        return bool(flag)
    return (os.environ.get(REVIEWS_API_ENV) or "").strip().lower() in _TRUE


def _reviews_api_prepass(page, pid: str | None, target_variant: dict,
                         rating_count, sorts, enabled: bool,
                         apply_variant_mode: bool = True) -> dict:
    """Быстрый предсрез отзывов через API. Всегда safe: на любой проблеме → {}.

    complete=True только когда API вернул не меньше доверенного rating_count и
    не нужен вариант/мульти-сорт (тогда DOM-страницы можно не докручивать).
    apply_variant_mode=True (дефолт сбора) закрывает предсрез целиком: API не
    несёт variant-поля, и merge влил бы отзывы чужих вариантов (I1, аудит
    2026-10-15) — см. ozon_fetch.api_plan.
    """
    tel = {"enabled": bool(enabled), "used": False, "reason": "",
           "fetched": 0, "pages": 0, "complete": False, "reviews": []}
    if not enabled:
        tel["reason"] = "disabled"
        return tel
    if not pid or str(pid) in ("", "unknown"):
        tel["reason"] = "no_pid"
        return tel
    allowed, why = ozon_fetch.api_plan(target_variant,
                                       need_dom_extras=bool(sorts),
                                       apply_variant_mode=apply_variant_mode)
    if not allowed:
        tel["reason"] = why
        return tel
    t0 = time.monotonic()
    try:
        got = ozon_fetch.fetch_slice(page, str(pid), max_pages=1)
    except Exception as e:
        # Предсрез — ускорение, а не источник данных: любая ошибка тихо
        # оставляет DOM-сбор как единственный путь.
        tel["reason"] = f"error:{type(e).__name__}"
        tel["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        log.warning(f"[reviews-api] предсрез не удался: {type(e).__name__}: {e}")
        return tel
    tel.update(used=bool(got.get("fetched")), fetched=got.get("fetched", 0),
               pages=got.get("pages", 0), reviews=got.get("reviews") or [])
    tel["complete"] = bool(sorts is None and ozon_fetch.api_slice_complete(
        tel["fetched"], rating_count))
    tel["reason"] = ("complete" if tel["complete"]
                     else (got.get("error") or "partial"))
    tel["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    log.info(f"[reviews-api] предсрез: {tel['fetched']} отзывов за "
             f"{tel['elapsed_ms']}мс (rating_count={rating_count}, "
             f"complete={tel['complete']}, reason={tel['reason']})")
    return tel


def _collect_reviews_stage(sess, rurl, target_variant, rating_count, pid,
                           max_pages, min_reviews, apply_variant_mode, sorts,
                           telemetry, no_count_hint, reviews_api):
    """Сбор отзывов товара (+ опциональный API-предсрез) для collect().

    Вынесено из collect(), чтобы не растить его CCN: логика «быстрый предсрез →
    ограничение глубины DOM-докрутки → merge» — отдельный этап со своим
    результатом (reviews, total_pages, page_log, variant_mode, raw_api_ctx).
    """
    api_ctx = _reviews_api_prepass(
        sess.page, pid, target_variant, rating_count, sorts,
        enabled=reviews_api_enabled(reviews_api),
        apply_variant_mode=apply_variant_mode)
    # Покрытие доказано счётчиком из карточки → глубокий DOM-скролл не нужен.
    # Мульти-сорт и variant-режим (CLI или нативный) сюда не попадают — см.
    # ozon_fetch.api_plan.
    eff_max_pages = 1 if api_ctx.get("complete") else max_pages
    if api_ctx.get("complete"):
        log.info(f"[reviews-api] покрытие доказано "
                 f"({api_ctx['fetched']} ≥ rating_count) — "
                 f"DOM докручиваю только 1 страницу")
    reviews, total_pages, page_log, variant_mode = collect_reviews(
        sess.page, rurl, target_variant, max_pages=eff_max_pages,
        min_reviews=min_reviews, apply_variant_mode=apply_variant_mode,
        sorts=sorts, telemetry=telemetry, no_count_hint=no_count_hint,
    )
    reviews, api_stats = ozon_fetch.merge_reviews_api(
        reviews, api_ctx.get("reviews") or [])
    if not api_ctx.get("used"):
        return reviews, total_pages, page_log, variant_mode, {
            "enabled": api_ctx.get("enabled", False), "used": False,
            "reason": api_ctx.get("reason", "disabled")}
    api_stats.update({k: v for k, v in api_ctx.items() if k != "reviews"})
    api_stats["dom_short_circuit"] = bool(api_ctx.get("complete"))
    return reviews, total_pages, page_log, variant_mode, api_stats


def collect(
    product_url: str,
    reviews_url: str | None = None,
    variant: str = "",
    max_pages: int = 12,
    min_reviews: int = 40,
    out_path: str | None = None,
    cdp_url: str = DEFAULT_CDP_URL,
    category: str | None = None,
    save_dom: bool = True,
    apply_variant_mode: bool = True,
    sorts: Optional[List[str]] = None,
    sess=None,
    overwrite: bool = False,
    skip_reviews: bool = False,
    reviews_api: bool | None = None,
) -> dict:
    """Полный сбор: карточка + отзывы варианта.

    По умолчанию сохраняет raw.json в data/{pid}_raw.json и DOM-снимок страницы
    в data/{pid}/dom/{YYYY-MM-DD}.html.gz (raw['artifacts']['dom_snapshot']).
    apply_variant_mode=True — включить нативный фильтр «Этот вариант товара»
    (raw['reviews_variant_mode']); False — собрать все отзывы без фильтра.
    sorts=None — одна сортировка по умолчанию; иначе список сортировок из
    SORT_PARAMS (published_at_desc/score_desc/score_asc) — отзывы собираются
    по каждой и объединяются с дедупом по uuid (raw['reviews_sorts']).
    skip_reviews=True — собрать только карточку, отзывы НЕ собирать
    (raw['reviews_skipped']=True). Режим быстрого обзора: вариант-рейтинг и
    проверки объёма отзывов неприменимы; нулевым отзывы не подтверждаются.
    sess — внешняя CDP-сессия (для переиспользования в search/gather):
    если передана, используется и НЕ закрывается; иначе создаётся своя.
    reviews_api — быстрый предсрез отзывов через ozon_fetch (None → env
    OZON_REVIEWS_API, по умолчанию выключено). DOM остаётся источником
    истины: API лишь дополняет список, а не докручивает страницы, пока не
    докажет покрытие rating_count (см. _reviews_api_prepass). При включённом
    apply_variant_mode предсрез отключается целиком (I1, см. api_plan).
    Возвращает dict."""
    target_variant = parse_variant(variant)
    sess_owned = sess is None
    if sess_owned:
        sess = Session(cdp_url)
    dom_html = None
    telemetry: dict = {}
    # Телеметрия API-предсреза: пишется в raw всегда (и когда выключен/пропущен),
    # иначе по отчёту нельзя отличить «API не пробовали» от «API не помог».
    raw_api_ctx: dict = {"enabled": reviews_api_enabled(reviews_api),
                         "used": False, "reason": "not_attempted"}
    auth_state = {"logged_in": None, "source": "unknown", "cookie_present": None}
    try:
        sess.goto(product_url)
        check_challenge(sess.page, "product_page")
        _pass_age_gate(sess.page)
        auth_state = detect_auth_state(sess.page)
        product_url = _canonical_product_url(sess.page, product_url)
        requested_pid = pid_from_url(product_url)
        actual_pid = pid_from_url(sess.page.url)
        if (requested_pid and actual_pid and requested_pid != "unknown"
                and actual_pid != "unknown" and requested_pid != actual_pid):
            raise RuntimeError(
                f"Page mismatch: requested PID {requested_pid}, "
                f"but browser shows PID {actual_pid}"
            )
        product = collect_product(sess.page, product_url, category=category)
        # Капча могла вылезти ПОСЛЕ первоначальной проверки (задержка инжекта
        # пазла / редирект ?__rr=1): если страница теперь заглушка или пазл,
        # данные карточки невалидны (название/цена из капчи). Собирать дальше
        # (отзывы, raw.json) нельзя — бросаем AccessChallengeError → recyclable.
        _ensure_no_challenge(sess.page, "product_post_extract")
        chars = product.get("characteristics") or {}
        if "Артикул" in chars and str(chars["Артикул"]) != str(requested_pid):
            log.warning(f"[collect] warning: артикул в характеристиках "
                        f"({chars['Артикул']}) отличается от PID {requested_pid}")
        if save_dom:
            try:
                dom_html = sess.page.content()
            except Exception as e:
                if B.is_renderer_failure(e):
                    raise
                log.warning(f"[collect] dom snapshot warn: {e}")
                dom_html = None
        rurl = reviews_url or product_url + "?tab=reviews"
        if reviews_url:
            rurl = _canonical_product_url(sess.page, reviews_url) + "?tab=reviews"
        rc = product.get("rating_count")
        no_count_hint = not (isinstance(rc, int) and rc > 0)
        if skip_reviews:
            # Быстрый режим: карточка уже собрана, отзывы намеренно не трогаем.
            # Отдельный маркер reviews_skipped (не путать с реальным нулём).
            log.info("[collect] skip_reviews=True — отзывы не собираю")
            reviews, total_pages, page_log = [], 1, []
            variant_mode = {"used": False, "reason": "reviews_skipped",
                            "variant_count": None}
            raw_api_ctx = {"enabled": raw_api_ctx.get("enabled", False),
                           "used": False, "reason": "reviews_skipped"}
        elif isinstance(rc, int) and rc <= 0:
            # У товара 0 отзывов целиком (кейс 4551551032: нет тумблера
            # варианта и пустой раздел) — прокручивать нечего, сортировки
            # и релоады бесполезны. Явный ноль из карточки доверяем.
            log.info("[collect] у товара 0 отзывов (rating_count=0) — "
                     "пропускаю сбор отзывов")
            reviews, total_pages, page_log = [], 1, []
            variant_mode = {"used": False, "reason": "no_reviews_product",
                            "variant_count": 0}
            raw_api_ctx = {"enabled": raw_api_ctx.get("enabled", False),
                           "used": False, "reason": "no_reviews_product"}
        else:
            (reviews, total_pages, page_log, variant_mode,
             raw_api_ctx) = _collect_reviews_stage(
                sess, rurl, target_variant, rc, requested_pid, max_pages,
                min_reviews, apply_variant_mode, sorts, telemetry,
                no_count_hint, reviews_api)
    finally:
        if sess_owned:
            sess.close()

    raw = {
        "collector_version": VERSION,
        "schema_version": SCHEMA_VERSION,
        "collected_at_utc": _t_utc(),
        "product_url": product_url,
        "reviews_url": rurl,
        "pid": requested_pid,
        "target_variant": target_variant,
        "product": product,
        "reviews": reviews,
        "reviews_collected": len(reviews),
        "reviews_skipped": bool(skip_reviews),
        "reviews_expected": None,
        "reviews_sorts": sorts,
        "reviews_variant_mode": variant_mode,
        "reviews_api": raw_api_ctx,
        "session": {"auth": auth_state},
        "pages": total_pages,
        "review_pages": page_log,
        "source": "cdp",
    }

    telemetry.setdefault("scroll_time", 0.0)
    telemetry.setdefault("extract_time", 0.0)
    telemetry.setdefault("pages", [])
    telemetry["scroll_jitter_seed"] = effective_scroll_jitter_seed()
    telemetry["total_review_collection_time"] = (
        telemetry["scroll_time"] + telemetry["extract_time"])
    raw["collection_telemetry"] = telemetry

    missing_fields = []
    if not (product.get("price") and product["price"].get("current")):
        missing_fields.append("product.price.current")
    if product.get("rating") is None:
        missing_fields.append("product.rating")
    raw["incomplete"] = bool(missing_fields)
    raw["missing_fields"] = missing_fields

    # Подтверждённый 0 отзывов: товар явно сообщает об отсутствии отзывов.
    # Такой raw кешируется, иначе отсутствие rating/price делает его
    # incomplete → каждый повторный проход пересобирает с нуля.
    # Два достоверных сигнала:
    #   1) rating_count == 0  (экстрактор карточки подтвердил)
    #   2) виджет варианта variant_count == 0 (при used=True)
    # Если rc=None и виджета нет — случай неоднозначный (сбой парсинга),
    # кешировать НЕЛЬЗЯ.
    rv = raw.get("reviews_variant_mode") or {}
    rc = (raw.get("product") or {}).get("rating_count")
    explicit_zero = (
        (isinstance(rc, int) and rc <= 0)
        or (bool(rv.get("used")) and rv.get("variant_count") == 0)
    )
    # skip_reviews НЕ подтверждает нулевика: отзывы просто не собирались, а не
    # проверены — иначе product-only raw закешировался бы как «отзывов нет».
    raw["no_reviews_confirmed"] = bool(
        explicit_zero
        and not raw.get("reviews_skipped")
        and not (raw.get("reviews") or [])
        and not variant_review_problems(raw)
    )

    if save_dom and dom_html:
        pid = pid_from_url(product_url)
        dom_dir = os.path.join("data", pid, "dom")
        os.makedirs(dom_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dom_path = os.path.join(dom_dir, f"{stamp}.html.gz")
        with gzip.open(dom_path, "wt", encoding="utf-8") as f:
            f.write(dom_html)
        raw["artifacts"] = {"dom_snapshot": dom_path}

    if not out_path:
        pid = pid_from_url(product_url)
        out_path = K.raw_save_path(pid, overwrite=overwrite)
    if out_path != K.raw_path(pid_from_url(product_url)) and not overwrite:
        print(f"[collect] {K.raw_path(pid_from_url(product_url))} уже "
              f"существует — сохраняю в {os.path.basename(out_path)} "
              f"(перезапись только с --overwrite)")
    K.atomic_write_json(out_path, raw)
    log.info(f"[collect] сохранено: {out_path} ({len(reviews)} отзывов)")
    return raw
