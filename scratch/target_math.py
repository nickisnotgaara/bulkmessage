"""Расклад: сколько WABA-попыток нужно, чтобы закрыть цель в 150 доставленных."""
import sys

sys.path.insert(0, "/opt/bulkmessage")

from bulkmessage import config  # noqa: E402

TG = 50
MX = 10
TARGET = config.TARGET_DELIVERED_PER_DAY
WABA_CAP = config.CHANNEL_DAILY_LIMITS.get("waba")

print("=== КОНФИГ ===")
print(f"   TG лимит          : {config.CHANNEL_DAILY_LIMITS.get('telegram')}")
print(f"   MAX лимит         : {config.CHANNEL_DAILY_LIMITS.get('max')}"
      f"  (hard cap {config.PERSONAL_CHANNEL_HARD_CAPS})")
print(f"   WABA лимит        : {WABA_CAP}")
print(f"   ЦЕЛЬ ДОСТАВОК     : {TARGET}")

print()
print("=== СКОЛЬКО ДОСТАВЛЕНИЙ ДАЮТ ЛИЧНЫЕ КАНАЛЫ ===")
print("   Разведка проверяет наличие аккаунта перед отправкой,")
print("   поэтому в личных каналах отказ 'аккаунта нет' = 0 попыток.")
personal = TG + MX
print(f"   TG  {TG:3d} попыток × ~100% = {TG:3d} доставленных")
print(f"   MAX {MX:3d} попыток × ~100% = {MX:3d} доставленных")
print(f"   ИТОГО гарантированно: {personal}")

need = max(0, TARGET - personal)
print()
print(f"=== ЧТО ДОЛЖЕН ДОБРАТЬ WABA ===")
print(f"   нужно доставленных: {need}")
print()
print("   попытки | конверсия | доставлено | всего в сумме | влезает в 250?")
for rate in (0.26, 0.30, 0.33, 0.40, 0.48, 0.55):
    attempts = -(-need / rate)  # ceil
    got = int(attempts * rate)
    total = personal + got
    fits = "ДА" if attempts <= WABA_CAP else "НЕТ — упрёмся в потолок Meta"
    print(f"   {attempts:8.0f} | {rate*100:8.0f}% | {got:10d} | "
          f"{total:13d} | {fits}")

print()
print("=== ОБРАТИМАЯ ТОЧКА ===")
# Минимальная конверсия, при которой 250 попыток ещё хватает.
min_rate = need / WABA_CAP
print(f"   {WABA_CAP} попыток WABA покрывают {need} доставлений")
print(f"   только если конверсия >= {min_rate*100:.0f}%")
print(f"   факт за 10.10: 6 из 15 = 40%  → запас есть")
print(f"   факт за 06–08.10: 26–33%   → может не хватить")