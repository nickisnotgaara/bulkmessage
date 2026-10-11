"""Фактическая конверсия WABA/TG/MAX по дням из локальной копии боевой БД.

Только SELECT и только в read-only режиме: файл боевой, даже копия не должна
меняться от диагностики.
"""
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "crm.db"
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

CHANNELS = ("waba", "telegram", "max")

print("=" * 78)
print("ПОПЫТКИ И ДОСТАВКИ ПО ДНЯМ (channel → день)")
print("=" * 78)
print(f"{'день':<12}{'канал':<10}{'попыток':>9}{'доставлено':>12}"
      f"{'конверсия':>12}{'принято(2xx)':>13}")
print("-" * 78)

agg = defaultdict(lambda: defaultdict(int))
for r in con.execute("""
        SELECT channel,
               substr(COALESCE(sent_at, created_at), 1, 10) AS d,
               COUNT(*)                                        AS attempts,
               SUM(CASE WHEN delivery_state = 'delivered'
                       OR status IN ('delivered','read')
                       OR delivered_at IS NOT NULL
                       THEN 1 ELSE 0 END)                    AS delivered
        FROM messages
        WHERE channel IN ('waba','telegram','max')
          AND message_id IS NOT NULL
        GROUP BY channel, d
        ORDER BY d DESC, channel
        """):
    agg[r["d"]][r["channel"]] = (r["attempts"], r["delivered"] or 0)

tot = defaultdict(lambda: [0, 0])
for day in sorted(agg, reverse=True)[:12]:
    for ch in CHANNELS:
        a, d = agg[day].get(ch, (0, 0))
        if not a:
            continue
        tot[ch][0] += a
        tot[ch][1] += d
        pct = 100 * d / a if a else 0
        print(f"{day:<12}{ch:<10}{a:>9}{d:>12}{pct:>11.0f}%{'':>13}")

print("-" * 78)
print(f"{'ВСЕГО':<12}{'':<10}", end="")
for ch in CHANNELS:
    a, d = tot[ch]
    pct = 100 * d / a if a else 0
    print(f"{ch}: {a} попыт. → {d} дост. ({pct:.0f}%)", end="   ")
print()

print()
print("=" * 78)
print("ЧТО ЭТО ЗНАЧИТ ДЛЯ ЦЕЛИ 150 ДОСТАВЛЕННЫХ В 10 ЧАСОВ")
print("=" * 78)
WABA_DAILY = 250
PERSONAL_SLOTS = 33
print(f"   личные каналы в 10-часовом окне при минимуме 15 мин: ~{PERSONAL_SLOTS}")
print()
for pct in (26, 33, 40):
    w = int(WABA_DAILY * pct / 100)
    print(f"   WABA {WABA_DAILY} попыток × {pct:2d}% = {w:3d} доставленных "
          f"+ {PERSONAL_SLOTS} личных = {w + PERSONAL_SLOTS:3d}  "
          f"{'ДОСТАТОЧНО' if w + PERSONAL_SLOTS >= 150 else 'НЕ ХВАТАЕТ'}")
print()
print("   если Meta подтвердит компанию (tier 0 → tier 1, лимит 250 → 1000):")
for pct in (26, 33, 40):
    w = int(1000 * pct / 100)
    print(f"   WABA 1000 попыток × {pct:2d}% = {w:3d} + {PERSONAL_SLOTS} = "
          f"{w + PERSONAL_SLOTS:3d}  {'ДОСТАТОЧНО' if w + PERSONAL_SLOTS >= 150 else 'НЕ ХВАТАЕТ'}")
print()
print(f"   нужно попыток WABA, чтобы личный остаток {_required if (_required := 150 - PERSONAL_SLOTS) else 0} "
      f"закрыть: при 33% это {int(_required / 0.33)} попыток в сутки")

con.close()