"""Разбор потерь WABA: сколько попыток тратится на номера без WhatsApp."""
import sqlite3

con = sqlite3.connect("file:data/crm.db?mode=ro", uri=True)
cur = con.cursor()

BAD = "%may not be on WhatsApp%"

print("=== 1. WABA BAD_CONTACT: сколько контактов и повторов ===")
r = cur.execute(
    "SELECT COUNT(*), COUNT(DISTINCT contact_id) FROM messages "
    "WHERE channel='waba' AND last_error LIKE ?", (BAD,)
).fetchone()
print(f"   строк отказа={r[0]}, уникальных контактов={r[1]}")

print()
print("=== 2. Эти контакты: есть ли вердикт whatsapp/waba ===")
rows = cur.execute(
    "SELECT COALESCE(s.verdict,'(NONE)'), COUNT(DISTINCT m.contact_id) "
    "FROM messages m LEFT JOIN contact_channel_state s "
    "  ON s.contact_id=m.contact_id AND s.channel IN ('waba','whatsapp') "
    "WHERE m.channel='waba' AND m.last_error LIKE ? GROUP BY 1", (BAD,)
).fetchall()
for v, n in rows:
    print(f"   verdict={v:10s} контактов={n}")

print()
print("=== 3. Повторные WABA-попытки на одном контакте (сожжённая квота) ===")
rows = cur.execute(
    "SELECT n, COUNT(*) FROM ("
    "  SELECT contact_id, COUNT(*) n FROM messages WHERE channel='waba' "
    "  GROUP BY contact_id) WHERE n > 1 GROUP BY n ORDER BY n"
).fetchall()
total_wasted = sum((n - 1) * c for n, c in rows)
print("   попыток-повторов всего:", total_wasted)
for n, c in rows[:6]:
    print(f"   {n} попыток у {c} контактов")

print()
print("=== 4. Сколько уникальных контактов вообще тронуто на WABA ===")
print("  ", cur.execute(
    "SELECT COUNT(DISTINCT contact_id) FROM messages WHERE channel='waba'"
).fetchone()[0])

print()
print("=== 5. Конверсия WABA по дням (attempts -> delivered) ===")
for r in cur.execute(
    "SELECT substr(sent_at,1,10), COUNT(*), "
    "  SUM(CASE WHEN delivery_state='delivered' THEN 1 ELSE 0 END), "
    "  SUM(CASE WHEN last_error LIKE '%may not be on WhatsApp%' THEN 1 ELSE 0 END) "
    "FROM messages WHERE channel='waba' AND sent_at IS NOT NULL "
    "GROUP BY 1 ORDER BY 1 DESC LIMIT 8"
):
    day, att, dlv, bad = r
    print(f"   {day}: {att:4d} попыток, доставлено {dlv:3d}, "
          f"мёртвых номеров {bad:3d} ({dlv/max(att,1)*100:.0f}% доставки)")

print()
print("=== 6. Пул без вердикта whatsapp — куда уйдут следующие WABA попытки ===")
print("   контактов вообще без вердикта:",
      cur.execute(
          "SELECT COUNT(*) FROM contacts c WHERE NOT EXISTS("
          "  SELECT 1 FROM contact_channel_state s WHERE s.contact_id=c.id)"
      ).fetchone()[0])
print("   контактов с verdict absent по whatsapp/waba:",
      cur.execute(
          "SELECT COUNT(DISTINCT contact_id) FROM contact_channel_state "
          "WHERE channel IN ('waba','whatsapp') AND verdict='absent'"
      ).fetchone()[0])