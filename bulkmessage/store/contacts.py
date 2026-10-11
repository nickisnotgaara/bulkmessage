"""Репозиторий контактов.

Единственное место, где знают про таблицу ``contacts``. Бизнес-логика работает
с функциями этого модуля и никогда не пишет SQL сама.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from .. import config
from . import messages
from .connection import count, read_only, transaction
from .enums import Channel, Consent


def upsert(
    phone: str,
    name: Optional[str] = None,
    category: Optional[str] = None,
    *,
    consent: Optional[str] = None,
    consent_source: Optional[str] = None,
) -> int:
    """Возвращает contact_id, создавая контакт при необходимости.

    Идемпотентно по телефону: повторный вызов с тем же номером не создаёт
    дубль (на phone стоит UNIQUE). Это важно: раньше дубль контакта означал
    две независимые истории отправок на одного человека.

    ``consent`` проверяется границей: значение вне словаря отвергается
    ValueError, а не молча ложится в базу. Раньше колонка была без CHECK, и
    поэтому в неё попадало ``yes`` — значение, которого нет ни в одном отчёте.
    """
    if consent is not None:
        consent = str(Consent.parse(consent)).lower()
    with transaction() as conn:
        row = conn.execute(
            "SELECT id FROM contacts WHERE phone = ?", (phone,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE contacts SET "
                "  name       = COALESCE(?, name), "
                "  category   = COALESCE(?, category), "
                "  consent    = COALESCE(?, consent), "
                "  consent_source = COALESCE(?, consent_source) "
                "WHERE id = ?",
                (name, category, consent, consent_source, row["id"]),
            )
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO contacts (phone, name, category, consent, "
            "                      consent_source, consent_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (phone, name, category, consent or Consent.UNKNOWN.value,
             consent_source, datetime.now(timezone.utc).isoformat() if consent else None),
        )
        return int(cur.lastrowid)


def get_by_phone(phone: str) -> Optional[dict]:
    with read_only() as conn:
        row = conn.execute(
            "SELECT * FROM contacts WHERE phone = ?", (phone,)
        ).fetchone()
        return dict(row) if row else None


def get(contact_id: int) -> Optional[dict]:
    with read_only() as conn:
        row = conn.execute(
            "SELECT * FROM contacts WHERE id = ?", (contact_id,)
        ).fetchone()
        return dict(row) if row else None


def exists(phone: str) -> bool:
    with read_only() as conn:
        return count(
            conn, "SELECT COUNT(*) FROM contacts WHERE phone = ?", (phone,)
        ) > 0


def total() -> int:
    """Всего контактов в базе."""
    with read_only() as conn:
        return count(conn, "SELECT COUNT(*) FROM contacts")


def sendable(
    limit: Optional[int] = None,
    *,
    require_opt_in: Optional[bool] = None,
    channels: Optional[Sequence[str]] = None,
) -> list[dict]:
    """Контакты, годные для боевой рассылки — граница пула.

    Исключаются:

      * тестовые (``is_test=1``) и не-телефоны (``is_invalid=1``) — их завели
        стенды и тесты, они не должны получать сообщения;
      * те, у кого закрыты ВСЕ каналы отправки. Правило действует ПО КАНАЛУ
        (решение владельца 2026-10-12): попытка в Telegram не закрывает
        WhatsApp, потому что человек может иметь только один мессенджер из
        трёх. Критерий живёт в ``store.messages``, здесь подставляется готовый
        предикат, чтобы правило не расходилось;
      * без согласия на рассылку (ч.1 ст.18 ФЗ-38). ``denied`` запрещает
        всегда; ``unknown`` разрешает, если не включён opt-in-режим.

    Раньше фильтр был ``consent = 'yes'`` — значения таких в базе нет
    (там ``granted``/``unknown``/``denied``), поэтому функция возвращала ноль
    строк всегда, а тест это пропускал: он проверял исключение ``is_test`` из
    пустого множества.

    ``channels`` — каналы, закрытие которых исчерпывает контакт. По умолчанию
    все четыре канала Wazzup. Пустой список = контакт не исчерпается никогда.
    """
    if require_opt_in is None:
        require_opt_in = bool(getattr(config, "CONSENT_REQUIRED_FOR_COLD", 0))
    allowed = Consent.sendable_values(require_opt_in)
    exhausted_sql, exhausted_params = messages.exhausted_channels_predicate(
        channels or messages.WAZZUP_SEND_CHANNELS
    )
    sql = (
        "SELECT c.id, c.phone, c.name, c.category FROM contacts c "
        "WHERE COALESCE(c.is_test, 0) = 0 "
        "  AND COALESCE(c.is_invalid, 0) = 0 "
        "  AND c.consent IN (" + ", ".join("?" * len(allowed)) + ") "
        "  AND NOT " + exhausted_sql
    )
    params: tuple = tuple(allowed) + exhausted_params
    if limit:
        sql += " LIMIT ?"
        params = params + (limit,)
    with read_only() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def pool_size(
    *,
    require_opt_in: Optional[bool] = None,
    channels: Optional[Sequence[str]] = None,
) -> int:
    """Сколько контактов осталось неотправленных.

    Число, ради которого затевался переход с Excel: раньше пул считался как
    «строки файла минус отправленные», и его нельзя было показать одной
    командой. Ноль означает, что рассылка закончится — об этом нужно сказать
    прямо, а не молча уйти в пустой лог.

    Считаются контакты, у которых НЕ закрыты все каналы (см. :func:`sendable`).
    """
    if require_opt_in is None:
        require_opt_in = bool(getattr(config, "CONSENT_REQUIRED_FOR_COLD", 0))
    allowed = Consent.sendable_values(require_opt_in)
    exhausted_sql, exhausted_params = messages.exhausted_channels_predicate(
        channels or messages.WAZZUP_SEND_CHANNELS
    )
    sql = (
        "SELECT COUNT(*) FROM contacts c "
        "WHERE COALESCE(c.is_test, 0) = 0 "
        "  AND COALESCE(c.is_invalid, 0) = 0 "
        "  AND c.consent IN (" + ", ".join("?" * len(allowed)) + ") "
        "  AND NOT " + exhausted_sql
    )
    with read_only() as conn:
        return count(conn, sql, tuple(allowed) + exhausted_params)


def mark_flags(
    phone: str, *, is_test: Optional[bool] = None, is_invalid: Optional[bool] = None
) -> None:
    """Помечает контакт как тестовый или как «не телефон».

    Не удаляет: на контакте может висеть история отправок, а она нужна для
    отчётности. Флаг исключает контакт из боевого пула, сохраняя данные.
    """
    sets: list[str] = []
    params: list = []
    if is_test is not None:
        sets.append("is_test = ?")
        params.append(1 if is_test else 0)
    if is_invalid is not None:
        sets.append("is_invalid = ?")
        params.append(1 if is_invalid else 0)
    if not sets:
        return
    params.append(phone)
    with transaction() as conn:
        conn.execute(
            f"UPDATE contacts SET {', '.join(sets)} WHERE phone = ?", tuple(params)
        )


def junk_candidates() -> tuple[list[str], list[str]]:
    """Номера, похожие на мусор: (не-телефоны, тестовые).

    Правила намеренно простые и объяснимы:
      * телефон — не 11 цифр или не начинается с 7 → не российский мобильный
        (в базе так попал Telegram peer id ``1288960948`` и номера 998);
      * имя содержит Test/Ghost/Sim → след стенда.
    """
    with read_only() as conn:
        invalid = [
            r["phone"]
            for r in conn.execute(
                "SELECT phone FROM contacts "
                "WHERE (LENGTH(phone) <> 11 OR phone NOT LIKE '7%') "
                "  AND COALESCE(is_invalid, 0) = 0 "
                "  AND COALESCE(is_test, 0) = 0"
            ).fetchall()
        ]
        tests = [
            r["phone"]
            for r in conn.execute(
                "SELECT phone FROM contacts "
                "WHERE (name LIKE '%Test%' OR name LIKE '%Ghost%' OR name LIKE '%Sim%') "
                "  AND COALESCE(is_test, 0) = 0 "
                "  AND COALESCE(is_invalid, 0) = 0"
            ).fetchall()
        ]
    return invalid, tests


def flagged_counts() -> tuple[int, int]:
    """Сколько контактов помечено как тестовые / невалидные."""
    with read_only() as conn:
        tests = count(conn, "SELECT COUNT(*) FROM contacts WHERE is_test = 1")
        invalid = count(conn, "SELECT COUNT(*) FROM contacts WHERE is_invalid = 1")
    return tests, invalid


__all__ = [
    "upsert",
    "get_by_phone",
    "get",
    "exists",
    "total",
    "sendable",
    "pool_size",
    "mark_flags",
    "junk_candidates",
    "flagged_counts",
]