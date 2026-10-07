"""Прозрачность рассылки в Telegram (план «устойчивый каскад», этап 5).

Владелец принимает решения по фактам, а факты раньше приходили в личку
только как «что-то сломалось» — без контекста. Три точки наблюдаемости:

1. `digest_start()` — один раз за прогон: что за каналы, сколько квоты,
   сколько контактов в работе, какой режим, какая версия кода.
2. `event()` — только значимые вещи: переключение канала по контакту,
   halt, пропуск фазы, исчерпание лимита, молчание вебхука, остановка.
3. `summary()` — итог прогона по каждому каналу: принято / доставлено /
   отказ / неизвестно / в откате / исчерпано.

Успехи НЕ летят построчно: иначе на 200 контактах будет 200+ сообщений в
день, и сам бот станет шумом. Вместо этого — агрегат раз в TG_DIGEST_EVERY.
Всё ограничено TG_MAX_MESSAGES_PER_HOUR, чтобы канал отчётов не стал
вторым источником спама.

Тестовые/стендовые прогоны (`test_*.py`, `bm_*.py`) подавляются в tglog,
поэтому весь этот модуль на них молчит сам по себе.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from . import config
from . import tglog

log = logging.getLogger("notify")

# Модуль не должен падать, если что-то пошло не так: уведомления — не
# критичный путь, их обрыв не должен ронять рассылку.
_SENT: list = []
_THROTTLE: list = []          # таймстемпы отправок за последний час
_AGG: dict = defaultdict(int)  # успехи, ещё не отправленные агрегатом
_PER_CONTACT_USED: int = 0     # построчных сообщений в этом прогоне


def _per_contact_allowed() -> bool:
    """Построчный режим и его бюджет на прогон.

    Владельцу нужен статус по каждому контакту, но на 250 контактах это 250
    сообщений в день. Поэтому режим включается флагом и ограничен потолком:
    исчерпали — дальше идёт агрегат, ничего не теряется молча.
    """
    if not int(getattr(config, "TG_PER_CONTACT", 1)):
        return False
    if _PER_CONTACT_USED >= int(getattr(config, "TG_PER_CONTACT_MAX", 120)):
        return False
    globals()["_PER_CONTACT_USED"] = _PER_CONTACT_USED + 1
    return True


def _prune_throttle() -> None:
    cutoff = time.time() - 3600
    while _THROTTLE and _THROTTLE[0] < cutoff:
        _THROTTLE.pop(0)


def _allowed() -> bool:
    """Не чаще TG_MAX_MESSAGES_PER_HOUR сообщений в личку."""
    _prune_throttle()
    limit = int(getattr(config, "TG_MAX_MESSAGES_PER_HOUR", 60) or 60)
    if len(_THROTTLE) >= limit:
        return False
    _THROTTLE.append(time.time())
    return True


def _send(text: str, level: str = "INFO") -> bool:
    # На тестовых прогонах tglog уже подавлен — считать такие «отправки» было
    # бы враньём в своих же счётчиках.
    if tglog.is_suppressed():
        return False
    if not _allowed():
        log.debug(f"tg-уведомление пропущено (rate-limit): {text[:60]}…")
        return False
    try:
        tglog.send(text, level)
        _SENT.append((datetime.now(timezone.utc).isoformat(), level))
        return True
    except Exception as e:  # noqa: BLE001 — уведомление не должно ронять прогон
        log.debug(f"tg-уведомление не отправлено: {e}")
        return False


def sent_count() -> int:
    return len(_SENT)


# ---------------------------------------------------------------------------
# 1. Стартовый дайджест
# ---------------------------------------------------------------------------


def digest_start(
    phase_order: list,
    caps: dict,
    contacts_planned: int,
    rescue_pending: int = 0,
    exhausted_total: int = 0,
    waba_tier: Optional[int] = None,
    code_version: str = "unknown",
) -> None:
    """Один раз за прогон: план, квоты, режим, версия кода.

    Это ровно то, чего не хватало при разборе инцидентов: по одному
    алерту «канал умер» невозможно понять ни что запускали, ни какая
    была квота, ни был ли это боевой режим.
    """
    try:
        cur_channel_lines = []
        for ch in phase_order:
            cap = caps.get(ch, "?")
            cur_channel_lines.append(f"  • {ch}: кап {cap}")
        now = datetime.now(timezone.utc).astimezone()
        dry = "DRY-RUN 💨 (реальных сообщений нет)" if getattr(
            config, "DRY_RUN", False
        ) else "БОЕВОЙ 🔥 (реальные сообщения)"
        text = (
            f"🚀 СТАРТ ПРОГОНА {now:%Y-%m-%d %H:%M %Z}\n"
            f"Режим: {dry}\n"
            f"Код: {code_version}\n"
            f"Каналы: {' → '.join(phase_order)}\n"
            + "\n".join(cur_channel_lines)
            + f"\n  • WABA tier: {waba_tier if waba_tier is not None else '?'}\n"
            f"Контактов в работе: {contacts_planned}\n"
            f"Очередь отката: {rescue_pending}"
            + (f" | исчерпано: {exhausted_total}" if exhausted_total else "")
            + f"\nГоризонты: явный отказ → откат через "
            f"{getattr(config, 'REJECT_GRACE_MINUTES', 5)} мин, молчание → "
            f"через {getattr(config, 'UNKNOWN_GRACE_MINUTES', 720)} мин"
        )
        _send(text, "INFO")
    except Exception as e:  # noqa: BLE001
        log.debug(f"digest_start не отправился: {e}")


# ---------------------------------------------------------------------------
# 2. События
# ---------------------------------------------------------------------------


def event(text: str, level: str = "WARNING") -> None:
    """Значимое событие: переключение канала, halt, пропуск фазы."""
    _send(text, level)


def channel_switch(phone: str, frm: str, to: str, reason: str,
                   name: str = "") -> None:
    """Контакт ушёл в другой канал — главное событие для владельца.

    Раньше сюда уходила строка вроде «🔀 296118091: waba → telegram → max,
    причина: молчание». По ней невозможно было понять, КТО контакт, что с ним
    случилось и — главное — что отправки в следующий канал ещё не было:
    «в очередь» и «отправлено» визуально неразличимы.
    """
    who = f"{name} · {phone}" if name else phone
    _send(
        f"🔀 {who}\n"
        f"    {frm} НЕ доставил → ставлю в очередь отката → {to}\n"
        f"    причина: {reason[:140]}",
        "INFO",
    )


def contact_sent(phone: str, name: str, channel: str, detail: str = "") -> None:
    """Статус по контакту: отправка принята транспортом."""
    if not _per_contact_allowed():
        return
    who = f"{name} · {phone}" if name else phone
    tail = f"\n    {detail[:120]}" if detail else ""
    _send(f"📤 {who} → {channel} ✅ принято{tail}", "INFO")


def contact_failed(phone: str, name: str, channel: str, reason: str,
                   next_channels: str = "") -> None:
    """Статус по контакту: канал отказал, контакт уходит в откат."""
    if not _per_contact_allowed():
        return
    who = f"{name} · {phone}" if name else phone
    nxt = f" → откат: {next_channels}" if next_channels else " → откат"
    _send(
        f"📤 {who} → {channel} ❌ НЕ доставил{nxt}\n"
        f"    {_human_reason(reason)}",
        "WARNING",
    )


def contact_skipped(phone: str, name: str, channel: str, reason: str) -> None:
    """Контакт не трогали: причина видна внятно, а не кодом."""
    if not _per_contact_allowed():
        return
    who = f"{name} · {phone}" if name else phone
    _send(f"⏭️ {who} · {channel}: {_human_reason(reason)}", "INFO")


_REASON_RU = (
    ("no_delivery_confirmation", "нет подтверждения доставки (вебхук молчит)"),
    ("silent_no_confirmation", "молчание канала, подтверждения нет"),
    ("delivery_rejected", "канал явно отказал в доставке"),
    ("delivery_failed", "канал отклонил сообщение"),
    ("131049", "Meta: контакт уже получил маркетинг сегодня"),
    ("marketing", "Meta не разрешает шаблон «Маркетинг» этому контакту"),
    ("CHANNEL_MAX_PHONE_NOT_OCCUPIED", "номер не заведён в MAX"),
    ("CHAT_WRITE_FORBIDDEN", "Telegram: чат недоступен отправителю"),
    ("PEER_FLOOD", "Telegram: слив ограничений (холодная рассылка)"),
    ("BUILD_TEMPLATE_ERROR", "Meta: неверный шаблон"),
    ("waba_exhausted", "контакт исчерпан в WABA"),
    ("bad_phones", "номер помечен мёртвым для этого канала"),
    ("safe_hours", "вне окна безопасного времени"),
    ("cooldown", "контакт ещё на cooldown в этом канале"),
    ("already_sent", "сегодня уже отправляли"),
)


def _human_reason(reason: str) -> str:
    """Переводит технический код отказа в то, что владелец может прочитать.

    В личку уходило «молчание: no_delivery_confirmation» — по этому нельзя
    было понять, что делать. Здесь та же причина человеческим языком.
    """
    raw = (reason or "").strip()
    if not raw:
        return "причина не указана"
    low = raw.lower()
    for needle, human in _REASON_RU:
        if needle.lower() in low:
            return human
    return raw[:140]


def counted_success(channel: str, n: int = 1) -> None:
    """Успех — копим и шлём агрегатом, чтобы не засорять личку."""
    _AGG[channel] += n
    every = int(getattr(config, "TG_DIGEST_EVERY", 25) or 25)
    if _AGG[channel] >= every:
        done = _AGG[channel]
        _AGG[channel] = 0
        _send(f"✅ {channel}: ещё {done} принято (всего в этом блоке)", "INFO")


def flush_successes() -> None:
    for channel, n in list(_AGG.items()):
        if n:
            _send(f"✅ {channel}: {n} принято", "INFO")
            _AGG[channel] = 0


# ---------------------------------------------------------------------------
# 3. Итог прогона
# ---------------------------------------------------------------------------


def summary(counters: dict, rescue_pending: int, exhausted: int,
            elapsed_sec: float, extra: Optional[str] = None) -> None:
    """Итог по каждому каналу из daily_counters (источник истины — SQLite)."""
    try:
        flush_successes()
        hidden = set(getattr(config, "TG_HIDE_CHANNELS", ()) or ())
        lines = []
        for channel, c in sorted(counters.items()):
            if channel in hidden:
                continue
            lines.append(
                f"  • {channel}: принято {c['accepted']}, "
                f"доставлено {c['delivered']}, отказ {c['rejected']}, "
                f"неизвестно {c['unknown']}"
            )
        mins = int(elapsed_sec // 60)
        text = (
            f"🏁 ИТОГ ПРОГОНА ({mins} мин)\n"
            + ("\n".join(lines) if lines else "  (отправок не было)")
            + f"\nВ очереди отката: {rescue_pending}"
            + f"\nИсчерпано (стоп автоматики): {exhausted}"
            + (f"\n{extra}" if extra else "")
        )
        _send(text, "INFO")
    except Exception as e:  # noqa: BLE001
        log.debug(f"summary не отправился: {e}")


def reset() -> None:
    """Сброс состояния модуля (для стендов и повторных прогонов)."""
    _SENT.clear()
    _THROTTLE.clear()
    _AGG.clear()
    globals()["_PER_CONTACT_USED"] = 0