"""Webhook event processing and reconciliation loops."""

from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

from . import config, db, sheets, wappi
from .sheets import sync_message_to_sheet


log = config.get_logger("reconcile")


# ============================================================
# Webhook payload processing
# ============================================================


def process_incoming_message(channel: str, msg: dict) -> None:
    raw_text = (msg.get("body") or "").strip()
    msg_type = (msg.get("type") or "chat").lower()
    wappi_message_id = msg.get("id") or msg.get("message_id")
    if msg_type != "chat" and not raw_text:
        raw_text = f"[{msg_type}]"

    phone = wappi.extract_phone_from_from(msg.get("from")) or wappi.extract_phone_from_chat(
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
        db.add_reply(conn, contact_id, channel, raw_text, wappi_message_id)
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


def process_delivery_status(channel: str, msg: dict) -> None:
    wappi_message_id = msg.get("id") or msg.get("message_id")
    status = wappi.wappi_status_to_local(msg.get("status"))
    if not wappi_message_id:
        log.warning("delivery_status: нет message_id")
        return
    log.info(
        f"📬 DELIVERY status: msg_id={wappi_message_id} channel={channel} -> {status}"
    )
    with db.db_conn() as conn:
        row = db.find_message_by_wappi_id(conn, wappi_message_id)
        if row is None:
            phone = wappi.extract_phone_from_chat(msg.get("chat_id") or msg.get("to"))
            if phone:
                contact = db.get_contact_by_phone(conn, phone)
                if contact is not None:
                    target = db.get_latest_inbound_target(conn, contact["id"], channel)
                    if target is not None and not target["message_id"]:
                        db.update_message_status(
                            conn,
                            message_pk=target["id"],
                            wappi_message_id=wappi_message_id,
                        )
                        row = db.find_message_by_wappi_id(conn, wappi_message_id)
        if row is None:
            log.warning(
                f"delivery_status: сообщение {wappi_message_id} не найдено в БД"
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


def process_outgoing_message(channel: str, msg: dict) -> None:
    wappi_message_id = msg.get("id") or msg.get("message_id")
    if not wappi_message_id:
        return
    log.info(f"📤 OUTGOING event: msg_id={wappi_message_id} channel={channel}")
    with db.db_conn() as conn:
        row = db.find_message_by_wappi_id(conn, wappi_message_id)
        if row is None:
            return
        if row["status"] in ("queued", "sent"):
            db.update_message_status(conn, message_pk=row["id"], status="sent")


def process_webhook_payload(channel_hint: Optional[str], payload: dict) -> None:
    if not isinstance(payload, dict):
        return
    if "messages" in payload:
        data = payload["messages"]
    else:
        data = payload
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = [data]
    else:
        return

    for msg in items:
        if not isinstance(msg, dict):
            continue
        wh_type = (msg.get("wh_type") or "").lower()
        ch = channel_hint or wappi.channel_from_profile_id(msg.get("profile_id"))
        if ch not in config.WAPPI_TOKENS:
            ch = wappi.channel_from_profile_id(msg.get("profile_id"))
        if not ch:
            log.debug(
                f"webhook: не удалось определить канал (profile_id={msg.get('profile_id')})"
            )
            continue
        try:
            if wh_type == "incoming_message":
                process_incoming_message(ch, msg)
            elif wh_type == "delivery_status":
                process_delivery_status(ch, msg)
            elif wh_type in ("outgoing_message_api", "outgoing_message_phone"):
                process_outgoing_message(ch, msg)
            elif wh_type in ("authorization_status", "application_status", "incoming_call"):
                log.info(f"Ignored wh_type={wh_type}")
            else:
                # Fallback detection
                if "status" in msg and "id" in msg and "chat_id" in msg:
                    process_delivery_status(ch, msg)
                elif "from" in msg or "chatId" in msg or "chat_id" in msg:
                    process_incoming_message(ch, msg)
        except Exception as e:
            log.error(f"webhook handler error ({wh_type}): {e}\n{traceback.format_exc()}")


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
    with db.db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT m.id, m.channel, m.message_id, m.sent_at, m.status
            FROM messages m
            WHERE m.message_id IS NOT NULL AND m.message_id != ''
              AND (m.status IN ('queued', 'sent', 'delivered')
                   OR m.read_at IS NULL)
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
                if (now - sent_dt).total_seconds() < config.STATUS_POLL_AGE_SEC:
                    continue
            data = wappi.fetch_wappi_status(row["channel"], row["message_id"])
            if not data:
                continue
            inner = data.get("message") or data
            delivery_status = (
                inner.get("delivery_status")
                or inner.get("status")
                or (inner.get("isRead") and "read")
            )
            is_read = bool(inner.get("isRead"))
            status = wappi.wappi_status_to_local(delivery_status)
            if is_read:
                status = "read"
            if status == row["status"]:
                continue
            log.info(
                f"Reconcile: msg_id={row['message_id']} {row['status']} -> {status}"
            )
            with db.db_conn() as conn:
                kwargs: dict = {"message_pk": row["id"], "status": status}
                if status == "delivered":
                    kwargs["delivered_at"] = True
                elif status == "read":
                    kwargs["delivered_at"] = True
                    kwargs["read_at"] = True
                elif status == "failed":
                    kwargs["last_error"] = "reconcile: failed"
                db.update_message_status(conn, **kwargs)
            sync_message_to_sheet(
                contact_id=None,
                channel=row["channel"],
                status=status,
                message_pk=row["id"],
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
    """Подтягивает пропущенные входящие ответы через GET /api/sync/messages/get.

    Используется, когда webhook'и от Wappi не дошли (наш сервер был недоступен).
    Для каждого контакта с отправленными сообщениями опрашивает Wappi с момента
    last_activity и добавляет входящие сообщения как reply.
    """
    with db.db_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT ct.id AS contact_id, ct.phone, ct.name,
                   m.channel,
                   MAX(COALESCE(m.updated_at, m.sent_at, m.created_at)) AS last_activity,
                   MAX(COALESCE(m.read_at, m.answered_at, m.delivered_at, m.sent_at)) AS last_event
            FROM contacts ct
            JOIN messages m ON m.contact_id = ct.id
            WHERE m.status IN ('sent', 'delivered', 'read', 'answered')
            GROUP BY ct.id, m.channel
            ORDER BY last_activity DESC
            LIMIT ?
            """,
            (limit_contacts,),
        )
        rows = c.fetchall()

    for row in rows:
        contact_id = row["contact_id"]
        phone = row["phone"]
        channel = row["channel"]
        last_activity = row["last_activity"]
        try:
            since_iso = None
            if last_activity:
                # Wappi принимает date в формате YYYY-mm-ddTHH:MM:ss
                try:
                    dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                    since_iso = dt.strftime("%Y-%m-%dT%H:%M:%S")
                except Exception:
                    since_iso = None

            messages = wappi.fetch_chat_messages(
                channel, phone, since_iso=since_iso, limit=50
            )
            if not messages:
                continue

            # Уже сохранённые wappi_message_id (чтобы не дублировать)
            with db.db_conn() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT wappi_message_id FROM replies "
                    "WHERE contact_id = ? AND channel = ? AND wappi_message_id IS NOT NULL",
                    (contact_id, channel),
                )
                already = {r["wappi_message_id"] for r in c.fetchall()}

            new_count = 0
            for msg in messages:
                # Входящее = fromMe == False (или from не наш профиль)
                if msg.get("fromMe") is True:
                    continue
                wappi_mid = msg.get("id") or msg.get("message_id")
                if wappi_mid and wappi_mid in already:
                    continue
                body = (msg.get("body") or "").strip()
                msg_type = (msg.get("type") or "chat").lower()
                if not body and msg_type:
                    body = f"[{msg_type}]"
                if not body:
                    continue
                with db.db_conn() as conn:
                    db.add_reply(conn, contact_id, channel, body, wappi_mid)
                    target = db.get_latest_inbound_target(conn, contact_id, channel)
                    if target is not None and target["status"] != "answered":
                        db.update_message_status(
                            conn,
                            message_pk=target["id"],
                            status="answered",
                            answered_at=True,
                        )
                        message_pk = target["id"]
                    else:
                        message_pk = None
                new_count += 1
                # Sync ALL outbound messages for this contact+channel,
                # не только тот, на который пришёл ответ
                with db.db_conn() as conn:
                    c = conn.cursor()
                    c.execute(
                        "SELECT id FROM messages WHERE contact_id = ? AND channel = ? AND message_id IS NOT NULL AND message_id != ''",
                        (contact_id, channel),
                    )
                    all_message_pks = [r["id"] for r in c.fetchall()]
                for mpk in all_message_pks:
                    try:
                        sync_message_to_sheet(
                            contact_id=contact_id,
                            channel=channel,
                            status="answered",
                            message_pk=mpk,
                        )
                    except Exception as ex:
                        log.warning(f"resync to sheet failed: {ex}")

            if new_count:
                log.info(
                    f"Reconcile replies: {phone}/{channel} -> "
                    f"+{new_count} missed reply(ies)"
                )
        except Exception as e:
            log.warning(
                f"reconcile_incoming_replies error for {phone}/{channel}: {e}"
            )


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
