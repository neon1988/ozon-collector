#!/usr/bin/env bash
# Запуск живого Chromium для CDP-сбора Ozon.
# НЕ передавать --enable-automation (иначе navigator.webdriver === true → антибот).
# Требуется реальный X-дисплей (DISPLAY=:0). setsid+disown — чтобы пережить конец bash-команды.
set -e

CHROME="${CHROME:-$HOME/.cache/ms-playwright/chromium-*/chrome-linux64/chrome}"
PORT="${PORT:-9222}"
PROFILE="${PROFILE:-/tmp/opencode/ozon-profile}"

# Маркер запуска коллектором (ADR §14.1, ownership): Session по нему отличает
# наш запуск (browser_owner=collector) от ручного/чужого (external).
mkdir -p "$PROFILE"
date -Is > "$PROFILE/.ozon_collector_launched"

ls $CHROME >/dev/null 2>&1 || {
  echo "Chromium не найден. Установи: python3 -m playwright install chromium" >&2
  exit 1
}

# Если на этом профиле уже запущен Chromium, он получит команду и сам выйдет —
# тогда полагаемся на уже запущенный инстанс (его и проверим ниже).
DISPLAY=:0 setsid bash -c "$CHROME --remote-debugging-port=$PORT \
  --user-data-dir=$PROFILE \
  --no-first-run --no-default-browser-check \
  --disable-features=OptimizationHints,MediaRouter \
  --window-size=1440,900 --no-sandbox about:blank" & disown

sleep 2
curl -s "http://127.0.0.1:$PORT/json/version" | head -c 200 || echo "ждём... проверьте curl http://127.0.0.1:$PORT/json/version"
