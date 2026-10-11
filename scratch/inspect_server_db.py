import sqlite3

con = sqlite3.connect("file:scratch/crm_from_server.db?mode=ro", uri=True)
cur = con.cursor()

print("integrity:", cur.execute("PRAGMA integrity_check").fetchone()[0])
print("contacts :", cur.execute("SELECT COUNT(*) FROM contacts").fetchone()[0])
print("messages :", cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
print("verdicts :", cur.execute("SELECT COUNT(*) FROM contact_channel_state").fetchone()[0])

done = ("sent", "delivered", "read", "answered")
marks = ",".join("?" for _ in done)
q = (
    "SELECT COUNT(*) FROM contacts c WHERE NOT EXISTS ("
    "SELECT 1 FROM messages m WHERE m.contact_id=c.id AND m.status IN (" + marks + "))"
)
print("never-contacted pool:", cur.execute(q, done).fetchone()[0])

print()
print("categories:")
for r in cur.execute(
    "SELECT category, COUNT(*) FROM contacts GROUP BY category ORDER BY 2 DESC LIMIT 8"
):
    print("  ", r)

print()
print("verdicts per channel:")
for r in cur.execute(
    "SELECT channel, verdict, COUNT(*) FROM contact_channel_state "
    "GROUP BY channel, verdict ORDER BY channel, 3 DESC"
):
    print("  ", r)

print()
print("last real send:", cur.execute(
    "SELECT MAX(sent_at) FROM messages WHERE message_id IS NOT NULL "
    "AND message_id NOT LIKE 'dryrun%'"
).fetchone()[0])

print()
print("sent_today rows for 2026-10-10:")
for r in cur.execute(
    "SELECT channel, COUNT(*), SUM(CASE WHEN delivery_state='delivered' THEN 1 ELSE 0 END) "
    "FROM messages WHERE substr(sent_at,1,10)='2026-10-10' GROUP BY channel"
):
    print("  ", r)