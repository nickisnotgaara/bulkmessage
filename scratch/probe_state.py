"""Состояние сервера: халт WABA, квоты, счётчики дня."""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/opt/bulkmessage")

from bulkmessage import config, state  # noqa: E402

st = state.load_state()

print("=== HALT ===")
for ch, until in (st.get("halted_channels") or {}).items():
    expired = not state.is_channel_halted(st, ch)
    print(f"  {ch}: until={until}  expired={expired}  "
          f"ACTIVE_NOW={not expired}")
print("  now(UTC) =", datetime.now(timezone.utc).isoformat())

print()
print("=== COUNTERS (today) ===")
for k in ("date", "contacts_today", "attempts_today", "successful_today",
          "target_today", "sent_today", "accepted_today", "delivered_today",
          "failed_today", "waba_tier", "waba_tier_updated_at"):
    print(f"  {k} = {json.dumps(st.get(k), ensure_ascii=False)[:200]}")

print()
print("=== STREAKS / COMPLAINTS ===")
for k in ("_overload_streak", "_provider_fail_streak", "_provider_fail_seen",
          "complaint_counts"):
    print(f"  {k} = {json.dumps(st.get(k), ensure_ascii=False)[:250]}")

print()
print("=== WABA CAP ===")
print("  effective_waba_cap =", state.effective_waba_cap(st))
print("  BULK_LIMIT_WABA    =", config.CHANNEL_DAILY_LIMITS.get("waba"))
print("  restricted templates:", config.WABA_RESTRICTED_TEMPLATES)