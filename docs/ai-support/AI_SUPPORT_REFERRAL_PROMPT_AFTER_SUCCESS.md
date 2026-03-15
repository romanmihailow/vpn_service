# Реферальный призыв после подтверждения «Всё работает»

**Дата:** 2025-03-15  
**Цель:** сразу после нажатия «Да, всё работает» в checkpoint показывать реферальное предложение с кнопкой «Пригласить друга».

---

## 1. Изменённый handler

**Файл:** `app/tg_bot_runner.py`, функция `config_check_ok_callback`.

**Добавлено после записи `config_check_ok` в subscription_notifications:**

```python
ref_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="👥 Пригласить друга",
                callback_data=f"ref:open_from_notify:{sub_id}",
            ),
        ],
    ]
)
await callback.message.answer(
    REFERRAL_PROMPT_AFTER_CONNECTION_SUCCESS,
    reply_markup=ref_kb,
)
log.info("[ReferralPrompt] tg_id=%s source=config_check_ok", callback.from_user.id if callback.from_user else None)
```

Кнопка использует существующий callback `ref:open_from_notify:{sub_id}` — при нажатии вызывается `ref_open_from_notify`, пользователь получает реферальную ссылку (как в других местах бота). Передача `sub_id` сохраняет возможность трекинга (в т.ч. `ref_nudge_clicked` при необходимости).

---

## 2. Текст сообщения

**Константа в `app/messages.py`:** `REFERRAL_PROMPT_AFTER_CONNECTION_SUCCESS`

```
🎉 Отлично! VPN работает.

Кстати, можно получить бесплатные дни VPN.

Пригласи друзей по своей ссылке — и получай дни VPN на баланс.
```

Под сообщением одна кнопка: **«👥 Пригласить друга»**.

---

## 3. Итоговый сценарий для пользователя

1. Пользователь нажимает **«✅ Да, всё работает»** в сообщении checkpoint.
2. Бот убирает кнопки с того сообщения и отправляет первое сообщение: **CONFIG_CHECK_SUCCESS** («Отлично 👌 Если что-то понадобится позже — просто напишите мне.»).
3. Бот отправляет второе сообщение: **REFERRAL_PROMPT_AFTER_CONNECTION_SUCCESS** и кнопку **«👥 Пригласить друга»**.
4. При нажатии на кнопку срабатывает существующий handler `ref_open_from_notify` (callback_data `ref:open_from_notify:{sub_id}`), пользователь получает реферальную ссылку.

---

## 4. Логирование

В лог пишется строка:

`[ReferralPrompt] tg_id=... source=config_check_ok`

Отдельная таблица в БД для этого не создаётся.

---

## 5. Что не менялось

- Логика AI-support, FSM, checkpoint job, payments, subscription, WireGuard — без изменений.
- Меняется только поведение после нажатия «Да, всё работает»: добавляется второе сообщение с реферальным текстом и кнопкой, переиспользуется существующий реферальный callback.
