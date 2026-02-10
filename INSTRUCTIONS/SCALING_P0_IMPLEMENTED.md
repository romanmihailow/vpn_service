# P0-изменения — реализовано

## 1) PostgreSQL: пул соединений
- Что было: `app/db.py` создавал новое соединение в `get_conn()` через `psycopg2.connect`.
- Что стало:
  - Добавлен пул `psycopg2.pool.ThreadedConnectionPool`.
  - `get_conn()` берёт соединение из пула и возвращает обратно.
  - Конфиг пула через `Settings`: `DB_POOL_MIN`, `DB_POOL_MAX`.
  - Инициализация пула выполнена в `app/db.py` на уровне модуля.
- Где:
  - `app/config.py` — новые настройки `DB_POOL_MIN`, `DB_POOL_MAX`.
  - `app/db.py` — пул и обновлённый `get_conn()`.
- Edge-case для ручного теста:
  - Запустить сервис при недоступной БД → убедиться, что ошибка понятна.
  - Нагрузочно: несколько одновременных запросов (webhook + bot) → нет всплеска соединений.

## 2) WireGuard: file lock + атомарная запись `wg0.conf`
- Что было: прямые `open(..., "a")` и `open(..., "w")` без блокировки.
- Что стало:
  - Введён lock-файл `WG_CONFIG_LOCK_PATH` (по умолчанию `/tmp/wg0.conf.lock`) с `fcntl.flock`.
  - Все изменения `wg0.conf` выполняются в критической секции.
  - Запись — атомарная через временный файл + `os.replace`.
- Где:
  - `app/config.py` — новый `WG_CONFIG_LOCK_PATH`.
  - `app/wg.py` — `_wg_config_lock()`, `_write_config_atomic()`, обновлённые `_append_peer_to_config` и `_remove_peer_from_config`.
- Edge-case для ручного теста:
  - Параллельно выдать несколько подписок → файл `wg0.conf` не ломается.
  - Удаление peer в момент добавления → конфиг консистентен.

## 3) /start и выдача VPN: защита от гонок IP/peer
- Что было: `generate_client_ip()` и `insert_subscription()` выполнялись без общей блокировки → риск дубликатов.
- Что стало:
  - Добавлен `pg_advisory_lock` с постоянным ключом `DB_IP_ALLOC_LOCK_ID`.
  - Лок берётся при выдаче IP (`wg.generate_client_ip`) и удерживается до вставки подписки.
  - Лок освобождается в `db.insert_subscription()` (даже при исключении).
  - В случае ошибки до вставки (например, `wg.add_peer`), лок освобождается.
- Где:
  - `app/config.py` — `DB_IP_ALLOC_LOCK_ID`.
  - `app/db.py` — `acquire_ip_allocation_lock`, `release_ip_allocation_lock`, вызов `release_ip_allocation_lock` в `insert_subscription`.
  - `app/wg.py` — `generate_client_ip` берёт лок, `add_peer` снимает лок при ошибке.
- Edge-case для ручного теста:
  - Два одновременных /start (или trial) → не должно быть одинаковых IP.
  - Принудительная ошибка `wg add_peer` → следующий запрос не блокируется.
  - Повторные оплаты через webhooks (быстрая серия) → нет дублей IP.

