"""Дневные квоты и остановки каналов — единственный источник истины.

Почему квоты переехали в SQLite, а не остались в ``broadcast_state.json``
----------------------------------------------------------------------------
Квоты — это числа, на которых стоит защита личных аккаунтов: превысил лимит
Telegram = риск бана. Хранить такое число в JSON-файле, который перезаписывается
целиком при каждом событии, нельзя: любой сбой посередине записи (Ctrl+C,
перезагрузка, два процесса) оставлял состояние в неопределённом состоянии, и
реально потраченная квота переставала совпадать с учтённой.

В SQLite это решается транзакцией: инкремент и запись вердикта — один
атомарный шаг.

Совместимость
-------------
``broadcast_state.json`` остаётся как ЗЕРКАЛО для ``state.py``: он переписывается
из базы, но больше не является источником истины. Старый код продолжает работать.

Схема
-----
Таблица ``channel_daily`` (создаётся миграцией): одна строка на (дата, канал).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import config
from .connection import count, read_only, transaction
from .enums import Channel


def _today() -> str:
    """Сегодняшняя дата в TZ проекта (Europe/Moscow по умолчанию)."""
    return config.now_tz().strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────── квоты ────────────────────────────────────


def _ensure_row(channel: str, day: Optional[str] = None) -> None:
    day = day or _today()
    with transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO channel_daily (date, channel) VALUES (?, ?)",
            (day, str(channel)),
        )


def sent_today(channel: str, day: Optional[str] = None) -> int:
    """Сколько успешных отправок учтено в канале за день.

    Читает СЧЁТЧИК ИЗ ``channel_daily`` — тот же, который двигает
    :func:`record_sent`. Раньше значение выводилось заново из ``messages``,
    и получались два независимых счётчика одного и того же: инкремент
    записывался в одно место, а читалось из другого, поэтому расхождение
    было вопросом времени.

    Значение всегда равно числу строк с ``message_id IS NOT NULL`` за день —
    это подтверждается тестом сверки ``quota_sent_matches_messages``.
    """
    day = day or _today()
    with read_only() as conn:
        row = conn.execute(
            "SELECT sent FROM channel_daily WHERE date=? AND channel=?",
            (day, str(channel)),
        ).fetchone()
    return int(row["sent"]) if row else 0


def record_sent(channel: str, *, day: Optional[str] = None) -> int:
    """Учитывает одну успешную отправку. Возвращает новый счётчик."""
    day = day or _today()
    with transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO channel_daily (date, channel, sent) VALUES (?, ?, 0)",
            (day, str(channel)),
        )
        conn.execute(
            "UPDATE channel_daily SET sent = sent + 1, updated_at = ? "
            "WHERE date = ? AND channel = ?",
            (_now(), day, str(channel)),
        )
        row = conn.execute(
            "SELECT sent FROM channel_daily WHERE date=? AND channel=?",
            (day, str(channel)),
        ).fetchone()
        return int(row["sent"]) if row else 0


def note_delivery(channel: str, state: str, day: Optional[str] = None) -> None:
    """Учитывает подтверждение доставки из вебхука."""
    day = day or _today()
    with transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO channel_daily (date, channel) VALUES (?, ?)",
            (day, str(channel)),
        )
        col = {
            "delivered": "delivered",
            "rejected": "rejected",
            "unknown": "unknown",
        }.get(state)
        if col:
            conn.execute(
                f"UPDATE channel_daily SET {col} = {col} + 1, updated_at = ? "
                "WHERE date=? AND channel=?",
                (_now(), day, str(channel)),
            )


def snapshot(day: Optional[str] = None) -> dict[str, dict]:
    """Срез по всем каналам за день: {канал: счётчики}."""
    day = day or _today()
    with read_only() as conn:
        rows = conn.execute(
            "SELECT * FROM channel_daily WHERE date = ?", (day,)
        ).fetchall()
    return {r["channel"]: dict(r) for r in rows}


# ────────────────────────── остановка канала ────────────────────────────────
# Complaint-halt живёт в БД, потому что это тоже защита личного аккаунта:
# решение «остановить канал» не должно теряться при перезаписи JSON.


def halt_channel(channel: str, hours: float, reason: str = "") -> Optional[str]:
    """Останавливает канал на N часов. Возвращает время окончания."""
    until = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + hours * 3600,
        tz=timezone.utc,
    ).isoformat()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO channel_daily (date, channel, halted_until, halt_reason, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(date, channel) DO UPDATE SET "
            "  halted_until=excluded.halted_until, halt_reason=excluded.halt_reason, "
            "  updated_at=excluded.updated_at",
            (_today(), str(channel), until, reason, _now()),
        )
    return until


def is_halted(channel: str) -> bool:
    """Канал остановлен прямо сейчас?"""
    with read_only() as conn:
        row = conn.execute(
            "SELECT halted_until FROM channel_daily "
            "WHERE date=? AND channel=?",
            (_today(), str(channel)),
        ).fetchone()
    if not row or not row["halted_until"]:
        return False
    try:
        until = datetime.fromisoformat(row["halted_until"])
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return False
    return until > datetime.now(timezone.utc)


def halted_until(channel: str) -> Optional[str]:
    with read_only() as conn:
        row = conn.execute(
            "SELECT halted_until FROM channel_daily WHERE date=? AND channel=?",
            (_today(), str(channel)),
        ).fetchone()
    return row["halted_until"] if row else None


def clear_halt(channel: str) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE channel_daily SET halted_until=NULL, halt_reason=NULL, updated_at=? "
            "WHERE date=? AND channel=?",
            (_now(), _today(), str(channel)),
        )


# ───────────────────────────── жалобы (halt) ────────────────────────────────


def record_complaint(
    contact_id: Optional[int],
    channel: str,
    kind: str,
    *,
    phone: Optional[str] = None,
    message_id: Optional[str] = None,
) -> None:
    """Фиксирует жалобу — основа для автоматической остановки канала.

    Пишет в реальные колонки таблицы. Раньше здесь вставлялись
    ``contact_id / kind / raw_json``, которых в ``complaint_events`` никогда
    не было (``phone / complaint_kind / message_id``), поэтому каждый вызов
    падал с ``OperationalError: no such column``. То есть весь путь
    complaint-halt — тот самый, что защищает личный номер от бана — был
    нерабочим и падал бы в момент жалобы, когда он нужнее всего.
    """
    if phone is None and contact_id is not None:
        with read_only() as conn:
            row = conn.execute(
                "SELECT phone FROM contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            phone = row["phone"] if row else None
    with transaction() as conn:
        conn.execute(
            "INSERT INTO complaint_events "
            "  (channel, phone, complaint_kind, message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(channel), phone, str(kind), message_id, _now()),
        )


def complaints_last_24h(channel: str) -> int:
    """Жалоб за последние сутки — основа complaint-halt."""
    with read_only() as conn:
        return count(
            conn,
            "SELECT COUNT(*) FROM complaint_events "
            "WHERE channel = ? AND created_at > datetime('now', '-24 hours')",
            (str(channel),),
        )


# ─────────────────────── зеркало для state.py ──────────────────────────────


def mirror_to_state(state: dict) -> dict:
    """Перезаписывает счётчики в state-структуре значениями из БД.

    Нужно старому коду (``state.py``), который читает квоты из state.json.
    Направление одно: база → JSON. Обратного потока нет намеренно: иначе мы
    вернули бы два источника истины, от которых ушли.
    """
    snap = snapshot()
    sent = state.setdefault("sent_today", {})
    for ch, values in snap.items():
        sent[ch] = int(values.get("sent") or 0)
    halted = state.setdefault("halted_channels", {})
    for ch, values in snap.items():
        until = values.get("halted_until")
        if until:
            halted[ch] = {
                "until": until,
                "reason": values.get("halt_reason") or "",
            }
        else:
            halted.pop(ch, None)
    return state


def push_mirror_to_disk(state_path: Optional[str] = None) -> None:
    """Пишет зеркало на диск (атомарно: временный файл + замена)."""
    path = Path(state_path or config.STATE_PATH)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.loads(Path(state_path or config.STATE_PATH).read_text(
        encoding="utf-8"
    )) if path.exists() else {}
    payload = mirror_to_state(payload)
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)


__all__ = [
    "sent_today",
    "record_sent",
    "note_delivery",
    "snapshot",
    "halt_channel",
    "is_halted",
    "halted_until",
    "clear_halt",
    "record_complaint",
    "complaints_last_24h",
    "mirror_to_state",
    "push_mirror_to_disk",
]