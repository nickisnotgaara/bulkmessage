"""Очередь отката: кому и через какой канал ещё можно написать.

Таблица исторически называется ``rescue_queue``. Название описывает ПРОЦЕСС
(«спасение» контакта), а суть — «контакт ждёт следующего канала», поэтому в
коде она называется ``queue``, а имя таблицы оставлено прежним: переименование
таблицы в работающей базе не даёт ничего, кроме риска.

Одна строка — одна НЕЗАВЕРШЁННАЯ ПРОВЫЛЕНОЧНАЯ ЦЕПОЧКА для контакта.
Повторных строк на контакт быть не должно: откат одной жертвы в два канала
разом означал бы двойное сообщение одному человеку.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .connection import read_only, transaction
from .enums import RescueStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue(
    phone: str,
    *,
    name: Optional[str] = None,
    category: Optional[str] = None,
    original_channel: Optional[str] = None,
    reason: Optional[str] = None,
) -> Optional[int]:
    """Ставит контакт в очередь отката. Повторная постановка игнорируется.

    Возвращает None, если контакт уже в очереди: незавершённая цепочка по
    контакту должна быть одна, иначе откат уйдёт в два канала сразу и человек
    получит два сообщения.
    """
    with transaction() as conn:
        existing = conn.execute(
            "SELECT id FROM rescue_queue WHERE phone = ? "
            "AND status IN ('pending','deferred') LIMIT 1",
            (phone,),
        ).fetchone()
        if existing:
            return None
        cur = conn.execute(
            "INSERT INTO rescue_queue (phone, name, category, original_channel, "
            "                              reason, status, first_seen, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (phone, name, category, original_channel, reason,
             _now(), _now(), _now()),
        )
        return int(cur.lastrowid)


def pending(
    *,
    limit: Optional[int] = None,
    cooldown_seconds: int = 0,
    exclude_tried: Optional[str] = None,
    phone: Optional[str] = None,
) -> list[dict]:
    """Кандидаты на откат.

    ``cooldown_seconds`` отсекает контакты, которым недавно уже слали в этот
    канал (защита от дублей после неудачной попытки).

    ``exclude_tried`` убирает каналы, по которым контакт уже получал
    СОСТОЯВШУЮСЯ отправку: в одном канале человеку пишут один раз.
    """
    sql = [
        "SELECT r.*, ct.name AS contact_name, ct.category AS contact_category "
        "FROM rescue_queue r LEFT JOIN contacts ct ON ct.phone = r.phone "
        "WHERE r.status IN ('pending','deferred')",
    ]
    params: list = []
    if phone:
        sql.append("AND r.phone = ?")
        params.append(phone)
    if cooldown_seconds:
        sql.append(
            "AND NOT EXISTS ("
            "  SELECT 1 FROM contact_cooldowns cc "
            "  WHERE cc.phone = r.phone AND cc.channel = ? "
            "    AND cc.until > datetime('now'))"
        )
        params.append(exclude_tried or "")
    sql.append("ORDER BY r.first_seen ASC, r.id ASC")
    if limit:
        sql.append("LIMIT ?")
        params.append(limit)
    with read_only() as conn:
        return [
            dict(r)
            for r in conn.execute(" ".join(sql), tuple(params)).fetchall()
        ]


def pending_in_channel(channel: str, tried_phones: set[str] | None = None) -> list[dict]:
    """Откат в конкретный канал: очередь минус уже отправленные в него.

    Тот же набор, что у :func:`pending`, но отфильтрованный по истории
    успешных отправок — это основной сценарий каскада WABA → TG → MAX.
    """
    rows = pending(cooldown_seconds=0, limit=None)
    return [r for r in rows if not (tried_phones and r["phone"] in tried_phones)]


def mark_attempt(queue_id: int, error: Optional[str] = None) -> None:
    """Фиксирует попытку отката. Третья подряд → failed."""
    with transaction() as conn:
        conn.execute(
            "UPDATE rescue_queue SET "
            "  attempts = attempts + 1, "
            "  attempts_total = COALESCE(attempts_total,0) + 1, "
            "  last_attempt_at = ?, last_outcome = ?, updated_at = ? "
            "WHERE id = ?",
            (_now(), error, _now(), queue_id),
        )
        conn.execute(
            "UPDATE rescue_queue SET status = 'failed' "
            "WHERE id = ? AND attempts >= 3",
            (queue_id,),
        )


def mark_rescued(queue_id: int, channel: str, message_id: Optional[str] = None) -> None:
    """Контакт доставлен в следующем канале — цепочка завершена."""
    with transaction() as conn:
        conn.execute(
            "UPDATE rescue_queue SET status='rescued', rescued_channel=?, "
            "  rescued_message_id=?, last_attempt_at=?, updated_at=? "
            "WHERE id=?",
            (channel, message_id, _now(), _now(), queue_id),
        )


def release(queue_id: int, *, until: Optional[str] = None) -> None:
    """Откладывает до ``until`` (cooldown). Не снимает задачу, а ждёт."""
    with transaction() as conn:
        conn.execute(
            "UPDATE rescue_queue SET status='deferred', next_eligible_at=?, "
            "  updated_at=? WHERE id=?",
            (until, _now(), queue_id),
        )


def clear_rescued_channel(queue_id: int) -> None:
    """Сбрасывает ``rescued_channel``.

    Вызывается, когда эхо-отказ показал, что «спасение» на самом деле не
    дошло: контакт должен снова попасть в очередь и уйти в следующий канал.
    """
    with transaction() as conn:
        conn.execute(
            "UPDATE rescue_queue SET rescued_channel=NULL, rescued_message_id=NULL, "
            "  status='pending', updated_at=? WHERE id=?",
            (_now(), queue_id),
        )


def cancel(phone: str) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE rescue_queue SET status='cancelled', updated_at=? WHERE phone=?",
            (_now(), phone),
        )


def get_by_phone(phone: str) -> Optional[dict]:
    with read_only() as conn:
        row = conn.execute(
            "SELECT * FROM rescue_queue WHERE phone = ? "
            "ORDER BY (status IN ('pending','deferred')) DESC, id DESC LIMIT 1",
            (phone,),
        ).fetchone()
        return dict(row) if row else None


def status_counts() -> dict[str, int]:
    with read_only() as conn:
        return {
            r["status"]: int(r["n"])
            for r in conn.execute(
                "SELECT status, COUNT(*) n FROM rescue_queue GROUP BY status"
            ).fetchall()
        }


def set_cooldown(phone: str, channel: str, until_iso: str, reason: str = "") -> None:
    """Ставит контакт на паузу в конкретном канале (anti-flood)."""
    with transaction() as conn:
        conn.execute(
            "INSERT INTO contact_cooldowns (phone, channel, until, reason, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(phone, channel) DO UPDATE SET "
            "  until=excluded.until, reason=excluded.reason, "
            "  updated_at=excluded.updated_at",
            (phone, channel, until_iso, reason, _now()),
        )


__all__ = [
    "enqueue",
    "pending",
    "pending_in_channel",
    "mark_attempt",
    "mark_rescued",
    "release",
    "clear_rescued_channel",
    "cancel",
    "get_by_phone",
    "status_counts",
    "set_cooldown",
]