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

# Маркер запуска коллектором пишется ПОСЛЕ старта (ниже): запись до запуска
# оставляла «наш» маркер после отказа, и ручной Chrome на том же профиле
# ошибочно считался collector (review 2026-10-20). Если профиль уже кем-то
# занят, новый Chrome делегирует и умрёт — маркер не пишем вовсе: чужой
# main нельзя объявлять нашим (ADR §14.1).
mkdir -p "$PROFILE"
PROFILE_HELD=0
# Exact argv match: a substring match also hits ozon-profile-worker-N and
# suppresses the main marker while any worker is alive. grep -F avoids regex
# characters in the profile path.
if command -v pgrep >/dev/null 2>&1; then
  for holder in $(pgrep -f -- "--user-data-dir=" 2>/dev/null || true); do
    if tr '\0' '\n' < "/proc/$holder/cmdline" 2>/dev/null \
       | grep -Fqx -- "--user-data-dir=${PROFILE%/}"; then
      PROFILE_HELD=1
      break
    fi
  done
fi

# Если на этом профиле уже запущен Chromium, он получит команду и сам выйдет —
# тогда полагаемся на уже запущенный инстанс (его и проверим ниже).
# --no-sandbox ТОЛЬКО здесь и только по необходимости: на Linux Chromium не
# стартует под root без него (типичный контейнер/WSL), а на Windows песочница
# работает и лишний флаг там — заметный сигнал automation (в .ps1 его нет
# намеренно, см. комментарий к $chromeArgs). Остальные флаги держат
# .sh и .ps1 синхронными: НЕ добавлять --enable-automation.
# --deny-permission-prompts: Chrome рисует permission-бабл («… запрашивает
# разрешение на:» ОТДЕЛЬНЫМ ВИДИМЫМ окном даже при скрытом главном (живой
# замер 2026-10-16) — глушим: запросы авто-отклоняются, UI не создаётся.
DISPLAY=${DISPLAY:-:0}
# Потолок дискового кеша Chromium (МБ, флаг принимает килобайты). Живой замер
# 2026-10-07: профиль раздувался до 880 МБ, 92% — воспроизводимый кеш, а не
# состояние сессии. Флаг не JS-наблюдаемый, отпечаток страницы не меняет.
DISK_CACHE_MB="${OZON_DISK_CACHE_MB:-100}"
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
  --user-data-dir=\"$PROFILE\" \
  --no-first-run --no-default-browser-check \
  --disable-features=OptimizationHints,MediaRouter \
  --deny-permission-prompts \
  --disk-cache-size=$((DISK_CACHE_MB * 1024)) \
  --window-size=1440,900 --no-sandbox $PROXY_ARGS about:blank" &
CHROME_PID=$!
disown "$CHROME_PID" 2>/dev/null || true

sleep 2
curl -s --max-time 2 "http://127.0.0.1:$PORT/json/version" | head -c 200 || echo "ждём... проверьте curl http://127.0.0.1:$PORT/json/version"

# Use the PID we launched, not “some process holding the profile”: pgrep may
# return the user's Chrome when a new instance delegates to it and exits.
# setsid usually execs in place; verify /proc/<pid>/exe before trusting $!.
if [ "$PROFILE_HELD" -eq 0 ]; then
  CHROME_REAL=$(readlink -f "$CHROME" 2>/dev/null || printf '%s' "$CHROME")
  LAUNCH_REAL=$(readlink -f "/proc/$CHROME_PID/exe" 2>/dev/null || true)
  # Wrapper launchers (snap/chromium shims) may exec a different binary. The
  # argv check still proves which profile $! was started for; a dead/delegated
  # pid is rejected later by the marker start-time check.
  ARGV_HAS_PROFILE=0
  if tr '\0' '\n' < "/proc/$CHROME_PID/cmdline" 2>/dev/null \
     | grep -Fqx -- "--user-data-dir=${PROFILE%/}"; then
    ARGV_HAS_PROFILE=1
  fi
  if { [ -n "$LAUNCH_REAL" ] && [ "$LAUNCH_REAL" = "$CHROME_REAL" ]; } \
     || [ "$ARGV_HAS_PROFILE" -eq 1 ]; then
    printf '{"pid": %s, "started_at": "%s"}\n' \
      "$CHROME_PID" "$(date -Is)" > "$PROFILE/.ozon_collector_launched"
  else
    echo "LAUNCHER-WARN: ownership marker not written (pid=$CHROME_PID," \
         "exe mismatch) - browser may be treated as external" >&2
  fi
fi
