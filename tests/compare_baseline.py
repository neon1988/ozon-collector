"""Offline-сравнение собранного raw с baseline по review-ключам (Phase A).

Запуск (из корня проекта):
  python -m tests.compare_baseline --raw data/2561238412_raw.json
                                   --baseline tests/data/baseline_products.json
  python -m tests.compare_baseline --raw combined.json --baseline ... [--digest-limit 100]

Логика:
  review_keys — через ozon_collector.collect._review_key (единый источник identity).
  Для каждого товара печатает baseline_count / actual_count / missing_count /
  extra_count и ключи. Exit code:
    0 — у всех товаров missing_keys == ∅ (нет потери baseline);
    1 — хотя бы у одного missing_keys непусто (потеря отзывов);
    --strict-extra — extra_keys != ∅ тоже fail (проверка детерминизма);
    --extract-runs N — извлечь review_keys из raw N раз и потребовать
    идентичности (офлайн-проверка детерминизма слоя анализа; 2 при отличии).
  extra_keys по умолчанию — диагностическая метрика (дрейф Ozon на живом
  товаре), НЕ причина fail.

Принимает как единичный raw товара, так и combined-объект search
(products[].raw) — в этом случае сравниваются все ok-товары.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ozon_collector.collect import _review_key  # noqa: E402


def review_keys(reviews: list) -> list:
    """Отсортированные уникальные review-ключи (uuid, fallback sha1)."""
    return sorted({_review_key(r) for r in (reviews or [])})


def extract_product(raw: dict) -> dict:
    """{pid, sku, count, review_keys} из единичного raw товара."""
    reviews = raw.get("reviews") or []
    prod = raw.get("product") or {}
    sku = prod.get("sku")
    if not sku:
        sku = (prod.get("characteristics") or {}).get("Артикул")
    return {
        "pid": str(raw.get("pid") or ""),
        "sku": str(sku) if sku else None,
        "count": len(reviews),
        "review_keys": review_keys(reviews),
    }


def extract_all(raw: dict) -> dict:
    """pid -> extract_product для combined (products[]) или единичного raw."""
    products = raw.get("products")
    if isinstance(products, list):
        out = {}
        for p in products:
            if not p.get("ok"):
                continue
            pr = p.get("raw") or {}
            pid = str(pr.get("pid") or p.get("pid") or "")
            if pid:
                out[pid] = extract_product(pr)
        return out
    p = extract_product(raw)
    return {p["pid"]: p} if p["pid"] else {}


def compare(actual: dict, baseline: dict) -> dict:
    """pid -> {baseline_count, actual_count, missing, extra, sku_ok, ...}."""
    out = {}
    for pid, base in baseline.items():
        act = actual.get(pid)
        if act is None:
            out[pid] = {"present": False, "baseline_count": base["baseline_count"],
                        "actual_count": 0, "missing": set(base["review_keys"]),
                        "extra": set()}
            continue
        base_keys = set(base["review_keys"])
        act_keys = set(act["review_keys"])
        out[pid] = {
            "present": True,
            "baseline_count": base["baseline_count"],
            "actual_count": act["count"],
            "missing": base_keys - act_keys,
            "extra": act_keys - base_keys,
            "sku_baseline": base.get("sku"),
            "sku_actual": act.get("sku"),
        }
    return out


def run(raw_path: str, baseline_path: str, strict_extra: bool = False,
        extract_runs: int = 1, digest_limit: int | None = None,
        only_pid: str | None = None) -> int:
    raw = json.load(open(raw_path, encoding="utf-8"))
    baseline = json.load(open(baseline_path, encoding="utf-8"))
    base_products = {str(p["pid"]): p for p in baseline.get("products", [])}
    if only_pid:
        base_products = {k: v for k, v in base_products.items()
                         if k == only_pid}
        if not base_products:
            print(f"FAIL: pid {only_pid} отсутствует в baseline", file=sys.stderr)
            return 1

    if extract_runs > 1:
        first = extract_all(raw)
        for _ in range(extract_runs - 1):
            again = extract_all(raw)
            if again != first:
                print(f"FAIL: извлечение review_keys недетерминировано "
                      f"(runs={extract_runs})", file=sys.stderr)
                return 2
        print(f"детерминизм извлечения: OK (runs={extract_runs})")

    actual = extract_all(raw)
    results = compare(actual, base_products)
    if only_pid:
        results = {k: v for k, v in results.items() if k == only_pid}

    fail = False
    print(f"{'pid':<14}{'base':>6}{'act':>6}{'miss':>6}{'extra':>6}  sku")
    print("-" * 60)
    for pid in sorted(results):
        r = results[pid]
        if not r["present"]:
            print(f"{pid:<14}{r['baseline_count']:>6}{'—':>6}"
                  f"{len(r['missing']):>6}{'—':>6}  НЕТ В RAW")
            fail = True
            continue
        sku_note = "ok"
        if r["sku_baseline"] and r["sku_actual"] and r["sku_baseline"] != r["sku_actual"]:
            sku_note = f"РАЗНЫЕ ({r['sku_baseline']} vs {r['sku_actual']})"
        print(f"{pid:<14}{r['baseline_count']:>6}{r['actual_count']:>6}"
              f"{len(r['missing']):>6}{len(r['extra']):>6}  {sku_note}")
        if r["missing"]:
            fail = True
            print(f"  MISSING ({len(r['missing'])}): "
                  f"{sorted(r['missing'])[:20]}")
        if r["extra"]:
            print(f"  EXTRA ({len(r['extra'])}, диагностика): "
                  f"{sorted(r['extra'])[:20]}")
            if strict_extra:
                fail = True
        if digest_limit is not None:
            cap = min(r["actual_count"], digest_limit)
            print(f"  digest_reviews (лимит {digest_limit}): {cap} "
                  f"(raw: {r['actual_count']})")
    print("-" * 60)
    total_missing = sum(len(r["missing"]) for r in results.values())
    print(f"итог: missing={total_missing} "
          f"extra={sum(len(r['extra']) for r in results.values())} "
          f"-> {'PASS' if not fail else 'FAIL'}")
    return 0 if not fail else 1


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="compare_baseline",
        description="Сравнение собранного raw с baseline по review-ключам.")
    p.add_argument("--raw", required=True, help="raw.json или combined.json")
    p.add_argument("--baseline", default="tests/data/baseline_products.json")
    p.add_argument("--strict-extra", action="store_true",
                   help="extra_keys != ∅ тоже считается fail (детерминизм)")
    p.add_argument("--extract-runs", type=int, default=1,
                   help="сколько раз пересчитать review_keys и потребовать "
                        "идентичности (офлайн-детерминизм анализа)")
    p.add_argument("--digest-limit", type=int, default=None,
                   help="лимит отзывов в digest (для диагностики raw vs digest)")
    p.add_argument("--pid", default=None,
                   help="сравнить только один товар (остальные в baseline не fail)")
    args = p.parse_args(argv)
    sys.exit(run(args.raw, args.baseline, strict_extra=args.strict_extra,
                 extract_runs=args.extract_runs,
                 digest_limit=args.digest_limit, only_pid=args.pid))


if __name__ == "__main__":
    main()
