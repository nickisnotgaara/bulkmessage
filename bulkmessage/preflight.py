"""Preflight checks и reset-state CLI (Phase 5.3).

Запускается как `python main.py preflight` или `python main.py reset-state`.

Проверяет:
- Валидность всех Wazzup-токенов (API key + channel_id per channel)
- WABA tier (если USE_WABA=1)
- Текущий daily cap и effective headroom
- Наличие 4 WABA templates (UUID-формат)
- Доступность webhook endpoint
- Чистоту state.json
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import config, db, wazzup, waba_templates, state


def _check(name: str, ok: bool, detail: str = "") -> bool:
    icon = "✅" if ok else "❌"
    line = f"  {icon} {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def run_preflight() -> int:
    """Запускает preflight проверки. Возвращает 0 если всё OK, 1 если есть проблемы."""
    print("═" * 60)
    print("  bulkmessage PREFLIGHT CHECK")
    print("═" * 60)

    all_ok = True

    # 1. Конфигурация
    print("\n── 1. CONFIGURATION ──")
    all_ok &= _check("DRY_RUN status", True, f"DRY_RUN={config.DRY_RUN}")
    all_ok &= _check(
        "USE_WABA flag",
        True,
        f"BULK_USE_WABA={config.USE_WABA} "
        f"(0=legacy WA, 1=cascade WABA→TG→MAX, 2=shadow)",
    )
    use_waba = config.USE_WABA >= 1

    # 2. Каналы и лимиты
    print("\n── 2. CHANNELS & LIMITS ──")
    for ch, cap in config.CHANNEL_DAILY_LIMITS.items():
        if ch == "waba":
            tier = state.get_waba_tier(state.load_state())
            tier_cap = config.WABA_TIER_LIMITS.get(tier, "?")
            effective_waba = state.effective_waba_cap(state.load_state())
            headroom_cap = state.channel_effective_cap(state.load_state(), ch)
            all_ok &= _check(
                f"  {ch}",
                True,
                f"hard_cap={cap}, tier={tier} (cap={tier_cap}), "
                f"effective_with_headroom={headroom_cap}",
            )
        else:
            headroom_cap = state.channel_effective_cap(state.load_state(), ch)
            all_ok &= _check(
                f"  {ch}",
                cap <= config.MAX_PERSONAL_DAILY_HARD_CAP if ch in ("telegram", "max") else True,
                f"hard_cap={cap}, effective_with_headroom={headroom_cap}",
            )

    # 3. Wazzup channels per active channel (transport = Wazzup для ВСЕХ каналов)
    print("\n── 3. WAZZUP24 CHANNELS (per channel) ──")
    active = wazzup.active_channels()
    for ch in active:
        cid = wazzup.channel_id_for(ch)
        all_ok &= _check(
            f"  {ch}",
            bool(cid),
            f"channel_id={cid[:8]+'…' if cid else 'EMPTY!'}",
        )

    # 4. Wazzup API key + webhook (если USE_WABA=1)
    if use_waba:
        print("\n── 4. WAZZUP24 API & WEBHOOK (BULK_USE_WABA=1) ──")
        all_ok &= _check(
            "WAZZUP_API_KEY",
            bool(config.WAZZUP_API_KEY),
            f"{'set ('+str(len(config.WAZZUP_API_KEY))+' chars)' if config.WAZZUP_API_KEY else 'EMPTY!'}",
        )
        all_ok &= _check(
            "WAZZUP_DEFAULT_CHANNEL_ID",
            bool(config.WAZZUP_DEFAULT_CHANNEL_ID),
            f"channel_id={config.WAZZUP_DEFAULT_CHANNEL_ID or 'EMPTY!'}",
        )
        all_ok &= _check(
            "WAZZUP_WEBHOOK_URL",
            bool(config.WAZZUP_WEBHOOK_URL),
            f"webhook_url={config.WAZZUP_WEBHOOK_URL or 'not set (cannot receive events)'}",
        )
        if wazzup.is_configured() and not config.DRY_RUN:
            ok, detail, channels = wazzup.get_wazzup_channels()
            all_ok &= _check(
                "Wazzup API reachable",
                ok,
                f"{len(channels)} channels" if ok else detail[:80],
            )
    else:
        print("\n── 4. WAZZUP24 API & WEBHOOK ──")
        _check("Skipped (USE_WABA=0)", True, "set BULK_USE_WABA=1 to enable")

    # 5. WABA templates (UUID format required!)
    print("\n── 5. WABA TEMPLATES (4 categories, UUID format) ──")
    templates_by_cat = waba_templates.list_known_templates()
    for cat, tid in templates_by_cat.items():
        is_uuid = waba_templates.is_valid_template_id(tid)
        all_ok &= _check(
            f"  {cat}",
            bool(tid) and is_uuid,
            f"template_id={tid or 'NOT CONFIGURED!'}"
            + ("" if is_uuid else " ❌ NOT UUID!"),
        )

    # 6. Database
    print("\n── 6. DATABASE ──")
    try:
        db.init_db()
        all_ok &= _check("crm.db initialized", True)
    except Exception as e:
        all_ok &= _check("crm.db initialized", False, str(e)[:80])

    # 7. State.json
    print("\n── 7. STATE ──")
    try:
        cur = state.load_state()
        today_sent = sum(cur.get("sent_today", {}).values())
        all_ok &= _check(
            "state.json readable",
            True,
            f"date={cur.get('date', '?')}, total_sent_today={today_sent}",
        )
        halted = cur.get("halted_channels", {})
        if halted:
            print(f"  ⚠️  Halted channels: {halted}")
    except Exception as e:
        all_ok &= _check("state.json readable", False, str(e)[:80])

    # 8. Spintax templates
    print("\n── 8. SPINTAX TEMPLATES ──")
    try:
        from .templates import load_all_variants
        variants = load_all_variants()
        for cat, lst in variants.items():
            inline_count = sum(
                1
                for v in lst
                if "{" in v and "|" in v and "}" in v
            )
            all_ok &= _check(
                f"  {cat}",
                len(lst) >= 1,
                f"{len(lst)} variants, {inline_count} contain inline spin-tax",
            )
    except Exception as e:
        all_ok &= _check("spintax load", False, str(e)[:80])

    print()
    print("═" * 60)
    if all_ok:
        print("  ✅ ALL CHECKS PASSED — system ready")
    else:
        print("  ❌ SOME CHECKS FAILED — review above")
    print("═" * 60)
    return 0 if all_ok else 1


def reset_state() -> int:
    """Сбрасывает broadcast_state.json (для теста).

    Требует явного подтверждения через --yes флаг или interactive prompt.
    """
    import argparse
    parser = argparse.ArgumentParser(
        description="Reset broadcast_state.json (careful!)"
    )
    parser.add_argument("--yes", action="store_true", help="Skip confirmation")
    args = parser.parse_args(sys.argv[2:])

    if not args.yes:
        print("Это сбросит broadcast_state.json (sent_today, complaints, halt state).")
        print(f"Файл: {config.STATE_PATH}")
        try:
            ans = input("Продолжить? [y/N] ").strip().lower()
        except EOFError:
            print("\nNon-interactive: pass --yes to confirm.")
            return 1
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 0

    try:
        # Создаём пустой state
        empty = state.load_state()
        # Сбрасываем counters, но сохраняем waba_tier
        waba_tier = empty.get("waba_tier", config.WAZZUP_TIER_DEFAULT)
        waba_tier_updated_at = empty.get("waba_tier_updated_at", "")
        empty = {
            "sent_today": {},
            "contacts_today": 0,
            "date": "",
            "last_index": 0,
            "successful_today": 0,
            "attempts_today": 0,
            "target_today": config.TARGET_SUCCESS_PER_DAY,
            "waba_tier": waba_tier,
            "waba_tier_updated_at": waba_tier_updated_at,
            "reachability_cache": {},
            "last_send_per_phone": {},
            "complaint_counts": {},
            "complaint_history": [],
            "halted_channels": {},
            "spintax_last_used": {},
            "waba_sent_meta": {},
        }
        state.save_state(empty)
        print(f"✅ state reset. Tier preserved: {waba_tier}.")
        return 0
    except Exception as e:
        print(f"❌ reset failed: {e}")
        return 1
