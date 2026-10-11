"""Проверка порядка фаз и отчёта — без сети и без отправки."""
import sys

sys.path.insert(0, "/opt/bulkmessage")

from bulkmessage import config, sender  # noqa: E402

print("=== 1. Порядок выполнения фаз ===")
active = list(config.PHASE_ORDER) if hasattr(config, "PHASE_ORDER") else []
print("   config.PHASE_ORDER:", active)
for po in (["max", "telegram", "waba"], ["waba", "telegram", "max"],
           ["waba"], ["telegram", "max"]):
    print(f"   {str(po):32s} -> {sender._build_exec_order(list(po))}")

print()
print("=== 2. Условие запуска пробы на фазе 0 ===")
probe_ch = sender._probe_channels()
print("   _probe_channels() =", probe_ch)
order = sender._build_exec_order(["max", "telegram", "waba"])
phase_ch = order[0]
_is_first = True
_wanted = phase_ch == "personal" or phase_ch in probe_ch
print(f"   phase_ch={phase_ch!r} first={_is_first} wanted={_wanted} "
      f"-> проба {'ЗАПУСТИТСЯ' if (_wanted and _is_first) else 'НЕ ЗАПУСТИТСЯ'}")
old = phase_ch in probe_ch
print(f"   СТАРОЕ условие (phase_ch in _probe_channels()) -> "
      f"{'сработало бы' if old else 'НЕ СРАБОТАЛО БЫ (это и был баг)'}")

print()
print("=== 3. Отчёт по покрытию (текст, который уйдёт в TG) ===")
from bulkmessage import probe as _probe  # noqa: E402

# перехватываем notify.event, чтобы увидеть текст и НЕ отправлять его
sent = []
import bulkmessage.notify as _notify  # noqa: E402

_notify.event = lambda text, level="INFO", **kw: sent.append((level, text))
_probe.notify = _notify
_probe.report_pool_coverage()
for lvl, txt in sent:
    print(f"   [{lvl}] {txt}")

print()
print("=== 4. Новые метрики pool_coverage ===")
cov = _probe.pool_coverage()
for k in ("total", "has_telegram", "has_max", "has_whatsapp", "has_all_three",
          "has_none", "blocked", "unknown", "no_whatsapp", "delivered_today"):
    print(f"   {k:16s} {cov.get(k)}")