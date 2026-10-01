"""SMS.RU API client — fallback-канал последней надежды.

SMS — ОТДЕЛЬНЫЙ транспорт (не Wazzup24). Используется только когда все
мессенджеры (WABA / Telegram / MAX) провалились для контакта.

API reference (https://sms.ru/api/send, /api/status):
- POST /sms/send   — отправка, form-encoded
- POST /sms/status — проверка статусов по sms_id
- Auth: параметр `api_id` (альтернатива: login+password)

Ключевые особенности SMS.RU против мессенджеров:
1. Цена зависит от длины: 70 символов = 1 сегмент (~8.4-8.5 ₽). Длинные
   мессенджерные тексты (330-380 символов) = 5-6 сегментов = 42-51 ₽.
   Поэтому SMS использует ОТДЕЛЬНЫЕ короткие шаблоны.
2. Ошибка 217 — если текст читается как реклама кредита, но не содержит
   фразу «Оцените свои финансовые возможности и риски», сообщение отвергается.
3. Ошибка 221 — нужен зарегистрированный буквенный отправитель (alphanumeric).
4. Ошибка 502 — IP сервера из другой страны, чем получатели (риск для нас:
   хост в Узбекистане, получатели в РФ).
5. `test=1` — сервис ничего не отправляет и не списывает баланс.

Retry policy: сетевые ошибки/5xx → 1 retry, всё остальное — на классификацию.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from . import config
from .wazzup import ErrorKind


log = logging.getLogger("sms_ru")


# Один сегмент SMS = 70 символов (кириллица, 7-bit packing).
CHARS_PER_SEGMENT = 70
# Абсолютный лимит SMS.RU — 8 сегментов (иначе ошибка 205).
SMSRU_HARD_MAX_SEGMENTS = 8
# Абсолютный лимит получателей на один HTTP-запрос.
SMSRU_MAX_RECIPIENTS = 100

# Таймаут HTTP-запроса (секунды).
_HTTP_TIMEOUT = 30
_RETRY_MAX = 1
_RETRY_DELAY_MIN = 1.5
_RETRY_DELAY_MAX = 3.0

# Субэндпоинты SMS.RU.
_SEND_PATH = "/sms/send"
_STATUS_PATH = "/sms/status"


# ---------------------------------------------------------------------------
# Коды статусов SMS.RU (см. https://sms.ru/api/send#codes)
# ---------------------------------------------------------------------------

# Принято в очередь / в пути — сообщение ушло, ждём доставку.
ACCEPTED_CODES = frozenset({100, 101, 102})
# Доставлено.
DELIVERED_CODES = frozenset({103})
# Прочитано (только Viber, для SMS не применимо).
READ_CODES = frozenset({110})

# Ошибки запроса: сообщение НЕ отправлено, деньги НЕ списаны.
_ERR_AUTH = frozenset({200, 204, 221, 300, 301, 302, 303, 304, 305})
_ERR_RATE = frozenset({201, 206, 230, 231, 232, 233})
_ERR_TRANSIENT = frozenset({220, 500, 501, 502, 503, 504, 505, 506, 507, 508, 509, 550})
_ERR_PERMANENT = frozenset({
    -1,   # сообщение не найдено
    202,  # неверный номер получателя
    203,  # нет текста сообщения
    205,  # слишком длинное (>8 SMS)
    207,  # нет маршрута доставки на этот номер
    209,  # номер в вашем стоп-листе
    210,  # GET вместо POST
    211,  # метод не найден
    212,  # текст не в UTF-8
    213,  # больше 5000 получателей
    214,  # номер зарубежный (при включённой настройке «только РФ»)
    215,  # номер в стоп-листе SMS.RU (получатель жаловался на спам)
    216,  # в тексте запрещённое слово
    217,  # реклама кредита без обязательной фразы
    222,  # оператор работает только по шаблонам
})

# Коды доставки, означающие, что сообщение НЕ дошло. 104/105/108 дополнительно
# трактуем как жалобу на спам (оператор удалил / отклонил).
FAILED_CODES = frozenset({104, 105, 106, 107, 108, 150})
SPAM_SIGNAL_CODES = frozenset({104, 105, 108})

# Фраза, которую SMS.RU требует для рекламы кредита/инвестиций (ошибка 217).
CREDIT_DISCLAIMER = "Оцените свои финансовые возможности и риски"


def is_configured() -> bool:
    """True если SMS-канал можно использовать (есть api_id и имя отправителя).

    Без обоих SMS.RU отвечает 221 — нет смысла даже пытаться.
    """
    return bool(config.SMSRU_API_ID) and bool(config.SMSRU_SENDER_NAME)


def normalize_phone(phone: str) -> str:
    """Приводит номер к формату SMS.RU: 11 цифр, начинается с 7, без '+'.

    '+7 (925) 507-06-02' -> '79255070602'
    '8 925 507 06 02'    -> '79255070602'
    '79255070602'       -> '79255070602'
    мусор               -> ''

    Пустая строка означает «невалидный номер» — caller обязан обработать
    это как PERMANENT и НЕ слать запрос (номер просто не существует).
    """
    if not phone:
        return ""
    digits = re.sub(r"\D", "", str(phone))
    if not digits:
        return ""
    # Российские номера: 7XXXXXXXXXX (11), 8XXXXXXXXXX (11) или 10XXXXXXXX (10).
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits[0] == "9":
        # 10 цифр без кода страны — подставляем 7 (номер начинается с 9).
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        return ""
    return digits


def estimate_segments(text: str) -> int:
    """Сколько сегментов займёт текст (1 сегмент = 70 символов)."""
    if not text:
        return 0
    return max(1, (len(text) + CHARS_PER_SEGMENT - 1) // CHARS_PER_SEGMENT)


def exceeds_segment_budget(text: str) -> bool:
    """True если текст не влезает в наш бюджет сегментов.

    Бюджет берётся из config.SMSRU_MAX_SEGMENTS (по умолчанию 2), но никогда
    не превышает жёсткий лимит SMS.RU (8 сегментов).
    """
    budget = min(config.SMSRU_MAX_SEGMENTS, SMSRU_HARD_MAX_SEGMENTS)
    return estimate_segments(text) > budget


def has_credit_disclaimer(text: str) -> bool:
    """True если в тексте есть обязательная фраза для рекламы кредита."""
    return CREDIT_DISCLAIMER.lower() in (text or "").lower()


def classify_smsru_error(
    status_code: Optional[int], status_text: str = ""
) -> ErrorKind:
    """Классифицирует код ответа SMS.RU в общий ErrorKind проекта.

    Маппинг соответствует таблице в плане: 200/204/221/3xx → AUTH (проблема
    аккаунта, повтор бессмысленен); 201/206/230-233 → RATE_LIMIT (лимиты);
    220/5xx → TRANSIENT (сервис моргнул, можно повторить); 202/205/207/209/
    215/216/217/222 и прочие → PERMANENT (номер/текст/настройка сломаны).
    """
    if status_code is None:
        return ErrorKind.TRANSIENT
    if status_code in ACCEPTED_CODES or status_code in DELIVERED_CODES:
        # Не ошибка — сообщение принято.
        return ErrorKind.UNKNOWN
    if status_code in _ERR_AUTH:
        return ErrorKind.AUTH
    if status_code in _ERR_RATE:
        return ErrorKind.RATE_LIMIT
    if status_code in _ERR_TRANSIENT:
        return ErrorKind.TRANSIENT
    if status_code in _ERR_PERMANENT:
        return ErrorKind.PERMANENT
    # Неизвестный код — считаем транзиентным, чтобы не выкинуть контакт
    # из воронки из-за недокументированного кода.
    log.warning(f"⚠️ SMS.RU вернул неизвестный код {status_code}: {status_text[:80]}")
    return ErrorKind.TRANSIENT


def is_auth_like_error(status_code: Optional[int]) -> bool:
    """True если код означает проблему уровня АККАУНТА (а не контакта).

    Такие ошибки требуют halt канала: повтор только выжигает дневную квоту.
    """
    return status_code in _ERR_AUTH


def _post_form(path: str, params: dict[str, Any], timeout: int = _HTTP_TIMEOUT):
    """POST form-encoded с одним retry на сетевую ошибку/5xx.

    Возвращает (parsed_json_or_None, raw_text, http_status). Исключения
    пробрасываются как (None, error_string, None).
    """
    url = f"{config.SMSRU_BASE_URL}{path}"
    body = urlencode(params).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")

    last_err: Optional[str] = None
    for attempt in range(_RETRY_MAX + 1):
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = resp.getcode()
                try:
                    return json.loads(raw), raw, status
                except json.JSONDecodeError:
                    return None, raw, status
        except HTTPError as e:
            # SMS.RU может отдавать ошибки с HTTP-кодом != 200, но с полезным JSON.
            raw = ""
            try:
                raw = e.read().decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover - defensive
                pass
            if 500 <= e.code < 600:
                last_err = f"http {e.code}"
                if attempt < _RETRY_MAX:
                    time.sleep(random.uniform(_RETRY_DELAY_MIN, _RETRY_DELAY_MAX))
                    continue
            try:
                return json.loads(raw), raw, e.code
            except json.JSONDecodeError:
                return None, raw or str(e), e.code
        except (URLError, TimeoutError, OSError) as e:
            last_err = f"network: {e}"
            if attempt < _RETRY_MAX:
                time.sleep(random.uniform(_RETRY_DELAY_MIN, _RETRY_DELAY_MAX))
                continue
    return None, last_err or "unknown network error", None


def _parse_send_response(raw_json: Optional[dict], phone: str) -> tuple[
    bool, Optional[str], str, Optional[int]
]:
    """Достаёт из ответа SMS.RU результат по ОДНОМУ номеру.

    Возвращает (ok, sms_id, detail, status_code) в том же формате, что и
    wazzup.send_wazzup / send_to_channel, чтобы sender.py обрабатывал
    оба транспорта одинаково.
    """
    if not isinstance(raw_json, dict):
        return False, None, "empty or non-JSON response", None

    top_status = raw_json.get("status_code")
    # Ошибка уровня запроса (даже если sms{} пуст).
    if raw_json.get("status") != "OK":
        text = raw_json.get("status_text") or raw_json.get("status") or "request rejected"
        return False, None, f"sms.ru {top_status}: {text}", top_status

    per_phone = (raw_json.get("sms") or {}).get(phone) or {}
    sms_status = per_phone.get("status")
    sms_code = per_phone.get("status_code", top_status)

    if sms_status == "OK":
        sms_id = per_phone.get("sms_id")
        balance = raw_json.get("balance")
        detail = f"sms.ru {sms_code} accepted"
        if balance is not None:
            detail += f", balance={balance}"
        return True, sms_id, detail, sms_code

    text = per_phone.get("status_text") or f"sms.ru {sms_code}"
    return False, None, f"sms.ru {sms_code}: {text}", sms_code


def send_sms(
    phone: str,
    text: str,
    *,
    sender: Optional[str] = None,
    test: Optional[bool] = None,
) -> tuple[bool, Optional[str], str, Optional[int]]:
    """Отправляет одну SMS через SMS.RU.

    Возвращает (ok, sms_id, detail, status_code) — тот же контракт, что у
    wazzup-транспорта, чтобы каскад в sender.py работал единообразно.

    Если config.DRY_RUN=True — НИ ОДНОГО сетевого вызова не происходит,
    возвращается имитация успеха (последняя линия обороны перед боевым запуском).
    """
    if config.DRY_RUN:
        sid = f"dryrun_{int(config.now_tz().timestamp())}_sms"
        return True, sid, "[DRY-RUN] SMS.RU API НЕ вызывался", 100

    normalized = normalize_phone(phone)
    if not normalized:
        return False, None, f"phone normalization failed: {phone!r}", 202

    if not text:
        return False, None, "empty message text", 203

    # Локальная проверка длины: экономим платный (даже если отвергнутый) вызов
    # и сразу показываем проблему шаблона в логах.
    if exceeds_segment_budget(text):
        segs = estimate_segments(text)
        return (
            False,
            None,
            f"text too long: {segs} segments (budget "
            f"{min(config.SMSRU_MAX_SEGMENTS, SMSRU_HARD_MAX_SEGMENTS)}, "
            f"{len(text)} chars)",
            205,
        )

    params: dict[str, Any] = {
        "api_id": config.SMSRU_API_ID,
        "to": normalized,
        "msg": text,
        "json": 1,
    }
    sender_name = sender if sender is not None else config.SMSRU_SENDER_NAME
    if sender_name:
        params["from"] = sender_name
    use_test = config.SMSRU_TEST_MODE if test is None else test
    if use_test:
        params["test"] = 1

    raw_json, raw_text, http_status = _post_form(_SEND_PATH, params)
    ok, sms_id, detail, status_code = _parse_send_response(raw_json, normalized)
    if not ok and raw_json is None:
        # Сетевая/5xx ошибка — классифицируем по http_status, иначе TRANSIENT.
        status_code = status_code if status_code is not None else None
    return ok, sms_id, detail, status_code


def get_balance() -> Optional[float]:
    """Текущий баланс SMS.RU или None (если не удалось получить).

    SMS.RU не имеет отдельного эндпоинта баланса; баланс приходит в ответе
    на любой запрос. Делаем безопасный test-запрос (test=1) и читаем поле balance.
    """
    if not config.SMSRU_API_ID:
        return None
    raw_json, _, _ = _post_form(
        _SEND_PATH,
        {"api_id": config.SMSRU_API_ID, "to": "79000000000", "msg": "balance", "json": 1, "test": 1},
    )
    if isinstance(raw_json, dict):
        bal = raw_json.get("balance")
        if isinstance(bal, (int, float)):
            return float(bal)
    return None


def poll_status(sms_ids: list[str]) -> dict[str, dict]:
    """Проверяет статусы доставки по списку sms_id (до 100 за раз).

    Возвращает {sms_id: {"status_code": int, "status_text": str, "cost": float|None}}.
    Нужен для DLR, потому что SMS.RU не пушит статусы без callback-регистрации.
    """
    if not sms_ids or not config.SMSRU_API_ID:
        return {}
    ids = [i for i in sms_ids if i][:SMSRU_MAX_RECIPIENTS]
    if not ids:
        return {}

    raw_json, _, _ = _post_form(
        _STATUS_PATH,
        {"api_id": config.SMSRU_API_ID, "sms_id": ",".join(ids), "json": 1},
    )
    if not isinstance(raw_json, dict):
        return {}

    result: dict[str, dict] = {}
    for sms_id, data in (raw_json.get("sms") or {}).items():
        if not isinstance(data, dict):
            continue
        result[sms_id] = {
            "status_code": data.get("status_code"),
            "status_text": data.get("status_text", ""),
            "cost": data.get("cost"),
        }
    return result


def map_status_to_db(status_code: Optional[int]) -> str:
    """Переводит код статуса SMS.RU в наш словарь db.messages.status.

    100/101/102 -> queued, 103 -> delivered, 104-108/150 -> failed.
    """
    if status_code in DELIVERED_CODES:
        return "delivered"
    if status_code in ACCEPTED_CODES or status_code in READ_CODES:
        return "queued"
    if status_code in FAILED_CODES:
        return "failed"
    if status_code is None:
        return "failed"
    return "queued"


def is_spam_signal(status_code: Optional[int]) -> bool:
    """True если код доставки указывает на жалобу/блокировку (104/105/108).

    SMS.RU не отдаёт webhook жалоб, поэтому эти коды — наш proxy для
    complaint-сигнала: оператор удалил или отклонил сообщение.
    """
    return status_code in SPAM_SIGNAL_CODES
