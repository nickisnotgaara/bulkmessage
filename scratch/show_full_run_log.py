"""Показать ВЕСЬ лог прогона в боте на реальных данных из базы.

Ничего не отправляет: перехватывает _send. Окно личных каналов принудительно
открыто, чтобы в плане стояли реальные слоты, а не ноль из-за полуночи.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bulkmessage import config, notify, sender  # noqa: E402

notify.reset()
notify.tglog.set_suppressed(True)   # страховка: ничего не уйдёт наружу

# Ставим время внутрь личного окна (12:00 МСК). Без этого слоты равны нулю:
# сейчас глуночь, окно 10–21 закрыто, и прогон личных отправок не делает.
# Подменяем ЧАСЫ, а не проверку окна — иначе расписание всё равно строится
# на «до завтра» и даёт неверные 60 слотов.
from datetime import datetime as _dt  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

config.now_tz = lambda: _dt(2026, 10, 12, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))


def _show(title, fn):
    print("\n" + "█" * 70)
    print("█ " + title)
    print("█" * 70)
    fn()


_out = []
notify._send = lambda text, level="INFO": _out.append((level, text)) or True
notify.reset()

# ── 1. СТАРТ ──────────────────────────────────────────────────────────────
plan = sender._build_run_plan({}, waba_planned=250)
notify.digest_start(
    phase_order=["personal", "waba", "personal"],
    caps={"personal": 33, "waba": 250},
    contacts_planned=250,
    rescue_pending=12,
    exhausted_total=0,
    waba_tier=0,
    code_version="9a05cfa",
    plan=plan,
)

# ── 2. РАБОТА ─────────────────────────────────────────────────────────────
notify.probe_miss("79031234567", "Иван Петров",
                  {"telegram": "absent", "max": "absent"})
notify.contact_sent("79163322370", "Мария Соколова", "telegram", "1288960948")
notify.probe_miss("79037777777", "Ольга К.",
                  {"telegram": "absent", "max": "unknown"})
notify.contact_failed("79876543210", "Пётр Ильин", "waba",
                      "131049", next_channels="telegram")

# ── 3. ФИНАЛ ──────────────────────────────────────────────────────────────
notify.summary(
    counters={
        "waba": {"accepted": 240, "delivered": 96,
                 "rejected": 144, "unknown": 0},
        "telegram": {"accepted": 30, "delivered": 27,
                     "rejected": 3, "unknown": 0},
        "max": {"accepted": 0, "delivered": 0,
                "rejected": 0, "unknown": 0},
    },
    rescue_pending=144, exhausted=0, elapsed_sec=583 * 60,
)

for lvl, txt in _out:
    print(f"\n────── [{lvl}] ──────")
    print(txt)

print("\n" + "=" * 70)
print("ПЛАН, ПО КОТОРОМУ СЧИТАЛОСЬ СРАВНЕНИЕ")
print("=" * 70)
for k, v in plan.items():
    print(f"   {k:<22} = {v}")