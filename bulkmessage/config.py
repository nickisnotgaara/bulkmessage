"""Configuration loaded from environment variables."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path


def _select_env_file() -> Path | None:
    """DEPRECATED: возвращает ПЕРВЫЙ найденный .env* файл (для обратной совместимости).

    Новый код использует _load_dotenv() который грузит ВСЕ .env* файлы
    в порядке приоритета (от низкого к высокому):
      .env             → базовые секреты/настройки (низший приоритет)
      .env.local       → локальные overrides (средний приоритет)
      .env.docker      → docker-specific (высший приоритет, грузится только в Docker)

    Эта функция оставлена для совместимости (возвращает первый существующий).
    """
    root = Path(__file__).resolve().parent.parent
    explicit = os.environ.get("BULK_ENV_FILE")
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = root / p
        return p if p.exists() else None

    in_docker = os.environ.get("INSIDE_DOCKER") == "1" or Path("/.dockerenv").exists()
    if in_docker and (root / ".env.docker").exists():
        return root / ".env.docker"

    if (root / ".env.local").exists():
        return root / ".env.local"
    if (root / ".env").exists():
        return root / ".env"
    return None


def _candidate_env_files() -> list[Path]:
    """Возвращает список .env* файлов в порядке приоритета (от низкого к высокому).

    Каждый следующий файл override'ит предыдущие (как в 12-factor app convention).
    Docker файл — только если INSIDE_DOCKER.
    """
    root = Path(__file__).resolve().parent.parent
    explicit = os.environ.get("BULK_ENV_FILE")
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = root / p
        return [p] if p.exists() else []

    in_docker = os.environ.get("INSIDE_DOCKER") == "1" or Path("/.dockerenv").exists()
    candidates: list[Path] = []
    if (root / ".env").exists():
        candidates.append(root / ".env")
    if (root / ".env.local").exists():
        candidates.append(root / ".env.local")
    if in_docker and (root / ".env.docker").exists():
        candidates.append(root / ".env.docker")
    return candidates


def _parse_env_file(path: Path) -> dict[str, str]:
    """Парсит .env файл с поддержкой UTF-8 и Windows-1251 fallback.

    Некоторые .env.local файлы сохранены в Windows-1251 (русские комментарии
    с длинным тире и т.п.), поэтому UTF-8 даст UnicodeDecodeError.
    Пробуем UTF-8 сначала, потом cp1251.
    """
    raw: str | None = None
    for enc in ("utf-8", "cp1251", "utf-8-sig"):
        try:
            raw = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        # Последний fallback — с errors='replace', чтоб не крашить импорт
        raw = path.read_text(encoding="utf-8", errors="replace")
    result: dict[str, str] = {}
    for line in raw.splitlines():
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
    """Грузит ВСЕ .env* файлы в порядке приоритета (от низкого к высокому).

    Convention:
      .env          → базовые секреты (например, WAZZUP_API_KEY)
      .env.local    → локальные overrides (если есть)
      .env.docker   → только в Docker

    Каждый следующий файл override'ит предыдущие. Если переменная
    уже в os.environ (например, задана через export в shell),
    она НЕ перезаписывается — это позволяет override'ить
    секреты через переменные окружения без правки файлов.
    """
    candidates = _candidate_env_files()
    for env_path in candidates:
        parsed = _parse_env_file(env_path)
        for key, val in parsed.items():
            if key not in os.environ:
                os.environ[key] = val


_load_dotenv()


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

# WABA tier limits per Meta (Phase 1): 250 / 1000 / 10000 / 100000 / unlimited
WABA_TIER_LIMITS = {
    0: 250,
    1: 1000,
    2: 10000,
    3: 100000,
    4: 1000000,  # unlimited — cap для health
}

# ЖЁСТКИЙ код-лимит для Personal-каналов. Никакой env override не может
# превысить это значение (защита от случайного скачка cap для original account).
MAX_PERSONAL_DAILY_HARD_CAP = 30

ORIGINAL_ACCOUNT_DAILY_HARD_CAPS = {
    "waba": 250,
    "telegram": 40,
    "max": 25,
}

# --- Daily limits: 60 на КАЖДЫЙ канал (whatsapp, telegram, max) ---
# 60 контактов × 3 канала = 180 сообщений в день максимум.
# Phase 0.2 (оригинальный аккаунт): TG=40, MAX=25 (понижены для anti-bank).
# WABA — отдельный ключ (BULK_LIMIT_WABA) или авто из tier.
# Personal-каналы ограничены жёстким MAX_PERSONAL_DAILY_HARD_CAP=30.
def _safe_limit(ch: str, default: int, hard_cap: int = 10**9) -> int:
    val = int(os.environ.get(f"BULK_LIMIT_{ch.upper()}", str(default)))
    return min(val, hard_cap)

CHANNEL_DAILY_LIMITS = {
    "whatsapp": _safe_limit("whatsapp", 60),
    "telegram": _safe_limit("telegram", 40, hard_cap=MAX_PERSONAL_DAILY_HARD_CAP),
    "max": _safe_limit("max", 25, hard_cap=MAX_PERSONAL_DAILY_HARD_CAP),
    "waba": _safe_limit("waba", 250),
}

# --- Wazzup24 (единственный транспорт) ---
WAZZUP_BASE_URL = os.environ.get("WAZZUP_BASE_URL", "https://api.wazzup24.com")
WAZZUP_API_KEY = os.environ.get("WAZZUP_API_KEY", "").strip()
WAZZUP_DEFAULT_CHANNEL_ID = os.environ.get("WAZZUP_DEFAULT_CHANNEL_ID", "").strip()
# Per-channel Wazzup channel IDs — ОБЯЗАТЕЛЬНЫ для всех мессенджеров.
WAZZUP_TG_CHANNEL_ID = os.environ.get("WAZZUP_TG_CHANNEL_ID", "").strip()
WAZZUP_MAX_CHANNEL_ID = os.environ.get("WAZZUP_MAX_CHANNEL_ID", "").strip()
WAZZUP_WHATSAPP_CHANNEL_ID = os.environ.get("WAZZUP_WHATSAPP_CHANNEL_ID", "").strip()
WAZZUP_WEBHOOK_URL = os.environ.get("WAZZUP_WEBHOOK_URL", "").strip()
WAZZUP_HMAC_SECRET = os.environ.get("WAZZUP_HMAC_SECRET", "").strip()
# Если tier неизвестен — дефолт 0 (250/day, безопаснее)
WAZZUP_TIER_DEFAULT = int(os.environ.get("WAZZUP_TIER_DEFAULT", "0"))
WAZZUP_VERIFY_SIGNATURE = os.environ.get(
    "WAZZUP_VERIFY_SIGNATURE", "1"
).strip().lower() in ("1", "true", "yes", "on")


def get_wazzup_channel_id_for(channel: str) -> str:
    """Возвращает UUID канала Wazzup для данного bulkmessage-канала.

    Канал bulkmessage ('waba'/'telegram'/'max'/'whatsapp') → UUID Wazzup.
    Возвращает пустую строку если не задан (тогда sender пропускает канал).
    """
    mapping = {
        "waba": WAZZUP_DEFAULT_CHANNEL_ID,
        "whatsapp": WAZZUP_WHATSAPP_CHANNEL_ID,
        "telegram": WAZZUP_TG_CHANNEL_ID,
        "max": WAZZUP_MAX_CHANNEL_ID,
    }
    return mapping.get(channel, "") or ""

# WABA templates per category (Phase 1.2)
WABA_TEMPLATE_IDS = {
    "Покупатели": os.environ.get("WABA_TEMPLATE_BUYER_ID", "").strip(),
    "Продавцы": os.environ.get("WABA_TEMPLATE_SELLER_ID", "").strip(),
    "Агенты": os.environ.get("WABA_TEMPLATE_AGENT_ID", "").strip(),
    "Инвесторы": os.environ.get("WABA_TEMPLATE_INVESTOR_ID", "").strip(),
}

# Sub endpoints (Phase 1.1)
WAZZUP_MESSAGE_SEND_PATH = "/v3/message"
WAZZUP_CHANNELS_PATH = "/v3/channels"
WAZZUP_WEBHOOKS_PATH = "/v3/webhooks"

# Feature flag: 0 = WABA off (legacy), 1 = WABA on (cascade), 2 = shadow (compute decision only)
USE_WABA = os.environ.get("BULK_USE_WABA", "0").strip()
try:
    USE_WABA = int(USE_WABA)
except ValueError:
    USE_WABA = 0

# --- Anti-bank hardening (Phase 3) для ОРИГИНАЛЬНОГО аккаунта ---
HEADROOM_PCT = int(os.environ.get("BULK_HEADROOM_PCT", "25"))

# "Безопасные часы" внутри активного окна. Personal — сужено (11-19), WABA — шире.
PERSONAL_SAFE_HOURS_START = int(os.environ.get("BULK_PERSONAL_SAFE_HOURS_START", "11"))
PERSONAL_SAFE_HOURS_END = int(os.environ.get("BULK_PERSONAL_SAFE_HOURS_END", "19"))
WABA_SAFE_HOURS_START = int(os.environ.get("BULK_WABA_SAFE_HOURS_START", "10"))
WABA_SAFE_HOURS_END = int(os.environ.get("BULK_WABA_SAFE_HOURS_END", "20"))

# Межканальный дроссель (не пер-контакт, а между отправками в любой канал).
MIN_INTER_SEND_SEC = int(os.environ.get("BULK_MIN_INTER_SEND_SEC", "60"))

# Per-phone cooldown после успешной отправки (часы).
PER_PHONE_COOLDOWN_HOURS_WABA = int(
    os.environ.get("BULK_PER_PHONE_COOLDOWN_HOURS_WABA", "0")
)
PER_PHONE_COOLDOWN_HOURS_PERSONAL = int(
    os.environ.get("BULK_PER_PHONE_COOLDOWN_HOURS_PERSONAL", "96")
)

# Complaint halt thresholds per channel (Phase 3.6).
COMPLAINT_HALT_THRESHOLD_TG = int(os.environ.get("BULK_COMPLAINT_HALT_TG", "3"))
COMPLAINT_HALT_THRESHOLD_MAX = int(os.environ.get("BULK_COMPLAINT_HALT_MAX", "2"))
COMPLAINT_HALT_THRESHOLD_WABA = int(os.environ.get("BULK_COMPLAINT_HALT_WABA", "3"))
COMPLAINT_WINDOW_HOURS = int(os.environ.get("BULK_COMPLAINT_WINDOW_HOURS", "24"))
COMPLAINT_HALT_DURATION_HOURS = int(
    os.environ.get("BULK_COMPLAINT_HALT_HOURS", "24")
)

# Adaptive decay: жалоба в день → cap × decay на следующий день.
ADAPTIVE_DECAY_ON_COMPLAINT = float(
    os.environ.get("BULK_ADAPTIVE_DECAY_ON_COMPLAINT", "0.5")
)

# Spintax минимальное количество вариантов.
SPINTAX_VARIANT_COUNT_MIN = int(os.environ.get("BULK_SPINTAX_VARIANT_COUNT_MIN", "3"))

# Reachability cache TTL (hours).
REACHABILITY_CACHE_TTL_HOURS = int(
    os.environ.get("BULK_REACHABILITY_CACHE_TTL_HOURS", "24")
)

# WABA-specific: бот-режим (free text в 24ч-окне) vs template mode (cold outreach).
WABA_FREE_TEXT_WINDOW_HOURS = 24

# Bypass safe hours check (для тестов и debugging). НЕ для продакшена.
# BULK_BYPASS_SAFE_HOURS=1 → sender шлёт в любое время суток.
BULK_BYPASS_SAFE_HOURS = os.environ.get(
    "BULK_BYPASS_SAFE_HOURS", "0"
).strip().lower() in ("1", "true", "yes", "on")

# Adaptive delay: жалоба в последние 60 мин → +50% к base delay.
COMPLAINT_ADAPTIVE_DELAY_PCT = float(
    os.environ.get("BULK_COMPLAINT_ADAPTIVE_DELAY_PCT", "0.5")
)
COMPLAINT_ADAPTIVE_LOOKBACK_MIN = int(
    os.environ.get("BULK_COMPLAINT_ADAPTIVE_LOOKBACK_MIN", "60")
)

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
# Пауза после FAILED контакта (мёртвый номер) — минимальная, чтоб быстро идти дальше
FAILED_DELAY_MIN = int(os.environ.get("BULK_FAILED_DELAY_MIN", "5"))
FAILED_DELAY_MAX = int(os.environ.get("BULK_FAILED_DELAY_MAX", "15"))

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
# Если = "1" / "true" — sender НЕ вызывает Wazzup API, только имитирует успешные
# отправки. Полезно для прогона расписания, проверки квот и таймингов без риска
# отправить что-то реальное. ВАЖНО: при боевом запуске должен быть 0 / пусто.
DRY_RUN = os.environ.get("BULK_DRY_RUN", "0").strip().lower() in ("1", "true", "yes", "on")

REPLY_SEPARATOR = "\n----------------\n"

# Bad phones cache (для skip'а мёртвых номеров, см. bad_phones.py)
BAD_PHONES_PATH = os.environ.get("BULK_BAD_PHONES_PATH", str(DATA_DIR / "bad_phones.json"))

# Failed contacts log (все 3 канала fail'нули сегодня, см. failed_contacts.py)
FAILED_TODAY_PATH = os.environ.get("BULK_FAILED_TODAY_PATH", str(DATA_DIR / "failed_today.json"))

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

# Target: сколько УСПЕШНЫХ контактов нужно в день (1+ канал доставлен = успех).
# Sender работает пока не достигнет target, не кончатся контакты, или не выйдет из активного окна.
TARGET_SUCCESS_PER_DAY = int(os.environ.get("BULK_TARGET_SUCCESS_PER_DAY", "60"))
# Safety cap: максимум попыток в день (чтоб не долбить 24/7 если данные плохие).
MAX_ATTEMPTS_PER_DAY = int(os.environ.get("BULK_MAX_ATTEMPTS_PER_DAY", "200"))


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
    """Быстрая проверка перед боевым запуском: Wazzup24 отвечает, каналы живы.

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
    """
    # Импортируем здесь, чтобы не было циклической зависимости
    from . import wazzup

    result: dict = {"ok": True, "channels": {}, "errors": []}
    active = wazzup.active_channels()

    if not active:
        result["ok"] = False
        result["errors"].append(
            "Нет активных каналов (проверьте WAZZUP_API_KEY и WAZZUP_*_CHANNEL_ID)"
        )
        return result

    # Один запрос к /v3/channels — Wazzup сам знает, какие каналы активны.
    ok, detail, channels = wazzup.get_wazzup_channels()
    if not ok:
        result["ok"] = False
        result["errors"].append(f"Wazzup API недоступен: {detail[:200]}")
        for ch in active:
            result["channels"][ch] = {
                "ok": False, "detail": "wazzup api unreachable", "http": None
            }
        return result

    # Нормализуем: channels может быть list[dict] или dict (Wazzup API менялся).
    live_channel_ids: set[str] = set()
    if isinstance(channels, list):
        for c in channels:
            if isinstance(c, dict):
                cid = c.get("channelId")
                if isinstance(cid, str) and cid:
                    live_channel_ids.add(cid)
    elif isinstance(channels, dict):
        for v in channels.values():
            if isinstance(v, dict):
                cid = v.get("channelId")
                if isinstance(cid, str) and cid:
                    live_channel_ids.add(cid)
            elif isinstance(v, list):
                for c in v:
                    if isinstance(c, dict):
                        cid = c.get("channelId")
                        if isinstance(cid, str) and cid:
                            live_channel_ids.add(cid)

    for ch in active:
        expected_cid = wazzup.channel_id_for(ch)
        if not expected_cid:
            result["channels"][ch] = {
                "ok": False, "detail": "no WAZZUP_*_CHANNEL_ID configured", "http": None
            }
            result["ok"] = False
            result["errors"].append(f"{ch}: WAZZUP_*_CHANNEL_ID пустой")
        elif live_channel_ids and expected_cid not in live_channel_ids:
            result["channels"][ch] = {
                "ok": False, "detail": f"channel {expected_cid[:8]}… не в /v3/channels", "http": None
            }
            result["ok"] = False
            result["errors"].append(f"{ch}: канал {expected_cid[:8]}… не найден в Wazzup")
        else:
            result["channels"][ch] = {
                "ok": True, "detail": f"channelId={expected_cid[:8]}…", "http": 200
            }

    return result
