"""Probe: what does --disable-blink-features=AutomationControlled actually do
to the JS-visible fingerprint vs a plain Chrome?

Runs the same probe twice (with and without the flag), prints a diff table.

Зачем: `launch_chrome.ps1` несёт `--disable-blink-features=AutomationControlled`
как «маскировку webdriver». Проверено 2026-09-26 на Chrome 154: флаг НЕ виден
из JS и НЕ нужен — без него `navigator.webdriver === false` и так, потому что
мы подключаемся по CDP к системному Chrome без `--enable-automation`.
Инструмент оставлен, чтобы перепроверять отпечаток при смене Chrome/флагов
и не принимать догадки за факт (см. PROJECT_NOTES P3).

Запуск: `python scripts/probe_fingerprint.py`
ВНИМАНИЕ: поднимает два временных Chrome на портах 9231/9232 с профилями в
`%TEMP%/probe-a|b` и гасит их. Не трогает профиль коллектора и его порты.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROBE_JS = r"""
() => {
  const d = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
  const own = Object.getOwnPropertyDescriptor(navigator, 'webdriver');
  return JSON.stringify({
    webdriver: navigator.webdriver,
    webdriver_type: typeof navigator.webdriver,
    on_proto: 'webdriver' in Navigator.prototype,
    proto_desc: d ? [typeof d.get, d.get ? d.get.toString().slice(0, 60) : null,
                     d.configurable, d.enumerable] : null,
    own_desc: own ? ['has own descriptor'] : null,
    visibility: document.visibilityState,
    hidden: document.hidden,
    has_focus: document.hasFocus(),
    screen: `${screen.width}x${screen.height}x${screen.colorDepth}`,
    avail: `${screen.availWidth}x${screen.availHeight}`,
    lang: navigator.language,
    langs: JSON.stringify(navigator.languages),
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    ua_platform: navigator.userAgentData
      ? navigator.userAgentData.platform : null,
    hw: navigator.hardwareConcurrency,
    perm: typeof Notification !== 'undefined'
      ? Notification.permission : null,
    inner: `${window.innerWidth}x${window.innerHeight}`,
    outer: `${window.outerWidth}x${window.outerHeight}`,
    dpr: window.devicePixelRatio,
    plugins: navigator.plugins.length,
    plugin_names: navigator.plugins.length
      ? Array.from(navigator.plugins).map(p => p.name).join(',') : null,
    has_chrome_obj: typeof window.chrome !== 'undefined',
    has_chrome_runtime: !!(window.chrome && window.chrome.runtime),
    webdriver_own: 'webdriver' in navigator,
  });
}
"""


def run_probe(port: int, profile: str, extra: list, label: str) -> dict:
    shutil.rmtree(profile, ignore_errors=True)
    args = [CHROME,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=ru-RU",
            "--window-size=1440,900"] + extra + ["about:blank"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=2):
                    break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError("chrome died")
        else:
            raise RuntimeError("cdp never came up")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = b.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            raw = page.evaluate(PROBE_JS)
            b.close()
        return json.loads(raw)
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
        shutil.rmtree(profile, ignore_errors=True)


def main():
    tmp = tempfile.gettempdir()
    with_flag = run_probe(
        9231, os.path.join(tmp, "ozon-probe-a"),
        ["--disable-blink-features=AutomationControlled",
         "--disable-infobars",
         "--disable-session-crashed-bubble",
         "--hide-crash-restore-bubble",
         "--disable-features=OptimizationHints,MediaRouter"],
        "with-flag")
    without = run_probe(9232, os.path.join(tmp, "ozon-probe-b"), [], "without")

    keys = list(without)
    print("%-16s %-34s %s" % ("FIELD", "WITH AutomationControlled-off", "plain chrome"))
    print("-" * 100)
    for k in keys:
        a, b = with_flag.get(k), without.get(k)
        mark = "  " if a == b else "**"
        print("%s%-14s %-34s %s" % (mark, k, str(a)[:34], str(b)[:40]))
    print()
    diff = [k for k in keys if with_flag.get(k) != without.get(k)]
    print("DIFF FIELDS:", diff or "NONE")


if __name__ == "__main__":
    main()
