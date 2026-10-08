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
SELECT m.contact_id, m.channel, m.delivery_state, m.status, m.sent_at,
       m.delivered_at, m.read_at, m.answered_at, m.last_error, m.message_id,
       m.message_text, m.updated_at, m.created_at,
       c.phone, c.name, c.category,
       (SELECT r.message FROM replies r
         WHERE r.contact_id = m.contact_id
         ORDER BY r.created_at DESC LIMIT 1) AS reply
FROM messages m
JOIN contacts c ON c.id = m.contact_id
WHERE m.message_id IS NOT NULL
  AND m.message_id NOT LIKE 'dryrun_%'
ORDER BY m.contact_id, m.sent_at, m.id
"""

# Вердикты «есть ли у человека аккаунт» — из contact_channel_state. Именно они
# дают колонки Has WhatsApp / Has Telegram / Has MAX: они отвечают на вопрос
# «куда вообще можно достучаться» ещё ДО отправки, а не по факту попытки.
_VERDICT_SQL = """
SELECT contact_id,
       MAX(CASE WHEN channel IN ('waba', 'whatsapp') THEN verdict END) AS wa,
       MAX(CASE WHEN channel = 'telegram' THEN verdict END) AS tg,
       MAX(CASE WHEN channel = 'max' THEN verdict END) AS mx
FROM contact_channel_state
GROUP BY contact_id
"""

# Ранг исхода попытки: меньше — лучше. Дойти до человека важнее, чем просто
# быть отправленным; отказ хуже молчания.
_RANK = {
    "delivered": 0, "read": 0, "answered": 0,
    "sent": 1,
    "unknown": 1, "queued": 1,
    "rejected": 2, "failed": 2, "exhausted": 2,
}

_VERDICT_YES = {"present", "delivered"}
_VERDICT_NO = {"absent", "blocked"}


def _verdict_cell(value: Optional[str]) -> str:
    """Вердикт канала → «yes» / «no» / «unknown»."""
    if value is None:
        return "unknown"
    if value in _VERDICT_YES:
        return "yes"
    if value in _VERDICT_NO:
        return "no"
    return "unknown"


def collect_rows() -> list[dict]:
    """Читает отправки из базы. Ничего не пишет."""
    from .db import db_conn

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(_EXPORT_SQL).fetchall()]


def collect_verdicts() -> dict:
    """Вердикты «есть ли аккаунт» по контактам: contact_id → (wa, tg, mx)."""
    from .db import db_conn

    with db_conn() as conn:
        conn.row_factory = sqlite3.Row
        return {
            r["contact_id"]: (r["wa"], r["tg"], r["mx"])
            for r in conn.execute(_VERDICT_SQL).fetchall()
        }


def _outcome(r: dict) -> str:
    """Короткая метка исхода попытки для колонки Cascade."""
    state = (r["delivery_state"] or "").strip()
    status = (r["status"] or "").strip()
    if r["delivered_at"]:
        return "delivered"
    if r["read_at"]:
        return "read"
    if r["answered_at"]:
        return "answered"
    if state == "delivered":
        return "delivered"
    if state == "rejected" or status == "failed":
        return "rejected"
    if status in ("sent", "queued"):
        return "sent"
    return state or status or "unknown"


def aggregate_people(records: list[dict], verdicts: dict) -> list[dict]:
    """Сворачивает попытки в одну запись на человека.

    Раньше лист строился по одной строке на ПОПЫТКУ, и один и тот же человек
    занимал три строки (WABA отказ → Telegram → MAX). Ответ на вопрос
    «достучались мы» требовал складывать их вручную — ровно та работа, ради
    которой отчёт и делался.
    """
    grouped: dict = {}
    for r in records:
        grouped.setdefault(r["contact_id"], []).append(r)

    people = []
    for cid, attempts in grouped.items():
        # Победитель — попытка с лучшим рангом; при равенстве берём самую
        # раннюю, чтобы канал отражал ПЕРВЫЙ успех, а не последний повтор.
        best = min(
            attempts,
            key=lambda a: (_RANK.get(_outcome(a), 1), a["sent_at"] or ""),
        )
        best_outcome = _outcome(best)
        channel = (best["channel"] or "") if _RANK.get(best_outcome, 1) < 2 \
            else "none of them"

        cascade = " → ".join(
            f"{a['channel']}:{_outcome(a)}" for a in attempts
        )
        wa, tg, mx = verdicts.get(cid, (None, None, None))
        people.append({
            "phone": best["phone"] or "",
            "name": best["name"] or "",
            "category": best["category"] or "",
            "channel": channel,
            "cascade": cascade,
            "has_whatsapp": _verdict_cell(wa),
            "has_telegram": _verdict_cell(tg),
            "has_max": _verdict_cell(mx),
            "reached": any(
                a["delivered_at"] or a["read_at"] or a["answered_at"]
                or a["delivery_state"] == "delivered"
                for a in attempts
            ),
            "message": best["message_text"] or "",
            "sent_at": best["sent_at"],
            "delivered_at": next(
                (a["delivered_at"] for a in attempts if a["delivered_at"]), None),
            "read_at": next(
                (a["read_at"] for a in attempts if a["read_at"]), None),
            "answered_at": next(
                (a["answered_at"] for a in attempts if a["answered_at"]), None),
            "reply": best["reply"] or "",
            "last_activity": max(
                (a["updated_at"] or a["created_at"] or "") for a in attempts),
            "last_error": best["last_error"] or "",
            "message_id": best["message_id"] or "",
        })
    people.sort(key=lambda p: p["phone"])
    return people


def build_rows(people: list[dict]) -> list[list[str]]:
    return [SHEETS.build_row_payload(**p) for p in people]


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
    people_rows = aggregate_people(records, collect_verdicts())
    rows = build_rows(people_rows)

    reached = sum(1 for p in people_rows if p["reached"])
    print(f"Лист: {config.GOOGLE_WORKSHEET_NAME}")
    print(f"Попыток в базе: {len(records)}")
    print(f"Строк в листе (каждая = один человек): {len(people_rows)}")
    print(f"Дошло до человека: {reached}")
    by_channel: dict = {}
    for p in people_rows:
        by_channel[p["channel"]] = by_channel.get(p["channel"], 0) + 1
    print("Чем доставили: " + ", ".join(
        f"{k}={v}" for k, v in sorted(by_channel.items())))
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


__all__ = ["collect_rows", "collect_verdicts", "aggregate_people",
           "build_rows", "main"]