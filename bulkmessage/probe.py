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
import sqlite3
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
    # WABA отвечает так через эхо уже ПОСЛЕ принятой отправки, то есть вердикт
    # «absent» приходит не синхронно с пробой. Без этого маркера номер с
    # «Number may not be on WhatsApp» вечно занимал один из 250 слотов квоты.
    "number may not be on whatsapp",
)

# Синхронный отказ = аккаунта нет. Остальное (auth, rate limit, транзиент)
# НЕ является ответом о существовании аккаунта и не должно им помечаться.
_PRESENT_STATUSES = frozenset({200, 201, 202})


class ProbeNetworkDisabled(RuntimeError):
    """Попытка реального сетевого вызова, когда сеть запрещена."""


# Стоп-кра для тестов. Проба — единственный путь в sender, который дёргает сеть
# МИМО подменяемого транспорта: _run_one_contact тесты всегда подменяют, а
# probe.probe_contact_channels — нет. Без этого флага прогон тестов шлёт
# НАСТОЯЩИЕ сообщения реальным номерам: suite выполняется с DRY_RUN=0, и
# добавление фазы личных слотов в _run_all_phases вскрыло это.
_NETWORK_ENABLED = True


def set_network_enabled(enabled: bool) -> None:
    """Разрешить или запретить реальные вызовы из пробы. Тесты зовут False."""
    global _NETWORK_ENABLED
    _NETWORK_ENABLED = bool(enabled)


def network_enabled() -> bool:
    return _NETWORK_ENABLED


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
    if not _NETWORK_ENABLED and not dry:
        raise ProbeNetworkDisabled(
            "probe.probe_channel: сеть запрещена (set_network_enabled(False)). "
            "Тест пытается отправить реальное сообщение."
        )
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
    # Проба-отправка — та же реальная отправка, что и обычная фаза: SAFETY CAP
    # обязан её видеть. Раньше счётчик попыток стоял в теле цикла фазы, и проба
    # в него не попадала — при инверсии фаз (max,telegram,waba) она стала
    # основным источником отправок в личные каналы.
    state.increment_attempts(current)
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
    # Прозрачность. Отправка в личный канал — та же реальная отправка, что и
    # в WABA, и владелец обязан её видеть. Раньше проба была единственным
    # путём в MAX/Telegram и молчала полностью: 09.10 в личку пришло
    # 9 сообщений о WABA и ни одного о личных каналах.
    try:
        from . import notify as _notify
        _notify.contact_sent(
            result.phone, contact.get("name") or "—",
            result.channel, detail=result.message_id or "",
        )
    except Exception as e:
        log.debug(f"tg-уведомление о пробе не отправилось: {e}")
    return contact_id


def _channel_usable(channel: str, current: dict) -> bool:
    """Можно ли СЕЙЧАС слать в этот канал.

    Проба-отправка списывает квоту личного канала ровно так же, как обычная
    фаза: успешный ответ = сообщение человеку. Поэтому канал, у которого
    квота исчерпана или который стоит в halt, проба обязана пропустить ДО
    сетевого вызова.

    Именно это обеспечивает приоритет владельца: лимит MAX кончился → тот же
    контакт уходит в Telegram, а не теряется.
    """
    if state.is_channel_halted(current, channel):
        log.info(f"   🔎 probe {channel}: канал в halt — пропускаю")
        return False
    if not state.channel_has_quota_with_headroom(current, channel):
        log.info(f"   🔎 probe {channel}: квота исчерпана — пропускаю")
        return False
    return True


def probe_contact_channels(
    contact: dict,
    channels: Optional[list[str]] = None,
    templates: Optional[dict[str, str]] = None,
) -> list[ProbeResult]:
    """Пробит каналы контакта по очереди, останавливаясь на первом успехе.

    Порядок берётся из ``config.PROBE_CHANNELS``. Фактически это
    ``telegram,max`` — решение владельца 2026-10-10. Прежний порядок
    ``max,telegram`` (решение 2026-10-09) был отменён, когда MAX ушёл в
    state='qr': при выходе из строя одного канала он больше не стоит первым.

    Логика: есть Telegram → сообщение в Telegram и СТОП, MAX не трогаем.
    Это осознанно: MAX проверяется только для тех, у кого Telegram не
    нашлось, иначе MAX съедал бы проверки на тех, где его всё равно нет.
    Нет ни одного личного аккаунта → контакт уходит в WABA.

    ⚠️ Докстринг раньше утверждал, что дефолт — ``max,telegram``, и это было
    неверно: код читал ``config.PROBE_CHANNELS``, а в конфиге стояло наоборот.
    Код был правильным, комментарий вводил в заблуждение при разборе.
    """
    channels = list(channels or getattr(config, "PROBE_CHANNELS", ["max", "telegram"]))
    templates = templates if templates is not None else load_templates()
    text = build_message(contact, templates)
    results: list[ProbeResult] = []
    current = state.load_state()

    for channel in channels:
        if not _channel_usable(channel, current):
            continue
        # Дневной потолок ПРОВЕРОК (отдельно от квоты сообщений). У MAX
        # проверка номера — самостоятельная активность со своим потолком
        # (1–10/день safe, 20/день, массовые запросы → бан). Без этой
        # проверки 400 проб за прогон уехали бы в аккаунт владельца.
        _pb = state.probe_budget_left(current, channel)
        if _pb == 0:
            log.info(
                f"   🔎 probe {channel}: потолок проверок на сегодня исчерпан"
            )
            continue
        if not db.probe_is_needed(
            contact["contact_id"], channel, config.PROBE_TTL_HOURS
        ):
            log.debug(
                f"   🔎 probe {channel} {contact['phone']}: свежий вердикт, пропуск"
            )
            continue
        # Считаем ДО вызова: если процесс упадёт на сети, обращение к чекеру
        # всё равно состоялось, и потолок обязан это учитывать.
        _c = state.load_state()
        state.increment_probe_check(_c, channel)
        state.save_state(_c)
        result = probe_channel(contact["phone"], channel, text)
        record_probe_result(result, contact, text)
        results.append(result)
        if result.delivered:
            break
        # Каждый вызов двигает счётчики дня, поэтому к следующему каналу
        # состояние обязано быть свежим: лимит мог кончиться прямо на пробе.
        current = state.load_state()
        # Пауза — защита живого личного аккаунта. У MAX она длиннее: там
        # серия проверок подряд и есть тот самый «массовый запрос».
        if not config.DRY_RUN:
            _pause = int(getattr(config, f"PROBE_{channel.upper()}_INTERVAL_MIN", 0)
                         or config.PROBE_INTERVAL_MIN)
            if _pause > 0 and results:
                time.sleep(_pause)
    return results


def report_miss(contact: dict, results: list) -> None:
    """Нет ни одного личного мессенджера — сообщаем в бот по каждому контакту.

    Требование владельца 2026-10-11: в Telegram должно быть видно то же, что
    в терминале, включая «у кого каких каналов нет». До этого строка жила
    только в логе на уровне INFO.

    Общая точка для обоих мест разведки (фаза PROBE и личное окно), потому
    что дублировать вызовы в sender — значит через месяц забыть про один из
    них и снова получить дыру в логе.
    """
    try:
        from . import notify as _notify
        verdicts = {r.channel: r.verdict for r in results}
        _notify.probe_miss(
            contact.get("phone", ""), contact.get("name") or "", verdicts
        )
    except Exception as e:  # noqa: BLE001 — лог не должен ронять разведку
        log.debug(f"probe_miss не отправился: {e}")


def run_phase(
    contacts: list[dict],
    log_fn=log,
    stop_requested: Optional[dict] = None,
) -> dict[str, int]:
    """Фаза PROBE: проверяет наличие аккаунта у всех переданных контактов.

    Соблюдает PROBE_MAX_PER_RUN и останавливается по stop_requested.

    Пауза между контактами обязательна. Живой случай 2026-10-09: цикл шёл
    подряд, без единого sleep, и реальные сообщения ушли в Telegram с
    разрывом 0.45 и 0.60 секунды — восемь отправок за полторы минуты. Для
    личного аккаунта это и есть сигнал бота.

    Темп двух уровней:
      * вердикт ``absent`` — сообщение НИКОМУ не уходит (синхронный HTTP 400),
        риска для человека нет, пауза здесь только вежливость к API;
      * вердикт ``present`` — сообщение РЕАЛЬНО ушло человеку, здесь пауза
        обязана быть полной (PROBE_SEND_INTERVAL_MIN), а не двухсекундной.

    Отчёт о покрытии пула уходит в Telegram на каждом прогоне: владельцу
    нужно видеть не «сколько отправили», а «у кого вообще есть куда писать».
    """
    stats = {"probed": 0, "present": 0, "absent": 0, "unknown": 0, "skipped": 0}
    if not getattr(config, "PROBE_ENABLED", True):
        log_fn.info("   🔎 PROBE выключен (BULK_PROBE_ENABLED=0) — пропуск")
        return stats

    absent_pause = max(0, int(config.PROBE_INTERVAL_MIN))
    send_pause = max(
        absent_pause,
        int(getattr(config, "PROBE_SEND_INTERVAL_MIN", absent_pause)),
    )
    # В DRY_RUN ни одного сетевого вызова не происходит, поэтому паузы только
    # замедляют прогон и ничего не защищают.
    _should_pace = not config.DRY_RUN

    log_fn.info(
        f"🔎 ФАЗА PROBE · {len(contacts)} контактов · "
        f"лимит {config.PROBE_MAX_PER_RUN} · пауза между отправками "
        f"{send_pause} с, между пропами без адресата {absent_pause} с"
    )
    for idx, contact in enumerate(contacts):
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
        delivered_here = False
        for res in results:
            if res.verdict in stats:
                stats[res.verdict] += 1
            if res.delivered:
                delivered_here = True

        if delivered_here:
            # Человеку только что ушло сообщение. Следующую пробу откладываем
            # на полный интервал — иначе серия реальных отправок в личный
            # канал выглядит как робот ровно так, как жаловался владелец.
            if _should_pace and idx + 1 < len(contacts) and not (
                stop_requested and stop_requested.get("v")
            ):
                log_fn.debug(f"   🔎 пауза {send_pause} с после отправки")
                _interruptible_sleep(send_pause, stop_requested)
        else:
            # Ни одного личного мессенджера: контакт уходит в WABA. Это
            # самый массовый исход разведки (~7 из 8 проверок при доле
            # попаданий 13%), и он раньше был виден ТОЛЬКО в терминале.
            report_miss(contact, results)
            if absent_pause and _should_pace and idx + 1 < len(contacts):
                _interruptible_sleep(absent_pause, stop_requested)

    # Отчёт о покрытии — НЕ здесь. Владелец 2026-10-10 попросил убрать его
    # из отчёта прогона: накопленные вердикты за всю историю («есть Telegram
    # 267 / всего 4442») не отвечают на вопрос «что сделал этот запуск».
    # Сводка по прогону — report_run_summary(), её зовёт sender в финале.

    return stats


def pool_coverage() -> dict:
    """Разбор пула по вердиктам: есть MAX, есть Telegram, нет ни одного.

    Считает по contact_channel_state, то есть по НАКОПЛЕННЫМ знаниям, а не по
    сегодняшнему прогону. Это накопительный актив: каждый новый вердикт
    делает следующий день дешевле.
    """
    out = {
        "total": 0, "has_max": 0, "has_telegram": 0, "has_both": 0,
        "has_none": 0, "blocked": 0, "unknown": 0, "no_whatsapp": 0,
        "has_whatsapp": 0, "has_all_three": 0, "delivered_today": {},
    }
    try:
        with db.db_conn() as conn:
            conn.row_factory = sqlite3.Row
            out["total"] = conn.execute(
                "SELECT COUNT(*) FROM contacts "
                "WHERE COALESCE(is_test,0)=0 AND COALESCE(is_invalid,0)=0"
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT contact_id, channel, verdict FROM contact_channel_state"
            ).fetchall()
    except Exception as e:
        log.warning(f"покрытие пула не прочитано: {e}")
        return out

    per: dict[int, dict[str, str]] = {}
    for r in rows:
        per.setdefault(r["contact_id"], {})[r["channel"]] = r["verdict"]

    for verdicts in per.values():
        m = verdicts.get("max")
        t = verdicts.get("telegram")
        if m == "blocked" or t == "blocked":
            out["blocked"] += 1
        good_m = m in ("present", "delivered")
        good_t = t in ("present", "delivered")
        if good_m:
            out["has_max"] += 1
        if good_t:
            out["has_telegram"] += 1
        if good_m and good_t:
            out["has_both"] += 1
        elif not good_m and not good_t:
            # absent в обоих мессенджерах — единственный выход остаётся WABA.
            if m == "absent" or t == "absent" or "absent" in verdicts.values():
                out["has_none"] += 1
            else:
                out["unknown"] += 1
        # Проверенные, но не годные ни в один мессенджер: WhatsApp их тоже
        # не спас — WABA по ним тратить квоту нельзя.
        wa_good = (verdicts.get("whatsapp") or verdicts.get("waba")) in (
            "present", "delivered",
        )
        if wa_good:
            out["has_whatsapp"] += 1
            if good_m and good_t:
                out["has_all_three"] += 1
        if any(v in ("absent", "blocked") for v in verdicts.values()):
            if not wa_good:
                out["no_whatsapp"] += 1

    # Доставлено сегодня по каналам — «сколько людей РЕАЛЬНО получили», а не
    # «сколько попыток сделано». HTTP 2xx означает лишь «принято», поэтому
    # берём delivery_state, а не status.
    try:
        with db.db_conn() as conn:
            rows = conn.execute(
                "SELECT channel, COUNT(*) n FROM messages "
                "WHERE substr(sent_at,1,10) = ? "
                "  AND delivery_state = 'delivered' "
                "GROUP BY channel ORDER BY n DESC",
                (datetime.now(timezone.utc).strftime("%Y-%m-%d"),),
            ).fetchall()
        out["delivered_today"] = {r["channel"]: r["n"] for r in rows}
    except Exception as e:
        log.debug(f"доставка за сегодня не посчитана: {e}")
    return out


def report_pool_coverage() -> None:
    """Отчёт по покрытию пула в Telegram + в лог."""
    c = pool_coverage()
    if not c["total"]:
        return

    delivered = c.get("delivered_today") or {}
    dsum = sum(delivered.values())
    if delivered:
        dl_line = "\n".join(
            f"    {ch:9s} {n}" for ch, n in sorted(
                delivered.items(), key=lambda kv: -kv[1])
        )
    else:
        dl_line = "    (подтверждённых доставок сегодня нет)"

    log.info("─" * 70)
    log.info("ПРОФИЛЬ ПУЛА (по накопленным вердиктам):")
    log.info(f"   есть MAX          {c['has_max']}")
    log.info(f"   есть Telegram     {c['has_telegram']}")
    log.info(f"   есть WhatsApp     {c['has_whatsapp']}")
    log.info(f"   есть ВСЕ ТРИ      {c['has_all_three']}")
    log.info(f"   нет ни одного      {c['has_none']}  ← единственный выход WABA")
    log.info(f"   заблокирован       {c['blocked']}")
    log.info(f"   без вердикта       {c['unknown']}")
    log.info(f"   WhatsApp нет       {c['no_whatsapp']}  ← WABA по ним тратить нельзя")
    log.info(f"   всего контактов    {c['total']}")
    log.info(f"   ДОСТАВЛЕНО СЕГОДНЯ {dsum}:")
    for line in dl_line.splitlines():
        log.info(line)
    log.info("─" * 70)
    try:
        from . import notify as _notify
        _notify.event(
            "ОТЧЁТ ПРОГОНА\n"
            "────────────────────\n"
            f"ДОСТАВЛЕНО СЕГОДНЯ: {dsum}\n{dl_line}\n\n"
            "ПРОФИЛЬ ПУЛА (накопленные вердикты):\n"
            f"  есть Telegram   {c['has_telegram']}\n"
            f"  есть MAX        {c['has_max']}\n"
            f"  есть WhatsApp   {c['has_whatsapp']}\n"
            f"  есть ВСЕ ТРИ    {c['has_all_three']}\n"
            f"  нет ни одного   {c['has_none']}  (единственный выход — WABA)\n"
            f"  заблокирован    {c['blocked']}\n"
            f"  без вердикта    {c['unknown']}\n"
            f"  WhatsApp нет    {c['no_whatsapp']}  (WABA по ним — пустая трата)\n"
            f"  всего контактов {c['total']}",
            "INFO",
        )
    except Exception as e:
        log.debug(f"отчёт по покрытию в TG не ушёл: {e}")


def report_run_summary(run_started_at: Optional[str] = None) -> dict:
    """Отчёт по КОНКРЕТНОМУ прогону, а не по накопленной базе.

    Живой случай 2026-10-10: прогон отправил 2 сообщения, а отчёт показывал
    «есть Telegram 267 / всего контактов 4442» — накопленные вердикты за всю
    историю. Владелец закономерно не понял, куда делись его 15 контактов.

    Теперь отвечает на вопрос «что сделал ЭТОТ запуск»:
      отправлено / доставлено по каждому каналу / кому некуда писать.

    ``run_started_at`` — ISO-метка начала прогона. Если не передана, берётся
    начало текущих суток UTC.
    """
    out = {"sent": 0, "accepted": 0, "delivered": {}, "no_channel": []}
    # Импорт наверху функции: pretty_phone нужен уже в блоке сбора данных,
    # а ленивый импорт стоял ниже и падал бы с NameError на первом же отчёте.
    try:
        from . import notify as _notify
    except Exception as e:  # noqa: BLE001
        log.warning(f"отчёт прогона без форматирования номера: {e}")

        class _NotifyStub:
            @staticmethod
            def pretty_phone(p: str) -> str:
                return p
        _notify = _NotifyStub()
    try:
        if not run_started_at:
            run_started_at = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()

        with db.db_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT m.channel, m.status, m.delivery_state, m.contact_id, "
                "       ct.phone, ct.name "
                "FROM messages m JOIN contacts ct ON ct.id = m.contact_id "
                "WHERE m.sent_at >= ? AND m.message_id IS NOT NULL "
                "  AND m.message_id NOT LIKE 'dryrun%'",
                (run_started_at,),
            ).fetchall()

            out["sent"] = len(rows)
            per: dict[str, dict[str, int]] = {}
            touched: set[int] = set()
            for r in rows:
                ch = r["channel"]
                slot = per.setdefault(ch, {"accepted": 0, "delivered": 0,
                                           "rejected": 0})
                if r["status"] in ("sent", "delivered", "read", "answered"):
                    slot["accepted"] += 1
                    out["accepted"] += 1
                if r["delivery_state"] == "delivered":
                    slot["delivered"] += 1
                if r["delivery_state"] == "rejected":
                    slot["rejected"] += 1
                touched.add(r["contact_id"])
            out["delivered"] = {c: v["delivered"] for c, v in per.items()}
            out["accepted_by_channel"] = {c: v["accepted"] for c, v in per.items()}

            # Кому из этого прогона писать некуда: ни один канал не подтверждён.
            if touched:
                marks = ",".join("?" * len(touched))
                no_ch = conn.execute(
                    "SELECT ct.phone, ct.name FROM contact_channel_state s "
                    "JOIN contacts ct ON ct.id = s.contact_id "
                    f"WHERE s.contact_id IN ({marks}) "
                    "  AND s.verdict IN ('present','delivered') "
                    "GROUP BY s.contact_id HAVING COUNT(*) = 0",
                    tuple(touched),
                ).fetchall()
                out["no_channel"] = [
                    f"{_notify.pretty_phone(r['phone'])} {r['name'] or '—'}"
                    for r in no_ch
                ]
    except Exception as e:
        log.warning(f"отчёт по прогону не собрался: {e}")
        return out

    def _n(ch: str, key: str) -> int:
        return out.get(key, {}).get(ch, 0)

    log.info("─" * 70)
    log.info("ОТЧЁТ ПО ЭТОМУ ПРОГОНУ:")
    log.info(f"   отправлено сообщений      {out['sent']}")
    log.info(f"   принято транспортом       {out['accepted']}")
    log.info(f"   доставлено в Telegram     {_n('telegram', 'delivered')}")
    log.info(f"   доставлено в MAX          {_n('max', 'delivered')}")
    log.info(f"   доставлено в WhatsApp     {_n('waba', 'delivered')}")
    log.info(f"   доставлено в WhatsApp(личн){_n('whatsapp', 'delivered')}")
    log.info(f"   нет ни одного канала      {len(out['no_channel'])}")
    for line in out["no_channel"][:20]:
        log.info(f"      {line}")
    log.info("─" * 70)

    try:
        from . import notify as _notify  # noqa: F811 — уже импортирован выше
        lines = [
            "ОТЧЁТ ПО ПРОГОНУ",
            "────────────────────",
            f"отправлено: {out['sent']}",
            f"принято:    {out['accepted']}",
            f"доставлено в тг:     {_n('telegram', 'delivered')}",
            f"доставлено в макс:   {_n('max', 'delivered')}",
            f"доставлено в ватсап: {_n('waba', 'delivered')}",
        ]
        wa = _n("whatsapp", "delivered")
        if wa:
            lines.append(f"доставлено в ватсап (личный): {wa}")
        lines.append("")
        lines.append(f"нет ни одного канала: {len(out['no_channel'])}")
        for line in out["no_channel"][:20]:
            lines.append(f"   {line}")
        if len(out["no_channel"]) > 20:
            lines.append(f"   … и ещё {len(out['no_channel']) - 20}")
        _notify.event("\n".join(lines), "INFO")
    except Exception as e:
        log.debug(f"отчёт по прогону в TG не ушёл: {e}")
    return out


def _deadline_passed(stop_requested: Optional[dict]) -> bool:
    """Истёк ли дедлайн прогона (--run-hours). Ленивый импорт — иначе цикл.

    Живой случай 2026-10-11: пауза между успешными пробами 300 с, и всё это
    время процесс СПАЛ. Проверка дедлайна жила только на границах цикла,
    поэтому прогон с `--run-hours 0.33` отработал 25 минут вместо 20 и ушёл
    за дедлайн ещё с одной отправкой. Владелец задал время — прогон обязан
    уложиться в него, а не «примерно в него».
    """
    try:
        from . import sender as _sender
        if _sender._RUN_DEADLINE is None:
            return False
        import time as _t
        if _t.time() < _sender._RUN_DEADLINE:
            return False
    except Exception as e:  # noqa: BLE001 — проверка не должна ронять сон
        log.debug(f"проверка дедлайна не сработала: {e}")
        return False
    if stop_requested is not None:
        stop_requested["v"] = True
    log.info("Дедлайн прогона истёк — выхожу из фазы разведки")
    return True


def _interruptible_sleep(seconds: float, stop_requested: Optional[dict]) -> None:
    """Сон кусками по 30 с, чтобы Ctrl+C и дедлайн не ждали лишнего."""
    remaining = float(seconds)
    while remaining > 0:
        if stop_requested and stop_requested.get("v"):
            return
        if _deadline_passed(stop_requested):
            return
        chunk = min(30.0, remaining)
        time.sleep(chunk)
        remaining -= chunk