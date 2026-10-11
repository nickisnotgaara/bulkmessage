import sys, inspect
from pathlib import Path
sys.path.insert(0, ".")
from bulkmessage import sender, db
src = inspect.getsource(sender._collect_fresh_waba_targets)
markers = ("get_sent_phones", "contacted_phones", "status IN ('sent'", "last_sent_by_phone")
found = [m for m in markers if m in src]
print("=" * 70)
print("ФИЛЬТР 'уже отправляли' В ФАЗЕ WABA")
print("=" * 70)
print("  найденные маркеры:", found or "НИ ОДНОГО")
print("  вывод:", "фильтр ЕСТЬ" if found else "фильтра НЕТ — фаза только СОРТИРУЕТ")
print()
print("  _never_sent_first (сортировка) в коде:", "_never_sent_first" in src)
