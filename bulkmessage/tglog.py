"""Telegram bot logger: notifications to admin via bot.

Архитектура:
  - Очередь сообщений (queue.Queue) — чтобы main loop не блокировался на HTTP.
  - Фоновый daemon-thread, который дренирует очередь с rate-limit.
  - `TelegramHandler` — стандартный logging.Handler, форвардит ERROR+ в очередь.
  - `send(text, level)` — manual API для важных событий (start, stop, quota exhausted).
  - Если TG_LOG_BOT_TOKEN/ADMIN_ID не заданы — тихий no-op.

Безопасность:
  - Бот может слать сообщения ТОЛЬКО на chat_id, заданный в TG_LOG_ADMIN_ID.
  - Через sendMessage нельзя ничего execute/admin-only — только текст.
  - Все запросы через HTTPS к api.telegram.org.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime
from typing import Optional

import requests

from . import config


log = config.get_logger("tglog")


_TG_API = "https://api.telegram.org/bot{token}/{method}"
_QUEUE: "queue.Queue[str]" = queue.Queue(maxsize=config.TG_LOG_MAX_QUEUE)
_SEND_INTERVAL = max(0.1, config.TG_LOG_SEND_INTERVAL)
_LAST_SENT = 0.0
_LOCK = threading.Lock()
_WORKER_STARTED = False

# Track last contact_fail to avoid spam (1 message per N contacts)
_LAST_FAIL_REPORT: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Подавление уведомлений на тестовых прогонах
# ---------------------------------------------------------------------------
#
# Реальный случай 2026-10-06: юнит-тест R26 в test_suite.py подставляет
# фикстурный ответ `HTTP 401 Not authorized in channel` (контакт +79995550044).
# sender логирует это как ERROR → TelegramHandler форвардит в боевой
# tglog-бот → владелец получает в личку «🔐 AUTH — Wazzup API key/channel
# невалиден для telegram», хотя никакого запроса в Wazzup не было.
#
# То есть фикстура теста выглядела как настоящая авария канала. Поэтому
# любой тестовый/стендовый прогон (test_*.py, bm_*.py) молчит в Telegram,
# а явный вызов send_test() — работает (это ручное действие человека).

_SUPPRESS_ENV_FLAG = "BULK_TG_LOG_DISABLE"

_TRUE_VALUES = ("1", "true", "yes", "on")


def _entry_script() -> str:
    """Имя скрипта, которым запущен процесс (test_suite.py, main.py...)."""
    try:
        return os.path.basename(sys.argv[0] or "").lower()
    except Exception:
        return ""


def _auto_suppressed() -> bool:
    """True, если процесс — тест/стенд или явно выставлен флаг отключения."""
    flag = (os.environ.get(_SUPPRESS_ENV_FLAG) or "").strip().lower()
    if flag in _TRUE_VALUES:
        return True
    name = _entry_script()
    if name.startswith("test_") or name.startswith("bm_"):
        return True
    return False


_SUPPRESSED = _auto_suppressed()


def is_suppressed() -> bool:
    """True — уведомления в Telegram подавлены (тест/стенд/флаг)."""
    return _SUPPRESSED


def set_suppressed(value: bool) -> None:
    """Переключить подавление во время работы процесса."""
    global _SUPPRESSED
    _SUPPRESSED = bool(value)


def _format_message(text: str, level: str = "INFO") -> str:
    """Форматирует сообщение для Telegram. Лимит Telegram: 4096 символов."""
    if len(text) > 3500:
        text = text[:3500] + "\n…[truncated]"
    icon = {
        "ERROR": "❌",
        "CRITICAL": "💥",
        "WARNING": "⚠️",
        "INFO": "ℹ️",
        "DEBUG": "🔍",
    }.get(level.upper(), "•")
    ts = datetime.now().strftime("%H:%M:%S")
    return f"{icon} [{ts}]\n{text}"


def _send_to_telegram(text: str) -> bool:
    """Один HTTP POST. Возвращает True если успешно."""
    if _SUPPRESSED:
        return False
    if not config.TG_LOG_ENABLED:
        return False
    try:
        resp = requests.post(
            _TG_API.format(token=config.TG_LOG_BOT_TOKEN, method="sendMessage"),
            json={
                "chat_id": config.TG_LOG_ADMIN_ID,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code == 429:
            # Telegram сказал подождать
            retry = resp.json().get("parameters", {}).get("retry_after", 5)
            log.warning(f"TG log: rate-limited, retry_after={retry}с")
            time.sleep(min(retry, 30))
            return False
        if resp.status_code != 200:
            log.warning(
                f"TG log: HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return False
        return True
    except requests.RequestException as e:
        log.warning(f"TG log: network error: {e}")
        return False


def _worker() -> None:
    """Фоновый поток: дренирует очередь, шлёт с rate-limit."""
    global _LAST_SENT
    while True:
        try:
            text = _QUEUE.get()
            with _LOCK:
                wait = _SEND_INTERVAL - (time.time() - _LAST_SENT)
                if wait > 0:
                    time.sleep(wait)
            _send_to_telegram(text)
            with _LOCK:
                _LAST_SENT = time.time()
        except Exception as e:
            log.warning(f"TG log worker: {type(e).__name__}: {e}")


def _start_worker() -> None:
    """Запускает фоновый поток (один раз за процесс)."""
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    if _SUPPRESSED:
        return
    if not config.TG_LOG_ENABLED:
        return
    t = threading.Thread(target=_worker, name="tglog-worker", daemon=True)
    t.start()
    _WORKER_STARTED = True
    log.info(
        f"TG log: worker started (admin={config.TG_LOG_ADMIN_ID}, "
        f"interval={_SEND_INTERVAL}с, level={config.TG_LOG_LEVEL})"
    )


def send(text: str, level: str = "INFO") -> None:
    """Отправить произвольное сообщение админу (неблокирующе).

    Если TG_LOG_ENABLED=False — no-op. Если очередь переполнена — drop + warn.
    На тестовых прогонах (test_*.py / bm_*.py / BULK_TG_LOG_DISABLE=1) —
    тоже no-op: фикстурные ошибки не должны прилетать в личку владельца.
    """
    if _SUPPRESSED:
        return
    if not config.TG_LOG_ENABLED:
        return
    msg = _format_message(text, level)
    try:
        _QUEUE.put_nowait(msg)
    except queue.Full:
        log.warning("TG log queue full, message dropped")
    _start_worker()


def send_test() -> Optional[dict]:
    """Прямая отправка тестового сообщения (синхронно).

    Возвращает response.json() или None при ошибке.
    Используется для smoke-test: проверить что токен и admin_id рабочие.
    """
    if not config.TG_LOG_ENABLED:
        return None
    try:
        resp = requests.post(
            _TG_API.format(token=config.TG_LOG_BOT_TOKEN, method="sendMessage"),
            json={
                "chat_id": config.TG_LOG_ADMIN_ID,
                "text": "🧪 Тест: бот bulkmessage подключен. Если ты это видишь — всё работает.",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
        log.warning(f"TG log send_test: HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    except requests.RequestException as e:
        log.warning(f"TG log send_test: {e}")
        return None


def get_me() -> Optional[dict]:
    """Запрос /getMe к Telegram — для проверки валидности токена.

    Возвращает dict {id, is_bot, first_name, username} или None при ошибке.
    """
    if not config.TG_LOG_BOT_TOKEN:
        return None
    try:
        resp = requests.get(
            _TG_API.format(token=config.TG_LOG_BOT_TOKEN, method="getMe"),
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("result")
        log.warning(f"TG log getMe: HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    except requests.RequestException as e:
        log.warning(f"TG log getMe: {e}")
        return None


# ---------------------------------------------------------------------------
# logging.Handler — форвардит ERROR+ в очередь
# ---------------------------------------------------------------------------


class TelegramHandler(logging.Handler):
    """logging.Handler, который кладёт записи в очередь TG-уведомлений.

    Уровень задаётся через config.TG_LOG_LEVEL (по умолчанию ERROR).
    Форвардит только имя логгера, уровень и текст сообщения — никакого секрета.
    """

    _LEVEL_MAP = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }

    def __init__(self) -> None:
        super().__init__(level=self._LEVEL_MAP.get(config.TG_LOG_LEVEL, logging.ERROR))
        self.setFormatter(
            logging.Formatter("%(name)s | %(message)s")
        )
        _start_worker()

    def emit(self, record: logging.LogRecord) -> None:
        if _SUPPRESSED:
            return
        try:
            text = self.format(record)
            msg = _format_message(text, record.levelname)
            try:
                _QUEUE.put_nowait(msg)
            except queue.Full:
                # Не спамим warning в stdout, иначе рекурсия
                pass
        except Exception:
            self.handleError(record)


def install_handler() -> bool:
    """Подключает TelegramHandler к root logger (если TG_LOG_ENABLED).

    Возвращает True если подключён, False если no-op.
    """
    if not config.TG_LOG_ENABLED:
        return False
    root = logging.getLogger()
    # Не дублируем если уже стоит
    for h in root.handlers:
        if isinstance(h, TelegramHandler):
            return True
    root.addHandler(TelegramHandler())
    if _SUPPRESSED:
        # Handler подключён (идемпотентность, тесты), но emit() молчит.
        log.info(
            "TG log: уведомления ПОДАВЛЕНЫ (тестовый/стендовый прогон) — "
            "в Telegram ничего не уйдёт"
        )
    else:
        log.info("TG log: TelegramHandler подключён к root logger")
    return True
