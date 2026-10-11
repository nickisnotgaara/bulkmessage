"""ПРЕДПРОСМОТР: кто именно получит сообщение. Проверка на повторы."""
import sqlite3
import sys

sys.path.insert(0, "/opt/bulkmessage")

from bulkmessage import config, sender, state  # noqa: E402

OK = ("sent", "delivered", "read", "answered")
con = sqlite3.connect("file:/opt/bulkmessage/data/crm.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row

N = int(sys.argv[1]) if len(sys.argv) > 1 else 15


def prior_messages(contact_id):
    return con.execute(
        "SELECT COUNT(*) FROM messages WHERE contact_id = ? "
        "AND status IN (?,?,?,?)", (contact_id, *OK)
    ).fetchone()[0]


print("=" * 72)
print(f"ПРЕДПРОСМОТР ПРОГОНА · {N} контактов")
print("=" * 72)

# ── Фаза 1: PROBE ──────────────────────────────────────────────
probe = sender.select_probe_candidates(limit=N)
print(f"\nФАЗА 1 · PROBE — кандидатов: {len(probe)}")
print("   (проба САМА отправляет сообщение тем, у кого аккаунт есть)")
bad = 0
for c in probe:
    pr = prior_messages(c["contact_id"])
    if pr:
        bad += 1
    tg = con.execute(
        "SELECT verdict FROM contact_channel_state WHERE contact_id=? AND channel='telegram'",
        (c["contact_id"],)).fetchone()
    mx = con.execute(
        "SELECT verdict FROM contact_channel_state WHERE contact_id=? AND channel='max'",
        (c["contact_id"],)).fetchone()
    print(f"   {c['phone']} {str(c['name'])[:18]:18s} {str(c['category'])[:14]:14s} "
          f"tg={tg['verdict'] if tg else '-':9s} mx={mx['verdict'] if mx else '-':9s} "
          f"история={pr}{'  <<< ПОВТОР!' if pr else ''}")
print(f"   ПОВТОРОВ В СПИСКЕ: {bad}  {'OK' if bad == 0 else 'СТОП'}")

# ── Фаза 2: WABA ───────────────────────────────────────────────
st = state.load_state()
pool = sender._select_contacts_for_cycle()
waba = sender._collect_fresh_waba_targets(pool, st)
print(f"\nФАЗА 2 · WABA — целей доступно: {len(waba)}, берём первые {N}")
bad2 = 0
for t in waba[:N]:
    pr = prior_messages(t["id"])
    if pr:
        bad2 += 1
    print(f"   {t['phone']} {str(t.get('name'))[:18]:18s} {str(t.get('category'))[:14]:14s} "
          f"история={pr}{'  <<< ПОВТОР!' if pr else ''}")
print(f"   ПОВТОРОВ В СПИСКЕ: {bad2}  {'OK' if bad2 == 0 else 'СТОП'}")

# ── Кто вообще НЕ должен попасть никуда ────────────────────────
print(f"\n" + "=" * 72)
print("КОНТРОЛЬНАЯ ПРОВЕРКА ПУЛА")
already = con.execute(
    "SELECT COUNT(*) FROM contacts c WHERE EXISTS ("
    "  SELECT 1 FROM messages m WHERE m.contact_id=c.id AND m.status IN (?,?,?,?))",
    OK).fetchone()[0]
print(f"   контактов с историей в базе: {already}")
print(f"   всего контактов: {con.execute('SELECT COUNT(*) FROM contacts').fetchone()[0]}")
print(f"   из них НЕ написанных: {con.execute('SELECT COUNT(*) FROM contacts').fetchone()[0] - already}")
print("   Ни один из этих не должен появиться в списках выше.")
print("=" * 72)