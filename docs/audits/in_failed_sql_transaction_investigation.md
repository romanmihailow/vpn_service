# Investigation: InFailedSqlTransaction в ReferralTrial

## Симптомы

```
[ReferralTrial] Add peer (trial) pubkey=... ip=10.8.0.6/32 for tg_id=...
[ReferralTrial] Failed to issue referral trial for tg_id=...: InFailedSqlTransaction(
  'current transaction is aborted, commands ignored until end of transaction block'
)
```

Пользователи нажимают «Получить тестовый доступ», wg.add_peer проходит, но insert_subscription падает.

---

## Причина

### Цепочка вызовов

1. `acquire_ip_allocation_lock()` — берёт conn из пула, advisory lock, кладёт conn в `_ip_lock_ctx`
2. `allocate_free_ip_from_pool()` — `get_conn()` возвращает conn из ctx, SELECT/UPDATE, commit, выход
3. `wg.add_peer()` — успех
4. `insert_subscription()` — `get_conn()` снова возвращает conn из ctx

### Место сбоя

**`db.py`, `release_ip_allocation_lock()` (строки 46–61):**

```python
def release_ip_allocation_lock() -> None:
    ...
    conn = ctx["conn"]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s);", ...)
    finally:
        _POOL.putconn(conn)  # <-- conn возвращается в пул БЕЗ rollback
        _ip_lock_ctx.set(None)
```

**`insert_subscription()`** вызывает `release_ip_allocation_lock()` в `finally` (строка 615). Если `cur.execute` или `conn.commit()` падает:

- исключение не перехватывается, `rollback` не вызывается;
- `finally` всё равно выполняется → `release_ip_allocation_lock()` → `putconn(conn)`;
- соединение уходит в пул в состоянии **aborted transaction**.

Следующий запрос, получивший это соединение из пула, получает `InFailedSqlTransaction`.

### Почему `get_conn` не спасает

Для контекста IP-lock `get_conn()` возвращает conn из ctx и в `finally` делает только `pass` (строки 106–112):

```python
if ctx is not None:
    conn = ctx["conn"]
    try:
        yield conn
    finally:
        pass  # <-- нет rollback, conn не возвращается в пул
    return
```

То есть при ошибке в `insert_subscription` соединение не откатывается, но всё равно возвращается в пул через `release_ip_allocation_lock()`.

---

## Исправление

В `release_ip_allocation_lock()` перед возвратом соединения в пул делать `rollback`:

```python
def release_ip_allocation_lock() -> None:
    ...
    conn = ctx["conn"]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s);", (settings.DB_IP_ALLOC_LOCK_ID,))
    finally:
        try:
            conn.rollback()  # Сброс aborted transaction перед возвратом в пул
        except Exception:
            pass
        _POOL.putconn(conn)
        _ip_lock_ctx.set(None)
```

Так соединение всегда возвращается в пул в чистом состоянии, даже если предыдущая операция завершилась с ошибкой.

---

## Файлы

| Файл | Функция | Строки |
|------|---------|--------|
| `app/db.py` | `release_ip_allocation_lock` | 46–61 |
| `app/db.py` | `insert_subscription` | 591–615 (вызывает release в finally) |
| `app/db.py` | `get_conn` | 102–120 (для ip_lock ctx не делает rollback) |
