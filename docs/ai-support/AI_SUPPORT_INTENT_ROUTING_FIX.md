# AI-Support: Intent routing fix (privacy, referral_stats, referral_balance)

Исправление маршрутизации: вопросы про конфиденциальность, статистику рефералов и баланс бонусов больше не попадают в общий `referral_info`. Добавлены отдельные интенты и сужены паттерны `referral_info`.

---

## Проблема

Вопросы вида «сколько рефералов оплатили?», «сколько у меня баллов?», «что с моими данными?» классифицировались как `referral_info`, что давало один и тот же ответ про приглашение друга.

---

## Новые интенты

| Intent | Назначение |
|--------|------------|
| **privacy_policy** | Персональные данные, конфиденциальность, что хранится |
| **referral_stats** | Сколько рефералов/друзей подключилось или оплатило |
| **referral_balance** | Баллы, бонусы, баланс, бонусные дни |

---

## Паттерны

### privacy_policy

- персональные данные  
- что с моими данными  
- храните ли данные  
- конфиденциальность  
- privacy  
- данные пользователя  

### referral_stats

- сколько рефералов  
- сколько друзей подключилось  
- сколько друзей оплатили  
- сколько рефералов оплатили  
- сколько подключились  

### referral_balance

- сколько баллов  
- сколько бонусов  
- мой баланс  
- бонусные дни  
- сколько бонусных дней  

### referral_info (сужено)

Только про приглашение:

- реферальная программа  
- как работает рефералка  
- как пригласить друга  
- реферальная ссылка  
- пригласить друга  

Убраны из `referral_info`: «сколько рефералов», «сколько друзей», «бонусные дни», «мой баланс» и т.п. — они обрабатываются `referral_stats` и `referral_balance`.

---

## Порядок интентов

1. human_request  
2. missing_config_after_payment  
3. resend_config  
4. vpn_not_working  
5. **privacy_policy**  
6. **referral_stats**  
7. **referral_balance**  
8. **referral_info**  
9. connect_help  
10. subscription_status  
11. handshake_status  
12. smalltalk  
13. unclear  

Порядок 5–8 гарантирует, что запросы про данные и статистику не попадают в `referral_info`.

---

## Ответы и действия

- **PRIVACY_POLICY_RESPONSE** — что храним (Telegram ID, срок подписки, параметры подключения), отсылка к политике и поддержке.  
- **REFERRAL_STATS_RESPONSE** — бот не показывает, сколько друзей подключилось/оплатило; бонусные дни начисляются; кнопка «Пригласить друга».  
- **REFERRAL_BALANCE_RESPONSE** — бот не показывает точный баланс бонусных дней; как начисляются дни; кнопка «Пригласить друга».  
- **REFERRAL_INFO_RESPONSE** — без изменений (приглашение и кнопка).

В `actions.py`: добавлены `action_privacy_policy()` (возвращает текст) и `action_referral_balance()` (текст + кнопка). В `service.py` добавлены ветки для `privacy_policy`, `referral_balance`; `referral_stats` и `referral_info` уже были.

---

## Изменённые файлы

- **app/support/intents.py** — добавлены `PRIVACY_POLICY_PATTERNS`, обновлены `REFERRAL_STATS_PATTERNS`, добавлены `REFERRAL_BALANCE_PATTERNS`, сужены `REFERRAL_PATTERNS`, обновлён порядок проверок.  
- **app/messages.py** — добавлены `PRIVACY_POLICY_RESPONSE`, `REFERRAL_BALANCE_RESPONSE`; уточнён текст `REFERRAL_STATS_RESPONSE`.  
- **app/support/actions.py** — добавлены `action_privacy_policy()`, `action_referral_balance()`, импорты новых констант.  
- **app/support/service.py** — импорты и ветки для `privacy_policy` и `referral_balance`.

Архитектура (guardrails, FSM, остальные интенты) не менялась.
