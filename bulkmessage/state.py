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
            # Phase 0+1: дополнительные ключи для cascade + anti-bank.
            data.setdefault("waba_tier", config.WAZZUP_TIER_DEFAULT)
            data.setdefault("waba_tier_updated_at", "")
            data.setdefault("reachability_cache", {})  # phone → {wa,tg,max,ts}
            data.setdefault("last_send_per_phone", {})  # phone → iso_ts
            data.setdefault("complaint_counts", {})  # channel → rolling count
            data.setdefault("complaint_history", [])  # [{channel,ts,kind}]
            data.setdefault("halted_channels", {})  # channel → iso_until
            data.setdefault("spintax_last_used", {})  # template_name → variant_idx
            data.setdefault("waba_sent_meta", {})  # phone → ts (для 24ч-окна)
            # Phase 5.5: circuit breaker для повальных PERMANENT fail (сломанные templates)
            data.setdefault("_waba_consecutive_permanent", 0)
            # Phase 6: последний успешный канал (для channel-aware delay между контактами)
            data.setdefault("last_success_channel", "")  # "waba" | "telegram" | "max" | ""
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
        "waba_tier": config.WAZZUP_TIER_DEFAULT,
        "waba_tier_updated_at": "",
        "reachability_cache": {},
        "last_send_per_phone": {},
        "complaint_counts": {},
        "complaint_history": [],
        "halted_channels": {},
        "spintax_last_used": {},
        "waba_sent_meta": {},
        "_waba_consecutive_permanent": 0,
        "last_success_channel": "",
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


# ============================================================
# Phase 0/1/3 additions: WABA tier, reachability cache, cooldown,
# complaint counts, halt, spintax, WABA 24h-window.
# ============================================================


def get_waba_tier(state: dict) -> int:
    return int(state.get("waba_tier", config.WAZZUP_TIER_DEFAULT))


def set_waba_tier(state: dict, tier: int) -> None:
    from datetime import datetime, timezone
    state["waba_tier"] = int(tier)
    state["waba_tier_updated_at"] = datetime.now(timezone.utc).isoformat()


def effective_waba_cap(state: dict) -> int:
    """Cap WABA по текущему tier (или override через env)."""
    tier = get_waba_tier(state)
    env_cap = config.CHANNEL_DAILY_LIMITS.get("waba", 250)
    tier_cap = config.WABA_TIER_LIMITS.get(tier, 250)
    return min(env_cap, tier_cap)


def channel_effective_cap(state: dict, channel: str) -> int:
    """Daily cap для канала с учётом headroom.

    Personal-каналы: hard cap = MAX_PERSONAL_DAILY_HARD_CAP.
    WABA: tier * (1 - headroom).
    """
    if channel == "waba":
        base = effective_waba_cap(state)
    else:
        base = config.CHANNEL_DAILY_LIMITS.get(channel, 0)
    headroom_pct = getattr(config, "HEADROOM_PCT", 25)
    effective = int(base * (100 - headroom_pct) / 100)
    return max(0, effective)


def channel_has_quota_with_headroom(state: dict, channel: str) -> bool:
    """True если канал ещё может отправить (с учётом headroom)."""
    sent = channel_sent_today(state, channel)
    effective = channel_effective_cap(state, channel)
    return sent < effective


# --- Reachability cache (Phase 2.1) ---


def get_cached_reachability(state: dict, phone: str) -> dict | None:
    """Возвращает кэш reachability для phone или None если нет/протух.

    Кэш: {wa: bool, tg: bool, max: bool, ts: iso_ts}.
    """
    from datetime import datetime, timezone, timedelta

    cache = state.get("reachability_cache", {})
    entry = cache.get(phone)
    if not isinstance(entry, dict):
        return None
    ts_str = entry.get("ts")
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - ts
    ttl = timedelta(hours=getattr(config, "REACHABILITY_CACHE_TTL_HOURS", 24))
    if age > ttl:
        return None
    return entry


def set_cached_reachability(
    state: dict, phone: str, reachability: dict[str, bool]
) -> None:
    from datetime import datetime, timezone
    cache = state.setdefault("reachability_cache", {})
    cache[phone] = {
        **reachability,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def mark_unreachable(state: dict, phone: str, channel: str) -> None:
    """Помечает phone×channel как unreachable в кэше."""
    entry = state.setdefault("reachability_cache", {}).get(phone)
    if not isinstance(entry, dict):
        entry = {}
        state["reachability_cache"][phone] = entry
    entry[channel] = False
    from datetime import datetime, timezone
    entry["ts"] = datetime.now(timezone.utc).isoformat()


# --- Per-phone cooldown (Phase 3.5) ---


def get_last_send_per_phone(state: dict, phone: str) -> str | None:
    return state.get("last_send_per_phone", {}).get(phone)


def set_last_send_per_phone(state: dict, phone: str, ts_iso: str) -> None:
    state.setdefault("last_send_per_phone", {})[phone] = ts_iso


def is_phone_in_cooldown(state: dict, phone: str, channel: str) -> bool:
    """True если phone ещё в cooldown для этого канала."""
    from datetime import datetime, timezone, timedelta

    last = get_last_send_per_phone(state, phone)
    if not last:
        return False
    try:
        ts = datetime.fromisoformat(last)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if channel == "waba":
        cooldown_h = getattr(config, "PER_PHONE_COOLDOWN_HOURS_WABA", 0)
    else:
        cooldown_h = getattr(config, "PER_PHONE_COOLDOWN_HOURS_PERSONAL", 96)
    if cooldown_h <= 0:
        return False
    age = datetime.now(timezone.utc) - ts
    return age < timedelta(hours=cooldown_h)


# --- Complaint counts (Phase 3.6) ---


def get_complaint_count(state: dict, channel: str) -> int:
    """Сколько жалоб в скользящем окне для канала."""
    history = state.get("complaint_history", [])
    if not history:
        return state.get("complaint_counts", {}).get(channel, 0)
    from datetime import datetime, timezone, timedelta
    window_h = getattr(config, "COMPLAINT_WINDOW_HOURS", 24)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_h)
    n = 0
    for h in history:
        if h.get("channel") != channel:
            continue
        try:
            ts = datetime.fromisoformat(h.get("ts", ""))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if ts >= cutoff:
            n += 1
    return n


def record_complaint(state: dict, channel: str, kind: str = "spam") -> None:
    from datetime import datetime, timezone
    history = state.setdefault("complaint_history", [])
    history.append({
        "channel": channel,
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
    })
    # Держим только последние 100 жалоб (cleanup)
    if len(history) > 100:
        state["complaint_history"] = history[-100:]


def is_channel_halted(state: dict, channel: str) -> bool:
    """True если канал halted (превышен лимит жалоб)."""
    from datetime import datetime, timezone
    halted = state.get("halted_channels", {})
    until = halted.get(channel)
    if not until:
        return False
    try:
        ts = datetime.fromisoformat(until)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return datetime.now(timezone.utc) < ts


def halt_channel(state: dict, channel: str, hours: int | None = None) -> None:
    from datetime import datetime, timezone, timedelta
    if hours is None:
        hours = getattr(config, "COMPLAINT_HALT_DURATION_HOURS", 24)
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    state.setdefault("halted_channels", {})[channel] = until.isoformat()


def get_halt_threshold(channel: str) -> int:
    if channel == "waba":
        return getattr(config, "COMPLAINT_HALT_THRESHOLD_WABA", 3)
    if channel == "max":
        return getattr(config, "COMPLAINT_HALT_THRESHOLD_MAX", 2)
    return getattr(config, "COMPLAINT_HALT_THRESHOLD_TG", 3)


def should_halt_after_complaint(state: dict, channel: str) -> bool:
    """После добавления жалобы — пора ли halt канал?"""
    n = get_complaint_count(state, channel)
    return n >= get_halt_threshold(channel)


# --- Spintax state (Phase 3.1) ---


def get_spintax_last(state: dict, template_name: str) -> int | None:
    return state.get("spintax_last_used", {}).get(template_name)


def set_spintax_last(state: dict, template_name: str, variant_idx: int) -> None:
    state.setdefault("spintax_last_used", {})[template_name] = variant_idx


# --- WABA 24h-window (Phase 1.4) ---


def get_waba_sent_meta(state: dict, phone: str) -> str | None:
    return state.get("waba_sent_meta", {}).get(phone)


def set_waba_sent_meta(state: dict, phone: str, ts_iso: str) -> None:
    state.setdefault("waba_sent_meta", {})[phone] = ts_iso


def is_waba_in_24h_window(state: dict, phone: str) -> bool:
    """True если контакт писал нам в WABA за последние 24ч (можно free text)."""
    from datetime import datetime, timezone, timedelta
    last = get_waba_sent_meta(state, phone)
    if not last:
        return False
    try:
        ts = datetime.fromisoformat(last)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    window_h = getattr(config, "WABA_FREE_TEXT_WINDOW_HOURS", 24)
    return datetime.now(timezone.utc) - ts < timedelta(hours=window_h)
