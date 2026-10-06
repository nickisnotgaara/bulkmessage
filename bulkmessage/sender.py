"""Sender service: long-running daemon that broadcasts messages via Wazzup24.

Все мессенджеры (WABA, Telegram, MAX, WhatsApp Personal) идут через
Wazzup24 API — это единственная точка отправки.

Per-channel backoffs and per-channel error classification: an error in one
channel (especially MAX) does not block the others.
"""

from __future__ import annotations

import csv
import os
import random
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import (
    config,
    contacts,
    db,
    bad_phones,
    failed_contacts,
    sheets,
    state,
    templates,
    wazzup,
)
from .sheets import sync_message_to_sheet
from .wazzup import ErrorKind

# SMS.RU — отдельный транспорт (НЕ Wazzup24). Импорт на уровне модуля безопасен:
# модуль не делает сетевых вызовов при импорте, всё только внутри send_sms().
from . import sms_ru


log = config.get_logger("sender")


# ---------------------------------------------------------------------------
# File lock (защита от двойного запуска)
# ---------------------------------------------------------------------------

def _try_acquire_lock() -> Optional[Path]:
    """Пытается создать lock-файл с нашим PID. Если уже есть — читает чей.

    Возвращает Path к lock-файлу при успехе, None если занято.
    """
    lock_path = Path(config.LOCK_PATH)
    try:
        if lock_path.exists():
            # Lock format: "<pid>:<hostname>" — hostname нужен чтоб различать
            # контейнеры, у которых внутри всегда PID 1.
            try:
                old_content = lock_path.read_text(encoding="utf-8").strip()
                old_hostname = ""
                if ":" in old_content:
                    old_pid_str, old_hostname = old_content.split(":", 1)
                    try:
                        old_pid = int(old_pid_str)
                    except ValueError:
                        old_pid = 0
                else:
                    old_pid = 0
                current_hostname = _container_hostname()
                # Если hostname другой — это другой контейнер, проверим PID
                if old_hostname and old_hostname != current_hostname:
                    if _pid_alive(old_pid):
                        log.error(
                            f"⛔ Sender запущен в ДРУГОМ контейнере "
                            f"(hostname={old_hostname}, PID={old_pid}). "
                            f"Второй запуск отменён."
                        )
                        return None
                    else:
                        log.warning(
                            f"⚠️  Stale lock от другого контейнера "
                            f"(hostname={old_hostname}), перезаписываю"
                        )
                elif not _pid_alive(old_pid):
                    log.warning(
                        f"⚠️  Stale lock от PID {old_pid} (процесс мёртв), "
                        f"перезаписываю"
                    )
                else:
                    log.error(
                        f"⛔ Sender уже запущен (PID={old_pid}, "
                        f"hostname={old_hostname or '?'}, lock={lock_path}). "
                        f"Второй запуск отменён."
                    )
                    return None
            except (ValueError, OSError) as e:
                log.warning(f"⚠️  Битый lock-файл ({e}), перезаписываю")
        # Пишем "<pid>:<hostname>" чтобы при следующем старте можно было отличить
        lock_content = f"{os.getpid()}:{_container_hostname()}"
        lock_path.write_text(lock_content, encoding="utf-8")
        return lock_path
    except (PermissionError, OSError) as e:
        log.error(f"⛔ Не могу создать lock-файл: {e}")
        return None


def _container_hostname() -> str:
    """Возвращает hostname контейнера (или имя машины если не в контейнере).

    Используется для различения lock-ов из разных контейнеров, у которых
    внутри всегда PID 1. Без hostname sender думает что любой PID 1 — это он.
    """
    try:
        # В Docker hostname = container ID (первые 12 символов SHA256)
        return os.environ.get("HOSTNAME", "") or __import__("socket").gethostname()
    except Exception:
        return "unknown"


def _pid_alive(pid: int) -> bool:
    """True если процесс с PID существует (кросс-платформенно)."""
    if pid <= 0:
        return False
    # Короткий путь: текущий процесс точно жив
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":
            # Windows: используем tasklist через subprocess или ctypes
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                return bool(ok) and exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        else:
            # Unix: kill -0 не убивает, но падает если процесса нет
            import errno
            try:
                os.kill(pid, 0)
            except OSError as e:
                if e.errno == errno.ESRCH:
                    return False
                if e.errno == errno.EPERM:
                    return True  # процесс есть, но не наш
            return True
    except Exception:
        return False


def _release_lock(lock_path: Optional[Path]) -> None:
    """Удаляет lock-файл (вызывать при корректном завершении)."""
    if lock_path is None:
        return
    try:
        if lock_path.exists():
            try:
                # Проверяем, что lock создан НАШИМ процессом.
                # Формат lock-а: "<pid>:<hostname>" (новый) или "<pid>" (старый).
                content = lock_path.read_text(encoding="utf-8").strip()
                our_pid = str(os.getpid())
                if content == our_pid:
                    lock_path.unlink()
                elif ":" in content:
                    file_pid, _ = content.split(":", 1)
                    if file_pid == our_pid:
                        lock_path.unlink()
                # иначе: lock чужой — не трогаем
            except (OSError, ValueError):
                pass
    except Exception as e:
        log.warning(f"⚠️  Не могу удалить lock-файл: {e}")


def _fmt_duration(seconds: float) -> str:
    """Красиво форматирует длительность: 45с / 12.3 мин / 1.5 ч."""
    if seconds is None:
        return "?"
    s = float(seconds)
    if s < 60:
        return f"{s:.0f}с"
    if s < 3600:
        return f"{s / 60:.1f} мин"
    return f"{s / 3600:.1f} ч"


def _estimate_total_time(seconds: float) -> str:
    """Оценка общего времени на 100 сообщений: Xч Yмин."""
    if seconds is None or seconds <= 0:
        return "<1мин"
    total_sec = int(seconds)
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    if h == 0 and m == 0:
        return "<1мин"
    return f"{h}ч {m:02d}мин"


def _append_log(phone: str, channel: str, status: str, detail: str = "") -> None:
    p = Path(config.LOG_PATH)
    is_new = not p.exists()
    try:
        with p.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(["phone", "channel", "status", "detail", "timestamp"])
            # Используем MSK таймстамп для согласованности с crm.db
            writer.writerow(
                [phone, channel, status, detail, config.now_tz().isoformat(timespec="seconds")]
            )
    except Exception as e:
        log.warning(f"append_log error: {e}")


def _seconds_until_midnight() -> float:
    # Считаем в настроенной TZ (BULK_TIMEZONE), иначе "сегодня" плавает
    # между часовыми поясами и дневные квоты сбрасываются не вовремя.
    from datetime import timedelta
    now = config.now_tz()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if midnight <= now:
        midnight = midnight + timedelta(days=1)
    return (midnight - now).total_seconds()


def _wait_until_active_window(log) -> bool:
    """Если мы вне активного окна — спим до его начала.

    Возвращает True если пришлось ждать (т.е. реально вышли из окна).
    Возвращает False если мы уже в окне.
    """
    secs = config.seconds_until_active_window()
    if secs <= 0:
        return False
    hrs = secs / 3600
    log.info(
        f"⏸  Вне активного окна (МСК {config.ACTIVE_HOURS_START:02d}:00-"
        f"{config.ACTIVE_HOURS_END:02d}:00). Сплю {hrs:.1f}ч до начала…"
    )
    # Спим кусками по 60 сек, чтобы Ctrl+C прерывал быстро
    slept = 0.0
    while slept < secs:
        time.sleep(min(60, secs - slept))
        slept += 60
    return True


def _next_backoff(kind: ErrorKind, current: int) -> int:
    """Вычисляет длительность backoff для данного типа ошибки.

    RATE_LIMIT — экспоненциальный рост: current*2 (с потолком MAX), старт от BASE.
    TRANSIENT — случайный в [MIN, MAX] (Wazzup моргнул, можно пробовать).
    AUTH — сразу MAX (токен надо чинить руками, частые попытки бессмысленны).
    PERMANENT/UNKNOWN — 0 (не повторится, нет смысла ждать).
    """
    if kind == ErrorKind.RATE_LIMIT:
        return min(
            (current * 2) or config.RATE_LIMIT_BACKOFF_BASE,
            config.RATE_LIMIT_BACKOFF_MAX,
        )
    if kind == ErrorKind.TRANSIENT:
        return random.randint(config.TRANSIENT_BACKOFF_MIN, config.TRANSIENT_BACKOFF_MAX)
    if kind == ErrorKind.AUTH:
        return config.RATE_LIMIT_BACKOFF_MAX
    # PERMANENT / UNKNOWN — no blocking backoff
    return 0


def _persist_success(phone: str, name: str, category: str, channel: str,
                     text: str, message_id: str, current_state: dict) -> Optional[int]:
    """Атомарная запись успешной отправки: СНАЧАЛА state, ПОТОМ DB.

    Порядок важен: если крэшнемся между save_state и insert_message,
    - state уже инкрементирован → квота списана → следующий запуск не отправит дубль
    - DB без записи → сообщение есть у получателя, но нет в логах (приемлемо,
      оператор увидит sent_today > COUNT(messages WHERE status='sent') и поймёт)
    Если бы делали DB-first, крэш оставил бы state НЕ инкрементированным,
    следующий запуск re-sent → дубль у получателя (НЕПРИЕМЛЕМО).
    """
    state.increment_channel_sent(current_state, channel)
    state.save_state(current_state)
    # NOTE: increment_successful() вызывается в _run_one_contact, не здесь
    try:
        with db.db_conn() as conn:
            contact_id = db.upsert_contact(conn, phone, name, category)
            message_pk = db.insert_message(
                conn,
                contact_id=contact_id,
                channel=channel,
                message_text=text,
                message_id=message_id,
                status="sent",
            )
        if sheets.SHEETS.enabled:
            try:
                sync_message_to_sheet(
                    contact_id=contact_id,
                    channel=channel,
                    status="sent",
                    message_pk=message_pk,
                )
                log.info(f"      📊 Google Sheets синхронизирован (pk={message_pk})")
            except Exception as e:
                log.error(f"      ⚠️ sync_message_to_sheet error: {e}")
        return message_pk
    except Exception as e:
        # DB write упал, но state уже сохранён — квота списана, дубля не будет.
        # Получатель УЖЕ получил сообщение (мы выше вызвали Wazzup), но в DB
        # записи нет. Это критическая ситуация — логируем как ERROR.
        log.error(
            f"      💥 DB WRITE FAILED после успешной отправки в {channel}! "
            f"phone={phone} message_id={message_id} error={e}. "
            f"Квота списана, но в БД записи нет. СООБЩИ ОПЕРАТОРУ."
        )
        return None


def _persist_failure(phone: str, name: str, category: str, channel: str,
                     text: str, detail: str, error_kind: str) -> None:
    """Записывает неудачную отправку в БД (только для аудита, квоту не меняет).

    Здесь же ставится запись в rescue_queue. Раньше это делал ТОЛЬКО вебхук
    (`reconcile.process_delivery_status`), поэтому синхронный отказ Wazzup
    (4xx/5xx, когда вебхук не успел или потерялся) оставлял сообщение в
    `messages` со `status='failed', message_id=None`: ни вебхук, ни сторож
    молчания такую строку уже не подбирали, и контакт просто исчезал из
    воронки без всякого отката.

    Фазы отката (TG/MAX) берут из очереди ЛЮБЫЕ неподтверждённые контакты,
    поэтому `original_channel` обязан быть именем канала, которым ОТПРАВЛЯЛИ —
    это же имя потом видно в отчёте и в разборе воронки.
    """
    try:
        with db.db_conn() as conn:
            contact_id = db.upsert_contact(conn, phone, name, category)
            db.insert_message(
                conn,
                contact_id=contact_id,
                channel=channel,
                message_text=text,
                message_id=None,
                status="failed",
                last_error=detail[:500],
                sent_at=None,
            )
            # Если это PERMANENT — добавляем в bad_phones cache
            if error_kind == "permanent" and phone and channel:
                if bad_phones.mark_bad(phone, channel):
                    log.debug(f"      📝 bad_phones: {phone} × {channel} помечен как мёртвый")

            # ── Откат: синхронный отказ тоже должен попасть в очередь ──
            # SMS — последний в каскаде, откатываться некуда.
            if channel and channel != "sms" and phone:
                try:
                    rid = db.enqueue_rescue(
                        phone=phone,
                        name=name,
                        category=category,
                        original_channel=channel,
                        reason=(detail or error_kind or "send_failed")[:200],
                    )
                    log.info(
                        f"      🚑 {channel} fail → rescue (id={rid}, "
                        f"phone={phone}) — контакт уйдёт в откат"
                    )
                except Exception as e:
                    log.warning(f"      ⚠️ Не удалось поставить в rescue: {e}")
    except Exception as e:
        log.warning(f"      ⚠️ Не удалось записать failed в БД: {e}")


def _cascade_order() -> list[str]:
    """Возвращает порядок каскада в зависимости от USE_WABA флага.

    USE_WABA=0 (default, legacy): whatsapp → telegram → max → sms.
    USE_WABA=1: waba → telegram → max → sms (основной режим).

    SMS всегда ПОСЛЕДНИЙ: это fallback-канал последней надежды, каждая SMS
    реально стоит денег (~8.5 ₽ за сегмент), поэтому до неё дело доходит
    только когда все мессенджеры провалились.
    """
    if getattr(config, "USE_WABA", 0) and config.USE_WABA >= 1:
        return ["waba", "telegram", "max", "sms"]
    # legacy
    return ["whatsapp", "telegram", "max", "sms"]


def _rescue_cascade_for(original_channel: str, current_state: dict,
                        already_tried: Optional[list] = None) -> list:
    """Каналы для rescue: следующий доступный после упавшего.

    Раньше был жёстко зашит ``["telegram", "max"]``, что:
      * игнорировало реальное состояние канала — в rescue шли даже в halt'нутый
        или уже исчерпавший квоту канал и жгли лимит впустую;
      * не включало SMS, хотя это единственный канал, работающий для людей,
        которых нет в мессенджерах (ошибка PHONE_NOT_OCCUPIED).

    Теперь отбираем каналы, которые реально могут отправить прямо сейчас.

    `already_tried` — каналы, по которым эта запись уже откатывалась.
    Без фильтра запись, не спасённая в Telegram, на следующем проходе снова
    шла в Telegram и получала второе сообщение.
    """
    order = list(_cascade_order())
    if original_channel in order:
        # откатываемся только ВПЕРЁД по каскаду: повторять упавший канал смысла нет
        idx = order.index(original_channel)
        order = order[idx + 1:]
    tried = set(already_tried or [])
    out = []
    for ch in order:
        if ch in tried:
            log.info(f"   rescue: {ch} уже пробовали для этого контакта — пропускаю")
            continue
        if ch == "sms":
            # SMS — отдельный транспорт, не Wazzup; проверяем отдельно
            if not getattr(config, "SMSRU_ENABLED", 1):
                continue
            if not getattr(config, "SMSRU_API_ID", ""):
                continue
        else:
            if state.is_channel_halted(current_state, ch):
                log.info(f"   rescue: {ch} halted — пропускаю")
                continue
            if not state.channel_has_quota_with_headroom(current_state, ch):
                log.info(f"   rescue: {ch} исчерпал квоту — пропускаю")
                continue
        out.append(ch)
    return out


def _drain_rescue_queue(current_state: dict, max_items: int = 2) -> int:
    """Phase 7: process pending rescues (WABA failed → cascade TG/MAX).

    Вызывается:
      - один раз перед основным циклом
      - между основными контактами (лимит max_items)

    Возвращает количество реально обработанных rescues.

    IMPORTANT: сохраняет/восстанавливает last_success_channel чтобы
    rescue-успех через TG/MAX НЕ замедлял следующий основной контакт.
    Без этого rescue замедлил бы основной цикл в 30-90 раз.
    """
    pending = db.pending_rescues(limit=max_items, cooldown_seconds=60)
    if not pending:
        return 0

    log.info(f"🚑 Rescue queue: {len(pending)} pending, обрабатываю до {max_items}")
    processed = 0

    # Сохраняем last_success_channel — rescue НЕ должен менять темп основного цикла
    saved_channel = current_state.get("last_success_channel", "")

    for r in pending:
        phone = r["phone"]
        name = r.get("name") or "—"
        category = r.get("category") or "—"
        log.info(
            f"🚑 Rescue [{r['id']}]: {phone} ({name}, {category}) "
            f"reason={r.get('reason', '?')}"
        )

        tried = db.rescue_tried_channels(r["id"])
        rescue_cascade = _rescue_cascade_for(
            r.get("original_channel") or "waba", current_state, tried
        )
        if not rescue_cascade:
            # Ничего не откатываем: важно НЕ тратить попытку (attempts),
            # иначе запись после 3 таких проходов станет 'failed' навсегда.
            log.info(
                f"🚑 Rescue [{r['id']}]: нет доступных каналов для отката "
                f"(исчерпаны/выключены/уже пробовали) — откладываю, "
                f"попытку не трачу"
            )
            continue

        contact = {"phone": phone, "name": name, "category": category}

        if "sms" in rescue_cascade:
            # SMS стоит денег (~8.36 ₽ за сегмент) и уходит АВТОМАТИЧЕСКИ,
            # без участия человека. Считаем стоимость заранее и логируем
            # явно, чтобы затраты были видны в отчёте, а не всплывали счётом.
            try:
                from . import sms_ru as _smsru
                _txt = templates.build_sms_message(
                    contact, templates.load_sms_templates()
                )
                n_seg = max(1, _smsru.estimate_segments(_txt))
                per_seg = float(
                    getattr(config, "SMSRU_COST_PER_SEGMENT", 8.36) or 8.36
                )
                log.info(
                    f"💸 Rescue [{r['id']}]: откат до SMS — сегментов {n_seg}, "
                    f"~{n_seg * per_seg:.2f} ₽/сообщение "
                    f"(мессенджеры недоступны или исчерпаны)"
                )
            except Exception as e:
                log.info(
                    f"💸 Rescue [{r['id']}]: откат до SMS "
                    f"(оценка стоимости не удалась: {e})"
                )

        # Помечаем ВСЕ каналы этой попытки как пройденные ДО отправки.
        # Иначе падение в Telegram оставляло бы запись с tried=''
        # и следующий проход снова дёргал бы тот же канал.
        for ch in rescue_cascade:
            db.mark_rescue_tried(r["id"], ch)

        try:
            ok = _run_one_contact(
                contact,
                rescue_cascade,
                {},                # backoffs
                {},                # permanent_skipped
                current_state,
                cascade_override=rescue_cascade,
            )
        except Exception as e:
            log.warning(f"rescue send failed for {phone}: {e}")
            ok = False

        # КОНКРЕТНЫЙ канал, а не 'telegram_or_max': _run_one_contact
        # возвращает только bool, поэтому берём last_success_channel,
        # который выставил сам успешный канал. Иначе в отчёте нельзя
        # отличить откат в Telegram от отката в MAX, а это ровно то, что
        # нужно знать при разборе «кто пережил WABA, а кто нет».
        # Читаем ДО восстановления saved_channel — иначе значение уже затёрто.
        _succ = current_state.get("last_success_channel") or ""

        # Восстанавливаем channel ДО следующего rescue (чтобы не накапливалось)
        current_state["last_success_channel"] = saved_channel

        if ok:
            db.mark_rescue_success(
                r["id"], channel=_succ or "telegram_or_max",
                message_id=f"rescued_from_{r['id']}",
            )
            log.info(f"  ✅ Rescued {phone} via {_succ or 'personal'}")
            processed += 1
        else:
            db.mark_rescue_attempt(r["id"], error="all_personal_failed")
            log.warning(f"  ❌ Rescue не удался для {phone} (attempts={r['attempts'] + 1})")

    # Финальный restore (на случай если последний rescue succeeded и сбил значение)
    current_state["last_success_channel"] = saved_channel

    if processed > 0:
        state.save_state(current_state)
    return processed


def _within_safe_hours(channel: str = "all") -> bool:
    """Phase 3.3: проверка активного и safe-окна.

    Active window: ACTIVE_HOURS_START..ACTIVE_HOURS_END (например, 10-22).
    Safe window (узкий): внутри active — для WABA 10-20, для Personal 11-19.

    Возвращает True если сейчас можно отправлять в указанный канал.
    BULK_BYPASS_SAFE_HOURS=1 → всегда True (только для тестов!).
    """
    if getattr(config, "BULK_BYPASS_SAFE_HOURS", False):
        return True
    now = config.now_tz()
    h = now.hour
    active_start = getattr(config, "ACTIVE_HOURS_START", 10)
    active_end = getattr(config, "ACTIVE_HOURS_END", 22)
    if h < active_start or h >= active_end:
        return False
    if channel == "waba":
        safe_start = getattr(config, "WABA_SAFE_HOURS_START", active_start)
        safe_end = getattr(config, "WABA_SAFE_HOURS_END", 20)
    elif channel == "sms":
        # SMS — свой канал (SMS.RU). По умолчанию 11-19 как у Personal:
        # ночные SMS жалуются чаще, а деньги SMS.RU списывает в любом случае.
        safe_start = getattr(config, "SMSRU_SAFE_HOURS_START", 11)
        safe_end = getattr(config, "SMSRU_SAFE_HOURS_END", 19)
    else:
        safe_start = getattr(config, "PERSONAL_SAFE_HOURS_START", 11)
        safe_end = getattr(config, "PERSONAL_SAFE_HOURS_END", 19)
    return safe_start <= h < safe_end


def _run_one_contact(
    contact: dict,
    channels: list[str],
    backoffs: dict[str, int],
    permanent_skipped: dict[str, set[str]],
    current_state: dict,
    cascade_override: Optional[list[str]] = None,
) -> bool:
    """Отправляет сообщение в ОДИН канал (cascade с break), не во все подряд.

    Возвращает True, если доставили хотя бы в один канал.

    Cascade order (Phase 2.2):
      USE_WABA=1: waba → telegram → max
      USE_WABA=0: whatsapp → telegram → max (legacy)

    На каждом канале:
      - если PERMANENT → mark_unreachable, пробуем следующий
      - если RATE/TRANSIENT → backoff, но НЕ пробуем следующий сразу (для sender это означает
        «сегодня не судьба», но контакт не помечается мёртвым)
      - если AUTH → halt канал, пробуем следующий
      - если успех → break

    Side effects:
      - backoffs[ch] обновляется для RATE/TRANSIENT/AUTH
      - permanent_skipped[phone] для PERMANENT
      - current_state обновляется при успехе
      - last_send_per_phone записывается при успехе
    """
    from . import waba_templates  # lazy import

    # Phase 3.3: dayparting проверяется per-channel внутри cascade loop,
    # чтобы WABA мог отправлять в свои часы (10-20), а Personal в свои (11-19),
    # даже если они не совпадают.

    phone = contact["phone"]
    name = contact.get("name", "") or "—"
    category = contact.get("category", "") or "—"

    # --- Согласие на рассылку (opt-in) ---------------------------------------
    # Ч.1 ст.18 ФЗ-38 и политика Meta: business-initiated сообщения только
    # opt-in контактам. Проверка стоит ДО любых расходов квоты, чтобы
    # «нельзя» не съедало лимит того, кто ещё не писал.
    # consent='denied' (отписка) уважается ВСЕГДА, независимо от флага.
    try:
        _consent = db.get_consent(phone)
        if not db.may_send_cold(phone):
            if _consent == db.CONSENT_DENIED:
                log.info(f"🚫 SKIP {phone} — отказ от рассылки (opt-out), не пишем")
            else:
                log.info(
                    f"🚫 SKIP {phone} — нет подтверждённого согласия "
                    f"(CONSENT_REQUIRED_FOR_COLD={config.CONSENT_REQUIRED_FOR_COLD})"
                )
            return False
    except Exception as e:
        # Неизвестный контакт в БД = согласие неизвестно → по флагу.
        # Ошибку БД не превращаем в запрет: логируем и решаем по флагу.
        log.debug(f"consent check failed for {phone}: {e}")
        if getattr(config, "CONSENT_REQUIRED_FOR_COLD", 0):
            log.info(f"🚫 SKIP {phone} — не удалось проверить согласие, требуется opt-in")
            return False

    # Выбираем категорию и шаблон (Phase 1.2)
    norm_category = templates.normalize_category(category)
    template_id = waba_templates.ensure_waba_template_for_category(norm_category)

    text = templates.build_message(contact, templates.load_templates())

    # SMS-текст строим ЛЕНИВО: отдельный короткий источник Message_script_sms.md.
    # Мессенджерный `text` (330-380 символов) в SMS стоил бы 5-6 сегментов
    # (42-51 ₽ против 8.5 ₽), поэтому SMS использует собственный шаблон.
    # Вычисляется один раз и переиспользуется, если контакт дошёл до SMS.
    _sms_text_cache: list[str] = []

    def _sms_text() -> str:
        if not _sms_text_cache:
            _sms_text_cache.append(
                templates.build_sms_message(contact, templates.load_sms_templates())
            )
        return _sms_text_cache[0]

    # Cascade: только каналы которые активны в текущей сессии.
    # Если передан cascade_override (для rescue queue — skip WABA),
    # используем его, но фильтруем по channels.
    if cascade_override is not None:
        cascade = [ch for ch in cascade_override if ch in channels]
    else:
        cascade = [ch for ch in _cascade_order() if ch in channels]

    # Если ВСЕ активные каналы в bad_phones — skip без API calls
    if cascade and bad_phones.is_fully_bad(phone, set(cascade)):
        log.info(
            f"⏭️  SKIP {phone} ({name}, {category}) — все каналы в bad_phones cache"
        )
        return False

    # Phase 3.x: smart WABA gating — если для этой категории НЕТ WABA template,
    # не пытаемся WABA вообще (cold text без template = PERMANENT fail, и cascade
    # упал бы в TG/MAX, что даёт СЛИШКОМ МНОГО TG/MAX отправок → риск бана).
    # Вместо этого пробуем только Personal-каналы (если они есть).
    if "waba" in cascade and not template_id:
        cascade = [ch for ch in cascade if ch != "waba"]
        log.debug(
            f"      🔀 WABA skip: нет template для категории {category!r}, "
            f"cascade → {cascade}"
        )

    # Pre-filter: bad_phones + per-phone cooldown + halted
    cascade = [
        ch for ch in cascade
        if ch not in permanent_skipped.get(phone, set())
        and not bad_phones.is_bad(phone, ch)
        and not state.is_phone_in_cooldown(current_state, phone, ch)
        and not state.is_channel_halted(current_state, ch)
    ]
    if not cascade:
        log.info(
            f"⏭️  SKIP {phone} ({name}, {category}) — все каналы в bad_phones/cooldown/halted"
        )
        return False

    log.info("─" * 70)
    log.info(
        f"📤 [{phone}] {name} | категория: {category} | "
        f"cascade: {cascade}"
    )
    # Текст покажем только когда реально отправляем (anti-pattern detection)
    log.info("─" * 70)

    sent_any = False
    counted_successful = False
    # NOTE: channel-aware delay читает `last_success_channel` из state.json
    # (cascade обновляет его в current_state и save_state сразу после успеха).

    for channel in cascade:
        # Per-channel safe hours check (WABA: 10-20, Personal: 11-19)
        if not _within_safe_hours(channel=channel):
            log.info(
                f"   💤 {channel.upper()} вне safe hours, пропускаем"
            )
            continue
        # Проверяем квоту (с headroom) ПЕРЕД каждой отправкой
        if not state.channel_has_quota_with_headroom(current_state, channel):
            log.info(
                f"   ⏭️  {channel.upper()} quota exhausted (headroom), skip"
            )
            continue
        # Phase 7: lazy retry — если WABA recently failed для этого phone,
        # сразу пробуем Personal channels (экономим WABA slot и время).
        if channel == "waba" and db.waba_recently_failed(phone, hours=24):
            recent = db.waba_recent_failures(phone, hours=24)
            log.warning(
                f"   🔁 WABA недавно failed ({len(recent)} в 24ч) для {phone}, "
                f"skip WABA → Personal channels"
            )
            continue
        # Phase 7: pre-check через reachability cache (из webhook'ов).
        # Если кэш говорит WABA unreachable для этого phone → skip.
        if channel == "waba":
            cached = state.get_cached_reachability(current_state, phone)
            if cached and cached.get("waba") is False:
                log.info(
                    f"   🚫 WABA reachability cache: unreachable для {phone}, skip"
                )
                continue

        ch_count_sent = state.channel_sent_today(current_state, channel)
        ch_attempt = ch_count_sent + 1

        # === WABA branch (Phase 1.4) ===
        if channel == "waba":
            started_at = config.now_tz()
            ch_count_sent = state.channel_sent_today(current_state, "waba")
            limit = state.effective_waba_cap(current_state)
            log.info(
                f"   🚀 WABA [{ch_count_sent + 1}/{limit}] отправляем {phone} ({name})…"
            )
            # Решаем: template или free text (24ч-окно).
            # Окно открывает ОТВЕТ КОНТАКТА, а не наша отправка.
            in_24h_window = state.is_waba_in_24h_window(current_state, phone)
            use_template = bool(template_id) and not in_24h_window
            log.info(
                f"      🧩 WABA decision: template_id={'set' if template_id else 'NONE'}, "
                f"in_24h_window={in_24h_window}, mode="
                f"{'template' if use_template else 'free-text'}"
            )
            # Per-user marketing cap Meta (~2/сутки суммарно по брендам) → 131049.
            # Мы уже писали этому контакту сегодня — второй раз НЕ шлём,
            # иначе Meta отвергнет и каскад зря сожжёт квоту.
            if use_template and state.waba_already_marketed_today(
                current_state, phone
            ):
                log.info(
                    f"⏭️  SKIP WABA {phone} — уже получили маркетинг сегодня "
                    f"(защита от 131049), уходим на следующий канал"
                )
                continue
            if config.DRY_RUN:
                ok = True
                message_id = f"dryrun_{int(started_at.timestamp())}_waba"
                detail = "[DRY-RUN] имитация WABA отправки"
                http_status = 200
                log.info("      🧪 WABA DRY-RUN")
            else:
                waba_chat_id = wazzup.normalize_phone(phone)
                if not waba_chat_id:
                    ok, message_id, detail, http_status = (
                        False, None, "phone normalization failed", None,
                    )
                else:
                    if use_template:
                        # WABA template требует значение для каждой
                        # переменной. У нас 1 переменная — имя. Пустая строка
                        # допустима (Meta API принимает var="" без ошибок).
                        template_values = [name if name else ""]
                        ok, message_id, detail, http_status = wazzup.send_wazzup(
                            channel_id=config.WAZZUP_DEFAULT_CHANNEL_ID,
                            chat_id=waba_chat_id,
                            template_id=template_id,
                            template_values=template_values,
                            chat_type="whatsapp",
                        )
                    else:
                        ok, message_id, detail, http_status = wazzup.send_wazzup(
                            channel_id=config.WAZZUP_DEFAULT_CHANNEL_ID,
                            chat_id=waba_chat_id,
                            text=text,
                            chat_type="whatsapp",
                        )
            _append_log(phone, "waba", "success" if ok else "fail", detail)
            elapsed = (config.now_tz() - started_at).total_seconds()

            if ok:
                # Phase 8: HTTP 2xx = ПРИНЯТО транспортом, а не доставлено.
                # Реальную доставку подтвердит вебхук и посчитает delivered_today.
                state.record_accepted(current_state, "waba")
                log.info(
                    f"   ✅ WABA ПРИНЯТ за {elapsed:.1f}с | message_id={message_id} | "
                    f"{'template' if use_template else 'free-text'} | {detail[:120]}"
                )
                _persist_success(
                    phone, name, category, "waba", text, message_id, current_state,
                )
                from datetime import datetime, timezone
                state.set_waba_sent_meta(current_state, phone, datetime.now(timezone.utc).isoformat())
                current_state["_waba_consecutive_permanent"] = 0  # reset circuit breaker
                state.set_last_send_per_phone(
                    current_state, phone, datetime.now(timezone.utc).isoformat(),
                    channel="waba",
                )
                current_state["last_success_channel"] = "waba"
                state.save_state(current_state)
                sent_any = True
                last_success_channel_local = "waba"
                if not counted_successful:
                    state.increment_successful(current_state)
                    state.save_state(current_state)
                    counted_successful = True
                break  # cascade: НЕ continue — broadcast был старым поведением
            else:
                kind = wazzup.classify_error(detail, http_status)
                log.warning(
                    f"   ❌ WABA FAIL [{kind.value}] http={http_status} за {elapsed:.1f}с | "
                    f"{phone} ({name}) | {detail[:200]}"
                )
                if kind == ErrorKind.PERMANENT:
                    state.mark_unreachable(current_state, phone, "waba")
                    permanent_skipped.setdefault(phone, set()).add("waba")
                    _persist_failure(phone, name, category, "waba", text, detail, "permanent")
                    backoffs["waba"] = 0
                    log.info("      🚫 WABA PERMANENT → unreachable, пробуем следующий канал")
                    # Phase 5.5 circuit breaker: если WABA PERMANENT 5 раз подряд,
                    # halt канал на сессию (template сломан → не валить всё в TG/MAX,
                    # это риск бана оригинальных аккаунтов).
                    consecutive_perm = int(current_state.get("_waba_consecutive_permanent", 0)) + 1
                    current_state["_waba_consecutive_permanent"] = consecutive_perm
                    if consecutive_perm >= 5:
                        log.error(
                            "      🔌 WABA circuit breaker: %d PERMANENT fail подряд — "
                            "halt WABA на сессию, не уходим в TG/MAX",
                            consecutive_perm,
                        )
                        state.halt_channel(current_state, "waba")
                        state.save_state(current_state)
                    continue  # следующий канал в cascade
                elif kind == ErrorKind.AUTH:
                    log.error(f"      🔐 WABA AUTH — token невалиден! Backoff MAX")
                    backoffs["waba"] = _next_backoff(kind, backoffs.get("waba", 0))
                    state.halt_channel(current_state, "waba")
                    _persist_failure(phone, name, category, "waba", text, "auth: " + detail, "auth")
                    continue
                else:
                    # TRANSIENT / RATE_LIMIT / UNKNOWN — сбрасываем circuit breaker
                    current_state["_waba_consecutive_permanent"] = 0
                    backoffs["waba"] = _next_backoff(kind, backoffs.get("waba", 0))
                    _persist_failure(phone, name, category, "waba", text, detail, kind.value)
                    log.info(f"      ⏭️  WABA [{kind.value}] — backoff={backoffs['waba']}с")
                    continue

        # === SMS.RU branch (fallback последней надежды) ===
        # ВАЖНО: этот блок стоит ДО Wazzup-guard, потому что у SMS нет
        # WAZZUP_*_CHANNEL_ID — это отдельный транспорт. Если бы блок стоял
        # после guard, cascade молча пропускал бы SMS на каждом контакте.
        if channel == "sms":
            sms_body = _sms_text()
            sms_limit = state.channel_effective_cap(current_state, "sms")
            started_at = config.now_tz()
            log.info(
                f"   🚀 SMS [{ch_attempt}/{sms_limit}] отправляем {phone} ({name}) "
                f"через SMS.RU…"
            )

            # Локальная проверка бюджета сегментов ДО сетевого вызова.
            # Экономит деньги: SMS.RU всё равно не спишет за отвергнутое
            # сообщение, но мы сразу увидим проблему шаблона в логах.
            if sms_ru.exceeds_segment_budget(sms_body):
                detail = (
                    f"SMS text too long: {sms_ru.estimate_segments(sms_body)} segments "
                    f"({len(sms_body)} chars), budget "
                    f"{min(config.SMSRU_MAX_SEGMENTS, sms_ru.SMSRU_HARD_MAX_SEGMENTS)} "
                    f"— fix Message_script_sms.md"
                )
                _append_log(phone, "sms", "fail", detail)
                _persist_failure(phone, name, category, "sms", sms_body, detail, "permanent")
                permanent_skipped.setdefault(phone, set()).add("sms")
                state.mark_unreachable(current_state, phone, "sms")
                log.error(f"      ❌ {detail}")
                continue

            ok, sms_id, detail, sms_code = sms_ru.send_sms(phone, sms_body)
            _append_log(phone, "sms", "success" if ok else "fail", detail)
            elapsed = (config.now_tz() - started_at).total_seconds()

            if ok:
                log.info(
                    f"   ✅ SMS OK за {elapsed:.1f}с | sms_id={sms_id} | {detail[:120]}"
                )
                # SMS-текст сохраняем в БД (не мессенджерный!) — по нему потом
                # считается фактическая стоимость рассылки.
                _persist_success(
                    phone, name, category, "sms", sms_body, sms_id, current_state,
                )
                sent_any = True
                if not counted_successful:
                    state.increment_successful(current_state)
                    state.save_state(current_state)
                    counted_successful = True
                from datetime import datetime, timezone
                # SMS — fire-and-forget (вебхука нет), поэтому здесь именно время
                # ОТПРАВКИ ложится в last_delivery_per_channel: иначе SMS-каскад
                # не будет уважать собственный cooldown.
                state.set_last_send_per_phone(
                    current_state, phone, datetime.now(timezone.utc).isoformat(),
                    channel="sms",
                )
                state.record_delivery_confirmed(
                    current_state, phone, "sms",
                    datetime.now(timezone.utc).isoformat(),
                )
                current_state["last_success_channel"] = "sms"
                state.save_state(current_state)
                break  # один контакт = одно сообщение в ОДИН канал

            kind = sms_ru.classify_smsru_error(sms_code, detail)
            log.warning(
                f"   ❌ SMS FAIL [{kind.value}] code={sms_code} за {elapsed:.1f}с | "
                f"{phone} ({name}) | {detail[:200]}"
            )
            if kind == ErrorKind.PERMANENT:
                # 207/150 (нет маршрута), 215 (стоп-лист SMS.RU), 205/216/217
                # (наш баг в тексте) — номер больше НЕ пробуем ни в SMS.
                permanent_skipped.setdefault(phone, set()).add("sms")
                state.mark_unreachable(current_state, phone, "sms")
                _persist_failure(
                    phone, name, category, "sms", sms_body, detail, "permanent",
                )
                backoffs["sms"] = 0
                log.info(
                    "      🚫 SMS PERMANENT — номер помечен как недоступный для SMS"
                )
                continue
            if kind == ErrorKind.AUTH:
                # 200/204/221/302 — проблема АККАУНТА, не контакта. Повтор только
                # выжигает дневную квоту, поэтомуhalt канала целиком.
                log.error(
                    "      🔐 SMS AUTH — %s. Проверьте BULK_SMSRU_API_ID, "
                    "BULK_SMSRU_SENDER_NAME и подтверждение аккаунта SMS.RU. "
                    "Канал SMS останавливается на 24ч.",
                    detail[:200],
                )
                state.halt_channel(current_state, "sms")
                state.save_state(current_state)
                backoffs["sms"] = _next_backoff(kind, backoffs.get("sms", 0))
                _persist_failure(
                    phone, name, category, "sms", sms_body, "auth: " + detail, "auth",
                )
                try:
                    from . import tglog
                    tglog.send(
                        f"🔐 SMS AUTH fail: {detail[:300]} — SMS-канал halted на 24ч",
                        level="ERROR",
                    )
                except Exception:
                    pass
                continue
            # RATE_LIMIT / TRANSIENT — SMS терминальный, падать дальше некуда.
            # НО код 206 = дневной лимит АККАУНТА исчерпан. Это не вина
            # контакта: номер в порядке, упёрлись в потолок SMS.RU. Без halt
            # sender продолжал бы биться в лимит по КАЖДОМУ rescue-контакту и
            # помечал нормальные номера как failed — 65 контактов в день
            # уходили бы в failed_today из-за одного лимита аккаунта.
            if sms_ru.is_daily_limit_reached(sms_code):
                log.error(
                    "      🛑 SMS дневной лимит аккаунта исчерпан (206) — "
                    "останавливаю SMS-канал на 24ч. Номера не виноваты: "
                    "квота SMS.RU кончилась. Остаток дня уходит в потери."
                )
                state.halt_channel(current_state, "sms")
                state.save_state(current_state)
                _persist_failure(
                    phone, name, category, "sms", sms_body, detail, kind.value,
                )
                try:
                    from . import tglog
                    tglog.send(
                        "🛑 SMS.RU: дневной лимит аккаунта исчерпан (206). "
                        "SMS-канал остановлен на 24ч.",
                        level="ERROR",
                    )
                except Exception:
                    pass
                continue
            backoffs["sms"] = _next_backoff(kind, backoffs.get("sms", 0))
            _persist_failure(
                phone, name, category, "sms", sms_body, detail, kind.value,
            )
            log.info(
                f"      ⏭️  SMS [{kind.value}] — backoff={backoffs['sms']}с. "
                f"Канал последний, следующих нет."
            )
            continue

        # === Wazzup-only branch (TG/MAX/WA Personal) ===
        wazzup_channel_id = wazzup.channel_id_for(channel)
        if not wazzup_channel_id or not wazzup.is_configured():
            log.error(
                f"      ⛔ {channel.upper()} не сконфигурирован: WAZZUP_*_CHANNEL_ID "
                f"для {channel!r} пустой или WAZZUP_API_KEY не задан. "
                f"Канал пропущен для этого контакта (cascade продолжится)."
            )
            _append_log(phone, channel, "fail", "wazzup channel not configured")
            continue

        transport_label = "Wazzup"

        # === Wazzup branch (TG/MAX/WA Personal) ===
        limit = config.CHANNEL_DAILY_LIMITS[channel]
        started_at = config.now_tz()
        log.info(
            f"   🚀 {channel.upper()} [{ch_attempt}/{limit}] "
            f"отправляем {phone} ({name}) через {transport_label}…"
        )

        # DRY-RUN: пропускаем реальный вызов, только имитируем успех
        if config.DRY_RUN:
            ok = True
            message_id = f"dryrun_{int(started_at.timestamp())}_{channel}"
            detail = f"[DRY-RUN] имитация успешной отправки, {transport_label} API НЕ вызывался"
            http_status = 200
            log.info(
                f"      🧪 {channel.upper()} DRY-RUN через {transport_label} — "
                f"реальная отправка пропущена, только имитация успеха"
            )
        else:
            ok, message_id, detail, http_status = wazzup.send_to_channel(
                channel=channel,
                phone=phone,
                text=text,
            )
        _append_log(phone, channel, "success" if ok else "fail", detail)
        elapsed = (config.now_tz() - started_at).total_seconds()

        if ok:
            log.info(
                f"   ✅ {channel.upper()} OK за {elapsed:.1f}с | "
                f"message_id={message_id} | ответ Wazzup: {detail[:120]}"
            )
            _persist_success(phone, name, category, channel, text, message_id, current_state)
            sent_any = True
            # channel-aware delay: `last_success_channel` пишем в state.json
            # (читается в run() после cascade через current_state.get(...)).
            # Считаем contact как "successful" ОДИН раз (при первой успешной отправке)
            if not counted_successful:
                state.increment_successful(current_state)
                state.save_state(current_state)
                counted_successful = True
            from datetime import datetime, timezone
            # Cooldown — по паре (номер, канал): откат WABA→TG→MAX не должен
            # блокироваться собственной же предыдущей отправкой.
            state.set_last_send_per_phone(
                current_state, phone, datetime.now(timezone.utc).isoformat(),
                channel=channel,
            )
            current_state["last_success_channel"] = channel
            state.save_state(current_state)
            break  # Phase 2.2 cascade: НЕ continue — broadcast был старым поведением

        kind = wazzup.classify_error(detail, http_status)
        log.warning(
            f"   ❌ {channel.upper()} FAIL [{kind.value}] http={http_status} "
            f"за {elapsed:.1f}с | {phone} ({name}) | {detail[:200]}"
        )

        if kind == ErrorKind.PERMANENT:
            permanent_skipped.setdefault(phone, set()).add(channel)
            # Сохраняем НА ДИСК. Раньше отказ Telegram/MAX жил только в
            # permanent_skipped — памяти сессии, — и следующий запуск снова
            # стучался в тот же мёртвый аккаунт: «сменил @никнейм / удалил
            # аккаунт», «user not found». У WABA и SMS здесь стоял
            # mark_unreachable + bad_phones, у мессенджеров — нет.
            state.mark_unreachable(current_state, phone, channel)
            bad_phones.mark_bad(phone, channel, reason=f"permanent: {detail[:120]}")
            _persist_failure(phone, name, category, channel, text, detail, "permanent")
            log.info(
                f"      🚫 PERMANENT — {channel} помечен как невалидный для этого "
                f"контакта НАВСЕГДА (bad_phones), пробуем другие каналы"
            )
            backoffs[channel] = 0
            continue

        if kind == ErrorKind.AUTH:
            # 401/403 = канал мёртв (забанен / отозван / невалидный channelId).
            # Это ПРОБЛЕМА АККАУНТА, а не контакта: тот же 401 прилетит на
            # каждом оставшемся контакте, и каскад будет долбиться в мёртвый
            # канал до конца прогона. Контракт задан в wazzup.py:12
            # («401/403 → AUTH (halt channel)») и выполняется ветками WABA/SMS
            # выше — эта ветка его не выполняла. Раньше в базе и стояло
            # 110 подряд «auth: HTTP 401 Not authorized»: канал умер, а sender
            # продолжал стучаться в него на каждом контакте.
            log.error(
                f"      🔐 AUTH — Wazzup API key/channel невалиден для {channel}! "
                f"Канал {channel} останавливается, каскад идёт в следующий канал. "
                f"Проверьте WAZZUP_API_KEY и WAZZUP_{channel.upper()}_CHANNEL_ID."
            )
            state.halt_channel(current_state, channel)
            state.save_state(current_state)
            backoffs[channel] = _next_backoff(kind, backoffs.get(channel, 0))
            _persist_failure(phone, name, category, channel, text,
                             "auth: " + detail, "auth")
            try:
                from . import tglog
                tglog.send(
                    f"🔐 AUTH fail на канале {channel}: {detail[:300]} — "
                    f"канал halted на 24ч, каскад переведён дальше",
                    level="ERROR",
                )
            except Exception:
                pass
            continue

        # RATE_LIMIT или TRANSIENT — накапливаем backoff, идём дальше.
        # .get(), а не [channel]: rescue-очередь передаёт backoffs={} (см.
        # _drain_rescue_queue), и прямое индексирование роняло весь rescue
        # с KeyError — исключение глушилось, контакт молча терялся.
        backoffs[channel] = _next_backoff(kind, backoffs.get(channel, 0))
        _persist_failure(phone, name, category, channel, text, detail, kind.value)
        log.info(
            f"      ⏭️  {channel.upper()} [{kind.value}] — backoff={backoffs[channel]}с. "
            f"Деталь: {detail[:120]}"
        )

    # Если контакт НИГДЕ не доставлен (sent_any=False), помечаем как полностью
    # мёртвый для экспорта (data/failed_today.json).
    #
    # ВАЖНО: считаем мёртвым только когда исчерпаны ВСЕ рабочие каналы, а не
    # те, что попали в КАСКАД ЭТОЙ отправки. Фаза отката отправляет контакт
    # одним каналом (cascade_override=[phase_ch]) — раньше отказ в Telegram
    # тут же означал «контакт мёртв», и до MAX он уже не доходил, хотя MAX был
    # доступен и мог бы сработать. Ровно та цепочка, ради которой всё
    # затевалось: TG не нашёл человека → пробуем MAX.
    if not sent_any and cascade:
        failed_channels = [ch for ch in cascade]
        _all_channels = set(_cascade_order()) | {"waba"}
        _still_open = [
            ch for ch in _all_channels
            if ch not in set(failed_channels)
            and ch in set(getattr(config, "PHASE_ORDER", []) or [])
            and not bad_phones.is_bad(phone, ch)
        ]
        if _still_open:
            log.info(
                f"   ↪ Контакт {phone} не доставлен в "
                f"{', '.join(failed_channels)}, но ещё открыты каналы: "
                f"{', '.join(sorted(_still_open))} — остаётся в rescue-очереди"
            )
        else:
            failed_contacts.mark_fully_failed(
                phone=phone, name=name, category=category,
                channels_failed=failed_channels,
            )
            log.info(
                f"   ☠️ Контакт {phone} ({name}) полностью мёртв — "
                f"fail'нули все {len(failed_channels)} канала. "
                f"Записан в data/failed_today.json"
            )

    return sent_any


def _wait_for_backoffs(backoffs: dict[str, int], log) -> float:
    """Если хоть один канал в backoff — спим максимальный из backoff, декрементируем.

    Возвращает сколько секунд проспали (0 если ждать не пришлось).
    """
    if not any(v > 0 for v in backoffs.values()):
        return 0.0
    wait = max(backoffs.values())
    # Декрементируем все каналы на wait (вычитаем одинаковое время)
    for ch in list(backoffs.keys()):
        backoffs[ch] = max(0, backoffs[ch] - wait)
    log.info(
        f"   ⏳ Backoff {wait}с: " + ", ".join(
            f"{ch}={v}с" for ch, v in backoffs.items()
        )
    )
    # Спим кусками по 30с для быстрого Ctrl+C
    slept = 0.0
    while slept < wait:
        time.sleep(min(30, wait - slept))
        slept += 30
    return wait


# =============================================================================
# ФАЗОВАЯ РАССЫЛКА (Phase 9)
# =============================================================================
# Прежде каскад был ПО-КОНТАКТНЫМ: внутри _run_one_contact шли waba → tg → max
# с break на первом HTTP 2xx. WABA отдаёт 2xx даже когда не доставляет, поэтому
# sender считал успехом всё подряд и до Telegram дело не доходило — отсюда
# «100 отправок → 28 доставок без переключения». Теперь канал задаётся ФАЗОЙ.

def _phase_safe_hours_for(channel: str) -> tuple[int, int]:
    """Safe-окно фазы для канала (часы, МСК)."""
    if channel == "waba":
        return (config.WABA_SAFE_HOURS_START, config.WABA_SAFE_HOURS_END)
    if channel == "sms":
        return (config.SMSRU_SAFE_HOURS_START, config.SMSRU_SAFE_HOURS_END)
    return (config.PERSONAL_SAFE_HOURS_START, config.PERSONAL_SAFE_HOURS_END)


def _phase_seconds_left(channel: str) -> float:
    """Сколько секунд осталось до конца safe-окна фазы. <=0 — окно закрыто.

    END=24 (окно «до конца дня») раньше ронял фазу с
    `ValueError: hour must be in 0..23, not 24` — datetime.replace не
    принимает 24. Конфиг это значение допускает (_within_safe_hours с ним
    работает), поэтому падение было настоящим, просто не на боевых
    значениях 11-19, а на тестовых/широких окнах.
    """
    from datetime import datetime as _dt, timedelta as _td
    _st, end = _phase_safe_hours_for(channel)
    now = config.now_tz()
    if end >= 24:
        # Конец суток = полночь следующих суток.
        end_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) + _td(days=1)
    else:
        end_dt = now.replace(hour=end, minute=0, second=0, microsecond=0)
    if end_dt <= now:
        end_dt = end_dt + _td(days=1)
    return (end_dt - now).total_seconds()


# Дедлайн текущего запуска sender'а (timestamp, UTC) и счётчик кругов.
# Ставится один раз в начале run(), когда включён режим длинного прогона
# (BULK_RUN_HOURS). Держим в модуле, потому что пауза между сообщениями
# (_phase_delay) тоже должна о нём знать: иначе 250 сообщений уходят
# за 1.4 часа по паузе 10-30 секунд, и «10-часовой» прогон заканчивается
# через час с четвертью.
_RUN_DEADLINE: Optional[float] = None
_RUN_CYCLE = 0


def _run_deadline() -> Optional[float]:
    """Абсолютный дедлайн запуска (unix ts, UTC) или None."""
    return _RUN_DEADLINE


def _run_seconds_left() -> float:
    """Сколько секунд осталось до дедлайна запуска.

    Возвращает float('inf'), если режим длинного прогона выключен —
    тогда ограничителем остаётся только safe-hours окна.
    """
    if _RUN_DEADLINE is None:
        return float("inf")
    return max(0.0, _RUN_DEADLINE - time.time())


def _phase_delay_bounds(channel: str) -> tuple[int, int]:
    if channel == "waba":
        return (config.WABA_DELAY_MIN, config.WABA_DELAY_MAX)
    if channel == "sms":
        return (config.SMS_DELAY_MIN, config.SMS_DELAY_MAX)
    return (config.PERSONAL_DELAY_MIN, config.PERSONAL_DELAY_MAX)


def _phase_delay(channel: str, seconds_left: float, remaining_msgs: int) -> float:
    """Детерминированная задержка между отправками внутри фазы.

    clamp(остаток_окна / остаток_сообщений, MIN, MAX). Фаза за счёт этого
    гарантированно укладывается в своё окно. Со случайными 300-900с суммарное
    σ для 45 сообщений ≈19 минут при запасе 20 — то есть ~10% вероятности
    вылеза за 19:00. С детерминированной задержкой исход не зависит от броска.

    Темп в режиме длинного прогона РАЗНЫЙ для разных каналов:

      * WABA идёт БЫСТРО (свои 10-30 секунд). Растягивать его на 10 часов
        нельзя: откаты TG/MAX запускаются только после того, как вебхук
        пришлёт `failed`, а вебхуку нужно время увидеть ошибки доставки.
        Плюс Meta шлёт события пачками — откладывать отправку на часы
        ради «равномерности» значит просто терять откатную ёмкость.

      * Откаты (TG/MAX) растягиваются на остаток дедлайна. Их мало (до 50
        на канал) и они идут последними, поэтому именно на них приходится
        «дотянуть» прогон до 10 часов.

    Без флага поведение прежнее: пауза упирается в границы канала.
    """
    lo, hi = _phase_delay_bounds(channel)
    if remaining_msgs <= 0:
        return 0.0
    if not getattr(config, "PHASE_DETERMINISTIC_DELAY", False):
        return random.uniform(lo, hi)

    # Режим длинного прогона включается ТОЛЬКО если задан дедлайн.
    # Важно: min(36000, inf) даёт 36000, а не inf — если не проверить это
    # отдельно, без флага пауза растягивается до «окно / остаток» и WABA
    # уходит 3 сообщения по 3.3 часа вместо 30 секунд.
    run_left = _run_seconds_left()
    if run_left == float("inf"):
        # Старое поведение: пауза упирается в границы канала.
        return max(lo, min(hi, seconds_left / remaining_msgs))

    # Первичный канал не растягиваем: он должен отработать быстро, чтобы
    # вебхук успел принести failed-события для откатов.
    if not getattr(config, "PHASE_STRETCH_FALLBACK_ONLY", False) \
            or channel == "waba":
        return max(lo, min(hi, seconds_left / remaining_msgs))

    # Откатная фаза: растягиваем на остаток дедлайна.
    left = min(seconds_left, run_left)
    if left <= 0:
        return 0.0
    per_msg = left / remaining_msgs

    # Верхняя граница паузы поднимается до «размазать по оставшемуся
    # времени»: без этого 50 откатов уходят за 50 минут вместо часов.
    ceiling = max(hi, per_msg)
    return max(lo, min(ceiling, per_msg))


def _collect_fresh_waba_targets(
    contacts: list[dict], current_state: dict
) -> list[dict]:
    """Свежие контакты для фазы WABA.

    Исключаются (решение №3 плана: «WABA работает только со свежими»):
      • номера, уже сидящие в rescue_queue (pending ИЛИ уже rescued) — им
        место в фазах TG/MAX, второй раз в WABA они только сожгут слот;
      • номера из waba_exhausted (долгоживущий маркер провала, TTL 30 дней);
      • номера в cooldown для waba;
      • номера, помеченные bad_phones для waba;
      • контакты БЕЗ одобренного шаблона Meta.

    Последнее — не перестраховка, а требование API: холодная рассылка в WABA
    допускается ТОЛЬКО по одобренному шаблону, у которого есть categoryName и
    templateId. В базе 308 контактов с категориями «Знакомый/Услуги/Прораб»,
    для которых шаблона нет: build_message для них возвращает заглушку в 67
    символов, а Wazzup отвечает BUILD_TEMPLATE_ERROR. То есть слот WABA
    тратится впустую, а человек получает отказ вместо сообщения.
    """
    queued: set[str] = set()
    try:
        for r in db.pending_rescues(limit=1000000, cooldown_seconds=0):
            queued.add(r.get("phone"))
    except Exception as e:  # pragma: no cover - defensive
        log.warning(f"не удалось прочитать rescue_queue для отбора свежих: {e}")

    fresh: list[dict] = []
    skipped_queued = skipped_exhausted = skipped_cooldown = skipped_bad = 0
    skipped_no_template = 0
    for c in contacts:
        phone = c.get("phone")
        if not phone or phone in queued:
            skipped_queued += 1
            continue
        if state.is_waba_exhausted(current_state, phone):
            skipped_exhausted += 1
            continue
        if state.is_phone_in_cooldown(current_state, phone, "waba"):
            skipped_cooldown += 1
            continue
        if bad_phones.is_bad(phone, "waba"):
            skipped_bad += 1
            continue
        # Нет одобренного шаблона Meta -> отправка вернёт ошибку шаблона.
        if templates.normalize_category(
                c.get("category", "")) not in config.ALLOWED_CATEGORIES:
            skipped_no_template += 1
            continue
        fresh.append(c)

    log.info("─" * 70)
    log.info(f"   🧺 Отбор свежих для WABA: {len(fresh)} из {len(contacts)}")
    log.info(
        f"      исключено: в очереди {skipped_queued}, "
        f"WABA-исчерпан {skipped_exhausted}, "
        f"cooldown {skipped_cooldown}, bad {skipped_bad}, "
        f"без шаблона Meta {skipped_no_template}"
    )
    log.info("─" * 70)
    return fresh


def _phase_fits(channel: str, n_messages: int) -> bool:
    """Вписывается ли фаза из n_messages в остаток safe-окна.

    Если нет — фазу надо ОТЛОЖИТЬ на завтра, а не начинать впритык и потом
    упереться в 19:00. Очередь не теряется: провалы лежат в rescue_queue.
    """
    if n_messages <= 0:
        return True
    left = _phase_seconds_left(channel)
    lo, _hi = _phase_delay_bounds(channel)
    need = n_messages * lo
    if left < need:
        log.info(
            f"   ⏳ Фаза {channel.upper()}: в окне осталось {left/60:.0f} мин, "
            f"а нужно минимум {need/60:.0f} мин на {n_messages} сообщений "
            f"→ откладываю на завтра (очередь не теряется)"
        )
        return False
    est = n_messages * ((lo + _hi) / 2.0)
    log.info(
        f"   ⏱️  Фаза {channel.upper()}: {n_messages} сообщений, "
        f"в окне {left/60:.0f} мин, оценка {est/60:.0f} мин"
    )
    return True


def _sleep_phase(seconds: float, stop_requested: dict) -> None:
    """Сон с учётом флага остановки (иначе Ctrl+C не работал бы часами)."""
    slept = 0.0
    while slept < seconds and not stop_requested["v"]:
        time.sleep(min(30, seconds - slept))
        slept += 30


def _phase_order_channels() -> list[str]:
    """Каналы фазовой схемы в порядке запуска (waba, telegram, max, ...)."""
    return [c.strip() for c in
            (getattr(config, "PHASE_ORDER", []) or [])
            if c.strip()]


def channel_delivery_health(channel: str, min_sample: int = 5,
                             window_hours: int = 6) -> dict:
    """Здоровье канала по недавней истории: доставилось хоть что-нибудь или нет.

    Нужно, потому что Wazzup принимает отправку с HTTP 200 и молчит, если
    канал не авторизован. Живой случай 2026-10-06: 8 отправок в Telegram,
    0 подтверждений доставки, все с красной иконкой в интерфейсе — и фаза
    продолжала жечь личный лимит, пока её не остановили вручную.

    Возвращает {"ok": bool, "sent": int, "delivered": int, "reason": str}.
    При недостаточной выборке ok=True: это «не знаем», а не «сломан».
    """
    empty = {"ok": True, "sent": 0, "delivered": 0, "reason": "нет данных"}
    try:
        import sqlite3 as _sq
        from . import config as _cfg
        dbp = Path(getattr(_cfg, "DB_PATH", "data/crm.db"))
        if not dbp.exists():
            return empty
        con = _sq.connect(f"file:{dbp}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT COUNT(*) AS total, "
                "       SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END) AS dlv "
                "FROM messages WHERE channel = ? "
                "  AND created_at >= datetime('now', ?)",
                (channel, f"-{int(window_hours)} hours"),
            ).fetchone()
        finally:
            con.close()
        sent = int(row[0] or 0)
        delivered = int(row[1] or 0)
    except Exception as e:
        log.debug(f"channel_delivery_health({channel}): не посчитали: {e}")
        return empty

    min_sample = int(getattr(config, "CHANNEL_HEALTH_MIN_SAMPLE", min_sample))

    if sent < min_sample:
        return {"ok": True, "sent": sent, "delivered": delivered,
                "reason": f"мало данных для вывода ({sent} < {min_sample})"}
    if delivered == 0:
        return {"ok": False, "sent": sent, "delivered": 0,
                "reason": (f"{sent} отправок за {window_hours}ч и НИ ОДНОГО "
                           f"подтверждения доставки — канал не авторизован "
                           f"или сломан")}
    return {"ok": True, "sent": sent, "delivered": delivered,
            "reason": f"доставлено {delivered} из {sent}"}


def _run_phase(
    phase_ch: str,
    items: list[dict],
    label: str,
    stop_requested: dict,
) -> int:
    """Одна фаза: отправляет items в ЕДИНСТВЕННЫЙ канал phase_ch.

    Вынесено на уровень модуля (а не вложенной функцией run()), чтобы его
    можно было прогнать в тестах с подменённым транспортом — это сердце всей
    фазовой перестройки, и проверять его только чтением исходника нельзя.

    Возвращает число реально обработанных контактов.
    """
    backoffs: dict[str, int] = {}
    permanent_skipped: dict[str, set[str]] = {}
    done = 0
    total = len(items)
    for i, item in enumerate(items):
        if stop_requested["v"]:
            log.info("⛔ Остановка по сигналу")
            break
        cur = state.load_state()
        cur = state.reset_daily_if_new_day(cur)
        if state.max_attempts_reached(cur):
            log.info(
                f"🛑 SAFETY CAP ({state.attempts_today(cur)} попыток) — "
                f"фаза {phase_ch.upper()} прервана"
            )
            break
        if not _within_safe_hours(phase_ch):
            log.info(
                f"🕐 Safe hours для {phase_ch.upper()} закрыты — "
                f"фаза прервана, остаток в очереди"
            )
            break
        if any(v > 0 for v in backoffs.values()):
            _wait_for_backoffs(backoffs, log)

        state.increment_attempts(cur)
        state.save_state(cur)

        try:
            ok = _run_one_contact(
                item, [phase_ch], backoffs, permanent_skipped, cur,
                cascade_override=[phase_ch],
            )
        except Exception as e:
            log.warning(f"фаза {phase_ch}: сбой на {item.get('phone')}: {e}")
            ok = False

        done += 1

        # Rescue-строки должны получить терминальный статус, иначе они
        # останутся pending и уйдут в фазу MAX повторно.
        rid = item.get("rescue_id")
        if rid and phase_ch != "waba":
            # Отмечаем канал пройденным ДО проверки результата: падение в
            # этом канале тоже должно исключить его из следующих фаз,
            # иначе MAX отправит тому же контакту второе сообщение.
            db.mark_rescue_tried(rid, phase_ch)
            if ok:
                db.mark_rescue_success(
                    rid, channel=phase_ch,
                    message_id=f"rescued_{phase_ch}_{rid}",
                )
                log.info(f"  ✅ Rescue {rid} → {item.get('phone')} через {phase_ch}")
            else:
                db.mark_rescue_attempt(rid, error=f"{phase_ch}_failed")
                # Все каналы отката для этого контакта уже отработали?
                # Тогда НЕ оставляем его болтаться в начале очереди: уводим
                # в конец с retry_after — на следующий день попробуем заново.
                # Раньше такая запись навсегда оставалась 'pending' и просто
                # занимала место, без единого шанса на повтор.
                try:
                    _tried = set(db.rescue_tried_channels(rid))
                except Exception as _te:
                    # Старая БД без колонки tried_channels: не роняем фазу
                    # из-за служебной проверки — просто не откладываем.
                    log.debug(f"rescue_tried_channels недоступна: {_te}")
                    _tried = set()
                _fallbacks = [c for c in _phase_order_channels() if c != "waba"]
                if _fallbacks and set(_fallbacks).issubset(_tried):
                    from datetime import timedelta as _td
                    _tomorrow = (
                        datetime.now(timezone.utc) + _td(days=1)
                    ).isoformat()
                    db.mark_rescue_deferred(rid, _tomorrow)
                    log.info(
                        f"  ⏭️ Rescue {rid} ({item.get('phone')}) провалился во "
                        f"всех каналах ({', '.join(_tried)}) — в конец очереди, "
                        f"повтор после {_tomorrow[:10]}"
                    )
                else:
                    log.info(
                        f"  ❌ Rescue {rid} не удался через {phase_ch} "
                        f"(остаётся в очереди)"
                    )

        if done % 10 == 0 or done == total:
            log.info(
                f"   [{label}] {done}/{total} · "
                f"принято {state.channel_sent_today(cur, phase_ch)}"
                f"/{state.channel_effective_cap(cur, phase_ch)}"
            )

        remaining = total - i - 1
        if remaining > 0 and not stop_requested["v"]:
            d = _phase_delay(phase_ch, _phase_seconds_left(phase_ch), remaining)
            _sleep_phase(d, stop_requested)

        total_sent = sum(cur.get("sent_today", {}).values())
        if (
            config.BATCH_SIZE
            and total_sent > 0
            and total_sent % config.BATCH_SIZE == 0
            and config.BATCH_BREAK_MAX > 0
        ):
            _sleep_phase(
                random.uniform(config.BATCH_BREAK_MIN, config.BATCH_BREAK_MAX),
                stop_requested,
            )
    return done


def _select_contacts_for_cycle(
    all_contacts: Optional[list] = None,
) -> list:
    """Контакты для ОДНОГО круга фаз, пересобираются каждый круг.

    В длинном режиме важно пересобирать список каждый круг: подхватываются
    новые контакты из Excel, а уже отправленные отсекаются по crm.db
    (get_sent_phones) и по cooldown. Без пересборки второй круг
    отправлял бы те же контакты повторно.
    """
    if all_contacts is None:
        try:
            all_contacts = contacts.load_contacts(config.EXCEL_PATH)
        except FileNotFoundError:
            log.error(f"Файл контактов не найден: {config.EXCEL_PATH}")
            return []

    try:
        with db.db_conn() as conn:
            sent_phones = db.get_sent_phones(conn)
    except Exception:
        sent_phones = set()

    cur = state.reset_daily_if_new_day(state.load_state())
    out = []
    for c in all_contacts:
        phone = c.get("phone")
        if not phone or phone in sent_phones:
            continue
        if state.is_phone_in_cooldown(cur, phone, "waba"):
            continue
        out.append(c)
    return out


def _sweep_silence_into_rescue(minutes: int) -> int:
    """Перед фазой отката: молчание вебхука = отказ Meta.

    Wazzup не присылает `failed` (замер 2026-10-06: 0 событий `failed` на
    1321 событий, реальные статусы — delivered/read/sent/error). Отказ по
    маркетинговому шаблону выглядит как тишина: HTTP 2xx есть, подтверждения
    доставки нет и не будет. Поэтому перед откатом мы сами превращаем
    молчание в явный провал и кладём контакты в rescue-очередь.

    Без этого шага фазы Telegram/MAX уходили в пустоту: очередь наполнял
    только `error` (2 события за всю историю), а откат на 100–250 сообщений
    не срабатывал НИ РАЗУ.
    """
    try:
        from . import reconcile as _reconcile
        n = _reconcile.sweep_unconfirmed_deliveries(
            limit=200, minutes=max(1, int(minutes))
        )
    except Exception as e:
        log.warning(f"Сторож молчания перед откатом не отработал (non-fatal): {e}")
        return 0
    if n:
        log.warning(
            f"🔇 Вебхук промолчал {n} контактов после паузы — они идут в откат "
            f"(TG/MAX). Явных 'failed' от Wazzup не бывает."
        )
    return n


def _run_all_phases(
    filtered: list,
    stop_requested: dict,
    backoffs: dict,
    permanent_skipped: dict,
    cycle_no: int = 1,
    only_phase: Optional[str] = None,
) -> dict[str, int]:
    """Один полный круг фаз: WABA → TG → MAX.

    Вынесено из run(), чтобы:
      * в режиме длинного прогона крутить её многократно до дедлайна;
      * тестировать её отдельно, не запуская весь sender.

    `filtered` — контакты, отобранные для ЭТОГО круга. В длинном режиме он
    пересобирается каждый круг, поэтому новые контакты подхватываются, а уже
    отправленные отсеиваются по базе.

    `only_phase` — прогнать ОДНУ фазу из схемы, не трогая config.PHASE_ORDER.
    Нужен сквозным стендам: они гоняют фазы по очереди, а логика отсрочки
    («исчерпаны ли ВСЕ каналы отката») обязана смотреть на полный порядок из
    конфига, а не на подмножество текущего прогона.
    """
    phase_order = list(getattr(config, "PHASE_ORDER", []) or [])
    if only_phase:
        phase_order = [only_phase]
    phase_stats: dict[str, int] = {}

    log.info("=" * 70)
    log.info(f"ФАЗОВЫЙ РЕЖИМ: {' → '.join(phase_order)}"
             + (f"  (круг {cycle_no})" if cycle_no > 1 else ""))
    log.info("=" * 70)

    for phase_no, phase_ch in enumerate(phase_order):
        if stop_requested["v"]:
            break

        if phase_no > 0:
            # Ждём события вебхука, ПОТОМ превращаем молчание в провал.
            #
            # Сторож вызывается ВСЕГДА, а не только когда пауза > 0. Раньше он
            # лежал внутри `if grace > 0`, и при PHASE_GRACE_MINUTES=0 (или
            # RESCUE_SETTLE_MINUTES=0) фазы отката читали пустую очередь и не
            # отправляли НИЧЕГО — цепочка молча вырождалась в один WABA-проход.
            # Пауза нужна, чтобы долетели статусы; наполнять очередь должна
            # тишина, а не пауза.
            prev_phase = phase_order[phase_no - 1]
            wait_min = int(
                getattr(config, "PHASE_GRACE_MINUTES", 5)
                if prev_phase == "waba"
                else getattr(config, "RESCUE_SETTLE_MINUTES", 5)
            )
            if wait_min > 0:
                log.info(
                    f"⏸️  Пауза {wait_min} мин после {prev_phase.upper()}: ждём "
                    f"подтверждений доставки (Wazzup не шлёт 'failed', "
                    f"недоказанное = провал)"
                )
                _sleep_phase(wait_min * 60, stop_requested)
            _sweep_silence_into_rescue(max(0, wait_min))
        elif phase_ch != "waba":
            # Первая фаза, а откатать уже нечего: запуск начат сразу с
            # Telegram/MAX (BULK_PHASE_ORDER=telegram,max), WABA в этом
            # прогоне не участвовал. Раньше сторож отрабатывал только ПОСЛЕ
            # первой фазы, поэтому откат читал пустую очередь и пропускал
            # Telegram, а отправлять уходил MAX — самый рискованный канал.
            # Наполняем очередь ДО чтения.
            _sweep_silence_into_rescue(max(
                0, int(getattr(config, "RESCUE_SETTLE_MINUTES", 5))))

        if phase_ch == "waba":
            # Ждём открытия активного окна (10:00 МСК) перед первой фазой.
            if _wait_until_active_window(log):
                pass
            cur = state.load_state()
            if not state.channel_has_quota_with_headroom(cur, "waba"):
                log.info("🛑 WABA: квота исчерпана — фаза пропущена")
                continue
            targets = _collect_fresh_waba_targets(filtered, cur)
            # Дневной кап канала (250) — верхняя граница по лимитам Meta.
            _cap_waba = int(state.channel_effective_cap(cur, "waba"))
            # Лимит запуска (--waba-limit N): для первых боевых прогонов,
            # когда вместо 250 сообщений сразу надо отдать 10–20.
            _run_cap = int(getattr(config, "WABA_RUN_LIMIT", 0) or 0)
            if _run_cap > 0:
                _run_cap = min(_run_cap, _cap_waba)
                if len(targets) > _run_cap:
                    log.info(
                        f"🔬 ТЕСТОВЫЙ РЕЖИМ: WABA ограничен { _run_cap } "
                        f"сообщениями в этом запуске (BULK_WABA_RUN_LIMIT), "
                        f"в базе доступно {len(targets)}"
                    )
                targets = targets[:_run_cap]
            targets = targets[:_cap_waba]
            if not targets:
                log.info("🛑 WABA: нет свежих контактов — фаза пропущена")
                continue
            if not _phase_fits("waba", len(targets)):
                continue
            log.info(
                f"▶️  ФАЗА 1 · WABA · {len(targets)} свежих контактов · "
                f"окно {config.WABA_SAFE_HOURS_START}–"
                f"{config.WABA_SAFE_HOURS_END} МСК"
            )
            phase_stats["waba"] = _run_phase(
                "waba", targets, "WABA", stop_requested
            )
        else:
            # Фаза отката: берём провалы WABA из rescue_queue, FIFO.
            # ПЕРЕД чтением очереди проверяем здоровье канала: сегодня
            # Telegram принимал отправки (HTTP 200) и не доставлял ни одной,
            # а фаза продолжала жечь личный лимит, пока не остановили руками.
            if getattr(config, "CHANNEL_HEALTH_GUARD", True):
                _h = channel_delivery_health(phase_ch)
                if not _h["ok"]:
                    log.error(
                        f"🩺 Канал {phase_ch.upper()} НЕ РАБОТАЕТ: {_h['reason']}. "
                        f"Фаза пропущена, очередь не тронута — откат уйдёт "
                        f"следующим каналом. Проверь авторизацию канала в Wazzup."
                    )
                    continue
                if _h["sent"] >= int(
                        getattr(config, "CHANNEL_HEALTH_MIN_SAMPLE", 5)):
                    log.info(f"🩺 {phase_ch.upper()}: {_h['reason']}")

            limit = int(state.channel_effective_cap(state.load_state(), phase_ch))
            # exclude_tried: контакт, которому фаза TG уже не смогла
            # доставить, НЕ должен снова попасть в фазу MAX — иначе
            # он получит второе-третье сообщение за один прогон.
            #
            # original_channel ФИЛЬТРОМ НЕ ЗАДАЁМ. Раньше фаза отката читала
            # только провалы WABA (хардкод на этот канал), и это ломало ровно
            # ту цепочку, ради которой всё затевалось: если WABA не доставил,
            # а Telegram принял и тоже не доставил (тишина вебхука), сторож
            # кладёт такой контакт в очередь с original_channel='telegram' —
            # и фаза MAX его уже не видела. Контакт зависал навсегда: ни
            # Telegram, ни MAX. Теперь фаза берёт любые провалы из очереди;
            # exclude_tried не даёт одному контакту получить
            # второе сообщение в одном канале за прогон.
            pending = db.pending_rescues(
                limit=limit, cooldown_seconds=0, exclude_tried=phase_ch,
            )
            if not pending:
                log.info(
                    f"▶️  Фаза {phase_ch.upper()}: пусто (нет неподтверждённых "
                    f"контактов в очереди для этого канала)"
                )
                continue
            items = [
                {
                    "phone": r["phone"],
                    "name": r.get("name") or "—",
                    "category": r.get("category") or "—",
                    "rescue_id": r["id"],
                }
                for r in pending
            ]
            if not _phase_fits(phase_ch, len(items)):
                continue
            log.info(
                f"▶️  ФАЗА {phase_no + 1} · {phase_ch.upper()} · "
                f"{len(items)} откатов · окно "
                f"{config.PERSONAL_SAFE_HOURS_START}–"
                f"{config.PERSONAL_SAFE_HOURS_END} МСК"
            )
            phase_stats[phase_ch] = _run_phase(
                phase_ch, items, phase_ch.upper(), stop_requested
            )

    return phase_stats


def run() -> None:
    # Дедлайн и номер круга живут на уровне модуля: их читают
    # _run_seconds_left() и _run_all_phases(). Без `global` присваивание
    # ниже создавало ЛОКАЛЬНЫЕ имена, и первый же круг падал с
    # `UnboundLocalError: _RUN_CYCLE`, а режим длинного прогона видел
    # дедлайн None (никогда не останавливался по времени).
    global _RUN_DEADLINE, _RUN_CYCLE

    config.configure_logging()
    db.init_db()

    # Подключаем Telegram-логгер (если задан токен в env)
    from . import tglog
    tglog.install_handler()

    # Загружаем кэш мёртвых номеров (если файл есть) + миграция из crm.db
    bad_phones.load()
    channels = wazzup.active_channels()
    migrated = bad_phones.migrate_from_db(set(channels))
    log.info(
        f"Bad phones cache: {bad_phones.count()} контактов в кэше"
        + (f" (мигрировано {migrated} из crm.db)" if migrated else "")
    )

    templates_map = templates.load_templates()
    # WABA всегда добавляем явно (USE_WABA>=1): это канал Wazzup, не отдельный токен.
    if getattr(config, "USE_WABA", 0) and config.USE_WABA >= 1:
        if "waba" not in channels:
            channels = list(channels) + ["waba"]

    # SMS.RU добавляем ТОЖЕ явно: это отдельный транспорт, у него нет
    # WAZZUP_*_CHANNEL_ID, поэтому wazzup.active_channels() его никогда не
    # вернёт. Без этой строки каскад молча работал бы только на 3 мессенджера.
    sms_templates_map = templates.load_sms_templates()
    if "sms" not in channels:
        if not getattr(config, "SMSRU_ENABLED", 1):
            log.info("📱 SMS.RU выключен (BULK_SMSRU_ENABLED=0) — канал SMS не используется")
        elif not config.SMSRU_API_ID:
            log.info(
                "📱 SMS.RU пропущен: не задан BULK_SMSRU_API_ID в .env.local. "
                "Без api_id SMS.RU отвечает ошибкой 200."
            )
        elif not config.SMSRU_SENDER_NAME:
            log.info(
                "📱 SMS.RU пропущен: не задан BULK_SMSRU_SENDER_NAME. Без "
                "зарегистрированного имени отправителя SMS.RU отвечает 221."
            )
        elif not sms_templates_map:
            log.warning(
                "📱 SMS.RU пропущен: не найден файл %s с SMS-шаблонами "
                "(SMS без коротких шаблонов стоил бы 5-6 сегментов = 42-51 ₽)",
                config.SMSRU_TEMPLATES_PATH,
            )
        else:
            channels = list(channels) + ["sms"]
            log.info(
                "📱 SMS.RU подключён как fallback (отправитель %r, "
                "дневной лимит %d, сегментов максимум %d, шаблонов: %d)",
                config.SMSRU_SENDER_NAME,
                config.CHANNEL_DAILY_LIMITS.get("sms", 0),
                config.SMSRU_MAX_SEGMENTS,
                len(sms_templates_map),
            )

    # Pre-flight: проверяем что для ВСЕХ категорий есть валидный WABA template UUID.
    # Если хоть один шаблон невалидный — WABA будет фейлиться с BUILD_TEMPLATE_ERROR
    # на КАЖДОМ контакте (теряем API quota и время). Поэтому отключаем WABA
    # динамически на этот запуск и продолжаем через TG/MAX.
    from . import waba_templates as wabat
    waba_template_status = {}
    for cat in templates_map.keys():
        tid = wabat.pick_waba_template(cat)
        waba_template_status[cat] = {
            "tid": tid,
            "valid": wabat.is_valid_template_id(tid),
        }
    valid_count = sum(1 for s in waba_template_status.values() if s["valid"])
    total_count = len(waba_template_status)
    if getattr(config, "USE_WABA", 0) and config.USE_WABA >= 1 and valid_count < total_count:
        bad_cats = [cat for cat, s in waba_template_status.items() if not s["valid"]]
        log.warning(
            f"⚠️  WABA TEMPLATES: {valid_count}/{total_count} категорий имеют "
            f"валидный UUID. Плохие: {bad_cats}. "
            f"WABA будет SKIPPED для всех контактов этой сессии (cascade пойдёт "
            f"через TG/MAX). Это защищает от BUILD_TEMPLATE_ERROR на каждом контакте."
        )
        log.warning(
            "💡 Чтобы включить WABA: получи UUID одобренных шаблонов из Meta "
            "Business Manager (WhatsApp → Message Templates → 'API ID') и "
            "пропиши в .env.local как WABA_TEMPLATE_*_ID"
        )
        if "waba" in channels:
            channels = [c for c in channels if c != "waba"]

    log.info(f"Шаблонов: {list(templates_map.keys())}")
    log.info(f"Активные каналы (через Wazzup): {channels}")
    log.info(f"Google Sheets: {'включены' if sheets.SHEETS.enabled else 'ОТКЛЮЧЕНЫ'}")

    if not channels:
        log.error(
            "Нет активных каналов. Проверьте WAZZUP_API_KEY и "
            "WAZZUP_*_CHANNEL_ID для telegram/max/whatsapp/waba в .env.local. "
            "Для SMS: BULK_SMSRU_ENABLED=1, BULK_SMSRU_API_ID, "
            "BULK_SMSRU_SENDER_NAME и файл %s."
            % config.SMSRU_TEMPLATES_PATH
        )
        tglog.send(
            "⛔ Sender не запущен: нет активных каналов (проверь WAZZUP_*_CHANNEL_ID)",
            "ERROR",
        )
        return

    log.info("=" * 70)
    log.info("📋 ШАБЛОНЫ СООБЩЕНИЙ:")
    for k, tpl in templates_map.items():
        snippet = (tpl[:200] + "…") if len(tpl) > 200 else tpl
        log.info(f"   • {k}: {snippet}")
    log.info("=" * 70)

    try:
        all_contacts = contacts.load_contacts(config.EXCEL_PATH)
    except FileNotFoundError:
        log.error(f"Файл контактов не найден: {config.EXCEL_PATH}")
        return

    with db.db_conn() as conn:
        sent_phones = db.get_sent_phones(conn)

    # ЗАЩИТА ОТ ДУБЛЕЙ: если crm.db пустая, но в Excel много контактов — это подозрительно.
    # Скорее всего забыли залить базу с историей → все 4000+ контактов уйдут в рассылку
    # повторно. Лучше остановиться и спросить.
    if len(sent_phones) == 0 and len(all_contacts) > 100:
        log.critical(
            f"🛑 ЗАЩИТА ОТ ДУБЛЕЙ: crm.db ПУСТАЯ (0 уникальных контактов в истории), "
            f"но в Excel {len(all_contacts)} контактов. Похоже, база с историей "
            f"не залита на сервер. Если запустить — все контакты получат повторную "
            f"рассылку. ОСТАНОВКА. Залейте crm.db: "
            f"scp data/crm.db user@server:/opt/bulkmessage/data/crm.db"
        )
        log.critical("💡 Если это ДЕЙСТВИТЕЛЬНО первый запуск (никогда раньше не слали) — "
                    "поставьте BULK_SKIP_DUP_PROTECTION=1 в .env.local и перезапустите.")
        if not os.environ.get("BULK_SKIP_DUP_PROTECTION"):
            return
        log.warning("⚠️  BULK_SKIP_DUP_PROTECTION=1 — защита отключена, продолжаю")

    # --- Импорт признака согласия из Excel ----------------------------------
    # Если в таблице есть столбец согласия, обновляем consent в БД.
    # denied из Excel перекрывает всё, granted — разрешает.
    try:
        _imp = {"granted": 0, "denied": 0, "unknown": 0}
        with db.db_conn() as _conn:
            for _ct in all_contacts:
                _raw = _ct.get("consent") or ""
                if not _raw:
                    continue
                _st = db.normalize_consent(_raw)
                if _st == db.CONSENT_UNKNOWN:
                    continue  # пустое/непонятное значение не трогаем
                db.set_consent(_ct["phone"], _st, source="excel", conn=_conn)
                _imp[_st] += 1
        if any(_imp.values()):
            log.info(
                f"📥 Импорт согласия из Excel: granted={_imp['granted']}, "
                f"denied={_imp['denied']}, unknown-без-данных={_imp['unknown']}"
            )
    except Exception as e:
        log.warning(f"consent import from excel failed: {e}")

    _cstats = db.consent_stats()
    log.info("=" * 70)
    log.info("📊 СТАТИСТИКА ЗАПУСКА:")
    log.info(f"   • Excel-файл: {config.EXCEL_PATH}")
    log.info(f"   • Контактов в файле: {len(all_contacts)}")
    log.info(f"   • Уже отправлено ранее: {len(sent_phones)}")
    log.info(f"   • Каналов активно: {channels}")
    # Лимиты печатаем ТОЛЬКО для активных каналов. Раньше тут был весь
    # config.CHANNEL_DAILY_LIMITS, где есть и whatsapp (legacy Personal WA,
    # канал не подключён — WAZZUP_WHATSAPP_CHANNEL_ID пуст), и sms (отключён).
    # В логе это читалось как «WhatsApp и WABA — два канала, оба работают»,
    # хотя активны ровно три: waba, telegram, max.
    _lim = {ch: cap for ch, cap in config.CHANNEL_DAILY_LIMITS.items()
            if ch in channels}
    _skipped = sorted(set(config.CHANNEL_DAILY_LIMITS) - set(channels))
    log.info(f"   • Дневные лимиты (по активным): {_lim}")
    if _skipped:
        log.info(f"   • Не используются (нет канала/выключены): {_skipped}")
    log.info(f"   • Google Sheets: {'включены' if sheets.SHEETS.enabled else 'ОТКЛЮЧЕНЫ'}")
    log.info(f"   • Анти-блок: батч={config.BATCH_SIZE}, "
             f"задержка={config.DELAY_MIN}-{config.DELAY_MAX}с")
    # Согласие — ключевой показатель риска бана и штрафа по ФЗ-38.
    log.info(
        f"   • Согласие: есть {_cstats['granted']}, "
        f"отказ {_cstats['denied']}, "
        f"нет данных {_cstats['unknown']} (из {_cstats['total']})"
    )
    if _cstats["denied"]:
        log.info(
            f"   🚫 Отказников {_cstats['denied']} — им НИКОГДА не пишем "
            f"(уважается всегда, независимо от CONSENT_REQUIRED_FOR_COLD)"
        )
    if _cstats["granted"] == 0 and _cstats["total"] > 0:
        if config.CONSENT_REQUIRED_FOR_COLD:
            log.info("   🛑 CONSENT_REQUIRED_FOR_COLD=1, но согласие не подтверждено "
                     "ни у кого — отправка будет пропускать всех.")
        else:
            log.warning(
                "   ⚠️  Ни у кого нет подтверждённого согласия. Массовая холодная "
                "рассылка без opt-in — нарушение ч.1 ст.18 ФЗ-38 (штраф юрлицам "
                "до 1 000 000 ₽) и главная причина блокировок. Разделите базу "
                "по согласию и включите BULK_CONSENT_REQUIRED_FOR_COLD=1."
            )
    log.info("=" * 70)

    backoffs: dict[str, int] = {ch: 0 for ch in channels}
    permanent_skipped: dict[str, set[str]] = {}
    stop_requested = {"v": False, "sig": None}
    lock_path = None  # будет установлен ниже

    # TG: уведомляем админа о старте рассылки
    try:
        _start_state = state.load_state()
        _sent_today = sum(_start_state.get("sent_today", {}).values())
    except Exception:
        _sent_today = 0
    if config.DRY_RUN:
        tglog.send(
            f"🧪 Sender ЗАПУЩЕН (DRY-RUN).\n"
            f"Каналы: {', '.join(channels)}\n"
            f"Лимиты: {config.CHANNEL_DAILY_LIMITS}\n"
            f"Уже отправлено сегодня: {_sent_today}\n"
            f"Окно: {config.ACTIVE_HOURS_START:02d}:00-"
            f"{config.ACTIVE_HOURS_END:02d}:00 {config.TIMEZONE_NAME}\n"
            f"⚠️  Это DRY-RUN, реальных отправок не будет.",
            "INFO",
        )
    else:
        tglog.send(
            f"🚀 Sender ЗАПУЩЕН (БОЕВОЙ РЕЖИМ).\n"
            f"Каналы: {', '.join(channels)}\n"
            f"Лимиты: {config.CHANNEL_DAILY_LIMITS}\n"
            f"Уже отправлено сегодня: {_sent_today}\n"
            f"Окно: {config.ACTIVE_HOURS_START:02d}:00-"
            f"{config.ACTIVE_HOURS_END:02d}:00 {config.TIMEZONE_NAME}",
            "INFO",
        )

    # H4: предупреждаем если последние 10 сообщений в БД — все failed
    # (значит WAZZUP_API_KEY/channel невалиден или аккаунт в бане — лучше не запускать)
    if not config.DRY_RUN:
        try:
            with db.db_conn() as conn:
                c = conn.cursor()
                c.execute("""
                    SELECT status, COUNT(*) FROM (
                        SELECT status FROM messages
                        WHERE sent_at IS NOT NULL
                        ORDER BY id DESC LIMIT 10
                    ) GROUP BY status
                """)
                rows = c.fetchall()
                total_recent = sum(r[1] for r in rows)
                failed_recent = sum(r[1] for r in rows if r[0] == "failed")
                if total_recent >= 5 and failed_recent == total_recent:
                    log.warning(
                        f"⚠️  ВНИМАНИЕ: последние {total_recent} отправок в БД — "
                        f"ВСЕ failed. Возможно, WAZZUP_API_KEY протух или аккаунт в бане. "
                        f"Рекомендую сначала прогнать config.preflight_check() перед боем."
                    )
        except Exception as e:
            log.warning(f"⚠️ Не удалось проверить историю failed: {e}")

    def _on_signal(sig, frame):
        """Корректная остановка: текущий контакт дорабатывает, цикл выходит."""
        try:
            sig_name = signal.Signals(sig).name
        except Exception:
            sig_name = f"signal {sig}"
        log.info(
            f"\n⛔ Получен {sig_name} — останавливаюсь после текущего контакта. "
            f"Отправленные сообщения уже сохранены в БД."
        )
        stop_requested["v"] = True
        stop_requested["sig"] = sig
        # Повторное нажатие — принудительный выход
        try:
            signal.signal(signal.SIGINT, _force_exit)
            signal.signal(signal.SIGTERM, _force_exit)
        except Exception:
            pass

    def _force_exit(sig, frame):
        log.error(
            "\n⛔⛔ Повторный сигнал — принудительный выход. Состояние сохранено."
        )
        raise KeyboardInterrupt()

    # SIGINT работает везде, SIGTERM — только на Unix.
    # На Windows пытаемся установить SIGTERM, но это не падает — просто игнорируется.
    try:
        signal.signal(signal.SIGINT, _on_signal)
    except Exception as e:
        log.warning(f"⚠️  Не удалось установить SIGINT handler: {e}")
    try:
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError, AttributeError) as e:
        # На Windows SIGTERM может не поддерживаться — это нормально, не warning
        if os.name != "nt":
            log.warning(f"⚠️  Не удалось установить SIGTERM handler: {e}")

    # C1: file lock — защита от двойного запуска
    lock_path = _try_acquire_lock()
    if lock_path is None:
        log.error("⛔ Sender не запущен из-за занятости lock-файла.")
        tglog.send(
            f"⛔ Sender не запущен — другой процесс держит lock-файл: {config.LOCK_PATH}. "
            f"Проверь, не запущен ли sender дважды.",
            "ERROR",
        )
        return

    log.info("─" * 70)
    log.info("💡 Нажмите Ctrl+C в этом терминале для корректной остановки.")
    log.info("─" * 70)

    # Стартовое предупреждение + оценка общего времени на 100 сообщений
    if config.DRY_RUN:
        log.warning(
            "🧪 DRY-RUN ВКЛЮЧЁН (BULK_DRY_RUN=1) — реальных отправок не будет, "
            "только имитация. Для боевого запуска установи BULK_DRY_RUN=0."
        )
    else:
        transport_label = "Wazzup (WABA → TG → MAX)"
        log.warning(
            f"🚀 БОЕВОЙ РЕЖИМ — сейчас будут ОТПРАВЛЕНЫ реальные сообщения через {transport_label}. "
            "Чтобы остановиться: Ctrl+C (текущий контакт доработает, остальные отменятся)."
        )

    # Активное окно + TZ
    log.info(f"🌍 Timezone: {config.TIMEZONE_NAME}")
    if config.ACTIVE_HOURS_START > 0 or config.ACTIVE_HOURS_END > 0:
        log.info(
            f"🕐 Активное окно: {config.ACTIVE_HOURS_START:02d}:00-"
            f"{config.ACTIVE_HOURS_END:02d}:00 ({config.TIMEZONE_NAME})"
        )
        now = config.now_tz()
        if not config.is_within_active_hours(now):
            wait = config.seconds_until_active_window(now) / 3600
            log.info(f"   Сейчас вне окна. До начала: {wait:.1f}ч")
        else:
            log.info(f"   Сейчас в окне ({now.strftime('%H:%M')})")

    # Грубая оценка общего времени на 100 сообщений (для оператора).
    # Channel-aware: 90% контактов ожидаем через WABA (быстро), 10% через
    # Personal (медленнее). Реальный mix зависит от WABA success rate.
    waba_avg = (config.WABA_DELAY_MIN + config.WABA_DELAY_MAX) / 2
    personal_avg = (config.PERSONAL_DELAY_MIN + config.PERSONAL_DELAY_MAX) / 2
    avg_delay = 0.9 * waba_avg + 0.1 * personal_avg
    msgs_per_batch = max(1, config.BATCH_SIZE)
    n_batches = max(0, 100 // msgs_per_batch - 1)  # кол-во длинных перерывов
    avg_break = (
        (config.BATCH_BREAK_MIN + config.BATCH_BREAK_MAX) / 2
        if config.BATCH_BREAK_MAX > 0 else 0
    )
    est_total = 100 * avg_delay + n_batches * avg_break
    log.info(
        f"⏱  Прогноз: 100 сообщений ≈ {_estimate_total_time(est_total)} "
        f"(средняя пауза {_fmt_duration(avg_delay)}, "
        f"длинный перерыв каждые {msgs_per_batch} сообщений ≈ "
        f"{_fmt_duration(avg_break)})"
    )
    log.info("─" * 70)

    # Скип: если контакту УЖЕ отправляли в любой канал — пропускаем полностью.
    contacts_to_send = [c for c in all_contacts if c["phone"] and c["phone"] not in sent_phones]

    # Фильтр по 4 целевым категориям из Message_script.md.
    # Нормализуем категорию тем же алгоритмом, что и templates.py.
    filtered: list[dict] = []
    skipped_by_cat: dict[str, int] = {}
    for c in contacts_to_send:
        cat_raw = (c.get("category") or "").strip()
        norm = templates.normalize_category(cat_raw)
        if norm in config.ALLOWED_CATEGORIES:
            c["_normalized_category"] = norm
            filtered.append(c)
        else:
            skipped_by_cat[cat_raw or "(пусто)"] = skipped_by_cat.get(cat_raw or "(пусто)", 0) + 1

    log.info("=" * 70)
    log.info("🎯 ФИЛЬТР ПО КАТЕГОРИЯМ (Message_script.md):")
    log.info(f"   Допустимые категории: {sorted(config.ALLOWED_CATEGORIES)}")
    log.info(f"   Уникальных статусов в Excel: "
             f"{sorted({c.get('category', '—') for c in all_contacts})}")
    log.info(f"   Всего контактов с телефоном: {len(contacts_to_send)}")
    log.info(f"   ✅ Подходит для рассылки: {len(filtered)}")
    if skipped_by_cat:
        log.info(f"   ⏭️  Пропущено (статус не в скрипте): {sum(skipped_by_cat.values())}")
        for cat, n in sorted(skipped_by_cat.items(), key=lambda x: -x[1]):
            log.info(f"      • {cat!r}: {n}")
    log.info("=" * 70)

    # Распределение по целевым категориям
    by_cat: dict[str, int] = {}
    for c in filtered:
        by_cat[c["_normalized_category"]] = by_cat.get(c["_normalized_category"], 0) + 1
    log.info("📊 Распределение по целевым категориям:")
    for cat in sorted(config.ALLOWED_CATEGORIES):
        log.info(f"   • {cat}: {by_cat.get(cat, 0)}")
    log.info("─" * 70)

    if filtered:
        preview = filtered[:5]
        for i, c in enumerate(preview, 1):
            log.info(f"   {i}. {c.get('phone')} — {c.get('name', '—')} "
                     f"({c.get('category', '—')} → {c['_normalized_category']})")
        if len(filtered) > 5:
            log.info(f"   … и ещё {len(filtered) - 5}")
    log.info("─" * 70)

    try:
        # Phase 8: сторож подтверждений ПЕРЕД сливом rescue.
        # Он находит сообщения, которые транспорт принял, но доставка не
        # подтверждена (webhook потерян или подписка молчит), и превращает
        # «молчание» в явный отказ → контакт попадает в rescue. Без этого
        # недоставки были невидимы: контакт навсегда оставался в статусе
        # 'sent' и просто исчезал из воронки.
        try:
            from . import reconcile as _reconcile
            _swept = _reconcile.sweep_unconfirmed_deliveries(limit=50)
            if _swept:
                log.info(
                    f"🔍 Сторож подтверждений вернул {_swept} контактов в rescue"
                )
        except Exception as e:
            log.warning(f"sweep unconfirmed failed (non-fatal): {e}")

        # Phase 7: drain rescue queue перед основным циклом
        # (если webhook'и уже пришли с момента прошлого запуска)
        try:
            # current_state — это state, загруженный ЗДЕСЬ. Раньше стояло
            # имя, которого в run() не существует: NameError на КАЖДЫЙ
            # запуск, пойманный пустым except ниже, и `_drain_rescue_queue`
            # молча не отрабатывал никогда.
            _drain_current = state.reset_daily_if_new_day(state.load_state())
            _drain_rescue_queue(_drain_current)
        except Exception as e:
            log.warning(f"rescue queue drain failed (non-fatal): {e}")

        # =====================================================================
        # ФАЗОВАЯ РАССЫЛКА: WABA-проход → Telegram → MAX
        # =====================================================================
        # Канал теперь задаётся ФАЗОЙ, а не по-контактным каскадом.
        # target_reached() внутри фаз НЕ проверяется: он считает ПРИНЯТИЕ
        # (HTTP 2xx), а 250 принятых WABA дали бы 250 «успешных» и цикл встал
        # бы на первом же контакте фазы Telegram, не дав откату отработать.
        # Итог считается по delivered_today в финальной воронке.
        backoffs: dict[str, int] = {}
        permanent_skipped: dict[str, set[str]] = {}

        # Режим длинного прогона: один процесс крутит круги фаз, пока не
        # выйдет дедлайн BULK_RUN_HOURS от старта. Без него — один круг
        # (старое поведение), и дальше процесс завершается.
        run_hours = float(getattr(config, "RUN_HOURS", 0) or 0)
        loop_pause = float(getattr(config, "RUN_LOOP_PAUSE_MINUTES", 10) or 0)

        # Сброс на каждый вызов run(): иначе повторный запуск в том же
        # процессе унаследует дедлайн и номер круга предыдущего.
        _RUN_CYCLE = 0
        _RUN_DEADLINE = None
        if run_hours > 0:
            _RUN_DEADLINE = time.time() + run_hours * 3600
            _dl = datetime.fromtimestamp(_RUN_DEADLINE, tz=timezone.utc)
            _dl_local = _dl.astimezone(config.now_tz().tzinfo)
            log.info("=" * 70)
            log.info(
                f"⏱️  ДЛИННЫЙ ПРОГОН: {run_hours:g}ч от старта "
                f"(дедлайн {_dl_local.strftime('%H:%M')} МСК)"
            )
            log.info(
                "   Планировщик НЕ нужен: sender сам перезапускает фазы, "
                "пока не истечёт время или не кончится база."
            )
            log.info("=" * 70)

        all_phase_stats: dict[str, int] = {}
        while True:
            # Каждый круг пересобирает список контактов: подхватываются новые,
            # уже отправленные отсекаются по БД.
            cycle_filtered = _select_contacts_for_cycle(all_contacts)
            cycle_stats = _run_all_phases(
                cycle_filtered, stop_requested, backoffs, permanent_skipped,
                cycle_no=(_RUN_CYCLE + 1),
            )
            for _ch, _n in cycle_stats.items():
                all_phase_stats[_ch] = all_phase_stats.get(_ch, 0) + _n
            _RUN_CYCLE += 1

            if run_hours <= 0:
                break
            left = _run_seconds_left()
            if left <= 0:
                log.info("⏹️  Дедлайн длинного прогона достигнут — выходим.")
                break
            if stop_requested["v"]:
                break
            cur_chk = state.reset_daily_if_new_day(state.load_state())
            if state.max_attempts_reached(cur_chk):
                log.info(
                    "🛑 SAFETY CAP — больше попыток сегодня делать нельзя."
                )
                break

            # Ничего не отправилось? Не гоняем пустые круги впустую.
            if not any(cycle_stats.values()):
                log.info(
                    f"😴 Круг отработал вхолостую (база/очередь пусты), жду "
                    f"{loop_pause:g} мин до следующего"
                )
            _sleep_phase(min(loop_pause, left) * 60, stop_requested)

        phase_stats = all_phase_stats
        log.info("─" * 70)
        log.info(f"📦 Фазы отработаны (кругов: {_RUN_CYCLE}): "
                 f"{phase_stats or '—'}")
        try:
            log.info(
                f"📋 Очередь на SMS (осталось pending): "
                f"{db.count_pending_rescues()}"
            )
        except Exception:
            pass
        log.info("─" * 70)

    finally:
        # Всегда освобождаем lock, даже при exception/KeyboardInterrupt.
        # Если бы не finally, повторный запуск считал бы "sender уже идёт".
        _release_lock(lock_path)
        log.debug("Lock-файл освобождён")

    log.info("=" * 70)
    log.info("✅ SENDER LOOP FINISHED.")
    final_state = state.load_state()
    log.info(f"   Итог за сегодня: {final_state.get('sent_today', {})}")
    log.info(f"   Всего активных каналов было: {channels}")

    # Честная воронка: принято ≠ доставлено. Доставка подтверждается вебхуком
    # (или сторожем), поэтому на момент остановки она может быть неполной —
    # это нормально и видно отдельной строкой.
    try:
        _acc = state.accepted_total_today(final_state)
        _dlv = state.delivered_total_today(final_state)
        _flt = sum(int(v) for v in (final_state.get("failed_today") or {}).values())
        _rate = (_dlv / _acc * 100.0) if _acc else 0.0
        _tgt = int(final_state.get("target_today", config.TARGET_SUCCESS_PER_DAY))
        log.info("─" * 70)
        log.info("   📊 ВОРОНКА ДОСТАВКИ (честные цифры):")
        log.info(f"      принято транспортом : {_acc}")
        log.info(f"      доставлено (webhook) : {_dlv}  ({_rate:.0f}%)")
        log.info(f"      отказов              : {_flt}")
        if _acc > _dlv + _flt:
            log.info(
                f"      без подтверждения    : {_acc - _dlv - _flt} "
                f"(ждём вебхук / сторож через "
                f"{config.CONFIRM_TIMEOUT_MINUTES} мин)"
            )
        log.info(f"      цель дня            : {_tgt}")
        log.info("   Полная воронка: `py main.py report`")
        log.info("─" * 70)
    except Exception as e:
        log.warning(f"delivery funnel summary failed: {e}")
    log.info("=" * 70)
    # TG: финальный отчёт
    tglog.send(
        f"✅ Sender ЗАВЕРШЁН.\n"
        f"Итог за сегодня: {final_state.get('sent_today', {})}\n"
        f"Каналов: {channels}\n"
        f"Режим: {'DRY-RUN' if config.DRY_RUN else 'БОЕВОЙ'}",
        "INFO",
    )


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("\n👋 Прервано пользователем (Ctrl+C). Все отправленные сообщения "
                 "уже сохранены в базе. Повторный запуск продолжит с того же места.")
    except Exception as e:
        log.error(f"Fatal: {e}")
        raise
