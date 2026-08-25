"""Трекинг полностью мёртвых контактов (все 3 канала 404 за текущий день).

Сохраняет в data/failed_today.json (сбрасывается в начале нового дня).
Каждый контакт хранится с phone, name, category, first/last_failed_at,
attempts (сколько раз пытались за сегодня) и channels_failed.

Используется для:
- Экспорта в CSV (чтобы почистить Excel от мёртвых номеров)
- Аналитики (какие категории чаще fail'ят)
- Не блокирует отправку — это просто лог.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from . import config, bad_phones


_LOCK = threading.RLock()
_DATA: Dict[str, Dict] = {}  # phone -> {name, category, first_failed, last_failed, attempts, channels}


def _path() -> Path:
    p = getattr(config, "FAILED_TODAY_PATH", None)
    if p:
        return Path(p)
    return Path(config.DATA_DIR) / "failed_today.json"


def load() -> int:
    """Загрузить кэш полностью мёртвых контактов за сегодня.

    Сбрасывается автоматически если файл создан вчера или раньше.
    """
    global _DATA
    _DATA = {}
    p = _path()
    if not p.exists():
        return 0
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        # Проверяем дату файла
        if raw.get("date") != config.now_tz().strftime("%Y-%m-%d"):
            return 0  # старый файл, игнорируем
        for phone, entry in raw.get("contacts", {}).items():
            _DATA[phone] = entry
        return len(_DATA)
    except (json.JSONDecodeError, OSError):
        return 0


def save() -> None:
    """Атомарно сохранить на диск."""
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": config.now_tz().strftime("%Y-%m-%d"),
        "contacts": _DATA,
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    with _LOCK:
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, p)
        except OSError as e:
            print(f"failed_contacts.save: {e}")


def mark_fully_failed(phone: str, name: str, category: str, channels_failed: list[str]) -> None:
    """Отметить контакт как полностью мёртвый (все активные каналы fail'нули).

    channels_failed — список каналов которые вернули 404 (например ["whatsapp", "telegram", "max"]).
    """
    if not phone:
        return
    with _LOCK:
        now = config.now_tz().isoformat(timespec="seconds")
        if phone in _DATA:
            entry = _DATA[phone]
            entry["last_failed_at"] = now
            entry["attempts"] = entry.get("attempts", 1) + 1
            entry["channels_failed"] = list(set(entry.get("channels_failed", []) + channels_failed))
        else:
            _DATA[phone] = {
                "name": name or "",
                "category": category or "",
                "first_failed_at": now,
                "last_failed_at": now,
                "attempts": 1,
                "channels_failed": channels_failed,
            }
    save()


def get_today() -> Dict[str, Dict]:
    """Получить копию словаря (read-only) — все failed сегодня."""
    return dict(_DATA)


def count() -> int:
    """Сколько мёртвых контактов сегодня."""
    return len(_DATA)


def get_with_all_channels_bad(active_channels: set) -> Dict[str, Dict]:
    """Получить контакты где ВСЕ активные каналы в bad_phones (для аналитики).

    Использует bad_phones cache, а не свой файл — cumulative.
    """
    result = {}
    for phone, channels in bad_phones.get_all().items():
        if all(ch in channels and channels[ch] for ch in active_channels):
            result[phone] = channels
    return result


def clear() -> None:
    """Очистить кэш (для тестов)."""
    global _DATA
    _DATA = {}
