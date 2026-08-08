#!/usr/bin/env python3
"""Верификация v1.5 (нативный «Этот вариант товара») на случайных товарах Ozon.

Запуск (нужен живой CDP Chromium, см. scripts/launch_chrome.sh):
  python3 tests/verify_random.py scan            # собрать пул PID по квотам
  python3 tests/verify_random.py run             # полный контур по verify_pids.json
  python3 tests/verify_random.py run --contrast-pid <pid>
  python3 tests/verify_random.py report [--raw PATH]

Квоты (scan):
  A 3-4 товара с тумблером вариантов (отзывы > 0)
  B 2-3 товара БЕЗ тумблера (used=false, no_widget/no_variant_opt)
  C 1 с пагинацией «Дальше» (тайм-бокс, иначе NOT_FOUND)
  D 1 с малым числом отзывов (< 40)

Метрики (run): reviews_variant_mode.used/reason, однородность первого аспекта,
качество текста (text.strip(), без «Цвет товара:/Размер:/Вариант:», date, author),
обязательный контраст apply_variant_mode True vs False.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ozon_collector.cdp import Session, browser_alive  # noqa: E402
from ozon_collector.collect import (  # noqa: E402
    PRODUCT_JS, REVIEWS_JS, VARIANT_MODE_JS, _scroll_to_reviews,
    collect as _collect, pid_from_url,
)
from ozon_collector import report as R  # noqa: E402

WORK = "/tmp/opencode"
PIDS_FILE = os.path.join(WORK, "verify_pids.json")
SUMMARY_FILE = os.path.join(WORK, "verify_summary.json")
RAW_DIR = os.path.join(WORK, "verify")

SECTION_KEYWORDS = {
    "electronics": ["elektronik", "smartfon", "naushnik", "noutbuk", "planshet",
                    "televizor", "elektro", "foto", "smart-chasy"],
    "clothing": ["odezhd", "obuv", "odezd", "belev", "sumk"],
    "home": ["dom", "mebel", "bytov", "kuhn", "posud", "tovar"],
    "books": ["knig", "knizh", "chit"],
}

QUOTAS = {"A": 4, "B": 3, "C": 1, "D": 1}

BANNED_IN_TEXT = ("Цвет товара:", "Размер:", "Вариант:")
VARIANT_OPT = "Этот вариант товара"

DEFAULT_CATEGORY_URLS = [
    "https://www.ozon.ru/category/elektronika-15500/",
    "https://www.ozon.ru/category/odezhda-7500/",
    "https://www.ozon.ru/category/tovary-dlya-doma-7550/",
    "https://www.ozon.ru/category/knigi-16500/",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _first_aspect(variant: str) -> str:
    return (variant or "").splitlines()[0].strip()


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def _discover_sections(s: Session) -> list[str]:
    """Категории из навигации главной (по ключевым словам), fallback на список."""
    found = {}
    try:
        s.goto("https://www.ozon.ru", wait_ms=6000)
        hrefs = s.eval(
            """() => Array.from(document.querySelectorAll('a[href*="/category/"]'))
                          .map(a => a.getAttribute('href'))
                          .filter(Boolean)"""
        )
        for h in hrefs:
            if h.startswith("/"):
                h = "https://www.ozon.ru" + h
            low = h.lower()
            for sec, kws in SECTION_KEYWORDS.items():
                if any(k in low for k in kws):
                    found.setdefault(sec, []).append(h)
    except Exception as e:
        print(f"[scan] главная недоступна ({e}), берём дефолтные категории")
    for sec in SECTION_KEYWORDS:
        if sec not in found:
            found[sec] = []
    sections = [sorted(set(found[s]))[0] for s in SECTION_KEYWORDS if found[s]]
    sections += [u for u in DEFAULT_CATEGORY_URLS if u not in sections]
    return sections


def _scan_section(s: Session, url: str) -> list[str]:
    """Ссылки на товары со страницы раздела (уникальные PID).

    Ссылки Ozon содержат query (?at=...) — валидность и дедуп по pid_from_url."""
    try:
        s.goto(url, wait_ms=6000)
        hrefs = s.eval(
            """() => Array.from(document.querySelectorAll('a[href*="/product/"]'))
                          .map(a => a.href)"""
        )
    except Exception as e:
        print(f"[scan] раздел {url} недоступен: {e}")
        return []
    pids, out = set(), []
    for h in hrefs:
        p = pid_from_url(h)
        if p and p != "unknown" and p not in pids:
            pids.add(p)
            out.append(h)
    return out


def _classify(s: Session, url: str) -> dict:
    """Классификация кандидата: отзывы, тумблер вариантов, тип variant.

    rating_count может быть null (счётчик подгружается после прокрутки) — тогда
    идём на ?tab=reviews и проверяем REVIEWS_JS. webReviewsVariantMode грузится
    асинхронно — обязателен предварительный скролл на вкладке отзывов."""
    info = {"url": url, "pid": pid_from_url(url)}
    try:
        s.goto(url, wait_ms=4000)
        card = s.eval(PRODUCT_JS)
    except Exception as e:
        info["blocked"] = True
        info["error"] = f"card: {e}"
        return info
    info["rating_count"] = (card or {}).get("rating_count")

    reviews_url = url + ("?" if "?" not in url else "&") + "tab=reviews"
    try:
        s.goto(reviews_url, wait_ms=4500)
        _scroll_to_reviews(s.page, target_min=5, max_iters=40)
        vm = s.eval(VARIANT_MODE_JS)
        rv = s.eval(REVIEWS_JS)
    except Exception as e:
        info["blocked"] = True
        info["error"] = f"reviews: {e}"
        return info

    info["vm_found"] = bool(vm.get("found"))
    info["variant_present"] = bool(vm.get("variant_present"))
    info["variant_count"] = vm.get("variant_count")
    info["total_count"] = vm.get("total_count")
    info["active"] = vm.get("active")
    info["reviews_ok"] = bool(rv and rv.get("ok"))
    info["reviews_count"] = len((rv or {}).get("reviews") or [])
    info["pages"] = (rv or {}).get("pages", 1)
    aspects = {_first_aspect(r.get("variant", ""))
               for r in (rv or {}).get("reviews", []) if r.get("variant")}
    info["aspects"] = sorted(aspects)[:5]
    return info


def _quota_for(info: dict, got: dict) -> str | None:
    has_reviews = bool(info.get("rating_count")) or bool(info.get("reviews_ok"))
    if not has_reviews:
        return None
    if info.get("blocked"):
        return None
    if info.get("pages", 1) > 1:
        return "C"
    n = info.get("rating_count") or info.get("total_count") or 0
    if isinstance(n, int) and 0 < n < 40:
        return "D"
    if info.get("variant_present"):
        return "A"
    return "B"


def cmd_scan(args):
    if not browser_alive():
        print("FAIL: CDP-браузер не запущен")
        return 1
    s = Session()
    try:
        sections = _discover_sections(s)
        print("[scan] разделы:", ", ".join(sections))
        got = {"A": 0, "B": 0, "C": 0, "D": 0}
        selected, seen = [], set()
        start = time.monotonic()
        timebox = args.timebox * 60
        blocked_streak = 0
        for sec_url in sections:
            if sum(got.values()) >= args.limit:
                break
            for url in _scan_section(s, sec_url):
                if sum(got.values()) >= args.limit:
                    break
                if pid_from_url(url) in seen:
                    continue
                if time.monotonic() - start > timebox:
                    print("[scan] тайм-бокс превышен, стоп")
                    break
                seen.add(pid_from_url(url))
                info = _classify(s, url)
                if info.get("blocked"):
                    blocked_streak += 1
                    print(f"[scan] {info['pid']}: BLOCKED ({info.get('error')})")
                    if blocked_streak >= args.max_blocked:
                        print("[scan] 3 блокировки подряд — вероятно антибот, стоп scan")
                        raise SystemExit(2)
                    time.sleep(4)
                    continue
                blocked_streak = 0
                q = _quota_for(info, got)
                if q and got[q] < QUOTAS[q]:
                    info["quota"] = q
                    info["section"] = sec_url.split("/category/")[-1].split("/")[0]
                    got[q] += 1
                    selected.append(info)
                    print(f"[scan] {info['pid']} quota={q} "
                          f"rating_count={info.get('rating_count')} "
                          f"variant_present={info.get('variant_present')} "
                          f"pages={info.get('pages')} reviews={info.get('reviews_count')}")
                time.sleep(4)  # пауза против капчи
            if sum(got.values()) >= args.limit:
                break

        for q, cnt in got.items():
            if cnt < QUOTAS[q]:
                print(f"[scan] квота {q}: заполнено {cnt}/{QUOTAS[q]}")
        print(f"[scan] итого отобрано: {len(selected)} (квоты {got})")
        if got.get("C") == 0:
            print("[scan] C: NOT_FOUND (пагинация «Дальше» не встретилась)")

        payload = {
            "meta": {
                "created_utc": _now(),
                "sections": sections,
                "quotas_filled": got,
            },
            "pids": selected,
        }
        os.makedirs(WORK, exist_ok=True)
        with open(PIDS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[scan] сохранено: {PIDS_FILE}")
    finally:
        s.close()
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def _collect_one(pid: str, category: str, out: str, apply_variant_mode: bool) -> dict:
    return _collect(
        product_url=f"https://www.ozon.ru/product/{pid}",
        category=category,
        max_pages=3,
        min_reviews=20,
        out_path=out,
        save_dom=False,
        apply_variant_mode=apply_variant_mode,
    )


def _analyze(raw: dict) -> dict:
    reviews = raw.get("reviews", [])
    vm = raw.get("reviews_variant_mode") or {}
    issues = []
    empty = {"variant": 0, "date": 0, "author": 0, "text": 0}
    banned = 0
    for r in reviews:
        for f in ("variant", "date", "author"):
            if not (r.get(f) or "").strip():
                empty[f] += 1
        if not (r.get("text") or "").strip():
            empty["text"] += 1
        t = (r.get("text") or "")
        if any(b in t for b in BANNED_IN_TEXT):
            banned += 1
            issues.append("system label in text")
    aspects = sorted({_first_aspect(r.get("variant", ""))
                      for r in reviews if r.get("variant")})
    quality = "ok"
    if any(empty.values()):
        quality = "warn"
    if issues:
        quality = "fail"
    return {
        "collected": len(reviews),
        "unique_first_aspects": aspects,
        "empty_fields": empty,
        "banned_in_text": banned,
        "quality": quality,
        "issues": sorted(set(issues)),
    }


def _quick_measure(pid: str) -> dict:
    s = Session()
    try:
        s.goto(f"https://www.ozon.ru/product/{pid}?tab=reviews", wait_ms=4500)
        _scroll_to_reviews(s.page, target_min=15, max_iters=60)
        vm = s.eval(VARIANT_MODE_JS)
        rv = s.eval(REVIEWS_JS)
        aspects = sorted({_first_aspect(r.get("variant", ""))
                          for r in (rv or {}).get("reviews", []) if r.get("variant")})
        return {
            "vm": vm,
            "reviews_ok": bool(rv and rv.get("ok")),
            "reviews_count": len((rv or {}).get("reviews") or []),
            "aspects": aspects,
        }
    finally:
        s.close()


def _contrast(pid: str, category: str) -> dict:
    out_native = os.path.join(RAW_DIR, f"contrast_{pid}_native_raw.json")
    out_all = os.path.join(RAW_DIR, f"contrast_{pid}_all_raw.json")
    os.makedirs(RAW_DIR, exist_ok=True)
    raw_native = _collect_one(pid, category, out_native, apply_variant_mode=True)
    raw_all = _collect_one(pid, category, out_all, apply_variant_mode=False)
    a_native = _analyze(raw_native)
    a_all = _analyze(raw_all)
    vm_native = raw_native.get("reviews_variant_mode") or {}
    ok = (
        vm_native.get("used") is True
        and len(a_native["unique_first_aspects"]) == 1
        and len(a_all["unique_first_aspects"]) >= 1
        and a_all["collected"] >= a_native["collected"]
    )
    return {
        "pid": pid,
        "native": {"used": vm_native.get("used"), "reason": vm_native.get("reason"),
                   "collected": a_native["collected"],
                   "unique_first_aspects": a_native["unique_first_aspects"]},
        "unfiltered": {"used": (raw_all.get("reviews_variant_mode") or {}).get("used"),
                       "collected": a_all["collected"],
                       "unique_first_aspects": a_all["unique_first_aspects"]},
        "ok": ok,
        "raw_native": out_native,
        "raw_all": out_all,
    }


def cmd_run(args):
    if not browser_alive():
        print("FAIL: CDP-браузер не запущен")
        return 1
    if not os.path.exists(PIDS_FILE):
        print(f"FAIL: нет {PIDS_FILE} — сначала `scan`")
        return 1
    payload = json.load(open(PIDS_FILE, encoding="utf-8"))
    pids = payload["pids"]
    os.makedirs(RAW_DIR, exist_ok=True)

    results = []
    if args.pid:
        pids = [p for p in pids if p["pid"] == args.pid]
    for info in pids:
        pid = info["pid"]
        print(f"\n[run] === pid {pid} (quota {info.get('quota')}, "
              f"{info.get('section')}) ===")
        q = _quick_measure(pid)
        print(f"[run]   quick: reviews_ok={q['reviews_ok']} "
              f"count={q['reviews_count']} aspects={q['aspects']} "
              f"vm_active={q['vm'].get('active')}")

        raw_path = os.path.join(RAW_DIR, f"{pid}_raw.json")
        raw = _collect_one(pid, info.get("section") or "", raw_path,
                           apply_variant_mode=True)
        vm = raw.get("reviews_variant_mode") or {}
        an = _analyze(raw)

        res = {
            "pid": pid,
            "category": info.get("section") or "",
            "quota": info.get("quota"),
            "quick": q,
            "variant_mode": {
                "used": vm.get("used"),
                "reason": vm.get("reason"),
                "clicked": vm.get("clicked"),
                "variant_present": vm.get("variant_present"),
                "variant_count": vm.get("variant_count"),
                "total_count": vm.get("total_count"),
                "reviews_matched_variant": vm.get("reviews_matched_variant"),
                "reviews_dropped_by_client_filter": vm.get("reviews_dropped_by_client_filter"),
            },
            "reviews": an,
            "raw": raw_path,
        }
        res["pass"] = _pass_for(res)
        results.append(res)
        print(f"[run]   used={vm.get('used')} reason={vm.get('reason')} "
              f"variant_count={vm.get('variant_count')} total={vm.get('total_count')} "
              f"collected={an['collected']} aspects={an['unique_first_aspects']} "
              f"quality={an['quality']} -> {'PASS' if res['pass'] else 'FAIL'}")

    contrast = None
    if args.contrast or args.contrast_pid:
        cp = args.contrast_pid or next(
            (r["pid"] for r in results if r["quota"] == "A"), None)
        if cp:
            cat = next((r["category"] for r in results if r["pid"] == cp), "")
            print(f"\n[run] === контраст True vs False на {cp} ===")
            contrast = _contrast(cp, cat)
            print("[run]   native:  "
                  f"used={contrast['native']['used']} reason={contrast['native']['reason']} "
                  f"collected={contrast['native']['collected']} "
                  f"aspects={contrast['native']['unique_first_aspects']}")
            print("[run]   unfiltered: "
                  f"used={contrast['unfiltered']['used']} "
                  f"collected={contrast['unfiltered']['collected']} "
                  f"aspects={contrast['unfiltered']['unique_first_aspects']}")
            print(f"[run]   контраст -> {'PASS' if contrast['ok'] else 'FAIL'}")

    summary = {
        "meta": {"created_utc": _now(), "pids_file": PIDS_FILE},
        "results": results,
        "contrast": contrast,
    }
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n[run] сохранено: {SUMMARY_FILE}")

    n_pass = sum(1 for r in results if r["pass"])
    print(f"[run] итог: {n_pass}/{len(results)} PASS")
    return 0 if n_pass == len(results) else 1


def _pass_for(res: dict) -> bool:
    vm = res["variant_mode"]
    an = res["reviews"]
    quota = res.get("quota")
    if an["quality"] == "fail" or an["collected"] == 0:
        return False
    if quota in ("A", "C"):
        ok = (
            vm.get("used") is True
            and (vm.get("variant_count") or 0) > 0
            and (vm.get("reviews_matched_variant") or 0) > 0
            and len(an["unique_first_aspects"]) == 1
        )
        dropped = vm.get("reviews_dropped_by_client_filter") or 0
        if not ok:
            return False
        if dropped > 1:
            return False
        if dropped == 1:
            print(f"[run]   WARN pid {res['pid']}: dropped=1 (старый отзыв без variant)")
        return True
    if quota == "B":
        return vm.get("used") is False and vm.get("reason") in (
            "no_widget", "no_variant_opt")
    if quota == "D":
        return True  # просто завершился и собрал отзывы
    return True


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def cmd_report(args):
    if args.raw:
        raws = [args.raw]
    else:
        raws = sorted(
            os.path.join(RAW_DIR, f) for f in os.listdir(RAW_DIR)
            if f.endswith("_raw.json") and not f.startswith("contrast_")
        )
    for path in raws:
        raw = json.load(open(path, encoding="utf-8"))
        md = R.render(raw)
        line = next((l for l in md.splitlines()
                     if l.startswith("**Фильтр Ozon:**")), None)
        pid = raw.get("pid")
        print(f"[report] pid={pid}: {'OK ' if line else 'MISSING '}-> "
              f"{line if line else 'строки «Фильтр Ozon» нет'}")
        if args.out:
            out = os.path.join(args.out, f"{pid}.md")
            os.makedirs(args.out, exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                f.write(md)
            print(f"[report] сохранено: {out}")


def main():
    ap = argparse.ArgumentParser(prog="verify_random")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scan", help="собрать пул PID по квотам")
    sc.add_argument("--limit", type=int, default=10)
    sc.add_argument("--timebox", type=int, default=12,
                    help="макс. минут на весь scan")
    sc.add_argument("--max-blocked", type=int, default=3,
                    help="стоп при N блокировках подряд (антибот)")
    sc.set_defaults(fn=cmd_scan)

    sr = sub.add_parser("run", help="полный контур по verify_pids.json")
    sr.add_argument("--pid", help="только один PID из пула")
    sr.add_argument("--contrast", action="store_true",
                    help="обязательный контраст True vs False (первый A)")
    sr.add_argument("--contrast-pid", help="PID для контраста")
    sr.set_defaults(fn=cmd_run)

    rp = sub.add_parser("report", help="R.render() + проверка строки «Фильтр Ozon»")
    rp.add_argument("--raw", help="конкретный raw.json")
    rp.add_argument("--out", help="каталог для .md отчётов")
    rp.set_defaults(fn=cmd_report)

    args = ap.parse_args()
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
