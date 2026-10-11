import sys
sys.path.insert(0, "scratch")
from remote import run

CMDS = [
    ("tier в state.json",
     "python3 -c \"import json;d=json.load(open('/opt/bulkmessage/data/broadcast_state.json'));"
     "print('waba_tier =', d.get('waba_tier'));"
     "print('updated_at =', d.get('waba_tier_updated_at'))\""),
    ("-tier события в pending_webhooks",
     "grep -c waba_tier_update /opt/bulkmessage/data/pending_webhooks.jsonl 2>/dev/null || echo 0"),
    ("последние tier-события",
     "grep waba_tier_update /opt/bulkmessage/data/pending_webhooks.jsonl 2>/dev/null | tail -3 || echo none"),
    ("подписка вебхука (события)",
     "python3 /opt/bulkmessage/setup_webhook.py --show 2>&1 | tail -20"),
]
for title, cmd in CMDS:
    code, out, err = run(cmd)
    print(f"--- {title} (exit {code}) ---")
    print((out or "").strip()[:1500])
    if err.strip():
        print("STDERR:", err.strip()[:400])
    print()
