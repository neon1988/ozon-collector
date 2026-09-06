#!/usr/bin/env python3
"""Live-проба структуры страницы выдачи Ozon: откуда берутся ссылки товаров.

Вопрос: key_daemon (SEARCH_JS) собирает pid ТОЛЬКО из div[data-widget="tileGridDesktop"].
Живая выдача по text= внутри бренд-категории при этом даёт мусор (рюкзаки/перчатки/конфеты).
Зонд открывает URL выдачи на ИЗОЛИРОВАННОМ профиле (порт 9337) и снимает карту:

  каждая ссылка /product/ → ближайший data-widget предок (widget → pids);

потом прокручивает выдачу (как _collect_links) и снова снимает карту. Цель — понять:
мусорные товары лежат ВНУТРИ tileGridDesktop (персонализация в главной сетке) или
в отдельных рекомендательных виджетах («Вы смотрели», «Возможно, вам понравится»),
которые в выборку SEARCH_JS попасть не должны.

Запуск:
  python tests/probe_search_grid.py --url "https://www.ozon.ru/category/edifier-26667829/?text=Edifier+W830NB"
  python tests/probe_search_grid.py --url <...> --profile <path> --out report.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from ozon_collector import cdp

DEFAULT_PORT = 9337
GOTO_WAIT_MS = 12000
SCROLL_BY = 2500
SCROLL_ROUNDS = 8
WAIT_MS_AFTER_SCROLL = 1200

MAIN_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "opencode", "ozon-profile")
PROBE_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                             "opencode", "ozon-profile-search-grid-probe")

# Карта ссылок: для каждой ссылки /product/ берём ближайшего data-widget предка.
MAP_LINKS_JS = """
() => {
  const out = { by_widget: {}, widget_headings: {}, total_links: 0, total_pids: 0 };
  const heading = (el) => {
    if (!el) return '';
    const h = el.querySelector('h1,h2,h3,h4,span[class*="tsHeadline"]');
    return h ? (h.innerText || '').trim().slice(0, 60) : '';
  };
  const pidof = (href) => {
    const m = (href || '').match(/(?:-|\\/)(\\d+)(?:\\/|\\?|$)/);
    return m ? m[1] : null;
  };
  const seen = new Set();
  document.querySelectorAll('a[href*="/product/"]').forEach((a) => {
    const href = a.getAttribute('href') || '';
    let pid = a.pathname ? pidof(a.pathname) : null;
    if (!pid) pid = pidof(href);
    if (!pid || pid.length < 5 || seen.has(pid)) return;
    seen.add(pid);
    let w = a.closest('[data-widget]');
    const wname = w ? w.getAttribute('data-widget') : '__no_widget__';
    if (!out.by_widget[wname]) {
      out.by_widget[wname] = { count: 0, pids: [] };
      out.widget_headings[wname] = heading(w);
    }
    const bucket = out.by_widget[wname];
    bucket.count += 1;
    if (bucket.pids.length < 8) bucket.pids.push(pid);
  });
  out.total_pids = seen.size;
  const keys = Object.keys(out.by_widget);
  out.total_links = keys.reduce((s, k) => s + out.by_widget[k].count, 0);
  out.widgets = keys.sort();
  out.reco_widgets = keys.filter((k) => {
    const h = (out.widget_headings[k] || '').toLowerCase();
    return h.includes('смотрели') || h.includes('возможно') || h.includes('понравится')
        || h.includes('похожие') || h.includes('рекомендуем') || h.includes('персональн');
  });
  return out;
}
"""

# Список виджетов с их заголовками (чтобы найти «Вы смотрели»/«Возможно, вам понравится»).
WIDGETS_WITH_HEADINGS_JS = """
() => {
  const out = [];
  const seenIds = new Set();
  document.querySelectorAll('[data-widget]').forEach((el) => {
    const id = el.getAttribute('data-widget') || '';
    if (seenIds.has(id)) return;
    seenIds.add(id);
    out.push({ widget: id, count: document.querySelectorAll(`[data-widget="${id}"]`).length });
  });
  return out;
}
"""


def _copy_profile(dst: str, src: str, force: bool = False) -> None:
    import shutil
    import subprocess
    if force and os.path.isdir(dst):
        try:
            shutil.rmtree(dst, ignore_errors=True)
        except Exception as e:
            print(f"[probe] rmtree warn: {e}")
    if os.path.isdir(dst):
        print(f"[probe] профиль уже есть: {dst}")
        return
    if not src or not os.path.isdir(src):
        print(f"[probe] источник профиля не найден: {src!r} — чистый {dst}")
        os.makedirs(dst, exist_ok=True)
        return
    print(f"[probe] копирую профиль {src} → {dst}")
    if os.name == "nt":
        cmd = ["robocopy", src, dst, "/E", "/R:0", "/W:0",
               "/NFL", "/NDL", "/NJH", "/NJS"]
        try:
            subprocess.run(cmd, timeout=120, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception as e:
            print(f"[probe] robocopy warn: {e}")
    try:
        shutil.copytree(src, dst,
                        ignore=shutil.ignore_patterns("Singleton*", "*.lock"))
    except Exception as e:
        print(f"[probe] copytree warn: {e}")
        os.makedirs(dst, exist_ok=True)


def _ensure_browser(port: int, profile: str) -> None:
    cdp_url = f"http://127.0.0.1:{port}"
    if cdp.browser_alive(cdp_url):
        print(f"[probe] браузер уже жив на {cdp_url}")
        return
    print(f"[probe] запускаю Chromium на {cdp_url} профиль={profile}")
    cdp._launch_browser(cdp_url, profile_path=profile)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if cdp.browser_alive(cdp_url):
            return
        time.sleep(0.5)
    raise SystemExit(f"браузер не поднялся на {cdp_url}")


def _safe_title(page) -> str:
    try:
        return page.title() or ""
    except Exception:
        return ""


def _probe(sess, url: str) -> dict:
    out = {"url": url, "rows": []}
    t0 = time.monotonic()
    try:
        sess.goto(url, wait_ms=GOTO_WAIT_MS)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["elapsed_s"] = round(time.monotonic() - t0, 2)
        return out
    out["title"] = _safe_title(sess.page)
    try:
        from ozon_collector import collect as C
        C.check_challenge(sess.page, "probe_search_grid")
    except Exception:
        pass

    def _snap(tag: str, scroll_pos: int) -> dict:
        snap = {"tag": tag, "scroll_pos": scroll_pos}
        for name, js in (("map", MAP_LINKS_JS),
                         ("widgets", WIDGETS_WITH_HEADINGS_JS)):
            try:
                snap[name] = sess.page.evaluate(js)
            except Exception as e:
                snap[f"{name}_error"] = f"{type(e).__name__}: {e}"
        return snap

    out["rows"].append(_snap("initial", 0))
    for i in range(1, SCROLL_ROUNDS + 1):
        try:
            sess.page.evaluate(
                f"() => window.scrollBy(0, {SCROLL_BY})")
            sess.page.wait_for_timeout(WAIT_MS_AFTER_SCROLL)
        except Exception as e:
            print(f"[probe] scroll warn round {i}: {e}")
            break
        out["rows"].append(_snap(f"scroll{i}", SCROLL_BY * i))

    # сводка: сколько pids итого и как они распределяются по виджетам на последнем кадре
    last = out["rows"][-1].get("map") or {}
    out["summary"] = {
        "final_total_pids": last.get("total_pids"),
        "final_by_widget": (last.get("by_widget") or {}),
        "reco_widgets": last.get("reco_widgets"),
        "rows_snapshotted": len(out["rows"]),
    }
    out["elapsed_s"] = round(time.monotonic() - t0, 2)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="probe_search_grid",
        description="Live-проба структуры выдачи Ozon: tileGridDesktop vs рекомендации")
    p.add_argument("--url", required=True, help="URL страницы выдачи (search/category)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"CDP-порт пробы (по умолч. {DEFAULT_PORT})")
    p.add_argument("--profile", default=None,
                   help="путь профиля-копии (по умолч. изолированный)")
    p.add_argument("--no-copy", action="store_true",
                   help="не создавать профиль-копию")
    p.add_argument("--force-copy", action="store_true",
                   help="пересоздать профиль-копию")
    p.add_argument("--out", default="probe_search_grid_report.json",
                   help="путь JSON-отчёта")
    p.add_argument("--yes", action="store_true",
                   help="не спрашивать подтверждения (live-трафик!)")
    return p


def main() -> int:
    args = build_parser().parse_args()
    port = args.port
    profile = args.profile or PROBE_PROFILE
    if not args.no_copy:
        _copy_profile(profile, MAIN_PROFILE, force=args.force_copy)
    if not args.yes:
        print("ВНИМАНИЕ: live-эксперимент, тратит живой бюджет Ozon "
              f"(url={args.url}, порт {port}).")
        try:
            input("Продолжить? [Enter] ")
        except KeyboardInterrupt:
            print("\nотменено")
            return 0
    _ensure_browser(port, profile)
    sess = cdp.Session(f"http://127.0.0.1:{port}",
                       profile_token="ozon-profile-search-grid-probe")
    try:
        report = _probe(sess, args.url)
    finally:
        try:
            sess.close()
        except Exception:
            pass
    report["port"] = port
    report["profile"] = profile
    print(json.dumps(report, ensure_ascii=False, indent=2))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nJSON-отчёт сохранён: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())