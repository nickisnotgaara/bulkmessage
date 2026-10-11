"""Разобрать два разных вопроса, которые перепутались.

1. Эти ДВА контакта — им реально что-то доставляли раньше?
2. А что с теми 192, кто попал в пул «кому отправляли раньше» —
   дойдёт ли до них WABA-фаза?

Вопрос 1 и вопрос 2 — разные. Первый про двух конкретных людей,
второй про политику отбора в фазе WABA.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bulkmessage import db  # noqa: E402

DB = Path(__file__).resolve().parents[1] / "data" / "crm.db"
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

print("=" * 72)
print("1. ЭТИ ДВА: доставлялось ли что-то ДО сегодняшнего прогона?")
print("=" * 72)
for phone in ("79167365456", "79656863433"):
    rows = con.execute("""
        SELECT m.id, m.channel, m.status, m.delivery_state, m.message_id
        FROM messages m JOIN contacts c ON c.id = m.contact_id
        WHERE c.phone = ? AND m.id NOT IN (1974, 1975)
    """, (phone,)).fetchall()
    accepted = [r for r in rows if r["message_id"]]
    delivered = [r for r in rows if r["delivery_state"] == "delivered"
                 or r["status"] in ("delivered", "read", "answered")]
    print(f"  {phone}: попыток {len(rows)}, "
          f"принято Wazzup (есть message_id) {len(accepted)}, "
          f"ДОСТАВЛЕНО {len(delivered)}")
    print(f"     → {'никто ничего не получал' if not delivered else 'ПОЛУЧАЛИ!'}")

print()
print("=" * 72)
print("2. А 192 «кому отправляли раньше» — дойдёт ли до них WABA?")
print("=" * 72)
last_seen = db.last_sent_by_phone()
print(f"  db.last_sent_by_phone() вернул {len(last_seen)} номеров с историей")

# Кто из них реально ПОЛУЧИЛ сообщение (успешная доставка)
real_delivered = con.execute("""
    SELECT COUNT(DISTINCT c.id) FROM contacts c JOIN messages m
      ON m.contact_id = c.id
    WHERE m.status IN ('delivered','read','answered')
       OR m.delivery_state = 'delivered'
""").fetchone()[0]
accepted_only = con.execute("""
    SELECT COUNT(DISTINCT c.id) FROM contacts c JOIN messages m
      ON m.contact_id = c.id
    WHERE m.message_id IS NOT NULL
      AND m.message_id NOT LIKE 'dryrun%'
      AND m.status NOT IN ('delivered','read','answered')
      AND COALESCE(m.delivery_state,'') != 'delivered'
""").fetchone()[0]

print(f"  контактов с подтверждённой ДОСТАВКОЙ : {real_delivered}")
print(f"  контактов «принято, но не доставлено»  : {accepted_only}")

# Проверяем ФАЗУ WABA: есть ли в ней вообще фильтр «уже отправляли»
from bulkmessage import sender  # noqa: E402
src = db.__class__.__module__
import inspect  # noqa: E402
waba_src = inspect.getsource(sender._collect_fresh_waba_targets)
has_filter = ("get_sent_phones" in waba_src
              or "contacted_phones" in waba_src
              or "status IN ('sent'" in waba_src)
              or "last_sent_by_phone" in waba_src)

print()
print("=" * 72)
print("3. ФИЛЬТР В ФАЗЕ WABA")
print("=" * 72)
print(f"  есть ли в _collect_fresh_waba_targets фильтр «уже отправляли»: "
      f"{'ДА' if has_filter else 'НЕТ'}")
if not has_filter:
    print("  → она только СОРТИРУЕТ (никогда → давно), но НЕ ИСКЛЮЧЕТ.")
    print("  → значит WABA-фаза ДОШЛА БЫ до контактов с прошлыми отправками.")
con.close()