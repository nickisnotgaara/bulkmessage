"""Репозиторий сообщений — попыток отправки.

Ключевое правило, которое держит всю отчётность
----------------------------------------------
``message_id IS NULL`` означает: **отправки не было**. Так пишутся отказы
каскада, ошибки транспорта и синхронные 400. Такой строке нельзя попасть в
счёт «мы написали человеку».

Именно это правило раньше нарушалось: число «803 отправленных» в Google
Sheets считало попытки и симуляции, а писали 362 человека.

Поэтому здесь два разных метода, и путать их нельзя:

* :func:`record_attempt` — попытка (может не дойти до адресата);
* :func:`record_send`    — состоялась отправка, у Wazzup есть message_id.

Два разных вопроса, два разных ответа
----------------------------------------
В проекте различаются вещи, которые оба называются «мы писали человеку», и
отвечают они по-разному:

* **«Скольким людям мы писали» — вопрос отчётности.** Здесь критерий жёсткий:
  ``message_id IS NOT NULL``. Это единственное, что подтверждает принятие
  запроса транспортом. Считать попытки здесь нельзя: так таблица показывала
  803 строки против 359 реальных адресатов. Отвечают :func:`contacted_on`,
  :func:`delivered_on`, :func:`funnel`.

* **«Кого нельзя трогать повторно» — вопрос безопасности.** Здесь критерий
  НАМЕРЕННО ШИРЕ: ``status IN ('sent','delivered','read','answered')``.
  Разница — 98 контактов, у которых статус успешный, но идентификатор потерян
  (старый код выставлял статус по HTTP-ответу и не писал время).
  Для отчётности эти строки сомнительны; для повторной отправки — нет: человек
  мог получить сообщение, и второй раз получит его уже как жалобу, а жалоба
  в личном канале означает перманентный бан номера.

Владелец проекта выбрал осторожность: исключаем всех 98. Значит критерий
отбора — статус, а не идентификатор. Отвечает :func:`contacted_phones`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .connection import count, read_only, scalar, transaction
from .enums import Channel, DeliveryState, MessageStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────── правило «писали этому контакту» ─────────────────────
#
# Единственный источник истины для отбора в пул. Список живёт здесь, а
# contacts.py подставляет предикат в свой запрос, чтобы не расходились копии.
# Проверено на боевых данных: 443 контакта против 359 по message_id.

SENT_STATUSES = (
    MessageStatus.SENT.value,
    MessageStatus.DELIVERED.value,
    MessageStatus.READ.value,
    MessageStatus.ANSWERED.value,
)

# Предикат для JOIN-а в других репозиториях. Псевдоним `c` — колонка контакта.
CONTACTED_PREDICATE = (
    "EXISTS (SELECT 1 FROM messages m WHERE m.contact_id = c.id "
    "AND m.status IN (?, ?, ?, ?))"
)


def contacted_contact_ids() -> set[int]:
    """id контактов, которым уже отправляли хотя бы раз.

    Критерий СТАТУСА, а не message_id: см. докстринг модуля. Шире отчётного
    счётчика на 98 контактов — это осознанный запас против повторной отправки.
    """
    with read_only() as conn:
        return {
            int(r["contact_id"])
            for r in conn.execute(
                "SELECT DISTINCT contact_id FROM messages "
                "WHERE status IN (?, ?, ?, ?)",
                SENT_STATUSES,
            ).fetchall()
        }


def contacted_phones() -> set[str]:
    """Телефоны контактов, которым уже писали — граница боевого пула.

    Контакт попадает сюда по ЛЮБОМУ каналу: написали в Telegram — значит в WABA
    и MAX повторно не пишем (решение владельца, вечный запрет).
    """
    with read_only() as conn:
        return {
            r["phone"]
            for r in conn.execute(
                "SELECT DISTINCT ct.phone FROM messages m "
                "JOIN contacts ct ON ct.id = m.contact_id "
                "WHERE m.status IN (?, ?, ?, ?)",
                SENT_STATUSES,
            ).fetchall()
        }


def record_attempt(
    contact_id: int,
    channel: str,
    *,
    message_text: Optional[str] = None,
    message_id: Optional[str] = None,
    status: str = MessageStatus.FAILED,
    last_error: Optional[str] = None,
) -> int:
    """Фиксирует ПОПЫТКУ отправки.

    Если ``message_id`` не передан, отправки фактически не было: строка
    попадёт в историю для разбора, но не в отчёт «кому мы написали».
    """
    sent_at = _now() if message_id else None
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO messages (contact_id, channel, message_text, message_id, "
            "                          status, delivery_state, sent_at, last_error, "
            "                          created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'unknown', ?, ?, ?, ?)",
            (contact_id, str(channel), message_text, message_id, str(status),
             sent_at, last_error, _now(), _now()),
        )
        return int(cur.lastrowid)


def record_send(
    contact_id: int,
    channel: str,
    message_text: str,
    message_id: str,
    *,
    status: str = MessageStatus.SENT,
) -> int:
    """Фиксирует состоявшуюся отправку.

    Требуется непустой ``message_id`` — это идентификатор от Wazzup, и его
    наличие доказывает, что запрос принят транспортом.
    """
    if not message_id:
        raise ValueError(
            "record_send требует message_id: без него отправка не состоялась, "
            "и строка не должна попасть в счёт «мы написали»."
        )
    return record_attempt(
        contact_id, channel, message_text=message_text,
        message_id=message_id, status=status,
    )


def set_delivery_state(message_pk: int, state: str, *, at_field: Optional[str] = None) -> None:
    """Проставляет трёхзначное состояние доставки по событию вебхука."""
    valid = DeliveryState.parse(state)
    if valid is None:
        raise ValueError(f"Неизвестный delivery_state: {state!r}")
    stamp = _now()
    extra = f", {at_field} = ?" if at_field else ""
    params = (str(valid), stamp, message_pk) + ((stamp,) if at_field else ())
    with transaction() as conn:
        conn.execute(
            f"UPDATE messages SET delivery_state = ?, updated_at = ?{extra} WHERE id = ?",
            params,
        )


def retire_unconfirmed(message_pk: int, state: str) -> None:
    """Сторож: снимает молчание с разбора и пишет состояние ОДНОЙ транзакцией.

    Вывод из оборота делает ``swept_at`` — выборка ``find_unconfirmed`` отсекает
    ``swept_at IS NULL``, поэтому одна и та же тишина не разбирается повторно.
    Состояние доставки при этом пишется рядом, в том же шаге.

    Почему одна транзакция, а не два вызова подряд
    ----------------------------------------------
    Раньше сторож звал ``mark_swept()`` и только потом ``set_delivery_state()``.
    Второй вызов падал на CHECK-ограничении (см. ниже), и получалось ровно
    худшее состояние: строка уже выведена из оборота, состояние не записано,
    а в логе остаётся только ``warning``. Данные молча расходятся с тем, что
    сторож считает обработанным.

    Почему ``state`` нельзя писать произвольной строкой
    --------------------------------------------------
    ``delivery_state`` — трёхзначный словарь ``DeliveryState``
    (``unknown`` / ``delivered`` / ``rejected``) с CHECK в БД. Значение
    ``'exhausted'`` в него не входит, и CHECK его отвергает. Это не
    формальность: миграция 2 (``_m0002_clean_delivery_state``) специально
    переписала 56 таких строк в ``'unknown'``, потому что «контакт исчерпал
    попытки» — характеристика КОНТАКТА, а не состояние ДОСТАВКИ сообщения.
    Сообщение, которому так и не пришло подтверждение, честно остаётся
    ``unknown``.

    Статус (``status``) этой функцией НЕ трогается намеренно: в нём
    ``'exhausted'`` допустим (это словарь ``MessageStatus``), но он не входит
    в ``SENT_STATUSES``, а значит перевод ``'sent'`` → ``'exhausted'`` вернул
    бы контакт в отборной пул. Для человека, которому уже писали, это ровно
    тот повтор, из-за которого горит личный номер.
    """
    valid = DeliveryState.parse(state)
    if valid is None:
        raise ValueError(f"Неизвестный delivery_state: {state!r}")
    stamp = _now()
    with transaction() as conn:
        conn.execute(
            "UPDATE messages SET swept_at = ?, delivery_state = ?, updated_at = ? "
            "WHERE id = ? AND swept_at IS NULL",
            (stamp, str(valid), stamp, message_pk),
        )


def get(message_pk: int) -> Optional[dict]:
    with read_only() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_pk,)
        ).fetchone()
        return dict(row) if row else None


def by_wazzup_id(message_id: str) -> Optional[dict]:
    with read_only() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE message_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        return dict(row) if row else None


def last_sent(contact_id: int, channel: str) -> Optional[dict]:
    """Последняя РЕАЛЬНО отправленная строка по (контакт, канал)."""
    with read_only() as conn:
        row = conn.execute(
            "SELECT * FROM messages "
            "WHERE contact_id = ? AND channel = ? "
            "  AND message_id IS NOT NULL AND sent_at IS NOT NULL "
            "ORDER BY sent_at DESC, id DESC LIMIT 1",
            (contact_id, str(channel)),
        ).fetchone()
        return dict(row) if row else None


# ──────────────────────────── отчётность ───────────────────────────────────
# Каждая функция возвращает число, воспроизводимое её же логикой. В отчёте
# SQL печатается рядом с цифрой, чтобы цифру можно было перепроверить руками.


def contacted_on(day: str) -> int:
    """Сколько уникальных людей мы РЕАЛЬНО написали за день.

    Единственный честный счётчик: требует message_id, который выдаёт Wazzup.
    """
    with read_only() as conn:
        return count(
            conn,
            "SELECT COUNT(DISTINCT contact_id) FROM messages "
            "WHERE message_id IS NOT NULL AND substr(sent_at,1,10) = ?",
            (day,),
        )


def contacted_all_time() -> int:
    with read_only() as conn:
        return count(
            conn,
            "SELECT COUNT(DISTINCT contact_id) FROM messages "
            "WHERE message_id IS NOT NULL",
        )


def delivered_on(day: str) -> int:
    with read_only() as conn:
        return count(
            conn,
            "SELECT COUNT(DISTINCT contact_id) FROM messages "
            "WHERE delivery_state = 'delivered' AND substr(sent_at,1,10) = ?",
            (day,),
        )


def delivered_all_time() -> int:
    with read_only() as conn:
        return count(
            conn,
            "SELECT COUNT(DISTINCT contact_id) FROM messages "
            "WHERE delivery_state = 'delivered'",
        )


def funnel(day: str) -> dict[str, int]:
    """Воронка дня: попытки = доставлено + отказ + без подтверждения.

    Сходится по построению; если не сходится — где-то появилось состояние
    вне словаря, и это повод проверить схему.
    """
    with read_only() as conn:
        base = "FROM messages WHERE message_id IS NOT NULL AND substr(sent_at,1,10) = ?"
        total = count(conn, f"SELECT COUNT(*) {base}", (day,))
        delivered = count(
            conn, f"SELECT COUNT(*) {base} AND delivery_state='delivered'", (day,)
        )
        rejected = count(
            conn, f"SELECT COUNT(*) {base} AND delivery_state='rejected'", (day,)
        )
        unknown = count(
            conn, f"SELECT COUNT(*) {base} AND delivery_state='unknown'", (day,)
        )
    return {
        "total": total,
        "delivered": delivered,
        "rejected": rejected,
        "unknown": unknown,
        "balances": delivered + rejected + unknown == total,
    }


def sent_phones() -> set[str]:
    """Номера с подтверждённой отправкой (``message_id`` заполнен).

    Критерий ОТЧЁТНОСТИ, а не безопасности — см. докстринг модуля.
    Для отбора в пул используйте :func:`contacted_phones`.
    """
    with read_only() as conn:
        return {
            r["phone"]
            for r in conn.execute(
                "SELECT DISTINCT ct.phone FROM messages m "
                "JOIN contacts ct ON ct.id = m.contact_id "
                "WHERE m.message_id IS NOT NULL AND m.sent_at IS NOT NULL"
            ).fetchall()
        }


def sent_phones_in(channel: str) -> set[str]:
    """Номера с подтверждённой отправкой ИМЕННО в этом канале."""
    with read_only() as conn:
        return {
            r["phone"]
            for r in conn.execute(
                "SELECT DISTINCT ct.phone FROM messages m "
                "JOIN contacts ct ON ct.id = m.contact_id "
                "WHERE m.channel = ? AND m.message_id IS NOT NULL "
                "  AND m.sent_at IS NOT NULL",
                (str(channel),),
            ).fetchall()
        }


def per_channel_counts() -> dict[str, dict[str, int]]:
    """Сводка по каналам: попытки / доставлено / уникальные люди."""
    with read_only() as conn:
        rows = conn.execute(
            "SELECT channel, "
            "  COUNT(*) AS attempts, "
            "  SUM(CASE WHEN message_id IS NOT NULL THEN 1 ELSE 0 END) AS sent, "
            "  SUM(CASE WHEN delivery_state='delivered' THEN 1 ELSE 0 END) AS delivered, "
            "  COUNT(DISTINCT CASE WHEN message_id IS NOT NULL THEN contact_id END) AS people "
            "FROM messages GROUP BY channel"
        ).fetchall()
    return {
        r["channel"]: {
            "attempts": int(r["attempts"] or 0),
            "sent": int(r["sent"] or 0),
            "delivered": int(r["delivered"] or 0),
            "people": int(r["people"] or 0),
        }
        for r in rows
    }


def last_contacted_at() -> Optional[str]:
    """Когда последний раз писали кому-нибудь — для сообщения «пул пуст».

    Берётся по самой свежей строке с непустым временем отправки, независимо
    от того, сохранился ли message_id: при пустом пуле важно знать, когда был
    последний рабочий день, а не сколько сообщений ушло.
    """
    with read_only() as conn:
        return scalar(
            conn,
            "SELECT MAX(sent_at) FROM messages WHERE sent_at IS NOT NULL",
        )


__all__ = [
    "SENT_STATUSES",
    "CONTACTED_PREDICATE",
    "record_attempt",
    "record_send",
    "set_delivery_state",
    "retire_unconfirmed",
    "get",
    "by_wazzup_id",
    "last_sent",
    "contacted_contact_ids",
    "contacted_phones",
    "last_contacted_at",
    "contacted_on",
    "contacted_all_time",
    "delivered_on",
    "delivered_all_time",
    "funnel",
    "sent_phones",
    "sent_phones_in",
    "per_channel_counts",
]