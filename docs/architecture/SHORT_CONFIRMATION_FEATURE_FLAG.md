# Short Confirmation Feature Flag

**Дата:** 2026-03-16  
**Цель:** Подготовить безопасное включение `auto_handshake_short_confirmation` через управляемый feature flag.

---

## 1. Какой feature flag добавлен

**ENABLE_HANDSHAKE_SHORT_CONFIRMATION** (bool)

- Читается из переменной окружения `ENABLE_HANDSHAKE_SHORT_CONFIRMATION`.
- Значение `True`, если env содержит: `"1"`, `"true"`, `"True"`.
- По умолчанию: **False** (при отсутствии переменной или любом другом значении).

**Файл:** `app/config.py`

---

## 2. Как теперь запускается job

В `main()` (app/tg_bot_runner.py) job запускается только при включённом флаге:

```python
if settings.ENABLE_HANDSHAKE_SHORT_CONFIRMATION:
    asyncio.create_task(auto_handshake_short_confirmation(bot))
```

- Ручное закомментирование убрано.
- Запуск полностью управляется настройкой.
- Без флага job не создаётся и не выполняется.

---

## 3. По умолчанию job выключена

- При отсутствии `ENABLE_HANDSHAKE_SHORT_CONFIRMATION` в .env или при значении `"0"` job **не запускается**.
- Чтобы включить: добавить в .env  
  `ENABLE_HANDSHAKE_SHORT_CONFIRMATION=1`  
  (или `true` / `True`) и перезапустить приложение.

---

## 4. Batch/max_age не менялись

- `HANDSHAKE_SHORT_CONFIRMATION_BATCH_SIZE = 10`
- `HANDSHAKE_SHORT_CONFIRMATION_MAX_AGE_SEC = 900`

Параметры job остались прежними.

---

Short confirmation re-enable prepared via feature flag.  
Default state: disabled.
