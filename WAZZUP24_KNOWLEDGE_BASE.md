# Wazzup24 — Knowledge Base (bulkmessage project)

> Источники: официальная документация Wazzup24 (`wazzup24.ru/help/`, `wazzup24.com/help/`, `wazzup24.es/help/`), отраслевые обзоры (amsales, vakas, radist, timeweb, elama), Meta-тарификация WABA, новости по Telegram/MAX антиспам.
> Дата сбора: 16.09.2026. После перепроверки — **сверяйся на сайте**, цены и лимиты Meta/Wazzup меняются.

---

## 1. Что такое Wazzup24 простыми словами

Wazzup24 — **шлюз** (bridge / wrapper) между CRM и мессенджерами. Не самостоятельный мессенджер, не SaaS для рассылок.

> **Вакансия:** "прослойка между вашей CRM и мессенджерами: WhatsApp, WABA, Telegram, MAX, Viber, ВКонтакте, Авито, Циан подключаются в один чат прямо внутри amoCRM или Битрикс24."

**Юрлицо**: ООО «ВАЗЗАП», резидент «Сколково», работает с **2017 года**.

**Главная мысль (важно для рассылок)**: Wazzup **не позиционируется** как инструмент массовых промо-рассылок (cold mailing). Для этого есть специализированные сервисы. Цитаты из обзоров:

- **vakas.ru**: «Главная путаница, на которой люди теряют деньги и банят номера: Wazzup создан для общения и поддержки из CRM, а массовые промо-рассылки в нём — прямой путь к блокировке.»
- **vakas.ru**: «Технически интерфейс рассылок есть, но в рамках антиспам-правил. Для WhatsApp — только одобренные платные шаблоны Meta вне 24-часового окна; холодные массовые рассылки запрещены.»

**Подключаемые каналы:**
| Канал | Режим подключения |
|---|---|
| WhatsApp (Personal) | QR-код |
| WhatsApp Business API (WABA) | через Meta-аккаунт, верификация |
| Telegram Personal | QR-код / сессия |
| Telegram Bot | Bot API token |
| MAX Personal | QR-код |
| MAX Bot | bot API (юрлица/ИП, модерация, ≤5 ботов на компанию) |
| Instagram* / Avito / ВКонтакте / Viber / Циан / HH.ru | соответственно |

---

## 2. API для скриптов — что есть

### 2.1 Базовый endpoint

```
POST   https://api.wazzup24.com/v3/message      # отправка
PATCH  https://api.wazzup24.com/v3/message/{messageId}  # редактировать
DELETE https://api.wazzup24.com/v3/message/{messageId}  # удалить
PATCH  https://api.wazzup24.com/v3/webhooks     # подписка на вебхуки
GET    https://api.wazzup24.com/v3/webhooks     # текущая подписка
```

> ⚠️ В документации одновременно живут **v2** (`/v2/messages`, `/v2/webhooks` с подпиской на каждое событие отдельно через `data[]`) и **v3** (`/v3/message`, групповая подписка `subscriptions`). Они **дублируют** функциональность, v3 — новее и проще. Источник путаницы: при ручном тестировании встречаются оба.

### 2.2 Авторизация (3 варианта)

| Метод | Когда использовать |
|---|---|
| **API key** (`Authorization: Bearer {apiKey}`) | своя интеграция, нет публичного маркетплейса |
| **OAuth (crmKey)** | публичное приложение, листинг на маркетплейсе Wazzup |
| **Sidecar API key** | родительская CRM (amoCRM / Битрикс24 / retailCRM) уже держит Wazzup как «транспорт» |

> Sidecar на практике означает: amoCRM/Битрикс24 хранят свой bearer-токен Wazzup, и твой скрипт берёт ключ у родителя. Wazzup в этом случае понимает, какой workspace вызывать, через URL/параметры.

### 2.3 Отправка — формат запроса `POST /v3/message`

```jsonc
// Минимальное текстовое сообщение
{
  "channelId": "e0629e11-0f67-4567-92a9-2237e91ec1b9",  // UUID канала в Wazzup
  "chatType": "whatsapp",                              // whatsapp | telegram | max | viber | instagram
  "chatId": "79994621848",                             // номер/юзернейм получателя
  "text": "Привет, это тест"
}

// WABA template с кнопками
{
  "channelId": "24197d5f-06de-421f-8576-9f6e6cb67f28",
  "chatType": "whatsapp",
  "chatId": "79994621848",
  "templateId": "6201005a-9a6f-486f-bdd5-e6cb86c76ddb",
  "templateValues": ["value1", "value2"],
  "buttonsObject": {
    "buttons": [
      { "payload": "btn1_payload" },
      { "payload": "btn2_payload" }
    ]
  }
}

// Инлайн-кнопки для TG Bot
{
  "channelId": "...",
  "chatType": "telegram",
  "chatId": "@username",
  "text": "Выбери:",
  "buttonsObject": {
    "buttons": [
      { "text": "Да",  "type": "text" },
      { "text": "Нет", "type": "text" }
    ]
  }
}
```

### 2.4 Лимиты длины `text` по каналам

| Канал | Максимум `text` |
|---|---|
| WhatsApp (personal) | 10 000 символов |
| Instagram* | 1 000 |
| **WABA** | **550 в шаблоне / 1024 в обычном сообщении внутри 24ч-окна** |
| Telegram / MAX | 4 096 |
| ВКонтакте / Авито | 1 000 |

> Правило: `text` и `contentUri` одновременно передавать **нельзя**. Получишь 400 "Cannot provide more that one of text, attachment or template".

### 2.5 Отправка файлов (`contentUri`)

- Файл скачивается Wazzup **сразу** по получении запроса → можно использовать **короткоживущие** ссылки (S3 pre-signed).
- Ссылка должна быть **без редиректов**.
- Формат и размер — по правилам мессенджера.
- Telegram: голосовое `.mp3`/`.ogg` до 1 МБ отправится **как голосовое**, иначе — файлом.

### 2.6 Кнопки (Telegram Bot)

```jsonc
{
  "buttons": [
    { "text": "Текст", "type": "text" | "url" | "callback",
      "url": "https://...",         // для type=url
      "callbackData": "1-64 bytes", // для callback
      "payload": "..."              // для WABA template
    }
  ]
}
```

| Поле | Ограничение |
|---|---|
| `text` кнопки | ≤ 64 символа |
| `url` | inline-кнопка, открывается на клик |
| `callbackData` | 1–64 байта |
| Шаблон WABA | payload обязателен, текст кнопки приходит из шаблона |

### 2.7 Редактирование и удаление

```http
PATCH  /v3/message/{messageId}   # только text или contentUri, не оба сразу
DELETE /v3/message/{messageId}   # поддерживают не все мессенджеры
```

Поддерживается только там, где мессенджер **сам** разрешает edit/delete. **WABA отдельно регламентирует** — отредактировать шаблонное сообщение после доставки клиенту нельзя.

---

## 3. Webhooks для автоматического фикса ответов

> Это **главная фишка** Wazzup: без webhooks твой `data/crm.db` не получит входящие сообщения и статусы доставки. Sender скрипт может жить без него, **tracking/CRM — не может**.

### 3.1 Подписка (`PATCH /v3/webhooks`)

В v3 упростили — флаговый объект `subscriptions`:

```jsonc
{
  "webhooksUri": "https://my-server.example.com/wh",
  "subscriptions": {
    "messagesAndStatuses":     true,  // message.add + message.status_update
    "contactsAndDealsCreation": true, // crm_entities.contact_add / deal_add
    "channelsUpdates":          true, // статус канала, QR, tier WABA
    "templateStatus":           true  // модерация шаблонов WABA
  }
}
```

В v2 — список `data[]` с парами `url + event`.

### 3.2 Полный список событий

| Event | Что приходит |
|---|---|
| `message.add` | **входящее** сообщение от клиента (с `messageId`, `chatType`, `text`, файлы и т.д.) |
| `message.status_update` | статус **исходящего**: `sent` → `delivered` → `read` или `error` |
| `message.delete` | сообщение удалено |
| `message.edit` | сообщение отредактировано (только поддерживающие каналы) |
| `group_chat.memberships_update` | изменение состава WhatsApp-группы |
| `group_chat.update` | обновление информации о группе |
| `crm_entities.contact_add` | «нужно создать новый контакт» |
| `crm_entities.deal_add` | «нужно создать новую сделку» (только если `contact_add` уже подключён) |
| `channel.status_update` | смена статуса канала |
| `channel.create` | канал создан |
| `channel.delete` | канал удалён |
| `channel.qr_update` | обновился QR-код (WhatsApp/TG/Viber) |
| `channel.waba_tier_update` | **изменился tier WABA** (Meta сама поднимает/опускает) |
| `waba_template.status_update` | **результат модерации шаблона** в Meta |
| `messages_dump.status_update` | экспорт сообщений готов (download link) |

### 3.3 Структура payload

**Webhook «новые сообщения»** (`messagesAndStatuses=true`):

```jsonc
{
  "messages": [
    {
      "messageId":   "uuid4",                  // guid сообщения в Wazzup
      "channelId":   "uuid4",                  // какой канал
      "chatType":    "whatsapp" | "telegram" | "max" | "viber" | "instagram" | "whatsgroup" | ...,
      "chatId":      "7999...",
      "text":        "...",
      "attachment":  { ... },                  // опционально
      "contact":     { "name": "...", "phone": "..." },
      "timestamp":   "ISO 8601",
      "type":        "incoming" | "outgoing",
      "status":      "delivered" | "read" | "sent" | "error",
      "crmUserId":   "...",
      "crmMessageId":"...",
      "quotedMessageId": "..."
    }
  ]
}
```

**Webhook «статусы исходящих»** (та же подписка, ключ `statuses`):

```jsonc
{
  "statuses": [
    {
      "messageId": "uuid4",
      "timestamp": "ISO 8601",
      "status":    "sent" | "delivered" | "read" | "error",
      "error": {                    // только при status=error
        "error":       "код_ошибки",
        "description": "описание",
        "[data]":      "детали"
      }
    }
  ]
}
```

> Один webhook-вызов **может содержать** и `messages`, и `statuses` одновременно. Так что в коде проверяй оба ключа.

### 3.4 Checklist для своего webhook endpoint (для bulkmessage)

1. HTTPS, public URL, принимает `POST application/json`.
2. Возвращай HTTP **200** как можно быстрее (Wazzup не любит таймауты).
3. **Идемпотентность**: `messageId` уникален — пиши его в `data/crm.db` с UNIQUE-индексом, чтобы повторные ретраи не задвоили записи.
4. Логируй **всё**: timestamp, headers, raw body, response code.
5. Подпишись минимум на `messagesAndStatuses` — это связка входящих + статусов доставки.
6. **Подтверди связь**: Wazzup шлёт `POST {test:true}` → отдай `200`. Без этого подписка считается невалидной.

---

## 4. Тарифы Wazzup24 (на 2026, RUB/мес за 1 канал/1 номер)

> Каждый канал/номер — отдельная подписка. Скидки: **−10%** за полгода, **−20%** за год.

### 4.1 WhatsApp / Telegram Personal / MAX / Viber

| Тариф | Цена ₽/мес | Цена $/мес | Цена ₸/мес | Диалоги | «Писать первым» | Групповые | Расшифровка войсов |
|---|---|---|---|---|---|---|---|
| **START** | 1 000 | 15 | 6 000 | 50 | ✅ | TG/MAX — да; WA/Viber — нет | ❌ |
| **INBOX** | 2 000 | 30 | 12 000 | 500 | ❌ | TG/MAX — да; WA/Viber — нет | ❌ |
| **PRO** | 4 000 | 45 | 24 000 | 500 | ✅ | TG/MAX — да; Viber — нет | ❌ |
| **MAX** | 6 000 | 90 | 36 000 | безлимит | ✅ | TG/MAX — да; Viber — нет | ✅ |

### 4.2 Бесплатные / бот-каналы

| Канал | FREE (500 диалогов) | Платный (безлимит) |
|---|---|---|
| Telegram Bot | ✅ есть | 4 000 ₽/мес |
| MAX Bot | ✅ есть (было, актуальность проверять) | 4 000 ₽/мес |
| ВКонтакте | ✅ есть | 4 000 ₽/мес |

> «Писать первым» в бот-каналах = **нет** в обоих вариантах.

### 4.3 WhatsApp Business API (WABA)

- Подписка ~6 000 ₽/мес (дешевле при годовой оплате).
- **+ отдельно Meta-тарификация за каждое шаблонное сообщение** (см. §6).
- Диалоги не лимитятся Wazzup-тарифом — лимит от Meta (tier).

### 4.4 Подключение, тест

- Каждый канал даёт **3 бесплатных дня** на знакомство (кроме некоторых интеграций).
- Подключение WhatsApp через QR — моментально. WABA — проверка Meta Business Manager (дни/недели).

---

## 5. WABA (WhatsApp Business API) — tiering и тарификация

### 5.1 Tiers (всегда «rolling 24h» по business-initiated conversations)

| Tier | Лимит конверсий / 24 ч | Условие |
|---|---|---|
| **Tier 0** | 250 | default, до верификации business в Meta Business Manager |
| **Tier 1** | 1 000 | после верификации + approved display name |
| **Tier 2** | 10 000 | ~50% от Tier 1 использовано за 7 дней **+** Yellow или Green Quality |
| **Tier 3** | 100 000 | ~50% от Tier 2, стабильная репутация |
| **Tier 4** | unlimited (с SLA) | reserved для крупных |

> В источниках стартовая нумерация спорит: некоторые говорят **«Tier 1 = 250»**, Meta-dock — **«Tier 0 = 250»**. Это лишь разница в отсчёте — факт: **по умолчанию новая бизнес-учётка стартует с 250/сутки**.

**Что определяет апгрейд:**
1. Quality Rating ≥ Yellow последние 7 дней.
2. Использовано ≥ 50% текущего tier за 7 дней.

**Что определяет даунгрейд:**
1. Quality → Red.
2. Нарушение policy (массовый спам, жалобы, нерелевантный consent).

### 5.2 Quality Rating

- 🟢 **Green (High)** — открыт для апгрейда.
- 🟡 **Yellow (Medium)** — можно держать, но апгрейд блокируется.
- 🔴 **Red (Low)** → автоматический даунгрейд, возможен бан.

> Считается **по реакциям получателей**: жалобы «report spam», «block», «not interested», удаление чата без ответа.

### 5.3 Шаблоны (categories)

Meta классифицирует шаблоны:

| Категория | Когда | Когда одобрят | Цена |
|---|---|---|---|
| **Utility** | подтверждение заказа, статус доставки, уведомления | быстрее | дешевле |
| **Marketing** | акции, офферы, просьбы об отзыве | дольше | дороже (~+15–30%) |
| **Authentication** | OTP / 2FA | быстро | дешевле Utility |

### 5.4 Цены за шаблонное сообщение с 01.07.2025

Per-template pricing, не за 24-часовую сессию. Цены у **Wazzup специально для WABA на момент 2025–2026** (для сравнения):

| Страна получателя | Инициатор | Цена (для WABA через Wazzup) |
|---|---|---|
| Россия | business | **7.03 ₽** |
| Россия | customer | **4.21 ₽** |
| Казахстан | business | **28.14 ₸** |

> Marketing-категория дороже. Уточняй по своей стране на момент подключения.

### 5.5 24-часовое окно

- Клиент написал первым → окно **24 часа**, в течение которых ты можешь отвечать **нешаблонными** сообщениями (сессия бесплатна).
- После закрытия окна — снова только шаблоны и **charged per send**.

---

## 6. Telegram — лимиты и риски блокировок (Personal + Bot)

### 6.1 Два режима у Telegram в Wazzup

| Режим | Подключение | Лимиты | Риск бана |
|---|---|---|---|
| **Telegram Personal** | QR/сессия | жёсткие, как для user-аккаунта | **высокий** при рассылке |
| **Telegram Bot** | Bot API token | мягкие (Bot API: 30 msg/sec, 20 в группу/мин) | низкий, если не спам |

> Для массовых рассылок Telegram Personal **в 2026 — рискованная история**. Wazzup в своей публикации явно пишет: «в Telegram и МАКС массовые рассылки безопаснее запускать через бота — при наличии согласия пользователя».

### 6.2 Лимиты для Personal-аккаунта (2026)

| Возраст/состояние аккаунта | DMs в день новым | DMs в день всем |
|---|---|---|
| Свежий (< 2 недель) | 2–3 → 10–20 | 30–60 |
| Прогретый (2–4 недели) | 30–50 | 80–120 |
| Warmed (1+ мес) | 60–80 | 100–200 |
| Trusted (3+ мес, история) | 80–150 (cold) | 200–300 |
| Premium user | 35–50 cold | доп. пороги |

**Темп и паузы:**
- 60–180 сек между сообщениями (рандомизировать, не точный интервал).
- Каждые 10–25 сообщений — пауза 5–10 минут.
- Раз в день — 60–120 минут отдыха.
- 1 msg/sec в один чат (hard rule, с исключениями).

### 6.3 Что триггерит бан

- 50+ новых DM/час **без** тёплой истории → «спамбот».
- 5–7 жалоб «Report spam» в течение 24ч → temp-block.
- 10+ жалоб → постоянный бан (номер телефона).
- Идентичный текст в десятки чатов.
- Незапргртый аккаунт сразу в рассылку.
- Групповой инвайтинг без согласия.
- Неофициальные клиенты / модифицированные сессии — **бан на уровне сессии**.

**Спектр санкций:**
1. Slowdown / 429 — задержка доставки.
2. Temp block 24–48 ч — нельзя писать неконтактам.
3. SpamBot-restriction — апелляция через `@SpamBot`.
4. Permanent ban → номер навсегда, восстановление невозможно.

---

## 7. MAX (Personal + Bot) — лимиты и риски

### 7.1 Два режима у MAX

| Режим | Подключение | Кто может | Лимиты | Риск |
|---|---|---|---|---|
| **MAX Personal** | QR/сессия | все | мягче ТГ по форме, **жёстче** по санкциям | **очень высокий** при рассылке |
| **MAX Bot** | bot API | **только юрлица/ИП** | зависит от возраста бота | средний |

> Рекомендация **timeweb.com**: «Рассылка по базе в несколько тысяч контактов не может идти потоком… для молодых ботов – порядка 100-200 сообщений в минуту. Превышение порога останавливает отправку и может вызвать временный бан на несколько часов. По мере роста возраста бота и его репутации лимиты расширяются, но никогда не снимаются полностью.»

> Из **elama.ru**: «За август 2025 года было заблокировано около 27 тысяч подозрительных профилей, большинство — за спам. Массовые рекламные рассылки по номерам телефонов запрещены.»

### 7.2 Лимиты для MAX Personal-аккаунта (2026)

| Действие | Safe | Max | Ключевое условие |
|---|---|---|---|
| Личные сообщения новым | до 30–50/день после прогрева | > 100/день — риск экспоненциально | задержка > 10–40 сек; **3–5 жалоб → бан** |
| Инвайтинг в чаты | 30–40/день | 70/день — почти гарантированный бан | пауза > 15 сек |
| Вступления в чаты | до 500/день | жёсткого лимита нет | пауза > 10 сек |
| Проверка номеров (через чекер) | 1–10/день | 20/день (потолок) | массовые запросы → бан |

### 7.3 Что триггерит бан в MAX

- **Жалобы** — главный сигнал. **3–5 жалоб и аккаунт перманентно заблокирован** (восстановление невозможно).
- Превышение 100 msg/день на прогретом аккаунте → риск резко растёт.
- Шаблонный текст (маркетинговые штампы «только сегодня», «гарантия 100%»).
- UTM/длинные ссылки в первом же сообщении.
- Прокси чужой страны / datacenter → снижение trust.
- Первая неделя без прогрева → мгновенный риск.
- Не действует «легальный» канал без согласия пользователя.

**По наблюдениям radist.online:** безопасный потолок для Telegram/MAX на прогретом аккаунте = **10–15 msg/день холодным контактам**.

### 7.4 MAX Bot — особенности

- Запустить может **только юрлицо или ИП**.
- Каждый бот проходит **модерацию**.
- **≤ 5 ботов на одну компанию**.
- Бот свободно пишет в течение **24 часов после последнего действия** пользователя.
- Когда окно закрывается — произвольная переписка блокируется.
- Лимит на новые боты: **100–200 msg/мин**, расширяется с возрастом.

---

## 8. Рекомендации для проекта bulkmessage

> Учитывая, что в проекте уже есть `main.py sender`, `state.channel_has_quota()`, `get_sent_phones()`, daily cap 60/канал — это **консервативная** защита для Personal. Для WABA её можно **ослабить** после прохождения tier-up.

### 8.1 Стратегия выбора канала

```
для каждого контакта:
  1. если есть WABA-номер (verified Green Quality):
       → WhatsApp через WABA  (безопасно, официально, платно за шаблон)
  2. если контакт явно писал тебе ранее:
       → Telegram через бота (если есть TG Bot) или TG Personal в 24h-окне
  3. если MAX-номер известен, нет возражающих:
       → MAX через бота
  4. иначе:
       → НЕ спамить через TG/MAX Personal (бан в течение дней)
```

### 8.2 Если решили мигрировать в Wazzup целиком

**Плюсы:**
- Один провайдер, одна панель.
- WABA не в Wappi (у Wappi основное — Personal-аккаунт, риск бана).
- Webhooks встроены, готовый pipeline.
- Корпоративный OAuth-механизм.
- Поддержка всех нужных каналов.

**Минусы:**
- Wazzup открыто декларирует, что **не для холодного спама**. Прямой холодной рассылки по неоптинившимся — нет.
- Тариф за каждый канал/номер отдельно: 1 000–6 000 ₽ × N.
- WABA — доп. подписка 6 000 ₽/мес + Meta-плата.
- Personal-канал всё равно ограничивает по окну 24ч и рискам бана.

> Комбинация: **Wazzup для WABA + Wappi для MAX/TG Personal** (для Personal есть смысл, пока база мала и риск-сенситивные не на низком уровне) — экономически обычно **дешевле**, чем перевод всего в Wazzup.

### 8.3 Hard-лимиты по каналам (для sender-скрипта)

| Канал | Hard-cap | Обоснование |
|---|---|---|
| WABA Tier 0 | **250 / сутки** на бизнес-номер | Meta cap, не Wazzup |
| WABA Tier 1 | 1 000 / сутки | Meta cap |
| WABA Tier 2 | 10 000 / сутки | Meta cap |
| WABA Tier 3 | 100 000 / сутки | Meta cap |
| Telegram Personal (холодные) | **10–15 / сутки на аккаунт** | radist / wsender / telega |
| Telegram Personal (тёплые, 1+ мес) | до 100–200 / сутки | max-catalog, crmchat |
| MAX Personal (холодные) | **10–15 / сутки на аккаунт** | radist / sendermax |
| MAX Personal (безопасный) | до 50 / день | max-catalog24 |
| MAX Bot (молодой) | 100–200 msg/min | timeweb |
| MAX Personal (инвайтинг) | 30–40 / сутки (max 70) | max-catalog24 |

### 8.4 Серверная «рассылочная» стратегия через API

1. **Подключи webhook** (`PATCH /v3/webhooks`) на свой endpoint → HTTPS, идемпотентная запись в `data/crm.db`.
2. **Получи список каналов** (`GET /v3/channels` или подобный метод) — узнать `channelId` для каждого активного мессенджера.
3. **Для WABA** — сначала создай template в Meta Business Manager, дождись модерации (webhook `waba_template.status_update`), потом используй `templateId` в `POST /v3/message`.
4. **Sender-скрипт** для каждого контакта:
   - Если в `data/crm.db` уже есть `chatType` для этого контакта (отвечал раньше) — внутри 24ч-окна можно слать без `templateId`.
   - Иначе — `templateId` обязателен (для WABA).
5. **Параллельность**: держи ≤ 1–2 одновременных сообщения на Personal-аккаунт. Для WABA — больше, но следи за tier.
6. **Retry policy**: при `error` из webhook (`status_update` со `status: 'error'`) — не лупить повторно; обрабатывать backoff и quarantine.
7. **Логируй** все запросы и ответы `data/broadcast_log.csv` (уже есть).

---

## 9. Wappi vs Wazzup24 — для справки

> Wappi уже у тебя подключен (для MAX/others). Оставлю как полезное сравнение.

| Параметр | Wazzup24 | Wappi |
|---|---|---|
| Фокус | CRM-интеграция / диалоги внутри CRM | Дешёвый агрегатор + рассылки |
| Подключение WhatsApp | Personal QR + WABA | Personal QR (преимущественно неофициальная браузерная сессия) |
| Каналы | WA / WABA / TG / TG-Bot / MAX / MAX-Bot / Inst / Avito / VK / Viber / Циан / HH | WA / TG / MAX / Avito / VK / HH |
| Тариф | 1 000–6 000 ₽/мес за 1 канал + WABA 6 000 + Meta | от 550 ₽/мес |
| API | полноценный REST v2/v3, webhooks | API + `/broadcast/init`, webhooks |
| Безопасность WA | официально (WABA), без бана | риск бана через браузерную сессию |
| Групповая рассылка | для WABA — шаблоны Meta, для Personal — окно 24ч | да, через Personal аккаунты (массовые) |
| Целевой юзер | средний/крупный бизнес с CRM | малый/стартовый сценарий |

> **amsales.ru:** «Главное — не делать через Wappi массовые рассылки холодной аудитории: риск блокировки реален.»
> **Wazzup blog:** «В Telegram и МАКС массовые рассылки безопаснее запускать через бота — при наличии согласия пользователя.»

---

## 10. Полезные ссылки

### Официальная документация Wazzup24

| Раздел | URL |
|---|---|
| Главная RU | https://wazzup24.ru/help/ |
| Главная EN | https://wazzup24.com/help/ |
| API RU — отправка | https://wazzup24.ru/help/api/messages/ |
| API EN — Sending messages | https://wazzup24.com/help/api-en/sending-messages/ |
| API RU — Вебхуки | https://wazzup24.ru/help/api/webhooks/ |
| API EN — Webhooks | https://wazzup24.com/help/api-en/webhooks/ |
| API EN — Integration schemes | https://wazzup24.com/help/api-en/integration-schemes/ |
| API EN — Working with channels | https://wazzup24.com/help/api-en/channels/ |
| Тарифы RU | https://wazzup24.ru/help/payment/price-plans-ru/ |
| WABA лимиты | https://wazzup24.ru/help/how-to-use/waba-limits/ |
| Шаблоны WABA | https://wazzup24.ru/help/how-to-use/templates/ |
| Бот vs Personal (TG/MAX) | https://wazzup24.ru/blog/bot-vs-personal/ |
| Документация ES | https://wazzup24.es/help/documentacion-api/ |

### Внешние источники (использованы в Wave 3-5)

| Тема | URL |
|---|---|
| Сравнение Wazzup/Wappi/ChatApp | https://amsales.ru/journal/wazzup-vs-wappi-vs-chatapp/ |
| Обзор тарифов Wazzup 2026 | https://vakas.ru/articles/wazzup-obzor/ |
| Что делать если TG/MAX банят | https://wsender.ru/blog/kak-ne-poluchit-ban-pri-rassylke |
| Лимиты MAX (max-catalog24) | https://max-catalog24.ru/limits.html |
| SenderMax WABA tiering | https://sendermax.ru/ |
| Как делать рассылку MAX | https://maxsurge.ru/blog/kak-sdelat-rassylku-v-max-poshagovo |
| Рассылки в MAX 2026 (timeweb) | https://timeweb.com/ru/community/articles/rassylki-v-max-rabochie-scenarii-v-2026-godu |
| WABA tier levels (keybe.ai) | https://keybe.ai/articles/sales/level-up-with-whatsapp-business-api/ |
| Quality rating (saysimple) | https://www.saysimple.com/blog/whatsapp-quality-rating-explained |
| Top-5 рассылок RU | https://work24.ru/spravochnik/povyshenie-kvalifikacii/reklama-i-marketing/messendjer-marketing/top-5-servisov-dlya-rassylok-v-messendjerah-sravnenie-i-vybor-dl |
| WABA tier down/up rules | https://www.tkana.sa/en/whatsapp/messaging-tiers |
| Wappi MAX API docs | https://wappi.pro/max-api-documentation |
| API рассылок (wsender) | https://wsender.ru/blog/api-rassylok-v-messendzherakh |

---

## 11. TODO для команды bulkmessage

> То, что **вытекает из этого исследования** и что стоит принять решение руками пользователя (а не делать автоматически):

1. **Решить**: держим ли мы Wappi для MAX/TG Personal после перехода на WABA через Wazzup? Или сворачиваемся и используем только **бесплатный Telegram Bot API** через Wazzup.
2. **Подписать webhook** для `messagesAndStatuses` на свой endpoint → сохранять ответы клиентов и статусы доставки в `data/crm.db`. Это база для аналитики «открыли/прочитали/ответили».
3. **Завести шаблоны WABA** под свои категории рассылки (Utility / Marketing). Дождаться модерации через webhook `waba_template.status_update`.
4. **Считать tier WABA** (хранить `data/broadcast_state.json[waba_tier]`) и автоматически поднимать `daily_cap` до 250/1000/10 000 по мере апгрейда. Подписка `channel.waba_tier_update`.
5. **Не поднимать лимиты Personal-аккаунтов** выше рекомендованных 60/день (наш текущий лимит OK). Wazzup-канал для уже подключённых Personal пусть живёт; для рассылки — только в 24ч-окне или через TG/MAX Bot.
6. **Compliance**: для каждого Personal-канала иметь подтверждение согласия (поле в `data/crm.db` + логика в sender), иначе в MAX — бан, в TG — жалоба, в WABA — quality red.
7. **Открытый вопрос для пользователя**: нужен ли нам немедленный переход всех каналов в Wazzup (включая те, что сейчас на Wappi), или оставляем гибрид?

---

*Конец Knowledge Base. Документ под сохранение в `bulkmessage/`.*
