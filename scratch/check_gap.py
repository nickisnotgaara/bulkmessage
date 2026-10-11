"""Проверка: 15 минут — это гарантированный минимум, а не среднее."""
import random
import sys

sys.path.insert(0, "/opt/bulkmessage")

from bulkmessage import config, schedule  # noqa: E402

print("=== КОНФИГ ===")
print(f"   PERSONAL_MIN_GAP_SEC : {config.PERSONAL_MIN_GAP_SEC}с "
      f"= {config.PERSONAL_MIN_GAP_SEC//60} мин")
print(f"   WABA_DELAY          : {config.WABA_DELAY_MIN}–{config.WABA_DELAY_MAX}с "
      "(Meta Business API, личный бан не грозит)")

print()
print("=== 1. ГАРАНТИЯ МИНИМУМА (1000 случайных прогонов) ===")
GAP = config.PERSONAL_MIN_GAP_SEC
WINDOW = 600 * 60  # 10 часов
violations = 0
counts = set()
for seed in range(1000):
    sl = schedule.build_slot_schedule(
        60, WINDOW, rng=random.Random(seed), min_gap_seconds=GAP
    )
    counts.add(len(sl))
    gaps = [b - a for a, b in zip(sl, sl[1:])]
    if any(g < GAP for g in gaps):
        violations += 1
print(f"   прогоны с интервалом КОРОЧЕ {GAP}с: {violations}  "
      f"{'OK — минимум держится' if violations == 0 else 'ПРОБЛЕМА!'}")
print(f"   число слотов всегда: {counts}")

print()
print("=== 2. СКОЛЬКО ВЛЕЗАЕТ В 10 ЧАСОВ ===")
for n in (40, 50, 60, 70):
    sl = schedule.build_slot_schedule(
        n, WINDOW, rng=random.Random(1), min_gap_seconds=GAP
    )
    gaps = [round(b - a) for a, b in zip(sl, sl[1:])]
    if gaps:
        print(f"   просили {n:3d} → получили {len(sl):3d} слотов | "
              f"интервалы мин {min(gaps)//60} мин, макс {max(gaps)//60} мин")
    else:
        print(f"   просили {n:3d} → получили {len(sl):3d} слотов")

print()
print("=== 3. РАНДОМИЗАЦИЯ ЖИВАЯ? (интервалы не одинаковые) ===")
sl = schedule.build_slot_schedule(
    40, WINDOW, rng=random.Random(7), min_gap_seconds=GAP
)
gaps = [round(b - a) for a, b in zip(sl, sl[1:])]
uniq = len(set(gaps))
print(f"   39 интервалов, уникальных значений: {uniq}  "
      f"{'OK — разброс настоящий' if uniq > 20 else 'ПРОБЛЕМА: почти равные'}")
print(f"   пример: {[g//60 for g in gaps[:10]]} минут")
print(f"   min={min(gaps)//60} мин (должно быть ≥15)")

print()
print("=== 4. ИТОГОВЫЙ РАСКЛАД ===")
personal = len(schedule.build_slot_schedule(
    60, WINDOW, rng=random.Random(1), min_gap_seconds=GAP))
print(f"   личные за 10 ч при 15-мин минимуме : {personal} доставленных")
print(f"   WABA 250 попыток × 40%             : 100 доставленных")
print(f"   ИТОГО                              : {personal + 100}")
print(f"   (чтобы вышло 150, окно нужно 15 ч → 60 личных)")