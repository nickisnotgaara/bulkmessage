"""Проба наличия аккаунта в канале (Telegram / MAX).

Зачем
-----
Владелец просит: перед откатом проверить, у кого вообще есть Telegram, а у кого
MAX, и сохранить статус — не отправляя сообщений. Оказывается, у Wazzup НЕТ
эндпоинта «есть ли у номера аккаунт в канале»: ни GET /v3/contacts (отдаёт только
тех, кто уже писал), ни POST /v3/contacts (требует chatId, которого у холодного
контакта нет). Единственный способ узнать — отправить запрос с `phone`.

Почему это почти бесплатно
--------------------------
Отрицательный вердикт приходит СИНХРОННЫМ HTTP 400 прямо в ответе на запрос:

    {"status":400,"error":"CHANNEL_TGAPI_PHONE_NOT_OCCUPIED",
     "description":"Can't resolve telegram contact by phone."}

Это подтверждено живой базой: все 166 строк с `*_NOT_OCCUPIED` имеют
message_id=NULL и sent_at=NULL (их пишет только sender._persist_failure), а в
506 сохранённых вебхуках этот код не встречается НИ РАЗУ — то есть сообщение
даже не создавалось, эха не будет.

Квота при этом НЕ списывается: state.increment_channel_sent() вызывается ровно
в одной точке — sender._persist_success(). Этот модуль её не зовёт вообще.

Чем за это платим
------------------
Если аккаунт ЕСТЬ — придёт 2xx и человеку реально уйдёт сообщение. Поэтому:

  1. Проба шлёт РАБОЧИЙ текст из Message_script.md, а не заглушку: иначе
     человек получит бессмысленное сообщение.
  2. Успех немедленно регистрируется как реальная отправка (сообщение в БД,
     квота, вердикт 'present'). Иначе контакт получит ДУБЛЬ в фазе отката.

Инварианты, нарушение которых — баг
-----------------------------------
  * Проба НЕ трогает rescue_queue, bad_phones и mark_unreachable. Иначе номер с
    «нет аккаунта» был бы помечен мёртвым навсегда, хотя это не «мёртвый
    контакт», а «нет аккаунта в этом канале» — и он выпал бы из всех каналов.
  * При verdict=absent квота не растёт (проверяется тестом).
  * Повторная проба того же (контакт, канал) не выполняется в течение TTL.
  * DRY_RUN не пишет никуда и не вызывает API — как и весь остальной sender.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from . import config, db, state, wazzup
from .templates import build_message, load_templates


log = logging.getLogger("probe")


# Признаки того, что аккаунта в канале нет — ответ приходит СИНХРОННО.
# Проверяем по телу ответа, а не по HTTP-коду: код 400 используется и для других
# причин, и ошибочно назвать «нет аккаунта» живого контакта опаснее, чем
# оставить его unknown.
#
# живой canary 2026-10-08 на +998933930709 вернул:
#   {"error":"POST_MESSAGE_ERROR","data":[{"code":"CHAT_IS_BLOCKED",
#    "description":"Chat is blocked"}]}
# Это НЕ «аккаунта нет» (у человека мог заблокировать бота), поэтому код
# намеренно НЕ включён: вердикт остаётся unknown. Проверено — квота при таком
# ответе не списывается, отправки не фиксируется.
_ABSENT_MARKERS = (
    "phone_not_occupied",
    "chat_id_invalid",
    "peer_id_invalid",
    "user_not_found",
    "recipient with this chatid does not exist",
)

# Синхронный отказ = аккаунта нет. Остальное (auth, rate limit, транзиент)
# НЕ является ответом о существовании аккаунта и не должно им помечаться.
_PRESENT_STATUSES = frozenset({200, 201, 202})


@dataclass
class ProbeResult:
    """Итог одной пробы. Ничего не пишет в БД — запись делает run_phase."""

    phone: str
    channel: str
    verdict: str                      # present | absent | unknown
    delivered: bool = False           # True → сообщение РЕАЛЬНО ушло человеку
    message_id: Optional[str] = None
    http_status: Optional[int] = None
    detail: str = ""
    chat_id: Optional[str] = None


def classify_probe_response(
    ok: bool, message_id: Optional[str], detail: str, http_status: Optional[int]
) -> str:
    """Вердикт по ОТВЕТУ на пробу. Ничего не знает о квоте и БД.

    present — транспорт принял запрос (2xx), аккаунт существует;
    absent  — транспорт синхронно отказал: аккаунта нет, квота не потрачена;
    unknown — отказ не связан с существованием аккаунта (auth, rate limit,
             сеть). Такой вердикт НИКОГДА не трактуется как «есть аккаунт».
    """
    if ok and http_status in _PRESENT_STATUSES:
        return "present"
    text = (detail or "").lower()
    if any(marker in text for marker in _ABSENT_MARKERS):
        return "absent"
    return "unknown"


def probe_channel(
    phone: str,
    channel: str,
    text: str,
    *,
    dry_run: Optional[bool] = None,
) -> ProbeResult:
    """Проверяет наличие аккаунта и возвращает вердикт. БД НЕ трогает.

    Проба отправляет РАБОЧИЙ текст `text`. При verdict='present' сообщение
    действительно доставлено — вызывающий обязан зарегистрировать его как
    реальную отправку.
    """
    dry = config.DRY_RUN if dry_run is None else dry_run
    if dry:
        # В симуляции не делаем НИ ОДНОГО сетевого вызова: неизвестно, есть ли
        # аккаунт, выдумывать вердикт нельзя — иначе DRY_RUN рисовал бы
        # правдоподобную, но вымышленную картину воронки.
        return ProbeResult(
            phone=phone, channel=channel, verdict="unknown",
            detail="[DRY-RUN] сеть не вызывалась, вердикт неизвестен",
        )

    ok, message_id, detail, http_status = wazzup.send_to_channel(
        channel, phone, text
    )
    verdict = classify_probe_response(ok, message_id, detail, http_status)
    chat_id = None
    if ok:
        # Wazzup возвращает разрешённый chatId в теле ответа для phone-параметра.
        try:
            import json as _json
            body = _json.loads(detail or "{}")
            chat_id = body.get("chatId") or body.get("chat_id")
        except (ValueError, AttributeError):
            chat_id = None
    return ProbeResult(
        phone=phone, channel=channel, verdict=verdict,
        delivered=verdict == "present",
        message_id=message_id, http_status=http_status,
        detail=(detail or "")[:300], chat_id=chat_id,
    )


def record_probe_result(
    result: ProbeResult,
    contact: dict,
    text: str,
) -> Optional[int]:
    """Сохраняет вердикт и, если проба реально доставила сообщение —
    регистрирует ПОЛНОЦЕННУЮ отправку.

    Разделение намеренное: вердикт всегда пишется в contact_channel_state,
    а реальная отправка (сообщение + квота) — только при delivered=True.
    Именно поэтому успешная проба не приводит к дублю в фазе отката.
    """
    now = datetime.now(timezone.utc).isoformat()

    with db.db_conn() as conn:
        contact_id = db.upsert_contact(
            conn, result.phone, contact.get("name") or "—",
            contact.get("category") or "—",
        )
    db.upsert_channel_state(
        contact_id,
        result.channel,
        result.verdict,
        verdict_source="probe_sync",
        chat_id=result.chat_id,
        last_probe_at=now,
        last_send_at=now if result.delivered else None,
        last_error=None if result.delivered else (result.detail or "")[:500],
    )

    if not result.delivered:
        return contact_id

    # Сообщение ушло человеку — обязаны учесть его полностью, иначе контакт
    # получит второе сообщение из фазы отката.
    current = state.load_state()
    state.increment_channel_sent(current, result.channel)
    state.record_accepted(current, result.channel)
    state.save_state(current)

    with db.db_conn() as conn:
        db.insert_message(
            conn,
            contact_id=contact_id,
            channel=result.channel,
            message_text=text,
            message_id=result.message_id,
            status="sent",
        )
    log.info(
        f"   🔎 probe {result.channel} {result.phone}: аккаунт ЕСТЬ — "
        f"сообщение отправлено (id={result.message_id}), квота списана"
    )
    return contact_id


def probe_contact_channels(
    contact: dict,
    channels: Optional[list[str]] = None,
    templates: Optional[dict[str, str]] = None,
) -> list[ProbeResult]:
    """Пробит все каналы контакта по очереди, останавливаясь на первом
    успехе (каскад: TG → MAX)."""
    channels = list(channels or getattr(config, "PROBE_CHANNELS", ["telegram", "max"]))
    templates = templates if templates is not None else load_templates()
    text = build_message(contact, templates)
    results: list[ProbeResult] = []

    for channel in channels:
        if not db.probe_is_needed(
            contact["contact_id"], channel, config.PROBE_TTL_HOURS
        ):
            log.debug(
                f"   🔎 probe {channel} {contact['phone']}: свежий вердикт, пропуск"
            )
            continue
        result = probe_channel(contact["phone"], channel, text)
        record_probe_result(result, contact, text)
        results.append(result)
        if result.delivered:
            break
        if config.PROBE_INTERVAL_MIN > 0 and results:
            time.sleep(config.PROBE_INTERVAL_MIN)
    return results


def run_phase(
    contacts: list[dict],
    log_fn=log,
    stop_requested: Optional[dict] = None,
) -> dict[str, int]:
    """Фаза PROBE: проверяет наличие аккаунта у всех переданных контактов.

    Соблюдает PROBE_MAX_PER_RUN и останавливается по stop_requested.
    Ничего не отправляет «вслепую»: откатные фазы потом читают ТОЛЬКО
    verdict='present'.
    """
    stats = {"probed": 0, "present": 0, "absent": 0, "unknown": 0, "skipped": 0}
    if not getattr(config, "PROBE_ENABLED", True):
        log_fn.info("   🔎 PROBE выключен (BULK_PROBE_ENABLED=0) — пропуск")
        return stats

    log_fn.info(
        f"🔎 ФАЗА PROBE · {len(contacts)} контактов · "
        f"лимит {config.PROBE_MAX_PER_RUN}"
    )
    for contact in contacts:
        if stop_requested and stop_requested.get("v"):
            log_fn.info("   🔎 PROBE прерван по сигналу остановки")
            break
        if stats["probed"] >= config.PROBE_MAX_PER_RUN:
            log_fn.warning(
                f"   🔎 PROBE: достигнут лимит {config.PROBE_MAX_PER_RUN} — "
                f"остальные контакты проверь в следующем прогоне"
            )
            break
        stats["probed"] += 1
        results = probe_contact_channels(contact)
        for res in results:
            if res.verdict in stats:
                stats[res.verdict] += 1
    return stats