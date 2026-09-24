#!/usr/bin/env python3
"""
Тест cascade: один номер → WABA → (fail) → TG → (fail) → MAX.

Запуск:
    py test_cascade.py +998941250709
    py test_cascade.py +998941250709 --dry-run

Без --dry-run: реальные отправки через Wazzup.
С --dry-run: имитация (BULK_DRY_RUN=1 на момент запуска).

Показывает каждый шаг cascade: какой канал пробовали, что получил, какой
следующий, и какой в итоге сработал (или все failed).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Загружаем .env ДО импорта bulkmessage.config
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        if "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

sys.path.insert(0, ".")

from bulkmessage import config, db, sender, state, wazzup  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Тест cascade на одном номере")
    p.add_argument("phone", help="Тестовый номер, например +998941250709")
    p.add_argument("--dry-run", action="store_true",
                   help="Не отправлять реально (BULK_DRY_RUN=1)")
    p.add_argument("--name", default="Тест", help="Имя контакта")
    p.add_argument("--category", default="Покупатели",
                   help="Категория для шаблона")
    args = p.parse_args()

    if args.dry_run:
        os.environ["BULK_DRY_RUN"] = "1"
        # Force reload of config.DRY_RUN
        config.DRY_RUN = True
        print("🧪 DRY-RUN — реальных отправок не будет")
    else:
        if config.DRY_RUN:
            print("⚠️  .env говорит BULK_DRY_RUN=1 — переключи на 0 для реальной отправки")
            print("    Или запусти без --dry-run после sed -i 's/BULK_DRY_RUN=1/BULK_DRY_RUN=0/' .env.local")
            return 1
        print("🚀 LIVE — реальные отправки через Wazzup")

    config.configure_logging()
    log = config.get_logger("test_cascade")
    db.init_db()

    phone = wazzup.normalize_phone(args.phone)
    if not phone:
        print(f"[ERR] Невалидный phone: {args.phone!r}")
        return 1

    print()
    print("=" * 70)
    print(f"  Тест cascade: {phone}")
    print(f"  Имя: {args.name}")
    print(f"  Категория: {args.category}")
    print("=" * 70)
    print()

    # Загружаем state и сбрасываем дневные счётчики (для теста)
    current_state = state.load_state()
    current_state = state.reset_daily_if_new_day(current_state)

    # Показываем что в начале
    print(f"📊 Quotas до теста:")
    for ch in ("waba", "telegram", "max"):
        sent = state.channel_sent_today(current_state, ch)
        cap = state.channel_effective_cap(current_state, ch)
        print(f"   {ch:10s}: {sent}/{cap}")

    # Контакт
    contact = {"phone": phone, "name": args.name, "category": args.category}

    # Cascade channels (тестируем все 3)
    channels = wazzup.active_channels()

    print()
    print(f"🔄 Запускаю cascade: {channels}")
    print("─" * 70)

    sent_ok = sender._run_one_contact(
        contact,
        channels,
        {},                # backoffs
        {},                # permanent_skipped
        current_state,
    )

    print("─" * 70)
    print()
    if sent_ok:
        successful = state.successful_today(current_state)
        target = current_state.get("target_today", config.TARGET_SUCCESS_PER_DAY)
        print(f"✅ CASCADE SUCCESS: доставлено в {current_state.get('last_success_channel', '?')}")
        print(f"   Прогресс: {successful}/{target}")
    else:
        print("❌ CASCADE FAILED: ни один канал не сработал")

    print()
    print(f"📊 Quotas после теста:")
    for ch in ("waba", "telegram", "max"):
        sent = state.channel_sent_today(current_state, ch)
        cap = state.channel_effective_cap(current_state, ch)
        print(f"   {ch:10s}: {sent}/{cap}")

    print()
    print("=" * 70)
    print(f" Итог для {phone}:")
    print(f"   • last_success_channel = {current_state.get('last_success_channel', '(none)')}")
    if sent_ok:
        print(f"   • доставлено ✅")
    else:
        print(f"   • не доставлено ❌")
        print(f"   • попробуй py main.py preflight — может WABA template не одобрен?")
    print("=" * 70)

    return 0 if sent_ok else 1


if __name__ == "__main__":
    sys.exit(main())