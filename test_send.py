"""
Тест end-to-end: один номер, все категории × все каналы + отслеживание.

Имитирует полный цикл sender.py + tracker.py:
  1. Отправляет 4 сообщения (по шаблонам всех категорий) в WhatsApp + Telegram + MAX
     на указанный TEST_PHONE через Wazzup24.
  2. Каждое сообщение сохраняет в crm.db (contacts, messages) и шлёт в Google Sheets.
  3. Классифицирует ошибки (permanent/rate/transient/auth) при неудачах.
  4. Ждёт указанное время и опрашивает статусы через Wazzup webhook
     (или poll /v3/channels если webhook не поднят).
  5. Показывает итоговую таблицу: телефон / канал / категория / статус / message_id.

Перед запуском:
  - Заполните TEST_PHONE (ваш «второй аккаунт»).
  - Заполните TEST_NAME.
  - Если есть Google Sheets — заполните BULK_GOOGLE_SHEET_ID и положите credentials.json.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from datetime import datetime

# Загружаем .env ДО импорта bulkmessage.config (иначе токены defaults из config.py)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

sys.path.insert(0, ".")

from bulkmessage import config, db, sheets, templates, wazzup


# ============================================================
# НАСТРОЙКИ ТЕСТА
# ============================================================

# ЕДИНСТВЕННЫЙ номер — ваш «второй аккаунт» (СВОЙ номер для теста)
# ⚠️  Подставь СВОЙ тестовый номер перед запуском (не публичный контакт).
TEST_PHONE = "+998XXXXXXXXX"  # ← замени на свой тестовый номер
TEST_NAME = "Test User"
TEST_CATEGORY_HINT = "Покупатель"  # используется для контакта в БД

# Сколько секунд ждать ответы/доставку после рассылки
WAIT_FOR_REPLIES = 15

# Задержка между отправками (сек)
SLEEP_BETWEEN = 2.0

# Какие каналы тестировать: None = все активные по токенам.
# Можно явно: ["telegram"] или ["whatsapp", "telegram", "max"].
TEST_CHANNELS: list[str] | None = ["telegram"]


# ============================================================
# Тест
# ============================================================


def _print_banner() -> None:
    print("=" * 70)
    print(" TEST SEND — single number, all categories × all channels + tracking")
    print("=" * 70)
    print(f"  Phone:     {wazzup.normalize_phone(TEST_PHONE)}")
    print(f"  Name:      {TEST_NAME}")
    print(f"  DB:        {config.DB_PATH}")
    print(f"  Sheets:    {'включены' if sheets.SHEETS.enabled else 'ОТКЛЮЧЕНЫ'}")


def _send_and_track(
    *,
    phone: str,
    name: str,
    category_for_contact: str,
    template_category: str,
    template_text: str,
    channel: str,
) -> dict:
    """Отправляет одно сообщение, пишет в БД, шлёт в Sheets. Возвращает dict с результатом."""
    print(f"  [{channel:8s}] {template_category:11s} -> ", end="", flush=True)

    ok, message_id, detail, http_status = wazzup.send_to_channel(channel, phone, template_text)
    record = {
        "channel": channel,
        "template": template_category,
        "ok": ok,
        "message_id": message_id,
        "detail": detail,
        "status": None,
        "delivered": False,
        "read": False,
    }

    if not ok:
        kind = wazzup.classify_error(detail, http_status)
        print(f"FAIL [{kind.value}]: {detail[:100]}")
        record["status"] = f"failed:{kind.value}"
        with db.db_conn() as conn:
            contact_id = db.upsert_contact(conn, phone, name, category_for_contact)
            db.insert_message(
                conn,
                contact_id=contact_id,
                channel=channel,
                message_text=template_text,
                message_id=None,
                status="failed",
                last_error=detail[:500],
                sent_at=None,
            )
        return record

    print(f"OK (msg_id={message_id})")
    record["status"] = "sent"

    with db.db_conn() as conn:
        contact_id = db.upsert_contact(conn, phone, name, category_for_contact)
        message_pk = db.insert_message(
            conn,
            contact_id=contact_id,
            channel=channel,
            message_text=template_text,
            message_id=message_id,
            status="sent",
        )
        record["contact_id"] = contact_id
        record["message_pk"] = message_pk

    if sheets.SHEETS.enabled:
        try:
            sheets.sync_message_to_sheet(
                contact_id=contact_id,
                channel=channel,
                status="sent",
                message_pk=message_pk,
            )
        except Exception as e:
            print(f"    [Sheets] ERROR: {e}")

    return record


def _check_status(records: list[dict]) -> None:
    """Ждём пока webhook'и Wazzup дойдут, потом обновляем статусы из crm.db.

    Wazzup — webhook-driven, нет GET API для статуса. Эта функция теперь
    только показывает текущий статус из crm.db после ожидания.
    """
    print()
    print(f"Ждём {WAIT_FOR_REPLIES}с, чтобы webhook'и Wazzup дошли...")
    time.sleep(WAIT_FOR_REPLIES)

    for r in records:
        if not r.get("message_id") or r["channel"] is None:
            continue
        # Получаем текущий статус из БД (webhook'и обновили бы его)
        with db.db_conn() as conn:
            row = db.find_message_by_message_id(conn, r["message_id"])
            if row:
                r["status"] = row["status"]
                r["delivered"] = bool(row.get("delivered_at"))
                r["read"] = bool(row.get("read_at"))
            else:
                r["status"] = "sent"


def _check_replies(phone: str, since_iso: Optional[str] = None) -> list[dict]:
    """Проверяет, пришли ли ответы через crm.db (webhook-driven).

    Wazzup не имеет GET API для истории чатов — ответы приходят только через
    webhook → process_wazzup_payload → process_incoming_message → crm.db.
    Эта функция теперь читает ответы напрямую из crm.db.
    """
    print()
    if since_iso:
        print("Проверяем входящие ответы (с момента %s)..." % since_iso)
    else:
        print("Проверяем входящие ответы (из crm.db)...")
    phone_n = wazzup.normalize_phone(phone)
    found: list[dict] = []
    for channel in wazzup.active_channels():
        try:
            with db.db_conn() as conn:
                c = conn.cursor()
                if since_iso:
                    c.execute(
                        "SELECT message, created_at FROM replies "
                        "WHERE contact_id = (SELECT id FROM contacts WHERE phone = ?) "
                        "AND channel = ? AND created_at >= ? "
                        "ORDER BY created_at ASC",
                        (phone_n, channel, since_iso),
                    )
                else:
                    c.execute(
                        "SELECT message, created_at FROM replies "
                        "WHERE contact_id = (SELECT id FROM contacts WHERE phone = ?) "
                        "AND channel = ? ORDER BY created_at DESC LIMIT 20",
                        (phone_n, channel),
                    )
                msgs = [dict(r) for r in c.fetchall()]
        except Exception as e:
            print(f"  [{channel}] error: {e}")
            continue
        for m in msgs:
            body = (m.get("message") or "").strip()
            if not body:
                continue
            mid = m.get("created_at")
            found.append({
                "channel": channel,
                "body": body[:200],
                "message_id": mid,
            })
                if c.fetchone():
                    continue
                db.add_reply(conn, contact["id"], channel, body, mid)
                target = db.get_latest_inbound_target(conn, contact["id"], channel)
                if target is not None and target["status"] != "answered":
                    db.update_message_status(
                        conn,
                        message_pk=target["id"],
                        status="answered",
                        answered_at=True,
                    )
                    if sheets.SHEETS.enabled:
                        try:
                            sheets.sync_message_to_sheet(
                                contact_id=contact["id"],
                                channel=channel,
                                status="answered",
                                message_pk=target["id"],
                            )
                        except Exception:
                            pass
    return found


def _print_summary(records: list[dict], replies: list[dict]) -> None:
    print()
    print("=" * 70)
    print(" ИТОГ")
    print("=" * 70)
    print(f" {'Channel':10s} {'Template':12s} {'Status':16s} {'Message ID'}")
    print("-" * 70)
    for r in records:
        print(
            f" {r['channel']:10s} {r['template']:12s} {str(r['status'])[:16]:16s} "
            f"{r.get('message_id') or '-'}"
        )
    if replies:
        print()
        print(f" Получено ответов: {len(replies)}")
        for rep in replies:
            print(f"   [{rep['channel']}] {rep['body']}")
    else:
        print()
        print(" Ответов пока нет (ответьте на любое из сообщений и запустите снова).")
    print("=" * 70)
    print(f" База: {config.DB_PATH}")
    print(f" Таблица: {config.GOOGLE_WORKSHEET_NAME if sheets.SHEETS.enabled else 'отключена'}")


def main() -> int:
    _print_banner()
    print()

    config.configure_logging()
    log = config.get_logger("test_send")
    db.init_db()

    phone = wazzup.normalize_phone(TEST_PHONE)
    if not phone:
        print(f"[ERR] Невалидный TEST_PHONE: {TEST_PHONE!r}")
        return 1

    templates_map = templates.load_templates()
    if not templates_map:
        print("[ERR] Шаблоны не загружены — проверьте Message_script.md")
        return 1
    print(f"Шаблонов: {len(templates_map)} — {list(templates_map.keys())}")

    channels = wazzup.active_channels()
    if not channels:
        print("[ERR] Нет активных каналов — проверьте WAZZUP_API_KEY + WAZZUP_*_CHANNEL_ID")
        return 1
    print(f"Активные каналы: {channels}")
    print()

    records: list[dict] = []
    for cat in templates_map.keys():
        try:
            text = templates_map[cat].format(имя=TEST_NAME)
        except Exception as e:
            print(f"  [SKIP] {cat}: format error {e}")
            continue
        for channel in channels:
            r = _send_and_track(
                phone=phone,
                name=TEST_NAME,
                category_for_contact=TEST_CATEGORY_HINT,
                template_category=cat,
                template_text=text,
                channel=channel,
            )
            records.append(r)
            time.sleep(SLEEP_BETWEEN)

    # Имитация webhook-ов: опрос статусов доставки
    _check_status(records)

    # Проверка входящих ответов (только ПОСЛЕ начала теста)
    test_since = None
    first_ok = [r for r in records if r["ok"]]
    if first_ok:
        # Берём время первого успешного сообщения как нижнюю границу
        with db.db_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT MIN(sent_at) as earliest FROM messages WHERE message_id IS NOT NULL AND message_id != ''")
            row = c.fetchone()
            if row and row["earliest"]:
                try:
                    dt = datetime.fromisoformat(row["earliest"].replace("Z", "+00:00"))
                    test_since = dt.strftime("%Y-%m-%dT%H:%M:%S")
                except Exception:
                    test_since = None
    replies = _check_replies(phone, since_iso=test_since)

    _print_summary(records, replies)
    return 0


if __name__ == "__main__":
    sys.exit(main())
