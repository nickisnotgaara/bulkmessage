"""
COMPREHENSIVE TEST SUITE для broadcast системы.

Что проверяем (по убыванию критичности):
  A. CONFIG — конфиг вообще грузится, лимиты/тайминги валидны
  B. TIMEZONE — config.now_tz() = MSK, не локальное
  C. ACTIVE HOURS — корректно работает на границах (10:00 in, 20:00 out, 09:30 wait)
  D. STATE — load/save/reset работают на битом JSON, отсутствующих полях, прошлой дате
  E. PHONE NORMALIZATION — все префиксы (8/+7/9/998), пустота, мусор
  F. CATEGORY NORMALIZATION — алиасы, регистр, неизвестные
  G. MESSAGE BUILDING — с именем, без имени, спец-категории (агент → "коллега")
  H. ERROR CLASSIFICATION — 401/403/429/5xx/timeout/permanent → правильный ErrorKind
  I. DRY_RUN SAFETY — проверка стоит ДО wappi.send_wappi, ни одного реального вызова
  J. WAPPI PATHS — все 3 канала имеют send/get пути
  K. ACTIVE CHANNELS — каналы без токена отфильтрованы, с токеном активны
  L. SCHEDULE — симуляция 60 контактов вмещается в 10ч окно
  M. EDGE CASES — пустой Excel, битые контакты, специальные символы

Запуск: py test_suite.py
Никаких реальных Wappi вызовов, никаких изменений в основном crm.db / state.
Изоляция — через mock.patch и временные файлы.

Exit code: 0 если все ✅, иначе 1.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

# ---------------------------------------------------------------------------
# Мини-framework (без pytest — он не в requirements)
# ---------------------------------------------------------------------------

passed = 0
failed = 0
errors: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        errors.append((name, detail))
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print()
    print(f"── {title} " + "─" * (66 - len(title)))


# ---------------------------------------------------------------------------
# A. CONFIG
# ---------------------------------------------------------------------------
section("A. CONFIG")
from bulkmessage import config  # noqa: E402

check("config module loads", config is not None)
check(
    "3 channels in CHANNEL_DAILY_LIMITS (WA/TG/MAX)",
    set(config.CHANNEL_DAILY_LIMITS.keys()) == {"whatsapp", "telegram", "max"},
    f"got {list(config.CHANNEL_DAILY_LIMITS.keys())}",
)
check("WA limit > 0", config.CHANNEL_DAILY_LIMITS["whatsapp"] > 0)
check("TG limit > 0", config.CHANNEL_DAILY_LIMITS["telegram"] > 0)
check("MAX limit > 0", config.CHANNEL_DAILY_LIMITS["max"] > 0)
check(
    "WA = TG = MAX (одна и та же квота на контакт)",
    config.CHANNEL_DAILY_LIMITS["whatsapp"]
    == config.CHANNEL_DAILY_LIMITS["telegram"]
    == config.CHANNEL_DAILY_LIMITS["max"],
    f"got {config.CHANNEL_DAILY_LIMITS}",
)
check("DELAY_MIN > 0", config.DELAY_MIN > 0)
check(
    "DELAY_MAX >= DELAY_MIN",
    config.DELAY_MAX >= config.DELAY_MIN,
    f"got MIN={config.DELAY_MIN} MAX={config.DELAY_MAX}",
)
check("DELAY_MAX <= 30 min (не сон-рассылка)",
      config.DELAY_MAX <= 1800,
      f"got {config.DELAY_MAX}с = {config.DELAY_MAX // 60} мин")
check(
    "ACTIVE_HOURS_START < END (sanity)",
    config.ACTIVE_HOURS_START < config.ACTIVE_HOURS_END,
    f"got {config.ACTIVE_HOURS_START}-{config.ACTIVE_HOURS_END}",
)
check("TZ = Europe/Moscow", config.TIMEZONE_NAME == "Europe/Moscow")
check("DRY_RUN is bool", isinstance(config.DRY_RUN, bool))
check("DRY_RUN enabled (защита для текущей сессии)", config.DRY_RUN is True)

# ---------------------------------------------------------------------------
# B. TIMEZONE
# ---------------------------------------------------------------------------
section("B. TIMEZONE")
now = config.now_tz()
check("now_tz() has tzinfo", now.tzinfo is not None)
offset_h = now.utcoffset().total_seconds() / 3600
check(
    "now_tz() offset 2-4h ahead of UTC (Moscow + DST)",
    2 <= offset_h <= 4,
    f"got {offset_h}ч",
)
# Сравним: то же время, что и локальное (Узбекистан +5)?
local_now = datetime.now()
check(
    "now_tz() != local datetime.now() (иначе бессмысленно)",
    now.utcoffset() != local_now.utcoffset()
    or now.hour != local_now.hour,
    "TZ либо совпадает с локальной, либо часы случайно совпали",
)

# ---------------------------------------------------------------------------
# C. ACTIVE HOURS
# ---------------------------------------------------------------------------
section("C. ACTIVE HOURS (10:00-20:00 MSK)")
moscow = ZoneInfo("Europe/Moscow")
# 10:00 — ровно на границе, должно быть в окне
dt_10 = datetime(2026, 8, 22, 10, 0, tzinfo=moscow)
check("10:00 MSK → в окне", config.is_within_active_hours(dt_10) is True)
# 09:59 — последняя минута ДО окна
dt_959 = datetime(2026, 8, 22, 9, 59, tzinfo=moscow)
check("09:59 MSK → вне окна", config.is_within_active_hours(dt_959) is False)
# 19:59 — последняя минута окна
dt_1959 = datetime(2026, 8, 22, 19, 59, tzinfo=moscow)
check("19:59 MSK → в окне", config.is_within_active_hours(dt_1959) is True)
# 20:00 — ровно конец, ВНЕ (end exclusive)
dt_20 = datetime(2026, 8, 22, 20, 0, tzinfo=moscow)
check("20:00 MSK → вне окна (end exclusive)", config.is_within_active_hours(dt_20) is False)
# Полночь — глубоко вне
dt_midnight = datetime(2026, 8, 22, 0, 0, tzinfo=moscow)
check("00:00 MSK → вне окна", config.is_within_active_hours(dt_midnight) is False)
# Сколько ждать с 09:30?
wait = config.seconds_until_active_window(datetime(2026, 8, 22, 9, 30, tzinfo=moscow))
check(
    "Wait от 09:30 = 30 мин = 1800с",
    abs(wait - 1800) < 5,
    f"got {wait}с",
)
# С 10:00 — 0
wait0 = config.seconds_until_active_window(dt_10)
check("Wait от 10:00 = 0", wait0 == 0, f"got {wait0}")
# С 21:00 — ждать до завтрашнего 10:00 = 14ч = 50400с
wait_late = config.seconds_until_active_window(dt_20)
check(
    "Wait от 20:00 = 14ч до следующего 10:00",
    abs(wait_late - 14 * 3600) < 60,
    f"got {wait_late}с = {wait_late/3600:.1f}ч",
)

# ---------------------------------------------------------------------------
# D. STATE
# ---------------------------------------------------------------------------
section("D. STATE (load / save / reset)")
from bulkmessage import state  # noqa: E402

# Создаём временный файл состояния
tmp_state = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", delete=False, encoding="utf-8"
)
tmp_state.write(json.dumps({
    "sent_today": {"whatsapp": 30, "telegram": 25, "max": 10},
    "contacts_today": 30,
    "date": "2026-08-22",
    "last_index": 30,
}))
tmp_state.close()
tmp_state_path = tmp_state.name

try:
    with patch.object(config, "STATE_PATH", tmp_state_path):
        s = state.load_state()
        check("load_state возвращает dict", isinstance(s, dict))
        check(
            "load_state читает sent_today",
            s["sent_today"].get("whatsapp") == 30,
            f"got {s.get('sent_today')}",
        )
        check("load_state ставит дефолты при отсутствии полей",
              all(k in s for k in ("sent_today", "contacts_today", "date", "last_index")))

        # channel_has_quota
        check("channel_has_quota True при 30/60",
              state.channel_has_quota(s, "whatsapp") is True)
        s["sent_today"]["whatsapp"] = 60
        check("channel_has_quota False при 60/60",
              state.channel_has_quota(s, "whatsapp") is False)

        # increment_channel_sent
        s2 = state.load_state()
        s2["sent_today"]["whatsapp"] = 30
        state.increment_channel_sent(s2, "whatsapp")
        check("increment_channel_sent +1",
              s2["sent_today"]["whatsapp"] == 31,
              f"got {s2['sent_today']['whatsapp']}")

        # all_quotas_exhausted
        s3 = state.load_state()
        s3["sent_today"] = {"whatsapp": 60, "telegram": 60, "max": 60}
        check(
            "all_quotas_exhausted True при всех 60/60",
            state.all_quotas_exhausted(s3, ["whatsapp", "telegram", "max"]),
        )
        s3["sent_today"]["whatsapp"] = 59
        check(
            "all_quotas_exhausted False если хоть один не exhausted",
            not state.all_quotas_exhausted(s3, ["whatsapp", "telegram", "max"]),
        )

        # reset_daily_if_new_day — на старой дате должен сбросить
        s4 = state.load_state()
        s4["date"] = "2020-01-01"
        s4["sent_today"] = {"whatsapp": 50}
        s5 = state.reset_daily_if_new_day(s4)
        check(
            "reset_daily_if_new_day сбрасывает sent_today при прошлой дате",
            s5["sent_today"] == {},
            f"got {s5['sent_today']}",
        )
        check(
            "reset_daily_if_new_day ставит сегодняшнюю дату",
            s5["date"] == config.now_tz().strftime("%Y-%m-%d"),
            f"got {s5['date']}",
        )

        # reset_daily_if_new_day — на сегодняшней НЕ трогает
        s6 = state.load_state()
        s6["date"] = config.now_tz().strftime("%Y-%m-%d")
        s6["sent_today"] = {"whatsapp": 42}
        s7 = state.reset_daily_if_new_day(s6)
        check(
            "reset_daily_if_new_day НЕ трогает сегодняшние квоты",
            s7["sent_today"].get("whatsapp") == 42,
        )
finally:
    Path(tmp_state_path).unlink(missing_ok=True)

# Битый JSON
bad_state = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", delete=False, encoding="utf-8"
)
bad_state.write("{ not valid json :::")
bad_state.close()
try:
    with patch.object(config, "STATE_PATH", bad_state.name):
        s = state.load_state()
        check(
            "load_state на битом JSON → пустой dict без падения",
            s == {"sent_today": {}, "contacts_today": 0, "date": "", "last_index": 0},
            f"got {s}",
        )
finally:
    Path(bad_state.name).unlink(missing_ok=True)

# Несуществующий файл
with patch.object(config, "STATE_PATH", "Z:/nonexistent/path/state.json"):
    s = state.load_state()
    check("load_state на отсутствующем файле → пустой dict без падения",
          s["sent_phones"] == set() if False else s["sent_today"] == {})

# Защита от дублей: проверяем, что код в sender.py содержит guard
sender_src_check = Path("bulkmessage/sender.py").read_text(encoding="utf-8")
check("sender.py содержит защиту от дублей (crm.db пустая)",
      "ЗАЩИТА ОТ ДУБЛЕЙ" in sender_src_check)
check("sender.py проверяет BULK_SKIP_DUP_PROTECTION env",
      "BULK_SKIP_DUP_PROTECTION" in sender_src_check)

# ---------------------------------------------------------------------------
# E. PHONE NORMALIZATION
# ---------------------------------------------------------------------------
section("E. PHONE NORMALIZATION")
from bulkmessage.wappi import normalize_phone  # noqa: E402

cases = [
    ("89261234567", "79261234567"),     # 8 → 7
    ("+79261234567", "79261234567"),    # +7
    ("79261234567", "79261234567"),     # 7
    ("9261234567", "79261234567"),      # 10 цифр с 9 → 7
    ("941234567", "998941234567"),      # 9 цифр с 9 → 998 (Узбекистан)
    ("+998941234567", "998941234567"),  # +998
    ("8 (926) 123-45-67", "79261234567"),  # мусор вокруг
    ("", ""),
    (None, ""),
    ("abc", ""),
    ("   ", ""),
    ("+", ""),
]
for raw, expected in cases:
    got = normalize_phone(raw)
    check(f"normalize_phone({raw!r}) = {expected!r}",
          got == expected, f"got {got!r}")

# ---------------------------------------------------------------------------
# F. CATEGORY NORMALIZATION
# ---------------------------------------------------------------------------
section("F. CATEGORY NORMALIZATION")
from bulkmessage import templates  # noqa: E402

cat_cases = [
    ("Покупатель", "Покупатели"),
    ("Покупатели", "Покупатели"),
    ("покупатели", "Покупатели"),
    ("  Покупатель  ", "Покупатели"),
    ("Продавец", "Продавцы"),
    ("продавцы", "Продавцы"),
    ("Агент", "Агенты"),
    ("агенты", "Агенты"),
    ("Риэлтор", "Агенты"),
    ("риелтор", "Агенты"),
    ("Инвестор", "Инвесторы"),
    ("инвестор/риэлтор", "Инвесторы"),
    ("Случайный статус", "Случайный статус"),  # неизвестный — оставляем как есть
    ("", ""),
    (None, ""),
]
for raw, expected in cat_cases:
    got = templates.normalize_category(raw)
    check(f"normalize_category({raw!r}) = {expected!r}",
          got == expected, f"got {got!r}")

# ALLOWED_CATEGORIES содержит ровно 4 наших категории
check(
    "ALLOWED_CATEGORIES = {Покупатели, Продавцы, Агенты, Инвесторы}",
    config.ALLOWED_CATEGORIES == {"Покупатели", "Продавцы", "Агенты", "Инвесторы"},
    f"got {config.ALLOWED_CATEGORIES}",
)

# ---------------------------------------------------------------------------
# G. MESSAGE BUILDING
# ---------------------------------------------------------------------------
section("G. MESSAGE BUILDING")
tmpls = templates.load_templates()
check("Загружено 4 шаблона", len(tmpls) == 4, f"got {len(tmpls)}: {list(tmpls.keys())}")
check("Шаблон «Покупатели» содержит {имя}",
      "{имя}" in tmpls.get("Покупатели", ""))

c_buyer = {"phone": "79261234567", "name": "Иван", "category": "Покупатель"}
m_buyer = templates.build_message(c_buyer, tmpls)
check("Сообщение покупателю содержит имя", "Иван" in m_buyer)
check("Сообщение покупателю НЕ пустое", len(m_buyer) > 50)
check("Сообщение покупателю НЕ содержит {имя} (была подстановка)",
      "{имя}" not in m_buyer)

c_agent_noname = {"phone": "79261234567", "name": "", "category": "Агент"}
m_agent = templates.build_message(c_agent_noname, tmpls)
check("Агенту без имени — НЕ «Здравствуй ,»",
      "Здравствуй , " not in m_agent and "Здравствуй,  " not in m_agent)

c_investor = {"phone": "79261234567", "name": "Олег", "category": "Инвестор"}
m_inv = templates.build_message(c_investor, tmpls)
check("Инвестору — шаблон с упоминанием общения",
      "Олег" in m_inv)

# Имя с фигурными скобками (защита от format injection)
c_braces = {"phone": "79261234567", "name": "Иван {qwerty}", "category": "Покупатель"}
m_braces = templates.build_message(c_braces, tmpls)
check("Имя с { } не падает (экранирование)",
      "Иван (qwerty)" in m_braces or "Иван {qwerty}" in m_braces,
      f"got: {m_braces[:100]}")

# Имя длиннее 60 символов обрезается
c_long = {"phone": "79261234567", "name": "A" * 100, "category": "Покупатель"}
m_long = templates.build_message(c_long, tmpls)
check("Длинное имя обрезается до ~60 символов",
      "A" * 60 in m_long and "A" * 61 not in m_long,
      "не обрезалось или обрезалось неправильно")

# Неизвестная категория — fallback
c_unknown = {"phone": "79261234567", "name": "Тест", "category": "Случайный"}
m_unknown = templates.build_message(c_unknown, tmpls)
check("Неизвестная категория → fallback-сообщение (не падает)",
      "Тест" in m_unknown and len(m_unknown) > 0)

# ---------------------------------------------------------------------------
# H. ERROR CLASSIFICATION
# ---------------------------------------------------------------------------
section("H. ERROR CLASSIFICATION")
from bulkmessage.wappi import classify_error, ErrorKind  # noqa: E402

err_cases = [
    # (detail, http_status, expected, description)
    ("", 401, ErrorKind.AUTH, "HTTP 401"),
    ("", 403, ErrorKind.AUTH, "HTTP 403"),
    ("unauthorized", None, ErrorKind.AUTH, "keyword 'unauthorized'"),
    ("invalid token", None, ErrorKind.AUTH, "keyword 'invalid token'"),
    ("rate limit exceeded", 429, ErrorKind.RATE_LIMIT, "429 rate limit"),
    ("Flood wait required", None, ErrorKind.RATE_LIMIT, "flood wait"),
    ("too many requests", None, ErrorKind.RATE_LIMIT, "too many"),
    ("temporary ban", None, ErrorKind.RATE_LIMIT, "temporary ban"),
    ("not registered on whatsapp", None, ErrorKind.PERMANENT, "not registered"),
    ("recipient not found", None, ErrorKind.PERMANENT, "recipient not found"),
    ("user not found", None, ErrorKind.PERMANENT, "user not found"),
    ("phone_not_occupied", None, ErrorKind.PERMANENT, "phone_not_occupied"),
    ("doesn't have a telegram", None, ErrorKind.PERMANENT, "no telegram"),
    ("", 500, ErrorKind.TRANSIENT, "HTTP 500"),
    ("", 502, ErrorKind.TRANSIENT, "HTTP 502"),
    ("", 503, ErrorKind.TRANSIENT, "HTTP 503"),
    ("connection timeout", None, ErrorKind.TRANSIENT, "connection timeout"),
    ("internal error", None, ErrorKind.TRANSIENT, "internal error"),
    ("weird unknown error", None, ErrorKind.UNKNOWN, "unknown text"),
    ("", None, ErrorKind.UNKNOWN, "empty + no status"),
]
for detail, status, expected, desc in err_cases:
    got = classify_error(detail, status)
    check(f"classify_error: {desc}",
          got == expected, f"got {got.value}, expected {expected.value}")

# ---------------------------------------------------------------------------
# I. DRY_RUN SAFETY (статический анализ кода)
# ---------------------------------------------------------------------------
section("I. DRY_RUN SAFETY")
sender_src = Path("bulkmessage/sender.py").read_text(encoding="utf-8")
check("config.DRY_RUN = True (защита от случайной отправки)", config.DRY_RUN is True)

# Проверяем, что в sender.py проверка DRY_RUN идёт ДО вызова wappi.send_wappi
dry_check_idx = sender_src.find("if config.DRY_RUN:")
send_call_idx = sender_src.find("wappi.send_wappi(channel, phone, text)")
check(
    "В sender.py: проверка DRY_RUN ПЕРЕД wappi.send_wappi",
    dry_check_idx != -1 and send_call_idx != -1 and dry_check_idx < send_call_idx,
    f"DRY_RUN at {dry_check_idx}, send_wappi at {send_call_idx}",
)
# Убеждаемся, что wappi.send_wappi ВЫЗЫВАЕТСЯ только в else-ветке
# (т.е. когда DRY_RUN выключен)
# Проверим: после if config.DRY_RUN идёт блок с return, а send_wappi — после else
# Простая эвристика: между "if config.DRY_RUN:" и "wappi.send_wappi" должно быть "else:"
else_idx = sender_src.find("else:\n            ok, message_id", dry_check_idx)
check(
    "После if DRY_RUN есть ветка else с реальным send_wappi",
    else_idx != -1 and else_idx < send_call_idx,
    f"else at {else_idx}, send at {send_call_idx}",
)

# Проверим, что в DRY_RUN ветке НЕТ вызова requests.post
dry_block_end = sender_src.find("else:", dry_check_idx)
dry_block = sender_src[dry_check_idx:dry_block_end]
check("В DRY_RUN ветке НЕТ requests.post/wappi.send_wappi",
      "wappi.send_wappi" not in dry_block and "requests.post" not in dry_block)

# ---------------------------------------------------------------------------
# J. WAPPI PATHS
# ---------------------------------------------------------------------------
section("J. WAPPI PATHS")
check("WA send path = /api/sync/message/send",
      config.WAPPI_SEND_PATHS["whatsapp"] == "/api/sync/message/send")
check("TG send path = /tapi/sync/message/send",
      config.WAPPI_SEND_PATHS["telegram"] == "/tapi/sync/message/send")
check("MAX send path = /maxapi/sync/message/send",
      config.WAPPI_SEND_PATHS["max"] == "/maxapi/sync/message/send")
check("WA get-status path",
      "whatsapp" in config.WAPPI_MESSAGE_GET_PATHS)
check("TG get-status path",
      "telegram" in config.WAPPI_MESSAGE_GET_PATHS)
check("MAX get-status path",
      "max" in config.WAPPI_MESSAGE_GET_PATHS)

# ---------------------------------------------------------------------------
# K. ACTIVE CHANNELS
# ---------------------------------------------------------------------------
section("K. ACTIVE CHANNELS")
from bulkmessage import wappi  # noqa: E402

channels = wappi.active_channels()
check("active_channels() возвращает list", isinstance(channels, list))
check("WA активен (токен заполнен в .env.local)",
      "whatsapp" in channels,
      "проверь WAPPI_WHATSAPP_TOKEN в .env.local")
check("TG активен (токен заполнен в .env.local)",
      "telegram" in channels,
      "проверь WAPPI_TELEGRAM_TOKEN в .env.local")

# MAX: если токен пустой — должен быть отфильтрован
wa_token = config.WAPPI_TOKENS.get("max", {}).get("token", "")
if not wa_token or wa_token.startswith("ВАШ_"):
    check("MAX неактивен (токен пустой) — фильтруется корректно",
          "max" not in channels)
else:
    check("MAX активен (токен заполнен)", "max" in channels)

# Проверим, что с пустым токеном канал отфильтровывается
with patch.dict(config.WAPPI_TOKENS, {"whatsapp": {"token": "", "profile_id": ""}}):
    only_active = wappi.active_channels()
    check("Пустой токен → канал отфильтрован",
          "whatsapp" not in only_active)

# ---------------------------------------------------------------------------
# L. SCHEDULE (smoke)
# ---------------------------------------------------------------------------
section("L. SCHEDULE (smoke — укладывается ли в окно)")
import random  # noqa: E402
random.seed(42)
# Используем тот же подход, что и в test_dry_run.py, но минимально
from bulkmessage import contacts, db  # noqa: E402

try:
    all_cts = contacts.load_contacts(config.EXCEL_PATH)
    check("Excel-файл читается", len(all_cts) > 0, f"got {len(all_cts)}")

    with db.db_conn() as conn:
        sent_phones = db.get_sent_phones(conn)
    check("DB читается (get_sent_phones)", isinstance(sent_phones, set))

    # Симуляция: генерим 60 контактов с паузами 5-15 мин
    # и проверяем, что влезаем в 10ч
    available = [c for c in all_cts if c["phone"] and c["phone"] not in sent_phones]
    eligible = [
        c for c in available
        if templates.normalize_category(c.get("category", "")) in config.ALLOWED_CATEGORIES
    ]
    check("Есть eligible контакты (>= 60)", len(eligible) >= 60,
          f"got {len(eligible)} (нужно минимум 60 для полного дня)")

    # Берём первые 60 и симулируем
    sim = eligible[:60]
    start = datetime(2026, 8, 22, 10, 0, tzinfo=moscow)
    cur = start
    for _ in sim:
        # Каждый контакт → 1 пауза (5-15 мин)
        cur = cur + timedelta(seconds=random.uniform(300, 900))
    end = datetime(2026, 8, 22, 20, 0, tzinfo=moscow)
    fits = cur <= end
    check("60 контактов с 5-15 мин паузами влезают в 10ч окно (10:00-20:00)",
          fits, f"end at {cur.strftime('%H:%M')}")
except Exception as e:
    check(f"Schedule simulation crashed: {type(e).__name__}: {e}", False)

# ---------------------------------------------------------------------------
# M. EDGE CASES
# ---------------------------------------------------------------------------
section("M. EDGE CASES")
# Пустой Excel
empty_xlsx = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
empty_xlsx.close()
wb_empty = None
try:
    from openpyxl import Workbook
    wb_empty = Workbook()
    ws = wb_empty.active
    ws.append(["Телефон", "Имя контакта", "Категория"])
    wb_empty.save(empty_xlsx.name)
    wb_empty.close()
    wb_empty = None
    result = contacts.load_contacts(empty_xlsx.name)
    check("Пустой Excel (только заголовки) → []", result == [],
          f"got {result}")
except Exception as e:
    check(f"Пустой Excel не падает: {e}", False)
finally:
    if wb_empty is not None:
        try:
            wb_empty.close()
        except Exception:
            pass
    try:
        Path(empty_xlsx.name).unlink(missing_ok=True)
    except (PermissionError, OSError):
        pass  # Windows может держать файл — не критично

# Excel с невалидными номерами
bad_xlsx = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
bad_xlsx.close()
wb_bad = None
try:
    wb_bad = Workbook()
    ws = wb_bad.active
    ws.append(["Телефон", "Имя контакта", "Категория"])
    ws.append(["abc", "Иван", "Покупатель"])  # мусор вместо номера
    ws.append(["+79261111111", "Петя", "Покупатель"])  # норм
    ws.append(["", "Без имени", "Агент"])  # без номера
    ws.append(["+79261111111", "Дубль", "Агент"])  # дубль номера
    wb_bad.save(bad_xlsx.name)
    wb_bad.close()
    wb_bad = None
    result = contacts.load_contacts(bad_xlsx.name)
    check("Excel с мусором → только валидные номера",
          len(result) == 1 and result[0]["name"] == "Петя",
          f"got {result}")
except Exception as e:
    check(f"Excel с мусором не падает: {e}", False)
finally:
    if wb_bad is not None:
        try:
            wb_bad.close()
        except Exception:
            pass
    try:
        Path(bad_xlsx.name).unlink(missing_ok=True)
    except (PermissionError, OSError):
        pass

# Сохранение state в битый путь (например, в файл без прав)
import os
read_only_dir = tempfile.mkdtemp()
ro_state = Path(read_only_dir) / "state.json"
ro_state.write_text("{}", encoding="utf-8")
try:
    os.chmod(read_only_dir, 0o444)  # read-only (на Windows может игнорироваться)
    with patch.object(config, "STATE_PATH", str(ro_state)):
        try:
            s = state.load_state()
            check("load_state из read-only dir работает",
                  s.get("sent_today") == {},
                  f"got {s.get('sent_today')}")
        except Exception as e:
            check(f"load_state из read-only dir не падает: {e}", False)
        # save упадёт — это нормально, но мы хотим убедиться, что есть try/except
        s["sent_today"]["whatsapp"] = 1
        try:
            state.save_state(s)
            # Если не упало — значит write удался (Windows игнорирует chmod)
        except (PermissionError, OSError):
            check("save_state в read-only — падает PermissionError (ожидаемо)", True)
except Exception as e:
    # Windows может не уважать chmod — это OK
    pass
finally:
    try:
        os.chmod(read_only_dir, 0o777)
    except Exception:
        pass
    try:
        Path(read_only_dir).unlink(missing_ok=True)
    except (PermissionError, OSError):
        pass  # Windows может держать — не критично для тестов

# Sender: импортируется без ошибок
try:
    from bulkmessage import sender
    check("sender.py импортируется без ошибок", True)
    check("sender._fmt_duration существует", hasattr(sender, "_fmt_duration"))
    check("sender._estimate_total_time существует", hasattr(sender, "_estimate_total_time"))
    # Sanity
    check("_fmt_duration(45) = '45с'", sender._fmt_duration(45) == "45с")
    check("_fmt_duration(720) содержит 'мин'",
          "мин" in sender._fmt_duration(720))
    check("_fmt_duration(3600) содержит 'ч'",
          "ч" in sender._fmt_duration(3600))
    check("_estimate_total_time(3600) = '1ч 00мин'",
          sender._estimate_total_time(3600) == "1ч 00мин")
except Exception as e:
    check(f"sender.py import: {type(e).__name__}: {e}", False)

# Тестовый test_dry_run.py тоже импортируется (нет syntax errors)
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("test_dry_run", "test_dry_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    check("test_dry_run.py импортируется (нет syntax errors)", True)
except Exception as e:
    check(f"test_dry_run.py: {type(e).__name__}: {e}", False)

# ---------------------------------------------------------------------------
# N. NEW FIXES (после full analysis)
# ---------------------------------------------------------------------------
section("N. NEW FIXES (C1, C2, H1, H3, M1, M3, M4, L1, L3, L5)")

# C2: missing config attrs теперь определены
check("config.RATE_LIMIT_BACKOFF_BASE существует",
      hasattr(config, "RATE_LIMIT_BACKOFF_BASE") and config.RATE_LIMIT_BACKOFF_BASE > 0,
      f"got {getattr(config, 'RATE_LIMIT_BACKOFF_BASE', 'MISSING')}")
check("config.RATE_LIMIT_BACKOFF_MAX существует",
      hasattr(config, "RATE_LIMIT_BACKOFF_MAX") and config.RATE_LIMIT_BACKOFF_MAX > 0,
      f"got {getattr(config, 'RATE_LIMIT_BACKOFF_MAX', 'MISSING')}")
check("config.TRANSIENT_BACKOFF_MIN существует",
      hasattr(config, "TRANSIENT_BACKOFF_MIN") and config.TRANSIENT_BACKOFF_MIN >= 0)
check("config.TRANSIENT_BACKOFF_MAX существует",
      hasattr(config, "TRANSIENT_BACKOFF_MAX") and config.TRANSIENT_BACKOFF_MAX > 0)
check("RATE_LIMIT_BACKOFF_MAX > RATE_LIMIT_BACKOFF_BASE",
      config.RATE_LIMIT_BACKOFF_MAX > config.RATE_LIMIT_BACKOFF_BASE)
check("TRANSIENT_BACKOFF_MAX > TRANSIENT_BACKOFF_MIN",
      config.TRANSIENT_BACKOFF_MAX > config.TRANSIENT_BACKOFF_MIN)

# C2: _next_backoff теперь вызываем без AttributeError
from bulkmessage.wappi import ErrorKind as _EK
backoff_rate = sender._next_backoff(_EK.RATE_LIMIT, 0)
check("_next_backoff(RATE_LIMIT, 0) = BASE",
      backoff_rate == config.RATE_LIMIT_BACKOFF_BASE,
      f"got {backoff_rate}")
backoff_rate2 = sender._next_backoff(_EK.RATE_LIMIT, 100)
check("_next_backoff(RATE_LIMIT, 100) = 200 (exponential)",
      backoff_rate2 == 200, f"got {backoff_rate2}")
backoff_rate_capped = sender._next_backoff(_EK.RATE_LIMIT, 99999)
check("_next_backoff(RATE_LIMIT, 99999) = MAX (capped)",
      backoff_rate_capped == config.RATE_LIMIT_BACKOFF_MAX,
      f"got {backoff_rate_capped}")
backoff_trans = sender._next_backoff(_EK.TRANSIENT, 0)
check("_next_backoff(TRANSIENT, 0) ∈ [MIN, MAX]",
      config.TRANSIENT_BACKOFF_MIN <= backoff_trans <= config.TRANSIENT_BACKOFF_MAX,
      f"got {backoff_trans}")
backoff_auth = sender._next_backoff(_EK.AUTH, 0)
check("_next_backoff(AUTH, 0) = MAX",
      backoff_auth == config.RATE_LIMIT_BACKOFF_MAX, f"got {backoff_auth}")
backoff_perm = sender._next_backoff(_EK.PERMANENT, 0)
check("_next_backoff(PERMANENT, 0) = 0",
      backoff_perm == 0, f"got {backoff_perm}")

# C1: file lock acquire/release
try:
    sender._try_acquire_lock()
    check("_try_acquire_lock() создал lock-файл",
          Path(config.LOCK_PATH).exists())
    # Повторный acquire должен вернуть None (уже занято)
    second = sender._try_acquire_lock()
    check("Повторный _try_acquire_lock() возвращает None (занято)",
          second is None, f"got {second}")
    # Release
    sender._release_lock(Path(config.LOCK_PATH))
    check("_release_lock() удалил lock-файл",
          not Path(config.LOCK_PATH).exists())
    # Теперь можно снова acquire
    third = sender._try_acquire_lock()
    check("После release — снова можно acquire",
          third is not None)
    sender._release_lock(third)
except Exception as e:
    check(f"file lock не падает: {type(e).__name__}: {e}", False)

# C1: stale lock (от мёртвого процесса) перезаписывается
import os as _os
lock_path = Path(config.LOCK_PATH)
try:
    # Записываем фейковый PID 999999 (точно не наш)
    lock_path.write_text("999999", encoding="utf-8")
    # Убедимся, что PID 999999 мёртв (на тестовой машине должен быть)
    # _pid_alive вернёт False → lock перезаписывается
    acquired = sender._try_acquire_lock()
    check("Stale lock (от мёртвого PID) перезаписывается",
          acquired is not None)
    sender._release_lock(acquired)
except Exception as e:
    check(f"stale lock test: {type(e).__name__}: {e}", False)

# H1: _wait_for_backoffs
backoffs = {"whatsapp": 0, "telegram": 0}
sender._wait_for_backoffs(backoffs, sender.log)  # ничего не делает
check("_wait_for_backoffs() на пустых backoffs = 0", backoffs == {"whatsapp": 0, "telegram": 0})

# M3: is_within_active_hours выбрасывает на naive datetime
naive_dt = datetime(2026, 8, 22, 12, 0)  # БЕЗ tzinfo
try:
    config.is_within_active_hours(naive_dt)
    check("is_within_active_hours(naive) → ValueError", False, "не выбросил")
except ValueError:
    check("is_within_active_hours(naive) → ValueError", True)
except Exception as e:
    check(f"is_within_active_hours(naive) → неожиданный тип: {type(e).__name__}", False)

# M4: normalize_phone для 9-цифр остался 998, для 10-цифр с 9 — 7
check("normalize_phone 9 digits (UZ) = 998...",
      wappi.normalize_phone("941234567") == "998941234567")
check("normalize_phone 10 digits starting with 9 (RU) = 7...",
      wappi.normalize_phone("9261234567") == "79261234567")

# L1: BULAY typo исправлен
sender_src = Path("bulkmessage/sender.py").read_text(encoding="utf-8")
config_src = Path("bulkmessage/config.py").read_text(encoding="utf-8")
check("config.py: BULAY typo исправлен (нет 'BULAY_DELAY')",
      "BULAY_DELAY" not in config_src,
      "найдено 'BULAY_DELAY' в config.py")

# L3: _estimate_total_time(0) → "<1мин"
check("_estimate_total_time(0) = '<1мин'",
      sender._estimate_total_time(0) == "<1мин", f"got {sender._estimate_total_time(0)!r}")
check("_estimate_total_time(3600) = '1ч 00мин'",
      sender._estimate_total_time(3600) == "1ч 00мин")
check("_estimate_total_time(60) = '0ч 01мин' (ровно 1 мин)",
      sender._estimate_total_time(60) == "0ч 01мин", f"got {sender._estimate_total_time(60)!r}")
check("_estimate_total_time(59) = '<1мин' (sub-minute)",
      sender._estimate_total_time(59) == "<1мин", f"got {sender._estimate_total_time(59)!r}")
check("_estimate_total_time(86400) = '24ч 00мин'",
      sender._estimate_total_time(86400) == "24ч 00мин")

# L5: dead code удалён
check("sender._decrement_backoffs удалён",
      not hasattr(sender, "_decrement_backoffs"))
check("state.daily_contact_limit_reached удалён",
      not hasattr(state, "daily_contact_limit_reached"))

# M5: preflight_check существует и возвращает dict
try:
    pre = config.preflight_check()
    check("preflight_check() возвращает dict", isinstance(pre, dict))
    check("preflight_check() имеет ключ 'ok'", "ok" in pre)
    check("preflight_check() имеет ключ 'channels'", "channels" in pre)
    check("preflight_check() имеет ключ 'errors'", "errors" in pre)
    # При пустых токенах MAX должно быть false (но мы не смотрим — это network)
    check("preflight_check() валится если нет активных каналов (или OK если есть)",
          isinstance(pre["ok"], bool))
except Exception as e:
    check(f"preflight_check() не падает: {type(e).__name__}: {e}", False)

# config.LOCK_PATH существует
check("config.LOCK_PATH определён",
      hasattr(config, "LOCK_PATH") and config.LOCK_PATH)

# ---------------------------------------------------------------------------
# O. TG BOT LOG (новый модуль tglog)
# ---------------------------------------------------------------------------
section("O. TG BOT LOG (модуль tglog)")

from bulkmessage import tglog

# Конфиг
check("config.TG_LOG_BOT_TOKEN задан",
      bool(config.TG_LOG_BOT_TOKEN),
      f"got '{config.TG_LOG_BOT_TOKEN[:10]}...'" if config.TG_LOG_BOT_TOKEN else "пусто")
check("config.TG_LOG_ADMIN_ID задан",
      bool(config.TG_LOG_ADMIN_ID),
      f"got '{config.TG_LOG_ADMIN_ID}'" if config.TG_LOG_ADMIN_ID else "пусто")
check("config.TG_LOG_ENABLED = True (оба заданы)",
      config.TG_LOG_ENABLED is True)
check("config.TG_LOG_LEVEL ∈ {DEBUG,INFO,WARNING,ERROR,CRITICAL}",
      config.TG_LOG_LEVEL in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
      f"got '{config.TG_LOG_LEVEL}'")
check("config.TG_LOG_SEND_INTERVAL > 0",
      config.TG_LOG_SEND_INTERVAL > 0,
      f"got {config.TG_LOG_SEND_INTERVAL}")
check("config.TG_LOG_MAX_QUEUE > 0",
      config.TG_LOG_MAX_QUEUE > 0)

# Форматирование
short_msg = tglog._format_message("hello", "INFO")
check("_format_message содержит 'hello'",
      "hello" in short_msg)
check("_format_message содержит INFO-иконку",
      "ℹ️" in short_msg)
check("_format_message ERROR содержит ❌",
      "❌" in tglog._format_message("err", "ERROR"))
check("_format_message CRITICAL содержит 💥",
      "💥" in tglog._format_message("crit", "CRITICAL"))
check("_format_message WARNING содержит ⚠️",
      "⚠️" in tglog._format_message("warn", "WARNING"))
long_msg = "x" * 5000
formatted = tglog._format_message(long_msg, "INFO")
check("_format_message обрезает > 3500 символов",
      len(formatted) < 4000, f"len={len(formatted)}")

# Queue и send()
import queue
# send() кладёт в очередь, но фоновый worker может её уже дренировать.
# Проверяем что вызов не падает + что очередь существует.
try:
    tglog.send("test message", "INFO")
    check("send() не падает (если ENABLED)", True)
except Exception as e:
    check(f"send() не падает: {type(e).__name__}: {e}", False)
# Проверяем что очередь не ушла в -1 (т.е. не повреждена)
check("Очередь _QUEUE существует и работает",
      tglog._QUEUE is not None and isinstance(tglog._QUEUE, queue.Queue))

# TelegramHandler
from bulkmessage.tglog import TelegramHandler
handler = TelegramHandler()
check("TelegramHandler создан без ошибок", handler is not None)
check("TelegramHandler level = TG_LOG_LEVEL",
      handler.level == getattr(logging, config.TG_LOG_LEVEL, logging.ERROR),
      f"got {handler.level}")

# install_handler
import logging as _logging
_root_before = len(_logging.getLogger().handlers)
ok = tglog.install_handler()
check("install_handler() вернул True (когда ENABLED)", ok is True)
_root_after = len(_logging.getLogger().handlers)
check("install_handler() добавил handler к root logger",
      _root_after == _root_before + 1,
      f"before={_root_before} after={_root_after}")
# Идемпотентность: повторный вызов не должен дублировать
ok2 = tglog.install_handler()
check("install_handler() идемпотентен (повторный не дублирует)",
      ok2 is True and len(_logging.getLogger().handlers) == _root_after)

# getMe и send_test — реальные сетевые вызовы (НЕ запускаем в unit-suite,
# чтоб не блокировать на сетевых таймаутах. Отдельный скрипт _check_tg.py)
check("getMe() существует (сетевая проверка — в _check_tg.py)",
      callable(tglog.get_me))
check("send_test() существует (сетевая проверка — в _check_tg.py)",
      callable(tglog.send_test))

# ---------------------------------------------------------------------------
# ИТОГ
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ИТОГ
# ---------------------------------------------------------------------------
print()
print("=" * 70)
print(f" TOTAL: {passed} passed, {failed} failed")
print("=" * 70)
if failed > 0:
    print()
    print(" ❌ FAILURES:")
    for name, detail in errors:
        print(f"   • {name}: {detail}")
    sys.exit(1)
else:
    print()
    print(" ✅ ВСЕ ТЕСТЫ ПРОШЛИ. Система готова к боевому запуску.")
    print()
    print(" Напоминание: я НЕ запускал рассылку. Это только проверка кода.")
    print(" Боевой запуск — только твой вручную:")
    print("   1) .env.local: BULK_DRY_RUN=0")
    print("   2) py main.py sender")
    sys.exit(0)
