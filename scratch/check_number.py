"""Проверка номера 79771501819 по всем трём транспортам (только чтение)."""
import sqlite3
import sys

sys.path.insert(0, "/opt/bulkmessage")

import requests  # noqa: E402

from bulkmessage import config, wazzup  # noqa: E402

PHONE = "79771501819"

print("=" * 66)
print(f"СТАТУС КАНАЛОВ WAZZUP (имя канала = номер аккаунта)")
print("=" * 66)
url = config.WAZZUP_BASE_URL + config.WAZZUP_CHANNELS_PATH
r = requests.get(url, headers=wazzup._headers(), timeout=20)
data = r.json()
items = data.get("data") if isinstance(data, dict) else data
if isinstance(items, dict):
    items = [items]

for it in items or []:
    print(f"  имя канала : {it.get('name')}")
    print(f"  transport  : {it.get('transport')}")
    print(f"  STATE      : {it.get('state')}      ← состояние сессии")
    print(f"  channelId  : {it.get('channelId')}")
    print()

print("=" * 66)
print("ЧТО ЭТО ЗНАЧИТ ПО КАЖДОМУ ТРАНСПОРТУ")
print("=" * 66)
print("  active = сессия авторизована, канал рабочий")
print("  qr     = сессия отвалилась, Wazzup просит пересканировать QR")
print()

print("=" * 66)
print("ИСТОРИЯ СООБЩЕНИЙ С ЭТИМ НОМЕРОМ (если он был в базе)")
print("=" * 66)
con = sqlite3.connect("file:/opt/bulkmessage/data/crm.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT m.sent_at, m.channel, m.status, m.delivery_state, m.message_text "
    "FROM messages m JOIN contacts ct ON ct.id=m.contact_id "
    "WHERE ct.phone = ? ORDER BY m.sent_at DESC LIMIT 8", (PHONE,)
).fetchall()
if not rows:
    print("  (этот номер не найден среди контактов — он не получал нашу рассылку)")
for x in rows:
    print(f"  {str(x['sent_at'])[:19]} [{x['channel']}] {x['status']} "
          f"del={x['delivery_state']}")
    print(f"     {(x['message_text'] or '')[:110]}")

print()
print("=" * 66)
print("СВЯЗАННЫЕ КОНТАКТЫ ПО ТОЙ ЖЕ ЦИФРЕ (совпадения номеров)")
print("=" * 66)
rows2 = con.execute(
    "SELECT id, phone, name, category FROM contacts "
    "WHERE phone LIKE '%1501819%' LIMIT 10"
).fetchall()
if not rows2:
    print("  (нет совпадений в contacts)")
for x in rows2:
    print(f"  id={x['id']} {x['phone']} {x['name']} [{x['category']}]")