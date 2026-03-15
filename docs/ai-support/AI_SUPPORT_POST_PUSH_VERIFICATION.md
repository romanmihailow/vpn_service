# AI Support Post-Push Verification Review

Проверка текущего состояния кода после push: AI-support, post-config checkpoint, logging, guardrails, discovery hints. Код не изменялся — только чтение и верификация.

---

## 1. Checkpoint job

**Где запускается:** `app/tg_bot_runner.py`, функция `main()` (строка ~7058).

**Фрагмент кода:**

```python
# tg_bot_runner.py, main()
asyncio.create_task(auto_config_checkpoint(bot))
```

**Функция job:** `auto_config_checkpoint(bot: Bot)` в том же файле (строки 6937–7014).

**Advisory lock:** используется `db.acquire_job_lock(settings.DB_JOB_LOCK_CONFIG_CHECKPOINT)` в начале; в `finally` — `db.release_job_lock(settings.DB_JOB_LOCK_CONFIG_CHECKPOINT)`.

**Lock ID:** `app/config.py`: `DB_JOB_LOCK_CONFIG_CHECKPOINT: int = int(os.getenv("DB_JOB_LOCK_CONFIG_CHECKPOINT", "2008"))` — по умолчанию **2008**.

**Sleep interval:**  
- Между итерациями цикла: `CONFIG_CHECKPOINT_JOB_INTERVAL_SEC = 60` (строка 6934).  
- После обработки каждого кандидата внутри батча: `await asyncio.sleep(1)` (строка 7006).

**Вывод:** Job запускается в `main()` через `asyncio.create_task`, использует advisory lock 2008, интервал опроса 60 с. Риск только в том, что при падении процесса до `release_job_lock` лок освободится при закрытии соединения; для одной инстанции бота этого достаточно.

---

## 2. Checkpoint registration

**Где регистрируется `config_checkpoint_pending`:** `app/bot.py`, функция `send_vpn_config_to_user` (строки 158–178).

**Фрагмент кода:**

```python
# bot.py, send_vpn_config_to_user()
if schedule_checkpoint:
    try:
        sub = db.get_latest_subscription_for_telegram(telegram_user_id)
        if sub and sub.get("id"):
            db.create_subscription_notification(
                subscription_id=sub["id"],
                notification_type="config_checkpoint_pending",
                telegram_user_id=telegram_user_id,
                expires_at=sub.get("expires_at"),
            )
```

**Условия:** регистрация выполняется только при `schedule_checkpoint=True` (значение по умолчанию) и при наличии подписки с `id` у пользователя.

**Отключение при resend:**  
- `app/support/actions.py`, `action_resend_config`: вызов `send_vpn_config_to_user(..., schedule_checkpoint=False)` (строка 81).  
- `app/tg_bot_runner.py`, `config_resend_callback`: `send_vpn_config_to_user(..., schedule_checkpoint=False)` (строка 3124).  
- `app/tg_bot_runner.py`, `config_check_resend_callback`: `send_vpn_config_to_user(..., schedule_checkpoint=False)` (строка 3244).

**Вывод:** Регистрация checkpoint делается только при первой выдаче конфига; во всех трёх сценариях resend явно передаётся `schedule_checkpoint=False`. Риска, что resend создаёт новый checkpoint, нет.

---

## 3. Duplicate protection

**Механизм:**  
- В таблице `subscription_notifications` уникальный индекс по `(subscription_id, notification_type)` (db.py, init_db).  
- Для одной подписки может быть одна запись `config_checkpoint_pending` и одна `config_checkpoint_sent` (разные `notification_type`).  
- `create_subscription_notification` использует `INSERT ... ON CONFLICT DO NOTHING` — повторная вставка того же типа для той же подписки не создаёт вторую запись и не меняет `sent_at`.

**Выборка кандидатов:** `db.get_pending_config_checkpoints(interval_seconds=180)` возвращает только те подписки, у которых есть `config_checkpoint_pending`, `sent_at` старше 180 с и **нет** записи с `notification_type = 'config_checkpoint_sent'` для этого `subscription_id` (NOT EXISTS в SQL, строки 1779–1787).

**После отправки checkpoint:** job вызывает `db.create_subscription_notification(..., "config_checkpoint_sent", ...)`. При следующих проходах эта подписка больше не попадёт в выборку.

**Вывод:** Повторная отправка checkpoint по одной подписке исключена: и выборкой (config_checkpoint_sent), и единственной записью pending на подписку (UNIQUE). Дублирования нет.

---

## 4. Handshake suppression

**Где проверяется handshake:** `app/tg_bot_runner.py`, `auto_config_checkpoint`, внутри цикла по `candidates` (строки 6957–6988).

**Логика:**  
1. Один раз за итерацию вызывается `wg.get_handshake_timestamps()` (через `asyncio.to_thread` или `run_in_executor`).  
2. Для каждого кандидата: загрузка подписки, проверка `handshakes.get(pub_key, 0) > 0` → `continue` (skip).  
3. Перед отправкой — вторая проверка: снова вызывается `wg.get_handshake_timestamps()`, проверка `handshakes_refresh.get(pub_key, 0) > 0` → `continue`.  
4. Только при двух подряд «нет handshake» вызываются `send_config_checkpoint_message` и создание `config_checkpoint_sent`.

**Фрагмент (вторая проверка):**

```python
if hasattr(asyncio, "to_thread"):
    handshakes_refresh = await asyncio.to_thread(wg.get_handshake_timestamps)
else:
    handshakes_refresh = await asyncio.get_running_loop().run_in_executor(...)
if handshakes_refresh.get(pub_key, 0) > 0:
    continue
await send_config_checkpoint_message(...)
```

**Вывод:** Двойная проверка handshake (в начале батча и непосредственно перед отправкой) реализована. Риск отправить checkpoint пользователю с уже установленным handshake минимален (только в узком окне между второй проверкой и отправкой).

---

## 5. Checkpoint callbacks

| Callback | Файл | Функция | Действие | Переиспользование |
|----------|------|---------|----------|-------------------|
| `config_check_ok` | tg_bot_runner.py | `config_check_ok_callback` | Убирает кнопки, отправляет CONFIG_CHECK_SUCCESS, пишет `config_check_ok` в subscription_notifications | Собственная логика, без дублирования resend/handoff |
| `config_check_failed` | tg_bot_runner.py | `config_check_failed_callback` | Отправляет CONFIG_CHECK_FAIL и 4 кнопки (config_issue_*) | Только UI |
| `config_check_resend` | tg_bot_runner.py | `config_check_resend_callback` | Строит конфиг через `wg.build_client_config`, вызывает `send_vpn_config_to_user(..., schedule_checkpoint=False)` | **Использует существующий resend flow** (та же функция отправки конфига) |
| `config_issue_not_found` | tg_bot_runner.py | `config_issue_not_found_callback` | Текст + кнопка «Отправить настройки ещё раз» (callback `config_check_resend`) | Ведёт в тот же resend flow |
| `config_issue_import` | tg_bot_runner.py | `config_issue_import_callback` | Отправляет `HELP_INSTRUCTION` | Использует общую инструкцию из messages |
| `config_issue_connected_no_internet` | tg_bot_runner.py | `config_issue_connected_no_internet_callback` | `build_user_context(user_id)` → `action_vpn_not_working(context)` → ответ и reply_markup | **Использует существующий troubleshooting** (support/actions.action_vpn_not_working) |
| `config_issue_support` | tg_bot_runner.py | `config_issue_support_callback` | `action_human_request()` → текст и кнопка поддержки | **Использует существующий human handoff** (support/actions.action_human_request) |

**Вывод:** Resend идёт через общий `send_vpn_config_to_user`; «connected no internet» — через `action_vpn_not_working`; «support» — через `action_human_request`. Дублирования бизнес-логики нет.

---

## 6. Resend safety

**Проверенные места:**

1. **action_resend_config** (`app/support/actions.py`, строки 77–82):  
   `send_vpn_config_to_user(..., schedule_checkpoint=False)` — checkpoint не планируется.

2. **config_resend_callback** (`app/tg_bot_runner.py`, строки 3119–3125):  
   `send_vpn_config_to_user(..., schedule_checkpoint=False)` — checkpoint не планируется.

3. **config_check_resend_callback** (`app/tg_bot_runner.py`, строки 3239–3245):  
   `send_vpn_config_to_user(..., schedule_checkpoint=False)` — checkpoint не планируется.

**Вывод:** Во всех трёх точках resend передаётся `schedule_checkpoint=False`. Нет места, где resend создаёт новый checkpoint.

---

## 7. Guardrails verification

**Файл:** `app/support/guardrails.py`.

**Пороги и средние интенты:**  
- `CONF_HIGH = 0.8`, `CONF_MED = 0.5`, `CONF_LOW = 0.3`.  
- `ALLOWED_MEDIUM_INTENTS = frozenset({"smalltalk", "subscription_status"})`, `CONF_MEDIUM = 0.7`.

**Логика `should_handle_directly`:**  
1. `human_request` → сразу `(False, None)` (дальше в service обрабатывается handoff).  
2. Если intent в `ALLOWED_MEDIUM_INTENTS` и `confidence >= 0.7` → `(True, None)` (smalltalk и subscription_status доходят до action).  
3. Иначе при `confidence >= CONF_HIGH` (0.8) → `(True, None)`.  
4. Иначе при `confidence >= CONF_MED` → `(False, get_clarification_prompt())`.  
5. Иначе → `(False, get_safe_fallback())`.

**Service** (`app/support/service.py`):  
- `human_request` обрабатывается в начале (строки 115–118), handoff без прохождения guardrails по handle_directly.  
- Для остальных вызывается `should_handle_directly`; при `can_handle` выполняются соответствующие action (в т.ч. smalltalk, subscription_status).  
- Fallback и handoff_to_human выставляются при `not can_handle` и при unclear.

**Вывод:** Smalltalk и subscription_status с confidence 0.7 проходят до action за счёт ALLOWED_MEDIUM_INTENTS. human_request сразу ведёт к handoff. Fallback и уточняющие вопросы работают по порогам. Настройки guardrails корректны.

---

## 8. Logging verification

**support_ai.log:**  
- `app/logger.py`: логгер `support_ai`, файл `SUPPORT_AI_LOG_FILE` (по умолчанию `LOG_DIR/support_ai.log`, `LOG_DIR` из env или `/app/logs`).  
- Создаётся через `logging.FileHandler(SUPPORT_AI_LOG_FILE, encoding="utf-8")`.

**process_support_message** (`app/support/service.py`):  
- В конце (строки 218–228) один вызов `log.info` с полями: `tg_id`, `intent`, `conf`, `action`, `fallback`, `handoff`, `resend`, `vpn_diagnosis`.  
- Intent, action, fallback, handoff, resend_done и vpn_diagnosis логируются.

**Checkpoint:**  
- В job: при ошибке по подписке — `log.warning("[ConfigCheckpoint] Failed for sub_id=... tg_id=...: %r", ...)`; при ошибке цикла — `log.error("[ConfigCheckpoint] Unexpected error in loop: %r", e)`.  
- В `send_config_checkpoint_message` (bot.py): при успешной отправке — `log.info("[ConfigCheckpoint] Sent checkpoint to tg_id=... sub_id=...")`.  
- В `send_vpn_config_to_user` при регистрации pending — `log.debug` (при включённом DEBUG).

**Вывод:** Логирования достаточно для аналитики: support_ai (intent/action/fallback/handoff/vpn_diagnosis) и checkpoint (успех/ошибки) покрыты. Уровень — достаточный.

---

## 9. Support conversations

**Создание таблицы:** `app/db.py`, `init_db()`: в блоке `create_table_sql` есть `CREATE TABLE IF NOT EXISTS support_conversations (...)` с полями id, telegram_user_id, user_message, ai_response, detected_intent, confidence, mode, handoff_to_human, created_at (строки 341–356).

**Функция:** `log_support_conversation` в db.py (строки 367–397): INSERT в support_conversations с обрезкой длинных полей.

**Вызов:** `app/support/service.py`, в конце `process_support_message` (строки 204–216):

```python
try:
    db.log_support_conversation(
        telegram_user_id=user_id,
        user_message=text,
        ai_response=reply_text[:500] if reply_text else None,
        detected_intent=meta["intent"],
        confidence=meta["confidence"],
        mode="ai",
        handoff_to_human=meta["handoff_to_human"],
    )
except Exception as e:
    log.warning("Failed to log support conversation: %r", e)
```

**Вывод:** Таблица создаётся в init_db; логирование обёрнуто в try/except; при ошибке записи в БД только warning, ответ пользователю уже сформирован и не блокируется. Сбой логирования не ломает ответ.

---

## 10. Discovery hints

**SUPPORT_DISCOVERY_TEXT:**  
- Определён в `app/messages.py` (строки 53–60).  
- Используется в `app/tg_bot_runner.py`:  
  - при успешной регистрации по реф-ссылке: `REF_LINK_WELCOME_TEXT + "\n\n" + SUPPORT_DISCOVERY_TEXT` (строка 950);  
  - при «уже есть реферер» + активная подписка: то же (строка 963);  
  - при обычном `/start`: `START_TEXT + "\n\n" + SUPPORT_DISCOVERY_TEXT` (строка 984).

**SUPPORT_AFTER_CONFIG_HINT:**  
- Определён в `app/messages.py` (строки 62–68).  
- Используется в `app/bot.py` в `send_vpn_config_to_user`: `instruction_with_hint = CONNECTION_INSTRUCTION_SHORT + "\n\n" + SUPPORT_AFTER_CONFIG_HINT` (строка 148), это текст третьего сообщения после выдачи конфига (файл + QR + инструкция).

**Вывод:** Подсказка про вопросы текстом добавлена в /start (все три сценария) и в финальное сообщение после конфига. Тексты вынесены в messages.py. Логика start/referral не менялась, только конкатенация строк — поведение сохранено.

---

## 11. Manual verification checklist

1. Написать боту «привет» → ожидать ответ smalltalk (кратко про помощника и возможности).  
2. Написать «статус подписки» или «до когда подписка» → ожидать ответ subscription_status (дата/тип или «нет активной подписки»).  
3. Написать «вышли конфиг» (с активной подпиской) → конфиг приходит, в последнем сообщении есть подсказка SUPPORT_AFTER_CONFIG_HINT.  
4. После получения конфига подождать 3–4 минуты без подключения к VPN → ожидать сообщение «Удалось подключиться к VPN?» с тремя кнопками.  
5. Подключиться к VPN до истечения ~3 мин → checkpoint не должен прийти (handshake suppression).  
6. Нажать «Да, всё работает» в checkpoint → ответ «Отлично 👌…», кнопки исчезают.  
7. Получить checkpoint, нажать «Нет, не получилось» → появляются 4 варианта; нажать «VPN подключён, но сайты не открываются» → текст troubleshooting и кнопка поддержки (action_vpn_not_working).  
8. В том же меню нажать «Нужна помощь» → текст и кнопка в поддержку (action_human_request).  
9. Нажать «Отправить настройки ещё раз» в checkpoint → конфиг приходит повторно, новый checkpoint через 3 мин не должен планироваться (resend, schedule_checkpoint=False).  
10. Написать «позови оператора» или «нужен человек» → сразу ответ с кнопкой поддержки (human_request, без уточнений).

---

## 12. Final verdict

**VERIFIED SAFE**

- Checkpoint job запускается в main с advisory lock и интервалом 60 с; регистрация pending только при первой выдаче конфига; при resend везде передаётся `schedule_checkpoint=False`.  
- Защита от дублей: одна запись pending на подписку и проверка config_checkpoint_sent в выборке; двойная проверка handshake перед отправкой.  
- Callback’и checkpoint переиспользуют resend, action_vpn_not_working и action_human_request; дублирования логики нет.  
- Guardrails: smalltalk и subscription_status с confidence 0.7 доходят до action; human_request сразу handoff; fallback по порогам.  
- Логирование: support_ai (intent/action/fallback/handoff/vpn_diagnosis), checkpoint (успех/ошибки), support_conversations в БД с try/except.  
- Discovery hints в /start и после конфига подключены, тексты в messages.py.

Рекомендуется после деплоя пройти пункты раздела 11 (Manual verification checklist) для уверенности в работе в реальном окружении.
