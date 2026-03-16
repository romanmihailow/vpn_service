# Production Hotfix: DB Pool Expansion and Short Confirmation Disable

**Дата:** 2026-03-16  
**Тип:** Стабилизационный hotfix без изменения архитектуры и UX.

---

## 1. Root cause инцидента

В проде наблюдался **PoolError("connection pool exhausted")**:

- **DB_POOL_MAX** был равен **10**.
- Одновременно работают около **9 фоновых jobs** (auto_deactivate_expired, auto_notify_expiring, auto_revoke_unused_promo, auto_new_handshake_admin, auto_handshake_followup, auto_handshake_short_confirmation, auto_welcome_after_first_payment, auto_no_handshake_reminder, auto_config_checkpoint, auto_recently_expired_trial_followup).
- Каждая job при прогоне занимает минимум одно соединение из пула (advisory lock и запросы к БД).
- В пике все 10 соединений оказывались заняты jobs, и пользовательские запросы (/start, /status, /ref) не могли получить соединение → PoolError → fallback-ответы (текст без кнопок у /start, «Не удалось загрузить статус подписки» у /status).

---

## 2. Почему DB_POOL_MAX увеличен до 20

- Десяти соединений недостаточно для текущего набора jobs и пользовательского трафика.
- Увеличение до **20** даёт запас: jobs продолжают работать, а запросы пользователей получают свободные соединения.
- Логика чтения настроек не менялась: по-прежнему `os.getenv("DB_POOL_MAX", "20")` — при отсутствии переменной окружения используется 20.

---

## 3. Почему временно отключён auto_handshake_short_confirmation

- Job ограничена (batch 10, max_age 900), но в моменты прогона тоже занимает соединение и конкурирует с остальными задачами.
- Временное отключение снижает число одновременно работающих потребителей пула и ускоряет стабилизацию после увеличения пула.
- Функция и её код не удалялись и не менялись — закомментирован только вызов `asyncio.create_task(auto_handshake_short_confirmation(bot))` в main. Job можно снова включить после стабилизации и при необходимости дополнительного батчинга.

---

## 4. Что это временная стабилизация

- Hotfix направлен на снятие симптомов (pool exhausted, тишина/fallback у пользователей).
- Долгосрочно целесообразно: мониторинг пула, при необходимости дополнительные batch limits в других jobs, и повторное включение short confirmation после проверки нагрузки.

---

## 5. Что архитектура и UX не менялись

- Не менялись: архитектура приложения, логика handlers, WireGuard lifecycle, billing, CRM, UX-flow.
- Не выполнялся рефакторинг jobs.
- Изменены только: дефолтное значение DB_POOL_MAX в config и отключение запуска одной фоновой задачи.

---

Hotfix applied:

- DB_POOL_MAX increased to 20
- auto_handshake_short_confirmation temporarily disabled
- no architecture changes
