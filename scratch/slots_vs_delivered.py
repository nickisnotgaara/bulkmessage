"""Разделить два разных числа, которые я смешал: СЛОТЫ и ДОСТАВЛЕННЫЕ.

Слот = сколько отправок влезает в окно при паузе 15+ мин (арифметика времени).
Доставлено = сколько из них реально дошло (конверсия канала).
Это разные вещи, и в сводке я сложил их в одну строку.
"""
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bulkmessage import config, schedule  # noqa: E402

DB = Path(__file__).resolve().parents[1] / "data" / "crm.db"
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

print("=" * 74)
print("A. СЛОТЫ — это арифметика времени, конверсия тут ни при чём")
print("=" * 74)
import random  # noqa: E402
gap, jit = config.PERSONAL_MIN_GAP_SEC, config.PERSONAL_JITTER_MAX
counts = [len(schedule.build_slot_schedule(60, 36000, rng=random.Random(s),
                                           min_gap_seconds=gap, jitter_max=jit))
          for s in range(200)]
print(f"   окно 10 ч, минимум {gap//60} мин, средняя пауза "
      f"{gap*(1+jit)/2/60:.0f} мин")
print(f"   600 мин / {gap*(1+jit)/2/60:.0f} мин = {min(counts)}–{max(counts)} слотов")
print(f"   600 / {gap/60:.0f} = {600/(gap/60):.0f} — потолок при РОВНОМ шаге")
print("   → это НЕ конверсия. Даже при доставке 100% больше 33 не отправить.")

print()
print("=" * 74)
print("B. КОНВЕРСИЯ личных каналов — по базе")
print("=" * 74)
for ch in ("telegram", "max"):
    r = con.execute("""
        SELECT COUNT(*) AS a,
               SUM(CASE WHEN delivery_state='delivered'
                         OR status IN ('delivered','read')
                         OR delivered_at IS NOT NULL THEN 1 ELSE 0 END) AS d
        FROM messages WHERE channel=? AND message_id IS NOT NULL
    """, (ch,)).fetchone()
    print(f"   {ch:<9} всего: {r['a']:3d} принято → {r['d']:3d} доставлено "
          f"({100*r['d']/r['a'] if r['a'] else 0:.0f}%)")

print()
print("   по дням (последние 6):")
for r in con.execute("""
        SELECT substr(COALESCE(sent_at, created_at),1,10) AS dd, channel,
               COUNT(*) AS a,
               SUM(CASE WHEN delivery_state='delivered'
                         OR status IN ('delivered','read')
                         OR delivered_at IS NOT NULL THEN 1 ELSE 0 END) AS d
        FROM messages
        WHERE channel IN ('telegram','max') AND message_id IS NOT NULL
          AND (sent_at IS NOT NULL OR created_at IS NOT NULL)
        GROUP BY dd, channel HAVING a > 0
        ORDER BY dd DESC LIMIT 6
    """):
    print(f"      {r['dd']}  {r['channel']:<9} {r['a']:3d} → {r['d']:3d} "
          f"({100*r['d']/r['a']:3.0f}%)")

print()
print("=" * 74)
print("C. ЧЕСТНЫЙ ИТОГ: слоты × конверсия")
print("=" * 74)
SLOTS = int(statistics.median(counts))
for label, conv in (("100% (ваш оптимизм)", 1.00),
                    ("TG 82% — наша статистика", 0.82),
                    ("смешанно TG+MAX, ~65%", 0.65),
                    ("MAX сейчас в qr, ~0%", 0.00)):
    print(f"   {len(label):<26} {SLOTS} слотов × {conv:>4.0%} = "
          f"{SLOTS*conv:>4.0f} доставленных личных")
print()
print(f"   ИТОГОВЫЙ РАЗБОР за 10 ч (WABA 250 попыток):")
for waba_pct in (26, 33, 53):
    for conv, tag in ((0.82, "TG 82%"), (1.00, "личные 100%")):
        tot = int(250*waba_pct/100) + int(SLOTS*conv)
        print(f"      WABA {waba_pct:2d}% ({int(250*waba_pct/100):3d}) + "
              f"{tag:<11} ({SLOTS*conv:>3.0f}) = {tot:3d}")
con.close()