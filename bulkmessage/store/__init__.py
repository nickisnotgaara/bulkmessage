"""Слой доступа к данным: единственное место, где живёт SQL.

Структура пакета
----------------
``enums``       словарь предметной области (каналы, вердикты, состояния).
                Значения определены ОДИН раз и валидируются на границе.
``connection``  подключение, PRAGMA, транзакции. Одна точка правды про
                атомарность и параллельный доступ.
``migrations``  версионированные изменения схемы. Схема БД никогда не
                редактируется вручную — только через миграции.
``repositories`` операции над доменными сущностями (контакты, сообщения,
                состояния каналов, очередь отката, квоты).

Почему SQL не торчит наружу
---------------------------
Раньше 61 запрос писался прямо в бизнес-логике (``reconcile.py``,
``webhook_app.py``, ``sheets.py``), и правка схемы требовала grep по всему
коду. Теперь бизнес-логика знает только про функции репозиториев, а все
запросы — в этом пакете.

Пример использования::

    from bulkmessage.store import contacts, Channel

    cid = contacts.upsert("+79991234567", "Иван", "Покупатели")
    rows = contacts.reachable_by_channel(Channel.TELEGRAM)
"""

from __future__ import annotations

from .connection import (
    SchemaOutOfDate,
    connect,
    count,
    read_only,
    require_current_schema,
    rows_to_dicts,
    scalar,
    transaction,
)
from .enums import (
    SCHEMA_VERSION,
    Channel,
    ComplaintKind,
    DeliveryState,
    MessageStatus,
    RescueStatus,
    Verdict,
)
from .migrations import (
    MIGRATIONS,
    applied_versions,
    current_version,
    ensure_migrated,
    migrate,
    pending,
)

# Репозитории импортируются последними: они зависят от connection/enums,
# но не от самого пакета (иначе получился бы циклический импорт).
from . import contacts, messages, channel_state, pending_sms, queue, quota  # noqa: E402

__all__ = [
    # соединение
    "connect",
    "transaction",
    "read_only",
    "scalar",
    "count",
    "rows_to_dicts",
    "require_current_schema",
    "SchemaOutOfDate",
    # словарь
    "Channel",
    "Verdict",
    "DeliveryState",
    "MessageStatus",
    "RescueStatus",
    "ComplaintKind",
    "SCHEMA_VERSION",
    # миграции
    "migrate",
    "ensure_migrated",
    "pending",
    "current_version",
    "applied_versions",
    "MIGRATIONS",
    # репозитории
    "contacts",
    "messages",
    "channel_state",
    "pending_sms",
    "queue",
    "quota",
]