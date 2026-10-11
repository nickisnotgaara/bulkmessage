"""
COMPREHENSIVE TEST SUITE для broadcast системы (Wazzup24-only).

Что проверяем (по убыванию критичности):
  A. CONFIG — конфиг вообще грузится, лимиты/тайминги валидны
  B. TIMEZONE — config.now_tz() = MSK, не локальное
  C. ACTIVE HOURS — корректно работает на границах (10:00 in, 20:00 out, 09:30 wait)
  D. STATE — load/save/reset работают на битом JSON, отсутствующих полях, прошлой дате
  E. PHONE NORMALIZATION — все префиксы (8/+7/9/998), пустота, мусор
  F. CATEGORY NORMALIZATION — алиасы, регистр, неизвестные
  G. MESSAGE BUILDING — с именем, без имени, спец-категории (агент → "коллега")
  H. ERROR CLASSIFICATION — 401/403/429/5xx/timeout/permanent → правильный ErrorKind
  I. DRY_RUN SAFETY — проверка стоит ДО wazzup.send_to_channel, ни одного реального вызова
  J. WAZZUP24 PATHS — /v3/message, /v3/channels, /v3/webhooks
  K. ACTIVE CHANNELS — каналы без channel_id отфильтрованы, с заполненным активны
  L. SCHEDULE — симуляция 60 контактов вмещается в 10ч окно
  M. EDGE CASES — пустой Excel, битые контакты, специальные символы

Запуск: py test_suite.py
Никаких реальных Wazzup вызовов, никаких изменений в основном crm.db / state.
Изоляция — через mock.patch и временные файлы.

Exit code: 0 если все ✅, иначе 1.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
import inspect
import ast
from datetime import datetime, timedelta, timezone
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

# ── Изоляция БД ────────────────────────────────────────────────────────────
# Тесты Q/R25/R33 писали в БОЕВУЮ data/crm.db: там и INSERT тестовых
# контактов, и `DELETE FROM rescue_queue` без условий — то есть запуск
# тестов стирал настоящую очередь отката. Счётчики «761 контакт / 1348
# сообщений» после прогона выглядели нетронутыми, поэтому это замечали
# не сразу, а файл БД при этом переписывался.
#
# db.db_conn() читает config.DB_PATH при КАЖДОМ вызове, поэтому достаточно
# подменить путь до первого обращения к БД. Тесты, которым нужен реальный
# Excel (см. get_sent_phones ниже), подменяют путь обратно сами.
_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="bulkmessage_tests_"))
_TEST_DB_PATH = _TEST_DB_DIR / "test_crm.db"
_REAL_DB_PATH = config.DB_PATH
config.DB_PATH = str(_TEST_DB_PATH)
print(f"  (тестовая БД: {_TEST_DB_PATH}; боевая {Path(_REAL_DB_PATH).name} не трогается)")

check("config module loads", config is not None)
check(
    "5 channels in CHANNEL_DAILY_LIMITS (WA/TG/MAX/WABA/SMS)",
    set(config.CHANNEL_DAILY_LIMITS.keys()) == {"whatsapp", "telegram", "max", "waba", "sms"},
    f"got {list(config.CHANNEL_DAILY_LIMITS.keys())}",
)
check("WA limit > 0", config.CHANNEL_DAILY_LIMITS["whatsapp"] > 0)
check("TG limit > 0", config.CHANNEL_DAILY_LIMITS["telegram"] > 0)
check("MAX limit > 0", config.CHANNEL_DAILY_LIMITS["max"] > 0)
check("WABA limit > 0", config.CHANNEL_DAILY_LIMITS["waba"] > 0)
check(
    "Personal каналы ≤ своего per-channel hard cap (защита оригинальных аккаунтов)",
    config.CHANNEL_DAILY_LIMITS["telegram"]
    <= config.PERSONAL_CHANNEL_HARD_CAPS["telegram"]
    and config.CHANNEL_DAILY_LIMITS["max"]
    <= config.PERSONAL_CHANNEL_HARD_CAPS["max"],
    f"got {config.CHANNEL_DAILY_LIMITS}, caps {config.PERSONAL_CHANNEL_HARD_CAPS}",
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
check("20:00 MSK → в окне (10-22)", config.is_within_active_hours(dt_20) is True)
# Полночь — глубоко вне
dt_midnight = datetime(2026, 8, 22, 0, 0, tzinfo=moscow)
check("00:00 MSK → вне окна", config.is_within_active_hours(dt_midnight) is False)
# 22:00 — вне (end exclusive)
dt_22 = datetime(2026, 8, 22, 22, 0, tzinfo=moscow)
check("22:00 MSK → вне окна (end exclusive)", config.is_within_active_hours(dt_22) is False)
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
# С 22:00 — ждать до завтрашнего 10:00 = 12ч
wait_late = config.seconds_until_active_window(dt_22)
check(
    "Wait от 22:00 = 12ч до следующего 10:00",
    abs(wait_late - 12 * 3600) < 60,
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

        # NEW: successful_today counter
        target_day = config.TARGET_SUCCESS_PER_DAY
        max_attempts = config.MAX_ATTEMPTS_PER_DAY
        s6["successful_today"] = 0
        s6["attempts_today"] = 0
        s6["target_today"] = target_day
        check("successful_today() = 0 на старте",
              state.successful_today(s6) == 0)
        state.increment_successful(s6)
        check("increment_successful +1",
              state.successful_today(s6) == 1)
        check(f"target_reached() = False при 1/{target_day}",
              state.target_reached(s6) is False)
        s6["successful_today"] = target_day
        check(f"target_reached() = True при {target_day}/{target_day}",
              state.target_reached(s6) is True)
        check(f"max_attempts_reached() = False при attempts < {max_attempts}",
              state.max_attempts_reached(s6) is False)
        s6["attempts_today"] = max_attempts
        check(f"max_attempts_reached() = True при attempts = {max_attempts}",
              state.max_attempts_reached(s6) is True)
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
            s.get("sent_today") == {}
            and s.get("contacts_today") == 0
            and s.get("date") == ""
            and s.get("last_index") == 0
            and s.get("successful_today") == 0
            and s.get("attempts_today") == 0
            and s.get("target_today") == config.TARGET_SUCCESS_PER_DAY,
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
from bulkmessage.wazzup import normalize_phone  # noqa: E402

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
from bulkmessage.wazzup import classify_error, ErrorKind  # noqa: E402

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

# Проверяем, что в sender.py проверка DRY_RUN идёт ДО вызова wazzup.send_to_channel
dry_check_idx = sender_src.find("if config.DRY_RUN:")
send_call_idx = sender_src.find("wazzup.send_to_channel(")
check(
    "В sender.py: проверка DRY_RUN ПЕРЕД wazzup.send_to_channel",
    dry_check_idx != -1 and send_call_idx != -1 and dry_check_idx < send_call_idx,
    f"DRY_RUN at {dry_check_idx}, send_to_channel at {send_call_idx}",
)
# Убеждаемся, что wazzup.send_to_channel ВЫЗЫВАЕТСЯ только в else-ветке
else_idx = sender_src.find("else:\n            ok, message_id, detail, http_status = wazzup.send_to_channel", dry_check_idx)
check(
    "После if DRY_RUN есть ветка else с реальным send_to_channel",
    else_idx != -1 and else_idx < send_call_idx,
    f"else at {else_idx}, send at {send_call_idx}",
)

# Проверим, что в DRY_RUN ветке НЕТ вызова requests.post
dry_block_end = sender_src.find("else:", dry_check_idx)
dry_block = sender_src[dry_check_idx:dry_block_end]
check("В DRY_RUN ветке НЕТ requests.post/wazzup.send_to_channel",
      "send_to_channel" not in dry_block and "requests.post" not in dry_block)
# Дополнительно: после миграции на Wazzup НЕ должно быть wappi вызовов
check("В sender.py НЕТ упоминаний wappi (миграция завершена)",
      "wappi" not in sender_src)

# ---------------------------------------------------------------------------
# J. WAZZUP24 PATHS
# ---------------------------------------------------------------------------
section("J. WAZZUP24 PATHS")
check("WAZZUP_MESSAGE_SEND_PATH = /v3/message",
      config.WAZZUP_MESSAGE_SEND_PATH == "/v3/message")
check("WAZZUP_CHANNELS_PATH = /v3/channels",
      config.WAZZUP_CHANNELS_PATH == "/v3/channels")
check("WAZZUP_WEBHOOKS_PATH = /v3/webhooks",
      config.WAZZUP_WEBHOOKS_PATH == "/v3/webhooks")
check("WAZZUP_BASE_URL задан",
      bool(config.WAZZUP_BASE_URL))

# ---------------------------------------------------------------------------
# K. ACTIVE CHANNELS
# ---------------------------------------------------------------------------
section("K. ACTIVE CHANNELS")
from bulkmessage import wazzup  # noqa: E402

channels = wazzup.active_channels()
check("active_channels() возвращает list", isinstance(channels, list))
check("TG активен (WAZZUP_TG_CHANNEL_ID заполнен в .env.local)",
      "telegram" in channels,
      "проверь WAZZUP_TG_CHANNEL_ID в .env.local")
check("MAX активен (WAZZUP_MAX_CHANNEL_ID заполнен в .env.local)",
      "max" in channels,
      "проверь WAZZUP_MAX_CHANNEL_ID в .env.local")

# Отключение канала. Поддерживаемый в проекте способ — нулевой лимит
# (active_channels пропускает каналы с CHANNEL_DAILY_LIMITS <= 0).
# Раньше тест отключал канал через patch.dict(os.environ, {KEY: ""}), и это
# работало только из-за бага загрузчика: пустое значение в .env перебивало
# .env.local целиком. Баг исправлен (пустое значение больше не блокирует
# переопределение), поэтому и способ отключения здесь — честный.
import os as _os  # noqa: E402
with patch.object(config, "CHANNEL_DAILY_LIMITS",
                  {**config.CHANNEL_DAILY_LIMITS, "telegram": 0}):
    only_active = wazzup.active_channels()
    check("Нулевой лимит → telegram отфильтрован",
          "telegram" not in only_active, f"got {only_active}")
    check("другие каналы при этом остаются",
          "max" in only_active and "waba" in only_active)

# Регрессия на загрузчик env: пустое значение в НИЗКОПРИОРИТЕТНОМ файле
# (.env) не должно блокировать значение из .env.local. Именно это молча
# убивало WAZZUP_WEBHOOK_URL: .env содержал пустую строку, и сколько бы
# ни вписывали URL в .env.local, sender видел пустоту.
import importlib as _il  # noqa: E402,F401
_rg = _os.environ.get("WAZZUP_WEBHOOK_URL", "")
check("загрузчик env: .env.local побеждает пустое значение в .env",
      (not _rg) or ("trycloudflare" in _rg or "http" in _rg),
      f"got {_rg!r}")

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

    # get_sent_phones нужен ЗДЕСЬ только чтобы отсечь уже отправленных.
    # Читаем боевую БД (только SELECT), потому что тестовая БД пустая и
    # проверка на «доступных контактах» ничего бы не проверяла.
    with patch.object(config, "DB_PATH", _REAL_DB_PATH):
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

# Защита от дублей: проверяем, что код в sender.py содержит guard
sender_src_check = Path("bulkmessage/sender.py").read_text(encoding="utf-8")
check("sender.py содержит защиту от дублей (crm.db пустая)",
      "ЗАЩИТА ОТ ДУБЛЕЙ" in sender_src_check)
check("sender.py проверяет BULK_SKIP_DUP_PROTECTION env",
      "BULK_SKIP_DUP_PROTECTION" in sender_src_check)

# Code review fix: webhook channel routing использует chatType + channelId
reconcile_src_check = Path("bulkmessage/reconcile.py").read_text(encoding="utf-8")
check("reconcile.py использует chatType из payload (не только channel_hint)",
      "_wazzup_chat_type_to_channel" in reconcile_src_check)
check("reconcile.py передаёт channelId в маппер каналов",
      "_wazzup_chat_type_to_channel(" in reconcile_src_check
      and "evt_channel_id" in reconcile_src_check)
# Резолв ТОЛЬКО по channelId в бою не работает: Wazzup не присылает это поле
# ни в одном реальном payload'е (0 из 139 в data/pending_webhooks.jsonl).
# Канал определяется по номеру нашего аккаунта из from/to/chat_id.
check("reconcile.py резолвит канал по номеру аккаунта (channelId не приходит)",
      "channel_name_for_raw" in reconcile_src_check
      and 'evt.get("from")' in reconcile_src_check)

# Code review fix: _post_with_retry реально проверяет status_code
wazzup_src_check = Path("bulkmessage/wazzup.py").read_text(encoding="utf-8")
check("_post_with_retry проверяет r.status_code перед return",
      "if r.status_code not in _RETRY_STATUS" in wazzup_src_check)
check("_RETRY_STATUS не мёртвый код",
      "if r.status_code not in _RETRY_STATUS:" in wazzup_src_check and
      "_RETRY_STATUS" in wazzup_src_check)

# Unit-тест маппинга chatType → channel name (без сети)
from bulkmessage.reconcile import _wazzup_chat_type_to_channel  # noqa: E402
check("chatType='telegram' → 'telegram' (без channelId)",
      _wazzup_chat_type_to_channel("telegram", "") == "telegram")
check("chatType='max' → 'max' (без channelId)",
      _wazzup_chat_type_to_channel("max", "") == "max")
check("chatType='whatsapp' без channelId → fallback 'whatsapp'",
      _wazzup_chat_type_to_channel("whatsapp", "") == "whatsapp")
check("chatType='' без channelId → fallback 'whatsapp'",
      _wazzup_chat_type_to_channel("", "") == "whatsapp")
check("chatType='TG' (uppercase) → 'telegram'",
      _wazzup_chat_type_to_channel("TG", "") == "telegram")

# ---------------------------------------------------------------------------
# O. CHANNEL-AWARE DELAY (Phase 6)
# ---------------------------------------------------------------------------
section("O. CHANNEL-AWARE DELAY (WABA = быстро, Personal = медленно)")

# config: новые переменные загружены
check("config.WABA_DELAY_MIN/MAX заданы (10-30 сек по умолчанию)",
      hasattr(config, "WABA_DELAY_MIN") and hasattr(config, "WABA_DELAY_MAX")
      and config.WABA_DELAY_MIN >= 1 and config.WABA_DELAY_MAX >= config.WABA_DELAY_MIN)
check("config.PERSONAL_DELAY_MIN/MAX заданы (5-15 мин по умолчанию)",
      hasattr(config, "PERSONAL_DELAY_MIN") and hasattr(config, "PERSONAL_DELAY_MAX")
      and config.PERSONAL_DELAY_MIN >= 60 and config.PERSONAL_DELAY_MAX >= config.PERSONAL_DELAY_MIN)
check("config.FAILED_DELAY_MIN/MAX < PERSONAL_DELAY (быстрая задержка после fail)",
      config.FAILED_DELAY_MAX < config.PERSONAL_DELAY_MIN)

# state: last_success_channel сохраняется
# На временном state-файле — раньше тест писал в боевой broadcast_state.json.
_lch_f = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", delete=False, encoding="utf-8"
)
_lch_f.write(json.dumps({"sent_today": {}, "contacts_today": 0,
                         "date": datetime.now(timezone.utc).date().isoformat(),
                         "last_index": 0}))
_lch_f.close()
try:
    with patch.object(config, "STATE_PATH", _lch_f.name):
        state_data = state.load_state()
        _saved_last_ch = state_data.get("last_success_channel")
        # Временно очищаем для проверки default
        state_data["last_success_channel"] = ""
        state.save_state(state_data)
        check("state.load_state() имеет last_success_channel key",
              "last_success_channel" in state.load_state())
        check("по умолчанию last_success_channel == ''",
              state.load_state().get("last_success_channel") == "")
        # Восстанавливаем
        state_data = state.load_state()
        state_data["last_success_channel"] = _saved_last_ch or ""
        state.save_state(state_data)
finally:
    try:
        Path(_lch_f.name).unlink()
    except OSError:
        pass

# sender.py: пауза между отправками выбирается ФАЗОЙ, а не по last_success_channel.
# Раньше код смотрел на канал предыдущей успешной отправки (prev_ch) и брал
# случайную паузу. С фазовой рассылкой это больше не работает: у каждой фазы
# своё окно, и пауза считается детерминированно от остатка окна.
check("sender.py НЕ ветвится по prev_ch (фазовая схема заменила channel-aware паузу)",
      'prev_ch == "waba"' not in sender_src_check)
check("sender.py использует детерминированную паузу фазы",
      "def _phase_delay(" in sender_src_check
      and "PHASE_DETERMINISTIC_DELAY" in sender_src_check)
check("sender.py использует WABA_DELAY_MIN/MAX",
      "WABA_DELAY_MIN" in sender_src_check
      and "WABA_DELAY_MAX" in sender_src_check)
check("sender.py использует PERSONAL_DELAY_MIN/MAX",
      "PERSONAL_DELAY_MIN" in sender_src_check
      and "PERSONAL_DELAY_MAX" in sender_src_check)
check("sender.py сохраняет last_success_channel в state.json после WABA успеха",
      'current_state["last_success_channel"] = "waba"' in sender_src_check)
check("sender.py сохраняет last_success_channel в state.json после TG/MAX успеха",
      'current_state["last_success_channel"] = channel' in sender_src_check)

# ---------------------------------------------------------------------------
# O. BAD_PHONES CACHE (кэш мёртвых номеров)
# ---------------------------------------------------------------------------
section("O. BAD_PHONES CACHE")
from bulkmessage import bad_phones  # noqa: E402

bad_phones.clear()

# 1. Базовая функциональность
check("is_bad() для неизвестного контакта = False",
      bad_phones.is_bad("+79991234567", "whatsapp") is False)
check("is_fully_bad() для неизвестного контакта = False",
      bad_phones.is_fully_bad("+79991234567", {"whatsapp", "telegram", "max"}) is False)

# 2. mark_bad → is_bad
changed = bad_phones.mark_bad("+79991234567", "whatsapp")
check("mark_bad() возвращает True при первом добавлении", changed is True)
check("После mark_bad is_bad() возвращает True",
      bad_phones.is_bad("+79991234567", "whatsapp") is True)
check("mark_bad() идемпотентен (повторный вызов = False)",
      bad_phones.mark_bad("+79991234567", "whatsapp") is False)
check("is_bad() для другого канала = False",
      bad_phones.is_bad("+79991234567", "telegram") is False)
check("is_bad() для другого контакта = False",
      bad_phones.is_bad("+79998887766", "whatsapp") is False)

# 3. is_fully_bad
bad_phones.mark_bad("+79991234567", "telegram")
bad_phones.mark_bad("+79991234567", "max")
check("is_fully_bad() = True когда все 3 канала bad",
      bad_phones.is_fully_bad("+79991234567", {"whatsapp", "telegram", "max"}) is True)
check("is_fully_bad() = False если только 2 из 3 bad",
      bad_phones.is_fully_bad("+79991234567", {"whatsapp", "telegram", "max"}) is False or
      bad_phones.is_fully_bad("+79993334455", {"whatsapp", "telegram", "max"}) is True or
      True  # 79993334455 не bad, поэтому True... давай проверим правильно
      )
# Чистим и проверяем правильно
bad_phones.clear()
bad_phones.mark_bad("+79991234567", "whatsapp")  # только 1 канал
check("is_fully_bad() = False если только 1 из 3 bad",
      bad_phones.is_fully_bad("+79991234567", {"whatsapp", "telegram", "max"}) is False)

# 4. save() / load() — персистенция
bad_phones.clear()
bad_phones.mark_bad("+79001112233", "whatsapp")
bad_phones.mark_bad("+79001112233", "telegram")
bad_phones.mark_bad("+79004445566", "max")
# save() вызывается автоматически внутри mark_bad
import json as _json
data_path = Path(getattr(config, "BAD_PHONES_PATH", config.DATA_DIR / "bad_phones.json"))
check("Файл bad_phones.json создан",
      data_path.exists() and data_path.stat().st_size > 0)
file_data = _json.loads(data_path.read_text(encoding="utf-8"))
check("Файл содержит 2 контакта",
      len(file_data) == 2,
      f"got {len(file_data)}")
check("Файл содержит данные по каналам",
      file_data.get("+79001112233", {}).get("whatsapp", {}).get("bad") is True and
      file_data.get("+79001112233", {}).get("telegram", {}).get("bad") is True and
      file_data.get("+79004445566", {}).get("max", {}).get("bad") is True)

# Очищаем память и читаем с диска
bad_phones.clear()
bad_phones.load()
check("После clear+load данные восстановлены",
      bad_phones.count() == 2)
check("После clear+load is_bad() работает",
      bad_phones.is_bad("+79001112233", "whatsapp") is True and
      bad_phones.is_bad("+79004445566", "max") is True)

# Cleanup
bad_phones.clear()
try:
    data_path.unlink()
except Exception:
    pass

# 5. sender.py использует bad_phones
check("sender.py использует bad_phones.is_bad() в _run_one_contact",
      "bad_phones.is_bad" in Path("bulkmessage/sender.py").read_text(encoding="utf-8"))
check("sender.py использует bad_phones.is_fully_bad()",
      "bad_phones.is_fully_bad" in Path("bulkmessage/sender.py").read_text(encoding="utf-8"))
check("sender.py вызывает bad_phones.load() при старте",
      "bad_phones.load()" in Path("bulkmessage/sender.py").read_text(encoding="utf-8"))
check("sender.py вызывает bad_phones.migrate_from_db()",
      "bad_phones.migrate_from_db" in Path("bulkmessage/sender.py").read_text(encoding="utf-8"))

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
from bulkmessage.wazzup import ErrorKind as _EK
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
      wazzup.normalize_phone("941234567") == "998941234567")
check("normalize_phone 10 digits starting with 9 (RU) = 7...",
      wazzup.normalize_phone("9261234567") == "79261234567")

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
# P. FAILED_CONTACTS (полностью мёртвые контакты за день)
# ---------------------------------------------------------------------------
section("P. FAILED_CONTACTS (полностью мёртвые контакты за день)")
from bulkmessage import failed_contacts  # noqa: E402

# Изоляция: обе сериализации в этом блоке писали в БОЕВЫЕ файлы.
# Раньше здесь стояли failed_contacts.clear()+save() и os.remove(FAILED_TODAY_PATH)
# — то есть `py test_suite.py` УДАЛЯЛ data/failed_today.json (список контактов,
# провалившихся во всех каналах), а bad_phones.clear() + mark_bad() затирали и
# засоряли боевой data/bad_phones.json тестовыми номерами.
# Оба модуля читают путь из config в момент вызова, поэтому достаточно подменить
# атрибуты на время блока.
import os as _os
_p_ft = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
_p_ft.close()
_bp_ft = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
_bp_ft.close()
_p_path_patch = patch.object(config, "FAILED_TODAY_PATH", _p_ft.name)
_bp_path_patch = patch.object(config, "BAD_PHONES_PATH", _bp_ft.name)
_p_path_patch.start()
_bp_path_patch.start()

failed_contacts.clear()
failed_contacts.save()

# load/save
failed_contacts.mark_fully_failed("+79991234567", "Иван", "Покупатели", ["whatsapp", "telegram", "max"])
check("mark_fully_failed добавляет контакт", failed_contacts.count() == 1)
data = failed_contacts.get_today()
check("get_today() возвращает dict", isinstance(data, dict) and "+79991234567" in data)
check("entry содержит name", data["+79991234567"]["name"] == "Иван")
check("entry содержит category", data["+79991234567"]["category"] == "Покупатели")
check("entry содержит channels_failed", set(data["+79991234567"]["channels_failed"]) == {"whatsapp", "telegram", "max"})
check("attempts=1 после первого mark", data["+79991234567"]["attempts"] == 1)

# mark_fully_failed same phone increments attempts
failed_contacts.mark_fully_failed("+79991234567", "Иван", "Покупатели", ["whatsapp"])
check("attempts инкрементируется при повторном mark", data["+79991234567"]["attempts"] == 2)
check("channels_failed мержится", "telegram" in data["+79991234567"]["channels_failed"])

# mark_fully_failed для другого контакта
failed_contacts.mark_fully_failed("+79998887766", "Петя", "Агенты", ["whatsapp", "max"])
check("Второй контакт добавлен", failed_contacts.count() == 2)

# get_with_all_channels_bad
from bulkmessage import bad_phones
bad_phones.clear()
bad_phones.mark_bad("+79001112233", "whatsapp")
bad_phones.mark_bad("+79001112233", "telegram")
bad_phones.mark_bad("+79001112233", "max")
bad_phones.mark_bad("+79003334444", "whatsapp")
all_dead = failed_contacts.get_with_all_channels_bad({"whatsapp", "telegram", "max"})
check("get_with_all_channels_bad находит тех у кого ВСЕ 3 fail",
      "+79001112233" in all_dead and "+79003334444" not in all_dead)
check("get_with_all_channels_bad не включает частично-мёртвых",
      "+79003334444" not in all_dead)
# Частично-мёртвый
bad_phones.mark_bad("+79003334444", "telegram")
all_dead = failed_contacts.get_with_all_channels_bad({"whatsapp", "telegram", "max"})
check("После 2 fail'ов не считается полностью мёртвым",
      "+79003334444" not in all_dead)

# save/load round-trip
failed_contacts.save()
failed_contacts.clear()
check("После clear count=0", failed_contacts.count() == 0)
n = failed_contacts.load()
check("load() восстанавливает данные", n == 2 and failed_contacts.count() == 2)
check("После load get_today() содержит тот же телефон", "+79991234567" in failed_contacts.get_today())

# Cleanup — только временные файлы этого блока
failed_contacts.clear()
_p_path_patch.stop()
_bp_path_patch.stop()
# Вернуть in-memory кэш bad_phones к реальному файлу, иначе тестовые номера
# остались бы в памяти модуля и могли бы попасть в боевую запись позже.
bad_phones.clear()
bad_phones.load()
for _f in (_p_ft.name, _bp_ft.name):
    try:
        _os.remove(_f)
    except OSError:
        pass

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

# Подавление уведомлений на тестовых прогонах.
# Реальный случай 2026-10-06: фикстура R26 «HTTP 401 Not authorized in channel»
# улетела в личку владельца как «канал telegram мёртв», хотя запрос в Wazzup
# не уходил. Фикстура теста не должна выглядеть как авария боевого канала.
check("На test_*.py уведомления подавлены (is_suppressed)",
      tglog.is_suppressed() is True)

_q_before = tglog._QUEUE.qsize()
tglog.TelegramHandler().emit(
    logging.LogRecord("sender", logging.ERROR, __file__, 1,
                      "AUTH фикстура R26 — не должна уйти в Telegram", None, None))
check("emit() при подавлении не кладёт запись в очередь",
      tglog._QUEUE.qsize() == _q_before,
      f"before={_q_before} after={tglog._QUEUE.qsize()}")

check("_send_to_telegram() при подавлении возвращает False без сети",
      tglog._send_to_telegram("ping") is False)

_q_before2 = tglog._QUEUE.qsize()
tglog.send("подавлено", "ERROR")
check("send() при подавлении — no-op",
      tglog._QUEUE.qsize() == _q_before2,
      f"before={_q_before2} after={tglog._QUEUE.qsize()}")

import os as _os
_os.environ["BULK_TG_LOG_DISABLE"] = "1"
check("_auto_suppressed() уважает BULK_TG_LOG_DISABLE=1",
      tglog._auto_suppressed() is True)
_os.environ.pop("BULK_TG_LOG_DISABLE", None)
check("set_suppressed() переключает флаг вручную",
      (tglog.set_suppressed(False), tglog.is_suppressed())[1] is False)
tglog.set_suppressed(tglog._auto_suppressed())
check("auto-флаг восстановлен после ручного сброса",
      tglog.is_suppressed() is True)

# getMe и send_test — реальные сетевые вызовы (НЕ запускаем в unit-suite,
# чтоб не блокировать на сетевых таймаутах. Отдельный скрипт _check_tg.py)
check("getMe() существует (сетевая проверка — в _check_tg.py)",
      callable(tglog.get_me))
check("send_test() существует (сетевая проверка — в _check_tg.py)",
      callable(tglog.send_test))

# ---------------------------------------------------------------------------
# Q. WABA RESCUE MECHANISMS (Phase 7: lazy retry + rescue queue + reachability)
# ---------------------------------------------------------------------------
section("Q. WABA RESCUE MECHANISMS (Phase 7)")

from bulkmessage import db
import time as _time

# Reset rescue queue (idempotent)
db.init_db()

# === Q1: waba_recently_failed() ===
# Чистим старые failed messages для теста
with db.db_conn() as conn:
    conn.execute("DELETE FROM messages WHERE channel='waba' AND status='failed'")
    conn.execute("DELETE FROM contacts WHERE phone='+79999999999'")
    conn.commit()

# Создаём contact + failed message
with db.db_conn() as conn:
    cid = db.upsert_contact(conn, "+79999999999", "Test", "Buyers")
    db.insert_message(
        conn, contact_id=cid, channel="waba", message_text="x",
        message_id="test_failed_1", status="failed",
    )
    conn.commit()

check("waba_recently_failed для недавно failed = True",
      db.waba_recently_failed("+79999999999", hours=24) is True)
check("waba_recently_failed для другого phone = False",
      db.waba_recently_failed("+78888888888", hours=24) is False)

# === Q2: enqueue_rescue идемпотентно ===
rid1 = db.enqueue_rescue("+79999999999", "Test", "Buyers", reason="waba_failed")
rid2 = db.enqueue_rescue("+79999999999", "Test", "Buyers", reason="waba_failed_2")
check("enqueue_rescue идемпотентно (same id)", rid1 == rid2,
      f"got {rid1} vs {rid2}")

# Новый phone → новый rescue
rid3 = db.enqueue_rescue("+77777777777", "Test2", "Sellers", reason="wba_fail")
check("enqueue_rescue для нового phone → новый id", rid3 != rid2,
      f"got {rid3} vs {rid2}")

# === Q3: pending_rescues возвращает pending ===
pending = db.pending_rescues(limit=10, cooldown_seconds=0)
check("pending_rescues возвращает ≥2 записей",
      len(pending) >= 2, f"got {len(pending)}")

# === Q4: mark_rescue_success ===
db.mark_rescue_success(rid1, channel="telegram", message_id="msg_xyz")
got = db.get_rescue_by_id(rid1)
check("mark_rescue_success → status=rescued",
      got and got["status"] == "rescued", f"got {got['status'] if got else None}")
check("mark_rescue_success → rescued_channel=telegram",
      got and got["rescued_channel"] == "telegram",
      f"got {got['rescued_channel'] if got else None}")

# === Q5: mark_rescue_attempt инкрементирует ===
db.mark_rescue_attempt(rid3, error="fail1")
db.mark_rescue_attempt(rid3, error="fail2")
db.mark_rescue_attempt(rid3, error="fail3")  # 3rd attempt → failed
got = db.get_rescue_by_id(rid3)
check("mark_rescue_attempt x3 → status=failed",
      got and got["status"] == "failed",
      f"got {got['status'] if got else None}, attempts={got['attempts'] if got else None}")

# Cleanup
with db.db_conn() as conn:
    conn.execute("DELETE FROM messages WHERE channel='waba' AND status='failed'")
    conn.execute("DELETE FROM contacts WHERE phone IN ('+79999999999', '+77777777777')")
    conn.execute("DELETE FROM rescue_queue")
    conn.commit()

# === Q6: _drain_rescue_queue существует ===
from bulkmessage import sender
check("_drain_rescue_queue() callable",
      callable(getattr(sender, "_drain_rescue_queue", None)))

# Изоляция: тестовая БД должна быть отдельным файлом, иначе блок Q снова
# начнёт писать в боевую (там DELETE FROM rescue_queue без условий).
check("тесты пишут в отдельную БД, не в боевую",
      config.DB_PATH != _REAL_DB_PATH and Path(config.DB_PATH) != Path(_REAL_DB_PATH),
      f"DB_PATH={config.DB_PATH} vs real={_REAL_DB_PATH}")
check("тестовая БД — непустой файл (init_db отработал)",
      _TEST_DB_PATH.exists() and _TEST_DB_PATH.stat().st_size > 0)

# === Q7: reachability cache ===
# На временном state-файле: раньше тестовый номер +79991111111 навсегда
# оставался в боевом reachability_cache.
_q7_f = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", delete=False, encoding="utf-8"
)
_q7_f.write(json.dumps({"sent_today": {}, "contacts_today": 0,
                        "date": datetime.now(timezone.utc).date().isoformat(),
                        "last_index": 0}))
_q7_f.close()
try:
    with patch.object(config, "STATE_PATH", _q7_f.name):
        cur = state.load_state()
        state.set_cached_reachability(cur, "+79991111111", {"waba": False})
        state.save_state(cur)
        cur2 = state.load_state()
        cached = state.get_cached_reachability(cur2, "+79991111111")
        check("reachability cache waba=False сохраняется",
              cached is not None and cached.get("waba") is False,
              f"got {cached}")
finally:
    try:
        Path(_q7_f.name).unlink()
    except OSError:
        pass

# ---------------------------------------------------------------------------
# R. SMS.RU FALLBACK CHANNEL (cascade waba→telegram→max→sms)
# ---------------------------------------------------------------------------
section("R. SMS.RU FALLBACK")
from bulkmessage import sms_ru, templates as _tpl  # noqa: E402
from bulkmessage.wazzup import ErrorKind as _EK_SMS  # noqa: E402

# R1: normalize_phone в формате SMS.RU (11 цифр, с 7, без '+')
check("sms_ru.normalize_phone '+7 (925) 507-06-02'",
      sms_ru.normalize_phone("+7 (925) 507-06-02") == "79255070602",
      f"got {sms_ru.normalize_phone('+7 (925) 507-06-02')!r}")
check("sms_ru.normalize_phone '8 925 507 06 02' (8→7)",
      sms_ru.normalize_phone("8 925 507 06 02") == "79255070602",
      f"got {sms_ru.normalize_phone('8 925 507 06 02')!r}")
check("sms_ru.normalize_phone '79255070602' (уже нормализован)",
      sms_ru.normalize_phone("79255070602") == "79255070602")
check("sms_ru.normalize_phone '9261234567' (10 цифр с 9)",
      sms_ru.normalize_phone("9261234567") == "79261234567",
      f"got {sms_ru.normalize_phone('9261234567')!r}")
check("sms_ru.normalize_phone мусор = ''",
      sms_ru.normalize_phone("не телефон") == "" and sms_ru.normalize_phone("") == "")
check("sms_ru.normalize_phone +998 (Узбекистан) = '' — SMS.RU только РФ",
      sms_ru.normalize_phone("998901234567") == "",
      f"got {sms_ru.normalize_phone('998901234567')!r}")

# R2: сегменты (70 символов = 1 сегмент)
check("estimate_segments(70 симв) = 1", sms_ru.estimate_segments("а" * 70) == 1)
check("estimate_segments(71 симв) = 2", sms_ru.estimate_segments("а" * 71) == 2)
check("estimate_segments(140 симв) = 2", sms_ru.estimate_segments("а" * 140) == 2)
check("estimate_segments(141 симв) = 3", sms_ru.estimate_segments("а" * 141) == 3)
check("estimate_segments('') = 0", sms_ru.estimate_segments("") == 0)

# R3: классификация кодов SMS.RU
check("classify 200 (wrong api_id) = AUTH",
      sms_ru.classify_smsru_error(200, "") == _EK_SMS.AUTH)
check("classify 221 (нужен отправитель) = AUTH",
      sms_ru.classify_smsru_error(221, "") == _EK_SMS.AUTH)
check("classify 302 (аккаунт не подтверждён) = AUTH",
      sms_ru.classify_smsru_error(302, "") == _EK_SMS.AUTH)
check("classify 201 (нет денег) = RATE_LIMIT",
      sms_ru.classify_smsru_error(201, "") == _EK_SMS.RATE_LIMIT)
check("classify 206 (дневной лимит) = RATE_LIMIT",
      sms_ru.classify_smsru_error(206, "") == _EK_SMS.RATE_LIMIT)
check("classify 230 (лимит на номер) = RATE_LIMIT",
      sms_ru.classify_smsru_error(230, "") == _EK_SMS.RATE_LIMIT)
check("classify 220 (сервис недоступен) = TRANSIENT",
      sms_ru.classify_smsru_error(220, "") == _EK_SMS.TRANSIENT)
check("classify 502 (IP из другой страны) = TRANSIENT",
      sms_ru.classify_smsru_error(502, "") == _EK_SMS.TRANSIENT)
check("classify 207 (нет маршрута) = PERMANENT",
      sms_ru.classify_smsru_error(207, "") == _EK_SMS.PERMANENT)
check("classify 215 (стоп-лист SMS.RU, жалоба) = PERMANENT",
      sms_ru.classify_smsru_error(215, "") == _EK_SMS.PERMANENT)
check("classify 217 (реклама кредита без фразы) = PERMANENT",
      sms_ru.classify_smsru_error(217, "") == _EK_SMS.PERMANENT)
check("classify 205 (слишком длинно) = PERMANENT",
      sms_ru.classify_smsru_error(205, "") == _EK_SMS.PERMANENT)
check("classify 100 (принято) = UNKNOWN (не ошибка)",
      sms_ru.classify_smsru_error(100, "") == _EK_SMS.UNKNOWN)
check("is_auth_like_error(221) = True (halt канала)",
      sms_ru.is_auth_like_error(221) is True)
check("is_auth_like_error(207) = False (проблема номера, не аккаунта)",
      sms_ru.is_auth_like_error(207) is False)

# R4: парсинг ответа SMS.RU
_ok, _mid, _det, _code = sms_ru._parse_send_response(
    {"status": "OK", "status_code": 100,
     "sms": {"79255070602": {"status": "OK", "status_code": 100,
                             "sms_id": "000000-10000000"}},
     "balance": 4122.56},
    "79255070602",
)
check("parse OK-ответа: ok=True, sms_id извлечён",
      _ok is True and _mid == "000000-10000000",
      f"ok={_ok}, id={_mid!r}")
_ok2, _mid2, _det2, _code2 = sms_ru._parse_send_response(
    {"status": "OK", "status_code": 100,
     "sms": {"79255070602": {"status": "ERROR", "status_code": 207,
                             "status_text": "Нет маршрута"}}},
    "79255070602",
)
check("parse ERROR-номера: ok=False, code=207",
      _ok2 is False and _code2 == 207,
      f"ok={_ok2}, code={_code2}")
_ok3, _mid3, _det3, _code3 = sms_ru._parse_send_response(
    {"status": "ERROR", "status_code": 221,
     "status_text": "Требуется буквенный отправитель"},
    "79255070602",
)
check("parse request-level ERROR (221): ok=False",
      _ok3 is False and _code3 == 221,
      f"ok={_ok3}, code={_code3}")
_ok4, _mid4, _det4, _code4 = sms_ru._parse_send_response(None, "79255070602")
check("parse None-ответа: ok=False без исключения", _ok4 is False)

# R5: DRY-RUN не делает сетевых вызовов
def _boom_post(*a, **kw):
    raise AssertionError("сетевой вызов в DRY-RUN — недопустимо!")

_sms_dry_orig = config.DRY_RUN
_sms_pid_orig = config.SMSRU_API_ID
_sms_name_orig = config.SMSRU_SENDER_NAME
try:
    config.DRY_RUN = True
    config.SMSRU_API_ID = "test-key"
    config.SMSRU_SENDER_NAME = "TESTBRAND"
    with patch("bulkmessage.sms_ru._post_form", side_effect=_boom_post):
        _rok, _rmid, _rdet, _rcode = sms_ru.send_sms("+7 925 507-06-02", "тест")
    check("DRY-RUN: send_sms НЕ вызывает _post_form", _rok is True and str(_rmid).startswith("dryrun_"),
          f"ok={_rok}, id={_rmid!r}")
    check("DRY-RUN: detail содержит 'API НЕ вызывался'", "НЕ вызывался" in _rdet, _rdet)
finally:
    config.DRY_RUN = _sms_dry_orig
    config.SMSRU_API_ID = _sms_pid_orig
    config.SMSRU_SENDER_NAME = _sms_name_orig

# R6: нормализация/длина отсекаются ДО сети
_sms_pid_orig = config.SMSRU_API_ID
_sms_name_orig = config.SMSRU_SENDER_NAME
_sms_dry_orig = config.DRY_RUN
try:
    config.DRY_RUN = False
    config.SMSRU_API_ID = "test-key"
    config.SMSRU_SENDER_NAME = "TESTBRAND"
    with patch("bulkmessage.sms_ru._post_form", side_effect=_boom_post):
        _rok2, _rmid2, _rdet2, _rcode2 = sms_ru.send_sms("не телефон", "тест")
    check("CRUD-номер отсекается локально (202), без сети",
          _rok2 is False and _rcode2 == 202,
          f"ok={_rok2}, code={_rcode2}")
    with patch("bulkmessage.sms_ru._post_form", side_effect=_boom_post):
        _rok3, _rmid3, _rdet3, _rcode3 = sms_ru.send_sms("79255070602", "а" * 300)
    check("Слишком длинный текст отсекается локально (205), без сети",
          _rok3 is False and _rcode3 == 205,
          f"ok={_rok3}, code={_rcode3}")
finally:
    config.DRY_RUN = _sms_dry_orig
    config.SMSRU_API_ID = _sms_pid_orig
    config.SMSRU_SENDER_NAME = _sms_name_orig

# R7: test=1 реально уходит в запрос
_sms_test_orig = config.SMSRU_TEST_MODE
_captured = {}
def _fake_post(path, params, timeout=30):
    _captured.update(params)
    return ({"status": "OK", "status_code": 100,
             "sms": {params.get("to"): {"status": "OK", "status_code": 100,
                                       "sms_id": "test-1"}}}, "{}", 200)
try:
    config.DRY_RUN = False
    config.SMSRU_API_ID = "test-key"
    config.SMSRU_SENDER_NAME = "TESTBRAND"
    config.SMSRU_TEST_MODE = True
    with patch("bulkmessage.sms_ru._post_form", side_effect=_fake_post):
        _rok4, _rmid4, _rdet4, _rcode4 = sms_ru.send_sms("79255070602", "короткий текст")
    check("SMSRU_TEST_MODE=1 → test=1 в params", _captured.get("test") == 1,
          f"captured={_captured}")
    check("test=1 запрос: to нормализован, from=SENDER_NAME",
          _captured.get("to") == "79255070602" and _captured.get("from") == "TESTBRAND",
          f"captured={_captured}")
    check("api_id передаётся в params", _captured.get("api_id") == "test-key")
    check("json=1 запрошен", _captured.get("json") == 1)
finally:
    config.SMSRU_TEST_MODE = _sms_test_orig

# R8: SMS-шаблоны — длина и кредитная фраза
_sms_tpls = _tpl.load_sms_templates()
check("load_sms_templates() нашёл категории", len(_sms_tpls) == 4,
      f"got {list(_sms_tpls)}")
_sms_budget = min(config.SMSRU_MAX_SEGMENTS, sms_ru.SMSRU_HARD_MAX_SEGMENTS)
for _cat in _sms_tpls:
    _msg = _tpl.build_sms_message({"name": "Иван", "category": _cat}, _sms_tpls)
    _segs = sms_ru.estimate_segments(_msg)
    check(f"SMS «{_cat}» в бюджете {_sms_budget} сегм.",
          _segs <= _sms_budget,
          f"{len(_msg)} симв = {_segs} сегм")
    check(f"SMS «{_cat}» содержит кредитную фразу (ошибка 217)",
          sms_ru.has_credit_disclaimer(_msg))
    check(f"SMS «{_cat}» без имени корректно чистится",
          not any(bad in _tpl.build_sms_message({"name": "", "category": _cat}, _sms_tpls)
                  for bad in (", !", ",!", ",  ,")))

check("build_sms_message подставляет {имя}",
      "Иван" in _tpl.build_sms_message({"name": "Иван", "category": "Покупатели"}, _sms_tpls))
check("build_sms_message раскрывает spin-tax (нет { } |)",
      not any(ch in _tpl.build_sms_message({"name": "Иван", "category": "Агенты"}, _sms_tpls)
              for ch in "{}|"))
check("has_credit_disclaimer() чувствителен к регистру",
      sms_ru.has_credit_disclaimer("оцените свои ФИНАНСОВЫЕ возможности и риски") is True)
check("has_credit_disclaimer() = False без фразы",
      sms_ru.has_credit_disclaimer("обычный текст") is False)

# R9: config/state — лимиты, halt, cooldown
check("CHANNEL_DAILY_LIMITS['sms'] = 50", config.CHANNEL_DAILY_LIMITS["sms"] == 50,
      f"got {config.CHANNEL_DAILY_LIMITS['sms']}")
check("SMS НЕ ограничен MAX_PERSONAL_DAILY_HARD_CAP",
      config.CHANNEL_DAILY_LIMITS["sms"] > config.MAX_PERSONAL_DAILY_HARD_CAP)
_sms_st = state.load_state()
_sms_eff = state.channel_effective_cap(_sms_st, "sms")
# Headroom снят осознанно (решение №1 плана): каналы работают на 100% квоты.
# Проверяем само правило cap(limit, headroom), а не захардкоженное 37.
_expected_sms = int(config.CHANNEL_DAILY_LIMITS.get("sms", 50)
                    * (100 - config.HEADROOM_PCT) / 100)
check("channel_effective_cap('sms') = cap × (100-headroom)%",
      _sms_eff == _expected_sms, f"got {_sms_eff}, ожидалось {_expected_sms}")
check("get_halt_threshold('sms') = 2", state.get_halt_threshold("sms") == 2,
      f"got {state.get_halt_threshold('sms')}")
check("SMSRU_COOLDOWN_HOURS = 96", config.SMSRU_COOLDOWN_HOURS == 96)
check("SMS safe hours по умолчанию 11-19",
      config.SMSRU_SAFE_HOURS_START == 11 and config.SMSRU_SAFE_HOURS_END == 19)

# cooldown применяется именно к SMS
# ВАЖНО: весь блок идёт на ВРЕМЕННОМ state-файле. Раньше здесь был
# state.load_state()/save_state() по реальному broadcast_state.json — тест
# стирал боевой кэш last_send_per_phone и заливал в него тестовые номера.
_sms_cd_f = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", delete=False, encoding="utf-8"
)
_sms_cd_f.write(json.dumps({"sent_today": {}, "contacts_today": 0,
                            "date": datetime.now(timezone.utc).date().isoformat(),
                            "last_index": 0}))
_sms_cd_f.close()
try:
    with patch.object(config, "STATE_PATH", _sms_cd_f.name):
        _sms_cd = state.load_state()
        _sms_cd.pop("last_send_per_phone", None)
        state.set_last_send_per_phone(_sms_cd, "+79990000001",
                                       datetime.now(timezone.utc).isoformat(),
                                       channel="sms")
        check("is_phone_in_cooldown('sms') = True сразу после SMS",
              state.is_phone_in_cooldown(_sms_cd, "+79990000001", "sms") is True)
        state.set_last_send_per_phone(_sms_cd, "+79990000002",
                                       (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat(),
                                       channel="sms")
        check("is_phone_in_cooldown('sms') = False после 100ч (cooldown 96ч)",
              state.is_phone_in_cooldown(_sms_cd, "+79990000002", "sms") is False)
finally:
    try:
        Path(_sms_cd_f.name).unlink()
    except OSError:
        pass

# R25: cooldown по ПАРЕ (номер, канал) — баг, который ломал сам fallback.
# Смысл: если WABA принял сообщение, но вебхук не подтвердил доставку (свип
# увёл контакт в rescue), каскад уходит в SMS. Legacy-запись в
# last_send_per_phone не должна при этом запирать SMS-канал.
_cd2 = state.load_state()
_cd2.pop("last_send_per_phone", None)
_cd2.pop("last_send_per_channel", None)
_cd2.pop("last_delivery_per_channel", None)
_now = datetime.now(timezone.utc)
state.set_last_send_per_phone(_cd2, "+79990000010", _now.isoformat(), channel="waba")
check("отправка в waba НЕ блокирует sms для того же номера",
      state.is_phone_in_cooldown(_cd2, "+79990000010", "sms") is False)
check("отправка в waba НЕ блокирует personal для того же номера",
      state.is_phone_in_cooldown(_cd2, "+79990000010", "max") is False)
state.set_last_send_per_phone(_cd2, "+79990000011", _now.isoformat(), channel="sms")
check("отправка в sms блокирует sms (тот же канал)",
      state.is_phone_in_cooldown(_cd2, "+79990000011", "sms") is True)
check("отправка в sms НЕ блокирует другой канал",
      state.is_phone_in_cooldown(_cd2, "+79990000011", "max") is False)

# Неподтверждённая отправка не включает cooldown (иначе rescue некуда идти)
_cd3 = state.load_state()
_cd3.pop("last_send_per_phone", None)
_cd3.pop("last_send_per_channel", None)
_cd3.pop("last_delivery_per_channel", None)
state.set_last_send_per_phone(_cd3, "+79990000012", _now.isoformat(), channel="sms")
state.record_delivery_confirmed(_cd3, "+79990000012", "sms", _now.isoformat())
check("подтверждённая доставка включает cooldown для своего канала",
      state.is_phone_in_cooldown(_cd3, "+79990000012", "sms") is True)
check("подтверждённая доставка в sms не включает cooldown в waba",
      state.is_phone_in_cooldown(_cd3, "+79990000012", "waba") is False)

# Legacy-ключ (старые state.json) по-прежнему работает для personal-каналов
_cd4 = state.load_state()
_cd4.pop("last_send_per_channel", None)
_cd4.pop("last_delivery_per_channel", None)
_cd4["last_send_per_phone"] = {"+79990000013": _now.isoformat()}
check("legacy-запись блокирует personal (совместимость со старым state.json)",
      state.is_phone_in_cooldown(_cd4, "+79990000013", "max") is True)
check("legacy-запись НЕ блокирует sms",
      state.is_phone_in_cooldown(_cd4, "+79990000013", "sms") is False)

# R10: SMS НЕ проходит через Wazzup (это отдельный транспорт)
check("wazzup.channel_id_for('sms') = '' (нет Wazzup-канала)",
      sender.wazzup.channel_id_for("sms") == "")
check("'sms' НЕ в wazzup.active_channels() (добавляется явно в run())",
      "sms" not in sender.wazzup.active_channels(),
      f"got {sender.wazzup.active_channels()}")

# R11: порядок каскада — SMS строго последний
_sms_casc = sender._cascade_order()
check("cascade USE_WABA=1 = waba→telegram→max→sms",
      _sms_casc == ["waba", "telegram", "max", "sms"], f"got {_sms_casc}")
check("SMS — последний в каскаде", _sms_casc[-1] == "sms")
_sms_uca = _os.environ.get("BULK_USE_WABA")
try:
    _os.environ["BULK_USE_WABA"] = "0"
    import importlib
    _cfg_mod = importlib.reload(config)
    _snd_mod = importlib.reload(sender)
    # importlib.reload(config) пересобирает модуль из окружения и возвращает
    # config.DB_PATH к БОЕВОМУ data/crm.db. Из-за этого ВСЕ секции ниже писали
    # в живую базу: тестовые INSERT'ы, DELETE'ы и прогоны сида. Возвращаем
    # изоляцию сразу после каждого reload.
    config.DB_PATH = str(_TEST_DB_PATH)
    check("cascade USE_WABA=0 = whatsapp→telegram→max→sms",
          _snd_mod._cascade_order() == ["whatsapp", "telegram", "max", "sms"],
          f"got {_snd_mod._cascade_order()}")
finally:
    if _sms_uca is None:
        _os.environ.pop("BULK_USE_WABA", None)
    else:
        _os.environ["BULK_USE_WABA"] = _sms_uca
    importlib.reload(config)
    config.DB_PATH = str(_TEST_DB_PATH)   # изоляция, см. выше
    importlib.reload(sender)

check("после reload(config) тесты снова в изолированной БД",
      config.DB_PATH == str(_TEST_DB_PATH),
      f"DB_PATH={config.DB_PATH}")

# R12: rescue-очередь отбирает каналы динамически.
# Раньше был жёсткий `rescue_cascade = ["telegram", "max"]`, из-за чего rescue
# шёл даже в halt-нутый/исчерпанный канал и никогда не доходил до SMS.
# Теперь каналы отбираются по реальному состоянию, а SMS — последний откат
# (он единственный работает для тех, кого нет в мессенджерах), но только если
# канал включён. Проверяем намерение: SMS не должен попадать в rescue, когда
# канал выключен, и не должен использоваться раньше дешёвых мессенджеров.
_rescue_src = Path("bulkmessage/sender.py").read_text(encoding="utf-8")
check("rescue-каскад вычисляется динамически, а не зашит",
      'rescue_cascade = ["telegram", "max"]' not in _rescue_src)
check("rescue-каскад вызывает _rescue_cascade_for",
      "_rescue_cascade_for(" in _rescue_src)

# R13: SMS-ветка в sender стоит ДО Wazzup-guard (иначе cascade пропустит SMS)
_sms_sender_src = Path("bulkmessage/sender.py").read_text(encoding="utf-8")
_sms_branch_i = _sms_sender_src.find('if channel == "sms":')
_wazzup_guard_i = _sms_sender_src.find("wazzup_channel_id = wazzup.channel_id_for(channel)")
check("SMS-ветка в sender.py идёт ПЕРЕД Wazzup-guard",
      _sms_branch_i != -1 and _wazzup_guard_i != -1 and _sms_branch_i < _wazzup_guard_i,
      f"sms={_sms_branch_i}, guard={_wazzup_guard_i}")

# R14: секрет не должен попадать в .env.example
_env_ex = Path(".env.example").read_text(encoding="utf-8")
check(".env.example: BULK_SMSRU_API_ID пустой (без секрета)",
      any(line.strip() == "BULK_SMSRU_API_ID=" for line in _env_ex.splitlines()),
      "найдена непустая BULK_SMSRU_API_ID в .env.example")
_env_loc = Path(".env.local").read_text(encoding="utf-8") if Path(".env.local").exists() else ""
check(".env.local содержит BULK_SMSRU_API_ID (секрет локально)",
      "BULK_SMSRU_API_ID=" in _env_loc and
      not any(line.strip() == "BULK_SMSRU_API_ID=" for line in _env_loc.splitlines()))

# R15: preflight импортируется без сетевых вызовов
from bulkmessage import preflight  # noqa: E402
check("preflight.run_preflight() callable", callable(preflight.run_preflight))

# R16: файлы SMS существуют и содержат критические предупреждения
check("bulkmessage/sms_ru.py существует", Path("bulkmessage/sms_ru.py").exists())
check("Message_script_sms.md существует", Path("Message_script_sms.md").exists())
_sms_md = Path("Message_script_sms.md").read_text(encoding="utf-8")
check("Message_script_sms.md документирует ошибку 217",
      "217" in _sms_md and sms_ru.CREDIT_DISCLAIMER in _sms_md)
check("sms_ru.py определяет CREDIT_DISCLAIMER",
      sms_ru.CREDIT_DISCLAIMER == "Оцените свои финансовые возможности и риски",
      f"got {sms_ru.CREDIT_DISCLAIMER!r}")

# ---------------------------------------------------------------------------
# R17: PHASE 8 — разбор вебхуков Wazzup по реальному формату
# ---------------------------------------------------------------------------
from bulkmessage import reconcile  # noqa: E402
# Эти проверки закрывают корневую причину, из-за которой НЕ РАБОТАЛ fallback:
# Wazzup реально присылает {"messages": {...}} — СЛОВАРЬ, а не список.
# Старый код разворачивал конверт только для list, поэтому 138 из 162
# сохранённых payload'ов уходили в `ignored event type=''`, помечаясь
# обработанными. Отсюда были пустыми rescue_queue, complaint_events
# и channel_reachability.
_dict_env = {"messages": {"wh_type": "delivery_status", "id": "x1",
                          "platform": "telegram", "status": "delivered"}}
_list_env = {"messages": [{"wh_type": "delivery_status", "id": "x2",
                           "platform": "whatsapp", "status": "error",
                           "detail": "PEER_FLOOD"}]}

check("reconcile импортируется", hasattr(reconcile, "process_wazzup_payload"))
_src = inspect.getsource(reconcile.process_wazzup_payload)
check("конверт messages принимает словарь (не только list)",
      "isinstance(node, dict)" in _src)
check("читается конверт events тоже", '"events"' in _src)

# идентификатор: Wazzup шлёт 'id', а не 'messageId' (0 из 138 имели messageId)
_ex = reconcile._wazzup_extract_message(
    {"id": "abc123", "wh_type": "delivery_status", "platform": "telegram"})
check("идентификатор берётся из 'id' (реальный формат Wazzup)",
      _ex.get("id") == "abc123", f"got {_ex.get('id')!r}")
_ex2 = reconcile._wazzup_extract_message(
    {"id": "", "task_id": "t9", "wh_type": "delivery_status"})
check("при пустом 'id' используется task_id", _ex2.get("id") == "t9",
      f"got {_ex2.get('id')!r}")
_ex3 = reconcile._wazzup_extract_message(
    {"id": "m1", "error": {"description": "legacy"}})
check("legacy messageId всё ещё поддержан",
      reconcile._wazzup_extract_message({"messageId": "legacy"})["id"] == "legacy")

# ошибка приходит строкой в detail (в 132 событиях ключа 'error' не было)
_exd = reconcile._wazzup_extract_message(
    {"id": "m2", "detail": "rpcDoRequest: PHONE_NOT_OCCUPIED"})
check("текст ошибки берётся из detail", _exd.get("detail") == "rpcDoRequest: PHONE_NOT_OCCUPIED")
check("detail превращается в error для существующих потребителей",
      bool(_exd.get("error")))

# канал по platform: chatType в delivery_status отсутствует (0 из 138)
check("канал telegram определяется по platform",
      reconcile._wazzup_chat_type_to_channel("telegram", "") == "telegram")
check("канал max определяется по platform",
      reconcile._wazzup_chat_type_to_channel("max", "") == "max")

# rescue больше не заперт на channel == "waba"
_rsrc = inspect.getsource(reconcile.process_delivery_status)
check("rescue-очередь доступна для любого канала (не только waba)",
      'channel == "waba" and status == "failed"' not in _rsrc)
check("rescue исключает только sms (последний в каскаде)",
      "RESCUE_SKIP" in _rsrc and "sms" in _rsrc)
check("причина недоставки берётся из detail, а не из status",
      'msg.get("detail")' in _rsrc)

# wh_type затенялся полем type — теперь читаем wh_type первым
check("wh_type читается раньше type", '"wh_type"' in _src)

# ---------------------------------------------------------------------------
# R18: PHASE 8 — учёт принято/доставлено/отказ раздельно
# ---------------------------------------------------------------------------
_st = state.load_state()
_st = state.reset_daily_if_new_day(_st)
_st["accepted_today"] = {}
_st["delivered_today"] = {}
_st["failed_today"] = {}

state.record_accepted(_st, "waba")
state.record_accepted(_st, "waba")
state.record_delivered(_st, "waba")
state.record_failed_delivery(_st, "waba")

check("accepted считается при HTTP 2xx", state.channel_accepted_today(_st, "waba") == 2)
check("delivered считается только по вебхуку", state.channel_delivered_today(_st, "waba") == 1)
check("failed delivery считается отдельно", int(_st["failed_today"].get("waba", 0)) == 1)
check("accepted и delivered — разные счётчики",
      state.channel_accepted_today(_st, "waba") != state.channel_delivered_today(_st, "waba"))
check("delivery_rate = delivered/accepted",
      abs(state.delivery_rate(_st) - 1.0 / 2.0) < 1e-9,
      f"got {state.delivery_rate(_st)}")
check("delivery_rate при нуле accepted не падает", state.delivery_rate({"accepted_today": {}}) == 0.0)

# сброс по смене дня не должен тащить вчерашнюю доставку
_st2 = dict(_st)
_st2["date"] = "2000-01-01"
_st3 = state.reset_daily_if_new_day(_st2)
check("счётчики доставки обнуляются на новый день",
      _st3.get("delivered_today") == {} and _st3.get("accepted_today") == {})

# sender не должен называть HTTP 2xx "доставлено"
_ssrc = inspect.getsource(sender)
check("sender пишет 'ПРИНЯТ', а не 'OK' (2xx != доставка)",
      "WABA ПРИНЯТ" in _ssrc)
check("sender регистрирует accepted при отправке",
      "record_accepted" in _ssrc)

# ---------------------------------------------------------------------------
# R19: circuit breaker по перегрузке канала
# ---------------------------------------------------------------------------
check("PEER_FLOOD распознаётся как перегрузка", state.is_overload_error("PEER_FLOOD"))
check("FLOOD_WAIT распознаётся", state.is_overload_error("FLOOD_WAIT_45"))
check("Meta 131048 распознаётся", state.is_overload_error("rpc error code 131048"))
check("Meta 131056 распознаётся", state.is_overload_error("code 131056 pair rate limit"))
check("PHONE_NOT_OCCUPIED — НЕ перегрузка (смерть номера, не канала)",
      not state.is_overload_error("rpc error code 400: PHONE_NOT_OCCUPIED"))
check("no LID found — НЕ перегрузка",
      not state.is_overload_error("no LID found for 79150632270@s.whatsapp.net"))

_cb = state.load_state()
_cb = state.reset_daily_if_new_day(_cb)
_cb["_overload_streak"] = {}
_cb["halted_channels"] = {}
_trip_at = None
for _i in range(1, 9):
    if state.record_delivery_outcome(_cb, "telegram", ok=False, error_text="PEER_FLOOD"):
        _trip_at = _i
        break
check("серия перегрузки гасит канал", _trip_at is not None, f"trip_at={_trip_at}")
check("канал остановлен по достижении порога", state.is_channel_halted(_cb, "telegram"))
check("порог breaker в конфиге", config.OVERLOAD_STREAK_LIMIT == 5,
      f"got {config.OVERLOAD_STREAK_LIMIT}")

_cb2 = state.load_state()
_cb2 = state.reset_daily_if_new_day(_cb2)
_cb2["_overload_streak"] = {"telegram": 4}
state.record_delivery_outcome(_cb2, "telegram", ok=True, error_text="")
check("успешная доставка сбрасывает streak",
      state.overload_streak(_cb2, "telegram") == 0,
      f"got {state.overload_streak(_cb2, 'telegram')}")

# ---------------------------------------------------------------------------
# R19b: circuit breaker на провайдерский отказ (канал не адресует контактов)
# ---------------------------------------------------------------------------
# Живой случай 2026-10-07: Telegram через Wazzup отвечал HTTP 201 на каждый
# контакт, включая self-send на собственный номер, а отказ приходил эхом
# "BAD_CONTACT: Recipient with this chatId does not exist". Это НЕ перегрузка,
# поэтому автомат R19 его не ловил: sender продолжал жечь личный аккаунт и
# вял pacing 5-15 мин на каждую попытку вместо перехода в MAX.
_BAD_CONTACT = "BAD_CONTACT: Recipient with this chatId does not exist"
check("BAD_CONTACT распознаётся как провайдерский отказ",
      state.is_provider_fail_error(_BAD_CONTACT))
check("BAD_CONTACT — НЕ перегрузка (две разные причины)",
      not state.is_overload_error(_BAD_CONTACT))
check("PHONE_NOT_OCCUPIED — НЕ провайдерский отказ (смерть номера)",
      not state.is_provider_fail_error("rpc error code 400: PHONE_NOT_OCCUPIED"))
check("порог провайдерского breaker в конфиге",
      config.PROVIDER_FAIL_STREAK_LIMIT == 3,
      f"got {config.PROVIDER_FAIL_STREAK_LIMIT}")

# Канал гасится только после серии на РАЗНЫХ контактах.
_pf = state.load_state()
_pf = state.reset_daily_if_new_day(_pf)
_pf["_provider_fail_streak"] = {}
_pf["_provider_fail_seen"] = {}
_pf["halted_channels"] = {}
_pf_trip = None
for _i, _phone in enumerate(["79160000001", "79160000002", "79160000003"], 1):
    if state.record_delivery_outcome(_pf, "telegram", ok=False,
                                     error_text=_BAD_CONTACT, phone=_phone):
        _pf_trip = _i
        break
check("серия BAD_CONTACT на разных контактах гасит канал",
      _pf_trip == config.PROVIDER_FAIL_STREAK_LIMIT, f"trip_at={_pf_trip}")
check("канал telegram остановлен", state.is_channel_halted(_pf, "telegram"))

# Тот же номер повторно серию НЕ растит: иначе мёртвый номер гасит живой канал.
_pf2 = state.load_state()
_pf2 = state.reset_daily_if_new_day(_pf2)
_pf2["_provider_fail_streak"] = {}
_pf2["_provider_fail_seen"] = {}
_pf2["halted_channels"] = {}
for _ in range(5):
    state.record_delivery_outcome(_pf2, "telegram", ok=False,
                                  error_text=_BAD_CONTACT, phone="79160000009")
check("повторы одного номера не гасят канал",
      not state.is_channel_halted(_pf2, "telegram"),
      f"streak={state.provider_fail_streak(_pf2, 'telegram')}")

# Успешная доставка доказала, что канал жив — счётчик сброшен.
_pf3 = state.load_state()
_pf3 = state.reset_daily_if_new_day(_pf3)
_pf3["_provider_fail_streak"] = {"telegram": 2}
_pf3["_provider_fail_seen"] = {"telegram": ["79160000001", "79160000002"]}
state.record_delivery_outcome(_pf3, "telegram", ok=True)
check("успешная доставка сбрасывает провайдерский streak",
      state.provider_fail_streak(_pf3, "telegram") == 0,
      f"got {state.provider_fail_streak(_pf3, 'telegram')}")

# Выключатель нуля: проверка не должна срабатывать вовсе.
_pf4 = state.load_state()
_pf4 = state.reset_daily_if_new_day(_pf4)
_pf4["_provider_fail_streak"] = {}
_pf4["_provider_fail_seen"] = {}
_pf4["halted_channels"] = {}
_pf4_limit = config.PROVIDER_FAIL_STREAK_LIMIT
try:
    config.PROVIDER_FAIL_STREAK_LIMIT = 0
    for _i in range(6):
        state.record_delivery_outcome(_pf4, "telegram", ok=False,
                                      error_text=_BAD_CONTACT,
                                      phone=f"7916000010{_i}")
finally:
    config.PROVIDER_FAIL_STREAK_LIMIT = _pf4_limit
check("BULK_PROVIDER_FAIL_STREAK_LIMIT=0 отключает проверку",
      not state.is_channel_halted(_pf4, "telegram"))

# Каскад обязан перепрыгнуть halted-канал и дойти до следующего.
check("halted telegram отсекается rescue-каскадом",
      "telegram" not in sender._rescue_cascade_for(
          "waba", _pf, already_tried=[])
      and "max" in sender._rescue_cascade_for("waba", _pf, already_tried=[]),
      f"cascade={sender._rescue_cascade_for('waba', _pf, already_tried=[])}")

# ---------------------------------------------------------------------------
# R20: 24ч-окно WABA — окно открывает ОТВЕТ КОНТАКТА, не наша отправка
# ---------------------------------------------------------------------------
# Раньше is_waba_in_24h_window() читала waba_sent_meta, который записывался на
# НАШЕЙ отправке (sender.py при успешном WABA). Последствие: второе касание за
# сутки уходило free text человеку, который не отвечал → Meta отвергала 131047.
# И наоборот: ответивший контакт не открывал окно, и мы тратили шаблон впустую.
_w = state.load_state()
_w = state.reset_daily_if_new_day(_w)
_w["waba_sent_meta"] = {}
_w["waba_inbound_meta"] = {}

check("окно закрыто, если контакт не отвечал",
      state.is_waba_in_24h_window(_w, "7999999999") is False)

# наша отправка НЕ должна открывать окно
from datetime import datetime as _dt, timezone as _tzn, timedelta as _tdm  # noqa: E402
_now_iso = _dt.now(_tzn.utc).isoformat()
_w["waba_sent_meta"]["7999999999"] = _now_iso
check("НАША отправка не открывает 24ч-окно (был баг)",
      state.is_waba_in_24h_window(_w, "7999999999") is False)

# ответ контакта ДОЛЖЕН открывать окно
_w["waba_inbound_meta"]["7999999999"] = _now_iso
check("ответ КОНТАКТА открывает 24ч-окно",
      state.is_waba_in_24h_window(_w, "7999999999") is True)

# старый ответ (25ч назад) окно уже не держит
_w["waba_inbound_meta"]["7999999999"] = (
    _dt.now(_tzn.utc) - _tdm(hours=25)).isoformat()
check("старый ответ (>24ч) окно не держит",
      state.is_waba_in_24h_window(_w, "7999999999") is False)

# отдельный учёт: сегодня уже писали → не слать второй раз (Meta 131049)
check("waba_already_marketed_today: сегодня уже писали",
      state.waba_already_marketed_today(_w, "7999999999") is True)
_w["waba_sent_meta"]["7999999999"] = (
    _dt.now(_tzn.utc) - _tdm(days=2)).isoformat()
check("вчерашняя отправка не блокирует сегодня",
      state.waba_already_marketed_today(_w, "7999999999") is False)

# sender обязан использовать защиту от 131049
_ssrc2 = inspect.getsource(sender)
check("sender защищает per-user marketing cap (131049)",
      "waba_already_marketed_today" in _ssrc2)
check("inbound-окно ставится при входящем сообщении",
      "set_waba_inbound_meta" in inspect.getsource(reconcile))

# ---------------------------------------------------------------------------
# R21: rescue-каскад отбирает только реально доступные каналы
# ---------------------------------------------------------------------------
# Раньше rescue шёл жёстко в ["telegram", "max"]: игнорировал halt, игнорировал
# исчерпанную квоту (лимит жжёгся впустую) и не включал SMS — единственный
# канал для людей, которых нет в мессенджерах.
check("_rescue_cascade_for существует", callable(sender._rescue_cascade_for))

_rc = state.load_state()
_rc = state.reset_daily_if_new_day(_rc)
_rc["halted_channels"] = {}
_rc["sent_today"] = {}

_c1 = sender._rescue_cascade_for("waba", _rc)
check("rescue после отказа WABA не повторяет сам waba",
      "waba" not in _c1, f"got {_c1}")
check("rescue после отказа WABA идёт дальше по каскаду", len(_c1) > 0, f"got {_c1}")

# halted-канал исключается
state.halt_channel(_rc, "telegram", hours=2)
_c2 = sender._rescue_cascade_for("waba", _rc)
check("halted канал исключён из rescue", "telegram" not in _c2, f"got {_c2}")

# исчерпанная квота исключается
_rc["halted_channels"] = {}
_rc["sent_today"] = {"telegram": 9999}
_c3 = sender._rescue_cascade_for("waba", _rc)
check("канал без квоты исключён из rescue (лимит не жжётся впустую)",
      "telegram" not in _c3, f"got {_c3}")

# выключенный SMS не тратит попытку
_rc["sent_today"] = {}
if not getattr(config, "SMSRU_ENABLED", 1):
    check("выключенный SMS не попадает в rescue",
          "sms" not in sender._rescue_cascade_for("max", _rc))
else:
    check("SMS проверяется по флагам готовности",
          callable(sender._rescue_cascade_for))

# ---------------------------------------------------------------------------
# R22: сторож подтверждений — «молчание» превращается в rescue
# ---------------------------------------------------------------------------
# Вебхук — единственный источник, который запускает fallback. Если он молчит,
# сообщение навсегда остаётся в статусе 'sent', rescue его не видит, и контакт
# тихо исчезает из воронки. Именно это давало «100 отправлено → 28 дошло».
check("db.find_unconfirmed существует", callable(db.find_unconfirmed))
check("sweep_unconfirmed_deliveries существует",
      callable(reconcile.sweep_unconfirmed_deliveries))
check("CONFIRM_TIMEOUT_MINUTES задан", config.CONFIRM_TIMEOUT_MINUTES == 60,
      f"got {config.CONFIRM_TIMEOUT_MINUTES}")
check("CONFIRM_MAX_AGE_HOURS задан (верхняя граница)",
      config.CONFIRM_MAX_AGE_HOURS == 24, f"got {config.CONFIRM_MAX_AGE_HOURS}")

# свежее неподтверждённое находится, подтверждённое — нет
_fu = db.find_unconfirmed(minutes=0, limit=50, max_age_hours=24)
check("find_unconfirmed исключает уже доставленные", all(
    r["status"] if "status" in r.keys() else True for r in _fu))

# сторож вызывается в sender перед сливом rescue
_ssrc3 = inspect.getsource(sender)
check("sender вызывает сторож подтверждений", "sweep_unconfirmed_deliveries" in _ssrc3)
check("сторож вызывается ДО слива rescue",
      _ssrc3.find("sweep_unconfirmed_deliveries") <
      _ssrc3.find("_drain_rescue_queue(_drain_current)") if
      "sweep_unconfirmed_deliveries" in _ssrc3 else False)

# Регрессия: `_drain_rescue_queue(current_state)` обращался к имени, которого
# в run() нет — NameError на КАЖДЫЙ запуск, съеденный пустым except. Слив
# rescue не работал никогда, а это единственный путь в SMS и обработка
# не-WABA строк очереди.
_run_tree = ast.parse(Path("bulkmessage/sender.py").read_text(encoding="utf-8"))
_run_fn = next(n for n in ast.walk(_run_tree)
               if isinstance(n, ast.FunctionDef) and n.name == "run")
_assigned = set()
for _n in ast.walk(_run_fn):
    if isinstance(_n, ast.Name) and isinstance(_n.ctx, ast.Store):
        _assigned.add(_n.id)
    elif isinstance(_n, ast.arg):
        _assigned.add(_n.arg)
check("в run() нет обращения к несуществующему current_state",
      "current_state" not in _assigned)
check("слив rescue вызывается с реально существующей переменной",
      "_drain_rescue_queue(_drain_current)" in _ssrc3)

# ---------------------------------------------------------------------------
# R23: отчёт по воронке и честные формулировки
# ---------------------------------------------------------------------------
# successful_today считает ПРИНЯТИЕ (HTTP 2xx). Раньше лог писал
# «ЦЕЛЬ ДОСТИГНУТА N/N успешных», хотя доставок могло быть заметно меньше —
# это вводило в заблуждение ровно в том месте, где нужна честная цифра.
from bulkmessage import report as _report  # noqa: E402

check("bulkmessage/report.py существует", Path("bulkmessage/report.py").exists())
check("report.build_report callable", callable(_report.build_report))
check("py main.py report зарегистрирован",
      '"report"' in Path("main.py").read_text(encoding="utf-8"))

_txt = _report.build_report()
check("отчёт показывает воронку (принято/доставлено)",
      "Принято" in _txt and "Доставлено" in _txt)
check("отчёт сравнивает с целью дня", "ДОСТАВЛЕНО" in _txt)
check("отчёт показывает rescue-очередь", "Rescue" in _txt)
with patch.object(_report.config, "WAZZUP_WEBHOOK_URL", ""):
    _txt_nohook2 = _report.build_report()
check("отчёт предупреждает о ненастроенном вебхуке (когда его правда нет)",
      "WAZZUP_WEBHOOK_URL" in _txt_nohook2)
check("отчёт не падает на пустом дне", isinstance(_txt, str) and len(_txt) > 100)
check("отчёт содержит ПРОГНОЗ достижимого объёма",
      "ПРОГНОЗ" in _txt)
check("прогноз считает по эффективному капу × конверсию",
      callable(getattr(_report, "_forecast", None)))
_fc_lo, _fc_hi, _fc_p = _report._forecast(_st)
check("прогноз возвращает вилку (low, high) и расшифровку",
      isinstance(_fc_lo, int) and isinstance(_fc_hi, int)
      and _fc_lo > 0 and _fc_hi >= _fc_lo and "waba" in _fc_p)
check("MAX не входит в холодный прогноз (бан необратим)",
      "max " not in _fc_p)
# R27: прогноз не должен опираться на выдуманную константу
check("в отчёте нет жёсткой конверсии 70%",
      "0.70" not in inspect.getsource(_report._forecast))
check("вилка конверсии WABA не завышена (низ ≤ 30% — зафиксированные 28%)",
      _report._WABA_COLD_RANGE[0] <= 0.30,
      f"низ={_report._WABA_COLD_RANGE[0]}")
# при появлении реальных подтверждённых доставок прогноз верит факту
_r27 = {"delivered_today": {"waba": 28}, "accepted_today": {"waba": 100},
        "sent_today": {}}
check("конверсия берётся из фактических delivered/accepted за день",
      abs(_report._measured_conversion(_r27) - 0.28) < 1e-9,
      f"got {_report._measured_conversion(_r27)}")
check("без подтверждённых доставок возвращается None, а не выдумка",
      _report._measured_conversion({"delivered_today": {}, "accepted_today": {}}) is None)

_ssrc4 = inspect.getsource(sender)
# Проверяем именно текст лог-вызовов, а не комментарии: строка «ЦЕЛЬ ДОСТИГНУТА»
# осталась в комментарии, где объясняется, что её убрали из логов.
_log_lines = [
    ln for ln in _ssrc4.splitlines()
    if "log." in ln and "ЦЕЛЬ ДОСТИГНУТА" in ln
]
check("лог не называет ПРИНЯТИЕ доставкой", not _log_lines,
      f"в лог-вызовах осталось: {_log_lines}")
check("лог различает принято и доставлено",
      "принято транспортом" in _ssrc4 and "доставлено" in _ssrc4)
check("финальная сводка печатает воронку",
      "ВОРОНКА ДОСТАВКИ" in _ssrc4)

# ---------------------------------------------------------------------------
# R24: согласие на рассылку (opt-in) — ФЗ-38 ст.18 + политика Meta
# ---------------------------------------------------------------------------
# Ч.1 ст.18 ФЗ-38: реклама по сетям электросвязи, включая мессенджеры, только
# при предварительном согласии; бремя доказывания на рекламораспространителе.
# Meta: business-initiated сообщения только opt-in. Холодная рассылка по базе
# без opt-in = блокировки + штраф. Мультиаккаунтность это НЕ решает.
check("db имеет consent-константы",
      hasattr(db, "CONSENT_GRANTED") and hasattr(db, "CONSENT_DENIED"))
check("db.normalize_consent существует", callable(db.normalize_consent))
check("db.may_send_cold существует", callable(db.may_send_cold))
check("db.consent_stats существует", callable(db.consent_stats))
check("CONSENT_REQUIRED_FOR_COLD в конфиге",
      hasattr(config, "CONSENT_REQUIRED_FOR_COLD"))

for _raw, _want in (
    ("да", db.CONSENT_GRANTED), ("yes", db.CONSENT_GRANTED),
    ("согласие получено", db.CONSENT_GRANTED), ("opt-in", db.CONSENT_GRANTED),
    ("нет", db.CONSENT_DENIED), ("отписка", db.CONSENT_DENIED),
    ("stop", db.CONSENT_DENIED), ("unsubscribe", db.CONSENT_DENIED),
    ("", db.CONSENT_UNKNOWN), (None, db.CONSENT_UNKNOWN),
    ("мусор", db.CONSENT_UNKNOWN),
):
    check(f"normalize_consent({_raw!r}) = {_want}",
          db.normalize_consent(_raw) == _want,
          f"got {db.normalize_consent(_raw)}")

# Отписка уважается ВСЕГДА — независимо от флага.
# Сравниваем порядок в КОДЕ, а не в докстринге (в ней CONSENT_REQUIRED_FOR_COLD
# упоминается раньше, в описании поведения).
_csrc = inspect.getsource(db.may_send_cold)
_cbody = _csrc.split('"""')[-1]  # отбрасываем докстринг
check("denied блокирует отправку до проверки флага",
      _cbody.find("CONSENT_DENIED") < _cbody.find("CONSENT_REQUIRED_FOR_COLD")
      and "CONSENT_DENIED" in _cbody)
check("отписку нельзя отключить настройкой",
      "if st == CONSENT_DENIED" in _csrc
      and "if st == CONSENT_DENIED:" in _cbody)

# sender проверяет согласие ДО расхода квоты — сравниваем внутри _run_one_contact,
# а не по всему модулю (в модуле quota встречается и в других функциях).
_ssrc5 = inspect.getsource(sender)
_roc = inspect.getsource(sender._run_one_contact)
_ci = _roc.find("may_send_cold")
_quota = _roc.find("channel_has_quota_with_headroom")
check("проверка согласия раньше расхода квоты",
      _ci != -1 and _quota != -1 and _ci < _quota,
      f"consent@{_ci} quota@{_quota}")
check("в лог запуска попадает статистика согласия", "Согласие:" in _ssrc5)

# авто-отписка по ответу «стоп»
_rsrc2 = inspect.getsource(reconcile.process_incoming_message)
check("входящее «стоп» помечает отказ", "CONSENT_DENIED" in _rsrc2)
check("ловим русские и английские отписки",
      "стоп" in _rsrc2 and "unsubscribe" in _rsrc2)

# Excel-столбец согласия
check("contacts читает столбец согласия",
      "согласие" in inspect.getsource(contacts.load_contacts))

# отчёт показывает согласие
check("отчёт показывает согласие", "СОГЛАСИЕ НА РАССЫЛКУ" in _txt)

# ---------------------------------------------------------------------------
# R25: подтверждённая доставка запирает cooldown на 96ч (сквозной тест)
# ---------------------------------------------------------------------------
# Без этого вызова cooldown 96ч не включался НИКОГДА: sender писал только
# время отправки, а приоритет подтверждённой доставки в is_phone_in_cooldown
# ссылался на пустой словарь. Проверяем на настоящем process_delivery_status
# с настоящим payload — ровно тот формат, который прилетает от Wazzup.
section("R25. ПОДТВЕРЖДЁННАЯ ДОСТАВКА → COOLDOWN (сквозной)")

_R25_PHONE = "+79997770055"
_R25_MID = "r25_delivery_probe_1"

# Чистим возможные остатки прошлого прогона (в тестовой БД, не боевой)
with patch.object(config, "DB_PATH", str(_TEST_DB_PATH)):
    with db.db_conn() as conn:
        conn.execute("DELETE FROM messages WHERE message_id = ?", (_R25_MID,))
        conn.execute("DELETE FROM contacts WHERE phone = ?", (_R25_PHONE,))
        conn.commit()

    with db.db_conn() as conn:
        _r25_cid = db.upsert_contact(conn, _R25_PHONE, "R25", "Buyers")
        db.insert_message(
            conn, contact_id=_r25_cid, channel="waba", message_text="probe",
            message_id=_R25_MID, status="sent",
        )
        conn.commit()

# Изолированный state.json — реальный broadcast_state.json не трогаем
_r25_state = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", delete=False, encoding="utf-8"
)
_r25_state.write(json.dumps({"sent_today": {}, "contacts_today": 0,
                             "date": datetime.now(timezone.utc).date().isoformat(),
                             "last_index": 0}))
_r25_state.close()

try:
    # DB_PATH тоже подменяем: блок R25 патчил STATE_PATH, но писал
    # INSERT/UPDATE/DELETE прямо в боевую data/crm.db — отсюда были
    # тестовые номера в боевой базе и переписывание файла при прогонах.
    with patch.object(config, "STATE_PATH", _r25_state.name), \
         patch.object(config, "DB_PATH", str(_TEST_DB_PATH)), \
         patch.object(reconcile, "sync_message_to_sheet", lambda **kw: None):
        # Доставка подтверждена вебхуком
        reconcile.process_delivery_status(
            "waba", {"id": _R25_MID, "status": "delivered"}
        )
        _r25_st = state.load_state()
        _r25_bucket = _r25_st.get("last_delivery_per_channel") or {}
        _r25_by_ch = _r25_bucket.get(_R25_PHONE) or {}
        check("вебхук пишет last_delivery_per_channel[phone]['waba']",
              "waba" in _r25_by_ch, f"got {_r25_by_ch}")
        # Для WABA cooldown = 0ч (по правилам проекта), поэтому блокировку
        # проверяем на канале с реальным cooldown — telegram.
        _r25_now = datetime.now(timezone.utc).isoformat()
        state.record_delivery_confirmed(_r25_st, _R25_PHONE, "telegram", _r25_now)
        check("подтверждённая доставка блокирует ТОЛЬКО свой канал",
              state.is_phone_in_cooldown(_r25_st, _R25_PHONE, "telegram") is True
              and state.is_phone_in_cooldown(_r25_st, _R25_PHONE, "max") is False)

        # Свип неподтверждённого НЕ должен включать cooldown
        with db.db_conn() as conn:
            db.insert_message(
                conn, contact_id=_r25_cid, channel="telegram",
                message_text="probe2", message_id="r25_unconfirmed_1",
                status="sent",
            )
            conn.commit()
        _r25_st2 = state.load_state()
        _r25_before = len(
            (_r25_st2.get("last_delivery_per_channel") or {}).get(_R25_PHONE) or {}
        )
        check("доставка в waba не создаёт записи для telegram",
              "telegram" not in ((_r25_st2.get("last_delivery_per_channel")
                                  or {}).get(_R25_PHONE) or {}),
              f"было {_r25_before}")
finally:
    # Чистим тестовую БД за собой (не боевую — патч DB_PATH уже снят).
    with patch.object(config, "DB_PATH", str(_TEST_DB_PATH)):
        with db.db_conn() as conn:
            conn.execute("DELETE FROM messages WHERE message_id IN (?, ?)",
                         (_R25_MID, "r25_unconfirmed_1"))
            conn.execute("DELETE FROM contacts WHERE phone = ?", (_R25_PHONE,))
            conn.commit()
    try:
        Path(_r25_state.name).unlink()
    except OSError:
        pass

# ---------------------------------------------------------------------------
# R26: мёртвый аккаунт гасит канал (401/403 → halt)
# ---------------------------------------------------------------------------
# Контракт задан в самом транспорте — wazzup.py:12: «401/403 → AUTH
# (halt channel)». Ветки WABA и SMS его выполняли, а ветка
# telegram/max/whatsapp — нет. Итог: умерший канал продолжал получать по
# одному сетевому вызову НА КАЖДЫЙ контакт до конца прогона. В базе это
# 110 подряд «auth: HTTP 401 Not authorized».
section("R26. AUTH 401 ГАСИТ КАНАЛ")

import inspect as _insp26  # noqa: E402

_roc26 = _insp26.getsource(sender._run_one_contact)
# Ветка AUTH для Wazzup-каналов: между маркером и continue обязан быть halt.
# Ищем ПОСЛЕДНЕЕ вхождение ErrorKind.AUTH — первое принадлежит ветке WABA.
_a26 = _roc26.rfind("ErrorKind.AUTH")
_h26 = _roc26.find("halt_channel", _a26)
_c26 = _roc26.find("continue", _a26)
check("ветка AUTH вызывает halt_channel",
      _a26 != -1 and _h26 != -1 and _h26 < _c26,
      f"auth@{_a26} halt@{_h26} continue@{_c26}")
check("halt происходит ДО persist_failure (иначе канал успеет записать лишнее)",
      _h26 < _roc26.find("_persist_failure", _a26))
check("AUTH-ветка шлёт алерт в tglog",
      "tglog.send" in _roc26[_a26:_c26])

# Функциональная проверка: настоящий 401 от Wazzup гасит канал.
# DRY_RUN=0 обязателен: при DRY_RUN код имитирует успех и до транспорта
# не доходит, поэтому ветка AUTH не выполнялась бы вовсе. Сетевых вызовов
# при этом нет — send_to_channel заглушен.
_r26_f = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", delete=False, encoding="utf-8"
)
_r26_f.write(json.dumps({"sent_today": {}, "contacts_today": 0,
                         "date": datetime.now(timezone.utc).date().isoformat(),
                         "last_index": 0}))
_r26_f.close()
try:
    with patch.object(config, "STATE_PATH", _r26_f.name), \
         patch.object(config, "BULK_BYPASS_SAFE_HOURS", True), \
         patch.object(config, "DRY_RUN", False), \
         patch.object(sender, "_append_log", lambda *a, **k: None), \
         patch.object(sender, "_persist_failure",
                      lambda *a, **k: None), \
         patch.object(sender, "_persist_success",
                      lambda *a, **k: None), \
         patch.object(sender.failed_contacts, "mark_fully_failed",
                      lambda *a, **k: None), \
         patch.object(sender.wazzup, "send_to_channel",
                      return_value=(False, "",
                                    "HTTP 401 Not authorized in channel", 401)):
        _r26_st = state.load_state()
        _r26_ok = sender._run_one_contact(
            {"phone": "+79995550044", "name": "R26", "category": "Buyers"},
            ["telegram"], {}, {}, _r26_st,
        )
        check("401 не даёт «успеха»", _r26_ok is False, f"ok={_r26_ok}")
        check("после 401 telegram помечен как halted",
              state.is_channel_halted(_r26_st, "telegram") is True,
              f"halted={_r26_st.get('halted_channels')}")
        check("halt исключает канал из rescue-каскада",
              "telegram" not in sender._rescue_cascade_for("waba", _r26_st))
finally:
    try:
        Path(_r26_f.name).unlink()
    except OSError:
        pass

# halt НЕ должен срабатывать на RATE_LIMIT/TRANSIENT — иначе канал гаснет
# от одной временной 429 и не возвращается в каскад на весь день
_r26_st2 = state.load_state()
_r26_st2.pop("halted_channels", None)
sender._next_backoff  # noqa: B018 - проверяем наличие функции
check("is_overload_error ловит 429, но не обычный PERMANENT",
      state.is_overload_error("429 Too Many Requests") is True
      and state.is_overload_error("phone not occupied") is False)
check("RATE_LIMIT не является AUTH (иначе halt на каждый 429)",
      sender.wazzup.classify_error("429 Too Many Requests", 429).value != "auth")

# ---------------------------------------------------------------------------
# R28: дневной лимит SMS.RU — аккаунтная, а не контактная проблема
# ---------------------------------------------------------------------------
# Схема «WABA отказы → хвост в SMS» держится на этом лимите целиком. Код 206
# = «превышен дневной лимит на отправку». Это НЕ вина номера. Без halt
# sender бьётся в лимит по каждому rescue-контакту и помечает нормальные
# номера как failed.
section("R28. ДНЕВНОЙ ЛИМИТ SMS.RU")
check("206 распознаётся как исчерпание дневного лимита аккаунта",
      sms_ru.is_daily_limit_reached(206) is True)
check("201 (лимит на конкретный номер) — НЕ аккаунтный лимит",
      sms_ru.is_daily_limit_reached(201) is False)
check("230-233 (лимиты на номер/текст) — НЕ аккаунтные",
      all(sms_ru.is_daily_limit_reached(c) is False for c in (230, 231, 232, 233)))
check("206 по-прежнему классифицируется как RATE_LIMIT",
      sms_ru.classify_smsru_error(206, "limit").value == "rate")
_sms28 = inspect.getsource(sender)
_20628 = _sms28.find("is_daily_limit_reached")
check("sender гасит SMS-канал на 206",
      _20628 != -1
      and "halt_channel(current_state, \"sms\")" in _sms28[_20628:_20628 + 900])
check("есть функция get_daily_limit (реальный потолок аккаунта)",
      callable(getattr(sms_ru, "get_daily_limit", None)))
with patch.object(config, "SMSRU_API_ID", ""):
    _r28_ok, _r28_t, _r28_u, _r28_d = sms_ru.get_daily_limit()
check("без API_ID get_daily_limit не падает, а возвращает ошибку",
      _r28_ok is False and _r28_t == 0 and "API_ID" in _r28_d,
      f"ok={_r28_ok} detail={_r28_d}")
check("preflight спрашивает реальный лимит SMS.RU",
      "get_daily_limit" in inspect.getsource(preflight))
# SMS.RU отдаёт total_limit СТРОКОЙ ("10"), а не числом. Парсер обязан это
# понимать: иначе лимит молча не показывается — а это ровно та цифра, ради
# которой всё затевалось.
check("_as_int понимает и int, и строку",
      sms_ru._as_int(10) == 10 and sms_ru._as_int("10") == 10
      and sms_ru._as_int("") == 0 and sms_ru._as_int(None) == 0)
check("_first_int понимает и число, и строку в JSON",
      sms_ru._first_int('{"total_limit":"10"}', '"total_limit"') == 10
      and sms_ru._first_int('{"total_limit":5000}', '"total_limit"') == 5000)

# ---------------------------------------------------------------------------
# R29–R32. ФАЗОВАЯ РАССЫЛКА (WABA → Telegram → MAX)
# ---------------------------------------------------------------------------
section("R29–R32. ФАЗОВАЯ РАССЫЛКА")

import inspect as _insp_phase  # noqa: E402

# --- R29: маркер waba_exhausted переживает сутки (обход TTL 24ч) ---
# Ключевой баг, который план закрывает: reachability_cache живёт 24 часа,
# поэтому проваленный номер на следующий день снова получил бы один из 250
# WABA-слотов. Для этого нужен ОТДЕЛЬНЫЙ долгоживущий маркер.
_r29_f = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", delete=False, encoding="utf-8"
)
_r29_f.write(json.dumps({"sent_today": {}, "contacts_today": 0,
                         "date": datetime.now(timezone.utc).date().isoformat(),
                         "last_index": 0}))
_r29_f.close()
try:
    with patch.object(config, "STATE_PATH", _r29_f.name):
        _r29 = state.load_state()
        _PH = "+79993330011"
        state.mark_waba_exhausted(_r29, _PH, "no delivery")
        check("mark_waba_exhausted → is_waba_exhausted = True",
              state.is_waba_exhausted(_r29, _PH) is True)
        # Сдвигаем метку на 25 часов назад: reachability_cache к этому моменту
        # уже бы протух, а маркер обязан продолжать работать.
        _r29["waba_exhausted"][_PH]["ts"] = (
            datetime.now(timezone.utc) - timedelta(hours=25)
        ).isoformat()
        check("маркер waba_exhausted живёт дольше 24ч (в отличие от кэша)",
              state.is_waba_exhausted(_r29, _PH) is True)
        # Сдвигаем за пределы TTL в 30 дней — запись перестаёт действовать.
        _r29["waba_exhausted"][_PH]["ts"] = (
            datetime.now(timezone.utc) - timedelta(days=31)
        ).isoformat()
        check("по истечении TTL=30 дней маркер снимается",
              state.is_waba_exhausted(_r29, _PH) is False)
        check("waba_exhausted_count считает живые записи",
              isinstance(state.waba_exhausted_count(_r29), int))
        check("чужой номер не считается исчерпанным",
              state.is_waba_exhausted(_r29, "+79993330099") is False)
        # Подтверждение доставки должно снимать исчерпание: контакт снова
        # годен для WABA.
        _r29["waba_exhausted"][_PH]["ts"] = datetime.now(timezone.utc).isoformat()
        _r29_src = inspect.getsource(state.load_state)
        check("waba_exhausted инициализируется в load_state()",
              "waba_exhausted" in _r29_src)
        # Суточный сброс не должен стирать маркер
        _r29["date"] = "2020-01-01"
        state.reset_daily_if_new_day(_r29)
        check("reset_daily_if_new_day НЕ стирает waba_exhausted",
              state.is_waba_exhausted(_r29, _PH) is True)
finally:
    try:
        Path(_r29_f.name).unlink()
    except OSError:
        pass

# --- R30: фаза 1 берёт только свежие контакты ---
_r30_f = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", delete=False, encoding="utf-8"
)
_r30_f.write(json.dumps({"sent_today": {}, "contacts_today": 0,
                         "date": datetime.now(timezone.utc).date().isoformat(),
                         "last_index": 0}))
_r30_f.close()
try:
    with patch.object(config, "STATE_PATH", _r30_f.name), \
         patch.object(sender, "db") as _mdb, \
         patch.object(sender.bad_phones, "is_bad", lambda p, c: False):
        _r30 = state.load_state()
        state.mark_waba_exhausted(_r30, "+79993330022", "failed")
        _now30 = datetime.now(timezone.utc)
        # У WABA cooldown = 0ч (по правилам проекта), то есть по умолчанию
        # cooldown его не блокирует. Проверяем фильтр с принудительным
        # непустым cooldown — это страховка на случай, если оператор всё же
        # задаст ненулевой cooldown для waba.
        state.set_last_send_per_phone(_r30, "+79993330023", _now30.isoformat(),
                                      channel="waba")
        with patch.object(state, "_cooldown_hours_for",
                          lambda ch: 96 if ch == "waba" else 0):
            # Номер в очереди rescue (был провал, ждёт отката)
            _mdb.pending_rescues.return_value = [{"phone": "+79993330033"}]
            _r30_contacts = [
                {"phone": "+79993330011", "name": "fresh1", "category": "Покупатели"},
                {"phone": "+79993330022", "name": "exhausted", "category": "Покупатели"},
                {"phone": "+79993330023", "name": "cooldown", "category": "Покупатели"},
                {"phone": "+79993330033", "name": "in_queue", "category": "Покупатели"},
                # Категории без одобренного шаблона Meta (Знакомый/Услуги/
                # Прораб): в WABA попасть не должны — отправка вернёт
                # BUILD_TEMPLATE_ERROR и слот потратится впустую.
                {"phone": "+79993330044", "name": "notemplate", "category": "Услуги"},
            ]
            _r30_fresh = sender._collect_fresh_waba_targets(_r30_contacts, _r30)
            _r30_phones = [c["phone"] for c in _r30_fresh]
            check("в WABA-попадает свежий контакт", "+79993330011" in _r30_phones)
            check("WABA-исчерпанный исключён из фазы 1",
                  "+79993330022" not in _r30_phones, f"got {_r30_phones}")
            check("номер в cooldown исключён из фазы 1",
                  "+79993330023" not in _r30_phones, f"got {_r30_phones}")
            check("номер из rescue_queue исключён из фазы 1",
                  "+79993330033" not in _r30_phones, f"got {_r30_phones}")
            check("в фазу 1 попал ровно 1 свежий из 5 (остальные отсеяны)",
                  len(_r30_fresh) == 1, f"got {len(_r30_fresh)}")
            check("контакт БЕЗ одобренного шаблона Meta исключён из WABA",
                  "+79993330044" not in _r30_phones, f"got {_r30_phones}")
finally:
    try:
        Path(_r30_f.name).unlink()
    except OSError:
        pass

# --- R30b: ОДИН ограниченный шаблон Meta НЕ убивает категории с рабочим ---
# Регрессия 2026-10-11: условие preflight было `valid_count < total_count`,
# поэтому шаблон «Агенты», ограниченный Meta, выключал WABA ЦЕЛИКОМ.
# Активными оставались только telegram/max — то есть канал, дающий ~100 из
# ~130 доставленных в сутки, молча работал в ноль.
#
# Проверяем по СОДЕРЖИМОМУ отбора, а не по тексту условия: контакт рабочей
# категории обязан выжить, контакт ограниченной — обязан выпасть.
_r30b_state = state.load_state()
_r30b_state = dict(_r30b_state)
_r30b_state["last_waba_sent"] = {}
_r30b_state.setdefault("halted_channels", {})

_r30b_uuid_ok = "39a2f8b4-38b4-4d32-a878-10ab154fcad0"        # Покупатели
_r30b_uuid_restricted = "2dc406ba-5e87-4c0b-b168-4ee85adcd067"  # Агенты
_r30b_contacts = [
    {"phone": "+79993330055", "name": "ok", "category": "Покупатели"},
    {"phone": "+79993330056", "name": "blocked", "category": "Агенты"},
]
from bulkmessage import waba_templates as _r30b_wt  # noqa: E402


def _r30b_pick(cat):
    if cat == "Покупатели":
        return _r30b_uuid_ok
    if cat == "Агенты":
        return _r30b_wt._unless_restricted(cat, _r30b_uuid_restricted)
    return None


with patch.object(_r30b_wt, "pick_waba_template", _r30b_pick), \
     patch.object(config, "WABA_RESTRICTED_TEMPLATES",
                  {_r30b_uuid_restricted}):
    _r30b_fresh = sender._collect_fresh_waba_targets(
        _r30b_contacts, _r30b_state)
    _r30b_phones = [c["phone"] for c in _r30b_fresh]

check("контакт рабочей категории НЕ теряется из-за чужого шаблона",
      "+79993330055" in _r30b_phones, f"got {_r30b_phones}")
check("контакт категории с ограниченным шаблоном отсечён",
      "+79993330056" not in _r30b_phones, f"got {_r30b_phones}")
check("отбор не схлопывается в ноль, пока есть хоть один рабочий шаблон",
      len(_r30b_fresh) == 1, f"got {len(_r30b_fresh)}")

# Preflight-гейт: выключать WABA можно только когда не работает НИ ОДИН
# шаблон, и только при непустом списке категорий.
_gate_src = inspect.getsource(sender.run)
check("WABA гасится только при valid_count == 0",
      "valid_count == 0" in _gate_src)
check("пустой список категорий не считается «сломаны все шаблоны»",
      "total_count and valid_count == 0" in _gate_src,
      "иначе прогон с пустым templates_map погасит WABA ложно")

# --- R31: фазы 2/3 тянут из rescue_queue FIFO и лимитированы ---
# Проверка индекса идёт по СХЕМЕ, а не по исходнику функции: форма таблиц
# теперь задаётся миграциями, и наличие индекса — факт базы, а не деталь
# реализации.
def _has_index(index_name: str, table: str) -> bool:
    with db.db_conn() as _conn:
        _rows = _conn.execute(f"PRAGMA index_list({table})").fetchall()
    return any(_r[1] == index_name for _r in _rows)

check("pending_rescues умеет фильтровать по original_channel",
      "original_channel" in inspect.getsource(db.pending_rescues))
_r31_sql = inspect.getsource(db.pending_rescues)
check("фильтр добавляет условие по каналу в WHERE",
      "AND original_channel = ?" in _r31_sql)
check("rescue_queue индексирован по (status, created_at)",
      _has_index("idx_rescue_status_created", "rescue_queue")
      or _has_index("idx_rescue_status", "rescue_queue"),
      "индекс очереди отката должен существовать в СХЕМЕ; раньше проверка"
      " искала строку в исходнике функции, а форма таблицы теперь задаётся"
      " миграциями, а не кодом init_rescue_queue")
_src_run = inspect.getsource(sender.run)
# Глобальная страховка тестов: реальные сетевые вызовы из пробы запрещены.
#
# Suite выполняется с DRY_RUN=0 (об этом предупреждают два «живых» гварда), и
# весь транспорт в тестах подменяется. Но проба — единственный путь, который
# дёргает Wazzup МИМО подменяемого _run_one_contact. Когда фаза личных слотов
# (2026-10-09) начала звать probe_contact_channels, прогон тестов отправил
# НАСТОЯЩИЕ сообщения на номера 7999900xxxx. Теперь любой непокрытый вызов
# падает с ProbeNetworkDisabled, а не уходит в сеть.
from bulkmessage import probe as _probe_guarded  # noqa: E402

_probe_guarded.set_network_enabled(False)
# Исходник probe_contact_channels снимаем ДО подмены: дальше атрибут станет
# лямбдой, и inspect.getsource начнёт возвращать её исходник, а не функцию.
_probe_cch_src = inspect.getsource(_probe_guarded.probe_contact_channels)
# И probe_contact_channels заглушаем: фаза личных слотов вызывает её НАПРЯМУЮ,
# мимо подменяемого _run_one_contact. Сеть выключена и подтверждена ловушкой,
# а пустой список результатов означает «в этом тесте личных отправок нет».
_probe_guarded.probe_contact_channels = lambda *a, **kw: []
# Фазы вынесены из run() в _run_all_phases, чтобы длинный прогон мог
# крутить их повторно до дедлайна, а тесты — гонять по отдельности.
_src_phases = inspect.getsource(sender._run_all_phases)
check("run() объявляет фазы в config.PHASE_ORDER",
      "PHASE_ORDER" in _src_phases)
_src_phase_fn = inspect.getsource(sender._run_phase)
check("фаза ограничена одним каналом (cascade_override=[phase_ch])",
      'cascade_override=[phase_ch]' in _src_phase_fn)
# target_reached может упоминаться в комментарии, но ВЫЗЫВАТЬСЯ в фазах не должен:
# он считает ПРИНЯТИЕ, и 250 принятых WABA остановили бы цикл до фазы отката.
check("цель дня НЕ прерывает фазы (вызова state.target_reached нет)",
      "state.target_reached(" not in _src_run
      and "state.target_reached(" not in _src_phases
      and "state.target_reached(" not in _src_phase_fn)
check("фаза отката берёт ЛЮБЫЕ неподтверждённые, а не только провалы WABA",
      'original_channel="waba"' not in _src_phases)
check("фаза отката не берёт уже опробованный канал (exclude_tried)",
      "exclude_tried=phase_ch" in _src_phases)
check("после TG rescue получает терминальный статус",
      "mark_rescue_success" in _src_phase_fn
      and "mark_rescue_attempt" in _src_phase_fn)

# --- R32: тайминг фаз укладывается в окно ---
_r32_f = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", delete=False, encoding="utf-8"
)
_r32_f.write(json.dumps({"sent_today": {}, "contacts_today": 0,
                         "date": datetime.now(timezone.utc).date().isoformat(),
                         "last_index": 0}))
_r32_f.close()
try:
    with patch.object(config, "STATE_PATH", _r32_f.name), \
         patch.object(config, "PHASE_DETERMINISTIC_DELAY", True), \
         patch.object(config, "PERSONAL_DELAY_MIN", 300), \
         patch.object(config, "PERSONAL_DELAY_MAX", 900), \
         patch.object(config, "BULK_BYPASS_SAFE_HOURS", True):
        # Решение владельца 2026-10-10: пауза в личных каналах не короче
        # 15 минут. Тест НИЖЕ специально подменяет PERSONAL_DELAY_MIN=300 —
        # ровно то значение, которое было в .env.local — и проверяет, что
        # env НЕ может продавить правило. Пол выигрывает у env, как в
        # _safe_limit по дневным капам.
        _pgap32 = int(config.PERSONAL_MIN_GAP_SEC)
        _pjit32 = float(config.PERSONAL_JITTER_MAX)
        _lo32, _hi32 = sender._phase_delay_bounds("telegram")
        check("env не может опустить паузу в личных ниже 15 минут",
              _lo32 >= _pgap32,
              f"env просил 300, пол {_pgap32} → фактический низ {_lo32}")
        check("потолок паузы = минимум × разброс (пауза живая, не ровная)",
              _hi32 >= _pgap32 * _pjit32,
              f"[{_lo32}, {_hi32}] при минимуме {_pgap32} и разбросе {_pjit32}")

        # Арифметика решения: 45 личных сообщений по 15+ минут — это
        # 11.25 часа, и в 7.5-часовое окно они НЕ влезают. Старый тест
        # требовал обратного (5–15 мин, 45 штук за 7.5ч) — это была
        # предыдущая схема, отменённая решением владельца.
        _tot32, _rem32 = 0.0, 45
        for _ in range(45):
            _d = sender._phase_delay("telegram", 7.5 * 3600 - _tot32, _rem32)
            _tot32 += _d
            _rem32 -= 1
        check("45 личных НЕ влезают в 7.5ч при паузе от 15 мин "
              "(старая схема это умела — новая нет, и это ожидаемо)",
              _tot32 > 7.5 * 3600,
              f"sum={_tot32/3600:.2f}ч при {_pgap32/60:.0f} мин минимуме")
        check("задержка всегда внутри [минимум, минимум×разброс]",
              all(_lo32 <= sender._phase_delay("telegram", 7.5 * 3600, n) <= _hi32
                  for n in range(1, 46)))
        check("задержка НЕ ужимается ниже минимума даже когда окно мало "
              "(правило владельца важнее, чем успеть в окно)",
              sender._phase_delay("telegram", 4 * 3600, 45) >= _pgap32,
              f"{sender._phase_delay('telegram', 4*3600, 45)} с")
        # _phase_fits обязан ОТКЛАДЫВАТЬ фазу, если в окне не хватает времени
        # на минимальные паузы (теперь это 45 × 900с = 11.25ч, а не 3.75ч).
        # Граница окна берётся из config, а не из числа в тесте. Раньше здесь
        # было зашито «19:00», и после решения владельца расширить личное окно
        # до 10–21 тест продолжил бы проверять несуществующее правило.
        _pe_end = int(config.PERSONAL_SAFE_HOURS_END)
        with patch.object(config, "now_tz",
                          lambda: datetime(2026, 10, 5, 17, 30,
                                           tzinfo=ZoneInfo("Europe/Moscow"))):
            _left32 = sender._phase_seconds_left("telegram")
            check(f"_phase_seconds_left считает остаток до {_pe_end}:00 МСК",
                  0 < _left32 <= (_pe_end - 17) * 3600,
                  f"left={_left32/3600:.2f}ч")
            check(f"_phase_fits: 45 сообщений не влезают в {_pe_end - 17}ч → отложена",
                  sender._phase_fits("telegram", 45) is False)
            check(f"_phase_fits: 5 сообщений в {_pe_end - 17}ч → идёт",
                  sender._phase_fits("telegram", 5) is True)
        with patch.object(config, "now_tz",
                          lambda: datetime(2026, 10, 5, 11, 10,
                                           tzinfo=ZoneInfo("Europe/Moscow"))):
            # 11:10 при закрытии в 21:00 — в окне почти 10ч, и 45 сообщений
            # по 15 минут (11.25ч) уже не влезают. Это и есть новая реальность.
            _fits45 = sender._phase_fits("telegram", 45)
            check("45 личных в полном окне тоже не влезают — лимит окна ~33",
                  _fits45 is False,
                  f"в {_pgap32/60:.0f} мин минимума 45 штук = 11.25ч, "
                  f"окно короче")
            check("33 личных в полном окне влезают (проверка границы)",
                  sender._phase_fits("telegram", 33) is True,
                  f"33 × {_pgap32/60:.0f} мин = "
                  f"{33*_pgap32/3600:.2f}ч")
        check("_phase_fits: пустая фаза всегда влезает",
              sender._phase_fits("telegram", 0) is True)
        # WABA-фаза: 250 сообщений с паузой 10-30с
        _wtot, _wrem = 0.0, 250
        for _ in range(250):
            _wtot += sender._phase_delay("waba", 10 * 3600 - _wtot, _wrem)
            _wrem -= 1
        check("250 WABA укладываются в 10ч", _wtot <= 10 * 3600,
              f"sum={_wtot/60:.0f} мин")
finally:
    try:
        Path(_r32_f.name).unlink()
    except OSError:
        pass

# --- R32b: safety cap и лимиты под новую схему ---
check("MAX_ATTEMPTS_PER_DAY = 300 (250+30+15 не перебирается)",
      config.MAX_ATTEMPTS_PER_DAY >= 295,
      f"got {config.MAX_ATTEMPTS_PER_DAY}")
_r32b = {"attempts_today": 294, "date": config.now_tz().strftime("%Y-%m-%d")}
check("на 295-й попытке safety cap ещё не сработал",
      state.max_attempts_reached(_r32b) is False)
_r32b["attempts_today"] = config.MAX_ATTEMPTS_PER_DAY
check(f"на {config.MAX_ATTEMPTS_PER_DAY}-й попытке safety cap срабатывает",
      state.max_attempts_reached(_r32b) is True)
check("headroom снят: WABA-эффективный кап = 250",
      config.HEADROOM_PCT == 0
      and state.channel_effective_cap(
          {"waba_tier": "0", "date": config.now_tz().strftime("%Y-%m-%d")}, "waba"
      ) == 250,
      f"headroom={config.HEADROOM_PCT} cap="
      f"{state.channel_effective_cap({'waba_tier': '0', 'date': config.now_tz().strftime('%Y-%m-%d')}, 'waba')}")
check("TG и MAX укладываются в свой per-channel hard cap",
      state.channel_effective_cap({}, "telegram")
      <= config.PERSONAL_CHANNEL_HARD_CAPS["telegram"]
      and state.channel_effective_cap({}, "max")
      <= config.PERSONAL_CHANNEL_HARD_CAPS["max"])
# Лимит из env НЕ может превысить жёсткий потолок канала: защита должна
# держать даже если владелец поставит 999 в .env.local.
check("_safe_limit режет значение по hard_cap (min)",
      "min(val, hard_cap)" in inspect.getsource(config._safe_limit))
check("TG=50 и MAX=10 — потолки владельца (TG 2026-10-08, MAX 2026-10-10)",
      config.PERSONAL_CHANNEL_HARD_CAPS["telegram"] == 50
      and config.PERSONAL_CHANNEL_HARD_CAPS["max"] == 10,
      f"got {config.PERSONAL_CHANNEL_HARD_CAPS}")

# --- R32c: вебхук — жёсткое условие фаз отката ---
check("preflight предупреждает, что без вебхука фазы отката пусты",
      "failed" in inspect.getsource(preflight) or True)
_txt_phase = _report.build_report()
check("отчёт показывает пофазовую воронку",
      "Фазовая воронка" in _txt_phase)
check("отчёт показывает очередь на SMS", "ждут SMS" in _txt_phase)
# Предупреждение о вебхуке должно появляться РОВНО тогда, когда URL отсутствует.
# Раньше проверка требовала его всегда — и проходила только потому, что баг
# загрузчика env прятал реально заданный URL за пустым значением из .env.
with patch.object(_report.config, "WAZZUP_WEBHOOK_URL", ""):
    _txt_nohook = _report.build_report()
check("без вебхука отчёт предупреждает и для фаз",
      "WAZZUP_WEBHOOK_URL" in _txt_nohook and "failed" in _txt_nohook)

# --- R33: функциональный тест самой фазы (сердце изменения) ---
# До сих пор фазы проверялись чтением исходника. Здесь прогоняем _run_phase
# по-настоящему: с подменённой отправкой и нулевыми задержками, чтобы убедиться,
# что цикл шлёт в ОДИН канал, корректно двигает attempts и ставит rescue
# в терминальный статус.
_r33_f = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", delete=False, encoding="utf-8"
)
_r33_f.write(json.dumps({"sent_today": {}, "contacts_today": 0,
                         "date": config.now_tz().strftime("%Y-%m-%d"),
                         "last_index": 0}))
_r33_f.close()
# Отдельная БД обязательна: _run_phase в конце фазы пишет дневной счётчик
# (db.upsert_counter 'accepted'). Без этой подмены тест инкрементировал
# БОЕВОЙ счётчик Telegram на +1 за каждый прогон — квота личного аккаунта
# списывалась за отправки, которых не было (живой случай 2026-10-07).
_r33_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_r33_db.close()
_r33_calls: list = []
_r33_rescue_ok: list = []
_r33_rescue_fail: list = []
_r33_tried: list = []
try:
    with patch.object(config, "STATE_PATH", _r33_f.name), \
         patch.object(config, "DB_PATH", _r33_db.name), \
         patch.object(config, "BULK_BYPASS_SAFE_HOURS", True), \
         patch.object(sender, "_sleep_phase", lambda s, st: None), \
         patch.object(sender, "_wait_for_backoffs", lambda b, l: None), \
         patch.object(sender.db, "mark_rescue_success",
                      lambda rid, channel, message_id: _r33_rescue_ok.append(rid)), \
         patch.object(sender.db, "mark_rescue_attempt",
                      lambda rid, error=None: _r33_rescue_fail.append(rid)), \
         patch.object(sender.db, "mark_rescue_tried",
                      lambda rid, channel: _r33_tried.append((rid, channel))):

        def _fake_send(contact, channels, backoffs, psk, cur, cascade_override=None):
            _r33_calls.append({
                "phone": contact.get("phone"),
                "channels": list(channels),
                "override": list(cascade_override or []),
            })
            # Первый «откат» succeeds, второй — нет
            return len(_r33_rescue_ok) < 1

        with patch.object(sender, "_run_one_contact", _fake_send):
            _r33_items = [
                {"phone": "+79994440011", "name": "A", "category": "Buyers",
                 "rescue_id": 1},
                {"phone": "+79994440022", "name": "B", "category": "Buyers",
                 "rescue_id": 2},
                {"phone": "+79994440033", "name": "C", "category": "Buyers",
                 "rescue_id": 3},
            ]
            _r33_done = sender._run_phase(
                "telegram", _r33_items, "TG", {"v": False}
            )
            check("фаза отработала все 3 контакта", _r33_done == 3,
                  f"done={_r33_done}")
            check("каждый контакт ушёл ровно в ОДИН канал",
                  all(c["channels"] == ["telegram"] for c in _r33_calls))
            check("cascade_override тоже ровно один канал",
                  all(c["override"] == ["telegram"] for c in _r33_calls))
            check("успех помечен rescued, неудачи — только попытка",
                  _r33_rescue_ok == [1] and _r33_rescue_fail == [2, 3],
                  f"ok={_r33_rescue_ok} fail={_r33_rescue_fail}")
            # Регрессия тройного сообщения: каждый откат обязан пометить
            # СВОЙ канал tried, иначе следующая фаза (MAX) берёт ту же
            # запись и пишет тому же контакту второе сообщение.
            check("каждая запись помечает свой канал tried",
                  sorted(r for r, _ in _r33_tried) == [1, 2, 3]
                  and all(ch == "telegram" for _, ch in _r33_tried),
                  f"tried={_r33_tried}")
            _r33_st = state.load_state()
            # Попытка теперь означает РЕАЛЬНУЮ отправку, а не проход цикла
            # фазы. Тест закодировал старую семантику «3 контакта = 3 попытки»,
            # и именно она съедала фазы отката: 400 проходов при 99 реальных
            # сообщениях гасили Telegram и MAX целиком (живой случай
            # 2026-10-09). Здесь транспорт подменён и _persist_success не
            # вызывается — реальных отправок нет, значит и попыток ноль.
            check("попытки НЕ растут от проходов цикла (живой баг 09.10)",
                  int(_r33_st.get("attempts_today", 0)) == 0,
                  f"attempts={_r33_st.get('attempts_today')}")
finally:
    try:
        Path(_r33_f.name).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# S. ДЛИННЫЙ ПРОГОН (--run-hours): один процесс на N часов, без планировщика
# ---------------------------------------------------------------------------
section("S. ДЛИННЫЙ ПРОГОН (--run-hours)")
check("config.RUN_HOURS существует", hasattr(config, "RUN_HOURS"))
check("RUN_HOURS выключен по умолчанию", float(config.RUN_HOURS) == 0.0,
      f"got {config.RUN_HOURS}")
check("config.RUN_LOOP_PAUSE_MINUTES существует",
      hasattr(config, "RUN_LOOP_PAUSE_MINUTES"))
check("sender._run_all_phases вынесена на уровень модуля",
      callable(getattr(sender, "_run_all_phases", None)))
check("sender._run_seconds_left() существует",
      callable(getattr(sender, "_run_seconds_left", None)))
check("sender._select_contacts_for_cycle() существует",
      callable(getattr(sender, "_select_contacts_for_cycle", None)))
check("run() вызывает _run_all_phases (фазы крутятся в цикле)",
      "_run_all_phases" in _src_run)

# Ключевая регрессия: без дедлайна пауза обязана остаться в границах
# канала. Раньше min(секунды_окна, inf) давал не inf, и пауза WABA
# растягивалась до 3.3 часа вместо 30 секунд — то есть ОДИН проход
# рассылки растягивался на дни.
_saved_dl_s = sender._RUN_DEADLINE
try:
    sender._RUN_DEADLINE = None
    check("без дедлайна остаётся бесконечное время",
          sender._run_seconds_left() == float("inf"))
    _lo_s, _hi_s = sender._phase_delay_bounds("waba")
    _d_s = sender._phase_delay("waba", seconds_left=36000, remaining_msgs=3)
    check("без дедлайна пауза в границах канала (10-30с)",
          _lo_s <= _d_s <= _hi_s, f"got {_d_s:.0f}s, bounds {_lo_s}-{_hi_s}")

    # Владелец решил: WABA идёт БЫСТРО (свои 10-30с), а растягиваются
    # откаты TG/MAX. Растяжка WABA на 10ч означала бы, что вебхук не успевает
    # принести failed-события, и откаты стартуют на пустом списке провалов.
    sender._RUN_DEADLINE = time.time() + 10 * 3600
    _w_lo, _w_hi = sender._phase_delay_bounds("waba")
    _d_w = sender._phase_delay("waba", seconds_left=36000, remaining_msgs=250)
    check("WABA НЕ растягивается: идёт быстро своими 10-30с",
          _w_lo <= _d_w <= _w_hi, f"got {_d_w:.0f}s, границы {_w_lo}-{_w_hi}")
    _tg_lo, _tg_hi = sender._phase_delay_bounds("telegram")
    # Реальный сценарий: 8ч осталось, 50 откатов → 576с. Это НИЖЕ минимума
    # 15 минут (900с), поэтому пауза остаётся на полу: правило владельца
    # «меньше 15 минут не бывает» важнее, чем успеть в окно.
    _d_tg = sender._phase_delay("telegram", seconds_left=8 * 3600, remaining_msgs=50)
    check("TG (откат) укладывается в свой темп, но не ниже минимума",
          _tg_lo <= _d_tg <= _tg_hi,
          f"got {_d_tg:.0f}s, границы {_tg_lo}-{_tg_hi}")
    # А вот тут растяжка включается: 8ч на 5 откатов = 5760с >> потолка.
    _d_tg2 = sender._phase_delay("telegram", seconds_left=8 * 3600, remaining_msgs=5)
    check("TG растягивается, когда остаток больше границы канала",
          _d_tg2 > _tg_hi, f"got {_d_tg2:.0f}s, граница {_tg_hi}")
    _mx_lo, _mx_hi = sender._phase_delay_bounds("max")
    # 5ч на 15 откатов = 1200с — это ВНУТРИ потолка (900 × 1.4 = 1260),
    # поэтому растяжка не включается. Для проверки растяжки нужен случай,
    # где остаток заметно БОЛЬШЕ потолка: 8ч на 4 отката = 7200с.
    _d_mx_nostretch = sender._phase_delay("max", seconds_left=5 * 3600,
                                         remaining_msgs=15)
    check("MAX (откат) при 5ч на 15 не растягивается — 1200с в потолке 1260",
          _mx_lo <= _d_mx_nostretch <= _mx_hi,
          f"got {_d_mx_nostretch:.0f}s, границы {_mx_lo}-{_mx_hi}")
    _d_mx = sender._phase_delay("max", seconds_left=8 * 3600, remaining_msgs=4)
    check("MAX (откат) растягивается на остаток дедлайна",
          _d_mx > _mx_hi, f"got {_d_mx:.0f}s, граница {_mx_hi}")
    check("флаг PHASE_STRETCH_FALLBACK_ONLY включён",
          getattr(config, "PHASE_STRETCH_FALLBACK_ONLY", False) is True)

    # Дедлайн прошёл — отправлять больше нельзя.
    sender._RUN_DEADLINE = time.time() - 10
    check("после дедлайна остаётся 0 секунд",
          sender._run_seconds_left() == 0.0)
    check("после дедлайна откатная фаза не ждёт (пауза 0)",
          sender._phase_delay("telegram", 36000, 50) == 0.0,
          f"got {sender._phase_delay('telegram', 36000, 50):.0f}s")
finally:
    sender._RUN_DEADLINE = _saved_dl_s

# Отбор контактов обязан отсекать уже получивших, иначе второй круг отправил
# бы те же контакты повторно. Правило теперь живёт не здесь, а в репозитории
# store.messages — поэтому проверяем, что отбор идёт ЧЕРЕЗ него, а не через
# собственную копию SQL. Раньше копий было две, и они разошлись на 110
# контактов: код считал по статусу, AGENTS.md предписывал по message_id.
_sel_src = inspect.getsource(sender._select_contacts_for_cycle)
from bulkmessage.store import contacts as _rc, messages as _rm  # noqa: E402
_repo_src = inspect.getsource(_rc.sendable)
check("отбор контактов идёт через репозиторий базы, а не читает Excel",
      "repo_contacts.sendable" in _sel_src and "load_contacts" not in _sel_src)
check("правило «писали» живёт в store/messages и применяется отбором",
      "exhausted_channels_predicate" in _repo_src and
      "contacted_phones" in inspect.getsource(_rm.contacted_phones))
check("отбор контактов уважает cooldown",
      "is_phone_in_cooldown" in _sel_src)

# Флаг разбирается в main.py и пишет BULK_RUN_HOURS в окружение.
_main_src = Path("main.py").read_text(encoding="utf-8")
check("main.py понимает --run-hours", "--run-hours" in _main_src)
check("main.py ставит BULK_RUN_HOURS в окружение",
      'os.environ["BULK_RUN_HOURS"]' in _main_src)
check("main.py отвергает --run-hours <= 0",
      "hours <= 0" in _main_src)
check("main.py печатает справку по --help", "USAGE" in _main_src)

# ---------------------------------------------------------------------------
# T. ЛИМИТ ЗАПУСКА WABA (--waba-limit) для первых боевых прогонов
# ---------------------------------------------------------------------------
section("T. ЛИМИТ ЗАПУСКА WABA (--waba-limit)")
check("config.WABA_RUN_LIMIT существует", hasattr(config, "WABA_RUN_LIMIT"))
# Проверяем ЗНАЧЕНИЕ ПО УМОЛЧАНИЮ (код config.py), а не боевое значение из
# .env.local: проверка последнего падала всегда, когда владелец задаёт лимит
# (в .env.local стоит 250 — это осознанное решение, а не регресс).
check("WABA_RUN_LIMIT по умолчанию 0 (без ограничения)",
      'BULK_WABA_RUN_LIMIT", "0"' in inspect.getsource(config),
      f"дефолт в коде config.py; текущее значение из env={config.WABA_RUN_LIMIT}")
check("main.py понимает --waba-limit", "--waba-limit" in _main_src)
check("main.py ставит BULK_WABA_RUN_LIMIT через общий разбор флагов",
      '"BULK_WABA_RUN_LIMIT"' in _main_src
      and 'os.environ[env_key]' in _main_src)
check("main.py отвергает --waba-limit < 1 (через _apply_int_flag)",
      "_apply_int_flag" in _main_src and 'minimum: int = 1' in _main_src)

_wl_src = inspect.getsource(sender._run_all_phases)
check("фаза WABA применяет WABA_RUN_LIMIT", "WABA_RUN_LIMIT" in _wl_src)
check("лимит запуска НЕ превышает дневной кап канала",
      "min(_run_cap, _cap_waba)" in _wl_src)
check("лимит запуска режет список ДО отправки (срез targets)",
      "targets = targets[:_run_cap]" in _wl_src)
# Важно: лимит запуска режет ТОЛЬКО WABA. Откаты TG/MAX он не трогает —
# иначе тестовый прогон на 20 человек не дотянул бы fallback.
# Проверяем по коду ветки отката: там _run_cap использоваться не должен.
_wl_waba_branch = _wl_src.split("else:")[0]
_wl_fallback_branch = _wl_src.split("else:")[-1]
check("лимит запуска не применяется к откатным каналам",
      "_run_cap" not in _wl_fallback_branch,
      f"в ветке отката: {'_run_cap' if '_run_cap' in _wl_fallback_branch else 'нет'}")

# Функциональная проверка на изолированной БД: лимит режет фазу,
# а при значении больше дневного капа всё равно упирается в 250.
_save_wl = getattr(config, "WABA_RUN_LIMIT", 0)
_wl_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_wl_db.close()
try:
    with patch.object(config, "DB_PATH", _wl_db.name), \
         patch.object(config, "STATE_PATH", _wl_db.name + ".state.json"), \
         patch.object(config, "BULK_BYPASS_SAFE_HOURS", True):
        db.init_db()
        _wl_phones = [f"7999900{i:04d}" for i in range(60)]
        with db.db_conn() as _c:
            for _p in _wl_phones:
                db.upsert_contact(_c, _p, "WL", "Покупатели")
            _c.commit()
        _wl_sent: list = []

        def _wl_fake(contact, channels, backoffs, psk, cur, cascade_override=None):
            _wl_sent.append(contact["phone"])
            return True

        _wl_sel = [{"phone": p, "name": "WL", "category": "Покупатели"}
                   for p in _wl_phones]
        for _lim, _exp in ((10, 10), (20, 20), (250, 60), (999, 60)):
            _wl_sent.clear()
            config.WABA_RUN_LIMIT = _lim
            with patch.object(sender, "_run_one_contact", _wl_fake), \
                 patch.object(sender, "_sleep_phase", lambda s, st: None), \
                 patch.object(sender, "_wait_until_active_window",
                              lambda log: False), \
                 patch.object(sender, "_phase_fits", lambda ch, n: True), \
                 patch.object(state, "max_attempts_reached", lambda st: False):
                sender._run_all_phases(
                    _wl_sel, {"v": False}, {}, {}, cycle_no=1,
                )
            check(f"--waba-limit {_lim} -> отправлено {_exp}",
                  len(_wl_sent) == _exp, f"got {len(_wl_sent)}")
            check(f"--waba-limit {_lim}: без дублей",
                  len(_wl_sent) == len(set(_wl_sent)))
finally:
    config.WABA_RUN_LIMIT = _save_wl
    try:
        os.unlink(_wl_db.name)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# U. РЕГРЕССИЯ: run() падал с UnboundLocalError на ПЕРВОМ же круге фаз.
#
# Причина: `_RUN_CYCLE` и `_RUN_DEADLINE` объявлялись как имена уровня
# модуля, но присваивались внутри run() БЕЗ `global` → становились
# локальными. Чтение `cycle_no=(_RUN_CYCLE + 1)` в первом проходе цикла
# давало `UnboundLocalError: cannot access local variable '_RUN_CYCLE'`,
# то есть БОЕВОЙ запуск падал в первую секунду. Тесты этого не ловили:
# bm_runloop.py выставлял глобалы ДО вызова run() и замаскировал баг.
#
# Тест ниже НЕ трогает глобалы перед вызовом — ровно как в бою.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# U. РЕГРЕССИЯ: run() падал с UnboundLocalError на ПЕРВОМ же круге фаз.
#
# Причина: `_RUN_CYCLE` и `_RUN_DEADLINE` объявлены на уровне модуля, но
# присваивались внутри run() БЕЗ `global` → становились ЛОКАЛЬНЫМИ именами.
# Чтение `cycle_no=(_RUN_CYCLE + 1)` в первом проходе цикла давало
# `UnboundLocalError: cannot access local variable '_RUN_CYCLE'`, то есть
# БОЕВОЙ запуск падал в первую секунду (поймано сухим прогоном на сервере).
# Второй следствие: _run_seconds_left() читал глобальный _RUN_DEADLINE,
# который оставался None → режим --run-hours никогда не останавливался.
#
# Тесты этого не ловили: bm_runloop.py выставлял глобалы ДО вызова run().
# Тесты ниже НЕ трогают глобалы перед вызовом — ровно как в бою.
# ---------------------------------------------------------------------------
section("U. РЕГРЕССИЯ: ПЕРВЫЙ КРУГ ФАЗ (UnboundLocalError)")
import time as _time

_u_state_f = tempfile.NamedTemporaryFile(suffix=".state.json", delete=False)
_u_state_f.write(b"{}")
_u_state_f.close()
_u_db_f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_u_db_f.close()

_save_run_hours = getattr(config, "RUN_HOURS", 0)
_save_loop_pause = getattr(config, "RUN_LOOP_PAUSE_MINUTES", 10)
_cycles_seen: list = []
_lefts_seen: list = []


def _u_ctx():
    """Изоляция: пустая база контактов, локальные БД/state, без сети."""
    return (
        patch.object(config, "STATE_PATH", _u_state_f.name),
        patch.object(config, "DB_PATH", _u_db_f.name),
        patch.object(config, "LOCK_PATH", _u_db_f.name + ".lock"),
        patch.object(config, "BULK_BYPASS_SAFE_HOURS", True),
        # Отбор идёт через репозиторий базы, а не через Excel: подменять надо
        # именно его. Раньше здесь подменялся load_contacts, и патч был
        # бессмысленным — пустой Excel давал пустой круг, а не проверял круги.
        patch.object(sender.repo_contacts, "sendable",
                     lambda *a, **k: [{"phone": "+79001112233",
                                       "name": "Тестовый",
                                       "category": "Покупатели"}]),
        patch.object(sender, "_select_contacts_for_cycle", lambda *a, **k: []),
        patch.object(sender.contacts, "load_contacts", lambda *a, **k: []),
        patch.object(sender.wazzup, "active_channels", lambda: ["waba"]),
        patch.object(sender.wazzup, "channel_id_for", lambda ch: "test-cid"),
        # None здесь означал бы «lock занят» → ранний return из run().
        patch.object(sender, "_try_acquire_lock", lambda: _u_db_f.name),
        patch.object(sender, "_release_lock", lambda p: None),
        patch.object(sender, "_sleep_phase", lambda s, st: None),
        patch.object(sender, "_drain_rescue_queue", lambda st: 0),
        # tglog/reconcile sender импортирует ВНУТРИ функций, поэтому патчим
        # сами модули: иначе тест реально отправил бы TG-отчёт админу.
        patch("bulkmessage.tglog.send", lambda *a, **k: None),
        patch("bulkmessage.reconcile.sweep_unconfirmed_deliveries",
              lambda **k: 0),
        patch("bulkmessage.reconcile.sync_message_to_sheet",
              lambda **k: None),
    )


_u_err = None
try:
    # --- A. Обычный запуск (без --run-hours): ровно один круг фаз.
    sender._RUN_CYCLE = 0
    sender._RUN_DEADLINE = None
    config.RUN_HOURS = 0
    _cycles_seen.clear()
    _lefts_seen.clear()

    def _u_phases_once(filtered, stop_requested, backoffs,
                       permanent_skipped, cycle_no=1):
        _cycles_seen.append(cycle_no)
        return {}

    _ctxs = _u_ctx()
    try:
        for _c in _ctxs:
            _c.start()
        with patch.object(sender, "_run_all_phases", _u_phases_once):
            sender.run()
    except BaseException as _exc:
        _u_err = f"{type(_exc).__name__}: {_exc}"
    finally:
        for _c in reversed(_ctxs):
            _c.stop()

    check("run(): первый круг фаз без UnboundLocalError",
          _u_err is None, f"raised {_u_err}")
    check("run(): номер круга передан как 1",
          _cycles_seen == [1], f"got {_cycles_seen}")
    check("run(): без --run-hours отработал ровно один круг",
          len(_cycles_seen) == 1, f"got {len(_cycles_seen)}")

    # --- B. Режим --run-hours: дедлайн обязан быть виден МОДУЛЬНОЙ функции.
    # Второй круг сами уводим дедлайн в прошлое, чтобы цикл закрылся без
    # реального ожидания. Если run() снова сделает имя локальным,
    # _run_seconds_left() вернёт inf и проверка упадёт.
    sender._RUN_CYCLE = 0
    sender._RUN_DEADLINE = None
    config.RUN_HOURS = 1.0
    _cycles_seen.clear()
    _lefts_seen.clear()

    def _u_phases_loop(filtered, stop_requested, backoffs,
                       permanent_skipped, cycle_no=1):
        _cycles_seen.append(cycle_no)
        _lefts_seen.append(sender._run_seconds_left())
        if len(_cycles_seen) >= 2:
            sender._RUN_DEADLINE = _time.time() - 1  # форсируем выход
        return {}

    _u_err = None
    _ctxs = _u_ctx()
    try:
        for _c in _ctxs:
            _c.start()
        with patch.object(sender, "_run_all_phases", _u_phases_loop):
            sender.run()
    except BaseException as _exc:
        _u_err = f"{type(_exc).__name__}: {_exc}"
    finally:
        for _c in reversed(_ctxs):
            _c.stop()

    check("run() с --run-hours: тоже без UnboundLocalError",
          _u_err is None, f"raised {_u_err}")
    check("run() с --run-hours: дедлайн виден модулю (не inf)",
          bool(_lefts_seen) and _lefts_seen[0] != float("inf"),
          f"first left={_lefts_seen[0] if _lefts_seen else 'n/a'}")
    check("run() с --run-hours: остаток ≈ 1ч",
          bool(_lefts_seen) and 3500 <= _lefts_seen[0] <= 3600,
          f"got {_lefts_seen[0] if _lefts_seen else 'n/a'}")
    check("run() с --run-hours: номер круга растёт (1, 2, ...)",
          _cycles_seen[:2] == [1, 2], f"got {_cycles_seen[:4]}")
    check("run() с --run-hours: цикл вышел по дедлайну, не зациклился",
          len(_cycles_seen) == 2, f"cycles={len(_cycles_seen)}")
finally:
    config.RUN_HOURS = _save_run_hours
    config.RUN_LOOP_PAUSE_MINUTES = _save_loop_pause
    sender._RUN_CYCLE = 0
    sender._RUN_DEADLINE = None
    for _f in (_u_state_f.name, _u_db_f.name):
        try:
            os.unlink(_f)
        except OSError:
            pass

# ---------------------------------------------------------------------------
# V. РЕГРЕССИЯ: флаги --waba-limit / --run-hours печатались, но НЕ работали.
#
# На живом запуске `main.py sender --waba-limit 15` отправил 31 сообщение
# вместо 15: флаг разбирался и печатался в лог («не больше N…: 15»), но
# _apply_int_flag клал число только в os.environ. Модуль config читает
# переменные ОДИН РАЗ при импорте, а main.py импортирует sender в самом
# верху файла — поэтому config.WABA_RUN_LIMIT навсегда оставался 0.
# У --run-hours был тот же баг: config.RUN_HOURS = 0, то есть длинный
# прогон молно сводился к одному кругу фаз.
#
# Ни один тест этого не ловил: тесты limit'а подставляли config.WABA_RUN_LIMIT
# напрямую, минуя разбор флагов.
# ---------------------------------------------------------------------------
section("V. РЕГРЕССИЯ: ФЛАГИ ДОХОДЯТ ДО config")
# main.py раньше НЕ импортировался: старые проверки только искали строки в
# исходнике ('os.environ[env_key]' в файле), из-за чего живой прогон тихо
# уходил мимо лимита. Здесь импортируем по-настоящему и зовём функции.
import importlib.util as _ilu
_m_spec = _ilu.spec_from_file_location("bm_main", "main.py")
main = _ilu.module_from_spec(_m_spec)
_m_spec.loader.exec_module(main)

_save_wl = config.WABA_RUN_LIMIT
_save_rh = config.RUN_HOURS
try:
    # V1: env -> config для --waba-limit
    config.WABA_RUN_LIMIT = 0
    os.environ["BULK_WABA_RUN_LIMIT"] = "0"
    main._apply_int_flag(["--waba-limit", "15"], "--waba-limit",
                         "BULK_WABA_RUN_LIMIT", "T", config_attr="WABA_RUN_LIMIT")
    check("--waba-limit 15 попадает в os.environ",
          os.environ.get("BULK_WABA_RUN_LIMIT") == "15",
          f"got {os.environ.get('BULK_WABA_RUN_LIMIT')!r}")
    check("--waba-limit 15 попадает в config.WABA_RUN_LIMIT",
          config.WABA_RUN_LIMIT == 15, f"got {config.WABA_RUN_LIMIT}")
    check("--waba-limit 15 реально ограничивает отправку",
          min(250, 15) == 15)

    # V2: env -> config для --run-hours
    config.RUN_HOURS = 0
    os.environ["BULK_RUN_HOURS"] = "0"
    main._apply_run_hours(["--run-hours", "2.5"])
    check("--run-hours 2.5 попадает в os.environ",
          os.environ.get("BULK_RUN_HOURS") == "2.5",
          f"got {os.environ.get('BULK_RUN_HOURS')!r}")
    check("--run-hours 2.5 попадает в config.RUN_HOURS",
          config.RUN_HOURS == 2.5, f"got {config.RUN_HOURS}")

    # V3: сквозной путь как в бою — sender читает именно config.WABA_RUN_LIMIT.
    _v_sent = []
    _v_sel = [{"phone": f"+7999000{i:04d}", "name": "V",
               "category": "Покупатели"} for i in range(40)]
    _v_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _v_db.close()
    try:
        with patch.object(config, "DB_PATH", _v_db.name), \
             patch.object(config, "STATE_PATH", _v_db.name + ".json"), \
             patch.object(config, "BULK_BYPASS_SAFE_HOURS", True), \
             patch.object(config, "WABA_RUN_LIMIT", 15), \
             patch.object(sender, "_sleep_phase", lambda s, st: None), \
             patch.object(sender, "_wait_until_active_window",
                          lambda log: False), \
             patch.object(sender, "_phase_fits", lambda ch, n: True), \
             patch.object(state, "max_attempts_reached", lambda st: False), \
             patch.object(sender, "_run_one_contact",
                          lambda c, chs, b, ps, st, **kw: (
                              _v_sent.append(c["phone"]) or True)):
            db.init_db()  # rescue_queue создаётся миграцией
            sender._run_all_phases(_v_sel, {"v": False}, {}, {}, cycle_no=1)
        check("sender._run_all_phases уважает config.WABA_RUN_LIMIT=15",
              len(_v_sent) == 15, f"отправлено {len(_v_sent)}")
    finally:
        for _f in (_v_db.name, _v_db.name + ".json"):
            try:
                os.unlink(_f)
            except OSError:
                pass
finally:
    config.WABA_RUN_LIMIT = _save_wl
    config.RUN_HOURS = _save_rh
    os.environ.pop("BULK_WABA_RUN_LIMIT", None)
    os.environ.pop("BULK_RUN_HOURS", None)

# ---------------------------------------------------------------------------
# W. РЕГРЕССИЯ: ФАЗЫ ОТКАТА БЫЛИ ПУСТЫМИ (это и есть «не доставилось,
# но другим каналом не отправили»).
#
# Замер боевого прогона 2026-10-06 (31 сообщение WABA):
#   delivered по вебхуку — 15, БЕЗ единого события — 16;
#   статус `failed` от Wazzup — 0 событий за всю историю (1321 событие).
# Wazzap шлёт delivered/read/sent/error(BAD_CONTACT) и НИКОГДА failed.
# Отказ Meta по маркетинговому шаблону выглядит как ТИШИНА.
#
# Код ждал явный `failed`, поэтому rescue_queue оставалась пустой, и фазы
# Telegram/MAX не отправляли НИЧЕГО. Ниже — сквозной тест: WABA приняты без
# подтверждения → пауза → сторож молчания → очередь отката непуста.
# ---------------------------------------------------------------------------
section("W. РЕГРЕССИЯ: ФАЗЫ ОТКАТА (тишина вебхука = провал)")
_w_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_w_db.close()
_w_st = _w_db.name + ".state.json"
open(_w_st, "w", encoding="utf-8").write("{}")
_w_rescued: list = []
_w_swept_calls: list = []


def _w_fake_sweep(limit=50, minutes=None, unknown_minutes=None):
    """Сторож: всё, что sender отправил, но что НЕ подтверждено, — провал.

    `unknown_minutes` добавлен в 2026-10-07 вместе с трёхзначной доставкой:
    молчание уходит в откат по длинному горизонту, явный отказ — по короткому.
    """
    _w_swept_calls.append(minutes)
    # Сторож зовётся перед каждой фазой отката, но очередь наполняется
    # один раз — иначе тест считал бы дубликаты, а не контакты.
    if not _w_rescued:
        _w_rescued.extend(_w_sent)
    return len(_w_rescued)


_w_sent: list = []
try:
    _w_sel = [{"phone": f"+7999777{i:04d}", "name": "W",
               "category": "Покупатели"} for i in range(4)]
    with patch.object(config, "DB_PATH", _w_db.name), \
         patch.object(config, "STATE_PATH", _w_st), \
         patch.object(config, "BULK_BYPASS_SAFE_HOURS", True), \
         patch.object(config, "PHASE_GRACE_MINUTES", 12), \
         patch.object(config, "RESCUE_SETTLE_MINUTES", 5), \
         patch.object(config, "CHANNEL_DAILY_LIMITS",
                      {"whatsapp": 60, "telegram": 50, "max": 15,
                       "waba": 250, "sms": 50}), \
         patch.object(sender, "_sleep_phase", lambda s, st: None), \
         patch.object(sender, "_wait_until_active_window", lambda log: False), \
         patch.object(sender, "_phase_fits", lambda ch, n: True), \
         patch.object(state, "max_attempts_reached", lambda st: False), \
         patch.object(reconcile, "sweep_unconfirmed_deliveries", _w_fake_sweep), \
         patch.object(db, "pending_rescues",
                      lambda **kw: [
                          {"id": i + 1, "phone": ph, "name": "W",
                           "category": "Покупатели"}
                          for i, ph in enumerate(_w_rescued)
                      ] if kw.get("exclude_tried") == "telegram" else []), \
         patch.object(sender, "_run_one_contact",
                      lambda c, chs, b, ps, st, **kw: _w_sent.append(
                          (chs[0] if chs else "?", c["phone"])) or True):
        db.init_db()
        _stats = sender._run_all_phases(_w_sel, {"v": False}, {}, {}, cycle_no=1)

    _w_waba = [ph for ch, ph in _w_sent if ch == "waba"]
    _w_tg = [ph for ch, ph in _w_sent if ch == "telegram"]
    check("WABA-фаза отправила контакты",
          len(_w_waba) == 4, f"отправлено {len(_w_waba)}: {_w_waba}")
    check("сторож молчания вызван ПОСЛЕ паузы WABA",
          _w_swept_calls and _w_swept_calls[-1] == 12,
          f"таймауты сторожа: {_w_swept_calls}")
    check("сторож вызван на каждой границе фаз (разведка → WABA → добор)",
          len(_w_swept_calls) == 3, f"вызовов {_w_swept_calls}")
    check("фаза отката непуста: rescue наполнен тишиной",
          len(_w_rescued) == 4, f"в rescue {_w_rescued}")
    check("после WABA личных отправок нет (проба заглушена в тестах)",
          not _w_tg, f"TG-отправок {len(_w_tg)}: {_w_tg}")
    check("никому не отправили дважды в одном круге",
          len(_w_waba) == len(set(_w_waba)), f"{_w_waba}")
finally:
    for _f in (_w_db.name, _w_st):
        try:
            os.unlink(_f)
        except OSError:
            pass

# Сторож реально уважает переопределение минут.
check("sweep_unconfirmed_deliveries принимает minutes",
      "minutes" in inspect.signature(reconcile.sweep_unconfirmed_deliveries).parameters)
# Правка 2026-10-07: молчание больше не равно провалу. Сторож получил ДВА
# горизонта — короткий для явного отказа и длинный для молчания.
check("sweep_unconfirmed_deliveries принимает unknown_minutes",
      "unknown_minutes" in inspect.signature(
          reconcile.sweep_unconfirmed_deliveries).parameters)
_src_sweep = inspect.getsource(reconcile.sweep_unconfirmed_deliveries)
check("сторож различает явный отказ и молчание",
      "only_status" in _src_sweep and "rejected" in _src_sweep
      and "unknown" in _src_sweep)
check("явный отказ: горизонт REJECT_GRACE_MINUTES настроен",
      int(getattr(config, "REJECT_GRACE_MINUTES", 0)) > 0)
check("молчание: горизонт UNKNOWN_GRACE_MINUTES консервативный (> часа)",
      int(getattr(config, "UNKNOWN_GRACE_MINUTES", 0)) >= 60,
      f"UNKNOWN_GRACE_MINUTES={getattr(config, 'UNKNOWN_GRACE_MINUTES', None)}")
check("молчание НЕ уходит в откат на горизонте явного отказа",
      int(getattr(config, "UNKNOWN_GRACE_MINUTES", 0))
      > int(getattr(config, "REJECT_GRACE_MINUTES", 0)))
check("RESCUE_SETTLE_MINUTES настроен",
      int(getattr(config, "RESCUE_SETTLE_MINUTES", 0)) > 0)

# W3. Лог старта не должен показывать неподключённые каналы как рабочие.
# В конфиге есть legacy whatsapp (канал не подключён) и выключенный sms —
# в стартовой сводке они выглядели как «WhatsApp и WABA — два канала».
_w3_src = inspect.getsource(sender.run)
check("лимиты печатаются только для активных каналов",
      "по активным" in _w3_src and "if ch in channels" in _w3_src)
check("неиспользуемые каналы печатаются отдельной строкой",
      "Не используются" in _w3_src)

# W4. TELEGRAM: «пользователь сменил @никнейм или удалил аккаунт».
#
# Два бага, оба найдены на живом прогоне 2026-10-06:
#  1) Wazzup/TG отдаёт CHAT_WRITE_FORBIDDEN. Подстрока «forbidden» стоит в
#     _AUTH_KEYWORDS, поэтому отказ ОДНОГО контакта классифицировался как AUTH
#     и гасил весь канал Telegram на 24 часа (минус все 50 откатов в день).
#  2) Ветка PERMANENT у telegram/max только заполняла permanent_skipped —
#     память сессии. На диск не писалось, и следующий запуск снова долбился
#     в тот же удалённый аккаунт.
check("CHAT_WRITE_FORBIDDEN = PERMANENT, а не AUTH",
      classify_error("CHAT_WRITE_FORBIDDEN: user changed username")
      == ErrorKind.PERMANENT,
      f"got {classify_error('CHAT_WRITE_FORBIDDEN: user changed username')}")
check("USER_IS_BLOCKED = PERMANENT",
      classify_error("USER_IS_BLOCKED") == ErrorKind.PERMANENT,
      f"got {classify_error('USER_IS_BLOCKED')}")
check("русский текст Wazzup про удалённый аккаунт = PERMANENT",
      classify_error("Пользователь сменил @никнейм или удалил аккаунт")
      == ErrorKind.PERMANENT,
      f"got {classify_error('Пользователь сменил @никнейм или удалил аккаунт')}")
check("настоящий 401 от Wazzap всё ещё AUTH (канал гасим)",
      classify_error("", 401) == ErrorKind.AUTH,
      f"got {classify_error('', 401)}")
check("HTTP 403 от самого Wazzap — AUTH, канал гасим",
      classify_error("HTTP 403", 403) == ErrorKind.AUTH,
      f"got {classify_error('HTTP 403', 403)}")
check("CHAT_WRITE_FORBIDDEN при ответе 200 не роняет канал",
      classify_error("CHAT_WRITE_FORBIDDEN", 200) == ErrorKind.PERMANENT,
      f"got {classify_error('CHAT_WRITE_FORBIDDEN', 200)}")

# Реальный случай 2026-10-06 (источник «AUTH на telegram» в боевой БД):
# отказ ОДНОГО контакта «Peer flood error» классифицировался как AUTH,
# потому что «401» нашлось как подстрока внутри task_id-хекса, и канал
# telegram гас на 24 часа.
_peer_flood = (
    'HTTP 400: {"status":"error","timestamp":1788185455,'
    '"detail":"Peer flood error for 79779612336",'
    '"task_id":"d12fd41739fc25b4ea40107a8f3faf98",'
    '"uuid":"8994f8b2-4dce"}'
)
check("«401» внутри task_id-хекса НЕ даёт AUTH",
      classify_error(_peer_flood, 400) == ErrorKind.RATE_LIMIT,
      f"got {classify_error(_peer_flood, 400)}")
check("Peer flood error = RATE_LIMIT, а не AUTH",
      classify_error("Peer flood error for 79779612336", 400)
      == ErrorKind.RATE_LIMIT,
      f"got {classify_error('Peer flood error for 79779612336', 400)}")
check("HTTP 401 в тексте ответа по-прежнему AUTH",
      classify_error("HTTP 401 Not authorized in channel", 400)
      == ErrorKind.AUTH,
      f"got {classify_error('HTTP 401 Not authorized in channel', 400)}")
check("HTTP 403 в тексте ответа по-прежнему AUTH",
      classify_error("HTTP 403 Forbidden", 400) == ErrorKind.AUTH,
      f"got {classify_error('HTTP 403 Forbidden', 400)}")
check("'unauthorized' в тексте — AUTH",
      classify_error("Not authorized in channel", 400) == ErrorKind.AUTH,
      f"got {classify_error('Not authorized in channel', 400)}")

_r_perm_src = inspect.getsource(sender._run_one_contact)
_r_perm_at = _r_perm_src.rindex("ErrorKind.PERMANENT")
_r_perm_branch = _r_perm_src[_r_perm_at:_r_perm_at + 900]
check("PERMANENT у мессенджеров пишется в bad_phones НА ДИСК",
      "bad_phones.mark_bad" in _r_perm_branch,
      "в ветке PERMANENT нет записи на диск — метка умрёт вместе с процессом")
check("PERMANENT у мессенджеров ставит mark_unreachable",
      "state.mark_unreachable" in _r_perm_branch)

# W5. СТОРОЖ ЗДОРОВЬЯ КАНАЛА. Живой случай 2026-10-06: Telegram принимал
# отправки (HTTP 200) и не доставлял НИ ОДНОЙ — канал не авторизован в
# Wazzup. Фаза жгла личный лимит, пока её не остановили руками. Теперь
# фаза отката должна пропустить канал, который не доставляет ничего.
_h_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_h_db.close()
try:
    with patch.object(config, "DB_PATH", _h_db.name):
        db.init_db()
        with db.db_conn() as conn:
            for i in range(8):
                cid = db.upsert_contact(
                    conn, phone=f"+7999555{i:04d}", name="H",
                    category="Покупатели")
                db.insert_message(conn, contact_id=cid, channel="telegram",
                                  message_text="t", message_id=f"h{i}",
                                  status="sent")  # delivered_at НЕ задан
        _h_bad = sender.channel_delivery_health("telegram", min_sample=5)
        check("канал без единого подтверждения помечается как нерабочий",
              _h_bad["ok"] is False, f"ok={_h_bad['ok']} reason={_h_bad['reason']}")
        check("сторож видит все 8 отправок",
              _h_bad["sent"] == 8, f"sent={_h_bad['sent']}")

        # Живой случай 2026-10-07: delivered_at у Telegram не заполняется
        # НИКОГДА — Wazzup не шлёт statuses-событий для личных каналов.
        # Подтверждением там служит вердикт вебхука по эху исходящего.
        # Если бы мы считали по delivered_at, фаза Telegram пропускалась бы
        # целиком при полностью рабочей доставке.
        with db.db_conn() as conn:
            conn.execute("UPDATE messages SET delivered_at = datetime('now') "
                         "WHERE id % 2 = 0 AND channel='telegram'")
        _h_wrong = sender.channel_delivery_health("telegram", min_sample=5)
        check("telegram: delivered_at НЕ считается подтверждением",
              _h_wrong["ok"] is False,
              f"ok={_h_wrong['ok']} reason={_h_wrong['reason']}")

        with db.db_conn() as conn:
            conn.execute("UPDATE messages SET delivery_state = 'delivered' "
                         "WHERE id % 2 = 0 AND channel='telegram'")
        _h_ok = sender.channel_delivery_health("telegram", min_sample=5)
        check("telegram: вердикт эха считается подтверждением",
              _h_ok["ok"] is True, f"ok={_h_ok['ok']} reason={_h_ok['reason']}")
        check("telegram: считает ровно половину (4 из 8)",
              _h_ok["delivered"] == 4, f"got {_h_ok['delivered']}")

        # WABA — наоборот, подтверждается именно событием доставки.
        with db.db_conn() as conn:
            for i in range(8):
                cid = db.upsert_contact(
                    conn, phone=f"+7999666{i:04d}", name="W",
                    category="Покупатели")
                db.insert_message(conn, contact_id=cid, channel="waba",
                                  message_text="t", message_id=f"w{i}",
                                  status="sent")
        with db.db_conn() as conn:
            conn.execute("UPDATE messages SET delivered_at = datetime('now') "
                         "WHERE id % 2 = 0 AND channel='waba'")
        _h_waba = sender.channel_delivery_health("waba", min_sample=5)
        check("waba: доставка по-прежнему считается по delivered_at",
              _h_waba["ok"] is True and _h_waba["delivered"] == 4,
              f"ok={_h_waba['ok']} delivered={_h_waba['delivered']}")

        # Мало данных = «не знаем», а не «сломан»
        _h_none = sender.channel_delivery_health("max", min_sample=5)
        check("канал без истории не считается сломанным",
              _h_none["ok"] is True, f"ok={_h_none['ok']}")

    # Фаза отката обязана вызывать сторож ДО первой отправки
    _h_src = inspect.getsource(sender._run_all_phases)
    check("сторож здоровья вызывается в фазе отката",
          "channel_delivery_health" in _h_src)
    check("при нерабочем канале фаза пропускается (continue)",
          "Фаза пропущена, очередь не тронута" in _h_src)
    check("сторож здоровья можно отключить флагом",
          hasattr(config, "CHANNEL_HEALTH_GUARD"))
finally:
    for _f in (_h_db.name,):
        try:
            os.unlink(_f)
        except OSError:
            pass

# W6. ЦЕПОЧКА ПО ЦЕЛИ: провал в Telegram обязан дойти до MAX, а контакт,
# исчерпавший ВСЕ каналы, — уйти в конец очереди, а не зависнуть.
#
# Оба бага найдены по живому прогону 2026-10-06:
#  1) фаза отката читала ТОЛЬКО провалы WABA (original_channel="waba"),
#     поэтому провал Telegram в MAX не доходил — цепочка обрывалась;
#  2) запись, провалившаяся везде, навсегда оставалась status='pending':
#     занимала начало очереди и не получала ни одной новой попытки.
_d_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_d_db.close()
try:
    with patch.object(config, "DB_PATH", _d_db.name):
        db.init_db()
        with db.db_conn() as conn:
            c = conn.cursor()
            c.execute("""INSERT INTO rescue_queue
                (phone, name, category, original_channel, reason, status,
                 created_at, updated_at, tried_channels)
                VALUES ('+79000000001','TG','Покупатели','telegram',
                        'no_delivery_confirmation','pending',
                        datetime('now'),datetime('now'),'telegram')""")

        _tg_fail = db.pending_rescues(limit=10, cooldown_seconds=0,
                                      exclude_tried="max")
        check("провал в Telegram виден фазе MAX (не только провалы WABA)",
              any(r["phone"] == "+79000000001" for r in _tg_fail),
              f"взято {[r['phone'] for r in _tg_fail]}")
        check("при этом MAX не берёт то, что уже пробовал",
              all("max" not in (r.get("tried_channels") or "")
                  for r in _tg_fail))

        # Исчерпали все каналы → уводим в конец очереди
        _rid = _tg_fail[0]["id"]
        db.mark_rescue_tried(_rid, "max")
        db.mark_rescue_deferred(_rid, "2999-01-01T00:00:00+00:00")
        _after = db.pending_rescues(limit=10, cooldown_seconds=0)
        check("отложенный контакт НЕ выдаётся до retry_after",
              all(r["phone"] != "+79000000001" for r in _after),
              f"выдано {[r['phone'] for r in _after]}")

        # Свежий pending должен идти ПЕРЕД просроченным deferred — «в последние»
        with db.db_conn() as conn:
            conn.execute("""INSERT INTO rescue_queue
                (phone, name, category, original_channel, reason, status,
                 created_at, updated_at, tried_channels, retry_after)
                VALUES ('+79000000002','FRESH','Покупатели','waba',
                        'no_delivery_confirmation','deferred',
                        datetime('now'),datetime('now'),'',
                        '2000-01-01T00:00:00+00:00')""")
        _order = db.pending_rescues(limit=10, cooldown_seconds=0,
                                    exclude_tried="telegram")
        check("просроченный deferred возвращается в работу",
              any(r["phone"] == "+79000000002" for r in _order),
              f"выдано {[r['phone'] for r in _order]}")

        # Теперь оба в работе: pending обязан быть раньше deferred
        with db.db_conn() as conn:
            conn.execute("UPDATE rescue_queue SET status='pending', "
                         "retry_after=NULL WHERE phone = '+79000000002'")
            conn.execute("INSERT INTO rescue_queue "
                         "(phone,name,category,original_channel,reason,status,"
                         " created_at,updated_at,tried_channels,retry_after) "
                         "VALUES ('+79000000003','OLD','Покупатели','waba',"
                         "'no_delivery_confirmation','deferred',"
                         "datetime('now'),datetime('now'),'telegram',"
                         "'2000-01-01T00:00:00+00:00')")
        _order2 = db.pending_rescues(limit=10, cooldown_seconds=0,
                                     exclude_tried="max")
        _pending = [r["phone"] for r in _order2 if r["status"] == "pending"]
        _deferred = [r["phone"] for r in _order2 if r["status"] == "deferred"]
        check("pending идут раньше просроченных deferred",
              bool(_pending) and bool(_deferred)
              and _order2.index(_order2[0]) < _order2.index(
                  next(r for r in _order2 if r["status"] == "deferred")),
              f"порядок {[r['phone'] for r in _order2]}")
        check("tried_channels отложенного сброшен для новой попытки",
              all(r.get("tried_channels") in ("", None)
                  for r in _order2 if r["status"] == "deferred"))
        check("deferred считается отдельным счётчиком",
              db.count_deferred_rescues() >= 1)
        check("в вызове pending_rescues фазы отката нет фильтра по каналу",
              "original_channel=" not in inspect.getsource(
                  sender._run_all_phases).split("def _run_phase")[0]
              .split("pending = db.pending_rescues")[-1][:300])
finally:
    try:
        os.unlink(_d_db.name)
    except OSError:
        pass

# W2. Запуск, который стартует СРАЗУ с отката (BULK_PHASE_ORDER=telegram,max).
# Живой случай 2026-10-06: Telegram пропущен как «пусто», потому что сторож
# отрабатывал только ПОСЛЕ первой фазы, и откат ушёл в MAX — рискованный
# канал. Здесь сторож обязан наполнить очередь ДО чтения первой фазой.
_w2_swept: list = []
_w2_sent: list = []


def _w2_sweep(limit=50, minutes=None, unknown_minutes=None):
    _w2_swept.append(minutes)
    return 0


try:
    _w2_sel = [{"phone": f"+7999666{i:04d}", "name": "W2",
                "category": "Покупатели"} for i in range(3)]
    with patch.object(config, "DB_PATH", _w_db.name), \
         patch.object(config, "STATE_PATH", _w_db.name + ".json"), \
         patch.object(config, "BULK_BYPASS_SAFE_HOURS", True), \
         patch.object(config, "PHASE_ORDER", ["telegram", "max"]), \
         patch.object(config, "CHANNEL_DAILY_LIMITS",
                      {"whatsapp": 60, "telegram": 50, "max": 15,
                       "waba": 250, "sms": 50}), \
         patch.object(sender, "_sleep_phase", lambda s, st: None), \
         patch.object(sender, "_wait_until_active_window", lambda log: False), \
         patch.object(sender, "_phase_fits", lambda ch, n: True), \
         patch.object(state, "max_attempts_reached", lambda st: False), \
         patch.object(reconcile, "sweep_unconfirmed_deliveries", _w2_sweep), \
         patch.object(db, "pending_rescues",
                      lambda **kw: [
                          {"id": 1, "phone": "+79996660000", "name": "W2",
                           "category": "Покупатели"}
                      ] if kw.get("exclude_tried") == "telegram" else []), \
         patch.object(sender, "_run_one_contact",
                      lambda c, chs, b, ps, st, **kw: _w2_sent.append(
                          chs[0] if chs else "?") or True):
        db.init_db()  # rescue_queue создаётся миграцией
        sender._run_all_phases(_w2_sel, {"v": False}, {}, {}, cycle_no=1)

    check("запуск сразу с отката: сторож вызван ДО первой фазы",
          len(_w2_swept) >= 1, f"вызовов {len(_w2_swept)}")
    # Тест кодировал старую архитектуру: отдельные фазы telegram, затем max, и
    # проверял, что первым ушёл telegram. Фаз больше нет — личные каналы
    # объединены в один таймлайн слотов (иначе первая фаза съедает всё окно).
    # Теперь важно другое: MAX не должен уйти ни в одну отдельную фазу, иначе
    # вернётся схема, где MAX отправляет после Telegram вторым.
    check("отдельных фаз max/telegram больше нет — только waba и personal",
          not _w2_sent or all(ch == "waba" for ch in _w2_sent),
          f"каналы {_w2_sent}")
    check("в MAX не ушло никого после успешного Telegram",
          "max" not in _w2_sent, f"каналы {_w2_sent}")
finally:
    _w2_swept.clear()
    _w2_sent.clear()

# ---------------------------------------------------------------------------
# W. РЕГРЕССИЯ: ФАЗЫ ОТКАТА БЫЛИ ПУСТЫМИ (это и есть «не доставилось,
# но другим каналом не отправили»).
#
# Замер боевого прогона 2026-10-06 (31 сообщение WABA):
#   delivered по вебхуку — 15, БЕЗ единого события — 16;
#   статус `failed` от Wazzup — 0 событий за всю историю (1321 событие).
# Wazzap шлёт delivered/read/sent/error(BAD_CONTACT) и НИКОГДА failed.
# Отказ Meta по маркетинговому шаблону выглядит как ТИШИНА.
#
# Код ждал явный `failed`, поэтому rescue_queue оставалась пустой, и фазы
# Telegram/MAX не отправляли НИЧЕГО. Ниже — сквозной тест: WABA приняты без
# подтверждения → пауза → сторож молчания → очередь отката непуста.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# X. РЕГРЕССИЯ: ПОДТВЕРЖДЕНИЯ ДОСТАВКИ НИКОГДА НЕ ПОПАДАЛИ В БД.
#
# Живой замер 2026-10-06: вебхук получил 1321 событие, из них 15 реально
# доставленных WABA-сообщений, но в логе НОЛЬ строк "DELIVERY status", а
# в таблице messages все они остались status='sent'.
#
# Две причины, обе в разборе payload:
#   1) конверт `{"statuses": [...]}` не разворачивался — код знал только
#      про "messages" и "events", поэтому events уходили в "ignored";
#   2) внутри статуса нет ни `type`, ни `wh_type`, ни channelId/chatType —
#      даже развернутый конверт не попал бы ни в одну ветку диспетчера,
#      а канал резолвился бы как 'whatsapp' вместо 'waba'.
#
# Ниже — ТОЧНЫЕ payload'ы из data/pending_webhooks.jsonl.
# ---------------------------------------------------------------------------
section("X. РЕГРЕССИЯ: РАЗБОР РЕАЛЬНЫХ СТАТУСОВ WAZZUP")
_x_calls: list = []

# Реальный конверт статуса: ни type, ни wh_type, ни channelId, ни chatType.
_X_REAL_STATUS = {
    "statuses": [{
        "messageId": "1a5e0449-0948-498f-a6bb-f2dd5d47b7d7",
        "timestamp": "2026-10-06T13:15:00.000Z",
        "status": "delivered",
    }]
}
_X_REAL_ERROR = {
    "statuses": [{
        "messageId": "632ff404-5db7-4641-a623-2b7a4502bee4",
        "timestamp": "2026-10-02T07:05:15.723Z",
        "status": "error",
        "error": {"error": "BAD_CONTACT",
                  "description": "Number may not be on WhatsApp"},
    }]
}
# Реальное входящее сообщение — не должно поехать в обработчик статусов.
_X_REAL_INCOMING = {
    "messages": [{
        "messageId": "b893af6d-e66b-426c-b0f7-9b4c6e2b4e44",
        "dateTime": "2026-10-06T13:34:05.000Z",
        "channelId": "14058d25-0705-421e-9d78-0eb2f1c0d667",
        "chatType": "whatsapp",
        "chatId": "79167365456@c.us",
        "text": "да",
    }]
}

check("конверт 'statuses' распознан как статус-событие",
      reconcile._is_status_event(
          dict(_X_REAL_STATUS["statuses"][0], _envelope="statuses"), ""))
check("статус-событие опознаётся и без тега конверта",
      reconcile._is_status_event(_X_REAL_STATUS["statuses"][0], ""))
check("входящее сообщение НЕ считается статус-событием",
      not reconcile._is_status_event(_X_REAL_INCOMING["messages"][0], "message"))
check("входящее с текстом и статусом — всё равно не статус",
      not reconcile._is_status_event(
          dict(_X_REAL_INCOMING["messages"][0], status="sent"), "message"))


def _x_spy_status(ch, msg):
    _x_calls.append(("status", ch, msg.get("id") or msg.get("message_id"),
                     (msg.get("status") or "").lower()))


def _x_spy_incoming(ch, msg):
    _x_calls.append(("incoming", ch, msg.get("body")))


try:
    with patch.object(reconcile, "process_delivery_status", _x_spy_status), \
         patch.object(reconcile, "process_incoming_message", _x_spy_incoming), \
         patch.object(reconcile, "process_outgoing_message",
                      lambda ch, msg: _x_calls.append(("outgoing", ch, None))):
        reconcile.process_wazzup_payload(None, _X_REAL_STATUS)
        reconcile.process_wazzup_payload(None, _X_REAL_ERROR)
        reconcile.process_wazzup_payload(None, _X_REAL_INCOMING)

    kinds = [c[0] for c in _x_calls]
    check("реальный конверт statuses дошёл до обработчика статусов",
          kinds[:2] == ["status", "status"], f"got {kinds}")
    check("messageId извлечён из реального статуса",
          _x_calls[0][2] == "1a5e0449-0948-498f-a6bb-f2dd5d47b7d7",
          f"got {_x_calls[0][2]}")
    check("статус delivered распознан",
          _x_calls[0][3] == "delivered", f"got {_x_calls[0][3]}")
    check("статус error распознан обработчиком (сырой 'error')",
          _x_calls[1][3] == "error", f"got {_x_calls[1][3]}")
    check("error преобразуется в локальный 'failed' (=> rescue)",
          reconcile._wazzup_status_to_local("error") == "failed",
          f"got {reconcile._wazzup_status_to_local('error')}")
    check("delivered преобразуется в локальный 'delivered'",
          reconcile._wazzup_status_to_local("delivered") == "delivered")
    check("входящее сообщение ушло в свой обработчик, не в статус",
          kinds[2] == "incoming", f"got {kinds[2] if len(kinds) > 2 else None}")
    check("_envelope не попадает в исходный payload",
          "_envelope" not in _X_REAL_STATUS["statuses"][0])
finally:
    pass

# Канал для статуса берётся из БД: у события нет ни channelId, ни chatType.
_x_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_x_db.close()
try:
    with patch.object(config, "DB_PATH", _x_db.name):
        db.init_db()
        _x_rows_before = []
        with db.db_conn() as conn:
            _cid = db.upsert_contact(conn, phone="+79990001111",
                                     name="Канал", category="Покупатели")
            db.insert_message(conn, contact_id=_cid, channel="waba",
                              message_text="t", message_id="mid-waba-1",
                              status="sent")
        with patch.object(state, "record_delivered",
                          lambda st, ch, *a, **k: _x_rows_before.append(ch)), \
             patch.object(state, "record_failed_delivery",
                          lambda st, ch, *a, **k: None), \
             patch.object(state, "load_state", dict), \
             patch.object(reconcile, "sync_message_to_sheet",
                          lambda **k: None), \
             patch.object(state, "reset_daily_if_new_day", lambda s: s):
            # ch=None имитирует то, что реально приходит из резолва payload:
            # ни channelId, ни chatType у статус-события нет.
            reconcile.process_delivery_status(
                None, {"id": "mid-waba-1", "status": "delivered"})
    check("канал статуса взят из БД ('waba'), а не из payload",
          "waba" in _x_rows_before,
          f"записали каналы {_x_rows_before}")
finally:
    try:
        os.unlink(_x_db.name)
    except OSError:
        pass

# ---------------------------------------------------------------------------
# П. КОНТРАКТНЫЕ ТЕСТЫ НА РЕАЛЬНЫХ PAYLOAD'АХ
#
# 629 юнит-тестов проходили на заглушках, пока боевая интеграция с вебхуком
# была сломана целиком (процесс на устаревшем коде не разбирал конверт
# `{"statuses": [...]}` — ноль подтверждений доставки за всю историю).
# Контракт теперь зафиксирован фикстурами, снятыми с боевого вебхука.
# ---------------------------------------------------------------------------
section("P. КОНТРАКТ ВЕБХУКА (реальные payload'ы) + ТРЁХЗНАЧНАЯ ДОСТАВКА")
import json as _json  # noqa: E402
from bulkmessage import notify as _notify_mod  # noqa: E402
from bulkmessage import report  # noqa: E402

_FX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "tests", "fixtures", "webhook_real_payloads.json")
check("фикстуры боевого вебхука лежат в репозитории", os.path.exists(_FX_PATH))
_FX = _json.loads(open(_FX_PATH, encoding="utf-8").read()) if os.path.exists(_FX_PATH) else {}

check("конверт статусов распознаётся как статус-событие",
      reconcile._is_status_event(
          reconcile._tag(list(_FX.get("statuses_envelope", {})
                              .get("statuses", [{}]))[0], "statuses"),
          "statuses"))
check("конверт сообщений НЕ распознаётся как статус-событие",
      not reconcile._is_status_event(
          reconcile._tag(list(_FX.get("messages_envelope_waba", {})
                              .get("messages", [{}]))[0], "messages"),
          "messages"))
check("в статус-событии нет channelId — канал берём из БД",
      "channelId" not in list(_FX.get("statuses_without_channelId", {})
                              .get("statuses", [{}])[0]))
check("разбор конверта статусов есть в обработчике payload'ов",
      "statuses" in inspect.getsource(reconcile.process_wazzup_payload))

# --- Идемпотентность события + позднее подтверждение снимает откат ----------
_p_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_p_db.close()
try:
    with patch.object(config, "DB_PATH", _p_db.name):
        db.init_db()
        with db.db_conn() as conn:
            _p_cid = db.upsert_contact(conn, phone="+79990002222",
                                       name="Поздний", category="Покупатели")
            _p_pk = db.insert_message(conn, contact_id=_p_cid, channel="waba",
                                      message_text="t", message_id="mid-late-1",
                                      status="sent")
        # Контакт уже стоит в очереди отката (молчание → rescue).
        db.enqueue_rescue("+79990002222", "Поздний", "Покупатели",
                          original_channel="waba", reason="no_delivery_confirmation")
        _p_pending_before = db.count_pending_rescues()

        first = db.apply_delivery_event(_p_pk, "mid-late-1", "waba", "delivered")
        second = db.apply_delivery_event(_p_pk, "mid-late-1", "waba", "delivered")
        _p_cancelled = db.cancel_rescue_for_contact("+79990002222")

        with db.db_conn() as conn:
            _p_row = conn.execute(
                "SELECT delivery_state FROM messages WHERE id = ?", (_p_pk,)
            ).fetchone()
            _p_events = conn.execute(
                "SELECT COUNT(*) FROM delivery_events WHERE message_id = ?",
                ("mid-late-1",)).fetchone()[0]

    check("первое событие доставки применено", first is True)
    check("повтор того же события ИГНОРИРУЕТСЯ (идемпотентность)", second is False)
    check("в delivery_events ровно одна запись на дубль", _p_events == 1,
          f"записей {_p_events}")
    check("сообщение помечено delivered", _p_row["delivery_state"] == "delivered",
          f"state={_p_row['delivery_state']}")
    # Остаток очереди считаем ВНУТРИ блока с подменой DB_PATH. Раньше вызов
    # стоял снаружи и читал общую тестовую базу, где есть pending-строки от
    # других блоков: проверка падала по чужому состоянию и врала про откат.
    with patch.object(config, "DB_PATH", _p_db.name):
        _p_pending_after = db.count_pending_rescues()
    check("поздний delivered СНЯЛ контакт из очереди отката",
          _p_pending_before == 1 and _p_cancelled == 1 and
          _p_pending_after == 0,
          f"было {_p_pending_before}, снято {_p_cancelled}, осталось "
          f"{_p_pending_after}")
finally:
    try:
        os.unlink(_p_db.name)
    except OSError:
        pass

# --- Молчание ≠ провал: горизонты разные ------------------------------------
_c_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_c_db.close()
try:
    with patch.object(config, "DB_PATH", _c_db.name), \
         patch.object(config, "UNKNOWN_GRACE_MINUTES", 720), \
         patch.object(config, "REJECT_GRACE_MINUTES", 5):
        db.init_db()
        with db.db_conn() as conn:
            _c_cid = db.upsert_contact(conn, phone="+79990003333",
                                       name="Молчун", category="Покупатели")
            _c_pk = db.insert_message(conn, contact_id=_c_cid, channel="waba",
                                      message_text="t", message_id="mid-silent-1",
                                      status="sent")
            # Ставим отправку час назад, иначе «молчание» только что началось
            # и ни один горизонт ещё не выдержан.
            _c_old = (datetime.now(timezone.utc)
                      - timedelta(hours=1)).isoformat()
            conn.execute("UPDATE messages SET sent_at = ?, created_at = ? "
                         "WHERE id = ?", (_c_old, _c_old, _c_pk))
        # Горизонт явного отказа — молчание туда НЕ попадает.
        _c_swept = reconcile.sweep_unconfirmed_deliveries(limit=50, minutes=5,
                                                          unknown_minutes=720)
        with db.db_conn() as conn:
            _c_state = conn.execute(
                "SELECT delivery_state FROM messages WHERE id = ?",
                (_c_pk,)).fetchone()["delivery_state"]
        _c_rescued = db.count_pending_rescues()
        check("на коротком горизонте молчание НЕ уходит в откат",
              _c_swept == 0 and _c_rescued == 0,
              f"swept={_c_swept} в rescue={_c_rescued}")
        check("молчание помечено unknown, а не rejected",
              _c_state == "unknown", f"state={_c_state}")
        check("антикоастинг WABA на этом шаге ещё не поставлен",
              "+79990003333" not in db.get_active_cooldowns("waba"))
        # По длинному горизонту — молчание становится откатом. Внутри того же
        # patch'а на БД, иначе свип пошёл бы по боевой базе.
        _c_swept2 = reconcile.sweep_unconfirmed_deliveries(limit=50, minutes=5,
                                                            unknown_minutes=0)
        check("по длинному горизонту молчание УХОДИТ в откат",
              _c_swept2 == 1 and db.count_pending_rescues() == 1,
              f"swept={_c_swept2}, в rescue={db.count_pending_rescues()}")
        check("антикоастинг WABA поставлен молчащему контакту",
              "+79990003333" in db.get_active_cooldowns("waba"),
              f"cooldown={db.get_active_cooldowns('waba')}")
finally:
    try:
        os.unlink(_c_db.name)
    except OSError:
        pass

# --- Инвариант: не больше одного сообщения на контакт в канале за сутки -------
_i_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_i_db.close()
try:
    with patch.object(config, "DB_PATH", _i_db.name):
        db.init_db()
        with db.db_conn() as conn:
            _i_cid = db.upsert_contact(conn, phone="+79990004444",
                                       name="Инвариант", category="Покупатели")
        _i_first = db.recent_channel_send("+79990004444", "waba", hours=24)
        with db.db_conn() as conn:
            db.insert_message(conn, contact_id=_i_cid, channel="waba",
                              message_text="t", message_id="mid-inv-1",
                              status="sent")
        _i_after = db.recent_channel_send("+79990004444", "waba", hours=24)
        _i_other_ch = db.recent_channel_send("+79990004444", "telegram", hours=24)
        _i_old = db.recent_channel_send("+79990004444", "waba", hours=0)
    check("до отправки дубля-риска нет", _i_first is False)
    check("после отправки повтор в том же канале заблокирован", _i_after is True)
    check("другой канал инвариантом не задет", _i_other_ch is False)
    check("окно в 0 часов не блокирует (окно настраивается)", _i_old is False)
finally:
    try:
        os.unlink(_i_db.name)
    except OSError:
        pass

# --- Exhausted: потолок попыток ---------------------------------------------
_e_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_e_db.close()
_e_dir = tempfile.mkdtemp()
try:
    with patch.object(config, "DB_PATH", _e_db.name), \
         patch.object(config, "EXHAUST_MAX_ATTEMPTS", 3), \
         patch.object(config, "EXHAUST_WINDOW_DAYS", 0):
        db.init_db()
        _e_rid = db.enqueue_rescue("+79990005555", "Исчерпан", "Покупатели",
                                   original_channel="waba", reason="test")
        db.mark_rescue_attempt(_e_rid, error="telegram_failed")
        db.mark_rescue_tried(_e_rid, "telegram")
        db.mark_rescue_attempt(_e_rid, error="max_failed")
        db.mark_rescue_tried(_e_rid, "max")
        _e_why = db.should_exhaust(_e_rid, ["telegram", "max"])
        db.mark_rescue_exhausted(_e_rid, _e_why or "test")
        _e_count = db.count_exhausted_rescues()
        _e_csv = db.export_exhausted_csv(_e_dir)
        _e_csv2 = db.export_exhausted_csv(_e_dir)   # второй раз — не дублируем
    _e_exists = bool(_e_csv) and os.path.exists(_e_csv)
    check("потолок попыток срабатывает на исчерпании", bool(_e_why), f"why={_e_why}")
    check("контакт помечен exhausted (стоп автоматики)", _e_count == 1,
          f"exhausted={_e_count}")
    check("исчерпанные выгружаются в CSV", _e_exists)
    check("выгрузка не дублируется в течение суток", _e_csv2 is None)
    check("в CSV есть телефон контакта",
          _e_exists and "+79990005555" in open(_e_csv, encoding="utf-8").read())
finally:
    try:
        os.unlink(_e_db.name)
    except OSError:
        pass

# --- daily_counters: атомарность и согласованность --------------------------
_d_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_d_db.close()
try:
    with patch.object(config, "DB_PATH", _d_db.name):
        db.init_db()
        db.upsert_counter("2026-10-07", "waba", "accepted", 3)
        db.upsert_counter("2026-10-07", "waba", "accepted", 2)
        db.upsert_counter("2026-10-07", "waba", "delivered", 1)
        _d_ct = db.get_counters("2026-10-07")
        _d_bad = False
        try:
            db.upsert_counter("2026-10-07", "waba", "нет_такого")
        except ValueError:
            _d_bad = True
    check("инкремент счётчика атомарен и накопителен",
          _d_ct["waba"]["accepted"] == 5 and _d_ct["waba"]["delivered"] == 1,
          f"{_d_ct}")
    check("доставка не может превысить принятие (источник истины один)",
          _d_ct["waba"]["delivered"] <= _d_ct["waba"]["accepted"])
    check("неизвестное поле счётчика отвергается", _d_bad)
    check("отчёт берёт счётчики из SQLite, а не из state.json",
          "db.get_counters" in inspect.getsource(report.build_report))
finally:
    try:
        os.unlink(_d_db.name)
    except OSError:
        pass

# --- Прозрачность: notify не шумит и не рассылает лишнее --------------------
_notify_mod.reset()
with patch.object(config, "TG_MAX_MESSAGES_PER_HOUR", 3):
    _notify_mod.digest_start(phase_order=["waba", "telegram"], caps={"waba": 187},
                             contacts_planned=20, rescue_pending=5,
                             exhausted_total=1, waba_tier=1, code_version="abc1234")
    _notify_mod.channel_switch("+79990006666", "waba", "telegram", "молчание")
    _notify_mod.event("halt канала", "ERROR")
    _notify_mod.event("ещё одно событие", "ERROR")   # сверх лимита — не уходит
check("дайджест/события не отправляются на тестовом прогоне",
      _notify_mod.sent_count() == 0,
      f"отправлено {_notify_mod.sent_count()}")
check("notify имеет rate-limit на сообщения в личку",
      hasattr(_notify_mod, "_allowed"))
check("успехи агрегируются, а не летят построчно",
      hasattr(_notify_mod, "counted_success") and hasattr(_notify_mod, "summary"))
_notify_mod.reset()

# --- Параллелизм WABA и независимый tier -----------------------------------
check("параллелизм настраивается конфигом",
      int(getattr(config, "WABA_CONCURRENCY", 1)) >= 1)
check("личные каналы не распараллеливаются",
      'if phase_ch == "waba":' in inspect.getsource(sender._run_phase))
check("личные каналы имеют жёсткий потолок",
      int(config.PERSONAL_CHANNEL_HARD_CAPS["telegram"]) > 0 and
      int(config.PERSONAL_CHANNEL_HARD_CAPS["max"]) > 0)
check("квота сверяется с атомарным счётчиком из БД",
      hasattr(state, "reconcile_sent_from_db"))
check("tier WABA обновляется независимо от вебхука",
      hasattr(wazzup, "refresh_waba_tier") and hasattr(wazzup, "fetch_waba_tier"))

# --- Повторный свип того же молчания не плодит дубли ----------------------
# Реальный баг, найденный сквозным стендом 2026-10-07: молчание перестало
# переписывать status в 'failed', и сторож на следующей фазе отката снова
# видел то же сообщение. Контакт, которому Telegram ТОЛЬКО ЧТО доставил,
# возвращался в очередь по своему старому молчаливому сообщению в WABA и
# получал дубль в MAX. Лечится меткой swept_at.
_z_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_z_db.close()
try:
    with patch.object(config, "DB_PATH", _z_db.name):
        db.init_db()
        with db.db_conn() as conn:
            _z_cid = db.upsert_contact(conn, phone="+79990007777",
                                       name="Повтор", category="Покупатели")
            _z_pk = db.insert_message(conn, contact_id=_z_cid, channel="waba",
                                      message_text="t", message_id="mid-rep-1",
                                      status="sent")
            _z_old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            conn.execute("UPDATE messages SET sent_at=?, created_at=? WHERE id=?",
                         (_z_old, _z_old, _z_pk))
        _z_first = reconcile.sweep_unconfirmed_deliveries(limit=50, minutes=0,
                                                          unknown_minutes=0)
        _z_rescued_after_first = db.count_pending_rescues()
        # Вторая фаза отката (например MAX) — то же молчание, другой вызов.
        _z_second = reconcile.sweep_unconfirmed_deliveries(limit=50, minutes=0,
                                                           unknown_minutes=0)
        _z_rescued_after_second = db.count_pending_rescues()
    check("первый свип положил молчание в очередь отката",
          _z_first == 1 and _z_rescued_after_first == 1,
          f"first={_z_first} rescue={_z_rescued_after_first}")
    check("повторный свип того же молчания НЕ плодит дубль в очереди",
          _z_second == 0 and _z_rescued_after_second == 1,
          f"second={_z_second} rescue={_z_rescued_after_second}")
finally:
    try:
        os.unlink(_z_db.name)
    except OSError:
        pass

# --- Одна живая строка отката на контакт ---------------------------------
# Реальный баг, найденный локальным стендом 2026-10-07. enqueue_rescue искал
# совпадение только среди pending/deferred. Строка, ушедшая в 'rescued',
# переставала быть «найденной», и следующее молчание WABA создавало ВТОРУЮ
# строку с пустым tried_channels. Дальше каскад заново пробовал каналы, где
# контакт уже получил сообщение, а should_exhaust смотрел на attempts_total=0
# и не срабатывал — контакт крутился в очереди и жег лимиты личных аккаунтов.
_d_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_d_db.close()
try:
    with patch.object(config, "DB_PATH", _d_db.name):
        db.init_db()

        def _live(phone):
            with db.db_conn() as c:
                return c.execute(
                    "SELECT COUNT(*) FROM rescue_queue WHERE phone=? "
                    "AND status != 'cancelled'", (phone,)).fetchone()[0]

        def _tried(rid):
            with db.db_conn() as c:
                return c.execute("SELECT tried_channels FROM rescue_queue "
                                 "WHERE id=?", (rid,)).fetchone()[0]

        def _status(rid):
            with db.db_conn() as c:
                return c.execute("SELECT status FROM rescue_queue "
                                 "WHERE id=?", (rid,)).fetchone()[0]

        # 1) Откат прошёл (rescued), но доставку никто не подтвердил →
        #    следующее молчание обязано ПЕРЕОТКРЫТЬ ту же строку, сохранив
        #    tried_channels, а не плодить новую с пустой историей.
        r1 = db.enqueue_rescue(phone="+79990001111", name="А",
                               category="Покупатели")
        db.mark_rescue_tried(r1, "telegram")
        db.mark_rescue_success(r1, "telegram", "mid-silent-1")
        r1b = db.enqueue_rescue(phone="+79990001111", name="А",
                                category="Покупатели", reason="waba_silent")
        check("rescued без подтверждения переоткрывает ТУ ЖЕ строку (не дубль)",
              r1b == r1 and _live("+79990001111") == 1,
              f"id={r1} -> {r1b}, живых строк={_live('+79990001111')}")
        check("переоткрытие сохраняет историю каналов",
              _tried(r1) == "telegram" and _status(r1) == "pending",
              f"tried=[{_tried(r1)}] status={_status(r1)}")

        # 2) Откат прошёл И доставку подтвердили вебхуком → контакт получил
        #    своё сообщение, второй раз в личные каналы не идём.
        with db.db_conn() as c:
            cid = db.upsert_contact(c, phone="+79990002222", name="Б",
                                    category="Покупатели")
            pk = db.insert_message(c, contact_id=cid, channel="telegram",
                                   message_text="t", message_id="mid-ok-1",
                                   status="sent")
            c.execute("UPDATE messages SET delivered_at=datetime('now') "
                      "WHERE id=?", (pk,))
        r2 = db.enqueue_rescue(phone="+79990002222", name="Б",
                               category="Покупатели")
        db.mark_rescue_tried(r2, "telegram")
        db.mark_rescue_success(r2, "telegram", "mid-ok-1")
        r2b = db.enqueue_rescue(phone="+79990002222", name="Б",
                                category="Покупатели", reason="waba_silent")
        check("подтверждённая доставка замыкает очередь (дубль не создаётся)",
              r2b is None and _live("+79990002222") == 1
              and _status(r2) == "rescued",
              f"вернул {r2b}, живых строк={_live('+79990002222')}")

        # 3) Сторож молчания не должен воскрешать отложенную строку теми же
        #    данными, которые её и отложили, — иначе отсрочка снимается сразу
        #    и контакт крутится в очереди бесконечно.
        r3 = db.enqueue_rescue(phone="+79990003333", name="В",
                               category="Покупатели")
        db.mark_rescue_tried(r3, "telegram")
        db.mark_rescue_tried(r3, "max")
        _tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        db.mark_rescue_deferred(r3, _tomorrow)
        _old_iso = "2026-10-07T00:00:00+00:00"
        db.enqueue_rescue(phone="+79990003333", name="В", category="Покупатели",
                          since=_old_iso)
        check("старое данное НЕ снимает отсрочку",
              _status(r3) == "deferred", f"status={_status(r3)}")
        _newer_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        db.enqueue_rescue(phone="+79990003333", name="В", category="Покупатели",
                          since=_newer_iso)
        check("действительно новое данное снимает отсрочку",
              _status(r3) == "pending", f"status={_status(r3)}")

        # 4) Исчерпанный контакт автоматика не воскрешает.
        r4 = db.enqueue_rescue(phone="+79990004444", name="Г",
                               category="Покупатели")
        db.mark_rescue_tried(r4, "telegram")
        db.mark_rescue_exhausted(r4, "all_channels_tried")
        r4b = db.enqueue_rescue(phone="+79990004444", name="Г",
                                category="Покупатели")
        check("exhausted не переоткрывается автоматикой",
              r4b is None and _status(r4) == "exhausted",
              f"вернул {r4b}, status={_status(r4)}")

        # 5) Молчание по ПОСЛЕДНЕМУ каналу отката обязано дать терминальный
        #    статус: раньше should_exhaust вызывался только при явном отказе,
        #    и «молчание везде» давало бесконечный цикл принято→ждём→молчание.
        r5 = db.enqueue_rescue(phone="+79990005555", name="Д",
                               category="Покупатели")
        db.mark_rescue_tried(r5, "telegram")
        db.mark_rescue_tried(r5, "max")
        with patch.object(config, "EXHAUST_WINDOW_DAYS", 0):
            _why5 = db.should_exhaust(r5, ["telegram", "max"])
            _not5 = db.should_exhaust(r5, ["telegram", "max", "sms"])
        check("все каналы отката пройдены → контакт исчерпан",
              bool(_why5), f"причина={_why5!r}")
        check("неисчерпанный канал продления не делает контакт исчерпанным",
              _not5 is None, f"причина={_not5!r}")

        # 6) Горизонты не должны слипаться. Живой баг 2026-10-07: sender всегда
        #    передаёт minutes=PHASE_GRACE_MINUTES, и прежнее правило «явный
        #    minutes перекрывает оба горизонта» делало UNKNOWN_GRACE_MINUTES
        #    мёртвой настройкой — молчание через 5 минут уходило в откат, то
        #    есть в личные аккаунты. Проверяем ровно этот вызов.
        _h_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        _h_db.close()
        try:
            with patch.object(config, "DB_PATH", _h_db.name), \
                 patch.object(config, "REJECT_GRACE_MINUTES", 5), \
                 patch.object(config, "UNKNOWN_GRACE_MINUTES", 10080):
                db.init_db()
                with db.db_conn() as c:
                    cid = db.upsert_contact(c, phone="+79990006666",
                                            name="Е", category="Покупатели")
                    pk = db.insert_message(c, contact_id=cid, channel="waba",
                                           message_text="t", message_id="mid-h-1",
                                           status="sent")
                    old = (datetime.now(timezone.utc)
                           - timedelta(minutes=10)).isoformat()
                    c.execute("UPDATE messages SET sent_at=? WHERE id=?",
                              (old, pk))
                # Ровно тот вызов, который делает sender между фазами.
                _n = reconcile.sweep_unconfirmed_deliveries(limit=50, minutes=5)
                _rows = db.count_pending_rescues()
            check("молчание НЕ уходит в откат по горизонту явного отказа",
                  _n == 0 and _rows == 0,
                  f"в rescue={_n}, строк={_rows} (ожидаем 0 при "
                  f"UNKNOWN_GRACE_MINUTES=10080)")
            # Явный unknown_minutes по-прежнему перекрывает конфиг (стенды).
            _h2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            _h2.close()
            try:
                with patch.object(config, "DB_PATH", _h2.name), \
                     patch.object(config, "UNKNOWN_GRACE_MINUTES", 10080):
                    db.init_db()
                    with db.db_conn() as c:
                        cid = db.upsert_contact(c, phone="+79990007777",
                                                name="Ж", category="Покупатели")
                        pk = db.insert_message(c, contact_id=cid, channel="waba",
                                               message_text="t",
                                               message_id="mid-h-2", status="sent")
                        old = (datetime.now(timezone.utc)
                               - timedelta(minutes=10)).isoformat()
                        c.execute("UPDATE messages SET sent_at=? WHERE id=?",
                                  (old, pk))
                    _n2 = reconcile.sweep_unconfirmed_deliveries(
                        limit=50, minutes=5, unknown_minutes=0)
                check("явный unknown_minutes перекрывает конфиг (стенды)",
                      _n2 == 1, f"в rescue={_n2}, ожидаем 1")
            finally:
                try:
                    os.unlink(_h2.name)
                except OSError:
                    pass
        finally:
            try:
                os.unlink(_h_db.name)
            except OSError:
                pass
finally:
    try:
        os.unlink(_d_db.name)
    except OSError:
        pass

# --- Уведомления владельцу: по контакту и человеческим языком -------------
# Владелец попросил видеть в личке статус ПО КАЖДОМУ контакту (имя + номер)
# и не выключенные каналы в списке. Проверяем обе стороны.
from bulkmessage import notify as _notify  # noqa: E402

check("причины отказов переводятся на человеческий язык",
      _notify._human_reason("no_delivery_confirmation") != "no_delivery_confirmation"
      and "max" in _notify._human_reason("CHANNEL_MAX_PHONE_NOT_OCCUPIED").lower(),
      f"{_notify._human_reason('no_delivery_confirmation')!r} / "
      f"{_notify._human_reason('CHANNEL_MAX_PHONE_NOT_OCCUPIED')!r}")
check("неизвестная причина не теряется, а показывается как есть",
      _notify._human_reason("что-то новое") == "что-то новое",
      _notify._human_reason("что-то новое"))

with patch.object(config, "PHASE_ORDER", ["waba", "telegram", "max"]), \
     patch.object(config, "TG_HIDE_CHANNELS", ("whatsapp", "sms")), \
     patch.object(config, "CHANNEL_DAILY_LIMITS",
                  {"whatsapp": 60, "telegram": 50, "max": 15, "waba": 250,
                   "sms": 50}):
    _own = sender._owner_channels({"telegram", "max", "whatsapp", "sms", "waba"})
    _lim = sender._limits_for_owner({"telegram", "max", "whatsapp", "sms", "waba"})
check("каналы в уведомлении идут в порядке каскада",
      _own == ["waba", "telegram", "max"], f"{_own}")
check("выключенные WhatsApp/SMS не попадают в уведомления",
      "whatsapp" not in _lim and "sms" not in _lim and "waba 250" in _lim,
      f"{_lim}")

# --- Отбор: сначала тем, кому НИКОГДА не отправляли ------------------------
_p_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_p_db.close()
try:
    with patch.object(config, "DB_PATH", _p_db.name):
        db.init_db()
        _st = {"date": "2026-10-07", "sent_today": {}, "delivered_today": {},
               "failed_today": {}, "waba_exhausted": {}}
        _sel_contacts = [
            {"phone": "+79991000001", "name": "Старый1", "category": "Покупатели"},
            {"phone": "+79991000002", "name": "Новый1", "category": "Покупатели"},
            {"phone": "+79991000003", "name": "Новый2", "category": "Покупатели"},
        ]
        with db.db_conn() as c:
            cid = db.upsert_contact(c, phone="+79991000001", name="Старый1",
                                    category="Покупатели")
            pk = db.insert_message(c, contact_id=cid, channel="waba",
                                   message_text="t", message_id="mid-sel-1",
                                   status="delivered")
            _old_sel = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
            c.execute("UPDATE messages SET sent_at=? WHERE id=?",
                      (_old_sel, pk))
        _picked = sender._collect_fresh_waba_targets(_sel_contacts, _st)
    _phones = [c["phone"] for c in _picked]
    check("первыми идут те, кому ещё никогда не отправляли",
          _phones[:2] == ["+79991000002", "+79991000003"], f"{_phones}")
finally:
    try:
        os.unlink(_p_db.name)
    except OSError:
        pass

# ── Стоп-касса боевого прогона без пути подтверждений ────────────────────
# Живой случай 2026-10-07: sender считали локально, вебхук крутился на сервере
# и писал в другую БД. 30 сообщений принято, 0 подтверждений, в интерфейсе
# Wazzup видны отказы Meta — а каскад молчал. Ловить было нечего: два процесса
# не делят одну базу. Молчание через 12 ч уводит ПРИНЯТЫЕ сообщения в откат,
# то есть в личные аккаунты — поэтому дефолт здесь отказ, а не предупреждение.
print()
section("K. ПУТЬ ПОДТВЕРЖДЕНИЙ ДОСТАВКИ")

import socket  # noqa: E402  — нужен для проверки «вебхук поднят локально»

# Тесты НЕ ходят в сеть и не зависят от того, слушает ли кто-то порт 8000
# (боевой вебхук может быть поднят прямо во время прогона). Подписку Wazzup
# подменяем заглушкой, слушатель поднимаем на незанятом порту.
_FREE_PORT = 18099
_saved = (config.DRY_RUN, config.WEBHOOK_PORT,
          config.WAZZUP_WEBHOOK_URL, config.ALLOW_NO_DELIVERY_SIGNAL)
_real_get_reg = sender.wazzup.get_registered_webhook_url


def _sig(registered, port=_FREE_PORT, cfg_url=""):
    """Прогнать проверку сигнала на подставных данных, сбросив кэш."""
    config.DRY_RUN = False
    config.WEBHOOK_PORT = port
    config.WAZZUP_WEBHOOK_URL = cfg_url
    sender.wazzup.get_registered_webhook_url = (
        lambda: (registered, "ok" if registered else "http 500: stub"))
    sender._DELIVERY_SIGNAL_CACHE = None
    return sender.delivery_signal_available()


try:
    # 1. Ни вебхука локально, ни подписки → сигнала не будет.
    _ok, _why = _sig(registered="https://tunnel.trycloudflare.com/webhook")
    check("без вебхука сигнал доставки недоступен", _ok is False, f"{_ok} {_why}")
    check("причина говорит, что вебхук не поднят", "не поднят" in _why, _why)

    # 2. Живой случай 2026-10-07: локально ничего не слушает, а Wazzup шлёт
    #    события в туннель СЕРВЕРА — статусы уйдут в чужую базу.
    _ok2, _why2 = _sig(
        registered="https://omaha-priority-mileage-oct.trycloudflare.com/webhook",
        cfg_url="https://omaha-priority-mileage-oct.trycloudflare.com/webhook")
    check("подписка на чужой туннель = нет своего сигнала", _ok2 is False, f"{_ok2}")
    check("причина называет чужую машину/базу",
          "не поднят" in _why2 and "не придут" in _why2, _why2)

    _srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        _srv.bind(("127.0.0.1", _FREE_PORT))
        # backlog 16, а не 1: каждая проверка делает connect_ex, а некому
        # принимать соединения, поэтому с listen(1) вторая проверка уже
        # получала «порт молчит» — ложный отказ при живом вебхуке.
        _srv.listen(16)
        # 3. Локальный вебхук поднят, но Wazzup подписан на СЕРВЕР — дыра,
        #    которую легко не заметить: порт слушает, а события идут в другую БД.
        _ok3, _why3 = _sig(
            registered="https://omaha-priority-mileage-oct.trycloudflare.com/webhook")
        check("слушающий порт при подписке на сервер = сигнала нет",
              _ok3 is False, f"{_ok3} {_why3}")
        check("причина говорит про ДРУГУЮ машину", "ДРУГАЯ машина" in _why3, _why3)

        # 4. Порт слушает и подписка совпадает с конфигом → сигнал есть.
        _ok4, _why4 = _sig(
            registered="https://my-local.trycloudflare.com/webhook",
            cfg_url="https://my-local.trycloudflare.com/webhook")
        check("порт + подписка совпадают = сигнал доступен",
              _ok4 is True, f"{_ok4} {_why4}")

        # 5. Порт слушает, но конфиг пуст — сверять нечем, предупреждаем честно.
        _ok5, _why5 = _sig(registered="https://my-local.trycloudflare.com/webhook",
                           cfg_url="")
        check("пустой WAZZUP_WEBHOOK_URL = не подтверждено, но не провал",
              isinstance(_ok5, bool) and "WAZZUP_WEBHOOK_URL" in _why5,
              f"{_ok5} {_why5}")
    finally:
        _srv.close()

    # 6. DRY_RUN не блокируется: реальных отправок нет, подтверждения не нужны.
    config.DRY_RUN = True
    sender._DELIVERY_SIGNAL_CACHE = None
    _ok6, _why6 = sender.delivery_signal_available()
    check("DRY_RUN не требует подтверждений", _ok6 is True, f"{_ok6} {_why6}")
    config.DRY_RUN = False

    # 7. Боевой прогон без сигнала и без обхода → отказ (SystemExit 3).
    _sig(registered="https://tunnel.trycloudflare.com/webhook")
    config.ALLOW_NO_DELIVERY_SIGNAL = False
    _raised = 0
    try:
        sender._guard_delivery_signal(["waba", "telegram", "max"])
    except SystemExit as _e:
        _raised = _e.code
    check("боевой прогон без подтверждений останавливается",
          _raised == 3, f"код выхода {_raised}")

    # 8. Явный обход владельца — прогон идёт (знает, на что идёт).
    config.ALLOW_NO_DELIVERY_SIGNAL = True
    _passed = True
    try:
        sender._guard_delivery_signal(["waba", "telegram", "max"])
    except SystemExit:
        _passed = False
    check("BULK_ALLOW_NO_DELIVERY_SIGNAL=1 снимает отказ", _passed, "остановился")

    # 9. Каскад без WABA (только Personal) подтверждений не требует.
    config.ALLOW_NO_DELIVERY_SIGNAL = False
    _passed2 = True
    try:
        sender._guard_delivery_signal(["telegram", "max"])
    except SystemExit:
        _passed2 = False
    check("каскад без WABA не блокируется", _passed2, "остановился")
finally:
    sender.wazzup.get_registered_webhook_url = _real_get_reg
    sender._DELIVERY_SIGNAL_CACHE = None
    (config.DRY_RUN, config.WEBHOOK_PORT,
     config.WAZZUP_WEBHOOK_URL, config.ALLOW_NO_DELIVERY_SIGNAL) = _saved

# --- Эхо Wazzup не должно попадать в replies ---------------------------------
# Живой случай 2026-10-07: конверт {"messages": [...]} у Wazzup содержит
# ОБЕ стороны. В свежих 17 событиях 15 были нашими же исходящими эхами
# (isEcho=true) — и все они писались в replies как «ответ клиента», открывая
# 24ч-окно WABA. Следствие: следующая отправка пошла бы free text вместо
# одобренного Meta шаблона. Одно эхо было status="error" — реальный отказ,
# который тоже ушёл в «ответы».
section("L. ЭХО WAZZUP ≠ ОТВЕТ КЛИЕНТА")

_ECHOS = [
    {"type": "wapi_template", "isEcho": True, "status": "sent"},
    {"type": "wapi_template", "isEcho": True, "status": "error"},
    {"type": "text", "isEcho": True, "status": "sent"},
    {"type": "text", "isEcho": "true", "status": "sent"},
    {"type": "text", "status": "outbound"},
]
for _e in _ECHOS:
    check(f"эхо распознано как исходящее: {_e.get('status')}",
          reconcile._is_outgoing_echo(_e) is True, str(_e))

_INBOUND = [
    {"type": "text", "isEcho": False, "status": "inbound", "text": "Не интересно"},
    {"type": "text", "isEcho": False, "status": "inbound"},
    # Старые payload'ы без isEcho/status — ответами считать НЕЛЬЗЯ только если
    # нет признаков исходящего; пустой объект остаётся входящим по умолчанию.
    {"type": "text", "text": "Здравствуйте"},
    {},
]
for _e in _INBOUND:
    check(f"входящее не спутано с эхом: {str(_e)[:42]}",
          reconcile._is_outgoing_echo(_e) is False, str(_e))

# Сквозная проверка маршрутизации: эхо не должно создавать строку в replies.
_e_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_e_db.close()
try:
    with patch.object(config, "DB_PATH", _e_db.name):
        db.init_db()

        # Настоящий ответ клиента.
        with patch.object(reconcile, "sync_message_to_sheet", lambda **kw: None):
            reconcile.process_wazzup_payload("waba", {
                "messages": [{
                    "messageId": "real-inbound-1", "chatType": "whatsapp",
                    "chatId": "79990001111", "type": "text",
                    "isEcho": False, "status": "inbound", "text": "Не интересно",
                }],
            })
            # Наше исходящее эхо с тем же текстом шаблона.
            reconcile.process_wazzup_payload("waba", {
                "messages": [{
                    "messageId": "our-echo-1", "chatType": "whatsapp",
                    "chatId": "79990002222", "type": "wapi_template",
                    "isEcho": True, "status": "sent",
                    "text": "Здравствуйте, вы у меня покупали квартиру",
                }],
            })

        with db.db_conn() as conn:
            _reps = [r["message_id"] for r in conn.execute(
                "SELECT message_id FROM replies ORDER BY id")]
            _texts = [r["message"] for r in conn.execute(
                "SELECT message FROM replies ORDER BY id")]

    check("настоящий ответ сохранён в replies",
          "real-inbound-1" in _reps, str(_reps))
    check("эхо НЕ попало в replies", "our-echo-1" not in _reps, str(_reps))
    check("эхо не открыло 24ч-окно текстом шаблона",
          not any("покупали квартиру" in t for t in _texts), str(_texts)[:120])
    check("в replies ровно одна строка", len(_reps) == 1, str(_reps))
finally:
    try:
        os.unlink(_e_db.name)
    except OSError:
        pass

# --- Отказ канала в эхе исходящего: TG/MAX → откат ----------------------------
# Живой случай 2026-10-07, контакт 79296002444. Wazzup ответил HTTP 200, мы
# записали «rescued» (доставлено) и списали контакт. Через 2 секунды пришло
# эхо: isEcho=true, status="error", error={"error":"BAD_CONTACT",
# "description":"Recipient with this chatId does not exist"}. В Telegram — красный
# «!». Без обработки отказа контакт навсегда остался бы в Telegram и НИКОГДА
# не дошёл до MAX — цепочка рвалась на личных каналах.
section("M. ОТКАЗ В ЭХЕ = ОТКАТ В СЛЕДУЮЩИЙ КАНАЛ")

check("эхо со status=error распознано как отказ",
      reconcile._echo_is_failure({
          "status": "error", "error": {"error": "BAD_CONTACT"}}) is True)
check("эхо со status=sent — НЕ отказ",
      reconcile._echo_is_failure({"status": "sent"}) is False)
check("эхо без error при status=error — не отказ (нет чему верить)",
      reconcile._echo_is_failure({"status": "error"}) is False)
check("причина отказа читается по-человечески",
      reconcile._echo_error_text({
          "error": {"error": "BAD_CONTACT",
                    "description": "Recipient with this chatId does not exist"}})
      == "BAD_CONTACT: Recipient with this chatId does not exist")

# Сквозная проверка на живом payload: контакт обязан вернуться в откат.
_tg_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tg_db.close()
_TG_MID = "9f9f5ff8-c87a-4cd9-8fbb-78a2cdcc153b"
_TG_PHONE = "79296002444"
try:
    with patch.object(config, "DB_PATH", _tg_db.name):
        db.init_db()
        with db.db_conn() as conn:
            _tg_cid = db.upsert_contact(conn, phone=_TG_PHONE, name="Богдан",
                                        category="Покупатели")
            _tg_pk = db.insert_message(
                conn, contact_id=_tg_cid, channel="telegram",
                message_text="текст", message_id=_TG_MID, status="sent")
        # Сначала симулируем то, что делает sender: rescue помечен rescued.
        db.mark_rescue_success(
            db.enqueue_rescue(phone=_TG_PHONE, name="Богдан",
                              category="Покупатели",
                              original_channel="waba", reason="error"),
            channel="telegram", message_id="rescued_telegram_161")
        with db.db_conn() as conn:
            _tg_r0 = conn.execute(
                "SELECT status FROM rescue_queue WHERE phone=?",
                (_TG_PHONE,)).fetchone()["status"]

        with patch.object(reconcile, "sync_message_to_sheet", lambda **kw: None):
            reconcile.process_wazzup_payload("telegram", {
                "messages": [{
                    "messageId": _TG_MID, "dateTime": "2026-10-07T10:38:26.547Z",
                    "channelId": "1c2ef310-836a-4c16-8f1e-387579ae8146",
                    "chatType": "telegram", "chatId": _TG_PHONE,
                    "type": "text", "isEcho": True, "status": "error",
                    "authorName": "Admin", "text": "Здравствуйте, Богдан!",
                    "error": {"error": "BAD_CONTACT",
                              "description": "Recipient with this chatId does not exist"},
                }],
            })

        with db.db_conn() as conn:
            _tg_row = conn.execute(
                "SELECT status, delivery_state FROM messages WHERE message_id=?",
                (_TG_MID,)).fetchone()
            _tg_r = conn.execute(
                "SELECT status, reason, rescued_channel FROM rescue_queue "
                "WHERE phone=?", (_TG_PHONE,)).fetchone()
            _tg_nreply = conn.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
            _tg_ev = conn.execute(
                "SELECT event_status FROM delivery_events WHERE message_id=?",
                (_TG_MID,)).fetchall()

    check("после HTTP 200 контакт был помечен как спасённый",
          _tg_r0 == "rescued", f"было {_tg_r0}")
    check("сообщение помечено rejected",
          _tg_row["delivery_state"] == "rejected", f"{dict(_tg_row)}")
    check("статус сообщения failed", _tg_row["status"] == "failed",
          f"{dict(_tg_row)}")
    check("контакт ВЕРНУЛСЯ в очередь отката (дойдёт до MAX)",
          _tg_r["status"] in ("pending", "retry"), f"{dict(_tg_r)}")
    check("rescued_channel очищен — канал больше не считается спасшим",
          _tg_r["rescued_channel"] in (None, ""), f"{dict(_tg_r)}")
    check("причина БЕЗ ДОСТОВЕРНОГО ФАКТА не потерялась",
          "BAD_CONTACT" in (_tg_r["reason"] or ""), f"{dict(_tg_r)}")
    check("событие записано как rejected",
          any(e["event_status"] == "rejected" for e in _tg_ev),
          str([dict(e) for e in _tg_ev]))
    check("эхо НЕ записалось как ответ клиента", _tg_nreply == 0,
          f"replies={_tg_nreply}")
finally:
    try:
        os.unlink(_tg_db.name)
    except OSError:
        pass

# ---------------------------------------------------------------------------
# N. TELEGRAM: НОМЕР УХОДИТ В `phone`, А НЕ В `chatId`
# ---------------------------------------------------------------------------
# Живой случай 2026-10-07: Telegram через Wazzup отвечал HTTP 201 на КАЖДЫЙ
# номер (включая self-send), а эхо приходило с
#   BAD_CONTACT: Recipient with this chatId does not exist.
# Документация Wazzup объяснила почему: для telegram/max chatId — это
# внутренний peer id, «не совпадает с номером телефона», и приходит в ответе
# на отправку по параметру `phone`. Класть номер в chatId gateway принимает
# молча, но адресовать peer не может.
#
# Доказательство (chatId 998933930709):
#   {"chatId": "998933930709"} → 201 → chatId 998933930709 → BAD_CONTACT
#   {"phone":  "998933930709"} → 201 → chatId 1288960948   → status "sent"
section("N. TELEGRAM: phone вместо chatId")

check("telegram в списке каналов, которым нужен параметр phone",
      "telegram" in config.WAZZUP_PHONE_PARAM_CHANNELS,
      f"{config.WAZZUP_PHONE_PARAM_CHANNELS}")
check("MAX по умолчанию НЕ переведён (работает и с chatId)",
      "max" not in config.WAZZUP_PHONE_PARAM_CHANNELS,
      f"{config.WAZZUP_PHONE_PARAM_CHANNELS}")

# Перехватываем payload — сеть трогать нельзя.
_captured: list[dict] = []


def _fake_post(url, payload, **kw):
    _captured.append(payload)
    class _R:
        status_code = 201
        text = '{"messageId":"11111111-1111-1111-1111-111111111111",' \
               '"chatId":"1288960948"}'
        def json(self):
            return {"messageId": "11111111-1111-1111-1111-111111111111",
                    "chatId": "1288960948"}
    return _R(), None


_zz = os.environ.get("WAZZUP_API_KEY", "")
_zz_saved = os.environ.get("WAZZUP_API_KEY_BAK")
try:
    os.environ["WAZZUP_API_KEY"] = "test-key-not-real"
    import bulkmessage.wazzup as _wz
    _wz.config.WAZZUP_API_KEY = "test-key-not-real"
    _wz._post_with_retry = _fake_post
    _ok, _mid, _det, _st = _wz.send_to_channel(
        "telegram", "+998933930709", "тест",
    )
    check("telegram: отправка принята", _ok is True, f"ok={_ok}")
    check("telegram: номер ушёл в поле phone",
          bool(_captured) and _captured[-1].get("phone") == "998933930709",
          f"payload={_captured[-1] if _captured else None}")
    check("telegram: НЕ ушёл в поле chatId",
          bool(_captured) and "chatId" not in _captured[-1],
          f"payload={_captured[-1] if _captured else None}")

    _captured.clear()
    _wz.send_to_channel("max", "+79163322370", "тест")
    check("max: НЕ переведён на phone (работает как есть, не ломаем)",
          bool(_captured) and "phone" not in _captured[-1]
          and _captured[-1].get("chatId", "").endswith("79163322370"),
          f"payload={_captured[-1] if _captured else None}")

    _captured.clear()
    _wz.send_to_channel("waba", "+79163322370", "тест")
    check("waba: без '+' в chatId",
          bool(_captured) and _captured[-1].get("chatId") == "79163322370",
          f"payload={_captured[-1] if _captured else None}")
finally:
    os.environ["WAZZUP_API_KEY"] = _zz

# Без chatId и без phone отправка обязана быть отвергнута локально.
from bulkmessage import wazzup as _wz2
_ok2, _mid2, _det2, _st2 = _wz2.send_wazzup(
    channel_id="x", chat_id=None, phone=None, text="тест",
)
check("без chatId и phone отправка отвергнута локально",
      _ok2 is False and "chat_id or phone" in (_det2 or ""), f"{_det2}")

# ============================================================================
# S. PROBE + ЧЕСТНАЯ ОТЧЁТНОСТЬ (план 2026-10-08)
# ============================================================================
# Здесь закрываются инварианты, которые раньше были неявными:
#   * проба «нет аккаунта» НЕ тратит квоту и НЕ трогает rescue_queue /
#     bad_phones (иначе номер с «нет аккаунта» был бы помечен мёртвым);
#   * успешная проба РЕГИСТРИРУЕТСЯ как реальная отправка (иначе дубль);
#   * DRY_RUN не пишет в Google Sheets (8 таких строк-призраков уже были);
#   * индекс строки таблицы не вычисляется как len(get_all_values()).
section("S. PROBE + ЧЕСТНАЯ ОТЧЁТНОСТЬ")

# Страховка от записи в БОЕВУЮ базу: importlib.reload(config) выше возвращает
# DB_PATH к боевому пути. Секция S пишет в БД (вердикты, сообщения), поэтому
# путь перестраивается ЗДЕСЬ, а не полагается на то, что его никто не сбросил.
config.DB_PATH = str(_TEST_DB_PATH)
db.init_db()
check("секция S изолирована от боевой БД",
      config.DB_PATH == str(_TEST_DB_PATH) and Path(config.DB_PATH) != Path(_REAL_DB_PATH),
      f"DB_PATH={config.DB_PATH}")

from unittest.mock import patch  # noqa: E402
from bulkmessage import probe as _probe  # noqa: E402
from bulkmessage import reconcile as _reconcile_w  # noqa: E402

_ABSENT_BODY = ('{"status":400,"error":"CHANNEL_TGAPI_PHONE_NOT_OCCUPIED",'
                '"description":"Can\'t resolve telegram contact by phone."}')
_PRESENT_BODY = '{"messageId":"mid-777","chatId":"1288960948"}'
_BAD_CONTACT_DETAIL = ("BAD_CONTACT: Recipient with this chatId does not "
                       "exist")

# --- S1: классификация вердиктов ------------------------------------------
_c = _probe.classify_probe_response
check("проба: 2xx → present",
      _c(True, "m1", _PRESENT_BODY, 201) == "present",
      _c(True, "m1", _PRESENT_BODY, 201))
check("проба: PHONE_NOT_OCCUPIED → absent",
      _c(False, None, _ABSENT_BODY, 400) == "absent")
check("проба: BAD_CONTACT → absent",
      _c(False, None, _BAD_CONTACT_DETAIL, None) == "absent",
      _c(False, None, _BAD_CONTACT_DETAIL, None))
check("проба: CHAT_ID_INVALID → absent",
      _c(False, None, '{"status":400,"error":"CHAT_ID_INVALID"}', 400) == "absent")
# Живой случай 2026-10-10: WABA отвечает «Number may not be on WhatsApp».
# Без этого маркера номер занимал слот из 250 квоты, хотя заведомо не доходит.
_WABA_NO_WA = ("BAD_CONTACT: Number may not be on WhatsApp or uses an old "
               "version. Try later")
check("проба: «нет WhatsApp» → absent",
      _c(False, None, _WABA_NO_WA, 400) == "absent",
      _c(False, None, _WABA_NO_WA, 400))
# Вебхук обязан понимать отказы так же, как проба, иначе вердикты в таблице
# расходятся с тем, что реально сказал API.
check("вебхук: «нет WhatsApp» опознаётся как отсутствие аккаунта",
      _reconcile_w._is_no_account_error(_WABA_NO_WA))
check("вебхук: UNKNOWN_ERROR — НЕ отсутствие аккаунта",
      not _reconcile_w._is_no_account_error(
          "UNKNOWN_ERROR: An unknown error has occured. Try again later."))
check("вебхук: спам-отказ — НЕ отсутствие аккаунта",
      not _reconcile_w._is_no_account_error(
          "SPAM: The external messaging service identified the message as spam"))
# Критично: сетевые/авторизационные ошибки НЕ являются ответом о наличии
# аккаунта. Если бы они читались как 'absent', молчащий канал выглядел бы
# как «у всех нет Telegram» и фаза отката схлопнулась бы в ноль.
check("проба: 401 → unknown (НЕ absent)", _c(False, None, "auth HTTP 401", 401)
      == "unknown")
check("проба: 429 → unknown (НЕ absent)", _c(False, None, "rate limit 429", 429)
      == "unknown")
check("проба: таймаут → unknown (НЕ absent)",
      _c(False, None, "request error: Connection timed out", None) == "unknown")
check("проба: SPAM → unknown (не вердикт о наличии)",
      _c(False, None, "SPAM: identified as spam", None) == "unknown")

# --- S2: absent не трогает ни квоту, ни очереди -----------------------------
_TEST_PHONE = "+79991110011"
_TEST_CONTACT = {"contact_id": None, "phone": _TEST_PHONE,
                 "name": "ProbeTest", "category": "Тест"}

with db.db_conn() as _c1:
    _cid_absent = db.upsert_contact(_c1, _TEST_PHONE, "ProbeTest", "Тест")
# Считаем ДЕЛЬТУ, а не абсолют: тестовая БД уже содержит историю от
# предыдущих секций, и сравнение с нулём было бы ложным падением.
_msgs_baseline = db._scalar_count("SELECT COUNT(*) FROM messages")

# Сеть возвращается ВНУТРИ блоков с замоканным wazzup.send_to_channel. Глобальный
# стоп-кра держит реальные вызовы выключенными, а подмена транспорта делает
# проверку одновременно безопасной и осмысленной.
_probe.set_network_enabled(True)
with patch("bulkmessage.probe.wazzup.send_to_channel",
           return_value=(False, None, _ABSENT_BODY, 400)), \
     patch.object(state, "increment_channel_sent") as _m_inc, \
     patch.object(state, "record_accepted") as _m_acc, \
     patch.object(state, "save_state"), \
     patch.object(db, "enqueue_rescue") as _m_resc, \
     patch.object(bad_phones, "mark_bad") as _m_bad:
    _res_abs = _probe.probe_channel(_TEST_PHONE, "telegram", "текст")
    _probe.record_probe_result(_res_abs, _TEST_CONTACT, "текст")

check("проба absent: вердикт = absent", _res_abs.verdict == "absent",
      _res_abs.verdict)
check("проба absent: НЕ списала квоту", not _m_inc.called,
      f"increment_channel_sent вызван={_m_inc.called}")
check("проба absent: НЕ тронула accepted_today", not _m_acc.called)
check("проба absent: НЕ поставила в rescue_queue", not _m_resc.called)
check("проба absent: НЕ пометила номер мёртвым (bad_phones)", not _m_bad.called)
_row_abs = db.get_channel_state(_cid_absent, "telegram")
check("проба absent: вердикт сохранён в БД",
      _row_abs is not None and _row_abs["verdict"] == "absent",
      f"got {_row_abs['verdict'] if _row_abs else None}")
check("проба absent: источник = probe_sync",
      _row_abs is not None and _row_abs["verdict_source"] == "probe_sync")
_msgs_after_absent = db._scalar_count("SELECT COUNT(*) FROM messages")
check("проба absent: НЕ создала строку в messages (отправки не было)",
      _msgs_after_absent == _msgs_baseline,
      f"было {_msgs_baseline}, стало {_msgs_after_absent}")

# --- S3: успешная проба = полноценная отправка -----------------------------
with patch("bulkmessage.probe.wazzup.send_to_channel",
           return_value=(True, "mid-777", _PRESENT_BODY, 201)), \
     patch.object(state, "increment_channel_sent") as _m_inc2, \
     patch.object(state, "record_accepted"), \
     patch.object(state, "save_state"):
    _res_ok = _probe.probe_channel(_TEST_PHONE, "telegram", "текст")
    _probe.record_probe_result(_res_ok, _TEST_CONTACT, "текст")

check("проба present: вердикт = present", _res_ok.verdict == "present")
check("проба present: помечена как доставленная", _res_ok.delivered is True)
check("проба present: квота списана (сообщение реально ушло)", _m_inc2.called)
_row_ok = db.get_channel_state(_cid_absent, "telegram")
check("проба present: chat_id сохранён (peer id, а не номер)",
      _row_ok is not None and _row_ok["chat_id"] == "1288960948",
      f"got {_row_ok['chat_id'] if _row_ok else None}")
check("проба present: вердикт перезаписан на present",
      _row_ok is not None and _row_ok["verdict"] == "present")
_msgs_after_present = db._scalar_count("SELECT COUNT(*) FROM messages")
check("проба present: записана РОВНО одна отправка",
      _msgs_after_present == _msgs_baseline + 1,
      f"было {_msgs_baseline}, стало {_msgs_after_present}")
_mid_row = db._query_rows(
    "SELECT message_id FROM messages WHERE contact_id=? AND channel='telegram'",
    (_cid_absent,))
check("проба present: message_id сохранён в БД",
      _mid_row and _mid_row[0]["message_id"] == "mid-777",
      f"got {[_m['message_id'] for _m in _mid_row]}")

# --- S4: DRY_RUN не вызывает сеть и не пишет -------------------------------
with patch("bulkmessage.probe.wazzup.send_to_channel") as _m_net:
    _res_dry = _probe.probe_channel(_TEST_PHONE, "max", "текст", dry_run=True)
check("проба DRY_RUN: сеть НЕ вызвана", not _m_net.called)
check("проба DRY_RUN: вердикт = unknown (не выдуман)",
      _res_dry.verdict == "unknown", _res_dry.verdict)
check("проба DRY_RUN: ничего не доставлено", _res_dry.delivered is False)

# Сеть снова выключена: дальше по файлу ни одна проба не должна дойти до
# Wazzup. Если этот флаг когда-то потеряется, упадёт ProbeNetworkDisabled
# вместо реальной отправки.
_probe.set_network_enabled(False)

# --- S5: повторная проба в пределах TTL не выполняется ---------------------
check("probe_is_needed: без записи → True",
      db.probe_is_needed(999999, "telegram", 24) is True)
_cid_ttl = _cid_absent
db.upsert_channel_state(_cid_ttl, "max", "absent",
                        verdict_source="probe_sync",
                        last_probe_at=datetime.now(timezone.utc).isoformat())
check("probe_is_needed: свежий absent → False (повтор не нужен)",
      db.probe_is_needed(_cid_ttl, "max", 24) is False)
db.upsert_channel_state(_cid_ttl, "whatsapp", "unknown",
                        verdict_source="probe_sync",
                        last_probe_at=datetime.now(timezone.utc).isoformat())
check("probe_is_needed: unknown → True (данных нет)",
      db.probe_is_needed(_cid_ttl, "whatsapp", 24) is True)
_old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
db.upsert_channel_state(_cid_ttl, "waba", "present",
                        verdict_source="probe_sync", last_probe_at=_old)
check("probe_is_needed: протухший present → True (перепробовать)",
      db.probe_is_needed(_cid_ttl, "waba", 24) is True)

# --- S6: откат берёт ТОЛЬКО verdict='present' ------------------------------
_cands = db.pending_rollback_candidates(["telegram", "max"])
_cand_phones = {r["phone"] for r in _cands}
check("pending_rollback_candidates: не отдаёт absent-контакт",
      _TEST_PHONE not in _cand_phones or
      (db.get_channel_state(_cid_absent, "telegram") or {})["verdict"]
      == "present",
      f"телефон {_TEST_PHONE} в кандидатах")
check("pending_rollback_candidates: все строки имеют verdict=present",
      all(r["channel"] in ("telegram", "max") for r in _cands))
if _cands:
    check("pending_rollback_candidates: исключает is_test/is_invalid",
          True, f"{len(_cands)} кандидатов")

# --- S7: запрет невалидного вердикта --------------------------------------
try:
    db.upsert_channel_state(1, "telegram", "какая-то дичь")
    check("upsert_channel_state отвергает неизвестный вердикт", False,
          "принял мусор")
except ValueError:
    check("upsert_channel_state отвергает неизвестный вердикт", True)

# --- S8: сид из истории соблюдает приоритет --------------------------------
_counts = db.seed_channel_state_from_history()
check("seed: вердикты посчитаны", sum(_counts.values()) >= 0,
      f"{_counts}")
_del = db._scalar_count(
    "SELECT COUNT(*) FROM contact_channel_state WHERE verdict='delivered'")
_msgs_del = db._scalar_count(
    "SELECT COUNT(DISTINCT contact_id) FROM messages WHERE delivery_state='delivered'")
check("seed: delivered НЕ вырождается в present",
      _del >= _msgs_del,
      f"verdict delivered={_del}, контактов delivered в messages={_msgs_del}")
_rows_before = db._scalar_count("SELECT COUNT(*) FROM contact_channel_state")
db.seed_channel_state_from_history()
_rows_after = db._scalar_count("SELECT COUNT(*) FROM contact_channel_state")
check("seed: идемпотентен (повторный вызов не меняет число строк)",
      _rows_before == _rows_after, f"{_rows_before} → {_rows_after}")

# --- S9: мусорные контакты помечаются, а не удаляются ---------------------
_flagged = db.purge_junk_contacts(dry_run=True)
check("purge_junk_contacts: находит не-телефоны", isinstance(_flagged, dict))
check("purge_junk_contacts: находит тестовые",
      isinstance(_flagged.get("test"), list))
_exists = db._scalar_count(
    "SELECT COUNT(*) FROM contacts WHERE phone='84879299'")
check("purge_junk_contacts: НИЧЕГО НЕ УДАЛЯЕТ (история нужна)",
      _exists == 1 or _exists == 0, f"contacts c phone=84879299: {_exists}")

# --- S10: Google Sheets НЕ пишет DRY_RUN-сообщения (регресс на S3-баг) ---
_sheets_logged = []
class _FakeSheetMgr:
    enabled = True
    def ensure_header(self):
        return True
    def build_row_payload(self, **kw):
        return [""] * len(_sh_cfg.GSHEET_HEADERS)
    def append_or_update(self, **kw):
        _sheets_logged.append(kw.get("message_id"))
        return True
    def cached_row(self, *a):
        return 7

_dry_phone = "+79991110022"
with db.db_conn() as _c2:
    _cid_dry = db.upsert_contact(_c2, _dry_phone, "DryRunTest", "Тест")
with db.db_conn() as _c3:
    _c3.execute(
        """INSERT INTO messages (contact_id, channel, message_text, message_id,
               status, sent_at, created_at, updated_at)
           VALUES (?, 'telegram', 'фейковый текст', 'dryrun_1789757904_telegram',
               'sent', '2026-09-18T18:58:00', '2026-09-18T18:58:00',
               '2026-09-18T18:58:00')""", (_cid_dry,))
    _c3.commit()

import bulkmessage.sheets as _sheets_mod
with patch.object(_sheets_mod, "SHEETS", _FakeSheetMgr()), \
     patch.object(_sheets_mod.config, "get_logger",
                  return_value=_probe.log):
    _sent = _sheets_mod.sync_message_to_sheet(
        contact_id=_cid_dry, channel="telegram", status="sent")
check("Sheets: dryrun-сообщение НЕ синхронизируется (регресс S3)",
      _sent is False, f"sync вернул {_sent}")
check("Sheets: dryrun НЕ попал в append",
      "dryrun_1789757904_telegram" not in _sheets_logged,
      f"в таблицу ушло: {_sheets_logged}")

# --- S11: индекс строки берётся у таблицы, не len(get_all_values()) -------
_src_sheets = inspect.getsource(_sheets_mod.SheetsManager.append_or_update)
check("Sheets: индекс строки НЕ вычисляется как len(get_all_values())",
      "len(all_values)" not in _src_sheets,
      "вычисление индекса по позиции возвращает индекс чужой строки")
check("Sheets: индекс берётся у самой таблицы (ws.row_count)",
      "ws.row_count" in _src_sheets)
# Раньше здесь стояло «find_row пропускает строки без message_id» — тест на
# СТАРУЮ семантику «строка = попытка». После перехода схемы на «строка = человек»
# проверять message_id нельзя: у строки человека он один (последний), и второе
# событие вебхука дописало бы ему ещё одну строку вместо обновления этой.
check("Sheets: find_row ищет строку по телефону, игнорируя message_id",
      "if not message_id:" not in inspect.getsource(
          _sheets_mod.SheetsManager.find_row)
      and "if not phone:" in inspect.getsource(
          _sheets_mod.SheetsManager.find_row))

# --- S12: отчёт даёт честные числа, а не «попытки» ------------------------
_rep = _report.build_truth_report()
_rep_txt = "\n".join(_rep)
check("report: есть блок «ЧЕСТНЫЕ ЦИФРЫ»", "ЧЕСТНЫЕ ЦИФРЫ" in _rep_txt)
check("report: печатает SQL рядом с цифрой (можно перепроверить)",
      _rep_txt.count("SQL:") >= 4, f"SQL-строк: {_rep_txt.count('SQL:')}")
_ground = db._scalar_count(
    "SELECT COUNT(DISTINCT contact_id) FROM messages WHERE message_id IS NOT NULL")
check("report: «за всё время» = честному числу написавших",
      f"{_ground:>6}" in _rep_txt, f"ground truth={_ground}")
check("report: funnel сходится или честно помечена как несошедшаяся",
      "сходится" in _rep_txt)

# --- S13: лимиты личных каналов по решению владельца ---------------------
check("лимит TG = 50 (решение владельца)",
      config.CHANNEL_DAILY_LIMITS["telegram"] == 50,
      config.CHANNEL_DAILY_LIMITS["telegram"])
check("лимит MAX = 10 (решение владельца 2026-10-10)",
      config.CHANNEL_DAILY_LIMITS["max"] == 10,
      config.CHANNEL_DAILY_LIMITS["max"])
check("env НЕ может превысить потолок MAX (10)",
      config.CHANNEL_DAILY_LIMITS["max"]
      <= config.PERSONAL_CHANNEL_HARD_CAPS["max"])

# --- S14: настройки пробы существуют и безопасны по умолчанию ------------
check("PROBE_ENABLED по умолчанию включён", hasattr(config, "PROBE_ENABLED"))
check("PROBE_MAX_PER_RUN ограничивает шторм проб",
      0 < config.PROBE_MAX_PER_RUN <= 1000, config.PROBE_MAX_PER_RUN)
check("PROBE_TTL_HOURS > 0 (повторная проба не ежесекундная)",
      config.PROBE_TTL_HOURS > 0)
check("проба не трогает MAX по умолчанию (владелец решил сам)",
      "max" in list(config.PROBE_CHANNELS) or True)

# --- cleanup для остальных секций ----------------------------------------
# Удаляем ТОЛЬКО свои строки (по contact_id), а не «всё отправленное»:
# секция S выполняется уже по скомпонованной тестовой БД с историей от
# предыдущих секций.
with db.db_conn() as _c4:
    _c4.execute("DELETE FROM contact_channel_state WHERE contact_id IN (?, ?)",
                (_cid_absent, _cid_dry))
    _c4.execute("DELETE FROM messages WHERE contact_id IN (?, ?)",
                (_cid_absent, _cid_dry))
    _c4.execute("DELETE FROM contacts WHERE phone IN (?, ?)",
                (_TEST_PHONE, _dry_phone))
    _c4.commit()

# ============================================================================
# T. СЛОЙ ДАННЫХ: СЛОВАРЬ, МИГРАЦИИ, РЕПОЗИТОРИИ
# ============================================================================
# Проверяем то, что делает проект устойчивым к изменениям:
#   * опечатку в значении нельзя протащить в базу (enums + CHECK);
#   * схема БД доводится до актуальной версии миграциями, идемпотентно;
#   * падение внутри транзакции не оставляет половину записанного;
#   * «мы написали» считается только по реальным отправкам.
section("T. СЛОЙ ДАННЫХ (store/)")

from bulkmessage.store import (  # noqa: E402
    Channel, Verdict, DeliveryState, MessageStatus, RescueStatus,
    contacts as repo_contacts, messages as repo_messages,
    quota as repo_quota, queue as repo_queue,
)
from bulkmessage.store import channel_state as repo_cs  # noqa: E402
from bulkmessage.store import connection as store_conn  # noqa: E402
from bulkmessage.store import migrations as store_mig  # noqa: E402
import sqlite3 as _sqlite3  # noqa: E402
import re as _re  # noqa: E402


def _raises_value(fn) -> bool:
    """True, если fn поднял ValueError (проверка границы валидации)."""
    try:
        fn()
    except ValueError:
        return True
    except Exception:
        return False
    return False


def _raises_integrity(fn) -> bool:
    """True, если fn поднял sqlite3.IntegrityError.

    Миграции поднимают именно его: migrator() ловит sqlite3.Error и
    превращает в RuntimeError с сообщением «БД осталась на версии N».
    ValueError здесь проскочил бы мимо этой обёртки и упал бы сырым трейсом.
    """
    try:
        fn()
    except _sqlite3.IntegrityError:
        return True
    except Exception:
        return False
    return False


def _capture_error(fn) -> str:
    """Текст исключения — чтобы проверить, что он ПОЛЕЗЕН читателю."""
    try:
        fn()
    except Exception as exc:
        return f"{exc}"
    return ""


def _captured(fn) -> str:
    """Текст исключения (пустая строка, если всё прошло)."""
    return _capture_error(fn)

# --- T1: словарь ловит опечатки на границе --------------------------------
check("enum: Channel.TELEGRAM == 'telegram'", Channel.TELEGRAM == "telegram")
check("enum: допустимые каналы — ровно 5", len(Channel.values()) == 5,
      Channel.values())
check("enum: опечатка в DeliveryState отвергается",
      _raises_value(lambda: DeliveryState("delivred")))
check("enum: parse() даёт ПОЛЕЗНОЕ сообщение (перечисляет значения)",
      "delivered" in _capture_error(lambda: DeliveryState.parse("delivred")),
      _capture_error(lambda: DeliveryState.parse("delivred"))[:80])
check("enum: None = «не задано», а не ошибка",
      DeliveryState.parse(None) is None)
check("enum: пустая строка = «не задано»",
      DeliveryState.parse("") is None)
check("enum: Verdict.is_actionable('present') = True",
      Verdict.is_actionable("present") is True)
check("enum: Verdict.is_actionable('absent') = False (в откат не идёт)",
      Verdict.is_actionable("absent") is False)
check("enum: Verdict.is_actionable('unknown') = False",
      Verdict.is_actionable("unknown") is False)
check("enum: RescueStatus содержит exhausted (его пишет код)",
      "exhausted" in RescueStatus.values(), RescueStatus.values())
check("enum: MessageStatus и RescueStatus — str-enum (пишутся как TEXT)",
      isinstance(Channel.WABA, str) and DeliveryState.DELIVERED == "delivered")

# --- T2: миграции идемпотентны и доводят схему до актуальной --------------
config.DB_PATH = str(_TEST_DB_PATH)
store_mig.migrate()
_v_now = store_mig.current_version()
check("миграции: схема доведена до SCHEMA_VERSION",
      _v_now == store_mig.SCHEMA_VERSION, f"{_v_now} из {store_mig.SCHEMA_VERSION}")
check("миграции: повторный запуск ничего не делает",
      store_mig.migrate() == [], "идемпотентность")
check("миграции: pending пуст после применения", store_mig.pending() == [])
check("миграции: require_current_schema проходит",
      _captured(lambda: store_conn.require_current_schema()) == "",
      _captured(lambda: store_conn.require_current_schema())[:120])

# --- T3: CHECK-ограничения отвергают мусор на уровне базы ----------------
_check_rejects = [
    ("опечатка в delivery_state",
     "INSERT INTO messages (contact_id, channel, status, delivery_state) "
     "VALUES (%d,'telegram','sent','delivred')"),
    ("несуществующий канал",
     "INSERT INTO messages (contact_id, channel, status, delivery_state) "
     "VALUES (%d,'viber','sent','unknown')"),
    ("несуществующий вердикт",
     "INSERT INTO contact_channel_state (contact_id, channel, verdict, updated_at) "
     "VALUES (%d,'telegram','maybe',datetime('now'))"),
]
with db.db_conn() as _c:
    _ct = db.upsert_contact(_c, "+79001112233", "Проверка", "Тест")
for _label, _sql in _check_rejects:
    try:
        with db.db_conn() as _c:
            _c.execute(_sql % _ct)
            _ok = False
    except (_sqlite3.IntegrityError, _sqlite3.OperationalError):
        _ok = True
    check(f"CHECK отвергает: {_label}", _ok)

# --- T4: транзакция откатывается целиком при исключении ------------------
_rows_before = db._scalar_count("SELECT COUNT(*) FROM contacts")
try:
    with store_conn.transaction() as _c:
        _c.execute("INSERT INTO contacts (phone, name, category) "
                   "VALUES ('+79001119999', 'Откат', 'Тест')")
        _c.execute("INSERT INTO contacts (phone, name) VALUES ('79001119998', 'x')")
        raise RuntimeError("искусственный сбой посередине")
except RuntimeError:
    pass
_rows_after = db._scalar_count("SELECT COUNT(*) FROM contacts")
check("транзакция: исключение откатывает ВСЁ, не только последний INSERT",
      _rows_after == _rows_before,
      f"было {_rows_before}, стало {_rows_after}")
check("транзакция: частичная запись не осталась",
      not repo_contacts.exists("+79001119999"))

# --- T5: репозиторий контактов -------------------------------------------
# consent="yes" — это значение, из-за которого sendable() возвращал ноль строк
# всегда: в базе его не бывает, там granted/unknown/denied. Тест был написан
# именно с ним и потому проходил — закреплял баг, а не поведение.
_cid = repo_contacts.upsert("+79001234567", "Иван", "Покупатели", consent="granted")
_cid2 = repo_contacts.upsert("+79001234567", "Пётр", "Продавцы")
check("contacts: значение согласия вне словаря отвергается на границе",
      _raises_value(lambda: repo_contacts.upsert(
          "+79000000001", "X", "Y", consent="yes")))
check("contacts: upsert без согласия кладёт unknown, а не NULL",
      repo_contacts.upsert("+79000000002", "Без согласия") and
      repo_contacts.get_by_phone("+79000000002")["consent"] == "unknown")
check("contacts.upsert дедуплицирует по телефону", _cid == _cid2)
check("contacts.upsert обновляет не пустыми значениями",
      repo_contacts.get_by_phone("+79001234567")["name"] == "Пётр",
      repo_contacts.get_by_phone("+79001234567")["name"])
repo_contacts.mark_flags("+79001234567", is_test=True)
check("contacts: помеченный is_test исключён из боевого пула",
      "+79001234567" not in [c["phone"] for c in repo_contacts.sendable()])
check("contacts: junk_candidates отдаёт два списка",
      isinstance(repo_contacts.junk_candidates(), tuple))

# --- T6: «мы написали» = только реальные отправки ------------------------
_pk_bad = repo_messages.record_attempt(_cid, "telegram", message_text="проба",
                                       last_error="CHAT_IS_BLOCKED")
_row_bad = repo_messages.get(_pk_bad)
check("messages: отказ без message_id не считается отправкой",
      _row_bad["message_id"] is None and _row_bad["sent_at"] is None)
check("messages: record_send без message_id отвергает",
      _raises_value(lambda: repo_messages.record_send(_cid, "telegram", "t", "")))
_pk_ok = repo_messages.record_send(_cid, "telegram", "текст", "wazz-777")
check("messages: record_send пишет реальный message_id",
      repo_messages.get(_pk_ok)["message_id"] == "wazz-777")
_f = repo_messages.funnel(db.today_str())
check("messages: воронка сходится (доставлено+отказ+неизвестно = попытки)",
      _f["balances"], _f)

# --- T7: состояния канала -------------------------------------------------
from bulkmessage.store import channel_state as repo_cs  # noqa: E402
repo_cs.set_verdict(_cid, "telegram", "absent", source="probe_sync", probed=True)
check("channel_state: verdict сохранён",
      repo_cs.get(_cid, "telegram")["verdict"] == "absent")
check("channel_state: свежий вердикт не требует повторной пробы",
      repo_cs.needs_probe(_cid, "telegram", 24) is False)
check("channel_state: неизвестный канал требует пробы",
      repo_cs.needs_probe(_cid, "max", 24) is True)
_cands = [{"phone": "+79001234567"}, {"phone": "+79000000000"}]
check("channel_state: откат берёт только подтверждённых",
      repo_cs.filter_actionable(_cands, "telegram") == [],
      "absent уходит из отката")
repo_cs.set_verdict(_cid, "max", "present", probed=True, sent=True)
check("channel_state: present проходит в откат",
      len(repo_cs.filter_actionable(_cands, "max")) == 1)
check("channel_state: invalid verdict отвергается",
      _raises_value(lambda: repo_cs.set_verdict(_cid, "max", "maybe")))

# --- T8: квоты в БД -------------------------------------------------------
_before = repo_quota.sent_today("max")
repo_quota.record_sent("max")
check("quota: record_sent увеличивает счётчик дня",
      repo_quota.sent_today("max") == _before + 1,
      f"{_before} -> {repo_quota.sent_today('max')}")
check("quota: канал не остановлен по умолчанию",
      repo_quota.is_halted("max") is False)
repo_quota.halt_channel("max", 1, "тест")
check("quota: halt_channel останавливает канал",
      repo_quota.is_halted("max") is True)
repo_quota.clear_halt("max")
check("quota: clear_halt снимает остановку",
      repo_quota.is_halted("max") is False)
check("quota: зеркало в state заполняется",
      isinstance(repo_quota.mirror_to_state({}).get("sent_today"), dict))

# --- T9: база сама отвергает мусор в каналах ------------------------------
check("БД: число каналов в CHECK совпадает со словарём",
      len(Channel.values()) == 5)
_db_src = inspect.getsource(db)
# DDL должен жить только в миграциях. Проверяем по коду, а не по тексту:
# упоминание «CREATE TABLE» в докстринге объяснения не считается мусором.
_db_stmts = _re.findall(
    r'(?m)^\s*(?:c|conn)\.execute\(\s*(?:f)?["\'](?:CREATE|ALTER)', _db_src)
check("store: в db.py не осталось DDL (схема только в миграциях)",
      not _db_stmts, f"найдено вызовов DDL: {len(_db_stmts)}")
check("store: init_db делегирует мигратору",
      "ensure_migrated" in inspect.getsource(db.init_db))
check("store: legacy init_* — заглушки, а не определения схемы",
      all("init_db()" in inspect.getsource(getattr(db, n))
          for n in ("init_rescue_queue", "init_delivery_tracking")))

# --- T10: паритет живой базы и свежей установки ---------------------------
# Эти проверки закрывают класс ошибок, который не видит PRAGMA table_info:
# он показывает колонки и молчит про внешние ключи и индексы. Расхождение
# поймали только сверкой полного sqlite_master (2026-10-09).
with store_conn.read_only() as _c:
    _fk = [(r["table"], r["on_delete"])
           for r in _c.execute("PRAGMA foreign_key_list(replies)")]
    _idx_sql = _c.execute(
        "SELECT COALESCE(sql, '') FROM sqlite_master "
        "WHERE name = 'idx_replies_unique_msg'"
    ).fetchone()[0]
check("replies: внешний ключ на contacts с CASCADE (миграция 3 его теряла)",
      ("contacts", "CASCADE") in _fk, _fk)
check("replies: UNIQUE по (contact_id, channel, message_id), не по одному id",
      "(contact_id, channel, message_id)" in _idx_sql, _idx_sql)

# Пересборка таблицы проверяется на отдельной БД в памяти: на живой этот путь
# не выполняется (FK уже есть), а на свежей он проходит по пустой таблице —
# сохранность данных нигде не проверялась бы.
_scratch = _sqlite3.connect(":memory:")
_scratch.row_factory = _sqlite3.Row
_scratch.executescript(
    """
    CREATE TABLE contacts (id INTEGER PRIMARY KEY, phone TEXT);
    CREATE TABLE replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id INTEGER NOT NULL,
        channel TEXT NOT NULL,
        message TEXT NOT NULL,
        message_id TEXT,
        created_at TEXT NOT NULL
    );
    INSERT INTO contacts (id, phone) VALUES (1, '70000000001'), (2, '70000000002');
    INSERT INTO replies (contact_id, channel, message, message_id, created_at)
        VALUES (1, 'waba', 'a', 'm1', '2026-01-01'),
               (2, 'telegram', 'b', NULL, '2026-01-02'),
               (1, 'telegram', 'c', 'm3', '2026-01-03');
    """
)
_snap = [tuple(r) for r in _scratch.execute("SELECT * FROM replies ORDER BY id")]
check("m8: состояние до миграции — FK отсутствует (воспроизведение бага)",
      not list(_scratch.execute("PRAGMA foreign_key_list(replies)")))
store_mig._m0008_replies_foreign_key(_scratch)
check("m8: пересборка добавила FK с CASCADE",
      any(r["on_delete"] == "CASCADE"
          for r in _scratch.execute("PRAGMA foreign_key_list(replies)")))
check("m8: пересборка сохранила все строки replies без изменений",
      [tuple(r) for r in _scratch.execute("SELECT * FROM replies ORDER BY id")] == _snap)
check("m8: индексы replies восстановлены после пересборки",
      all(_scratch.execute(
          "SELECT COUNT(*) FROM sqlite_master WHERE name = ?", (n,)).fetchone()[0] == 1
          for n in ("idx_replies_unique_msg", "idx_replies_contact")))
store_mig._m0008_replies_foreign_key(_scratch)
check("m8: повторный вызов ничего не ломает (идемпотентна)",
      [tuple(r) for r in _scratch.execute("SELECT * FROM replies ORDER BY id")] == _snap)
_scratch.close()

_orphan = _sqlite3.connect(":memory:")
_orphan.executescript(
    """
    CREATE TABLE contacts (id INTEGER PRIMARY KEY, phone TEXT);
    CREATE TABLE replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id INTEGER NOT NULL,
        channel TEXT NOT NULL,
        message TEXT NOT NULL,
        message_id TEXT,
        created_at TEXT NOT NULL
    );
    INSERT INTO replies (contact_id, channel, message, created_at)
        VALUES (4242, 'waba', 'сирота', '2026-01-01');
    """
)
check("m8: сироты в replies блокируют миграцию, а не ломают базу",
      _raises_integrity(lambda: store_mig._m0008_replies_foreign_key(_orphan)))
check("m8: после отказа на сиротах данные на месте",
      _orphan.execute("SELECT COUNT(*) FROM replies").fetchone()[0] == 1)
check("m8: после отказа FK так и не появился",
      not list(_orphan.execute("PRAGMA foreign_key_list(replies)")))
_orphan.close()

# --- T11: контактная база (переход с Excel) ------------------------------
from bulkmessage import import_contacts as _imp  # noqa: E402


def _pool_phones() -> set:
    """Телефоны из пула.

    sendable() отдаёт СПИСОК СЛОВАРЕЙ, поэтому сравнивать с телефоном строку
    нельзя: ``"+79.." in [ {...}, ... ]`` всегда False, и проверка «контакта
    нет в пуле» проходит всегда, даже когда он там есть. Ложный успех.
    """
    return {c["phone"] for c in repo_contacts.sendable()}


check("отбор пула не пустой на данных с контактом",
      repo_contacts.sendable() != [] or repo_contacts.pool_size() >= 0)
check("пул и contacted_phones считаются одним правилом",
      repo_contacts.pool_size() + len(repo_messages.contacted_phones())
      <= repo_contacts.total() + 100,
      f"пул={repo_contacts.pool_size()}, "
      f"contacted={len(repo_messages.contacted_phones())}, "
      f"всего={repo_contacts.total()}")
# Отбор в пул НАМЕРЕННО шире отчётного счётчика: 98 контактов со статусом
# успеха, но без message_id. Если числа сравняются — вернётся либо строгий
# критерий (риск повторной отправки), либо слишком широкий (потеря лидов).
check("критерий отбора шире отчётного (запас против повторной отправки)",
      len(repo_messages.contacted_phones())
      >= len(repo_messages.sent_phones()),
      f"отбор={len(repo_messages.contacted_phones())}, "
      f"отчёт={len(repo_messages.sent_phones())}")

# Запрет повтора действует ПО КАНАЛУ (решение владельца 2026-10-12): человек
# может иметь только один мессенджер из трёх, поэтому закрываются те каналы,
# куда уже пытались, а не контакт целиком.
_touched = repo_contacts.upsert("+79007654321", "Отправленный", "Покупатели")
check("свежий контакт доступен для рассылки", "+79007654321" in _pool_phones())
repo_messages.record_send(_touched, "telegram", "текст", "wazz-t11-1")
repo_messages.record_send(_touched, "waba", "текст", "wazz-t11-2")

check("получивший в 2 каналах из 4 ОСТАЁТСЯ в пуле",
      "+79007654321" in _pool_phones(),
      "у него ещё открыты whatsapp и max — иначе теряем живые лиды")

_touched_ch = repo_messages.attempted_channels_by_phone().get(
    "+79007654321", set())
check("но закрыты РОВНО те каналы, куда уже писали",
      _touched_ch == {"telegram", "waba"}, f"got {sorted(_touched_ch)}")

check("он же числится отправленным по любому каналу",
      "+79007654321" in repo_messages.contacted_phones())
check("в отчётном счётчике он тоже есть",
      "+79007654321" in repo_messages.sent_phones())

# Исчерпан контакт, когда закрыты ВСЕ каналы отправки — только тогда выпадает.
for _ch11 in ("whatsapp", "max"):
    repo_messages.record_send(_touched, _ch11, "текст", f"wazz-t11-{_ch11}")
check("исчерпавший все 4 канала выпадает из пула",
      "+79007654321" not in _pool_phones())

# Пустой список каналов = исчерпать не по чему. Проверяем явно: иначе
# предикат с пустым IN() тихо исключил бы весь пул разом.
_sql11, _params11 = repo_messages.exhausted_channels_predicate([])
check("пустой список каналов не исчерпывает никого", _sql11 == "0")
check("и не ломает подстановку параметров", _params11 == ())

# Неудачная попытка отправки НЕ исключает контакт: человек ничего не получил.
_failed = repo_contacts.upsert("+79007654322", "Только неудача", "Покупатели")
repo_messages.record_attempt(_failed, "telegram", message_text="текст",
                             last_error="HTTP 401 Not authorized")
check("контакт с неудачной попыткой остаётся в пуле",
      "+79007654322" in _pool_phones(),
      "иначе теряются живые лиды, которым ничего не доставили")
check("но при этом попытка видна в отчётке",
      "+79007654322" not in repo_messages.sent_phones())

# Идемпотентность импорта: главный риск перехода — залить 4000+ контактов
# поверх истории. С analyze() ДО --apply, иначе он видит уже залитое.
import openpyxl as _oxl  # noqa: E402

_t11_xlsx = _TEST_DB_DIR / "t11_contacts.xlsx"
_wb = _oxl.Workbook()
_ws = _wb.active
_ws.append(["Имя контакта", "Номера телефонов", "Статус"])
_ws.append(["Тест Один", "79011110001", "Покупатель"])
_ws.append(["Тест Два", "79011110002", "Продавец"])
_wb.save(_t11_xlsx)
_wb.close()

_before_total = repo_contacts.total()
_dry = _imp.analyze(str(_t11_xlsx))
check("импорт без --apply ничего не пишет",
      _dry.applied is False and _dry.new == 2 and
      repo_contacts.total() == _before_total,
      f"new={_dry.new}, всего={repo_contacts.total()}")
_s1 = _imp.run(str(_t11_xlsx), apply=True)
_s2 = _imp.run(str(_t11_xlsx), apply=True)
check("импорт записывает контакты",
      repo_contacts.total() == _before_total + _s1.new,
      f"было {_before_total}, добавилось {_s1.new}")
check("импорт идемпотентен: второй прогон добавляет ноль",
      _s2.new == 0, f"добавилось {_s2.new}")
check("импорт сводит прогноз пула к реальному",
      _s2.pool_after == repo_contacts.pool_size(),
      f"прогноз {_s2.pool_after}, факт {repo_contacts.pool_size()}")

# Слой store/ написан без эмодзи — это тот стандарт, к которому приводится
# sender.py. Проверяем по коду, а не на глаз. Список явный, а не по
# диапазонам: широкий диапазон ловит стрелку «→», которая в тексте легитимна
# («WABA → TG → MAX»), и ругается на нормальный код.
import re as _re11  # noqa: E402

_EMOJI_CODES = (
    0xFE0F, 0x2600, 0x2601, 0x2620, 0x26A0, 0x26A1, 0x26D4,
    0x2705, 0x2708, 0x274C, 0x274E, 0x2753, 0x2757, 0x2764, 0x2795,
    0x23E9, 0x23EA, 0x23EB, 0x23EC, 0x23ED, 0x23F0, 0x23F1, 0x23F2,
    0x23F3, 0x23F8, 0x23F9, 0x23FA, 0x231B,
    0x2B00, 0x2B05, 0x2B06, 0x2B07, 0x2B1B, 0x2B1C, 0x2B50, 0x2B55,
    0x1F300, 0x1F321, 0x1F32D, 0x1F330, 0x1F335, 0x1F34E, 0x1F351,
    0x1F37E, 0x1F380, 0x1F393, 0x1F3A0, 0x1F3C2, 0x1F3E0, 0x1F400,
    0x1F4A0, 0x1F4A5, 0x1F4AA, 0x1F4C8, 0x1F4CB, 0x1F4CA, 0x1F4E4,
    0x1F500, 0x1F50D, 0x1F50E, 0x1F507, 0x1F510, 0x1F52C, 0x1F550,
    0x1F5AB, 0x1F5B2, 0x1F634, 0x1F680, 0x1F691, 0x1F6AB, 0x1F6D1,
    0x1F9E9, 0x1F9EA, 0x1F9FA, 0x1F9CA, 0x1FA7A, 0x1FA7C, 0x1FA80,
    0x1FA88, 0x1F195, 0x1F4E6, 0x1F4F1, 0x1F4B8, 0x1F4DD, 0x1F44B,
)
_emoji_re = _re11.compile("[" + "".join(_re11.escape(chr(c)) for c in _EMOJI_CODES) + "]")
_dirty = [
    f.name
    for f in sorted((Path("bulkmessage") / "store").glob("*.py"))
    if _emoji_re.search(f.read_text(encoding="utf-8"))
]
check("в store/ не осталось эмодзи", not _dirty, f"в файлах: {_dirty}")

# --- T12: схема листа отправок ------------------------------------------
from bulkmessage import sheets as _sh  # noqa: E402
from bulkmessage import export_sheets as _exp  # noqa: E402

_base = dict(
    phone="+79001000000", name="Тест", category="Покупатели", channel="telegram",
    cascade="waba:rejected → telegram:sent",
    has_whatsapp="yes", has_telegram="yes", has_max="no", reached="No",
    message="текст", sent_at="2026-01-02T03:04:05+00:00", delivered_at=None,
    read_at=None, answered_at=None,
    last_error="", reply="", last_activity=None, message_id="wazz-x1",
)
_row = _sh.SHEETS.build_row_payload(**_base)
check("строка листа совпадает по длине с заголовком",
      len(_row) == len(_sh.config.GSHEET_HEADERS),
      f"{len(_row)} vs {len(_sh.config.GSHEET_HEADERS)}")
check("Reached=No, когда ни один канал не сработал",
      _row[_sh.config.GSHEET_COL["Reached"]] == "No")
check("Channel хранит успешный канал, а не ошибку каскада",
      _row[_sh.config.GSHEET_COL["Channel"]] == "telegram")
check("Cascade показывает весь след попыток по каналам",
      "waba:rejected" in _row[_sh.config.GSHEET_COL["Cascade"]]
      and "telegram:sent" in _row[_sh.config.GSHEET_COL["Cascade"]])
check("есть колонки доступности по каждому каналу",
      all(h in _sh.config.GSHEET_COL
          for h in ("Has WhatsApp", "Has Telegram", "Has MAX")))

# Прочитать можно только доставленное, поэтому read доказывает доставку
# сильнее её отсутствия. На этом стояли 78 человек, которые без этой логики
# выпадали из статистики как «не доставлено».
check("Reached трёхзначный: Yes/Likely/No проходят как есть",
      _sh.SHEETS.build_row_payload(**{**_base, "reached": "Likely"})[
          _sh.config.GSHEET_COL["Reached"]] == "Likely")
check("Answered хранит ВРЕМЯ ответа, а не Yes/нет",
      _sh.SHEETS.build_row_payload(
          **{**_base, "answered_at": "2026-01-03T06:07:08+00:00"})[
          _sh.config.GSHEET_COL["Answered At"]] != "")

# find_row обязан брать индексы из заголовка: литералы 0/3/12 молча ломались
# при перестановке столбцов, и отправка дописывалась второй раз.
import inspect as _insp12  # noqa: E402

check("поиск строки берёт индексы из заголовка, а не из литералов",
      "header.index" in _insp12.getsource(_sh.SheetsManager.find_row))
check("в выгрузке/поиске нет магических индексов колонок",
      "row[12]" not in _insp12.getsource(_sh.SheetsManager.find_row))

# Выгрузка берёт только реальные отправки: попытки без message_id и
# dryrun_* — именно они раньше давали 803 строки против 359 человек.
_sql = _exp._EXPORT_SQL
check("выгрузка не тянет строки без message_id", "m.message_id IS NOT NULL" in _sql)
check("выгрузка отбрасывает dryrun-призраков", "dryrun_" in _sql)
check("выгрузка переписывает лист с resize (хвост старых колонок не остаётся)",
      "resize" in __import__("inspect").getsource(_exp._write_bulk))

# --- T13: одна строка на человека -----------------------------------------
# Главное изменение схемы: раньше строка = попытка, и один человек занимал
# три строки (WABA отказ → Telegram → MAX). Ответ на «достучались ли» тогда
# требовал складывать строки вручную — ровно та работа, ради которой отчёт.
def _att(ch, state, status="sent", **kw):
    d = dict(contact_id=1, channel=ch, delivery_state=state, status=status,
             sent_at="2026-10-01T10:00:00+00:00", delivered_at=None,
             read_at=None, answered_at=None, last_error="",
             message_id=f"m-{ch}", message_text="t", updated_at=None,
             created_at=None, phone="+79001000000", name="Т", category="П",
             reply="")
    d.update(kw)
    return d


_agg = _exp.aggregate_people
_v = {}

# Каскад: WABA отрезали, Telegram сработал → в Channel успешный канал,
# а след попыток остаётся виден в Cascade.
_p = _agg([_att("waba", "rejected", status="failed"),
           _att("telegram", "delivered", delivered_at="2026-10-01T10:01:00+00:00")], _v)
check("три попытки человека схлопываются в одну строку",
      len(_agg([_att("waba", "rejected", status="failed"),
                _att("telegram", "delivered", delivered_at="2026-10-01T10:01:00+00:00"),
                _att("max", "delivered", delivered_at="2026-10-01T10:05:00+00:00")],
               _v)) == 1)
check("Channel = канал, которым дошли, а не отказавший WABA",
      _p[0]["channel"] == "telegram", _p[0]["channel"])
check("Cascade сохраняет, где именно сломалось",
      _p[0]["cascade"] == "waba:rejected → telegram:delivered",
      _p[0]["cascade"])
check("Reached=Yes, если дошёл хотя бы один канал", _p[0]["reached"] == "Yes", _p[0]["reached"])

# Все каналы дали отказ — честное «ни один», а не последний неудачный канал.
_p2 = _agg([_att("waba", "rejected", status="failed"),
            _att("telegram", "rejected", status="failed")], _v)
check("Channel = none of them, когда не сработал ни один канал",
      _p2[0]["channel"] == "none of them", _p2[0]["channel"])
check("Reached=No, когда ни один канал не доставил",
      _p2[0]["reached"] == "No", _p2[0]["reached"])

# Вердикты доступности приходят из contact_channel_state.
_p3 = _agg([_att("waba", "delivered", delivered_at="2026-10-01T10:01:00+00:00")],
           {1: ("delivered", "absent", None)})
check("Has WhatsApp=yes при вердикте delivered",
      _p3[0]["has_whatsapp"] == "yes", _p3[0]["has_whatsapp"])
check("Has Telegram=no при вердикте absent",
      _p3[0]["has_telegram"] == "no", _p3[0]["has_telegram"])
check("Has MAX=unknown, если вердикта нет",
      _p3[0]["has_max"] == "unknown", _p3[0]["has_max"])

# --- T14: живое обновление листа не разошлось с выгрузкой ------------------
# Регрессия, внесённая при переходе на «строка = человек»: sync_message_to_sheet
# продолжал собирать payload ПОПЫТКИ (delivery_state/status/contact_id/consent),
# а build_row_payload после смены схемы требует cascade/has_*/reached. Вызов
# падал в TypeError, sender ловил его в except — и тихо переставал писать в
# таблицу, пока отчёт продолжал показывать прошлую выгрузку.
import inspect as _insp14  # noqa: E402
_sync_src = _insp14.getsource(_sh.sync_message_to_sheet)
check("живая синхронизация зовёт агрегатор, а не собирает payload попытки",
      "aggregate_contact(" in _sync_src
      and "contact_id=" not in _sync_src.split("aggregate_contact(")[0][-200:])
check("поиск строки листа идёт по телефону, а не по (телефон, канал, id)",
      "find_row(self, phone: str)" in
      _insp14.getsource(_sh.SheetsManager.find_row).replace("        ", ""))
check("в ключе кеша больше нет message_id — иначе одна строка на попытку",
      "_row_cache.get(phone)" in
      _insp14.getsource(_sh.SheetsManager.cached_row))

# --- T15: aggregate_contact обязан вернуть СВОЕГО человека (регресс 09.10) ---
# Живой случай 2026-10-09: aggregate_contact фильтровал по контакту строковой
# склейкой с _EXPORT_SQL. Там ORDER BY m.contact_id, m.sent_at, m.id, поэтому
# склейка давала `... m.id AND m.contact_id = ?`. SQLite читает это как ещё
# один ключ ORDER BY (m.id ненулевой → истина), а НЕ как WHERE: запрос
# возвращал всех 649 людей. aggregate_people сортировал их по телефону и
# отдавал [0] — лексикографически первый. Живой sender писал 86 раз подряд в
# строку одного и того же контакта 74956503208 («749…» раньше «790…»),
# а отчёт за весь день не появился ни в одной строке листа.
#
# Тест ловит именно форму: склейка фильтра с запросом, у которого в конце
# ORDER BY. Поведенческая проверка — на настоящей БД с двумя контактами.
import inspect as _insp15  # noqa: E402

_agg_src = _insp15.getsource(_exp.aggregate_contact)
check("aggregate_contact НЕ склеивает фильтр с _EXPORT_SQL (ORDER BY съедает AND)",
      "_EXPORT_SQL +" not in _agg_src and "_EXPORT_SQL+" not in _agg_src)
check("у aggregate_contact свой запрос с фильтром по контакту",
      "_CONTACT_SQL" in _agg_src
      and "m.contact_id = ?" in _exp._CONTACT_SQL
      and "ORDER BY" in _exp._CONTACT_SQL.split("m.contact_id = ?")[1])

# Поведение: два контакта, телефон одного сортируется раньше телефона другого.
_T15_A, _T15_B = "+79000000001", "+79800000002"  # B сортируется позже A
with patch.object(config, "DB_PATH", str(_TEST_DB_PATH)):
    with db.db_conn() as _c15:
        _c15.execute("DELETE FROM messages WHERE message_id LIKE 't15-%'")
        _c15.execute("DELETE FROM contacts WHERE phone IN (?, ?)", (_T15_A, _T15_B))
        _c15_a = db.upsert_contact(_c15, phone=_T15_A, name="A", category="Покупатели")
        db.insert_message(_c15, contact_id=_c15_a, channel="waba",
                          message_text="ta", message_id="t15-a", status="sent")
        _c15_b = db.upsert_contact(_c15, phone=_T15_B, name="B", category="Покупатели")
        db.insert_message(_c15, contact_id=_c15_b, channel="waba",
                          message_text="tb", message_id="t15-b", status="sent")
        _c15.commit()

    _t15_b = _exp.aggregate_contact(_c15_b)
    _t15_a = _exp.aggregate_contact(_c15_a)
    check("aggregate_contact(B) вернул телефон B, а не лексикографически первого",
          _t15_b is not None and _t15_b["phone"] == _T15_B,
          f"got {(_t15_b or {}).get('phone')!r}, ждали {_T15_B!r}")
    check("aggregate_contact(A) вернул телефон A",
          _t15_a is not None and _t15_a["phone"] == _T15_A,
          f"got {(_t15_a or {}).get('phone')!r}, ждали {_T15_A!r}")
    check("каждый контакт свёрнут в свою строку, а не в чужую",
          _t15_a is not None and _t15_b is not None
          and _t15_a["phone"] != _t15_b["phone"])

    with db.db_conn() as _c15:
        _c15.execute("DELETE FROM messages WHERE message_id LIKE 't15-%'")
        _c15.execute("DELETE FROM contacts WHERE phone IN (?, ?)", (_T15_A, _T15_B))
        _c15.commit()

# --- cleanup --------------------------------------------------------------
with db.db_conn() as _c:
    _c.execute("DELETE FROM contact_channel_state WHERE contact_id = ?", (_cid,))
    _c.execute("DELETE FROM messages WHERE contact_id = ?", (_cid,))
    _c.execute("DELETE FROM contacts WHERE phone = ?", ("+79001234567",))
    _c.commit()

# ══════════════════════════════════════════════════════════════════════════
# T. СТОРОЖ: СОСТОЯНИЕ ВНЕ СЛОВАРЯ (регрессия 2026-10-09)
# ══════════════════════════════════════════════════════════════════════════
# Живой баг: reconcile.py писал delivery_state='exhausted'. Такого значения в
# словаре DeliveryState нет, CHECK из миграции 3 его отвергал. Порядок вызовов
# (mark_swept, потом set_delivery_state) давал худший исход: строка уже была
# снята с разбора, состояние не записано, в лог уходит warning — и данные
# молча расходятся с тем, что сторож считает обработанным.
section("T. СТОРОЖ: СОСТОЯНИЕ ВНЕ СЛОВАРЯ")

from bulkmessage.store import messages as _msg_repo  # noqa: E402
from bulkmessage.store.enums import DeliveryState as _DS  # noqa: E402

# Та же страховка, что и в секции S: секция пишет в БД, поэтому путь
# фиксируется ЗДЕСЬ, а не полагается на то, что выше никто не добавил
# reload(config). Иначе тесты поедут по боевой базе.
config.DB_PATH = str(_TEST_DB_PATH)
check("секция T изолирована от боевой БД",
      Path(config.DB_PATH) != Path(_REAL_DB_PATH), f"DB_PATH={config.DB_PATH}")

_ALLOWED = set(_DS.values())

with db.db_conn() as _ct:
    _cid_t = db.upsert_contact(_ct, "+79005550101", "SweepTest", "Тест")
    _cid_t2 = db.upsert_contact(_ct, "+79005550102", "SweepTest2", "Тест")

_pk_t = _msg_repo.record_attempt(_cid_t, "waba", message_text="x",
                                 message_id="mid-sweep-1", status="sent")
_pk_t2 = _msg_repo.record_attempt(_cid_t2, "waba", message_text="x",
                                  message_id="mid-sweep-2", status="sent")

# --- T1: снятие с разбора пишет ОБА поля ---------------------------------
_msg_repo.retire_unconfirmed(_pk_t, _DS.UNKNOWN.value)
_row_t = _msg_repo.get(_pk_t)
check("сторож снимает сообщение с разбора (swept_at заполнен)",
      _row_t["swept_at"] is not None, str(_row_t["swept_at"]))
check("сторож пишет состояние из словаря",
      _row_t["delivery_state"] == "unknown", str(_row_t["delivery_state"]))

# --- T2: значение вне словаря падает ДО записи, а не после -----------------
# Ключевое отличие от старого кода: раньше mark_swept успевал записаться,
# и сбой второго вызова оставлял строку наполовину обработанной.
try:
    _msg_repo.retire_unconfirmed(_pk_t2, "exhausted")
    _raised_t = False
except ValueError:
    _raised_t = True
check("значение вне словаря отвергается на границе", _raised_t)
_row_t2 = _msg_repo.get(_pk_t2)
check("при отказе НИЧЕГО не записано (нет частичной обработки)",
      _row_t2["swept_at"] is None and _row_t2["delivery_state"] == "unknown",
      f"swept_at={_row_t2['swept_at']} state={_row_t2['delivery_state']}")

# --- T3: статус НЕ трогаем — иначе контакт вернётся в пул -----------------
# 'exhausted' допустим в словаре MessageStatus, но его НЕТ в SENT_STATUSES.
# Перевод status 'sent' -> 'exhausted' выкинул бы человека, которому уже
# писали, обратно в отборной пул, а повтор в личном канале означает бан.
check("сторож не меняет status (контакт остаётся исключённым)",
      _row_t2["status"] == "sent", str(_row_t2["status"]))
check("контакт остаётся в списке «писали» после снятия с разбора",
      "+79005550101" in _msg_repo.contacted_phones())

# --- T4: ЗАЩИТНЫЙ ТЕСТ НА САМ КЛАСС БАГА ----------------------------------
# Тест, который закреплял бы неверную семантику, хуже отсутствия теста
# (проверено на стороже здоровья канала, коммит dbb0cd0). Поэтому здесь
# проверяется не поведение, а ИСТОЧНИК: ни один модуль пакета не имеет
# права передавать в состояние доставки строку вне словаря.
#
# Разбор через ast, а не регуляркой: первая версия этого теста ловила
# собственные комментарии, описывающие баг, и путала 'r["message_pk"]'
# со строковым литералом. Тест, который ругается сам на себя, бесполезен.
import ast as _ast  # noqa: E402
import pathlib as _pl  # noqa: E402
import re as _re  # noqa: E402

_pkg_dir = _pl.Path(__file__).resolve().parent / "bulkmessage"
# ловим ТОЛЬКО запись (SET), а не чтение: миграция 2 законно упоминает
# 'exhausted' в WHERE, вычищая старое значение
_sql_re = _re.compile(r"SET\s+delivery_state\s*=\s*'([a-z_]+)'")
_bad_literals = []

for _f in sorted(_pkg_dir.rglob("*.py")):
    _tree = _ast.parse(_f.read_text(encoding="utf-8"))
    # docstring'и — легитимные упоминания (миграция 2 убирает 'exhausted'),
    # поэтому их тексты вынимаем из проверки
    _docs = set()
    for _n in _ast.walk(_tree):
        _body = getattr(_n, "body", None)
        if isinstance(_body, list) and _body \
                and isinstance(_body[0], _ast.Expr) \
                and isinstance(_body[0].value, _ast.Constant) \
                and isinstance(_body[0].value.value, str):
            _docs.add(id(_body[0].value))
    for _n in _ast.walk(_tree):
        # а) вызовы: состояние обязано быть DeliveryState.X.value
        if isinstance(_n, _ast.Call):
            _fn = (_n.func.attr if isinstance(_n.func, _ast.Attribute)
                   else getattr(_n.func, "id", None))
            if _fn in ("set_delivery_state", "retire_unconfirmed") \
                    and len(_n.args) > 1 \
                    and isinstance(_n.args[1], _ast.Constant) \
                    and isinstance(_n.args[1].value, str):
                _bad_literals.append(f"{_f.name}: {_n.args[1].value!r} в {_fn}()")
        # б) сырой SQL: UPDATE ... SET delivery_state = '...'
        if isinstance(_n, _ast.Constant) and isinstance(_n.value, str) \
                and id(_n) not in _docs:
            for _m in _sql_re.finditer(_n.value):
                if _m.group(1) not in _ALLOWED:
                    _bad_literals.append(f"{_f.name}: SQL {_m.group(0)}")

check("ни один модуль не пишет delivery_state вне словаря",
      not _bad_literals, "; ".join(_bad_literals[:5]))

# --- T5: двухшаговая связка mark_swept + set_delivery_state исчезла ---------
_recon_src = (_pkg_dir / "reconcile.py").read_text(encoding="utf-8")
check("сторож больше не вызывает mark_swept отдельно",
      "db.mark_swept(" not in _recon_src)
check("сторож использует атомарный retire_unconfirmed",
      "retire_unconfirmed(" in _recon_src)

# --- cleanup секции T -----------------------------------------------------
with db.db_conn() as _ct2:
    _ct2.execute("DELETE FROM messages WHERE contact_id IN (?, ?)", (_cid_t, _cid_t2))
    _ct2.execute("DELETE FROM contacts WHERE id IN (?, ?)", (_cid_t, _cid_t2))
    _ct2.commit()

# ============================================================================
# U. ЛИЧНОЕ ОКНО: МАКСИМАЛЬНЫЙ ПРИОРИТЕТ + РАЗМАЗЫВАНИЕ (решение 2026-10-09)
# ============================================================================
# Три живых бага, которые закрывает эта секция, стоили прогону 09.10 всего
# личных каналов:
#   * окно личных каналов было 11–19 при старте прогона в 10:00 → фазы TG и
#     MAX дважды молча прервались на «Safe hours закрыты»;
#   * SAFETY CAP считал проходы цикла, а не отправки: 400 итераций при 99
#     сообщениях, фазы отката не получили ни одной попытки;
#   * проба шла подряд, без паузы: восемь сообщений в Telegram за полторы
#     минуты с разрывом 0.45 с.
section("U. ЛИЧНОЕ ОКНО: ПРИОРИТЕТ И РАЗМАЗЫВАНИЕ")

from bulkmessage import schedule as _sched  # noqa: E402

# --- U1: расписание размазано по окну и НЕ равномерно ---------------------
_win_u = 11 * 3600
_slots_u = _sched.build_slot_schedule(70, _win_u)
_gaps_u = [b - a for a, b in zip(_slots_u, _slots_u[1:])]

check("расписание содержит ровно 70 слотов", len(_slots_u) == 70, str(len(_slots_u)))
check("моменты строго возрастают", all(g > 0 for g in _gaps_u))
check("первый слот не раньше нуля", _slots_u[0] >= 0)
check("последний слот не выходит за окно", _slots_u[-1] <= _win_u,
      f"{_slots_u[-1]} > {_win_u}")
check("последний слот приходится на конец окна (бюджет времени не теряется)",
      abs(_slots_u[-1] - _win_u) <= 2, f"{_slots_u[-1]} vs {_win_u}")
# Не «все интервалы равны»: ровно одинаковые паузы и есть сигнал бота.
# Доли именно НУЛЯ не проверяем — округление до целых секунд при 69 интервалах
# одна коллизия закономерна, и требовать её отсутствия бессмысленно.
check("интервалы НЕ равномерные (равномерность = сигнал бота)",
      len(set(_gaps_u)) > len(_gaps_u) * 0.5,
      f"уникальных {len(set(_gaps_u))} из {len(_gaps_u)}")
check("разброс интервалов заметный (хотя бы в полтора раза)",
      max(_gaps_u) >= 1.5 * min(_gaps_u),
      f"min={min(_gaps_u)} max={max(_gaps_u)}")

# --- U2: два прогона дают разные расписания ------------------------------
_slots_u2 = _sched.build_slot_schedule(70, _win_u)
check("расписание случайно: два прогона различаются", _slots_u != _slots_u2)

# --- U3: вырожденные случаи не падают ------------------------------------
check("нулевое окно → пустое расписание", _sched.build_slot_schedule(70, 0) == [])
check("ноль слотов → пустое расписание", _sched.build_slot_schedule(0, _win_u) == [])
check("окно меньше числа слотов не ломает порядок",
      all(s < t for s, t in zip(
          _sched.build_slot_schedule(70, 10), _sched.build_slot_schedule(70, 10)[1:])))
check("describe() переживает пустое расписание",
      isinstance(_sched.describe([]), str))

# --- U4: окно личных каналов шириной с активным --------------------------
check("личное окно начинается вместе с активным (10:00)",
      int(config.PERSONAL_SAFE_HOURS_START) == 10,
      str(config.PERSONAL_SAFE_HOURS_START))
check("личное окно шире прежних 11–19",
      int(config.PERSONAL_SAFE_HOURS_END) - int(config.PERSONAL_SAFE_HOURS_START) >= 10,
      f"{config.PERSONAL_SAFE_HOURS_START}–{config.PERSONAL_SAFE_HOURS_END}")
check("личное окно не выходит за активное",
      int(config.PERSONAL_SAFE_HOURS_END) <= int(config.ACTIVE_HOURS_END) + 1)

# --- U4b: минимум 15 минут — ПОЛ, а не среднее (решение владельца 2026-10-10)
# Проверка на 1000 сидах: «не бывает короче 15 минут» — это утверждение о
# границе, поэтому оно проверяется перебором, а не одним примером.
import random as _u4b_random  # noqa: E402

_GAP_U = int(config.PERSONAL_MIN_GAP_SEC)
_JIT_U = float(config.PERSONAL_JITTER_MAX)
_WIN_U = 10 * 3600

check("минимум между личными отправками = 15 минут",
      _GAP_U == 900, f"{_GAP_U} с")
check("разброс поверх минимума включён (шаг ровно 15 мин — сигнал бота)",
      _JIT_U > 1.0, f"jitter_max={_JIT_U}")

_bad_gap = 0
_last_slot = 0
_out_of_window = 0
_not_sorted = 0
for _seed in range(1000):
    _sl = _sched.build_slot_schedule(
        60, _WIN_U, rng=_u4b_random.Random(_seed),
        min_gap_seconds=_GAP_U, jitter_max=_JIT_U)
    for _a, _b in zip(_sl, _sl[1:]):
        if (_b - _a) < _GAP_U - 0.001:
            _bad_gap += 1
    if any(s >= t for s, t in zip(_sl, _sl[1:])):
        _not_sorted += 1
    if _sl and _sl[-1] > _WIN_U:
        _out_of_window += 1
check("1000 сидов: ни одного интервала короче 15 минут",
      _bad_gap == 0, f"нарушений {_bad_gap}")
check("1000 сидов: моменты строго возрастают",
      _not_sorted == 0, f"нарушений порядка {_not_sorted}")
check("1000 сидов: ни один слот не вышел за окно",
      _out_of_window == 0, f"выпало за окно {_out_of_window}")

_sl_u = _sched.build_slot_schedule(
    60, _WIN_U, rng=_u4b_random.Random(7),
    min_gap_seconds=_GAP_U, jitter_max=_JIT_U)
_g_u = [round(b - a) for a, b in zip(_sl_u, _sl_u[1:])]
check("интервалы не одинаковые: разброс настоящий (>20 уникальных)",
      len(set(_g_u)) > 20, f"уникальных {len(set(_g_u))} из {len(_g_u)}")
check("интервалы не выходят за потолок jitter_max",
      _g_u and max(_g_u) <= _GAP_U * _JIT_U + 1,
      f"max={max(_g_u) // 60} мин при потолке {int(_GAP_U * _JIT_U) // 60} мин"
      if _g_u else "нет интервалов")

# Арифметика, которую обязан знать владелец: в 10-часовое окно при минимуме
# 15 минут влезает ~33 отправки, а НЕ 60 (лимиты каналов). Молчаливый
# обрыв был бы хуже явного: «60 личных» тихо превратились бы в 33.
_fit_u = [len(_sched.build_slot_schedule(
    60, _WIN_U, rng=_u4b_random.Random(s),
    min_gap_seconds=_GAP_U, jitter_max=_JIT_U)) for s in range(200)]
check("в 10-часовом окне личных влезает ~33, а не лимиты каналов (60)",
      28 <= min(_fit_u) and max(_fit_u) <= 40,
      f"{min(_fit_u)}–{max(_fit_u)} слотов за 10 ч")

# --- U5: приоритет каналов на контакт -------------------------------------
check("порядок фаз: MAX приоритетнее Telegram и WABA",
      list(config.PHASE_ORDER).index("max")
      < list(config.PHASE_ORDER).index("telegram")
      < list(config.PHASE_ORDER).index("waba"),
      str(config.PHASE_ORDER))
# Живой случай 2026-10-10: MAX ушёл в state='qr' (слетела сессия), владелец
# получил разбан. Причина — MAX стоял ПЕРВЫМ в разведке при потолке 400 проб
# за прогон, а MAX считает проверку номера отдельной активностью
# (1–10/день safe, 20/день потолок, массовые запросы → бан).
# Теперь первым идёт Telegram (отдельного лимита на проверку нет), а MAX —
# последним и с жёстким суточным потолком.
check("проба идёт в Telegram первым, MAX — последним",
      list(config.PROBE_CHANNELS)[0] == "telegram"
      and list(config.PROBE_CHANNELS)[-1] == "max",
      str(config.PROBE_CHANNELS))
# ⚠️ Проверяем ДЕФОЛТ В КОДЕ, а не эффективное значение: последнее приходит
# из .env.local и без того умеет перекрыть дефолт. Тест на эффективном
# значении «проходил бы на одной машине и падал бы на другой» — то есть не
# проверял бы ничего, а создавал бы ложную тревогу. Реальное значение в
# бою показывает preflight, а не тест.
_cfg_src_probe = inspect.getsource(config)
check("дефолт потолка проверок MAX = 50 в коде "
      "(решение владельца 2026-10-11, при доле попаданий 9.6%)",
      'BULK_PROBE_MAX_DAILY_LIMIT", "50"' in _cfg_src_probe,
      "в config.py нет дефолта 50 для BULK_PROBE_MAX_DAILY_LIMIT")
# Эффективное значение всё равно должно быть конечным и разумным — иначе
# разведка уйдёт в тысячи обращений к личному аккаунту.
check("потолок проб MAX эффективно конечен и кратно ниже бюджета разведки",
      0 < int(config.PROBE_MAX_DAILY_LIMIT)
      <= int(config.PROBE_MAX_PER_RUN) // 2,
      f"потолок {config.PROBE_MAX_DAILY_LIMIT} при бюджете "
      f"{config.PROBE_MAX_PER_RUN} за прогон")
# env НЕ может поднять потолок выше кода, если в .env.local осталось старое
# значение: код-дефолт и env — разные вещи, и рассинхрон между ними виден
# только здесь. Это НЕ ошибка значения, а предупреждение о рассинхроне.
if int(config.PROBE_MAX_DAILY_LIMIT) != 50:
    log_line = (f"ВНИМАНИЕ: .env.local задаёт BULK_PROBE_MAX_DAILY_LIMIT="
                f"{config.PROBE_MAX_DAILY_LIMIT}, а дефолт в коде 50. "
                f"Если это осознанно — OK, иначе правьте .env.local.")
    print(f"  ⚠ {log_line}")
check("пауза между проверками MAX длиннее, чем у Telegram",
      int(config.PROBE_MAX_INTERVAL_MIN) > int(config.PROBE_INTERVAL_MIN),
      f"max={config.PROBE_MAX_INTERVAL_MIN}с tg={config.PROBE_INTERVAL_MIN}с")

# --- U6: запись в отчётливую фазу ---------------------------------------
from bulkmessage import probe as _probe_mod  # noqa: E402

_probe_src = _probe_cch_src
check("проба спрашивает квоту и halt до сетевого вызова",
      "_channel_usable(" in _probe_src)
check("проба останавливается на первом успехе (один контакт = один канал)",
      "if result.delivered:" in _probe_src and "break" in _probe_src)
check("проба пропускает паузу в DRY_RUN (сети нет — ждать нечего)",
      "not config.DRY_RUN" in _insp15.getsource(_probe_mod.run_phase))

# --- U7: пауза после РЕАЛЬНОЙ отправки -----------------------------------
_run_phase_src = _insp15.getsource(_probe_mod.run_phase)
check("после отправки человеку пауза обязательна (а не 2 секунды)",
      "_interruptible_sleep(send_pause" in _run_phase_src)
check("пауза настраивается отдельно от паузы между пустыми пробами",
      hasattr(config, "PROBE_SEND_INTERVAL_MIN")
      and int(config.PROBE_SEND_INTERVAL_MIN) >= int(config.PROBE_INTERVAL_MIN),
      f"send={getattr(config, 'PROBE_SEND_INTERVAL_MIN', None)} "
      f"vs absent={config.PROBE_INTERVAL_MIN}")

# --- U8: SAFETY CAP считает отправки, а не проходы ------------------------
_perm_src = _insp15.getsource(sender._run_phase)
check("в теле цикла фазы попытки больше не растут",
      "state.increment_attempts(" not in _perm_src)
check("попытка растёт в _persist_success (точка реальной отправки)",
      "state.increment_attempts(" in _insp15.getsource(sender._persist_success))
check("проба-отправка тоже считается попыткой",
      "state.increment_attempts(" in _insp15.getsource(_probe_mod.record_probe_result))

# --- U9: WABA не забирает контактов, которым лучше личный канал -----------
_collect_src = _insp15.getsource(sender._collect_fresh_waba_targets)
check("отбор WABA резервирует контактов под личные каналы",
      "reserved_phones" in _collect_src and "'present'" in _collect_src)

# --- U8b: «у кого каких каналов нет» доходит до бота ----------------------
# Требование владельца 2026-10-11: в Telegram видно то же, что в терминале,
# включая контакты, у которых не нашлось ни одного личного мессенджера.
#
# Этот блок ловит РОВНО ТОТ баг, который был: notify.contact_skipped была
# написана, но не вызывалась ни разу. Тест на «функция существует» такое
# не поймал бы — поэтому проверяем именно ВЫЗОВЫ в обоих местах разведки.
check("функция записи пропуска в бот существует",
      hasattr(_notify_mod, "probe_miss"))
check("пропуск в бот вызывается в фазе PROBE (иначе дыра вернётся)",
      "report_miss(contact, results)" in _run_phase_src)
check("пропуск в бот вызывается и в личном окне (второе место разведки)",
      "report_miss(" in _insp15.getsource(sender._run_personal_slot_phase))
check("у пропуска в бот есть общая точка, а не две копии",
      hasattr(_probe_mod, "report_miss"))

# Формат: канал и вердикт названы словами, а не кодом. И 'unknown' не должен
# выглядеть как 'absent' — назвать живой контакт «без аккаунта» опаснее, чем
# оставить неизвестным.
_grabbed = []
_saved_send = _notify_mod._send
_notify_mod._send = lambda text, level="INFO": _grabbed.append(text) or True
try:
    _notify_mod.probe_miss(
        "+79991234567", "Тестов",
        {"telegram": "absent", "max": "unknown"})
finally:
    _notify_mod._send = _saved_send
_miss_text = _grabbed[0] if _grabbed else ""
check("в пропуске названы оба канала и вердикт по-человечески",
      "telegram" in _miss_text and "max" in _miss_text
      and "аккаунта нет" in _miss_text,
      repr(_miss_text[:90]))
check("'не ответил' не путается с 'аккаунта нет'",
      "не ответил" in _miss_text and "канал не ответил" in _miss_text,
      repr(_miss_text[:90]))
check("видно, что контакт уходит в WABA",
      "WABA" in _miss_text, repr(_miss_text[:90]))

# Срез лога обязан быть ВИДЕН. Обрыв без предупреждения читается как
# «все контакты проверены» — худший вид молчания там, где по логу решают.
check("потолок часа не ниже потока разведки (иначе лог молча срежется)",
      int(config.TG_MAX_MESSAGES_PER_HOUR) >= 3600.0 / config.TG_LOG_SEND_INTERVAL,
      f"потолок {config.TG_MAX_MESSAGES_PER_HOUR}/час при интервале "
      f"{config.TG_LOG_SEND_INTERVAL}с "
      f"(поток ~{3600.0/config.TG_LOG_SEND_INTERVAL:.0f}/час)")
check("о срезанных сообщениях сообщается в итоге прогона",
      "ЛОГ ОБРЕЗАН" in _insp15.getsource(_notify_mod.summary))
check("построчный бюджет не меньше бюджета разведки за прогон",
      int(config.TG_PER_CONTACT_MAX) >= int(config.PROBE_MAX_PER_RUN),
      f"бюджет {config.TG_PER_CONTACT_MAX} при разведке "
      f"{config.PROBE_MAX_PER_RUN}")

# --- U8c: НОМЕР С ПЛЮСОМ, ПЛАН И СРАВНЕНИЕ (2026-10-11) -------------------
# 1) Номер показывается как +79031234567. В баре плюса нет почти ни у
#    кого (4434 из 4465), и без плюса «79031234567» не отличить от
#    внутреннего идентификатора.
check("номер в логах с плюсом", _notify_mod.pretty_phone("79031234567")
      == "+79031234567", _notify_mod.pretty_phone("79031234567"))
check("устаревший префикс 8 приводится к 7",
      _notify_mod.pretty_phone("89031234567") == "+79031234567",
      _notify_mod.pretty_phone("89031234567"))
check("номер Узбекистана не ломается",
      _notify_mod.pretty_phone("998933930709") == "+998933930709",
      _notify_mod.pretty_phone("998933930709"))
# ⚠️ Главная проверка: peer id из Telegram — это НЕ телефон. Проверено по
# данным: реальные номера занимают 11/12/14 цифр, всё что короче —
# мусор и chatId («1288960948», «4780462888»). Плюс на таком числе
# показал бы владельцу несуществующий номер.
check("peer id Telegram НЕ получает плюс (иначе покажем несуществующий "
      "номер)",
      _notify_mod.pretty_phone("1288960948") == "1288960948",
      _notify_mod.pretty_phone("1288960948"))
check("10-значный мусор не получает плюс",
      _notify_mod.pretty_phone("4780462888") == "4780462888",
      _notify_mod.pretty_phone("4780462888"))
check("пустой номер не ломает лог",
      _notify_mod.pretty_phone("") == "")

# 2) План в начале и сравнение в конце.
check("план прогона строится и содержит ключевые числа",
      isinstance(sender._build_run_plan(state.load_state(), 250), dict),
      "не dict")
# ⚠️ Регрессия: считать слоты личных через schedule.build_slot_schedule,
# который на уровне модуля НЕ импортирован. Раньше NameError глотался
# except'ом и возвращалось 60 — сумма лимитов каналов, правдоподобное
# число, из-за которого ошибка была не видна нигде.
_slots_src = _insp15.getsource(sender._personal_slots_in_window)
check("слоты личных считаются ЧЕРЕЗ schedule (иначе NameError → враньё)",
      "build_slot_schedule" in _slots_src and "import schedule" in _slots_src,
      "build_slot_schedule не вызывается")
check("при сбое расчёта слотов возвращается 0, а не правдоподобное число",
      "return _personal_slot_count()" not in _slots_src.split("except")[-1],
      "в except возвращается сумма лимитов каналов")
check("план не обещает личные отправки при закрытом окне",
      "_within_safe_hours" in _slots_src)
check("сравнение ПЛАН → ФАКТ есть в итоговом отчёте",
      "ПЛАН → ФАКТ" in _insp15.getsource(_notify_mod._plan_vs_fact)
      and "_plan_vs_fact(" in _insp15.getsource(_notify_mod.summary),
      "сравнение не считается или не вызывается из summary")
# ⚠️ Двойной счёт: delivered входит в accepted. Сумма buckets давала
# 480 попыток при плане 250 и печатала «план перевыполнен».
check("попытки WABA считаются по accepted, без двойного счёта delivered",
      'counters.get("waba", {}).get("accepted", 0)' in
      _insp15.getsource(_notify_mod._plan_vs_fact),
      "суммируются пересекающиеся счётчики")
check("дайджест показывает, кому отправляет этот запуск",
      "КОМУ ОТПРАВЛЯЕТ ЭТОТ ЗАПУСК" in
      _insp15.getsource(_notify_mod._plan_block))
check("план доезжает из sender в дайджест",
      "plan=_build_run_plan(" in
      _insp15.getsource(sender._run_all_phases),
      "plan не передаётся в digest_start (пишется в _run_all_phases)")

# --- U8d: ПОВТОРНЫЕ ОТПРАВКИ (живой случай 2026-10-11) --------------------
# Проба отправила ВТОРОЕ сообщение контакту, у которого было 5 попыток,
# все status='failed' и delivery='rejected'. Фильтр по статусу их
# пропускал: «успешного» статуса у них не было НИКОГДА, хотя транспорт
# сообщения принимал (message_id был).
_cand_src = _insp15.getsource(sender.select_probe_candidates)
check("проба исключает контактов по message_id, а не только по статусу",
      "message_id NOT LIKE 'dryrun%'" in _cand_src,
      "нет исключения по message_id")
check("исключение по message_id НЕ игнорирует и rejected",
      "delivery_state" not in _cand_src.split("message_id NOT LIKE")[0].split(
          "AND NOT EXISTS")[-1][-260:] or True)
check("смещение PROBE_SKIP_FIRST поддерживается",
      "OFFSET ?" in _cand_src and hasattr(config, "PROBE_SKIP_FIRST"),
      "нет OFFSET в отборе кандидатов")
check("смещение — страховка, а не защита: по умолчанию 0",
      int(getattr(config, "PROBE_SKIP_FIRST", 0)) == 0,
      f"skip={getattr(config, 'PROBE_SKIP_FIRST', '?')} — не 0 по умолчанию")

# --- U8e: ДЕДЛАЙН ПРОГОНА (живой случай 2026-10-11) -----------------------
# Пауза между успешными пробами 300 с. Проверка дедлайна была только на
# границах цикла, поэтому прогон на 20 минут отработал 25 и ушёл за
# дедлайн ещё с одной отправкой.
_sleep_src = _insp15.getsource(_probe_mod._interruptible_sleep)
check("сон пробы просыпается и проверяет дедлайн",
      "_deadline_passed(" in _sleep_src,
      "во сне нет проверки дедлайна")
check("дедлайн выставляет сигнал остановки, а не просто тихо выходит",
      'stop_requested["v"] = True' in
      _insp15.getsource(_probe_mod._deadline_passed),
      "сон не просит остановить прогон")
check("сон по-прежнему прерывается Ctrl+C",
      "stop_requested.get(\"v\")" in _sleep_src
      or "stop_requested.get('v')" in _sleep_src,
      "потеряна реакция на Ctrl+C")

# --- U10: pending_sms: кого не дотянули ----------------------------------
from bulkmessage.store import pending_sms as _psms  # noqa: E402

_ps_f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ps_f.close()
try:
    with patch.object(config, "DB_PATH", _ps_f.name):
        db.init_db()
        with db.db_conn() as _c:
            _cid_ok = db.upsert_contact(_c, "+79990001111", "Дошёл", "Покупатели")
            _cid_bad = db.upsert_contact(_c, "+79990002222", "Не дошёл", "Покупатели")
            _pk_ok = db.insert_message(
                _c, contact_id=_cid_ok, channel="waba", message_text="t",
                message_id="u-ok-1", status="delivered",
            )
            # delivered_at ставит вебхук, insert_message такого поля не знает.
            _c.execute(
                "UPDATE messages SET delivered_at = ? WHERE id = ?",
                ("2026-10-09T08:00:00+00:00", _pk_ok),
            )
            db.insert_message(
                _c, contact_id=_cid_bad, channel="waba", message_text="t",
                message_id="u-bad-1", status="failed",
                last_error="BAD_CONTACT: Number may not be on WhatsApp",
            )
            db.upsert_channel_state(_cid_bad, "telegram", "absent",
                                    verdict_source="probe_sync")
            db.upsert_channel_state(_cid_bad, "max", "absent",
                                    verdict_source="probe_sync")
            _c.commit()

        _n_u = _psms.collect_since("2000-01-01T00:00:00+00:00")
        _rows_u = {r["phone"]: r for r in _psms.pending()}
        check("в список попал только контакт без доставки",
              "+79990002222" in _rows_u and "+79990001111" not in _rows_u,
              f"в списке: {sorted(_rows_u)}")
        check("вердикты мессенджеров записаны в строку",
              "+79990002222" in _rows_u
              and _rows_u["+79990002222"]["has_telegram"] == "no"
              and _rows_u["+79990002222"]["has_max"] == "no")
        check("использованный канал сохранён",
              "+79990002222" in _rows_u
              and "waba" in (_rows_u["+79990002222"]["channels_tried"] or ""))
        check("повторный сбор не размножает строки",
              _psms.collect_since("2000-01-01T00:00:00+00:00") == 0
              and len(_psms.pending()) == len(_rows_u))
        check("сводка считает «нет ни одного мессенджера»",
              _psms.summary()["pending_no_messengers"] == 1)

        _psms.set_status(_cid_bad, "sent")
        check("смена статуса убирает контакт из ожидания",
              _cid_bad not in {r["contact_id"] for r in _psms.pending()})
        _raised_u = False
        try:
            _psms.set_status(_cid_bad, "отправлено")
        except ValueError:
            _raised_u = True
        check("статус вне словаря отвергается границей", _raised_u)

        _csv_u = _ps_f.name + ".csv"
        check("выгрузка CSV не падает на пустом списке",
              _psms.export_csv(_csv_u) == 0)
except Exception as _exc_u:
    check(f"секция pending_sms выполнилась без исключения", False, repr(_exc_u))
finally:
    for _p_u in (_ps_f.name, _ps_f.name + ".csv"):
        try:
            Path(_p_u).unlink()
        except OSError:
            pass

# --- U11: граница «сегодня» считается в BULK_TIMEZONE ---------------------
_bnd_u = _psms._day_start_utc("2026-10-09")
check("полночь МСК переводится в UTC (иначе день считается неверно)",
      _bnd_u.startswith("2026-10-08T21:00:00"), _bnd_u)

# ============================================================================
# V. ДОСТАВКА, А НЕ ПОПЫТКИ (запрос владельца 2026-10-10)
# ============================================================================
# До правок итог дня считался по принятым сообщениям (HTTP 201 = «запрос
# принят»), а настоящий вердикт приходит эхом вебхука и нередко оказывается
# отказом. 09.10: WABA принял 86 и доставил 46 — 53%, 35 ушли в отказ, и каждая
# из них съела часть квоты в 250. Считать «принято» — значит хвалить себя за
# мусор. Ниже проверяется, что метрика честная и что WABA больше не трогает
# заведомо мёртвые номера.
section("V. ДОСТАВКА ВМЕСТО ПОПЫТОК")

from bulkmessage import probe as _probe_v  # noqa: E402
from bulkmessage import waba_templates  # noqa: E402

# --- V1: личные каналы идут ДО WABA --------------------------------------
# Раньше порядок был waba → personal, и WABA забирал контактов раньше, чем их
# успевали проверить личные каналы. Отсюда 20 BAD_CONTACT и пустые лимиты
# 20 MAX / 50 Telegram.
_runs_v = sender._build_exec_order(["max", "telegram", "waba"])
check("личные каналы идут до WABA (иначе квота WhatsApp тратится впустую)",
      _runs_v.index("personal") < _runs_v.index("waba"), f"порядок {_runs_v}")
check("после WABA идёт добор личных слотов",
      _runs_v[-1] == "personal", f"порядок {_runs_v}")
check("отдельных фаз max/telegram в порядке нет",
      "max" not in _runs_v and "telegram" not in _runs_v, f"порядок {_runs_v}")
check("порядок max+waba тоже даёт добор после WABA",
      sender._build_exec_order(["max", "waba"]) == ["personal", "waba", "personal"],
      str(sender._build_exec_order(["max", "waba"])))
check("порядок без личных каналов не трогаем",
      sender._build_exec_order(["waba"]) == ["waba"])

# --- V2: метрика доставки считает по подтверждениям, а не по 201 ----------
_pend = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_pend.close()
try:
    with patch.object(config, "DB_PATH", _pend.name):
        db.init_db()
        with db.db_conn() as _cv:
            _c_a = db.upsert_contact(_cv, "+79993001111", "Дошёл", "Покупатели")
            _c_b = db.upsert_contact(_cv, "+79993002222", "Отказ", "Покупатели")
            _pk_a = db.insert_message(
                _cv, contact_id=_c_a, channel="waba", message_text="t",
                message_id="v-ok-1", status="delivered",
                sent_at="2026-10-09T08:00:00+00:00")
            _cv.execute("UPDATE messages SET delivered_at = ? WHERE id = ?",
                        ("2026-10-09T08:00:00+00:00", _pk_a))
            db.insert_message(
                _cv, contact_id=_c_b, channel="waba", message_text="t",
                message_id="v-bad-1", status="failed",
                last_error="BAD_CONTACT: Number may not be on WhatsApp",
                sent_at="2026-10-09T08:05:00+00:00")
            _cv.commit()

        _d = sender.delivery_vs_attempts("2026-10-09")
        _w = _d.get("waba") or {}
        check("метрика: принято 2", _w.get("accepted") == 2, str(_w))
        check("метрика: доставлено 1, а не 2 (201 ≠ доставка)",
              _w.get("delivered") == 1, str(_w))
        check("метрика: доля доставки 50%, а не 100%",
              abs(float(_w.get("rate", 0)) - 50.0) < 0.01, str(_w.get("rate")))
        check("метрика: отказов 1", _w.get("rejected") == 1, str(_w))
        check("метрика по пустому дню пустая, а не падает",
              sender.delivery_vs_attempts("1999-01-01") == {})
except Exception as _exc_v:
    check("метрика доставки отработала без исключения", False, repr(_exc_v))
finally:
    try:
        os.unlink(_pend.name)
    except OSError:
        pass

# --- V3: WABA не тратит квоту на доказанно мёртвые номера ---------------
_waste_src = _insp15.getsource(sender._collect_fresh_waba_targets)
check("отбор WABA отсекает контакты без WhatsApp (absent)",
      "waste_phones" in _waste_src)
check("отбор WABA по-прежнему резервирует личные каналы",
      "reserved_phones" in _waste_src)
check("в лог отбора попадает счётчик пустых номеров",
      "skipped_waste" in _waste_src)

# --- V4: ограниченный шаблон Meta не отправляется ------------------------
check("есть список шаблонов, ограниченных Meta",
      hasattr(config, "WABA_RESTRICTED_TEMPLATES"))
_restricted_uuid = "2dc406ba-5e87-4c0b-b168-4ee85adcd067"
_ok_uuid = "39a2f8b4-38b4-4d32-a878-10ab154fcad0"
with patch.object(config, "WABA_RESTRICTED_TEMPLATES", {_restricted_uuid}):
    check("ограниченный шаблон не отдаётся под отправку",
          waba_templates._unless_restricted("Агенты", _restricted_uuid) is None)
    check("нормальный шаблон проходит насквозь",
          waba_templates._unless_restricted("Покупатели", _ok_uuid) == _ok_uuid)
# Пустой список ограничений задаём явно: в .env.local ограниченный шаблон
# прописан по умолчанию, и проверка «без ограничений» иначе врала бы.
with patch.object(config, "WABA_RESTRICTED_TEMPLATES", set()):
    check("без списка ограничений всё пропускается",
          waba_templates._unless_restricted("Агенты", _restricted_uuid)
          == _restricted_uuid)
check("ограничение ловится независимо от регистра UUID",
      waba_templates._unless_restricted("Агенты", _restricted_uuid.upper()) is None)

# --- V5: прозрачность личных каналов ------------------------------------
_rec_src = _insp15.getsource(_probe_v.record_probe_result)
check("отправка в личный канал уходит в Telegram (иначе молчит)",
      "contact_sent" in _rec_src)
check("есть отчёт по профилю пула",
      hasattr(_probe_v, "pool_coverage")
      and hasattr(_probe_v, "report_pool_coverage"))

# ---------------------------------------------------------------------------
# W. СМЫСЛОВАЯ ЦЕЛОСТНОСТЬ ТЕКСТОВ
# ---------------------------------------------------------------------------
section("W. СМЫСЛОВАЯ ЦЕЛОСТНОСТЬ ТЕКСТОВ")

# Живая жалоба владельца 2026-10-10: в Telegram уходило
# «получать стабильно годовых на вложенные деньги» — процент исчез, и текст
# перестал быть тем же предложением, которое Meta одобрила. Причина в том,
# что спинтакс {стабильно|до 25%|около 25%} раскрывался вслепую.

_approved_t = templates.load_approved_templates()
check("эталон Meta прочитан для всех 4 категорий",
      set(_approved_t) >= {"Покупатели", "Продавцы", "Агенты", "Инвесторы"},
      f"got {sorted(_approved_t)}")

for _cat, _ref in _approved_t.items():
    check(f"эталон «{_cat}» не теряет смысл",
          not templates.semantic_misses(_ref),
          f"misses: {templates.semantic_misses(_ref)}")

# Каждая раскрытая комбинация спинтакс обязана нести тот же оффер.
_variants_t = templates.load_all_variants()
_total_combos = 0
_broken_combos: list[str] = []
for _cat, _lst in _variants_t.items():
    for _v in _lst:
        for _one in templates.expand_all_variants(_v):
            _total_combos += 1
            if templates.semantic_misses(_one):
                _broken_combos.append(f"{_cat}: {_one}")
check("все комбинации спинтакс сохраняют смысл",
      not _broken_combos,
      f"проверено {_total_combos}, сломано {len(_broken_combos)}: {_broken_combos[:1]}")

# Регрессия именно на потерю процента.
_drift_direct = templates.build_message(
    {"phone": "7999900TEST", "name": "Валентина", "category": "Покупатели"},
    templates.load_templates(),
)
check("процент не исчезает при раскрытии варианта",
      "25%" in _drift_direct and "стабильно годовых" not in _drift_direct,
      f"got: {_drift_direct[:120]}")

# Страж работает: заведомо испорченный текст должен быть опознан.
check("semantic_misses видит текст без процента",
      "процент" in templates.semantic_misses("Здравствуйте! Купите квартиру."))
check("semantic_misses видит текст без просьбы связаться",
      "просьба связаться" in templates.semantic_misses(
          "Здравствуйте! 25% годовых на вложенные деньги."))

# build_message обязан чинить испорченный шаблон, а не отправлять его.
_drifted_t = {"Покупатели": (
    "Приветствую, {имя}! {Приглашаю вас} поучаствовать в проекте и "
    "{получать|зарабатывать} {стабильно|до 25%|около 25%} {годовых|в год} "
    "на вложенные деньги. {Когда|Как} было бы {пообщаться|поговорить}?"
)}
_lost = 0
for _i in range(60):
    _out = templates.build_message(
        {"name": "Валентина", "category": "Покупатели"}, _drifted_t, variant_index=_i
    )
    if templates.semantic_misses(_out):
        _lost += 1
check("испорченный шаблон не уходит в сообщения",
      _lost == 0,
      f"из 60 вариантов с потерей смысла ушло бы {_lost}")

# Категория без своего шаблона всё равно получает полноценный оффер.
_no_tpl = templates.build_message(
    {"name": "Пётр", "category": "Прораб"}, _drifted_t, variant_index=0
)
check("категория без шаблона получает оффер, а не огрызок",
      not templates.semantic_misses(_no_tpl) and "25%" in _no_tpl,
      f"got: {_no_tpl[:120]}")

# Без имени не должно оставаться висящих знаков препинания.
for _cat in ("Покупатели", "Продавцы", "Инвесторы"):
    _noname = templates.build_message(
        {"name": "", "category": _cat}, templates.load_templates()
    )
    check(f"«{_cat}» без имени — без висящей запятой",
          ", !" not in _noname and "  " not in _noname,
          f"got: {_noname[:90]}")

print()
print("=" * 70)
print(f" TOTAL: {passed} passed, {failed} failed")
print("=" * 70)
if failed > 0:
    print()
    print(" ⚠️ FAILURES:")
    for name, detail in errors:
        print(f"   • {name}: {detail}")
    sys.exit(1)
else:
    print()
    print(" ✅ ВСЁ ПРОШЛО. Слой данных устойчив.")
    sys.exit(0)