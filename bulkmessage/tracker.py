"""Tracker service: webhook receiver + reconciler.

Runs uvicorn in the main thread + reconciler loop in a background thread.
"""

from __future__ import annotations

import signal
import sys

from . import config, db, reconcile, webhook_app


log = config.get_logger("tracker")


def run() -> None:
    config.configure_logging()
    db.init_db()

    if webhook_app.app is None:
        log.error(
            "FastAPI/uvicorn не установлены. Tracker не может стартовать. "
            "Установите: pip install fastapi uvicorn"
        )
        sys.exit(1)

    log.info(
        f"Tracker starting: webhook on {config.WEBHOOK_HOST}:{config.WEBHOOK_PORT}"
        f"{config.WEBHOOK_PATH}"
    )
    log.info(
        f"Reconcile interval: {config.RECONCILE_INTERVAL}s, "
        f"Sheets: {'включены' if _sheets_enabled() else 'ОТКЛЮЧЕНЫ'}"
    )

    # Start reconciler
    reconcile.start_reconcile_loop()

    # Import uvicorn here to avoid heavy import at module load
    import uvicorn

    stop_event_set = {"v": False}

    def _on_signal(sig, frame):
        log.info(f"Tracker: signal {sig}, shutting down")
        stop_event_set["v"] = True

    try:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except Exception:
        pass

    config_uv = uvicorn.Config(
        app=webhook_app.app,
        host=config.WEBHOOK_HOST,
        port=config.WEBHOOK_PORT,
        log_level="info",
        lifespan="on",
    )
    server = uvicorn.Server(config_uv)
    try:
        server.run()
    finally:
        reconcile.stop_reconcile_loop()
        log.info("Tracker stopped.")


def _sheets_enabled() -> bool:
    from . import sheets
    return sheets.SHEETS.enabled


if __name__ == "__main__":
    run()
