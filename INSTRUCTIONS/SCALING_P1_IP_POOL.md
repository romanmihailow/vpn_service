# P1.1 — VPN IP Pool (PostgreSQL)--

## Что было до P1
- IP выдавался через перебор диапазона `/16` в `generate_client_ip()`.
- На каждом запросе проверялось, свободен ли IP в БД.
- При заполнении диапазона время выдачи росло линейно.

## Что сделано
- Добавлена таблица `vpn_ip_pool (ip, allocated, allocated_at)` для пула IP.
- Выдача IP выполняется через `SELECT ... FOR UPDATE SKIP LOCKED` и `UPDATE` в рамках транзакции.
- В `generate_client_ip()` теперь используется `allocate_free_ip_from_pool()`.
- При ошибке до создания подписки IP освобождается через `release_ip_in_pool()`.

## Как это работает при параллельной нагрузке
- `pg_advisory_lock` остаётся как глобальный сериализатор операции выдачи.
- `FOR UPDATE SKIP LOCKED` блокирует выбранную строку, исключая дубликаты при конкуренции.
- Даже при нескольких воркерах один IP не может быть выделен дважды.

## Что НЕ изменилось
- Бизнес-логика выдачи подписок и Telegram-логика.
- WireGuard и формат peer-конфигов (/32 для клиентов).
- Действующие клиенты продолжают работать без изменений.

## Ограничения и риски
- Если ошибка происходит после выделения IP, но до записи подписки — IP освобождается вручную кодом.
- Advisory-lock оставлен, чтобы гарантировать последовательность выделения IP при высокой нагрузке.

## Как откатить / проверить

### Проверить свободные IP
```sql
SELECT ip
FROM vpn_ip_pool
WHERE allocated = FALSE
ORDER BY ip
LIMIT 10;
```

### Проверить занят IP конкретного пользователя
```sql
SELECT ip, allocated, allocated_at
FROM vpn_ip_pool
WHERE ip = '10.8.0.55'::inet;
```

### Освободить IP вручную
```sql
UPDATE vpn_ip_pool
SET allocated = FALSE,
    allocated_at = NULL
WHERE ip = '10.8.0.55'::inet;
```

## Admin: /admin_stats

### Что сделано
- Добавлена админ-команда `/admin_stats` (и кнопка в админ-меню).
- Показывает статистику пула, активных подписок и проверку консистентности.

### Какие запросы используются
- Сводка по пулу:
  - `SELECT COUNT(*) AS total, ... FROM vpn_ip_pool`
- Сводка по активным подпискам:
  - `SELECT COUNT(*) AS active_subs, COUNT(DISTINCT vpn_ip::inet) AS active_ips FROM vpn_subscriptions WHERE active = TRUE AND expires_at > NOW()`
- Проверка консистентности:
  - `subs_with_ip_not_in_pool`
  - `allocated_without_active_sub`

### Как использовать
- В Telegram как админ отправить `/admin_stats`.
- При проблемах смотреть:
  - `free` — если мало, диапазон скоро закончится;
  - `subs_with_ip_not_in_pool` и `allocated_without_active_sub` — должны быть 0.
