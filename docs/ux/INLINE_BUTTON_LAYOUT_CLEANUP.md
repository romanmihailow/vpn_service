# Inline Button Layout Cleanup

**Дата:** 2026-03-16  
**Цель:** Убрать обрезку длинных кнопок на мобильных — ставить по одной на строку там, где две и более кнопок в ряду могли обрезаться.

---

## 1. Какие места были найдены

Проверены: `app/tg_bot_runner.py`, `app/bot.py`, `app/support/actions.py`, `app/support/service.py`.

**Строки с двумя и более кнопками, которые могли обрезаться:**

| Место | Описание |
|-------|----------|
| **get_status_keyboard** | «Продлить подписку» и «Продлить баллами» в одной строке |
| **bot.py combined_keyboard** (post-config) | «Проверить подключение» + «Нужна помощь» в одной строке; «Да, установлен» + «Скачать WireGuard» в одной строке |
| **no_handshake_reminder keyboard** | «Получить настройки» + «Нужна помощь» в одной строке |
| **action_pricing_info** | Уже по одной кнопке на строку — изменений не требовалось |

**Оставлены без изменений (короткие или уже по одной):**

- SUBSCRIBE_KEYBOARD, START_KEYBOARD, SUBSCRIPTION_PAGE_KEYBOARD, REF_TRIAL_KEYBOARD
- POINTS_KEYBOARD, SUBSCRIPTION_RENEW_KEYBOARD
- HANDSHAKE_USER_CONNECTED_KEYBOARD, HANDSHAKE_FOLLOWUP_2H_KEYBOARD
- _make_10m_keyboard, _make_ref_nudge_keyboard
- config_check keyboards, onboarding keyboards
- Админские клавиатуры (используются на десктопе)

---

## 2. Какие клавиатуры изменены

1. **get_status_keyboard** (tg_bot_runner.py) — /status
2. **combined_keyboard** в send_vpn_config_to_user (bot.py) — post-config flow
3. **keyboard** в auto_no_handshake_reminder (tg_bot_runner.py) — напоминания «ты ещё не подключался»

---

## 3. Где кнопки перенесены на отдельные строки

| Клавиатура | Было | Стало |
|------------|------|-------|
| **get_status_keyboard** | [Получить настройки], [Продлить подписку, Продлить баллами], [Пригласить друга] | [Получить настройки], [Продлить подписку], [Продлить баллами], [Пригласить друга] |
| **combined_keyboard** | [Проверить подключение, Нужна помощь], [Да установлен, Скачать WireGuard] | [Проверить подключение], [Нужна помощь], [Да установлен], [Скачать WireGuard] |
| **no_handshake_reminder** | [Получить настройки, Нужна помощь] | [Получить настройки], [Нужна помощь] |

---

## 4. Изменённые файлы

| Файл | Изменения |
|------|-----------|
| **app/tg_bot_runner.py** | get_status_keyboard — «Продлить подписку» и «Продлить баллами» на отдельных строках; no_handshake_reminder keyboard — «Получить настройки» и «Нужна помощь» на отдельных строках |
| **app/bot.py** | combined_keyboard в send_vpn_config_to_user — все 4 кнопки по одной на строку |

---

## 5. Подтверждение

- Тексты кнопок не менялись
- callback_data и url не менялись
- Порядок кнопок сохранён
- Менялось только расположение (layout) — количество строк увеличено, кнопок в строке уменьшено

---

## UX-правило

- Короткие кнопки (1–2 слова) можно оставлять рядом, если они явно влезают
- Средние и длинные CTA — по одной в строке
- Платежные, статусные, support и referral кнопки — предпочтительно по одной в строке

---

Inline button layout cleaned up.  
Long buttons now render one per row where needed.
