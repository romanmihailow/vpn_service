# Post-config checkpoint — hardening для production

**Дата:** 2025-03-15  
**Цель:** сделать post-config connection checkpoint устойчивым к рестарту процесса и деплою. Вместо одноразового `asyncio.create_task` используется персистентная регистрация в БД и отдельная background job.

---

## 1. Изменённые файлы

| Файл | Изменения |
|------|-----------|
| `app/config.py` | Добавлен `DB_JOB_LOCK_CONFIG_CHECKPOINT` (2008) для advisory lock job. |
| `app/db.py` | Добавлена функция `get_pending_config_checkpoints(interval_seconds=180)` — возвращает подписки с `config_checkpoint_pending`, у которых прошло не менее 180 с с момента выдачи конфига и ещё нет `config_checkpoint_sent`. |
| `app/bot.py` | Удалена `_run_config_checkpoint` (asyncio.create_task). Добавлены `_make_config_checkpoint_keyboard`, `send_config_checkpoint_message`. В `send_vpn_config_to_user` при `schedule_checkpoint=True` только регистрируется запись `config_checkpoint_pending` в БД, без создания задачи. Удалён импорт `wg`. |
| `app/tg_bot_runner.py` | Добавлена job `auto_config_checkpoint(bot)` с advisory lock; зарегистрирована в `main()` через `asyncio.create_task(auto_config_checkpoint(bot))`. Импорт `send_config_checkpoint_message` из `bot`. |

---

## 2. Хранение факта «конфиг выдан, нужен checkpoint»

- Используется существующая таблица **`subscription_notifications`**.
- При выдачи конфига (в `send_vpn_config_to_user` при `schedule_checkpoint=True`) вызывается  
  `db.create_subscription_notification(subscription_id, "config_checkpoint_pending", telegram_user_id, expires_at)`.
- В INSERT задаётся `sent_at = NOW()` (в текущей реализации это делается в самом SQL функции). Таким образом, **`sent_at` строки с типом `config_checkpoint_pending`** интерпретируется как время выдачи конфига («config issued at»).
- Регистрация идемпотентна: для одной пары `(subscription_id, notification_type)` действует UNIQUE, повторный вызов не меняет `sent_at`.

---

## 3. Background job: где и как работает

- **Функция:** `auto_config_checkpoint(bot)` в `app/tg_bot_runner.py`.
- **Запуск:** в `main()` после остальных фоновых задач:  
  `asyncio.create_task(auto_config_checkpoint(bot))`.
- **Логика цикла:**
  1. Берётся advisory lock `DB_JOB_LOCK_CONFIG_CHECKPOINT`; при неудаче job не стартует (логируется «already running in another instance»).
  2. В бесконечном цикле:
     - Вызов `db.get_pending_config_checkpoints(interval_seconds=180)` — подписки, у которых есть `config_checkpoint_pending`, `sent_at` старше 180 с и нет записи `config_checkpoint_sent`.
     - Один раз за итерацию запрашиваются handshake: `wg.get_handshake_timestamps()` (через `asyncio.to_thread` или `run_in_executor`).
     - Для каждой подписки из списка: проверка активности подписки и наличия `wg_public_key`; проверка handshake по текущему ключу; при отсутствии handshake — повторная проверка handshake непосредственно перед отправкой (см. ниже); затем вызов `send_config_checkpoint_message(telegram_user_id, subscription_id)` и запись `config_checkpoint_sent`.
  3. Ошибка обработки одной подписки логируется, цикл продолжается.
  4. После прохода по списку — `await asyncio.sleep(CONFIG_CHECKPOINT_JOB_INTERVAL_SEC)` (60 с), затем новая итерация.
- **Завершение:** в `finally` всегда вызывается `db.release_job_lock(settings.DB_JOB_LOCK_CONFIG_CHECKPOINT)`.

---

## 4. Защита от повторной отправки

- Перед отправкой проверяется отсутствие записи **`config_checkpoint_sent`** для данного `subscription_id`: выборка в `get_pending_config_checkpoints` исключает подписки, для которых уже есть такая запись.
- После успешной отправки сообщения вызывается  
  `db.create_subscription_notification(..., "config_checkpoint_sent", ...)`, поэтому при следующих проходах эта подписка больше не попадёт в выборку.
- Дубликаты по одной подписке не отправляются.

---

## 5. Устойчивость к рестартам

- Состояние хранится только в БД:
  - «Нужен checkpoint» — запись `config_checkpoint_pending` с `sent_at` = время выдачи конфига.
  - «Checkpoint уже отправлен» — запись `config_checkpoint_sent`.
- После рестарта или деплоя процесс больше не опирается на «живые» asyncio-задачи: job при следующем запуске заново читает `get_pending_config_checkpoints` и доставляет checkpoint по всем подпискам, для которых прошло ≥180 с и ещё нет handshake и нет `config_checkpoint_sent`.
- Никаких in-memory очередей или таймеров для доставки не используется.

---

## 6. Try/except и логирование

- **В job:** весь цикл обёрнут в `try`/`except`: при любой неожиданной ошибке логируется `log.error("[ConfigCheckpoint] Unexpected error in loop: %r", e)`, выполняется `await asyncio.sleep(CONFIG_CHECKPOINT_JOB_INTERVAL_SEC)` и цикл продолжается — задача не падает тихо.
- **Обработка одной подписки:** внутри цикла по кандидатам каждый элемент обрабатывается в своём `try`/`except`: при ошибке (БД, handshake, отправка) логируется `log.warning("[ConfigCheckpoint] Failed for sub_id=... tg_id=...: %r", ...)`, переход к следующей подписке.
- **В bot.py:** при неудачной регистрации checkpoint в `send_vpn_config_to_user` логируется `log.warning("[SendConfig] Failed to register checkpoint for tg_id=...: %r", ...)`; при успешной отправке в `send_config_checkpoint_message` — `log.info("[ConfigCheckpoint] Sent checkpoint to tg_id=... sub_id=...")`.

---

## 7. Подтверждение: остальная логика не изменена

- **AI-support:** intents, guardrails, actions, support router, service не менялись.
- **FSM:** не менялись.
- **Payment / referral:** не менялись; вызовы `send_vpn_config_to_user` по-прежнему с дефолтным `schedule_checkpoint=True` для первичной выдачи; в сценариях resend явно передаётся `schedule_checkpoint=False`.
- **Callback handlers** checkpoint (`config_check_ok`, `config_check_failed`, `config_check_resend`, `config_issue_*`) не менялись.
- **WireGuard provisioning** не трогался.
- Добавлены только: один lock ID в config, одна функция в db, замена «создать задачу» на «записать pending» в bot и новая job в tg_bot_runner в стиле существующих (advisory lock, цикл, sleep между итерациями и между подписками).
