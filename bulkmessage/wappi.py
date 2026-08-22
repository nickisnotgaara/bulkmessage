"""Wappi.pro API client with smart error classification."""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

import requests

from . import config


class ErrorKind(Enum):
    PERMANENT = "permanent"     # recipient_not_found / invalid_phone / blocked — skip forever
    RATE_LIMIT = "rate"        # rate limit / 429 / temporary ban — long backoff
    TRANSIENT = "transient"    # network / 5xx / timeout — short backoff + retry
    AUTH = "auth"              # 401 / 403 / invalid token — fatal for channel
    UNKNOWN = "unknown"


_PERMANENT_KEYWORDS = (
    "not registered",
    "invalid phone",
    "recipient not found",
    "is not on whatsapp",
    "blocked",
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
)

_RATE_KEYWORDS = (
    "rate limit",
    "too many",
    "flood",
    "429",
    "retry later",
    "spam",
    "temporary ban",
)

_TRANSIENT_KEYWORDS = (
    "timeout",
    "timed out",
    "connection",
    "5xx",
    " 502 ",
    " 503 ",
    " 504 ",
    "request error",
    "internal error",
    "service unavailable",
)


def classify_error(detail: str, http_status: Optional[int] = None) -> ErrorKind:
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


def active_channels() -> list[str]:
    result: list[str] = []
    for ch in config.CHANNEL_DAILY_LIMITS:
        if config.CHANNEL_DAILY_LIMITS[ch] <= 0:
            continue
        creds = config.WAPPI_TOKENS.get(ch, {})
        if not creds.get("token") or str(creds.get("token", "")).startswith("ВАШ_"):
            continue
        result.append(ch)
    return result


def send_wappi(channel: str, phone: str, text: str) -> tuple[bool, Optional[str], str, Optional[int]]:
    """Отправляет сообщение. Возвращает (ok, message_id, detail, http_status)."""
    creds = config.WAPPI_TOKENS.get(channel)
    if not creds:
        return False, None, "channel not configured", None

    url = config.WAPPI_BASE_URL + config.WAPPI_SEND_PATHS[channel]
    headers = {"Authorization": creds["token"], "Content-Type": "application/json"}

    try:
        resp = requests.post(
            url,
            headers=headers,
            params={"profile_id": creds["profile_id"]},
            json={"recipient": phone, "body": text},
            timeout=(5, 15),
        )
    except requests.RequestException as e:
        return False, None, f"request error: {e}", None

    if resp.status_code != 200:
        return False, None, f"HTTP {resp.status_code}: {resp.text[:300]}", resp.status_code

    try:
        data = resp.json()
    except ValueError:
        return False, None, f"non-json response: {resp.text[:200]}", resp.status_code

    if data.get("status") in ("error", False):
        return False, None, str(data)[:300], resp.status_code

    message_id = data.get("message_id") or data.get("id")
    return True, message_id, str(data)[:300], resp.status_code


def fetch_wappi_status(channel: str, message_id: str) -> Optional[dict]:
    """Запрашивает у Wappi статус сообщения по ID. Возвращает dict или None."""
    creds = config.WAPPI_TOKENS.get(channel)
    if not creds:
        return None
    path = config.WAPPI_MESSAGE_GET_PATHS.get(channel)
    if not path:
        return None
    url = config.WAPPI_BASE_URL + path
    headers = {"Authorization": creds["token"]}
    try:
        resp = requests.get(
            url,
            headers=headers,
            params={"profile_id": creds["profile_id"], "message_id": message_id},
            timeout=20,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


# Пути для получения сообщений чата (используется реконсилятором для поиска пропущенных ответов)
WAPPI_MESSAGES_GET_PATHS = {
    "whatsapp": "/api/sync/messages/get",
    "telegram": "/tapi/sync/messages/get",
    "max": "/maxapi/sync/messages/get",
}


def fetch_chat_messages(
    channel: str,
    chat_id: str,
    since_iso: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Получает список сообщений из чата (Wappi: GET /.../sync/messages/get).

    Возвращает список словарей сообщений (только с полями) или [].
    chat_id — обычно номер телефона (нормализованный).
    since_iso — ISO-строка, с которой выводить сообщения; если None — берём последние.
    """
    creds = config.WAPPI_TOKENS.get(channel)
    if not creds:
        return []
    path = WAPPI_MESSAGES_GET_PATHS.get(channel)
    if not path:
        return []
    url = config.WAPPI_BASE_URL + path
    headers = {"Authorization": creds["token"]}
    params: dict = {
        "profile_id": creds["profile_id"],
        "chat_id": chat_id,
        "limit": int(limit),
        "order": "asc",  # от старых к новым — чтобы обработать по порядку
    }
    if since_iso:
        params["date"] = since_iso
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
    except requests.RequestException:
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    # Wappi может вернуть "messages" или "results"
    msgs = data.get("messages") or data.get("results") or []
    if not isinstance(msgs, list):
        return []
    return msgs


def channel_from_profile_id(profile_id: Optional[str]) -> Optional[str]:
    if not profile_id:
        return None
    for ch, creds in config.WAPPI_TOKENS.items():
        if creds.get("profile_id") == profile_id:
            return ch
    return None


def _extract_phone_from_jid(jid: Optional[str]) -> str:
    if not jid:
        return ""
    return normalize_phone(re.split(r"[@:/]", jid)[0])


def normalize_phone(raw) -> str:
    """Нормализует телефон к международному формату (только цифры, без +).

    Поддерживаемые префиксы:
      - 8XXXXXXXXXX (11 цифр, РФ) → 7XXXXXXXXXX
      - 7XXXXXXXXXX (11 цифр, РФ) → как есть
      - +7XXXXXXXXXX (11 цифр после +) → 7XXXXXXXXXX
      - 9XXXXXXXXX (9 цифр, Узбекистан) → 9989XXXXXXXXX
      - +998XXXXXXXXX (12 цифр после +) → 998XXXXXXXXX
      - 9XXXXXXXXX (10 цифр с 9, РФ мобильные) → 7XXXXXXXXXX

    Неизвестные форматы возвращаются как есть (только цифры), чтобы оператор
    мог разобраться вручную.
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    digits = re.sub(r"\D+", "", s)
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 9:
        # 9 цифр — это Узбекистан (коды операторов: 90, 91, 93, 94, 95, 97, 98, 99).
        # Не УЗ-номер начинается с 9 маловероятен. Префикс 998.
        digits = "998" + digits
    if len(digits) == 10 and digits.startswith("9"):
        # 10 цифр с 9 — российский мобильный (9XX-XXX-XX-XX)
        digits = "7" + digits
    return digits


def extract_phone_from_from(from_field: Optional[str]) -> str:
    return _extract_phone_from_jid(from_field)


def extract_phone_from_chat(chat_id: Optional[str]) -> str:
    return _extract_phone_from_jid(chat_id)


def wappi_status_to_local(status: Optional[str]) -> str:
    if not status:
        return "sent"
    s = str(status).lower()
    if s == "read":
        return "read"
    if s == "delivered":
        return "delivered"
    if s in ("pending", "queued", "sent"):
        return "sent"
    if s in ("undelivered", "failed", "error", "temporary ban"):
        return "failed"
    return s
