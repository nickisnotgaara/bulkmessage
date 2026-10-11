"""Почему PROBE бьёт по уже написанным: смотрим rescue_queue и отбор кандидатов."""
import sqlite3
import sys
from datetime import datetime, timezone

DB = sys.argv[1] if len(sys.argv) > 1 else "/opt/bulkmessage/data/crm.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()

cols = [r[1] for r in cur.execute("PRAGMA table_info(rescue_queue)")]
print("rescue_queue columns:", cols)
print("rows:", cur.execute("SELECT COUNT(*) FROM rescue_queue").fetchone()[0])
print()

rows = cur.execute("SELECT * FROM rescue_queue").fetchall()
print(f"=== rescue_queue: {len(rows)} строк ===")

OK = ("sent", "delivered", "read", "answered")
already = 0
for r in rows:
    d = dict(r)
    cid = d.get("contact_id")
    if cid is None:
        prior = cur.execute(
            "SELECT COUNT(*) FROM contacts WHERE phone = ?", (d.get("phone"),)
        ).fetchone()[0]
        cid = prior or None
    n_prior = 0
    if cid:
        n_prior = cur.execute(
            "SELECT COUNT(*) FROM messages WHERE contact_id = ? "
            "AND status IN (?,?,?,?)", (cid, *OK)
        ).fetchone()[0]
    if n_prior:
        already += 1
    print(f"  phone={str(d.get('phone')):14s} contact_id={str(cid):6s} "
          f"prior_msgs={n_prior}  {({k: str(v)[:28] for k, v in list(d.items())[:7]})}")

print()
print(f"!!! в rescue_queue УЖЕ написанных ранее: {already} из {len(rows)}")

print()
print("=== сегодняшние повторы: откуда они пришли в rescue ===")
day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
for r in cur.execute(
    "SELECT ct.phone, ct.name, m.channel, m.sent_at, m.message_id FROM messages m "
    "JOIN contacts ct ON ct.id=m.contact_id "
    "WHERE substr(m.sent_at,1,10)=? AND m.message_id IS NOT NULL "
    "  AND m.message_id NOT LIKE 'dryrun%' AND m.channel IN ('telegram','max') "
    "ORDER BY m.sent_at", (day,)
):
    inq = cur.execute(
        "SELECT COUNT(*) FROM rescue_queue WHERE phone = ?", (r["phone"],)
    ).fetchone()[0]
    print(f"  {r['phone']} {str(r['name'])[:14]:14s} {r['channel']:9s} "
          f"{r['sent_at'][11:19]}  in_rescue_queue={inq}")