"""Подключение к SQLite и дисциплина транзакций.

Зачем этот модуль
-----------------
Раньше код открывал соединение напрямую (``sqlite3.connect(...)``) в 41 месте,
и каждое место само решало, когда закоммитить. Из-за этого:

  * падение посередине пачки вставок оставляло половину записанного;
  * два параллельных прогона могли переписать квоты друг друга;
  * никто не мог сказать, какая схема сейчас в базе.

Здесь ровно три вещи, на которые можно положиться:

  * :func:`connect` — одно место, где настраивается соединение (WAL, busy_timeout);
  * :func:`transaction` — атомарный блок: откат при ЛЮБОМ исключении;
  * :func:`require_current_schema` — проверка версии схемы перед боевой работой.

Использование::

    with transaction() as conn:
        conn.execute("INSERT INTO contacts (phone, name) VALUES (?, ?)", (p, n))
    # здесь либо всё записано, либо ничего
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .. import config


# Сериализуем записи в пределах процесса: SQLite допускает одного писателя,
# и без этого два потока получали бы «database is locked».
_WRITE_LOCK = threading.RLock()


def connect() -> sqlite3.Connection:
    """Открывает соединение с настроенными PRAGMA.

    Не коммитит автоматически: режим транзакций задаёт :func:`transaction`.
    """
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL: читатели не блокируют писателя. Именно это позволяет вебхуку
    # читать, пока sender пишет.
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL вместо FULL: при WAL это безопасно для наших целей и в разы
    # быстрее. Компромисс осознанный: при сбое питания теряются последние
    # транзакции, но не сама база.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Ждать освобождения блокировки вместо мгновенной ошибки.
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def transaction(*, destructive: bool = False) -> Iterator[sqlite3.Connection]:
    """Атомарный блок работы с БД.

    Гарантии:
      * исключение внутри блока → ROLLBACK, состояние не меняется;
      * повторный вход (вложенность) не открывает вторую транзакцию —
        используется SAVEPOINT, иначе внешний ROLLBACK откатил бы
        уже закоммиченную внешнюю работу.

    ``destructive=True`` — для миграций, пересоздающих таблицы.

    Тонкость SQLite, из-за которой нужен отдельный режим: PRAGMA foreign_keys
    и legacy_alter_table действуют ТОЛЬКО ВНЕ ТРАНЗАКЦИИ — внутри BEGIN они
    молча игнорируются. Без них пересоздание таблицы ломается: при
    ``ALTER TABLE contacts RENAME TO contacts__old`` внешние ключи в других
    таблицах переписываются на ``contacts__old``, и последующий DROP падает с
    ``FOREIGN KEY constraint failed``. Поэтому для таких миграций прагмы
    выставляются ДО открытия транзакции и возвращаются после.
    """
    conn = connect()
    saved: list[tuple[str, str]] = []
    if destructive:
        for pragma in ("foreign_keys", "legacy_alter_table"):
            row = conn.execute(f"PRAGMA {pragma}").fetchone()
            saved.append((pragma, str(row[0])))
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
    with _WRITE_LOCK:
        in_outer = conn.in_transaction
        savepoint = None
        try:
            if in_outer:
                savepoint = f"sp_{id(conn):x}"
                conn.execute(f"SAVEPOINT {savepoint}")
            else:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if savepoint:
                if conn.in_transaction:
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            elif conn.in_transaction:
                # executescript() неявно завершает транзакцию перед запуском
                # скрипта, поэтому к этому моменту её может уже не быть.
                # Без проверки COMMIT падал бы на миграциях с DDL.
                conn.execute("COMMIT")
        except BaseException:
            try:
                if savepoint:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                elif conn.in_transaction:
                    conn.execute("ROLLBACK")
            except sqlite3.Error:
                # Откат не удался — молчать здесь нельзя: вызывающий считает,
                # что данные записаны. Пусть исключение уйдёт дальше.
                pass
            raise
        finally:
            if destructive:
                for pragma, value in saved:
                    conn.execute(f"PRAGMA {pragma}={value}")
            conn.close()


@contextmanager
def read_only() -> Iterator[sqlite3.Connection]:
    """Соединение только для чтения.

    Отдельный контекст не избыточность: он даёт читателям честную семантику
    (ничего не пишем) и не берёт пишущую блокировку.
    """
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def rows_to_dicts(rows) -> list[dict]:
    """sqlite3.Row → list[dict] для удобной сериализации (JSON, логи)."""
    return [dict(row) for row in rows]


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    """Первое значение первой строки. None, если строк нет.

    Короткая замена для повторяющегося ``fetchone()[0]`` с проверкой None.
    """
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    """Целое число по запросу вида ``SELECT COUNT(*) ...``. 0, если строк нет."""
    return int(scalar(conn, sql, params) or 0)


def require_current_schema() -> None:
    """Проверяет, что схема БД не старше требуемой версии.

    Боевая работа (sender) обязан вызвать это при старте. Без проверки код
    после структурной правки схемы падал бы в самый неподходящий момент —
    посреди рассылки, когда часть строк уже записана.
    """
    from .enums import SCHEMA_VERSION
    from .migrations import applied_versions, current_version

    actual = current_version()
    if actual < SCHEMA_VERSION:
        raise SchemaOutOfDate(
            f"Схема БД устарела: версия {actual}, требуется {SCHEMA_VERSION}.\n"
            f"   Применить миграции:  py main.py migrate\n"
            f"   Файл БД: {Path(config.DB_PATH).name}"
        )
    if applied_versions() is None:
        raise SchemaOutOfDate(
            "Таблица schema_migrations отсутствует — БД создана до системы миграций.\n"
            f"   Применить миграции:  py main.py migrate"
        )


class SchemaOutOfDate(RuntimeError):
    """БД не соответствует текущему коду. Боевой запуск обязан быть остановлен."""


__all__ = [
    "connect",
    "transaction",
    "read_only",
    "rows_to_dicts",
    "scalar",
    "count",
    "require_current_schema",
    "SchemaOutOfDate",
]