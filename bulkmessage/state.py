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
            data.setdefault("waba_inbound_meta", {})  # phone → ts ответа КОНТАКТА
            # Phase 5.5: circuit breaker для повальных PERMANENT fail (сломанные templates)
            data.setdefault("_waba_consecutive_permanent", 0)
            # Phase 6: последний успешный канал (для channel-aware delay между контактами)
            data.setdefault("last_success_channel", "")  # "waba" | "telegram" | "max" | ""
            # Phase 8: доставлено (подтверждено вебхуком) отдельно от принятого.
            # accepted — это "запрос принят транспортом", оно НЕ значит доставку.
            data.setdefault("accepted_today", {})   # channel → count
            data.setdefault("delivered_today", {})  # channel → count (только по webhook)
            data.setdefault("failed_today", {})     # channel → count
            data.setdefault("_overload_streak", {})  # channel → подряд отказов перегрузки
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
        "waba_exhausted": {},
        "last_send_per_phone": {},
        "last_send_per_channel": {},
        "last_delivery_per_channel": {},
        "complaint_counts": {},
        "complaint_history": [],
        "halted_channels": {},
        "spintax_last_used": {},
        "waba_sent_meta": {},
        "waba_inbound_meta": {},
        "_waba_consecutive_permanent": 0,
        "last_success_channel": "",
        "accepted_today": {},
        "delivered_today": {},
        "failed_today": {},
        "_overload_streak": {},
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
        # Phase 8: счётчики доставки тоже живут в пределах дня
        state["accepted_today"] = {}
        state["delivered_today"] = {}
        state["failed_today"] = {}
    return state


def channel_sent_today(state: dict, channel: str) -> int:
    return state.get("sent_today", {}).get(channel, 0)


def increment_channel_sent(state: dict, channel: str) -> None:
    st = state.setdefault("sent_today", {})
    st[channel] = st.get(channel, 0) + 1


def channel_has_quota(state: dict, channel: str) -> bool:
    return channel_sent_today(state, channel) < config.CHANNEL_DAILY_LIMITS.get(channel, 0)


def reconcile_sent_from_db(state: dict, channel: str) -> dict:
    """Сверяет sent_today с атомарным счётчиком принятого в SQLite.

    При параллельной отправке WABA несколько потоков пишут state.json, и
    часть обновлений теряется — квота занижается. Источник истины теперь
    daily_counters (инкремент через UPSERT, гонки нет), поэтому после
    каждой отправки берём максимум из файла и БД.
    """
    try:
        from . import db as _db
        counters = _db.get_counters()
        accepted = int((counters.get(channel) or {}).get("accepted", 0))
    except Exception:
        return state
    if accepted > channel_sent_today(state, channel):
        state.setdefault("sent_today", {})[channel] = accepted
    return state


def all_quotas_exhausted(state: dict, channels: list[str]) -> bool:
    return not any(channel_has_quota(state, ch) for ch in channels)


def successful_today(state: dict) -> int:
    """Сколько УНИКАЛЬНЫХ контактов получили ≥1 успешную отправку сегодня."""
    return int(state.get("successful_today", 0))


def increment_successful(state: dict) -> None:
    """Вызывается когда контакт получил хотя бы 1 успешный канал."""
    state["successful_today"] = successful_today(state) + 1


# --- Phase 8: доставка vs принятие -----------------------------------------
# Ключевое различие, которого раньше не было в системе:
#   accepted  — транспорт (Meta/Wazzup) принял запрос. НЕ значит доставку.
#   delivered — вебхук status=delivered/read. Только это считается доставкой.
# Без этого счётчика «100 отправлено → 28 дошло» выглядело как 72 ошибки,
# хотя на самом деле это были недоставки, о которых система не узнавала.


def _bump(state: dict, bucket: str, channel: str) -> int:
    d = state.setdefault(bucket, {})
    d[channel] = int(d.get(channel, 0)) + 1
    return d[channel]


def record_accepted(state: dict, channel: str) -> int:
    """Запрос отправлен и принят транспортом (HTTP 2xx). Не доставка."""
    return _bump(state, "accepted_today", channel)


def record_delivered(state: dict, channel: str) -> int:
    """Вебхук подтвердил доставку. Это единственный честный счётчик."""
    return _bump(state, "delivered_today", channel)


def record_failed_delivery(state: dict, channel: str) -> int:
    """Вебхук сообщил об отказе доставки → контакт уходит в rescue."""
    return _bump(state, "failed_today", channel)


def channel_delivered_today(state: dict, channel: str) -> int:
    return int(state.get("delivered_today", {}).get(channel, 0))


def channel_accepted_today(state: dict, channel: str) -> int:
    return int(state.get("accepted_today", {}).get(channel, 0))


def delivered_total_today(state: dict) -> int:
    """Сколько сообщений реально доставлено сегодня по всем каналам."""
    return sum(int(v) for v in state.get("delivered_today", {}).values())


def accepted_total_today(state: dict) -> int:
    return sum(int(v) for v in state.get("accepted_today", {}).values())


def delivery_rate(state: dict) -> float:
    """Доля доставленных от принятых. None если принято 0 (избегаем /0)."""
    acc = accepted_total_today(state)
    if acc <= 0:
        return 0.0
    return delivered_total_today(state) / float(acc)


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


# --- WABA exhausted (долгоживущий маркер провала WABA) ---
# reachability_cache протухает за 24 часа (REACHABILITY_CACHE_TTL_HOURS). Для
# контакта, которому WABA отдал HTTP 2xx, но НЕ доставил, этого мало: на
# следующий день он снова получил бы один из 250 драгоценных WABA-слотов, хотя
# заведомо не доходит. При конверсии 36% это ~90 списанных в пустоту слотов в
# день и как раз тот эффект, из-за которого фазовая схема затевалась: WABA
# должен работать ТОЛЬКО со свежими контактами.
def mark_waba_exhausted(state: dict, phone: str, reason: str = "") -> None:
    """Исключает номер из WABA-пула на WABA_EXHAUSTED_TTL_DAYS дней."""
    from datetime import datetime, timezone
    bucket = state.setdefault("waba_exhausted", {})
    if not isinstance(bucket, dict):
        bucket = {}
        state["waba_exhausted"] = bucket
    bucket[phone] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "reason": str(reason or "")[:200],
    }


def is_waba_exhausted(state: dict, phone: str) -> bool:
    """True если номер уже исчерпал WABA и срок ещё не вышел."""
    from datetime import datetime, timezone, timedelta
    entry = (state.get("waba_exhausted") or {}).get(phone)
    if not isinstance(entry, dict):
        return False
    ts_str = entry.get("ts")
    if not ts_str:
        # Запись без метки времени считаем бессрочной: безопаснее потратить
        # слот, чем второй раз долбить номер, который уже не доходит.
        return True
    try:
        ts = datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ttl = timedelta(days=getattr(config, "WABA_EXHAUSTED_TTL_DAYS", 30))
    return datetime.now(timezone.utc) - ts <= ttl


def waba_exhausted_count(state: dict) -> int:
    """Сколько номеров сейчас выведено из WABA-пула."""
    bucket = state.get("waba_exhausted") or {}
    return sum(1 for ph in bucket if is_waba_exhausted(state, ph))



# --- Per-phone cooldown (Phase 3.5) ---
# ВАЖНО: cooldown хранится ПО ПАРЕ (номер, канал), а не только по номеру.
# Раньше был один timestamp на весь телефон, из-за чего отправка через ОДИН канал
# блокировала ВСЕ остальные на время cooldown этого канала. Реальный баг:
# WABA принял (HTTP 2xx) → проставили last_send_per_phone → rescue пошёл в SMS →
# is_phone_in_cooldown(phone, "sms") видел тот же timestamp и возвращал True
# (SMSRU_COOLDOWN_HOURS=96) → SMS отбрасывался, контакт молча терялся.
# Ровно тот сценарий, ради которого строится fallback.
#
# last_delivery_per_channel — время ПОДТВЕРЖДЁННОЙ доставки (из вебхука).
# Для каналов с вебхуком приоритет у него: неподтверждённая отправка не должна
# запирать контакт на 96 часов. Для SMS (fire-and-forget, вебхука нет)
# используется время отправки.
LAST_SEND_PER_CHANNEL = "last_send_per_channel"
LAST_DELIVERY_PER_CHANNEL = "last_delivery_per_channel"


def _scoped_ts(state: dict, bucket: str, phone: str, channel: str) -> str | None:
    m = state.get(bucket) or {}
    per_phone = m.get(phone)
    if isinstance(per_phone, dict):
        v = per_phone.get(channel)
        if v:
            return v
    return None


def get_last_send_per_phone(state: dict, phone: str) -> str | None:
    """Legacy-ключ: последняя отправка по любому каналу. Оставлен для совместимости."""
    v = (state.get("last_send_per_phone") or {}).get(phone)
    if v:
        return v
    m = state.get(LAST_SEND_PER_CHANNEL) or {}
    for per_phone in m.values():
        if isinstance(per_phone, dict) and per_phone:
            return max(per_phone.values())
    return None


def set_last_send_per_phone(
    state: dict, phone: str, ts_iso: str, channel: str | None = None
) -> None:
    """Фиксирует отправку. С channel — по паре (номер, канал), без — legacy."""
    if channel:
        state.setdefault(LAST_SEND_PER_CHANNEL, {}).setdefault(phone, {})[channel] = ts_iso
    else:
        state.setdefault("last_send_per_phone", {})[phone] = ts_iso


def record_delivery_confirmed(state: dict, phone: str, channel: str, ts_iso: str) -> None:
    """Вебхук подтвердил доставку — с этого момента считаем cooldown."""
    state.setdefault(LAST_DELIVERY_PER_CHANNEL, {}).setdefault(phone, {})[channel] = ts_iso


def _cooldown_hours_for(channel: str) -> int:
    if channel == "waba":
        return getattr(config, "PER_PHONE_COOLDOWN_HOURS_WABA", 0)
    if channel == "sms":
        # SMS — отдельный транспорт (SMS.RU), но поведение как у Personal:
        # контакт, до которого дошла SMS, не трогаем SMS-каналом ещё 96ч.
        return getattr(config, "SMSRU_COOLDOWN_HOURS", 96)
    return getattr(config, "PER_PHONE_COOLDOWN_HOURS_PERSONAL", 96)


def is_phone_in_cooldown(state: dict, phone: str, channel: str) -> bool:
    """True если phone ещё в cooldown ИМЕННО для этого канала.

    Порядок источников:
      1) подтверждённая доставка в этом канале (самый сильный сигнал)
      2) отправка в этом канале (для SMS — единственный сигнал, вебхука нет)
      3) legacy-ключ — только если пара (номер, канал) вообще не встречалась;
         применяется лишь к personal-каналам, потому что исторически этот
         timestamp ставили personal-отправки. SMS и WABA от legacy-значения
         намеренно не зависят — иначе старый баг вернётся.
    """
    from datetime import datetime, timezone, timedelta

    cooldown_h = _cooldown_hours_for(channel)
    if cooldown_h <= 0:
        return False

    last = _scoped_ts(state, LAST_DELIVERY_PER_CHANNEL, phone, channel)
    if not last:
        last = _scoped_ts(state, LAST_SEND_PER_CHANNEL, phone, channel)
    if not last:
        if channel in ("sms", "waba"):
            return False  # legacy не блокирует SMS/WABA
        last = (state.get("last_send_per_phone") or {}).get(phone)
    if not last:
        return False

    try:
        ts = datetime.fromisoformat(last)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
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


# --- Circuit breaker: серия отказов перегрузки, не связанных с жалобой ------
# PEER_FLOOD / FLOOD_WAIT / rate-limit не являются жалобами, поэтому
# complaint-логика их не ловит. Но по факту канал после серии таких отказов
# перестаёт работать: в базе это 520 PEER_FLOOD подряд у Telegram, после чего
# канал замолчал. Без отдельного счётчика sender продолжает в него стрелять
# и выжигает остаток дня.
_OVERLOAD_MARKERS = (
    "peer_flood", "peer flood", "flood_wait", "flood wait", "floodwait",
    "too many requests", "rate limit", "rate_limit", "ratelimit",
    "131048", "131056", "131049", "130429", "slow_down", "slowdown",
)

# --- Circuit breaker: провайдер перестал адресовать КОНТАКТЫ ВООБЩЕ ---------
# Живой случай 2026-10-07 (Telegram через Wazzup, канал 1c2ef310…): каждый
# контакт отклонялся с `BAD_CONTACT: Recipient with this chatId does not
# exist` — включая self-send на собственный номер 79771501819. Канал при этом
# «жив»: Wazzup отвечает HTTP 201, а отказ приходит эхом через пару секунд.
# Это НЕ перегрузка, поэтому автомат выше его не ловил: sender продолжал
# слать в мёртвый канал, жёг личный аккаунт и вял pacing 5–15 мин на
# каждую попытку вместо того, чтобы сразу уйти в MAX.
#
# Чем «мёртвый номер» отличается от «мёртвого канала»: один и тот же телефон
# повторно не считается. Если канал отверг N РАЗНЫХ контактов подряд — виноват
# канал, а не номера.
_PROVIDER_FAIL_MARKERS = (
    "bad_contact",
    "recipient with this chatid does not exist",
    "chat not found",
    "chat_id_invalid",
    "peer_id_invalid",
    "user is not found",
    "user_not_found",
    "channel invalid",
)


def _bump_overload(state: dict, channel: str) -> int:
    data = state.setdefault("_overload_streak", {})
    data[channel] = int(data.get(channel, 0)) + 1
    return data[channel]


def _clear_overload(state: dict, channel: str) -> None:
    data = state.get("_overload_streak") or {}
    if channel in data:
        data[channel] = 0


def is_overload_error(text: str) -> bool:
    t = str(text or "").lower()
    return any(m in t for m in _OVERLOAD_MARKERS)


def is_provider_fail_error(text: str) -> bool:
    """Канал не может адресовать получателя — провайдерский, а не контактный отказ."""
    t = str(text or "").lower()
    return any(m in t for m in _PROVIDER_FAIL_MARKERS)


def _bump_provider_fail(state: dict, channel: str, phone: str) -> int:
    """Считаем отказ, только если этот контакт в канале ещё не падал.

    Без этого мёртвый номер, повторно прошедший через rescue, накапливал бы
    серию и гасил исправный канал.
    """
    seen = state.setdefault("_provider_fail_seen", {})
    phones = seen.setdefault(channel, [])
    if phone and phone in phones:
        return provider_fail_streak(state, channel)
    if phone:
        phones.append(phone)
    streak = state.setdefault("_provider_fail_streak", {})
    streak[channel] = int(streak.get(channel, 0)) + 1
    return streak[channel]


def _clear_provider_fail(state: dict, channel: str) -> None:
    for key in ("_provider_fail_streak", "_provider_fail_seen"):
        data = state.get(key) or {}
        if channel in data:
            del data[channel]


def provider_fail_streak(state: dict, channel: str) -> int:
    return int((state.get("_provider_fail_streak") or {}).get(channel, 0))


def record_delivery_outcome(
    state: dict, channel: str, ok: bool, error_text: str = "", phone: str = ""
) -> bool:
    """Учёт исхода доставки + два circuit breaker'а.

    ok=True  → оба счётчика сбрасываются (канал отвечает).
    ok=False → отказ классифицируется:
      * перегрузка (PEER_FLOOD/FLOOD_WAIT/rate-limit) → серия растёт, при
        достижении `OVERLOAD_STREAK_LIMIT` канал в halt на 1 час;
      * провайдерский отказ (BAD_CONTACT и т.п.) → серия по РАЗНЫМ контактам
        растёт, при `PROVIDER_FAIL_STREAK_LIMIT` канал в halt на
        `PROVIDER_HALT_HOURS`. Такой канал Wazzup продолжает принимать
        (HTTP 201), но адресовать не способен никого.

    Возвращает True, если канал только что был остановлен.
    """
    if ok:
        _clear_overload(state, channel)
        _clear_provider_fail(state, channel)
        return False
    if is_provider_fail_error(error_text):
        limit = getattr(config, "PROVIDER_FAIL_STREAK_LIMIT", 3)
        if limit <= 0:            # 0 — проверка выключена
            return False
        streak = _bump_provider_fail(state, channel, phone)
        if streak >= limit:
            halt_channel(
                state, channel,
                hours=getattr(config, "PROVIDER_HALT_HOURS", 6),
            )
            _clear_provider_fail(state, channel)
            return True
        return False
    threshold = getattr(config, "OVERLOAD_STREAK_LIMIT", 5)
    if not is_overload_error(error_text):
        return False
    streak = _bump_overload(state, channel)
    if streak >= threshold:
        halt_channel(
            state, channel,
            hours=getattr(config, "OVERLOAD_HALT_HOURS", 1),
        )
        _clear_overload(state, channel)
        return True
    return False


def overload_streak(state: dict, channel: str) -> int:
    return int((state.get("_overload_streak") or {}).get(channel, 0))


def get_halt_threshold(channel: str) -> int:
    if channel == "waba":
        return getattr(config, "COMPLAINT_HALT_THRESHOLD_WABA", 3)
    if channel == "max":
        return getattr(config, "COMPLAINT_HALT_THRESHOLD_MAX", 2)
    if channel == "sms":
        # Жалоба на спам в SMS.RU блокирует зарегистрированное имя отправителя
        # у ВСЕХ операторов сразу — поэтому порог строже, чем у TG/WABA.
        return getattr(config, "COMPLAINT_HALT_THRESHOLD_SMS", 2)
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
    """True если КОНТАКТ писал НАМ в WABA за последние 24ч (можно free text).

    ВАЖНО: 24-часовое окно Meta открывает ответ КЛИЕНТА, а не наша отправка.
    Раньше здесь читался `waba_sent_meta`, который записывался на нашей
    собственной отправке (sender.py при успешном WABA). Из-за этого второе
    касание за сутки уходило free text человеку, который не отвечал, и Meta
    отвергала его с 131047. Теперь окно считается по `waba_inbound_meta`,
    который ставится в process_incoming_message.
    """
    from datetime import datetime, timezone, timedelta
    last = get_waba_inbound_meta(state, phone)
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


# --- WABA: раздельный учыт входящих и исходящих -----------------------------
# `waba_inbound_meta` — когда КОНТАКТ последний раз писал нам (открывает окно
# для free text). `waba_sent_meta` — когда МЫ последний раз писали ему
# (используется для ограничения 1 маркетингового сообщения в сутки на контакт,
# чтобы не ловить Meta 131049).


def get_waba_inbound_meta(state: dict, phone: str) -> str | None:
    return state.get("waba_inbound_meta", {}).get(phone)


def set_waba_inbound_meta(state: dict, phone: str, ts_iso: str) -> None:
    state.setdefault("waba_inbound_meta", {})[phone] = ts_iso


def waba_already_marketed_today(state: dict, phone: str) -> bool:
    """Мы уже отправляли этому контакту маркетинговое сообщение сегодня.

    Per-user marketing cap Meta — примерно 2 сообщения в сутки суммарно по всем
    брендам (ошибка 131049). Держим 1 в сутки: при 761 контакте этого хватает,
    а запас нужен на автоапгрейд и повторные попытки.
    """
    from datetime import datetime, timezone
    last = get_waba_sent_meta(state, phone)
    if not last:
        return False
    try:
        ts = datetime.fromisoformat(last)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    return ts.astimezone(now.tzinfo).date() == now.astimezone(now.tzinfo).date()
