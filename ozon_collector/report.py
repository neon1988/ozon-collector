"""Рендер Markdown-отчёта по собранному raw.json (без analysis-слоя)."""
from __future__ import annotations

import html as _html
import json
import os
import re

DEFAULT_REVIEWS_PER_PRODUCT = 100
DEFAULT_MAX_PRODUCTS_PER_FILE = 100


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
    current = _num(p.get("current"))
    if current:
        money = lambda v: f"{_num(v):,}".replace(",", " ")
        lines.append(f"Цена: {money(current)} ₽")
        if p.get("old"):
            disc = round((1 - current / _num(p["old"])) * 100)
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
            lines.append(f"- {p.get('raw')}")
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
        lines.append(f"- {product['price_text']}")
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
    if ca == "OZON":
        ctext = "_(текст скрыт)_"
    else:
        ctext = _norm_nbsp(c.get("text") or "")
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
    author = r.get("author") or "—"
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
    txt = _norm_nbsp(r.get("text") or "")
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
    avg = _avg_review_rating(reviews)
    if avg is not None:
        L.append(f"- Средний рейтинг по отзывам: {avg:.3f} / 5 "
                 f"({len(reviews)} {plural(len(reviews), ('оценка', 'оценки', 'оценок'))})")
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
    first_meta = None
    if dedup_replies:
        all_comments = [c for r in reviews for c in (r.get("comments") or [])]
        first_meta = {id(c): n for c, n in _group_comments(all_comments)}
    for i, r in enumerate(reviews, 1):
        L.extend(_review_block(r, first_meta, variant_in_header, no=i))
    if raw.get("stars_full") is not None:
        total, counts = raw["stars_full"]["total"], raw["stars_full"]["counts"]
    else:
        counts = {s: 0 for s in range(1, 6)}
        for r in reviews:
            s = r.get("stars")
            if isinstance(s, int) and 1 <= s <= 5:
                counts[s] += 1
        total = len(reviews)
    star_parts = [f"★{s}: {counts[s]}" for s in range(5, 0, -1) if counts[s]]
    if star_parts:
        L.append(f"Из собранных после удаление дубликатов: {total} {plural(total)}"
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

    products = combined.get("products") or [] if products is None else products
    listed = [p for p in products if not p.get("excluded")]
    excluded = [p for p in products if p.get("excluded")]
    if excluded:
        e0 = excluded[0].get("excluded") or {}
        L.append(f"- Исключено по рейтингу: {len(excluded)} товаров "
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
            raw_view["reviews"] = _top_reviews(reviews, reviews_per_product)
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
            L.append(f"- {p.get('pid')} · рейтинг {e.get('rating')} "
                     f"(мин. {e.get('min_rating')}): {title}")
        L.append("")

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
