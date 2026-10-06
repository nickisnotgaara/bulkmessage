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

    # --- Авто-отписка --------------------------------------------------------
    # Человек написал «стоп» — это отказ от рассылки. Зак. ст.18 ФЗ-38 требует
    # прекратить отправку по его отказу, а Meta считает игнорирование отписки
    # жалобой. Ловим и русские, и английские формулировки.
    _low = raw_text.strip().lower().strip(".,!?… ")
    _optout_stems = (
        "стоп", "stop", "stop_", "unsubscribe", "отпишись", "отписка",
        "не пиши", "не пишите", "не беспокоить", "не надо писать",
        "не interested", "удали", "удалите", "удали меня", "отказаться",
    )
    if any(_low.startswith(s) or s in _low for s in _optout_stems):
        try:
            db.set_consent(phone, db.CONSENT_DENIED, source=f"auto_optout:{channel}")
            log.warning(
                f"🚫 OPT-OUT: {phone} отказался от рассылки "
                f"(«{raw_text[:40]}») — больше не пишем ни в один канал"
            )
        except Exception as e:
            log.warning(f"opt-out save failed for {phone}: {e}")

    # 24ч-окно WABA открывает ОТВЕТ КОНТАКТА. Раньше это не фиксировалось
    # вообще, поэтому sender всегда считал окно закрытым и слал шаблон даже
    # тем, кто ответил (упущенная возможность), а на повторном касании
    # наоборот уходил в free text не отвечавшему (Meta → 131047).
    if channel in ("waba", "whatsapp"):
        try:
            from datetime import datetime, timezone as _tz
            from . import state as _st
            _cur = _st.load_state()
            _st.reset_daily_if_new_day(_cur)
            _st.set_waba_inbound_meta(
                _cur, phone, datetime.now(_tz.utc).isoformat())
            _st.save_state(_cur)
            log.info(
                f"   🪟 24ч-окно WABA открыто для {phone} (ответ контакта)"
            )
        except Exception as e:
            log.warning(f"waba inbound window update failed: {e}")

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
            # Причина отказа лежит в `detail`, а status всегда равен
            # 'error'/'failed'. Раньше сюда писался status, и в БД у всех
            # провалов стояло одинаковое 'error' — разобрать, что именно
            # сломалось (PHONE_NOT_OCCUPIED / PEER_FLOOD / rate-limit),
            # было невозможно. В rescue-очередь причина шла правильно,
            # в messages терялась.
            _reason = (
                msg.get("detail")
                or (msg.get("error") if isinstance(msg.get("error"), str) else None)
                or status
            )
            kwargs.update({
                "status": "failed",
                "last_error": str(_reason)[:200],
            })
        else:
            kwargs["status"] = status
        db.update_message_status(conn, **kwargs)
        contact_id = row["contact_id"]
        message_pk = row["id"]

    sync_message_to_sheet(
        contact_id=contact_id, channel=channel, status=status, message_pk=message_pk
    )

    # Phase 8: доставка подтверждена вебхуком — только теперь это «доставлено».
    # accepted_today уже был увеличен в момент HTTP 2xx; здесь мы доводим
    # счётчик доставки. Именно эта пара счётчиков делает fallback возможным:
    # без подтверждения неизвестно, дошло сообщение или нет.
    try:
        from . import state as state_mod
        cur_state = state_mod.load_state()
        cur_state = state_mod.reset_daily_if_new_day(cur_state)
        if status in ("delivered", "read"):
            state_mod.record_delivered(cur_state, channel)
            state_mod.record_delivery_outcome(cur_state, channel, ok=True)
            # Cooldown по паре (номер, канал): только подтверждённая доставка
            # включает откат на 96ч. Неподтверждённая отправка (свип уведёт её
            # в rescue) контакт не должна запирать.
            # `phone` сверху — это ОТПРАВИТЕЛЬ в status-вебхуке, поэтому
            # номер берём из БД по contact_id, а не из payload.
            if contact_id:
                with db.db_conn() as conn3:
                    row3 = conn3.execute(
                        "SELECT phone FROM contacts WHERE id = ?", (contact_id,)
                    ).fetchone()
                if row3 and row3["phone"]:
                    from datetime import datetime as _dt, timezone as _tz
                    state_mod.record_delivery_confirmed(
                        cur_state, row3["phone"], channel, _dt.now(_tz.utc).isoformat()
                    )
            log.info(
                f"   📊 доставлено подтверждено: {channel} "
                f"delivered_today={state_mod.channel_delivered_today(cur_state, channel)}"
            )
        elif status == "failed":
            state_mod.record_failed_delivery(cur_state, channel)
            # WABA: явный failed от вебхука — единственный сигнал, по которому
            # номер выводится из WABA-пула. reachability_cache ниже протухает
            # за 24ч, а нам номер нужно исключать надолго (по умолчанию 30
            # дней), иначе он завтра снова заберёт один из 250 слотов, хотя
            # заведомо не доходит. Phone — из БД по contact_id: в статусном
            # вебхуке поле from это ОТПРАВИТЕЛЬ.
            if channel == "waba" and contact_id:
                with db.db_conn() as conn4:
                    row4 = conn4.execute(
                        "SELECT phone FROM contacts WHERE id = ?", (contact_id,)
                    ).fetchone()
                if row4 and row4["phone"]:
                    state_mod.mark_waba_exhausted(
                        cur_state, row4["phone"],
                        (msg.get("detail") or msg.get("error") or "failed")[:200],
                    )
                    log.info(
                        f"   🚫 WABA исчерпан: {row4['phone']} исключён из "
                        f"WABA-пула на "
                        f"{getattr(config, 'WABA_EXHAUSTED_TTL_DAYS', 30)} дней"
                    )
            # Circuit breaker: серия PEER_FLOOD/FLOOD_WAIT/rate-limit гасит канал.
            # Причина лежит в detail, а не в status (status всегда "error").
            tripped = state_mod.record_delivery_outcome(
                cur_state, channel, ok=False,
                error_text=(msg.get("detail") or msg.get("status") or ""),
            )
            if tripped:
                log.error(
                    f"   🛑 CIRCUIT BREAKER: {channel} остановлен на 1ч "
                    f"после серии отказов перегрузки"
                )
        state_mod.save_state(cur_state)
    except Exception as e:
        log.warning(f"delivery accounting failed: {e}")

    # Phase 7: reachability cache update (используется cascade'ом как pre-check).
    # Если WABA delivery status == 'delivered'/'read' → этот канал reachable.
    # Если status == 'failed' → этот канал UNreachable для этого phone.
    if channel in ("waba", "whatsapp", "telegram", "max"):
        try:
            from . import state as state_mod
            cur_state = state_mod.load_state()
            cur_state = state_mod.reset_daily_if_new_day(cur_state)
            # Получаем phone через contact_id
            with db.db_conn() as conn2:
                row2 = conn2.execute(
                    "SELECT phone FROM contacts WHERE id = ?", (contact_id,)
                ).fetchone()
                if row2:
                    phone = row2["phone"]
                    reachable = status in ("delivered", "read", "sent")
                    state_mod.set_cached_reachability(
                        cur_state, phone, {channel: reachable}
                    )
                    state_mod.save_state(cur_state)
        except Exception as e:
            log.warning(f"reachability cache update failed: {e}")

    # Phase 7: rescue queue — доставка не удалась → следующий канал каскада.
    # Раньше условие было `channel == "waba"`, но канал в вебхуке без channelId
    # определяется как "whatsapp", поэтому ветка была недостижима и очередь
    # оставалась пустой при 899 отказах. Теперь rescue работает для любого канала.
    RESCUE_SKIP = ("sms",)  # SMS — последний в каскаде, откатываться некуда
    if status == "failed" and channel not in RESCUE_SKIP:
        try:
            contact_phone = None
            contact_name = None
            contact_category = None
            with db.db_conn() as conn2:
                crow = conn2.execute(
                    "SELECT c.phone, c.name, c.category FROM contacts c WHERE c.id = ?",
                    (contact_id,),
                ).fetchone()
                if crow:
                    contact_phone = crow["phone"]
                    contact_name = crow["name"]
                    contact_category = crow["category"]
            if contact_phone:
                # Причина из detail — в нём лежит реальная ошибка
                # (PHONE_NOT_OCCUPIED, no LID found, PEER_FLOOD, ...),
                # тогда как status всегда равен "error".
                reason = (msg.get("detail") or msg.get("status") or "delivery_failed")[:200]
                rid = db.enqueue_rescue(
                    phone=contact_phone,
                    name=contact_name,
                    category=contact_category,
                    original_channel=channel,
                    reason=reason,
                )
                log.info(
                    f"🚑 {channel} failed → enqueued for rescue "
                    f"(id={rid}, phone={contact_phone}, reason={reason[:60]})"
                )
        except Exception as e:
            log.warning(f"rescue enqueue failed: {e}")

    # Phase 3.6: complaint halt — если error/detail сигналит о жалобе (spam/report).
    # Wazzup присылает причину строкой в `detail`, а объектного `error` в
    # доставленных статусах не бывает вовсе — поэтому раньше эта логика
    # не срабатывала ни разу и таблица complaint_events оставалась пустой.
    error_blob = msg.get("error") or msg.get("detail") or {}
    if isinstance(error_blob, dict):
        err_code = str(
            error_blob.get("error") or error_blob.get("code") or ""
        ).lower()
        err_kind = str(
            error_blob.get("description") or error_blob.get("detail") or ""
        ).lower()
    else:
        err_code = str(error_blob).lower()
        err_kind = ""
    # Нормализованный haystack: код + текст описания
    haystack = f"{err_code} {err_kind}"
    spam_markers = (
        "spam", "spam_report", "report_spam", "user_complaint", "complaint",
        "block", "blocked", "quality_red", "low_quality", "reason_blocked",
    )
    if any(m in haystack for m in spam_markers):
        try:
            _record_complaint_and_maybe_halt(
                channel, kind=(err_code or "spam_report")[:100]
            )
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


def _wazzup_chat_type_to_channel(chat_type: str, channel_id: str = "",
                                 raw_ids: Optional[list] = None) -> str:
    """Маппинг Wazzup chatType (+ channelId) → bulkmessage channel name.

    Wazzup chatType: 'whatsapp' | 'telegram' | 'max' | 'viber' | 'instagram' |
    'whatsgroup' | ''.
    chatType "whatsapp" НЕ различает Personal WA от WABA — для этого нужен
    channelId → transport (см. wazzup.channel_name_for()).

    Приоритет:
      1. Если задан channelId и он есть в кэше Wazzup → берём из кэша.
      2. Если channelId НЕ пришёл (а в реальных payload'ах он не приходит
         ни разу — 0 из 139 сохранённых событий) → определяем канал по
         номеру нашего аккаунта из raw_ids (from/to/chat_id + '@c.us').
      3. Иначе маппим по chatType (Telegram/MAX уникальны).
      4. Для chatType='whatsapp' без channelId → fallback 'whatsapp'.
    """
    from . import wazzup as _wz
    # 1) channelId — если вдруг придёт.
    if channel_id:
        name = _wz.channel_name_for(channel_id)
        if name:
            return name

    # 2) Номер нашего аккаунта из payload'а. Это рабочий путь: Wazzup шлёт
    #    'to'/'chat_id'/'from' с нашим же номером + '@c.us'.
    for raw in (raw_ids or []):
        name = _wz.channel_name_for_raw(raw, chat_type)
        if name:
            return name

    # 3) chatType — для TG/MAX он однозначен.
    ct = (chat_type or "").lower().strip()
    if ct in ("telegram", "tg", "tgapi"):
        return "telegram"
    if ct in ("max", "vk", "maxapi"):
        return "max"

    # 4) Fallback
    if ct in ("whatsapp", "waba", "whatsgroup"):
        return "whatsapp"
    return "whatsapp"  # неизвестное → как раньше (default)


def _wazzup_extract_message(msg: dict) -> dict:
    """Нормализует message payload от Wazzup к плоскому виду.

    Wazzup реально шлёт payload'ы, которые НЕ совпадают с документацией:

      - идентификатор приходит в ``id`` (а не в ``messageId``); в 132 из 138
        сохранённых событий ``delivery_status`` ключ ``messageId`` отсутствует
        полностью. Для части событий ``id`` пустой, но есть запасной ``task_id``.
      - текст сообщения приходит в ``text`` либо ``body``.
      - ошибка приходит строкой в ``detail``, а не объектом в ``error``
        (в 132 сохранённых событиях ``error`` не встречается ни разу).
      - тип канала приходит в ``platform``, а ``chatType`` в событиях
        ``delivery_status`` отсутствует (0 из 138).

    Поэтому читаем все известные варианты каждого поля.
    """
    message_id = (
        msg.get("id")
        or msg.get("messageId")
        or msg.get("message_id")
        or msg.get("task_id")
    )
    chat_id = msg.get("chatId") or msg.get("chat_id") or msg.get("to")
    body = msg.get("text") or msg.get("body") or ""
    # Wazzup отдаёт ошибку строкой в detail; некоторые каналы — объектом в error.
    error = msg.get("error")
    detail = msg.get("detail")
    if not error and detail:
        error = {"description": str(detail)}
    return {
        "id": message_id,
        "message_id": message_id,
        "body": body,
        "type": msg.get("type"),
        "chatId": chat_id,
        "chat_id": chat_id,
        "from": chat_id,  # входящее = от chatId
        "status": msg.get("status"),
        "error": error,
        "detail": str(detail) if detail else None,
        "timestamp": msg.get("timestamp"),
        "channelId": msg.get("channelId"),
        "profile_id": msg.get("profile_id"),
        # chat_type — то, чем реально пользуется вызов _wazzup_chat_type_to_channel
        "chatType": (msg.get("chatType") or msg.get("platform") or "").strip().lower(),
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
    # Wazzup реально присылает {"messages": {...}} — СЛОВАРЬ, а не список.
    # Раньше код разворачивал конверт только если это был list, поэтому 138 из 162
    # сохранённых payload'ов молча уходили в `ignored event type=''` и при этом
    # помечались обработанными. Отсюда были пустыми rescue_queue, complaint_events
    # и channel_reachability.
    items: list = []
    for key in ("messages", "events"):
        node = payload.get(key)
        if isinstance(node, list):
            items.extend(node)
        elif isinstance(node, dict):
            items.append(node)
    if not items:
        # Один объект без конверта — оборачиваем
        items = [payload]

    for evt in items:
        if not isinstance(evt, dict):
            continue
        # wh_type — тип события Wazzup. Он должен идти ПЕРЕД type, потому что
        # в реальных payload'ах поле `type` заполнено ('text'/'chat'/...) и
        # затеняло wh_type, из-за чего событие не попадало ни в одну ветку.
        evt_type = (
            evt.get("wh_type")
            or evt.get("type")
            or evt.get("event")
            or ""
        ).lower()

        try:
            # Канал: chatType у delivery_status отсутствует, но есть platform.
            # channelId — единственный способ отличить WABA от Personal WA,
            # но и он в 132 из 138 событий не приходит.
            chat_type = (
                evt.get("chatType") or evt.get("platform") or ""
            ).strip().lower()
            evt_channel_id = (evt.get("channelId") or "").strip()
            ch = channel_hint or _wazzup_chat_type_to_channel(
                chat_type, evt_channel_id,
                # Наш собственный номер: в status-событии это `from`,
                # во входящем — `chat_id`/`chatId` (уже с '@c.us').
                [evt.get("from"), evt.get("to"), evt.get("chat_id"),
                 evt.get("chatId"), evt.get("chat_id_full")],
            )

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


def sweep_unconfirmed_deliveries(limit: int = 50,
                                 minutes: int | None = None) -> int:
    """Сторож подтверждений: принято, но не подтверждено доставкой.

    ВЕБХУК — единственный источник, который запускает fallback. Если он
    потерян или подписка молчит, сообщение навсегда остаётся в статусе
    `sent`: rescue-очередь его не видит, и контакт тихо исчезает из воронки.
    Именно это и приводило к «100 отправлено → 28 дошло»: недоставки были,
    а узнать о них было негде.

    Здесь мы делаем «молчание» явным: помечаем сообщение как `failed`
    с причиной `no_delivery_confirmation` и ставим контакт в rescue-очередь,
    откуда sender отправит его следующим каналом.

    ПОЧЕМУ ЭТО КРИТИЧНО: замер боевого прогона 2026-10-06 показал, что
    Wazzup НЕ присылает статус `failed` вообще — ни разу за 1321 событие.
    Реальный набор статусов: delivered / read / sent / error(BAD_CONTACT).
    То есть отказ Meta по маркетинговому шаблону выглядит как ТИШИНА:
    сообщение принято (HTTP 2xx), и про него больше никто не пишет.
    Поэтому запускать откат только по явному `failed` бессмысленно —
    очередь всегда пуста, и фазы Telegram/MAX не отрабатывают никогда.
    Молчание и есть отказ.

    `minutes` — переопределение таймаута ожидания подтверждения. Фазы
    отката ждут PHASE_GRACE_MINUTES (минуты), а не дефолтные 60: иначе
    откат начнётся через час, когда человек уже не в окне 11–19 МСК.

    Возвращает количество контактов, отправленных в rescue.
    """
    if minutes is None:
        minutes = getattr(config, "CONFIRM_TIMEOUT_MINUTES", 60)
    max_age = getattr(config, "CONFIRM_MAX_AGE_HOURS", 24)
    try:
        rows = db.find_unconfirmed(
            minutes=minutes, limit=limit, max_age_hours=max_age
        )
    except Exception as e:
        log.warning(f"sweep_unconfirmed: выборка не удалась: {e}")
        return 0

    if not rows:
        return 0

    log.warning(
        f"🔍 Сторож подтверждений: {len(rows)} сообщений приняты, но доставка "
        f"не подтверждена за {minutes} мин (webhook молчит?) — отправляю в rescue"
    )

    swept = 0
    try:
        from . import state as state_mod
        cur_state = state_mod.load_state()
        cur_state = state_mod.reset_daily_if_new_day(cur_state)
    except Exception:
        cur_state = {}

    with db.db_conn() as conn:
        for r in rows:
            channel = r["channel"]
            try:
                db.update_message_status(
                    conn,
                    message_pk=r["message_pk"],
                    status="failed",
                    last_error="no_delivery_confirmation",
                )
            except Exception as e:
                log.warning(f"sweep: не пометить {r['message_id']}: {e}")
                continue

            # SMS — последний в каскаде, откатываться некуда
            if channel == "sms":
                continue

            try:
                db.enqueue_rescue(
                    phone=r["phone"],
                    name=r["name"],
                    category=r["category"],
                    original_channel=channel,
                    reason="no_delivery_confirmation",
                )
                swept += 1
            except Exception as e:
                log.warning(f"sweep: rescue не поставить {r['phone']}: {e}")

            try:
                if cur_state:
                    state_mod.record_failed_delivery(cur_state, channel)
                    state_mod.record_delivery_outcome(
                        cur_state, channel, ok=False,
                        error_text="no_delivery_confirmation",
                    )
            except Exception:
                pass

    if cur_state:
        try:
            state_mod.save_state(cur_state)
        except Exception:
            pass
    log.info(f"🔍 Сторож: {swept} контактов отправлены в rescue")
    return swept


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
