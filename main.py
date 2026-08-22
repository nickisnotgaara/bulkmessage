"""
Backwards-compatible shim.

The full implementation lives in the `bulkmessage` package:
  - bulkmessage.sender  (long-running sender daemon)
  - bulkmessage.tracker (webhook + reconciler)

Run modes:
  python main.py sender    -> bulkmessage.sender.run()
  python main.py tracker   -> bulkmessage.tracker.run()
  python main.py           -> bulkmessage.sender.run()  (default, for back-compat)
"""

import sys

from bulkmessage.contacts import load_contacts
from bulkmessage.db import init_db
from bulkmessage.sender import run as run_sender
from bulkmessage.state import (
    load_state,
    save_state,
    channel_sent_today,
    increment_channel_sent,
    channel_has_quota,
    reset_daily_if_new_day,
)
from bulkmessage.templates import build_message, load_templates
from bulkmessage.wappi import active_channels, classify_error, send_wappi, normalize_phone
from bulkmessage import config


__all__ = [
    "load_templates",
    "build_message",
    "load_contacts",
    "init_db",
    "load_state",
    "save_state",
    "channel_sent_today",
    "increment_channel_sent",
    "channel_has_quota",
    "reset_daily_if_new_day",
    "active_channels",
    "send_wappi",
    "classify_error",
    "normalize_phone",
    "config",
    "main",
]


def main() -> None:
    """Default entry point: runs the sender (back-compat with old single-process mode)."""
    run_sender()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "sender"
    if mode == "sender":
        run_sender()
    elif mode == "tracker":
        from bulkmessage.tracker import run as run_tracker
        run_tracker()
    else:
        print(f"Unknown mode: {mode}. Use 'sender' or 'tracker'.", file=sys.stderr)
        sys.exit(1)
