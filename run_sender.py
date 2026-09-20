#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
  bulkmessage SENDER — единая точка запуска рассылки
═══════════════════════════════════════════════════════════════

Что делает:
  1. Preflight (проверка конфига, токенов, tier, шаблонов)
  2. Если DRY_RUN=1 — только симуляция, без реальных вызовов
  3. Если DRY_RUN=0 — боевой режим через Wazzup24

Каскад (по умолчанию):
  WABA → Telegram → MAX (один контакт = одно успешное сообщение)

Если WABA templates невалидны — WABA автоматически SKIP,
cascade становится Telegram → MAX.

Запуск:
  py run_sender.py              # DRY_RUN (безопасно)
  py run_sender.py --live       # боевой (НЕ запускай без проверки!)

═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).parent))

from bulkmessage import config, wazzup, waba_templates, db, tglog
from bulkmessage.sender import run as run_sender_background


def print_banner(text: str, char: str = "═") -> None:
    """Красивый баннер."""
    line = char * 63
    print()
    print(line)
    print(f"  {text}")
    print(line)


def check_wazzup_templates() -> dict[str, dict]:
    """Проверить что для всех категорий есть валидные WABA template UUIDs.

    Возвращает {category: {'tid': ..., 'valid': bool}}.
    """
    from bulkmessage.templates import load_templates
    templates_map = load_templates()
    result = {}
    for cat in templates_map.keys():
        tid = waba_templates.pick_waba_template(cat)
        result[cat] = {
            "tid": tid,
            "valid": waba_templates.is_valid_template_id(tid),
        }
    return result


def run_preflight() -> bool:
    """Полная preflight-проверка перед запуском sender."""
    print_banner("PREFLIGHT CHECK")

    ok = True

    # 1. Config
    print(f"   • Timezone: {config.TIMEZONE_NAME}")
    print(f"   • DRY_RUN:  {config.DRY_RUN}")
    print(f"   • USE_WABA: {getattr(config, 'USE_WABA', 0)}")
    print()

    # 2. Wazzup channels
    channels = wazzup.active_channels()
    print(f"   • Активные Wazzup-каналы: {channels or '(пусто)'}")
    if not channels:
        print("   ❌ Нет активных каналов — нечего слать!")
        print("      Проверь .env: WAZZUP_API_KEY + WAZZUP_TG_CHANNEL_ID + WAZZUP_MAX_CHANNEL_ID")
        ok = False
    else:
        for ch in channels:
            cid = wazzup.channel_id_for(ch)
            print(f"      ✅ {ch}: {cid[:8]}…{cid[-4:]}")
    print()

    # 3. WABA templates
    template_status = check_wazzup_templates()
    valid_count = sum(1 for s in template_status.values() if s["valid"])
    total_count = len(template_status)
    print(f"   • WABA templates: {valid_count}/{total_count} категорий валидные")
    for cat, s in template_status.items():
        mark = "✅" if s["valid"] else "❌"
        print(f"      {mark} {cat}: {s['tid'][:20]}…" if s["tid"] else f"      ❌ {cat}: NOT CONFIGURED")
    if valid_count == 0:
        print()
        print("   ⚠️  WABA будет SKIPPED для этой сессии (cascade → TG/MAX).")
        print("   💡 Чтобы включить WABA: узнай UUID одобренных шаблонов")
        print("      в Meta Business Manager → WhatsApp → Message Templates → 'API ID'")
        print("      и пропиши их в .env.local как WABA_TEMPLATE_*_ID")
    elif valid_count < total_count:
        print()
        print(f"   ⚠️  WABA будет SKIPPED в категориях без валидного UUID.")
        print(f"      Плохие категории: {[c for c, s in template_status.items() if not s['valid']]}")
    print()

    # 4. DB
    db.init_db()
    print("   • DB: crm.db инициализирована")
    print()

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Запуск рассылки через Wazzup24")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Боевой режим (по умолчанию — DRY_RUN симуляция)",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Только preflight проверка, без запуска sender",
    )
    args = parser.parse_args()

    # Боевой режим требует явного подтверждения
    if args.live:
        print()
        print("⚠️  ⚠️  ⚠️  БОЕВОЙ РЕЖИМ — реальные сообщения!  ⚠️  ⚠️  ⚠️")
        print()
        confirm = input("Ты уверен что хочешь отправить реальные сообщения? (yes/no): ").strip()
        if confirm.lower() not in ("yes", "y", "да"):
            print("Отменено.")
            return 0
        # Принудительно выключаем DRY_RUN в этом процессе
        os.environ["BULK_DRY_RUN"] = "0"
        config.DRY_RUN = False

    # Preflight (всегда)
    if not run_preflight():
        print()
        print("❌ Preflight FAILED. Sender не запущен.")
        tglog.send("⛔ Sender не запущен: preflight failed", "ERROR")
        return 1

    if args.preflight_only:
        print()
        print("✅ Preflight OK. Sender не запущен (флаг --preflight-only).")
        return 0

    # Запускаем sender
    print_banner("LAUNCHING SENDER", "─")
    if config.DRY_RUN:
        print("   🧪 DRY_RUN MODE — только симуляция, реальных отправок не будет")
    else:
        print("   🚀 LIVE MODE — реальные отправки через Wazzup")
    print()

    try:
        run_sender_background()
    except KeyboardInterrupt:
        print()
        print("⏹  Sender остановлен пользователем (Ctrl+C)")
    return 0


if __name__ == "__main__":
    sys.exit(main())