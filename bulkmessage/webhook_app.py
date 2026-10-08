"""FastAPI app for receiving Wazzup24 webhooks."""

import json
import os
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from fastapi import Body, FastAPI, Header, HTTPException, Request
except ImportError:
    Body = FastAPI = Request = Header = HTTPException = None  # type: ignore

from . import config, db, reconcile, wazzup

# ──────────────────── версия кода этого процесса ──────────────────────
# code_version фиксируется ОДИН РАЗ при старте процесса, а не на каждый
# запрос. Иначе проверка бессмысленна: /health начал бы показывать sha
# свежего git pull, а процесс продолжал бы жить на старом коде.
# Реальный случай 2026-10-06: вебхук стартовал в 12:53 UTC, фикс разбора
# конверта {"statuses": [...]} приехал в 13:47 — 54 минуты события статусов
# ПРИХОДИЛИ и логировались, а подтверждений доставки не появилось ни одного.
_ROOT = Path(__file__).resolve().parent.parent
_CODE_VERSION_FILE = _ROOT / ".code_version"


def _sha_from_git_dir(git_dir: Path) -> Optional[str]:
    """Читает sha из .git/HEAD (+ сам ref, + packed-refs)."""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head[:40] or None
    ref = head.split(":", 1)[1].strip()
    try:
        return (git_dir / ref).read_text(encoding="utf-8").strip()[:40] or None
    except OSError:
        pass
    try:
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            sha, _, name = line.partition(" ")
            if name.strip() == ref:
                return sha.strip()[:40]
    except OSError:
        pass
    return None


def _detect_code_version() -> str:
    """SHA кода на момент старта процесса."""
    for git_dir in (Path("/opt/bulkmessage/.git"), _ROOT / ".git"):
        sha = _sha_from_git_dir(git_dir)
        if sha:
            return sha
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_ROOT), capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    # deploy.sh кладёт sha сюда: .git на сервер не едет (rsync его исключает),
    # иначе сервер отвечал бы на /health вечно "unknown".
    try:
        v = _CODE_VERSION_FILE.read_text(encoding="utf-8").strip()
        if v:
            return v
    except OSError:
        pass
    return os.environ.get("BULK_CODE_VERSION", "").strip() or "unknown"


_CODE_VERSION = _detect_code_version()
_START_TS = time.time()
_STARTED_AT = datetime.now(timezone.utc).isoformat()


def _current_webhook_url() -> Optional[str]:
    """URL подписки Wazzup для ЭТОГО процесса. Без сетевых вызовов."""
    try:
        info = json.loads(
            (Path(config.DATA_DIR) / "webhook_runtime.json").read_text(
                encoding="utf-8"
            )
        )
        if int(info.get("pid") or 0) == os.getpid() and info.get("url"):
            return str(info["url"])
    except Exception:
        pass
    return config.WAZZUP_WEBHOOK_URL or None


def _pending_webhooks_count() -> int:
    """Необработанных событий в очереди (0 при недоступной БД)."""
    try:
        with db.db_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM pending_webhooks WHERE processed = 0"
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:  # pragma: no cover - defensive
        reconcile.log.error(f"/health: не смог посчитать pending_webhooks: {e}")
        return 0


def _handle_payload(payload: dict) -> dict:
    """Общая логика обработки webhook — единый обработчик Wazzup24.

    Тонкая обёртка над process_wazzup_payload для вызовов через /webhook.
    """
    return _handle_wazzup_payload(None, payload)


def _is_status_event(payload: dict) -> bool:
    """Событие статуса доставки приходит конвертом {"statuses": [...]}."""
    if not isinstance(payload, dict):
        return False
    return any(
        isinstance(payload.get(k), list) and payload[k]
        for k in ("statuses", "statusesUpdate")
    )


def _handle_wazzup_payload(channel_hint: Optional[str], payload: dict) -> dict:
    """Обработка webhook payload от Wazzup24 (единственный транспорт).

    enqueue + process.
    """
    if not isinstance(payload, dict):
        return {"status": "bad request"}

    # РОВНО ОДНА строка лога на событие. Раньше POST логировался дважды
    # («Webhook POST received» в роуте + «Wazzup webhook received» здесь),
    # и это замаскировало потерю статусов: событий thousands, а строк
    # «DELIVERY status» — ноль. Маркер `DELIVERY status` сохранён: по нему
    # владелец ищет подтверждения доставки в логе.
    source = "/webhook/wazzup" if channel_hint else config.WEBHOOK_PATH
    kind = "DELIVERY status" if _is_status_event(payload) else "webhook"
    reconcile.log.info(f"{kind} <- POST {source}: {str(payload)[:200]}")

    try:
        db.enqueue_pending_webhook(channel_hint, payload)
    except Exception as e:
        reconcile.log.error(f"enqueue_pending_webhook (wazzup) error: {e}")

    try:
        reconcile.process_wazzup_payload(channel_hint, payload)
        with db.db_conn() as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE pending_webhooks SET processed = 1 WHERE id = "
                "(SELECT id FROM pending_webhooks WHERE payload = ? "
                "ORDER BY id DESC LIMIT 1)",
                (json.dumps(payload, ensure_ascii=False, default=str),),
            )
    except Exception as e:
        reconcile.log.error(
            f"wazzup webhook processing error: {e}\n{traceback.format_exc()}"
        )

    return {"status": "ok"}


def build_app():
    if FastAPI is None:
        return None

    app = FastAPI(title="Wazzup24 Webhook Receiver")

    # POST /webhook: основной endpoint для Wazzup24
    @app.post(config.WEBHOOK_PATH)
    async def webhook_post(request: Request, payload: dict = Body(...)):
        # HMAC verification (если WAZZUP_VERIFY_SIGNATURE on и secret задан)
        if config.WAZZUP_VERIFY_SIGNATURE and config.WAZZUP_HMAC_SECRET:
            raw = await request.body()
            sig = (
                request.headers.get("x-wazzup-signature")
                or request.headers.get("x-signature")
                or request.headers.get("signature")
            )
            if not wazzup.verify_webhook_signature(raw, sig):
                reconcile.log.warning(
                    f"Webhook {config.WEBHOOK_PATH}: invalid signature (sig={sig!r})"
                )
                raise HTTPException(status_code=401, detail="invalid signature")
        return _handle_wazzup_payload(None, payload)

    # POST /webhook/wazzup: явный Wazzup endpoint
    @app.post("/webhook/wazzup")
    async def webhook_wazzup_post(
        request: Request,
        payload: dict = Body(...),
    ):
        if config.WAZZUP_VERIFY_SIGNATURE and config.WAZZUP_HMAC_SECRET:
            raw = await request.body()
            sig = (
                request.headers.get("x-wazzup-signature")
                or request.headers.get("x-signature")
                or request.headers.get("signature")
            )
            if not wazzup.verify_webhook_signature(raw, sig):
                reconcile.log.warning(
                    f"Webhook /webhook/wazzup: invalid signature (sig={sig!r})"
                )
                raise HTTPException(status_code=401, detail="invalid signature")
        return _handle_wazzup_payload("waba", payload)

    # HEAD /webhook: некоторые сервисы проверяют URL HEAD-запросом
    @app.head(config.WEBHOOK_PATH)
    async def webhook_head():
        reconcile.log.info("Webhook HEAD received (URL verification)")
        return {"status": "ok"}

    # GET /health — диагностический источник истины, а не «жив/мёртв».
    # code_version — sha кода НА МОМЕНТ СТАРТА процесса (см. _CODE_VERSION):
    # если он разошёлся с боевым git — процесс молча живёт на старом коде,
    # и события статусов будут приходить и теряться (случай 2026-10-06).
    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "code_version": _CODE_VERSION,
            "started_at": _STARTED_AT,
            "uptime_sec": int(time.time() - _START_TS),
            "webhook_url": _current_webhook_url(),
            "pending_webhooks": _pending_webhooks_count(),
        }

    # Catch-all ПОСЛЕ статических роутов — логирует что угодно ещё
    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS", "PATCH"],
    )
    async def catch_all(path: str, request: Request):
        if path in ("health", "webhook"):
            return {"status": "skip"}
        method = request.method
        query = dict(request.query_params)
        body_preview = ""
        try:
            body_bytes = await request.body()
            if body_bytes:
                body_preview = body_bytes[:500].decode("utf-8", errors="replace")
        except Exception:
            pass
        reconcile.log.warning(
            f"CATCH-ALL: {method} /{path} query={query} body={body_preview[:200]}"
        )
        return {"status": "ok", "method": method, "path": path}

    return app


# ASGI app object (for uvicorn)
app = build_app()
