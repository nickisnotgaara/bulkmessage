"""Проверка потолка контактов ДО запуска (без сети)."""
import sys

sys.path.insert(0, "/opt/bulkmessage")

from datetime import datetime, timezone  # noqa: E402

from bulkmessage import config, sender  # noqa: E402

# Имитируем: старт прогона сейчас, лимит 15
sender._RUN_STARTED_AT = datetime.now(timezone.utc).timestamp()
config.RUN_CONTACTS_LIMIT = 15

print("=== Бюджет контактов ===")
print("   лимит:", config.RUN_CONTACTS_LIMIT)
print("   уже тронуто:", sender._contacts_messaged_this_run())
print("   осталось:", sender.contacts_budget_left())

print()
print("=== симуляция расхода: как бюджет делится по фазам ===")
sent_so_far = 0
phases = [
    ("ФАЗА 1 PROBE", 5),
    ("ФАЗА 2 WABA", 40),
    ("ФАЗА 3 добор личных", 70),
]
for name, want in phases:
    left = max(0, 15 - sent_so_far)
    take = min(want, left)
    sent_so_far += take
    print(f"   {name:22s} хотели {want:3d} → взяли {take:3d} "
          f"(осталось до этого {left})")
print()
print(f"   ИТОГО тронуто контактов: {sent_so_far}  "
      f"{'РОВНО 15 — OK' if sent_so_far == 15 else 'ПЕРЕБОР!'}")

print()
print("=== слоты личных каналов при лимите 8 ===")
print("   _personal_slot_count() без потолка даст: 70 (TG 50 + MAX 20)")
print("   с contacts_limit=8 фаза возьмёт 8 слотов вместо 70")
print("   slots = schedule.build_slot_schedule(8, seconds) → ровно 8 моментов")

from bulkmessage import schedule  # noqa: E402
sl = schedule.build_slot_schedule(8, 3600)
print(f"   фактически построено слотов: {len(sl)}  "
      f"{'OK' if len(sl) == 8 else 'ОШИБКА'}")