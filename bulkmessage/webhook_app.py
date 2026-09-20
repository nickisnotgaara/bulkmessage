"""FastAPI app for receiving Wazzup24 webhooks."""

import json
import traceback
from typing import Optional

try:
    from fastapi import Body, FastAPI, Header, HTTPException, Request
except ImportError:
    Body = FastAPI = Request = Header = HTTPException = None  # type: ignore

from . import config, db, reconcile, wazzup


def _handle_payload(payload: dict) -> dict:
    """Общая логика обработки webhook — единый обработчик Wazzup24.

    Тонкая обёртка над process_wazzup_payload для вызовов через /webhook.
    """
    return _handle_wazzup_payload(None, payload)


def _handle_wazzup_payload(channel_hint: Optional[str], payload: dict) -> dict:
    """Обработка webhook payload от Wazzup24 (единственный транспорт).

    enqueue + process.
    """
    if not isinstance(payload, dict):
        return {"status": "bad request"}

    reconcile.log.info(f"Wazzup webhook received: {str(payload)[:200]}")

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
                    f"Webhook /webhook: invalid signature (sig={sig!r})"
                )
                raise HTTPException(status_code=401, detail="invalid signature")
        reconcile.log.info(f"Webhook POST received: {str(payload)[:200]}")
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
        reconcile.log.info(f"Webhook /webhook/wazzup POST: {str(payload)[:200]}")
        return _handle_wazzup_payload("waba", payload)

    # HEAD /webhook: некоторые сервисы проверяют URL HEAD-запросом
    @app.head(config.WEBHOOK_PATH)
    async def webhook_head():
        reconcile.log.info("Webhook HEAD received (URL verification)")
        return {"status": "ok"}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

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
