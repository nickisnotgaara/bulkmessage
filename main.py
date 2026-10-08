"""
Backwards-compatible shim.

The full implementation lives in the `bulkmessage` package:
  - bulkmessage.sender  (long-running sender daemon)
  - bulkmessage.tracker (webhook + reconciler)

Run modes:
  python main.py sender    -> bulkmessage.sender.run()
  python main.py tracker   -> bulkmessage.tracker.run()
  python main.py           -> bulkmessage.sender.run()  (default, for back-compat)
"""

import os
import sys

from bulkmessage.contacts import load_contacts
from bulkmessage.db import init_db
from bulkmessage.sender import run as run_sender
from bulkmessage.state import (
    load_state,
    save_state,
    channel_sent_today,
    increment_channel_sent,
    channel_has_quota,
    reset_daily_if_new_day,
)
from bulkmessage.templates import build_message, load_templates
from bulkmessage import sms_ru
from bulkmessage.templates import build_sms_message, load_sms_templates
from bulkmessage.wazzup import (
    active_channels,
    classify_error,
    send_to_channel,
    normalize_phone,
)
from bulkmessage import config


__all__ = [
    "load_templates",
    "build_message",
    "load_sms_templates",
    "build_sms_message",
    "sms_ru",
    "load_contacts",
    "init_db",
    "load_state",
    "save_state",
    "channel_sent_today",
    "increment_channel_sent",
    "channel_has_quota",
    "reset_daily_if_new_day",
    "active_channels",
    "send_to_channel",
    "classify_error",
    "normalize_phone",
    "config",
    "main",
]


def _apply_int_flag(argv: list, flag: str, env_key: str, title: str,
                    minimum: int = 1, config_attr: str | None = None) -> None:
    """Разбирает флаг вида --flag N и кладёт число в os.environ[env_key]
    И в атрибут модуля config.

    Флаги нужны для удобства запуска, но config читает переменные ОДИН РАЗ
    при импорте, а sender импортируется в самом верху файла. Поэтому одного
    os.environ мало: config.WABA_RUN_LIMIT уже вычислен и остался 0.
    Именно так `--waba-limit 15` молча отправлял 250: флаг печатался в лог,
    окружение менялось, а sender читал нулевое значение из config.

    Пишем в оба места: os.environ — чтобы ничего не перезаписал загрузчик
    .env, config_attr — чтобы sender увидел значение в том же процессе.
    """
    if flag not in argv:
        return
    i = argv.index(flag)
    if i + 1 >= len(argv):
        print(f"❌ {flag} требует число: {flag} 20", file=sys.stderr)
        raise SystemExit(2)
    raw = str(argv[i + 1]).strip()
    try:
        val = int(raw)
    except ValueError:
        print(f"❌ {flag}: '{raw}' не целое число", file=sys.stderr)
        raise SystemExit(2)
    if val < minimum:
        print(f"❌ {flag} должен быть >= {minimum} "
              f"(0 = без ограничения)", file=sys.stderr)
        raise SystemExit(2)
    os.environ[env_key] = raw
    if config_attr is not None:
        setattr(config, config_attr, val)
    print(f"🔬 {title}: {val}", file=sys.stderr)


def _apply_run_hours(argv: list) -> None:
    """Обрабатывает --run-hours N, ставя BULK_RUN_HOURS в окружение."""
    if "--run-hours" not in argv:
        return
    i = argv.index("--run-hours")
    if i + 1 >= len(argv):
        print("❌ --run-hours требует число часов: --run-hours 10", file=sys.stderr)
        raise SystemExit(2)
    raw = str(argv[i + 1]).strip()
    try:
        hours = float(raw)
    except ValueError:
        print(f"❌ --run-hours: '{raw}' не число", file=sys.stderr)
        raise SystemExit(2)
    if hours <= 0:
        print("❌ --run-hours должен быть больше 0 "
              "(без флага — один проход, как раньше)", file=sys.stderr)
        raise SystemExit(2)
    os.environ["BULK_RUN_HOURS"] = raw
    # config прочитал переменные при импорте, поэтому одного окружения мало:
    # без этой строки --run-hours печатался в лог, но sender видел RUN_HOURS=0
    # и делал ровно один круг фаз вместо длинного прогона.
    config.RUN_HOURS = hours
    print(f"⏱️  Длинный прогон: {hours:g} часов от старта "
          f"(BULK_RUN_HOURS={raw})", file=sys.stderr)


USAGE = """bulkmessage — массовая рассылка через Wazzup24

Запуск рассылки:
  py main.py sender                один проход фаз WABA→TG→MAX и выход
  py main.py sender --run-hours 10 один процесс, который сам крутит фазы
                                   10 часов от старта (планировщик не нужен)

Тестовый прогон (сначала 10-20, потом полный объём):
  py main.py sender --waba-limit 20              только 20 сообщений WABA
  py main.py sender --run-hours 2 --waba-limit 20   то же, но на 2 часа

Дневные лимиты (жёсткие, env их не превысит):
  WABA 250/день · Telegram 50/день · MAX 20/день
  Откаты: WABA провалился → TG (50) → MAX (20)

Проверки:
  py main.py migrate             применить миграции схемы БД (идемпотентно)
  py main.py preflight             лимиты, каналы, токены, вебхук
  py main.py report                воронка за сегодня + честные цифры
  py main.py seed-channel-state    разовая миграция вердиктов (проба аккаунтов)
  py main.py tracker               приём входящих вебхуков

Контакты:
  py main.py import-contacts       показать, что будет загружено (ничего не пишет)
  py main.py import-contacts --apply   записать контакты из Excel в базу
                                    повторный запуск добавляет 0 контактов

Отчётность:
  py main.py export-sheets           переписать лист new_messages из базы
  py main.py export-sheets --dry-run показать, что уйдёт в лист

Служебное:
  py main.py reset-state --yes     сбросить broadcast_state.json (квоты дня)

Запуск на сервере:
  bash run.sh                      webхук + рассылка одним движением
  bash run.sh stop                 остановить всё
  bash run.sh status               что сейчас запущено
"""


def main() -> None:
    """Default entry point: runs the sender (back-compat with old single-process mode)."""
    _apply_run_hours(sys.argv[1:])
    _apply_int_flag(sys.argv[1:], "--waba-limit", "BULK_WABA_RUN_LIMIT",
                    "Тестовый прогон: не больше N сообщений WABA за запуск",
                    config_attr="WABA_RUN_LIMIT")
    run_sender()


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(USAGE)
        sys.exit(0)

    mode = sys.argv[1] if len(sys.argv) > 1 else "sender"
    if mode == "sender":
        _apply_run_hours(sys.argv[2:])
        _apply_int_flag(sys.argv[2:], "--waba-limit", "BULK_WABA_RUN_LIMIT",
                        "Тестовый прогон: не больше N сообщений WABA за запуск",
                    config_attr="WABA_RUN_LIMIT")
        run_sender()
    elif mode == "tracker":
        from bulkmessage.tracker import run as run_tracker
        run_tracker()
    elif mode == "preflight":
        from bulkmessage.preflight import run_preflight
        sys.exit(run_preflight())
    elif mode == "migrate":
        # Применение миграций схемы. Идемпотентно: повторный запуск ничего
        # не делает. Боевая работа обязана вызывать это при старте.
        from bulkmessage.store import migrations as _m
        _todo = _m.pending()
        if not _todo:
            print(f"✅ Схема актуальна (версия {_m.current_version()}) — "
                  f"применять нечего.")
            sys.exit(0)
        print(f"🔧 Неприменённые миграции ({len(_todo)}):")
        for v, n in _todo:
            print(f"     {v} — {n}")
        done = _m.migrate()
        print(f"✅ Применено: {', '.join(done)}")
        print(f"   Схема теперь на версии {_m.current_version()}.")
        sys.exit(0)
    elif mode == "import-contacts":
        # Переход с Excel на базу контактов. Без --apply ничего не пишет:
        # повторный импорт поверх истории — единственный способ отправить
        # 4000+ номеров второй раз.
        from bulkmessage.import_contacts import main as run_import
        sys.exit(run_import())
    elif mode == "export-sheets":
        # Переписывает лист new_messages из базы. Идемпотентно: повторный
        # запуск даёт тот же лист, а не задваивает строки.
        from bulkmessage.export_sheets import main as run_export
        sys.exit(run_export())
    elif mode == "report":
        from bulkmessage.report import main as run_report
        sys.exit(run_report())
    elif mode == "seed-channel-state":
        # Разовая миграция: заполняет contact_channel_state вердиктами из
        # уже накопленной истории. Без неё фаза PROBE не знает, кого пробивать,
        # и откат уходит во все неудачные контакты подряд.
        from bulkmessage import db as _db
        _db.init_db()
        flagged = _db.purge_junk_contacts(dry_run=False)
        print(f"🚩 мусорные контакты помечены: "
              f"is_invalid={len(flagged['invalid'])}, is_test={len(flagged['test'])}")
        counts = _db.seed_channel_state_from_history()
        print("🔎 вердикты по каналам:")
        for _row in _db._query_rows(
            "SELECT channel, verdict, COUNT(*) n FROM contact_channel_state "
            "GROUP BY channel, verdict ORDER BY channel, n DESC"
        ):
            print(f"     {_row['channel']:<10} {_row['verdict']:<10} {_row['n']}")
        print(f"   итого: {sum(counts.values())}")
        sys.exit(0)
    elif mode == "reset-state":
        from bulkmessage.preflight import reset_state
        sys.exit(reset_state())
    else:
        print(
            f"Unknown mode: {mode}. Use 'sender'|'tracker'|'preflight'|'report'"
            f"|'migrate'|'import-contacts'|'seed-channel-state'|'reset-state'.",
            file=sys.stderr,
        )
        sys.exit(1)
