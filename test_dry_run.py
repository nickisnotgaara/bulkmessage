"""
DRY-RUN СИМУЛЯЦИЯ РАССЫЛКИ (без единого реального вызова).

Логика (после ребрендинга 22.08.2026):
  • Каждый контакт → мгновенно во ВСЕ активные каналы (WA+TG+MAX).
  • Между контактами случайная пауза 5-15 мин.
  • Отправляем только в активном окне (BULK_ACTIVE_HOURS_START..END) в TZ МСК.
  • Вне окна — ждём до его начала.
  • Дневной лимит = 60 на КАНАЛ (= 60 контактов × 3 канала = 180 сообщений).

Что делает:
  • Грузит конфиг, контакты из Excel, шаблоны из Message_script.md.
  • Прокручивает цикл В ПАМЯТИ — никакого HTTP, никаких записей в БД / Sheets.
  • Генерирует расписание (время каждой отправки) в data/dryrun_schedule.json.
  • Проверяет: укладывается ли в активное окно, не превышает ли квоты.

НЕ ЗАПУСКАЕТ РАССЫЛКУ. Ничего реально не отправляет.

Запуск:
  python test_dry_run.py
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

from bulkmessage import config, contacts, db, templates, wappi
from bulkmessage.sender import _fmt_duration, _estimate_total_time


def _start_in_active_window() -> datetime:
    """Возвращает время старта симуляции в настроенной TZ.

    Если сейчас вне активного окна — сдвигает на начало окна.
    """
    now = config.now_tz()
    if config.is_within_active_hours(now):
        return now
    # Передвигаем на начало сегодняшнего окна
    start = now.replace(
        hour=config.ACTIVE_HOURS_START, minute=0, second=0, microsecond=0
    )
    if start <= now:
        # начало окна сегодня уже прошло → значит мы "после" окна → берём завтра
        start = start + timedelta(days=1)
    return start


def _seconds_until_window_end(current: datetime) -> float:
    """Сколько секунд осталось до конца активного окна."""
    end = current.replace(
        hour=config.ACTIVE_HOURS_END, minute=0, second=0, microsecond=0
    )
    if end <= current:
        return 0.0
    return (end - current).total_seconds()


def simulate_broadcast(seed: int = 42) -> dict:
    """Симулирует цикл рассылки в памяти. Возвращает статистику + расписание."""
    random.seed(seed)
    start = _start_in_active_window()

    templates_map = templates.load_templates()
    channels = wappi.active_channels()
    if not channels:
        return {"error": "Нет активных каналов (проверьте WAPPI_*_TOKEN в .env.local)"}

    # Контакты
    try:
        all_contacts = contacts.load_contacts(config.EXCEL_PATH)
    except FileNotFoundError:
        return {"error": f"Файл контактов не найден: {config.EXCEL_PATH}"}

    # Кто уже был отправлен (read-only)
    db.init_db()
    with db.db_conn() as conn:
        sent_phones = db.get_sent_phones(conn)

    # Фильтр по 4 целевым категориям
    contacts_to_send = [
        c for c in all_contacts
        if c["phone"] and c["phone"] not in sent_phones
    ]
    filtered: list[dict] = []
    skipped_by_cat: dict[str, int] = {}
    for c in contacts_to_send:
        cat_raw = (c.get("category") or "").strip() or "(пусто)"
        norm = templates.normalize_category(cat_raw)
        if norm in config.ALLOWED_CATEGORIES:
            c["_normalized_category"] = norm
            filtered.append(c)
        else:
            skipped_by_cat[cat_raw] = skipped_by_cat.get(cat_raw, 0) + 1

    # Симуляция
    sent_count: dict[str, int] = {ch: 0 for ch in channels}
    sent_contacts: set[str] = set()
    current = start
    schedule: list[dict] = []
    window_closures: list[dict] = []  # моменты, когда окно закрылось
    sleeps_outside_window: list[dict] = []

    for contact in filtered:
        # Проверяем: все ли каналы достигли дневного лимита?
        if all(
            sent_count[ch] >= config.CHANNEL_DAILY_LIMITS.get(ch, 0)
            for ch in channels
        ):
            break

        # Проверяем: мы в активном окне?
        if not config.is_within_active_hours(current):
            wait_secs = config.seconds_until_active_window(current)
            sleeps_outside_window.append({
                "from": current.isoformat(timespec="seconds"),
                "wait_hours": round(wait_secs / 3600, 2),
            })
            current = current + timedelta(seconds=wait_secs)

        # Проверяем: успеем ли мы в окно? (опционально — стоп если конец окна близко)
        remaining = _seconds_until_window_end(current)
        if remaining <= 0:
            window_closures.append({
                "at": current.isoformat(timespec="seconds"),
                "reason": "достигли конца окна до отправки",
            })
            break

        # Готовим текст
        text = templates.build_message(contact, templates_map)

        # Отправляем во все каналы сразу
        for ch in channels:
            if sent_count[ch] >= config.CHANNEL_DAILY_LIMITS.get(ch, 0):
                continue
            sent_count[ch] += 1
            schedule.append({
                "n_msg": sum(sent_count.values()),
                "n_contact": len(sent_contacts) + 1,
                "ts": current.isoformat(timespec="seconds"),
                "channel": ch,
                "phone": contact["phone"],
                "name": contact.get("name", ""),
                "category": contact.get("_normalized_category", ""),
                "in_channel": sent_count[ch],
                "limit": config.CHANNEL_DAILY_LIMITS[ch],
            })

        sent_contacts.add(contact["phone"])

        # Пауза до следующего контакта
        delay = random.uniform(config.DELAY_MIN, config.DELAY_MAX)
        if random.random() < 0.10:
            delay += random.uniform(30, 90)
        current = current + timedelta(seconds=delay)

        # Если batch_size > 0 и не отключён — большой перерыв
        total_sent = sum(sent_count.values())
        if (
            config.BATCH_SIZE > 0
            and total_sent > 0
            and total_sent % config.BATCH_SIZE == 0
            and config.BATCH_BREAK_MAX > 0
        ):
            batch_break = random.uniform(config.BATCH_BREAK_MIN, config.BATCH_BREAK_MAX)
            current = current + timedelta(seconds=batch_break)

    end = current
    return {
        "tz": config.TIMEZONE_NAME,
        "active_window": f"{config.ACTIVE_HOURS_START:02d}:00-{config.ACTIVE_HOURS_END:02d}:00",
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "duration_sec": (end - start).total_seconds(),
        "duration_human": _estimate_total_time((end - start).total_seconds()),
        "total_messages": sum(sent_count.values()),
        "total_contacts": len(sent_contacts),
        "by_channel": sent_count,
        "channels": channels,
        "limits": {ch: config.CHANNEL_DAILY_LIMITS.get(ch, 0) for ch in channels},
        "all_contacts": len(all_contacts),
        "already_sent": len(sent_phones),
        "filtered_out": len(contacts_to_send) - len(filtered),
        "matched": len(filtered),
        "skipped_by_cat": skipped_by_cat,
        "sleeps_outside_window": sleeps_outside_window,
        "window_closures": window_closures,
        "schedule": schedule,
    }


def main() -> int:
    print("=" * 70)
    print(" 🧪 DRY-RUN СИМУЛЯЦИЯ (МСК, активное окно 10:00-20:00)")
    print("=" * 70)
    print()
    print(f"  Excel:        {config.EXCEL_PATH}")
    print(f"  DB:           {config.DB_PATH}  (только чтение)")
    print(f"  DRY_RUN:      {'ВКЛЮЧЁН' if config.DRY_RUN else 'выключен'}")
    print(f"  Timezone:     {config.TIMEZONE_NAME}")
    print(f"  Окно:         {config.ACTIVE_HOURS_START:02d}:00-{config.ACTIVE_HOURS_END:02d}:00")
    print(f"  Каналы:       {wappi.active_channels()}")
    print(f"  Лимиты/канал: {config.CHANNEL_DAILY_LIMITS}")
    print(f"  Задержка:     {config.DELAY_MIN}-{config.DELAY_MAX}с "
          f"({_fmt_duration(config.DELAY_MIN)}-{_fmt_duration(config.DELAY_MAX)})")
    print()

    result = simulate_broadcast(seed=42)
    if "error" in result:
        print(f"❌ {result['error']}")
        return 1

    # Статистика
    print("─" * 70)
    print("📊 СТАТИСТИКА:")
    print("─" * 70)
    print(f"  Всего в Excel:           {result['all_contacts']}")
    print(f"  Уже отправлено:          {result['already_sent']}")
    print(f"  Не подошло по категории: {result['filtered_out']}")
    print(f"  Подходит сегодня:        {result['matched']}")
    print(f"  ▶ К отправке:            {result['total_contacts']} контактов, "
          f"{result['total_messages']} сообщений")
    print(f"  ▶ По каналам:            {result['by_channel']}")
    print()

    # Расписание
    print("─" * 70)
    print("⏱  ПРОГНОЗИРУЕМОЕ РАСПИСАНИЕ (МСК):")
    print("─" * 70)
    print(f"  Старт:    {result['start']}")
    print(f"  Финиш:    {result['end']}")
    print(f"  Всего:    {result['duration_human']} ({result['duration_sec']:.0f}с)")
    if result["sleeps_outside_window"]:
        total_sleep = sum(s["wait_hours"] for s in result["sleeps_outside_window"])
        print(f"  Спящих пауз вне окна: {len(result['sleeps_outside_window'])} "
              f"({total_sleep:.1f}ч всего)")
    if result["window_closures"]:
        print(f"  Окончаний окна:       {len(result['window_closures'])}")
    print()

    # Проверки
    print("─" * 70)
    print("✅ ПРОВЕРКИ:")
    print("─" * 70)
    fails: list[str] = []
    for ch, n in result["by_channel"].items():
        limit = result["limits"].get(ch, 0)
        if n > limit:
            fails.append(f"Канал {ch}: {n} > лимита {limit}")
    if result["total_messages"] == 0:
        fails.append("0 сообщений — нечего слать")
    # Проверяем, что все отправки в окне
    for entry in result["schedule"]:
        dt = datetime.fromisoformat(entry["ts"])
        if not config.is_within_active_hours(dt):
            fails.append(f"Отправка {entry['n_msg']} вне окна: {entry['ts']}")
            break

    if fails:
        for f in fails:
            print(f"  ❌ {f}")
        print()
        print("  ⚠️  Тест провален.")
    else:
        print(f"  ✅ Дневные лимиты не превышены")
        print(f"  ✅ Все отправки в активном окне {result['active_window']} ({result['tz']})")
        print(f"  ✅ DRY_RUN: никаких реальных вызовов не было")
    print()

    # Первые/последние 5 контактов (не сообщений — иначе будет 15 строк)
    if result["schedule"]:
        print("─" * 70)
        print("📬 ПЕРВЫЕ 5 КОНТАКТОВ (каждый = 3 сообщения в WA+TG+MAX):")
        print("─" * 70)
        seen_contacts: set[str] = set()
        first_contacts: list[dict] = []
        for e in result["schedule"]:
            if e["phone"] in seen_contacts:
                continue
            seen_contacts.add(e["phone"])
            first_contacts.append(e)
            if len(first_contacts) >= 5:
                break
        for e in first_contacts:
            print(
                f"  #{e['n_contact']:2d}  {e['ts']}  {e['phone']:15s}  "
                f"({e['category']})"
            )

        if result["total_contacts"] > 10:
            print(f"  ... ({result['total_contacts'] - 10} пропущено) ...")
            print("─" * 70)
            print("📬 ПОСЛЕДНИЕ 5 КОНТАКТОВ:")
            print("─" * 70)
            last_contacts: list[dict] = []
            seen = set()
            for e in reversed(result["schedule"]):
                if e["phone"] in seen:
                    continue
                seen.add(e["phone"])
                last_contacts.append(e)
                if len(last_contacts) >= 5:
                    break
            for e in reversed(last_contacts):
                print(
                    f"  #{e['n_contact']:2d}  {e['ts']}  {e['phone']:15s}  "
                    f"({e['category']})"
                )
        print()

    # Сохраняем
    out = Path(config.DATA_DIR) / "dryrun_schedule.json"
    with out.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("─" * 70)
    print(f"💾 Полное расписание сохранено: {out}")
    print(f"   {len(result['schedule'])} точек отправки")
    print()

    print("=" * 70)
    print(" 🎯 ИТОГ")
    print("=" * 70)
    if not fails:
        print(" ✅ Расписание валидно.")
        print()
        print(" ⚠️  Я НЕ запускал рассылку. Это только симуляция.")
        print()
        print(" Когда будешь готов к реальной рассылке:")
        print("   1) В .env.local: BULK_DRY_RUN=0")
        print("   2) В терминале:  python main.py sender")
        print("   3) Оставь терминал открытым. Ctrl+C = мягкая остановка.")
    else:
        print(" ❌ Расписание не прошло проверки.")
    print("=" * 70)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
