"""SQLite layer: schema, connection context, helpers."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from . import config


_DB_LOCK = threading.RLock()


@contextmanager
def db_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True если таблица содержит указанную колонку (для миграций)."""
    c = conn.cursor()
    c.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in c.fetchall())


# --- Согласие на рассылку (opt-in) -----------------------------------------
# Ч.1 ст.18 ФЗ-38: реклама по сетям электросвязи, включая мессенджеры, только
# при предварительном согласии, и БРЕМЯ ДОКАЗЫВАНИЯ лежит на рекламораспространителе.
# Мета-политика WhatsApp: business-initiated сообщения только opt-in контактам.
# Поэтому храним согласие явно, а не «где-то в CRM».
#
# Три состояния:
#   granted — есть согласие, можно писать
#   unknown — данных нет (холодная база). Поведение задаёт
#            config.CONSENT_REQUIRED_FOR_COLD: 0 = писать (старое поведение),
#            1 = пропускать
#   denied  — человек отказался. УВАЖАЕТСЯ ВСЕГДА, независимо от флага:
#            отписка не обсуждается и не отключается настройкой.
CONSENT_GRANTED = "granted"
CONSENT_UNKNOWN = "unknown"
CONSENT_DENIED = "denied"

_CONSENT_VALUES = (CONSENT_GRANTED, CONSENT_UNKNOWN, CONSENT_DENIED)


def normalize_consent(value) -> str:
    """Приводит любой ввод (из Excel, AmoCRM, API) к трём состояниям."""
    if value is None:
        return CONSENT_UNKNOWN
    s = str(value).strip().lower()
    if s in ("granted", "yes", "y", "true", "1", "да", "1", "согласие",
             "согласие получено", "opt-in", "optin", "подписан", "ok", "+"):
        return CONSENT_GRANTED
    if s in ("denied", "no", "n", "false", "0", "нет", "отказ", "отписка",
             "opt-out", "optout", "unsubscribe", "stop", "блок", "-"):
        return CONSENT_DENIED
    return CONSENT_UNKNOWN


def set_consent(
    phone: str, value, source: str = "", conn: sqlite3.Connection | None = None
) -> str:
    """Проставляет согласие контакту. source — откуда (amocrm_stage, excel, manual).

    ВАЖНО: если контакта ещё нет — он СОЗДАЁтся. Раньше был только UPDATE,
    и это была дыра в отписке: человек писал «стоп» в первый раз, номера
    ещё не было в базе, UPDATE не задевал ни одной строки, а контакт
    потом создавался в process_incoming_message уже с consent='unknown'.
    Итог: отписка была записана в лог, помечалась обработанной — и терялась.
    Для ФЗ-38 (ч.1 ст.18) это самая дорогая ошибка такого класса: мы
    продолжаем писать тому, кто попросил не писать.
    """
    st = normalize_consent(value)
    ts = datetime.now(timezone.utc).isoformat()

    def _apply(c: sqlite3.Connection) -> None:
        cur = c.execute(
            "UPDATE contacts SET consent=?, consent_source=?, consent_at=? WHERE phone=?",
            (st, source or None, ts, phone),
        )
        if cur.rowcount == 0:
            # Контакта не было — создаём, иначе согласие потеряется.
            c.execute(
                "INSERT INTO contacts (phone, name, category, consent, "
                "consent_source, consent_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(phone) DO UPDATE SET consent=excluded.consent, "
                "consent_source=excluded.consent_source, consent_at=excluded.consent_at",
                (phone, "", "", st, source or None, ts, ts),
            )

    if conn is not None:
        _apply(conn)
    else:
        with db_conn() as c2:
            _apply(c2)
    return st


def get_consent(phone: str) -> str:
    """Текущее согласие контакта. Если контакта нет — unknown."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT consent FROM contacts WHERE phone=?", (phone,))
        row = c.fetchone()
    if row is None or not row[0]:
        return CONSENT_UNKNOWN
    return row[0] if row[0] in _CONSENT_VALUES else CONSENT_UNKNOWN


def may_send_cold(phone: str) -> bool:
    """Разрешена ли холодная отправка (WABA/TG/MAX/SMS) этому контакту.

    denied → False ВСЕГДА.
    unknown → по config.CONSENT_REQUIRED_FOR_COLD.
    granted → True.
    """
    st = get_consent(phone)
    if st == CONSENT_DENIED:
        return False
    if st == CONSENT_GRANTED:
        return True
    return not getattr(config, "CONSENT_REQUIRED_FOR_COLD", 0)


def consent_stats() -> dict:
    """Сводка по согласию — для отчёта и для понимания риска."""
    out = {v: 0 for v in _CONSENT_VALUES}
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT consent, COUNT(*) FROM contacts GROUP BY consent")
        for val, n in c.fetchall():
            out[val if val in _CONSENT_VALUES else CONSENT_UNKNOWN] = n
    out["total"] = sum(out[v] for v in _CONSENT_VALUES)
    return out


def init_db() -> None:
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT UNIQUE NOT NULL,
                name TEXT,
                category TEXT,
                consent TEXT NOT NULL DEFAULT 'unknown',
                consent_source TEXT,
                consent_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        # Миграция для существующих баз: consent-колонки могло не быть.
        if not _has_column(conn, "contacts", "consent"):
            c.execute(
                "ALTER TABLE contacts ADD COLUMN consent TEXT NOT NULL DEFAULT 'unknown'"
            )
        if not _has_column(conn, "contacts", "consent_source"):
            c.execute("ALTER TABLE contacts ADD COLUMN consent_source TEXT")
        if not _has_column(conn, "contacts", "consent_at"):
            c.execute("ALTER TABLE contacts ADD COLUMN consent_at TEXT")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id INTEGER NOT NULL,
                channel TEXT NOT NULL,
                message_id TEXT,
                message_text TEXT,
                sent_at TEXT,
                delivered_at TEXT,
                read_at TEXT,
                answered_at TEXT,
                status TEXT NOT NULL,
                last_error TEXT,
                gsheet_row INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_message_id ON messages(message_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_contact_channel ON messages(contact_id, channel)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status)")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id INTEGER NOT NULL,
                channel TEXT NOT NULL,
                message TEXT NOT NULL,
                message_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_replies_contact ON replies(contact_id)")

        # Legacy-migration: для БД, созданных до 2026-09-19, переименовать
        # старую колонку wappi_message_id → message_id. Идемпотентно.
        if _has_column(conn, "replies", "wappi_message_id"):
            try:
                c.execute(
                    "ALTER TABLE replies RENAME COLUMN wappi_message_id TO message_id"
                )
                c.execute("DROP INDEX IF EXISTS idx_replies_unique_wappi")
                conn.commit()
            except Exception as e:
                import logging
                logging.getLogger("db").warning(
                    f"legacy rename wappi_message_id failed: {e}"
                )

        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_replies_unique_msg "
            "ON replies(contact_id, channel, message_id) "
            "WHERE message_id IS NOT NULL"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_webhooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT,
                payload TEXT NOT NULL,
                received_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                processed INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_pending_processed ON pending_webhooks(processed)")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sheets_ops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                op TEXT NOT NULL,
                message_pk INTEGER,
                payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                done INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_sheets_ops_done ON sheets_ops(done)")
        # Phase 7: rescue queue (WABA → Personal cascade retry on async WABA failure)
        init_rescue_queue()
        # Трёхзначная доставка + счётчики дня в SQLite (план «устойчивый каскад»)
        init_delivery_tracking()
        # Phase 0.1: channel_reachability cache (pre-check results, TTL 24h)
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_reachability (
                phone TEXT NOT NULL,
                channel TEXT NOT NULL,
                reachable INTEGER NOT NULL,
                last_checked_at TEXT NOT NULL,
                last_error TEXT,
                PRIMARY KEY (phone, channel)
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_reach_checked "
            "ON channel_reachability(last_checked_at)"
        )
        # Phase 1.2: WABA templates registry (synced from Wazzup)
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS waba_templates (
                template_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT,
                language_code TEXT NOT NULL DEFAULT 'ru',
                status TEXT NOT NULL,
                last_synced_at TEXT NOT NULL,
                last_error TEXT
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_waba_templates_status "
            "ON waba_templates(status)"
        )
        # Phase 3.6: complaint events log (rolling 24h window)
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS complaint_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                phone TEXT,
                complaint_kind TEXT NOT NULL,
                message_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_complaints_channel_time "
            "ON complaint_events(channel, created_at)"
        )
        conn.commit()


# --- Contacts ---


def upsert_contact(
    conn: sqlite3.Connection, phone: str, name: str, category: str
) -> int:
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    c.execute("SELECT id FROM contacts WHERE phone = ?", (phone,))
    row = c.fetchone()
    if row:
        contact_id = row["id"]
        c.execute(
            "UPDATE contacts SET name = COALESCE(NULLIF(?, ''), name), "
            "category = COALESCE(NULLIF(?, ''), category) WHERE id = ?",
            (name or "", category or "", contact_id),
        )
        return contact_id
    c.execute(
        "INSERT INTO contacts (phone, name, category, created_at) VALUES (?, ?, ?, ?)",
        (phone, name or "", category or "", now),
    )
    return c.lastrowid


def get_contact_by_phone(
    conn: sqlite3.Connection, phone: str
) -> Optional[sqlite3.Row]:
    c = conn.cursor()
    c.execute("SELECT * FROM contacts WHERE phone = ?", (phone,))
    return c.fetchone()


def get_sent_phones(conn: sqlite3.Connection) -> set[str]:
    """Контакты, у которых ХОТЬ ОДИН канал уже доставил сообщение."""
    c = conn.cursor()
    c.execute(
        "SELECT DISTINCT ct.phone FROM contacts ct JOIN messages m ON m.contact_id = ct.id "
        "WHERE m.status IN ('sent','delivered','read','answered')"
    )
    return set(row["phone"] for row in c.fetchall())


def get_sent_phone_channels(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Пары (phone, channel), по которым сообщение реально ушло через Wazzup24
    (status sent/delivered/read/answered). Используется для per-channel скипа:
    контакт пропускается в канале, только если в ЭТОМ канале уже был успех."""
    c = conn.cursor()
    c.execute(
        "SELECT DISTINCT ct.phone, m.channel FROM contacts ct "
        "JOIN messages m ON m.contact_id = ct.id "
        "WHERE m.status IN ('sent','delivered','read','answered')"
    )
    return {(row["phone"], row["channel"]) for row in c.fetchall()}


# --- Messages ---


def insert_message(
    conn: sqlite3.Connection,
    contact_id: int,
    channel: str,
    message_text: str,
    message_id: Optional[str] = None,
    status: str = "queued",
    last_error: Optional[str] = None,
    sent_at: Optional[str] = None,
) -> int:
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    # Если передан message_id (значит сообщение реально ушло через Wazzup),
    # автоматически проставляем sent_at = now, даже если вызывающий забыл.
    if message_id and not sent_at:
        sent_at = now
    c.execute(
        """
        INSERT INTO messages
        (contact_id, channel, message_id, message_text, status, last_error,
         sent_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (contact_id, channel, message_id, message_text, status, last_error,
         sent_at, now, now),
    )
    return c.lastrowid


def update_message_status(
    conn: sqlite3.Connection,
    *,
    message_id: Optional[str] = None,
    message_pk: Optional[int] = None,
    status: Optional[str] = None,
    delivered_at: bool = False,
    read_at: bool = False,
    answered_at: bool = False,
    last_error: Optional[str] = None,
) -> int:
    c = conn.cursor()
    sets: list[str] = []
    args: list[Any] = []
    if status is not None:
        sets.append("status = ?")
        args.append(status)
    if delivered_at:
        sets.append("delivered_at = COALESCE(delivered_at, ?)")
        args.append(datetime.now(timezone.utc).isoformat())
    if read_at:
        sets.append("read_at = COALESCE(read_at, ?)")
        args.append(datetime.now(timezone.utc).isoformat())
    if answered_at:
        sets.append("answered_at = COALESCE(answered_at, ?)")
        args.append(datetime.now(timezone.utc).isoformat())
    if last_error is not None:
        sets.append("last_error = ?")
        args.append(last_error)
    if not sets:
        return 0
    sets.append("updated_at = ?")
    args.append(datetime.now(timezone.utc).isoformat())
    if message_pk is not None:
        where = "id = ?"
        args.append(message_pk)
    elif message_id is not None:
        where = "message_id = ?"
        args.append(message_id)
    else:
        return 0
    c.execute(f"UPDATE messages SET {', '.join(sets)} WHERE {where}", args)
    return c.rowcount


def find_message_by_message_id(
    conn: sqlite3.Connection, message_id: str
) -> Optional[sqlite3.Row]:
    c = conn.cursor()
    c.execute(
        "SELECT * FROM messages WHERE message_id = ? ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
    return c.fetchone()


def get_latest_inbound_target(
    conn: sqlite3.Connection, contact_id: int, channel: str
) -> Optional[sqlite3.Row]:
    c = conn.cursor()
    c.execute(
        """
        SELECT * FROM messages
        WHERE contact_id = ? AND channel = ?
        ORDER BY id DESC LIMIT 1
        """,
        (contact_id, channel),
    )
    return c.fetchone()


# --- Replies ---


def add_reply(
    conn: sqlite3.Connection,
    contact_id: int,
    channel: str,
    message: str,
    message_id: Optional[str] = None,
) -> int:
    """Добавляет reply. Идемпотентно: если reply с таким message_id уже есть
    для этого контакта+канала, не дублирует (возвращает id существующего)."""
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    if message_id:
        # Проверяем — есть ли уже такой reply
        c.execute(
            "SELECT id FROM replies WHERE contact_id = ? AND channel = ? AND message_id = ?",
            (contact_id, channel, message_id),
        )
        existing = c.fetchone()
        if existing:
            return existing["id"]
    c.execute(
        """
        INSERT INTO replies (contact_id, channel, message, message_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (contact_id, channel, message, message_id, now),
    )
    return c.lastrowid


def get_replies_text(conn: sqlite3.Connection, contact_id: int, channel: str) -> str:
    c = conn.cursor()
    c.execute(
        "SELECT message, created_at FROM replies WHERE contact_id = ? AND channel = ? "
        "ORDER BY id ASC",
        (contact_id, channel),
    )
    rows = c.fetchall()
    if not rows:
        return ""
    parts = [f"[{r['created_at']}] {r['message']}" for r in rows]
    return config.REPLY_SEPARATOR.join(parts)


def get_replies_since(conn: sqlite3.Connection, contact_id: int, channel: str, since: str) -> str:
    """Возвращает только ответы ПОСЛЕ указанного времени (по sent_at сообщения)."""
    c = conn.cursor()
    c.execute(
        "SELECT message, created_at FROM replies WHERE contact_id = ? AND channel = ? "
        "AND created_at > ? ORDER BY id ASC",
        (contact_id, channel, since),
    )
    rows = c.fetchall()
    if not rows:
        return ""
    parts = [f"[{r['created_at']}] {r['message']}" for r in rows]
    return config.REPLY_SEPARATOR.join(parts)


# --- Queues ---


def enqueue_pending_webhook(channel: Optional[str], payload: dict) -> None:
    payload_text = json.dumps(payload, ensure_ascii=False, default=str)
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO pending_webhooks (channel, payload, received_at) VALUES (?, ?, ?)",
            (channel, payload_text, datetime.now(timezone.utc).isoformat()),
        )
    try:
        with open(config.PENDING_WEBHOOKS_PATH, "a", encoding="utf-8") as f:
            f.write(payload_text + "\n")
    except Exception:
        pass


def enqueue_sheet_op(op: str, payload: dict, message_pk: Optional[int] = None) -> None:
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO sheets_ops (op, message_pk, payload, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                op,
                message_pk,
                json.dumps(payload, ensure_ascii=False, default=str),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


# --- Rescue queue (Phase 7) ---
# Webhook handler enqueues failed WABA sends here.
# Sender drains this queue between main contacts → cascade TG/MAX.
#


def init_rescue_queue() -> None:
    """Создаёт таблицу rescue_queue (idempotent)."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS rescue_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                name TEXT,
                category TEXT,
                original_channel TEXT NOT NULL,
                reason TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                last_attempt_at TEXT,
                rescued_channel TEXT,
                rescued_message_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                tried_channels TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # Миграция для БД, созданных до появления tried_channels.
        if not _has_column(conn, "rescue_queue", "tried_channels"):
            c.execute(
                "ALTER TABLE rescue_queue ADD COLUMN tried_channels "
                "TEXT NOT NULL DEFAULT ''"
            )
        # Контакт, провалившийся во ВСЕХ каналах отката, не должен зависать
        # в очереди навсегда и не должен блокировать её начало. Он уходит
        # в status='deferred' с retry_after и возвращается в конец очереди
        # на следующий день — ровно то «переводим в последние назад».
        if not _has_column(conn, "rescue_queue", "retry_after"):
            c.execute(
                "ALTER TABLE rescue_queue ADD COLUMN retry_after TEXT"
            )
        # Потолок попыток (план «устойчивый каскад»): контакт, исчерпавший
        # каналы, обязан получить ТЕРМИНАЛЬНЫЙ статус 'exhausted', а не крутиться
        # в очереди вечно. Для этого считаем попытки по всем каналам, а не по
        # одной строке, и помним исход последней попытки.
        for _col, _ddl in (
            ("attempts_total", "INTEGER NOT NULL DEFAULT 0"),
            ("last_outcome", "TEXT"),
            ("next_eligible_at", "TEXT"),
            ("first_seen", "TEXT"),
        ):
            if not _has_column(conn, "rescue_queue", _col):
                c.execute(f"ALTER TABLE rescue_queue ADD COLUMN {_col} {_ddl}")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_rescue_status "
            "ON rescue_queue(status, created_at)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_rescue_phone "
            "ON rescue_queue(phone)"
        )


def init_delivery_tracking() -> None:
    """Трёхзначная доставка + счётчики дня в SQLite.

    Замер 2026-10-06 показал, что отправка (HTTP 200) и доставка — разные
    вещи, а молчание Meta вообще не приходит событием. Поэтому:

    * `messages.delivery_state` — трёхзначное состояние: delivered / unknown /
      rejected. `status` остаётся для обратной совместимости и UI.
    * `delivery_events` — аудит каждого события; UNIQUE(message_id,
      event_status) делает обработку идемпотентной, поэтому повторный POST
      или переигрывание лога не двигают счётчики.
    * `daily_counters` — счётчики дня, которые раньше жили в state.json.
      Два процесса (sender и webhook) делали read-modify-write одного файла,
      из-за чего `delivered_today` переползал `accepted_today` и отчёт
      показывал 128%. Здесь инкремент атомарен через UPSERT.
    """
    with db_conn() as conn:
        c = conn.cursor()
        if not _has_column(conn, "messages", "delivery_state"):
            c.execute(
                "ALTER TABLE messages ADD COLUMN delivery_state "
                "TEXT NOT NULL DEFAULT 'unknown'"
            )
            # Бэкфилл: исторические строки переводятся из прежнего status.
            c.execute(
                "UPDATE messages SET delivery_state = CASE "
                "  WHEN status IN ('delivered','read') THEN 'delivered' "
                "  WHEN status = 'failed' THEN 'rejected' "
                "  ELSE 'unknown' END "
                "WHERE delivery_state = 'unknown'"
            )
        # swept_at — метка «сторож уже разбирал это сообщение».
        #
        # Без неё тихое сообщение пересобиралось сторожем в каждой фазе
        # заново: контакт, которому Telegram только что ДОСТАВИЛ, снова
        # попадал в очередь отката по своему же старому молчаливому
        # сообщению в WABA и получал дубль в MAX. Раньше этого не
        # случалось только потому, что сторож переписывал status на
        # 'failed', и следующий проход его не видел.
        if not _has_column(conn, "messages", "swept_at"):
            c.execute("ALTER TABLE messages ADD COLUMN swept_at TEXT")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS delivery_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_pk INTEGER,
                message_id TEXT NOT NULL,
                channel TEXT,
                event_status TEXT NOT NULL,
                envelope TEXT,
                received_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'webhook'
            )
            """
        )
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_event "
            "ON delivery_events(message_id, event_status)"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_counters (
                date TEXT NOT NULL,
                channel TEXT NOT NULL,
                accepted INTEGER NOT NULL DEFAULT 0,
                delivered INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                unknown INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (date, channel)
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_cooldowns (
                phone TEXT NOT NULL,
                channel TEXT NOT NULL,
                until TEXT NOT NULL,
                reason TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (phone, channel)
            )
            """
        )


# ============================================================
# Счётчики дня (источник истины — SQLite, не state.json)
# ============================================================


def today_str() -> str:
    """'YYYY-MM-DD' в настроенной TZ проекта (BULK_TIMEZONE)."""
    return config.now_tz().strftime("%Y-%m-%d")


def upsert_counter(
    date: str,
    channel: str,
    field: str = "accepted",
    delta: int = 1,
) -> int:
    """Атомарный инкремент счётчика дня. Возвращает новое значение.

    UPSERT, а не read-modify-write: sender и webhook пишут счётчики
    одновременно, и при старой схеме один терял обновления другого.
    """
    if field not in ("accepted", "delivered", "rejected", "unknown"):
        raise ValueError(f"unknown counter field: {field}")
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            f"""
            INSERT INTO daily_counters (date, channel, {field}, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date, channel) DO UPDATE SET
                {field} = {field} + excluded.{field},
                updated_at = excluded.updated_at
            """,
            (date, channel, delta, now),
        )
        row = c.execute(
            f"SELECT {field} AS v FROM daily_counters "
            "WHERE date = ? AND channel = ?",
            (date, channel),
        ).fetchone()
        return int(row["v"]) if row else 0


def get_counters(date: Optional[str] = None) -> dict:
    """{channel: {accepted, delivered, rejected, unknown}} за дату."""
    date = date or today_str()
    out: dict = {}
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT channel, accepted, delivered, rejected, unknown "
            "FROM daily_counters WHERE date = ?",
            (date,),
        ).fetchall()
    for r in rows:
        out[r["channel"]] = {
            "accepted": int(r["accepted"]),
            "delivered": int(r["delivered"]),
            "rejected": int(r["rejected"]),
            "unknown": int(r["unknown"]),
        }
    return out


def migrate_counters_from_state(state_dict: dict) -> int:
    """Переносит счётчики дня из state.json в daily_counters (однократно).

    Возвращает число перенесённых значений. Идемпотентно: если на дату уже
    есть непустая строка в daily_counters — перенос не делается.
    """
    date = str(state_dict.get("date") or today_str())
    existing = get_counters(date)
    if any(any(v for v in row.values()) for row in existing.values()):
        return 0
    moved = 0
    for bucket, field in (
        ("accepted_today", "accepted"),
        ("delivered_today", "delivered"),
        ("failed_today", "rejected"),
    ):
        for channel, value in (state_dict.get(bucket) or {}).items():
            value = int(value or 0)
            if value <= 0:
                continue
            upsert_counter(date, channel, field, delta=value)
            moved += 1
    return moved


# ============================================================
# События доставки (идемпотентные)
# ============================================================


def apply_delivery_event(
    message_pk: int,
    message_id: str,
    channel: str,
    event_status: str,
    envelope: Optional[str] = None,
    source: str = "webhook",
) -> bool:
    """Регистрирует событие доставки. True — если это НОВОЕ событие.

    Идемпотентно: повтор по (message_id, event_status) игнорируется, поэтому
    дубль POST от Wazzup или переигрывание лога не двигают ни состояние
    сообщения, ни счётчики.
    """
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT OR IGNORE INTO delivery_events
                (message_pk, message_id, channel, event_status, envelope,
                 received_at, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (message_pk, message_id, channel, event_status,
             (envelope or "")[:500], now, source),
        )
        if c.rowcount == 0:
            return False
        if event_status == "delivered":
            c.execute(
                "UPDATE messages SET delivery_state = 'delivered', "
                "status = CASE WHEN status IN ('sent','queued') THEN 'delivered' "
                "             ELSE status END, "
                "updated_at = ? WHERE id = ?",
                (now, message_pk),
            )
        elif event_status == "rejected":
            c.execute(
                "UPDATE messages SET delivery_state = 'rejected', "
                "updated_at = ? WHERE id = ?",
                (now, message_pk),
            )
        return True


def set_delivery_state(message_pk: int, state: str) -> None:
    """Проставляет трёхзначное состояние без события (sweeper/sender)."""
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        conn.execute(
            "UPDATE messages SET delivery_state = ?, updated_at = ? WHERE id = ?",
            (state, now, message_pk),
        )


def mark_swept(message_pk: int) -> None:
    """Помечает сообщение как уже разобранное сторожем.

    Повторный разбор одного и того же молчания в следующей фазе отката
    создавал дубль: контакт, доставленный в Telegram, снова попадал в
    очередь по своему старому молчаливому сообщению в WABA.
    """
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        conn.execute(
            "UPDATE messages SET swept_at = ? WHERE id = ? AND swept_at IS NULL",
            (now, message_pk),
        )


def cancel_rescue_for_contact(phone: str) -> int:
    """Снимает контакт из очереди отката: доставка подтверждена.

    Позднее `delivered` после постановки в откат обязан отменять запись —
    иначе контакт получает дубликат в личном канале.
    """
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE rescue_queue SET status = 'cancelled', next_eligible_at = NULL, "
            "updated_at = ? WHERE phone = ? AND status IN ('pending', 'deferred')",
            (now, phone),
        )
        return c.rowcount


# ============================================================
# Антикоастинг по WABA (молчание = отказ Meta по маркетинговому лимиту)
# ============================================================


def set_contact_cooldown(
    phone: str, channel: str, until_iso: str, reason: Optional[str] = None
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO contact_cooldowns (phone, channel, until, reason, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(phone, channel) DO UPDATE SET "
            "until = excluded.until, reason = excluded.reason, "
            "updated_at = excluded.updated_at",
            (phone, channel, until_iso, (reason or "")[:200], now),
        )


def get_active_cooldowns(channel: str, now_iso: Optional[str] = None) -> set:
    """Номера с активным антикоастингом по каналу (для выборки контактов)."""
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT phone FROM contact_cooldowns WHERE channel = ? AND until > ?",
            (channel, now_iso),
        ).fetchall()
    return {r["phone"] for r in rows}


def recent_channel_send(phone: str, channel: str, hours: int = 24) -> bool:
    """True, если по этому контакту уже уходила отправка в канале за N часов.

    Инвариант «не больше одного сообщения на контакт в канале за сутки».
    Считаем именно отправки (status НЕ 'failed' — явный отказ канала не
    считаем, после него повтор по другому каналу разрешён), а не доставки:
    доставка неизвестна по определению, и ждать её нельзя.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM messages m
            JOIN contacts ct ON ct.id = m.contact_id
            WHERE ct.phone = ? AND m.channel = ?
              AND m.status != 'failed'
              AND m.sent_at IS NOT NULL
              AND CAST(strftime('%s', m.sent_at) AS INTEGER) >= ?
            """,
            (phone, channel, cutoff),
        ).fetchone()
    return bool(row and int(row[0]) > 0)


def enqueue_rescue(
    phone: str,
    name: Optional[str],
    category: Optional[str],
    original_channel: str = "waba",
    reason: Optional[str] = None,
) -> Optional[int]:
    """Ставит phone в очередь на rescue (cascade Personal).

    Идемпотентно: если уже есть pending rescue для этого phone,
    НЕ создаём дубль (просто обновляем reason/updated_at).
    """
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id FROM rescue_queue
            WHERE phone = ? AND status = 'pending'
            ORDER BY id DESC LIMIT 1
            """,
            (phone,),
        )
        existing = c.fetchone()
        if existing is not None:
            c.execute(
                """
                UPDATE rescue_queue
                SET reason = COALESCE(?, reason),
                    updated_at = ?
                WHERE id = ?
                """,
                (reason, now, existing["id"]),
            )
            return existing["id"]
        c.execute(
            """
            INSERT INTO rescue_queue
                (phone, name, category, original_channel, reason, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (phone, name, category, original_channel, reason, now, now),
        )
        return c.lastrowid


def pending_rescues(
    limit: int = 5,
    cooldown_seconds: int = 60,
    original_channel: Optional[str] = None,
    exclude_tried: Optional[str] = None,
) -> list[dict]:
    """Возвращает список pending rescues, готовых к обработке.

    Не возвращает записи, у которых last_attempt_at свежее cooldown_seconds
    (чтобы не спамить Personal каналы).

    original_channel — фильтр по каналу, на котором была неудача (например
    'waba'). Нужен фазовой рассылке: фаза Telegram берёт только провалы WABA,
    а не всё подряд из очереди.

    exclude_tried — канал отката, который для этой записи УЖЕ пробовали
    (tried_channels). Без этого фильтра фаза MAX брала те же записи, что
    только что отработала фаза Telegram, и контакт получал второе сообщение.
    """
    from datetime import datetime, timezone, timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=cooldown_seconds)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    # pending — обычная очередь; deferred — вернётся, когда подойдёт retry_after.
    # Порядок: сначала свежие pending, потом «отложенные» — то есть отложенные
    # ВСЕГДА уходят в конец, ровно как требовалось.
    sql = (
        "SELECT * FROM rescue_queue "
        "WHERE (status = 'pending'"
        "   OR (status = 'deferred' AND retry_after IS NOT NULL "
        "       AND retry_after <= ?)) "
        "AND (last_attempt_at IS NULL OR last_attempt_at < ?)"
    )
    args: list = [now_iso, cutoff]
    if original_channel:
        sql += " AND original_channel = ?"
        args.append(original_channel)
    if exclude_tried:
        # «telegram» не должен попадать в «telegram,telegram,» — сравниваем
        # по целому токену, поэтому LIKE с границами через запятую.
        sql += (
            " AND (tried_channels IS NULL OR tried_channels = ''"
            " OR (',' || tried_channels || ',') NOT LIKE ?)"
        )
        args.append(f"%,{exclude_tried},%")
    sql += " ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, " \
           "created_at ASC LIMIT ?"
    args.append(limit)

    with db_conn() as conn:
        c = conn.cursor()
        rows = c.execute(sql, args).fetchall()
    # Запись, которую мы подобрали как отложенную, снова в работе: снимаем
    # отметку отсрочки и чистим tried_channels, иначе exclude_tried
    # продолжал бы считать канал «уже пробованным» и контакт не получал бы
    # ни одной новой попытки.
    picked = [dict(r) for r in rows]
    revived = [r["id"] for r in picked if r.get("status") == "deferred"]
    if revived:
        with db_conn() as conn:
            conn.execute(
                "UPDATE rescue_queue SET status='pending', retry_after=NULL, "
                "tried_channels='' WHERE id IN "
                f"({','.join('?' * len(revived))})",
                revived,
            )
        for r in picked:
            if r["id"] in revived:
                r["tried_channels"] = ""
    return picked


def mark_rescue_deferred(rescue_id: int, retry_after_iso: str) -> None:
    """Уводит контакт в конец очереди до retry_after_iso.

    Все каналы отката уже отработали и не сработали — контакт не теряется,
    а просто ждёт следующего дня. tried_channels чистится, чтобы новая
    попытка прошла по всем каналам заново.
    """
    if not rescue_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        conn.execute(
            "UPDATE rescue_queue SET status='deferred', retry_after=?, "
            "tried_channels='', updated_at=? WHERE id = ?",
            (retry_after_iso, now, rescue_id),
        )


def count_deferred_rescues() -> int:
    with db_conn() as conn:
        return (conn.execute(
            "SELECT COUNT(*) FROM rescue_queue WHERE status = 'deferred'"
        ).fetchone() or [0])[0]


def count_pending_rescues() -> int:
    with db_conn() as conn:
        c = conn.cursor()
        return (c.execute("SELECT COUNT(*) FROM rescue_queue WHERE status = 'pending'").fetchone() or [0])[0]


def mark_rescue_attempt(rescue_id: int, error: Optional[str] = None) -> None:
    """Инкрементирует attempts, ставит timestamp. Если attempts >= 3 → failed."""
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            UPDATE rescue_queue
            SET attempts = attempts + 1,
                attempts_total = attempts_total + 1,
                last_attempt_at = ?,
                last_outcome = ?,
                first_seen = COALESCE(first_seen, ?),
                updated_at = ?,
                status = CASE WHEN attempts + 1 >= 3 THEN 'failed' ELSE 'pending' END
            WHERE id = ?
            """,
            (now, (error or "")[:200], now, now, rescue_id),
        )


# ============================================================
# Терминальный статус exhausted (план «устойчивый каскад»)
# ============================================================


def get_rescue_row(rescue_id: int) -> Optional[dict]:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM rescue_queue WHERE id = ?", (rescue_id,)
        ).fetchone()
    return dict(row) if row else None


def mark_rescue_exhausted(rescue_id: int, reason: str = "all_channels_tried") -> None:
    """Терминальный статус: контакт больше не трогается автоматически.

    Бесконечный retry в очереди — это расход квоты на заведомо недостижимые
    номера. Здесь контакт окончательно выходит из автоматики и попадает в
    ежедневную выгрузку для ручного разбора.
    """
    if not rescue_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        conn.execute(
            "UPDATE rescue_queue SET status='exhausted', next_eligible_at=NULL, "
            "last_outcome=?, updated_at=? WHERE id=?",
            (reason[:200], now, rescue_id),
        )


def bump_attempts_total(rescue_id: int, outcome: Optional[str] = None) -> int:
    """Считает попытку в канале отката (в т.ч. явный отказ транспорта)."""
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE rescue_queue SET attempts_total = attempts_total + 1, "
            "last_outcome = ?, first_seen = COALESCE(first_seen, ?), "
            "updated_at = ? WHERE id = ?",
            ((outcome or "")[:200], now, now, rescue_id),
        )
        row = c.execute(
            "SELECT attempts_total FROM rescue_queue WHERE id = ?", (rescue_id,)
        ).fetchone()
        return int(row["attempts_total"]) if row else 0


def should_exhaust(
    rescue_id: int, all_channels: list, now_iso: Optional[str] = None
) -> Optional[str]:
    """Решает, исчерпан ли контакт. Возвращает причину или None.

    Условия (из плана):
      * attempts_total >= EXHAUST_MAX_ATTEMPTS, или
      * испробованы ВСЕ каналы И первая попытка старше EXHAUST_WINDOW_DAYS.
    """
    row = get_rescue_row(rescue_id)
    if row is None:
        return None
    if row.get("status") == "exhausted":
        return None
    if int(row.get("attempts_total") or 0) >= int(config.EXHAUST_MAX_ATTEMPTS):
        return f"attempts>={config.EXHAUST_MAX_ATTEMPTS}"
    tried = set(_split_tried(row.get("tried_channels")))
    if all_channels and set(all_channels).issubset(tried):
        first_seen = row.get("first_seen") or row.get("created_at")
        try:
            age_days = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(str(first_seen))
            ).days
        except Exception:
            age_days = 0
        if age_days >= int(config.EXHAUST_WINDOW_DAYS):
            return f"all_channels_tried, age>={config.EXHAUST_WINDOW_DAYS}d"
    return None


def count_exhausted_rescues() -> int:
    with db_conn() as conn:
        return (conn.execute(
            "SELECT COUNT(*) FROM rescue_queue WHERE status = 'exhausted'"
        ).fetchone() or [0])[0]


def exhausted_rows() -> list:
    with db_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT phone, name, category, original_channel, reason, "
                "tried_channels, attempts_total, last_outcome, last_attempt_at, "
                "updated_at FROM rescue_queue WHERE status = 'exhausted' "
                "ORDER BY updated_at DESC"
            )
        ]


def export_exhausted_csv(data_dir) -> Optional[str]:
    """Выгружает исчерпанных в CSV, но не чаще одного раза в сутки.

    Терминальный статус без выгрузки = тихая потеря контакта: система
    перестаёт его трогать, и владелец не узнает, что номер выбыл. Файл
    `exhausted_YYYY-MM-DD.csv` служит маркером того, что выгрузка за день
    уже была.
    """
    import csv
    from pathlib import Path

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    date = today_str()
    out = data_dir / f"exhausted_{date}.csv"
    if out.exists():
        return None
    rows = exhausted_rows()
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["phone", "name", "category", "original_channel",
                    "tried_channels", "attempts_total", "reason",
                    "last_outcome", "last_attempt_at"])
        for r in rows:
            w.writerow([
                r["phone"], r.get("name"), r.get("category"),
                r.get("original_channel"), r.get("tried_channels"),
                r.get("attempts_total"), r.get("reason"),
                r.get("last_outcome"), r.get("last_attempt_at"),
            ])
    return str(out)


def _split_tried(raw: Optional[str]) -> list[str]:
    return [x for x in (raw or "").split(",") if x]


def rescue_tried_channels(rescue_id: int) -> list[str]:
    """Какие каналы отката уже пробовали для этой записи."""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT tried_channels FROM rescue_queue WHERE id = ?",
            (rescue_id,),
        ).fetchone()
    return _split_tried(row["tried_channels"] if row else "")


def mark_rescue_tried(rescue_id: int, channel: str) -> None:
    """Отмечает канал отката как использованный.

    Зачем: без этого одна запись в очереди обслуживалась ВСЕМИ фазами
    подряд (TG → MAX → SMS), и контакт получал три сообщения подряд в
    разные каналы — после отказа в первом. Для отказов по номеру (нет
    аккаунта) это просто мусор, а для отказа с жалобой — прямой путь к
    бану личного аккаунта.
    """
    if not rescue_id or not channel:
        return
    with db_conn() as conn:
        row = conn.execute(
            "SELECT tried_channels FROM rescue_queue WHERE id = ?",
            (rescue_id,),
        ).fetchone()
        if row is None:
            return
        tried = _split_tried(row["tried_channels"])
        if channel not in tried:
            tried.append(channel)
        conn.execute(
            "UPDATE rescue_queue SET tried_channels = ?, updated_at = ? WHERE id = ?",
            (",".join(tried), datetime.now(timezone.utc).isoformat(), rescue_id),
        )


def mark_rescue_success(
    rescue_id: int, channel: str, message_id: str
) -> None:
    """Помечает rescue успешно завершённым (Personal доставлен)."""
    now = datetime.now(timezone.utc).isoformat()
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            UPDATE rescue_queue
            SET status = 'rescued',
                rescued_channel = ?,
                rescued_message_id = ?,
                last_attempt_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (channel, message_id, now, now, rescue_id),
        )


def get_rescue_by_id(rescue_id: int) -> Optional[dict]:
    with db_conn() as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT * FROM rescue_queue WHERE id = ?", (rescue_id,)
        ).fetchone()
    return dict(row) if row else None


# --- Channel reachability cache (Phase 0.1 + 2.1) ---


def upsert_reachability(
    conn: sqlite3.Connection,
    phone: str,
    channel: str,
    reachable: bool,
    last_error: Optional[str] = None,
) -> None:
    """Сохраняет результат pre-check в кэш reachability.

    TTL=24ч читается приложением (state.py фильтрует по timestamp).
    """
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO channel_reachability (phone, channel, reachable, last_checked_at, last_error)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(phone, channel) DO UPDATE SET
            reachable = excluded.reachable,
            last_checked_at = excluded.last_checked_at,
            last_error = excluded.last_error
        """,
        (phone, channel, 1 if reachable else 0,
         datetime.now(timezone.utc).isoformat(), last_error),
    )


def get_reachability(
    conn: sqlite3.Connection, phone: str, channel: str
) -> Optional[sqlite3.Row]:
    c = conn.cursor()
    c.execute(
        "SELECT * FROM channel_reachability WHERE phone = ? AND channel = ?",
        (phone, channel),
    )
    return c.fetchone()


def get_all_reachability_for_phone(
    conn: sqlite3.Connection, phone: str
) -> dict[str, sqlite3.Row]:
    c = conn.cursor()
    rows = c.execute(
        "SELECT * FROM channel_reachability WHERE phone = ?", (phone,)
    ).fetchall()
    return {r["channel"]: r for r in rows}


def find_unconfirmed(
    minutes: int = 60,
    limit: int = 200,
    channels: tuple = ("waba", "telegram", "max", "whatsapp"),
    max_age_hours: int = 24,
    only_status: Optional[tuple] = ("sent",),
    min_age_hours: int = 0,
) -> list[sqlite3.Row]:
    """Сообщения, ПРИНЯТЫЕ транспортом, но БЕЗ подтверждения доставки.

    Это страховка на случай, когда вебхук не пришёл вовсе: без неё сообщение
    навсегда остаётся в статусе `sent`, и rescue-очередь его никогда не видит
    — контакт просто тихо исчезает из воронки. Wazzup обычно присылает
    delivery_status за секунды-минуты, поэтому 60 минут — консервативный порог.

    ВАЖНО: есть и верхняя граница `max_age_hours`. Без неё первый запуск
    поднял бы в rescue ВСЮ историю (в реальной базе — 150 сообщений от июля,
    2,5 месяца назад) и повторно отрекламировал бы старым контактам, выбив
    дневной бюджет. Сторож ловит только «свежее принятое, но не подтверждённое».

    `only_status` позволяет сторожу разбирать ЯВНЫЕ отказы ('failed') и
    МОЛЧАНИЕ ('sent') разными горизонтами: молчание не равно отказу, у него
    свой (длинный) таймаут. `max_age_hours=0` снимает верхнюю границу —
    так отбираются сообщения для перевода в exhausted, и там обязательно
    задаётся `min_age_hours`, иначе в exhausted уедут СВЕЖИЕ сообщения,
    которые просто ещё не дождались подтверждения.
    """
    statuses = only_status or ("sent",)
    s_marks = ",".join("?" * len(statuses))
    c_limit = minutes * 60
    age_limit = max_age_hours * 3600
    min_limit = min_age_hours * 3600
    marks = ",".join("?" * len(channels))
    age_clause = (
        "" if max_age_hours <= 0 else
        "AND CAST(strftime('%s','now') AS INTEGER) - "
        "CAST(strftime('%s', COALESCE(m.sent_at, m.created_at)) AS INTEGER) < ?"
    )
    min_clause = (
        "AND CAST(strftime('%s','now') AS INTEGER) - "
        "CAST(strftime('%s', COALESCE(m.sent_at, m.created_at)) AS INTEGER) > ?"
    ) if min_limit > 0 else ""
    # Порядок параметров ДОЛЖЕН совпадать с порядком плейсхолдеров в SQL:
    # сначала идёт `m.status IN (...)`, затем `m.channel IN (...)`.
    params: list = [*statuses, *channels]
    params.append(c_limit)
    if max_age_hours > 0:
        params.append(age_limit)
    if min_limit > 0:
        params.append(min_limit)
    params.append(limit)
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            f"""
            SELECT m.id AS message_pk, m.channel, m.message_id, m.sent_at,
                   m.created_at, ct.id AS contact_id, ct.phone, ct.name,
                   ct.category
            FROM messages m
            JOIN contacts ct ON ct.id = m.contact_id
            WHERE m.status IN ({s_marks})
              AND m.delivered_at IS NULL
              AND m.read_at IS NULL
              AND m.answered_at IS NULL
              AND m.swept_at IS NULL
              AND m.channel IN ({marks})
              AND CAST(strftime('%s','now') AS INTEGER) -
                  CAST(strftime('%s', COALESCE(m.sent_at, m.created_at)) AS INTEGER) > ?
              {age_clause}
              {min_clause}
            ORDER BY m.id ASC
            LIMIT ?
            """,
            params,
        )
        return c.fetchall()


def waba_recently_failed(phone: str, hours: int = 24) -> bool:
    """True если для этого phone был WABA `failed` в последние N часов.

    Используется cascade'ом для skip WABA → сразу TG/MAX (lazy retry).
    Запрос по status='failed' в messages table, не rely на webhook.
    """
    c_limit = hours * 3600  # seconds
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT COUNT(*) FROM messages m
            JOIN contacts ct ON ct.id = m.contact_id
            WHERE ct.phone = ? AND m.channel = 'waba'
              AND m.status = 'failed'
              AND CAST(strftime('%s','now') AS INTEGER) -
                  CAST(strftime('%s', m.created_at) AS INTEGER) < ?
            """,
            (phone, c_limit),
        )
        return (c.fetchone() or [0])[0] > 0


def waba_recent_failures(phone: str, hours: int = 24) -> list[dict]:
    """Возвращает список WABA failed messages для phone (для дебага/log)."""
    c_limit = hours * 3600
    with db_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            """
            SELECT m.id, m.message_id, m.last_error, m.created_at
            FROM messages m
            JOIN contacts ct ON ct.id = m.contact_id
            WHERE ct.phone = ? AND m.channel = 'waba'
              AND m.status = 'failed'
              AND CAST(strftime('%s','now') AS INTEGER) -
                  CAST(strftime('%s', m.created_at) AS INTEGER) < ?
            ORDER BY m.created_at DESC
            """,
            (phone, c_limit),
        ).fetchall()
    return [dict(r) for r in rows]


# --- WABA templates registry (Phase 1.2) ---


def upsert_waba_template(
    conn: sqlite3.Connection,
    template_id: str,
    name: str,
    category: Optional[str],
    language_code: str,
    status: str,
    last_error: Optional[str] = None,
) -> None:
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO waba_templates (template_id, name, category, language_code,
                                    status, last_synced_at, last_error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(template_id) DO UPDATE SET
            name = excluded.name,
            category = excluded.category,
            language_code = excluded.language_code,
            status = excluded.status,
            last_synced_at = excluded.last_synced_at,
            last_error = excluded.last_error
        """,
        (
            template_id, name, category, language_code, status,
            datetime.now(timezone.utc).isoformat(), last_error,
        ),
    )


def get_waba_template(
    conn: sqlite3.Connection, template_id: str
) -> Optional[sqlite3.Row]:
    c = conn.cursor()
    c.execute("SELECT * FROM waba_templates WHERE template_id = ?", (template_id,))
    return c.fetchone()


def list_waba_templates(
    conn: sqlite3.Connection, only_approved: bool = False
) -> list[sqlite3.Row]:
    c = conn.cursor()
    if only_approved:
        return c.execute(
            "SELECT * FROM waba_templates WHERE status = 'approved' "
            "ORDER BY category, name"
        ).fetchall()
    return c.execute(
        "SELECT * FROM waba_templates ORDER BY category, name"
    ).fetchall()


# --- Complaint events log (Phase 3.6) ---


def log_complaint(
    conn: sqlite3.Connection,
    channel: str,
    phone: Optional[str],
    complaint_kind: str,
    message_id: Optional[str] = None,
) -> int:
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO complaint_events (channel, phone, complaint_kind, message_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            channel, phone, complaint_kind, message_id,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return c.lastrowid


def count_complaints_since(
    conn: sqlite3.Connection, channel: str, since_iso: str
) -> int:
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) AS n FROM complaint_events "
        "WHERE channel = ? AND created_at >= ?",
        (channel, since_iso),
    )
    row = c.fetchone()
    return int(row["n"]) if row else 0


def cleanup_old_complaints(conn: sqlite3.Connection, older_than_iso: str) -> int:
    """Удаляет старые жалобы (для per-day housekeeping)."""
    c = conn.cursor()
    c.execute("DELETE FROM complaint_events WHERE created_at < ?", (older_than_iso,))
    return c.rowcount
