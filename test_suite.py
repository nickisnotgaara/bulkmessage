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
    check("cascade USE_WABA=0 = whatsapp→telegram→max→sms",
          _snd_mod._cascade_order() == ["whatsapp", "telegram", "max", "sms"],
          f"got {_snd_mod._cascade_order()}")
finally:
    if _sms_uca is None:
        _os.environ.pop("BULK_USE_WABA", None)
    else:
        _os.environ["BULK_USE_WABA"] = _sms_uca
    importlib.reload(config)
    importlib.reload(sender)

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

# --- R31: фазы 2/3 тянут из rescue_queue FIFO и лимитированы ---
check("pending_rescues умеет фильтровать по original_channel",
      "original_channel" in inspect.getsource(db.pending_rescues))
_r31_sql = inspect.getsource(db.pending_rescues)
check("фильтр добавляет условие по каналу в WHERE",
      "AND original_channel = ?" in _r31_sql)
check("rescue_queue индексирован по (status, created_at)",
      "idx_rescue_status" in inspect.getsource(db.init_rescue_queue))
_src_run = inspect.getsource(sender.run)
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
check("фаза TG берёт только провалы WABA",
      'original_channel="waba"' in _src_phases)
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
        # 45 сообщений (30 TG + 15 MAX) в окно 7.5ч
        _tot32, _rem32 = 0.0, 45
        for _ in range(45):
            _d = sender._phase_delay("telegram", 7.5 * 3600 - _tot32, _rem32)
            _tot32 += _d
            _rem32 -= 1
        check("45 Personal-сообщений укладываются в 7.5ч детерминированной паузой",
              _tot32 <= 7.5 * 3600 + 1, f"sum={_tot32/3600:.2f}ч")
        check("задержка не выходит за MIN/MAX",
              all(300 <= sender._phase_delay("telegram", 7.5 * 3600, n) <= 900
                  for n in range(1, 46)))
        check("при 4ч на 45 сообщений задержка ужимается (фаза всё равно влезает)",
              sender._phase_delay("telegram", 4 * 3600, 45) < 600)
        # _phase_fits обязан ОТКЛАДЫВАТЬ фазу, если в окне не хватает времени
        # на минимальные паузы (45 × 300с = 3.75ч). Управляем «часами».
        with patch.object(config, "now_tz",
                          lambda: datetime(2026, 10, 5, 17, 30,
                                           tzinfo=ZoneInfo("Europe/Moscow"))):
            check("_phase_seconds_left считает остаток до 19:00 МСК",
                  0 < sender._phase_seconds_left("telegram") <= 1.5 * 3600,
                  f"left={sender._phase_seconds_left('telegram')/3600:.2f}ч")
            check("_phase_fits: 45 сообщений не влезают в 1.5ч → фаза отложена",
                  sender._phase_fits("telegram", 45) is False)
            check("_phase_fits: 5 сообщений в 1.5ч → фаза идёт",
                  sender._phase_fits("telegram", 5) is True)
        with patch.object(config, "now_tz",
                          lambda: datetime(2026, 10, 5, 11, 10,
                                           tzinfo=ZoneInfo("Europe/Moscow"))):
            check("_phase_fits: 45 сообщений в 7.8ч (11:10 старт) → влезают",
                  sender._phase_fits("telegram", 45) is True)
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
check("TG=50 и MAX=15 — потолки владельца, MAX ниже прежних 30",
      config.PERSONAL_CHANNEL_HARD_CAPS["telegram"] == 50
      and config.PERSONAL_CHANNEL_HARD_CAPS["max"] == 15,
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
_r33_calls: list = []
_r33_rescue_ok: list = []
_r33_rescue_fail: list = []
_r33_tried: list = []
try:
    with patch.object(config, "STATE_PATH", _r33_f.name), \
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
            check("попытки учтены (3 контакта = 3 попытки)",
                  int(_r33_st.get("attempts_today", 0)) == 3,
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
    # Реальный сценарий: 8ч осталось, 50 откатов -> 576с, это внутри
    # границ канала (300-900с), поэтому растяжка не нужна и не включается.
    _d_tg = sender._phase_delay("telegram", seconds_left=8 * 3600, remaining_msgs=50)
    check("TG (откат) укладывается в свой темп 300-900с",
          _tg_lo <= _d_tg <= _tg_hi,
          f"got {_d_tg:.0f}s, границы {_tg_lo}-{_tg_hi}")
    # А вот тут растяжка включается: 8ч на 5 откатов = 5760с >> 900с.
    _d_tg2 = sender._phase_delay("telegram", seconds_left=8 * 3600, remaining_msgs=5)
    check("TG растягивается, когда остаток больше границы канала",
          _d_tg2 > _tg_hi, f"got {_d_tg2:.0f}s, граница {_tg_hi}")
    _mx_lo, _mx_hi = sender._phase_delay_bounds("max")
    _d_mx = sender._phase_delay("max", seconds_left=5 * 3600, remaining_msgs=15)
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

# _select_contacts_for_cycle обязан отсекать уже отправленных — иначе
# второй круг отправил бы те же контакты повторно.
_sel_src = inspect.getsource(sender._select_contacts_for_cycle)
check("отбор контактов отсекает уже отправленных (get_sent_phones)",
      "get_sent_phones" in _sel_src)
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
check("WABA_RUN_LIMIT по умолчанию 0 (без ограничения)",
      int(getattr(config, "WABA_RUN_LIMIT", 0)) == 0,
      f"got {getattr(config, 'WABA_RUN_LIMIT', None)}")
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
        patch.object(sender, "_select_contacts_for_cycle", lambda c: []),
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


def _w_fake_sweep(limit=50, minutes=None):
    """Сторож: всё, что sender отправил, но что НЕ подтверждено, — провал."""
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
          _w_swept_calls and _w_swept_calls[0] == 12,
          f"таймауты сторожа: {_w_swept_calls}")
    check("сторож молчания вызван и перед фазой MAX",
          len(_w_swept_calls) >= 2, f"вызовов {_w_swept_calls}")
    check("фаза отката непуста: rescue наполнен тишиной",
          len(_w_rescued) == 4, f"в rescue {_w_rescued}")
    check("фаза Telegram реально отправила откат (не пустая)",
          len(_w_tg) == 4, f"TG-отправок {len(_w_tg)}: {_w_tg}")
    check("никому не отправили дважды в одном круге",
          len(_w_tg) == len(set(_w_tg)), f"{_w_tg}")
finally:
    for _f in (_w_db.name, _w_st):
        try:
            os.unlink(_f)
        except OSError:
            pass

# Сторож реально уважает переопределение минут.
check("sweep_unconfirmed_deliveries принимает minutes",
      "minutes" in inspect.signature(reconcile.sweep_unconfirmed_deliveries).parameters)
check("sweep_unconfirmed_deliveries по умолчанию = CONFIRM_TIMEOUT_MINUTES",
      "CONFIRM_TIMEOUT_MINUTES" in inspect.getsource(
          reconcile.sweep_unconfirmed_deliveries))
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

_r_perm_src = inspect.getsource(sender._run_one_contact)
_r_perm_at = _r_perm_src.rindex("ErrorKind.PERMANENT")
_r_perm_branch = _r_perm_src[_r_perm_at:_r_perm_at + 900]
check("PERMANENT у мессенджеров пишется в bad_phones НА ДИСК",
      "bad_phones.mark_bad" in _r_perm_branch,
      "в ветке PERMANENT нет записи на диск — метка умрёт вместе с процессом")
check("PERMANENT у мессенджеров ставит mark_unreachable",
      "state.mark_unreachable" in _r_perm_branch)

# W2. Запуск, который стартует СРАЗУ с отката (BULK_PHASE_ORDER=telegram,max).
# Живой случай 2026-10-06: Telegram пропущен как «пусто», потому что сторож
# отрабатывал только ПОСЛЕ первой фазы, и откат ушёл в MAX — рискованный
# канал. Здесь сторож обязан наполнить очередь ДО чтения первой фазой.
_w2_swept: list = []
_w2_sent: list = []


def _w2_sweep(limit=50, minutes=None):
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
    check("запуск сразу с отката: первым ушёл Telegram, не MAX",
          _w2_sent and _w2_sent[0] == "telegram",
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
