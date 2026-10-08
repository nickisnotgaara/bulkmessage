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

    Каждый следующий файл override'ит предыдущие. Если переменная уже задана
    в окружении НЕПУСТЫМ значением (например, через export в shell) —
    она НЕ перезаписывается: это позволяет override'ить секреты через
    переменные окружения без правки файлов.

    ВАЖНО: пустое значение НЕ считается «уже заданным». Раньше проверка была
    `if key not in os.environ`, и это ломало всё: в .env лежит
    `WAZZUP_WEBHOOK_URL=` (пусто), поэтому os.environ получал пустую строку
    из .env, а .env.local уже не мог её переопределить. Итог: сколько бы ни
    вписывали webhook URL в .env.local, sender видел пустоту и preflight
    писал «webhook не задан» — причём молча, без единого предупреждения.
    Теперь пустое значение пропускается, и .env.local работает как задумано.
    """
    candidates = _candidate_env_files()
    for env_path in candidates:
        parsed = _parse_env_file(env_path)
        for key, val in parsed.items():
            if os.environ.get(key):
                continue  # реально задано окружением/более приоритетным файлом
            if not val:
                continue  # пустышки ничего не добавляют
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
#
# ВНИМАНИЕ: кап стал ПЕР-КАНАЛЬНЫМ. Раньше он был один общий на TG и MAX
# (30), из-за чего BULK_LIMIT_TELEGRAM=50 молча резался до 30 — владелец
# ставил 50, а фактически выходило 30, и причина была неочевидна.
# Теперь у каждого канала своя потолок, и лимит из .env.local режет именно
# его: TG до 50, MAX до 15.
#
# Почему MAX остаётся низким (15): 3–5 жалоб Meta = перманентный бан
# ЛИЧНОГО аккаунта MAX. Владелец сознательно выбрал 15 вместо прежних 30.
MAX_PERSONAL_DAILY_HARD_CAP = 30  # legacy-значение, ниже не используется

# Жёсткие потолки для личных аккаунтов: env НЕ может их превысить.
#
# ВНИМАНИЕ (решение владельца 2026-10-08): MAX поднят 15 → 20.
# Это ОСОЗНАННЫЙ РИСК: 3-5 жалоб = перманентный бан ЛИЧНОГО номера MAX.
# Защита — COMPLAINT_HALT_THRESHOLD_MAX (2 жалобы за 24ч → halt канала),
# она срабатывает независимо от дневного капа.
PERSONAL_CHANNEL_HARD_CAPS = {
    "telegram": 50,
    "max": 20,
}

ORIGINAL_ACCOUNT_DAILY_HARD_CAPS = {
    "waba": 250,
    "telegram": 50,
    "max": 20,
}

# --- Daily limits ---
# WABA — отдельный ключ (BULK_LIMIT_WABA) или авто из tier.
# Personal-каналы ограничены PERSONAL_CHANNEL_HARD_CAPS (TG 50, MAX 20).
def _safe_limit(ch: str, default: int, hard_cap: int = 10**9) -> int:
    val = int(os.environ.get(f"BULK_LIMIT_{ch.upper()}", str(default)))
    return min(val, hard_cap)

CHANNEL_DAILY_LIMITS = {
    "whatsapp": _safe_limit("whatsapp", 60),
    "telegram": _safe_limit(
        "telegram", 50, hard_cap=PERSONAL_CHANNEL_HARD_CAPS["telegram"]),
    "max": _safe_limit(
        "max", 20, hard_cap=PERSONAL_CHANNEL_HARD_CAPS["max"]),
    "waba": _safe_limit("waba", 250),
    # SMS — fallback последней надежды. НЕ ограничен MAX_PERSONAL_DAILY_HARD_CAP:
    # SMS идёт через агрегатор с зарегистрированным именем отправителя, а не через
    # оригинальный личный аккаунт мессенджера. Но каждая SMS реально стоит денег
    # (~8.4-8.5 ₽ за сегмент), поэтому кап всё равно жёсткий: 50/день = 37 effective
    # при headroom 25% (~314 ₽/день максимум).
    "sms": _safe_limit("sms", 50),
}

# --- SMS.RU (fallback-канал, отдельный транспорт НЕ через Wazzup24) ---
SMSRU_BASE_URL = os.environ.get("BULK_SMSRU_BASE_URL", "https://sms.ru").strip().rstrip("/")
# СЕКРЕТ. Только в .env.local, никогда не коммитить и не логировать.
SMSRU_API_ID = os.environ.get("BULK_SMSRU_API_ID", "").strip()
# Буквенный отправитель (alphanumeric sender), согласованный с SMS.RU.
# Без него SMS.RU отвечает ошибкой 221.
SMSRU_SENDER_NAME = os.environ.get("BULK_SMSRU_SENDER_NAME", "").strip()
# Цена одного сегмента SMS по прайсу SMS.RU (НДС включён). Используется только
# для оценки затрат в логах — реальная цена считается на стороне SMS.RU.
SMSRU_COST_PER_SEGMENT = float(
    os.environ.get("BULK_SMSRU_COST_PER_SEGMENT", "8.36") or "8.36"
)
# 0 = полностью выключить SMS-канал (каскад работает как раньше, 3 мессенджера).
SMSRU_ENABLED = int(os.environ.get("BULK_SMSRU_ENABLED", "1") or "1")
# 1 = добавлять test=1 ко всем вызовам SMS.RU (сервис ничего не отправляет,
# баланс не тратится). Дополнительный предохранитель к BULK_DRY_RUN.
SMSRU_TEST_MODE = os.environ.get(
    "BULK_SMSRU_TEST_MODE", "0"
).strip().lower() in ("1", "true", "yes", "on")
# Номер для preflight smoke-теста (test=1 — SMS.RU ничего не отправляет).
# По умолчанию 79000000000: заведомо несуществующий, но валидный формат,
# поэтому 202/207 не сработают и мы увидим именно ошибку конфигурации.
SMSRU_TEST_PHONE = os.environ.get("BULK_SMSRU_TEST_PHONE", "79000000000").strip()
# Жёсткий лимит сегментов (SMS.RU отвергает >8 сегментов ошибкой 205).
# По умолчанию 2 сегмента = 140 символов = ~17 ₽ максимум за сообщение.
SMSRU_MAX_SEGMENTS = int(os.environ.get("BULK_SMSRU_MAX_SEGMENTS", "2"))
# Свой файл с короткими SMS-шаблонами (длинные мессенджерные не подходят).
SMSRU_TEMPLATES_PATH = os.environ.get(
    "BULK_SMSRU_TEMPLATES_PATH", "Message_script_sms.md"
).strip()
# Safe hours для SMS. По умолчанию как у Personal (11-19): ночные SMS
# жалуются чаще, а SMS.RU тратит на них деньги.
SMSRU_SAFE_HOURS_START = int(os.environ.get("BULK_SMS_SAFE_HOURS_START", "11"))
SMSRU_SAFE_HOURS_END = int(os.environ.get("BULK_SMS_SAFE_HOURS_END", "19"))
# Per-phone cooldown после успешной SMS (контакт не трогаем SMS-каналом 4 дня).
SMSRU_COOLDOWN_HOURS = int(os.environ.get("BULK_SMS_COOLDOWN_HOURS", "96"))
# Жалоба на спам в SMS.RU может заблокировать имя отправителя у ВСЕХ операторов,
# поэтому порог строже, чем у TG/WABA (и такой же, как у MAX).
COMPLAINT_HALT_THRESHOLD_SMS = int(os.environ.get("BULK_COMPLAINT_HALT_SMS", "2"))
# Пауза между контактами, когда предыдущий доставлен через SMS. SMS — терминальный
# канал с низким объёмом, поэтому пауза заметно короче PERSONAL_DELAY (300-900с).
SMS_DELAY_MIN = int(os.environ.get("BULK_SMS_DELAY_MIN", "30"))
SMS_DELAY_MAX = int(os.environ.get("BULK_SMS_DELAY_MAX", "90"))

# --- Wazzup24 (единственный транспорт для мессенджеров) ---
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

# Circuit breaker: сколько подряд отказов перегрузки (PEER_FLOOD, FLOOD_WAIT,
# rate-limit) до полной остановки канала на 1 час. Эти ошибки не являются
# жалобами, поэтому complaint-пороги их не ловят, но по факту канал после
# серии таких отказов перестаёт отвечать.
OVERLOAD_STREAK_LIMIT = int(os.environ.get("BULK_OVERLOAD_STREAK_LIMIT", "5"))
OVERLOAD_HALT_HOURS = int(os.environ.get("BULK_OVERLOAD_HALT_HOURS", "1"))

# Circuit breaker на ПРОВАЙДЕРСКИЙ отказ: канал принимает сообщения (HTTP 201),
# но адресовать получателей не может — Wazzup отвечает BAD_CONTACT даже на
# self-send (живой случай 2026-10-07, Telegram). Считаются только РАЗНЫЕ
# контакты: один мёртвый номер не должен гасить исправный канал. Порог
# достигнут → канал в halt, каскад идёт сразу в следующий по порядку.
# 0 — выключить проверку целиком.
PROVIDER_FAIL_STREAK_LIMIT = int(os.environ.get("BULK_PROVIDER_FAIL_STREAK_LIMIT", "3"))
PROVIDER_HALT_HOURS = int(os.environ.get("BULK_PROVIDER_HALT_HOURS", "6"))

# Каналы, для которых номер телефона уходит в поле `phone`, а НЕ в `chatId`.
# По документации Wazzup chatId для telegram/max — это внутренний peer id,
# «не совпадает с номером телефона», и приходит в ответе на отправку по phone.
# Номер в chatId Wazzup принимает молча (201), но Telegram отвечает
# BAD_CONTACT на каждый адресат, включая self-send.
# MAX работает и с `phone`, и с `chatId`, поэтому оставлен на chatId, пока
# не проверен на живых отправках. Дописать `max` после проверки.
WAZZUP_PHONE_PARAM_CHANNELS = tuple(
    c.strip() for c in os.environ.get(
        "BULK_WAZZUP_PHONE_PARAM_CHANNELS", "telegram"
    ).split(",") if c.strip()
)

# Сторож подтверждений: сколько ждать delivery_status, прежде чем считать
# сообщение недоставленным и отправить контакт в rescue. Вебхук — единственный
# источник запуска fallback; если он молчит, контакт иначе навсегда остаётся
# в статусе 'sent' и тихо исчезает. Wazzup присылает статус за секунды-минуты,
# поэтому 60 минут — консервативный порог с запасом.
CONFIRM_TIMEOUT_MINUTES = int(
    os.environ.get("BULK_CONFIRM_TIMEOUT_MINUTES", "60") or "60"
)
# Верхняя граница: сторож НЕ трогает сообщения старше этого возраста. Без неё
# первый запуск поднял бы в rescue всю историю (в реальной базе — 150 сообщений
# от июля, 2,5 месяца назад) и повторно отрекламировал бы старым контактам,
# выбив дневной бюджет.
CONFIRM_MAX_AGE_HOURS = int(
    os.environ.get("BULK_CONFIRM_MAX_AGE_HOURS", "24") or "24"
)

# Согласие на рассылку. Ч.1 ст.18 ФЗ-38 + политика Meta: business-initiated
# сообщения только opt-in контактам, бремя доказывания на рекламораспространителе.
#
# 0 (по умолчанию) — писать контактам со статусом consent='unknown' (старое
#   поведение: вся база считалась рабочей).
# 1 — писать ТОЛЬКО тем, у кого consent='granted'. Включайте, когда разделите
#   базу по согласию (например, по этапу сделки в AmoCRM).
#
# ВНИМАНИЕ: consent='denied' (отписка) уважается ВСЕГДА, независимо от этого
# флага. Отключить отписку настройкой нельзя.
CONSENT_REQUIRED_FOR_COLD = int(
    os.environ.get("BULK_CONSENT_REQUIRED_FOR_COLD", "0") or "0"
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
GOOGLE_WORKSHEET_NAME = os.environ.get("BULK_GOOGLE_WORKSHEET", "new_messages")

# Схема листа отправок.
#
# Отличие от прежней (Messages) — не косметика. Старая схема не позволяла
# отличить «не доставлено» от «подтверждения ещё нет»: колонка Delivered
# хранила время, а Delivery State (unknown/delivered/rejected) в таблице не
# было вообще. Из-за этого 803 строки выглядели как отправки, а писали
# 359 человек.
#
# Что добавлено и почему:
#   Reached        — «дошло ли до человека»: доставлено ИЛИ прочитано
#                    ИЛИ есть ответ. Прочитать можно только доставленное,
#                    поэтому событие `read` доказывает доставку сильнее,
#                    чем её отсутствие. Без этой колонки 78 человек,
#                    прочитавших без события delivered, выпадали из счёта.
#   Delivery State — трёхзначный вердикт вебхука. Пусто ≠ «не доставлено».
#   Last Error     — причина отказа, без неё отказ невозможно разобрать.
#   Contact ID     — связь строки листа с записью в БД.
#   Consent        — правовое основание рассылки (ФЗ-38 ч.1 ст.18).
#
# Что переименовано: Delivered/Read/Answered → *At, потому что это ВРЕМЯ,
# а «Answered» было «Yes/нет» и теряло само время ответа.
GSHEET_HEADERS = [
    "Phone",
    "Name",
    "Category",
    "Channel",
    "Message",
    "Sent At",
    "Delivered At",
    "Read At",
    "Answered At",
    "Reached",
    "Delivery State",
    "Status",
    "Last Error",
    "Reply",
    "Last Activity",
    "Message ID",
    "Contact ID",
    "Consent",
]

# Индексы колонок для поиска строки. Считаются от заголовка, а не числами
# в коде: перестановка столбца больше не ломает поиск молча.
GSHEET_COL = {name: i for i, name in enumerate(GSHEET_HEADERS)}

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

# Channel-aware inter-contact delay (Phase 6 — оптимизация скорости рассылки).
# Зависит от того, какой канал доставил ПРЕДЫДУЩИЙ контакт:
#  • WABA успех → Personal не использовался → короткая пауза (15 сек)
#  • Personal (TG/MAX) успех → anti-bank → длинная пауза (5-15 мин)
#  • Все 3 фейл → FAILED_DELAY (быстро идём дальше, anti-bank не нужен)
# Внутри cascade (WABA → TG → MAX для одного контакта) задержек нет.
WABA_DELAY_MIN = int(os.environ.get("BULK_WABA_DELAY_MIN", "10"))
WABA_DELAY_MAX = int(os.environ.get("BULK_WABA_DELAY_MAX", "30"))
PERSONAL_DELAY_MIN = int(os.environ.get("BULK_PERSONAL_DELAY_MIN", "300"))
PERSONAL_DELAY_MAX = int(os.environ.get("BULK_PERSONAL_DELAY_MAX", "900"))

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

# Лимит отправок WABA в ОДНОМ запуске (0 = не ограничивать, берётся
# дневной лимит BULK_LIMIT_WABA).
#
# Нужен для первых боевых прогонов: вместо 250 сообщений сразу отдать
# 10–20, посмотреть в отчёте реальную конверсию доставки и жалоб, и только
# потом отпускать полный объём. Ставится флагом:
#     py main.py sender --run-hours 2 --waba-limit 20
# или постоянно в .env.local: BULK_WABA_RUN_LIMIT=20
WABA_RUN_LIMIT = int(os.environ.get("BULK_WABA_RUN_LIMIT", "0") or 0)

# --- Фазовая рассылка (WABA-проход → TG → MAX) ---
# Раньше каскад был по-контактным: waba → tg → max внутри _run_one_contact с
# break на первом HTTP 2xx. Из-за этого WABA отдавал 2xx на всё, sender считал
# это успехом, и до Telegram дело не доходило (100 отправок → 28 доставок без
# переключения). Теперь выбор канала задаётся ФАЗОЙ, а не контактом.
PHASE_ORDER = [
    ch.strip()
    for ch in os.environ.get("BULK_PHASE_ORDER", "waba,telegram,max").split(",")
    if ch.strip()
]

# ============================================================================
# PROBE — фаза проверки наличия аккаунта в канале
# ============================================================================
# Механика: у Wazzup НЕТ эндпоинта «есть ли у номера аккаунт в Telegram».
# Единственный способ — отправить запрос с `phone`. Отрицательный вердикт
# приходит СИНХРОННЫМ HTTP 400 (CHANNEL_TGAPI_PHONE_NOT_OCCUPIED) на самом
# запросе, а не эхом вебхука, и квоту при этом НЕ списывает
# (increment_channel_sent вызывается только в _persist_success).
#
# Цена: если аккаунт ЕСТЬ — придёт 201 и человеку реально уйдёт сообщение.
# Поэтому проба обязана слать РАБОЧИЙ текст, а успех — немедленно учитываться
# как реальная отправка, иначе контакт получит дубль в фазе отката.
PROBE_ENABLED = bool(
    os.environ.get("BULK_PROBE_ENABLED", "1",).strip().lower()
    in ("1", "true", "yes", "on")
)
# Предохранитель от шторма проб за один прогон.
PROBE_MAX_PER_RUN = int(os.environ.get("BULK_PROBE_MAX_PER_RUN", "400") or 400)
# Пауза между пробами, сек. Вежливость к API Wazzup.
PROBE_INTERVAL_MIN = int(os.environ.get("BULK_PROBE_INTERVAL_MIN", "2") or 2)
# Повторно не пробивать тот же (контакт, канал) в течение TTL часов.
PROBE_TTL_HOURS = int(os.environ.get("BULK_PROBE_TTL_HOURS", "24") or 24)
# Каналы, на которых проба имеет смысл (личные аккаунты).
PROBE_CHANNELS = [
    ch.strip()
    for ch in os.environ.get("BULK_PROBE_CHANNELS", "telegram,max").split(",")
    if ch.strip()
]
# Canary-номер для проверки механики на живом канале (не входит в боевой пул).
TEST_CONTACT = os.environ.get("BULK_TEST_CONTACT", "").strip()

# --- Режим «длинный прогон»: сколько ЧАСОВ работает один запуск sender'а ---
# 0 = выключен (по умолчанию): один проход по фазам и выход, как раньше.
#
# Зачем: без внешнего планировщика рассылка заканчивается за ~1.4 часа —
# 250 сообщений по паузе 10-30 секунд. Раньше это компенсировали кроном,
# который дёргал sender несколько раз в день, и он перезапускался в том же
# окне. С этим флагом запускаешь ОДИН процесс, и он сам крутит цикл
# «фазы → пауза → фазы» до истечения дедлайна.
RUN_HOURS = float(os.environ.get("BULK_RUN_HOURS", "0") or 0)
# Пауза между кругами цикла, когда все фазы отработали (например, ждём
# события вебхука или просто держим процесс живым до дедлайна).
RUN_LOOP_PAUSE_MINUTES = float(
    os.environ.get("BULK_RUN_LOOP_PAUSE_MINUTES", "10") or 10
)
# Растягивать ли паузу до дедлайна ТОЛЬКО на откатных фазах (TG/MAX).
#
# 1 = да (рекомендуется): WABA отрабатывает быстро своими 10-30 секундами,
#     чтобы вебхук успел принести failed-события, а откаты TG/MAX занимают
#     остаток времени. Именно это и нужно владельцу: «сначала быстро по
#     WABA, потом по ошибкам — откат, всё за 10 часов».
# 0 = нет: все фазы, включая WABA, растягиваются равномерно на дедлайн.
#     Тогда 250 сообщений идут по 2.4 минуты, а вебхук не успевает
#     подтвердить доставку до старта отката — TG/MAX стартуют на пустом
#     списке провалов.
PHASE_STRETCH_FALLBACK_ONLY = bool(
    os.environ.get("BULK_PHASE_STRETCH_FALLBACK_ONLY", "1")
)

# Пауза между последней отправкой фазы WABA и стартом фазы Telegram: столько
# ждём события вебхука. ВЕБХУК — единственный источник подтверждения доставки.
#
# ВАЖНО: Wazzup не присылает статус `failed` (замер 2026-10-06: 0 из 1321
# событий; реальные статусы — delivered / read / sent / error). Отказ Meta по
# маркетинговому шаблону выглядит как ТИШИНА. Поэтому после этой паузы
# sender прогоняет сторож молчания и кладёт неподтверждённые сообщения в
# rescue — иначе фазы TG/MAX уходят в пустоту и откат не работает никогда.
PHASE_GRACE_MINUTES = int(os.environ.get("BULK_PHASE_GRACE_MINUTES", "5"))

# Трёхзначная доставка (план «устойчивый каскад», 2026-10-07).
#
# Замер 2026-10-06: из 16 принятых WABA-сообщений `delivered`-подтверждение
# пришло ровно для одного, при этом в UI Wazzup 4 из 6 были без ошибок.
# То есть «✓ в интерфейсе» ≠ «доставлено», а молчание ≠ отказ. Старая логика
# («нет подтверждения за RESCUE_SETTLE_MINUTES → провал → откат») давала
# ложные провалы порядка 70-80% и лила дубли в личные каналы.
#
# Теперь молчание порождает отдельное состояние `unknown`, и в откат оно
# попадает только спустя длинный горизонт. Явный отказ (`failed` с события
# или от транспорта) — по короткому горизонту.
REJECT_GRACE_MINUTES = int(
    os.environ.get("BULK_REJECT_GRACE_MINUTES",
                   os.environ.get("BULK_RESCUE_SETTLE_MINUTES", "5"))
    or "5"
)
UNKNOWN_GRACE_MINUTES = int(os.environ.get("BULK_UNKNOWN_GRACE_MINUTES", "720"))

# Боевой прогон без работающего вебхука: подтверждений доставки не будет, и
# каскад откатов превратится в таймер — через UNKNOWN_GRACE_MINUTES принятые
# сообщения уйдут в Telegram/MAX, то есть в ЛИЧНЫЕ аккаунты. Дефолт — отказ
# со старта (sender.delivery_signal_available / _guard_delivery_signal).
# Обход — только осознанно, цифрой 1.
ALLOW_NO_DELIVERY_SIGNAL = bool(int(os.environ.get(
    "BULK_ALLOW_NO_DELIVERY_SIGNAL", "0") or "0"
))

# Потолок попыток для одного контакта: исчерпал → status='exhausted',
# больше автоматических попыток нет, контакт уходит в ежедневную выгрузку.
EXHAUST_MAX_ATTEMPTS = int(os.environ.get("BULK_EXHAUST_MAX_ATTEMPTS", "3"))
EXHAUST_WINDOW_DAYS = int(os.environ.get("BULK_EXHAUST_WINDOW_DAYS", "7"))
# Верхняя граница «молчания» для перевода в exhausted. Без неё первый запуск
# после деплоя пометил бы exhausted ВСЮ историю сообщений, а на боевой базе
# это тысячи строк с июля.
EXHAUST_MAX_AGE_DAYS = int(os.environ.get("BULK_EXHAUST_MAX_AGE_DAYS", "30"))

# Молчание по WABA почти всегда означает отказ Meta по маркетинговому лимиту
# («Сейчас этому клиенту нельзя отправить шаблон „Маркетинг“»). Отказ не
# приходит событием, но и повторно слать бессмысленно: контакт уходит в
# антикоастинг на столько дней, но ОСТАЁТСЯ доступен для отката по другим
# каналам.
WABA_SILENT_COOLDOWN_DAYS = int(os.environ.get("BULK_WABA_SILENT_COOLDOWN_DAYS", "14"))

# Параллелизм фазы WABA. После пополнения баланса tier 1 = 1000/день, при
# последовательной паузе 10-30с это ~9 часов прогона. Личные каналы НИКОГДА
# не распараллеливаются — их потолок не скорость, а лимит.
WABA_CONCURRENCY = max(1, int(os.environ.get("BULK_WABA_CONCURRENCY", "5")))

# Инвариант: не более одного сообщения на контакт в одном канале за окно.
CONTACT_CHANNEL_COOLDOWN_HOURS = int(
    os.environ.get("BULK_CONTACT_CHANNEL_COOLDOWN_HOURS", "24")
)

# Прозрачность в Telegram: по умолчанию успехи не летят построчно, только
# агрегат раз в N. Но владельцу нужен СТАТУС ПО КАЖДОМУ контакту — по имени и
# номеру, поэтому построчный режим включается отдельно и ограничен бюджетом на
# прогон: иначе на 250 контактах бот сам станет источником спама.
TG_DIGEST_EVERY = int(os.environ.get("BULK_TG_DIGEST_EVERY", "25"))
TG_MAX_MESSAGES_PER_HOUR = int(os.environ.get("BULK_TG_MAX_PER_HOUR", "60"))
# 1 = писать в личку каждый контакт (имя, номер, канал, исход).
# 0 = только агрегаты и значимые события (прежнее поведение).
TG_PER_CONTACT = int(os.environ.get("BULK_TG_PER_CONTACT", "1"))
# Потолок построчных сообщений за ОДИН прогон. После него — агрегат.
TG_PER_CONTACT_MAX = int(os.environ.get("BULK_TG_PER_CONTACT_MAX", "120"))
# Каналы, которых НЕТ в этой рассылке, не упоминаются в уведомлениях владельцу.
# WhatsApp Personal давно выключен (BULK_USE_WABA=1 → каскад WABA→TG→MAX),
# но в дайджест он попадал из общего словаря лимитов и путал при разборе.
TG_HIDE_CHANNELS = tuple(
    c.strip() for c in os.environ.get(
        "BULK_TG_HIDE_CHANNELS", "whatsapp,sms").split(",") if c.strip()
)

# Как часто опрашивать Wazzup за реальным tier WABA. Через вебхук тир приходит
# событием channel.waba_tier_update, но вебхук может быть не поднят/устарел —
# тогда баланс пополнен, а система thinks tier 0 и живёт на 250/день.
WABA_TIER_REFRESH_HOURS = int(os.environ.get("BULK_WABA_TIER_REFRESH_HOURS", "12"))

# Такая же пауза между фазами отката (после TG — перед MAX), чтобы долетели
# статусы по предыдущему каналу. По той же причине сопровождается сторожом.
RESCUE_SETTLE_MINUTES = int(os.environ.get("BULK_RESCUE_SETTLE_MINUTES", "5"))

# Сторож здоровья канала перед фазой отката. Живой случай 2026-10-06:
# Telegram принимал отправки (HTTP 200) и НИ ОДНОЙ не доставлял — канал не
# авторизован в Wazzup. Фаза продолжала жечь личный лимит, пока её не
# остановили вручную. Теперь при N отправках без единого подтверждения
# фаза пропускается, а очередь остаётся нетронутой для следующего канала.
CHANNEL_HEALTH_GUARD = os.environ.get("BULK_CHANNEL_HEALTH_GUARD", "1") != "0"
CHANNEL_HEALTH_MIN_SAMPLE = int(
    os.environ.get("BULK_CHANNEL_HEALTH_MIN_SAMPLE", "5"))
CHANNEL_HEALTH_WINDOW_HOURS = int(
    os.environ.get("BULK_CHANNEL_HEALTH_WINDOW_HOURS", "6"))

# Сколько дней контакт, которому WABA НЕ доставил, исключается из WABA-пула.
# НЕОБХОДИМО отдельный маркер: reachability_cache живёт всего 24 часа
# (REACHABILITY_CACHE_TTL_HOURS), и без этого контакт на следующий день снова
# получил бы один из 250 драгоценных WABA-слотов, хотя заведомо не доходит.
WABA_EXHAUSTED_TTL_DAYS = int(os.environ.get("BULK_WABA_EXHAUSTED_TTL_DAYS", "30"))

# Детерминированные задержки вместо случайных: перед каждой отправкой фазы
# ждём clamp(остаток_окна / остаток_сообщений, MIN, MAX). Так фаза гарантированно
# укладывается в своё окно. При случайных 300-900с суммарное σ для 45 сообщений
# ≈19 минут, а запас был всего 20 — то есть ~10% вероятности вылеза за 19:00.
PHASE_DETERMINISTIC_DELAY = os.environ.get(
    "BULK_PHASE_DETERMINISTIC_DELAY", "1"
).strip().lower() in ("1", "true", "yes", "on")



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

    def _collect_ids(node) -> None:
        if isinstance(node, dict):
            cid = node.get("channelId")
            if isinstance(cid, str) and cid:
                live_channel_ids.add(cid)
            for v in node.values():
                _collect_ids(v)
        elif isinstance(node, list):
            for v in node:
                _collect_ids(v)

    _collect_ids(channels)

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

    # Инвентаризация каналов. В Wazzup «канал» = один аккаунт мессенджера, и
    # оплата идёт за КАЖДЫЙ канал отдельно. Поэтому полезно видеть, сколько
    # каналов реально оплачено, а сколько из них задействовано конфигом:
    # bulkmessage адресует ровно один UUID на канал (get_wazzup_channel_id_for),
    # поэтому лишние каналы в аккаунте молча простаивают.
    used_ids = {wazzup.channel_id_for(ch) for ch in active if wazzup.channel_id_for(ch)}
    unused_ids = sorted(live_channel_ids - used_ids) if live_channel_ids else []
    result["channel_inventory"] = {
        "live_total": len(live_channel_ids),
        "used": len(used_ids),
        "unused": len(unused_ids),
        "unused_ids": unused_ids[:20],
    }

    return result
