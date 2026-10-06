"""Отчёт по воронке доставки: сколько РЕАЛЬНО доставлено сегодня.

Зачем: `successful_today` в state считает ПРИНЯТИЕ (HTTP 2xx), а не доставку.
Из-за этого лог отправителя рапортовал «ЦЕЛЬ ДОСТИГНУТА 200/200», хотя
доставок могло быть существенно меньше. Этот отчёт показывает честные числа:
принято → доставлено → отказано, по каждому каналу, плюс rescue-очередь.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bulkmessage import config, db, state  # noqa: E402

_CH_ORDER = ("waba", "telegram", "max", "whatsapp", "sms")


def _row(label: str, accepted: int, delivered: int, failed: int, cap: int) -> str:
    rate = (delivered / accepted * 100.0) if accepted else 0.0
    used = f"{accepted}/{cap}" if cap else str(accepted)
    return (
        f"  {label:<10} принято {accepted:>4}  доставлено {delivered:>4}  "
        f"отказов {failed:>4}  конверсия {rate:>5.1f}%  капа {used:>9}"
    )


def _measured_conversion(st: dict) -> float | None:
    """Реальная конверсия доставки по данным СЕГОДНЯШНЕГО дня.

    Возвращает delivered_today / accepted_today по всем каналам, либо None,
    если подтверждённых доставок ещё не было.

    Раньше здесь стояла константа 0.70 — выдуманная, не измеренная. Из-за
    неё прогноз показывал 122 доставленных в день, хотя единственный
    зафиксированный реальный прогон WABA дал 28%. Прогноз на несуществующей
    константе опаснее отсутствия прогноза: он выглядит как факт.
    """
    dlv = st.get("delivered_today") or {}
    acc = st.get("accepted_today") or {}
    d = sum(int(v) for v in dlv.values() if isinstance(v, (int, float)))
    a = sum(int(v) for v in acc.values() if isinstance(v, (int, float)))
    if d <= 0 or a <= 0:
        return None
    return min(1.0, d / a)


# Конверсии каналов. WABA и Telegram — ХОЛОДНАЯ рассылка, реальная отдача
# неизвестна до первого прогона с вебхуком, поэтому показываем вилку, а не
# одно число. SMS — единственный канал, где «принято» практически равно
# «доставлено»: оператор отдаёт статус по номеру, а Meta — нет.
_WABA_COLD_RANGE = (0.28, 0.60)   # низ: зафиксированные 28% отправки. верх: гипотеза
_TG_COLD_RANGE = (0.40, 0.60)     # исторически 41% без учёта флуда
_SMS_DELIVERY = 0.85


def _forecast(st: dict) -> tuple:
    """Прогноз достижимого объёма ДОСТАВКИ: вилка по конверсиям.

    Считает: эффективный кап канала × конверсия доставки. WABA/Telegram —
    холодная рассылка, их реальная конверсия неизвестна, поэтому возвращаем
    диапазон [низкий, высокий] вместо единственного числа.

    Возвращает: (low, high, parts)
    """
    try:
        waba_cap = state.channel_effective_cap(st, "waba")
    except Exception:
        waba_cap = 0
    try:
        sms_cap = state.channel_effective_cap(st, "sms")
    except Exception:
        sms_cap = 0
    tg_eff = 10   # реалистичный потолок TG: 22 по капту, но флуд убивает выше
    max_eff = 0   # MAX в холодном каскаде = 0: личный аккаунт, бан необратим

    # Если за сегодня уже есть подтверждённые доставки — верю им, а не вилке.
    measured = _measured_conversion(st)
    if measured is not None:
        waba_lo, waba_hi = measured, measured
        basis = f"по факту сегодня: {measured * 100:.0f}%"
    else:
        waba_lo, waba_hi = _WABA_COLD_RANGE
        basis = "холодная рассылка, вилка (зафиксировано 28% на 100 отправок)"

    parts = []
    low = high = 0
    d_lo, d_hi = int(waba_cap * waba_lo), int(waba_cap * waba_hi)
    parts.append(f"waba {waba_cap}→{d_lo}–{d_hi}")
    low += d_lo
    high += d_hi
    d_lo, d_hi = int(tg_eff * _TG_COLD_RANGE[0]), int(tg_eff * _TG_COLD_RANGE[1])
    parts.append(f"telegram {tg_eff}→{d_lo}–{d_hi}")
    low += d_lo
    high += d_hi
    if config.SMSRU_ENABLED and sms_cap:
        d = int(sms_cap * _SMS_DELIVERY)
        parts.append(f"sms {sms_cap}→{d}")
        low += d
        high += d
    return low, high, f"{', '.join(parts)}  [WABA: {basis}]"



def build_report() -> str:
    st = state.load_state()
    st = state.reset_daily_if_new_day(st)

    acc = st.get("accepted_today", {}) or {}
    dlv = st.get("delivered_today", {}) or {}
    flt = st.get("failed_today", {}) or {}

    out: list[str] = []
    out.append("=" * 70)
    out.append(" bulkmessage — ВОРОНКА ДОСТАВКИ")
    out.append(f" дата (МСК): {st.get('date', '—')}   DRY_RUN={bool(config.DRY_RUN)}")
    out.append("=" * 70)
    out.append("")
    out.append("  Канал          Принято   Доставлено  Отказов   Конверсия   Капа")
    out.append("  " + "-" * 66)

    for ch in _CH_ORDER:
        a, d, f = int(acc.get(ch, 0)), int(dlv.get(ch, 0)), int(flt.get(ch, 0))
        if not (a or d or f):
            continue
        try:
            cap = state.channel_effective_cap(st, ch)
        except Exception:
            cap = 0
        out.append(_row(ch, a, d, f, cap))

    a_tot, d_tot, f_tot = (
        sum(int(v) for v in acc.values()),
        sum(int(v) for v in dlv.values()),
        sum(int(v) for v in flt.values()),
    )

    if a_tot == 0 and d_tot == 0:
        out.append("  (сегодня ещё не было отправок)")
    else:
        out.append("  " + "-" * 66)
        out.append(_row("ВСЕГО", a_tot, d_tot, f_tot, 0))
    out.append("")

    # Цель дня: сравниваем ЧЕСТНУЮ доставку, а не принятие
    target = int(st.get("target_today", config.TARGET_SUCCESS_PER_DAY))
    out.append(f"  Цель дня (TARGET_SUCCESS_PER_DAY): {target}")
    out.append(
        f"  ДОСТАВЛЕНО: {d_tot}/{target}"
        + ("   ✅ цель по доставке достигнута"
           if d_tot >= target else f"   (не хватает {target - d_tot})")
    )
    if a_tot and d_tot < a_tot:
        pend = a_tot - d_tot - f_tot
        if pend > 0:
            out.append(
                f"  ⏳ без подтверждения: {pend} — ждём вебхук "
                f"или сработает сторож через "
                f"{config.CONFIRM_TIMEOUT_MINUTES} мин"
            )
    out.append("")

    # Rescue-очередь
    try:
        with db.db_conn() as conn:
            pend_rq = conn.execute(
                "SELECT COUNT(*) FROM rescue_queue WHERE status = 'pending'"
            ).fetchone()[0]
            done_rq = conn.execute(
                "SELECT COUNT(*) FROM rescue_queue WHERE rescued_channel IS NOT NULL"
            ).fetchone()[0]
    except Exception:
        pend_rq = done_rq = 0
    out.append("  Rescue-очередь (перехват недоставки):")
    out.append(f"    в ожидании: {pend_rq}   уже спасено: {done_rq}")

    # Пофазовая воронка. Решение №3 плана: WABA работает только со свежими,
    # провалы уходят в очередь. Поэтому важно видеть не только «сколько спасено»,
    # но и сколько контактов выведено из WABA-пула и сколько ещё ждёт SMS.
    try:
        with db.db_conn() as conn:
            waba_fail_pend = conn.execute(
                "SELECT COUNT(*) FROM rescue_queue WHERE status='pending' "
                "AND original_channel='waba'"
            ).fetchone()[0]
            waba_fail_done = conn.execute(
                "SELECT COUNT(*) FROM rescue_queue WHERE rescued_channel IS NOT NULL "
                "AND original_channel='waba'"
            ).fetchone()[0]
    except Exception:
        waba_fail_pend = waba_fail_done = 0
    try:
        _exhausted = state.waba_exhausted_count(st)
    except Exception:
        _exhausted = 0
    out.append("")
    out.append("  Фазовая воронка (WABA → Telegram → MAX):")
    sent = st.get("sent_today") or {}
    out.append(
        f"    фаза 1 WABA     : отправлено "
        f"{int(sent.get('waba', 0) or 0)}"
        f"/{state.channel_effective_cap(st, 'waba')}"
    )
    out.append(
        f"    фаза 2 Telegram : отправлено "
        f"{int(sent.get('telegram', 0) or 0)}"
        f"/{state.channel_effective_cap(st, 'telegram')}"
    )
    out.append(
        f"    фаза 3 MAX      : отправлено "
        f"{int(sent.get('max', 0) or 0)}"
        f"/{state.channel_effective_cap(st, 'max')}"
    )
    out.append(f"    провалов WABA спасено откатом : {waba_fail_done}")
    out.append(
        f"    провалов WABA ждут SMS         : {waba_fail_pend}"
    )
    out.append(
        f"    исключено из WABA-пула (30 дн) : {_exhausted}"
    )
    if not config.WAZZUP_WEBHOOK_URL:
        out.append(
            "    ⚠️  WAZZUP_WEBHOOK_URL не задан — фазы Telegram/MAX получают "
            "пустой пул: список провалов формирует ТОЛЬКО явный 'failed' "
            "от вебхука."
        )
    out.append("")

    # Хalted-каналы
    halted = [c for c in _CH_ORDER if state.is_channel_halted(st, c)]
    if halted:
        out.append(f"  🛑 Остановленные каналы: {', '.join(halted)}")
    else:
        out.append("  ✅ Остановленных каналов нет")

    # Согласие на рассылку — главный показатель риска бана и штрафа
    try:
        cs = db.consent_stats()
        out.append("")
        out.append("─" * 70)
        out.append("  СОГЛАСИЕ НА РАССЫЛКУ (ФЗ-38 ст.18 + политика Meta):")
        out.append(
            f"    есть согласие : {cs['granted']:>4}   можно писать"
        )
        out.append(
            f"    отказ/отписка : {cs['denied']:>4}   не пишем ВСЕГДА"
        )
        out.append(
            f"    нет данных    : {cs['unknown']:>4}   зависит от флага"
        )
        out.append(
            f"    BULK_CONSENT_REQUIRED_FOR_COLD = "
            f"{config.CONSENT_REQUIRED_FOR_COLD}"
            + ("  (пишем только granted)" if config.CONSENT_REQUIRED_FOR_COLD
               else "  (пишем и unknown — холодная база)")
        )
        if cs["granted"] == 0 and cs["total"] > 0:
            out.append("")
            out.append("    ⚠️  Ни у кого нет подтверждённого согласия.")
            out.append("        Массовая холодная рассылка без opt-in — нарушение")
            out.append("        ч.1 ст.18 ФЗ-38 (штраф юрлицам до 1 000 000 ₽) и")
            out.append("        причина блокировок аккаунта.")
    except Exception as e:
        out.append(f"  (статистика согласия недоступна: {e})")

    # Проблема с вебхуком — главная причина «принято, но неизвестно доставлено»
    if not getattr(config, "WAZZUP_WEBHOOK_URL", ""):
        out.append("")
        out.append("  ❌ WAZZUP_WEBHOOK_URL не задан → доставку подтвердить нечем.")
        out.append("     Сторож подтверждений заменит вебхук, но с задержкой "
                  f"{config.CONFIRM_TIMEOUT_MINUTES} мин.")

    # ПРОГНОЗ: сколько реально доставлю сегодня при текущем конфиге.
    # Вилка, а не одно число: конверсия холодной рассылки неизвестна до
    # первого прогона с вебхуком, и единственное зафиксированное значение
    # (28 доставок на 100 отправок WABA) сильно ниже любых аналогий.
    out.append("")
    out.append("─" * 70)
    out.append("  ПРОГНОЗ достижимого объёма (кап × конверсия, ВИЛКА):")
    lo, hi, fparts = _forecast(st)
    out.append(f"    {fparts}")
    out.append(f"    итого {lo}–{hi} доставленных в день")
    if hi < target:
        out.append(f"    🔴 Даже по оптимистичной оценке НЕ хватает до цели {target}")
    elif lo < target <= hi:
        out.append(f"    🟡 Цель {target} достижима ТОЛЬКО если конверсия WABA верхняя")
    else:
        out.append(f"    ✅ Прогноз покрывает цель {target}")
    out.append("    Конверсия WABA — главный рычаг: без вебхука она неизвестна.")
    if not config.SMSRU_ENABLED:
        out.append("    Рычаг №1: BULK_SMSRU_ENABLED=1 — SMS единственный канал,")
        out.append("        где «принято» практически равно «доставлено».")
    try:
        waba_now = state.channel_effective_cap(st, "waba")
        waba_250 = int(250 * 0.75)
        if waba_250 > waba_now:
            out.append(
                f"    Рычаг №2: BULK_LIMIT_WABA "
                f"{config.CHANNEL_DAILY_LIMITS.get('waba')}→250 "
                f"({waba_now}→{waba_250} эффективных) = "
                f"+{int((waba_250 - waba_now) * _WABA_COLD_RANGE[0])}…"
                f"+{int((waba_250 - waba_now) * _WABA_COLD_RANGE[1])} доставленных"
            )
    except Exception:
        pass
    out.append("─" * 70)

    out.append("=" * 70)
    return "\n".join(out)


def main() -> int:
    print(build_report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
