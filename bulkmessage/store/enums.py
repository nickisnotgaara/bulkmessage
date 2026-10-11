"""Единый словарь предметной области: каналы, вердикты, состояния.

Зачем этот модуль
-----------------
До него значения вроде ``"delivered"`` или ``"telegram"`` повторялись
строковыми литералами по всему коду — ``'delivered'`` встречался в 18 строках,
``'failed'`` в 19. Любая опечатка (``'deliverd'``) не падала, а молча
портила данные: в отчёте контакт просто исчезал из воронки, и никто не
понимал почему.

Теперь значение определяется ОДИН раз, валидируется при создании и хранится
в базе с CHECK-ограничением. Опечатка ловится на границе модуля, а не через
месяц в чужом отчёте.

Использование::

    from bulkmessage.db.enums import Channel, DeliveryState

    Channel.TELEGRAM          # 'telegram'
    DeliveryState("delivered")  # DeliveryState.DELIVERED
    DeliveryState("delivred")  # ValueError — сразу, на границе
"""

from __future__ import annotations

from enum import Enum


class _StrEnum(str, Enum):
    """Строковый enum: хранится в SQLite как обычный TEXT без адаптеров."""

    def __str__(self) -> str:  # pragma: no cover - тривиально
        return str(self.value)

    @classmethod
    def values(cls) -> list[str]:
        """Все допустимые значения — для построения CHECK-ограничений в DDL."""
        return [member.value for member in cls]

    @classmethod
    def parse(cls, raw: str | None, default: "_StrEnum | None" = None):
        """Мягкий разбор: None/пустое → default, мусор → ValueError.

        Значение None означает «не задано» (например, ещё не наступило время),
        и это НЕ ошибка. Неизвестная строка — ошибка, потому что она означает
        рассинхрон кода и данных.
        """
        if raw is None or raw == "":
            return default
        try:
            return cls(raw)
        except ValueError as exc:
            allowed = ", ".join(cls.values())
            raise ValueError(
                f"{raw!r} — недопустимое значение {cls.__name__}. Допустимо: {allowed}"
            ) from exc


class Channel(_StrEnum):
    """Каналы доставки. Порядок каскада задан в config.PHASE_ORDER."""

    WABA = "waba"          # WhatsApp Business API через Meta
    TELEGRAM = "telegram"  # личный аккаунт Telegram
    MAX = "max"            # личный аккаунт MAX
    WHATSAPP = "whatsapp"  # личный аккаунт WhatsApp (legacy, BULK_USE_WABA=0)
    SMS = "sms"            # платный SMS через sms_ru


class Verdict(_StrEnum):
    """Канонический вердикт по паре (контакт, канал).

    Отвечает на вопрос «что мы знаем об этом человеке в этом канале» и
    хранится в ``contact_channel_state``.
    """

    UNKNOWN = "unknown"      # данных нет
    PRESENT = "present"      # аккаунт есть (проба прошла, сообщение ушло)
    ABSENT = "absent"        # аккаунта нет — квота НЕ потрачена
    DELIVERED = "delivered"  # доставка подтверждена вебхуком
    REJECTED = "rejected"    # явный отказ канала
    BLOCKED = "blocked"      # жалоба / спам-отказ

    @classmethod
    def is_actionable(cls, value: "Verdict | str") -> bool:
        """Вердикт, по которому контакт ИМЕЕТ СМЫСЛ слать в откат.

        Только ``PRESENT`` и ``DELIVERED``: аккаунт подтверждён. Остальное
        (нет аккаунта, жалоба, неизвестно) в откате лишь сожжёт личный лимит.
        """
        return Verdict.parse(value) in (cls.PRESENT, cls.DELIVERED)


class DeliveryState(_StrEnum):
    """Трёхзначное состояние доставки — единственный честный сигнал.

    201 от Wazzup означает «запрос принят», а не «доставлено». Настоящий вердикт
    приходит вебхуком и попадает сюда.
    """

    UNKNOWN = "unknown"      # подтверждения нет — НЕ значит «доставлено»
    DELIVERED = "delivered"  # вебхук подтвердил доставку
    REJECTED = "rejected"    # вебхук/эхо сообщили об отказе


class MessageStatus(_StrEnum):
    """Жизненный цикл попытки отправки в ``messages``."""

    QUEUED = "queued"
    SENT = "sent"          # принято транспортом (ещё не доставлено)
    DELIVERED = "delivered"
    READ = "read"
    ANSWERED = "answered"  # человек ответил
    DELETED = "deleted"    # сообщение удалено у получателя
    FAILED = "failed"      # отправка не состоялась
    EXHAUSTED = "exhausted"  # контакт исчерпан (достигнут лимит попыток)


class RescueStatus(_StrEnum):
    """Состояние записи в очереди отката (``rescue_queue``).

    Список соответствует тому, что реально пишет код, — ни больше, ни меньше.
    ``exhausted`` — контакт исчерпал попытки по всем доступным каналам;
    ``failed`` — попытка не удалась, но контакт ещё может вернуться.
    """
    PENDING = "pending"      # ждёт следующего канала
    DEFERRED = "deferred"    # отложен (cooldown / не время)
    RESCUED = "rescued"      # доставлен в следующем канале
    FAILED = "failed"        # последняя попытка не удалась
    EXHAUSTED = "exhausted"  # исчерпаны все каналы — терминально
    CANCELLED = "cancelled"  # снят вручную


class Consent(_StrEnum):
    """Состояние согласия на рассылку (``contacts.consent``).

    Три состояния, не два. Отсутствие данных — это НЕ «отказ»: 847 из 848
    контактов имеют ``UNKNOWN``, потому что согласие никогда не фиксировалось.
    Записать им ``DENIED`` значило бы выдумать 847 отписок, которых не было.

    Политика холодной рассылки задаётся ``config.CONSENT_REQUIRED_FOR_COLD``:
    ``0`` — писать можно ``GRANTED`` и ``UNKNOWN``, ``1`` — только ``GRANTED``.
    ``DENIED`` запрещает отправку всегда, независимо от флага.
    """

    GRANTED = "granted"
    UNKNOWN = "unknown"
    DENIED = "denied"

    @classmethod
    def sendable_values(cls, require_opt_in: bool) -> list[str]:
        """Значения, которые разрешают отправку при текущей политике."""
        if require_opt_in:
            return [cls.GRANTED.value]
        return [cls.GRANTED.value, cls.UNKNOWN.value]


class ComplaintKind(_StrEnum):
    """Основание для автоматической остановки канала."""

    SPAM = "spam"
    BLOCK = "block"
    OTHER = "other"


# Поколение схемы. Bump при каждом структурном изменении: мигратор обязан
# дойти до этого числа при первом же запуске, иначе БД считается устаревшей.
SCHEMA_VERSION = 10

__all__ = [
    "Channel",
    "Verdict",
    "DeliveryState",
    "MessageStatus",
    "RescueStatus",
    "Consent",
    "ComplaintKind",
    "SCHEMA_VERSION",
]