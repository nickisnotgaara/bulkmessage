"""Выгрузка истории отправок в Google Sheets.

Зачем
----
Лист ``new_messages`` создаётся заново и наполняется из БД, а не дописывается
по одной строке из sender'а. Причины две:

* в старом листе ``Messages`` накопились 803 строки, из которых реальных
  отправок было меньше: считались попытки, симуляции и повторы;
* колонок, по которым видно, дошло ли сообщение до человека, там не было
  вообще — именно их не хватало, чтобы отличить «не доставлено» от
  «подтверждения ещё нет».

Выгрузка читает базу (источник истины) и переписывает лист целиком. Поэтому
она идемпотентна: повторный запуск даёт тот же результат, а не задваивает
строки.
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Optional

from . import config
from .sheets import SHEETS


# Что попадает в лист. Строки без message_id НЕ выгружаются: это попытки
# без отправки (отказ каскада, синхронный 400, ошибка транспорта), и именно
# они раньше раздували таблицу.
_EXPORT_SQL = """
SELECT m.id, m.contact_id, m.channel, m.message_text, m.message_id,
       m.status, m.delivery_state, m.sent_at, m.delivered_at, m.read_at,
       m.answered_at, m.last_error, m.updated_at, m.created_at,
       c.phone, c.name, c.category, c.consent,
       (SELECT r.message FROM replies r
         WHERE r.contact_id = m.contact_id AND r.channel = m.channel
         ORDER BY r.created_at DESC LIMIT 1) AS reply
FROM messages m
JOIN contacts c ON c.id = m.contact_id
WHERE m.message_id IS NOT NULL
  AND m.message_id NOT LIKE 'dryrun_%'
ORDER BY m.sent_at, m.id
"""


def collect_rows() -> list[dict]:
    """Читает отправки из базы. Ничего не пишет."""
    from .db import db_conn

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(_EXPORT_SQL).fetchall()]


def build_rows(records: list[dict]) -> list[list[str]]:
    return [
        SHEETS.build_row_payload(
            phone=r["phone"] or "",
            name=r["name"] or "",
            category=r["category"] or "",
            channel=r["channel"] or "",
            message=r["message_text"] or "",
            sent_at=r["sent_at"],
            delivered_at=r["delivered_at"],
            read_at=r["read_at"],
            answered_at=r["answered_at"],
            delivery_state=r["delivery_state"] or "unknown",
            status=r["status"] or "",
            last_error=r["last_error"] or "",
            reply=r["reply"] or "",
            last_activity=r["updated_at"] or r["created_at"],
            message_id=r["message_id"] or "",
            contact_id=r["contact_id"],
            consent=r["consent"] or "unknown",
        )
        for r in records
    ]


def _write_bulk(ws, header: list[str], rows: list[list[str]]) -> int:
    """Переписывает лист целиком пачками по 500 строк.

    ``resize`` обязателен и не формальность. Лист мог остаться от прежней
    схемы: живой случай — лист остался с 26 столбцами, из них 8 старых
    заголовков висели правее новых, и ``get_all_values`` возвращал шапку из
    26 имён, хотя код знал только про 18. Поиск строки по заголовку после
    этого давал бы индексы не туда. Перезапись без resize оставляет хвост
    и лишние строки — их и убирает resize.
    """
    ws.resize(max(len(rows) + 1, 2), len(header))
    ws.update("A1", [header])
    if not rows:
        return 0
    batch = 500
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        end_row = 2 + start + len(chunk) - 1
        last_col = chr(ord("A") + len(header) - 1)
        ws.update(f"A2:{last_col}{end_row}", chunk, value_input_option="USER_ENTERED")
    return len(rows)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not SHEETS.enabled:
        print("Google Sheets недоступны — проверьте BULK_GOOGLE_SHEET_ID "
              "и credentials.json", file=sys.stderr)
        return 1

    records = collect_rows()
    rows = build_rows(records)

    reached = sum(
        1 for r in records
        if r["delivered_at"] or r["read_at"] or r["answered_at"]
        or r["delivery_state"] == "delivered"
    )
    people = len({r["contact_id"] for r in records})
    reached_people = len({
        r["contact_id"] for r in records
        if r["delivered_at"] or r["read_at"] or r["answered_at"]
        or r["delivery_state"] == "delivered"
    })
    print(f"Лист: {config.GOOGLE_WORKSHEET_NAME}")
    print(f"Строк (каждая = одна отправка): {len(records)}")
    print(f"Уникальных людей: {people}")
    print(f"Дошло до человека: {reached_people} человек "
          f"({reached} отправок)")
    print(f"Столбцов: {len(config.GSHEET_HEADERS)}")

    if "--dry-run" in argv:
        print()
        print("Сухой прогон — лист не тронут.")
        if rows:
            print("Первая строка:")
            for h, v in zip(config.GSHEET_HEADERS, rows[0]):
                print(f"   {h:16} {v}")
        return 0

    ws = SHEETS._get_worksheet()
    if ws is None:
        print("Не удалось открыть лист для записи", file=sys.stderr)
        return 1
    try:
        written = _write_bulk(ws, config.GSHEET_HEADERS, rows)
    except Exception as e:
        print(f"Ошибка записи: {e}", file=sys.stderr)
        return 1
    print(f"Записано строк: {written}")
    return 0


__all__ = ["collect_rows", "build_rows", "main"]