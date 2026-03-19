# Аудит: пользовательские настройки реферальных уведомлений

## 1. Текущие точки отправки

### referral_user_connected («Есть результат!»)
| Файл | Функция/контекст | Условие перед отправкой |
|------|-------------------|--------------------------|
| `app/tg_bot_runner.py` | `auto_new_handshake_admin_notification` (job) | `referrer_id and not has_subscription_notification(sub_id, "referral_user_connected")` → затем `send_referral_user_connected_notification` и `create_subscription_notification(..., "referral_user_connected", ...)` |

Одна точка вызова.

### referral_points_awarded (начисление баллов)
| Файл | Контекст | Условие перед отправкой |
|------|----------|--------------------------|
| `app/yookassa_webhook_runner.py` | Обработка payment.succeeded (основной сценарий) | `if not has_subscription_notification(subscription_id, "referral_points_awarded")` → send + create |
| `app/yookassa_webhook_runner.py` | Продление подписки (base_sub_id) | то же по base_sub_id |
| `app/heleket_webhook_runner.py` | Extension (ext_sub_id) | то же по ext_sub_id |
| `app/heleket_webhook_runner.py` | Продление (sub_id) | то же по sub_id |
| `app/heleket_webhook_runner.py` | Новая подписка (subscription_id) | то же по subscription_id |

Пять точек вызова (2 в YooKassa, 3 в Heleket).

---

## 2. Где проверять настройки

**Рекомендация: Вариант A — в местах вызова (перед send + create).**

- **Почему не B (внутри send_*):** если проверять только внутри `send_*`, то в call site после вызова всё равно вызывается `create_subscription_notification`. При выключенной настройке send_* молча выйдет без отправки, но запись в БД создастся → CRM будет считать «отправлено». Пришлось бы менять контракт send_* (возвращать bool «отправлено») и во всех 6 местах создавать запись только при True — это размазывает логику и даёт больше правок.
- **Почему A:** в каждом месте перед блоком «send + create» добавляем проверку `is_ref_connected_notification_enabled(referrer_id)` / `is_ref_points_notification_enabled(ref_tg_id)`. Если False — не вызываем ни send_*, ни create_subscription_notification. CRM остаётся корректным (запись = реально отправленное уведомление), одна точка решения на каждый call site.

---

## 3. Таблица настроек

В проекте есть `user_profiles` (is_referral_blocked, is_banned) — для блокировок, не для настроек уведомлений. Добавлять туда флаги уведомлений нежелательно (смешение ответственности).

**Рекомендация:** новая таблица `user_notification_preferences`:

- `telegram_user_id BIGINT PRIMARY KEY`
- `ref_connected_enabled BOOLEAN NOT NULL DEFAULT TRUE`
- `ref_points_enabled BOOLEAN NOT NULL DEFAULT TRUE`
- `updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

При отсутствии строки считаем оба уведомления включёнными (default TRUE).

---

## 4. Какие файлы изменятся

| Файл | Изменения |
|------|-----------|
| `app/db.py` | init_db: CREATE TABLE user_notification_preferences. Функции: get_or_create_user_notification_preferences, set_ref_connected_notification_enabled, set_ref_points_notification_enabled, is_ref_connected_notification_enabled, is_ref_points_notification_enabled. |
| `app/tg_bot_runner.py` | Команды: /notify_ref_status, /notify_ref_connected, /notify_ref_points. В job auto_new_handshake_admin_notification: проверка is_ref_connected_notification_enabled(referrer_id) перед отправкой и записью. |
| `app/yookassa_webhook_runner.py` | В двух блоках с referral_points_awarded: проверка is_ref_points_notification_enabled(ref_tg_id); при False не вызывать send и create. |
| `app/heleket_webhook_runner.py` | В трёх блоках с referral_points_awarded: та же проверка. |

bot.py, callback handlers, CRM, daily summary, тексты — не меняем.

---

## 5. Почему не ломается архитектура

- Отправка и запись в subscription_notifications остаются парными: запись создаётся только при реальной отправке.
- Callback-кнопки и типы referral_user_connected_ref_clicked / referral_points_awarded_* не трогаем; они привязаны к уже отправленным сообщениям.
- /crm_report считает по subscription_notifications — при отключённой настройке записей не будет, метрики останутся «только реально отправленные».
- Новая таблица изолирована; user_profiles и остальная логика не затрагиваются.

---

## 6. Минимальный patch-план

1. **db.py**  
   - В init_db добавить CREATE TABLE user_notification_preferences.  
   - Реализовать: get_or_create_user_notification_preferences(telegram_user_id) → dict; set_ref_connected_notification_enabled(telegram_user_id, enabled); set_ref_points_notification_enabled(telegram_user_id, enabled); is_ref_connected_notification_enabled(telegram_user_id) → bool (нет строки → True); is_ref_points_notification_enabled(telegram_user_id) → bool (нет строки → True).

2. **tg_bot_runner.py**  
   - Зарегистрировать Command("notify_ref_status"), Command("notify_ref_connected"), Command("notify_ref_points").  
   - notify_ref_status: показать состояние из get_or_create_* и текст с командами.  
   - notify_ref_connected: парсить аргумент (on/off), при неизвестном — подсказка; иначе set_ref_connected_notification_enabled + короткий ответ.  
   - notify_ref_points: то же для ref_points.  
   - В auto_new_handshake_admin_notification: после `if referrer_id and not has_subscription_notification(...)` добавить `and is_ref_connected_notification_enabled(referrer_id)` (через to_thread при необходимости); при False блок send + create не выполнять.

3. **yookassa_webhook_runner.py**  
   - В обоих блоках: условие расширить до «not has_subscription_notification(...) and is_ref_points_notification_enabled(ref_tg_id)»; внутри блока по-прежнему send + create.

4. **heleket_webhook_runner.py**  
   - В трёх блоках: то же условие с is_ref_points_notification_enabled(ref_tg_id).

5. Регистрация команд бота (set_bot_commands): при необходимости добавить три новые команды в список.

После этого реализую код по этому плану.
