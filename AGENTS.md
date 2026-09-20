# AGENTS.md — bulkmessage

Файл для AI-агентов (MiniMax / Cursor / Claude Code / и т.д.), чтобы они
сразу понимали правила проекта **без чтения всего кода**.

## 🚨 ЖЁСТКИЕ ПРАВИЛА (не нарушать)

### 1. **НИКОГДА не запускать рассылку автоматически**
   - Только пользователь делает это вручную: `py main.py sender`.
   - Причина: реальные Wazzup24-сообщения, риск бана, реальные деньги.
   - Все автоматические «запуски» должны быть **DRY_RUN** (симуляция).
   - Если пользователь просит «запусти» — переспросить и убедиться, что
     `BULK_DRY_RUN=0` он выставил сам.

### 2. **Дневной лимит: hard cap для каждого канала**
   - **WABA** через Wazzup: tier 0 = 250/day (headroom 25% → effective 187).
     tier 1/2/3/4 — 1000/10K/100K/unlimited.
   - **Telegram Personal** (через Wazzup): MAX 30/день (hard cap в коде,
     никакой env override не может превысить). С headroom 25% → effective ~22.
   - **MAX Personal** (через Wazzup): MAX 30/день (тот же hard cap).
   - **WhatsApp Personal** (через Wazzup): 60/день. С headroom 25% → ~45.
   - **Это оригинальные (свои) аккаунты.** Любой бан = потеря личного аккаунта.
   - Система защищает через `state.channel_has_quota_with_headroom()` +
     `MAX_PERSONAL_DAILY_HARD_CAP=30` (в коде, не env).
   - **НО**: если state.json повреждён / сброшен руками — защита ломается.

### 3. **Cascade: WABA → TG → MAX (Phase 2)**
   - По умолчанию `BULK_USE_WABA=0` → старая логика: WA → TG → MAX (broadcast).
   - С `BULK_USE_WABA=1` → cascade WABA → TG → MAX с break на успехе.
     Один контакт = одно сообщение в ОДИН канал (самый приоритетный из доступных).
   - **НЕ БОЛЬШЕ одного успеха на контакт за итерацию** (был баг: `continue` в цикле).
   - Если у контакта все каналы failed → контакт помечается в `data/failed_today.json`.

### 4. **Scheduler + ручной запуск**
   - В проекте есть планировщик, который автоматически запускает sender.
   - Если планировщик не сработал — пользователь запускает sender вручную.
   - Это значит: sender может отработать **больше одного раза в день**.
   - Перед любым ручным запуском ОБЯЗАТЕЛЬНО проверить `broadcast_state.json`:
     - если `sent_today` уже близок к hard cap в каком-то канале — НЕ запускать.
   - Preflight: `py main.py preflight` показывает лимиты, tier, halted channels, tokens.

### 5. **DRY_RUN — последняя линия обороны**
   - `BULK_DRY_RUN=1` в `.env.local` — НИКОГДА не отправляет реальные сообщения.
   - DRY_RUN имитирует успешные отправки во всех каналах (WABA+TG+MAX).
   - Перед боевым запуском пользователь ставит `BULK_DRY_RUN=0` САМОСТОЯТЕЛЬНО.
   - Агент **не имеет права** менять `BULK_DRY_RUN` на 0 без явного «ок» от пользователя.

### 6. **Anti-bank для оригинального аккаунта (Phase 3)**
   - **Headroom 25%** — никогда не доходим до 100% квоты.
   - **Safe hours**: WABA 10:00-20:00, Personal 11:00-19:00 МСК.
   - **Per-phone cooldown** (WABA 0ч, Personal 96ч) — после успешной отправки
     в Personal, этот контакт не трогаем 4 дня в Personal-каналах.
   - **Spin-tax** (`{opt1|opt2|opt3}`) — текст рандомизируется для анти-pattern.
     Phase 3.1 templates живут в `Message_script.md`.
   - **Complaint halt**: при 2 жалобах за 24ч для MAX, 3 для TG/WABA —
     канал HALTED на 24ч, alert в TG-бот.
   - **Adaptive decay**: жалоба → cap × 0.5 на следующий день.

### 7. **WABA Templates (Phase 1.2)**
   - У пользователя есть **4 одобренных шаблона** в Meta: по одному на
     категорию (Покупатели/Продавцы/Агенты/Инвесторы).
   - UUID шаблонов хранятся в `.env.local`: `WABA_TEMPLATE_BUYER_ID` и т.д.
   - **ОБЯЗАТЕЛЬНО формат UUID** (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`).
     Числовые ID типа `2147911229434328` НЕ подходят — Wazzup вернёт
     `BUILD_TEMPLATE_ERROR`. Источник UUID: Meta Business Manager →
     WhatsApp → Message Templates → "API ID" / templateId.
   - cold outreach = `templateId` (только одобренные Meta).
   - В 24ч-окне после reply от клиента — можно free text.
   - **НЕ редактировать Message_script.md тексты шаблонов WABA без явного «ок»** —
     они должны соответствовать тому, что одобрила Meta, иначе шаблон упадёт.

### 8. **Telegram-бот для уведомлений (опционально)**
   - `BULK_TG_LOG_BOT_TOKEN` + `BULK_TG_LOG_ADMIN_ID` в `.env.local` — модуль `tglog`.
   - Если оба заданы — логи ERROR+ и ключевые события (start, finish, квоты,
     halted channel) идут в личку админу.
   - Перед боевым запуском — проверить связь: `py -c "from bulkmessage import tglog; print(tglog.send_test())"`.

---

## 🕐 Активное окно и safe hours

- `BULK_TIMEZONE=Europe/Moscow` — дневные квоты и расписание считаются в МСК.
- `BULK_ACTIVE_HOURS_START=10`, `BULK_ACTIVE_HOURS_END=20` — общее окно.
- `PERSONAL_SAFE_HOURS_START=11`, `PERSONAL_SAFE_HOURS_END=19` — для TG/MAX (уже).
- `WABA_SAFE_HOURS_START=10`, `WABA_SAFE_HOURS_END=20` — для WABA (шире).
- Вне safe window — sender спит до открытия (`_within_safe_hours`).

## 🧪 Тестирование

- `py test_suite.py` — **227 unit/integration тестов**. Должен проходить перед любым запуском.
- `py main.py preflight` — preflight check (config, tokens, tiers, templates, spintax).
- `py test_dry_run.py` — симуляция расписания (без реальных вызовов).
- `py main.py reset-state --yes` — сбросить broadcast_state.json (для теста).

## 📁 Ключевые файлы

| Файл | Назначение |
|---|---|
| `.env.local` | Конфиг (лимиты, токены, TZ, DRY_RUN, USE_WABA). **Содержит секреты Wazzup24.** |
| `.env.example` | Шаблон env-файла с комментариями (коммитится в git). |
| `.env.docker` | Конфиг для Docker-контейнера. |
| `Message_script.md` | Шаблоны сообщений + spin-tax варианты |
| `data/Contact.xlsx` | Список контактов (имя, телефон, категория) |
| `data/crm.db` | SQLite: история всех отправок, ответы, статусы. **Не удалять.** |
| `data/broadcast_state.json` | Текущие дневные квоты, halt-state, tier. Сбрасывается по дате (МСК). |
| `data/broadcast_log.csv` | Лог каждой отправки (phone, channel, status, timestamp). |
| `data/complaints.db` (в crm.db) | Жалобы (Phase 3.6). |
| `data/waba_templates` (в crm.db) | Кэш одобренных WABA-шаблонов (UUID от Meta). |
| `data/channel_reachability` (в crm.db) | Pre-check кэш (TTL 24ч). |
| `bulkmessage/wazzup.py` | Wazzup24 API client (единственный транспорт). |
| `bulkmessage/waba_templates.py` | WABA template registry (UUID-валидация, sync). |
| `bulkmessage/preflight.py` | Preflight CLI (Phase 5.3). |
| `WAZZUP24_KNOWLEDGE_BASE.md` | Полный knowledge base Wazzup API. |
| `BULK_MESSAGING_SERVICES.md` | Сервисы типа Molnio (knowledge base). |

## 🔐 Секреты (НЕ коммитить, НЕ логировать)

- `WAZZUP_API_KEY`, `WAZZUP_HMAC_SECRET` — в `.env.local`.
- `WAZZUP_*_CHANNEL_ID` (UUID каналов) — в `.env.local`.
- `WABA_TEMPLATE_*_ID` (UUID одобренных шаблонов) — в `.env.local`.
- `BULK_TG_LOG_BOT_TOKEN` — в `.env.local`.
- `BULK_GOOGLE_SHEET_ID` + `credentials.json` — для синхронизации с Google Sheets.
- Файл `.env` в проекте — **архивный, не используется** (см. комментарий в нём).
- Файл `.env.docker` — для Docker-запуска.

## 🚀 Запуск

```bash
# 1) Preflight check
py main.py preflight

# 2) DRY-RUN (без реальных отправок)
BULK_DRY_RUN=1 py main.py sender

# 3) Симуляция расписания
py test_dry_run.py

# 4) Боевой запуск (только вручную, после проверки preflight)
BULK_DRY_RUN=0 py main.py sender
```