"""Проверка рендера: главное — всегда ли есть 25% и нет ли битой грамматики."""
import re
import sys

sys.path.insert(0, "/opt/bulkmessage")

from bulkmessage import templates  # noqa: E402

tpl = templates.load_templates()

# Битое: предложить/пригласить ВАС поучаствовать. Корректно: «Приглашаю вас поучаствовать».
BAD_PATTERNS = [
    r"предложить вас",
    r"предложить вас",
    r"Что думаете",
    r"продолжаю работаю",
    r"продолжаю занимаюсь",
    r"Хочу предложить вас",
]

bad_25, bad_grammar = 0, []
total = 0
for cat in tpl:
    for i in range(80):
        t = templates.build_message({"category": cat, "name": "Тест"},
                                    tpl, variant_index=i)
        total += 1
        if "25%" not in t:
            bad_25 += 1
            if bad_25 <= 3:
                print("  NO-25%:", t[:140])
        for p in BAD_PATTERNS:
            if re.search(p, t):
                bad_grammar.append((p, t[:140]))
                break

print(f"checked {total} expansions")
print(f"  WITHOUT 25% : {bad_25}")
print(f"  BAD GRAMMAR : {len(bad_grammar)}")
for p, t in bad_grammar[:3]:
    print(f"    [{p}] {t}")
print()

print("=== 2 samples per category ===")
for cat in tpl:
    for i in (0, 5):
        t = templates.build_message({"category": cat, "name": "Валентина"},
                                    tpl, variant_index=i)
        print(f"[{cat} i={i}] {t}")
    print()

print("=== config ===")
from bulkmessage import config  # noqa: E402
print("DRY_RUN        =", config.DRY_RUN)
print("WABA_RUN_LIMIT =", config.WABA_RUN_LIMIT)
print("LIMIT_WABA     =", config.CHANNEL_DAILY_LIMITS.get("waba"))