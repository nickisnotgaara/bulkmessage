"""Скольким ЛЮДЯМ реально отправлено и скольким доставлено.

Три разных вопроса, которые постоянно путают:

  1. ПРИНЯТО   — Wazzup вернул 2xx и выдал message_id.
                 Человеку могло ничего не дойти.
  2. ДОСТАВЛЕНО — есть подтверждение: delivered_at заполнен либо статус
                 delivered/read. Только это означает «дошло до человека».
  3. НАПРАВЛЕНО — есть message_id, но подтверждения может не быть никогда
                 (вебхук молчит). Это «может дошло, а может нет».

Считаем по ЛЮДЯМ (distinct contact_id), не по строкам: человеку могли
отправить в трёх каналах — это один человек, а не три.
"""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "crm.db"
con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

CH = ("waba", "telegram", "max", "whatsapp", "sms")


def one(sql, params=()):
    return con.execute(sql, params).fetchone()[0]


TOTAL = one("SELECT COUNT(*) FROM contacts "
            "WHERE COALESCE(is_test,0)=0 AND COALESCE(is_invalid,0)=0")

print("=" * 76)
print(f"БОЕВОЙ ПУЛ: {TOTAL} контактов")
print("=" * 76)

print()
print(f"{'канал':<11}{'принято':>9}{'людей':>8}{'доставлено':>12}"
      f"{'людей':>8}{'без подтв.':>12}")
print("-" * 76)
rows = {}
for ch in CH:
    acc = one("""SELECT COUNT(*) FROM messages
                WHERE channel=? AND message_id IS NOT NULL
                  AND message_id NOT LIKE 'dryrun%'""", (ch,))
    acc_ppl = one("""SELECT COUNT(DISTINCT contact_id) FROM messages
                     WHERE channel=? AND message_id IS NOT NULL
                       AND message_id NOT LIKE 'dryrun%'""", (ch,))
    dlv = one("""SELECT COUNT(*) FROM messages
                 WHERE channel=? AND (delivered_at IS NOT NULL
                   OR delivery_state='delivered'
                   OR status IN ('delivered','read','answered'))""", (ch,))
    dlv_ppl = one("""SELECT COUNT(DISTINCT contact_id) FROM messages
                     WHERE channel=? AND (delivered_at IS NOT NULL
                       OR delivery_state='delivered'
                       OR status IN ('delivered','read','answered'))""", (ch,))
    rows[ch] = (acc, acc_ppl, dlv, dlv_ppl)
    if acc or dlv:
        print(f"{ch:<11}{acc:>9}{acc_ppl:>8}{dlv:>12}{dlv_ppl:>8}"
              f"{acc - dlv:>12}")

acc_all = sum(r[1] for r in rows.values())
accp_all = sum(r[1] for r in rows.values() if r[1])
dlv_all = sum(r[3] for r in rows.values())
dlvp_all = sum(r[3] for r in rows.values() if r[3])
print("-" * 76)

print()
print("=" * 76)
print("ИТОГ ПО ЛЮДЯМ (без двойного счёта между каналами)")
print("=" * 76)
u_acc = one("""SELECT COUNT(DISTINCT contact_id) FROM messages
               WHERE message_id IS NOT NULL AND message_id NOT LIKE 'dryrun%'""")
u_dlv = one("""SELECT COUNT(DISTINCT contact_id) FROM messages
               WHERE delivered_at IS NOT NULL OR delivery_state='delivered'
                 OR status IN ('delivered','read','answered')""")
u_sent = one("""SELECT COUNT(DISTINCT contact_id) FROM messages
                WHERE status IN ('sent','delivered','read','answered')""")
u_any = one("SELECT COUNT(DISTINCT contact_id) FROM messages")

print(f"   хотя бы раз ПРИНЯТО транспортом : {u_acc} человек")
print(f"   хотя бы раз ДОСТАВЛЕНО           : {u_dlv} человек")
print(f"   статус успеха (sent/delivered/…): {u_sent} человек")
print(f"   вообще была хоть одна строка    : {u_any} человек")
print(f"   ещё НИКОГУДА не писали          : {TOTAL - u_acc}")

print()
print("=" * 76)
print("СКОЛЬКО ЖДЁТ ПОДТВЕРЖДЕНИЯ")
print("=" * 76)
pend = one("""SELECT COUNT(DISTINCT contact_id) FROM messages
              WHERE message_id IS NOT NULL AND message_id NOT LIKE 'dryrun%'
                AND delivered_at IS NULL
                AND COALESCE(delivery_state,'') NOT IN ('delivered','rejected')
                AND status NOT IN ('delivered','read','answered')""")
print(f"   принято, но подтверждения нет: {pend} человек")
print("   (это те, кого через UNKNOWN_GRACE_MINUTES унесёт в откат)")

print()
print("=" * 76)
print("ПО ДНЯМ: сколько реально доставлено людям")
print("=" * 76)
for r in con.execute("""
        SELECT substr(COALESCE(sent_at, created_at),1,10) AS d,
               COUNT(*) AS attempts,
               COUNT(DISTINCT CASE WHEN delivered_at IS NOT NULL
                     OR delivery_state='delivered'
                     OR status IN ('delivered','read','answered')
                     THEN contact_id END) AS people_delivered
        FROM messages
        WHERE message_id IS NOT NULL AND message_id NOT LIKE 'dryrun%'
        GROUP BY d ORDER BY d DESC LIMIT 12
"""):
    conv = 100 * r["people_delivered"] / r["attempts"] if r["attempts"] else 0
    print(f"   {r['d']}  попыток {r['attempts']:>4}  "
          f"доставлено людям {r['people_delivered']:>4}  ({conv:>3.0f}%)")
con.close()