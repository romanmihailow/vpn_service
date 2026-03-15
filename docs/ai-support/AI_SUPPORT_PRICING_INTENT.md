# AI-Support: intent pricing_info

Добавлен новый интент **pricing_info** для ответов на вопросы о стоимости и тарифах VPN. Фразы вроде «сколько стоит?» больше не уходят в fallback, а возвращают тот же текст, что команда /subscription, с кнопками оплаты.

---

## 1. Интент и паттерны

**Файл:** `app/support/intents.py`

**Паттерны** `PRICING_PATTERNS`:

- сколько стоит  
- цена  
- стоимость  
- тариф  
- тарифы  
- сколько стоит vpn  
- цена vpn  
- сколько стоит подписка  
- стоимость подписки  

**Порядок в классификации:** после `subscription_status`, перед `connect_help`:

`… → subscription_status → pricing_info → connect_help → handshake_status → …`

Так запросы «сколько осталось подписки» по-прежнему обрабатываются как `subscription_status`, а «сколько стоит» — как `pricing_info`.

---

## 2. Текст ответа (как в /subscription)

**Файл:** `app/messages.py`

В общие сообщения вынесены константы, используемые и в /subscription, и в AI-support:

- **PRICING_HEADER** — заголовок «💳 Тарифы MaxNet VPN»
- **TARIFFS_UNAVAILABLE** — «Сейчас тарифы временно недоступны. Попробуй позже.»
- **SUBSCRIPTION_TEXT** — блок про выгоду сроков, способы оплаты (/buy, /buy_crypto), ссылка на сайт

Команда `/subscription` в `app/tg_bot_runner.py` переведена на импорт этих констант из `messages.py`, текст сообщения не менялся.

---

## 3. Действие action_pricing_info()

**Файл:** `app/support/actions.py`

**Сигнатура:** `action_pricing_info() -> Tuple[str, InlineKeyboardMarkup]`

**Логика:**

1. Загрузка тарифов из БД: `db.get_active_tariffs()`.
2. Сборка текста: заголовок (PRICING_HEADER) + список тарифов (название — сумма ₽) или TARIFFS_UNAVAILABLE + SUBSCRIPTION_TEXT.
3. Клавиатура с двумя кнопками:
   - «💳 Оплатить картой (/buy)» → `callback_data="pay:open"` (то же, что по /buy).
   - «₿ Криптой (/buy_crypto)» → `callback_data="heleket:open"` (то же, что по /buy_crypto).

Пользователь получает тот же контент, что и по команде /subscription, плюс кнопки перехода к оплате картой или криптой.

---

## 4. Роутинг в process_support_message()

**Файл:** `app/support/service.py`

Добавлена ветка:

```python
elif result.intent == "pricing_info":
    meta["action"] = "pricing_info"
    reply_text, reply_markup = action_pricing_info()
```

Остальной поток (guardrails, логирование, запись в support_conversations) без изменений; для `pricing_info` confidence 0.85, порог CONF_HIGH выполняется.

---

## 5. Изменённые и затронутые файлы

| Файл | Изменения |
|------|-----------|
| `app/support/intents.py` | Паттерны PRICING_PATTERNS, проверка pricing_info после subscription_status, перед connect_help. |
| `app/messages.py` | PRICING_HEADER, TARIFFS_UNAVAILABLE, SUBSCRIPTION_TEXT. |
| `app/tg_bot_runner.py` | Импорт SUBSCRIPTION_TEXT, PRICING_HEADER, TARIFFS_UNAVAILABLE из messages; удалён локальный SUBSCRIPTION_TEXT; cmd_subscription использует константы из messages. |
| `app/support/actions.py` | action_pricing_info(): текст как в /subscription, клавиатура pay:open и heleket:open. |
| `app/support/service.py` | Импорт action_pricing_info, ветка обработки intent pricing_info. |

Архитектура AI-support, FSM, платежи, реферальная логика и checkpoint не менялись.
