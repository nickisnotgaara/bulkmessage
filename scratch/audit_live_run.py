"""Кто получил сообщение в последнем прогоне и писали ли им РАНЬШЕ.

Правило владельца «написали — навсегда»: контакт исключается из пула по
ЛЮБОМУ каналу. Если человеку уже отправляли, второй раз — это жалоба,
а жалоба в личном канале означает бан номера.
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "crm.db"
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

print("=" * 78)
print("ЧТО УШЛО ЗА ПОСЛЕДНИЕ СООБЩЕНИЯ (по id DESC, последние 20)")
print("=" * 78)
rows = con.execute("""
    SELECT m.id, m.channel, m.status, m.delivery_state, m.message_id,
           m.sent_at, m.created_at, c.phone, c.name
    FROM messages m LEFT JOIN contacts c ON c.id = m.contact_id
    ORDER BY m.id DESC LIMIT 20
""").fetchall()

for r in rows:
    print(f"  #{r['id']:<6} {str(r['channel']):<9} {str(r['status']):<10}"
          f" тел={r['phone']}  отправлен={r['sent_at']}")

print()
print("=" * 78)
print("ГЛАВНЫЙ ВОПРОС: ПИСАЛИ ЛИ ИМ РАНЬШЕ?")
print("=" * 78)
if not rows:
    print("  Новых строк в messages нет.")
else:
    for r in rows:
        # все сообщения этому контакту, кроме текущего
        prev = con.execute("""
            SELECT COUNT(*) AS n,
                   MIN(sent_at) AS first_sent,
                   GROUP_CONCAT(DISTINCT channel) AS channels
            FROM messages
            WHERE contact_id = (SELECT contact_id FROM messages WHERE id = ?)
              AND id != ?
        """, (r["id"], r["id"])).fetchone()
        n = prev["n"] or 0
        verdict = "🔴 ПИСАЛИ РАНЬШЕ" if n else "🟢 первый контакт"
        print(f"  #{r['id']} {r['phone']}  [{verdict}]")
        print(f"        прошлых сообщений: {n}"
              + (f", первое {prev['first_sent']}"
                 if prev["first_sent"] else "")
              + (f", каналы {prev['channels']}" if prev["channels"] else ""))

print()
print("=" * 78)
print("СВОДКА ПО ПОСЛЕДНЕМУ ЗАПУСКУ")
print("=" * 78)
cnt = con.execute("""
    SELECT COUNT(*) AS total,
           SUM(CASE WHEN m.channel='telegram' THEN 1 ELSE 0 END) AS tg
    FROM messages m WHERE m.id > (SELECT MAX(id)-30 FROM messages)
""").fetchone()
print(f"  строк в хвосте таблицы: {cnt['total']}, из них telegram: {cnt['tg']}")
con.close()