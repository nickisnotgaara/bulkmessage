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
_THROTTLED: int = 0            # сообщений, срезанных потолком часа
_BUDGET_OUT: int = 0           # сообщений, срезанных потолком прогона
_PLAN: dict = {}               # план прогона: кого и сколько собирались


def _per_contact_allowed() -> bool:
    """Построчный режим и его бюджет на прогон.

    Владельцу нужен статус по каждому контакту, но на 250 контактах это 250
    сообщений в день. Поэтому режим включается флагом и ограничен потолком:
    исчерпали — дальше идёт агрегат, ничего не теряется молча.

    ⚠️ Срез больше не молчит: счётчик срезанных сообщений попадает в итог
    прогона. Обрыв лога без предупреждения читается как «всё хорошо» —
    а это худший вид молчания в системе, где по логу принимают решения.
    """
    if not int(getattr(config, "TG_PER_CONTACT", 1)):
        return False
    if _PER_CONTACT_USED >= int(getattr(config, "TG_PER_CONTACT_MAX", 120)):
        globals()["_BUDGET_OUT"] = _BUDGET_OUT + 1
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
        globals()["_THROTTLED"] = _THROTTLED + 1
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
    plan: Optional[dict] = None,
) -> None:
    """Один раз за прогон: план, квоты, режим, версия кода.

    Это ровно то, чего не хватало при разборе инцидентов: по одному
    алерту «канал умер» невозможно понять ни что запускали, ни какая
    была квота, ни был ли это боевой режим.

    ``plan`` — что прогон СОБИРАЕТСЯ сделать. Требование владельца
    2026-10-11: «в начале уточнить, каким контактам отправляет этот запуск».
    План не только печатается — он запоминается, чтобы в финале сравнить
    «собирались» с «получилось». Без этого сравнения отчёт показывает
    факты, а владелец не видит, не потерял ли что-то по дороге.
    """
    try:
        cur_channel_lines = []
        for ch in phase_order:
            cap = caps.get(ch, "?")
            cur_channel_lines.append(f"  • {ch}: кап {cap}")
        now = config.now_tz()
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
            f"Очередь отката: {rescue_pending}"
            + (f" | исчерпано: {exhausted_total}" if exhausted_total else "")
            + f"\nГоризонты: явный отказ → откат через "
            f"{getattr(config, 'REJECT_GRACE_MINUTES', 5)} мин, молчание → "
            f"через {getattr(config, 'UNKNOWN_GRACE_MINUTES', 720)} мин"
        )
        if plan:
            text += "\n\n" + _plan_block(plan)
            _PLAN.clear()
            _PLAN.update(plan)
            # Обратная совместимость: старые вызовы передавали только число
            # контактов. Оно обязано попасть в план, иначе в финале строка
            # «было 0 → стало 250» окажется ложью.
            _PLAN.setdefault("waba_planned", contacts_planned)
        else:
            _PLAN.clear()
            _PLAN["waba_planned"] = contacts_planned
        _send(text, "INFO")
    except Exception as e:  # noqa: BLE001
        log.debug(f"digest_start не отправился: {e}")


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 слот / 2 слота / 31 слот."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _plan_lines(plan: dict) -> list[str]:
    """Строки «кому отправляет этот запуск»."""
    rows = []
    if plan.get("pool_total") is not None:
        rows.append(f"  Пул контактов всего: {plan['pool_total']}")
    if plan.get("already_sent") is not None:
        rows.append(f"  Уже писали (исключены навсегда): {plan['already_sent']}")
    if plan.get("probe_planned") is not None:
        rows.append(f"  Проверка аккаунтов (разведка): {plan['probe_planned']}")
    if plan.get("personal_planned") is not None:
        _n = int(plan["personal_planned"])
        rows.append(
            f"  Личные каналы: {_n} {_plural(_n, 'слот', 'слота', 'слотов')}"
            f"  (окно {plan.get('personal_window_h', '?')} ч,"
            f" пауза {plan.get('min_gap_min', '?')}–"
            f"{plan.get('max_gap_min', '?')} мин)"
        )
    if plan.get("waba_planned") is not None:
        rows.append(f"  WABA: {plan['waba_planned']} контактов")
    if plan.get("rescue_planned") is not None:
        rows.append(f"  Откат (принятые без подтверждения): "
                    f"{plan['rescue_planned']}")
    return rows


def _plan_block(plan: dict) -> str:
    return "КОМУ ОТПРАВЛЯЕТ ЭТОТ ЗАПУСК\n" + "\n".join(_plan_lines(plan))


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
    who = f"{name} · {pretty_phone(phone)}" if name else pretty_phone(phone)
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
    who = f"{name} · {pretty_phone(phone)}" if name else pretty_phone(phone)
    tail = f"\n    {detail[:120]}" if detail else ""
    _send(f"📤 {who} → {channel} ✅ принято{tail}", "INFO")


def contact_failed(phone: str, name: str, channel: str, reason: str,
                   next_channels: str = "") -> None:
    """Статус по контакту: канал отказал, контакт уходит в откат."""
    if not _per_contact_allowed():
        return
    who = f"{name} · {pretty_phone(phone)}" if name else pretty_phone(phone)
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
    who = f"{name} · {pretty_phone(phone)}" if name else pretty_phone(phone)
    _send(f"⏭️ {who} · {channel}: {_human_reason(reason)}", "INFO")


# Человеческие подписи вердиктов разведки. Ключ — код из ProbeResult.verdict.
# 'unknown' намеренно НЕ называется «нет»: канал не ответил, и это не то же
# самое, что «аккаунта нет». Называть живой контакт «без аккаунта» опаснее,
# чем оставить его неизвестным (правило владельца 2026-10-10).
_VERDICT_RU = {
    "present": "есть аккаунт",
    "absent": "аккаунта нет",
    "unknown": "канал не ответил",
    "blocked": "заблокирован",
    "skipped": "пропущен",
}


def probe_miss(phone: str, name: str, verdicts: dict) -> None:
    """Проба не нашла ни одного личного мессенджера — по каждому контакту.

    Требование владельца 2026-10-11: «кто не имеет каких каналов» должно быть
    видно в боте так же, как в терминале. Раньше эта строка жила только в
    логе на уровне INFO и в личку не уходила вовсе — функция
    ``contact_skipped`` была написана, но не вызывалась ни разу.

    Формат: одна строка на контакт, все проверенные каналы сразу. Посылать
    по строке на каждый канал нельзя — на контакт без аккаунтов это даст
    две-три строки вместо одной и размножит шум вчетверо.
    """
    if not _per_contact_allowed():
        return
    who = f"{name} · {pretty_phone(phone)}" if name else pretty_phone(phone)
    if verdicts:
        parts = []
        for ch, v in verdicts.items():
            mark = {"present": "✅", "absent": "❌",
                    "unknown": "❔", "blocked": "🚫"}.get(v, "·")
            parts.append(f"{mark} {ch}: {_VERDICT_RU.get(v, v)}")
        detail = "; ".join(parts)
    else:
        detail = "проверок не было (каналы недоступны или потолок исчерпан)"
    _send(f"🚫 {who}\n    {detail}\n    → уходит в WABA", "INFO")


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


def pretty_phone(phone: str) -> str:
    """Номер в виде, пригодном ЧТОБЫ ПРОЧИТАТЬ: '+79031234567'.

    Требование владельца 2026-10-11: в логах номер должен быть в международном
    формате с плюсом — в базе он лежит без плюса (4434 из 4465 контактов
    начинаются с «7»), и без плюса его невозможно отличить от лида на
    паспорт.

    ⚠️ ЭТО ТОЛЬКО ОТОБРАЖЕНИЕ. В Wazzup уходит нормализованный номер БЕЗ
    плюса и без 00 (``wazzup.normalize_phone``), и трогать это нельзя: Meta
    отклоняет номер с плюсом. Функция ничего не отправляет.

    Число, которое не похоже на телефон, возвращается КАК ЕСТЬ. В базе
    встречаются строки вида «1288960948» — это peer id из Telegram, а не
    телефон. Приклеить к нему «+» значит показать владельцу номер, которого
    не существует; молчаливая подмена хуже, чем честный неформат.
    """
    raw = str(phone or "").strip()
    if not raw:
        return raw
    digits = raw[1:] if raw.startswith("+") else raw
    digits = "".join(ch for ch in digits if ch.isdigit())
    if not digits.isdigit():
        return raw
    # Россия: 8XXXXXXXXXX -> 7XXXXXXXXXX (8 — устаревший префикс)
    if len(digits) == 11 and digits.startswith("8"):
        return "+7" + digits[1:]
    # 11–15 цифр. ГРАНИЦА ПРОВЕРЕНА ПО ДАННЫМ: в баре реальные телефоны
    # занимают 11 (7XXX), 12 (998…) и 14 цифр, а всё что короче — это
    # мусор и peer id из Telegram («1288960948», «4780462888»). Приклеив
    # к ним плюс, мы бы показали владельцу номер, которого не существует.
    if 11 <= len(digits) <= 15:
        return "+" + digits
    return raw


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


def _plan_vs_fact(counters: dict) -> str:
    """Сравнение «собирались → получилось».

    Требование владельца 2026-10-11: «после завершения сравниваем как отчёт».
    Одних фактов мало — по ним не видно, потерял ли прогон контакты по дороге.
    Каждая строка показывает, что планировалось, что вышло и чем разошлось.
    """
    rows = []
    waba_p = _PLAN.get("waba_planned")
    # ⚠️ НЕ складываем accepted+delivered+rejected+unknown: delivered
    # входит в accepted (статус 'delivered' попадает и туда, и туда), и
    # сумма давала 480 попыток при плане 250 — план «перевыполнен» из
    # двойного счёта. Попытки = accepted, всё.
    waba_a = counters.get("waba", {}).get("accepted", 0)
    if waba_p is not None:
        gap = waba_p - waba_a
        rows.append(f"  WABA:   план {waba_p} → попыток {waba_a}"
                    + (f"  (−{gap} не отправлено)" if gap > 0
                       else "  (план перевыполнен)" if gap < 0
                       else "  (план выполнен)"))

    personal_p = _PLAN.get("personal_planned")
    if personal_p is not None:
        pa = sum(counters.get(c, {}).get("accepted", 0)
                 for c in ("telegram", "max"))
        gap = personal_p - pa
        rows.append(
            f"  личные: план {personal_p}"
            f" {_plural(int(personal_p), 'слот', 'слота', 'слотов')}"
            f" → отправлено {pa}"
            + (f"  (−{gap} "
               f"{_plural(gap, 'слот', 'слота', 'слотов')} не закрыт)"
               if gap > 0
               else "  (выполнено)" if gap == 0 else "  (выше плана)")
        )

    target = int(getattr(config, "TARGET_DELIVERED_PER_DAY", 0) or 0)
    if target:
        got = sum(counters.get(c, {}).get("delivered", 0)
                  for c in ("waba", "telegram", "max", "whatsapp"))
        gap = target - got
        mark = "🎯 ЦЕЛЬ ДОСТИГНУТА" if got >= target else f"не добрали {gap}"
        rows.append(f"  цель:   {target} доставленных → {got}   {mark}")

    return ("ПЛАН → ФАКТ\n" + "\n".join(rows)) if rows else ""


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
        )
        pvf = _plan_vs_fact(counters)
        if pvf:
            text += "\n\n" + pvf
        # Срез лога обязан быть виден. Если молчание вызвано потолком,
        # владелец должен знать, что часть контактов в бот не попала —
        # иначе он сделает вывод «всех проверили» из неполного лога.
        cut = _THROTTLED + _BUDGET_OUT
        if cut:
            text += (
                f"\n⚠️ ЛОГ ОБРЕЗАН: не отправлено {_BUDGET_OUT} "
                f"(потолок прогона) + {_THROTTLED} (потолок часа) "
                f"сообщений. Это НЕ «все контакты проверены»."
            )
        if extra:
            text += f"\n{extra}"
        _send(text, "INFO")
    except Exception as e:  # noqa: BLE001
        log.debug(f"summary не отправился: {e}")


def reset() -> None:
    """Сброс состояния модуля (для стендов и повторных прогонов)."""
    _SENT.clear()
    _THROTTLE.clear()
    _AGG.clear()
    _PLAN.clear()
    global _PER_CONTACT_USED, _THROTTLED, _BUDGET_OUT
    _PER_CONTACT_USED = 0
    _THROTTLED = 0
    _BUDGET_OUT = 0
    globals()["_PER_CONTACT_USED"] = 0