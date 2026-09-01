# CDP-хелпер: показать/позиционировать окно браузера коллектора.
# Chrome прячет своё окно за экран; SetWindowPos не помогает (Chrome возвращает
# позицию). Но через CDP Browser domain сам Chrome корректно ставит своё окно.
# Запуск: python scripts/cdp_show_window.py [port]
import json
import sys
import time
import urllib.request

import websocket


def ws_send(ws, method, params=None, timeout=10):
    cmd = {"id": 1, "method": method, "params": params or {}}
    ws.settimeout(timeout)
    ws.send(json.dumps(cmd))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == 1:
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return msg.get("result", {})


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "9222"
    base = f"http://127.0.0.1:{port}"
    version = json.load(urllib.request.urlopen(base + "/json/version", timeout=5))
    ws_url = version["webSocketDebuggerUrl"]
    ws = websocket.create_connection(ws_url, timeout=15,
                                     origin="")
    result = ws_send(ws, "Browser.getVersion")
    wins = ws_send(ws, "Browser.getWindowForTarget")
    window_id = wins.get("windowId")
    print(f"browser={result.get('product')} windowId={window_id}")
    bounds = ws_send(ws, "Browser.getWindowBounds", {"windowId": window_id})
    print(f"bounds now: {bounds.get('bounds')}")
    newb = {
        "left": 160, "top": 80, "width": 1520, "height": 900,
        "windowState": "normal",
    }
    ws_send(ws, "Browser.setWindowBounds", {"windowId": window_id, "bounds": newb})
    time.sleep(0.3)
    bounds = ws_send(ws, "Browser.getWindowBounds", {"windowId": window_id})
    print(f"bounds after: {bounds.get('bounds')}")
    ws.close()


if __name__ == "__main__":
    main()