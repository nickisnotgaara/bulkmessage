"""Версионированные миграции схемы.

Зачем
----
Раньше структурные изменения делались вручную внутри ``init_db()``: куча
``CREATE TABLE IF NOT EXISTS`` и ``ALTER TABLE`` с проверкой ``_has_column``.
Это работает ровно до первого изменения, которое нужно применить к БД,
созданной ПРЕДЫДУЩЕЙ версией кода: часть таблиц уже есть со старыми
колонками, часть — нет, и результат зависит от того, в каком порядке
отработал процесс.

Здесь каждая правка — отдельная пронумерованная функция. Применённые версии
хранятся в ``schema_migrations``, поэтому:

  * миграция выполняется ровно один раз за жизнь базы;
  * повторный запуск безопасен (идемпотентно);
  * каждая миграция идёт в своей транзакции — упавшая откатывается целиком;
  * новая БД создаётся применением всех миграций по порядку, отдельного
    «CREATE TABLE со всеми колонками» не существует — иначе схема
    разъезжалась бы с историей.

Добавить миграцию:

  1. дописать функцию ``_m0003_что_делаем(conn)`` в :data:`MIGRATIONS`;
  2. поднять :data:`~bulkmessage.db.enums.SCHEMA_VERSION`;
  3. ничего больше — мигратор найдёт её сам.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from .connection import connect, transaction
from .enums import SCHEMA_VERSION, Consent


Migration = Callable[[sqlite3.Connection], None]


# ═══════════════════════════════════════════════════════════════════════
# Миграции. Порядок в списке = порядок применения. Удалять нельзя.
# ═══════════════════════════════════════════════════════════════════════


def _m0001_baseline(conn: sqlite3.Connection) -> None:
    """Начальная схема.

    Формы таблиц сняты с живой базы 2026-10-08 побайтно (PRAGMA table_info),
    а не придуманы: расхождение в одну колонку означало бы, что свежая база и
    старая ведут себя по-разному, и половина кода падала бы только на новых
    установках — худший вид поломки.

    Дальше каждое изменение делается отдельной миграцией, а не правкой здесь.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS contacts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            phone          TEXT    NOT NULL UNIQUE,
            name           TEXT,
            category       TEXT,
            consent        TEXT    NOT NULL DEFAULT 'unknown',
            consent_source TEXT,
            consent_at     TEXT,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            is_test        INTEGER NOT NULL DEFAULT 0,
            is_invalid     INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS messages (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id     INTEGER NOT NULL,
            channel        TEXT    NOT NULL,
            message_id     TEXT,
            message_text   TEXT,
            sent_at        TEXT,
            delivered_at   TEXT,
            read_at        TEXT,
            answered_at    TEXT,
            status         TEXT    NOT NULL,
            last_error     TEXT,
            gsheet_row     INTEGER,
            created_at     TEXT    NOT NULL,
            updated_at     TEXT    NOT NULL,
            delivery_state TEXT    NOT NULL DEFAULT 'unknown',
            swept_at       TEXT,
            FOREIGN KEY (contact_id) REFERENCES contacts(id)
        );

        CREATE TABLE IF NOT EXISTS replies (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER NOT NULL,
            channel    TEXT    NOT NULL,
            message    TEXT    NOT NULL,
            message_id TEXT,
            created_at TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS delivery_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            message_pk   INTEGER,
            message_id   TEXT NOT NULL,
            channel      TEXT,
            event_status TEXT NOT NULL,
            envelope     TEXT,
            received_at  TEXT NOT NULL,
            source       TEXT NOT NULL DEFAULT 'webhook'
        );

        CREATE TABLE IF NOT EXISTS rescue_queue (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            phone              TEXT    NOT NULL,
            name               TEXT,
            category           TEXT,
            original_channel   TEXT    NOT NULL,
            reason             TEXT,
            attempts           INTEGER NOT NULL DEFAULT 0,
            status             TEXT    NOT NULL DEFAULT 'pending',
            last_attempt_at    TEXT,
            rescued_channel    TEXT,
            rescued_message_id TEXT,
            created_at         TEXT    NOT NULL,
            updated_at         TEXT    NOT NULL,
            tried_channels     TEXT    NOT NULL DEFAULT '',
            retry_after        TEXT,
            attempts_total     INTEGER NOT NULL DEFAULT 0,
            last_outcome       TEXT,
            next_eligible_at   TEXT,
            first_seen         TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_counters (
            date      TEXT    NOT NULL,
            channel   TEXT    NOT NULL,
            accepted  INTEGER NOT NULL DEFAULT 0,
            delivered INTEGER NOT NULL DEFAULT 0,
            rejected  INTEGER NOT NULL DEFAULT 0,
            unknown   INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (date, channel)
        );

        CREATE TABLE IF NOT EXISTS contact_cooldowns (
            phone      TEXT NOT NULL,
            channel    TEXT NOT NULL,
            until      TEXT NOT NULL,
            reason     TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (phone, channel)
        );

        CREATE TABLE IF NOT EXISTS pending_webhooks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            channel     TEXT,
            payload     TEXT NOT NULL,
            received_at TEXT NOT NULL,
            attempts    INTEGER NOT NULL DEFAULT 0,
            last_error  TEXT,
            processed   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS sheets_ops (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            op         TEXT    NOT NULL,
            message_pk INTEGER,
            payload    TEXT    NOT NULL,
            attempts   INTEGER NOT NULL DEFAULT 0,
            done       INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS complaint_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            channel        TEXT NOT NULL,
            phone          TEXT,
            complaint_kind TEXT NOT NULL,
            message_id     TEXT,
            created_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS channel_reachability (
            phone           TEXT    NOT NULL,
            channel         TEXT    NOT NULL,
            reachable       INTEGER NOT NULL,
            last_checked_at TEXT    NOT NULL,
            last_error      TEXT,
            PRIMARY KEY (phone, channel)
        );

        CREATE TABLE IF NOT EXISTS waba_templates (
            template_id    TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            category       TEXT,
            language_code  TEXT NOT NULL DEFAULT 'ru',
            status         TEXT NOT NULL,
            last_synced_at TEXT NOT NULL,
            last_error     TEXT
        );

        CREATE TABLE IF NOT EXISTS contact_channel_state (
            contact_id     INTEGER NOT NULL,
            channel        TEXT    NOT NULL,
            verdict        TEXT    NOT NULL DEFAULT 'unknown',
            verdict_source TEXT,
            chat_id        TEXT,
            last_probe_at  TEXT,
            last_send_at   TEXT,
            last_error     TEXT,
            updated_at     TEXT    NOT NULL,
            PRIMARY KEY (contact_id, channel),
            FOREIGN KEY (contact_id) REFERENCES contacts(id)
        );
        """
    )


def _m0002_clean_delivery_state(conn: sqlite3.Connection) -> None:
    """Чистит смешанные понятия в ``messages.delivery_state``.

    Найдено на живой базе 2026-10-08: 56 строк имели
    ``delivery_state='exhausted'``. Это НЕ состояние доставки — «контакт
    исчерпал попытки» относится к контакту, а не к сообщению. Ни у одной из
    этих строк не было подтверждения доставки (``delivered_at`` и ``read_at``
    пусты), поэтому честное значение — ``unknown``.

    Пока значение было 'exhausted', отчёт не мог отличить «не доставлено»
    от «контакт исчерпан», и строки выпадали из воронки молча.
    """
    conn.execute(
        "UPDATE messages SET delivery_state = 'unknown' "
        "WHERE delivery_state = 'exhausted'"
    )


def _m0003_constraints(conn: sqlite3.Connection) -> None:
    """CHECK-ограничения: база сама отвергает мусор.

    Ограничения на уровне схемы — последний рубеж: даже если в коде появится
    опечатка, мусор не дойдёт до базы, а не осядет в отчёте.
    CHECK добавлены пересозданием таблиц, потому что SQLite не умеет
    ALTER TABLE ... ADD CONSTRAINT.
    """
    # Полные определения нужны для пересоздания: SQLite переносит данные
    # при rename, но не сохраняет внешние ключи и CHECK.
    _recreate_with_constraints(
        conn,
        table="messages",
        columns="""
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER,
            channel TEXT NOT NULL
                CHECK (channel IN ('waba','telegram','max','whatsapp','sms')),
            message_text TEXT,
            message_id TEXT,
            status TEXT DEFAULT 'queued'
                CHECK (status IN ('queued','sent','delivered','read','answered',
                                  'deleted','failed','exhausted')),
            delivery_state TEXT DEFAULT 'unknown'
                CHECK (delivery_state IN ('unknown','delivered','rejected')),
            sent_at TEXT,
            delivered_at TEXT,
            read_at TEXT,
            answered_at TEXT,
            last_error TEXT,
            swept_at TEXT,
            gsheet_row INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (contact_id) REFERENCES contacts(id)
        """,
        # Без этого удаление контакта запрещено — в истории отправок это
        # разорвало бы связь молча и оставило «сиротские» сообщения.
        on_delete="SET NULL",
    )

    _recreate_with_constraints(
        conn,
        table="rescue_queue",
        columns="""
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            name TEXT,
            category TEXT,
            original_channel TEXT,
            reason TEXT,
            attempts INTEGER DEFAULT 0,
            attempts_total INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending'
                CHECK (status IN ('pending','deferred','rescued','failed',
                                  'exhausted','cancelled')),
            last_attempt_at TEXT,
            rescued_channel TEXT,
            rescued_message_id TEXT,
            tried_channels TEXT,
            retry_after TEXT,
            next_eligible_at TEXT,
            last_outcome TEXT,
            first_seen TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        """,
        on_delete="CASCADE",
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_channel_state_v2 (
            contact_id INTEGER NOT NULL,
            channel TEXT NOT NULL
                CHECK (channel IN ('waba','telegram','max','whatsapp','sms')),
            verdict TEXT NOT NULL DEFAULT 'unknown'
                CHECK (verdict IN ('unknown','present','absent','delivered',
                                   'rejected','blocked')),
            verdict_source TEXT,
            chat_id TEXT,
            last_probe_at TEXT,
            last_send_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (contact_id, channel),
            FOREIGN KEY (contact_id) REFERENCES contacts(id)
        )
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO contact_channel_state_v2 "
        "SELECT * FROM contact_channel_state"
    )
    conn.execute("DROP TABLE contact_channel_state")
    conn.execute("ALTER TABLE contact_channel_state_v2 RENAME TO contact_channel_state")

    # contacts: флаги мусора (тестовые/не-телефоны) — см. purge_junk_contacts.
    for col, ddl in (("is_test", "INTEGER NOT NULL DEFAULT 0"),
                     ("is_invalid", "INTEGER NOT NULL DEFAULT 0")):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
        if col not in cols:
            conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {ddl}")

    _ensure_indexes(conn)


def _recreate_with_constraints(
    conn: sqlite3.Connection, table: str, columns: str, on_delete: str = "NO ACTION"
) -> None:
    """Пересоздаёт таблицу с новыми ограничениями, сохраняя данные.

    Порядок: выключаем проверку внешних ключей (она бы не дала переименовать
    таблицу, на которую ссылаются другие), переименовываем в «--old», создаём
    новую с ограничениями, переносим данные, удаляем старую.

    ``on_delete`` должен быть частью самого DDL (``columns``). Параметр
    оставлен явным только затем, чтобы вызывающий код не думал, что правило
    удаления применяется где-то само: SQLite не умеет ALTER для внешних ключей,
    поэтому правило обязано быть написано в ``CREATE TABLE``. Значение по
    умолчанию ``NO ACTION`` совпадает с фактическим поведением SQLite.
    """
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}__old")
        conn.execute(f"CREATE TABLE {table} ({columns})")
        common = [r[1] for r in conn.execute(f"PRAGMA table_info({table}__old)")]
        target = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        shared = [c for c in common if c in target]
        if shared:
            cols = ", ".join(shared)
            conn.execute(
                f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {table}__old"
            )
        conn.execute(f"DROP TABLE {table}__old")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    """Индексы под реальные запросы отчётности и выборок.

    Без них «кому мы написали» и «кого надо пробить» сканируют таблицу целиком.
    """
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_message_id
            ON messages(message_id);
        CREATE INDEX IF NOT EXISTS idx_messages_contact_channel
            ON messages(contact_id, channel);
        CREATE INDEX IF NOT EXISTS idx_messages_status
            ON messages(status);
        CREATE INDEX IF NOT EXISTS idx_messages_sent_at
            ON messages(sent_at);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_event
            ON delivery_events(message_id, event_status);
        CREATE INDEX IF NOT EXISTS idx_replies_contact
            ON replies(contact_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_replies_unique_msg
            ON replies(message_id);
        CREATE INDEX IF NOT EXISTS idx_rescue_status
            ON rescue_queue(status);
        -- Составной: выборка отката всегда идёт «по статусу, затем по времени»,
        -- т.е. WHERE status IN (...) ORDER BY created_at. Один status-only
        -- индекс такой запрос не обслуживает — сортировка остаётся в памяти.
        CREATE INDEX IF NOT EXISTS idx_rescue_status_created
            ON rescue_queue(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_rescue_phone
            ON rescue_queue(phone);
        CREATE INDEX IF NOT EXISTS idx_pending_processed
            ON pending_webhooks(processed);
        CREATE INDEX IF NOT EXISTS idx_sheets_ops_done
            ON sheets_ops(done);
        CREATE INDEX IF NOT EXISTS idx_complaints_channel_time
            ON complaint_events(channel, created_at);
        CREATE INDEX IF NOT EXISTS idx_ccs_verdict
            ON contact_channel_state(channel, verdict);
        CREATE INDEX IF NOT EXISTS idx_ccs_probe
            ON contact_channel_state(channel, last_probe_at);
        CREATE INDEX IF NOT EXISTS idx_messages_delivery_funnel
            ON messages(sent_at, delivery_state);
        """
    )


def _m0004_channel_daily(conn: sqlite3.Connection) -> None:
    """Квоты дня и остановки каналов переезжают в БД.

    Раньше квоты жили в ``broadcast_state.json``, который перезаписывался целиком
    при каждом событии. Любой сбой посередине (Ctrl+C, перезагрузка, два
    процесса) оставлял «потрачено» и «учтено» в разных состояниях — а на этой
    цифре стоит защита личного аккаунта от бана.

    Теперь учёт квоты — одна транзакция рядом с записью отправки, и сбой
    посередине не может оставить счётчик «в воздухе».

    ``halted_until`` хранится здесь же: решение «остановить канал» — это тоже
    защита личного номера, и оно не должно теряться при перезаписи JSON.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_daily (
            date         TEXT NOT NULL,
            channel      TEXT NOT NULL,
            sent         INTEGER NOT NULL DEFAULT 0,
            delivered    INTEGER NOT NULL DEFAULT 0,
            rejected     INTEGER NOT NULL DEFAULT 0,
            unknown      INTEGER NOT NULL DEFAULT 0,
            halted_until TEXT,
            halt_reason  TEXT,
            updated_at   TEXT,
            PRIMARY KEY (date, channel),
            CHECK (channel IN ('waba','telegram','max','whatsapp','sms'))
        )
        """
    )
    # Переносим текущие счётчики из broadcast_state.json-эпохи: считаем по
    # фактическим сообщениям, а не по json, который к этому моменту уже мог
    # разойтись с базой.
    conn.execute(
        """
        INSERT OR IGNORE INTO channel_daily (date, channel, sent, delivered, rejected, updated_at)
        SELECT substr(sent_at,1,10), channel,
               COUNT(DISTINCT contact_id),
               SUM(CASE WHEN delivery_state='delivered' THEN 1 ELSE 0 END),
               SUM(CASE WHEN delivery_state='rejected' THEN 1 ELSE 0 END),
               datetime('now')
        FROM messages
        WHERE message_id IS NOT NULL AND sent_at IS NOT NULL
        GROUP BY substr(sent_at,1,10), channel
        """
    )


def _m0005_normalize_contacts(conn: sqlite3.Connection) -> None:
    """Приводит ``contacts`` к канонической форме.

    Найдено на живой базе 2026-10-08: ``contacts.created_at`` объявлен как
    ``NOT NULL`` БЕЗ значения по умолчанию. Любой INSERT, который не передаёт
    эту колонку явно, падал с ``NOT NULL constraint failed``. Старый код
    избегал этого тем, что всегда подставлял created_at руками, — то есть
    форма таблицы молча зависела от дисциплины вызова.

    Здесь таблица пересоздаётся с DEFAULT и явным UNIQUE на phone: номер
    контакта — его естественный ключ, и дубль означал бы две независимые
    истории отправок на одного человека.

    ``consent`` получает DEFAULT 'unknown': согласие по умолчанию НЕ должно
    превращаться в «можно слать».
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
    has_unique = any(
        r["unique"]
        for r in conn.execute("PRAGMA index_list(contacts)").fetchall()
    )
    created_ok = "created_at" in cols and any(
        r[1] == "created_at" and r[4] is not None
        for r in conn.execute("PRAGMA table_info(contacts)").fetchall()
    )
    if created_ok and has_unique:
        return  # уже каноническая — не трогаем

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("ALTER TABLE contacts RENAME TO contacts__old")
        conn.execute(
            """
            CREATE TABLE contacts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                phone          TEXT    NOT NULL UNIQUE,
                name           TEXT,
                category       TEXT,
                consent        TEXT    NOT NULL DEFAULT 'unknown',
                consent_source TEXT,
                consent_at     TEXT,
                created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                is_test        INTEGER NOT NULL DEFAULT 0,
                is_invalid     INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        target = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
        shared = [c for c in cols if c in target]
        if shared:
            cols_sql = ", ".join(shared)
            conn.execute(
                f"INSERT OR IGNORE INTO contacts ({cols_sql}) "
                f"SELECT {cols_sql} FROM contacts__old"
            )
        conn.execute("DROP TABLE contacts__old")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _m0006_indexes_v2(conn: sqlite3.Connection) -> None:
    """Догоняет индексы для баз, мигрированных до их появления.

    Отдельная миграция, а не правка миграции 3: у баз, которые уже прошли
    1..5, миграция 3 больше не выполняется, и новый индекс просто не был бы
    создан. ``_ensure_indexes`` идемпотентна (IF NOT EXISTS), поэтому
    повторный вызов на свежей базе безопасен.
    """
    _ensure_indexes(conn)


def _ensure_reply_indexes(conn: sqlite3.Connection) -> None:
    """Индексы ``replies`` в канонической форме.

    Отдельная функция потому, что ``_ensure_indexes`` описывает историческую
    форму — UNIQUE по одному ``message_id``. Пересборка таблицы роняет индексы
    вместе с ней, а ``_ensure_indexes`` вернул бы на место старую, более
    строгую форму. Индекс сначала удаляется, иначе CREATE с другим определением
    под тем же именем просто не сработает.
    """
    conn.execute("DROP INDEX IF EXISTS idx_replies_unique_msg")
    conn.execute(
        "CREATE UNIQUE INDEX idx_replies_unique_msg "
        "ON replies(contact_id, channel, message_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_replies_contact ON replies(contact_id)"
    )


def _m0007_index_parity(conn: sqlite3.Connection) -> None:
    """Сводит набор индексов живой базы и свежей установки к одному.

    Найдено 2026-10-09: база, созданная с нуля миграциями, и уже
    существующая база расходились по индексам, потому что часть индексов
    появилась в старом коде руками и не попала в миграции. Проверка формы
    таблиц через ``PRAGMA table_info`` этого НЕ видит — она показывает
    только колонки, поэтому расхождение осталось незамеченным.

    Три различия:

    1. ``idx_reach_checked`` и ``idx_waba_templates_status`` отсутствовали в
       свежей базе — запросы по ним работали медленно только на новой
       установке.
    2. ``idx_replies_unique_msg`` в старой базе UNIQUE по
       (contact_id, channel, message_id), а в свежей — UNIQUE по одному
       message_id. Это не косметика: одна и та же вставка проходила бы в
       старой базе и падала бы в свежей.

    Приводим к варианту старой базы: он проверен боевыми прогонами и не может
    отвергнуть ничего из того, что уже работало.
    """
    _ensure_reply_indexes(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reach_checked "
        "ON channel_reachability(last_checked_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_waba_templates_status "
        "ON waba_templates(status)"
    )


def _m0008_replies_foreign_key(conn: sqlite3.Connection) -> None:
    """Возвращает replies внешний ключ на contacts.

    Найдено 2026-10-09 сверкой живой базы с базой, собранной миграциями с
    нуля: миграция 3 пересоздавала ``replies`` и теряла
    ``FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE``,
    который в боевой базе был создан старым DDL руками. Расхождение не видно
    через ``PRAGMA table_info`` — тот показывает только колонки; наличие
    внешнего ключа читается только через ``PRAGMA foreign_key_list``.

    Последствие для свежей базы: ``purge_junk_contacts`` удаляет контакты, и их
    ответы оставались сиротами — каскада, который их убирает, не существовало.

    Миграция идёт в дополнение к уже применённой третьей, а не правит её:
    применённые миграции неизменяемы, иначе базы, прошедшие 1..3, получили бы
    другой результат, чем базы, созданные после.
    """
    existing = [
        fk for fk in conn.execute("PRAGMA foreign_key_list(replies)")
        if fk[2] == "contacts"
    ]
    if existing:
        return

    orphans = conn.execute(
        "SELECT COUNT(*) FROM replies r "
        "LEFT JOIN contacts c ON c.id = r.contact_id "
        "WHERE c.id IS NULL"
    ).fetchone()[0]
    if orphans:
        raise sqlite3.IntegrityError(
            f"replies: {orphans} строк ссылаются на несуществующий контакт. "
            "Внешний ключ добавлять нельзя — сначала починить данные."
        )

    _recreate_with_constraints(
        conn,
        "replies",
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "contact_id INTEGER NOT NULL, "
        "channel TEXT NOT NULL, "
        "message TEXT NOT NULL, "
        "message_id TEXT, "
        "created_at TEXT NOT NULL, "
        "FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE",
    )
    _ensure_reply_indexes(conn)


def _m0009_consent_constraint(conn: sqlite3.Connection) -> None:
    """Ограничивает ``contacts.consent`` словарём из трёх значений.

    Найдено 2026-10-09 при переходе с Excel на базу контактов: у колонки не
    было CHECK, поэтому ``sendable()`` фильтровал по ``consent = 'yes'`` —
    значению, которого в базе никогда не бывает, — и возвращал ноль строк
    всегда, не падая. Опечатка в значении молча обнуляла весь боевой пул.

    Так выглядит цена отсутствующего словаря: проверять нечего, ошибаться
    негде, а тест проходил, потому что проверял исключение ``is_test`` из
    ПУСТОГО множества.

    Значения вне словаря (например ``yes``, если его успел записать старый
    код) приводятся к ``unknown`` — выдумывать отказ или согласие нельзя.
    """
    known = tuple(Consent.values())
    marks = ", ".join("?" * len(known))
    stray = conn.execute(
        f"SELECT COUNT(*) FROM contacts WHERE consent NOT IN ({marks})",
        known,
    ).fetchone()[0]
    if stray:
        conn.execute(
            f"UPDATE contacts SET consent = ? WHERE consent NOT IN ({marks})",
            (Consent.UNKNOWN.value,) + known,
        )

    # В CHECK литералы обязательны: параметры (?1, ?) в ограничениях запрещены,
    # SQLite отвергает такую форму ещё на CREATE TABLE.
    literals = ", ".join("'%s'" % value for value in Consent.values())

    _recreate_with_constraints(
        conn,
        "contacts",
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "phone TEXT NOT NULL UNIQUE, "
        "name TEXT, "
        "category TEXT, "
        f"consent TEXT NOT NULL DEFAULT '{Consent.UNKNOWN.value}' "
        f"CHECK (consent IN ({literals})), "
        "consent_source TEXT, "
        "consent_at TEXT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "is_test INTEGER NOT NULL DEFAULT 0, "
        "is_invalid INTEGER NOT NULL DEFAULT 0",
    )


def _m0010_pending_sms(conn: sqlite3.Connection) -> None:
    """Кому не дотянулись — список для будущей отправки через SMS.

    Решение владельца 2026-10-09. Мессенджеры исчерпали себя у части контактов:
    WABA отказал, Telegram и MAX аккаунта не имеют. Таких людей нельзя
    отслеживать по ``rescue_queue`` — та очередь живёт своей логикой попыток и
    TTL, а SMS это не откат, а отдельная платная отправка.

    Строка = человек (не попытка): индекс по contact_id уникальный, повторный
    прогон ничего не размножает.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_sms (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id     INTEGER NOT NULL,
            phone          TEXT NOT NULL,
            reason         TEXT,
            channels_tried TEXT,
            has_whatsapp   TEXT,
            has_telegram   TEXT,
            has_max        TEXT,
            last_attempt_at TEXT,
            status         TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending','sent','skipped')),
            created_at     TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at     TEXT
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_sms_contact "
        "ON pending_sms(contact_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_sms_status "
        "ON pending_sms(status)"
    )


MIGRATIONS: list[tuple[int, str, Migration]] = [
    (1, "baseline", _m0001_baseline),
    (2, "clean_delivery_state", _m0002_clean_delivery_state),
    (3, "constraints_and_indexes", _m0003_constraints),
    (4, "channel_daily_quota", _m0004_channel_daily),
    (5, "normalize_contacts", _m0005_normalize_contacts),
    (6, "indexes_v2", _m0006_indexes_v2),
    (7, "index_parity", _m0007_index_parity),
    (8, "replies_foreign_key", _m0008_replies_foreign_key),
    (9, "consent_constraint", _m0009_consent_constraint),
    (10, "pending_sms", _m0010_pending_sms),
]


# ═══════════════════════════════════════════════════════════════════════
# Мигратор
# ═══════════════════════════════════════════════════════════════════════


def _bootstrap_migration_table(conn: sqlite3.Connection) -> None:
    """Создаёт журнал миграций. Идемпотентно."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def current_version() -> int:
    """Версия схемы, применённая к этой БД. 0 — миграции не начинались."""
    try:
        with connect() as conn:
            _bootstrap_migration_table(conn)
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) v FROM schema_migrations"
            ).fetchone()
            return int(row["v"]) if row else 0
    except sqlite3.Error:
        return 0


def applied_versions() -> set[int] | None:
    """Множество применённых версий. None — журнала нет вовсе."""
    try:
        with connect() as conn:
            _bootstrap_migration_table(conn)
            return {
                int(r["version"])
                for r in conn.execute("SELECT version FROM schema_migrations")
            }
    except sqlite3.Error:
        return None


def pending() -> list[tuple[int, str]]:
    """Какие миграции ещё не применены."""
    done = applied_versions()
    if done is None:
        return [(v, n) for v, n, _ in MIGRATIONS]
    return [(v, n) for v, n, _ in MIGRATIONS if v not in done]


def migrate(target: int | None = None) -> list[str]:
    """Применяет неприменённые миграции. Возвращает список выполненных.

    Каждая миграция — в своей транзакции. Если миграция N упала, уже
    применённые N-1 остаются в силе, а повторный запуск попробует снова
    только N (использует idempotent-форму SQL: IF NOT EXISTS, INSERT OR IGNORE).
    """
    target = target if target is not None else SCHEMA_VERSION
    executed: list[str] = []
    for version, name, fn in MIGRATIONS:
        if version > target:
            break
        if version in (applied_versions() or set()):
            continue
        try:
            with transaction(destructive=True) as conn:
                _bootstrap_migration_table(conn)
                fn(conn)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, name) "
                    "VALUES (?, ?)",
                    (version, name),
                )
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Миграция {version} ({name}) не применилась: {exc}\n"
                f"   БД осталась на версии {current_version()} — это безопасно.\n"
                f"   Разберись с причиной и запусти миграции снова."
            ) from exc
        executed.append(f"{version} ({name})")
    return executed


def ensure_migrated() -> list[str]:
    """Мигрирует, если есть что мигрировать; молчит, если всё применено."""
    todo = pending()
    if not todo:
        return []
    return migrate()


__all__ = [
    "migrate",
    "ensure_migrated",
    "pending",
    "current_version",
    "applied_versions",
    "MIGRATIONS",
]