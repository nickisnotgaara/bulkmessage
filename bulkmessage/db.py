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
                created_at TEXT NOT NULL
            )
            """
        )
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
                wappi_message_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_replies_contact ON replies(contact_id)")
        # Уникальный индекс для защиты от дублей: один и тот же wappi_message_id
        # для одного контакта+канала должен быть только один reply
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_replies_unique_wappi "
            "ON replies(contact_id, channel, wappi_message_id) "
            "WHERE wappi_message_id IS NOT NULL"
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
    """Пары (phone, channel), по которым сообщение реально ушло в Wappi
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
    # Если передан message_id (значит сообщение реально ушло в Wappi),
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
    wappi_message_id: Optional[str] = None,
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
    if wappi_message_id:
        sets.append("message_id = COALESCE(NULLIF(message_id, ''), ?)")
        args.append(wappi_message_id)
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


def find_message_by_wappi_id(
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
    wappi_message_id: Optional[str] = None,
) -> int:
    """Добавляет reply. Идемпотентно: если reply с таким wappi_message_id уже есть
    для этого контакта+канала, не дублирует (возвращает id существующего)."""
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    if wappi_message_id:
        # Проверяем — есть ли уже такой reply
        c.execute(
            "SELECT id FROM replies WHERE contact_id = ? AND channel = ? AND wappi_message_id = ?",
            (contact_id, channel, wappi_message_id),
        )
        existing = c.fetchone()
        if existing:
            return existing["id"]
    c.execute(
        """
        INSERT INTO replies (contact_id, channel, message, wappi_message_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (contact_id, channel, message, wappi_message_id, now),
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
