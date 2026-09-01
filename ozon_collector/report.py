"""Рендер Markdown-отчёта по собранному raw.json (без analysis-слоя)."""
from __future__ import annotations

import os
import re

DEFAULT_REVIEWS_PER_PRODUCT = 100
DEFAULT_MAX_PRODUCTS_PER_FILE = 100


def plural(n: int, forms=("отзыв", "отзыва", "отзывов")) -> str:
    n10, n100 = n % 10, n % 100
    if 10 <= n100 <= 20:
        return forms[2]
    if n10 == 1:
        return forms[0]
    if 2 <= n10 <= 4:
        return forms[1]
    return forms[2]


def _avg_review_rating(reviews: list) -> float | None:
    """Средний балл по оценкам собранных отзывов (stars 1–5), None — отзывов нет."""
    vals = [r.get("stars") for r in (reviews or [])]
    vals = [v for v in vals if isinstance(v, (int, float)) and 1 <= v <= 5]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _num(v):
    return float(v) if isinstance(v, str) else v


def _price_block(product: dict) -> list:
    """Строки секции цены: price (webPrice, точная) / price_parsed / price_parts."""
    lines = []
    p = product.get("price") or {}
    current = p.get("current")
    if current:
        money = lambda v: f"{v:,}".replace(",", " ")
        lines.append(f"**Цена: {money(current)} ₽**")
        if p.get("old"):
            disc = round((1 - current / p["old"]) * 100)
            lines.append(f"- Старая цена: {money(p['old'])} ₽ (скидка {disc}%)")
        if p.get("bank"):
            lines.append(f"- С банком (кнопка «С банками»): {money(p['bank'])} ₽")
        if p.get("confidence"):
            lines.append(f"- Уверенность: {p['confidence']:.0%} (источник: {p.get('source')})")
        return lines
    parsed = product.get("price_parsed") or []
    parts = product.get("price_parts") or []
    if parsed:
        for p in parsed[:3]:
            lines.append(f"- **{p.get('raw')}**")
        lines.append("")
        lines.append("_Цена актуальная (крупный кегль/bold). Для оплаты через СБП/иные способы "
                     "могут быть другие цены — см. price_parsed выше._")
    if parts:
        lines.append("")
        lines.append("### Структура цены (price_parts)")
        lines.append("")
        lines.append("| Текст | Кегль | Bold | Зачёркнута |")
        lines.append("|-------|------|------|------------|")
        for p in parts:
            strike = "✓" if p.get("strike") else ""
            lines.append(f"| {p.get('t', '')} | {p.get('fs', 0)} | {'✓' if p.get('weight') else ''} | {strike} |")
        lines.append("")
    elif product.get("price_text"):
        lines.append(f"- **{product['price_text']}**")
    return lines


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
    ca = c.get("author") or "—"
    cdate = c.get("date") or ""
    reply = c.get("reply_to")
    reply_txt = f" (в ответ: {reply})" if reply else ""
    lines = [
        f"> **→ {ca}**{reply_txt} — {cdate}",
        f"> {c.get('text') or ''}",
    ]
    like = c.get("like") or 0
    dislike = c.get("dislike") or 0
    if like or dislike:
        lines.append(f"> _👍 {like} · 👎 {dislike}_")
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


def _review_block(r: dict, first_meta=None) -> list:
    star = "★" * min(5, r.get("stars") or 0) + "☆" * (5 - min(5, r.get("stars") or 0))
    author = r.get("author") or "—"
    date = r.get("date") or ""
    variant = r.get("variant") or ""
    marker = " _(без текста)_" if r.get("_no_text") else ""
    lines = [f"**{author}** — {date} | {variant}{marker}"]
    txt = r.get("text") or ""
    if r.get("_no_text"):
        txt = "_(без текста)_"
    lines.append(f"> {star} {txt}")
    helpful_yes = r.get("helpful_yes") or 0
    helpful_no = r.get("helpful_no") or 0
    if helpful_yes or helpful_no:
        lines.append(f"> _👍 полезно: {helpful_yes}, нет: {helpful_no}_")
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
    L.append(f"- **Дата сбора (UTC):** {raw.get('collected_at_utc', '—')}")
    L.append(f"- **Collector:** {raw.get('collector_version', '—')}")
    L.append(f"- **Схема:** {raw.get('schema_version', '—')}")
    L.append("- **Источник:** Ozon (CDP)")
    if raw.get("incomplete"):
        missing = ", ".join(raw.get("missing_fields") or ["?"])
        L.append("")
        L.append(f"> ⚠ **Неполный сбор.** Отсутствуют: {missing}")
    L.append("")

    # ---- 1. Карточка ------------------------------------------------------
    L.append("## 1. Карточка товара")
    L.append("")
    L.append(f"- **Товар:** {product.get('title', '—')}")
    avg = _avg_review_rating(reviews)
    if avg is not None:
        L.append(f"- **Средний рейтинг по отзывам:** {avg:.3f} / 5 "
                 f"({len(reviews)} {plural(len(reviews), ('оценка', 'оценки', 'оценок'))})")
    if product.get("category"):
        L.append(f"- **Категория:** {product['category']}")
    variant_str = _variant_str(target)
    if variant_str:
        L.append(f"- **Вариант:** {variant_str}")
    L.append(f"- **URL:** {raw.get('product_url', '—')}")
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
            text = d if isinstance(d, str) else str(d)
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
            label = f"**img {o.get('path')}**"
            if o.get("url"):
                label = f"[{label}]({o['url']})"
            if o.get("text"):
                L.append(f"- {label} (conf {o.get('mean_conf')}): {o['text']}")
            else:
                L.append(f"- {label}: _(без текста)_")
        L.append("")

    # ---- 6. Все отзывы варианта ----------------------------------------------
    L.append(f"## 6. Все отзывы варианта ({len(reviews)})")
    L.append("")
    vm = raw.get("reviews_variant_mode") or {}
    if vm.get("used") and vm.get("variant_count"):
        line = (f"**Фильтр Ozon:** «Этот вариант товара» "
                f"({vm.get('variant_count')} из {vm.get('total_count') or '—'})")
        if vm.get("reviews_dropped_by_client_filter"):
            line += f"; клиентский фильтр отбросил {vm.get('reviews_dropped_by_client_filter')}"
        L.append(line)
        L.append("")
    elif vm.get("variant_present") is not None and not vm.get("used"):
        L.append("**Фильтр Ozon:** не применён (собраны все отзывы)")
        L.append("")
    sorts_req = raw.get("reviews_sorts")
    if sorts_req:
        _sort_labels = {
            "published_at_desc": "новые и полезные",
            "score_desc": "сначала высокие оценки",
            "score_asc": "сначала низкие оценки",
        }
        parts = []
        for s in sorts_req:
            n = sum(1 for r in reviews if s in (r.get("review_sorts") or []))
            parts.append(f"{_sort_labels.get(s, s)}: {n}")
        L.append("**Сортировки:** " + " · ".join(parts))
        L.append("")
    if not reviews:
        L.append("_Отзывов не собрано._")
        L.append("")
    counts = {s: 0 for s in range(1, 6)}
    for r in reviews:
        s = r.get("stars")
        if isinstance(s, int) and 1 <= s <= 5:
            counts[s] += 1
    star_parts = [f"★{s}: {counts[s]}" for s in range(5, 0, -1) if counts[s]]
    if star_parts:
        L.append(f"**Всего: {len(reviews)} {plural(len(reviews))}** · " + " · ".join(star_parts))
        L.append("")
    first_meta = None
    if dedup_replies:
        all_comments = [c for r in reviews for c in (r.get("comments") or [])]
        first_meta = {id(c): n for c, n in _group_comments(all_comments)}
    for r in reviews:
        L.extend(_review_block(r, first_meta))

    # ---- 7. Метаданные --------------------------------------------------------
    L.append("## 7. Метаданные")
    L.append("")
    L.append(f"- Собрано (UTC): {raw.get('collected_at_utc', '—')}")
    L.append(f"- Отзывов собрано: {raw.get('reviews_collected', 0)}")
    L.append(f"- OCR: {raw.get('gallery_stats_text', 'не выполнялся')}")
    L.append("")

    return "\n".join(L).rstrip() + "\n"


def render_search_digest(combined: dict,
                         reviews_per_product: int = DEFAULT_REVIEWS_PER_PRODUCT,
                         dedup_replies: bool = True,
                         products: list | None = None,
                         chunk: tuple[int, int] | None = None) -> str:
    """Markdown-дайджест выдачи Ozon для передачи в LLM (Gemini).

    combined — результат collect_search. Для каждого успешного товара
    используется существующий render(raw), но отзывы обрезаются ТОЛЬКО в
    дайджесте (raw остаётся полным). Ошибки товаров — компактным блоком.

    products — необязательный список товаров для рендера (по умолчанию берутся
    все из combined["products"]). chunk — (номер, всего) для файлов-частей.
    """
    import copy

    L = []
    search = combined.get("search") or {}
    L.append("# Дайджест выдачи Ozon")
    L.append("")
    if chunk:
        L.append(f"- **Часть {chunk[0]} из {chunk[1]}**")
    if search.get("query"):
        L.append(f"- **Запрос:** {search['query']}")
    if search.get("url"):
        L.append(f"- **URL выдачи:** {search['url']}")
    L.append(f"- **Дата сбора (UTC):** {combined.get('collected_at_utc', '—')}")
    L.append(f"- **Найдено товаров в выдаче:** "
             f"{combined.get('found', 0)} уникальных товаров, "
             f"собрано {combined.get('collected', 0)}.")
    L.append(f"- **Отзывы в дайджесте:** до {reviews_per_product} на товар "
             f"(в raw сохраняются все).")
    L.append("")

    products = combined.get("products") or [] if products is None else products
    listed = [p for p in products if not p.get("excluded")]
    excluded = [p for p in products if p.get("excluded")]
    if excluded:
        e0 = excluded[0].get("excluded") or {}
        L.append(f"- **Исключено по рейтингу:** {len(excluded)} товаров "
                 f"(рейтинг ниже {e0.get('min_rating', '?')}, см. секцию "
                 f"«Исключено по рейтингу»).")
        L.append("")
    for i, p in enumerate(listed, 1):
        L.append("---")
        L.append("")
        if not p.get("ok"):
            L.append(f"## Товар {p.get('pid', '?')}")
            L.append("")
            L.append(f"⚠ Не удалось собрать: {p.get('error', 'неизвестная ошибка')}")
            L.append("")
            continue
        meta = []
        if p.get("cache"):
            meta.append(f"кэш: {p['cache']}")
        if p.get("ocr"):
            meta.append(f"ocr: {p['ocr']}")
        raw = p.get("raw") or {}
        meta.append(f"отзывов: {raw.get('reviews_collected', 0)}")
        L.append(f"## Товар {i}: {p.get('pid', '?')} ({', '.join(meta)})")
        L.append("")
        if reviews_per_product and reviews_per_product > 0:
            raw_view = copy.deepcopy(raw)
            reviews = raw_view.get("reviews") or []
            raw_view["reviews"] = reviews[:reviews_per_product]
        else:
            raw_view = raw
        md = render(raw_view, dedup_replies=dedup_replies)
        for line in md.rstrip("\n").split("\n"):
            L.append(line)
        L.append("")

    if excluded:
        L.append("---")
        L.append("")
        L.append("## Исключено по рейтингу")
        L.append("")
        for p in excluded:
            e = p.get("excluded") or {}
            rawp = p.get("raw") or {}
            title = (rawp.get("product", {}).get("title") or "").strip() or "—"
            L.append(f"- **{p.get('pid')}** · рейтинг {e.get('rating')} "
                     f"(мин. {e.get('min_rating')}): {title}")
        L.append("")

    L.append("---")
    L.append("")
    L.append("## Задание для LLM")
    L.append("")
    L.append("Выбери лучший товар из списка выше и объясни, почему. "
             "Учитывай цену, рейтинг, количество оценок и содержание отзывов "
             "(плюсы/минусы, частые жалобы).")
    L.append("")
    md = "\n".join(L)
    size_kb = len(md.encode("utf-8")) / 1024
    L.append(f"- **Размер дайджеста:** {size_kb:.2f} КБ")
    return "\n".join(L)


def write_search_digest(combined: dict,
                        out: str,
                        reviews_per_product: int = DEFAULT_REVIEWS_PER_PRODUCT,
                        dedup_replies: bool = True,
                        max_products_per_file: int = DEFAULT_MAX_PRODUCTS_PER_FILE) -> list:
    """Записать дайджест в md-файл(ы), разбивая на части по числу товаров.

    max_products_per_file (по умолчанию 100) — максимум товаров в одном файле.
    При превышении рендерятся отдельные файлы: `out` для первой части,
    `out.part2`, `out.part3`, … (каждая со своей шапкой «Часть k из N»).
    `max_products_per_file <= 0` — без разбиения. Возвращает список записанных путей.
    """
    import math

    products = combined.get("products") or []
    total = len(products)
    if max_products_per_file <= 0 or total <= max_products_per_file:
        md = render_search_digest(combined, reviews_per_product, dedup_replies)
        _write_md(md, out)
        return [out]

    n_parts = math.ceil(total / max_products_per_file)
    written = []
    for k in range(n_parts):
        chunk_products = products[k * max_products_per_file:(k + 1) * max_products_per_file]
        md = render_search_digest(
            combined, reviews_per_product, dedup_replies,
            products=chunk_products, chunk=(k + 1, n_parts))
        if k == 0:
            path = out
        else:
            root, ext = os.path.splitext(out)
            path = f"{root}.part{k + 1}{ext}"
        _write_md(md, path)
        written.append(path)
    return written


def _write_md(md: str, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
