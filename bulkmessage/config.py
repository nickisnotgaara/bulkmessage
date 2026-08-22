"""Configuration loaded from environment variables."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path


def _select_env_file() -> Path | None:
    """Выбирает .env файл по приоритету:
    1. BULK_ENV_FILE (если задан)
    2. INSIDE_DOCKER=1 или /.dockerenv → .env.docker
    3. иначе → .env.local
    """
    root = Path(__file__).resolve().parent.parent
    explicit = os.environ.get("BULK_ENV_FILE")
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = root / p
        return p if p.exists() else None

    in_docker = os.environ.get("INSIDE_DOCKER") == "1" or Path("/.dockerenv").exists()
    if in_docker:
        p = root / ".env.docker"
        if p.exists():
            return p

    p = root / ".env.local"
    return p if p.exists() else None


def _parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            result[key] = val
    return result


def _load_dotenv() -> None:
    env_path = _select_env_file()
    if env_path is None:
        return
    parsed = _parse_env_file(env_path)
    for key, val in parsed.items():
        if key not in os.environ:
            os.environ[key] = val


_load_dotenv()


def _load_tokens() -> dict:
    raw = os.environ.get("WAPPI_TOKENS_JSON")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {
        "whatsapp": {
            "token": os.environ.get("WAPPI_WHATSAPP_TOKEN", ""),
            "profile_id": os.environ.get("WAPPI_WHATSAPP_PROFILE", ""),
        },
        "telegram": {
            "token": os.environ.get("WAPPI_TELEGRAM_TOKEN", ""),
            "profile_id": os.environ.get("WAPPI_TELEGRAM_PROFILE", ""),
        },
        "max": {
            "token": os.environ.get("WAPPI_MAX_TOKEN", ""),
            "profile_id": os.environ.get("WAPPI_MAX_PROFILE", ""),
        },
    }


# --- Files / paths ---
DATA_DIR = Path(os.environ.get("BULK_DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

EXCEL_PATH = os.environ.get("BULK_EXCEL_PATH", str(DATA_DIR / "Contact.xlsx"))
LOG_PATH = os.environ.get("BULK_LOG_PATH", str(DATA_DIR / "broadcast_log.csv"))
STATE_PATH = os.environ.get("BULK_STATE_PATH", str(DATA_DIR / "broadcast_state.json"))
DB_PATH = os.environ.get("BULK_DB_PATH", str(DATA_DIR / "crm.db"))
PENDING_WEBHOOKS_PATH = os.environ.get(
    "BULK_PENDING_WEBHOOKS", str(DATA_DIR / "pending_webhooks.jsonl")
)
TEMPLATES_PATH = os.environ.get("BULK_TEMPLATES_PATH", "Message_script.md")

# --- Daily limits: 60 на КАЖДЫЙ канал (whatsapp, telegram, max) ---
# 60 контактов × 3 канала = 180 сообщений в день максимум.
CHANNEL_DAILY_LIMITS = {
    "whatsapp": int(os.environ.get("BULK_LIMIT_WHATSAPP", "60")),
    "telegram": int(os.environ.get("BULK_LIMIT_TELEGRAM", "60")),
    "max": int(os.environ.get("BULK_LIMIT_MAX", "60")),
}

# --- Wappi ---
WAPPI_BASE_URL = os.environ.get("WAPPI_BASE_URL", "https://wappi.pro")
WAPPI_TOKENS: dict = _load_tokens()
WAPPI_SEND_PATHS = {
    "whatsapp": "/api/sync/message/send",
    "telegram": "/tapi/sync/message/send",
    "max": "/maxapi/sync/message/send",
}
WAPPI_MESSAGE_GET_PATHS = {
    "whatsapp": "/api/sync/messages/id/get",
    "telegram": "/tapi/sync/messages/id/get",
    "max": "/maxapi/sync/messages/id/get",
}

# --- Google Sheets ---
GOOGLE_SHEET_ID = os.environ.get("BULK_GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get(
    "BULK_GOOGLE_CREDENTIALS_JSON", str(DATA_DIR / "credentials.json")
)
GOOGLE_WORKSHEET_NAME = os.environ.get("BULK_GOOGLE_WORKSHEET", "Messages")
GSHEET_HEADERS = [
    "Phone",
    "Name",
    "Category",
    "Channel",
    "Message",
    "Sent At",
    "Delivered",
    "Read",
    "Answered",
    "Reply",
    "Last Activity",
    "Status",
    "Message ID",
]

# --- Webhook ---
WEBHOOK_HOST = os.environ.get("BULK_WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.environ.get("BULK_WEBHOOK_PORT", "8000"))
WEBHOOK_PATH = os.environ.get("BULK_WEBHOOK_PATH", "/webhook")

# --- Reconciliation ---
RECONCILE_INTERVAL = int(os.environ.get("BULK_RECONCILE_INTERVAL", "60"))
STATUS_POLL_AGE_SEC = int(os.environ.get("BULK_STATUS_POLL_AGE", "30"))
STATUS_POLL_LIMIT = int(os.environ.get("BULK_STATUS_POLL_LIMIT", "100"))
SHEETS_RETRY_LIMIT = int(os.environ.get("BULK_SHEETS_RETRY_LIMIT", "5"))

# --- Anti-block ---
BATCH_SIZE = int(os.environ.get("BULK_BATCH_SIZE", "10"))
BATCH_BREAK_MIN = int(os.environ.get("BULK_BATCH_BREAK_MIN", "0"))
BATCH_BREAK_MAX = int(os.environ.get("BULK_BATCH_BREAK_MAX", "0"))
DELAY_MIN = int(os.environ.get("BULK_DELAY_MIN", "8"))
DELAY_MAX = int(os.environ.get("BULK_DELAY_MAX", "18"))

# --- Backoff (для wire-up _next_backoff в sender.py) ---
# При RATE_LIMIT (429 / flood wait) — экспоненциальный backoff от BASE до MAX.
# При TRANSIENT (5xx, timeout) — случайный в [MIN, MAX].
# При AUTH (401, 403) — сразу MAX (токен протух, нужно чинить вручную).
RATE_LIMIT_BACKOFF_BASE = int(os.environ.get("BULK_RATE_BACKOFF_BASE", "300"))
RATE_LIMIT_BACKOFF_MAX = int(os.environ.get("BULK_RATE_BACKOFF_MAX", "3600"))
TRANSIENT_BACKOFF_MIN = int(os.environ.get("BULK_TRANSIENT_BACKOFF_MIN", "30"))
TRANSIENT_BACKOFF_MAX = int(os.environ.get("BULK_TRANSIENT_BACKOFF_MAX", "120"))
TRANSIENT_RETRY = int(os.environ.get("BULK_TRANSIENT_RETRY", "1"))

# --- Dry-run mode ---
# Если = "1" / "true" — sender НЕ вызывает Wappi API, только имитирует успешные
# отправки. Полезно для прогона расписания, проверки квот и таймингов без риска
# отправить что-то реальное. ВАЖНО: при боевом запуске должен быть 0 / пусто.
DRY_RUN = os.environ.get("BULK_DRY_RUN", "0").strip().lower() in ("1", "true", "yes", "on")

REPLY_SEPARATOR = "\n----------------\n"

try:
    from zoneinfo import ZoneInfo
    _DEFAULT_TZ = ZoneInfo(os.environ.get("BULK_TIMEZONE", "Europe/Moscow"))
except Exception:
    _DEFAULT_TZ = None
TIMEZONE_NAME = os.environ.get("BULK_TIMEZONE", "Europe/Moscow")


def now_tz() -> datetime:
    """Текущее время в настроенной TZ (по умолчанию Europe/Moscow).

    Использовать ВМЕСТО datetime.now() — иначе дневные квоты будут
    считаться по локальной TZ машины, а не по серверной.
    """
    if _DEFAULT_TZ is not None:
        return datetime.now(_DEFAULT_TZ)
    return datetime.now()


# --- Active hours (окно отправки в TIMEZONE_NAME) ---
# 0 если окно не задано (работаем 24/7). Иначе отправляем только в [start, end).
ACTIVE_HOURS_START = int(os.environ.get("BULK_ACTIVE_HOURS_START", "0"))
ACTIVE_HOURS_END = int(os.environ.get("BULK_ACTIVE_HOURS_END", "0"))


def is_within_active_hours(dt: datetime | None = None) -> bool:
    """True если dt (или сейчас) попадает в [ACTIVE_HOURS_START, ACTIVE_HOURS_END).

    Если ACTIVE_HOURS_START=0 и ACTIVE_HOURS_END=0 — окно не задано, всегда True.

    dt должен быть timezone-aware (иначе сравнение часа бессмысленно).
    Наивный datetime вызовет ValueError — лучше явно, чем silent footgun.
    """
    if ACTIVE_HOURS_START == 0 and ACTIVE_HOURS_END == 0:
        return True
    if dt is None:
        dt = now_tz()
    if dt.tzinfo is None:
        raise ValueError(
            "is_within_active_hours() требует timezone-aware datetime, "
            f"получен naive: {dt}. Используйте config.now_tz() или datetime.now(tz=...)."
        )
    h = dt.hour
    if ACTIVE_HOURS_START < ACTIVE_HOURS_END:
        # обычное окно в одних сутках, напр. 10..20
        return ACTIVE_HOURS_START <= h < ACTIVE_HOURS_END
    # окно через полночь, напр. 22..6 (если когда-то понадобится)
    return h >= ACTIVE_HOURS_START or h < ACTIVE_HOURS_END


def seconds_until_active_window(dt: datetime | None = None) -> float:
    """Сколько секунд ждать до начала активного окна.

    Если мы УЖЕ в окне — возвращает 0. Если окно не задано — 0.
    """
    if ACTIVE_HOURS_START == 0 and ACTIVE_HOURS_END == 0:
        return 0.0
    if dt is None:
        dt = now_tz()
    if is_within_active_hours(dt):
        return 0.0
    # Считаем секунды до начала окна
    from datetime import timedelta
    target = dt.replace(hour=ACTIVE_HOURS_START, minute=0, second=0, microsecond=0)
    if target <= dt:
        target += timedelta(days=1)
    return (target - dt).total_seconds()

# --- Templates ---
TEMPLATE_MAP = {
    "Покупатели": "Покупатель",
    "Продавцы": "Продавец",
    "Агенты": "Риэлтор",
    "Инвесторы": "Инвестор",
}
CATEGORY_ALIASES = {
    "покупатель": "Покупатели",
    "покупатели": "Покупатели",
    "продавец": "Продавцы",
    "продавцы": "Продавцы",
    "агент": "Агенты",
    "агенты": "Агенты",
    "риэлтор": "Агенты",
    "риелтор": "Агенты",
    "риэлторы": "Агенты",
    "инвестор": "Инвесторы",
    "инвесторы": "Инвесторы",
    "предприниматель": "Инвесторы",
    "предприниматели": "Инвесторы",
    "инвестор/риэлтор": "Инвесторы",
    "инвестор/риелтор": "Инвесторы",
    "риэлтор/инвестор": "Инвесторы",
    "риелтор/инвестор": "Инвесторы",
    "агент/инвестор": "Инвесторы",
}

ALLOWED_CATEGORIES = {"Покупатели", "Продавцы", "Агенты", "Инвесторы"}

VALID_STATUSES = {
    "queued",
    "sent",
    "delivered",
    "read",
    "failed",
    "answered",
    "undelivered",
}


def configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if any(isinstance(h, logging.StreamHandler)
           and not isinstance(h, logging.FileHandler)
           for h in root.handlers):
        if any(isinstance(h, logging.FileHandler)
               and getattr(h, 'baseFilename', '').endswith('broadcast.log')
               for h in root.handlers):
            return
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-12s | %(message)s"
    )
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        fh = _log_file_handler()
        if fh:
            root.addHandler(fh)
    except Exception as e:
        sys.stderr.write(f"log_file_handler error: {e}\n")


def get_logger(name: str = "bulkmessage"):
    configure_logging()
    return logging.getLogger(name)


def _log_file_handler() -> logging.Handler | None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = str(DATA_DIR / "broadcast.log")
        Path(path).touch(exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8", mode="a")
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(name)-12s | %(message)s"
            )
        )
        fh.setLevel(logging.INFO)
        return fh
    except Exception as e:
        sys.stderr.write(f"log_file_handler error: {e}\n")
        return None


# --- Lock file path (для защиты от двойного запуска sender.py) ---
LOCK_PATH = os.environ.get(
    "BULK_LOCK_PATH", str(DATA_DIR / "sender.lock")
)

# --- Telegram-бот для уведомлений админу ---
# Если токен и admin_id заданы — ошибки и ключевые события идут в личку.
# Если нет — модуль tglog тихо no-op.
TG_LOG_BOT_TOKEN = os.environ.get("BULK_TG_LOG_BOT_TOKEN", "").strip()
TG_LOG_ADMIN_ID = os.environ.get("BULK_TG_LOG_ADMIN_ID", "").strip()
TG_LOG_ENABLED = bool(TG_LOG_BOT_TOKEN) and bool(TG_LOG_ADMIN_ID)
# Мин. уровень логов, которые идут в TG: ERROR по умолчанию (ERROR, CRITICAL).
# WARNING если хочется шумнее.
TG_LOG_LEVEL = os.environ.get("BULK_TG_LOG_LEVEL", "ERROR").strip().upper()
# Мин. секунд между сообщениями (Telegram rate limit ≈ 30/sec, 1 per chat/sec
# рекомендуется; 2 сек = безопасно).
TG_LOG_SEND_INTERVAL = float(os.environ.get("BULK_TG_LOG_SEND_INTERVAL", "2.0"))
# Макс. размер очереди (если переполнена — последние сообщения дропаются).
TG_LOG_MAX_QUEUE = int(os.environ.get("BULK_TG_LOG_MAX_QUEUE", "1000"))


def preflight_check(test_phone: str = "") -> dict:
    """Быстрая проверка перед боевым запуском: токены валидны, Wappi отвечает.

    Возвращает dict:
      {
        "ok": bool,        # все активные каналы ответили 2xx
        "channels": {      # per-channel результат
          "whatsapp": {"ok": bool, "detail": str, "http": int|None},
          "telegram": {"ok": bool, "detail": str, "http": int|None},
          "max":      {"ok": bool, "detail": str, "http": int|None},
        },
        "errors": [str],   # список ошибок
      }

    Если test_phone пустой — проверяем только /status endpoint (HEAD/GET без отправки).
    """
    # Импортируем здесь, чтобы не было циклической зависимости
    from . import wappi

    result: dict = {"ok": True, "channels": {}, "errors": []}
    active = wappi.active_channels()

    if not active:
        result["ok"] = False
        result["errors"].append("Нет активных каналов (проверьте WAPPI_*_TOKEN)")
        return result

    for ch in active:
        # Простой GET на /status endpoint, чтобы убедиться что токен валиден
        creds = WAPPI_TOKENS.get(ch, {})
        token = creds.get("token", "")
        profile_id = creds.get("profile_id", "")
        if not token or not profile_id:
            result["channels"][ch] = {
                "ok": False, "detail": "empty token or profile_id", "http": None
            }
            result["ok"] = False
            result["errors"].append(f"{ch}: пустой токен или profile_id")
            continue
        # Пробуем GET /messages/id/get с фейковым message_id — если токен битый,
        # вернёт 401/403. Если ок — 404 (message not found) или 200.
        try:
            import requests
            url = WAPPI_BASE_URL + WAPPI_MESSAGE_GET_PATHS[ch]
            resp = requests.get(
                url,
                headers={"Authorization": token},
                params={"profile_id": profile_id, "message_id": "preflight_check"},
                timeout=10,
            )
            http = resp.status_code
            if http in (401, 403):
                result["channels"][ch] = {
                    "ok": False, "detail": f"auth fail: HTTP {http}", "http": http
                }
                result["ok"] = False
                result["errors"].append(f"{ch}: токен невалиден (HTTP {http})")
            else:
                # 200 / 404 / 400 — все ОК (токен принят, message_id не существует)
                result["channels"][ch] = {
                    "ok": True, "detail": f"HTTP {http}", "http": http
                }
        except requests.RequestException as e:
            result["channels"][ch] = {
                "ok": False, "detail": f"network: {e}", "http": None
            }
            result["ok"] = False
            result["errors"].append(f"{ch}: сеть недоступна — {e}")

    return result
