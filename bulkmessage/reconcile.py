"""Webhook event processing and reconciliation loops."""

from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

from . import config, db, sheets, wazzup
from .sheets import sync_message_to_sheet


log = config.get_logger("reconcile")


# ============================================================
# Webhook payload processing
# ============================================================


def _extract_phone(value) -> str:
    """Извлечь и нормализовать телефон из поля (chatId / from / sender и т.п.).

    Wazzup24 присылает chatId как нормализованный цифровой phone.
    Если встречается JID-хвост (@s.whatsapp.net и т.п.) — отрезаем.
    """
    if not value:
        return ""
    s = str(value)
    # отрезаем JID-хвост если был
    for sep in ("@", ":"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return wazzup.normalize_phone(s) or ""


def process_incoming_message(channel: str, msg: dict) -> None:
    raw_text = (msg.get("body") or "").strip()
    msg_type = (msg.get("type") or "chat").lower()
    message_id = msg.get("id") or msg.get("message_id")
    if msg_type != "chat" and not raw_text:
        raw_text = f"[{msg_type}]"

    phone = _extract_phone(msg.get("from")) or _extract_phone(
        msg.get("chatId") or msg.get("chat_id")
    )
    if not phone:
        log.warning("incoming_message: не удалось определить телефон")
        return
    if not raw_text:
        raw_text = f"[{msg_type or 'message'}]"

    log.info(f"📩 REPLY received: {phone} via {channel}: {raw_text[:120]}")

    with db.db_conn() as conn:
        contact = db.get_contact_by_phone(conn, phone)
        if not contact:
            db.upsert_contact(
                conn,
                phone,
                msg.get("contact_name") or msg.get("senderName") or "",
                "",
            )
            contact = db.get_contact_by_phone(conn, phone)
        contact_id = contact["id"]
        db.add_reply(conn, contact_id, channel, raw_text, message_id)
        # Помечаем ВСЕ сообщения этого контакта в этом канале как answered
        # (т.к. контакт ответил в чате — не важно на какое именно сообщение)
        with db.db_conn() as conn2:
            c2 = conn2.cursor()
            c2.execute(
                "SELECT id FROM messages WHERE contact_id = ? AND channel = ?",
                (contact_id, channel),
            )
            all_targets = [r["id"] for r in c2.fetchall()]
        for tid in all_targets:
            db.update_message_status(
                conn,
                message_pk=tid,
                status="answered",
                answered_at=True,
            )
        message_pk = all_targets[-1] if all_targets else None

    if message_pk is not None:
        # Re-sync ALL outbound messages for this contact+channel, не только последний
        with db.db_conn() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT id FROM messages WHERE contact_id = ? AND channel = ? AND message_id IS NOT NULL AND message_id != ''",
                (contact_id, channel),
            )
            all_pks = [r["id"] for r in c.fetchall()]
        for mpk in all_pks:
            try:
                sync_message_to_sheet(
                    contact_id=contact_id, channel=channel, status="answered", message_pk=mpk
                )
            except Exception as ex:
                log.warning(f"resync to sheet failed: {ex}")
    log.info(f"Reply stored and Google updated for {phone}/{channel}")


def _wazzup_status_to_local(status: Optional[str]) -> str:
    """Wazzup status → наш локальный status.

    Wazzup statuses (см. WAZZUP24_KNOWLEDGE_BASE.md):
      - sent / delivered / read / error
    """
    if not status:
        return "sent"
    s = str(status).lower()
    if s == "read":
        return "read"
    if s == "delivered":
        return "delivered"
    if s in ("sent", "outgoing", "queued"):
        return "sent"
    if s in ("error", "failed", "undelivered"):
        return "failed"
    return s


def process_delivery_status(channel: str, msg: dict) -> None:
    message_id = msg.get("id") or msg.get("message_id")
    status = _wazzup_status_to_local(msg.get("status"))
    if not message_id:
        log.warning("delivery_status: нет message_id")
        return
    log.info(
        f"📬 DELIVERY status: msg_id={message_id} channel={channel} -> {status}"
    )
    with db.db_conn() as conn:
        row = db.find_message_by_message_id(conn, message_id)
        if row is None:
            phone = _extract_phone(msg.get("chat_id") or msg.get("to"))
            if phone:
                contact = db.get_contact_by_phone(conn, phone)
                if contact is not None:
                    target = db.get_latest_inbound_target(conn, contact["id"], channel)
                    if target is not None and not target["message_id"]:
                        db.update_message_status(
                            conn,
                            message_pk=target["id"],
                            message_id=message_id,
                        )
                        row = db.find_message_by_message_id(conn, message_id)
        if row is None:
            log.warning(
                f"delivery_status: сообщение {message_id} не найдено в БД"
            )
            return
        kwargs: dict = {"message_pk": row["id"]}
        if status == "delivered":
            kwargs.update({"status": "delivered", "delivered_at": True})
        elif status == "read":
            kwargs.update({"status": "read", "delivered_at": True, "read_at": True})
        elif status == "failed":
            kwargs.update({"status": "failed", "last_error": msg.get("status", "failed")})
        else:
            kwargs["status"] = status
        db.update_message_status(conn, **kwargs)
        contact_id = row["contact_id"]
        message_pk = row["id"]

    sync_message_to_sheet(
        contact_id=contact_id, channel=channel, status=status, message_pk=message_pk
    )

    # Phase 3.6: complaint halt — если error сигналит о жалобе (spam/report).
    error_blob = msg.get("error") or {}
    if isinstance(error_blob, dict):
        err_code = (error_blob.get("error") or error_blob.get("code") or "").lower()
        err_kind = (error_blob.get("description") or "").lower()
    else:
        err_code = str(error_blob).lower()
        err_kind = ""
    spam_markers = (
        "spam", "spam_report", "report_spam", "user_complaint",
        "block", "blocked", "quality_red", "low_quality",
    )
    if any(m in err_code for m in spam_markers) or any(m in err_kind for m in spam_markers):
        try:
            _record_complaint_and_maybe_halt(channel, kind=err_code or "spam_report")
        except Exception as e:
            log.warning(f"complaint halt handler error: {e}")


def _record_complaint_and_maybe_halt(channel: str, kind: str = "spam") -> None:
    """Phase 3.6: записывает жалобу и halt'ит канал если порог превышен."""
    # Lazy import чтобы не было цикла
    from . import state, tglog
    cur = state.load_state()
    cur = state.reset_daily_if_new_day(cur)
    state.record_complaint(cur, channel, kind=kind)
    # Запись в db для аудита (long-term)
    try:
        with db.db_conn() as conn:
            db.log_complaint(conn, channel, phone=None, complaint_kind=kind)
    except Exception as e:
        log.warning(f"db.log_complaint failed: {e}")
    if state.should_halt_after_complaint(cur, channel):
        state.halt_channel(cur, channel)
        tglog.send(
            f"🚨 CHANNEL HALT: {channel} достиг {state.get_complaint_count(cur, channel)} "
            f"жалоб за {config.COMPLAINT_WINDOW_HOURS}ч. Канал остановлен до "
            f"{cur['halted_channels'].get(channel, '?')}",
            "ERROR",
        )
        log.error(
            f"🚨 HALTED {channel} after {state.get_complaint_count(cur, channel)} complaints"
        )
    state.save_state(cur)


def process_outgoing_message(channel: str, msg: dict) -> None:
    message_id = msg.get("id") or msg.get("message_id")
    if not message_id:
        return
    log.info(f"📤 OUTGOING event: msg_id={message_id} channel={channel}")
    with db.db_conn() as conn:
        row = db.find_message_by_message_id(conn, message_id)
        if row is None:
            return
        if row["status"] in ("queued", "sent"):
            db.update_message_status(conn, message_pk=row["id"], status="sent")


# ============================================================
# Wazzup24 webhook handling (Phase 1.3)
# ============================================================


def _wazzup_chat_type_to_channel(chat_type: str, channel_id: str = "") -> str:
    """Маппинг Wazzup chatType (+ channelId) → bulkmessage channel name.

    Wazzup chatType: 'whatsapp' | 'telegram' | 'max' | 'viber' | 'instagram' |
    'whatsgroup' | ''.
    chatType "whatsapp" НЕ различает Personal WA от WABA — для этого нужен
    channelId → transport (см. wazzup.channel_name_for()).

    Приоритет:
      1. Если задан channelId и он есть в кэше Wazzup → берём из кэша.
      2. Иначе маппим по chatType (Telegram/MAX уникальны).
      3. Для chatType='whatsapp' без channelId → fallback 'whatsapp' (старый код).
    """
    # 1. По channelId (точнее, учитывает WABA vs Personal)
    if channel_id:
        from . import wazzup as _wz
        name = _wz.channel_name_for(channel_id)
        if name:
            return name

    # 2. По chatType (для TG/MAX, где он однозначен)
    ct = (chat_type or "").lower().strip()
    if ct in ("telegram", "tg", "tgapi"):
        return "telegram"
    if ct in ("max", "vk", "maxapi"):
        return "max"

    # 3. Fallback
    if ct in ("whatsapp", "waba", "whatsgroup"):
        return "whatsapp"
    return "whatsapp"  # неизвестное → как раньше (default)


def _wazzup_extract_message(msg: dict) -> dict:
    """Нормализует message payload от Wazzup к плоскому виду для process_incoming_message.

    Wazzup fields:
      - messageId (uuid)
      - chatType (whatsapp/telegram/...)
      - chatId   (phone or username)
      - text
      - timestamp
      - type: 'incoming' | 'outgoing' | 'status'
      - status: 'sent' | 'delivered' | 'read' | 'error'
      - error: {code, description}
      - channelId
    """
    return {
        "id": msg.get("messageId"),
        "message_id": msg.get("messageId"),
        "body": msg.get("text") or "",
        "type": msg.get("type"),
        "chatId": msg.get("chatId"),
        "chat_id": msg.get("chatId"),
        "from": msg.get("chatId"),  # входящее = от chatId
        "status": msg.get("status"),
        "error": msg.get("error"),
        "timestamp": msg.get("timestamp"),
        "channelId": msg.get("channelId"),
    }


def process_wazzup_payload(channel_hint: Optional[str], payload: dict) -> None:
    """Обрабатывает webhook payload от Wazzup24.

    Поддерживаемые события (см. WAZZUP24_KNOWLEDGE_BASE.md):
      - message.add (входящее сообщение)
      - message.status_update (sent → delivered → read → error)
      - channel.waba_tier_update (Meta повысил/понизил tier)
      - waba_template.status_update (Meta одобрила/отклонила шаблон)
      - channel.qr_update (обновился QR — для админа)
    """
    if not isinstance(payload, dict):
        return
    # Wazzup может слать как list, так и dict-with-list. Нормализуем.
    items: list = []
    if "messages" in payload and isinstance(payload["messages"], list):
        items = payload["messages"]
    elif "events" in payload and isinstance(payload["events"], list):
        items = payload["events"]
    else:
        # Один объект — оборачиваем в list
        items = [payload]

    for evt in items:
        if not isinstance(evt, dict):
            continue
        evt_type = (
            evt.get("type")
            or evt.get("event")
            or evt.get("wh_type")
            or ""
        ).lower()

        try:
            # Канал определяем из payload'а (chatType + channelId), а не из channel_hint.
            # hint переопределяет только если он задан (для обратной совместимости).
            # chatType от Wazzup: whatsapp | telegram | max | viber | instagram | …
            # channelId нужен для различения WABA vs Personal WA (chatType='whatsapp'
            # для обоих).
            chat_type = (evt.get("chatType") or "").strip().lower()
            evt_channel_id = (evt.get("channelId") or "").strip()
            ch = channel_hint or _wazzup_chat_type_to_channel(chat_type, evt_channel_id)

            if evt_type in ("incoming_message", "message", "incoming"):
                msg = _wazzup_extract_message(evt)
                msg["chat_type"] = chat_type or "whatsapp"
                process_incoming_message(ch, msg)
            elif evt_type in ("delivery_status", "status_update", "outgoing_message_api",
                              "outgoing_message_phone", "message_status"):
                msg = _wazzup_extract_message(evt)
                process_delivery_status(ch, msg)
            elif evt_type in ("outgoing", "outgoing_message"):
                msg = _wazzup_extract_message(evt)
                process_outgoing_message(ch, msg)
            elif evt_type == "channel.waba_tier_update":
                _handle_wazzup_tier_update(evt)
            elif evt_type == "waba_template.status_update":
                _handle_wazzup_template_status(evt)
            elif evt_type in ("channel.qr_update", "channel.status_update",
                              "channel.create", "channel.delete"):
                _handle_wazzup_channel_event(evt)
            else:
                log.debug(f"process_wazzup_payload: ignored event type={evt_type!r}")
        except Exception as e:
            log.error(
                f"process_wazzup_payload handler error ({evt_type}): {e}\n"
                f"{traceback.format_exc()}"
            )


def _handle_wazzup_tier_update(evt: dict) -> None:
    """Канал WABA получил новый tier (Meta сама повышает/понижает)."""
    tier = evt.get("tier")
    if tier is None:
        log.debug("waba_tier_update: no tier field")
        return
    try:
        tier = int(tier)
    except (TypeError, ValueError):
        log.warning(f"waba_tier_update: invalid tier value {tier!r}")
        return
    # Сохраняем в state.json (lazy import чтобы не было цикла)
    from . import state as _state
    _state.set_waba_tier(tier)
    log.info(f"📈 WABA tier updated → {tier} (cap = {config.WABA_TIER_LIMITS.get(tier, '?')})")


def _handle_wazzup_template_status(evt: dict) -> None:
    """Meta одобрила/отклонила template."""
    template_id = evt.get("templateId") or evt.get("id")
    status = (evt.get("status") or "").lower()
    name = evt.get("name") or ""
    category = evt.get("category") or ""
    language = evt.get("language") or "ru"
    error = evt.get("error")
    if not template_id or not status:
        log.debug(f"waba_template.status_update: missing templateId/status")
        return
    try:
        with db.db_conn() as conn:
            db.upsert_waba_template(
                conn,
                template_id=template_id,
                name=name or f"template-{template_id[:8]}",
                category=category or None,
                language_code=language,
                status=status,
                last_error=str(error) if error else None,
            )
        log.info(f"📋 WABA template status: {template_id} → {status}")
    except Exception as e:
        log.error(f"_handle_wazzup_template_status error: {e}")


def _handle_wazzup_channel_event(evt: dict) -> None:
    """QR-обновление, статус канала, etc — просто логируем для админа."""
    evt_type = evt.get("type") or evt.get("event")
    channel_id = evt.get("channelId")
    log.info(
        f"🔔 Wazzup channel event: type={evt_type!r} channelId={channel_id!r}"
    )


def process_webhook_payload(channel_hint: Optional[str], payload: dict) -> None:
    """Прокси для webhook payload — единый обработчик Wazzup24.

    Эта функция — тонкая обёртка над process_wazzup_payload для вызовов из
    webhook_app._handle_payload.
    """
    process_wazzup_payload(channel_hint, payload)


# ============================================================
# Reconciliation loops
# ============================================================


def reconcile_pending_webhooks() -> None:
    with db.db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, channel, payload, attempts FROM pending_webhooks "
            "WHERE processed = 0 ORDER BY id ASC LIMIT 200"
        )
        rows = c.fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
            process_webhook_payload(row["channel"], payload)
            with db.db_conn() as conn:
                c = conn.cursor()
                c.execute(
                    "UPDATE pending_webhooks SET processed = 1, attempts = attempts + 1 "
                    "WHERE id = ?",
                    (row["id"],),
                )
            log.info(f"Pending webhook {row['id']} retried successfully")
        except Exception as e:
            with db.db_conn() as conn:
                c = conn.cursor()
                c.execute(
                    "UPDATE pending_webhooks SET attempts = attempts + 1, last_error = ? "
                    "WHERE id = ?",
                    (str(e)[:500], row["id"]),
                )
            log.warning(f"Pending webhook {row['id']} retry failed: {e}")


def reconcile_message_statuses() -> None:
    """Reconcile статусов сообщений.

    После миграции на Wazzup24 у нас нет GET-API для статуса конкретного
    сообщения (Wazzup — webhook-driven). Статусы приходят в
    `message.status_update` webhook → process_wazzup_payload.

    Эта функция теперь только ищет зависшие 'queued' / 'sent' сообщения,
    которые старше N часов и должны были получить delivered/read, но не
    получили (например, webhook'и потерялись). В таком случае помечаем их
    как 'sent' с предупреждением, чтобы не висели вечно.
    """
    horizon = datetime.now(timezone.utc).timestamp() - 4 * 3600  # 4 hours
    with db.db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT m.id, m.channel, m.message_id, m.sent_at, m.status
            FROM messages m
            WHERE m.message_id IS NOT NULL AND m.message_id != ''
              AND m.status IN ('queued', 'sent', 'delivered')
              AND m.read_at IS NULL
              AND m.sent_at IS NOT NULL
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (config.STATUS_POLL_LIMIT,),
        )
        rows = c.fetchall()
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            sent_at = row["sent_at"]
            if sent_at:
                try:
                    sent_dt = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
                except Exception:
                    sent_dt = now
                if sent_dt.timestamp() > horizon:
                    continue
            # Wazzup webhook не доставил финального статуса. Оставляем sent,
            # помечаем last_error, чтобы оператор видел "что-то не так".
            log.warning(
                f"Reconcile: msg_id={row['message_id']} висит в '{row['status']}' "
                f"без финального статуса >4ч — webhook'и от Wazzup могли потеряться"
            )
        except Exception as e:
            log.warning(
                f"reconcile_message_statuses error for {row['message_id']}: {e}"
            )


def reconcile_sheets_queue() -> None:
    with db.db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, op, message_pk, payload, attempts FROM sheets_ops "
            "WHERE done = 0 ORDER BY id ASC LIMIT 100"
        )
        rows = c.fetchall()
    for row in rows:
        if row["attempts"] >= config.SHEETS_RETRY_LIMIT:
            log.error(f"Sheet op {row['id']} превысил лимит попыток — отбрасываем")
            with db.db_conn() as conn:
                c = conn.cursor()
                c.execute("UPDATE sheets_ops SET done = -1 WHERE id = ?", (row["id"],))
            continue
        try:
            payload = json.loads(row["payload"])
            row_data = sheets.SHEETS.build_row_payload(**payload)
            ok = sheets.SHEETS.append_or_update(
                row_data=row_data,
                phone=payload.get("phone", ""),
                channel=payload.get("channel", ""),
                message_id=payload.get("message_id", ""),
            )
            with db.db_conn() as conn:
                c = conn.cursor()
                if ok:
                    c.execute("UPDATE sheets_ops SET done = 1 WHERE id = ?", (row["id"],))
                    log.info(f"Sheet op {row['id']} retried successfully")
                else:
                    c.execute(
                        "UPDATE sheets_ops SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                        ("append_or_update returned False", row["id"]),
                    )
        except Exception as e:
            with db.db_conn() as conn:
                c = conn.cursor()
                c.execute(
                    "UPDATE sheets_ops SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                    (str(e)[:500], row["id"]),
                )
            log.warning(f"Sheet op {row['id']} retry error: {e}")


def reconcile_incoming_replies(limit_contacts: int = 100) -> None:
    """Подтягивает пропущенные входящие ответы.

    После миграции на Wazzup24 эта функция стала no-op: Wazzup не имеет
    GET API для истории чатов — все входящие приходят через webhook
    `message.add` (см. process_wazzup_payload → process_incoming_message).
    Функция оставлена как stub для обратной совместимости вызовов из
    reconcile_loop, чтобы ничего не падало.
    """
    return


def reconcile_loop(stop_event: threading.Event) -> None:
    log.info(f"Reconcile loop started, interval={config.RECONCILE_INTERVAL}s")
    while not stop_event.is_set():
        try:
            reconcile_pending_webhooks()
        except Exception as e:
            log.error(f"reconcile_pending_webhooks: {e}")
        try:
            reconcile_message_statuses()
        except Exception as e:
            log.error(f"reconcile_message_statuses: {e}")
        try:
            reconcile_incoming_replies()
        except Exception as e:
            log.error(f"reconcile_incoming_replies: {e}")
        try:
            reconcile_sheets_queue()
        except Exception as e:
            log.error(f"reconcile_sheets_queue: {e}")
        stop_event.wait(config.RECONCILE_INTERVAL)
    log.info("Reconcile loop stopped")


_reconcile_thread: Optional[threading.Thread] = None
_reconcile_stop: Optional[threading.Event] = None


def start_reconcile_loop() -> threading.Thread:
    global _reconcile_thread, _reconcile_stop
    if _reconcile_thread and _reconcile_thread.is_alive():
        return _reconcile_thread
    _reconcile_stop = threading.Event()
    _reconcile_thread = threading.Thread(
        target=reconcile_loop,
        args=(_reconcile_stop,),
        name="reconcile",
        daemon=True,
    )
    _reconcile_thread.start()
    return _reconcile_thread


def stop_reconcile_loop() -> None:
    if _reconcile_stop is not None:
        _reconcile_stop.set()
