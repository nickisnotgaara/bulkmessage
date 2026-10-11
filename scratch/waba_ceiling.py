"""Проверка: реальная ли конверсия WABA и что ограничивает число попыток."""
import sqlite3

con = sqlite3.connect("file:data/crm.db?mode=ro", uri=True)
cur = con.cursor()

print("=== A. delivered_at vs delivery_state по дням (waba) ===")
print("   дата        попыток  delivery_state=delivered  delivered_at есть")
for r in cur.execute(
    "SELECT substr(sent_at,1,10), COUNT(*), "
    "  SUM(CASE WHEN delivery_state='delivered' THEN 1 ELSE 0 END), "
    "  SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END) "
    "FROM messages WHERE channel='waba' AND sent_at IS NOT NULL "
    "GROUP BY 1 ORDER BY 1 DESC LIMIT 8"
):
    d, att, ds, da = r
    print(f"   {d}   {att:6d}     {ds:6d}                  {da:6d}")

print()
print("=== B. Есть ли вебхук-события по этим сообщениям (delivery_events) ===")
try:
    for r in cur.execute(
        "SELECT substr(created_at,1,10), COUNT(*) FROM delivery_events "
        "GROUP BY 1 ORDER BY 1 DESC LIMIT 8"
    ):
        print("   ", r[0], r[1])
except sqlite3.Error as e:
    print("   нет таблицы/колонки:", e)

print()
print("=== C. pending_webhooks: сколько событий накоплено ===")
try:
    print("   строк pending_webhooks:",
          cur.execute("SELECT COUNT(*) FROM pending_webhooks").fetchone()[0])
    for r in cur.execute(
        "SELECT channel, COUNT(*) FROM pending_webhooks GROUP BY channel"
    ):
        print("   ", r[0], r[1])
except sqlite3.Error as e:
    print("   ошибка:", e)

print()
print("=== D. Сколько СВЕЖИХ контактов доступно для WABA сейчас ===")
print("   всего контактов:", cur.execute("SELECT COUNT(*) FROM contacts").fetchone()[0])
q = """SELECT COUNT(*) FROM contacts c
      WHERE COALESCE(c.is_test,0)=0 AND COALESCE(c.is_invalid,0)=0
        AND NOT EXISTS (
          SELECT 1 FROM messages m WHERE m.contact_id=c.id
            AND m.status IN ('sent','delivered','read','answered'))"""
print("   никогда не написанных:", cur.execute(q).fetchone()[0])

print()
print("=== E. rescue_queue: размер ===")
try:
    print("   строк:", cur.execute("SELECT COUNT(*) FROM rescue_queue").fetchone()[0])
except sqlite3.Error as e:
    print("   ошибка:", e)

print()
print("=== F. Распределение попыток WABA по часам (10-08, лучший день) ===")
for r in cur.execute(
    "SELECT substr(sent_at,12,2) h, COUNT(*), "
    "  SUM(CASE WHEN delivery_state='delivered' THEN 1 ELSE 0 END) "
    "FROM messages WHERE channel='waba' AND substr(sent_at,1,10)='2026-10-08' "
    "GROUP BY 1 ORDER BY 1"
):
    print(f"   час {r[0]}: {r[1]:3d} попыток, {r[2]} доставлено")