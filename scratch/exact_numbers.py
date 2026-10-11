"""Точные цифры: что реально ушло за тестовые прогоны и что с доставкой."""
import sqlite3

DB = "/opt/bulkmessage/data/crm.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

print("=== ВСЁ СЕГОДНЯ (2026-10-10), по времени ===")
rows = con.execute(
    "SELECT m.sent_at, m.channel, m.status, m.delivery_state, m.message_id, "
    "       ct.phone, ct.name "
    "FROM messages m JOIN contacts ct ON ct.id=m.contact_id "
    "WHERE substr(m.sent_at,1,10)='2026-10-10' AND m.message_id IS NOT NULL "
    "  AND m.message_id NOT LIKE 'dryrun%' ORDER BY m.sent_at"
).fetchall()
for r in rows:
    tag = "ПРОБА-1" if r["sent_at"][11:19] < "12:39" else (
        "ПРОБА-2" if r["sent_at"][11:19] < "12:50" else "ПОЗЖЕ")
    print(f"{r['sent_at'][11:19]} [{tag:8s}] {r['channel']:9s} "
          f"{r['status']:9s} del={r['delivery_state']:9s} "
          f"{r['phone']} {str(r['name'])[:14]}")

print()
print("=== ИТОГ ПО КАНАЛАМ ЗА СЕГОДНЯ ===")
for r in con.execute(
    "SELECT channel, COUNT(*) att, "
    "  SUM(CASE WHEN status IN ('sent','delivered','read','answered') THEN 1 ELSE 0 END) acc, "
    "  SUM(CASE WHEN delivery_state='delivered' THEN 1 ELSE 0 END) dlv "
    "FROM messages WHERE substr(sent_at,1,10)='2026-10-10' "
    "  AND message_id IS NOT NULL AND message_id NOT LIKE 'dryrun%' GROUP BY channel"
):
    print(f"  {r['channel']:9s} попыток={r['att']:3d} принято={r['acc']:3d} "
          f"ДОСТАВЛЕНО={r['dlv']:3d}")

print()
print("=== ТОЛЬКО ТЕСТОВЫЕ ПРОГОНЫ (после 12:35) ===")
r = con.execute(
    "SELECT COUNT(*) att, "
    "  SUM(CASE WHEN status IN ('sent','delivered','read','answered') THEN 1 ELSE 0 END) acc, "
    "  SUM(CASE WHEN delivery_state='delivered' THEN 1 ELSE 0 END) dlv "
    "FROM messages WHERE m.sent_at >= '2026-10-10T12:35' AND message_id IS NOT NULL "
    "  AND message_id NOT LIKE 'dryrun%'"
).fetchone()
print(f"  попыток={r['att']}  принято={r['acc'] or 0}  доставлено={r['dlv'] or 0}")