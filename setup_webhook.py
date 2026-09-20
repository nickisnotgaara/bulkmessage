#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
  bulkmessage WAZZUP WEBHOOK SUBSCRIBER
═══════════════════════════════════════════════════════════════

Подписывает Wazzup24 на webhook для приёма:
  - message.add (входящие от клиентов)
  - message.status_update (delivered/read/error)
  - waba_template.status_update (UUID одобренных шаблонов)
  - channel.waba_tier_update (повышение tier)

Использование:
  # Если используешь Cloudflare quick tunnel:
  py setup_webhook.py --url https://abc.trycloudflare.app/webhook

  # Если используешь named tunnel (prod):
  py setup_webhook.py --url https://webhook.your-domain.com/webhook

  # Проверить текущую подписку:
  py setup_webhook.py --show

═══════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bulkmessage import config, wazzup


def show_current():
    """Показать текущую подписку Wazzup."""
    print("📡 Текущая подписка Wazzup:")
    print()
    # GET /v3/webhooks
    import requests
    headers = wazzup._headers()
    if not config.WAZZUP_API_KEY:
        print("❌ WAZZUP_API_KEY не задан")
        return 1

    url = f"{config.WAZZUP_BASE_URL}{config.WAZZUP_WEBHOOKS_PATH}"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            import json
            data = r.json()
            print(json.dumps(data, indent=2, ensure_ascii=False))
            return 0
        else:
            print(f"❌ HTTP {r.status_code}: {r.text[:300]}")
            return 1
    except Exception as e:
        print(f"❌ Ошибка запроса: {e}")
        return 1


def subscribe(url: str):
    """Подписать Wazzup на новый webhook URL."""
    if not url.startswith("https://"):
        print("❌ URL должен быть HTTPS (Wazzup требует TLS)")
        return 1

    print(f"📡 Подписываю Wazzup на: {url}")
    print()

    subscriptions = {
        "messagesAndStatuses": True,        # входящие сообщения + статусы доставки
        "contactsAndDealsCreation": False,  # auto-create contacts/deals (нам не нужно)
        "channelsUpdates": True,            # смена статуса канала (для halt logic)
        "templateStatus": True,             # результат модерации шаблона (для UUID)
    }

    ok, detail = wazzup.subscribe_wazzup_webhook(url, subscriptions)
    if ok:
        print(f"✅ Подписка успешна!")
        print()
        print(f"   URL:              {url}")
        print(f"   messagesAndStatuses:    {subscriptions['messagesAndStatuses']}")
        print(f"   channelsUpdates:        {subscriptions['channelsUpdates']}")
        print(f"   templateStatus:         {subscriptions['templateStatus']}")
        print()
        print("⚠️  Wazzup сразу пошлёт первый POST (test event). Убедись что:")
        print(f"   • webhook сервер запущен: py run_webhook.py")
        print(f"   • Cloudflare tunnel запущен: py run_webhook.py --tunnel")
        print(f"   • порт 8000 доступен")
        return 0
    else:
        print(f"❌ Подписка не удалась: {detail}")
        return 1


def main():
    parser = argparse.ArgumentParser(description="Управление Wazzup webhook подпиской")
    parser.add_argument(
        "--url",
        help="HTTPS URL для webhook (например, https://abc.trycloudflare.app/webhook)",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Показать текущую подписку",
    )
    args = parser.parse_args()

    if args.show:
        return show_current()
    if not args.url:
        parser.print_help()
        print()
        print("ℹ️  Чтобы получить trycloudflare URL:")
        print("   1. Запусти: py run_webhook.py --tunnel")
        print("   2. В логе cloudflared появится URL вида:")
        print("      https://<random>.trycloudflare.app")
        print("   3. Запусти: py setup_webhook.py --url <этот URL>/webhook")
        return 0

    return subscribe(args.url)


if __name__ == "__main__":
    sys.exit(main())