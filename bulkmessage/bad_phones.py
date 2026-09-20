"""Кэш мёртвых номеров — каналы, в которых номер дал 404 (PERMANENT).

Если для phone все 3 активных канала в кэше, контакт сразу skip'ается
(0 API calls). Иначе — пробуем только неизвестные каналы.

Перситенсия: data/bad_phones.json. Загружается при старте, обновляется
на каждом PERMANENT fail.

Concurrency: in-process lock + atomic write (write to .tmp + rename).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Dict, Optional, Set

from . import config


_LOCK = threading.RLock()
# Структура:
#   phone -> channel -> {"bad": bool, "cooldown_until": iso_ts | None, "reason": str}
_DATA: Dict[str, Dict[str, dict]] = {}


def _path() -> Path:
    """Путь к файлу кэша. Можно переопределить через env."""
    p = getattr(config, "BAD_PHONES_PATH", None)
    if p:
        return Path(p)
    return Path(config.DATA_DIR) / "bad_phones.json"


def load() -> int:
    """Загрузить кэш с диска. Возвращает сколько контактов загружено.

    Поддерживает обратную совместимость со старым форматом
    {phone: {channel: bool}}. Если встречает bool — конвертирует в
    {"bad": bool, "cooldown_until": None, "reason": "legacy"}.
    """
    global _DATA
    p = _path()
    if not p.exists():
        _DATA = {}
        return 0
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            _DATA = {}
            return 0
        # Migrate old {channel: bool} → {channel: dict}
        migrated: Dict[str, Dict[str, dict]] = {}
        for phone, channels in raw.items():
            if not isinstance(channels, dict):
                continue
            migrated[phone] = {}
            for channel, val in channels.items():
                if isinstance(val, bool):
                    migrated[phone][channel] = {
                        "bad": val,
                        "cooldown_until": None,
                        "reason": "legacy",
                    }
                elif isinstance(val, dict):
                    # New format — keep as-is
                    migrated[phone][channel] = val
                else:
                    migrated[phone][channel] = {
                        "bad": bool(val),
                        "cooldown_until": None,
                        "reason": "legacy",
                    }
        _DATA = migrated
        return len(_DATA)
    except (json.JSONDecodeError, OSError) as e:
        # Битый файл — лучше начать с пустого, чем крашить
        print(f"bad_phones.load: failed to load {p}: {e}")
        _DATA = {}
        return 0


def save() -> None:
    """Атомарно сохранить кэш на диск (write to .tmp + rename)."""
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with _LOCK:
        try:
            tmp.write_text(
                json.dumps(_DATA, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, p)
        except OSError as e:
            print(f"bad_phones.save: failed to save {p}: {e}")


def is_bad(phone: str, channel: str) -> bool:
    """True если phone+channel ранее получал PERMANENT (404)."""
    if not phone:
        return False
    entry = _DATA.get(phone, {}).get(channel)
    if isinstance(entry, dict):
        return bool(entry.get("bad"))
    return False


def is_in_cooldown(phone: str, channel: str, now_iso: str) -> bool:
    """True если phone+channel в cooldown (cooldown_until > now)."""
    if not phone or not channel:
        return False
    entry = _DATA.get(phone, {}).get(channel)
    if not isinstance(entry, dict):
        return False
    until = entry.get("cooldown_until")
    if not until:
        return False
    return str(until) > str(now_iso)


def mark_bad(
    phone: str,
    channel: str,
    reason: str = "permanent",
    cooldown_until_iso: Optional[str] = None,
) -> bool:
    """Пометить phone+channel как мёртвый. Возвращает True если реально изменилось.

    Записывает на диск атомарно. Можно вызывать часто — запись короткая.
    """
    if not phone or not channel:
        return False
    new_entry = {
        "bad": True,
        "cooldown_until": cooldown_until_iso,
        "reason": reason,
    }
    with _LOCK:
        if phone not in _DATA:
            _DATA[phone] = {}
        existing = _DATA[phone].get(channel)
        if existing == new_entry:
            return False  # уже было
        _DATA[phone][channel] = new_entry
    # save() вне lock (он сам лочится)
    save()
    return True


def mark_complaint_cooldown(
    phone: str, channel: str, cooldown_until_iso: str, reason: str = "complaint"
) -> bool:
    """Пометить phone+channel как в cooldown после жалобы (Phase 3.6)."""
    return mark_bad(phone, channel, reason=reason, cooldown_until_iso=cooldown_until_iso)


def is_fully_bad(phone: str, active_channels: Set[str]) -> bool:
    """True если для phone ВСЕ переданные каналы мёртвые.

    Если active_channels пустое — возвращает True (нет смысла пытаться).
    Учитывает ТОЛЬКО `bad=True`, не учитывает временный cooldown.
    """
    if not phone:
        return False
    if not active_channels:
        return True
    phone_data = _DATA.get(phone, {})
    for ch in active_channels:
        entry = phone_data.get(ch)
        if isinstance(entry, dict):
            if not entry.get("bad"):
                return False
        else:
            return False
    return True


def get_known_channels(phone: str) -> Set[str]:
    """Множество каналов, для которых phone уже помечен как мёртвый."""
    if not phone:
        return set()
    out: Set[str] = set()
    for ch, entry in _DATA.get(phone, {}).items():
        if isinstance(entry, dict):
            if entry.get("bad"):
                out.add(ch)
        elif entry:  # legacy bool
            out.add(ch)
    return out


def clear() -> None:
    """Очистить кэш (для тестов)."""
    global _DATA
    _DATA = {}


def get_all() -> Dict[str, Dict[str, bool]]:
    """Получить весь кэш (для отладки / экспорта)."""
    return dict(_DATA)


def count() -> int:
    """Сколько контактов в кэше."""
    return len(_DATA)


def migrate_from_db(active_channels: Optional[Set[str]] = None) -> int:
    """Сканирует crm.db и добавляет в кэш все (phone, channel) пары,
    которые имеют status='failed' в таблице messages.

    Одноразовая миграция — вызывается при старте sender.py, если
    файл кэша ещё не существует.

    Возвращает сколько контактов было добавлено.
    """
    if active_channels is None:
        active_channels = {"whatsapp", "telegram", "max"}

    # Если файл уже есть — ничего не делаем (миграция одноразовая)
    p = _path()
    if p.exists() and p.stat().st_size > 0:
        return 0

    # Импортируем db лениво (избегаем циклических импортов)
    from . import db

    added = 0
    try:
        with db.db_conn() as conn:
            c = conn.cursor()
            # Находим все failed пары (phone, channel)
            rows = c.execute("""
                SELECT DISTINCT ct.phone, m.channel
                FROM messages m
                JOIN contacts ct ON ct.id = m.contact_id
                WHERE m.status = 'failed' AND m.channel IN ({})
            """.format(",".join("?" * len(active_channels))),
                tuple(active_channels),
            ).fetchall()
            for phone, channel in rows:
                if phone and channel:
                    if mark_bad(phone, channel):
                        added += 1
    except Exception as e:
        print(f"bad_phones.migrate_from_db: failed: {e}")
    return added
