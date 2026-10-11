"""АУДИТ: не трогали ли сегодня контакты, которым УЖЕ писали раньше."""
import sqlite3
import sys
from datetime import datetime, timezone

DB = sys.argv[1] if len(sys.argv) > 1 else "/opt/bulkmessage/data/crm.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()

day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
OK = ("sent", "delivered", "read", "answered")

rows = cur.execute(
    "SELECT m.id, m.sent_at, m.channel, m.status, ct.id AS cid, ct.phone, "
    "       ct.name, ct.category "
    "FROM messages m JOIN contacts ct ON ct.id = m.contact_id "
    "WHERE substr(m.sent_at,1,10) = ? AND m.message_id IS NOT NULL "
    "  AND m.message_id NOT LIKE 'dryrun%' "
    "ORDER BY m.id", (day,)
).fetchall()

print(f"=== Сегодня {day}: {len(rows)} отправок ===\n")

repeats = []
for r in rows:
    prior = cur.execute(
        "SELECT sent_at, channel, status, message_id FROM messages "
        "WHERE contact_id = ? AND sent_at IS NOT NULL "
        "  AND sent_at < ? AND status IN (?,?,?,?) "
        "ORDER BY sent_at", (r["cid"], r["sent_at"], *OK)
    ).fetchall()
    if prior:
        repeats.append((r, prior))

print(f"!!! ПОВТОРНЫХ ОТПРАВОК: {len(repeats)} из {len(rows)}\n")
for r, prior in repeats:
    print(f"  {r['phone']} {str(r['name'])[:16]:16s} {str(r['category'])[:12]:12s}")
    print(f"     сегодня: {r['sent_at'][:19]} {r['channel']} {r['status']}")
    for p in prior:
        print(f"     РАНЬШЕ: {p['sent_at'][:19]} {p['channel']:9s} {p['status']:9s} id={p['message_id']}")
    print()

print()
print("=== Сводка по каналам сегодня ===")
for r in cur.execute(
    "SELECT channel, COUNT(*), "
    "  SUM(CASE WHEN status IN ('sent','delivered','read','answered') THEN 1 ELSE 0 END), "
    "  SUM(CASE WHEN delivery_state='delivered' THEN 1 ELSE 0 END) "
    "FROM messages WHERE substr(sent_at,1,10) = ? "
    "  AND message_id IS NOT NULL AND message_id NOT LIKE 'dryrun%' "
    "GROUP BY channel", (day,)
):
    print(f"  {r[0]:9s} попыток={r[1]:3d} принято={r[2]:3d} доставлено={r[3]:3d}")

print()
print("=== Порядок каскада у сегодняшних контактов ===")
for r in cur.execute(
    "SELECT ct.phone, ct.name, m.channel, m.status FROM messages m "
    "JOIN contacts ct ON ct.id=m.contact_id "
    "WHERE substr(m.sent_at,1,10)=? AND m.message_id IS NOT NULL "
    "  AND m.message_id NOT LIKE 'dryrun%' ORDER BY m.id", (day,)
):
    pass
casc = {}
for r in rows:
    casc.setdefault(r["cid"], []).append(r["channel"])
multi = {k: v for k, v in casc.items() if len(v) > 1}
print(f"  контактов с >1 каналом: {len(multi)}")
for k, v in list(multi.items())[:10]:
    print("   ", v)