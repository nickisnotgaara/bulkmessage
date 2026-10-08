"""Google Sheets integration."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config


try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSHEET_AVAILABLE = True
except ImportError:  # pragma: no cover
    gspread = None  # type: ignore
    Credentials = None  # type: ignore
    GSHEET_AVAILABLE = False


class SheetsManager:
    """Thread-safe Google Sheets manager."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._worksheet = None
        self._client = None
        self._enabled = False
        self._last_error: Optional[str] = None
        self._header_initialized = False
        self._row_cache: dict[tuple[str, str, str], int] = {}
        self._init_client()

    def _init_client(self) -> None:
        if not GSHEET_AVAILABLE:
            self._log().warning("gspread/google-auth недоступны — Google Sheets отключены")
            return
        if not config.GOOGLE_SHEET_ID:
            self._log().warning("BULK_GOOGLE_SHEET_ID не задан — Google Sheets отключены")
            return
        cred_path = Path(config.GOOGLE_CREDENTIALS_JSON)
        if not cred_path.exists():
            self._log().warning(f"Файл учётных данных Google не найден: {cred_path}")
            return
        try:
            scope = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]
            creds = Credentials.from_service_account_file(str(cred_path), scopes=scope)
            self._client = gspread.authorize(creds)
            self._enabled = True
            self._log().info("Google Sheets: авторизация прошла успешно")
        except Exception as e:
            self._log().error(f"Google Sheets: ошибка авторизации: {e}")
            self._enabled = False

    @staticmethod
    def _log():
        return config.get_logger("sheets")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _get_worksheet(self):
        if not self._enabled:
            return None
        if self._worksheet is not None:
            return self._worksheet
        try:
            sh = self._client.open_by_key(config.GOOGLE_SHEET_ID)
        except gspread.SpreadsheetNotFound as e:
            self._log().error(
                f"Google Sheets: таблица не найдена или нет доступа. "
                f"Проверьте: 1) правильный BULK_GOOGLE_SHEET_ID, "
                f"2) таблица расшарена для {self._client.auth.signer_email if hasattr(self._client.auth, 'signer_email') else 'service account'}. "
                f"Original: {e}"
            )
            self._last_error = f"SpreadsheetNotFound: {e}"
            return None
        except gspread.APIError as e:
            code = getattr(e, "response", None)
            status = code.status_code if code is not None else "?"
            self._log().error(
                f"Google Sheets: API error {status}: {e}. "
                f"Проверьте, что Google Sheets API включён в проекте."
            )
            self._last_error = f"APIError {status}: {e}"
            return None
        except Exception as e:
            import traceback
            self._log().error(
                f"Google Sheets: не удалось открыть таблицу: {type(e).__name__}: {e}\n{traceback.format_exc()}"
            )
            self._last_error = f"{type(e).__name__}: {e}"
            return None
        try:
            try:
                ws = sh.worksheet(config.GOOGLE_WORKSHEET_NAME)
            except gspread.WorksheetNotFound:
                ws = sh.add_worksheet(
                    title=config.GOOGLE_WORKSHEET_NAME,
                    rows=1000,
                    cols=len(config.GSHEET_HEADERS),
                )
            self._worksheet = ws
            return ws
        except Exception as e:
            self._log().error(f"Google Sheets: ошибка worksheet: {e}")
            self._last_error = str(e)
            return None

    def ensure_header(self) -> bool:
        if not self._enabled or self._header_initialized:
            return self._header_initialized
        ws = self._get_worksheet()
        if ws is None:
            return False
        try:
            existing = ws.row_values(1)
            if existing != config.GSHEET_HEADERS:
                ws.update("A1", [config.GSHEET_HEADERS])
            self._header_initialized = True
            return True
        except Exception as e:
            self._log().error(f"Google Sheets: ensure_header ошибка: {e}")
            self._last_error = str(e)
            return False

    @staticmethod
    def _format_dt(iso: Optional[str]) -> str:
        if not iso:
            return ""
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            # Переводим в таймзону из config (по умолчанию Europe/Moscow)
            try:
                from zoneinfo import ZoneInfo
                dt = dt.astimezone(ZoneInfo(config.TIMEZONE_NAME))
            except Exception:
                pass
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return iso

    def build_row_payload(
        self,
        *,
        phone: str,
        name: str,
        category: str,
        channel: str,
        message: str,
        sent_at: Optional[str],
        delivered_at: Optional[str],
        read_at: Optional[str],
        answered_at: Optional[str],
        delivery_state: str,
        status: str,
        last_error: str,
        reply: str,
        last_activity: Optional[str],
        message_id: str,
        contact_id: str,
        consent: str,
    ) -> list[str]:
        """Собирает строку листа.

        Порядок обязан совпадать с ``config.GSHEET_HEADERS``; порядок там и
        здесь задан одним списком, а не числами в разных местах.
        """
        reached = bool(delivered_at or read_at or answered_at
                       or delivery_state == "delivered")
        return [
            phone,
            name,
            category,
            channel,
            message,
            self._format_dt(sent_at),
            self._format_dt(delivered_at),
            self._format_dt(read_at),
            self._format_dt(answered_at),
            "Yes" if reached else "No",
            delivery_state or "unknown",
            status,
            (last_error or "")[:500],
            reply,
            self._format_dt(last_activity),
            message_id or "",
            str(contact_id or ""),
            consent or "unknown",
        ]

    def find_row(self, phone: str, channel: str, message_id: str) -> Optional[int]:
        """Индекс строки в таблице для конкретной ОТПРАВКИ.

        Ключ — (phone, channel, message_id). message_id уникален на каждую
        отправку, поэтому вторая отправка тому же человеку принципиально не
        может совпасть с первой строкой: это не баг поиска, а семантика
        «строка = попытка отправки».

        Кеш в памяти работает только как ускоритель; источник истины — сама
        таблица. Сканирование идёт по ЛОКАЛЬНЫМ значениям, без повторных
        сетевых запросов на каждую строку.
        """
        if not message_id:
            return None
        cache_key = (phone, channel, message_id)
        if cache_key in self._row_cache:
            return self._row_cache[cache_key]
        ws = self._get_worksheet()
        if ws is None:
            return None
        try:
            all_values = ws.get_all_values()
        except Exception as e:
            self._log().error(f"Google Sheets: get_all_values ошибка: {e}")
            self._last_error = str(e)
            return None
        if not all_values:
            return None

        # Индексы берём из заголовка, а не числами в коде. Раньше стояли
        # литералы 0/3/12, и любая перестановка столбцов ломала поиск молча:
        # строка не находилась, отправка дописывалась второй раз.
        header = all_values[0]
        try:
            i_phone = header.index("Phone")
            i_channel = header.index("Channel")
            i_msg_id = header.index("Message ID")
        except ValueError:
            self._log().error(
                "Google Sheets: в заголовке нет Phone/Channel/Message ID — "
                "строка не найдена"
            )
            return None

        need = max(i_phone, i_channel, i_msg_id)
        for idx, row in enumerate(all_values[1:], start=2):
            if len(row) <= need:
                continue
            if (row[i_phone] == phone and row[i_channel] == channel
                    and row[i_msg_id] == message_id):
                self._row_cache[cache_key] = idx
                return idx
        return None

    def append_or_update(
        self,
        *,
        row_data: list[str],
        phone: str,
        channel: str,
        message_id: str,
    ) -> bool:
        if not self._enabled:
            return False
        if not self.ensure_header():
            return False
        with self._lock:
            ws = self._get_worksheet()
            if ws is None:
                return False
            try:
                existing = self.find_row(phone, channel, message_id)
                if existing:
                    ws.update(f"A{existing}", [row_data])
                    self._row_cache[(phone, channel, message_id)] = existing
                    self._log().info(
                        f"Google updated row {existing} for {phone}/{channel} "
                        f"(msg_id={message_id})"
                    )
                    return True

                ws.append_row(row_data, value_input_option="USER_ENTERED")
                # Индекс берём у САМОЙ таблицы, а не считаем как len(get_all_values()):
                # между append и повторным чтением в таблице могут появиться строки
                # (другой процесс, ручная правка), и вычисленный индекс указывал бы
                # на ЧУЖУЮ строку — ws.update() затирал бы чужого контакта.
                # Именно это наблюдалось: gsheet_row 43 был назначен трём разным
                # сообщениям.
                new_idx = int(ws.row_count)
                self._row_cache[(phone, channel, message_id)] = new_idx
                self._log().info(
                    f"Google appended row {new_idx} for {phone}/{channel} "
                    f"(msg_id={message_id})"
                )
                return True
            except Exception as e:
                self._log().error(f"Google Sheets: append_or_update ошибка: {e}")
                self._last_error = str(e)
                return False

    def cached_row(self, phone: str, channel: str, message_id: str) -> Optional[int]:
        return self._row_cache.get((phone, channel, message_id))


SHEETS = SheetsManager()


def sync_message_to_sheet(
    *,
    contact_id: int,
    channel: str,
    status: str,
    message_pk: Optional[int] = None,
) -> bool:
    """Собирает данные по сообщению из БД и отправляет в Google Sheets.

    При неудаче ставит операцию в очередь sheets_ops для реконсиляции.
    """
    from . import db  # local import to avoid circulars

    with db.db_conn() as conn:
        c = conn.cursor()
        if message_pk is not None:
            c.execute(
                """
                SELECT m.*, ct.phone AS phone, ct.name AS name, ct.category AS category,
                       ct.consent AS consent
                FROM messages m JOIN contacts ct ON ct.id = m.contact_id
                WHERE m.id = ?
                """,
                (message_pk,),
            )
        elif contact_id and channel:
            # Брать ПОСЛЕДНЮЮ строку по времени отправки, а не по id DESC.
            # Раньше стояло `ORDER BY m.id DESC LIMIT 1` — при отсутствии
            # message_pk это выбирало новейшую строку канала, и эхо по старому
            # сообщению перезаписывало свежую запись другого контакта.
            c.execute(
                """
                SELECT m.*, ct.phone AS phone, ct.name AS name, ct.category AS category,
                       ct.consent AS consent
                FROM messages m JOIN contacts ct ON ct.id = m.contact_id
                WHERE m.contact_id = ? AND m.channel = ?
                  AND m.message_id IS NOT NULL AND m.sent_at IS NOT NULL
                ORDER BY m.sent_at DESC, m.id DESC LIMIT 1
                """,
                (contact_id, channel),
            )
        else:
            return False
        row = c.fetchone()
        if not row:
            return False
        if not row["message_id"] or not row["sent_at"]:
            return False
        # DRY_RUN-сообщения — фейк (`dryrun_<ts>_<channel>`), реальной отправки
        # за ними не стоит. Раньше этот синтетический id проходил проверку выше и
        # попадал в БОЕВУЮ таблицу, где выглядел как настоящая отправка: так
        # появились 8 строк-призраков по 4 контактам, никогда не получавшим
        # сообщений. В таблицу идёт только то, что подтверждено Wazzup.
        if str(row["message_id"]).startswith("dryrun_"):
            config.get_logger("sheets").debug(
                f"Skipping DRY_RUN message pk={row['id']} — не реальная отправка"
            )
            return False
        reply = db.get_replies_since(conn, row["contact_id"], channel, row["sent_at"])
        last_activity = row["updated_at"]
        c.execute(
            "SELECT MAX(created_at) AS mx FROM replies WHERE contact_id = ? AND channel = ?",
            (row["contact_id"], channel),
        )
        r = c.fetchone()
        if r and r["mx"] and (not last_activity or r["mx"] > last_activity):
            last_activity = r["mx"]

        payload = {
            "phone": row["phone"],
            "name": row["name"] or "",
            "category": row["category"] or "",
            "channel": channel,
            "message": row["message_text"] or "",
            "sent_at": row["sent_at"],
            "delivered_at": row["delivered_at"],
            "read_at": row["read_at"],
            "answered_at": row["answered_at"],
            "delivery_state": row["delivery_state"] or "unknown",
            "status": status,
            "last_error": row["last_error"] or "",
            "reply": reply,
            "last_activity": last_activity,
            "message_id": row["message_id"] or "",
            "contact_id": row["contact_id"],
            "consent": row["consent"] or "unknown",
        }
        message_pk_resolved = row["id"]

    if not SHEETS.enabled:
        return False

    row_data = SHEETS.build_row_payload(**payload)
    ok = SHEETS.append_or_update(
        row_data=row_data,
        phone=payload["phone"],
        channel=channel,
        message_id=payload["message_id"],
    )
    if not ok:
        db.enqueue_sheet_op("upsert", payload, message_pk=message_pk_resolved)
        config.get_logger("sheets").warning(
            f"Sheets недоступны — операция поставлена в очередь (message_pk={message_pk_resolved})"
        )
    else:
        cached = SHEETS.cached_row(payload["phone"], channel, payload["message_id"])
        with db.db_conn() as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE messages SET gsheet_row = COALESCE(gsheet_row, ?) WHERE id = ?",
                (cached, message_pk_resolved),
            )
    return ok
