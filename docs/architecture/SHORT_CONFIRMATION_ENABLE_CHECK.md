# Short Confirmation Enable Check

**Дата:** 2026-03-16  
**Цель:** Подтвердить готовность `auto_handshake_short_confirmation` к безопасному включению через feature flag.

---

## 1. Проверка защиты от дублей

- **Отдельный notification type:** `handshake_short_confirmation` — используется и при проверке, и при записи.
- **Проверка перед отправкой:** в SQL `get_handshake_short_confirmation_candidates` — `NOT EXISTS (SELECT 1 FROM subscription_notifications n2 WHERE n2.subscription_id = n.subscription_id AND n2.notification_type = 'handshake_short_confirmation')`. В выборку попадают только подписки, для которых ещё нет такой записи.
- **Запись после отправки:** при успешной отправке вызывается `db.create_subscription_notification(..., notification_type="handshake_short_confirmation")`.

**Вывод:** одна подписка получает максимум одно short confirmation сообщение. Дополнительная защита не требуется.

---

## 2. Проверка max_age

- В `get_handshake_short_confirmation_candidates` передаётся `max_age_seconds=900`.
- SQL-условие: `n.sent_at >= NOW() - INTERVAL '1 second' * max_age_seconds` — учитывается `sent_at` первого handshake (`handshake_user_connected`).
- Кандидатами становятся только те, у кого первый handshake был **не более 15 минут назад**.

**Вывод:** старые пользователи (например, сутки назад) не попадают в выборку. Ограничение по времени реализовано в SQL, менять ничего не нужно.

---

## 3. Проверка batch_size

- **HANDSHAKE_SHORT_CONFIRMATION_BATCH_SIZE = 10** (константа в tg_bot_runner.py).
- Обработка: `for row in candidates[:HANDSHAKE_SHORT_CONFIRMATION_BATCH_SIZE]` — не более 10 кандидатов за один прогон.

**Вывод:** batch limit уже реализован, изменений не требуется.

---

## 4. Добавленные логи

- `log.info("[ShortConfirm] job started")` — при старте job (после получения lock).
- `log.info("[ShortConfirm] candidates=%s", len(candidates))` — количество кандидатов перед обработкой.
- `log.info("[ShortConfirm] sent tg_id=%s sub_id=%s", tg_id, sub_id)` — при успешной отправке.
- `log.exception("[ShortConfirm] failed to record sub_id=%s", sub_id)` — при ошибке записи notification.
- `log.exception("[ShortConfirm] unexpected error: %r", e)` — при неожиданной ошибке в цикле.

В логах не фигурируют лишние данные, только id.

---

## 5. Job безопасна к включению

- Защита от дублей реализована.
- Ограничение по времени (max_age 900 сек) реализовано.
- Batch limit (10 за прогон) реализован.
- Добавлено базовое логирование.
- Текст, кнопки, другие jobs, DB pool, handlers, UX flow не менялись.

Job можно безопасно включить, установив `ENABLE_HANDSHAKE_SHORT_CONFIRMATION=1` в .env.

---

Short confirmation validated for safe enable.
