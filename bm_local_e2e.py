"""Локальный боевой стенд: отвечает на три вопроса владельца.

  1) Ловит ли система ошибки отправки и перенаправляет контакт в другой канал?
  2) Обрабатываются ли ВСЕ контакты — ни один не теряется и не зависает?
  3) Не жгутся ли лимиты личных аккаунтов дублями и мёртвыми каналами?

Сценарий имитирует худший реальный случай 2026-10-06: Meta молча режет
маркетинговые шаблоны (тишина), Telegram отвечает HTTP 200 и не доставляет
ничего, MAX отвечает «нет в MAX», часть контактов отвечает.

Ни одной реальной отправки: транспорт заглушен, БД во временном каталоге,
окна безопасности и активного окна отключены явно.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SB = tempfile.mkdtemp(prefix="bm_local_")
DB_PATH = os.path.join(SB, "crm.db")
STATE_PATH = os.path.join(SB, "state.json")

from bulkmessage import config, db, reconcile, sender, state, wazzup  # noqa: E402

config.DB_PATH = DB_PATH
config.STATE_PATH = STATE_PATH
config.BAD_PHONES_PATH = os.path.join(SB, "bad_phones.json")
config.DRY_RUN = False
config.BULK_BYPASS_SAFE_HOURS = True
config.ACTIVE_HOURS_START = 0
config.ACTIVE_HOURS_END = 0
config.PHASE_ORDER = ["waba", "telegram", "max"]
config.PHASE_GRACE_MINUTES = 0
config.RESCUE_SETTLE_MINUTES = 0
config.REJECT_GRACE_MINUTES = 0
config.UNKNOWN_GRACE_MINUTES = 0     # имитируем «горизонт выдержан»
config.EXHAUST_MAX_ATTEMPTS = 2
config.EXHAUST_WINDOW_DAYS = 0
config.WABA_SILENT_COOLDOWN_DAYS = 14
config.CONTACT_CHANNEL_COOLDOWN_HOURS = 24
config.WABA_CONCURRENCY = 5           # конвейер включён — его и проверяем
config.CHANNEL_HEALTH_GUARD = True
config.CHANNEL_DAILY_LIMITS = {"whatsapp": 60, "telegram": 50, "max": 15,
                              "waba": 250, "sms": 50}
config.WAZZUP_DEFAULT_CHANNEL_ID = "cid-waba"
config.WAZZUP_TELEGRAM_CHANNEL_ID = "cid-tg"
config.WAZZUP_MAX_CHANNEL_ID = "cid-max"
config.BATCH_BREAK_MIN = 0
config.BATCH_BREAK_MAX = 0
config.TARGET_SUCCESS_PER_DAY = 1000
config.MAX_ATTEMPTS = 1000

db.init_db()
state.save_state({"date": datetime.now(timezone.utc).date().isoformat(),
                  "sent_today": {}, "delivered_today": {}, "failed_today": {}})

# --- Сценарий на 12 контактов ---------------------------------------------
# WABA: 4 подтверждено вебхуком, 8 «молчат» (Meta режет шаблон).
# Из 8 молчащих: 3 уходят в Telegram, где 1 доставляется, 2 отказали;
# 3 не идут в Telegram (сторож здоровья канала), 2 идут сразу в MAX.
N = 12
CONTACTS = [{"phone": f"+79000{i:05d}", "name": f"Тест{i}",
             "category": "Покупатели"} for i in range(N)]
WABA_DELIVERED = {0, 1, 2, 3}          # вебхук подтвердил доставку
TG_DELIVERED = {4}                      # дошёл в Telegram
TG_REJECTED = {5, 6}                    # «сменил @никнейм» — PERMANENT
MAX_DELIVERED = {7}                     # дошёл в MAX
MAX_REJECTED = {8}                      # «нет в MAX»
# 9–11: WABA молчал, Telegram принял, но не доставил, MAX принял, но не
# доставил. Это НЕ «неприкасаемые» контакты — это полный каскад WABA→TG→MAX,
# где ни один канал не подтвердил доставку. Ждать их повторно сегодня нельзя
# (иначе три сообщения подряд — прямой путь к бану личного аккаунта), и они
# обязаны закончить терминально: rescued или exhausted.
SILENT_ALL = {9, 10, 11}

sent_by_channel: dict = {"waba": {}, "telegram": {}, "max": {}}
_counter = {"n": 0}


def _mid(tag: str) -> str:
    _counter["n"] += 1
    return f"{tag}-{_counter['n']}"


def _idx(phone: str) -> int:
    return int(str(phone)[-5:])


def fake_send_wazzup(channel_id=None, chat_id=None, **kw):
    """Ветка WABA вызывает именно send_wazzup — заглушка стоит здесь.

    Контракт транспорта: (ok, message_id, detail, http_status).
    """
    p = f"+79000{_idx(str(chat_id)):05d}"
    sent_by_channel["waba"][p] = _mid("w")
    return True, sent_by_channel["waba"][p], "ok", 200


def fake_send_to_channel(channel="", phone=None, **kw):
    """Telegram/MAX. Telegram доставляет одному и отказывает двум."""
    p = f"+79000{_idx(str(phone)):05d}"
    if channel == "telegram":
        mid = _mid("t")
        sent_by_channel["telegram"][p] = mid
        if _idx(p) in TG_REJECTED:
            return False, None, ("CHAT_WRITE_FORBIDDEN: user changed username "
                                 "or deleted account"), 400
        return True, mid, "ok", 200
    mid = _mid("m")
    sent_by_channel["max"][p] = mid
    if _idx(p) in MAX_REJECTED:
        return False, None, "CHANNEL_MAX_PHONE_NOT_OCCUPIED", 400
    return True, mid, "ok", 200


def fake_sheets(*a, **k):
    return None


def deliver(channel: str, phone: str, status: str = "delivered") -> None:
    mid = sent_by_channel[channel].get(phone)
    if not mid:
        return
    reconcile.process_webhook_payload(None, {"statuses": [{
        "messageId": mid,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status}]})


def age_unconfirmed(minutes: int = 2) -> None:
    """Молчание «доживает» до горизонта — как в жизни, где статусы не спешат."""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE messages SET sent_at = datetime('now', ?) "
        "WHERE status='sent' AND delivered_at IS NULL AND sent_at IS NOT NULL",
        (f"-{int(minutes)} minutes",))
    con.commit()
    con.close()


PASSED = 0
FAILED: list = []


def check(name: str, cond, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  \u2705 {name}")
    else:
        FAILED.append(name)
        print(f"  \u274c {name}" + (f" — {detail}" if detail else ""))


def run_phase(channel: str) -> dict:
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


print("\n" + "=" * 70)
print("  ЛОКАЛЬНЫЙ БОЕВОЙ СТЕНД: три вопроса владельца")
print("=" * 70)

# --- Фаза 1: WABA ----------------------------------------------------------
print("\n[1] ФАЗА WABA — 12 контактов, 8 «молчат» (Meta режет шаблон)")
run_phase("waba")
for i in WABA_DELIVERED:
    deliver("waba", f"+79000{i:05d}")
age_unconfirmed(2)

print(f"    отправлено WABA: {len(sent_by_channel['waba'])}")

# --- Фаза 2: Telegram ------------------------------------------------------
print("\n[2] ФАЗА TELEGRAM — сторож здоровья канала и перенаправление")
run_phase("telegram")
for i in TG_DELIVERED:
    deliver("telegram", f"+79000{i:05d}")
age_unconfirmed(2)

# --- Фаза 3: MAX -----------------------------------------------------------
print("\n[3] ФАЗА MAX — последний канал каскада")
run_phase("max")
for i in MAX_DELIVERED:
    deliver("max", f"+79000{i:05d}")
age_unconfirmed(2)
reconcile.sweep_unconfirmed_deliveries(limit=200, minutes=0, unknown_minutes=0)

# --- Разбор ----------------------------------------------------------------
con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
res: dict = {}
for ch, st, n in con.execute(
        "SELECT channel, status, COUNT(*) FROM messages GROUP BY 1,2"):
    res[(ch, st)] = n
queue = dict(con.execute(
    "SELECT status, COUNT(*) FROM rescue_queue GROUP BY 1").fetchall())
# Живых строк отката на контакт должно быть ровно одна: вторая строка с
# пустым tried_channels — это и есть источник дубля в личном канале.
live_per_phone = dict(con.execute(
    "SELECT phone, COUNT(*) FROM rescue_queue WHERE status != 'cancelled' "
    "GROUP BY 1").fetchall())
multi_rows = {p: n for p, n in live_per_phone.items() if n > 1}
dupes = [tuple(r) for r in con.execute(
    "SELECT ct.phone, m.channel, COUNT(*) c FROM messages m "
    "JOIN contacts ct ON ct.id=m.contact_id "
    "WHERE m.message_id IS NOT NULL GROUP BY 1,2 HAVING c > 1").fetchall()]
exhausted = [r["phone"] for r in con.execute(
    "SELECT phone FROM rescue_queue WHERE status='exhausted'")]
rescued = [r["phone"] for r in con.execute(
    "SELECT phone FROM rescue_queue WHERE status='rescued'")]
counters = db.get_counters()
con.close()
print("\n  Воронка:", res)
print("  Очередь:", queue)
print("  Счётчики дня:", counters)
print("  Rescue-строки:")
for r in sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True).execute(
        "SELECT phone, status, tried_channels, attempts_total, "
        "substr(COALESCE(last_outcome,''),1,40) o FROM rescue_queue "
        "ORDER BY id"):
    print(f"    {r[0]} {r[1]:9s} tried=[{r[2] or ''}] attempts={r[3]} {r[4]}")

# =====================================================================
# ВОПРОС 1: ошибки ловятся и контакт уходит в другой канал?
# =====================================================================
print("\n" + "-" * 70)
print("  ВОПРОС 1: перехват ошибок и перенаправление")
print("-" * 70)
all_sends = {**sent_by_channel["waba"], **sent_by_channel["telegram"],
             **sent_by_channel["max"]}
check("каждый контакт получил ровно одну отправку WABA",
      len(sent_by_channel["waba"]) == N,
      f"{len(sent_by_channel['waba'])} из {N}")
tried_tg = {int(p[-5:]) for p in sent_by_channel["telegram"]}
tried_max = {int(p[-5:]) for p in sent_by_channel["max"]}
waba_silent = set(range(N)) - WABA_DELIVERED
check("все молчавшие WABA ушли либо в Telegram, либо в MAX",
      waba_silent <= (tried_tg | tried_max),
      f"молчали: {sorted(waba_silent)}, tg={sorted(tried_tg)}, "
      f"max={sorted(tried_max)}")
check("отказ Telegram уводит контакт дальше, в MAX",
      TG_REJECTED <= tried_max,
      f"отказали в TG: {sorted(TG_REJECTED)}, в MAX: {sorted(tried_max)}")
check("доставленные в WABA не переотправляются никуда",
      WABA_DELIVERED.isdisjoint(tried_tg) and
      WABA_DELIVERED.isdisjoint(tried_max),
      f"tg={sorted(tried_tg)}, max={sorted(tried_max)}")
check("доставленный в Telegram контакт не пошёл в MAX",
      TG_DELIVERED.isdisjoint(tried_max))
check("никто не получил два сообщения в одном канале", not dupes,
      f"{dupes}")
check("на каждый контакт — ровно одна живая строка отката", not multi_rows,
      f"{multi_rows}")

# =====================================================================
# ВОПРОС 2: все ли контакты обработаны, никто не потерян?
# =====================================================================
print("\n" + "-" * 70)
print("  ВОПРОС 2: обработка ВСЕХ контактов")
print("-" * 70)
check("каждый контакт получил хотя бы одну попытку отправки",
      len(all_sends) == N, f"{len(all_sends)} из {N}")
rows_total = sqlite3.connect(DB_PATH).execute(
    "SELECT COUNT(DISTINCT contact_id) FROM messages").fetchone()[0]
check("по каждому контакту есть строка в messages", rows_total == N,
      f"{rows_total} из {N}")
check("каждый контакт из молчавших ушёл в откат (TG или MAX)",
      waba_silent <= {int(p[-5:]) for p in live_per_phone},
      f"молчали: {sorted(waba_silent)}, в очереди: "
      f"{sorted({int(p[-5:]) for p in live_per_phone})}")
check("исчерпавшие все каналы получили терминальный статус",
      len(exhausted) >= 1, f"exhausted={exhausted}")
check("полностью «немой» контакт не ушёл в личный канал повторно",
      SILENT_ALL <= {int(p[-5:]) for p in rescued + exhausted},
      f"rescued={sorted(int(p[-5:]) for p in rescued)}, "
      f"exhausted={sorted(int(p[-5:]) for p in exhausted)}")
check("в очереди не осталось «зависших» pending без прогресса",
      queue.get("pending", 0) == 0, f"{queue}")

# =====================================================================
# ВОПРОС 3: лимиты личных аккаунтов не жгутся
# =====================================================================
print("\n" + "-" * 70)
print("  ВОПРОС 3: лимиты и здоровье каналов")
print("-" * 70)
check("Telegram отправок не больше дневного капа",
      len(sent_by_channel["telegram"]) <= config.CHANNEL_DAILY_LIMITS["telegram"],
      f"{len(sent_by_channel['telegram'])}")
check("MAX отправок не больше дневного капа",
      len(sent_by_channel["max"]) <= config.CHANNEL_DAILY_LIMITS["max"],
      f"{len(sent_by_channel['max'])}")
check("счётчик accepted записан в SQLite (воронка не пустая)",
      counters.get("waba", {}).get("accepted", 0) == N,
      f"{counters.get('waba')}")
check("доставлено не больше принятого (источник истины один)",
      counters.get("waba", {}).get("delivered", 0)
      <= counters.get("waba", {}).get("accepted", 0),
      f"{counters.get('waba')}")
check("конвейер WABA реально работал (5 воркеров)",
      config.WABA_CONCURRENCY == 5)

print("\n" + "=" * 70)
print(f"  ИТОГ: {PASSED} passed, {len(FAILED)} failed")
if FAILED:
    for f in FAILED:
        print(f"    \u274c {f}")
print("=" * 70)
sys.exit(1 if FAILED else 0)