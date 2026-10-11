"""SQLite layer: schema, connection context, helpers."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from . import config
from .store.enums import Consent


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
#
# Значения не выдуманы здесь: они живут в store/enums.Consent и защищены
# CHECK-ограничением в БД (миграция 9). Дубль определений — прямая причина
# бага, из-за которого sendable() искал consent='yes' и не находил ничего.
CONSENT_GRANTED = Consent.GRANTED.value
CONSENT_UNKNOWN = Consent.UNKNOWN.value
CONSENT_DENIED = Consent.DENIED.value

_CONSENT_VALUES = (CONSENT_GRANTED, CONSENT_UNKNOWN, CONSENT_DENIED)


def normalize_consent(value) -> str:
    """Приводит любой ввод (из Excel, AmoCRM, API) к трём состояниям.

    Возвращает значение из словаря ``Consent`` — не изобретает своих.
    """
    if value is None:
        return Consent.UNKNOWN.value
    s = str(value).strip().lower()
    if s in ("granted", "yes", "y", "true", "1", "да", "1", "согласие",
             "согласие получено", "opt-in", "optin", "подписан", "ok", "+"):
        return Consent.GRANTED.value
    if s in ("denied", "no", "n", "false", "0", "нет", "отказ", "отписка",
             "opt-out", "optout", "unsubscribe", "stop", "блок", "-"):
        return Consent.DENIED.value
    return Consent.UNKNOWN.value


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
    """Создаёт или обновляет схему БД до актуальной версии. Идемпотентно.

    Вся работа со схемой живёт в ``bulkmessage.store.migrations``. Здесь её
    нет намеренно: DDL, продублированный в двух местах, разъезжается — колонка
    добавляется в одной копии, а работает другая. Свежую базу создаёт
    применение миграций 1..N по порядку, старая доводится до той же версии
    теми же миграциями. Один путь — один результат.
    """
    from .store import migrations as _migrations

    _migrations.ensure_migrated()


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


def get_sent_phones(conn: sqlite3.Connection | None = None) -> set[str]:
    """Контакты, которым уже отправляли в ЛЮБОМ канале.

    Фасад над ``store.messages.contacted_phones`` — правило живёт там, и
    больше нигде. Держать его здесь тоже было можно, но тогда правило
    существовало бы в двух местах, и именно это расхождение стоило 110
    контактов: код считал по статусу, AGENTS.md предписывал по message_id.

    Параметр ``conn`` оставлен ради совместимости вызовов и НЕ используется:
    репозиторий открывает своё соединение через ``store.connection``, чтобы
    не тащить SQL сюда. Чтение при включённом WAL не блокирует писателя.
    """
    from .store import messages as _messages

    return _messages.contacted_phones()


def count_pool_contacts(conn: sqlite3.Connection | None = None) -> int:
    """Сколько контактов вообще доступно для рассылки.

    Живёт здесь, а не в вызывающем коде, потому что критерий «доступен»
    (не тест, не невалид) обязан быть ОДНИМ во всех местах: иначе отчёт
    «пуск 4465, в работе 250» начинает расходиться с «из чего выбирали».
    """
    from .store import contacts as _contacts

    try:
        return _contacts.pool_size()
    except Exception:  # noqa: BLE001 — план не должен ронять старт прогона
        return 0


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
    """Схему теперь создаёт мигратор (``bulkmessage.store.migrations``).

    Здесь стоял ещё один ``CREATE TABLE`` со своей формой таблицы. Две копии
    одной схемы в разных файлах разъезжаются: колонка могла быть добавлена в
    одной, а работала другая, и результат зависел от порядка вызовов.
    Функция остаётся как совместимая заглушка, а форма таблицы определяется
    ТОЛЬКО миграциями.
    """
    init_db()

def init_delivery_tracking() -> None:
    """Схему теперь создаёт мигратор (``bulkmessage.store.migrations``).

    Здесь стоял ещё один ``CREATE TABLE`` со своей формой таблицы. Две копии
    одной схемы в разных файлах разъезжаются: колонка могла быть добавлена в
    одной, а работала другая, и результат зависел от порядка вызовов.
    Функция остаётся как совместимая заглушка, а форма таблицы определяется
    ТОЛЬКО миграциями.
    """
    init_db()

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


def _iso_newer(a_iso: Optional[str], b_iso: Optional[str]) -> bool:
    """True, если метка a отвечает моменту позже метки b (мягкая разборка)."""
    if not a_iso or not b_iso:
        return False
    try:
        a = datetime.fromisoformat(str(a_iso))
        b = datetime.fromisoformat(str(b_iso))
    except (TypeError, ValueError):
        return False
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return a > b


def _rescue_delivery_confirmed(conn, row) -> bool:
    """Подтверждена ли доставка сообщения, которым контакт был «спасён».

    Строка rescue со status='rescued' означает «отправка ПРИНЯТА каналом», а не
    «доставлено». Разница принципиальна: принятое и неподтверждённое сообщение
    должно довести контакт до следующего канала, а подтверждённое — закрыть
    вопрос, чтобы не слать человеку второе сообщение.
    """
    mid = row["rescued_message_id"] if "rescued_message_id" in row.keys() else None
    if not mid:
        return False
    r = conn.execute(
        "SELECT status, delivery_state, delivered_at FROM messages "
        "WHERE message_id = ?",
        (mid,),
    ).fetchone()
    if r is None:
        return False
    if r["delivered_at"] or str(r["status"] or "").lower() == "delivered":
        return True
    return str(r["delivery_state"] or "") == "delivered"


def _iso_within_hours(iso: Optional[str], hours: float) -> bool:
    """True, если метка времени не старше N часов назад.

    Мягкая разборка: битая метка считается «старой» — лучше лишняя новая
    строка (её погасит инвариант 24ч/канал), чем вечно заблокированный контакт.
    """
    if not iso:
        return False
    try:
        ts = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() < hours * 3600


def last_sent_by_phone() -> dict:
    """{phone: метка последней отправки} по всем каналам.

    Нужна для отбора: владелец требует «сначала тем, кому мы ещё НИКОГДА не
    писали». Без этого выбор шёл в порядке Excel, и прогон мог целиком уйти
    в тех, кому уже писали — а те, кто не получил рекламы ни разу, ждали.
    """
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT ct.phone, MAX(COALESCE(m.sent_at, m.created_at)) AS last_sent
            FROM messages m JOIN contacts ct ON ct.id = m.contact_id
            WHERE ct.phone IS NOT NULL AND ct.phone != ''
            GROUP BY ct.phone
            """
        ).fetchall()
    return {r["phone"]: r["last_sent"] for r in rows}


def enqueue_rescue(
    phone: str,
    name: Optional[str],
    category: Optional[str],
    original_channel: str = "waba",
    reason: Optional[str] = None,
    since: Optional[str] = None,
) -> Optional[int]:
    """Ставит phone в очередь на rescue (cascade Personal).

    Идемпотентно ПО КОНТАКТУ, а не по статусу строки: у номера в каждый момент
    есть только одна спасательная строка. Раньше совпадение искалось лишь среди
    pending/deferred, и как только строка уходила в rescued (успешный откат),
    следующее молчание WABA создавало вторую строку с ПУСТЫМ tried_channels.
    Дальше каскад заново пробовал каналы, где контакт уже получил сообщение
    (спасал только инвариант «одно сообщение на канал в сутки»), а
    should_exhaust смотрел на attempts_total=0 и не срабатывал никогда —
    контакт крутился в очереди и жег лимиты личных аккаунтов.

    Правила:
      * pending/deferred — живая строка: обновляем reason, отсрочку снимаем.
      * rescued с ПОДТВЕРЖДЁННОЙ доставкой — сообщение дошло, вопрос закрыт,
        в личные каналы второй раз не идём (возвращаем None).
      * rescued без подтверждения — канал ПРИНЯЛ, но не доставил. Это не
        дубль: контакт обязан дойти до следующего канала, поэтому строка
        переоткрывается в той же записи, а tried_channels сохраняется —
        канал, где сообщение уже было, повторно не трогаем.
      * failed — попытка не сработала, строка возвращается в работу.
      * exhausted — терминальный статус, автоматика контакт не трогает (None).
      * rescued/failed старше окна — новый цикл с чистой историей: окно
        прошло, а инвариант «одно сообщение на контакт в канале за сутки»
        снова не даст повторить тот же канал.

    since — момент «нового» события (для сторожа молчания это sent_at самого
    сообщения). Если строка отложена и это данное не новее её updated_at,
    отсрочка сохраняется: иначе сторож воскресил бы строку теми же самыми
    данными, которые её и отложили.
    """
    now = datetime.now(timezone.utc).isoformat()
    cooldown_hours = int(getattr(config, "CONTACT_CHANNEL_COOLDOWN_HOURS", 24) or 24)
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT * FROM rescue_queue
            WHERE phone = ? AND status != 'cancelled'
            ORDER BY id DESC LIMIT 1
            """,
            (phone,),
        )
        existing = c.fetchone()
        if existing is not None and existing["status"] not in ("pending", "deferred"):
            # Строка уже не ждёт отката: успех (rescued), исчерпание
            # (exhausted) или неуспех (failed).
            st = existing["status"]
            if st == "exhausted":
                logging.getLogger("db").info(
                    f"rescue: {phone} исчерпан "
                    f"({existing['last_outcome'] or ''}) — повторно не ставим"
                )
                return None
            if st == "rescued" and _rescue_delivery_confirmed(conn, existing):
                logging.getLogger("db").info(
                    f"rescue: {phone} уже получил сообщение в "
                    f"{existing['rescued_channel']} (доставка подтверждена) — "
                    "дубль не создаём"
                )
                return None
            if _iso_within_hours(existing["updated_at"], cooldown_hours):
                # Свежий неуспех: тот же контакт, тот же каскад. Возвращаем
                # запись в работу, НЕ трогая tried_channels.
                c.execute(
                    """
                    UPDATE rescue_queue
                    SET reason = COALESCE(?, reason),
                        status = 'pending',
                        retry_after = NULL,
                        rescued_channel = NULL,
                        rescued_message_id = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (reason, now, existing["id"]),
                )
                logging.getLogger("db").info(
                    f"rescue: {phone} переоткрыт для следующего канала "
                    f"(был {st}, tried=[{existing['tried_channels'] or ''}])"
                )
                return existing["id"]
        if existing is not None:
            # Найдена живая запись. Если она отложена (deferred), а контакт
            # ТОЛЬКО ЧТО снова провалился — новый провал снимает отсрочку,
            # иначе он «переспрашивал» бы контакт завтра, хотя уже сегодня
            # есть свежие данные.
            #
            # Но только если это действительно НОВЫЕ данные. Сторож молчания
            # разбирает в том числе тот самый отказ, из-за которого строку
            # только что отложили; без такой проверки отсрочка снималась бы
            # теми же данными и контакт крутился бы в очереди бесконечно,
            # сжигая квоту личных каналов на заведомо мёртвый номер.
            if existing["status"] == "deferred" and since and not _iso_newer(
                    since, existing["updated_at"]):
                logging.getLogger("db").info(
                    f"rescue: {phone} отложен до "
                    f"{existing['retry_after'] or 'завтра'} — свежих данных "
                    "нет, остаётся в конце очереди"
                )
                return existing["id"]
            c.execute(
                """
                UPDATE rescue_queue
                SET reason = COALESCE(?, reason),
                    status = CASE WHEN status = 'deferred' THEN 'pending'
                                  ELSE status END,
                    retry_after = CASE WHEN status = 'deferred' THEN NULL
                                       ELSE retry_after END,
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


# ══════════════════════════════════════════════════════════════════════════
# СОВМЕСТИМОСТЬ: тонкие обёртки над bulkmessage.store
# ══════════════════════════════════════════════════════════════════════════
# Реальная логика живёт в store/: там единственный SQL, там миграции и
# словарь значений. Этот блок существует только для вызывающего кода
# (probe.py, preflight.py, report.py, sender.py), который исторически
# обращается к db.<функция>.
#
# Новый код должен импортировать store напрямую:
#     from bulkmessage.store import channel_state
# и не растить этот список.


def _query_rows(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Строки по SQL (совместимость). В store — в репозиториях."""
    from .store.connection import read_only
    with read_only() as _conn:
        return _conn.execute(sql, params).fetchall()


def _scalar_count(sql: str, params: tuple = ()) -> int:
    """Целое по SQL (совместимость)."""
    from .store.connection import read_only
    with read_only() as _conn:
        _row = _conn.execute(sql, params).fetchone()
    return int(_row[0]) if _row else 0


def retire_unconfirmed(message_pk: int, state: str) -> None:
    """См. store.messages.retire_unconfirmed."""
    from .store import messages as _messages
    _messages.retire_unconfirmed(message_pk, state)


def upsert_channel_state(
    contact_id: int,
    channel: str,
    verdict: str,
    *,
    verdict_source: Optional[str] = None,
    chat_id: Optional[str] = None,
    last_probe_at: Optional[str] = None,
    last_send_at: Optional[str] = None,
    last_error: Optional[str] = None,
) -> None:
    """См. store.channel_state.set_verdict."""
    from .store import channel_state as _cs
    _cs.set_verdict(
        contact_id, channel, verdict, source=verdict_source, chat_id=chat_id,
        probe_at=last_probe_at, send_at=last_send_at,
        probed=last_probe_at is not None, sent=last_send_at is not None,
        last_error=last_error,
    )


def get_channel_state(contact_id: int, channel: str):
    """См. store.channel_state.get."""
    from .store import channel_state as _cs
    return _cs.get(contact_id, channel)


def probe_is_needed(contact_id: int, channel: str, ttl_hours: int) -> bool:
    """См. store.channel_state.needs_probe."""
    from .store import channel_state as _cs
    return _cs.needs_probe(contact_id, channel, ttl_hours)


def pending_rollback_candidates(channels: Optional[list] = None) -> list:
    """См. store.channel_state.actionable."""
    from .store import channel_state as _cs
    return _cs.actionable(channels)


def seed_channel_state_from_history() -> dict:
    """См. store.channel_state.seed_from_history."""
    from .store import channel_state as _cs
    return _cs.seed_from_history()


def purge_junk_contacts(dry_run: bool = True) -> dict:
    """См. store.contacts.junk_candidates + mark_flags."""
    from .store import contacts as _contacts
    invalid, tests = _contacts.junk_candidates()
    if not dry_run:
        for _phone in invalid:
            _contacts.mark_flags(_phone, is_invalid=True)
        for _phone in tests:
            _contacts.mark_flags(_phone, is_test=True)
    return {"invalid": invalid, "test": tests}
