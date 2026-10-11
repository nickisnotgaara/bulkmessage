"""Команда ``pending-sms`` — показать и выгрузить тех, до кого не дотянулись.

Зачем
----
SMS платный (~8.5 ₽ за сегмент) и запускается вручную, отдельным решением.
Эта команда НИЧЕГО не отправляет: она только показывает, кого мессенджеры не
достали, с вердиктами по каналам и списком уже использованных каналов.

Использование::

    py main.py pending-sms                 # сводка и первые строки
    py main.py pending-sms --collect 2026-10-09   # собрать за день в базу
    py main.py pending-sms --csv data/sms.csv     # выгрузка в CSV
    py main.py pending-sms --limit 100

Без ``--collect`` команда ничего не пишет: список наполняется либо в конце
прогона sender'а, либо вручную этой командой за конкретный день.
"""

from __future__ import annotations

import sys
from typing import Optional

from . import config
from .store import pending_sms


def _print_rows(rows: list[dict], limit: int) -> None:
    if not rows:
        print("Список пуст — до этих контактов в базе не дошли или "
              "список ещё не собран (см. --collect).")
        return
    print(f"\n{'Телефон':<14} {'TG':<4} {'MAX':<4} {'Каналы':<24} Причина")
    print("─" * 78)
    for r in rows[:limit]:
        print(
            f"{r.get('phone') or '':<14} "
            f"{r.get('has_telegram') or '—':<4} "
            f"{r.get('has_max') or '—':<4} "
            f"{(r.get('channels_tried') or '')[:23]:<24} "
            f"{r.get('reason') or ''}"
        )
    if len(rows) > limit:
        print(f"\n… и ещё {len(rows) - limit}")


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # main.py передаёт sys.argv[1:], поэтому первым элементом приходит само
    # имя режима. Остальные команды его молча игнорируют — здесь тоже.
    if argv and argv[0] == "pending-sms":
        argv = argv[1:]

    collect_day: Optional[str] = None
    csv_path: Optional[str] = None
    limit = 50

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--collect":
            i += 1
            collect_day = argv[i] if i < len(argv) else None
            if not collect_day:
                print("--collect требует дату: --collect 2026-10-09",
                      file=sys.stderr)
                return 1
        elif arg == "--csv":
            i += 1
            csv_path = argv[i] if i < len(argv) else None
            if not csv_path:
                print("--csv требует путь файла", file=sys.stderr)
                return 1
        elif arg == "--limit":
            i += 1
            limit = int(argv[i]) if i < len(argv) else 50
        else:
            print(f"Неизвестный аргумент: {arg}", file=sys.stderr)
            return 1
        i += 1

    if collect_day:
        added = pending_sms.collect_since(pending_sms._day_start_utc(collect_day))
        print(f"Собрано за {collect_day}: добавлено {added} контактов в pending_sms")

    info = pending_sms.summary()
    print("\n── PENDING SMS ──")
    for status, n in sorted(info["by_status"].items()):
        print(f"  {status:<10} {n}")
    print(f"  из них ожидают: есть TG но нет MAX — {info['pending_tg_no_max']}, "
          f"нет ни одного мессенджера — {info['pending_no_messengers']}")
    print("\nSMS здесь НЕ отправляется: команда только показывает список. "
          "Отправка — отдельное решение.")

    rows = pending_sms.pending()
    print(f"\nВсего в ожидании: {len(rows)}")
    _print_rows(rows, limit)

    if csv_path:
        written = pending_sms.export_csv(csv_path)
        print(f"\nЗаписано в CSV: {csv_path} ({written} строк)")

    return 0


__all__ = ["main"]