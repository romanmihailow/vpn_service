# UX-патч: CTA в connection flow (первый handshake + пост-конфиг)

**Дата:** 2026-03-16  
**Цель:** усилить продажу в момент первого успешного подключения VPN и убрать лишнюю кнопку после отправки конфига. Менялись только тексты и inline-кнопки, без изменений архитектуры, follow-up jobs, WG lifecycle и IP logic.

---

## 1. CTA-кнопки под первым handshake-сообщением

**Где:** первое пользовательское сообщение после детекта handshake — отправляется в job `auto_new_handshake_admin_notification` в `app/tg_bot_runner.py` (фрагмент с отправкой `HANDSHAKE_USER_CONNECTED_TEXT` пользователю).

**Было:** сообщение «VPN подключён 👍» + текст про тариф 3 мес / 270 ₽ и «Оформить можно здесь: /buy» — **без inline-кнопок**.

**Сделано:** под тем же сообщением добавлена inline-клавиатура:

| Строка | Текст кнопки | callback_data |
|--------|----------------|---------------|
| 1 | 💎 Закрепить доступ — 3 месяца / 270 ₽ | `pay:open` |
| 2 | 📅 Все тарифы | `pay:open` |

Оба действия ведут на экран тарифов/оплаты (`pay:open`). Отдельного callback для «все тарифы» в проекте нет, используется тот же `pay:open` — платежная логика не менялась.

**Файл:** `app/tg_bot_runner.py`  
- Константа `HANDSHAKE_USER_CONNECTED_KEYBOARD` (после `HANDSHAKE_USER_CONNECTED_TEXT`).  
- В вызове `safe_send_message(..., text=HANDSHAKE_USER_CONNECTED_TEXT, ..., reply_markup=HANDSHAKE_USER_CONNECTED_KEYBOARD)` при отправке первого handshake-уведомления пользователю.

Это именно **первое** сообщение после handshake (отправка в том же job при детекте), а не follow-up через 10 минут.

---

## 2. Убрана кнопка «🚀 Подключить VPN» из пост-конфиг клавиатуры

**Где:** сообщение «Готово 👌 Я отправил файл конфигурации и QR-код...» (инструкция после отправки конфига) в `app/bot.py`, функция `send_vpn_config_to_user()`.

**Было:** под сообщением три кнопки:
- 🚀 Подключить VPN (`onboarding:start`)
- 🧑‍💻 Нужна помощь (url)
- 🔍 Проверить подключение (`config_check_now:{sub_id}`)

**Сделано:** в **этом** сообщении оставлены только:
- 1-я строка: 🔍 Проверить подключение
- 2-я строка: 🧑‍💻 Нужна помощь

Кнопка «🚀 Подключить VPN» убрана **только** из post-config instruction keyboard. Onboarding flow и callback `onboarding:start` в других местах (например, в AI smalltalk, других экранах) не трогались.

**Файл:** `app/bot.py`  
- Изменена сборка `instruction_keyboard` в `send_vpn_config_to_user()`: при наличии подписки — два ряда (Проверить подключение, Нужна помощь); при отсутствии подписки — один ряд (Нужна помощь).  
- Удалён неиспользуемый импорт `ONBOARDING_START_BUTTON` из `app/bot.py`.

---

## 3. Изменённые файлы

| Файл | Изменения |
|------|-----------|
| `app/tg_bot_runner.py` | Добавлена константа `HANDSHAKE_USER_CONNECTED_KEYBOARD` и передача `reply_markup=HANDSHAKE_USER_CONNECTED_KEYBOARD` при отправке первого handshake-сообщения. |
| `app/bot.py` | Упрощена post-config клавиатура (убрана кнопка «Подключить VPN»), удалён импорт `ONBOARDING_START_BUTTON`. |

---

## 4. Что не менялось

- Сообщение с вопросом «У тебя уже установлен WireGuard?» и его клавиатура
- Follow-up 10m / 2h / 24h и их тексты/кнопки
- Логика `config_check_now`, checkpoint jobs
- Логика resend (отправка конфига повторно)
- Кнопки поддержки в других местах
- CRM, БД, типы уведомлений
- WireGuard peer lifecycle, IP allocation, платежная логика

---

## Краткий итог

- **Added CTA buttons to first handshake message** — под первым сообщением «VPN подключён 👍» добавлены кнопки «💎 Закрепить доступ — 3 месяца / 270 ₽» и «📅 Все тарифы» с `pay:open`.
- **Removed redundant "Подключить VPN" button from post-config screen** — в сообщении после отправки конфига оставлены только «🔍 Проверить подключение» и «🧑‍💻 Нужна помощь».
