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
import sqlite3
import time
from datetime import datetime, timedelta, timezone
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
from .store import contacts as repo_contacts
from .store import messages as repo_messages

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
                            f"Sender запущен в ДРУГОМ контейнере "
                            f"(hostname={old_hostname}, PID={old_pid}). "
                            f"Второй запуск отменён."
                        )
                        return None
                    else:
                        log.warning(
                            f"Stale lock от другого контейнера "
                            f"(hostname={old_hostname}), перезаписываю"
                        )
                elif not _pid_alive(old_pid):
                    log.warning(
                        f"Stale lock от PID {old_pid} (процесс мёртв), "
                        f"перезаписываю"
                    )
                else:
                    log.error(
                        f"Sender уже запущен (PID={old_pid}, "
                        f"hostname={old_hostname or '?'}, lock={lock_path}). "
                        f"Второй запуск отменён."
                    )
                    return None
            except (ValueError, OSError) as e:
                log.warning(f"Битый lock-файл ({e}), перезаписываю")
        # Пишем "<pid>:<hostname>" чтобы при следующем старте можно было отличить
        lock_content = f"{os.getpid()}:{_container_hostname()}"
        lock_path.write_text(lock_content, encoding="utf-8")
        return lock_path
    except (PermissionError, OSError) as e:
        log.error(f"Не могу создать lock-файл: {e}")
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
                    return True # процесс есть, но не наш
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
        log.warning(f"Не могу удалить lock-файл: {e}")


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
        f"Вне активного окна (МСК {config.ACTIVE_HOURS_START:02d}:00-"
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


# Кэш «номер → каналы, куда уже пытались». Загружается один раз за процесс:
# _run_one_contact зовётся на каждый контакт каждого цикла, и перечитывать
# таблицу целиком каждый раз — это тысячи лишних SELECT за прогон.
_ATTEMPTED_CHANNELS: Optional[dict[str, set[str]]] = None


def _attempted_channels_for(phone: str) -> set[str]:
    """Каналы, в которые по этому номеру УЖЕ была попытка.

    Правило «написали — навсегда» действует по каналу, а не на контакт
    (решение владельца 2026-10-12): человек может иметь только один
    мессенджер из трёх. Закрывается ровно тот канал, куда уже пытались.
    """
    global _ATTEMPTED_CHANNELS
    if _ATTEMPTED_CHANNELS is None:
        try:
            _ATTEMPTED_CHANNELS = db.get_attempted_channels()
        except Exception as e:
            # Не глотаем: без этого кэша каскад решил бы, что попыток не
            # было, и пошёл бы слать повторно — а повтор в личный канал это
            # жалоба и бан номера. Лучше упасть на старте, чем слать.
            log.error(
                f"история попыток по каналам не прочитана: {type(e).__name__}: {e}. "
                f"Прогон остановлен: без неё защита от повторов не работает."
            )
            raise
    return _ATTEMPTED_CHANNELS.get(phone) or set()


def _mark_attempted(phone: str, channel: str) -> None:
    """Отметить канал закрытым в кэше — сразу после успешной отправки.

    Иначе в длинном прогоне кэш устареет: контакт, получивший сообщение в
    начале прогона, снова стал бы «не получавшим» к следующему циклу.
    """
    if _ATTEMPTED_CHANNELS is not None:
        _ATTEMPTED_CHANNELS.setdefault(phone, set()).add(channel)


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
    # Кэш защиты от повторов обновляем СРАЗУ: этот канал для этого номера
    # теперь закрыт, и следующий цикл прогона обязан это видеть.
    _mark_attempted(phone, channel)
    # Попытка считается по РЕАЛЬНОЙ отправке, а не по проходу цикла фазы.
    # Живой случай 2026-10-09: счётчик стоял в теле цикла, и 400 проходов дали
    # 99 реальных сообщений — SAFETY CAP съел фазы Telegram и MAX целиком, а
    # лимит WABA 250 остался выбранным на треть. Контакт, отброшенный фильтром
    # наличия аккаунта, попытку теперь не расходует — это правильно: попытка
    # означает сообщение, дошедшее до транспорта.
    state.increment_attempts(current_state)
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
                _synced = sync_message_to_sheet(
                    contact_id=contact_id,
                    channel=channel,
                    status="sent",
                    message_pk=message_pk,
                )
                # Раньше здесь стоял log.info безусловно, и в DRY_RUN он писал
                # «синхронизировано» для каждого контакта, хотя sync вернул False:
                # sheets.py отбрасывает dryrun_* как нереальную отправку. Строка
                # врала ровно тогда, когда её читали, чтобы верить.
                if _synced:
                    log.info(f"Google Sheets синхронизирован (pk={message_pk})")
            except Exception as e:
                log.error(f"sync_message_to_sheet error: {e}")
        # Владельцу нужен статус ПО КАЖДОМУ контакту: кто, какой номер, какой
        # канал. Раньше в личку улетал только агрегат «принято 15», и по нему
        # невозможно было понять, кому именно ушло.
        try:
            from . import notify as _notify
            _notify.contact_sent(phone, name, channel, detail=message_id or "")
        except Exception as e:
            log.debug(f"tg-уведомление об отправке не отправилось: {e}")
        return message_pk
    except Exception as e:
        # DB write упал, но state уже сохранён — квота списана, дубля не будет.
        # Получатель УЖЕ получил сообщение (мы выше вызвали Wazzup), но в DB
        # записи нет. Это критическая ситуация — логируем как ERROR.
        log.error(
            f"DB WRITE FAILED после успешной отправки в {channel}! "
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
                    log.debug(f" bad_phones: {phone} × {channel} помечен как мёртвый")

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
                        f"{channel} fail → rescue (id={rid}, "
                        f"phone={phone}) — контакт уйдёт в откат"
                    )
                    # Владелец должен видеть отказ поимённо: «WABA не
                    # доставил этому контакту → ставлю в очередь отката».
                    # Раньше это приходило только в файл лога, а в личку уходил
                    # обезличенный код, из-за чего выглядело, что MAX «не
                    # отправился», хотя он просто ещё не наступил.
                    try:
                        from . import notify as _notify
                        _nxt = [c for c in _phase_order_channels()
                                if c != channel] or ["нет каналов"]
                        _notify.contact_failed(
                            phone, name, channel, detail or error_kind,
                            next_channels=" → ".join(_nxt),
                        )
                    except Exception as _ne:
                        log.debug(f"tg-уведомление об отказе не отправилось: {_ne}")
                except Exception as e:
                    log.warning(f"Не удалось поставить в rescue: {e}")
    except Exception as e:
        log.warning(f"Не удалось записать failed в БД: {e}")


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
            log.info(f"rescue: {ch} уже пробовали для этого контакта — пропускаю")
            continue
        if ch == "sms":
            # SMS — отдельный транспорт, не Wazzup; проверяем отдельно
            if not getattr(config, "SMSRU_ENABLED", 1):
                continue
            if not getattr(config, "SMSRU_API_ID", ""):
                continue
        else:
            if state.is_channel_halted(current_state, ch):
                log.info(f"rescue: {ch} halted — пропускаю")
                continue
            if not state.channel_has_quota_with_headroom(current_state, ch):
                log.info(f"rescue: {ch} исчерпал квоту — пропускаю")
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

    log.info(f"Rescue queue: {len(pending)} pending, обрабатываю до {max_items}")
    processed = 0

    # Сохраняем last_success_channel — rescue НЕ должен менять темп основного цикла
    saved_channel = current_state.get("last_success_channel", "")

    for r in pending:
        phone = r["phone"]
        name = r.get("name") or "—"
        category = r.get("category") or "—"
        log.info(
            f"Rescue [{r['id']}]: {phone} ({name}, {category}) "
            f"reason={r.get('reason', '?')}"
        )

        tried = db.rescue_tried_channels(r["id"])
        rescue_cascade = _rescue_cascade_for(
            r.get("original_channel") or "waba", current_state, tried
        )
        if not rescue_cascade:
            # Ничего не откатываем: важно НЕ тратить попытку (attempts),
            # иначе запись после 3 таких проходов станет 'failed' навсегда.
            #
            # Но оставлять её 'pending' нельзя: контакт, у которого все каналы
            # отката уже в tried_channels, иначе перебирается каждой фазой
            # бесконечно и просто занимает очередь. Решаем терминально так же,
            # как при отказе: исчерпан — стоп, иначе — в конец на завтра.
            log.info(
                f"Rescue [{r['id']}]: нет доступных каналов для отката "
                f"(исчерпаны/выключены/уже пробовали: {', '.join(tried) or '—'}) "
                f"— попытку не трачу"
            )
            _fallbacks = [c for c in _phase_order_channels() if c != "waba"]
            try:
                _why = db.should_exhaust(r["id"], _fallbacks)
            except Exception as _se:
                log.debug(f"should_exhaust недоступна: {_se}")
                _why = None
            if _why:
                db.mark_rescue_exhausted(r["id"], _why)
                log.info(
                    f"Rescue {r['id']} ({phone}) исчерпан ({_why}) — "
                    "стоп автоматики, в ежедневную выгрузку"
                )
            elif _fallbacks and set(_fallbacks).issubset(set(tried)):
                from datetime import timedelta as _td
                _tomorrow = (
                    datetime.now(timezone.utc) + _td(days=1)
                ).isoformat()
                db.mark_rescue_deferred(r["id"], _tomorrow)
                log.info(
                    f"Rescue {r['id']} ({phone}) — все каналы отката уже "
                    f"пробованы ({', '.join(tried)}), повтор после "
                    f"{_tomorrow[:10]}"
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
                    f"Rescue [{r['id']}]: откат до SMS — сегментов {n_seg}, "
                    f"~{n_seg * per_seg:.2f} ₽/сообщение "
                    f"(мессенджеры недоступны или исчерпаны)"
                )
            except Exception as e:
                log.info(
                    f"Rescue [{r['id']}]: откат до SMS "
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
                {}, # backoffs
                {}, # permanent_skipped
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
            log.info(f"Rescued {phone} via {_succ or 'personal'}")
            processed += 1
        else:
            db.mark_rescue_attempt(r["id"], error="all_personal_failed")
            log.warning(f"Rescue не удался для {phone} (attempts={r['attempts'] + 1})")

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
    from . import waba_templates # lazy import

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
                log.info(f"SKIP {phone} — отказ от рассылки (opt-out), не пишем")
            else:
                log.info(
                    f"SKIP {phone} — нет подтверждённого согласия "
                    f"(CONSENT_REQUIRED_FOR_COLD={config.CONSENT_REQUIRED_FOR_COLD})"
                )
            return False
    except Exception as e:
        # Неизвестный контакт в БД = согласие неизвестно → по флагу.
        # Ошибку БД не превращаем в запрет: логируем и решаем по флагу.
        log.debug(f"consent check failed for {phone}: {e}")
        if getattr(config, "CONSENT_REQUIRED_FOR_COLD", 0):
            log.info(f"SKIP {phone} — не удалось проверить согласие, требуется opt-in")
            return False

    # Инвариант защиты личных аккаунтов: не более одного сообщения на контакт
    # в одном канале за окно CONTACT_CHANNEL_COOLDOWN_HOURS (24ч).
    #
    # Зачем: после трёхзначной доставки молчание может означать и доставку
    # без события. Даже при 12-часовом горизонте рано или поздно появится
    # контакт, которому уже отправили — и второй канал продублирует ему
    # сообщение. Дубликаты в личных аккаунтах = бан. Проверяем ДО расхода
    # квоты, чтобы «нельзя» не съедало лимит.
    try:
        if db.recent_channel_send(phone, channels[0],
                                  hours=getattr(config,
                                                "CONTACT_CHANNEL_COOLDOWN_HOURS", 24)):
            log.info(
                f"SKIP {phone} — уже получал сообщение в канале "
                f"{channels[0]} за последние "
                f"{getattr(config, 'CONTACT_CHANNEL_COOLDOWN_HOURS', 24)}ч. "
                f"Дубль в личный аккаунт не отправляем."
            )
            return False
    except Exception as e:
        log.debug(f"channel-cooldown check failed for {phone}: {e}")

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
            f"SKIP {phone} ({name}, {category}) — все каналы в bad_phones cache"
        )
        return False

    # Phase 3.x: smart WABA gating — если для этой категории НЕТ WABA template,
    # не пытаемся WABA вообще (cold text без template = PERMANENT fail, и cascade
    # упал бы в TG/MAX, что даёт СЛИШКОМ МНОГО TG/MAX отправок → риск бана).
    # Вместо этого пробуем только Personal-каналы (если они есть).
    if "waba" in cascade and not template_id:
        cascade = [ch for ch in cascade if ch != "waba"]
        log.debug(
            f" WABA skip: нет template для категории {category!r}, "
            f"cascade → {cascade}"
        )

    # Правило «написали — навсегда» действует ПО КАНАЛУ (решение владельца
    # 2026-10-12). Человек может иметь только один мессенджер из трёх, поэтому
    # попытка в Telegram НЕ закрывает ему WhatsApp: закрывается ровно тот
    # канал, куда уже пытались. Повтор в тот же канал недопустим — жалоба в
    # личном канале означает перманентный бан номера.
    already = _attempted_channels_for(phone)
    if already:
        _closed = [ch for ch in cascade if ch in already]
        if _closed:
            log.info(
                f"SKIP каналов {phone} ({name}) — уже была попытка: {_closed}. "
                f"Остаются доступными: {[ch for ch in cascade if ch not in already]}"
            )
        cascade = [ch for ch in cascade if ch not in already]

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
            f"SKIP {phone} ({name}, {category}) — закрыты все каналы "
            f"(уже была попытка / bad_phones / cooldown / halted)"
        )
        return False

    log.info("─" * 70)
    log.info(
        f"[{phone}] {name} | категория: {category} | "
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
                f"{channel.upper()} вне safe hours, пропускаем"
            )
            continue
        # Проверяем квоту (с headroom) ПЕРЕД каждой отправкой
        if not state.channel_has_quota_with_headroom(current_state, channel):
            log.info(
                f"{channel.upper()} quota exhausted (headroom), skip"
            )
            continue
        # Phase 7: lazy retry — если WABA recently failed для этого phone,
        # сразу пробуем Personal channels (экономим WABA slot и время).
        if channel == "waba" and db.waba_recently_failed(phone, hours=24):
            recent = db.waba_recent_failures(phone, hours=24)
            log.warning(
                f"WABA недавно failed ({len(recent)} в 24ч) для {phone}, "
                f"skip WABA → Personal channels"
            )
            continue
        # Phase 7: pre-check через reachability cache (из webhook'ов).
        # Если кэш говорит WABA unreachable для этого phone → skip.
        if channel == "waba":
            cached = state.get_cached_reachability(current_state, phone)
            if cached and cached.get("waba") is False:
                log.info(
                    f"WABA reachability cache: unreachable для {phone}, skip"
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
                f"WABA [{ch_count_sent + 1}/{limit}] отправляем {phone} ({name})…"
            )
            try:
                from . import tglog as _tg
                _tg.send(
                    f"Отправка #{ch_count_sent + 1}/{limit} · канал WABA · Wazzup\n"
                    f"Кому: {phone} ({name})\n"
                    f"Категория: {category or '—'}",
                    "INFO",
                )
            except Exception:
                pass
            # Решаем: template или free text (24ч-окно).
            # Окно открывает ОТВЕТ КОНТАКТА, а не наша отправка.
            in_24h_window = state.is_waba_in_24h_window(current_state, phone)
            use_template = bool(template_id) and not in_24h_window
            log.info(
                f"WABA decision: template_id={'set' if template_id else 'NONE'}, "
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
                    f"SKIP WABA {phone} — уже получили маркетинг сегодня "
                    f"(защита от 131049), уходим на следующий канал"
                )
                continue
            if config.DRY_RUN:
                ok = True
                message_id = f"dryrun_{int(started_at.timestamp())}_waba"
                detail = "[DRY-RUN] имитация WABA отправки"
                http_status = 200
                log.info("WABA DRY-RUN")
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
                    f"WABA ПРИНЯТ за {elapsed:.1f}с | message_id={message_id} | "
                    f"{'template' if use_template else 'free-text'} | {detail[:120]}"
                )
                _persist_success(
                    phone, name, category, "waba", text, message_id, current_state,
                )
                from datetime import datetime, timezone
                state.set_waba_sent_meta(current_state, phone, datetime.now(timezone.utc).isoformat())
                current_state["_waba_consecutive_permanent"] = 0 # reset circuit breaker
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
                break # cascade: НЕ continue — broadcast был старым поведением
            else:
                kind = wazzup.classify_error(detail, http_status)
                log.warning(
                    f"WABA FAIL [{kind.value}] http={http_status} за {elapsed:.1f}с | "
                    f"{phone} ({name}) | {detail[:200]}"
                )
                if kind == ErrorKind.PERMANENT:
                    state.mark_unreachable(current_state, phone, "waba")
                    permanent_skipped.setdefault(phone, set()).add("waba")
                    _persist_failure(phone, name, category, "waba", text, detail, "permanent")
                    backoffs["waba"] = 0
                    log.info("WABA PERMANENT → unreachable, пробуем следующий канал")
                    # Phase 5.5 circuit breaker: если WABA PERMANENT 5 раз подряд,
                    # halt канал на сессию (template сломан → не валить всё в TG/MAX,
                    # это риск бана оригинальных аккаунтов).
                    consecutive_perm = int(current_state.get("_waba_consecutive_permanent", 0)) + 1
                    current_state["_waba_consecutive_permanent"] = consecutive_perm
                    if consecutive_perm >= 5:
                        log.error(
                            "WABA circuit breaker: %d PERMANENT fail подряд — "
                            "halt WABA на сессию, не уходим в TG/MAX",
                            consecutive_perm,
                        )
                        state.halt_channel(current_state, "waba")
                        state.save_state(current_state)
                    continue # следующий канал в cascade
                elif kind == ErrorKind.AUTH:
                    log.error(f"WABA AUTH — token невалиден! Backoff MAX")
                    backoffs["waba"] = _next_backoff(kind, backoffs.get("waba", 0))
                    state.halt_channel(current_state, "waba")
                    _persist_failure(phone, name, category, "waba", text, "auth: " + detail, "auth")
                    continue
                else:
                    # TRANSIENT / RATE_LIMIT / UNKNOWN — сбрасываем circuit breaker
                    current_state["_waba_consecutive_permanent"] = 0
                    backoffs["waba"] = _next_backoff(kind, backoffs.get("waba", 0))
                    _persist_failure(phone, name, category, "waba", text, detail, kind.value)
                    log.info(f"WABA [{kind.value}] — backoff={backoffs['waba']}с")
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
                f"SMS [{ch_attempt}/{sms_limit}] отправляем {phone} ({name}) "
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
                log.error(f"{detail}")
                continue

            ok, sms_id, detail, sms_code = sms_ru.send_sms(phone, sms_body)
            _append_log(phone, "sms", "success" if ok else "fail", detail)
            elapsed = (config.now_tz() - started_at).total_seconds()

            if ok:
                log.info(
                    f"SMS OK за {elapsed:.1f}с | sms_id={sms_id} | {detail[:120]}"
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
                break # один контакт = одно сообщение в ОДИН канал

            kind = sms_ru.classify_smsru_error(sms_code, detail)
            log.warning(
                f"SMS FAIL [{kind.value}] code={sms_code} за {elapsed:.1f}с | "
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
                    "SMS PERMANENT — номер помечен как недоступный для SMS"
                )
                continue
            if kind == ErrorKind.AUTH:
                # 200/204/221/302 — проблема АККАУНТА, не контакта. Повтор только
                # выжигает дневную квоту, поэтомуhalt канала целиком.
                log.error(
                    "SMS AUTH — %s. Проверьте BULK_SMSRU_API_ID, "
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
                        f"SMS AUTH fail: {detail[:300]} — SMS-канал halted на 24ч",
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
                    "SMS дневной лимит аккаунта исчерпан (206) — "
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
                        "SMS.RU: дневной лимит аккаунта исчерпан (206). "
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
                f"SMS [{kind.value}] — backoff={backoffs['sms']}с. "
                f"Канал последний, следующих нет."
            )
            continue

        # === Wazzup-only branch (TG/MAX/WA Personal) ===
        wazzup_channel_id = wazzup.channel_id_for(channel)
        if not wazzup_channel_id or not wazzup.is_configured():
            log.error(
                f"{channel.upper()} не сконфигурирован: WAZZUP_*_CHANNEL_ID "
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
            f"{channel.upper()} [{ch_attempt}/{limit}] "
            f"отправляем {phone} ({name}) через {transport_label}…"
        )
        # Прозрачность прогона: каждое отправленное сообщение уходит в Telegram
        # отдельной строкой с каналом и транспортом. Раньше в Telegram приходили
        # только агрегаты («принято 15»), а по ним невозможно понять, кому
        # именно ушло и по какой дороге.
        try:
            from . import tglog as _tg
            _tg.send(
                f"Отправка #{ch_attempt}/{limit} · канал {channel.upper()} · "
                f"{transport_label}\n"
                f"Кому: {phone} ({name})\n"
                f"Категория: {category or '—'}",
                "INFO",
            )
        except Exception:
            pass

        # DRY-RUN: пропускаем реальный вызов, только имитируем успех
        if config.DRY_RUN:
            ok = True
            message_id = f"dryrun_{int(started_at.timestamp())}_{channel}"
            detail = f"[DRY-RUN] имитация успешной отправки, {transport_label} API НЕ вызывался"
            http_status = 200
            log.info(
                f"{channel.upper()} DRY-RUN через {transport_label} — "
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
                f"{channel.upper()} OK за {elapsed:.1f}с | "
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
            break # Phase 2.2 cascade: НЕ continue — broadcast был старым поведением

        kind = wazzup.classify_error(detail, http_status)
        log.warning(
            f"{channel.upper()} FAIL [{kind.value}] http={http_status} "
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
                f"PERMANENT — {channel} помечен как невалидный для этого "
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
                f"AUTH — Wazzup API key/channel невалиден для {channel}! "
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
                    f"AUTH fail на канале {channel}: {detail[:300]} — "
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
            f"{channel.upper()} [{kind.value}] — backoff={backoffs[channel]}с. "
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
                f"↪ Контакт {phone} не доставлен в "
                f"{', '.join(failed_channels)}, но ещё открыты каналы: "
                f"{', '.join(sorted(_still_open))} — остаётся в rescue-очереди"
            )
        else:
            failed_contacts.mark_fully_failed(
                phone=phone, name=name, category=category,
                channels_failed=failed_channels,
            )
            log.info(
                f"Контакт {phone} ({name}) полностью мёртв — "
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
        f"Backoff {wait}с: " + ", ".join(
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
# Метка старта прогона — для честного «сколько заняло» в итоговом отчёте.
_RUN_STARTED_AT = 0.0
# Проба аккаунтов выполняется ОДИН раз за прогон: она тратит время (и при
# успехе — реальные сообщения), а вердикты не меняются в пределах суток.
_PROBE_DONE = False
# Разведка MAX идёт ОТДЕЛЬНОЙ фазой в конце круга, поэтому и флаг свой:
# за круг MAX проверяется один раз, независимо от числа фаз личных
# каналов (их две — разведка и добор слотов).
_MAX_PROBE_DONE = False


def _max_probe_done_for_run() -> bool:
    return _MAX_PROBE_DONE


def _mark_max_probe_done_for_run() -> None:
    global _MAX_PROBE_DONE
    _MAX_PROBE_DONE = True


def delivered_today_total() -> int:
    """Сколько сообщений РЕАЛЬНО доставлено сегодня по всем каналам.

    Источник — ``daily_counters`` в SQLite: там доставку помечает вебхук
    (delivery_state='delivered'), то есть это единственное число, отвечающее
    на вопрос «сколько человек получило», а не «сколько запросов принял
    транспорт». Именно эта разница объясняет, почему 250 принятых WABA
    дают ~100 живых доставок.
    """
    try:
        counters = db.get_counters()
        return sum(int(c.get("delivered") or 0) for c in counters.values())
    except Exception as e:
        log.debug(f"подсчёт доставок не удался: {e}")
        return 0


def delivery_target_reached() -> bool:
    """Достигнута ли цель по доставкам за сутки."""
    target = int(getattr(config, "TARGET_DELIVERED_PER_DAY", 0) or 0)
    if target <= 0:
        return False
    return delivered_today_total() >= target


def _contacts_messaged_this_run() -> int:
    """Сколько РАЗНЫХ контактов получили сообщение в этом прогоне.

    Считается по БД от метки старта прогона, а не по state.json: счётчики
    state живут read-modify-write и теряются, когда sender и вебхук пишут
    в один файл. COUNT(DISTINCT) — потому что один контакт в каскад может
    получить пробу и откат, но человек он один.
    """
    if not _RUN_STARTED_AT:
        return 0
    since = datetime.fromtimestamp(
        _RUN_STARTED_AT, tz=timezone.utc
    ).isoformat()
    try:
        with db.db_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(DISTINCT contact_id) FROM messages "
                "WHERE sent_at >= ? AND message_id IS NOT NULL "
                "  AND message_id NOT LIKE 'dryrun%'",
                (since,),
            ).fetchone()[0]
        return int(n or 0)
    except Exception as e:
        log.debug(f"подсчёт контактов прогона не удался: {e}")
        return 0


def contacts_budget_left() -> Optional[int]:
    """Сколько контактов ещё можно тронуть. None = потолка нет.

    Ровно ограничивает, а не «до ограничения»: бюджет режется по всему
    прогону (проба → WABA → добор личных слотов), и каждый следующий шаг
    получает остаток, а не исходную цифру.
    """
    limit = int(getattr(config, "RUN_CONTACTS_LIMIT", 0) or 0)
    if limit <= 0:
        return None
    return max(0, limit - _contacts_messaged_this_run())


def _probe_channels() -> set:
    """Каналы, для которых имеет смысл фаза PROBE."""
    if not getattr(config, "PROBE_ENABLED", True):
        return set()
    return set(getattr(config, "PROBE_CHANNELS", ["telegram", "max"]) or ())


def _probe_done_for_run() -> bool:
    return _PROBE_DONE


def _mark_probe_done_for_run() -> None:
    global _PROBE_DONE
    _PROBE_DONE = True


def _probe_ran_with_verdicts(channel: str) -> bool:
    """Есть ли по этому каналу вердикты, на которые можно опереться.

    True только если в contact_channel_state есть хотя бы одна запись по
    каналу. Пустая таблица означает «пробы не было» — и тогда откат обязан
    работать как раньше, а не схлопнуться в ноль отправок.
    """
    try:
        rows = db._query_rows(
            "SELECT COUNT(*) n FROM contact_channel_state WHERE channel = ?",
            (channel,),
        )
        return bool(rows and rows[0]["n"])
    except Exception:
        return False


def _filter_by_presence(pending: list[dict], channel: str) -> list[dict]:
    """Оставляет только контакты с verdict='present' по этому каналу.

    Контакт без вердикта (unknown) отбрасывается: неизвестно, есть ли аккаунт,
    а отправка вслепую — это и есть та трата личного лимита, ради которой
    затевалась проба.
    """
    kept, dropped_phones = [], []
    with db.db_conn() as conn:
        cur = conn.cursor()
        for row in pending:
            phone = row.get("phone")
            if not phone:
                continue
            cur.execute(
                """
                SELECT s.verdict FROM contact_channel_state s
                JOIN contacts ct ON ct.id = s.contact_id
                WHERE ct.phone = ? AND s.channel = ?
                """,
                (phone, channel),
            )
            found = cur.fetchone()
            if found and found["verdict"] == "present":
                kept.append(row)
            else:
                dropped_phones.append(phone)
    if dropped_phones:
        log.info(
            f"Фильтр наличия аккаунта ({channel}): "
            f"пропущено {len(dropped_phones)} контактов без подтверждённого аккаунта"
        )
        # Каждый пропущенный номер уходит в Telegram отдельной строкой: иначе
        # «пропущено 9 контактов» не отвечает на вопрос «кого именно мы не
        # смогли тронуть и на каком канале». Именно этого не хватало, чтобы
        # понимать, куда вообще можно достучаться.
        try:
            from . import tglog as _tg
            _head = f"Нет канала {channel} у {len(dropped_phones)} контактов — отправка невозможна:\n"
            _body = "\n".join(f"  {_p}" for _p in dropped_phones[:15])
            if len(dropped_phones) > 15:
                _body += f"\n  … и ещё {len(dropped_phones) - 15}"
            _tg.send(_head + _body, "WARNING")
        except Exception:
            pass
    return kept


def select_probe_candidates(
    limit: Optional[int] = None,
    channel: Optional[str] = None,
    skip_first: Optional[int] = None,
) -> list[dict]:
    """Кандидаты на ПРОБУ по конкретному каналу.

    Живой случай 2026-10-10 (повторы): кандидаты брались из ``rescue_queue``,
    то есть из людей, которым WABA УЖЕ ушёл. Проба при успехе сама
    отправляет сообщение, поэтому семь человек получили по второму
    сообщению в личный канал — двое уже прочитали первое.

    Живой случай 2026-10-10 (бан MAX): MAX считает ПРОВЕРКУ НОМЕРА
    отдельной активностью со своим потолком (1–10/день, потолок 20/день,
    массовые запросы → бан). Поэтому правила отбора у каналов РАЗНЫЕ:

    * ``telegram`` — любой неотправленный контакт без свежего вердикта;
      отдельного лимита на проверку номера нет.
    * ``max`` — только те, у кого НЕТ ни Telegram, ни WhatsApp. Платить
      риском бана аккаунта владельца за 399 ответов «номера нет» ради
      одного «есть» невыгодно: достижимых в MAX ~20 контактов из 4448.

    Общее для обоих: контакт ещё ни разу не получал сообщение, номер
    валиден, согласие не отклонено.
    """
    limit = int(limit or getattr(config, "PROBE_MAX_PER_RUN", 400) or 400)
    ttl_hours = int(getattr(config, "PROBE_TTL_HOURS", 24) or 24)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
    ).isoformat()

    # Доп. условие для MAX: нет подтверждённого мессенджера и нет WhatsApp.
    max_extra = ""
    if channel == "max":
        max_extra = """
          AND NOT EXISTS (
                SELECT 1 FROM contact_channel_state s
                WHERE s.contact_id = c.id
                  AND s.channel IN ('telegram','waba','whatsapp')
                  AND s.verdict IN ('present','delivered')
          )
        """

    sql = f"""
        SELECT c.id AS contact_id, c.phone, c.name, c.category
        FROM contacts c
        WHERE COALESCE(c.is_test, 0) = 0
          AND COALESCE(c.is_invalid, 0) = 0
          AND COALESCE(c.consent, 'unknown') != 'denied'
          AND NOT EXISTS (
                SELECT 1 FROM messages m
                WHERE m.contact_id = c.id
                  AND m.status IN ('sent','delivered','read','answered')
          )
          -- Живой случай 2026-10-11: проба отправила ВТОРОЕ сообщение
          -- контакту, у которого было 5 попыток, все со status='failed'
          -- и delivery='rejected'. Фильтр по status их пропускал, потому
          -- что «успешного» статуса у них не было НИКОГДА.
          -- Транспорт при этом сообщение принимал (message_id был).
          -- Требование владельца: исключать всех, кому что-то уходило
          -- через ЛЮБОЙ канал, независимо от того, дошло оно или нет.
          -- Нельзя назвать человека «не получали» только потому, что у
          -- него в таблице нет зелёной галочки.
          AND NOT EXISTS (
                SELECT 1 FROM messages m
                WHERE m.contact_id = c.id
                  AND m.message_id IS NOT NULL
                  AND m.message_id NOT LIKE 'dryrun%'
          )
          AND NOT EXISTS (
                SELECT 1 FROM contact_channel_state s
                WHERE s.contact_id = c.id
                  AND s.channel = ?
                  AND s.verdict IN ('present','delivered','absent','blocked')
                  AND COALESCE(s.last_probe_at, s.updated_at, '') >= ?
          )
          {max_extra}
        ORDER BY c.id
        LIMIT ? OFFSET ?
    """
    # Пропуск первых N — страховка владельца перед боевым прогоном по свежей
    # базе (2026-10-11). Смещение НЕ замена фильтру: оно лишь сдвигает окно,
    # чтобы тест не лёг в самый верх списка, где исторически скапливались
    # самые старые и самые «обстрелянные» номера.
    _skip = int(skip_first if skip_first is not None
                else getattr(config, "PROBE_SKIP_FIRST", 0) or 0)
    params: tuple = (channel or "telegram", cutoff, limit, max(0, _skip))
    try:
        with db.db_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        log.warning(f"PROBE: не удалось отобрать кандидатов: {e}")
        return []

    return [
        {
            "contact_id": r["contact_id"],
            "phone": r["phone"],
            "name": r["name"] or "",
            "category": r["category"] or "",
        }
        for r in rows
        if r["phone"]
    ]


def _run_probe_phase(
    stop_requested: dict, phase_stats: dict, channel: str = "telegram",
) -> None:
    """Проверяет наличие аккаунта в ОДНОМ канале у свежих контактов.

    Кандидаты — из неотправленного пула (см. select_probe_candidates), а НЕ из
    rescue_queue: там лежат те, кому WABA уже ушёл, и проба при успехе
    отправила бы им ВТОРОЕ сообщение.

    Проба и есть отправка: при вердикте 'present' сообщение уходит человеку.
    При 'absent' квота сообщений не списывается — но у MAX это НЕ значит
    «бесплатно»: проверка номера там считается отдельной активностью, и
    потолок держится через state.probe_checks_today.
    """
    from . import probe as _probe

    # Дневной потолок проверок этого канала. Для Telegram его нет (None),
    # для MAX — жёсткие 10/день.
    _cur = state.load_state()
    _budget = state.probe_budget_left(_cur, channel)
    if _budget == 0:
        log.info(
            f"PROBE {channel}: дневной потолок проверок исчерпан "
            f"({state.probe_checks_today(_cur, channel)} сегодня) — "
            "фаза пропущена"
        )
        return
    _budget = _budget if _budget is not None else (
        int(getattr(config, "PROBE_MAX_PER_RUN", 400) or 400)
    )

    # Бюджет контактов на прогон: 15 контактов означает ровно 15.
    _cbudget = contacts_budget_left()
    if _cbudget == 0:
        log.info(f"PROBE {channel}: бюджет контактов исчерпан — пропускаю")
        return
    _take = _budget
    if _cbudget is not None:
        _take = min(_take, _cbudget)

    candidates = select_probe_candidates(limit=_take, channel=channel)
    if not candidates:
        log.info(f"PROBE {channel}: нет кандидатов для проверки")
        return

    stats = _probe.run_phase(candidates, log_fn=log,
                             stop_requested=stop_requested)
    phase_stats["probe"] = stats.get("probed", 0)
    log.info(
        f"PROBE итог: проверено {stats['probed']}, "
        f"аккаунт есть {stats['present']}, нет {stats['absent']}, "
        f"неясно {stats['unknown']} — в откат пойдут только 'есть'"
    )


def _personal_slots_in_window() -> int:
    """Сколько личных отправок РЕАЛЬНО влезает в окно при паузе 15+ мин.

    ⚠️ Не путать с ``_personal_slot_count()``: та отдаёт сумму лимитов
    каналов (TG 50 + MAX 10 = 60), а отправить 60 за 10 часов при паузе
    15 минут физически нельзя — нужно 15 часов. В плане прогона должно
    стоять фактическое число, иначе отчёт покажет «было 60 → стало 30»
    и владелец будет искать пропавших 30, которых никто не обещал.

    Локальный импорт `schedule` обязателен: на уровне модуля его нет
    (он импортируется внутри фазы личных каналов). Без этого NameError
    проглатывался except'ом и возвращал 60 — правдоподобное число,
    из-за которого ошибка не видна в логе вообще.
    """
    from . import schedule as _sched

    try:
        # Окно закрыто → в этом запуске личных отправок НЕ будет.
        # ⚠️ Без этой проверки план врал бы: _phase_seconds_left при закрытом
        # окне переносит дедлайн на ЗАВТРА (end_dt += 1 день) и отдаёт ~23
        # часа. Планировщик построил бы 60 слотов на ночь вперёд, а
        # сравнение «план → факт» показало бы «было 60 → стало 0» и
        # заставило бы искать несуществующую потерю.
        if not (_within_safe_hours("telegram") and _within_safe_hours("max")):
            return 0
        _sec = min(_phase_seconds_left("max"), _phase_seconds_left("telegram"))
        if _sec <= 0:
            return 0
        _gap = int(getattr(config, "PERSONAL_MIN_GAP_SEC", 0) or 0)
        if _gap <= 0:
            return _personal_slot_count()
        return len(_sched.build_slot_schedule(
            _personal_slot_count(), _sec,
            min_gap_seconds=_gap,
            jitter_max=float(getattr(config, "PERSONAL_JITTER_MAX", 1.0) or 1.0),
        ))
    except Exception as e:  # noqa: BLE001 — план не должен ронять старт
        # ⚠️ Возвращаем 0, а не _personal_slot_count(): правдоподобное
        # «план 60» выглядит как правда и прячет ошибку от владельца.
        # Лучше честное «не посчитали», чем уверенное враньё в отчёте.
        log.warning(f"слоты личных каналов НЕ посчитаны ({type(e).__name__}: "
                    f"{e}) — в плане будет 0, это не значит «отправок не будет»")
        return 0


def _build_run_plan(cur: dict, waba_planned: int) -> dict:
    """Что этот запуск СОБИРАЕТСЯ сделать. Уходит в дайджест на старте и
    потом в отчёт как «план», с которым сравнивается факт.

    Требование владельца 2026-10-11: «в начале уточнить, каким контактам
    отправляет этот запуск». Без этого отчёт в финале показывает только
    факты, а по фактам не видно, что именно прогон собирался делать и
    сколько контактов по дороге потерялось.

    Каждое число здесь — ЧТО-ТО ДЕЛАЕТ, а не украшение: пул и уже
    отправленные объясняют, почему в работе столько контактов; слоты
    личных каналов — результат арифметики окна и паузы, а не «кап канала».
    """
    plan: dict = {
        "waba_planned": waba_planned,
        "personal_planned": _personal_slots_in_window(),
        "personal_window_h": (
            int(getattr(config, "PERSONAL_SAFE_HOURS_END", 0))
            - int(getattr(config, "PERSONAL_SAFE_HOURS_START", 0))
        ),
        "min_gap_min": int(getattr(config, "PERSONAL_MIN_GAP_SEC", 0) or 0) // 60,
        "max_gap_min": int(
            (int(getattr(config, "PERSONAL_MIN_GAP_SEC", 0) or 0)
             * float(getattr(config, "PERSONAL_JITTER_MAX", 1.0) or 1.0)) // 60
        ),
        "rescue_planned": db.count_pending_rescues(),
    }
    try:
        cbudget = contacts_budget_left()
        if cbudget is not None:
            plan["contacts_budget"] = (
                int(getattr(config, "RUN_CONTACTS_LIMIT", 0) or 0)
            )
        _sec = min(_phase_seconds_left("max"), _phase_seconds_left("telegram"))
        if _sec > 0:
            plan["probe_planned"] = int(_sec // max(
                1, int(getattr(config, "PROBE_INTERVAL_MIN", 0) or 0) * 60))
    except Exception:  # noqa: BLE001 — план не должен ронять старт прогона
        pass
    try:
        plan["pool_total"] = db.count_pool_contacts()
        plan["already_sent"] = len(db.get_sent_phones())
    except Exception as e:  # noqa: BLE001
        log.debug(f"план прогона посчитан частично: {e}")
    return plan


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

    # Личные каналы: env НЕ имеет права опустить паузу ниже решения владельца
    # «меньше 15 минут между отправленными сообщениями не бывает»
    # (2026-10-10). Раньше PERSONAL_DELAY_MIN=300 (5 мин) позволял каскаду
    # отката слать в личный канал с паузой вчетверо меньше заявленной.
    # Логика та же, что у _safe_limit по дневным капам: env урезается,
    # но не расширяет защиту.
    _gap = int(getattr(config, "PERSONAL_MIN_GAP_SEC", 0) or 0)
    _jit = float(getattr(config, "PERSONAL_JITTER_MAX", 1.0) or 1.0)
    _lo = max(int(config.PERSONAL_DELAY_MIN), _gap)
    _hi = max(int(config.PERSONAL_DELAY_MAX), _lo)
    if _gap > 0:
        # Потолок = минимум × разброс: пауза остаётся живой (15–21 мин),
        # а не превращается в ровные 15 минут подряд — это сигнал бота.
        _hi = max(_hi, int(_gap * max(1.0, _jit)))
    return (_lo, _hi)


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
    except Exception as e: # pragma: no cover - defensive
        log.warning(f"не удалось прочитать rescue_queue для отбора свежих: {e}")

    fresh: list[dict] = []
    skipped_queued = skipped_exhausted = skipped_cooldown = skipped_bad = 0
    skipped_no_template = 0
    skipped_personal = 0
    skipped_waste = 0
    # Контакты, которым личный канал подходит лучше, резервируем под фазы
    # MAX/Telegram (решение владельца 2026-10-09). Приоритет на контакт:
    # есть оба аккаунта → MAX, только Telegram → Telegram. Отдавать такому
    # человеку шаблон Meta вместо личного канала — значит потратить один из
    # 20/50 дефицитных слотов личного аккаунта на отправку туда, где
    # вероятность доставки ниже.
    reserved_phones: set[str] = set()
    # Контакты, у которых УЖЕ доказано отсутствие WhatsApp (вердикт absent
    # по waba/whatsapp). Попытка WABA по ним не может быть доставлена в
    # принципе — это чистый расход дневной квоты в 250 попыток. На 09.10
    # таких было 118, и каждая из них гарантированно съедала попытку.
    # Повторная попытка по известно-негодному номеру — то же самое, что
    # повторная отправка человеку, которому уже отказали.
    waste_phones: set[str] = set()
    try:
        for r in db._query_rows(
            "SELECT DISTINCT ct.phone AS phone, s.channel AS channel, "
            "       s.verdict AS verdict "
            "FROM contact_channel_state s "
            "JOIN contacts ct ON ct.id = s.contact_id "
            "WHERE (s.channel IN ('max', 'telegram') AND s.verdict = 'present') "
            "   OR (s.channel IN ('waba', 'whatsapp') AND s.verdict = 'absent')"
        ):
            if not r["phone"]:
                continue
            if r["channel"] in ("max", "telegram"):
                reserved_phones.add(r["phone"])
            else:
                waste_phones.add(r["phone"])
    except Exception as e:  # pragma: no cover - defensive
        log.warning(f"резерв личных каналов не прочитан: {e}")

    # Индекс вердиктов для отсева «нет ни одного мессенджера» при разведке.
    log.info(
        f"Резерв WABA: под личные каналы {len(reserved_phones)}, "
        f"без WhatsApp {len(waste_phones)} (повторная попытка бессмысленна)"
    )

    # Категории с РАБОЧИМ шаблоном Meta. Считаем один раз, а не на контакт:
    # pick_waba_template() ходит в БД, а контактов тысячи.
    # Шаблон может быть невалидным не только «не настроен», но и ОГРАНИЧЕН
    # Meta (BULK_WABA_RESTRICTED_TEMPLATES) — тогда pick вернёт None.
    template_ready: set[str] = set()
    from . import waba_templates  # lazy import, как в _run_one_contact
    try:
        for cat in config.ALLOWED_CATEGORIES:
            if waba_templates.is_valid_template_id(
                waba_templates.pick_waba_template(cat)
            ):
                template_ready.add(cat)
    except Exception as e:
        # НЕ глотаем. Раньше здесь стоял except, который превращал в том числе
        # NameError в пустой template_ready — пул WABA молча схлопывался в
        # ноль, и прогон выглядел «нормальным», просто без WABA. Ошибка
        # отбора пула обязана валить прогон, а не тихо уменьшать цель.
        log.error(
            f"НЕ УДАЛОСЬ прочитать шаблоны WABA: {type(e).__name__}: {e}. "
            f"Прогон остановлен: без этого отбора пул WABA был бы неверным."
        )
        raise
    blocked_cats = sorted(config.ALLOWED_CATEGORIES - template_ready)
    if blocked_cats:
        log.info(
            f"Категории без рабочего шаблона Meta исключены из пула WABA: "
            f"{blocked_cats}. Остальные категории идут в WABA как обычно."
        )
    if not template_ready:
        # Ни одного рабочего шаблона — пул WABA будет пустым. Это не тихий
        # ноль: preflight выше уже должен был выключить канал. Если канал
        # всё же включился, значит список категорий пуст (тестовый прогон)
        # и судить не о чем.
        log.warning(
            "Ни один шаблон Meta не читается — пул WABA будет пустым. Если это "
            "боевой прогон, проверь WABA_TEMPLATE_*_ID в .env.local "
            "(py main.py preflight)."
        )

    for c in contacts:
        phone = c.get("phone")
        if not phone or phone in queued:
            skipped_queued += 1
            continue
        if phone in reserved_phones:
            skipped_personal += 1
            continue
        if phone in waste_phones:
            # Доказанный absent по WhatsApp: доставка невозможна, квота
            # потратится впустую. Такие контакты живут дальше — они попадут
            # в pending_sms, когда владелец решит отправлять по SMS.
            skipped_waste += 1
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
        # Нет одобренного шаблона Meta -> отправка вернёт BUILD_TEMPLATE_ERROR
        # и зря съест попытку из 250. Проверяем и «нет такого шаблона», и
        # «шаблон есть, но Meta его ограничила» — обе причины отсекают
        # контакт одинаково.
        if templates.normalize_category(
                c.get("category", "")) not in template_ready:
            skipped_no_template += 1
            continue
        fresh.append(c)

    log.info("─" * 70)
    # Порядок отбора: сначала тем, кому мы НИКОГДА не отправляли, потом тем, кому
    # писали давно. Раньше шёл порядок Excel, и прогон мог целиком уйти в тех,
    # кому уже писали, а свежие контакты так и не получали сообщение.
    try:
        _last_seen = db.last_sent_by_phone()
    except Exception as e:
        log.warning(f"история отправок недоступна, порядок как в Excel: {e}")
        _last_seen = {}

    def _never_sent_first(c: dict):
        ts = _last_seen.get(c.get("phone"))
        # (0, пусто) — никогда не отправляли; (1, метка) — отправляли.
        return (1, ts) if ts else (0, "")

    fresh.sort(key=_never_sent_first)
    _never = sum(1 for c in fresh if not _last_seen.get(c.get("phone")))
    if _never or _last_seen:
        log.info(
            f"Приоритет отбора: {_never} из {len(fresh)} — тем, кому ещё "
            f"никогда не отправляли; остальным — у кого отправка была раньше"
        )
    log.info(f"Отбор свежих для WABA: {len(fresh)} из {len(contacts)}")
    log.info(
        f"исключено: в очереди {skipped_queued}, "
        f"резерв под личные каналы {skipped_personal}, "
        f"WhatsApp доказанно нет {skipped_waste}, "
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
            f"Фаза {channel.upper()}: в окне осталось {left/60:.0f} мин, "
            f"а нужно минимум {need/60:.0f} мин на {n_messages} сообщений "
            f"→ откладываю на завтра (очередь не теряется)"
        )
        return False
    est = n_messages * ((lo + _hi) / 2.0)
    log.info(
        f"Фаза {channel.upper()}: {n_messages} сообщений, "
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


def _owner_channels(channels) -> list[str]:
    """Каналы в порядке КАСКАДА и без выключенных транспортов.

    `wazzup.active_channels()` возвращает набор каналов без порядка, поэтому в
    личку уходило «Каналы: telegram, max, waba» — порядок отката по такой
    строке восстановить нельзя. WhatsApp Personal и SMS в этой рассылке не
    используются, но попадали в «Лимиты:» общим словарём и выглядели как
    рабочие каналы.
    """
    order = _phase_order_channels()
    hidden = set(getattr(config, "TG_HIDE_CHANNELS", ()) or ())
    present = [c for c in channels if c not in hidden]
    ordered = [c for c in order if c in present]
    ordered += [c for c in present if c not in ordered]
    return ordered


def report_delivery_by_contact(channel: str, to_tg: bool = True) -> dict:
    """Кому доставилось, кому нет — поимённо, после фазы.

    Владелец смотрит интерфейс Wazzup и видит там по каждому контакту红 «не
    доставлено», а в логах был только агрегат. Отчёт отвечает на два вопроса
    сразу: кому дошло, и что будет с теми, кому не дошло.

    Честность важнее удобства: без вебхука доставка НЕИЗВЕСТНА, и такая строка
    помечается «не подтверждено», а не «не доставлено». Разница принципиальна:
    «не доставлено» — это отказ и решение об откате, «не подтверждено» —
    отсутствие данных.
    """
    stats = {"delivered": [], "rejected": [], "unknown": [], "failed": []}
    try:
        with db.db_conn() as conn:
            sql = (
                """
                SELECT ct.name, ct.phone, m.status, m.delivery_state,
                       m.delivered_at
                FROM messages m JOIN contacts ct ON ct.id = m.contact_id
                WHERE m.channel = ? AND date(m.sent_at) = date('now')
                  AND m.message_id IS NOT NULL
                ORDER BY m.id DESC LIMIT 500
                """
            )
            rows = conn.execute(sql, [channel]).fetchall()
    except Exception as e:
        log.warning(f"отчёт по контактам не собрался: {e}")
        return stats

    seen = set()
    for r in rows:
        phone = r["phone"]
        if phone in seen:
            continue
        seen.add(phone)
        if r["delivered_at"] or str(r["status"] or "").lower() == "delivered" \
                or str(r["delivery_state"] or "") == "delivered":
            stats["delivered"].append((r["name"], phone))
        elif str(r["status"] or "").lower() == "failed" \
                or str(r["delivery_state"] or "") == "rejected":
            stats["rejected"].append((r["name"], phone))
        else:
            stats["unknown"].append((r["name"], phone))

    n_del = len(stats["delivered"])
    n_rej = len(stats["rejected"])
    n_unk = len(stats["unknown"])
    horizon = int(getattr(config, "UNKNOWN_GRACE_MINUTES", 720))
    fallbacks = [c for c in _phase_order_channels() if c != channel]

    log.info("─" * 70)
    log.info(f"ДОСТАВКА ПО КОНТАКТАМ · {channel.upper()}")
    for _name, _phone in stats["delivered"]:
        log.info(f"{_phone} {_name or ''} — доставлено")
    for _name, _phone in stats["rejected"]:
        log.info(f"{_phone} {_name or ''} — отказ канала → откат")
    if n_unk:
        log.info(
            f"{n_unk} контактов — доставка НЕ ПОДТВЕРЖДЕНА "
            f"(вебхук молчит). Это не отказ: через {horizon} мин молчание "
            f"уйдёт в откат {' → '.join(fallbacks) or 'нет каналов'}"
        )
    log.info("─" * 70)

    if to_tg:
        try:
            from . import notify as _notify
            lines = [
                f" Доставка по контактам · {channel.upper()}",
                f" доставлено: {n_del}",
                f" отказ канала (в откат): {n_rej}",
                f" не подтверждено (не отказ!): {n_unk} → откат через "
                f"{horizon} мин через {' → '.join(fallbacks) or 'нет каналов'}",
            ]
            for _n, _p in stats["delivered"][:20]:
                lines.append(f" {_p} {_n or ''}")
            for _n, _p in stats["rejected"][:20]:
                lines.append(f" {_p} {_n or ''} — уходит в откат")
            _notify.event("\n".join(lines), "INFO")
        except Exception as e:
            log.debug(f"tg-отчёт по контактам не отправился: {e}")
    return stats


def delivery_vs_attempts(day: Optional[str] = None) -> dict:
    """ГЛАВНАЯ метрика дня: сколько ДОСТАВЛЕНО, а не сколько принято.

    Почему именно так. 09.10 WABA принял 86 сообщений и доставил 46 — 53%.
    Число «принято» почти всегда радует, потому что HTTP 201 означает лишь
    «запрос принят», а настоящий вердикт приходит эхом вебхука через секунды
    и может оказаться отказом. Считать попытки — значит хвалить себя за
    мусор: 35 из 86 ушли в отказ, и каждый занял часть квоты в 250.

    Считаем по messages: accepted = строки с message_id, delivered = из них
    с подтверждённой доставкой (delivered_at либо статус read/answered).
    """
    out: dict[str, dict] = {}
    try:
        with db.db_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT channel,
                       COUNT(*) AS accepted,
                       SUM(CASE WHEN delivered_at IS NOT NULL
                                 OR status IN ('delivered','read','answered')
                                THEN 1 ELSE 0 END) AS delivered,
                       SUM(CASE WHEN status = 'failed'
                                  OR delivery_state = 'rejected'
                                THEN 1 ELSE 0 END) AS rejected
                FROM messages
                WHERE substr(COALESCE(sent_at, created_at),1,10) = ?
                  AND message_id IS NOT NULL
                GROUP BY channel
                """,
                (day or config.now_tz().strftime("%Y-%m-%d"),),
            ).fetchall()
    except Exception as e:
        log.warning(f"метрика доставки не собрана: {e}")
        return out

    for r in rows:
        acc = int(r["accepted"] or 0)
        dl = int(r["delivered"] or 0)
        out[r["channel"]] = {
            "accepted": acc,
            "delivered": dl,
            "rejected": int(r["rejected"] or 0),
            "rate": (dl / acc * 100) if acc else 0.0,
        }
    return out


def _report_delivery_ratio() -> None:
    """Печатает и отправляет в Telegram доставку против попыток."""
    stats = delivery_vs_attempts()
    if not stats:
        return
    total_acc = sum(s["accepted"] for s in stats.values())
    total_dl = sum(s["delivered"] for s in stats.values())
    log.info("─" * 70)
    log.info("ДОСТАВЛЕНО (а не принято!) — главная метрика дня:")
    for ch in ("waba", "telegram", "max", "sms"):
        s = stats.get(ch)
        if not s:
            continue
        log.info(
            f"   {ch:<10} доставлено {s['delivered']:>4} из {s['accepted']:>4} "
            f"принятых ({s['rate']:.0f}%), отказов {s['rejected']}"
        )
    log.info(
        f"   ИТОГО      доставлено {total_dl} из {total_acc} принятых "
        f"({(total_dl / total_acc * 100) if total_acc else 0:.0f}%)"
    )
    log.info("─" * 70)
    try:
        from . import notify as _notify
        lines = [" ДОСТАВЛЕНО сегодня (не принято!):"]
        for ch in ("waba", "telegram", "max", "sms"):
            s = stats.get(ch)
            if s:
                lines.append(
                    f"  {ch:<10} {s['delivered']:>4} / {s['accepted']:>4} "
                    f"({s['rate']:.0f}%), отказов {s['rejected']}"
                )
        lines.append(f"  ИТОГО      {total_dl} / {total_acc}")
        _notify.event("\n".join(lines), "INFO")
    except Exception as e:
        log.debug(f"tg-метрика доставки не ушла: {e}")


def _report_all_channels_delivery() -> None:
    for _ch in _phase_order_channels():
        try:
            report_delivery_by_contact(_ch)
        except Exception:
            pass
    try:
        _report_delivery_ratio()
    except Exception:
        pass


def _limits_for_owner(channels) -> str:
    """Лимиты только по используемым каналам, в порядке каскада."""
    parts = []
    for ch in _owner_channels(channels):
        cap = (getattr(config, "CHANNEL_DAILY_LIMITS", {}) or {}).get(ch, "?")
        parts.append(f"{ch} {cap}")
    return ", ".join(parts) if parts else "—"


# Результат проверки кэшируется на время процесса: она ходит в Wazzup
# (GET /v3/webhooks) и слушает порт, а зовётся дважды за прогон — из сводки
# запуска и из стоп-кассы. Второй сетевой вызов только добавляет задержку.
_DELIVERY_SIGNAL_CACHE: Optional[tuple] = None


def delivery_signal_available(refresh: bool = False) -> tuple[bool, str]:
    """Есть ли в принципе путь, по которому придёт подтверждение доставки.

    Живой случай 2026-10-07, из-за которого весь каскад выглядел сломанным:
    sender запускали ЛОКАЛЬНО на Windows, а вебхук крутился на сервере
    217.149.23.102 и писал статусы в ДРУГУЮ базу (`/opt/bulkmessage/data/crm.db`).
    Wazzup отдавал 30 принятых сообщений, ни одного события статуса в локальную
    базу не приходило — и витрина честно показывала «доставка не подтверждена».
    Но владелец видел в интерфейсе Wazzup красные отказы Meta и справедливо
    спрашивал: «почему хук не ловит ошибки и не перенаправляет на другой канал».

    Ответ: ловить было нечего. Хук исправен, он просто смотрит в другую БД.
    Раз два процесса не делят одну базу, подтверждений не будет НИКОГДА, и
    молчание через 12 ч уведёт все принятые сообщения в откат — то есть в
    личные аккаунты. Поэтому проверка обязана быть ДО отправки и громкой.

    Возвращает (доступен ли сигнал, объяснение для владельца).
    """
    global _DELIVERY_SIGNAL_CACHE
    if _DELIVERY_SIGNAL_CACHE is not None and not refresh:
        return _DELIVERY_SIGNAL_CACHE
    _r = _delivery_signal_available_uncached()
    _DELIVERY_SIGNAL_CACHE = _r
    return _r


def _delivery_signal_available_uncached() -> tuple[bool, str]:
    if config.DRY_RUN:
        return True, "DRY-RUN — реальных отправок нет, подтверждения не нужны"

    # 1. Локальный вебхук: если поднят здесь же — события попадут в эту базу.
    # НО «слушает порт» ≠ «события приходят сюда»: Wazzup может быть
    # подписан на туннель СЕРВЕРА, пока локальный вебхук просто включён.
    # Поэтому п.1 проверяем вместе с п.2 — сверяем, куда Wazzup шлёт СЕЙЧАС.
    port = int(getattr(config, "WEBHOOK_PORT", 8000) or 8000)
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            local_listening = s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        local_listening = False

    url = (getattr(config, "WAZZUP_WEBHOOK_URL", "") or "").strip()

    # 2. Куда Wazzup реально шлёт события. Это единственный честный источник
    # истины: конфиг может быть пустым, а подписка — жить на другой машине.
    registered, reg_err = wazzup.get_registered_webhook_url()
    if registered and local_listening:
        if not url:
            return False, (
                f"локальный вебхук слушает 127.0.0.1:{port}, но Wazzup шлёт "
                f"события на {registered} — это ДРУГАЯ машина и другая база. "
                f"Укажи WAZZUP_WEBHOOK_URL={registered} и подними вебхук "
                f"здесь, либо верни боевой прогон туда."
            )
        if url.rstrip("/") != registered.rstrip("/"):
            return False, (
                f"Wazzup подписан на {registered}, а в конфиге WAZZUP_WEBHOOK_URL="
                f"{url} — статусы придут туда, а не в нашу базу."
            )
        return True, (
            f"вебхук слушает 127.0.0.1:{port}, Wazzup подписан на {registered} "
            f"— статусы придут в эту базу"
        )
    if registered and not local_listening:
        return False, (
            f"локальный вебхук не поднят (127.0.0.1:{port} молчит), Wazzup "
            f"подписан на {registered} — статусы уйдут туда, в нашу базу не "
            f"придут"
        )
    if local_listening and not registered:
        return True, (
            f"вебхук слушает 127.0.0.1:{port}; подписка Wazzup не читается "
            f"({reg_err}) — подтверждения возможны только если ты подписан на "
            f"этот же endpoint"
        )
    return False, (
        f"локальный вебхук не поднят (127.0.0.1:{port} молчит), "
        f"WAZZUP_WEBHOOK_URL={url or 'пусто'}, а читать подписку Wazzup не "
        f"удалось ({reg_err}) — подтверждений доставки не будет"
    )


def _guard_delivery_signal(channels) -> None:
    """Боевой прогон без пути подтверждений — отказ, а не предупреждение.

    Молчание в этой системе опаснее отказа: оно через UNKNOWN_GRACE_MINUTES
    уводит ПРИНЯТЫЕ сообщения в откат, а откат — это личные аккаунты
    (Telegram/MAX), где бан стоит личного номера. Поэтому дефолт — остановиться
    и объяснить, а не слать вслепую.
    """
    ok, why = delivery_signal_available()
    order = [c for c in _phase_order_channels() if c in channels]
    # Каскад без WABA (только Personal) подтверждений не требует вовсе.
    if ok or "waba" not in order:
        if ok:
            log.info(f"• Путь подтверждений: {why}")
        return

    allow = bool(getattr(config, "ALLOW_NO_DELIVERY_SIGNAL", False))
    log.error("=" * 70)
    log.error("ОСТАНОВКА: подтверждения доставки недоступны.")
    log.error(f"Причина: {why}")
    log.error(
        "Без подтверждений каскад WABA → TG → MAX не сработает: провалы "
        "не видны, контакты не попадут в rescue, и через "
        f"{getattr(config, 'UNKNOWN_GRACE_MINUTES', 720)} мин молчания ПРИНЯТЫЕ "
        "сообщения уйдут в откат — то есть в личные аккаунты."
    )
    log.error(
        "Что сделать: запускать sender ТАМ ЖЕ, где работает вебхук "
        "(сервер 217.149.23.102, /opt/bulkmessage), либо поднять вебхук "
        "локально и подписать Wazzup на его URL."
    )
    if not allow:
        log.error(
            "Обход: BULK_ALLOW_NO_DELIVERY_SIGNAL=1 — только если вы понимаете, "
            "что принимаете слепые отказы в личные аккаунты."
        )
        raise SystemExit(3)
    log.warning(
        "ALLOW_NO_DELIVERY_SIGNAL=1 — идём вслепую по решению владельца."
    )
    try:
        tglog.send(
            "Боевой прогон запущен БЕЗ подтверждений доставки\n"
            f"Причина: {why}\n"
            "Каскад откатов работать не будет: провалы не видны, а принятые "
            "сообщения уйдут в резервные каналы по таймауту молчания.",
            "ERROR",
        )
    except Exception:
        pass


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
    # Каналы без событий statuses: Wazzup их НЕ присылает в принципе.
    # Единственный сигнал — эхо исходящего, по которому вебхук пишет вердикт.
    # Проверять по delivered_at для них бессмысленно: он не заполнится НИКОГДА,
    # и канал был бы объявлен сломанным при полностью рабочей доставке.
    # Живой случай 2026-10-07: telegram отвечал status="sent" (доставлено),
    # а guard видел 0 подтверждений и пропускал фазу целиком — откаты уходили
    # сразу в MAX, минуя Telegram.
    NO_STATUSES = ("telegram", "max")
    try:
        import sqlite3 as _sq
        from . import config as _cfg
        dbp = Path(getattr(_cfg, "DB_PATH", "data/crm.db"))
        if not dbp.exists():
            return empty
        if channel in NO_STATUSES:
            _conf_expr = (
                "SUM(CASE WHEN last_error IS NOT NULL "
                " OR (delivery_state IS NOT NULL "
                " AND delivery_state != 'unknown') "
                "THEN 1 ELSE 0 END)"
            )
        else:
            _conf_expr = ("SUM(CASE WHEN delivered_at IS NOT NULL "
                          "THEN 1 ELSE 0 END)")
        con = _sq.connect(f"file:{dbp}?mode=ro", uri=True)
        try:
            row = con.execute(
                f"SELECT COUNT(*) AS total, {_conf_expr} AS dlv "
                f"FROM messages WHERE channel = ? "
                f" AND created_at >= datetime('now', ?)",
                (channel, f"-{int(window_hours)} hours"),
            ).fetchone()
        finally:
            con.close()
        sent = int(row[0] or 0)
        delivered = int(row[1] or 0)
    except Exception as e:
        log.debug(f"channel_delivery_health({channel}): не посчитали: {e}")
        return empty

    _what = ("отклика от канала" if channel in NO_STATUSES
             else "подтверждения доставки")

    min_sample = int(getattr(config, "CHANNEL_HEALTH_MIN_SAMPLE", min_sample))

    if sent < min_sample:
        return {"ok": True, "sent": sent, "delivered": delivered,
                "reason": f"мало данных для вывода ({sent} < {min_sample})"}
    if delivered == 0:
        return {"ok": False, "sent": sent, "delivered": 0,
                "reason": (f"{sent} отправок за {window_hours}ч и НИ ОДНОГО "
                           f"{_what} — канал не авторизован или сломан")}
    return {"ok": True, "sent": sent, "delivered": delivered,
            "reason": f"{_what}: {delivered} из {sent}"}


def _personal_slot_count() -> int:
    """Сколько персональных отправок вообще можно сделать сегодня.

    Это не «сколько хочется», а сумма потолков каналов: MAX 20 + Telegram 50.
    Планировать больше бессмысленно — лишние слоты всё равно не заполнятся, а
    окно будет поделено на них, и каждая реальная отправка станет реже.
    """
    cur = state.reset_daily_if_new_day(state.load_state())
    total = 0
    for ch in ("max", "telegram"):
        if state.is_channel_halted(cur, ch):
            continue
        cap = int(state.channel_effective_cap(cur, ch))
        left = cap - int(state.channel_sent_today(cur, ch))
        total += max(0, left)
    return total


def _run_personal_slot_phase(
    filtered: list,
    stop_requested: dict,
    phase_stats: dict,
    label: str = "разведка",
    contacts_limit: Optional[int] = None,
) -> None:
    """Личные каналы по расписанию: MAX и Telegram одним таймлайном.

    ``contacts_limit`` — сколько контактов этой фазе ещё разрешено тронуть.
    Потолок ставится на СЛОТЫ расписания, а не на уже отправленные: иначе
    фаза нарезала бы 70 слотов, а «15 контактов» молча превращалось в 70.
    None = потолка нет (обычный длинный прогон).

    Запускается ДВАЖДЫ за круг. Первый проход — разведка: он идёт ДО WABA,
    чтобы личные каналы забирали людей раньше, чем на них потратит квоту
    WhatsApp. Второй проход — добор слотов в остаток окна; если лимит уже
    выбран, он отрабатывает за миллисекунды и просто выходит.

    Почему нельзя просто инвертировать фазы
    ---------------------------------------
    Если разведка не ограничена бюджетом, она съедает всё окно (70 отправок
    × 300 с ≈ 5.8 часа) и WABA не получит ни минуты. Если ограничить её
    слишком сильно — недобор личных каналов. Поэтому бюджет явный и
    настраиваемый, а доля успеха попыток докладывается владельцу: решение
    «сколько разведывать» должно приниматься по факту, а не вслепую.

    Почему не две отдельные фазы
    ---------------------------
    Наивный порядок «фаза MAX целиком, потом фаза Telegram целиком» не
    выполняет задачу владельца. Каждая фаза растягивается на остаток окна,
    MAX из 20 сообщений съедает окно один, и до Telegram не доходит ничего.
    Живой случай 09.10 — фазы TG и MAX не отправили НИЧЕГО: личное окно
    начиналось в 11:00, а фаза стартовала в 10:42.

    Что делает эта функция
    ---------------------
    Строит одно расписание на оба канала (schedule.build_slot_schedule):
    N случайных моментов, размазанных по остатку окна. В каждый момент
    отправляется ОДНО сообщение в тот канал, который ещё не исчерпал квоту и
    в котором у контакта есть аккаунт (MAX приоритетнее — он самый дефицитный).
    Между отправками ничего не происходит: контакт либо уже известен как
    подходящий, либо выявляется пробой.

    Ограничение, о котором честно пишем в лог: если у контакта нет аккаунта
    ни в MAX, ни в Telegram, слот потратится впустую. Разведка и отправка для
    личных каналов неразделимы — проба на ``present`` САМА отправляет сообщение
    (см. probe.py), поэтому «сначала выяснить, потом отправить по расписанию»
    для этих каналов физически невозможно.
    """
    from . import schedule

    n_slots = _personal_slot_count()
    # Потолок контактов режет СЛОТЫ. Без этого «прогон на 15» нарезал 70
    # слотов по лимиту каналов и уходил на 3+ часа (живой случай 2026-10-10).
    if contacts_limit is not None:
        if contacts_limit <= 0:
            log.info("Личные каналы: бюджет контактов исчерпан — расписание "
                     "не строится")
            return
        if n_slots > contacts_limit:
            log.info(
                f"Личные каналы: режу слоты с {n_slots} до {contacts_limit} "
                "(потолок контактов на прогон)"
            )
            n_slots = contacts_limit
    if n_slots <= 0:
        log.info(
            "Личные каналы: дневные лимиты исчерпаны или каналы в halt — "
            "расписание не строится"
        )
        return

    seconds_left = min(_phase_seconds_left("max"), _phase_seconds_left("telegram"))
    if seconds_left <= 0:
        log.info("Личные каналы: окно закрыто — расписание не строится")
        return

    # Минимум между отправками — пол, а не среднее (решение владельца
    # 2026-10-10: не меньше 15 минут). Если окно не вмещает нужное число
    # слотов с таким минимумом, расписание строится на максимально возможном
    # числе — и это пишется в лог, иначе «60 личных сообщений» тихо
    # превратились бы в 40.
    _gap = int(getattr(config, "PERSONAL_MIN_GAP_SEC", 0) or 0)
    _jitter = float(getattr(config, "PERSONAL_JITTER_MAX", 1.0) or 1.0)
    _fits = int(seconds_left // _gap) if _gap > 0 else n_slots
    if _fits < n_slots:
        log.info(
            f"Личные каналы: в окне {int(seconds_left/60)} мин при минимуме "
            f"{_gap//60} мин помещается {_fits} отправок, а лимиты каналов "
            f"дают {n_slots} — строю расписание на {_fits}"
        )
        n_slots = _fits

    slots = schedule.build_slot_schedule(
        n_slots, seconds_left, min_gap_seconds=_gap, jitter_max=_jitter
    )
    if slots and len(slots) < n_slots:
        # Слоты урезались не минимумом, а запасом под разброс: ровный шаг в
        # 15 минут — это сигнал бота, поэтому интервалы идут 15–21 мин и
        # средняя пауза больше минимума. Молча урезать нельзя: владелец
        # видит в логе «режу слоты с N на M» и должен понимать, почему.
        log.info(
            f"Личные каналы: с разбросом {_gap//60}–"
            f"{int(_gap*_jitter)//60} мин в окно влезло {len(slots)} из "
            f"{n_slots} — строю расписание на {len(slots)}"
        )
    log.info("=" * 70)
    log.info(f"РАСПИСАНИЕ ЛИЧНЫХ КАНАЛОВ: {schedule.describe(slots)}")
    log.info(
        f"Каналы в порядке приоритета: "
        f"{' → '.join(_owner_channels(_probe_channels()) or ['telegram', 'max'])}"
    )
    log.info("=" * 70)

    candidates = _personal_candidates(filtered)
    if not candidates:
        log.info("Личные каналы: нет кандидатов на отбор — фаза пропущена")
        return

    from . import probe as _probe

    sent = {"max": 0, "telegram": 0}
    filled = 0
    cursor = 0
    started_at = time.time()
    budget = max(1, int(getattr(config, "DISCOVERY_MAX_PROBES",
                                  config.PROBE_MAX_PER_RUN)))
    probed = 0
    present_hits = 0

    for slot_i, slot_at in enumerate(slots, start=1):
        if stop_requested["v"]:
            log.info(f"Личные каналы ({label}): остановка по сигналу")
            break
        if probed >= budget:
            log.info(
                f"Личные каналы ({label}): исчерпан бюджет разведки "
                f"{budget} проб — остаток окна уходит в WABA"
            )
            break

        wait = (started_at + slot_at) - time.time()
        if wait > 0:
            _sleep_phase(wait, stop_requested)
        if stop_requested["v"]:
            break

        cur = state.reset_daily_if_new_day(state.load_state())
        # К моменту слота квота могла кончиться сама (жалобы, halt) — тогда
        # слот пропускаем, а не тратим на заведомо неотправляемый канал.
        usable = [
            ch for ch in ("max", "telegram")
            if not state.is_channel_halted(cur, ch)
            and state.channel_has_quota_with_headroom(cur, ch)
        ]
        if not usable:
            log.info(
                f"Слот {slot_i}/{len(slots)}: оба личных канала недоступны — "
                f"оставшиеся слоты пропускаются"
            )
            break

        if cursor >= len(candidates):
            log.info(
                f"Слот {slot_i}/{len(slots)}: кандидаты кончились "
                f"(проба может дать вердикт unknown — он не считается успехом)"
            )
            break

        contact = candidates[cursor]
        cursor += 1
        probed += 1

        results = _probe.probe_contact_channels(contact)
        hit = next((r for r in results if r.delivered), None)
        if hit is None:
            # Слот не закрыт: аккаунта не оказалось ни там, ни там. Контакт
            # уходит в WABA обычным путём — он не попал ни в резерв, ни в
            # отчёт о персональных отправках.
            verdicts = ",".join(
                f"{r.channel}:{r.verdict}" for r in results
            ) or "нет вердиктов"
            log.info(
                f"Слот {slot_i}/{len(slots)}: {contact['phone']} — "
                f"личного аккаунта нет ({verdicts}), контакт уходит в WABA"
            )
            # То же самое в бот: владелец 2026-10-11 требует видеть в Telegram
            # не только кому отправили, но и у кого каналов нет. Раньше эта
            # строка жила только в терминале — на уровне INFO в бот не шло.
            _probe.report_miss(contact, results)
            continue

        filled += 1
        present_hits += 1
        sent[hit.channel] = sent.get(hit.channel, 0) + 1
        phase_stats[hit.channel] = phase_stats.get(hit.channel, 0) + 1
        log.info(
            f"Слот {slot_i}/{len(slots)}: ✅ {contact['phone']} → "
            f"{hit.channel.upper()} (заполнено {filled}/{len(slots)}, "
            f"{hit.channel} {sent[hit.channel]})"
        )

    rate = (present_hits / probed * 100) if probed else 0.0
    log.info("─" * 70)
    log.info(
        f"Личные каналы ({label}): заполнено {filled} из {len(slots)} слотов "
        f"(MAX {sent['max']}, Telegram {sent['telegram']})"
    )
    log.info(
        f"Разведка: проб {probed}, успех {present_hits} ({rate:.0f}%). "
        f"Один успех стоит {1 / max(present_hits, 1):.1f} проб. "
        f"Это и есть решение по бюджету: при доле {rate:.0f}% на "
        f"{config.DISCOVERY_MAX_PROBES} проб хватит на "
        f"{int(config.DISCOVERY_MAX_PROBES * rate / 100)} личных отправок."
    )
    if filled < len(slots):
        log.info(
            f"Недобор слотов: {len(slots) - filled}. Причина — кандидаты "
            f"кончились или личные аккаунты не подтвердились. Недообранные "
            f"контакты доступны через WABA и в списке pending_sms."
        )
    log.info("─" * 70)


def _personal_candidates(filtered: list) -> list[dict]:
    """Кого вообще можно пробить в личные каналы.

    Сначала те, у кого уже есть подтверждённый аккаунт — их отправка не требует
    пробы и гарантированно закроет слот. Затем остальные: у них проба сама
    станет отправкой, если аккаунт найдётся.
    """
    known: list[dict] = []
    unknown: list[dict] = []

    try:
        rows = db._query_rows(
            "SELECT ct.id AS contact_id, ct.phone AS phone, ct.name AS name, "
            "       ct.category AS category, "
            "       MAX(CASE WHEN s.channel='max' AND s.verdict='present' "
            "                THEN 1 ELSE 0 END) AS has_max, "
            "       MAX(CASE WHEN s.channel='telegram' AND s.verdict='present' "
            "                THEN 1 ELSE 0 END) AS has_tg "
            "FROM contacts ct "
            "LEFT JOIN contact_channel_state s ON s.contact_id = ct.id "
            "WHERE COALESCE(ct.is_test,0)=0 AND COALESCE(ct.is_invalid,0)=0 "
            "GROUP BY ct.id"
        )
    except Exception as e:
        log.warning(f"кандидаты личных каналов не прочитаны: {e}")
        return []

    # Правило «написали — навсегда» действует ПО КАНАЛУ: человека исключаем
    # из личных кандидатов только если закрыты ОБА личных канала. Написали в
    # Telegram — MAX для него ещё свободен (у человека может быть только
    # один мессенджер из двух).
    personal_channels = {"telegram", "max"}
    attempted_by_phone = db.get_attempted_channels()

    pool_phones = {c.get("phone") for c in filtered if c.get("phone")}

    for r in rows:
        phone = r["phone"]
        if not phone:
            continue
        closed = attempted_by_phone.get(phone, set())
        if personal_channels.issubset(closed):
            continue
        item = {
            "contact_id": r["contact_id"],
            "phone": phone,
            "name": r["name"] or "—",
            "category": r["category"] or "—",
        }
        if r["has_max"] or r["has_tg"]:
            known.append(item)
        elif not pool_phones or phone in pool_phones:
            unknown.append(item)

    known.sort(key=lambda c: c["phone"])
    unknown.sort(key=lambda c: c["phone"])
    log.info(
        f"Кандидаты личных каналов: {len(known)} с подтверждённым аккаунтом, "
        f"{len(unknown)} под пробу"
    )
    return known + unknown


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
    _run_accepted = 0
    total = len(items)

    # Скорость WABA. После пополнения баланса tier 1 = 1000/день, и при
    # последовательной паузе 10–30с это ~9 часов. Поэтому HTTP-отправки идут
    # конвейером: одновременно в работе до WABA_CONCURRENCY сообщений, а учёт
    # результатов остаётся ПОСЛЕДОВАТЕЛЬНЫМ и в исходном порядке — иначе мы бы
    # вернули гонку записи в state.json, которую только что убрали.
    #
    # Личные каналы (telegram/max) НИКОГДА не распараллеливаются: их потолок
    # не скорость, а лимит, и это личные аккаунты владельца.
    _concurrency = 1
    if phase_ch == "waba":
        try:
            _concurrency = max(1, int(getattr(config, "WABA_CONCURRENCY", 1)))
        except Exception:
            _concurrency = 1
    _pool = None
    if _concurrency > 1:
        try:
            from concurrent.futures import ThreadPoolExecutor
            _pool = ThreadPoolExecutor(max_workers=_concurrency)
        except Exception as e:
            log.warning(f"пул воркеров не создался ({e}) — последовательный режим")
            _pool = None

    def _submit(it: dict):
        # Каждый поток работает со СВОИМИ копиями backoffs/permanent_skipped:
        # общие словари из нескольких потоков — та же гонка, только тише.
        _bo = dict(backoffs)
        _ps = {k: set(v) for k, v in permanent_skipped.items()}
        _st = state.reset_daily_if_new_day(state.load_state())
        return _pool.submit(
            _run_one_contact, it, [phase_ch], _bo, _ps, _st,
            cascade_override=[phase_ch],
        ), _bo, _ps

    _inflight: dict = {}
    _next_submit = 0

    def _pump() -> None:
        """Долить конвейер, не превышая _concurrency одновременных отправок.

        ВАЖНО: долив происходит ВНУТРИ цикла, после всех проверок контакта
        (safe hours, safety cap, backoff). Раньше первые N контактов
        отправлялись ДО проверок — то есть прогон, начатый вне окна или после
        срабатывания safety cap, всё равно отправлял первую пачку.
        """
        nonlocal _next_submit
        if _pool is None:
            return
        while len(_inflight) < _concurrency and _next_submit < total:
            _f, _bo, _ps = _submit(items[_next_submit])
            _inflight[_f] = (items[_next_submit], _bo, _ps)
            _next_submit += 1
    for i, item in enumerate(items):
        if stop_requested["v"]:
            log.info("Остановка по сигналу")
            break
        cur = state.load_state()
        cur = state.reset_daily_if_new_day(cur)
        if state.max_attempts_reached(cur):
            log.info(
                f"SAFETY CAP ({state.attempts_today(cur)} попыток) — "
                f"фаза {phase_ch.upper()} прервана"
            )
            break
        if not _within_safe_hours(phase_ch):
            log.info(
                f"Safe hours для {phase_ch.upper()} закрыты — "
                f"фаза прервана, остаток в очереди"
            )
            break
        # Остановка по ЦЕЛИ ДОСТАВОК, а не по числу попыток.
        # Пока считается только принятие (HTTP 2xx), WABA набирает «успех» на
        # каждом запросе и останавливается, не доставив ничего: Meta подтверждает
        # доставку вебхуком с задержкой, поэтому к началу фазы счётчик
        # доставок отстаёт. Фаза идёт ВНИЗ ПО СПИСКУ и смотрит на цель после
        # каждой отправки — тогда лишние попытки не тратятся зря.
        _tgt = int(getattr(config, "TARGET_DELIVERED_PER_DAY", 0) or 0)
        if _tgt > 0:
            _dlv_now = delivered_today_total()
            if _dlv_now >= _tgt:
                log.info(
                    f"ЦЕЛЬ ПО ДОСТАВКАМ достигнута ({_dlv_now}/{_tgt}) — "
                    f"фаза {phase_ch.upper()} прервана"
                )
                break
        if any(v > 0 for v in backoffs.values()):
            _wait_for_backoffs(backoffs, log)

        # Попытки здесь больше НЕ растут: счётчик двигает _persist_success,
        # то есть ровно одна реальная отправка = одна попытка. Считать проходы
        # цикла было неверно: контакт, отсеянный фильтром наличия аккаунта,
        # занимал попытку, не отправив ничего, и SAFETY CAP душил фазы отката
        # ещё до того, как до них дошла очередь.

        if _pool is not None:
            # Сначала доливаем конвейер (после проверок контакта!), затем
            # забираем самый старый результат — учёт остаётся в порядке
            # исходного списка.
            _pump()
            fut = next(iter(_inflight))
            item, _bo, _ps = _inflight.pop(fut)
            try:
                ok = fut.result()
            except Exception as e:
                log.warning(f"фаза {phase_ch}: сбой на {item.get('phone')}: {e}")
                ok = False
            # Сливаем результаты потока обратно и сразу сверяем квоту с БД:
            # состояние в файле могло потерять обновление из-за параллельных
            # потоков, а квотные решения принимаются по daily_counters.
            backoffs.update({k: v for k, v in _bo.items() if v})
            permanent_skipped.update({k: v for k, v in _ps.items() if v})
            try:
                cur = state.load_state()
                cur = state.reconcile_sent_from_db(cur, phase_ch)
            except Exception:
                pass
        else:
            try:
                ok = _run_one_contact(
                    item, [phase_ch], backoffs, permanent_skipped, cur,
                    cascade_override=[phase_ch],
                )
            except Exception as e:
                log.warning(f"фаза {phase_ch}: сбой на {item.get('phone')}: {e}")
                ok = False

        done += 1
        if ok:
            _run_accepted += 1
            # Счётчик «принято транспортом» живёт в SQLite. Без этого строки
            # отчёт был бы пустым: delivered/rejected пишутся вебхуком и
            # сторожем, а accepted не писал никто — и воронка выглядела бы
            # как «0 принято, 15 доставлено».
            try:
                db.upsert_counter(db.today_str(), phase_ch, "accepted")
            except Exception as e:
                log.debug(f"счётчик accepted не записан: {e}")

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
                log.info(f"Rescue {rid} → {item.get('phone')} через {phase_ch}")
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
                # Потолок попыток: контакт, исчерпавший каналы N раз, больше
                # НЕ крутится в очереди автоматически. Иначе «1 попытка в
                # день вечно» — это просто расход квоты на заведомо
                # недостижимые номера. Такой контакт уходит в exhausted и в
                # ежедневную выгрузку для ручного разбора.
                try:
                    _why = db.should_exhaust(rid, _fallbacks)
                except Exception as _se:
                    log.debug(f"should_exhaust недоступна: {_se}")
                    _why = None
                if _why:
                    db.mark_rescue_exhausted(rid, _why)
                    log.info(
                        f"Rescue {rid} ({item.get('phone')}) исчерпан "
                        f"({_why}) — стоп автоматики, в ежедневную выгрузку"
                    )
                elif _fallbacks and set(_fallbacks).issubset(_tried):
                    from datetime import timedelta as _td
                    _tomorrow = (
                        datetime.now(timezone.utc) + _td(days=1)
                    ).isoformat()
                    db.mark_rescue_deferred(rid, _tomorrow)
                    log.info(
                        f"Rescue {rid} ({item.get('phone')}) провалился во "
                        f"всех каналах ({', '.join(_tried)}) — в конец очереди, "
                        f"повтор после {_tomorrow[:10]}"
                    )
                else:
                    log.info(
                        f"Rescue {rid} не удался через {phase_ch} "
                        f"(остаётся в очереди)"
                    )

        if done % 10 == 0 or done == total:
            log.info(
                f"[{label}] {done}/{total} · "
                f"принято {state.channel_sent_today(cur, phase_ch)}"
                f"/{state.channel_effective_cap(cur, phase_ch)}"
            )

        remaining = total - i - 1
        if remaining > 0 and not stop_requested["v"]:
            d = _phase_delay(phase_ch, _phase_seconds_left(phase_ch), remaining)
            if _pool is not None and _concurrency > 1:
                # Пауза делится между воркерами: общая скорость фазы должна
                # остаться прежней, просто запросы идут наложенно.
                d = max(1.0, d / _concurrency)
            _sleep_phase(d, stop_requested)

        # Анти-блок: длинный перерыв считаем по принятым ЗА ЭТОТ ПРОГОН, а не
        # по кросс-канальной сумме sent_today за сутки. Раньше сумма копилась
        # по всем каналам и попадала на круглое число «случайно»: 2026-10-06
        # прогон встал на 15 минут после 6-го сообщения, потому что сумма
        # суток (44 + 6) кратно совпала с BATCH_SIZE=50. Выглядело как зависание.
        if (
            config.BATCH_SIZE
            and _run_accepted > 0
            and _run_accepted % config.BATCH_SIZE == 0
            and config.BATCH_BREAK_MAX > 0
        ):
            log.info(
                f"{getattr(config, 'UNKNOWN_GRACE_MINUTES', 720)} мин, "
                f"принято {_run_accepted} в этом прогоне"
            )
            _sleep_phase(
                random.uniform(config.BATCH_BREAK_MIN, config.BATCH_BREAK_MAX),
                stop_requested,
            )
    if _pool is not None:
        try:
            _pool.shutdown(wait=True)
        except Exception:
            pass
    return done


def _select_contacts_for_cycle() -> list:
    """Контакты для ОДНОГО круга фаз, пересобираются каждый круг.

    Источник контактов — БАЗА, а не Excel. Пул это «ещё не писали ни в один
    канал», минус тестовые и негодные номера. Правило живёт в
    ``store.messages.contacted_phones`` и вызывается отсюда через репозиторий,
    поэтому документ и код разойтись больше не могут: раньше код считал
    «писали» по статусу, а AGENTS.md предписывал по message_id — расхождение
    на 110 контактов, и неизвестно, кому бы поверил следующий читатель.

    Пересборка каждый круг обязательна: отправки предыдущего круга уже
    записаны в messages, и без перезапроса второй круг писал бы тем же
    контактам второй раз.
    """
    try:
        pool = repo_contacts.sendable()
    except Exception as e:
        log.error(f"Не удалось получить пул контактов из базы: {e}")
        return []

    cur = state.reset_daily_if_new_day(state.load_state())
    out = []
    for c in pool:
        phone = c.get("phone")
        if not phone:
            continue
        if state.is_phone_in_cooldown(cur, phone, "waba"):
            continue
        out.append(c)
    # Антикоастинг по WABA: молчание по этому номеру почти всегда означает
    # отказ Meta по маркетинговому лимиту («нельзя отправить шаблон
    # „Маркетинг“»). Повтор на следующий день сожжёт ещё один слот квоты и
    # даст тот же отказ. Номер исключается ТОЛЬКО из WABA — в откат по
    # Telegram/MAX он остаётся доступен.
    try:
        _waba_cooling = db.get_active_cooldowns("waba")
    except Exception:
        _waba_cooling = set()
    if _waba_cooling:
        _before = len(out)
        out = [c for c in out if c.get("phone") not in _waba_cooling]
        if len(out) != _before:
            log.info(
                f"Антикоастинг WABA: {len(_waba_cooling)} номеров в "
                f"молчании исключены из WABA-отбора "
                f"({getattr(config, 'WABA_SILENT_COOLDOWN_DAYS', 14)} дней), "
                f"но остаются доступны для отката"
            )
    return out


def _sweep_silence_into_rescue(minutes: int, unknown_minutes: int | None = None) -> int:
    """Перед фазой отката: молчание вебхука разбирается в две корзины.

    Раньше молчание и явный отказ считались одним и тем же, и уходили в откат
    через 5 минут. Замер 2026-10-06 показал, что это ложь: из 16 принятых
    сообщений подтверждение пришло для одного, а в UI Wazzup 4 из 6 были без
    ошибок — то есть большинство «провалов» были просто доставками без события.
    Такой откат лил дубли в личные аккаунты.

    Теперь:
      * ЯВНЫЙ отказ → откат через `minutes` (REJECT_GRACE_MINUTES, 5 мин).
      * Молчание → откат только через `unknown_minutes` (UNKNOWN_GRACE_MINUTES,
        12 ч по умолчанию), а контакт получает антикоастинг на WABA.
    """
    try:
        from . import reconcile as _reconcile
        n = _reconcile.sweep_unconfirmed_deliveries(
            limit=200,
            minutes=max(1, int(minutes)),
            unknown_minutes=(
                int(unknown_minutes) if unknown_minutes is not None else None
            ),
        )
    except Exception as e:
        log.warning(f"Сторож молчания перед откатом не отработал (non-fatal): {e}")
        return 0
    if n:
        log.warning(
            f"В rescue отправлено {n}: явные отказы (горизонт {int(minutes)} мин) "
            f"+ молчания (горизонт "
            f"{getattr(config, 'UNKNOWN_GRACE_MINUTES', 720)} мин). "
            f"Молчание НЕ равно отказу: в откат оно уходит только по своему "
            f"горизонту, иначе личные аккаунты получали бы дубли."
        )
    return n


def _build_exec_order(phase_order: list[str]) -> list[str]:
    """Порядок ВЫПОЛНЕНИЯ фаз из порядка ПРИОРИТЕТОВ.

    config.PHASE_ORDER отвечает на вопрос «кому какой канал лучше»
    (max → telegram → waba), а не «что запускать первым».

    Главное: ЛИЧНЫЕ КАНАЛЫ ИДУТ ПЕРВЫМИ. 09.10 WABA отработал первым и забрал
    86 контактов, 40% из которых оказались без WhatsApp (20 BAD_CONTACT), а у
    части наверняка был Telegram — квота ушла впустую, пока личные каналы
    (20 MAX + 50 Telegram) остались нетронутыми. Мы платили квотой WhatsApp за
    право НЕ узнать, что у человека есть мессенджер.

    Итоговый порядок: personal (разведка) → waba → personal (добор слотов).
    Второй проход личных каналов занимает миллисекунды, если лимит уже выбран.
    """
    personal_first = any(ch in ("max", "telegram") for ch in phase_order)
    if personal_first and "waba" in phase_order:
        return ["personal", "waba", "personal"]
    if personal_first:
        merged: list[str] = []
        for ch in phase_order:
            if ch in ("max", "telegram"):
                if "personal" not in merged:
                    merged.append("personal")
            else:
                merged.append(ch)
        return merged
    return list(phase_order)


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
             + (f" (круг {cycle_no})" if cycle_no > 1 else ""))
    log.info("=" * 70)

    # Стартовый дайджест в Telegram: один раз за прогон, только на первом
    # круге. Дальше повторять его бессмысленно — это шум, а не прозрачность.
    if cycle_no == 1:
        try:
            from . import notify as _notify
            _cur = state.load_state()
            _caps = {
                ch: state.channel_effective_cap(_cur, ch)
                for ch in phase_order
            }
            _planned = 0
            try:
                _planned = len(_collect_fresh_waba_targets(
                    _select_contacts_for_cycle(), _cur))
            except Exception:
                pass
            # Ежедневная выгрузка исчерпанных: контакт, на котором система
            # остановилась, обязан быть виден владельцу, а не просто исчезнуть.
            try:
                _csv = db.export_exhausted_csv(getattr(config, "DATA_DIR", "data"))
                if _csv:
                    log.info(f"Выгрузка исчерпанных: {_csv}")
                    _notify.event(
                        f" Исчерпано контактов: {db.count_exhausted_rescues()}. "
                        f"Выгрузка: {_csv} — разбери вручную.",
                        "WARNING",
                    )
            except Exception as e:
                log.debug(f"выгрузка exhausted не удалась: {e}")
            _notify.digest_start(
                phase_order=list(phase_order),
                caps=_caps,
                contacts_planned=_planned,
                rescue_pending=db.count_pending_rescues(),
                exhausted_total=db.count_exhausted_rescues(),
                waba_tier=state.get_waba_tier(_cur),
                code_version=getattr(config, "CODE_VERSION", "unknown"),
                plan=_build_run_plan(_cur, _planned),
            )
        except Exception as e:
            log.debug(f"digest_start не отправился: {e}")

    # ── Порядок ВЫПОЛНЕНИЯ отличается от порядка ПРИОРИТЕТОВ ──
    # Логика и обоснование — в _build_exec_order() ниже.
    exec_order = _build_exec_order(phase_order)

    for phase_no, phase_ch in enumerate(exec_order):
        if stop_requested["v"]:
            break

        # ── Фаза PROBE Telegram — ПЕРВОЙ.
        # Живой случай 2026-10-10: условие было `phase_ch in _probe_channels()`
        # — то есть `phase_ch in {'telegram','max'}`. Но в порядке выполнения
        # фаза личных каналов называется 'personal', и туда сводятся оба
        # мессенджера. Условие не срабатывало НИКОГДА: проба молча не
        # запускалась, и каскад уходил в личные каналы вслепую.
        #
        # Telegram проверяем первым: отдельного лимита на проверку номера
        # у него нет, и вердикты накапливаются бесплатно по квоте сообщений.
        _is_first_phase = (phase_no == 0)
        if (_is_first_phase and "telegram" in _probe_channels()
                and not _probe_done_for_run()):
            _run_probe_phase(stop_requested, phase_stats, channel="telegram")
            _mark_probe_done_for_run()

        if phase_no > 0:
            # Ждём события вебхука, ПОТОМ превращаем молчание в провал.
            #
            # Сторож вызывается ВСЕГДА, а не только когда пауза > 0. Раньше он
            # лежал внутри `if grace > 0`, и при PHASE_GRACE_MINUTES=0 (или
            # RESCUE_SETTLE_MINUTES=0) фазы отката читали пустую очередь и не
            # отправляли НИЧЕГО — цепочка молча вырождалась в один WABA-проход.
            # Пауза нужна, чтобы долетели статусы; наполнять очередь должна
            # тишина, а не пауза.
            # Предыдущая фаза берётся из ПОРЯДКА ВЫПОЛНЕНИЯ, а не из
            # config.PHASE_ORDER. Порядки разошлись: конфиг задаёт приоритет
            # каналов (max,telegram,waba), а выполняется waba → personal. По
            # старому коду после WABA фаза personal ждала бы RESCUE_SETTLE и
            # сторож получил бы неверный таймаут — молчание после WABA
            # отсчитывается PHASE_GRACE_MINUTES, это другая величина.
            prev_phase = exec_order[phase_no - 1]
            wait_min = int(
                getattr(config, "PHASE_GRACE_MINUTES", 5)
                if prev_phase == "waba"
                else getattr(config, "RESCUE_SETTLE_MINUTES", 5)
            )
            if wait_min > 0:
                log.info(
                    f"Пауза {wait_min} мин после {prev_phase.upper()}: ждём "
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

        # ── Фаза личных каналов по расписанию ──
        # Заменяет отдельные фазы MAX и Telegram: обе отправки идут одним
        # таймлайном со случайными интервалами, иначе первая фаза съест всё
        # окно (живой случай 09.10: фазы TG и MAX не отправили ничего).
        #
        # Стоит ПОСЛЕ блока грейса и сторожа, а не до него. Иначе тишина после
        # WABA не успевала бы превратиться в очередь отката, и каскад молча
        # выродился бы в один WABA-проход: молчание вебхука = провал, а без
        # сторожа провалы в откат не попадают.
        if phase_ch == "personal":
            _label = "разведка" if phase_no == 0 else "добор"
            _pb = contacts_budget_left()
            if _pb == 0:
                log.info(
                    "Личные каналы: бюджет контактов исчерпан "
                    f"({_contacts_messaged_this_run()}) — фаза пропущена"
                )
                continue
            _run_personal_slot_phase(
                filtered, stop_requested, phase_stats, label=_label,
                contacts_limit=_pb)
            continue

        if phase_ch == "waba":
            # Ждём открытия активного окна (10:00 МСК) перед первой фазой.
            if _wait_until_active_window(log):
                pass
            cur = state.load_state()
            if not state.channel_has_quota_with_headroom(cur, "waba"):
                log.info("WABA: квота исчерпана — фаза пропущена")
                continue
            targets = _collect_fresh_waba_targets(filtered, cur)
            # Бюджет контактов: WABA получает остаток после пробы и любых
            # предыдущих шагов, иначе «15 контактов» превращается в 15 WABA
            # плюс все личные слоты сверху.
            _wb = contacts_budget_left()
            if _wb == 0:
                log.info(
                    "WABA: бюджет контактов исчерпан "
                    f"({_contacts_messaged_this_run()}) — фаза пропущена"
                )
                continue
            if _wb is not None and len(targets) > _wb:
                log.info(
                    f"WABA: режу цели с {len(targets)} до {_wb} "
                    "(потолок контактов на прогон)"
                )
                targets = targets[:_wb]
            # Дневной кап канала (250) — верхняя граница по лимитам Meta.
            _cap_waba = int(state.channel_effective_cap(cur, "waba"))
            # Лимит запуска (--waba-limit N): для первых боевых прогонов,
            # когда вместо 250 сообщений сразу надо отдать 10–20.
            _run_cap = int(getattr(config, "WABA_RUN_LIMIT", 0) or 0)
            if _run_cap > 0:
                _run_cap = min(_run_cap, _cap_waba)
                if len(targets) > _run_cap:
                    log.info(
                        f"ТЕСТОВЫЙ РЕЖИМ: WABA ограничен { _run_cap } "
                        f"сообщениями в этом запуске (BULK_WABA_RUN_LIMIT), "
                        f"в базе доступно {len(targets)}"
                    )
                targets = targets[:_run_cap]
            targets = targets[:_cap_waba]
            if not targets:
                log.info("WABA: нет свежих контактов — фаза пропущена")
                continue
            if not _phase_fits("waba", len(targets)):
                continue
            log.info(
                f"ФАЗА 1 · WABA · {len(targets)} свежих контактов · "
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
                        f"Канал {phase_ch.upper()} НЕ РАБОТАЕТ: {_h['reason']}. "
                        f"Фаза пропущена, очередь не тронута — откат уйдёт "
                        f"следующим каналом. Проверь авторизацию канала в Wazzup."
                    )
                    continue
                if _h["sent"] >= int(
                        getattr(config, "CHANNEL_HEALTH_MIN_SAMPLE", 5)):
                    log.info(f"{phase_ch.upper()}: {_h['reason']}")

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
            # Если проба аккаунтов отработала, откатываемся ТОЛЬКО на те
            # номера, где аккаунт подтверждён. Иначе фаза отката сжигает
            # личный лимит канала на заведомо несуществующие аккаунты.
            # Фильтр применяется только когда вердикты ЕСТЬ: пустая таблица
            # не должна молча превращать откат в ноль отправок.
            if _probe_ran_with_verdicts(phase_ch):
                pending = _filter_by_presence(pending, phase_ch)
            if not pending:
                log.info(
                    f"Фаза {phase_ch.upper()}: пусто (нет неподтверждённых "
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
                f"ФАЗА {phase_no + 1} · {phase_ch.upper()} · "
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

    global _RUN_STARTED_AT
    _RUN_STARTED_AT = time.time()

    # Проба аккаунтов — один раз за запуск. В длинном прогоне круги
    # повторяются, но вердикты не меняются (TTL сутки), поэтому повторная
    # проба только жгла бы время без новой информации.
    global _PROBE_DONE, _MAX_PROBE_DONE
    _PROBE_DONE = False
    _MAX_PROBE_DONE = False

    config.configure_logging()
    db.init_db()

    # Перенос счётчиков дня из state.json в SQLite — однократно и только
    # если по текущей дате в daily_counters ещё ничего нет.
    try:
        _moved = db.migrate_counters_from_state(state.load_state())
        if _moved:
            log.info(
                f"Счётчики дня перенесены из state.json в SQLite: {_moved} значений"
            )
    except Exception as e:
        log.warning(f"миграция счётчиков не удалась (non-fatal): {e}")

    # Актуализируем tier WABA до расчёта квот: он приходит событием вебхука,
    # а вебхук может быть мёртвым (баланс пополнен, а мы считаем tier 0).
    try:
        _tier_before = state.get_waba_tier(state.load_state())
        _tier_now = wazzup.refresh_waba_tier()
        if _tier_now and _tier_now != _tier_before:
            log.info(
                f"WABA tier {_tier_before} → {_tier_now} "
                f"(кап {config.WABA_TIER_LIMITS.get(_tier_now, '?')}) — "
                f"источник: опрос Wazzup, не вебхук"
            )
    except Exception as e:
        log.debug(f"refresh_waba_tier не отработал: {e}")

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
            log.info("SMS.RU выключен (BULK_SMSRU_ENABLED=0) — канал SMS не используется")
        elif not config.SMSRU_API_ID:
            log.info(
                "SMS.RU пропущен: не задан BULK_SMSRU_API_ID в .env.local. "
                "Без api_id SMS.RU отвечает ошибкой 200."
            )
        elif not config.SMSRU_SENDER_NAME:
            log.info(
                "SMS.RU пропущен: не задан BULK_SMSRU_SENDER_NAME. Без "
                "зарегистрированного имени отправителя SMS.RU отвечает 221."
            )
        elif not sms_templates_map:
            log.warning(
                "SMS.RU пропущен: не найден файл %s с SMS-шаблонами "
                "(SMS без коротких шаблонов стоил бы 5-6 сегментов = 42-51 ₽)",
                config.SMSRU_TEMPLATES_PATH,
            )
        else:
            channels = list(channels) + ["sms"]
            log.info(
                "SMS.RU подключён как fallback (отправитель %r, "
                "дневной лимит %d, сегментов максимум %d, шаблонов: %d)",
                config.SMSRU_SENDER_NAME,
                config.CHANNEL_DAILY_LIMITS.get("sms", 0),
                config.SMSRU_MAX_SEGMENTS,
                len(sms_templates_map),
            )

    # Pre-flight: проверяем шаблоны WABA.
    # Раньше условие было `valid_count < total_count`, то есть ОДИН плохой
    # шаблон выключал WABA целиком — а это канал, дающий ~100 из ~130
    # доставленных в сутки. Причина отключения ложная: контакт с плохим
    # шаблоном и так отсекается по своей категории (см. sender.py:782
    # «WABA skip: нет template для категории» и отбор пула в
    # _collect_fresh_waba_targets). Ошибка на одном контакте не
    # распространяется на остальные.
    #
    # Выключаем WABA только когда не работает НИ ОДИН шаблон — тогда фаза
    # была бы чистым расходом квоты в 250 попыток.
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
    if getattr(config, "USE_WABA", 0) and config.USE_WABA >= 1 and total_count and valid_count == 0:
        log.warning(
            f"WABA TEMPLATES: 0/{total_count} категорий имеют валидный UUID. "
            f"WABA будет SKIPPED для всех контактов этой сессии (cascade пойдёт "
            f"через TG/MAX). Это защищает от BUILD_TEMPLATE_ERROR на каждом "
            f"контакте."
        )
        log.warning(
            "Чтобы включить WABA: получи UUID одобренных шаблонов из Meta "
            "Business Manager (WhatsApp → Message Templates → 'API ID') и "
            "пропиши в .env.local как WABA_TEMPLATE_*_ID"
        )
        if "waba" in channels:
            channels = [c for c in channels if c != "waba"]
    elif total_count and valid_count < total_count:
        bad_cats = [cat for cat, s in waba_template_status.items() if not s["valid"]]
        log.warning(
            f"WABA TEMPLATES: {valid_count}/{total_count} категорий имеют "
            f"валидный UUID. WABA ОСТАЁТСЯ ВКЛЮЧЁННЫМ для {valid_count} "
            f"категорий. Контакты категорий {bad_cats} будут отсечены на "
            f"этапе отбора (без шаблона отправка заведомо не пройдёт) и "
            f"уйдут в pending_sms. Заменить шаблон: Meta Business Manager → "
            f"WhatsApp → Message Templates → 'API ID'."
        )

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
            "Sender не запущен: нет активных каналов (проверь WAZZUP_*_CHANNEL_ID)",
            "ERROR",
        )
        return

    log.info("=" * 70)
    log.info("ШАБЛОНЫ СООБЩЕНИЙ:")
    for k, tpl in templates_map.items():
        snippet = (tpl[:200] + "…") if len(tpl) > 200 else tpl
        log.info(f"• {k}: {snippet}")
    log.info("=" * 70)

    # Пул контактов берётся из базы. Excel больше не участвует в рантайме:
    # он прочитан один раз командой `import-contacts`, и все контакты, кого
    # мы уже вычеркнули, лежат в таблице contacts вместе с историей.
    try:
        all_contacts = repo_contacts.sendable()
        pool_size = len(all_contacts)
    except Exception as e:
        log.error(f"Не удалось получить пул контактов из базы: {e}")
        return

    try:
        contacted = len(repo_messages.contacted_phones())
    except Exception:
        contacted = 0

    # ЗАЩИТА ОТ ДУБЛЕЙ. Смысл изменился вместе с источником: раньше пугала
    # пустая crm.db при полном Excel («забыли залить историю»). Теперь опасна
    # обратная картина — много контактов в базе и НИ ОДНОЙ отправки в
    # истории, то есть база залита без сообщений и пул врубную.
    if contacted == 0 and repo_contacts.total() > 100:
        log.critical(
            f"ЗАЩИТА ОТ ДУБЛЕЙ: в базе {repo_contacts.total()} контактов, "
            f"но НИ ОДНОЙ отправки в истории. Похоже, база залита контактами "
            f"без истории сообщений — весь пул получит повторную рассылку. "
            f"ОСТАНОВКА. Сначала залейте историю или проверьте базу."
        )
        log.critical("Если это действительно первый запуск (никогда раньше не слали) — "
                     "поставьте BULK_SKIP_DUP_PROTECTION=1 и перезапустите.")
        if not os.environ.get("BULK_SKIP_DUP_PROTECTION"):
            return
        log.warning("BULK_SKIP_DUP_PROTECTION=1 — защита отключена, продолжаю")

    # Импорт согласия из Excel больше не выполняется: контакты приехали в базу
    # вместе с признаком согласия, а столбца согласия в файле всё равно нет.
    _cstats = db.consent_stats()
    log.info("=" * 70)
    log.info("СТАТИСТИКА ЗАПУСКА:")
    log.info(f"• Источник контактов: база (таблица contacts)")
    log.info(f"• Контактов в базе всего: {repo_contacts.total()}")
    log.info(f"• Уже отправлено ранее (в пул не попадут): {contacted}")
    log.info(f"• Неписаных осталось: {pool_size}")
    log.info(f"• Каналов активно: {channels}")
    # Лимиты печатаем ТОЛЬКО для активных каналов. Раньше тут был весь
    # config.CHANNEL_DAILY_LIMITS, где есть и whatsapp (legacy Personal WA,
    # канал не подключён — WAZZUP_WHATSAPP_CHANNEL_ID пуст), и sms (отключён).
    # В логе это читалось как «WhatsApp и WABA — два канала, оба работают»,
    # хотя активны ровно три: waba, telegram, max.
    _lim = {ch: cap for ch, cap in config.CHANNEL_DAILY_LIMITS.items()
            if ch in channels}
    _skipped = sorted(set(config.CHANNEL_DAILY_LIMITS) - set(channels))
    log.info(f"• Дневные лимиты (по активным): {_lim}")
    if _skipped:
        log.info(f"• Не используются (нет канала/выключены): {_skipped}")
    log.info(f"• Google Sheets: {'включены' if sheets.SHEETS.enabled else 'ОТКЛЮЧЕНЫ'}")
    # Проверка пути подтверждений ДО любой отправки. Живой случай 2026-10-07:
    # sender считали локально на Windows, вебхук писал на сервере в другую БД —
    # 30 принятых сообщений и НОЛЬ подтверждений, при этом в интерфейсе Wazzup
    # были видны отказы Meta. Каскад молчал, потому что ловить ему было нечего.
    try:
        _sig_ok, _sig_why = delivery_signal_available()
        log.info(f"• Путь подтверждений: {_sig_why}")
    except Exception as _se:
        log.debug(f"проверка пути подтверждений не отработала: {_se}")
    log.info(f"• Анти-блок: батч={config.BATCH_SIZE}, "
             f"задержка={config.DELAY_MIN}-{config.DELAY_MAX}с")
    # Согласие — ключевой показатель риска бана и штрафа по ФЗ-38.
    log.info(
        f"• Согласие: есть {_cstats['granted']}, "
        f"отказ {_cstats['denied']}, "
        f"нет данных {_cstats['unknown']} (из {_cstats['total']})"
    )
    if _cstats["denied"]:
        log.info(
            f"Отказников {_cstats['denied']} — им НИКОГДА не пишем "
            f"(уважается всегда, независимо от CONSENT_REQUIRED_FOR_COLD)"
        )
    if _cstats["granted"] == 0 and _cstats["total"] > 0:
        if config.CONSENT_REQUIRED_FOR_COLD:
            log.info("CONSENT_REQUIRED_FOR_COLD=1, но согласие не подтверждено "
                     "ни у кого — отправка будет пропускать всех.")
        else:
            # Раньше это было log.warning и вылезало в консоль на КАЖДЫЙ
            # запуск — 813 контактов без opt-in это постоянная, а не разовая
            # ситуация, и строка просто забивала стартовый баннер. Сама
            # проверка осталась, но уровень опущен до DEBUG: включается
            # только при отладке (python -O / logging DEBUG на sender).
            # ВНИМАНИЕ: риск ФЗ-38 ч.1 ст.18 никуда не делся, он просто
            # больше не кричит сам. Решение — на владельце рассылки.
            log.debug(
                " Ни у кого нет подтверждённого согласия (%d контактов). "
                "Холодная рассылка без opt-in — риск по ч.1 ст.18 ФЗ-38.",
                _cstats["total"],
            )
    log.info("=" * 70)

    backoffs: dict[str, int] = {ch: 0 for ch in channels}
    permanent_skipped: dict[str, set[str]] = {}
    stop_requested = {"v": False, "sig": None}
    lock_path = None # будет установлен ниже

    # TG: уведомляем админа о старте рассылки
    try:
        _start_state = state.load_state()
        _sent_today = sum(_start_state.get("sent_today", {}).values())
    except Exception:
        _sent_today = 0
    if config.DRY_RUN:
        tglog.send(
            f"Sender ЗАПУЩЕН (DRY-RUN).\n"
            f"Каналы: {' → '.join(_owner_channels(channels))}\n"
            f"Лимиты: {_limits_for_owner(channels)}\n"
            f"Уже отправлено сегодня: {_sent_today}\n"
            f"Окно: {config.ACTIVE_HOURS_START:02d}:00-"
            f"{config.ACTIVE_HOURS_END:02d}:00 {config.TIMEZONE_NAME}\n"
            f" Это DRY-RUN, реальных отправок не будет.",
            "INFO",
        )
    else:
        tglog.send(
            f"Sender ЗАПУЩЕН (БОЕВОЙ РЕЖИМ).\n"
            f"Каналы: {' → '.join(_owner_channels(channels))}\n"
            f"Лимиты: {_limits_for_owner(channels)}\n"
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
                        f"ВНИМАНИЕ: последние {total_recent} отправок в БД — "
                        f"ВСЕ failed. Возможно, WAZZUP_API_KEY протух или аккаунт в бане. "
                        f"Рекомендую сначала прогнать config.preflight_check() перед боем."
                    )
        except Exception as e:
            log.warning(f"Не удалось проверить историю failed: {e}")

    def _on_signal(sig, frame):
        """Корректная остановка: текущий контакт дорабатывает, цикл выходит."""
        try:
            sig_name = signal.Signals(sig).name
        except Exception:
            sig_name = f"signal {sig}"
        log.info(
            f"\n Получен {sig_name} — останавливаюсь после текущего контакта. "
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
            "\n Повторный сигнал — принудительный выход. Состояние сохранено."
        )
        raise KeyboardInterrupt()

    # SIGINT работает везде, SIGTERM — только на Unix.
    # На Windows пытаемся установить SIGTERM, но это не падает — просто игнорируется.
    try:
        signal.signal(signal.SIGINT, _on_signal)
    except Exception as e:
        log.warning(f"Не удалось установить SIGINT handler: {e}")
    try:
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError, AttributeError) as e:
        # На Windows SIGTERM может не поддерживаться — это нормально, не warning
        if os.name != "nt":
            log.warning(f"Не удалось установить SIGTERM handler: {e}")

    # C1: file lock — защита от двойного запуска
    lock_path = _try_acquire_lock()
    if lock_path is None:
        log.error("Sender не запущен из-за занятости lock-файла.")
        tglog.send(
            f"Sender не запущен — другой процесс держит lock-файл: {config.LOCK_PATH}. "
            f"Проверь, не запущен ли sender дважды.",
            "ERROR",
        )
        return

    log.info("─" * 70)
    log.info("Нажмите Ctrl+C в этом терминале для корректной остановки.")
    log.info("─" * 70)

    # Стартовое предупреждение + оценка общего времени на 100 сообщений
    if config.DRY_RUN:
        log.warning(
            "DRY-RUN ВКЛЮЧЁН (BULK_DRY_RUN=1) — реальных отправок не будет, "
            "только имитация. Для боевого запуска установи BULK_DRY_RUN=0."
        )
    else:
        transport_label = "Wazzup (WABA → TG → MAX)"
        log.warning(
            f"БОЕВОЙ РЕЖИМ — сейчас будут ОТПРАВЛЕНЫ реальные сообщения через {transport_label}. "
            "Чтобы остановиться: Ctrl+C (текущий контакт доработает, остальные отменятся)."
        )

    # Активное окно + TZ
    log.info(f"Timezone: {config.TIMEZONE_NAME}")
    if config.ACTIVE_HOURS_START > 0 or config.ACTIVE_HOURS_END > 0:
        log.info(
            f"Активное окно: {config.ACTIVE_HOURS_START:02d}:00-"
            f"{config.ACTIVE_HOURS_END:02d}:00 ({config.TIMEZONE_NAME})"
        )
        now = config.now_tz()
        if not config.is_within_active_hours(now):
            wait = config.seconds_until_active_window(now) / 3600
            log.info(f"Сейчас вне окна. До начала: {wait:.1f}ч")
        else:
            log.info(f"Сейчас в окне ({now.strftime('%H:%M')})")

    # Грубая оценка общего времени на 100 сообщений (для оператора).
    # Channel-aware: 90% контактов ожидаем через WABA (быстро), 10% через
    # Personal (медленнее). Реальный mix зависит от WABA success rate.
    waba_avg = (config.WABA_DELAY_MIN + config.WABA_DELAY_MAX) / 2
    personal_avg = (config.PERSONAL_DELAY_MIN + config.PERSONAL_DELAY_MAX) / 2
    avg_delay = 0.9 * waba_avg + 0.1 * personal_avg
    msgs_per_batch = max(1, config.BATCH_SIZE)
    n_batches = max(0, 100 // msgs_per_batch - 1) # кол-во длинных перерывов
    avg_break = (
        (config.BATCH_BREAK_MIN + config.BATCH_BREAK_MAX) / 2
        if config.BATCH_BREAK_MAX > 0 else 0
    )
    est_total = 100 * avg_delay + n_batches * avg_break
    log.info(
        f"Прогноз: 100 сообщений ≈ {_estimate_total_time(est_total)} "
        f"(средняя пауза {_fmt_duration(avg_delay)}, "
        f"длинный перерыв каждые {msgs_per_batch} сообщений ≈ "
        f"{_fmt_duration(avg_break)})"
    )
    log.info("─" * 70)

    # all_contacts — это уже пул из базы: «ещё не писали ни в один канал».
    # Повторно отсекать по sent_phones здесь не нужно и опасно: правило должно
    # жить в одном месте, иначе документ и код снова разойдутся.
    contacts_to_send = list(all_contacts)

    # Пул пуст — это закономерность, а не сбой. Молчать здесь нельзя:
    # владелец должен понять, что рассылка закончилась из-за списка, а не
    # из-за ошибки.
    if not contacts_to_send:
        _last = None
        try:
            _last = repo_messages.last_contacted_at()
        except Exception:
            pass
        log.critical(
            "ПУЛ КОНТАКТОВ ПУСТ — неотправленных больше не осталось."
            + (f" Последняя отправка: {_last}." if _last else "")
        )
        tglog.send(
            "Рассылка остановлена: пул контактов пуст.\n"
            "Неотправленных больше не осталось — новые контакты не добавлялись.\n"
            "Чтобы продолжить, добавьте контакты и выполните:\n"
            "py main.py import-contacts --apply",
            "INFO",
        )
        return

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
    log.info("ФИЛЬТР ПО КАТЕГОРИЯМ (Message_script.md):")
    log.info(f"Допустимые категории: {sorted(config.ALLOWED_CATEGORIES)}")
    log.info(f"Категории в базе: "
             f"{sorted({c.get('category', '—') for c in all_contacts})}")
    log.info(f"Всего контактов с телефоном: {len(contacts_to_send)}")
    log.info(f"Подходит для рассылки: {len(filtered)}")
    if skipped_by_cat:
        log.info(f"Пропущено (статус не в скрипте): {sum(skipped_by_cat.values())}")
        for cat, n in sorted(skipped_by_cat.items(), key=lambda x: -x[1]):
            log.info(f"• {cat!r}: {n}")
    log.info("=" * 70)

    # Распределение по целевым категориям
    by_cat: dict[str, int] = {}
    for c in filtered:
        by_cat[c["_normalized_category"]] = by_cat.get(c["_normalized_category"], 0) + 1
    log.info("Распределение по целевым категориям:")
    for cat in sorted(config.ALLOWED_CATEGORIES):
        log.info(f"• {cat}: {by_cat.get(cat, 0)}")
    log.info("─" * 70)

    if filtered:
        preview = filtered[:5]
        for i, c in enumerate(preview, 1):
            log.info(f"{i}. {c.get('phone')} — {c.get('name', '—')} "
                     f"({c.get('category', '—')} → {c['_normalized_category']})")
        if len(filtered) > 5:
            log.info(f"… и ещё {len(filtered) - 5}")
    log.info("─" * 70)

    # Стоп-касса ДО первой отправки. Живой случай 2026-10-07: боевой прогон
    # шёл с Windows, вебхук — с сервера, в другую базу. 30 сообщений принято,
    # 0 подтверждений, каскад молчал — и через 12 ч молчания принятые сообщения
    # ушли бы в откат, то есть в личные аккаунты.
    _guard_delivery_signal(channels)

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
                    f"Сторож подтверждений вернул {_swept} контактов в rescue"
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
                f"ДЛИННЫЙ ПРОГОН: {run_hours:g}ч от старта "
                f"(дедлайн {_dl_local.strftime('%H:%M')} МСК)"
            )
            log.info(
                "Планировщик НЕ нужен: sender сам перезапускает фазы, "
                "пока не истечёт время или не кончится база."
            )
            log.info("=" * 70)

        all_phase_stats: dict[str, int] = {}
        while True:
            # Каждый круг пересобирает список контактов: подхватываются новые,
            # уже отправленные отсекаются по БД.
            cycle_filtered = _select_contacts_for_cycle()
            cycle_stats = _run_all_phases(
                cycle_filtered, stop_requested, backoffs, permanent_skipped,
                cycle_no=(_RUN_CYCLE + 1),
            )
            for _ch, _n in cycle_stats.items():
                all_phase_stats[_ch] = all_phase_stats.get(_ch, 0) + _n
            _RUN_CYCLE += 1

            if run_hours <= 0:
                # Последняя фаза круга — разведка MAX. Только здесь, и
                # только под конец: к этому моменту уже известно, у кого нет
                # ни Telegram (проба фазы 1), ни WhatsApp (провалы WABA).
                # MAX жёстче Telegram по санкциям, а проверка номера там —
                # отдельная активность с потолком 10–20/день, поэтому
                # проверять надо точечно, а не весь пул.
                if "max" in _probe_channels() and not _max_probe_done_for_run():
                    _run_probe_phase(stop_requested, phase_stats, channel="max")
                    _mark_max_probe_done_for_run()
                break
            left = _run_seconds_left()
            if left <= 0:
                log.info("Дедлайн длинного прогона достигнут — выходим.")
                break
            if stop_requested["v"]:
                break
            cur_chk = state.reset_daily_if_new_day(state.load_state())
            if state.max_attempts_reached(cur_chk):
                log.info(
                    "SAFETY CAP — больше попыток сегодня делать нельзя."
                )
                break

            # Ничего не отправилось? Не гоняем пустые круги впустую.
            if not any(cycle_stats.values()):
                log.info(
                    f"Круг отработал вхолостую (база/очередь пусты), жду "
                    f"{loop_pause:g} мин до следующего"
                )
            _sleep_phase(min(loop_pause, left) * 60, stop_requested)

        phase_stats = all_phase_stats
        log.info("─" * 70)
        log.info(f"Фазы отработаны (кругов: {_RUN_CYCLE}): "
                 f"{phase_stats or '—'}")
        # Отчёт именно по ЭТОМУ прогону. Накопленный профиль пула отвечает на
        # другой вопрос и в финале прогону только мешает: владелец видел
        # «есть Telegram 267 / всего 4442» после отправки 2 сообщений и не
        # понимал, куда делись его контакты.
        try:
            from . import probe as _probe
            # _RUN_STARTED_AT хранится как unix-время, а SQL ждёт ISO.
            _since = datetime.fromtimestamp(
                _RUN_STARTED_AT, tz=timezone.utc
            ).isoformat() if _RUN_STARTED_AT else None
            _probe.report_run_summary(_since)
        except Exception as _rse:
            log.debug(f"отчёт по прогону не отработал: {_rse}")
        # Поимённый отчёт по доставке — то, чего в логах раньше не было.
        try:
            _report_all_channels_delivery()
        except Exception as _re:
            log.debug(f"отчёт по контактам не отработал: {_re}")
        try:
            log.info(
                f"Очередь на SMS (осталось pending): "
                f"{db.count_pending_rescues()}"
            )
        except Exception:
            pass

        # Кого за день не дотянули — фиксируем для будущей отправки через
        # SMS (решение владельца 2026-10-09). Список НЕ отправляется сам:
        # SMS платный, и решение принимается отдельно. Идемпотентно, повторный
        # прогон строк не размножает.
        try:
            from .store import pending_sms as _psms
            _added = _psms.collect_today()
            _info = _psms.summary()
            log.info(
                f"Список для SMS (pending_sms): +{_added} за сегодня, "
                f"всего в ожидании "
                f"{_info['by_status'].get('pending', 0)} — "
                f"см. py main.py pending-sms"
            )
        except Exception as _pe:
            log.warning(f"список для SMS не наполнен: {_pe}")
        log.info("─" * 70)

    finally:
        # Всегда освобождаем lock, даже при exception/KeyboardInterrupt.
        # Если бы не finally, повторный запуск считал бы "sender уже идёт".
        _release_lock(lock_path)
        log.debug("Lock-файл освобождён")

    log.info("=" * 70)
    log.info("SENDER LOOP FINISHED.")
    final_state = state.load_state()
    log.info(f"Итог за сегодня: {final_state.get('sent_today', {})}")
    log.info(f"Всего активных каналов было: {channels}")

    # Честная воронка: принято ≠ доставлено. Источник истины — daily_counters
    # в SQLite, а не state.json: два процесса (sender и webhook) писали
    # счётчики в один JSON-файм методом read-modify-write и теряли обновления
    # друг друга — из-за этого отчёт показывал «доставлено 60 (128%)» при
    # принятых 47.
    try:
        _counters = db.get_counters()
        _acc = sum(c["accepted"] for c in _counters.values())
        _dlv = sum(c["delivered"] for c in _counters.values())
        _rjt = sum(c["rejected"] for c in _counters.values())
        _unk = sum(c["unknown"] for c in _counters.values())
        _rate = (_dlv / _acc * 100.0) if _acc else 0.0
        _tgt = int(final_state.get("target_today", config.TARGET_SUCCESS_PER_DAY))
        log.info("─" * 70)
        log.info("ВОРОНКА ДОСТАВКИ (источник истины — SQLite):")
        log.info(f"принято транспортом : {_acc}")
        log.info(f"доставлено (webhook) : {_dlv} ({_rate:.0f}%)")
        log.info(f"отказов (явных) : {_rjt}")
        log.info(f"молчание (unknown) : {_unk} → откат через "
                 f"{config.UNKNOWN_GRACE_MINUTES} мин")
        log.info(f"цель дня : {_tgt}")
        for _ch, _c in sorted(_counters.items()):
            log.info(
                f"· {_ch}: принято {_c['accepted']}, "
                f"доставлено {_c['delivered']}, отказ {_c['rejected']}, "
                f"неизвестно {_c['unknown']}"
            )
        # Сверка счётчика с фактом. Живой случай 2026-10-07: в воронке было
        # «telegram: принято 5», хотя строк channel='telegram' в messages за
        # день не было ни одной — квота личного канала списывается, отправок
        # нет. Молча такое расхождение выглядит как правда, а владелец на
        # этих цифрах решает, запускать ли откат.
        try:
            with db.db_conn() as _conn:
                _facts = {
                    r["channel"]: int(r["n"])
                    for r in _conn.execute(
                        """
                        SELECT channel, COUNT(*) n FROM messages
                        WHERE date(sent_at) = date('now')
                          AND status != 'failed'
                          AND sent_at IS NOT NULL
                        GROUP BY 1
                        """
                    )
                }
            _drift = []
            for _ch, _c in sorted(_counters.items()):
                if _ch in (getattr(config, "TG_HIDE_CHANNELS", ()) or ()):
                    continue
                _fact = _facts.get(_ch, 0)
                if int(_c["accepted"]) != _fact:
                    _drift.append(
                        f"{_ch}: счётчик {_c['accepted']} ≠ строк в БД {_fact}")
            if _drift:
                log.warning(
                    "Расхождение счётчика с фактом (квота может списываться "
                    "за несуществующие отправки): " + "; ".join(_drift)
                )
        except Exception as _de:
            log.debug(f"сверка счётчика с фактом не отработала: {_de}")
        log.info("Полная воронка: `py main.py report`")
        log.info("─" * 70)
    except Exception as e:
        log.warning(f"delivery funnel summary failed: {e}")
        _counters, _acc, _dlv, _rjt, _unk = {}, 0, 0, 0, 0

    # Прозрачность: итог прогона в Telegram — по каналам, с откатом и
    # исчерпанными. Раньше уходило две строки («итог за сегодня» и «каналов»),
    # из которых нельзя было понять, что реально дошло.
    try:
        from . import notify as _notify
        _notify.summary(
            counters=_counters,
            rescue_pending=db.count_pending_rescues(),
            exhausted=db.count_exhausted_rescues(),
            elapsed_sec=(time.time() - _RUN_STARTED_AT) if _RUN_STARTED_AT else 0.0,
            extra=f"принято {_acc} / доставлено {_dlv} / отказ {_rjt}",
        )
    except Exception as e:
        log.debug(f"notify.summary не отправился: {e}")
    log.info("=" * 70)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("\n Прервано пользователем (Ctrl+C). Все отправленные сообщения "
                 "уже сохранены в базе. Повторный запуск продолжит с того же места.")
    except Exception as e:
        log.error(f"Fatal: {e}")
        raise
