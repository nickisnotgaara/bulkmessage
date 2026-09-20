"""Wazzup24 API client (Phase 1).

Используется для отправки WABA-сообщений через Wazzup gateway.

API reference (см. WAZZUP24_KNOWLEDGE_BASE.md):
- POST /v3/message      — отправка (text OR templateId, not both)
- PATCH /v3/webhooks    — подписка (4 флага)
- GET  /v3/channels     — список каналов
- Auth: Bearer {apiKey | sidecarApiKey}

Retry policy: 5xx/timeout → 1 retry, 429 → exponential backoff 5min-1h,
401/403 → AUTH (halt channel).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
import re
import time
from enum import Enum
from typing import Optional

import requests

from . import config


log = logging.getLogger("wazzup")


class ErrorKind(Enum):
    """Классификация ошибок отправки (PERMANENT/RATE_LIMIT/TRANSIENT/AUTH/UNKNOWN)."""
    PERMANENT = "permanent"
    RATE_LIMIT = "rate"
    TRANSIENT = "transient"
    AUTH = "auth"
    UNKNOWN = "unknown"


# WABA-specific error codes (см. WAZZUP24_KNOWLEDGE_BASE.md)
_PERMANENT_KEYWORDS = (
    "template_paused",
    "template_rejected",
    "template_disabled",
    "build_template_error",   # Wazzup: Meta не может построить template из строки
                              # (неверный template_id, не одобрен, неверные variables,
                              # template привязан к другому WABA каналу).
                              # Без этого cascade откатывался в TG/MAX на КАЖДОМ контакте
                              # → риск бана оригинальных аккаунтов.
    "template not found",
    "template_not_found",
    "invalid_template",
    "unapproved template",
    "re_engagement_not_allowed",
    "recipient_not_in_allowed_contacts",
    "quality_blocked",
    "frequency_cap_exceeded",  # хардкап по тиру — на сутки стоп
    "not registered",
    "invalid phone",
    "blocked",
    "is not on whatsapp",
    # Универсальные (все каналы)
    "recipient not found",
    "doesn't have a telegram",
    "no telegram account",
    "user not found",
    "phone_not_occupied",
)

_AUTH_KEYWORDS = (
    "401",
    "403",
    "unauthorized",
    "invalid token",
    "forbidden",
    "auth",
    "channel not found",
)

_RATE_KEYWORDS = (
    "rate limit",
    "too many",
    "flood",
    "429",
    "retry later",
    "spam",
    "temporary ban",
    "frequency_cap_exceeded",  # тоже rate-style — soft limit на tier
)

_TRANSIENT_KEYWORDS = (
    "timeout",
    "timed out",
    "connection",
    " 502 ",
    " 503 ",
    " 504 ",
    "request error",
    "internal error",
    "service unavailable",
)


# Per-call retry policy
_RETRY_STATUS = (429, 500, 502, 503, 504)
_RETRY_MAX = 1
_RETRY_DELAY_MIN = 1.5
_RETRY_DELAY_MAX = 3.0


def _headers() -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {config.WAZZUP_API_KEY}",
        "Content-Type": "application/json",
    }
    return h


def is_configured() -> bool:
    """True если Wazzup API key задан. Не зависит от USE_WABA флага."""
    return bool(config.WAZZUP_API_KEY)


def classify_error(detail: str, http_status: Optional[int] = None) -> ErrorKind:
    """Классификация ошибок Wazzup по тексту ответа и HTTP-коду."""
    s = (detail or "").lower()
    if http_status in (401, 403):
        return ErrorKind.AUTH
    if any(k in s for k in _AUTH_KEYWORDS):
        return ErrorKind.AUTH
    if any(k in s for k in _PERMANENT_KEYWORDS):
        return ErrorKind.PERMANENT
    if any(k in s for k in _RATE_KEYWORDS):
        return ErrorKind.RATE_LIMIT
    if http_status and 500 <= http_status < 600:
        return ErrorKind.TRANSIENT
    if any(k in s for k in _TRANSIENT_KEYWORDS):
        return ErrorKind.TRANSIENT
    return ErrorKind.UNKNOWN


def _post_with_retry(url: str, payload: dict, timeout: int = 30) -> tuple[Optional[requests.Response], Optional[str]]:
    """POST с retry на transient ошибки (сеть ИЛИ HTTP 429/5xx).

    Возвращает (response, error_string). Если error_string не None — все попытки
    ушли в сеть без ответа. Если response есть — это финальный ответ сервера
    (даже если он 4xx/5xx), и caller должен сам решить что с ним делать.
    """
    last_exc: Optional[str] = None
    for attempt in range(_RETRY_MAX + 1):
        try:
            r = requests.post(url, json=payload, headers=_headers(), timeout=timeout)
            # Transient HTTP-коды → retry. Иначе возвращаем как есть.
            if r.status_code not in _RETRY_STATUS:
                return r, None
            last_exc = f"http {r.status_code} (attempt {attempt + 1}/{_RETRY_MAX + 1})"
        except requests.exceptions.Timeout as e:
            last_exc = f"timeout: {e}"
        except requests.exceptions.ConnectionError as e:
            last_exc = f"connection error: {e}"
        except requests.exceptions.RequestException as e:
            last_exc = f"request error: {e}"
        if attempt < _RETRY_MAX:
            time.sleep(random.uniform(_RETRY_DELAY_MIN, _RETRY_DELAY_MAX))
    return None, last_exc


def send_wazzup(
    *,
    channel_id: str,
    chat_id: str,
    chat_type: str = "whatsapp",
    text: Optional[str] = None,
    template_id: Optional[str] = None,
    template_values: Optional[list[str]] = None,
    content_uri: Optional[str] = None,
    request_id: Optional[str] = None,
) -> tuple[bool, Optional[str], str, Optional[int]]:
    """Отправить сообщение через Wazzup.

    Возвращает (ok, message_id, detail, http_status).

    Правило API: `text` и `contentUri` не могут быть переданы одновременно.
    Для cold outreach используй `template_id`. Для reply внутри 24ч окна —
    свободный `text`.

    chat_type ∈ {'whatsapp','telegram','instagram'} для совместимости API,
    но фактический transport определяется channelId.
    """
    if not is_configured():
        return False, None, "WAZZUP_API_KEY not configured", None
    if not channel_id:
        return False, None, "channel_id required", None
    if not chat_id:
        return False, None, "chat_id required", None
    if text and content_uri:
        return False, None, "cannot provide both text and contentUri", None
    if not (text or template_id or content_uri):
        return False, None, "one of text|templateId|contentUri required", None

    payload: dict = {
        "channelId": channel_id,
        "chatType": chat_type,
        "chatId": chat_id,
    }
    if text is not None:
        payload["text"] = text
    if template_id is not None:
        payload["templateId"] = template_id
        if template_values:
            payload["templateValues"] = template_values
    if content_uri is not None:
        payload["contentUri"] = content_uri
    if request_id is not None:
        payload["requestId"] = request_id

    url = f"{config.WAZZUP_BASE_URL}{config.WAZZUP_MESSAGE_SEND_PATH}"
    response, exc = _post_with_retry(url, payload)
    if response is None:
        detail = f"network error: {exc}"
        return False, None, detail, None

    http_status = response.status_code
    body_text = (response.text or "")[:1500]

    if 200 <= http_status < 300:
        # Wazzup returns messageId в ответе
        try:
            body = response.json()
        except ValueError:
            body = {}
        message_id = body.get("messageId") or body.get("id") or body.get("message_id")
        return True, message_id, body_text or "ok", http_status

    return False, None, body_text, http_status


def get_wazzup_channels() -> tuple[bool, str, list[dict]]:
    """Получить список подключённых каналов (WABA, TG, MAX, ...).

    Возвращает (ok, detail, channels).
    Каждый канал: {channelId, transport, scope, title}.
    """
    if not is_configured():
        return False, "WAZZUP_API_KEY not configured", []
    url = f"{config.WAZZUP_BASE_URL}{config.WAZZUP_CHANNELS_PATH}"
    try:
        r = requests.get(url, headers=_headers(), timeout=30)
    except requests.exceptions.RequestException as e:
        return False, f"network error: {e}", []
    if r.status_code == 200:
        try:
            body = r.json()
            # Wazzup может вернуть: {"channels": [...]}, [...] напрямую, или {"data": [...]}
            if isinstance(body, list):
                channels = body
            elif isinstance(body, dict):
                channels = body.get("channels") or body.get("data") or []
            else:
                channels = []
            return True, "ok", list(channels) if isinstance(channels, list) else []
        except ValueError:
            return False, f"invalid json: {r.text[:200]}", []
    return False, f"http {r.status_code}: {r.text[:200]}", []


def resolve_wazzup_chat(chat_id: str, channel_id: str) -> Optional[bool]:
    """Pre-check: существует ли chat на канале?

    Возвращает True/False если ответ определён, None если сеть/timeout
    (тогда sender должен fallthrough на try/send).
    """
    if not is_configured():
        return None
    # Wazzup не имеет отдельного endpoint для проверки chat.
    # Лучший pre-check — попробовать отправить пустое сообщение НЕЛЬЗЯ
    # (потратит Meta деньги + статус изменится).
    # Fallback: вернуть None (кэш остаётся) → sender идёт по try/send.
    # Реальная реализация pre-check для WABA — через webhook channel.qr_update
    # + history imports (Phase 5).
    return None


def subscribe_wazzup_webhook(url: str, subscriptions: dict) -> tuple[bool, str]:
    """Подписать webhook URL на события.

    subscriptions = {
        "messagesAndStatuses":      bool,
        "contactsAndDealsCreation": bool,
        "channelsUpdates":          bool,
        "templateStatus":           bool,
    }
    """
    if not is_configured():
        return False, "WAZZUP_API_KEY not configured"
    api_url = f"{config.WAZZUP_BASE_URL}{config.WAZZUP_WEBHOOKS_PATH}"
    payload = {"webhooksUri": url, "subscriptions": subscriptions}
    try:
        r = requests.patch(api_url, json=payload, headers=_headers(), timeout=30)
    except requests.exceptions.RequestException as e:
        return False, f"network error: {e}"
    if 200 <= r.status_code < 300:
        return True, "ok"
    return False, f"http {r.status_code}: {r.text[:300]}"


def verify_webhook_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """Проверка HMAC-SHA256 подписи webhook.

    Wazzup использует заголовок X-Signature или аналог. Точное имя зависит
    от настроек канала; мы поддерживаем несколько вариантов.

    Если WAZZUP_HMAC_SECRET не задан — возвращает True (signature verification
    отключена).
    """
    if not config.WAZZUP_VERIFY_SIGNATURE:
        return True
    if not config.WAZZUP_HMAC_SECRET:
        return True
    if not signature_header:
        return False
    try:
        expected = hmac.new(
            config.WAZZUP_HMAC_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        # Поддержка "sha256=<hex>" и просто "<hex>"
        received = signature_header.strip()
        if received.startswith("sha256="):
            received = received[len("sha256="):]
        return hmac.compare_digest(expected, received.lower())
    except Exception as e:
        log.warning(f"verify_webhook_signature error: {e}")
        return False


# --- Channel resolution helpers ---


def is_waba_channel(channel_transport: str) -> bool:
    """True если это WABA канал (transport в ответе get_wazzup_channels)."""
    if not channel_transport:
        return False
    return channel_transport.lower() in ("whatsapp", "waba", "whatsapp_business")


def is_personal_channel(channel_transport: str) -> bool:
    """True если это Personal-канал (TG/MAX/Instagram) через Wazzup QR."""
    if not channel_transport:
        return False
    return channel_transport.lower() in ("telegram", "max", "instagram", "viber", "vk", "avito")


# Кэш маппинга channelId → bulkmessage channel name ('telegram'/'max'/'waba'/'whatsapp').
# Заполняется лениво при первом webhook. Кэш процесса (в памяти).
_CHANNEL_NAME_CACHE: dict[str, str] = {}
_CHANNELS_FETCHED = False


def _fetch_channels_cached() -> list[dict]:
    """Получить список каналов один раз за процесс. Если API недоступен — []."""
    global _CHANNELS_FETCHED
    if _CHANNELS_FETCHED:
        return list(_CHANNEL_NAME_CACHE.values())  # type: ignore[return-value]
    _CHANNELS_FETCHED = True
    ok, _detail, channels = get_wazzup_channels()
    if not ok or not channels:
        return []
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        cid = ch.get("channelId")
        transport = (ch.get("transport") or "").lower()
        if not cid:
            continue
        # Маппинг transport → bulkmessage channel name.
        if transport in ("telegram", "tg", "tgapi"):
            name = "telegram"
        elif transport in ("max", "vk", "maxapi"):
            name = "max"
        elif transport in ("wapi", "whatsapp_business", "waba"):
            name = "waba"
        elif transport in ("whatsapp", "whapi"):
            name = "whatsapp"  # Personal WhatsApp через Wazzup QR
        else:
            name = "waba"  # неизвестный → считаем WABA (безопаснее для лимитов)
        _CHANNEL_NAME_CACHE[cid] = name
    return list(_CHANNEL_NAME_CACHE.values())


def channel_name_for(channel_id: str) -> Optional[str]:
    """По Wazzup channelId → bulkmessage channel name ('telegram'/'max'/'waba'/'whatsapp').

    None если канал неизвестен / API недоступен.
    Использует in-memory кэш, обновляемый при первом вызове.
    """
    if not channel_id:
        return None
    if channel_id in _CHANNEL_NAME_CACHE:
        return _CHANNEL_NAME_CACHE[channel_id]
    _fetch_channels_cached()
    return _CHANNEL_NAME_CACHE.get(channel_id)


def normalize_phone(phone) -> str:
    """Нормализация телефона в международный формат.

    WABA требует цифровой формат БЕЗ '+' и без ведущих 00.
    Пример: '+7 (999) 123-45-67' → '79991234567'.

    Поддерживаемые префиксы:
      - 8XXXXXXXXXX (11 цифр, РФ) → 7XXXXXXXXXX
      - 7XXXXXXXXXX (11 цифр, РФ) → как есть
      - +7XXXXXXXXXX → 7XXXXXXXXXX
      - 9XXXXXXXXX (9 цифр, Узбекистан) → 9989XXXXXXXXX
      - 10 цифр с 9 (РФ мобильные) → 7XXXXXXXXXX

    Невалидный/пустой вход → пустая строка (НЕ None).
    """
    if phone is None:
        return ""
    s = str(phone).strip()
    if not s:
        return ""
    digits = re.sub(r"\D+", "", s)
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 9:
        # Узбекистан (коды операторов: 90/91/93/94/95/97/98/99)
        digits = "998" + digits
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    return digits


def channel_id_for(channel: str) -> str:
    """Маппинг bulkmessage-канала ('telegram'/'max'/'whatsapp'/'waba') → UUID Wazzup.

    Возвращает пустую строку если Wazzup-канал не сконфигурирован.
    """
    return config.get_wazzup_channel_id_for(channel) or ""


def active_channels() -> list[str]:
    """Список bulkmessage-каналов, у которых есть сконфигурированный Wazzup channel_id.

    Только эти каналы доступны для отправки через Wazzup (единственный транспорт).
    """
    result: list[str] = []
    for ch in config.CHANNEL_DAILY_LIMITS:
        if config.CHANNEL_DAILY_LIMITS[ch] <= 0:
            continue
        if not channel_id_for(ch):
            continue
        result.append(ch)
    return result


# Map bulkmessage channel name → Wazzup chatType для /v3/message
CHANNEL_TO_CHAT_TYPE = {
    "telegram": "telegram",
    "max": "max",
    "whatsapp": "whatsapp",
    "waba": "whatsapp",  # WABA = WhatsApp Business API = chatType=whatsapp
}


def send_to_channel(
    channel: str,
    phone: str,
    text: str,
    *,
    template_id: Optional[str] = None,
    template_values: Optional[list] = None,
    request_id: Optional[str] = None,
) -> tuple[bool, Optional[str], str, Optional[int]]:
    """Унифицированная отправка через Wazzup для любого канала.

    Возвращает (ok, message_id, detail, http_status) — стандартный контракт
    отправки, чтобы sender.py работал с одним call-site для всех каналов.
    """
    channel_id = channel_id_for(channel)
    if not channel_id:
        return (
            False,
            None,
            f"channel {channel!r} has no WAZZUP_*_CHANNEL_ID configured",
            None,
        )
    chat_type = CHANNEL_TO_CHAT_TYPE.get(channel, channel)
    # Wazzup API принимает chat_id для WABA как digits-only (без '+')
    if channel == "waba":
        chat_id = normalize_phone(phone) or ""
    elif chat_type == "whatsapp":
        chat_id = normalize_phone(phone) or phone
    else:
        # TG/MAX — chat_id обычно равен phone, без нормализации
        chat_id = phone
    if not chat_id:
        return False, None, "phone normalization produced empty chat_id", None
    return send_wazzup(
        channel_id=channel_id,
        chat_id=chat_id,
        text=text,
        chat_type=chat_type,
        template_id=template_id,
        template_values=template_values,
        request_id=request_id,
    )
