"""Что успел отправить последний WABA-прогон (запуск в 12:58)."""
import sqlite3

DB = "/opt/bulkmessage/data/crm.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
OK = ("sent", "delivered", "read", "answered")

since = "2026-10-10T12:58"
rows = con.execute(
    "SELECT m.sent_at, m.channel, m.status, m.delivery_state, "
    "       m.message_text, ct.id AS cid, ct.phone, ct.name, ct.category "
    "FROM messages m JOIN contacts ct ON ct.id=m.contact_id "
    "WHERE m.sent_at >= ? AND m.message_id IS NOT NULL "
    "  AND m.message_id NOT LIKE 'dryrun%' ORDER BY m.sent_at", (since,)
).fetchall()

print(f"=== СООБЩЕНИЙ ПОСЛЕ 12:58: {len(rows)} ===")
repeats = 0
for r in rows:
    prior = con.execute(
        "SELECT COUNT(*) FROM messages WHERE contact_id=? AND sent_at<? "
        "AND status IN (?,?,?,?)", (r["cid"], r["sent_at"], *OK)
    ).fetchone()[0]
    if prior:
        repeats += 1
    txt = (r["message_text"] or "").replace("\n", " ")
    print(f"{r['sent_at'][11:19]} [{r['channel']}] {r['phone']} "
          f"{str(r['name'])[:14]:14s} {r['status']:9s} del={r['delivery_state']}")
    print(f"    25%: {'ДА' if '25%' in txt else 'НЕТ'}  история ранее: {prior}"
          f"{'  <<< ПОВТОР' if prior else ''}")

print()
print(f"ПОВТОРОВ: {repeats}")
print()
print("=== ИТОГ ПОСЛЕ 12:58 ПО КАНАЛАМ ===")
for r in con.execute(
    "SELECT channel, COUNT(*) att, "
    "  SUM(CASE WHEN status IN ('sent','delivered','read','answered') THEN 1 ELSE 0 END) acc, "
    "  SUM(CASE WHEN delivery_state='delivered' THEN 1 ELSE 0 END) dlv "
    "FROM messages WHERE sent_at >= '2026-10-10T12:58' AND message_id IS NOT NULL "
    "  AND message_id NOT LIKE 'dryrun%' GROUP BY channel"
):
    print(f"  {r['channel']:9s} попыток={r['att']:3d} принято={r['acc']:3d} "
          f"ДОСТАВЛЕНО={r['dlv']:3d}")

print()
print("=== ВСЁ СЕГОДНЯ ===")
for r in con.execute(
    "SELECT channel, COUNT(*) att, "
    "  SUM(CASE WHEN status IN ('sent','delivered','read','answered') THEN 1 ELSE 0 END) acc, "
    "  SUM(CASE WHEN delivery_state='delivered' THEN 1 ELSE 0 END) dlv "
    "FROM messages WHERE substr(sent_at,1,10)='2026-10-10' "
    "  AND message_id IS NOT NULL AND message_id NOT LIKE 'dryrun%' GROUP BY channel"
):
    print(f"  {r['channel']:9s} попыток={r['att']:3d} принято={r['acc']:3d} "
          f"ДОСТАВЛЕНО={r['dlv']:3d}")