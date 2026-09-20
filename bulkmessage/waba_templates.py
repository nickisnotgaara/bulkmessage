"""WABA template registry (Phase 1.2).

Управляет 4 одобренными WABA-шаблонами (по одному на категорию):
- Покупатели
- Продавцы
- Агенты
- Инвесторы

Источник template_id:
1. config.WABA_TEMPLATE_IDS (заполняется через .env.local)
2. db.waba_templates (синхронизируется из Wazzup)
3. Fallback: None

ВАЖНО: Wazzup API ожидает templateId в формате UUID вида
  `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`
(см. WAZZUP24_KNOWLEDGE_BASE.md). Цифровые ID типа `2147911229434328`
вернут ошибку `BUILD_TEMPLATE_ERROR` от Meta.

Functions:
- pick_waba_template(category) → template_id | None
- sync_waba_templates_from_wazzup() → int (сколько обновлено)
- ensure_waba_template_for_category(category) → bool
- is_valid_template_id(tid) → bool (UUID format check)
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from . import config, db, wazzup


log = logging.getLogger("waba_templates")


# UUID v1-v5 (Wazzup использует UUIDv4 для template_id от Meta)
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def is_valid_template_id(tid: Optional[str]) -> bool:
    """True если tid похож на UUID (то, что ожидает Wazzup)."""
    if not tid:
        return False
    return bool(_UUID_PATTERN.match(tid.strip()))


def pick_waba_template(category: str) -> Optional[str]:
    """Возвращает template_id для категории, или None если не настроен.

    Приоритет:
    1. db.waba_templates с status='approved' (свежее из Wazzup)
    2. config.WABA_TEMPLATE_IDS (статичный, из .env.local)

    Любые ID, которые НЕ похожи на UUID, отбрасываются с warn-логом —
    иначе Wazzup вернёт BUILD_TEMPLATE_ERROR (cant build template from string).
    """
    template_id_env = config.WABA_TEMPLATE_IDS.get(category, "")
    template_id_db: Optional[str] = None
    try:
        with db.db_conn() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT template_id, status FROM waba_templates WHERE category = ?",
                (category,),
            )
            rows = c.fetchall()
            for row in rows:
                if row["status"] == "approved":
                    tid = row["template_id"]
                    if is_valid_template_id(tid):
                        template_id_db = tid
                        break
                    else:
                        log.warning(
                            f"pick_waba_template: db record for {category!r} has "
                            f"invalid template_id (not UUID): {tid!r}"
                        )
    except Exception as e:
        log.warning(f"pick_waba_template: db lookup failed: {e}")

    if template_id_db:
        return template_id_db

    # Env fallback, но только если это UUID
    if is_valid_template_id(template_id_env):
        return template_id_env
    if template_id_env:
        log.warning(
            f"pick_waba_template: env WABA template for {category!r} is not a UUID "
            f"(value={template_id_env!r}). Wazzup ожидает формат UUID вида "
            f"'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'. У вас в .env.local, "
            f"похоже, числовой ID (например WABA phone-number ID или "
            f"WhatsApp Business Account ID) — он НЕ подходит для templateId. "
            f"Нужно взять UUID одобренного шаблона из Meta Business Manager "
            f"(WhatsApp → Message Templates → 'API ID' / templateId) "
            f"или через Wazzup webhook 'waba_template.status_update'."
        )
    return None


def sync_waba_templates_from_wazzup() -> int:
    """Синхронизирует список WABA templates из Wazzup в db.waba_templates.

    Возвращает количество обновлённых/добавленных записей.

    Примечание: у Wazzup нет публичного endpoint для списка templates —
    они приходят через webhook `waba_template.status_update` и сохраняются
    в db.waba_templates отдельно (см. reconcile.process_wazzup_payload).
    Эта функция только проверяет env-конфиг и валидирует UUID-формат.
    """
    n = 0
    try:
        with db.db_conn() as conn:
            for category, template_id in config.WABA_TEMPLATE_IDS.items():
                if not template_id:
                    continue
                if not is_valid_template_id(template_id):
                    log.warning(
                        f"sync_waba_templates: env WABA_TEMPLATE_*_ID for "
                        f"{category!r} = {template_id!r} is NOT a valid UUID. "
                        f"Skip until you fix .env.local."
                    )
                    continue
                # Проверить, есть ли уже запись
                existing = db.get_waba_template(conn, template_id)
                if existing is None:
                    db.upsert_waba_template(
                        conn,
                        template_id=template_id,
                        name=f"WABA template for {category}",
                        category=category,
                        language_code="ru",
                        status="approved",  # доверие к admin env
                    )
                    n += 1
                    log.info(f"waba_templates: added {category} → {template_id}")
                else:
                    log.debug(f"waba_templates: {category} already exists")
    except Exception as e:
        log.error(f"sync_waba_templates_from_wazzup error: {e}")
    return n


def ensure_waba_template_for_category(category: str) -> Optional[str]:
    """Гарантирует наличие template_id для категории.

    Если в БД нет — создаёт запись из env как approved.
    Возвращает template_id или None.
    """
    tid = pick_waba_template(category)
    if tid:
        return tid
    log.warning(f"ensure_waba_template: no template for category={category!r}")
    return None


def list_known_templates() -> dict[str, Optional[str]]:
    """Возвращает маппинг {category: template_id} для всех 4 категорий."""
    return {cat: pick_waba_template(cat) for cat in config.WABA_TEMPLATE_IDS.keys()}
