"""FastAPI app for receiving Wappi webhooks."""

import json
import traceback
from typing import Optional

try:
    from fastapi import Body, FastAPI, Request
except ImportError:
    Body = FastAPI = Request = None  # type: ignore

from . import config, db, reconcile, wappi


def _handle_payload(payload: dict) -> dict:
    """Общая логика обработки webhook — используется и для GET, и для POST."""
    if not isinstance(payload, dict):
        return {"status": "bad request"}

    reconcile.log.info(f"Webhook received: {str(payload)[:200]}")

    channel_hint: Optional[str] = None
    try:
        data = payload.get("messages") if "messages" in payload else payload
        if isinstance(data, dict):
            channel_hint = wappi.channel_from_profile_id(data.get("profile_id"))
        elif isinstance(data, list) and data:
            channel_hint = wappi.channel_from_profile_id(data[0].get("profile_id"))
    except Exception:
        channel_hint = None

    # Enqueue first
    try:
        db.enqueue_pending_webhook(channel_hint, payload)
    except Exception as e:
        reconcile.log.error(f"enqueue_pending_webhook error: {e}")

    try:
        reconcile.process_webhook_payload(channel_hint, payload)
        with db.db_conn() as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE pending_webhooks SET processed = 1 WHERE id = "
                "(SELECT id FROM pending_webhooks WHERE payload = ? "
                "ORDER BY id DESC LIMIT 1)",
                (json.dumps(payload, ensure_ascii=False, default=str),),
            )
    except Exception as e:
        reconcile.log.error(f"webhook processing error: {e}\n{traceback.format_exc()}")

    return {"status": "ok"}


def build_app():
    if FastAPI is None:
        return None

    app = FastAPI(title="Wappi Webhook Receiver")

    # POST /webhook: JSON body (стандартный Wappi формат)
    @app.post(config.WEBHOOK_PATH)
    async def webhook_post(payload: dict = Body(...)):
        reconcile.log.info(f"Webhook POST received: {str(payload)[:200]}")
        return _handle_payload(payload)

    # GET /webhook: Wappi может слать query-параметры
    @app.get(config.WEBHOOK_PATH)
    async def webhook_get(request: Request):
        params = dict(request.query_params)
        reconcile.log.info(f"Webhook GET received: query={params}")
        if not params:
            return {"status": "empty"}
        # Собираем query-параметры в структуру
        msg = {}
        for k, v in params.items():
            if k in ("is_me", "isReply", "is_forwarded", "is_edited", "is_deleted", "is_bot"):
                msg[k] = v.lower() in ("true", "1")
            elif k in ("time",):
                try:
                    msg[k] = int(v)
                except Exception:
                    msg[k] = v
            else:
                msg[k] = v
        payload = {"messages": [msg]}
        return _handle_payload(payload)

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
