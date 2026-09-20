# bulkmessage

Self-hosted WhatsApp + Telegram + MAX (VK) mass-messaging system via
[Wazzup24](https://wazzup24.com).

> ⚠️ **Disclaimer**: This tool is for legitimate business communication with
> contacts who have given consent. Mass-spam violates the ToS of Wazzup24,
> WhatsApp, Telegram, and VK. Use at your own risk.

## Features

- **4 channels, 1 transport**: WhatsApp (Personal + WABA), Telegram, MAX — all via Wazzup24
- **Cascade mode** (`BULK_USE_WABA=1`): per contact try WABA → TG → MAX, first success wins
- **WABA tier-aware**: tier 0 = 250/day, tier 1 = 1000, tier 2/3/4 = 10K/100K/unlimited
- **Hard cap on Personal channels**: 30/day (anti-bank для оригинальных аккаунтов)
- **Smart pacing**: random 5–15 min между контактами, batch breaks
- **Active hours**: 10:00–20:00 Moscow time
- **TZ-aware**: вся time math в `Europe/Moscow` независимо от server locale
- **Webhook receiver**: Wazzup events (delivered, read, replied, status_update)
- **Telegram bot notifications**: ошибки и ключевые события в личку админу
- **Google Sheets mirror**: каждая отправка + status update зеркалится в таблицу
- **Dry-run mode**: полная симуляция без реальных вызовов Wazzup
- **Self-tests**: 227 unit/integration тестов
- **Lock-file protection**: защита от двойного запуска

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
# Edit .env.local — fill in your real Wazzup tokens, channel IDs, template UUIDs
```

Required secrets in `.env.local`:
- `WAZZUP_API_KEY` — из личного кабинета Wazzup24 → Настройки → API
- `WAZZUP_DEFAULT_CHANNEL_ID` — UUID канала WABA (WhatsApp Business через Wazzup)
- `WAZZUP_TG_CHANNEL_ID` — UUID канала Telegram
- `WAZZUP_MAX_CHANNEL_ID` — UUID канала MAX (бывший VK)
- `WAZZUP_WEBHOOK_URL` — публичный HTTPS URL для webhook (используй ngrok для dev)
- `WAZZUP_HMAC_SECRET` — секрет подписи webhook
- `WABA_TEMPLATE_*_ID` — UUID одобренных шаблонов из Meta Business Manager
- `BULK_GOOGLE_SHEET_ID` — из URL Google Sheets
- `credentials.json` — в корне проекта, service account key
- `BULK_TG_LOG_BOT_TOKEN` + `BULK_TG_LOG_ADMIN_ID` — (опционально)

**Критично для WABA**: `templateId` от Meta должен быть в формате **UUID**
(`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`). Числовые ID (`2147911229434328`)
НЕ подходят — Wazzup вернёт `BUILD_TEMPLATE_ERROR`. Источник UUID:
[Meta Business Manager](https://business.facebook.com/) → WhatsApp →
Message Templates → одобренный шаблон → "API ID".

### 3. Add contacts

Положи `data/Contact.xlsx` со столбцами:
- `Телефон` (или `Номер телефона`, `Phone`)
- `Имя контакта` (или `Имя`)
- `Категория` (или `Статус`) — одна из: Покупатели / Продавцы / Агенты / Инвесторы

### 4. Verify everything works

```bash
# Unit + integration тесты (без реальных Wazzup вызовов)
py test_suite.py

# Симуляция расписания (60 контактов, проектируемое время отправок)
py test_dry_run.py

# Preflight check (проверяет конфиг, токены, tier, шаблоны)
py main.py preflight
```

### 5. Run the sender (только вручную!)

```bash
# 1. Dry-run: проверка логики без реальных отправок
BULK_DRY_RUN=1 py main.py sender

# 2. Preflight (рекомендуется перед каждым запуском)
py main.py preflight

# 3. В .env.local: BULK_DRY_RUN=0

# 4. Боевой запуск (оставь терминал открытым; Ctrl+C = graceful stop)
py main.py sender
```

## Architecture

```
┌────────────────┐    ┌──────────────┐    ┌──────────────────┐
│ data/Contact.  │───▶│ sender.py    │───▶│ Wazzup24 API     │
│ xlsx           │    │ (long-run)   │    │ WABA + TG + MAX  │
└────────────────┘    └──────┬───────┘    └──────────────────┘
                            │                    │
                            ▼                    ▼
                       ┌─────────┐        ┌──────────────┐
                       │ crm.db  │◀───────│ Webhook       │
                       │ (SQLite)│        │ (status, reply)│
                       └────┬────┘        └──────────────┘
                            │
                            ▼
                  ┌──────────────────┐
                  │ Google Sheets    │ (mirror)
                  └──────────────────┘
                            │
                            ▼
                  ┌──────────────────┐
                  │ Telegram bot     │ (admin DMs)
                  └──────────────────┘
```

## Files

| Path | Purpose |
|---|---|
| `main.py` | Entry point: `py main.py sender` или `py main.py preflight` |
| `bulkmessage/wazzup.py` | Wazzup24 API client (единственный транспорт) |
| `bulkmessage/sender.py` | Long-running broadcast daemon + cascade |
| `bulkmessage/reconcile.py` | Webhook handler + reconciliation loops |
| `bulkmessage/waba_templates.py` | WABA template registry (UUID-валидация) |
| `bulkmessage/contacts.py` | Excel contact loader |
| `bulkmessage/templates.py` | Message templates (`Message_script.md`) |
| `bulkmessage/db.py` | SQLite schema + queries |
| `bulkmessage/state.py` | Daily quota tracker (`data/broadcast_state.json`) |
| `bulkmessage/sheets.py` | Google Sheets manager |
| `bulkmessage/tglog.py` | Telegram bot for admin notifications |
| `bulkmessage/webhook_app.py` | FastAPI webhook server |
| `bulkmessage/preflight.py` | Preflight CLI (Phase 5.3) |
| `Message_script.md` | Message templates per category (edit me!) |
| `data/Contact.xlsx` | Your contacts (NOT in repo) |
| `data/crm.db` | Broadcast history (NOT in repo) |
| `data/broadcast_state.json` | Current daily quota state (NOT in repo) |
| `.env.local` | Secrets (NOT in repo) |
| `credentials.json` | Google service account key (NOT in repo) |

## Tests

```bash
py test_suite.py       # 227 unit/integration тестов, без реальных вызовов
py test_dry_run.py     # Симуляция расписания
py main.py preflight   # Live check против Wazzup API
```

## Important rules

См. [`AGENTS.md`](./AGENTS.md) для полного AI-agent контракта.

Для людей: **никогда не запускай `py main.py sender` автоматически** —
только владелец проекта запускает рассылки вручную.

## License

Private / unreleased. Add a license before making this public.