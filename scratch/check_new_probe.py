"""Проверка новой разведки: потолки, порядок, отбор кандидатов."""
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/opt/bulkmessage")

from bulkmessage import config, sender, state  # noqa: E402

print("=== 1. КОНФИГ ===")
print("   PROBE_CHANNELS            :", config.PROBE_CHANNELS)
print("   PROBE_MAX_DAILY_LIMIT     :", config.PROBE_MAX_DAILY_LIMIT)
print("   PROBE_MAX_INTERVAL_MIN    :", config.PROBE_MAX_INTERVAL_MIN)
print("   PROBE_INTERVAL_MIN (tg)   :", config.PROBE_INTERVAL_MIN)

print()
print("=== 2. ПОТОЛОК ПРОВЕРОК MAX ===")
st = state.load_state()
st["probe_checks_today"] = {}
print("   сегодня проверок MAX:", state.probe_checks_today(st, "max"),
      " осталось:", state.probe_budget_left(st, "max"))
for i in range(13):
    state.increment_probe_check(st, "max")
left = state.probe_budget_left(st, "max")
print(f"   после 13 проверок: осталось {left} "
      f"{'OK — потолок держит' if left == 0 else 'ПРОБЛЕМА: потолок не сработал'}")
print("   telegram потолок:", state.probe_budget_left(st, "telegram"),
      "(None = отдельного лимита нет)")
st["probe_checks_today"] = {}
state.save_state(st)

print()
print("=== 3. СБРОС СУТОЧНЫХ СЧЕТЧИКОВ ===")
st2 = state.load_state()
st2["probe_checks_today"] = {"max": 10, "telegram": 400}
st2 = state.reset_daily_if_new_day({**st2, "date": "1999-01-01"})
print("   probe_checks_today после нового дня:", st2.get("probe_checks_today"))
print("   MAX осталось:", state.probe_budget_left(st2, "max"))

print()
print("=== 4. ОТБОР КАНДИДАТОВ ===")
tg = sender.select_probe_candidates(limit=5, channel="telegram")
mx = sender.select_probe_candidates(limit=5, channel="max")
print(f"   telegram-кандидатов: {len(tg)}, max-кандидатов: {len(mx)}")
print("   примеры TG:", [c["phone"] for c in tg[:3]])
print("   примеры MAX:", [c["phone"] for c in mx[:3]])

print()
print("=== 5. ПРАВИЛО MAX: только без TG и без WhatsApp ===")
import sqlite3  # noqa: E402

con = sqlite3.connect("file:/opt/bulkmessage/data/crm.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
violations = 0
for c in mx:
    n = con.execute(
        "SELECT COUNT(*) FROM contact_channel_state WHERE contact_id=? "
        "AND channel IN ('telegram','waba','whatsapp') "
        "AND verdict IN ('present','delivered')", (c["contact_id"],)
    ).fetchone()[0]
    if n:
        violations += 1
        print(f"   !!! {c['phone']} уже есть в мессенджере")
print(f"   нарушений: {violations} {'OK' if violations == 0 else 'ПРОБЛЕМА'}")

print()
print("=== 6. НИ ОДИН КАНДИДАТ НЕ ДОЛЖЕН ИМЕТЬ ИСТОРИЮ ===")
bad = 0
for c in tg + mx:
    n = con.execute(
        "SELECT COUNT(*) FROM messages WHERE contact_id=? "
        "AND status IN ('sent','delivered','read','answered')",
        (c["contact_id"],)).fetchone()[0]
    if n:
        bad += 1
print(f"   кандидатов с историей: {bad} {'OK' if bad == 0 else 'ПОВТОР!'}")

print()
print("=== 7. ПОРЯДОК ФАЗ ===")
for po in (["max", "telegram", "waba"], ["waba"]):
    print(f"   {str(po):28s} -> {sender._build_exec_order(list(po))}")
print("   PROBE telegram — фаза 0 (до WABA)")
print("   PROBE max      — конец круга (после WABA)")
print(f"   _probe_done flag есть: {hasattr(sender, '_mark_probe_done_for_run')}")
print(f"   _max_probe flag есть  : {hasattr(sender, '_mark_max_probe_done_for_run')}")