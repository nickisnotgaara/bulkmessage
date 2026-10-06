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

from . import config, db, wazzup, waba_templates, state, sms_ru, templates


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
            # Потолок личных каналов ПЕР-КАНАЛЬНЫЙ (TG 50, MAX 15). Раньше здесь
            # стоял общий MAX_PERSONAL_DAILY_HARD_CAP=30, и preflight ставил ❌
            # на telegram=50 — красный статус, хотя лимит выставлен корректно.
            personal_cap = getattr(
                config, "PERSONAL_CHANNEL_HARD_CAPS", {}
            ).get(ch, config.MAX_PERSONAL_DAILY_HARD_CAP)
            all_ok &= _check(
                f"  {ch}",
                cap <= personal_cap if ch in ("telegram", "max") else True,
                f"hard_cap={cap} (потолок {personal_cap}), "
                f"effective_with_headroom={headroom_cap}",
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

    # 3.5 SMS.RU fallback (отдельный транспорт, НЕ Wazzup)
    if config.SMSRU_ENABLED:
        print("\n── 3.5 SMS.RU FALLBACK (sms.ru) ──")
        all_ok &= _check(
            "BULK_SMSRU_API_ID",
            bool(config.SMSRU_API_ID),
            f"set ({len(config.SMSRU_API_ID)} chars, значение скрыто)"
            if config.SMSRU_API_ID
            else "EMPTY! (SMS.RU ответит ошибкой 200)",
        )
        sender_name = config.SMSRU_SENDER_NAME
        name_ok = bool(sender_name) and sender_name.isalpha() and 3 <= len(sender_name) <= 11
        all_ok &= _check(
            "BULK_SMSRU_SENDER_NAME",
            name_ok,
            f"{sender_name!r} (лат. буквы, 3-11 символов)"
            if sender_name
            else "EMPTY! (SMS.RU ответит 221 — нужен зарегистрированный отправитель)",
        )
        sms_tpls = templates.load_sms_templates()
        all_ok &= _check(
            "SMS шаблоны",
            bool(sms_tpls),
            f"{len(sms_tpls)} категорий из {config.SMSRU_TEMPLATES_PATH}",
        )
        # Аудит длины: SMS тарифицируется по сегментам (70 символов = сегмент).
        budget = min(config.SMSRU_MAX_SEGMENTS, sms_ru.SMSRU_HARD_MAX_SEGMENTS)
        if sms_tpls:
            for cat in sms_tpls:
                msg = templates.build_sms_message(
                    {"name": "Тест", "category": cat}, sms_tpls
                )
                segs = sms_ru.estimate_segments(msg)
                all_ok &= _check(
                    f"  SMS «{cat}»",
                    segs <= budget,
                    f"{len(msg)} симв. = {segs} сегм. (~{segs * 8.5:.1f} ₽), "
                    f"бюджет {budget} сегм.",
                )
        if config.SMSRU_TEST_MODE:
            print("  ℹ️  BULK_SMSRU_TEST_MODE=1 — ко всем вызовам добавляется test=1")
        else:
            print("  ℹ️  BULK_SMSRU_TEST_MODE=0 — SMS.RU будет слать РЕАЛЬНЫЕ SMS")
        # Реальный потолок аккаунта SMS.RU. Без этого запроса приходится
        # гадать лимит, а потом discover'ить его через код 206 в середине
        # rescue-очереди. Схема «WABA + хвост в SMS» держится целиком на нём.
        try:
            _lok, _total, _used, _ldet = sms_ru.get_daily_limit()
            if _lok:
                _rem = max(0, _total - _used)
                all_ok &= _check(
                    "SMS.RU дневной лимит",
                    _total >= state.channel_effective_cap(state.load_state(), "sms"),
                    f"использовано {_used} из {_total}, осталось {_rem}",
                )
            else:
                _check("SMS.RU дневной лимит", False, _ldet)
        except Exception as e:  # pragma: no cover - defensive
            _check("SMS.RU дневной лимит", False, f"exception: {e}")
        if not config.DRY_RUN and config.SMSRU_API_ID and name_ok:
            # test=1: SMS.RU ничего не отправляет и не списывает баланс.
            print("  … проверка связи с SMS.RU (test=1, без трат)…")
            try:
                _ok, _mid, _detail, _code = sms_ru.send_sms(
                    config.SMSRU_TEST_PHONE, "preflight", test=True
                )
                all_ok &= _check(
                    "SMS.RU test=1 smoke",
                    _ok,
                    f"{_code}: {_detail[:70]}",
                )
            except Exception as e:  # pragma: no cover - defensive
                all_ok &= _check("SMS.RU test=1 smoke", False, f"exception: {e}")
    else:
        print("\n── 3.5 SMS.RU FALLBACK (sms.ru) ──")
        print("  ⏭️  выключен (BULK_SMSRU_ENABLED=0) — каскад без SMS")

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

    # 4.1 Инвентаризация каналов Wazzup.
    # В Wazzup «канал» = один аккаунт мессенджера, и оплата идёт за КАЖДЫЙ
    # канал отдельно. Показываем, сколько каналов подключено и сколько из
    # них задействовано конфигом: bulkmessage адресует ровно один channelId на
    # канал (config.get_wazzup_channel_id_for), поэтому «второй аккаунт» в
    # текущем коде адресовать нечем.
    # Запрос GET /v3/channels ничего не отправляет, поэтому он НЕ под гейтом
    # DRY_RUN — иначе инвентаризация была бы не видна ровно на preflight,
    # который запускают перед боевым стартом.
    if use_waba and wazzup.is_configured():
        print("\n── 4.1 КАНАЛЫ WAZZUP (инвентаризация) ──")
        try:
            _ok, _detail, _ch = wazzup.get_wazzup_channels()
            if not _ok:
                _check("инвентаризация каналов", False, _detail[:80])
            else:
                _live = set()

                def _ids(node):
                    if isinstance(node, dict):
                        cid = node.get("channelId")
                        if isinstance(cid, str) and cid:
                            _live.add(cid)
                        for v in node.values():
                            _ids(v)
                    elif isinstance(node, list):
                        for v in node:
                            _ids(v)

                _ids(_ch)
                _used = {wazzup.channel_id_for(c) for c in active}
                _used.discard("")
                _unused = sorted(_live - _used)
                _check("каналов подключено", True,
                       f"всего {len(_live)}, задействовано {len(_used)}, "
                       f"не используется {len(_unused)}")
                if _unused:
                    print("      " + ", ".join(u[:8] + "…" for u in _unused[:10])
                          + (" …" if len(_unused) > 10 else ""))
                    print("      ⚠️  каждый канал оплачивается отдельно; эти "
                          "оплачены, но адресовать их нечем — код знает "
                          "ровно 1 channelId на канал")
        except Exception as e:  # pragma: no cover - defensive
            _check("инвентаризация каналов", False, f"exception: {e}"[:80])

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
