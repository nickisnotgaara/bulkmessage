"""Реальная доля попаданий при проверке аккаунта, по накопленной истории.

Вопрос «сколько проверок MAX нужно на 10 живых контактов» — это не мнение,
а измеримая величина: сколько проверок ушло и сколько дали verdict=present.
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "crm.db"
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

print("=" * 72)
print("1. ВЕРДИКТЫ ПРОБ, накопленные в contact_channel_state")
print("=" * 72)
print(f"{'канал':<10}{'проверено':>10}{'present':>10}{'absent':>9}"
      f"{'unknown':>9}{'доля present':>14}")
print("-" * 72)
for r in con.execute("""
        SELECT channel, verdict, COUNT(*) AS n
        FROM contact_channel_state GROUP BY channel, verdict
    """):
    pass
tot = {}
for r in con.execute("""
        SELECT channel, verdict, COUNT(*) AS n
        FROM contact_channel_state GROUP BY channel, verdict
    """):
    d = tot.setdefault(r["channel"], {})
    d[r["verdict"]] = r["n"]
for ch, d in sorted(tot.items()):
    n = sum(d.values())
    p = d.get("present", 0)
    print(f"{ch:<10}{n:>10}{p:>10}{d.get('absent',0):>9}"
          f"{d.get('unknown',0):>9}{(100*p/n if n else 0):>13.0f}%")

print()
print("=" * 72)
print("2. ЧТО ЭТО ЗНАЧИТ ДЛЯ ЛИМИТА ПРОБ MAX")
print("=" * 72)
mx = tot.get("max", {})
n_max = sum(mx.values())
p_max = mx.get("present", 0)
tg = tot.get("telegram", {})
n_tg = sum(tg.values())
p_tg = tg.get("present", 0)

print(f"   MAX     : {n_max} проверок → {p_max} живых "
      f"({100*p_max/n_max if n_max else 0:.1f}%)")
print(f"   Telegram: {n_tg} проверок → {p_tg} живых "
      f"({100*p_tg/n_tg if n_tg else 0:.1f}%)")
print()
if p_max:
    rate = p_max / n_max
    print(f"   При доле {rate*100:.1f}% на 10 живых MAX-контактов нужно "
          f"≈{int(10/rate)} проверок.")
else:
    print("   Живых MAX-контактов в вердиктах нет — оценивать долю нечем.")
    print("   Единственный источник: реальные пробы, которых почти не было.")

print()
print("=" * 72)
print("3. Сколько контактов вообще доступно для проверок MAX")
print("=" * 72)
for r in con.execute("""
        SELECT verdict, COUNT(DISTINCT contact_id) AS n
        FROM contact_channel_state
        WHERE channel='max' GROUP BY verdict
    """):
    print(f"   MAX {r['verdict']:<9}: {r['n']:>5} контактов")
tot_c = con.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
print(f"   всего контактов в базе: {tot_c}")

print()
print("=" * 72)
print("4. ПОТОЛОК СООБЩЕНИЙ MAX = 10. Проба-успех = реальное сообщение.")
print("=" * 72)
print("   Проверок можно делать больше 10, но успешных из них будет не больше")
print("   10 — каждая удачная проба списывает квоту сообщения.")
print("   Значит лимит проб задаёт не число доставок, а число БЕСПОЛЕЗНЫХ")
print("   обращений к номерам без MAX.")
con.close()