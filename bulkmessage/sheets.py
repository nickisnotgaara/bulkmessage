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
        reply: str,
        last_activity: Optional[str],
        status: str,
        message_id: str,
    ) -> list[str]:
        return [
            phone,
            name,
            category,
            channel,
            message,
            self._format_dt(sent_at),
            self._format_dt(delivered_at),
            self._format_dt(read_at),
            "Yes" if answered_at else "",
            reply,
            self._format_dt(last_activity),
            status,
            message_id or "",
        ]

    def find_row(self, phone: str, channel: str, message_id: str) -> Optional[int]:
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
        for idx, row in enumerate(all_values[1:], start=2):
            r_phone = row[0] if len(row) > 0 else ""
            r_channel = row[3] if len(row) > 3 else ""
            r_msg_id = row[12] if len(row) > 12 else ""
            if r_phone == phone and r_channel == channel and r_msg_id and r_msg_id == message_id:
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
                    self._log().info(
                        f"Google updated row {existing} for {phone}/{channel} "
                        f"(msg_id={message_id})"
                    )
                else:
                    ws.append_row(row_data, value_input_option="USER_ENTERED")
                    all_values = ws.get_all_values()
                    new_idx = len(all_values)
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
                SELECT m.*, ct.phone AS phone, ct.name AS name, ct.category AS category
                FROM messages m JOIN contacts ct ON ct.id = m.contact_id
                WHERE m.id = ?
                """,
                (message_pk,),
            )
        elif contact_id and channel:
            c.execute(
                """
                SELECT m.*, ct.phone AS phone, ct.name AS name, ct.category AS category
                FROM messages m JOIN contacts ct ON ct.id = m.contact_id
                WHERE m.contact_id = ? AND m.channel = ?
                ORDER BY m.id DESC LIMIT 1
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
            "reply": reply,
            "last_activity": last_activity,
            "status": status,
            "message_id": row["message_id"] or "",
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
