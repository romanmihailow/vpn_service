# UX-патч: short confirmation follow-up после первого handshake

**Дата:** 2026-03-16  
**Цель:** заполнить окно между первым handshake-сообщением и follow-up через 10 минут коротким подтверждением без продажи. Архитектура VPN, существующий handshake flow, jobs 10m/2h/24h, CRM, WG lifecycle, IP pool и resend logic не менялись.

---

## 1. Где добавлен новый follow-up

- **Job:** `auto_handshake_short_confirmation(bot)` в `app/tg_bot_runner.py`.
- **Регистрация:** `asyncio.create_task(auto_handshake_short_confirmation(bot))` в `main()` рядом с остальными handshake/admin jobs.
- **Логика:** job раз в 60 сек запрашивает кандидатов через `db.get_handshake_short_confirmation_candidates(interval_seconds=60)`, отправляет каждому одно сообщение с кнопкой поддержки и создаёт запись `handshake_short_confirmation` (идемпотентно: один раз на подписку).

---

## 2. Notification type

Используется **`handshake_short_confirmation`**.

- Кандидаты: есть запись `handshake_user_connected`, её `sent_at` не менее 60 секунд назад, записи `handshake_short_confirmation` для этой подписки ещё нет.
- После успешной отправки вызывается `db.create_subscription_notification(..., notification_type="handshake_short_confirmation", ...)`.

---

## 3. Текст сообщения

Константа **`HANDSHAKE_SHORT_CONFIRMATION_TEXT`** в `app/messages.py`:

```
Если всё открывается нормально — VPN настроен правильно 👍

Если что-то не работает, нажми «🧑‍💻 Нужна помощь».
```

Коротко, без продажи, без рефералки, без длинных инструкций.

---

## 4. Кнопки

Под сообщением одна inline-кнопка (минималистичный вариант):

| Текст            | Действие   |
|------------------|------------|
| 🧑‍💻 Нужна помощь | `url=SUPPORT_URL` |

Кнопка «🔍 Проверить подключение» не добавлена, чтобы не усложнять flow.

---

## 5. Изменённые файлы

| Файл | Изменения |
|------|-----------|
| `app/db.py` | Функция `get_handshake_short_confirmation_candidates(interval_seconds=60)` — подписки с `handshake_user_connected` и без `handshake_short_confirmation`, с задержкой не менее 60 сек. |
| `app/messages.py` | Константа `HANDSHAKE_SHORT_CONFIRMATION_TEXT`. |
| `app/config.py` | `DB_JOB_LOCK_HANDSHAKE_SHORT_CONFIRMATION = 2010`. |
| `app/tg_bot_runner.py` | Импорт `HANDSHAKE_SHORT_CONFIRMATION_TEXT`; константы `HANDSHAKE_SHORT_CONFIRMATION_INTERVAL_SEC`, `HANDSHAKE_SHORT_CONFIRMATION_DELAY_SEC`; job `auto_handshake_short_confirmation`; регистрация в `main()`. |

---

## 6. Что не менялось

- Первое handshake-сообщение (`HANDSHAKE_USER_CONNECTED_TEXT`, клавиатура).
- Тексты и кнопки upsell (10m, 2h, 24h).
- Jobs: `auto_new_handshake_admin_notification`, `auto_handshake_followup_notifications` (10m/2h/24h/referral nudge).
- Логика `get_handshake_followup_candidates`, интервалы 10 min / 2h / 24h.
- CRM-отчёт, типы уведомлений в отчёте.
- Платёжная логика, resend_config, config_check_now.
- WireGuard lifecycle, IP allocation, пул IP.

---

Short confirmation follow-up added after first handshake.  
No sales CTA added to avoid message overload.
