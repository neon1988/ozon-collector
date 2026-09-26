#!/usr/bin/env bash
# Запуск живого Chromium для CDP-сбора Ozon.
# НЕ передавать --enable-automation (иначе navigator.webdriver === true → антибот).
# Требуется реальный X-дисплей (DISPLAY=:0). setsid+disown — чтобы пережить конец bash-команды.
set -e

PORT="${PORT:-9222}"
PROFILE="${PROFILE:-/tmp/opencode/ozon-profile}"

# CHROME: если не задан — берём САМУЮ СВЕЖУЮ сборку ms-playwright (как
# launch_chrome.ps1 ищет самую свежую версию системного Chrome: свежий
# Chromium = лучший anti-automation отпечаток).
# Раньше путь с глобом жил внутри "${CHROME:-...chromium-*/chrome}", и
# ExpandGlob не срабатывал: литерал '*' уезжал в bash -c, где он
# разворачивался уже в НУЖНОМ месте — но при двух установленных ревизиях
# Chromium получал лишние аргументы и просто не стартовал.
if [ -z "${CHROME:-}" ]; then
  CHROME="$(ls -1d "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux64/chrome 2>/dev/null \
            | sort -Vr | head -n 1)"
fi
if [ -z "$CHROME" ] || [ ! -x "$CHROME" ]; then
  echo "Chromium не найден или не исполняем: ${CHROME:-<пусто>}" >&2
  echo "Установи: python3 -m playwright install chromium" >&2
  exit 1
fi

# Маркер запуска коллектором (ADR §14.1, ownership): Session по нему отличает
# наш запуск (browser_owner=collector) от ручного/чужого (external).
mkdir -p "$PROFILE"
date -Is > "$PROFILE/.ozon_collector_launched"

# Если на этом профиле уже запущен Chromium, он получит команду и сам выйдет —
# тогда полагаемся на уже запущенный инстанс (его и проверим ниже).
# --no-sandbox ТОЛЬКО здесь и только по необходимости: на Linux Chromium не
# стартует под root без него (типичный контейнер/WSL), а на Windows песочница
# работает и лишний флаг там — заметный сигнал automation (в .ps1 его нет
# намеренно, см. комментарий к $chromeArgs). Остальные флаги держат
# .sh и .ps1 синхронными: НЕ добавлять --enable-automation.
DISPLAY=${DISPLAY:-:0}
PROXY_ARGS=""
# Per-worker прокси (ревизия 2.9, ADR §18.7): пусто → напрямую. ВНИМАНИЕ:
# аргумент виден в /proc/<pid>/cmdline — не кладите пароль прокси в общий доступ.
if [ -n "${PROXY:-}" ]; then
  PROXY_ARGS="--proxy-server=$PROXY"
fi

# Рекомендация (ADR §10, ревизия 2.9): на сервере без X — Xvfb
# (`Xvfb :99 -screen 0 1920x1080x24` + `DISPLAY=:99 ./launch_chrome.sh`),
# тогда окно не свёрнуто, document.visibilityState === 'visible', а
# композитор полностью рисует кадры (главная причина плоских FLAT_STD-кадров).
DISPLAY="$DISPLAY" setsid bash -c "$CHROME --remote-debugging-port=$PORT \
  --user-data-dir=$PROFILE \
  --no-first-run --no-default-browser-check \
  --disable-features=OptimizationHints,MediaRouter \
  --window-size=1440,900 --no-sandbox $PROXY_ARGS about:blank" & disown

sleep 2
curl -s "http://127.0.0.1:$PORT/json/version" | head -c 200 || echo "ждём... проверьте curl http://127.0.0.1:$PORT/json/version"
