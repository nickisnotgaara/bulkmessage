#!/bin/bash
# =============================================================================
# run.sh - запустить рассылку на сервере ОДНИМ ДВИЖЕНИЕМ
# =============================================================================
# Что делает:
#   1) поднимает вебхук + туннель Cloudflare и подписывает Wazzup
#      (без вебхука фазы отката TG/MAX всегда пустые — это главная причина,
#      по которой рассылка выглядела «100 отправлено → 28 дошло»);
#   2) запускает рассылку как ОДИН процесс на HOURS часов от старта
#      (планировщик/cron НЕ нужен: sender сам перезапускает фазы в цикле).
#
# Использование:
#   bash run.sh                 # запустить: хук + рассылка на 10ч
#   bash run.sh 14              # ... на 14 часов
#   bash run.sh 10 dry          # DRY_RUN (ничего не отправит)
#   bash run.sh stop            # остановить всё
#   bash run.sh status          # что запущено + хвост логов
#   bash run.sh url             # текущий публичный URL вебхука
#   bash run.sh doctor          # preflight-проверки
#
# Тестовый прогон (СНАЧАЛА делай так, потом полный объём):
#   bash run.sh 2  live 20      # 20 сообщений WABA, 2 часа
#   bash run.sh 10 live         # полный объём: до 250 WABA + откаты
#
# Логи:
#   /tmp/webhook.log            вебхук, туннель, подписка Wazzup
#   /tmp/bulk.log               рассылка
# =============================================================================

set -uo pipefail

APP_DIR="${BULK_APP_DIR:-/opt/bulkmessage}"
HOOK_LOG=/tmp/webhook.log
BULK_LOG=/tmp/bulk.log
PY="${PYTHON:-python3}"

cd "$APP_DIR" || { echo "FAIL: нет каталога $APP_DIR"; exit 1; }

MODE="${1:-run}"
HOURS="${2:-10}"
RUN_MODE="${3:-live}"
# 4-й аргумент: ограничить число сообщений WABA в этом запуске.
# Пусто или 0 = без ограничения (берётся дневной лимит 250).
WABA_LIMIT="${4:-}"

SENDER_ARGS=(--run-hours "$HOURS")
if [ -n "$WABA_LIMIT" ] && [ "$WABA_LIMIT" != "0" ]; then
  SENDER_ARGS+=(--waba-limit "$WABA_LIMIT")
  echo "🔬 Тестовый прогон: максимум $WABA_LIMIT сообщений WABA за запуск"
fi

kill_screen() { screen -S "$1" -X quit 2>/dev/null; sleep 1; }

check_blockers() {
  local bad=0
  # 1) DRY_RUN должен быть выключен для боевого запуска
  local dr
  dr=$(grep -E '^BULK_DRY_RUN=' .env.local 2>/dev/null | tail -1 | cut -d= -f2)
  if [ "$RUN_MODE" != "dry" ] && [ "$dr" = "1" ]; then
    echo "❌ В .env.local BULK_DRY_RUN=1 — рассылка ничего не отправит."
    echo "   Поставь BULK_DRY_RUN=0 для боевого запуска, либо: bash run.sh 10 dry"
    bad=1
  fi
  # 2) Вебхук должен быть настроен, иначе откат не работает
  if [ -z "${WAZZUP_WEBHOOK_URL:-}" ] \
     && ! grep -qE '^WAZZUP_WEBHOOK_URL=https://' .env.local 2>/dev/null; then
    echo "⚠️  WAZZUP_WEBHOOK_URL пуст — подписка будет создана автоматически"
    echo "   при старте хука. Проверь после старта: bash run.sh url"
  fi
  return $bad
}

start_hook() {
  kill_screen hook
  rm -f "$HOOK_LOG"
  # --keep-url: URL записывается в .env.local и НЕ откатывается при выходе.
  # На сервере это нужно — sender работает отдельным процессом и должен
  # видеть URL. Локально (Windows) лаунчер по умолчанию откатывает URL,
  # потому что quick-tunnel умирает вместе с процессом.
  screen -dmS hook -L -Logfile "$HOOK_LOG" \
    "$PY" start_webhook.py --keep-url
  echo "⏳ хук поднимается (туннель ~40-90с)..."
  local i=0 url=""
  while [ $i -lt 40 ]; do
    url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$HOOK_LOG" 2>/dev/null | head -1)
    [ -n "$url" ] && break
    sleep 3; i=$((i+1))
  done
  if [ -z "$url" ]; then
    echo "❌ туннель не поднялся за 2 минуты. Лог: $HOOK_LOG"; tail -20 "$HOOK_LOG"; return 1
  fi
  echo "✅ вебхук: $url"
  # Проверка СНАРУЖИ — единственное доказательство, что Wazzup достучится.
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$url/health" || echo 000)
  echo "   /health снаружи: HTTP $code"
  [ "$code" = "200" ] || { echo "❌ health не 200 — Wazzup события не доедут"; return 1; }
  "$PY" start_webhook.py --check 2>&1 | tail -5
  return 0
}

start_sender() {
  kill_screen bulk
  rm -f "$BULK_LOG"
  # --run-hours: один процесс на $HOURS часов. Планировщик не нужен.
  # --waba-limit (если задан 4-м аргументом) режет только фазу WABA,
  # откаты TG/MAX работают в любом случае.
  screen -dmS bulk -L -Logfile "$BULK_LOG" \
    "$PY" main.py sender "${SENDER_ARGS[@]}"
  echo "✅ рассылка запущена на ${HOURS}ч ($RUN_MODE), лог: $BULK_LOG"
}

show_status() {
  echo "=== ЭКРАНЫ ==="
  screen -ls 2>&1 | grep -v 'No Sockets' || echo "(ничего не запущено)"
  echo
  echo "=== ХУК (последние 25) ==="; tail -25 "$HOOK_LOG" 2>/dev/null || echo "(нет лога)"
  echo
  echo "=== РАССЫЛКА (последние 20) ==="; tail -20 "$BULK_LOG" 2>/dev/null || echo "(нет лога)"
}

case "$MODE" in
  run)
    check_blockers || exit 1
    "$PY" main.py preflight 2>&1 | tail -25
    start_hook || exit 1
    start_sender
    echo
    echo "Проверить:  bash run.sh status"
    echo "Остановить: bash run.sh stop"
    ;;
  stop)   kill_screen bulk; kill_screen hook; echo "остановлено" ;;
  status) show_status ;;
  url)    grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$HOOK_LOG" 2>/dev/null | head -1 ;;
  doctor) "$PY" main.py preflight ;;
  *) echo "usage: $0 [run [часы] [dry|live] [лимит_WABA]] | stop | status | url | doctor"
     echo "примеры:"
     echo "  bash run.sh 2 live 20     # ТЕСТ: 20 сообщений WABA на 2 часа"
     echo "  bash run.sh 10 live       # полный объём: до 250 WABA + откаты TG 50 / MAX 15"
     exit 1 ;;
esac