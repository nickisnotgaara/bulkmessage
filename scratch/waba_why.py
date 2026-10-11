"""Почему WABA доставляет 33%: проверяем гипотезы."""
import sqlite3
import json

con = sqlite3.connect("file:data/crm.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=== 1. Что в pending_webhooks без канала (436 шт) ===")
cols = [r[1] for r in cur.execute("PRAGMA table_info(pending_webhooks)")]
print("   колонки:", cols)
for r in cur.execute(
    "SELECT * FROM pending_webhooks WHERE channel IS NULL LIMIT 3"
):
    d = dict(r)
    print("  ", {k: (str(v)[:90] if v is not None else None) for k, v in d.items()})

print()
print("=== 2. 'no_delivery_confirmation' — сколько и это ловушка? ===")
n = cur.execute(
    "SELECT COUNT(*) FROM messages WHERE last_error LIKE '%no_delivery_confirmation%'"
).fetchone()[0]
print("   строк:", n)
print("   их delivery_state:")
for r in cur.execute(
    "SELECT delivery_state, COUNT(*) FROM messages "
    "WHERE last_error LIKE '%no_delivery_confirmation%' GROUP BY 1"
):
    print("   ", r[0], r[1])

print()
print("=== 3. WABA:Template used? сравним delivered vs rejected по template_id ===")
print("   Есть ли сохранённый template_id в messages:")
print("  ", [c[1] for c in cur.execute("PRAGMA table_info(messages)")])
print()
print("   waba_templates в БД:")
try:
    for r in cur.execute("SELECT * FROM waba_templates LIMIT 6"):
        print("   ", dict(r))
except sqlite3.Error as e:
    print("   ", e)

print()
print("=== 4. Главное: 10-02 (100 попыток, 0 доставок) vs 10-07 (86 попыток, 28) ===")
print("   В 10-02 webhook был или нет?")
for r in cur.execute(
    "SELECT substr(sent_at,1,10), COUNT(*), "
    "  SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END) "
    "FROM messages WHERE channel='waba' AND sent_at IS NOT NULL "
    "GROUP BY 1 ORDER BY 1 DESC LIMIT 8"
):
    print(f"   {r[0]}: {r[1]:4d} попыток, с last_error={r[2]}")

print()
print("=== 5. Сколько waba-сообщений вообще имеют text vs template ===")
for r in cur.execute(
    "SELECT CASE WHEN message_text IS NULL OR message_text='' THEN '(template, no text)' "
    "  ELSE 'free text' END k, COUNT(*), "
    "  SUM(CASE WHEN delivery_state='delivered' THEN 1 ELSE 0 END) "
    "FROM messages WHERE channel='waba' GROUP BY 1"
):
    print(f"   {r[0]}: {r[1]} шт, доставлено {r[2]}")

print()
print("=== 6. Персональные каналы: delivery_state по дням (проверка ~100%) ===")
for r in cur.execute(
    "SELECT substr(sent_at,1,10), channel, COUNT(*), "
    "  SUM(CASE WHEN status IN ('sent','delivered','read','answered') THEN 1 ELSE 0 END) "
    "FROM messages WHERE channel IN ('telegram','max') AND sent_at >= '2026-10-06' "
    "GROUP BY 1,2 ORDER BY 1 DESC,2"
):
    print(f"   {r[0]} {r[1]:9s}: {r[2]:3d} попыток, принято {r[3]}")