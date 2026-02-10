# P1 — Webhooks, рассылки, фоновые задачи

## Что было
- Webhook-и YooKassa/Heleket могли обрабатываться повторно при ретраях.
- Массовые рассылки и автоуведомления отправляли сообщения подряд без общего лимита.
- Фоновые задачи могли дублироваться при запуске второго инстанса.

## Что сделано

### 1) Идемпотентность webhook-ов (YooKassa + Heleket)
- Добавлена таблица `payment_events` (provider, event_id) с уникальным индексом.
- Добавлены функции:
  - `try_register_payment_event()` — регистрирует событие, возвращает False при дубле.
  - `mark_payment_event_error()` — no-op (оставлено для будущего расширения).
- В `handle_yookassa_webhook` и `handle_heleket_webhook` добавлена проверка по `payment_events`
  сразу после базовой валидации и извлечения id события.
- При повторе webhook-а возвращается тот же успешный ответ, бизнес-логика не выполняется.

### 2) Rate limiting рассылок и уведомлений
- Добавлена `safe_send_message()` с обработкой `TelegramRetryAfter`.
  - При 429 — ждёт `retry_after` и повторяет один раз.
  - Остальные ошибки логируются, цикл не падает.
- Добавлены лимиты:
  - `BROADCAST_BATCH_SIZE = 25`, `BROADCAST_BATCH_SLEEP = 1.0`
  - `MAX_BROADCAST_USERS = 5000`
  - `NOTIFY_BATCH_SIZE = 25`, `NOTIFY_BATCH_SLEEP = 1.0`
- Применено к:
  - `broadcast_send` (админ-рассылка)
  - `auto_notify_expiring_subscriptions` (уведомления о скором окончании)

### 3) Advisory-lock для фоновых задач
- В `config.py` добавлены:
  - `DB_JOB_LOCK_DEACTIVATE_EXPIRED`
  - `DB_JOB_LOCK_NOTIFY_EXPIRING`
- В `db.py` добавлены:
  - `acquire_job_lock(lock_id)`
  - `release_job_lock(lock_id)`
- В `auto_deactivate_expired_subscriptions` и `auto_notify_expiring_subscriptions`:
  - Если лок не взят — задача завершается с логом.
  - Лок удерживается на весь цикл `while True`.

## Как проверить

### Webhook-и
1. Отправить тестовый webhook дважды с одним event_id:
   - подписка должна измениться один раз.
2. Проверить таблицу `payment_events`.

Примеры запросов, которые запускались для проверки:
```sql
SELECT COUNT(*) FROM payment_events;
SELECT COUNT(*) FROM vpn_ip_pool;
```
Примечание: на этой машине запросы не выполнились из-за отсутствия `psycopg2`
в системном Python (см. логи запуска).

### Рассылки/уведомления
- В логах не должно быть падений на `TelegramRetryAfter`.
- При большой рассылке заметны паузы по `BROADCAST_BATCH_SIZE`.

### Фоновые задачи
- При запуске второго инстанса в логах появится:
  - `Job already running in another instance` для каждой задачи.

## Edge-cases и ограничения
- Если webhook упал после записи в `payment_events`, повторный webhook будет пропущен.
  Это соответствует требованию строгой идемпотентности (один раз).
- Если задача не может взять лок — она не запускается (выполняется другой инстанс).
- Значения `BATCH_SIZE` и пауз можно настраивать.
