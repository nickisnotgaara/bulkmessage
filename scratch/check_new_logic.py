"""Проверка новой логики на боевой базе (только чтение)."""
import sys

sys.path.insert(0, "/opt/bulkmessage")

from bulkmessage import config, sender  # noqa: E402
from bulkmessage.probe import pool_coverage  # noqa: E402

cands = sender.select_probe_candidates(limit=20)
print(f"=== кандидаты на пробу: {len(cands)} ===")
for c in cands[:10]:
    print(f"   id={c['contact_id']:5d} {c['phone']} {str(c['name'])[:16]:16s} "
          f"{str(c['category'])[:14]}")

print()
print("=== проверка: ни один кандидат не должен быть уже написанным ===")
import sqlite3  # noqa: E402

con = sqlite3.connect("file:/opt/bulkmessage/data/crm.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
bad = 0
for c in cands:
    n = con.execute(
        "SELECT COUNT(*) FROM messages WHERE contact_id = ? "
        "AND status IN ('sent','delivered','read','answered')",
        (c["contact_id"],),
    ).fetchone()[0]
    if n:
        bad += 1
        print(f"   !!! УЖЕ НАПИСАН {c['phone']} ({n} сообщений)")
print(f"   кандидатов с историей: {bad}  {'OK' if bad == 0 else 'ПРОБЛЕМА'}")

print()
print("=== покрытие пула (новая метрика) ===")
cov = pool_coverage()
for k in ("total", "has_telegram", "has_max", "has_whatsapp", "has_all_three",
          "has_none", "blocked", "unknown", "no_whatsapp"):
    print(f"   {k:16s} {cov.get(k)}")
print(f"   delivered_today  {cov.get('delivered_today')}")

print()
print("=== WABA-цели ===")
from bulkmessage import state  # noqa: E402

st = state.load_state()
pool = sender._select_contacts_for_cycle()
targets = sender._collect_fresh_waba_targets(pool, st)
print(f"   пул: {len(pool)}, цели WABA: {len(targets)}")

# Проверка: у WABA-целей не должно быть подтверждённых личных каналов
reserved = 0
for t in targets[:500]:
    n = con.execute(
        "SELECT COUNT(*) FROM contact_channel_state WHERE contact_id = ? "
        "AND channel IN ('telegram','max') "
        "AND verdict IN ('present','delivered')",
        (t["id"],),
    ).fetchone()[0]
    if n:
        reserved += 1
print(f"   WABA-целей с подтверждённым TG/MAX: {reserved}  "
      f"{'OK' if reserved == 0 else 'ПРОБЛЕМА'}")