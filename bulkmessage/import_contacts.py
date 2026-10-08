"""Импорт контактов из Excel в базу — разовая операция перехода.

Зачем
----
До этого проекта источником контактов был ``data/Contact.xlsx``, а таблица
``contacts`` была побочным продуктом рассылки: она наполнялась в момент
отправки. Человека, которому ещё не писали, в базе не было — он существовал
только в файле. Из 4428 строк Excel база знала 848, то есть ровно тех, кого
уже вычеркнули.

После импорта источник истины один — таблица ``contacts``. Excel читается
здесь один раз и дальше не участвует в работе.

Почему нужен ``--apply``
------------------------
Импорт без флага ничего не пишет. Причина не в осторожности ради осторожности:
повторный импорт поверх истории — единственный способ отправить 4000+ номеров
второй раз. Тот же риск закрыт предохранителем в sender, но предохранитель
срабатывает уже во время рассылки, а здесь мы показываем результат ДО записи.

Идемпотентность
---------------
``upsert`` идемпотентен по телефону, поэтому повторный прогон на том же файле
добавляет ноль контактов. Это проверяется тестом, а не обещанием.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config
from .contacts import load_contacts
from .store import contacts as repo_contacts
from .store import messages as repo_messages


@dataclass
class ImportSummary:
    """Что увидели в файле и что предлагаем сделать."""

    file_total: int = 0
    without_phone: int = 0
    duplicates_in_file: int = 0
    already_in_db: int = 0
    new: int = 0
    already_contacted: int = 0
    new_unusable_phone: int = 0
    pool_now: int = 0
    pool_after: int = 0
    applied: bool = False

    def render(self, path: str) -> str:
        verb = "Записано" if self.applied else "Будет добавлено"
        lines = [
            f"Источник: {path}",
            f"{verb}: {self.new} новых контактов",
            f"Уже в базе: {self.already_in_db}",
            f"  из них уже получали сообщение (в пул не попадут): "
            f"{self.already_contacted}",
            f"Строк в файле: {self.file_total}",
            f"Без телефона: {self.without_phone}",
            f"Дублей телефона внутри файла: {self.duplicates_in_file}",
            f"Новых с негодным номером (в пул не попадут): {self.new_unusable_phone}",
            "",
        ]
        if self.applied:
            lines.append(f"Пул после импорта: {self.pool_after}")
        else:
            lines.append(f"Пул сейчас: {self.pool_now}")
            lines.append(f"Ожидается после импорта: {self.pool_after}")
        return "\n".join(lines) + "\n"


def _collect(path: str) -> tuple[list[dict], dict]:
    """Читает Excel и возвращает (нормализованные строки, счётчики отбраковки)."""
    raw = load_contacts(path)
    rows: list[dict] = []
    seen: set[str] = set()
    skipped = {"without_phone": 0, "duplicates": 0}
    for item in raw:
        phone = str(item.get("phone") or "").strip()
        if not phone:
            skipped["without_phone"] += 1
            continue
        if phone in seen:
            skipped["duplicates"] += 1
            continue
        seen.add(phone)
        rows.append({
            "phone": phone,
            "name": (item.get("name") or "").strip(),
            "category": (item.get("category") or "").strip(),
            "consent": item.get("consent"),
        })
    skipped["total"] = len(raw)
    return rows, skipped


def _is_usable_phone(phone: str) -> bool:
    """Российский мобильный по форме: 11 цифр, начинается с 7.

    Тот же критерий, что у ``store.contacts.junk_candidates``: номер из стенда
    или peer id Telegram в пул попадать не должен.
    """
    return len(phone) == 11 and phone.startswith("7") and phone.isdigit()


def analyze(path: Optional[str] = None) -> ImportSummary:
    """Считает, что изменится, ничего не записывая."""
    path = path or config.EXCEL_PATH
    rows, skipped = _collect(path)
    known = {c["phone"] for c in _all_contacts()}
    fresh = [r for r in rows if r["phone"] not in known]
    contacted = repo_messages.contacted_phones()
    usable = [r for r in fresh if _is_usable_phone(r["phone"])]
    pool_now = repo_contacts.pool_size()
    return ImportSummary(
        file_total=skipped["total"],
        without_phone=skipped["without_phone"],
        duplicates_in_file=skipped["duplicates"],
        already_in_db=len(rows) - len(fresh),
        new=len(fresh),
        already_contacted=len(known & contacted),
        new_unusable_phone=len(fresh) - len(usable),
        pool_now=pool_now,
        pool_after=pool_now + len(usable),
        applied=False,
    )


def _all_contacts() -> list[dict]:
    from .store.connection import read_only

    with read_only() as conn:
        return [
            dict(r)
            for r in conn.execute("SELECT phone FROM contacts").fetchall()
        ]


def run(path: Optional[str] = None, *, apply: bool = False) -> ImportSummary:
    """Разбирает файл и, при ``apply=True``, записывает новых контактов."""
    path = path or config.EXCEL_PATH
    if not Path(path).exists():
        raise FileNotFoundError(
            f"Файл контактов не найден: {path}\n"
            "Без него переход на базу невозможен — путь задаётся BULK_EXCEL_PATH."
        )
    summary = analyze(path)
    if not apply:
        return summary

    rows, _ = _collect(path)
    already = {c["phone"] for c in _all_contacts()}
    fresh = [r for r in rows if r["phone"] not in already]
    for row in fresh:
        repo_contacts.upsert(
            row["phone"], row["name"], row["category"], consent=None
        )
    summary.applied = True
    summary.already_in_db = len(rows) - len(fresh)
    summary.new = len(fresh)

    # Флаги is_test / is_invalid обязаны встать сразу: отбор пула смотрит
    # именно на них, а не на форму номера. Без этого новый контакт с номером
    # из стенда попал бы в пул и ждал бы отдельного запуска сида — то есть
    # рассылка попыталась бы написать на 998933930709 или на peer id.
    flagged = _flag_junk()
    summary.new_unusable_phone = flagged["unusable_in_db"]
    summary.pool_after = repo_contacts.pool_size()
    return summary


def _flag_junk() -> dict:
    """Помечает мусорные контакты и считает, сколько из них негодных по форме."""
    from .db import purge_junk_contacts

    result = purge_junk_contacts(dry_run=False)
    unusable = sum(
        1 for phone in result["invalid"]
        if not _is_usable_phone(phone)
    )
    return {"invalid": len(result["invalid"]),
            "test": len(result["test"]),
            "unusable_in_db": unusable}


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    path = config.EXCEL_PATH
    if "--path" in argv:
        i = argv.index("--path")
        if i + 1 >= len(argv):
            print("Ошибка: --path требует путь к файлу", file=sys.stderr)
            return 2
        path = argv[i + 1]
    apply = "--apply" in argv

    try:
        summary = run(path, apply=apply)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(summary.render(path))
    if not summary.applied:
        print()
        print("Ничего не записано. Повторите с --apply, чтобы записать.")
        return 0

    pool = repo_contacts.pool_size()
    print()
    print(f"Контактов всего: {repo_contacts.total()}")
    print(f"Не писали ещё, доступно для рассылки: {pool}")
    if pool == 0:
        print("Внимание: пул пуст, рассылка не сможет отправить ничего.")
    return 0


__all__ = ["ImportSummary", "analyze", "run", "main"]