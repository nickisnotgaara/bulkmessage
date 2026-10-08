#!/usr/bin/env bash
# =============================================================================
# deploy.sh — раскладка кода на боевой сервер + рестарт вебхука + ДОКАЗАТЕЛЬСТВО,
# что процесс поднялся именно на новой версии кода.
# =============================================================================
# Зачем: 2026-10-06 вебхук стартовал в 12:53 UTC, фикс разбора конверта
# {"statuses": [...]} приехал в 13:47 — 54 минуты события статусов ПРИХОДИЛИ
# и логировались, а подтверждений доставки не появилось ни одного. Python
# импортирует модули при старте, поэтому rsync без рестарта = тихий старый код.
# Этот скрипт рестартует сервис и сверяет code_version в /health с локальным
# git sha. Не сошлось за 30 секунд — скрипт падает с ненулевым кодом.
#
# БЕЗОПАСНОСТЬ:
#   * секретов в файле нет и быть не должно — пароль/ключ НЕ хардкодим.
#     SSH идёт по ключу или через ssh-agent; если ключа нет, ssh/rsync спросят
#     пароль сами. Файл НЕ должен попадать в git с ключами рядом.
#   * chmod +x deploy.sh   (или запускать как: bash deploy.sh)
#
# Использование:
#   bash deploy.sh --dry-run    # печатает, что СДЕЛАЛ бы, ничего не выполняя
#   bash deploy.sh              # раскладка + рестарт + проверка версии
#
# Юнит ставится ОДИН РАЗ руками (deploy.sh его не трогает):
#   ssh root@$HOST 'install -m 644 /dev/stdin /etc/systemd/system/bulkmessage-webhook.service' \
#     < deploy/bulkmessage-webhook.service
#   ssh root@$HOST 'systemctl daemon-reload && systemctl enable bulkmessage-webhook'
# -m 644 обязателен: с Windows/WSL файл приезжает с битами 0777, и systemd
# ругается на world-writable юнит.
# =============================================================================

set -euo pipefail

HOST=217.149.23.102
USER=root
REMOTE=/opt/bulkmessage
SERVICE=bulkmessage-webhook
HEALTH_URL="http://127.0.0.1:8010/health"
HEALTH_TIMEOUT=30

# .git не едет намеренно: deploy.sh сам кладёт sha в $REMOTE/.code_version,
# и /health читает его, когда .git/HEAD недоступен.
# tools/ — ТОЖЕ обязательный исключитель: локально там cloudflared.exe (Windows),
# а на сервере нужен cloudflared (Linux). С --delete Linux-бинар удалился бы,
# и вебхук после рестарта не поднял бы туннель.
EXCLUDES=(
  --exclude '.git'
  --exclude 'data/'
  --exclude '.env*'
  --exclude '*.log'
  --exclude '__pycache__'
  --exclude '.venv'
  --exclude 'tools/'
)

DRY_RUN=0
usage() {
  sed -n '2,/^# =\{20,\}$/p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage ;;
    *) echo "Неизвестный аргумент: $arg" >&2; usage ;;
  esac
done

run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# ── 0. Локальная версия кода: без неё проверка не имеет смысла ──────────
if ! LOCAL_SHA="$(git rev-parse --short HEAD 2>/dev/null)"; then
  echo "❌ deploy.sh запущен не из git-репозитория." >&2
  echo "   Без 'git rev-parse --short HEAD' нельзя доказать, что на сервере" >&2
  echo "   свежий код — ровно та проверка, ради которой скрипт написан." >&2
  exit 1
fi
TARGET="${USER}@${HOST}"

echo "→ deploy: $TARGET:$REMOTE, сервис $SERVICE, локальный код $LOCAL_SHA"

command -v rsync >/dev/null 2>&1 || {
  echo "❌ rsync не найден (нужен для раскладки без удаления data/ и .env*)." >&2
  echo "   Debian/Ubuntu: apt-get install -y rsync" >&2
  exit 1
}

# ── 1. Раскладка кода ─────────────────────────────────────────────────
echo "→ 1/4 rsync (без .git, data/, .env*, *.log, __pycache__, .venv, tools/)"
# Исключённые файлы rsync при --delete НЕ трогает (нужен был бы
# --delete-excluded), поэтому .env*, data/ и .git на сервере остаются.
# --no-perms: не тащить биты с Windows (там всё 0777) — на сервере права
# сохраняются прежние, новые файлы берут umask.
run rsync -az --delete --no-perms \
  "${EXCLUDES[@]}" \
  ./ "${TARGET}:${REMOTE}/"

# ── 2. Метка версии для /health ───────────────────────────────────────
# .git не едет (п. выше), поэтому сервер иначе не смог бы определить
# code_version и всегда отвечал бы "unknown". Метка кладётся ПОСЛЕ rsync:
# --delete удалил бы файл, которого нет в исходном дереве.
echo "→ 2/4 метка версии ${REMOTE}/.code_version = ${LOCAL_SHA}"
run ssh "$TARGET" "printf '%s\\n' '${LOCAL_SHA}' > ${REMOTE}/.code_version"

# ── 3. Рестарт ────────────────────────────────────────────────────────
echo "→ 3/4 systemctl restart ${SERVICE}"
if [ "$DRY_RUN" = "0" ]; then
  cat >&2 <<WARN

⚠️  ВНИМАНИЕ: рестарт переподписывает Wazzup на НОВЫЙ quick-tunnel URL.
    ~10 секунд вебхук будет недоступен, события этого окна Wazzup не дошлёт.
    Статусы доставки в это окно не потеряются окончательно (sender
    переспрашивает), но confirmations могут прийти позже.

WARN
fi
run ssh "$TARGET" "systemctl restart ${SERVICE}"

# ── 4. Ждём, пока процесс ответит НОВЫМ code_version ──────────────────
echo "→ 4/4 опрос ${HEALTH_URL} до ${HEALTH_TIMEOUT}s (ждём code_version=${LOCAL_SHA})"
if [ "$DRY_RUN" = "1" ]; then
  echo "[dry-run] опросил бы $HEALTH_URL на сервере и сравнил code_version с $LOCAL_SHA"
  echo "[dry-run] НИЧЕГО не выполнено."
  exit 0
fi

set +e
ssh "$TARGET" bash -s -- "$LOCAL_SHA" "$HEALTH_URL" "$HEALTH_TIMEOUT" <<'REMOTE_POLL'
set -u
want="$1"; url="$2"; limit="$3"
deadline=$(( $(date +%s) + limit ))
last=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  body="$(curl -s --max-time 3 "$url" 2>/dev/null)"
  if [ -n "$body" ]; then
    ver="$(printf '%s' "$body" | sed -n 's/.*"code_version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    if [ -n "$ver" ]; then
      # Сервер отдаёт полный sha из .git/HEAD (40 симв.), локально --short (7).
      case "$ver" in
        "$want"|"$want"*)
          echo "OK: code_version=$ver совпадает с локальным $want"
          exit 0
          ;;
      esac
      last="$ver"
    fi
  fi
  sleep 2
done
echo "MISMATCH: последний code_version=${last:-нет ответа}, ждали ${want}"
exit 1
REMOTE_POLL
rc=$?
set -e

if [ "$rc" -ne 0 ]; then
  echo "" >&2
  echo "❌ ВЕБХУК НЕ ПОДТВЕРДИЛ НОВУЮ ВЕРСИЮ КОДА за ${HEALTH_TIMEOUT}s." >&2
  echo "   Вебхук живёт на устаревшем коде: события приходят, статусы" >&2
  echo "   доставки разбираются старым парсером и теряются." >&2
  echo "   Смотри: ssh $TARGET 'systemctl status ${SERVICE}'" >&2
  echo "            ssh $TARGET 'tail -50 /tmp/webhook_new.log'" >&2
  echo "            ssh $TARGET 'curl -s $HEALTH_URL'" >&2
  exit 1
fi

echo "✅ Деплой завершён: вебхук отвечает свежим кодом $LOCAL_SHA"