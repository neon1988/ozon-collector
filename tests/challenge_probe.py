#!/usr/bin/env python3
"""Проба механизма выхода из access_challenge (FAB) на ЖИВОЙ странице Ozon.

Эксперимент (вне pytest, live-бюджет): парсер получает FAB на карточке, а
ручное открытие в той же сессии проходит. Скрипт проверяет гипотезы
разблокировки ПООЧЕРЁДНО на одном pid и останавливается после первого
успеха (+ контрольный повтор через CONTROL_WAIT_S для воспроизводимости 2/2).

Порядок серии (адаптивный, STOP после первого PASS):
  0 — открыть карточку (gate: если нет FAB — «нечего разблокировать», стоп)
  1 — FAB → пауза 10с → повтор в ТОЙ ЖЕ вкладке
  4 — FAB → открыть карточку в НОВОЙ вкладке
  2 — FAB → главная ozon.ru → карточка
  3 — FAB → reload
  5 — (только по явному запросу) FAB → пауза N → повтор в той же вкладке

Ограничения:
  * cookies снимаются ТОЛЬКО по именам (count + дельта набора), значения НЕ
    читаются и не сохраняются;
  * автостоп после MAX_CONSECUTIVE_CHALLENGES access_challenge подряд
    (рекомендация cooldown);
  * по умолчанию работает на ИЗОЛИРОВАННОЙ копии профиля (порт 9334),
    production ozon-profile/9222 не трогается;
  * запускать только на товаре, где СЕЙЧАС воспроизводится challenge.

Использование:
  python tests/challenge_probe.py --pid 1214739369
  python tests/challenge_probe.py --pid 1214739369 --scenario 1
  python tests/challenge_probe.py --pid 1214739369 --scenario 5 --pause 30
  python tests/challenge_probe.py --pid 1214739369 --use-main-profile --yes
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from ozon_collector import cdp
from ozon_collector import collect as C

DEFAULT_PORT = 9334
DEFAULT_PID = "1214739369"
GOTO_WAIT_MS = 7000
REPEAT_DELAY_S = 10.0
HOME_URL = "https://www.ozon.ru/"
MAX_CONSECUTIVE_CHALLENGES = 3
CONTROL_WAIT_S = 60.0
SERIES_ORDER = [0, 1, 4, 2, 3]

MAIN_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "opencode", "ozon-profile")
PROBE_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                             "opencode", "ozon-profile-probe")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cookie_names(sess) -> list:
    """Имена cookies сессии (ТОЛЬКО имена, без значений)."""
    try:
        ctx = sess.page.context
        return sorted(c["name"] for c in ctx.cookies())
    except Exception:
        return []


def _delta(before, after):
    """Добавленные/удалённые имена cookies между двумя снимками."""
    b, a = set(before), set(after)
    return {"added": sorted(a - b), "removed": sorted(b - a)}


def _step(sess, url: str, label: str, page=None) -> dict:
    """Один переход: открыть url, детект challenge, снимок кук (имена)."""
    page = page or sess.page
    before = _cookie_names(sess)
    t0 = time.monotonic()
    try:
        if page is sess.page:
            sess.goto(url, wait_ms=GOTO_WAIT_MS)
        else:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(GOTO_WAIT_MS)
        sig = C.detect_challenge(page)
        real = False
        if sig is None:
            try:
                real = "/product/" in (page.url or "")
            except Exception:
                real = False
    except Exception as e:
        sig = f"error:{type(e).__name__}:{e}"
        real = False
    after = _cookie_names(sess)
    dt = _delta(before, after)
    return {
        "step": label,
        "url": url,
        "sig": sig,
        "pass": bool(sig is None and real),
        "challenge": bool(sig is not None and not str(sig).startswith("error:")),
        "cookies_before": len(before),
        "cookies_after": len(after),
        "cookies_added": dt["added"],
        "cookies_removed": dt["removed"],
        "elapsed_s": round(time.monotonic() - t0, 2),
    }


def scenario(sess, n: int, url: str, pause: float = 30.0) -> tuple:
    """Выполнить сценарий n. Возвращает (steps, passed)."""
    steps = []
    if n == 0:
        steps.append(_step(sess, url, "0-open"))
    elif n == 1:
        steps.append(_step(sess, url, "1a-open"))
        time.sleep(REPEAT_DELAY_S)
        steps.append(_step(sess, url, "1b-repeat-same-tab"))
    elif n == 4:
        steps.append(_step(sess, url, "4a-open"))
        ctx = sess.page.context
        old = sess.page
        new = ctx.new_page()
        sess.page = new
        try:
            steps.append(_step(sess, url, "4b-new-tab"))
        finally:
            sess.page = old
            try:
                new.close()
            except Exception:
                pass
    elif n == 2:
        steps.append(_step(sess, url, "2a-open"))
        steps.append(_step(sess, HOME_URL, "2b-home"))
        time.sleep(3.0)
        steps.append(_step(sess, url, "2c-card-after-home"))
    elif n == 3:
        steps.append(_step(sess, url, "3a-open"))
        try:
            sess.page.reload()
            sess.page.wait_for_timeout(5000)
        except Exception:
            pass
        steps.append(_step(sess, url, "3b-after-reload"))
    elif n == 5:
        steps.append(_step(sess, url, "5a-open"))
        time.sleep(pause)
        steps.append(_step(sess, url, "5b-repeat-after-pause"))
    else:
        raise ValueError(f"нет сценария {n}")
    return steps, _passed(steps)


def _passed(steps) -> bool:
    """Вердикт сценария по ФИНАЛЬНОМУ шагу (для сценария 2 промежуточная
    «главная» не является карточкой и не должна фейлить сценарий)."""
    return bool(steps) and steps[-1]["pass"]


def run_series(sess, url: str, order, pause: float) -> tuple:
    """Автосерия со STOP-on-success и автостопом по N challenge подряд.

    Возвращает (results, first_pass_scenario, aborted_reason)."""
    results = []
    first_pass = None
    aborted = None
    consecutive = 0
    for n in order:
        if consecutive >= MAX_CONSECUTIVE_CHALLENGES:
            aborted = (f"{consecutive} access_challenge подряд — "
                       f"рекомендую cooldown, стоп")
            break
        steps, passed = scenario(sess, n, url, pause=pause)
        results.append({"scenario": n, "steps": steps, "passed": passed})
        for s in steps:
            consecutive = consecutive + 1 if s.get("challenge") else 0
        if passed:
            first_pass = n
            break
    return results, first_pass, aborted


def control_repeat(sess, url: str, n: int, pause: float) -> dict:
    """Контрольный повтор сценария после паузы (воспроизводимость 2/2)."""
    time.sleep(CONTROL_WAIT_S)
    steps, passed = scenario(sess, n, url, pause=pause)
    return {"scenario": n, "steps": steps, "passed": passed}


def _build_report(config, started_at, steps, verdict,
                  first_pass, control, duration_s, aborted=None) -> dict:
    return {
        "config": config,
        "started_at_utc": started_at,
        "duration_s": round(duration_s, 2),
        "steps": steps,
        "verdict": verdict,
        "first_pass_scenario": first_pass,
        "control": control,
        "aborted": aborted,
    }


def _render_lines(report: dict) -> list:
    """Читаемый лог отчёта (чистая функция, тестируется)."""
    L = []
    L.append("=== challenge_probe report ===")
    cfg = report["config"]
    L.append(f"pid={cfg['pid']} port={cfg['port']} "
             f"profile={cfg['profile']} url={cfg['url']}")
    L.append(f"started_at_utc={report['started_at_utc']} "
             f"duration_s={report['duration_s']}")
    if report.get("aborted"):
        L.append(f"ABORTED: {report['aborted']}")
    for ev in report["steps"]:
        if "aborted" in ev:
            L.append(f"ABORTED: {ev['aborted']}")
            continue
        L.append(f"--- scenario {ev['scenario']} "
                 f"({'PASS' if ev['passed'] else 'FAIL'}) ---")
        for s in ev["steps"]:
            if s["pass"]:
                mark = "PASS"
            elif s.get("challenge"):
                mark = "CHALLENGE"
            else:
                mark = "ERR"
            added = f" +{s['cookies_added']}" if s["cookies_added"] else ""
            removed = f" -{s['cookies_removed']}" if s["cookies_removed"] else ""
            L.append(
                f"  [{s['elapsed_s']}s] {s['step']}: {mark} sig={s['sig']!r} "
                f"cookies {s['cookies_before']}->{s['cookies_after']}"
                f"{added}{removed}")
    ctrl = report.get("control")
    ctrl_txt = "None"
    if ctrl:
        ctrl_txt = f"scenario {ctrl['scenario']} {'PASS' if ctrl['passed'] else 'FAIL'}"
    L.append(f"verdict={report['verdict']} "
             f"first_pass_scenario={report['first_pass_scenario']} "
             f"control={ctrl_txt}")
    return L


def _json_dump(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def _copy_profile(dst: str, src: str, force: bool = False) -> None:
    """Копирует профиль src → dst (если dst уже есть — пропуск; при force —
    предварительно удаляет dst). Заблокированные файлы (открытый основной
    браузер, напр. Network\\Cookies) пропускаются."""
    if force and os.path.isdir(dst):
        import shutil
        try:
            shutil.rmtree(dst, ignore_errors=True)
        except Exception as e:
            print(f"[probe] rmtree warn: {e}")
    if os.path.isdir(dst):
        print(f"[probe] профиль уже есть: {dst}")
        return
    if not src or not os.path.isdir(src):
        print(f"[probe] источник профиля не найден: {src!r} — "
              f"запускаю с чистым {dst}")
        os.makedirs(dst, exist_ok=True)
        return
    print(f"[probe] копирую профиль {src} → {dst}")
    if os.name == "nt":
        cmd = ["robocopy", src, dst, "/E", "/R:0", "/W:0",
               "/NFL", "/NDL", "/NJH", "/NJS"]
        try:
            subprocess.run(cmd, timeout=120, check=False,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return
        except Exception as e:
            print(f"[probe] robocopy warn: {e}")
    import shutil
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
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if cdp.browser_alive(cdp_url):
            return
        time.sleep(0.5)
    raise SystemExit(f"браузер не поднялся на {cdp_url} "
                     f"(профиль={profile})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="challenge_probe",
        description="Проба выхода из access_challenge Ozon (live-эксперимент)")
    p.add_argument("--pid", default=DEFAULT_PID,
                   help=f"артикул-мишень (по умолч. {DEFAULT_PID})")
    p.add_argument("--url", default=None,
                   help="полный URL карточки (по умолчанию /product/{pid})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"CDP-порт пробы (по умолч. {DEFAULT_PORT})")
    p.add_argument("--profile", default=None,
                   help="путь профиля-копии (по умолч. ozon-profile-probe)")
    p.add_argument("--use-main-profile", action="store_true",
                   help="использовать ОСНОВНОЙ профиль ozon-profile/порт 9222 "
                        "(риск: production-куки)")
    p.add_argument("--no-copy", action="store_true",
                   help="не создавать профиль-копию автоматически")
    p.add_argument("--force-copy", action="store_true",
                   help="пересоздать профиль-копию (удалить и скопировать заново)")
    p.add_argument("--scenario", type=int, default=None,
                   help="запустить ТОЛЬКО сценарий N (без автосерии/контроля)")
    p.add_argument("--pause", type=float, default=30.0,
                   help="пауза сценария 5, сек (по умолч. 30)")
    p.add_argument("--no-control", action="store_true",
                   help="не делать контрольный повтор после успеха")
    p.add_argument("--out", default="challenge_probe_report.json",
                   help="путь JSON-отчёта (по умолч. challenge_probe_report.json)")
    p.add_argument("--yes", action="store_true",
                   help="не спрашивать подтверждения (live-трафик!)")
    return p


def main() -> int:
    args = build_parser().parse_args()

    pid = str(args.pid)
    if not pid.isdigit():
        print(f"Error: --pid должен быть числовым артикулом, got {pid!r}",
              file=sys.stderr)
        return 2
    url = args.url or f"https://www.ozon.ru/product/{pid}"

    if args.use_main_profile:
        port, profile = 9222, MAIN_PROFILE
    else:
        port = args.port
        profile = args.profile or PROBE_PROFILE
        if not args.no_copy:
            _copy_profile(profile, MAIN_PROFILE, force=args.force_copy)

    if not args.yes:
        print("ВНИМАНИЕ: live-эксперимент, тратит живой бюджет Ozon "
              f"(pid={pid}, {url}, порт {port}).")
        try:
            input("Продолжить? [Enter] ")
        except KeyboardInterrupt:
            print("\nотменено")
            return 0

    _ensure_browser(port, profile)

    config = {"pid": pid, "url": url, "port": port, "profile": profile}
    started_at = _now()
    t0 = time.monotonic()
    sess = cdp.Session(f"http://127.0.0.1:{port}", profile_token="probe")
    try:
        if args.scenario is not None:
            steps, passed = scenario(sess, args.scenario, url,
                                     pause=args.pause)
            report = _build_report(
                config, started_at,
                [{"scenario": args.scenario, "steps": steps,
                  "passed": passed}],
                verdict="pass" if passed else "fail",
                first_pass=args.scenario if passed else None,
                control=None, duration_s=time.monotonic() - t0)
        else:
            results, first_pass, aborted = run_series(
                sess, url, SERIES_ORDER, pause=args.pause)
            control = None
            if first_pass is not None and not args.no_control:
                control = control_repeat(sess, url, first_pass,
                                         pause=args.pause)
                control = {"scenario": first_pass, "passed": control["passed"]}
            verdict = "no_challenge"
            if results and results[0]["passed"]:
                verdict = "no_challenge"   # gate: FAB не воспроизводится
            elif first_pass is not None:
                verdict = "pass"
            elif aborted:
                verdict = "aborted"
            else:
                verdict = "fail"
            report = _build_report(
                config, started_at, results, verdict, first_pass, control,
                time.monotonic() - t0, aborted=aborted)
    finally:
        try:
            sess.close()
        except Exception:
            pass

    report["finished_at_utc"] = _now()
    print("\n".join(_render_lines(report)))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(_json_dump(report))
    print(f"\nJSON-отчёт сохранён: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
