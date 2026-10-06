#!/bin/bash
# =============================================================================
# server_start.sh - start/stop bulkmessage services in detached screens
# =============================================================================
# Usage:
#   bash server_start.sh hook     # webhook + tunnel + автоподписка Wazzup
#   bash server_start.sh sender   # broadcast sender
#   bash server_start.sh all      # hook, wait, then sender
#   bash server_start.sh stop     # stop both
#   bash server_start.sh status   # show screens + last log lines
#   bash server_start.sh url      # print clean trycloudflare URL
#   bash server_start.sh doctor   # preflight
# =============================================================================
#
# ВАЖНО, почему hook запускает start_webhook.py, а НЕ run_webhook.py --tunnel:
# run_webhook.py только поднимает сервер и туннель. Он НЕ подписывает Wazzup
# на события и НЕ записывает WAZZUP_WEBHOOK_URL в .env.local. Из-за этого
# sender не знает, что вебхук есть, а события доставки уходят в никуда:
# фазы отката TG/MAX остаются пустыми, и sender ждёт их впустую.
# start_webhook.py делает всё в правильном порядке: сервер → туннель →
# проверка /health СНАРУЖИ → подписка Wazzup → запись URL в .env.local.

APP_DIR=/opt/bulkmessage
HOOK_LOG=/tmp/webhook.log
BULK_LOG=/tmp/bulk.log

cd "$APP_DIR" || { echo "FAIL: no $APP_DIR"; exit 1; }

kill_screen() {
  screen -S "$1" -X quit 2>/dev/null
  sleep 1
}

start_hook() {
  kill_screen hook
  rm -f "$HOOK_LOG"
  # --keep-url: URL записывается в .env.local и НЕ откатывается при выходе.
  # На сервере это нужно — sender запускается отдельным screen'ом и должен
  # видеть URL. Локально (Windows) лаунчер по умолчанию откатывает URL,
  # потому что quick-tunnel умирает вместе с процессом.
  screen -dmS hook -L -Logfile "$HOOK_LOG" python3 start_webhook.py --keep-url
  echo "hook screen started (log: $HOOK_LOG)"
}

start_sender() {
  kill_screen bulk
  rm -f "$BULK_LOG"
  screen -dmS bulk -L -Logfile "$BULK_LOG" python3 main.py sender
  echo "bulk screen started (log: $BULK_LOG)"
}

show_status() {
  echo "=== SCREENS ==="
  screen -ls 2>&1 | grep -v 'No Sockets' || echo "(none)"
  echo
  echo "=== HOOK LOG (last 30) ==="
  tail -30 "$HOOK_LOG" 2>/dev/null || echo "(no log)"
  echo
  echo "=== BULK LOG (last 15) ==="
  tail -15 "$BULK_LOG" 2>/dev/null || echo "(no log)"
}

show_url() {
  grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$HOOK_LOG" 2>/dev/null | head -1
}

case "$1" in
  hook)   start_hook ;;
  sender) start_sender ;;
  all)    start_hook; sleep 20; start_sender ;;
  stop)   kill_screen bulk; kill_screen hook; echo "stopped" ;;
  status) show_status ;;
  url)    show_url ;;
  doctor) python3 main.py preflight ;;
  *) echo "usage: $0 {hook|sender|all|stop|status|url|doctor}"; exit 1 ;;
esac