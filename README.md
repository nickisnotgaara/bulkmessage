# bulkmessage

Self-hosted WhatsApp + Telegram + MAX (VK) mass-messaging system via [Wappi.pro](https://wappi.pro).

> ⚠️ **Disclaimer**: This tool is for legitimate business communication with
> contacts who have given consent. Mass-spam violates the ToS of Wappi,
> WhatsApp, Telegram, and VK. Use at your own risk.

## Features

- **3 channels per contact**: WhatsApp + Telegram + MAX, sent as one action
- **Daily quotas**: configurable per channel (default 60/канал/день)
- **Smart pacing**: random 5–15 min between contacts, batch breaks
- **Active hours**: only sends between 10:00–20:00 Moscow time
- **TZ-aware**: all time math in `Europe/Moscow` regardless of server locale
- **Telegram bot notifications**: error and milestone events to admin's DM
- **Google Sheets mirror**: every send + status update mirrored to a sheet
- **Webhook receiver**: Wappi status updates (delivered, read, replied)
- **Reconciliation**: polls Wappi for missed replies/webhooks
- **Dry-run mode**: full simulation without real Wappi API calls
- **Self-tests**: 181 unit/integration tests + 2 smoke tests
- **Lock-file protection**: prevents double-launch

## Quick start

### 1. Clone & install

```bash
git clone https://github.com/<your-org>/bulkmessage.git
cd bulkmessage
pip install -r requirements.txt
```

### 2. Configure secrets

```bash
cp .env.example .env.local
# Edit .env.local — fill in your real Wappi / Google Sheet / TG bot tokens
# See docs/SETUP.md for where to get each one.
```

Required secrets in `.env.local`:
- `WAPPI_WHATSAPP_TOKEN` + `WAPPI_WHATSAPP_PROFILE` — from [app.wappi.pro](https://app.wappi.pro)
- `WAPPI_TELEGRAM_TOKEN` + `WAPPI_TELEGRAM_PROFILE`
- `WAPPI_MAX_TOKEN` + `WAPPI_MAX_PROFILE` (optional)
- `BULK_GOOGLE_SHEET_ID` — from Google Sheets URL
- `credentials.json` in project root — see docs/SETUP.md
- `BULK_TG_LOG_BOT_TOKEN` + `BULK_TG_LOG_ADMIN_ID` (optional)

### 3. Add contacts

Place your `data/Contact.xlsx` with columns:
- `Телефон` (or `Номер телефона`, `Phone`)
- `Имя контакта` (or `Имя`)
- `Категория` (or `Статус`, `Category`) — one of: Покупатели / Продавцы / Агенты / Инвесторы

### 4. Verify everything works

```bash
# Unit + integration tests (no real Wappi calls)
py test_suite.py

# Schedule simulation (60 contacts, projected send times)
py test_dry_run.py

# Live checks against real APIs
py -c "from bulkmessage import config; print(config.preflight_check())"
py test_tg_log.py
```

### 5. Run the sender (only you, manually!)

```bash
# 1. Verify dry-run works
BULK_DRY_RUN=1 py main.py sender
#   Should send "DRY-RUN" simulation, no real Wappi calls.

# 2. Pre-flight check (recommended before each launch)
py -c "from bulkmessage import config; print(config.preflight_check())"
#   Should show ok=True for all active channels.

# 3. Set DRY_RUN=0 in .env.local

# 4. Launch (leave the terminal open; Ctrl+C = graceful stop)
py main.py sender
```

## Architecture

```
┌────────────────┐    ┌──────────────┐    ┌──────────────┐
│ data/Contact.  │───▶│ sender.py    │───▶│ Wappi API    │
│ xlsx           │    │ (long-run)   │    │ WA / TG / MAX│
└────────────────┘    └──────┬───────┘    └──────────────┘
                            │                    │
                            ▼                    ▼
                       ┌─────────┐        ┌──────────────┐
                       │ crm.db  │◀───────│ Webhook      │
                       │ (SQLite)│        │ (delivered,  │
                       └────┬────┘        │  read, replied)│
                            │             └──────────────┘
                            ▼
                  ┌──────────────────┐
                  │ Google Sheets    │ (mirror)
                  │ (live status)    │
                  └──────────────────┘
                            │
                            ▼
                  ┌──────────────────┐
                  │ Telegram bot     │ (admin DMs)
                  │ @<bot>           │
                  └──────────────────┘
```

## Files

| Path | Purpose |
|---|---|
| `main.py` | Entry point: `py main.py sender` or `py main.py tracker` |
| `bulkmessage/config.py` | Loads `.env.local`, defines limits, active hours, TZ |
| `bulkmessage/sender.py` | Long-running broadcast daemon |
| `bulkmessage/reconcile.py` | Webhook handler + reconciliation loops |
| `bulkmessage/wappi.py` | Wappi API client + error classification |
| `bulkmessage/contacts.py` | Excel contact loader |
| `bulkmessage/templates.py` | Message templates (`Message_script.md`) |
| `bulkmessage/db.py` | SQLite schema + queries |
| `bulkmessage/state.py` | Daily quota tracker (`data/broadcast_state.json`) |
| `bulkmessage/sheets.py` | Google Sheets manager |
| `bulkmessage/tglog.py` | Telegram bot for admin notifications |
| `bulkmessage/webhook_app.py` | FastAPI webhook server |
| `Message_script.md` | Message templates per category (edit me!) |
| `data/Contact.xlsx` | Your contacts (NOT in repo) |
| `data/crm.db` | Broadcast history (NOT in repo) |
| `data/broadcast_state.json` | Current daily quota state (NOT in repo) |
| `.env.local` | Secrets (NOT in repo) |
| `credentials.json` | Google service account key (NOT in repo) |

## Tests

```bash
py test_suite.py       # 181 unit/integration tests, no real calls
py test_dry_run.py     # Schedule simulation
py test_tg_log.py      # Telegram bot smoke test
```

## Important rules (READ BEFORE TOUCHING)

See [`AGENTS.md`](./AGENTS.md) for the full AI-agent contract.
For humans: **never run `py main.py sender` automatically** — only the
project owner launches broadcasts manually.

## License

Private / unreleased. Add a license before making this public.
