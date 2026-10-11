"""Проверить ДО запуска: кого именно возьмёт проба после skip=50.

Обязательная проверка перед боевой отправкой: каждый выбранный контакт
не должен иметь НИ ОДНОЙ строки с message_id. Если есть хоть одна —
владелец должен увидеть это ДО того, как человеку уйдёт сообщение.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bulkmessage import config, notify, sender  # noqa: E402

SKIP = 50
LIMIT = 10

cands = sender.select_probe_candidates(limit=LIMIT, channel="telegram",
                                       skip_first=SKIP)

print("=" * 72)
print(f"КАНДИДАТЫ НА ПРОБУ: пропустить {SKIP}, взять {LIMIT}")
print("=" * 72)
print(f"{'#':<4}{'телефон':<17}{'имя':<22}{'история отправок'}")
print("-" * 72)

DB = Path(__file__).resolve().parents[1] / "data" / "crm.db"
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

bad = 0
for i, c in enumerate(cands, 1):
    row = con.execute("""
        SELECT COUNT(*) AS n FROM messages
        WHERE contact_id=? AND message_id IS NOT NULL
          AND message_id NOT LIKE 'dryrun%'
    """, (c["contact_id"],)).fetchone()
    n = row["n"]
    if n:
        bad += 1
    mark = f"🔴 {n} строк!" if n else "🟢 чисто"
    print(f"{i:<4}{notify.pretty_phone(c['phone']):<17}"
          f"{(c.get('name') or '—')[:20]:<22}{mark}")

print("-" * 72)
print(f"  контактов с историей отправок: {bad} из {len(cands)}")
print(f"  {'✅ БЕЗОПАСНО — никому из списка раньше не отправляли'
      if bad == 0 else '❌ ОСТАНОВИТЬСЯ — есть контакты с историей!'}")

# Сколько всего доступно кандидатов с учётом жёсткого фильтра
total_ok = con.execute("""
    SELECT COUNT(*) FROM contacts c
    WHERE COALESCE(c.is_test,0)=0 AND COALESCE(c.is_invalid,0)=0
      AND COALESCE(c.consent,'unknown') != 'denied'
      AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.contact_id=c.id
                      AND m.status IN ('sent','delivered','read','answered'))
      AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.contact_id=c.id
                      AND m.message_id IS NOT NULL
                      AND m.message_id NOT LIKE 'dryrun%')
""").fetchone()[0]
print()
print(f"  всего контактов проходят жёсткий фильтр: {total_ok}")
print(f"  (из пула {config.DB_PATH})")
con.close()