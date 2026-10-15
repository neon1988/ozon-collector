"""QA-сканер собранных raw-файлов: офлайн-контроль качества данных.

Проверяет каждый canonical raw (data/{pid}_raw.json) по утверждённой
спецификации Data Quality Gate и присваивает товару вердикт:

FAIL  — доказанная порча данных: дубликаты uuid, собрано больше официального
        счётчика (при подтверждённом scope), звёзд категории больше официальной
        гистограммы, битый JSON/структура, challenge-страница вместо отзывов,
        мусор в текстах (\\uFFFD/undefined/NaN/HTML-артефакты), даты из будущего.
WARN  — подозрительно, но не обязательно ошибка: неполное покрытие в FULL,
        короткий итог в WALL_LIMITED, перекос сортировок при неполном сборе,
        ноль собранных отзывов, записи без текста и без звёзд, повторы одного
        текста у разных авторов, доля stars=0 > 5%,
        ориентир взят из rating_count (no_widget), данные старше недели.
INFO  — метрики и видимость: coverage в WALL_LIMITED, пропуск гистограммы,
        паттерн «оценка без текста» (будущий rating_only), неподтверждённый scope,
        причина недобора у сессионного потолка Ozon: UNAUTH_WALL (профиль не
        авторизован), AUTH_UNKNOWN (состояние не определено),
        AUTH_NOT_MEASURED (raw без session.auth). Последние три — только при
        WALL_LIMITED и не влияют на вердикт: срез неавторизованной сессии не
        делает собранное браком.

verdict = FAIL при любом FAIL; иначе WARN при любом WARN; иначе OK.
collection_mode: FULL (official <= 100) | WALL_LIMITED (> 100) | UNKNOWN.
official_count берётся из reviews_variant_mode.variant_count, при no_widget —
fallback на product.rating_count (источник фиксируется в отчёте).
Exit code: 1 только при FAIL (--strict: и при WARN); 2 — нечего сканировать.

Использование:
    python scripts/qa_raw.py                            # весь data/
    python scripts/qa_raw.py --since "2026-08-21 14:30"
    python scripts/qa_raw.py --pids 1234567,2345678
    python scripts/qa_raw.py --out docs/qa --strict
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from cli import parse_pids
from ozon_collector.collect import (
    CHALLENGE_TITLE_MARKERS,
    SORT_DEVIATION_RATIO,
    VARIANT_COUNT_FULL_LIMIT,
    VARIANT_COUNT_MIN_LARGE,
    title_challenge_marker,
)

try:
    from ozon_collector.collect import VM_BAD_REASONS
except ImportError:
    VM_BAD_REASONS = {"click_error", "click_no_effect", "timeout", "disabled"}

TOLERANCE_RATIO = 0.03
STARS_MISSING_WARN_SHARE = 0.05
DUP_TEXT_THRESHOLD = 5
OLD_DATA_DAYS = 7
FUTURE_SKEW_TOL_S = 600
TOP_WORST = 5
SHOW_ALL_LIMIT = 50

SEV_RANK = {"FAIL": 0, "WARN": 1, "INFO": 2}
RATING_ONLY_MARKERS = (
    "куплено на ozon",
    "положено в пункт выдачи",
    "оставлен в пункте выдачи",
    "получен в пункте выдачи",
)
GARBAGE_RE = re.compile(
    "\ufffd|(?<![A-Za-zА-Яа-яЁё])(?:undefined|NaN)(?![A-Za-zА-Яа-яЁё])"
)
HTML_ARTIFACT_RE = re.compile(
    r"</?(?:div|span|p|br|b|i|a|td|tr|table|ul|li)\b[^>]*>"
    r"|&(?:nbsp|amp|lt|gt|quot);",
    re.IGNORECASE,
)


def _add(issues, code, severity, message=""):
    issues.append({"code": code, "severity": severity, "message": message})


def _norm_text(t):
    return " ".join((t or "").split()).lower()


def _looks_rating_only(text):
    t = (text or "").strip()
    if not t:
        return False
    lines = [ln.strip() for ln in t.lower().splitlines() if ln.strip()]
    if not any(any(m in ln for m in RATING_ONLY_MARKERS) for ln in lines):
        return False
    return all(
        any(m in ln for m in RATING_ONLY_MARKERS) or len(ln) <= 40 for ln in lines
    )


def _parse_iso(s):
    try:
        return dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _official(raw):
    """Ориентир счётчика: variant_count виджета, fallback rating_count."""
    product = raw.get("product") or {}
    vm = raw.get("reviews_variant_mode") or {}
    vc = vm.get("variant_count")
    rc = product.get("rating_count")
    if isinstance(vc, int) and vc > 0:
        return vc, "variant_count"
    if isinstance(rc, int) and rc > 0:
        return rc, "rating_count_fallback"
    return None, "none"


def _broken_report(pid, message):
    return {
        "pid": pid,
        "verdict": "FAIL",
        "collection_mode": "UNKNOWN",
        "official_count": None,
        "official_count_source": "none",
        "collected": 0,
        "coverage": None,
        "issues": [{"code": "BAD_JSON", "severity": "FAIL", "message": message}],
        "metrics": {},
    }


def qa_product(raw, pid):
    issues = []

    def add(code, severity, message=""):
        _add(issues, code, severity, message)

    if not isinstance(raw, dict):
        return _broken_report(pid, "raw не является словарём")

    product = raw.get("product") or {}
    vm = raw.get("reviews_variant_mode") or {}
    reviews = raw.get("reviews")
    if not isinstance(reviews, list):
        add("BAD_STRUCTURE", "FAIL", f"reviews имеет тип {type(reviews).__name__}")
        reviews = []
    collected = len(reviews)
    skipped = bool(raw.get("reviews_skipped"))

    if str(raw.get("pid") or "") != pid:
        add("BAD_IDENTIFIER", "FAIL",
            f"pid в файле={raw.get('pid')!r}, ожидался {pid}")
    bad_uuid = sum(
        1 for r in reviews
        if not isinstance(r, dict)
        or not isinstance(r.get("uuid"), str)
        or not r.get("uuid", "").strip()
    )
    if bad_uuid:
        add("BAD_IDENTIFIER", "FAIL", f"невалидных uuid: {bad_uuid}/{collected}")

    uuids = [r.get("uuid") for r in reviews if isinstance(r, dict)]
    dup_uuid = len(uuids) - len(set(uuids))
    if dup_uuid:
        add("DUP_UUID", "FAIL", f"дубликатов uuid: {dup_uuid}")

    title = (product.get("title") or "").strip().lower()
    challenge = bool(title) and title_challenge_marker(title) is not None
    if challenge:
        add("CHALLENGE_PAGE", "FAIL", f"title: {title[:60]!r}")

    if skipped:
        # Режим --no-reviews: смысл raw — только карточка. Если цена и рейтинг
        # не извлечены, «успешный» product-only сбор эквивалентен сломанному:
        # REVIEWS_SKIPPED не должен давать безусловный OK (плохая карточка — FAIL).
        price_map = (product.get("price") or {}) if isinstance(
            product.get("price"), dict) else {}
        have_price = bool(price_map.get("current") or price_map.get("bank")
                          or product.get("price_text"))
        have_rating = (product.get("rating") is not None) or bool(
            (product.get("customer_opinion") or {}).get("counts"))
        if not (have_price and have_rating):
            add("INCOMPLETE_CARD", "FAIL",
                "режим --no-reviews: карточка без цены и/или рейтинга")

    official, source = _official(raw)
    if official is None:
        mode = "UNKNOWN"
        add("NO_OFFICIAL_COUNT", "INFO",
            "ни variant_count, ни rating_count недоступны; объёмные проверки пропущены")
    elif official <= VARIANT_COUNT_FULL_LIMIT:
        mode = "FULL"
    else:
        mode = "WALL_LIMITED"

    # Состояние авторизации профиля: делает видимым потолок ~100 отзывов.
    # Только INFO — вердикт не меняется, чтобы не пересобирать уже валидные
    # pid; смысл кода в объяснении, а не в браковке.
    # auth_state пустой = детект не выполнялся (raw до его появления) —
    # это не то же самое, что «измерили и не определили».
    auth = ((raw.get("session") or {}).get("auth") or {})
    auth_logged_in = auth.get("logged_in")
    if mode == "WALL_LIMITED":
        if auth_logged_in is False:
            add("UNAUTH_WALL", "INFO",
                "недобор у сессионного среза: профиль не авторизован (нет куки _ozon_sat)")
        elif not auth:
            add("AUTH_NOT_MEASURED", "INFO",
                "недобор при variant_count > 100; авторизация не измерялась "
                "(raw без session.auth)")
        elif auth_logged_in is None:
            add("AUTH_UNKNOWN", "INFO",
                "недобор при variant_count > 100; состояние авторизации не определено")

    scope_ok = None
    if source == "rating_count_fallback":
        if not skipped:
            # В режиме --no-reviews отзывы не собирались, ориентиром объёма они
            # смысла не имеют → фолбэк счётчика не предупреждаем (карточка цела).
            add("FALLBACK_OFFICIAL_COUNT", "WARN",
                f"variant_count отсутствует (reason={vm.get('reason')!r}), "
                f"ориентир rating_count={official}")
        scope_ok = True
    elif source == "variant_count":
        reason = vm.get("reason")
        scope_ok = bool(vm.get("used")) and reason not in VM_BAD_REASONS
        if not scope_ok:
            add("SCOPE_UNKNOWN", "INFO",
                f"scope варианта не подтверждён (used={vm.get('used')!r}, "
                f"reason={reason!r}); OVERCOUNT/гистограмма пропущены")

    coverage = (collected / official) if official else None

    if official is not None and not skipped:
        if collected > official and scope_ok:
            add("OVERCOUNT", "FAIL",
                f"собрано {collected} > официальных {official} ({source})")
        if mode == "FULL":
            tol = max(1, round(official * TOLERANCE_RATIO))
            if collected < official - tol:
                add("UNDER_TOLERANCE", "WARN",
                    f"собрано {collected} < {official} − допуск {tol}")
        elif mode == "WALL_LIMITED":
            if collected < VARIANT_COUNT_MIN_LARGE:
                add("SHORT_TOTAL", "WARN",
                    f"собрано {collected} < {VARIANT_COUNT_MIN_LARGE} "
                    f"при official={official}")
            add("LOW_COVERAGE", "INFO",
                f"{collected}/{official} ({coverage:.1%}), стена логина")

    if collected == 0:
        confirmed_zero = bool(raw.get("no_reviews_confirmed"))
        if skipped:
            # Режим --no-reviews: отзывы намеренно не собирались (reviews
            # пусты по режиму, а не по факту отсутствия). Не брак и не
            # подтверждение нуля — только INFO. Проверки объёма выше уже
            # пропущены (блок `official is not None and not skipped`).
            add("REVIEWS_SKIPPED", "INFO",
                "отзывы намеренно не собирались (режим --no-reviews)")
        elif confirmed_zero:
            # Экстрактор подтвердил «Отзывы о товаре 0» (reviewsCount/totalScore=0
            # в state-JSON webReviewProductScore) — это валидный нулевик с
            # нормальной карточкой, а не сломанный сбор. Не FAIL.
            add("CONFIRMED_NO_REVIEWS", "INFO",
                "подтверждённый ноль отзывов (no_reviews_confirmed)")
        else:
            add("ZERO_REVIEWS", "FAIL",
                "не собрано ни одного отзыва (пустая вкладка / у выбранного варианта нет своих отзывов)")

    sorts_expected = raw.get("reviews_sorts") or []
    per_sort = Counter(
        (r.get("review_sort") or "?") for r in reviews if isinstance(r, dict)
    )
    # Перекос сортировок значим только при неполном сборе: когда собрано всё
    # (collected >= official), полнота уже доказана объёмом, а пропущенные
    # хвостовые сортировки — легитимный результат skip-оптимизации.
    if len(sorts_expected) > 1 and collected \
            and official is not None and collected < official:
        best = max(per_sort.get(s, 0) for s in sorts_expected)
        if best > 0:
            weak = {
                s: per_sort.get(s, 0)
                for s in sorts_expected
                if per_sort.get(s, 0) < SORT_DEVIATION_RATIO * best
            }
            if weak:
                detail = ", ".join(f"{k}={v}" for k, v in weak.items())
                add("SORT_IMBALANCE", "WARN",
                    f"сортировки ниже {int(SORT_DEVIATION_RATIO * 100)}% "
                    f"лучшей ({best}): {detail}")

    coll_dt = _parse_iso(raw.get("collected_at_utc"))
    coll_ts = coll_dt.timestamp() if coll_dt else None

    stars_missing = rating_only = empty_text = garbage = future = 0
    hist_stars = Counter()
    text_authors = {}
    for r in reviews:
        if not isinstance(r, dict):
            continue
        stars = r.get("stars")
        has_stars = isinstance(stars, int) and stars > 0
        text = r.get("text") or ""
        has_text = bool(text.strip())
        if not has_stars:
            stars_missing += 1
        if has_text:
            if GARBAGE_RE.search(text) or HTML_ARTIFACT_RE.search(text):
                garbage += 1
            norm = _norm_text(text)
            text_authors.setdefault(norm, set()).add(r.get("uuid"))
            if _looks_rating_only(text):
                rating_only += 1
        else:
            if has_stars:
                rating_only += 1
            else:
                empty_text += 1
        if has_stars:
            hist_stars[stars] += 1
        try:
            pa_ts = float(r.get("publishedAt"))
        except (TypeError, ValueError):
            pa_ts = None
        if coll_ts is not None and pa_ts is not None \
                and pa_ts > coll_ts + FUTURE_SKEW_TOL_S:
            future += 1

    if garbage:
        add("GARBAGE_TEXT", "FAIL",
            f"записей с мусором (\\uFFFD/undefined/NaN/HTML): {garbage}")
    if future:
        add("FUTURE_DATE", "FAIL", f"дат из будущего: {future}")
    if empty_text:
        add("EMPTY_TEXT", "WARN",
            f"без текста и без звёзд (extraction_unknown): {empty_text}")
    dup_texts = {
        k: len(v) for k, v in text_authors.items() if len(v) > DUP_TEXT_THRESHOLD
    }
    if dup_texts:
        top = "; ".join(
            f"{k[:40]!r}x{n}"
            for k, n in sorted(dup_texts.items(), key=lambda x: -x[1])[:3]
        )
        add("DUP_TEXT", "WARN",
            f"повторы текста у >{DUP_TEXT_THRESHOLD} авторов: {top}")
    if collected and stars_missing / collected > STARS_MISSING_WARN_SHARE:
        add("STARS_MISSING", "WARN",
            f"{stars_missing}/{collected} ({stars_missing / collected:.0%}) "
            f"записей без звёзд")
    if rating_only:
        add("RATING_ONLY_PATTERN", "INFO",
            f"{rating_only} записей похоже на «оценку без текста»")

    co_counts = ((product.get("customer_opinion") or {}).get("counts")) or {}
    if co_counts and scope_ok:
        over = []
        for k, v in co_counts.items():
            try:
                star = int(str(k).strip().rstrip("*★☆").strip())
            except ValueError:
                continue
            got = hist_stars.get(star, 0)
            if isinstance(v, int) and got > v:
                over.append(f"{star}*: {got}>{v}")
        if over:
            add("STARS_OVER_HISTOGRAM", "FAIL", "; ".join(over))
    else:
        why = "customer_opinion.counts пуст" if not co_counts else "scope не совпадает"
        add("HISTOGRAM_SKIPPED", "INFO", why)

    if coll_dt is not None:
        age_days = (dt.datetime.now(dt.timezone.utc) - coll_dt).total_seconds() / 86400
        if age_days > OLD_DATA_DAYS:
            add("OLD_DATA", "WARN", f"data собрана {age_days:.0f} дн. назад")

    metrics = {
        "collector_version": raw.get("collector_version"),
        "collected_at_utc": raw.get("collected_at_utc"),
        "reviews_total": collected,
        "review_count": collected - rating_only,
        "reviews_skipped": skipped,
        "rating_only_count": rating_only,
        "stars_missing_count": stars_missing,
        "duplicate_uuid_count": dup_uuid,
        "variant_count": vm.get("variant_count") or None,
        "rating_count": product.get("rating_count"),
        "official_count": official,
        "official_count_source": source,
        "coverage": round(coverage, 4) if coverage is not None else None,
        "per_sort_coverage": {s: per_sort.get(s, 0) for s in sorts_expected},
        "vm_reason": vm.get("reason"),
        "challenge_suspect": challenge,
        "auth_logged_in": auth_logged_in,
        "auth_source": auth.get("source"),
    }
    verdict = (
        "FAIL" if any(i["severity"] == "FAIL" for i in issues)
        else "WARN" if any(i["severity"] == "WARN" for i in issues)
        else "OK"
    )
    return {
        "pid": pid,
        "verdict": verdict,
        "collection_mode": mode,
        "official_count": official,
        "official_count_source": source,
        "collected": collected,
        "coverage": metrics["coverage"],
        "issues": issues,
        "metrics": metrics,
    }


def scan_file(path, pid):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        return _broken_report(pid, str(e)[:120])
    return qa_product(raw, pid)


def discover(data_dir, since=None, only_pids=None):
    items = []
    for p in sorted(Path(data_dir).glob("*_raw.json")):
        pid = p.name[: -len("_raw.json")]
        if only_pids and pid not in only_pids:
            continue
        if since is not None:
            mtime = dt.datetime.fromtimestamp(p.stat().st_mtime)
            if mtime < since:
                continue
        items.append((pid, p))
    return items


def parse_since(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise SystemExit(f"qa_raw: не могу разобрать --since {s!r}")


def summarize(reports):
    by_verdict = Counter(r["verdict"] for r in reports)
    sm = sum(r["metrics"].get("stars_missing_count") or 0 for r in reports)
    rt = sum(r["metrics"].get("reviews_total") or 0 for r in reports)
    return {
        "products": len(reports),
        "ok": by_verdict.get("OK", 0),
        "warn": by_verdict.get("WARN", 0),
        "fail": by_verdict.get("FAIL", 0),
        "wall_limited_count": sum(
            1 for r in reports if r["collection_mode"] == "WALL_LIMITED"
        ),
        "stars_missing_total": sm,
        "reviews_total_sum": rt,
        "stars_missing_share": round(sm / rt, 4) if rt else None,
        "rating_only_pattern_total": sum(
            r["metrics"].get("rating_only_count") or 0 for r in reports
        ),
    }


def exit_code_for(summary, strict):
    if summary["fail"]:
        return 1
    return 1 if strict and summary["warn"] else 0


def _codes(report):
    return ", ".join(dict.fromkeys(i["code"] for i in report["issues"])) or "-"


def print_console(reports, summary):
    s = summary
    print(f"[qa] товаров: {s['products']} | OK {s['ok']} | WARN {s['warn']} | "
          f"FAIL {s['fail']} | WALL_LIMITED {s['wall_limited_count']}")
    if s["stars_missing_share"] is not None:
        print(f"[qa] KPI stars_missing: {s['stars_missing_total']}/"
              f"{s['reviews_total_sum']} ({s['stars_missing_share']:.1%}) | "
              f"rating_only-паттерн: {s['rating_only_pattern_total']}")
    rows = reports
    if len(reports) > SHOW_ALL_LIMIT:
        rows = [r for r in reports if r["verdict"] != "OK"]
        print(f"[qa] (показаны только не-OK; всего товаров: {len(reports)})")
    for r in rows:
        off = r["official_count"] if r["official_count"] is not None else "?"
        cov = f"{r['coverage']:.1%}" if r["coverage"] is not None else "-"
        print(f"{r['pid']:<12} {r['verdict']:<5} {r['collection_mode']:<12} "
              f"{r['collected']:>5}/{str(off):<6} {cov:>7}  {_codes(r)}")


def _worst_key(r):
    sev = min((SEV_RANK[i["severity"]] for i in r["issues"]), default=3)
    cov = r["coverage"] if r["coverage"] is not None else 2.0
    return (sev, cov)


def render_md(summary, reports, meta, generated_at):
    s = summary
    L = ["# QA-отчёт raw", ""]
    L.append(f"- Сгенерирован: {generated_at}")
    L.append(f"- Данные: `{meta['data_dir']}`"
             + (f", фильтр с {meta['since']}" if meta["since"] else ""))
    L.append(f"- Строгий режим (--strict): {'да' if meta['strict'] else 'нет'}")
    L.append("")
    L.append("## Summary")
    L.append("")
    L.append("| метрика | значение |")
    L.append("|---|---|")
    L.append(f"| товары | {s['products']} |")
    L.append(f"| OK / WARN / FAIL | {s['ok']} / {s['warn']} / {s['fail']} |")
    L.append(f"| WALL_LIMITED | {s['wall_limited_count']} |")
    if s["stars_missing_share"] is not None:
        L.append(f"| stars_missing | {s['stars_missing_total']}/"
                 f"{s['reviews_total_sum']} ({s['stars_missing_share']:.1%}) |")
    L.append(f"| rating_only-паттерн | {s['rating_only_pattern_total']} |")
    L.append("")
    worst = [w for w in sorted(reports, key=_worst_key) if w["verdict"] != "OK"]
    if worst:
        L.append("## Top проблемных")
        L.append("")
        for w in worst[:TOP_WORST]:
            msgs = "; ".join(
                f"{i['code']}: {i['message']}"
                for i in w["issues"] if i["severity"] in ("FAIL", "WARN")
            )
            L.append(f"- **{w['pid']}** ({w['verdict']}): {msgs or '-'}")
        L.append("")
    L.append("## Все товары")
    L.append("")
    L.append("| pid | verdict | mode | collected/official | coverage | issues |")
    L.append("|---|---|---|---|---|---|")
    for r in reports:
        off = r["official_count"] if r["official_count"] is not None else "?"
        cov = f"{r['coverage']:.1%}" if r["coverage"] is not None else "-"
        L.append(f"| {r['pid']} | {r['verdict']} | {r['collection_mode']} | "
                 f"{r['collected']}/{off} | {cov} | {_codes(r)} |")
    L.append("")
    return "\n".join(L)


def write_reports(out_dir, reports, summary, meta, generated_at):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = generated_at.strftime("%Y%m%d-%H%M%S")
    payload = {
        "generated_at_utc": generated_at.isoformat(),
        "args": meta,
        "summary": summary,
        "products": reports,
    }
    paths = []
    for suffix, latest, body in (
        (".json", "qa_latest.json", json.dumps(payload, ensure_ascii=False, indent=2)),
        (".md", "qa_latest.md", render_md(summary, reports, meta, generated_at)),
    ):
        stamped = out_dir / f"qa_{ts}{suffix}"
        stamped.write_text(body, encoding="utf-8")
        (out_dir / latest).write_text(body, encoding="utf-8")
        paths.append(str(stamped))
    return paths


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="QA-сканер собранных raw-файлов (офлайн)"
    )
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--since", default=None,
                    help='товары, собранные после: "YYYY-MM-DD[ HH:MM]"')
    ap.add_argument("--pids", default=None, help="запятая-разделённые pid")
    ap.add_argument("--out", default=str(Path("docs") / "qa"))
    ap.add_argument("--strict", action="store_true",
                    help="WARN тоже дают exit 1")
    ap.add_argument("--no-report", action="store_true",
                    help="не писать файлы отчёта")
    ap.add_argument("--report-json", default=None,
                    help="путь к JSON-файлу с полным payloadом "
                         "{summary, products} — машинный контракт для "
                         "супервизора (key_daemon); не пишет qa_*.{md,json}")
    args = ap.parse_args(argv)

    since = parse_since(args.since) if args.since else None
    # args.pids — строка с запятыми/пробелами; parse_pids ждёт список токенов
    pid_tokens = (args.pids or "").replace(",", " ").split()
    only_pids = set(parse_pids(pid_tokens)) if pid_tokens else None

    items = discover(args.data_dir, since, only_pids)
    if not items:
        print("[qa] нет файлов для скана", file=sys.stderr)
        return 2
    reports = []
    for i, (pid, path) in enumerate(items, 1):
        reports.append(scan_file(path, pid))
        if i % 25 == 0 or i == len(items):
            print(f"[qa] просканировано {i}/{len(items)}", file=sys.stderr)

    summary = summarize(reports)
    print_console(reports, summary)

    code = exit_code_for(summary, args.strict)
    if args.report_json:
        # машинный контракт: полный payload одним файлом (для супервизора)
        Path(args.report_json).parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "args": {
                "data_dir": args.data_dir, "since": args.since,
                "pids": args.pids, "strict": args.strict,
            },
            "summary": summary,
            "products": reports,
        }
        Path(args.report_json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[qa] JSON-отчёт: {args.report_json}", file=sys.stderr)
    elif not args.no_report:
        generated_at = dt.datetime.now(dt.timezone.utc)
        meta = {
            "data_dir": args.data_dir,
            "since": args.since,
            "pids": args.pids,
            "strict": args.strict,
        }
        paths = write_reports(Path(args.out), reports, summary, meta, generated_at)
        print(f"[qa] отчёт: {paths[0]} (+ qa_latest.*)")
    print(f"[qa] exit={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
