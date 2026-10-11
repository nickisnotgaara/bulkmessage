"""Показать, как выглядит новый отчёт по прогону (TG-отправку перехватываем)."""
import sys

sys.path.insert(0, "/opt/bulkmessage")

import bulkmessage.notify as _notify  # noqa: E402
from bulkmessage import probe  # noqa: E402

captured = []
_notify.event = lambda text, level="INFO", **kw: captured.append((level, text))

# Отчёт по тестовому прогону сегодня после 12:35
probe.report_run_summary("2026-10-10T12:35:00+00:00")

print()
print("=" * 60)
print("ТЕКСТ, КОТОРЫЙ УЙДЁТ В TELEGRAM:")
print("=" * 60)
for lvl, txt in captured:
    print(f"[{lvl}]")
    print(txt)