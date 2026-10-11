"""Показать, КАК ИМЕННО выглядит новая строка лога в Telegram.

Не отправляет ничего: подменяет _send на печать. Проверять формат глазами
полезно — цифры и галочки читаются быстрее, чем описание.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bulkmessage import config, notify  # noqa: E402

captured = []
notify.reset()
notify._send = lambda text, level="INFO": (
    captured.append((level, text)) or True
)
notify.tglog.send = lambda *a, **k: True

CASES = [
    # Типичный промах: нет ни одного личного мессенджера
    ("+79991234567", "Иван Петров",
     {"telegram": "absent", "max": "absent"}),
    # Частично неясный исход: MAX не ответил — это НЕ «аккаунта нет»
    ("+79997654321", "Мария С.",
     {"telegram": "absent", "max": "unknown"}),
    # Проб вообще не было: каналы в halt либо потолок исчерпан
    ("+79163322370", None, {}),
    # Ограниченный канал в halt, а не «нет аккаунта»
    ("+79037777777", "Ольга", {"telegram": "absent"}),
]

print("=" * 66)
print("ЧТО ПРИХОДИТ В БОТ ПОСЛЕ ПРАВКИ")
print("=" * 66)
for phone, name, verdicts in CASES:
    notify.probe_miss(phone, name or "", verdicts)

for level, text in captured:
    print(f"\n───── [{level}] ─────")
    print(text)

print()
print("=" * 66)
print("БЮДЖЕТ ЛОГА")
print("=" * 66)
print(f"   построчный бюджет за прогон : {config.TG_PER_CONTACT_MAX}")
print(f"   потолок в час              : {config.TG_MAX_MESSAGES_PER_HOUR}")
print(f"   интервал между сообщениями : {config.TG_LOG_SEND_INTERVAL} с")
print(f"   очередь                    : {config.TG_LOG_MAX_QUEUE}")
print()
rate = 3600 / config.TG_LOG_SEND_INTERVAL
print(f"   Поток разведки: пауза между проверками 2 с → до {rate:.0f} проб/час")
print(f"   Поток сообщений: интервал {config.TG_LOG_SEND_INTERVAL} с "
      f"→ до {rate:.0f} сообщений/час")
print(f"   Потолок {config.TG_MAX_MESSAGES_PER_HOUR} сообщений/час "
      f"={'ПЕРЕКРЫВАЕТ поток ✓' if config.TG_MAX_MESSAGES_PER_HOUR >= rate else 'НЕ ХВАТАЕТ ✗'}")
print()
print(f"   На прогоне с 400 проверками и долей попаданий 13% придёт")
print(f"   ~{int(400*0.87)} сообщений о пропусках и "
      f"~{int(400*0.13)} об успехах.")
print(f"   Время на дренирование очереди: "
      f"{int(400 * config.TG_LOG_SEND_INTERVAL / 60)} мин.")