"""Probe: does ozon.ru answer HEAD with 403/405 while GET is fine?

That is what `_network_block_status` (search.py) uses to detect an origin
deny, and the mismatch is the reason a challenge used to be logged as
«сетевой бан IP».

Замер 2026-09-26 на живом ozon.ru (обычный Chrome, свежий профиль):
    HEAD /  -> 405 Method Not Allowed
    GET  /  -> 200, ~475 KB, title «Похоже, нет соединения» (то есть challenge
               отдаётся со 200, а не 403)

Вывод: 403/405 на HEAD — ответ WAF на метод, а не verdict о репутации адреса.
`_network_block_status` поэтому подтверждает статус отказа GET'ом.

Дальше (2026-10-02, живой лог демона) выяснилось и второе: даже согласованные
403/429/503 не доказывают бан адреса. В 16:03 тот же адрес отдал NORMAL_DONE
(discovered 336, valid 10), в 16:13-17:24 все 8 воркеров получили 403 подряд
при том, что Ozon из другого браузера открывался нормально. Значит 403 — это
отказ сессии, а не адреса, и смена сессии (new_tab/recycle) его снимает.
Поэтому отказ origin больше НЕ является основанием пропустить лестницу.

Запуск: `python scripts/probe_head_status.py`
ВНИМАНИЕ: поднимает временный Chrome на порту 9233 с профилем в
`%TEMP%/ozon-probe-head` и гасит его; профиль коллектора не трогает.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9233

JS = r"""
async () => {
  const out = {};
  const url = location.origin + '/';
  const modes = [
    ['head_nostore', {method: 'HEAD', cache: 'no-store', credentials: 'include'}],
    ['head_plain',   {method: 'HEAD', credentials: 'include'}],
    ['get_nostore',  {method: 'GET', cache: 'no-store', credentials: 'include'}],
  ];
  for (const [name, init] of modes) {
    try {
      const r = await fetch(url, init);
      out[name] = {status: r.status, type: r.type};
    } catch (e) {
      out[name] = {error: String(e)};
    }
  }
  // GET главной с открытым счётчиком, чтобы отличить WAF-отказ от 200
  try {
    const r = await fetch(url, {cache: 'no-store', credentials: 'include'});
    const t = await r.text();
    out.get_body = {status: r.status, len: t.length,
                    head: t.slice(0, 60).replace(/\s+/g, ' ')};
  } catch (e) {
    out.get_body = {error: String(e)};
  }
  return JSON.stringify(out);
}
"""


def main():
    profile = os.path.join(tempfile.gettempdir(), "ozon-probe-head")
    shutil.rmtree(profile, ignore_errors=True)
    proc = subprocess.Popen(
        [CHROME, f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", "--lang=ru-RU",
         "--window-size=1440,900", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{PORT}/json/version", timeout=2):
                    break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError("chrome died")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
            page = b.contexts[0].pages[0] if b.contexts[0].pages \
                else b.contexts[0].new_page()
            page.goto("https://www.ozon.ru/", wait_until="domcontentloaded",
                      timeout=45000)
            time.sleep(6)
            data = None
            for attempt in range(6):
                try:
                    data = json.loads(page.evaluate(JS))
                    break
                except Exception as e:
                    # Ozon перезагружает страницу (anti-bot redirect) —
                    # Execution context destroyed. Ждём и пробуем снова.
                    print("  retry %d: %s" % (
                        attempt, type(e).__name__))
                    time.sleep(4)
            if data is None:
                raise RuntimeError("probe never got a stable context")
            title = page.title()
            b.close()
        print("page title:", title.encode("ascii", "replace").decode())
        for k, v in data.items():
            print("  %-12s %s" % (k, json.dumps(v, ensure_ascii=False)))
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    main()
