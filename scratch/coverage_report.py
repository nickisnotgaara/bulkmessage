"""Отчёт: покрытие каналов по пулу + кто без каналов, кто во всех трёх."""
import sqlite3
import sys
from datetime import datetime, timezone

DB = sys.argv[1] if len(sys.argv) > 1 else "/opt/bulkmessage/data/crm.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()

OK = ("sent", "delivered", "read", "answered")

print("=== 1. ДОСТАВЛЕНО ПО КАНАЛАМ (все время) ===")
for r in cur.execute(
    "SELECT channel, COUNT(*) att, "
    "  SUM(CASE WHEN delivery_state='delivered' THEN 1 ELSE 0 END) dlv, "
    "  SUM(CASE WHEN status IN ('sent','delivered','read','answered') THEN 1 ELSE 0 END) acc "
    "FROM messages WHERE message_id IS NOT NULL AND message_id NOT LIKE 'dryrun%' "
    "GROUP BY channel ORDER BY dlv DESC"
):
    print(f"  {r['channel']:9s} попыток={r['att']:5d}  принято={r['acc']:5d}  "
          f"ДОСТАВЛЕНО={r['dlv']:5d}  ({r['dlv']/max(r['att'],1)*100:.0f}%)")

print()
print("=== 2. ПОКРЫТИЕ КАНАЛАМИ (только не написанные) ===")
pool = cur.execute(
    "SELECT COUNT(*) FROM contacts c WHERE COALESCE(c.is_test,0)=0 "
    "AND COALESCE(c.is_invalid,0)=0 AND NOT EXISTS ("
    "  SELECT 1 FROM messages m WHERE m.contact_id=c.id AND m.status IN (?,?,?,?))",
    OK,
).fetchone()[0]
print(f"  пул (никогда не написанных): {pool}")


def reachable(contact_id: int, chans: str) -> bool:
    q = (
        "SELECT COUNT(*) FROM contact_channel_state WHERE contact_id=? "
        "AND channel IN (" + ",".join("?" * len(chans)) + ") "
        "AND verdict IN ('present','delivered')"
    )
    return cur.execute(q, (contact_id, *chans)).fetchone()[0] > 0


stats = {"tg+max+wa": 0, "no_tg_no_max": 0, "no_all3": 0, "probed": 0}
all3, only_wa, none_all = [], [], []
ids = [r[0] for r in cur.execute(
    "SELECT c.id FROM contacts c WHERE COALESCE(c.is_test,0)=0 "
    "AND COALESCE(c.is_invalid,0)=0 AND NOT EXISTS ("
    "  SELECT 1 FROM messages m WHERE m.contact_id=c.id AND m.status IN (?,?,?,?))",
    OK,
)]

for cid in ids:
    tg = reachable(cid, ("telegram",))
    mx = reachable(cid, ("max",))
    wa = reachable(cid, ("waba", "whatsapp"))
    row = cur.execute("SELECT phone, name FROM contacts WHERE id=?", (cid,)).fetchone()
    if tg and mx and wa:
        stats["tg+max+wa"] += 1
        all3.append(f"{row['phone']} {row['name']}")
    if not tg and not mx:
        stats["no_tg_no_max"] += 1
    if not tg and not mx and not wa:
        stats["no_all3"] += 1
        none_all.append(f"{row['phone']} {row['name']}")
    if wa and not tg and not mx:
        only_wa.append(f"{row['phone']} {row['name']}")

print(f"  есть ВСЕ ТРИ (tg+max+whatsapp): {stats['tg+max+wa']}")
print(f"  НЕТ ни tg, ни max            : {stats['no_tg_no_max']}")
print(f"  НЕТ ни одного из трёх        : {stats['no_all3']}")
print(f"  есть только whatsapp         : {len(only_wa)}")

print()
print("=== 3. Есть все три канала (первые 20) ===")
for s in all3[:20]:
    print("   ", s)
print(f"    ... всего {len(all3)}")

print()
print("=== 4. Без tg и без max, но с whatsapp (первые 20) ===")
for s in only_wa[:20]:
    print("   ", s)
print(f"    ... всего {len(only_wa)}")

print()
print("=== 5. Покрытие вердиктами вообще ===")
for r in cur.execute(
    "SELECT channel, verdict, COUNT(*) n FROM contact_channel_state "
    "GROUP BY channel, verdict ORDER BY channel, n DESC"
):
    print(f"   {r['channel']:9s} {r['verdict']:10s} {r['n']}")