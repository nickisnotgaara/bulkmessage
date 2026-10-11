"""Почему проба выбрала контакты, которым уже отправляли.

Сравниваем ТРИ разных определения «уже отправляли»:
  1. SQL внутри select_probe_candidates — status IN (sent/delivered/read/answered)
  2. Каноническое db.get_sent_phones() — правило из AGENTS.md §2.3
  3. Факт: строки в messages

Если (1) и (2) расходятся — в проекте две копии одного правила, и это ровно
то расхождение, из-за которого уже было 110 потерянных контактов.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bulkmessage import db  # noqa: E402

DB = Path(__file__).resolve().parents[1] / "data" / "crm.db"
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

TARGETS = ("79167365456", "79656863433")

for phone in TARGETS:
    print("=" * 70)
    print(f"КОНТАКТ {phone}")
    print("=" * 70)
    cid = con.execute("SELECT id FROM contacts WHERE phone=?",
                      (phone,)).fetchone()
    if not cid:
        print("  не найден в contacts")
        continue
    cid = cid["id"]
    for r in con.execute(
        "SELECT id,channel,status,delivery_state,message_id,sent_at "
        "FROM messages WHERE contact_id=? ORDER BY id", (cid,)
    ):
        mid = (r["message_id"] or "")[:20]
        print(f"  #{r['id']:<5} {r['channel']:<9} status={r['status']:<10} "
              f"delivery={str(r['delivery_state']):<10} msg={mid}")

    sql_blocked = con.execute(
        "SELECT COUNT(*) FROM messages m WHERE m.contact_id=? "
        "AND m.status IN ('sent','delivered','read','answered')", (cid,)
    ).fetchone()[0]
    print(f"\n  строк со статусом из SQL-пробы   : {sql_blocked} "
          f"→ {'ЗАБЛОКИРОВАЛ' if sql_blocked else 'НЕ заблокировал'}")
    in_canon = phone in db.get_sent_phones()
    print(f"  есть в db.get_sent_phones()      : {in_canon} "
          f"→ {'ЗАБЛОКИРОВАЛ' if in_canon else 'НЕ заблокировал'}")
    if sql_blocked and not in_canon:
        print("  ⚠️  РАСХОЖДЕНИЕ: две копии правила дают разный ответ")
    print()

print("=" * 70)
print("МАСШТАБ РАСХОЖДЕНИЯ ПО ВСЕЙ БАЗЕ")
print("=" * 70)
canon = db.get_sent_phones()
rows = con.execute("""
    SELECT c.phone,
           SUM(CASE WHEN m.status IN ('sent','delivered','read','answered')
                    THEN 1 ELSE 0 END) AS ok_rows
    FROM contacts c JOIN messages m ON m.contact_id = c.id
    GROUP BY c.id, c.phone
""").fetchall()
# кого SQL-проба считает «не отправляли», хотя канон говорит обратное
missed = [r["phone"] for r in rows
          if (r["ok_rows"] or 0) == 0 and r["phone"] in canon]
print(f"  канонически уже отправляли: {len(canon)}")
print(f"  из них SQL-проба НЕ отсекает: {len(missed)}")
if missed:
    print(f"  примеры: {', '.join(missed[:10])}")
con.close()