#!/usr/bin/env python3
"""
Тест cascade с принудительным WABA FAIL — показывает что TG/MAX пробуются.

Использует monkey-patch wazzup.send_to_channel для WABA чтобы симулировать
"номер не существует / шаблон не разрешён".

Это DRY-RUN для WABA и LIVE для TG/MAX — если хочешь проверить реальные
отправки в Personal каналы, нужно подставить свой номер где есть TG/MAX.

Запуск:
    py test_cascade_sim.py +998941250709
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
    p = argparse.ArgumentParser(description="Симуляция WABA fail для теста cascade")
    p.add_argument("phone", help="Тестовый номер")
    p.add_argument("--name", default="CascadeSim", help="Имя контакта")
    p.add_argument("--category", default="Покупатели",
                   help="Категория для шаблона")
    args = p.parse_args()

    config.configure_logging()
    log = config.get_logger("test_cascade_sim")
    db.init_db()

    phone = wazzup.normalize_phone(args.phone)
    if not phone:
        print(f"[ERR] Невалидный phone: {args.phone!r}")
        return 1

    print()
    print("=" * 70)
    print(f"  СИМУЛЯЦИЯ WABA FAIL: {phone}")
    print("=" * 70)
    print()
    print("⚠️  WABA будет принудительно fail'ить (как будто номер")
    print("    не существует или template не разрешён).")
    print("    Cascade должен попробовать TG, потом MAX.")
    print()

    # === MONKEY-PATCH WABA + force DRY_RUN=0 ===
    # WABA path: wazzup.send_wazzup (для template И free-text)
    # Personal path: wazzup.send_to_channel
    _original_send_wazzup = wazzup.send_wazzup
    _original_send_to_channel = wazzup.send_to_channel

    def _waba_fake_send_wazzup(**kwargs):
        # Fail ТОЛЬКО для WABA (chat_type=whatsapp). TG/MAX — реальные.
        chat_type = kwargs.get("chat_type", "")
        if chat_type != "whatsapp":
            return _original_send_wazzup(**kwargs)
        log.warning(
            f"🧪 SIMULATED WABA FAIL для {kwargs.get('chat_id', '?')} "
            f"(template rejected / number not on WA)"
        )
        return False, None, "SIMULATED: number not on WhatsApp or template rejected", 403

    def _personal_passthrough(channel, phone, text, **kwargs):
        return _original_send_to_channel(
            channel=channel, phone=phone, text=text, **kwargs
        )

    wazzup.send_wazzup = _waba_fake_send_wazzup
    wazzup.send_to_channel = _personal_passthrough
    sender.wazzup.send_wazzup = _waba_fake_send_wazzup
    sender.wazzup.send_to_channel = _personal_passthrough

    # Force DRY_RUN=0 чтобы WABA branch вызывал send_wazzup (без short-circuit)
    original_dry_run = config.DRY_RUN
    config.DRY_RUN = False

    # === Очищаем cooldown для теста ===
    cur = state.load_state()
    cur = state.reset_daily_if_new_day(cur)
    cur.get("last_send_per_phone", {}).pop(phone, None)
    state.save_state(cur)

    # === Запускаем cascade ===
    contact = {"phone": phone, "name": args.name, "category": args.category}
    channels = ["waba", "telegram", "max"]

    print(f"🔄 Cascade: {channels}")
    print("─" * 70)

    # backoffs dict должен содержать все channels (sender читает backoffs[ch])
    backoffs = {ch: 0 for ch in channels}
    permanent_skipped: dict[str, set[str]] = {}

    try:
        sent_ok = sender._run_one_contact(
            contact, channels, backoffs, permanent_skipped, cur
        )
    finally:
        # Восстанавливаем оригинал
        wazzup.send_wazzup = _original_send_wazzup
        wazzup.send_to_channel = _original_send_to_channel
        sender.wazzup.send_wazzup = _original_send_wazzup
        sender.wazzup.send_to_channel = _original_send_to_channel
        config.DRY_RUN = original_dry_run

    print("─" * 70)
    print()
    if sent_ok:
        ch = cur.get("last_success_channel", "?")
        print(f"✅ CASCADE SUCCESS через {ch}")
    else:
        print("❌ CASCADE FAILED: все 3 канала не сработали")
        print("   (если WABA fail'нул а TG/MAX не пробовались — баг в cascade)")

    print()
    print("=" * 70)
    print(f" Итог для {phone}:")
    print(f"   • last_success_channel = {cur.get('last_success_channel', '(none)')}")
    print(f"   • sent_today           = {cur.get('sent_today', {})}")
    print("=" * 70)

    return 0 if sent_ok else 1


if __name__ == "__main__":
    sys.exit(main())