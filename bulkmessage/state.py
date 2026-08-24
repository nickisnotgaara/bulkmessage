"""Persistent broadcast state (resume, daily quotas)."""

from __future__ import annotations

import json
from pathlib import Path

from . import config


def load_state() -> dict:
    p = Path(config.STATE_PATH)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            # Обратная совместимость: гарантируем наличие нужных ключей
            data.setdefault("sent_today", {})
            data.setdefault("contacts_today", 0)
            data.setdefault("date", "")
            data.setdefault("last_index", 0)
            data.setdefault("successful_today", 0)
            data.setdefault("attempts_today", 0)
            data.setdefault("target_today", config.TARGET_SUCCESS_PER_DAY)
            return data
        except Exception:
            pass
    return {
        "sent_today": {},
        "contacts_today": 0,
        "date": "",
        "last_index": 0,
        "successful_today": 0,
        "attempts_today": 0,
        "target_today": config.TARGET_SUCCESS_PER_DAY,
    }


def save_state(state: dict) -> None:
    Path(config.STATE_PATH).write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )


def reset_daily_if_new_day(state: dict) -> dict:
    # Считаем "сегодня" в настроенной TZ (BULK_TIMEZONE, по умолчанию Europe/Moscow),
    # а не в локальной TZ машины.
    today = config.now_tz().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state["date"] = today
        state["sent_today"] = {}
        state["contacts_today"] = 0
        state["last_index"] = 0
        state["successful_today"] = 0
        state["attempts_today"] = 0
        state["target_today"] = config.TARGET_SUCCESS_PER_DAY
    return state


def channel_sent_today(state: dict, channel: str) -> int:
    return state.get("sent_today", {}).get(channel, 0)


def increment_channel_sent(state: dict, channel: str) -> None:
    st = state.setdefault("sent_today", {})
    st[channel] = st.get(channel, 0) + 1


def channel_has_quota(state: dict, channel: str) -> bool:
    return channel_sent_today(state, channel) < config.CHANNEL_DAILY_LIMITS.get(channel, 0)


def all_quotas_exhausted(state: dict, channels: list[str]) -> bool:
    return not any(channel_has_quota(state, ch) for ch in channels)


def successful_today(state: dict) -> int:
    """Сколько УНИКАЛЬНЫХ контактов получили ≥1 успешную отправку сегодня."""
    return int(state.get("successful_today", 0))


def increment_successful(state: dict) -> None:
    """Вызывается когда контакт получил хотя бы 1 успешный канал."""
    state["successful_today"] = successful_today(state) + 1


def attempts_today(state: dict) -> int:
    """Сколько контактов пытались обработать сегодня (вкл. failed)."""
    return int(state.get("attempts_today", 0))


def increment_attempts(state: dict) -> None:
    state["attempts_today"] = attempts_today(state) + 1


def target_reached(state: dict) -> bool:
    """True если достигли TARGET_SUCCESS_PER_DAY."""
    return successful_today(state) >= int(state.get("target_today", config.TARGET_SUCCESS_PER_DAY))


def max_attempts_reached(state: dict) -> bool:
    """Safety cap: если сделали слишком много попыток за день."""
    return attempts_today(state) >= config.MAX_ATTEMPTS_PER_DAY


def contacts_processed_today(state: dict) -> int:
    """Сколько УНИКАЛЬНЫХ контактов обработано сегодня (любые каналы)."""
    return int(state.get("contacts_today", 0))


def increment_contacts_processed(state: dict) -> None:
    state["contacts_today"] = contacts_processed_today(state) + 1
