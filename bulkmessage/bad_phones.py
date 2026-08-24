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
_DATA: Dict[str, Dict[str, bool]] = {}


def _path() -> Path:
    """Путь к файлу кэша. Можно переопределить через env."""
    p = getattr(config, "BAD_PHONES_PATH", None)
    if p:
        return Path(p)
    return Path(config.DATA_DIR) / "bad_phones.json"


def load() -> int:
    """Загрузить кэш с диска. Возвращает сколько контактов загружено."""
    global _DATA
    p = _path()
    if not p.exists():
        _DATA = {}
        return 0
    try:
        _DATA = json.loads(p.read_text(encoding="utf-8"))
        # Sanity check
        if not isinstance(_DATA, dict):
            _DATA = {}
            return 0
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
    return _DATA.get(phone, {}).get(channel, False)


def mark_bad(phone: str, channel: str) -> bool:
    """Пометить phone+channel как мёртвый. Возвращает True если реально изменилось.

    Записывает на диск атомарно. Можно вызывать часто — запись короткая.
    """
    if not phone or not channel:
        return False
    with _LOCK:
        if phone not in _DATA:
            _DATA[phone] = {}
        if _DATA[phone].get(channel):
            return False  # уже было
        _DATA[phone][channel] = True
    # save() вне lock (он сам лочится)
    save()
    return True


def is_fully_bad(phone: str, active_channels: Set[str]) -> bool:
    """True если для phone ВСЕ переданные каналы мёртвые.

    Если active_channels пустое — возвращает True (нет смысла пытаться).
    """
    if not phone:
        return False
    if not active_channels:
        return True
    phone_data = _DATA.get(phone, {})
    return all(phone_data.get(ch, False) for ch in active_channels)


def get_known_channels(phone: str) -> Set[str]:
    """Множество каналов, для которых phone уже помечен как мёртвый."""
    if not phone:
        return set()
    return {ch for ch, bad in _DATA.get(phone, {}).items() if bad}


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
