"""Что РЕАЛЬНО ушло за второй прогон (00:38–01:00) и кому.

Проверяем главное: у каждого получившего ДО отправки не было ни одной
строки с message_id. То есть никто не получил второе сообщение.
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "crm.db"
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

RUN_START = "2026-10-11T19:38:00"   # 22:38 МСК в UTC
rows = con.execute("""
    SELECT m.id, m.channel, m.status, m.delivery_state, m.message_id,
           m.sent_at, c.phone, c.name
    FROM messages m JOIN contacts c ON c.id = m.contact_id
    WHERE m.sent_at >= ?
    ORDER BY m.id
""", (RUN_START,)).fetchall()

print("=" * 74)
print(f"ОТПРАВЛЕНО ЗА ПРОГОН: {len(rows)}")
print("=" * 74)
print(f"{'#':<4}{'телефон':<17}{'канал':<10}{'статус':<8}{'было раньше'}")
print("-" * 74)
repeats = 0
for r in rows:
    prev = con.execute("""
        SELECT COUNT(*) FROM messages
        WHERE contact_id=(SELECT contact_id FROM messages WHERE id=?)
          AND id != ? AND message_id IS NOT NULL AND message_id NOT LIKE 'dryrun%'
    """, (r["id"], r["id"])).fetchone()[0]
    if prev:
        repeats += 1
    mark = f"🔴 ПОВТОР ({prev})" if prev else "🟢 первый раз"
    print(f"#{r['id']:<4}{r['phone']:<17}{r['channel']:<10}"
          f"{r['status']:<8}{mark}")

print("-" * 74)
print(f"  ПОВТОРНЫХ ОТПРАВОК: {repeats}")
print(f"  ЧИСТЫХ ПЕРВЫХ КАСАНИЙ: {len(rows) - repeats}")

print()
print("=" * 74)
print("ВЕРДИКТЫ ПРОБ ПО ЭТОМУ ПРОГОНУ")
print("=" * 74)
for r in con.execute("""
    SELECT s.channel, s.verdict, COUNT(*) n
    FROM contact_channel_state s
    WHERE s.last_probe_at >= ? GROUP BY s.channel, s.verdict
""", (RUN_START,)):
    print(f"   {r['channel']:<10} {r['verdict']:<10} {r['n']}")

print()
print("=" * 74)
print("ЧТО ОСТАЛОСЬ НЕПОДТВЕРЖДЁННЫМ")
print("=" * 74)
pend = con.execute("""
    SELECT COUNT(*) FROM messages WHERE sent_at >= ?
      AND delivered_at IS NULL
      AND COALESCE(delivery_state,'') NOT IN ('delivered','rejected')
""", (RUN_START,)).fetchone()[0]
print(f"   без подтверждения доставки: {pend} из {len(rows)}")
print("   (вебхук мёртв — подтверждения не придут вообще)")
con.close()