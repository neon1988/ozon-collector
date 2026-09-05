"""Рендер Markdown-отчёта по собранному raw.json (без analysis-слоя)."""
from __future__ import annotations

import html as _html
import json
import logging
import os
import re

DEFAULT_REVIEWS_PER_PRODUCT = 100
# Максимальный размер одного файла дайджеста в байтах. 3 МиБ = 3 * 1024 * 1024.
DEFAULT_MAX_FILE_SIZE_BYTES = 3 * 1024 * 1024

_log = logging.getLogger(__name__)


def _norm_nbsp(s: str) -> str:
    """Очистить текст для вывода в markdown: убрать HTML-теги/коды, заменить
    неразрывные и похожие юникод-пробелы на обычные."""
    if not s:
        return s
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    out = list(s)
    for i, ch in enumerate(out):
        if ch in "\xa0\u202f\u2009\u200a":
            out[i] = " "
    return "".join(out)


def _collapse_newlines(s: str) -> str:
    """Схлопнуть повторные переводы строк в одиночный (убрать пустые строки),
    сохранив реальные разрывы строк; неразрывные/узкие юникод-пробелы заменить
    обычными. Используется для текстов отзывов, комментариев и описаний товаров:
    «абзац» на отдельной строке без пустых."""
    if not s:
        return s
    out = []
    for ln in s.split("\n"):
        if ln.strip() == "":
            continue
        # \xa0 (NBSP), \u202f, \u2009, \u200a — на обычный пробел
        ln = "".join(" " if ch in "\xa0\u202f\u2009\u200a" else ch for ch in ln)
        out.append(ln.rstrip())
    return "\n".join(out)


_ASPECT_RE = re.compile(r"^[^:\n]{1,40}:\s*\S")


def _customer_aspects(customer_opinion) -> list[str]:
    """Аксты из «По мнению покупателей» (`customer_opinion.text`).

    Сырой текст блока содержит аспектную сводку (Качество звука: хорошее …
    по одной строке на аспект) и следом мусор «Подобрали для вас»/футер.
    Возвращаем только аспектные строки (метка: значение), без мусора.
    """
    if not customer_opinion:
        return []
    text = (customer_opinion.get("text") or "").strip()
    if not text:
        return []
    lines = _norm_nbsp(text).split("\n")
    # начать после заголовка «По мнению покупателей»
    start = next((i for i, l in enumerate(lines)
                  if re.search(r"По\s+мнению\s+покупателей", l, re.I) and i + 1 < len(lines)),
                 None)
    if start is None:
        start = -1
    aspects = []
    for line in lines[start + 1:]:
        line = line.strip()
        if not line:
            break
        if not _ASPECT_RE.match(line):
            break
        aspects.append(line)
    return aspects


def _to_int_stamp(v):
    """publishedAt (строка Unix-сек или int) → int; None/мусор → 0."""
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return 0


def _review_text_sig(r: dict) -> bool:
    """Значимый ли текст отзыва (пригоден ли для LLM).

    False для пустышек, где в тексте только имя автора (+ первая буква с новой
    строки), напр. `М\\nМаксим С.` или `Андрей С.`. Также для stale-пустышек
    вида `О\\nОльга Б.\\n\\nОтзыв устарел` (плашка Ozon попала в текст, а
    содержательного текста нет — первая буква имени на отдельной строке).
    Такие отзывы собираются (остаются в raw), но не несут ценности для
    дайджеста. Если автор скрыт — «Имя скрыто» — тоже учитывается. При
    неуверенности (нет автора) — по длине.
    """
    text = (r.get("text") or "").strip()
    if not text:
        return False
    name = (r.get("author") or "").strip()
    if name:
        # убрать все вхождения имени (и имя дважды подряд) — остаток значим?
        rest = text
        low_name = name.lower()
        rest = re.sub(re.escape(name), "", text)
        rest = rest.lower()
        for pat in (low_name, low_name + " " + low_name):
            rest = rest.replace(pat, "")
        # служебный маркер Ozon «Отзыв устарел», попавший в текст пустышки
        rest = rest.replace("отзыв устарел", "")
        rest = re.sub(r"[\s\W_]+", "", rest)  # только буквы/цифры
        # одна буква/пусто после удаления имени — пустышка
        if len(rest) <= 1:
            return False
        return True
    # нет имени — по длине
    return len(re.sub(r"[\s\W_]+", "", text)) > 1


def _top_reviews(reviews: list, n: int) -> list:
    """Топ отзывов для дайджеста (по «популярности»).

    Метрика: (-(helpful_yes+helpful_no), -comments_count, -publishedAt,
    original_index) — сначала полезность, затем число комментариев, затем
    новизна; при полном равенстве сохраняется исходный порядок (стабильность).

    Пустышки (пусто либо только имя автора в text) исключаются из топа, если
    среди всех есть хоть один значимый (бесполезны для LLM); если значимых нет
    вовсе — топается как есть. Пустышками до n НЕ добираем. Не мутирует вход.
    """
    if not reviews or n <= 0:
        return []
    ranked = []
    for i, r in enumerate(reviews):
        if r in ranked:
            continue
        q = (r.get("helpful_yes") or 0) + (r.get("helpful_no") or 0)
        rk = (-q, -(r.get("comments_count") or 0),
              -_to_int_stamp(r.get("publishedAt")), i)
        ranked.append((rk, r, _review_text_sig(r)))
    has_text = any(t for _, _, t in ranked)
    if has_text:
        ranked = [x for x in ranked if x[2]]
    ranked.sort(key=lambda x: x[0])
    return [r for _, r, _ in ranked[:n]]


def plural(n: int, forms=("отзыв", "отзыва", "отзывов")) -> str:
    n10, n100 = n % 10, n % 100
    if 10 <= n100 <= 20:
        return forms[2]
    if n10 == 1:
        return forms[0]
    if 2 <= n10 <= 4:
        return forms[1]
    return forms[2]


def _is_score_asc(review: dict) -> bool:
    """Отзыв получен через диагностическую сортировку «сначала низкие оценки»
    (score_asc). Старые raw без метки review_sorts — не диагностические."""
    ss = review.get("review_sorts")
    if not ss:
        return False
    return "score_asc" in ss


def variant_rating_from(reviews: list) -> tuple:
    """(avg, n) по нейтральной выборке собранных отзывов.

    Средний балл (stars 1–5) и число оценок по отзывам, НЕ полученным через
    диагностическую сортировку score_asc («сначала низкие оценки»): её порция
    искажает распределение в сторону худших отзывов и к среднему не допускается
    (артефакт занижал рейтинг популярных товаров). (None, 0) — нейтральных
    отзывов с оценкой нет.
    """
    vals = []
    for r in (reviews or []):
        if _is_score_asc(r):
            continue
        v = r.get("stars")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 1 <= v <= 5:
            vals.append(v)
    if not vals:
        return None, 0
    return sum(vals) / len(vals), len(vals)


def official_rating_info(product: dict) -> tuple:
    """Справочный рейтинг всей карточки Ozon: (rating, count).

    Сначала точное полное распределение оценок customer_opinion.counts
    (среднее по 1–5★), затем product.rating (count из rating_count, если есть).
    (None, None) — данных нет.
    """
    co = (product or {}).get("customer_opinion") or {}
    total, acc = 0, 0.0
    for k, v in (co.get("counts") or {}).items():
        try:
            star, n = int(k), int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= star <= 5 and n > 0:
            acc += star * n
            total += n
    if total:
        return acc / total, total
    r = (product or {}).get("rating")
    if isinstance(r, (int, float)) and not isinstance(r, bool) and 0 < r <= 5:
        rc = (product or {}).get("rating_count")
        if isinstance(rc, (int, float)) and not isinstance(rc, bool) and rc > 0:
            return float(r), int(rc)
        return float(r), None
    return None, None


def official_negative_share(product: dict) -> tuple:
    """Доля негативных оценок (1–2★): (share 0..1, total).

    Считается ТОЛЬКО по полному распределению всей карточки Ozon
    (customer_opinion.counts): (N1 + N2) / (N1+N2+N3+N4+N5). Собранные отзывы
    для доли не используются: диагностический канал score_asc и лимит сбора
    смещают выборку в сторону худших отзывов (selection bias — «показатель
    эффективности скрипта», а не статистика покупателей). (None, None) —
    распределения нет: долю не выводить и не выдумывать.
    """
    co = (product or {}).get("customer_opinion") or {}
    neg, total = 0, 0
    for k, v in (co.get("counts") or {}).items():
        try:
            star, n = int(k), int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= star <= 5 and n > 0:
            total += n
            if star <= 2:
                neg += n
    if not total:
        return None, None
    return neg / total, total


def digest_rating(product: dict, reviews: list) -> tuple:
    """(rating, count, source) — основной рейтинг дайджеста.

    source:
      'variant'  — среднее по нейтральной выборке собранных отзывов (без
                   score_asc); count — число таких оценок. Рейтинг выбранного
                   варианта.
      'official' — рейтинг всей карточки Ozon (нейтральной выборки нет);
                   count — полное число оценок (0, если неизвестно).
      None       — рейтинга нет вовсе.
    """
    avg, n = variant_rating_from(reviews)
    if avg is not None:
        return avg, n, "variant"
    rating, cnt = official_rating_info(product)
    if rating is not None:
        return rating, int(cnt or 0), "official"
    return None, 0, None


def sort_digest_products(products: list) -> None:
    """Отсортировать товары дайджеста in-place: «лучшие» в начало.

    Балл — рейтинг выбранного варианта по нейтральной выборке (без
    диагностической сортировки «сначала низкие оценки» score_asc) × число
    таких оценок (решение пользователя 2026-09-05, уточнено после разбора
    артефакта score_asc: порция «сначала низкие» занижала рейтинг популярных
    товаров). Товары без нейтральной выборки — по официальному рейтингу
    карточки без множителя (ниже имеющих рейтинг варианта). Товары без
    рейтинга вовсе — ниже имеющих его; ошибочные (ok=False) — в конец.
    При равном балле — больше N, затем pid.
    """
    def _key(p):
        if not p.get("ok"):
            return (1, 0, 0, 0, p.get("pid", 0))
        raw = p.get("raw") or {}
        rating, n, src = digest_rating(raw.get("product") or {}, raw.get("reviews"))
        if rating is None:
            return (0, float("inf"), 0, 0, p.get("pid", 0))   # без рейтинга — ниже
        cnt = n if src == "variant" else 1
        return (0, -(rating * cnt), -cnt, 0, p.get("pid", 0))
    products.sort(key=_key)


def _num(v):
    return float(v) if isinstance(v, str) else v


def _price_block(product: dict) -> list:
    """Строки секции цены: только цена с Ozon Картой (price.bank, кнопка
    «С банками») одной строкой. Старая цена, current, уверенность и структура
    price_parts не выводятся (по решению пользователя 2026-09-03).

    Если цены с картой нет — fallback на текущую цену (price.current) одной
    строкой; при её отсутствии — на raw price_text.
    """
    p = product.get("price") or {}
    money = lambda v: f"{_num(v):,}".replace(",", " ")
    if p.get("bank"):
        return [f"Цена с Ozon Картой: {money(p['bank'])} ₽"]
    if p.get("current"):
        return [f"Цена: {money(p['current'])} ₽"]
    if product.get("price_text"):
        return [f"- {product['price_text']}"]
    return []


def _variant_str(target: dict) -> str:
    """Человекочитаемая строка варианта: размеры (Д×Ш×В см) или ключ:значение."""
    if not target:
        return ""
    dims = target.get("Длина_см"), target.get("Ширина_см"), target.get("Высота_см")
    if any(v is not None for v in dims):
        return f"{dims[0]} × {dims[1]} × {dims[2]} см (Д × Ш × В)"
    parts = [f"{k}: {v}" for k, v in target.items() if v]
    return "; ".join(parts) if parts else ""


def _dedup_key(c: dict):
    """Ключ группы повторяющихся ответов: (автор, первая фраза без «Имя,»).

    Первая фраза — до первого '.', '!', '?' или перевода строки; ведущее
    «Имя покупателя, » вырезается (ответы шаблонные и отличаются только именем).
    Пустой текст → None (такие ответы не группируются).
    """
    text = re.sub(r"\s+", " ", (c.get("text") or "").strip())
    if not text:
        return None
    first = re.split(r"[.!?\n]", text, maxsplit=1)[0].strip()
    first = re.sub(r"^[^,\n]+,\s*", "", first).strip()
    return ((c.get("author") or "").strip(), first.lower())


def _group_comments(comments: list) -> list:
    """[(comment, dup_count), ...] — первый ответ каждой группы + сколько
    одинаковых пропущено. Порядок = порядок первого появления в raw.
    Ответы без текста всегда выводятся (dup_count=0)."""
    first = {}
    counts = {}
    for c in comments:
        k = _dedup_key(c)
        if k is not None and k not in first:
            first[k] = c
        counts[k] = counts.get(k, 0) + 1
    out = []
    for c in comments:
        k = _dedup_key(c)
        if k is not None and first[k] is not c:
            continue
        out.append((c, counts[k] - 1 if k is not None else 0))
    return out


def _comment_lines(c: dict) -> list:
    ca = _collapse_newlines(_norm_nbsp(c.get("author") or "—"))
    cdate = c.get("date") or ""
    reply = c.get("reply_to")
    reply_txt = f" (в ответ: {reply})" if reply else ""
    if ca == "OZON":
        ctext = "_(текст скрыт)_"
    else:
        ctext = _collapse_newlines(_norm_nbsp(c.get("text") or ""))
    lines = [
        f"> → {ca}{reply_txt} — {cdate}",
        f"> {ctext}",
    ]
    like = c.get("like") or 0
    dislike = c.get("dislike") or 0
    if like or dislike:
        lines.append(f"> _👍 {like}, 👎 {dislike}_")
    return lines


def _render_comments(comments: list, first_meta=None) -> list:
    """Рендер комментариев.

    first_meta: {id(comment): dup_count} — выводятся только первые комментарии
    групп, с меткой «…и ещё N … пропущено». None — рендерить все (без дедупа).
    """
    lines = []
    for c in comments:
        if first_meta is not None and id(c) not in first_meta:
            continue
        lines.extend(_comment_lines(c))
        n = (first_meta or {}).get(id(c)) or 0
        if n > 0:
            form = plural(n,
                          ("такой же ответ", "таких же ответа", "таких же ответов"))
            lines.append(f"> _…и ещё {n} {form} продавца пропущено_")
    return lines


def _review_block(r: dict, first_meta=None, variant_in_header: bool = False,
                  no: int = 0) -> list:
    star = "★" * min(5, r.get("stars") or 0) + "☆" * (5 - min(5, r.get("stars") or 0))
    author = _collapse_newlines(_norm_nbsp(r.get("author") or "—"))
    date = r.get("date") or ""
    marker = " _(без текста)_" if r.get("_no_text") else ""
    numbered = f"{no}. {author}" if no else author
    v_raw = r.get("variant") or ""
    v_parts = v_raw.split("\n") if v_raw else [""]
    v_head = v_parts[0].strip()
    v_aspects = " · ".join(x.strip() for x in v_parts[1:] if x.strip())
    if v_head and variant_in_header:
        lines = [f"{numbered} — {date}{marker}"]
    elif v_head:
        lines = [f"{numbered} — {date} | {v_head}{marker}"]
    else:
        lines = [f"{numbered} — {date}{marker}"]
    if v_aspects:
        lines.append(v_aspects)
    txt = _collapse_newlines(_norm_nbsp(r.get("text") or ""))
    if r.get("_no_text"):
        txt = "_(без текста)_"
    if r.get("stale"):
        lines.append(f"> {txt}")
    else:
        lines.append(f"> {star} {txt}")
    helpful_yes = r.get("helpful_yes") or 0
    helpful_no = r.get("helpful_no") or 0
    if helpful_yes or helpful_no:
        lines.append(f"> _👍 {helpful_yes}, 👎 {helpful_no}_")
    lines.extend(_render_comments(r.get("comments") or [], first_meta))
    lines.append("")
    return lines


def render(raw: dict, dedup_replies: bool = True) -> str:
    product = raw.get("product", {})
    target = raw.get("target_variant", {})
    reviews = raw.get("reviews", [])
    L: list = []

    L.append(f"# {product.get('title') or 'Отчёт'}")
    L.append("")
    L.append(f"- Дата сбора (UTC): {raw.get('collected_at_utc', '—')}")
    L.append(f"- Collector: {raw.get('collector_version', '—')}")
    L.append(f"- Схема: {raw.get('schema_version', '—')}")
    L.append("- Источник: Ozon (CDP)")
    if raw.get("incomplete"):
        missing = ", ".join(raw.get("missing_fields") or ["?"])
        L.append("")
        L.append(f"> ⚠ Неполный сбор. Отсутствуют: {missing}")
    L.append("")

    # ---- 1. Карточка ------------------------------------------------------
    L.append("## 1. Карточка товара")
    L.append("")
    L.append(f"- Товар: {product.get('title', '—')}")
    pre = raw.get("_digest_rating")
    if pre:
        rating, n, src = pre["rating"], pre["n"], pre["source"]
    else:
        rating, n, src = digest_rating(product, reviews)
    if src == "variant":
        L.append(f"- Рейтинг по собранным отзывам (без «сначала низкие»): "
                 f"{rating:.3f} / 5 "
                 f"({n} {plural(n, ('оценка', 'оценки', 'оценок'))})")
        off_rating, off_count = official_rating_info(product)
        if off_rating is not None:
            cnt_txt = (f" · {off_count} "
                       f"{plural(off_count, ('оценка', 'оценки', 'оценок'))}"
                       if off_count else "")
            L.append(f"- Ozon (вся карточка): {off_rating:.3f} / 5{cnt_txt}")
    elif src == "official":
        cnt_txt = (f" ({n} {plural(n, ('оценка', 'оценки', 'оценок'))})" if n else "")
        L.append(f"- Рейтинг карточки Ozon: {rating:.3f} / 5{cnt_txt} "
                 f"_(нет нейтральной выборки)_")
    neg_share, neg_total = official_negative_share(product)
    if neg_share is not None:
        L.append(f"- Негативных оценок (1–2★): {neg_share * 100:.1f}% · "
                 f"по {neg_total} "
                 f"{plural(neg_total, ('оценке', 'оценкам', 'оценкам'))} "
                 f"(вся карточка Ozon)")
    if product.get("category"):
        L.append(f"- Категория: {product['category']}")
    variant_str = _variant_str(target)
    if variant_str:
        L.append(f"- Вариант: {variant_str}")
    aspects = _customer_aspects(product.get("customer_opinion"))
    if aspects:
        L.append("- По мнению покупателей:")
        for a in aspects:
            L.append(f"  - {a}")
    co = product.get("customer_opinion") or {}
    rec = co.get("recommend_percent")
    if rec is not None:
        L.append(f"  - Рекомендуют: {rec}% покупателей")
    L.append(f"- URL: {raw.get('product_url', '—')}")
    L.append("")

    # ---- 2. Цена -----------------------------------------------------------
    L.append("## 2. Цена и акции")
    L.append("")
    L.extend(_price_block(product))
    L.append("")

    # ---- 3. Характеристики ------------------------------------------------
    chars = product.get("characteristics") or {}
    if chars:
        L.append("## 3. Характеристики")
        L.append("")
        L.append("| Параметр | Значение |")
        L.append("|----------|----------|")
        for k, v in chars.items():
            L.append(f"| {k} | {v} |")
        L.append("")

    # ---- 4. Описание --------------------------------------------------------
    descs = product.get("descriptions") or []
    if descs:
        L.append("## 4. Описание")
        L.append("")
        for d in descs:
            text = _collapse_newlines(d) if isinstance(d, str) else str(d)
            if text.lower().startswith("описание"):
                text = text[len("описание"):].lstrip()
            L.append(text)
            L.append("")

    # ---- 5. Галерея (OCR) ----------------------------------------------------
    ocr = raw.get("gallery_ocr") or []
    if ocr:
        L.append("## 5. Галерея (OCR)")
        L.append("")
        L.append(f"_{raw.get('gallery_stats_text', '')}_")
        L.append("")
        for o in ocr:
            label = f"img {o.get('path')}"
            if o.get("url"):
                label = f"[{label}]({o['url']})"
            if o.get("text"):
                L.append(f"- {label} (conf {o.get('mean_conf')}): {o['text']}")
            else:
                L.append(f"- {label}: _(без текста)_")
        L.append("")

    # ---- 6. Все отзывы варианта ----------------------------------------------
    vm = raw.get("reviews_variant_mode") or {}
    _total_hdr = None
    if vm.get("used") and vm.get("variant_count"):
        _total_hdr = vm.get("variant_count")
    elif (raw.get("stars_full") or {}).get("total") is not None:
        _total_hdr = raw["stars_full"]["total"]
    L.append(f"## 6. Все отзывы варианта ({_total_hdr if _total_hdr is not None else len(reviews)})")
    L.append("")
    if vm.get("used") and vm.get("variant_count"):
        line = (f"Фильтр Ozon: «Этот вариант товара» "
                f"({vm.get('variant_count')} из {vm.get('total_count') or '—'})")
        if vm.get("reviews_dropped_by_client_filter"):
            line += f"; клиентский фильтр отбросил {vm.get('reviews_dropped_by_client_filter')}"
        L.append(line)
        L.append("")
    elif vm.get("variant_present") is not None and not vm.get("used"):
        L.append("Фильтр Ozon: не применён (собраны все отзывы)")
        L.append("")
    sorts_req = raw.get("reviews_sorts")
    if sorts_req:
        _sort_labels = {
            "published_at_desc": "новые и полезные",
            "score_desc": "сначала высокие оценки",
            "score_asc": "сначала низкие оценки",
        }
        parts = []
        # по умолчанию — по текущей выборке reviews; если есть sorts_full
        # (счётчики по ВСЕМ собранным отзывам, док-пол восстановлен
        # в render_search_digest до обрезания топ-N) — берём их.
        if raw.get("sorts_full"):
            counts = raw["sorts_full"]
        else:
            counts = {s: sum(1 for r in reviews if s in (r.get("review_sorts") or []))
                      for s in sorts_req}
        parts = [f"собрано {_sort_labels.get(s, s)}: {counts.get(s, 0)}" for s in sorts_req]
        if parts:
            parts[0] = "Собрано " + parts[0][len("собрано "):]
        L.append(" · ".join(parts))
        L.append("")
    variants = {(r.get("variant") or "").split("\n", 1)[0] for r in reviews}
    variant_in_header = len(variants) == 1 and next(iter(variants))
    if variant_in_header:
        L.append("Вариант: " + next(iter(variants)))
        L.append("")
    if not reviews:
        L.append("_Отзывов не собрано._")
        L.append("")
    else:
        # Отзывы делятся на две статистически разные роли:
        #   neutral — обычная выборка (без «сначала низкие»), используется для
        #             рейтинга варианта (variant_rating_from) — ИНВАРИАНТ:
        #             это ровно то же множество отзывов.
        #   diag    — диагностические (score_asc): целенаправленный поиск
        #             проблем/брака; их количество НЕ доля недовольных.
        neutral = [r for r in reviews if not _is_score_asc(r)]
        diag = [r for r in reviews if _is_score_asc(r)]
        first_meta = None
        if dedup_replies:
            all_comments = [c for r in reviews for c in (r.get("comments") or [])]
            first_meta = {id(c): n for c, n in _group_comments(all_comments)}

        def _stars_summary(revs: list) -> list:
            cc = {s: 0 for s in range(1, 6)}
            for r in revs:
                s = r.get("stars")
                if isinstance(s, int) and 1 <= s <= 5:
                    cc[s] += 1
            return [f"★{s}: {cc[s]}" for s in range(5, 0, -1) if cc[s]]

        if neutral:
            L.append(f"### Отзывы покупателей — обычная выборка ({len(neutral)} "
                     f"{plural(len(neutral), ('отзыв', 'отзыва', 'отзывов'))})")
            L.append("")
            L.append("_Отзывы без сортировки «сначала низкие оценки». Эта выборка "
                     "используется для расчёта рейтинга варианта (раздел 1) и "
                     "отражает обычное впечатление покупателей._")
            L.append("")
            for i, r in enumerate(neutral, 1):
                L.extend(_review_block(r, first_meta, variant_in_header, no=i))
            sp = _stars_summary(neutral)
            if sp:
                L.append(" · ".join(sp))
                L.append("")

        if diag:
            L.append(f"### Негативные отзывы — целенаправленный поиск проблем "
                     f"({len(diag)} "
                     f"{plural(len(diag), ('отзыв', 'отзыва', 'отзывов'))})")
            L.append("")
            L.append("> ⚠️ Эти отзывы собраны сортировкой «сначала низкие "
                     "оценки» для поиска недостатков. Их количество НЕ является "
                     "долей недовольных покупателей и не должно использоваться "
                     "для оценки распространённости негатива. Реальная "
                     "статистика — в разделе 1 и официальных данных Ozon.")
            L.append("")
            for i, r in enumerate(diag, 1):
                L.extend(_review_block(r, first_meta, variant_in_header, no=i))
            sp = _stars_summary(diag)
            if sp:
                L.append(" · ".join(sp))
                L.append("")

        # Итог по всем собранным отзывам (включая диагностическую порцию).
        if raw.get("stars_full") is not None:
            total = raw["stars_full"]["total"]
            counts = raw["stars_full"]["counts"]
        else:
            counts = {s: 0 for s in range(1, 6)}
            for r in reviews:
                s = r.get("stars")
                if isinstance(s, int) and 1 <= s <= 5:
                    counts[s] += 1
            total = len(reviews)
        star_parts = [f"★{s}: {counts[s]}" for s in range(5, 0, -1) if counts[s]]
        if star_parts:
            L.append(f"Итог по всем собранным отзывам: {total} {plural(total)}"
                     " · " + " · ".join(star_parts))
            L.append("")

    # ---- 7. Метаданные --------------------------------------------------------
    L.append("## 7. Метаданные")
    L.append("")
    L.append(f"- Собрано (UTC): {raw.get('collected_at_utc', '—')}")
    L.append(f"- Отзывов собрано: {raw.get('reviews_collected', 0)}")
    L.append(f"- OCR: {raw.get('gallery_stats_text', 'не выполнялся')}")
    L.append("")

    return "\n".join(L).rstrip() + "\n"


def _digest_header_lines(combined: dict, chunk: tuple[int, int] | None,
                         reviews_per_product: int) -> list[str]:
    """Строки шапки дайджеста (общей для каждой части) без продуктовой части."""
    search = combined.get("search") or {}
    L = ["# Дайджест выдачи Ozon", ""]
    if chunk:
        L.append(f"- Часть {chunk[0]} из {chunk[1]}")
    if search.get("query"):
        L.append(f"- Запрос: {search['query']}")
    if search.get("url"):
        L.append(f"- URL выдачи: {search['url']}")
    L.append(f"- Дата сбора (UTC): {combined.get('collected_at_utc', '—')}")
    L.append(f"- Найдено товаров в выдаче: "
             f"{combined.get('found', 0)} уникальных товаров, "
             f"собрано {combined.get('collected', 0)}.")
    L.append(f"- Отзывы в дайджесте: до {reviews_per_product} на товар "
             f"(в raw сохраняются все).")
    L.append("")
    return L


def _excluded_summary_lines(excluded: list) -> list[str]:
    """Строки верхней сводки об исключённых по рейтингу товарах."""
    if not excluded:
        return []
    e0 = excluded[0].get("excluded") or {}
    return [
        f"- Исключено по рейтингу: {len(excluded)} товаров "
        f"(рейтинг ниже {e0.get('min_rating', '?')}, см. секцию "
        f"«Исключено по рейтингу»).",
        "",
    ]


def _excluded_section_lines(excluded: list) -> list[str]:
    """Строки нижней секции «Исключено по рейтингу»."""
    L = ["---", "", "## Исключено по рейтингу", ""]
    for p in excluded:
        e = p.get("excluded") or {}
        rawp = p.get("raw") or {}
        title = (rawp.get("product", {}).get("title") or "").strip() or "—"
        L.append(f"- {p.get('pid')} · рейтинг {e.get('rating')} "
                 f"(мин. {e.get('min_rating')}): {title}")
    L.append("")
    return L


def _product_block_lines(p: dict, idx: int, reviews_per_product: int,
                         dedup_replies: bool) -> list[str]:
    """Строки markdown-блока одного товара (с ведущим разделителем `---`).

    Чистая функция без побочных эффектов: не мутирует `combined`/`raw`.
    `idx` — глобальный номер товара в выдаче (сквозной, не per-part), т.к.
    байт-размер блока должен быть одинаков в планировании и при записи.
    """
    import copy

    L = ["---", ""]
    if not p.get("ok"):
        L.append(f"## Товар {p.get('pid', '?')}")
        L.append("")
        L.append(f"⚠ Не удалось собрать: {p.get('error', 'неизвестная ошибка')}")
        L.append("")
        return L
    meta = []
    if p.get("cache"):
        meta.append(f"кэш: {p['cache']}")
    if p.get("ocr"):
        meta.append(f"ocr: {p['ocr']}")
    raw = p.get("raw") or {}
    meta.append(f"отзывов: {raw.get('reviews_collected', 0)}")
    L.append(f"## Товар {idx}: {p.get('pid', '?')} ({', '.join(meta)})")
    L.append("")
    if reviews_per_product and reviews_per_product > 0:
        raw_view = copy.deepcopy(raw)
        reviews = raw_view.get("reviews") or []
        # счётчики сортировок по ВСЕМ собранным отзывам (до топ-N),
        # чтобы строка «Сортировки» отражала реальный сбор, а не топ.
        sorts_req = raw.get("reviews_sorts") or []
        if sorts_req:
            raw_view["sorts_full"] = {
                s: sum(1 for r in reviews if s in (r.get("review_sorts") or []))
                for s in sorts_req}
        # счётчик оценок по ВСЕМ собранным отзывам (до топ-N), чтобы
        # строка «Всего: … ★…» в конце блока отражала весь сбор.
        _counts = {s: 0 for s in range(1, 6)}
        for r in reviews:
            _s = r.get("stars")
            if isinstance(_s, int) and 1 <= _s <= 5:
                _counts[_s] += 1
        raw_view["stars_full"] = {"total": len(reviews), "counts": _counts}
        # рейтинг варианта по ВСЕМ собранным отзывам (до топ-N), чтобы порция
        # срезанных из дайджеста отзывов не влияла на рейтинг в карточке.
        rrating, rn, rsrc = digest_rating(raw.get("product") or {}, reviews)
        raw_view["_digest_rating"] = {"rating": rrating, "n": rn,
                                      "source": rsrc}
        raw_view["reviews"] = _top_reviews(reviews, reviews_per_product)
    else:
        raw_view = raw
    md = render(raw_view, dedup_replies=dedup_replies)
    for line in md.rstrip("\n").split("\n"):
        L.append(line)
    L.append("")
    return L


def render_search_digest(combined: dict,
                         reviews_per_product: int = DEFAULT_REVIEWS_PER_PRODUCT,
                         dedup_replies: bool = True,
                         products: list | None = None,
                         chunk: tuple[int, int] | None = None,
                         start_index: int = 1) -> str:
    """Markdown-дайджест выдачи Ozon для передачи в LLM (Gemini).

    combined — результат collect_search. Для каждого успешного товара
    используется существующий render(raw), но отзывы обрезаются ТОЛЬКО в
    дайджесте (raw остаётся полным). Ошибки товаров — компактным блоком.

    products — необязательный список товаров для рендера (по умолчанию берутся
    все из combined["products"]). chunk — (номер, всего) для файлов-частей.
    start_index — глобальный номер первого товара (для сквозной нумерации при
    рендере подмножества в частях; по умолчанию 1).
    """
    L = _digest_header_lines(combined, chunk, reviews_per_product)

    products = combined.get("products") or [] if products is None else products
    listed = [p for p in products if not p.get("excluded")]
    excluded = [p for p in products if p.get("excluded")]
    L.extend(_excluded_summary_lines(excluded))
    for j, p in enumerate(listed, start_index):
        L.extend(_product_block_lines(p, j, reviews_per_product, dedup_replies))
    L.extend(_excluded_section_lines(excluded))

    L.append("---")
    L.append("")
    md = "\n".join(L)
    size_kb = len(md.encode("utf-8")) / 1024
    L.append(f"- Размер дайджеста: {size_kb:.2f} КБ")
    return "\n".join(L)


def write_search_digest(combined: dict,
                        out: str,
                        reviews_per_product: int = DEFAULT_REVIEWS_PER_PRODUCT,
                        dedup_replies: bool = True,
                        max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES) -> list:
    """Записать дайджест в md-файл(ы), разбивая на части по РАЗМЕРУ файла.

    max_file_size_bytes (по умолчанию 3 МиБ) — жёсткий лимит размера каждого
    файла в байтах UTF-8. При превышении рендерятся отдельные файлы: `out` для
    первой части, `out.part2`, `out.part3`, … (каждая со своей шапкой
    «Часть k из N»). `max_file_size_bytes <= 0` — без разбиения (один файл).

    Гарантия: каждый файл ≤ max_file_size_bytes, КРОМЕ единственного разрешённого
    исключения — части, содержащей РОВНО один товар, чей блок сам по себе больше
    лимита (такой файл может превысить лимит, в лог пишется warning).

    Планирование границ делает консервативную (завышенную) оценку размера:
    шапка-части и строка «Размер дайджеста» берутся худшим случаем, поэтому итог
    записи никогда не превышает оценку. После записи каждый нормальный файл
    проверяется через os.path.getsize; превышение вне исключения — RuntimeError.
    """
    products = combined.get("products") or []
    listed = [p for p in products if not p.get("excluded")]
    excluded = [p for p in products if p.get("excluded")]

    if max_file_size_bytes <= 0 or not listed:
        md = render_search_digest(combined, reviews_per_product, dedup_replies)
        _write_md(md, out)
        return [out]

    # -- Проход A: рендерим блок каждого товара ОДИН раз, считаем байты. --
    # Консервативная (верхняя) оценка размера части:
    #   - шапка с «Часть N из N» (ширина не меньше реальной при любом N <= len(listed));
    #   - запас на строку «Размер дайджеста» (число КБ не больше 99999.99),
    #     ново-строки между блоками и разделители.
    worst_chunk = (len(listed), len(listed))
    prefix_lines = _digest_header_lines(combined, worst_chunk, reviews_per_product)
    prefix_lines += _excluded_summary_lines(excluded)
    prefix = "\n".join(prefix_lines)
    tails = _excluded_section_lines(excluded) + ["---", ""]
    tail_bytes = len("\n".join(tails).encode("utf-8"))
    # запас на разделители-переносы между блоками/шапкой/хвостом и строку размера
    separator_overhead = len(listed) + len(tails) + 1
    size_line_bytes = len("- Размер дайджеста: 99999.99 КБ".encode("utf-8"))

    block_contents = [
        _product_block_lines(p, i, reviews_per_product, dedup_replies)
        for i, p in enumerate(listed, 1)]
    block_bytes = [len("\n".join(b).encode("utf-8")) for b in block_contents]

    prefix_bytes = len(prefix.encode("utf-8"))

    def group_bytes(indices: list[int]) -> int:
        return (prefix_bytes + sum(block_bytes[i] for i in indices)
                + separator_overhead + tail_bytes + size_line_bytes)

    # -- Жадная нарезка по порядку (без перестановок), консервативная оценка. --
    # Оценка заведомо завышена (worst-case шапка и строка размера), поэтому
    # итог записи не превысит max для групп размером >1 товара. Ширина реальной
    # строки «Часть k из N» не больше ширины worst-случая при N = len(listed)
    # (N ≤ len(listed)), т.к. цифр у len(listed) не меньше. Перепланирование
    # по изменению N не требуется.
    groups: list[list[int]] = []
    cur: list[int] = []
    for i in range(len(listed)):
        cur.append(i)
        if len(cur) > 1 and group_bytes(cur) > max_file_size_bytes:
            groups.append(cur[:-1])
            cur = [cur[-1]]
    if cur:
        groups.append(cur)

    # -- Проход B: запись и контроль фактического размера на диске. --
    written = []
    for k, grp in enumerate(groups):
        n_parts = len(groups)
        base = grp[0] + 1  # глобальный номер первого товара группы
        md = render_search_digest(
            combined, reviews_per_product, dedup_replies,
            products=[listed[idx] for idx in grp],
            chunk=(k + 1, n_parts) if n_parts > 1 else None,
            start_index=base)
        if k == 0:
            path = out
        else:
            root, ext = os.path.splitext(out)
            path = f"{root}.part{k + 1}{ext}"
        _write_md(md, path)
        actual = os.path.getsize(path)
        if actual > max_file_size_bytes:
            if len(grp) == 1:
                _log.warning(
                    "Товар %s превышает лимит дайджеста: %d байт (> %d). "
                    "Выделен в отдельный файл %s",
                    listed[grp[0]].get("pid"), actual, max_file_size_bytes, path)
            else:
                raise RuntimeError(
                    f"файл {path} ({actual} байт) превысил лимит "
                    f"{max_file_size_bytes} байт без разрешённого исключения")
        written.append(path)
    return written


def _write_md(md: str, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(md)


def minimum_header_size_bytes(combined: dict,
                              reviews_per_product: int = DEFAULT_REVIEWS_PER_PRODUCT) -> int:
    """Минимальный размер одного файла дайджеста (только шапка + хвост).

    Считается по контексту combined (длина запроса/URL/даты, сводка об
    исключённых). Нужен CLI для отклонения заведомо невыполнимого лимита.
    """
    products = combined.get("products") or []
    listed = [p for p in products if not p.get("excluded")]
    excluded = [p for p in products if p.get("excluded")]
    if not listed:
        return 0
    worst_chunk = (len(listed), len(listed))
    prefix = _digest_header_lines(combined, worst_chunk, reviews_per_product)
    prefix += _excluded_summary_lines(excluded)
    tails = _excluded_section_lines(excluded) + ["---", ""]
    size_line_bytes = len("- Размер дайджеста: 99999.99 КБ".encode("utf-8"))
    return (len("\n".join(prefix).encode("utf-8"))
            + len("\n".join(tails).encode("utf-8"))
            + size_line_bytes + 2)


def _best_for_digest(pid: str, data_dir: str):
    """Лучший raw pid для дайджеста.

    Возвращает (raw|None, path|None, reason): reason == "ok" при валидном
    кандидате; иначе причина отклонения (missing / challenge / no_reviews /
    old_version / incompatible / incomplete / bad_timestamp).
    """
    from ozon_collector import cache as K
    from ozon_collector.constants import CHALLENGE_TITLE_MARKERS

    best = None
    best_path = None
    best_key = None
    reason = "missing"
    for path in K._raw_candidates(pid, data_dir):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            continue
        r = K._validate_raw(raw, pid)
        if r is not None:
            reason = r
            continue
        title = (raw.get("product") or {}).get("title") or ""
        t = title.strip().lower()
        if any(m in t for m in CHALLENGE_TITLE_MARKERS):
            reason = "challenge"
            continue
        n = int(raw.get("reviews_collected") or 0)
        if n <= 0:
            reason = "no_reviews"
            continue
        dt = K._parse_utc(raw.get("collected_at_utc"))
        key = (
            K._version_tuple(raw.get("collector_version")) or (0,),
            dt.timestamp() if dt is not None else 0.0,
            n,
        )
        if best is None or key > best_key:
            best, best_path, best_key = raw, path, key
    if best is None:
        return None, None, reason
    return best, best_path, "ok"


def build_search_digest_combined(pids, data_dir: str, query: str | None = None):
    """Собрать combined-структуру дайджеста из raw по списку pid.

    Для каждого pid выбирается лучший валидный raw (см. _best_for_digest),
    по умолчанию query — «артикулы: …» из отсортированных pid (офлайн, из raw).
    Возвращает (combined, table): combined — готовый дайджест для
    render_search_digest/write_search_digest; table — строки-таблица «pid / число
    отзывов / версия / файл / title» для вывода в консоль (или список ошибок SKIP).
    """
    from datetime import datetime, timezone

    products = []
    table = []
    for pid in sorted(pids):
        raw, path, reason = _best_for_digest(pid, data_dir)
        if raw is None:
            products.append({"ok": False, "pid": pid,
                             "error": f"нет валидных данных ({reason})"})
            table.append(f"{pid:>12}  SKIP  {reason}")
            continue
        n = int(raw.get("reviews_collected") or 0)
        ver = raw.get("collector_version") or "?"
        title = (raw.get("product") or {}).get("title") or ""
        title_short = (title[:70] + "…") if len(title) > 70 else title
        table.append(f"{pid:>12}  {n:>3} отз.  {ver:>6}  "
                     f"{os.path.basename(path):<38} {title_short}")
        products.append({
            "ok": True,
            "pid": pid,
            "raw": raw,
            "cache": f"raw:{os.path.basename(path)}",
        })

    sort_digest_products(products)

    if query is None:
        query = (f"артикулы: {', '.join(sorted(pids))} (офлайн, из raw)")
    combined = {
        "search": {"query": query},
        "collected_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "found": len(pids),
        "collected": sum(1 for p in products if p.get("ok")),
        "products": products,
    }
    return combined, table
