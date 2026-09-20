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
from datetime import datetime
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
    """Записывает неудачную отправку в БД (только для аудита, квоту не меняет)."""
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
    except Exception as e:
        log.warning(f"      ⚠️ Не удалось записать failed в БД: {e}")


def _cascade_order() -> list[str]:
    """Возвращает порядок каскада в зависимости от USE_WABA флага.

    USE_WABA=0 (default, legacy): whatsapp → telegram → max.
    USE_WABA=1: waba → telegram → max (основной режим).
    """
    if getattr(config, "USE_WABA", 0) and config.USE_WABA >= 1:
        return ["waba", "telegram", "max"]
    # legacy
    return ["whatsapp", "telegram", "max"]


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

    # Выбираем категорию и шаблон (Phase 1.2)
    norm_category = templates.normalize_category(category)
    template_id = waba_templates.ensure_waba_template_for_category(norm_category)

    text = templates.build_message(contact, templates.load_templates())

    # Cascade: только каналы которые активны в текущей сессии
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
            # Решаем: template или free text (24ч-окно)
            in_24h_window = state.is_waba_in_24h_window(current_state, phone)
            use_template = bool(template_id) and not in_24h_window
            log.info(
                f"      🧩 WABA decision: template_id={'set' if template_id else 'NONE'}, "
                f"in_24h_window={in_24h_window}, mode="
                f"{'template' if use_template else 'free-text'}"
            )
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
                log.info(
                    f"   ✅ WABA OK за {elapsed:.1f}с | message_id={message_id} | "
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
                )
                state.save_state(current_state)
                sent_any = True
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
                    backoffs["waba"] = _next_backoff(kind, backoffs["waba"])
                    state.halt_channel(current_state, "waba")
                    _persist_failure(phone, name, category, "waba", text, "auth: " + detail, "auth")
                    continue
                else:
                    # TRANSIENT / RATE_LIMIT / UNKNOWN — сбрасываем circuit breaker
                    current_state["_waba_consecutive_permanent"] = 0
                    backoffs["waba"] = _next_backoff(kind, backoffs["waba"])
                    _persist_failure(phone, name, category, "waba", text, detail, kind.value)
                    log.info(f"      ⏭️  WABA [{kind.value}] — backoff={backoffs['waba']}с")
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
            # Считаем contact как "successful" ОДИН раз (при первой успешной отправке)
            if not counted_successful:
                state.increment_successful(current_state)
                state.save_state(current_state)
                counted_successful = True
            from datetime import datetime, timezone
            state.set_last_send_per_phone(
                current_state, phone, datetime.now(timezone.utc).isoformat(),
            )
            state.save_state(current_state)
            break  # Phase 2.2 cascade: НЕ continue — broadcast был старым поведением

        kind = wazzup.classify_error(detail, http_status)
        log.warning(
            f"   ❌ {channel.upper()} FAIL [{kind.value}] http={http_status} "
            f"за {elapsed:.1f}с | {phone} ({name}) | {detail[:200]}"
        )

        if kind == ErrorKind.PERMANENT:
            permanent_skipped.setdefault(phone, set()).add(channel)
            _persist_failure(phone, name, category, channel, text, detail, "permanent")
            log.info(
                f"      🚫 PERMANENT — {channel} помечен как невалидный для этого "
                f"контакта, пробуем другие каналы"
            )
            backoffs[channel] = 0
            continue

        if kind == ErrorKind.AUTH:
            log.error(
                f"      🔐 AUTH — Wazzup API key/channel невалиден для {channel}! "
                f"Проверьте WAZZUP_API_KEY и WAZZUP_{channel.upper()}_CHANNEL_ID. "
                f"Backoff = {config.RATE_LIMIT_BACKOFF_MAX}с"
            )
            backoffs[channel] = _next_backoff(kind, backoffs[channel])
            _persist_failure(phone, name, category, channel, text,
                             "auth: " + detail, "auth")
            continue

        # RATE_LIMIT или TRANSIENT — накапливаем backoff, идём дальше
        backoffs[channel] = _next_backoff(kind, backoffs[channel])
        _persist_failure(phone, name, category, channel, text, detail, kind.value)
        log.info(
            f"      ⏭️  {channel.upper()} [{kind.value}] — backoff={backoffs[channel]}с. "
            f"Деталь: {detail[:120]}"
        )

    # Если контакт НИГДЕ не доставлен (sent_any=False), помечаем как полностью мёртвый
    # для экспорта (data/failed_today.json)
    if not sent_any and cascade:
        failed_channels = [ch for ch in cascade]
        failed_contacts.mark_fully_failed(
            phone=phone, name=name, category=category, channels_failed=failed_channels
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


def run() -> None:
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
            "WAZZUP_*_CHANNEL_ID для telegram/max/whatsapp/waba в .env.local."
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

    log.info("=" * 70)
    log.info("📊 СТАТИСТИКА ЗАПУСКА:")
    log.info(f"   • Excel-файл: {config.EXCEL_PATH}")
    log.info(f"   • Контактов в файле: {len(all_contacts)}")
    log.info(f"   • Уже отправлено ранее: {len(sent_phones)}")
    log.info(f"   • Каналов активно: {channels}")
    log.info(f"   • Дневные лимиты: {config.CHANNEL_DAILY_LIMITS}")
    log.info(f"   • Google Sheets: {'включены' if sheets.SHEETS.enabled else 'ОТКЛЮЧЕНЫ'}")
    log.info(f"   • Анти-блок: батч={config.BATCH_SIZE}, "
             f"задержка={config.DELAY_MIN}-{config.DELAY_MAX}с")
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

    # Грубая оценка общего времени на 100 сообщений (для оператора)
    avg_delay = (config.DELAY_MIN + config.DELAY_MAX) / 2
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
        for idx, contact in enumerate(filtered):
            if stop_requested["v"]:
                log.info("⛔ Остановка по сигналу")
                break

            current_state = state.load_state()
            current_state = state.reset_daily_if_new_day(current_state)

            # H1: если какой-то канал в backoff — ждём перед обработкой
            if any(v > 0 for v in backoffs.values()):
                _wait_for_backoffs(backoffs, log)

            # Активное окно (например, 10:00-22:00 МСК): вне его спим до начала
            if _wait_until_active_window(log):
                # После ожидания сбрасываем состояние (мог начаться новый день)
                current_state = state.load_state()
                current_state = state.reset_daily_if_new_day(current_state)

            # 🎯 ГЛАВНАЯ ЦЕЛЬ: достичь target_today успешных контактов
            if state.target_reached(current_state):
                successful = state.successful_today(current_state)
                target = current_state.get("target_today", config.TARGET_SUCCESS_PER_DAY)
                log.info(
                    f"🎯 ЦЕЛЬ ДОСТИГНУТА: {successful}/{target} успешных контактов сегодня. "
                    f"Остановка до завтра."
                )
                tglog.send(
                    f"🎯 Рассылка за {datetime.now().strftime('%Y-%m-%d')}: "
                    f"{successful}/{target} успешных контактов. Цель достигнута!",
                    "INFO",
                )
                break  # Done for today

            # Safety cap: слишком много попыток за день
            if state.max_attempts_reached(current_state):
                attempts = state.attempts_today(current_state)
                successful = state.successful_today(current_state)
                log.info(
                    f"🛑 Достигнут SAFETY CAP: {attempts} попыток за день, "
                    f"{successful} успешных. Остановка."
                )
                tglog.send(
                    f"🛑 SAFETY CAP: {attempts} попыток, {successful} успешных. "
                    f"Остановка до завтра.",
                    "WARNING",
                )
                break

            # Учитываем попытку (счётчик attempts_today)
            state.increment_attempts(current_state)
            state.save_state(current_state)

            sent_ok = _run_one_contact(
                contact, channels, backoffs, permanent_skipped, current_state
            )

            if not sent_ok:
                log.info(f"  ↪️ Не доставлено никому (нет доступных каналов)")
            else:
                # Логируем прогресс
                successful = state.successful_today(current_state)
                target = current_state.get("target_today", config.TARGET_SUCCESS_PER_DAY)
                log.info(f"  📈 Прогресс: {successful}/{target} успешных сегодня")

            # Пауза между контактами:
            # - sent_ok=True (1+ канал доставлен) → 5-7 мин (анти-бан)
            # - sent_ok=False (все 3 канала fail) → 5-15 сек (сразу next)
            if not sent_ok:
                delay = random.uniform(config.FAILED_DELAY_MIN, config.FAILED_DELAY_MAX)
                log.info(f"  ⏩ Пропускаем быстро ({_fmt_duration(delay)}) — контакт мёртвый")
            else:
                delay = random.uniform(config.DELAY_MIN, config.DELAY_MAX)
                if random.random() < 0.10:
                    delay += random.uniform(30, 90)
                    log.info(f"  💤 Удлинённая пауза {_fmt_duration(delay)} (анти-бан)")
                else:
                    log.info(f"  💤 Пауза между контактами: {_fmt_duration(delay)}")
            time.sleep(delay)

            total_sent = sum(current_state.get("sent_today", {}).values())
            if total_sent > 0 and total_sent % config.BATCH_SIZE == 0:
                # Если лимит на паузу между батчами = 0 — перерыв отключён
                if config.BATCH_BREAK_MAX <= 0:
                    log.info(
                        f"✅ Батч {total_sent // config.BATCH_SIZE} завершён "
                        f"({total_sent} сообщений сегодня). Перерыв между батчами отключён."
                    )
                else:
                    batch_break = random.uniform(config.BATCH_BREAK_MIN, config.BATCH_BREAK_MAX)
                    log.info(
                        f"🛌 Батч {total_sent // config.BATCH_SIZE} завершён "
                        f"({total_sent} сообщений сегодня). "
                        f"Перерыв {batch_break / 60:.0f} мин..."
                    )
                    slept = 0.0
                    while slept < batch_break and not stop_requested["v"]:
                        time.sleep(min(60, batch_break - slept))
                        slept += 60
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
