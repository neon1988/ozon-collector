#!/usr/bin/env python3
"""Smoke-тест на ЖИВОЙ странице Ozon через CDP (вне обычного pytest).

Запуск: python3 tests/smoke_cdp.py [--pid 3627230434] [--pid2 2561238412] [--compare]
Нужен запущенный Chromium: scripts/launch_chrome.sh
Отличие от tests/test_extractors.py: реальная сеть + живой рендер,
в т.ч. гидратация React. Для запуска в CI/headless не предназначен.

Проверки:
  * одностраничная: ключевые поля PRODUCT_JS (rating/source/count/chars/desc/price/gallery);
  * --compare: сверка с эталоном tests/expected_snapshots.json (свежесть фикстур/разметки);
  * двухстраничная: после перехода A → B данные и pid соответствуют странице B
    (регрессия бага «CDP оставался на предыдущей странице»).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ozon_collector.cdp import browser_alive, Session
from ozon_collector.collect import (
    PRODUCT_JS, REVIEWS_JS, VARIANT_MODE_JS, _scroll_to_reviews, pid_from_url,
)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOTS = os.path.join(TESTS_DIR, "expected_snapshots.json")

SCORE_WIDGETS = {
    "webProductScore",
    "webSingleProductScore",
    "webReviewProductScore",
    "fallback_text",
}

VARIANT_OPT = "Этот вариант товара"


def load_snapshots() -> dict:
    with open(SNAPSHOTS, encoding="utf-8") as f:
        return json.load(f)


def fetch(pid: str) -> tuple[Session, dict]:
    s = Session()
    s.goto(f"https://www.ozon.ru/product/{pid}", wait_ms=8000)
    return s, s.eval(PRODUCT_JS)


def check(pid: str, snapshot: dict | None = None) -> int:
    if not browser_alive():
        print("FAIL: CDP-браузер не запущен (см. scripts/launch_chrome.sh)")
        return 1
    s, d = fetch(pid)
    try:
        url_pid = pid_from_url(s.page.url)
    finally:
        s.close()

    rating = d.get("rating")
    source = d.get("rating_source")
    count = d.get("rating_count")
    chars = d.get("characteristics") or {}
    desc = d.get("descriptions") or []
    price = d.get("price") or {}
    gallery = d.get("gallery") or []

    fails = []
    if not (isinstance(rating, (int, float)) and 0 < rating <= 5):
        fails.append(f"rating={rating!r}")
    if source not in SCORE_WIDGETS:
        fails.append(f"source={source!r}")
    if not (isinstance(count, (int, float)) and count > 0):
        fails.append(f"count={count!r}")
    if len(chars) < 10:
        fails.append(f"chars={len(chars)}")
    if not desc:
        fails.append("descriptions пуст")
    if not (isinstance(price.get("current"), (int, float)) and price["current"] > 0):
        fails.append(f"price.current={price.get('current')!r}")
    if not gallery:
        fails.append("gallery пуст")

    if snapshot:
        for key, field in (("rating", "rating"), ("rating_count", "rating_count"),
                           ("price_current", "price.current")):
            exp = snapshot.get(key)
            got = rating if key == "rating" else (count if key == "rating_count" else price.get("current"))
            if exp is not None and got != exp:
                fails.append(f"snapshot {key}: ожидалось {exp}, получено {got}")
        if "article" in snapshot and chars.get("Артикул") != snapshot["article"]:
            fails.append(f"snapshot article: ожидалось {snapshot['article']}, получено {chars.get('Артикул')}")

    print(f"pid={pid}: rating={rating} source={source} count={count} "
          f"chars={len(chars)} desc={len(desc)} price={price.get('current')} "
          f"gallery={len(gallery)} url_pid={url_pid}")
    if fails:
        print("FAIL: " + "; ".join(fails))
        return 1
    print("SMOKE OK")
    return 0


def check_variant_mode(pid: str) -> int:
    """Живая проверка нативного фильтра «Этот вариант товара»: открываем
    ?tab=reviews&reviewsVariantMode=1, скроллим, извлекаем REVIEWS_JS и
    VARIANT_MODE_JS. Ожидаем: фильтр активен, вариантная лента однородна
    по первому аспекту (цвету)."""
    if not browser_alive():
        print("FAIL: CDP-браузер не запущен (см. scripts/launch_chrome.sh)")
        return 1
    s = Session()
    fails = []
    first_aspects = set()
    try:
        s.goto(f"https://www.ozon.ru/product/{pid}?tab=reviews&reviewsVariantMode=1",
               wait_ms=9000)
        if not _scroll_to_reviews(s.page, target_min=20):
            fails.append("отзывы не появились при прокрутке")
        vm = s.eval(VARIANT_MODE_JS)
        d = s.eval(REVIEWS_JS)
        if d and d.get("ok"):
            first_aspects = {
                (r.get("variant") or "").splitlines()[0].strip()
                for r in d["reviews"]
                if r.get("variant")
            }
    finally:
        s.close()

    if not vm.get("found"):
        fails.append(f"widget webReviewsVariantMode не найден: {vm}")
    elif vm.get("active") != VARIANT_OPT:
        fails.append(f"фильтр не активен: active={vm.get('active')!r} (options={vm.get('options')})")
    elif vm.get("variant_present") and not (vm.get("variant_count", 0) or 0) > 0:
        fails.append(f"variant_count не число: {vm.get('variant_count')!r}")

    if not (d and d.get("ok")):
        fails.append("REVIEWS_JS не вернул отзывы")
    elif not first_aspects:
        fails.append("в отзывах нет variant-полей")
    elif len(first_aspects) > 1:
        fails.append(f"вариантная лента не однородна: {first_aspects}")

    print(f"variant_mode pid={pid}: active={vm.get('active')} "
          f"variant_count={vm.get('variant_count')} total={vm.get('total_count')} "
          f"reviews={len((d or {}).get('reviews') or [])} "
          f"aspects={sorted(first_aspects)}")
    if fails:
        print("FAIL: " + "; ".join(fails))
        return 1
    print("VARIANT MODE OK")
    return 0


def check_two_pages(pid_a: str, pid_b: str) -> int:
    """После goto A → B данные и pid должны соответствовать странице B."""
    if not browser_alive():
        print("FAIL: CDP-браузер не запущен (см. scripts/launch_chrome.sh)")
        return 1
    s = Session()
    try:
        s.goto(f"https://www.ozon.ru/product/{pid_a}", wait_ms=8000)
        url_pid_a = pid_from_url(s.page.url)
        da = s.eval(PRODUCT_JS)
        s.goto(f"https://www.ozon.ru/product/{pid_b}", wait_ms=8000)
        url_pid_b = pid_from_url(s.page.url)
        db = s.eval(PRODUCT_JS)
    finally:
        s.close()

    fails = []
    if url_pid_a != pid_a:
        fails.append(f"page A: url_pid={url_pid_a} != {pid_a}")
    if url_pid_b != pid_b:
        fails.append(f"page B: url_pid={url_pid_b} != {pid_b}")
    if url_pid_a == url_pid_b:
        fails.append("pid страниц не сменился — вероятен кэш/смешивание вкладок")
    if (da.get("characteristics") or {}).get("Артикул") == (db.get("characteristics") or {}).get("Артикул"):
        fails.append("характеристики A и B идентичны — данные не перезагрузились")

    print(f"two-page: A pid={url_pid_a} (rating {da.get('rating')}) → "
          f"B pid={url_pid_b} (rating {db.get('rating')})")
    if fails:
        print("FAIL: " + "; ".join(fails))
        return 1
    print("TWO-PAGE OK")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", default="3627230434")
    ap.add_argument("--pid2", default="2561238412", help="вторая страница для two-page проверки")
    ap.add_argument("--compare", action="store_true",
                    help="сверка с tests/expected_snapshots.json")
    args = ap.parse_args()
    rc = check(args.pid, load_snapshots().get(args.pid) if args.compare else None)
    rc |= check_two_pages(args.pid, args.pid2)
    rc |= check_variant_mode(args.pid)
    sys.exit(rc)


if __name__ == "__main__":
    main()
