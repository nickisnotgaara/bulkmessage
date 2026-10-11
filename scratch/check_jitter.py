"""Проверка гарантии «не меньше 15 минут» + живости разброса.

Чистая математика: ни БД, ни сети, ни config. Всегда можно запускать без
опа��ки, что что-то уйдёт реальному человеку.
"""
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bulkmessage import schedule  # noqa: E402

GAP = 900          # PERSONAL_MIN_GAP_SEC
JITTER = 1.4       # PERSONAL_JITTER_MAX
WINDOW = 10 * 3600  # 10-часовое окно
MAX_SLOTS = 60     # лимиты каналов: TG 50 + MAX 10

print("=" * 68)
print("1. ГАРАНТИЯ МИНИМУМА — 1000 случайных прогонов")
print("=" * 68)
violations = 0
counts = {}
for seed in range(1000):
    sl = schedule.build_slot_schedule(
        MAX_SLOTS, WINDOW, rng=random.Random(seed),
        min_gap_seconds=GAP, jitter_max=JITTER,
    )
    counts[len(sl)] = counts.get(len(sl), 0) + 1
    gaps = [b - a for a, b in zip(sl, sl[1:])]
    if gaps and min(gaps) < GAP - 0.001:
        violations += 1
print(f"   прогонов с интервалом короче {GAP//60} мин: {violations}  "
      f"{'OK — пол держится' if violations == 0 else 'ПРОБЛЕМА!'}")
print(f"   число слотов по прогонам: {dict(sorted(counts.items()))}")

print()
print("=" * 68)
print("2. ЖИВОЙ РАЗБРОС — интервалы не должны быть одинаковыми")
print("=" * 68)
uniq_all = set()
for seed in range(50):
    sl = schedule.build_slot_schedule(
        MAX_SLOTS, WINDOW, rng=random.Random(seed),
        min_gap_seconds=GAP, jitter_max=JITTER,
    )
    gaps = [round(b - a) for a, b in zip(sl, sl[1:])]
    if gaps:
        uniq_all |= set(gaps)
sl = schedule.build_slot_schedule(
    MAX_SLOTS, WINDOW, rng=random.Random(7),
    min_gap_seconds=GAP, jitter_max=JITTER,
)
gaps = [round(b - a) for a, b in zip(sl, sl[1:])]
print(f"   один прогон: {len(gaps)} интервалов, уникальных {len(set(gaps))}  "
      f"{'OK' if len(set(gaps)) > 20 else 'ПРОБЛЕМА: почти равные'}")
print(f"   50 прогонов: уникальных значений интервала всего {len(uniq_all)}")
print(f"   пример (минуты): {[g // 60 for g in gaps[:12]]}")
print(f"   min={min(gaps)//60} мин (должно быть >= {GAP//60}), "
      f"медиана={statistics.median(gaps)//60}, max={max(gaps)//60}")

print()
print("=" * 68)
print("3. СКОЛЬКО ЛИЧНЫХ СООБЩЕНИЙ ВЛЕЗАЕТ В ОКНО")
print("=" * 68)
for hours in (10, 12, 15):
    w = hours * 3600
    fits = []
    for seed in range(200):
        sl = schedule.build_slot_schedule(
            MAX_SLOTS, w, rng=random.Random(seed),
            min_gap_seconds=GAP, jitter_max=JITTER,
        )
        fits.append(len(sl))
    print(f"   окно {hours:2d} ч → {min(fits)}–{max(fits)} личных сообщений "
          f"(лимит каналов {MAX_SLOTS})")

print()
print("=" * 68)
print("4. ИТОГОВЫЙ РАСКЛАД НА СУТКИ")
print("=" * 68)
personal = min(fits) if hours == 15 else 33
for pct in (26, 33, 40):
    waba = int(250 * pct / 100)
    print(f"   личные ~33 + WABA 250×{pct}% = {waba:3d}  →  итого "
          f"{33 + waba:3d} доставленных  "
          f"{'ЦЕЛЬ 150' if 33 + waba >= 150 else 'НЕ хватает'}")
print(f"   при tier 1 (1000/сутки) и конверсии 33%: "
      f"33 + {int(1000*0.33)} = {33 + int(1000*0.33)} доставленных — цель 150 "
      f"перекрыта с запасом")