"""Сброс дневных счётчиков для чистого тестового прогона (бэкап state)."""
import json
import shutil
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "/opt/bulkmessage")

P = "/opt/bulkmessage/data/broadcast_state.json"
shutil.copy2(P, f"{P}.bak-testrun-{time.strftime('%Y%m%d-%H%M%S')}")

with open(P, encoding="utf-8") as f:
    st = json.load(f)

print("ДО сброса:")
for k in ("date", "contacts_today", "attempts_today", "successful_today",
          "sent_today", "accepted_today", "delivered_today", "failed_today"):
    print(f"   {k} = {json.dumps(st.get(k), ensure_ascii=False)[:110]}")

for k in ("contacts_today", "attempts_today", "successful_today",
          "sent_today", "accepted_today", "delivered_today", "failed_today"):
    st[k] = 0 if not isinstance(st.get(k), dict) else {}

st["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

with open(P, "w", encoding="utf-8") as f:
    json.dump(st, f, ensure_ascii=False)

print("\nПОСЛЕ сброса:")
for k in ("date", "contacts_today", "attempts_today", "successful_today",
          "sent_today", "accepted_today", "delivered_today", "failed_today"):
    print(f"   {k} = {json.dumps(st.get(k), ensure_ascii=False)[:110]}")
print("\nБэкап:", f"{P}.bak-testrun-*")