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
                    minimum: int = 1) -> None:
    """Разбирает флаг вида --flag N и кладёт число в os.environ[env_key].

    Флаги нужны для удобства запуска, но config читает переменные при
    импорте, а sender импортируется в самом верху файла. Поэтому ставим их
    в окружение ДО вызова sender: загрузчик .env не перезаписывает непустые
    значения из os.environ, так флаги работают без правки .env.local.
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
  WABA 250/день · Telegram 50/день · MAX 15/день
  Откаты: WABA провалился → TG (50) → MAX (15)

Проверки:
  py main.py preflight             лимиты, каналы, токены, вебхук
  py main.py report                воронка за сегодня
  py main.py tracker               приём входящих вебхуков

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
                    "Тестовый прогон: не больше N сообщений WABA за запуск")
    run_sender()


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(USAGE)
        sys.exit(0)

    mode = sys.argv[1] if len(sys.argv) > 1 else "sender"
    if mode == "sender":
        _apply_run_hours(sys.argv[2:])
        _apply_int_flag(sys.argv[2:], "--waba-limit", "BULK_WABA_RUN_LIMIT",
                        "Тестовый прогон: не больше N сообщений WABA за запуск")
        run_sender()
    elif mode == "tracker":
        from bulkmessage.tracker import run as run_tracker
        run_tracker()
    elif mode == "preflight":
        from bulkmessage.preflight import run_preflight
        sys.exit(run_preflight())
    elif mode == "report":
        from bulkmessage.report import main as run_report
        sys.exit(run_report())
    elif mode == "reset-state":
        from bulkmessage.preflight import reset_state
        sys.exit(reset_state())
    else:
        print(
            f"Unknown mode: {mode}. Use 'sender'|'tracker'|'preflight'|'report'|'reset-state'.",
            file=sys.stderr,
        )
        sys.exit(1)
