"""Smoke test: проверить, что TG-бот может писать админу.

Когда запускать:
  1. После первого /start в боте — проверить, что связь работает.
  2. После revoke токена в @BotFather — проверить новый токен.
  3. Если уведомления перестали приходить — диагностика.

Запуск:
  py test_tg_log.py
"""
import sys

sys.path.insert(0, ".")

from bulkmessage import config, tglog


def main() -> int:
    print("=" * 70)
    print(" TG LOG SMOKE TEST")
    print("=" * 70)
    print()
    print(f"  TG_LOG_ENABLED:   {config.TG_LOG_ENABLED}")
    print(f"  BOT_TOKEN:        {config.TG_LOG_BOT_TOKEN[:20]}…"
          if config.TG_LOG_BOT_TOKEN else "  BOT_TOKEN:        (пусто)")
    print(f"  ADMIN_ID:         {config.TG_LOG_ADMIN_ID}")
    print(f"  TG_LOG_LEVEL:     {config.TG_LOG_LEVEL}")
    print(f"  SEND_INTERVAL:    {config.TG_LOG_SEND_INTERVAL}с")
    print()

    if not config.TG_LOG_ENABLED:
        print("❌ TG_LOG_DISABLED — заполни BULK_TG_LOG_BOT_TOKEN и BULK_TG_LOG_ADMIN_ID в .env.local")
        return 1

    # 1. getMe
    print("─" * 70)
    print("Step 1: getMe() — проверяем что токен валиден")
    print("─" * 70)
    me = tglog.get_me()
    if me is None:
        print("❌ getMe() failed — токен невалиден или сеть недоступна")
        return 1
    print(f"✅ Бот: id={me.get('id')} name={me.get('first_name')} "
          f"username=@{me.get('username')}")
    print(f"   Прямая ссылка: https://t.me/{me.get('username')}")
    print()

    # 2. sendMessage
    print("─" * 70)
    print("Step 2: sendMessage() — проверяем что бот может писать админу")
    print("─" * 70)
    result = tglog.send_test()
    if result is None:
        print("❌ sendMessage() failed")
        print()
        print("Возможные причины:")
        print(f"  1) Admin {config.TG_LOG_ADMIN_ID} не нажал /start в боте.")
        print(f"     Открой https://t.me/{me.get('username')} и нажми Start.")
        print("  2) Admin ID неверный (перепутал с username?).")
        print("     Чтобы узнать свой ID, напиши @userinfobot или @RawDataBot.")
        print("  3) Бот заблокирован админом.")
        return 1

    msg = result.get("result", {})
    chat = msg.get("chat", {})
    print(f"✅ Сообщение отправлено:")
    print(f"   message_id: {msg.get('message_id')}")
    print(f"   chat.id:    {chat.get('id')}")
    print(f"   chat.type:  {chat.get('type')}")
    print()
    print("─" * 70)
    print("✅ Всё работает! Проверь Telegram — должно прийти тестовое сообщение.")
    print("─" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
