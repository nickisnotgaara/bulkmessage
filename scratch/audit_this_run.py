"""Проверка ТОЛЬКО этого тестового прогона (после 12:35 UTC)."""
import sqlite3
import sys

DB = "/opt/bulkmessage/data/crm.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
OK = ("sent", "delivered", "read", "answered")

rows = con.execute(
    "SELECT m.id, m.sent_at, m.channel, m.status, m.delivery_state, "
    "       m.message_text, ct.id AS cid, ct.phone, ct.name, ct.category "
    "FROM messages m JOIN contacts ct ON ct.id = m.contact_id "
    "WHERE m.sent_at >= '2026-10-10T12:35' AND m.message_id IS NOT NULL "
    "  AND m.message_id NOT LIKE 'dryrun%' ORDER BY m.id"
).fetchall()

print(f"=== СООБЩЕНИЯ ЭТОГО ТЕСТОВОГО ПРОГОНА: {len(rows)} ===\n")
repeats = 0
for r in rows:
    prior = con.execute(
        "SELECT sent_at, channel, status FROM messages "
        "WHERE contact_id = ? AND sent_at < ? AND status IN (?,?,?,?) "
        "ORDER BY sent_at",
        (r["cid"], r["sent_at"], *OK),
    ).fetchall()
    if prior:
        repeats += 1
    txt = (r["message_text"] or "").replace("\n", " ")
    print(f"[{r['channel']}] {r['phone']} {str(r['name'])[:16]:16s} "
          f"{str(r['category'])[:12]:12s} {r['status']}")
    print(f"    {txt[:150]}")
    print(f"    25%: {'ДА' if '25%' in txt else 'НЕТ!!!'}   "
          f"история ранее: {len(prior)} {prior if prior else ''}")
    print()

print(f"!!! ПОВТОРОВ В ЭТОМ ПРОГОНЕ: {repeats}")

print()
print("=== сверка: все сообщения сегодня после 12:35 ни у кого нет истории ===")
for r in rows:
    n = con.execute(
        "SELECT COUNT(*) FROM messages WHERE contact_id=? AND sent_at < '2026-10-10T12:35' "
        "AND status IN (?,?,?,?)", (r["cid"], *OK)).fetchone()[0]
    print(f"   {r['phone']:14s} ранее успешных: {n}  {'OK' if n == 0 else 'ПОВТОР!'}")