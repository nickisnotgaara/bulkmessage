"""Репозиторий состояний «контакт × канал».

Хранит ответ на вопрос «что мы знаем об этом человеке в этом канале».
До этой таблицы ответ собирался ручным JOIN-ом по ``messages``,
``rescue_queue`` и ``broadcast_state.json`` — и три источника расходились.

Вердикты
--------
``present``   аккаунт подтверждён пробой (и сообщение ушло);
``absent``    аккаунта нет, квота НЕ потрачена;
``delivered`` доставка подтверждена вебхуком;
``rejected``  явный отказ канала;
``blocked``   жалоба / спам-отказ;
``unknown``   данных нет.

Откат берёт ТОЛЬКО ``present`` и ``delivered``: отправка заведомо
недоставленного контакта — это чистая трата личного лимита.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .connection import count, read_only, transaction
from .enums import Verdict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_verdict(
    contact_id: int,
    channel: str,
    verdict: str,
    *,
    source: Optional[str] = None,
    chat_id: Optional[str] = None,
    probed: bool = False,
    sent: bool = False,
    probe_at: Optional[str] = None,
    send_at: Optional[str] = None,
    last_error: Optional[str] = None,
) -> None:
    """Записывает вердикт по паре (контакт, канал).

    Метки времени двигаются только когда соответствующее событие правда
    произошло: ``probed=True`` — проба была, ``sent=True`` — ушло сообщение.
    Иначе «проверили час назад» и «отправили час назад» склеились бы в
    одном поле, и TTL перестал бы значить что-то осмысленное.

    ``probe_at`` / ``send_at`` позволяют задать МОМЕНТ события явно (например
    при переносе истории), а не «сейчас». Без этого миграция старых записей
    проставляла бы им свежие метки, и протухшая проба выглядела бы только что
    сделанной.
    """
    value = Verdict.parse(verdict)
    if value is None:
        raise ValueError(f"Пустой вердикт для contact={contact_id} channel={channel}")
    probe_at = probe_at or (_now() if probed else None)
    send_at = send_at or (_now() if sent else None)
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO contact_channel_state (
                contact_id, channel, verdict, verdict_source, chat_id,
                last_probe_at, last_send_at, last_error, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(contact_id, channel) DO UPDATE SET
                verdict        = excluded.verdict,
                verdict_source = COALESCE(excluded.verdict_source,
                                          contact_channel_state.verdict_source),
                chat_id        = COALESCE(excluded.chat_id,
                                          contact_channel_state.chat_id),
                last_probe_at  = COALESCE(excluded.last_probe_at,
                                          contact_channel_state.last_probe_at),
                last_send_at   = COALESCE(excluded.last_send_at,
                                          contact_channel_state.last_send_at),
                last_error     = excluded.last_error,
                updated_at     = excluded.updated_at
            """,
            (contact_id, channel, str(value), source, chat_id,
             probe_at, send_at, last_error, _now()),
        )


def get(contact_id: int, channel: str) -> Optional[dict]:
    with read_only() as conn:
        row = conn.execute(
            "SELECT * FROM contact_channel_state WHERE contact_id = ? AND channel = ?",
            (contact_id, channel),
        ).fetchone()
        return dict(row) if row else None


def for_phone(phone: str) -> dict[str, dict]:
    """Все вердикты по номеру: {канал: строка}."""
    with read_only() as conn:
        rows = conn.execute(
            "SELECT s.* FROM contact_channel_state s "
            "JOIN contacts ct ON ct.id = s.contact_id WHERE ct.phone = ?",
            (phone,),
        ).fetchall()
    return {r["channel"]: dict(r) for r in rows}


def needs_probe(contact_id: int, channel: str, ttl_hours: int) -> bool:
    """Нужно ли пробивать (контакт, канал) прямо сейчас.

    Повторно не трогаем то, что уже известно в пределах TTL. Исключение —
    ``present``: он протухает вместе с TTL, потому что номер могли удалить,
    и тогда вердикт надо подтвердить заново.
    """
    row = get(contact_id, channel)
    if row is None or row["verdict"] == Verdict.UNKNOWN.value:
        return True
    stamp = row["last_probe_at"] or row["updated_at"]
    if not stamp:
        return True
    try:
        probed = datetime.fromisoformat(stamp)
        if probed.tzinfo is None:
            probed = probed.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return True
    return (datetime.now(timezone.utc) - probed) >= timedelta(hours=ttl_hours)


def actionable(channels: Optional[list[str]] = None) -> list[dict]:
    """Контакты для отката: аккаунт подтверждён.

    Возвращает только ``present``/``delivered``. ``absent``/``blocked``/
    ``unknown`` в откат не идут: отправка заведомо не дойдёт.
    """
    channels = channels or ["telegram", "max"]
    if not channels:
        return []
    ph = ",".join("?" for _ in channels)
    sql = (
        "SELECT s.contact_id, s.channel, s.chat_id, s.last_send_at, "
        "       ct.phone, ct.name, ct.category "
        "FROM contact_channel_state s "
        "JOIN contacts ct ON ct.id = s.contact_id "
        f"WHERE s.verdict IN ('present','delivered') AND s.channel IN ({ph}) "
        "  AND COALESCE(ct.is_test,0) = 0 AND COALESCE(ct.is_invalid,0) = 0 "
        "ORDER BY s.updated_at ASC"
    )
    with read_only() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(channels)).fetchall()]


def filter_actionable(candidates: list[dict], channel: str) -> list[dict]:
    """Оставляет из ``candidates`` только подтверждённые для этого канала.

    Работает по списку телефонов: выборка идёт из очереди отката, а вердикт
    лежит в другой таблице, и поштучный JOIN на каждый контакт здесь дороже
    одного запроса.
    """
    if not candidates:
        return []
    phones = [c["phone"] for c in candidates if c.get("phone")]
    if not phones:
        return []
    ph = ",".join("?" for _ in phones)
    sql = (
        "SELECT ct.phone FROM contact_channel_state s "
        "JOIN contacts ct ON ct.id = s.contact_id "
        f"WHERE s.channel = ? AND s.verdict IN ('present','delivered') "
        f"  AND ct.phone IN ({ph})"
    )
    with read_only() as conn:
        ok = {
            r["phone"]
            for r in conn.execute(sql, (channel, *phones)).fetchall()
        }
    return [c for c in candidates if c.get("phone") in ok]


def verdict_counts() -> list[dict]:
    """Сводка «канал × вердикт» для отчёта и preflight."""
    with read_only() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT channel, verdict, COUNT(*) AS n "
                "FROM contact_channel_state GROUP BY channel, verdict "
                "ORDER BY channel, n DESC"
            ).fetchall()
        ]


def total() -> int:
    with read_only() as conn:
        return count(conn, "SELECT COUNT(*) FROM contact_channel_state")


def seed_from_history() -> dict[str, int]:
    """Заполняет вердикты из уже накопленной истории сообщений.

    Бесплатно: ни одного сетевого запроса, всё нужное уже лежит в
    ``messages.last_error`` / ``delivery_state``. Идемпотентно.

    Приоритет задан порядком CASE — сначала жалоба, потом явный отказ
    «нет аккаунта», потом подтверждённая доставка, и лишь в конце «отправка
    вообще была». Первая сработавшая ветка выигрывает. Наивный вариант
    «INSERT по одному правилу» здесь неверен: следующее правило
    перезаписывало бы предыдущее, и ``delivered`` вырождался бы в ``present``,
    потому что у доставленного сообщения тоже есть message_id.
    """
    verdict_case = """
        CASE
            WHEN lower(COALESCE(m.last_error,'')) LIKE '%spam%'
                THEN 'blocked'
            WHEN lower(COALESCE(m.last_error,'')) LIKE '%phone_not_occupied%'
              OR lower(COALESCE(m.last_error,'')) LIKE '%bad_contact%'
              OR m.delivery_state = 'rejected'
                THEN 'absent'
            WHEN m.delivery_state = 'delivered'
                THEN 'delivered'
            WHEN m.message_id IS NOT NULL AND m.sent_at IS NOT NULL
                THEN 'present'
            ELSE NULL
        END
    """
    source_case = """
        CASE
            WHEN lower(COALESCE(m.last_error,'')) LIKE '%spam%' THEN 'history'
            WHEN m.delivery_state = 'delivered' THEN 'webhook_echo'
            ELSE 'history'
        END
    """
    with transaction() as conn:
        conn.execute(
            f"""
            INSERT INTO contact_channel_state (
                contact_id, channel, verdict, verdict_source,
                last_send_at, updated_at
            )
            SELECT m.contact_id, m.channel, {verdict_case}, {source_case},
                   MAX(m.sent_at), datetime('now')
            FROM messages m
            WHERE {verdict_case.strip()} IS NOT NULL
            GROUP BY m.contact_id, m.channel
            ON CONFLICT(contact_id, channel) DO UPDATE SET
                verdict        = excluded.verdict,
                verdict_source = excluded.verdict_source,
                last_send_at   = MAX(
                    COALESCE(contact_channel_state.last_send_at,''),
                    COALESCE(excluded.last_send_at,'')),
                updated_at     = excluded.updated_at
            """
        )
    return {r["verdict"]: r["n"] for r in verdict_counts()}


__all__ = [
    "set_verdict",
    "get",
    "for_phone",
    "needs_probe",
    "actionable",
    "filter_actionable",
    "verdict_counts",
    "total",
    "seed_from_history",
]