"""Репозиторий списка «кому не дотянулись» — будущая отправка через SMS.

Единственное место, где знают про таблицу ``pending_sms``.

Зачем
----
Мессенджеры исчерпываются у части контактов: WABA отказал, а в Telegram и MAX
аккаунта нет (``PHONE_NOT_OCCUPIED``). Для таких людей остаётся SMS — платный
канал, поэтому он не входит ни в каскад, ни в rescue_queue, а требует явного
решения владельца. Этот модуль только НАБИРАЕТ факты, ничего не отправляет.

Строка = человек, а не попытка. На ``contact_id`` стоит UNIQUE, повторный прогон
не размножает записи: контакт, который сегодня не дотянули, не должен
превратиться в две строки после второго запуска.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from typing import Optional

from .connection import read_only, rows_to_dicts, transaction


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Вердикт «есть ли аккаунт» живёт в contact_channel_state. Отсутствие строки —
# не «нет», а «не проверяли»: именно это различение пригодится, когда владелец
# решит отправлять SMS (не платить за заведомо мёртвые номера).
_VERDICT_SQL = """
SELECT ct.id AS contact_id,
       COALESCE(ct.phone, '') AS phone,
       MAX(CASE WHEN s.channel IN ('waba', 'whatsapp') THEN s.verdict END) AS has_whatsapp,
       MAX(CASE WHEN s.channel = 'telegram' THEN s.verdict END) AS has_telegram,
       MAX(CASE WHEN s.channel = 'max' THEN s.verdict END) AS has_max,
       MAX(COALESCE(m.sent_at, m.created_at)) AS last_attempt_at,
       GROUP_CONCAT(DISTINCT m.channel) AS channels_tried
FROM contacts ct
JOIN messages m ON m.contact_id = ct.id
LEFT JOIN contact_channel_state s ON s.contact_id = ct.id
WHERE COALESCE(m.sent_at, m.created_at) >= ?
  AND NOT EXISTS (
        SELECT 1 FROM messages d
        WHERE d.contact_id = ct.id
          AND (d.delivered_at IS NOT NULL
               OR d.read_at IS NOT NULL
               OR d.answered_at IS NOT NULL
               OR d.delivery_state = 'delivered'))
GROUP BY ct.id
"""


def _cell(value: Optional[str]) -> str:
    """Вердикт в ту же форму, что у листа: yes / no / unknown."""
    if not value:
        return "unknown"
    if value in ("present", "delivered"):
        return "yes"
    if value in ("absent", "blocked"):
        return "no"
    return "unknown"


def _day_start_utc(day: Optional[str] = None) -> str:
    """Граница «сегодня» как UTC-метка времени.

    Тонкость, на которой легко споткнуться: ``sent_at`` пишется в UTC, а
    «сегодня» система считает в BULK_TIMEZONE (Europe/Moscow). Полночь МСК —
    это 21:00 предыдущих суток UTC, поэтому сравнение со строкой
    ``'2026-10-09T00:00:00'`` молча включало бы в отчёт лишние сутки.
    Здесь граница переводится в тот же timezone-aware UTC, в котором лежат
    ``sent_at``, и сравнение строк остаётся корректным.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from .. import config as _config

    if day is None:
        day = _config.now_tz().strftime("%Y-%m-%d")
    try:
        tz = ZoneInfo(_config.TIMEZONE_NAME)
    except Exception:
        tz = timezone.utc
    midnight = _dt.strptime(day, "%Y-%m-%d").replace(tzinfo=tz)
    return midnight.astimezone(timezone.utc).isoformat()


def mark(contact_id: int, phone: str, reason: str = "") -> bool:
    """Добавляет контакт в список. Повторный вызов — обновление, не дубль.

    Возвращает True, если строка появилась впервые.
    """
    with transaction() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pending_sms "
            "  (contact_id, phone, reason, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (int(contact_id), phone, reason or None),
        )
        first_time = cur.rowcount > 0
        if not first_time:
            conn.execute(
                "UPDATE pending_sms SET reason = COALESCE(?, reason), "
                "updated_at = datetime('now') WHERE contact_id = ?",
                (reason or None, int(contact_id)),
            )
    return first_time


def collect_since(since_iso: str) -> int:
    """Набирает в список тех, кого трогали с указанного момента, но кто НЕ
    получил подтверждённой доставки ни по одному каналу.

    Ключевое условие — ``NOT EXISTS`` по подтверждённой доставке. Человек с
    отказом WABA, но без вердиктов по мессенджерам в список попадает: до него
    не дошли, и SMS остаётся единственным оставшимся каналом.

    Вердикты и список каналов дописываются в ту же строку, чтобы по выгрузке
    было видно, куда уже пробовали. Повторный вызов обновляет существующие
    строки и не создаёт новых.
    """
    added = 0
    with read_only() as conn:
        rows = conn.execute(_VERDICT_SQL, (since_iso,)).fetchall()

    with transaction() as conn:
        for r in rows:
            existing = conn.execute(
                "SELECT id FROM pending_sms WHERE contact_id = ?",
                (r["contact_id"],),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE pending_sms SET "
                    "  channels_tried = ?, has_whatsapp = ?, has_telegram = ?, "
                    "  has_max = ?, last_attempt_at = ?, updated_at = ? "
                    "WHERE contact_id = ?",
                    (r["channels_tried"] or "",
                     _cell(r["has_whatsapp"]), _cell(r["has_telegram"]),
                     _cell(r["has_max"]), r["last_attempt_at"],
                     _now(), r["contact_id"]),
                )
                continue
            conn.execute(
                "INSERT INTO pending_sms "
                "  (contact_id, phone, reason, channels_tried, has_whatsapp, "
                "   has_telegram, has_max, last_attempt_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                (r["contact_id"], r["phone"], "no_confirmed_delivery",
                 r["channels_tried"] or "",
                 _cell(r["has_whatsapp"]), _cell(r["has_telegram"]),
                 _cell(r["has_max"]), r["last_attempt_at"]),
            )
            added += 1
    return added


def collect_today() -> int:
    """Набирает за сегодня (по BULK_TIMEZONE). Точка входа для конца прогона."""
    return collect_since(_day_start_utc())


def pending(limit: Optional[int] = None) -> list[dict]:
    with read_only() as conn:
        sql = ("SELECT * FROM pending_sms WHERE status = 'pending' "
               "ORDER BY has_telegram, has_max, phone")
        params: tuple = ()
        if limit:
            sql += " LIMIT ?"
            params = (int(limit),)
        return rows_to_dicts(conn.execute(sql, params).fetchall())


def set_status(contact_id: int, status: str) -> None:
    """Ставит статус. Значение вне словаря отвергается границей (enums)."""
    allowed = ("pending", "sent", "skipped")
    if status not in allowed:
        raise ValueError(f"status должен быть одним из {allowed}, получено {status!r}")
    with transaction() as conn:
        conn.execute(
            "UPDATE pending_sms SET status = ?, updated_at = ? WHERE contact_id = ?",
            (status, _now(), int(contact_id)),
        )


def summary() -> dict:
    """Сводка по списку — для preflight и отчёта."""
    with read_only() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) n FROM pending_sms GROUP BY status"
        ).fetchall()
        by_channel = conn.execute(
            "SELECT has_telegram, has_max, COUNT(*) n FROM pending_sms "
            "WHERE status = 'pending' GROUP BY has_telegram, has_max"
        ).fetchall()
    return {
        "by_status": {r["status"]: r["n"] for r in rows},
        "pending_tg_no_max": sum(
            r["n"] for r in by_channel
            if r["has_telegram"] == "yes" and r["has_max"] == "no"
        ),
        "pending_no_messengers": sum(
            r["n"] for r in by_channel
            if r["has_telegram"] == "no" and r["has_max"] == "no"
        ),
    }


def export_csv(path: str, limit: Optional[int] = None) -> int:
    """Выгрузка в CSV. Сколько строк записано — возвращает числом."""
    rows = pending(limit)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "Phone", "Name", "Reason", "Channels Tried",
            "Has WhatsApp", "Has Telegram", "Has MAX", "Last Attempt",
        ])
        for r in rows:
            writer.writerow([
                r.get("phone") or "", r.get("reason") or "",
                r.get("channels_tried") or "", r.get("has_whatsapp") or "",
                r.get("has_telegram") or "", r.get("has_max") or "",
                r.get("last_attempt_at") or "",
            ])
    return len(rows)


__all__ = [
    "mark",
    "collect_since",
    "collect_today",
    "pending",
    "set_status",
    "summary",
    "export_csv",
]