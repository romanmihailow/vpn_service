# AI Support — улучшения V1

Дата: 2025-03-12

---

## 1. Список изменённых файлов

| Файл | Изменения |
|------|-----------|
| `app/support/intents.py` | Порядок intents, новые intents `vpn_not_working` и `smalltalk` |
| `app/support/actions.py` | Cooldown resend, `action_smalltalk()`, `action_vpn_not_working()` |
| `app/support/service.py` | Обработка intents `vpn_not_working` и `smalltalk` |

**Не изменялись:** `app/support/router.py`, `app/tg_bot_runner.py`, payment flows, referral flows, FSM, WireGuard logic, db schema.

---

## 2. Ключевые изменения (diff)

### app/support/intents.py

- **Порядок проверки:** `MISSING_CONFIG_PATTERNS` перенесён выше `RESEND_PATTERNS`.
- **Новые константы:** `VPN_NOT_WORKING_PATTERNS`, `SMALLTALK_PHRASES`.
- **Порядок в `classify_intent()`:**
  1. human_request  
  2. missing_config_after_payment  
  3. resend_config  
  4. vpn_not_working  
  5. connect_help  
  6. subscription_status  
  7. handshake_status  
  8. smalltalk  
  9. unclear  

- **Логика `missing_config_after_payment`:** при совпадении `MISSING_CONFIG_PATTERNS` и наличии `has_active_subscription` и `can_resend_config` возвращается `missing_config_after_payment` (conf 0.85), иначе — `missing_config_after_payment` (conf 0.9). Раньше при active subscription возвращался `resend_config`.

### app/support/actions.py

- **Cooldown:**
  - `RESEND_COOLDOWN_SEC = 30`
  - `RESEND_COOLDOWN: Dict[int, float] = {}`
  - перед вызовом `send_vpn_config_to_user` проверка: если `now - last < 30`, ответ «Я уже отправил конфиг недавно. Проверь сообщения выше.» без отправки
  - после успешной отправки: `RESEND_COOLDOWN[telegram_user_id] = now`

- **`action_smalltalk()`:** возвращает короткий текст с перечислением возможностей.

- **`action_vpn_not_working()`:** возвращает текст с советом и кнопку поддержки.

### app/support/service.py

- **Импорт:** добавлены `action_smalltalk`, `action_vpn_not_working`.
- **Обработка:**
  - `intent == "vpn_not_working"` → вызов `action_vpn_not_working()`
  - `intent == "smalltalk"` → вызов `action_smalltalk()`

---

## 3. Финальный список intents

| № | Intent | Confidence | Описание |
|---|--------|------------|----------|
| 1 | `human_request` | 0.95 | Запрос оператора |
| 2 | `missing_config_after_payment` | 0.7–0.9 | Конфиг не пришёл после оплаты |
| 3 | `resend_config` | 0.9 | Переотправка конфига |
| 4 | `vpn_not_working` | 0.8 | VPN подключен, интернет не работает |
| 5 | `connect_help` | 0.85 | Как подключиться |
| 6 | `subscription_status` | 0.7–0.85 | Статус подписки |
| 7 | `handshake_status` | 0.8 | Статус handshake |
| 8 | `smalltalk` | 0.7 | Привет, кто ты и т.п. |
| 9 | `unclear` | 0.0–0.3 | Неопределённый запрос |

---

## 4. Подтверждение: router и FSM не изменены

- **`app/support/router.py`** — без изменений.
- **`app/tg_bot_runner.py`** — без изменений.
- FSM-сценарии (PromoStates, DemoRequest, PromoAdmin и др.) — не затронуты.
- Платежные сценарии, referral flow, WireGuard-логика, схема БД — без изменений.

---

## 5. Поведение после изменений

- Фраза «я оплатил но конфиг не пришел» попадает в `missing_config_after_payment` (проверка `MISSING_CONFIG_PATTERNS` до `RESEND_PATTERNS`).
- Частые запросы «вышли конфиг» ограничены cooldown 30 сек.
- Добавлены ответы на `vpn_not_working` и `smalltalk`.
