"""Результат боевого прогона: кто, что, доставлено ли."""
import sqlite3
import sys
from datetime import datetime, timezone

DB = sys.argv[1] if len(sys.argv) > 1 else "/opt/bulkmessage/data/crm.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()

day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
rows = cur.execute(
    "SELECT m.id, m.sent_at, m.channel, m.status, m.delivery_state, "
    "       m.message_id, m.last_error, m.message_text, ct.phone, ct.name, "
    "       ct.category "
    "FROM messages m JOIN contacts ct ON ct.id = m.contact_id "
    "WHERE substr(m.sent_at,1,10) = ? AND m.message_id IS NOT NULL "
    "  AND m.message_id NOT LIKE 'dryrun%' "
    "ORDER BY m.id", (day,)
).fetchall()

print(f"=== {day}: {len(rows)} сообщений ===")
ok = 0
no25 = 0
for r in rows:
    txt = (r["message_text"] or "").replace("\n", " ")
    has = "25%" in txt
    no25 += 0 if has else 1
    if r["status"] in ("sent", "delivered", "read", "answered"):
        ok += 1
    print(f"[{r['channel']:6s}] {r['phone']} {str(r['name'])[:14]:14s} "
          f"{str(r['category'])[:12]:12s} {r['status']:9s} "
          f"del={r['delivery_state']} 25%={'YES' if has else 'NO'}")
    print(f"         {txt[:120]}")

print()
print(f"принято транспортом: {ok}/{len(rows)}")
print(f"сообщений БЕЗ 25%   : {no25}")

print()
print("=== вердикты вебхука (pending_webhooks) ===")
try:
    n = cur.execute(
        "SELECT COUNT(*) FROM pending_webhooks WHERE processed=0"
    ).fetchone()[0]
    print("необработанных:", n)
    for r in cur.execute(
        "SELECT channel, COUNT(*) FROM pending_webhooks WHERE processed=0 "
        "GROUP BY channel"
    ):
        print("  ", r[0], r[1])
except sqlite3.Error as e:
    print("ошибка:", e)