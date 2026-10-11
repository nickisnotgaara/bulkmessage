"""Диагностика MAX-канала: сессия слетела или аккаунт удалён."""
import sys

sys.path.insert(0, "/opt/bulkmessage")

import requests  # noqa: E402

from bulkmessage import config, state, wazzup  # noqa: E402

print("=== 1. Каналы в Wazzup: state / transport ===")
url = config.WAZZUP_BASE_URL + config.WAZZUP_CHANNELS_PATH
try:
    r = requests.get(url, headers=wazzup._headers(), timeout=20)
    print("   HTTP", r.status_code)
    data = r.json()
    items = data.get("data") if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = [items]
    for it in items or []:
        print(f"   {it.get('name', '?')}")
        print(f"      channelId : {it.get('channelId')}")
        print(f"      state     : {it.get('state')}")
        print(f"      transport : {it.get('transport')}")
        print(f"      прочие ключи: {sorted(k for k in it if k not in ('channelId','name','plainId','state','transport'))}")
except Exception as e:
    print("   ОШИБКА запроса каналов:", type(e).__name__, e)

print()
print("=== 2. MAX-канал в halt/счётчиках ===")
st = state.load_state()
print("   halted_channels      :", st.get("halted_channels"))
print("   _overload_streak     :", st.get("_overload_streak"))
print("   _provider_fail_streak:", st.get("_provider_fail_streak"))
print("   sent_today           :", st.get("sent_today"))
print("   is_channel_halted(max):", state.is_channel_halted(st, "max"))
print("   effective cap (max)  :", state.channel_effective_cap(st, "max"))

print()
print("=== 3. Последние ошибки по max (что именно отвечает транспорт) ===")
from bulkmessage import db  # noqa: E402

try:
    rows = db._query_rows(
        "SELECT substr(sent_at,1,19) ts, ct.phone, m.last_error, m.status "
        "FROM messages m JOIN contacts ct ON ct.id=m.contact_id "
        "WHERE m.channel='max' AND m.last_error IS NOT NULL "
        "ORDER BY m.id DESC LIMIT 8"
    )
    for r in rows:
        print(f"   {r['ts']} {r['phone']} [{r['status']}]")
        print(f"      {str(r['last_error'])[:150]}")
    if not rows:
        print("   (ошибок не записано)")
except Exception as e:
    print("   ", e)

print()
print("=== 4. Живая проверка: один запрос к MAX-каналу ===")
print("   (НЕ отправка сообщения — только опрос состояния канала)")
mx_id = wazzup.channel_id_for("max")
print("   MAX channel_id:", mx_id)
try:
    r2 = requests.get(
        config.WAZZUP_BASE_URL + config.WAZZUP_CHANNELS_PATH,
        headers=wazzup._headers(), params={"channelId": mx_id}, timeout=20
    )
    print("   HTTP", r2.status_code, "->", str(r2.text)[:300])
except Exception as e:
    print("   ОШИБКА:", type(e).__name__, e)