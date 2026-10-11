"""Проверка на сервере: рендер всегда несёт 25% и нет мусорных вариантов."""
import sys

sys.path.insert(0, "/opt/bulkmessage")

from bulkmessage import templates  # noqa: E402

tpl = templates.load_templates()
print("categories:", list(tpl.keys()))
print()

bad = 0
total = 0
for cat in tpl:
    for i in range(60):
        c = {"category": cat, "name": "Тест"}
        t = templates.build_message(c, tpl, variant_index=i)
        total += 1
        if "25%" not in t:
            bad += 1
            if bad <= 5:
                print("  NO-25%:", t[:130])
        if "вас поучаствовать" in t or "Что думаете" in t:
            print("  GARBAGE:", t[:130])

print(f"checked {total} expansions, without 25%: {bad}")
print()

print("=== samples ===")
for cat in tpl:
    for i in (0, 1, 2):
        c = {"category": cat, "name": "Валентина"}
        print(f"[{cat} i={i}]", templates.build_message(c, tpl, variant_index=i)[:170])

print()
print("=== config check ===")
from bulkmessage import config  # noqa: E402
print("DRY_RUN            =", config.DRY_RUN)
print("WABA_RUN_LIMIT     =", config.WABA_RUN_LIMIT)
print("LIMIT_WABA         =", config.CHANNEL_DAILY_LIMITS.get("waba"))
print("RESTRICTED (server)=", getattr(config, "WABA_RESTRICTED_TEMPLATES", "n/a (old code)"))