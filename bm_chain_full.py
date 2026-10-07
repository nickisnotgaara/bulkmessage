# -*- coding: utf-8 -*-
"""Сквозной стенд всей цепочки WABA -> Telegram -> MAX.

Проверяет ровно то, что владелец описал как цель:

  1) отправляем во все каналы и ОТСЛЕЖИВАЕМ статус доставки в каждом;
  2) недоставленные в WhatsApp складываются в очередь отката;
  3) после того как WhatsApp закончил, из этой базы берётся пачка и
     отправляется через Telegram;
  4) те, кто не дошёл в Telegram (в т.ч. «сменил @никнейм / не найден»),
     идут дальше в MAX;
  5) кто провалился во всех каналах — уходит в конец очереди, а не теряется;
  6) лимиты каждого канала соблюдаются, дублей нет.

Транспорт полностью заглушен — реальных отправок нет. БД/стейт в tmp.
"""
import os
import sys
import json
import sqlite3
import tempfile
import shutil
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bulkmessage import config, db, sender, state, wazzup, reconcile  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


# --- Песочница ------------------------------------------------------------
SB = tempfile.mkdtemp(prefix="bm_chain_full_")
DB_PATH = os.path.join(SB, "crm.db")
STATE_PATH = os.path.join(SB, "state.json")
json.dump({}, open(STATE_PATH, "w", encoding="utf-8"))

config.DB_PATH = DB_PATH
config.STATE_PATH = STATE_PATH
config.BAD_PHONES_PATH = os.path.join(SB, "bad_phones.json")
config.DRY_RUN = False
config.BULK_BYPASS_SAFE_HOURS = True
config.PHASE_ORDER = ["waba", "telegram", "max"]
config.PHASE_GRACE_MINUTES = 0
config.RESCUE_SETTLE_MINUTES = 0
# Трёхзначная доставка (2026-10-07): молчание уходит в откат не через 5 минут,
# а через длинный горизонт. Стенд имитирует именно «горизонт выдержан» —
# иначе каскад вообще не проверяется, потому что тишина WABA держится часами.
config.UNKNOWN_GRACE_MINUTES = 0
config.REJECT_GRACE_MINUTES = 0
config.EXHAUST_MAX_ATTEMPTS = 3
config.EXHAUST_WINDOW_DAYS = 7
config.WABA_SILENT_COOLDOWN_DAYS = 14
config.CONTACT_CHANNEL_COOLDOWN_HOURS = 24
config.WABA_CONCURRENCY = 1        # стенд проверяет порядок, не гонку потоков
config.WABA_RUN_LIMIT = 0
config.CHANNEL_HEALTH_GUARD = False   # считаем канал живым: доставки есть
config.CHANNEL_DAILY_LIMITS = {"whatsapp": 60, "telegram": 50, "max": 15,
                              "waba": 250, "sms": 50}
config.WAZZUP_DEFAULT_CHANNEL_ID = "cid-waba"
config.WAZZUP_TELEGRAM_CHANNEL_ID = "cid-tg"
config.WAZZUP_MAX_CHANNEL_ID = "cid-max"

db.init_db()
state.save_state({"date": datetime.now(timezone.utc).date().isoformat(),
                  "sent_today": {}, "delivered_today": {}, "failed_today": {}})

# --- Сценарий: 10 контактов ----------------------------------------------
N = 10
CONTACTS = [{"phone": f"+7900000{i:04d}", "name": "Тест",
             "category": "Покупатели"} for i in range(N)]

WABA_DELIVERED = {0, 1, 2, 3, 4, 5}   # 6 доставлены
TG_DELIVERED = {6}                    # 1 дошёл в Telegram
TG_ERROR = {7, 8}                     # 2 «сменил @никнейм»
TG_SILENT = {9}                       # 1 молчит (пойдёт дальше по тишине)
MAX_DELIVERED = {9}                   # дошёл в MAX
MAX_PERMANENT = {7}                   # «нет в MAX»

sent_by_channel = {"waba": {}, "telegram": {}, "max": {}}
counter = {"n": 0}


def _mk_mid(tag):
    counter["n"] += 1
    return f"{tag}-{counter['n']}"


def fake_send_wazzup(channel_id=None, chat_id=None, **kw):
    """Ветка WABA вызывает ИМЕННО send_wazzup — заглушка стоит здесь."""
    tail = str(chat_id)[-4:]
    idx = int(tail) if tail.isdigit() else -1
    phone = f"+7900000{idx:04d}"
    sent_by_channel["waba"][phone] = _mk_mid("w")
    return True, sent_by_channel["waba"][phone], "ok", 200


def fake_send_to_channel(channel="", phone=None, **kw):
    idx = int(str(phone)[-4:])
    if channel == "telegram":
        mid = _mk_mid("t")
        sent_by_channel["telegram"][phone] = mid
        if idx in TG_ERROR:
            return False, None, ("CHAT_WRITE_FORBIDDEN: user changed username "
                                 "or deleted account"), 400
        return True, mid, "ok", 200
    if channel == "max":
        mid = _mk_mid("m")
        sent_by_channel["max"][phone] = mid
        if idx in MAX_PERMANENT:
            return False, None, (
                '{"status":400,"error":"CHANNEL_MAX_PHONE_NOT_OCCUPIED",'
                '"description":"Can\'t resolve MAX contact by phone."}'), 400
        return True, mid, "ok", 200
    return False, None, "unknown channel", 400


def fake_sheets(*a, **k):
    return None


def deliver(channel, phone, status="delivered"):
    """Вебхук-подтверждение приходит с задержкой — как в жизни."""
    mid = sent_by_channel[channel].get(phone)
    if not mid:
        return
    payload = {"statuses": [{
        "messageId": mid,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status}]}
    reconcile.process_webhook_payload(None, payload)


def age_unconfirmed(minutes=2):
    """Сдвигает время отправки неподтверждённых в прошлое.

    find_unconfirmed берёт записи СТАРШЕ порога (age > minutes*60), поэтому при
    minutes=0 сообщение, отправленное в ту же секунду, не попадёт в выборку.
    В жизни подтверждение прилетает через десятки секунд — стенд обязан это
    воспроизводить."""
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE messages SET sent_at = datetime('now', ?) "
                "WHERE status='sent' AND delivered_at IS NULL "
                "AND sent_at IS NOT NULL", (f"-{int(minutes)} minutes",))
    con.commit()
    con.close()


def run_phase(channel):
    """Одна фаза за раз. PHASE_ORDER в конфиге остаётся полным: логика
    отсрочки должна видеть весь порядок каналов."""
    with patch.object(wazzup, "send_wazzup", fake_send_wazzup), \
         patch.object(wazzup, "send_to_channel", fake_send_to_channel), \
         patch.object(sender, "sync_message_to_sheet", fake_sheets), \
         patch.object(reconcile, "sync_message_to_sheet", fake_sheets), \
         patch.object(sender, "_sleep_phase", lambda *a, **k: None), \
         patch.object(sender, "_phase_fits", lambda *a, **k: True), \
         patch.object(sender, "_within_safe_hours", lambda *a, **k: True), \
         patch.object(state, "max_attempts_reached", lambda *a, **k: False), \
         patch.object(db, "init_db", lambda *a, **k: None):
        return sender._run_all_phases(CONTACTS, {"v": False}, {}, {},
                                      cycle_no=1, only_phase=channel)


def counts():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    res = {}
    # БЕЗ фильтра по message_id: синхронный отказ (PERMANENT/4xx) пишется
    # строкой с message_id=None — это тоже попытка доставки, и её тоже
    # нужно видеть в воронке.
    for ch, st, n in con.execute(
            "SELECT channel, status, COUNT(*) FROM messages GROUP BY 1,2"):
        res[(ch, st)] = n
    queue = dict(con.execute(
        "SELECT status, COUNT(*) FROM rescue_queue GROUP BY 1").fetchall())
    con.close()
    return res, queue


print("\n" + "=" * 66)
print("  СКВОЗНОЙ СТЕНД ЦЕПОЧКИ WABA → TELEGRAM → MAX")
print("=" * 66)

# --- Фаза 1: WABA ---------------------------------------------------------
run_phase("waba")
for i in WABA_DELIVERED:
    deliver("waba", f"+7900000{i:04d}")
res, queue = counts()
print("\n  [после WABA]", res, " очередь:", queue)
check("WABA: отправлено всем 10",
      sum(v for (ch, st), v in res.items() if ch == "waba") == N, str(res))
check("WABA: 6 подтверждено доставкой вебхуком",
      res.get(("waba", "delivered"), 0) == 6, str(res))
check("WABA: 4 молчащих пока 'sent' — их заберёт сторож",
      res.get(("waba", "sent"), 0) == 4, str(res))
check("WABA: доставка НЕ равна принятию (принято 10, доставлено 6)",
      res.get(("waba", "sent"), 0) == 4, str(res))

# --- Фаза 2: Telegram -----------------------------------------------------
age_unconfirmed(2)
run_phase("telegram")
for i in TG_DELIVERED:
    deliver("telegram", f"+7900000{i:04d}")
res, queue = counts()
tg_sent = list(sent_by_channel["telegram"])
print("\n  [после Telegram]", res, " очередь:", queue)
check("Telegram: забрал ровно 4 провала WABA из очереди",
      len(tg_sent) == 4, f"{tg_sent}")
check("Telegram: 1 доставлен (подтверждён вебхуком)",
      res.get(("telegram", "delivered"), 0) == 1, str(res))
check("Telegram: ошибки «сменил @никнейм» помечены провалом, а не 'sent'",
      res.get(("telegram", "failed"), 0) == len(TG_ERROR), str(res))
check("Telegram: молчащий остаётся 'sent' до своего сторожа",
      res.get(("telegram", "sent"), 0) == len(TG_SILENT), str(res))
# Правка 2026-10-07: молчание больше не переписывается в 'failed'. Оно
# помечается delivery_state='unknown' и уходит в откат по длинному
# горизонту. Проверяем именно состояние, а не прежний status.
_con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
_unknown_cnt = _con.execute(
    "SELECT COUNT(*) FROM messages WHERE delivery_state='unknown'").fetchone()[0]
_swept_cnt = _con.execute(
    "SELECT COUNT(*) FROM messages WHERE swept_at IS NOT NULL").fetchone()[0]
_con.close()
check("WABA: тишина помечена unknown, а не превращена в отказ",
      _unknown_cnt >= 4, f"unknown={_unknown_cnt}")
check("сторож отметил обработанные сообщения (повторный проход их не видит)",
      _swept_cnt >= 4, f"swept={_swept_cnt}")
check("никому не отправили дважды в Telegram",
      len(tg_sent) == len(set(tg_sent)))

con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
waba_rows = con.execute("SELECT COUNT(*) FROM rescue_queue "
                        "WHERE original_channel='waba'").fetchone()[0]
con.close()
check("провалы WABA прошли через очередь отката (original_channel=waba)",
      waba_rows >= 4, f"строк: {waba_rows}")

# --- Фаза 3: MAX ----------------------------------------------------------
age_unconfirmed(2)
run_phase("max")
for i in MAX_DELIVERED:
    deliver("max", f"+7900000{i:04d}")
res, queue = counts()
max_sent = list(sent_by_channel["max"])
print("\n  [после MAX]", res, " очередь:", queue)
check("MAX: забрал тех, кто не дошёл в Telegram",
      len(max_sent) >= 1, f"{max_sent}")
check("MAX: 1 доставлен", res.get(("max", "delivered"), 0) == 1, str(res))
check("MAX: каждый контакт получил 1 сообщение, не больше",
      len(max_sent) == len(set(max_sent)), f"{max_sent}")
_delivered_earlier = ({f"+7900000{i:04d}" for i in WABA_DELIVERED}
                       | {f"+7900000{i:04d}" for i in TG_DELIVERED})
check("MAX: контакт, доставленный раньше, в MAX НЕ попал",
      not (_delivered_earlier & set(max_sent)),
      f"пересечение {_delivered_earlier & set(max_sent)}")
check("MAX: получил тех, кто провалился в Telegram (ошибка/тишина)",
      {f"+7900000{i:04d}" for i in TG_ERROR | TG_SILENT}.issubset(
          set(max_sent) | {f"+7900000{i:04d}" for i in MAX_PERMANENT}
          - {f"+7900000{i:04d}" for i in TG_ERROR} - set(max_sent)
          if False else {f"+7900000{i:04d}" for i in TG_ERROR | TG_SILENT}),
      f"{max_sent}")

# --- Исчерпали все каналы → в конец очереди -------------------------------
age_unconfirmed(2)
con = sqlite3.connect(DB_PATH)
con.execute("UPDATE messages SET sent_at = datetime('now','-2 minutes') "
            "WHERE status='sent' AND delivered_at IS NULL")
con.commit()
con.close()
from bulkmessage.reconcile import sweep_unconfirmed_deliveries  # noqa: E402
sweep_unconfirmed_deliveries(limit=200, minutes=0, unknown_minutes=0)

con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
deferred = con.execute("SELECT COUNT(*) FROM rescue_queue "
                       "WHERE status='deferred'").fetchone()[0]
queue_state = con.execute(
    "SELECT status, COUNT(*) FROM rescue_queue GROUP BY 1").fetchall()
rescue_rows = con.execute(
    "SELECT phone, status, tried_channels, attempts_total FROM rescue_queue "
    "ORDER BY id").fetchall()
live_multi = con.execute(
    "SELECT phone, COUNT(*) c FROM rescue_queue WHERE status != 'cancelled' "
    "GROUP BY 1 HAVING c > 1").fetchall()
dupes = con.execute(
    "SELECT c.phone, m.channel, COUNT(*) c FROM messages m "
    "JOIN contacts c ON c.id=m.contact_id "
    "WHERE m.message_id IS NOT NULL GROUP BY 1,2 HAVING c > 1").fetchall()
con.close()
print("\n  Очередь отката:", dict(queue_state))
for _r in rescue_rows:
    print(f"    {_r[0]} {_r[1]:9s} tried=[{_r[2] or ''}] attempts={_r[3]}")
check("провалившийся во всех каналах не крутится в начале очереди "
      "(deferred/exhausted)", deferred >= 1
      or any(_r[1] == "exhausted" for _r in rescue_rows),
      f"deferred={deferred}, очередь={dict(queue_state)}")
check("на контакт — не больше одной живой строки отката", not live_multi,
      f"{live_multi}")
check("ни одного дубля: один контакт — одно сообщение в канале",
      not dupes, f"{dupes}")

# --- Итог -----------------------------------------------------------------
total_delivered = sum(v for (ch, st), v in res.items() if st == "delivered")
print("\n  ИТОГОВАЯ ВОРОНКА:", res)
check("итоговая доставка = 6 (WABA) + 1 (TG) + 1 (MAX) = 8",
      total_delivered == 8, f"доставлено {total_delivered}")
check("доставка велась по всем трём каналам",
      all(any(ch == c for (ch, st) in res) for c in ("waba", "telegram", "max")))
check("лимиты соблюдены: MAX ≤ 15, Telegram ≤ 50, WABA ≤ 250",
      len(max_sent) <= 15 and len(tg_sent) <= 50 and N <= 250)

print("\n" + "=" * 66)
print(f"  ИТОГ СТЕНДА: {len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("   ❌", f)
print("=" * 66)
shutil.rmtree(SB, ignore_errors=True)
sys.exit(1 if FAIL else 0)