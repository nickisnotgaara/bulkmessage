import sqlite3

DB = "/opt/bulkmessage/data/crm.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row


def block(title, where, params=()):
    print(f"=== {title} ===")
    rows = con.execute(
        "SELECT m.sent_at, m.channel, m.status, m.delivery_state, m.last_error, "
        "       ct.phone, ct.name FROM messages m "
        "JOIN contacts ct ON ct.id = m.contact_id "
        f"WHERE {where} AND m.message_id IS NOT NULL "
        "  AND m.message_id NOT LIKE 'dryrun%' ORDER BY m.sent_at", params
    ).fetchall()
    if not rows:
        print("   (пусто)")
        return
    for r in rows:
        print(f"   {str(r['sent_at'])[:19]} {r['phone']:14s} "
              f"{str(r['name'])[:14]:14s} {r['status']:9s} del={r['delivery_state']:9s} "
              f"{str(r['last_error'])[:60]}")
    print(f"   ИТОГО строк: {len(rows)}")
    print()


block("MAX — СЕГОДНЯ", "m.channel='max' AND substr(m.sent_at,1,10)='2026-10-10'")
block("MAX — ЗА ВСЁ ВРЕМЯ", "m.channel='max'")

print("=== СВОДКА: MAX по дням ===")
for r in con.execute(
    "SELECT substr(sent_at,1,10) d, COUNT(*) att, "
    "  SUM(CASE WHEN status IN ('sent','delivered','read','answered') THEN 1 ELSE 0 END) acc, "
    "  SUM(CASE WHEN delivery_state='delivered' THEN 1 ELSE 0 END) dlv "
    "FROM messages WHERE channel='max' AND sent_at IS NOT NULL "
    "  AND message_id IS NOT NULL AND message_id NOT LIKE 'dryrun%' "
    "GROUP BY d ORDER BY d DESC LIMIT 10"
):
    print(f"   {r['d']}: попыток={r['att']:3d} принято={r['acc']:3d} "
          f"ДОСТАВЛЕНО={r['dlv']:3d}")

print()
print("=== ВЕРДИКТЫ ПО MAX (reachability) ===")
for r in con.execute(
    "SELECT verdict, COUNT(*) n FROM contact_channel_state "
    "WHERE channel='max' GROUP BY verdict ORDER BY n DESC"
):
    print(f"   {r['verdict']:10s} {r['n']}")

print()
print("=== ЛИМИТ MAX ===")
print("   hard cap в коде: 20/день")
print("   сегодня отправлено:", con.execute(
    "SELECT COUNT(*) FROM messages WHERE channel='max' "
    "AND substr(sent_at,1,10)='2026-10-10' AND message_id IS NOT NULL "
    "AND message_id NOT LIKE 'dryrun%'").fetchone()[0])