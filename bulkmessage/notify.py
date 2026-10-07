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


def channel_switch(phone: str, frm: str, to: str, reason: str) -> None:
    """Контакт ушёл в другой канал — главное событие для владельца."""
    _send(
        f"🔀 {phone[-6:]}: {frm} → {to}\n    причина: {reason[:120]}",
        "INFO",
    )


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
        lines = []
        for channel, c in sorted(counters.items()):
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