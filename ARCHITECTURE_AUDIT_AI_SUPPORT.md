# Архитектурный и технический аудит VPN-сервиса MaxNet для проектирования AI-support

Дата: 2025-03-13

---

# 1. Общая структура проекта

| Путь | Назначение |
|------|------------|
| `app/tg_bot_runner.py` | Точка входа (CMD в Dockerfile). Aiogram router, polling, handlers, 5 background jobs. ~6365 строк. |
| `app/main.py` | Отдельный FastAPI: Tribute webhook, admin endpoints (`/admin/subscriptions`, `/admin/subscriptions/{id}/deactivate`). Запускается отдельно (uvicorn), не в Docker CMD. |
| `app/bot.py` | Отправка конфигов, QR, уведомлений. `send_vpn_config_to_user`, `send_subscription_extended_notification`, `send_referral_reward_notification`, `send_subscription_expired_notification`. |
| `app/db.py` | PostgreSQL, connection pool, схемы, бизнес-логика БД. Advisory locks для IP и jobs. ~3500 строк. |
| `app/wg.py` | WireGuard: ключи, peer, handshake timestamps, build_client_config. |
| `app/config.py` | Pydantic Settings, env. |
| `app/logger.py` | Логгеры: vpn_service, yookassa, heleket, promo. Пишут в файлы `logs/*.log`. |
| `app/format_admin.py` | Форматирование админ-уведомлений: `fmt_user_line`, `fmt_ref_display`, `fmt_date` (МСК). |
| `app/messages.py` | Тексты: REF_LINK_WELCOME_TEXT, CONNECTION_INSTRUCTION_SHORT, SUPPORT_BUTTON_TEXT, SUPPORT_URL, HELP_INSTRUCTION и др. |
| `app/yookassa_client.py` | `create_yookassa_payment()`. |
| `app/yookassa_webhook_runner.py` | aiohttp app, `create_app()`, `/yookassa/webhook`. Импортирует Heleket handler. |
| `app/heleket_client.py` | `create_heleket_payment()`. |
| `app/heleket_webhook_runner.py` | `handle_heleket_webhook`, `/heleket/webhook`. |
| `app/promo_codes.py` | `generate_promo_codes`, `build_insert_sql_for_postgres`, `PromoGenerationParams`. |
| `scripts/` | Диагностические скрипты: `delete_user_for_test.py`, `diagnose_handshake_notification.py`, `list_no_handshake_since.py`, `send_config_with_promo.py`, `fix_duplicate_ips.py`, `verify_points_consistency.py`. |
| `tests/` | `test_yookassa_idempotency.py`, `conftest.py`. |

**Точка входа:** `python -m app.tg_bot_runner` (Dockerfile CMD). В `main()`:
- `db.init_db()`
- Bot + Dispatcher, `dp.include_router(router)`
- `bot.delete_webhook()` (polling)
- `set_bot_commands(bot)`
- `asyncio.create_task()` для 5 фоновых задач
- `create_app()` (aiohttp) на порту 8080 — YooKassa + Heleket webhooks
- `dp.start_polling(bot)`

**Tribute (app/main.py):** Отдельный процесс. Не входит в docker-compose CMD. Endpoints: `/`, `/health`, `/admin/subscriptions`, `/admin/subscriptions/{id}/deactivate`, `/tribute/webhook`.

---

# 2. Telegram-архитектура

| Файл | Функция | Что делает | Зависимости |
|------|---------|------------|-------------|
| `tg_bot_runner.py` | `cmd_start` (CommandStart) | Парсит deep-link. `db.register_referral_start()` → REF_LINK_WELCOME + REF_TRIAL_KEYBOARD или START_TEXT + get_start_keyboard. При `already_has_referrer` + active sub — onboarding + resend. | db, messages |
| `tg_bot_runner.py` | `cmd_help` | Отправляет HELP_INSTRUCTION | messages |
| `tg_bot_runner.py` | `cmd_support` | Отправляет SUPPORT_TEXT (@MaxNet_VPN_Support) | — |
| `tg_bot_runner.py` | `cmd_terms`, `cmd_privacy` | Отправка файлов TERMS.md, PRIVACY.md | — |
| `tg_bot_runner.py` | `cmd_my_id` | Показывает telegram_user_id | — |
| `tg_bot_runner.py` | `cmd_subscription`, `cmd_buy`, `cmd_buy_points`, `cmd_buy_crypto` | Открывают pay/points/heleket flows | db, keyboards |
| `tg_bot_runner.py` | `cmd_status` | Подписка, expires_at, кнопки get_status_keyboard(sub_id) | db, wg |
| `tg_bot_runner.py` | `cmd_ref` | Реферальная ссылка, статистика | db |
| `tg_bot_runner.py` | `config_resend_callback` | callback `config:resend:<sub_id>` — resend конфиг | db, wg, bot.send_vpn_config_to_user |
| `tg_bot_runner.py` | `ref_trial_claim_callback` | callback `ref_trial:claim` — триал или resend | db, wg, try_give_referral_trial_7d, send_vpn_config_to_user |
| `tg_bot_runner.py` | `ref_open_from_ref`, `ref_open_from_notify` | Кнопка «Пригласить друга», share text | db |
| `tg_bot_runner.py` | `pay:open`, `points:open`, `heleket:open`, `promo:open` | Открытие тарифов, FSM | db, create_yookassa_payment, create_heleket_payment |
| `tg_bot_runner.py` | `pay:tariff:`, `points:tariff:`, `heleket:tariff:` | Выбор тарифа, создание платежа или списание баллов | db, wg, bot |
| `tg_bot_runner.py` | PromoStates, DemoRequest, PromoAdmin, AdminAddSub, Broadcast, BroadcastList, BonusList | FSM для промо, демо, админки | db |
| `tg_bot_runner.py` | Множество `adm:`, `adminlist:`, `addsub:`, `admcmd:` callbacks | Админ-действия: активация, деактивация, удаление, add_sub, regenerate_vpn, resend_config | db, wg, bot |

**Inline keyboards:** SUBSCRIBE_KEYBOARD, REF_TRIAL_KEYBOARD, get_start_keyboard(telegram_user_id), SUBSCRIPTION_RENEW_KEYBOARD, get_status_keyboard(sub_id), TARIFF_KEYBOARD, HELEKET_TARIFF_KEYBOARD, POINTS_TARIFF_KEYBOARD. В no_handshake reminders: «📱 Получить настройки» + «🧑‍💻 Нужна помощь» (url).

**Отправка конфига:** Единственная точка — `bot.send_vpn_config_to_user(telegram_user_id, config_text, caption)`. Файл → QR → инструкция + кнопка SUPPORT_URL. Задержка 0.7 сек между сообщениями.

**Support-related handlers:** Только `cmd_support` — статический текст. Отдельных support FSM / support-router / support conversation state — **не найдено**.

---

# 3. Выдача VPN-конфига

## Сводная таблица

| Сценарий | Entry point | Функции | Новая подписка | Новый peer | Resend |
|----------|-------------|---------|----------------|------------|--------|
| Referral trial | ref_trial_claim_callback | try_give_referral_trial_7d → wg.add_peer, db.insert_subscription, send_vpn_config_to_user | Да | Да | Да (при active sub) |
| YooKassa payment.succeeded | process_yookassa_event (webhook) | wg.add_peer, db.insert_subscription, send_vpn_config_to_user | Да | Да | Нет (идемпотентность по event_id) |
| Heleket webhook | handle_heleket_webhook | wg.add_peer, db.insert_subscription, send_vpn_config_to_user | Да | Да | Нет |
| Tribute new_subscription / new_donation | handle_new_subscription, handle_new_donation (app/main.py) | wg.add_peer, db.insert_subscription, bot.send_vpn_config_to_user | Да | Да | Да (при duplicate webhook — resend) |
| Оплата баллами | points_tariff_callback | wg.add_peer, db.insert_subscription, send_vpn_config_to_user | Да | Да | Нет |
| Промокод (новая подписка) | promo_code_apply (PromoStates) | wg.add_peer, db.insert_subscription, send_vpn_config_to_user | Да | Да | Нет |
| Промокод (продление) | promo_code_apply | db.update_subscription_expiration, send_vpn_config_to_user | Нет | Нет | По сути resend |
| config:resend | config_resend_callback | db.get_subscription_by_id, wg.build_client_config, send_vpn_config_to_user | Нет | Нет | Да |
| Admin add_sub | addsub:period callback | wg.add_peer, db.insert_subscription, send_vpn_config_to_user | Да | Да | Нет |
| Admin regenerate_vpn | cmd_admin_regenerate_vpn | wg.remove_peer, wg.add_peer, db.update keys, send_vpn_config_to_user | Нет | Обновление | Да |
| Admin resend_config | cmd_admin_resend_config | db.get_latest_subscription, wg.build_client_config, send_vpn_config_to_user | Нет | Нет | Да |
| No-handshake reminder | auto_no_handshake_reminder | Кнопка config:resend — при нажатии config_resend_callback | Нет | Нет | Да |

**Сбор config_text:** `wg.build_client_config(client_private_key, client_ip)`. Генерация QR: `bot.generate_qr_image_bytes(config_text)`.

**Переотправка существующего конфига:** Возможна через `config:resend`, `ref_trial:claim` (если active sub), admin resend, Tribute duplicate. Конфиг собирается из `sub["wg_private_key"]`, `sub["vpn_ip"]`.

---

# 4. Подписки и статусы доступа

| Файл | Модель/функция | Поля / смысл |
|------|----------------|--------------|
| `db.py` | Таблица `vpn_subscriptions` | id, tribute_user_id, telegram_user_id, telegram_user_name, subscription_id, period_id, period, channel_id, channel_name, vpn_ip, wg_private_key, wg_public_key, created_at, expires_at, active, last_event_name |
| `db.py` | `get_latest_subscription_for_telegram(telegram_user_id)` | Последняя подписка по tg_id (ORDER BY created_at DESC). Возвращает dict или None. |
| `db.py` | `get_subscription_by_id(sub_id)` | Подписка по id. |
| `db.py` | `get_subscription_by_event(event_name)` | По last_event_name. |
| `db.py` | `deactivate_subscription_by_id`, `delete_subscription_by_id` | Деактивация/удаление. |

**Типы (channel_name, last_event_name, period):**
- Referral trial: `Referral trial`, `referral_free_trial_7d`, `referral_trial_7d`
- YooKassa: `YooKassa`, `yookassa_payment_succeeded_{payment_id}`, период тарифа
- Heleket: `Heleket`, `heleket_payment_paid_*`, `heleket_*`
- Points: `Points balance`, `points_payment_*`
- Promo: `Promo code`, `promo_code`
- Tribute: `lbs` и др., `new_subscription`, `new_donation`
- Admin: `Admin manual`, `admin_*`, `admin_3d`, `admin_7d`, …

**Активность:** `active = TRUE` и `expires_at > NOW()`.

**Trial/promo vs paid:** По `last_event_name` (`referral_free_trial_7d`, `promo%`) или channel_name. В `get_subscriptions_for_no_handshake_reminder` фильтр: только `referral_free_trial_7d` или `promo%`.

---

# 5. Логика handshake

| Файл | Функция | Что делает | Данные |
|------|---------|------------|--------|
| `wg.py` | `get_handshake_timestamps()` | `wg show wg0 latest-handshakes` → dict[public_key, unix_timestamp] | WireGuard ядро |
| `tg_bot_runner.py` | `_not_connected(pubkey)` | `handshakes.get(pubkey, 0) == 0` | Результат get_handshake_timestamps |
| `tg_bot_runner.py` | `auto_new_handshake_admin_notification` | Подписки без `new_handshake_admin`, где handshake > 0. Отправка админу. | db.get_subscriptions_for_new_handshake_admin, wg.get_handshake_timestamps |
| `tg_bot_runner.py` | `auto_no_handshake_reminder` | Подписки trial/promo без handshake. Интервалы: 2h, 24h, 5d. | db.get_subscriptions_for_no_handshake_reminder(reminder_type) |
| `db.py` | `get_subscriptions_for_no_handshake_reminder(reminder_type)` | Только `last_event_name = 'referral_free_trial_7d' OR LIKE 'promo%'`. Исключает уже отправленные notification_type. | vpn_subscriptions, subscription_notifications |
| `db.py` | `get_subscriptions_for_new_handshake_admin()` | Все типы подписок (включая paid). Фильтр: нет записи `new_handshake_admin`. | vpn_subscriptions, subscription_notifications |

**Интервалы no_handshake:** 2 часа, 24 часа, 5 дней. Идемпотентность: `subscription_notifications` (subscription_id, notification_type) UNIQUE.

**Paid users:** В no_handshake reminders **исключены** — только trial и promo. В new_handshake_admin — **включены**.

---

# 6. Напоминания и фоновые задачи

| Задача | Где определена | Lock | Периодичность | Действие |
|--------|----------------|------|---------------|----------|
| `auto_deactivate_expired_subscriptions` | tg_bot_runner.py | DB_JOB_LOCK_DEACTIVATE_EXPIRED | Цикл + sleep | Деактивация подписок с expires_at < NOW, remove_peer |
| `auto_notify_expiring_subscriptions(bot)` | tg_bot_runner.py | DB_JOB_LOCK_NOTIFY_EXPIRING | ~1h | Уведомления об истечении, SUBSCRIPTION_RENEW_KEYBOARD |
| `auto_revoke_unused_promo_points()` | tg_bot_runner.py | DB_JOB_LOCK_REVOKE_UNUSED_PROMO | 1 раз в сутки | Отзыв неиспользованных промо-баллов |
| `auto_new_handshake_admin_notification(bot)` | tg_bot_runner.py | DB_JOB_LOCK_NEW_HANDSHAKE_ADMIN | 120 сек | Админ: «Новых подписчиков с handshake», subscription_notifications |
| `auto_no_handshake_reminder(bot)` | tg_bot_runner.py | DB_JOB_LOCK_NO_HANDSHAKE_REMINDER | 3600 сек | Триал/промо без handshake: 2h, 24h, 5d напоминания |

Запуск: `asyncio.create_task()` в `main()`. Конкурентность: один экземпляр по advisory lock.

Support-related jobs — **не найдено**.

---

# 7. Оплаты

| Провайдер | Entry point | Success flow | Failure / retry |
|-----------|-------------|--------------|-----------------|
| YooKassa | POST /yookassa/webhook (aiohttp:8080) | payment.succeeded → try_register_payment_event → wg.add_peer, insert_subscription, send_vpn_config_to_user, apply_referral_rewards | payment.canceled → deactivate_subscription, remove_peer. Идемпотентность по payment_events. |
| Heleket | POST /heleket/webhook (aiohttp:8080) | IP 31.133.220.8, verify_heleket_signature → add_peer, insert_subscription, send_vpn_config_to_user | Логирование ошибок. |
| Tribute | POST /tribute/webhook (FastAPI, отдельный процесс) | HMAC trbt-signature → new_subscription / new_donation → add_peer, insert_subscription, send_vpn_config_to_user | Дубликат по subscription_id → resend config без новой подписки. |

**Риски:** Если конфиг не отправился после оплаты — нет автоматического retry. Пользователь может нажать «Получить настройки» (config:resend) или написать в поддержку. Post-payment recovery path — только через ручной resend или поддержку.

---

# 8. Referral и promo логика

| Файл | Функция | Действие | Ограничения |
|------|---------|----------|-------------|
| db.py | `register_referral_start(invited_telegram_user_id, referral_code)` | create_referral_link. Возвращает ok/error (already_has_referrer, code_not_found, self_ref). | — |
| db.py | `user_can_claim_referral_trial(telegram_user_id)` | True если: есть referrer, не было referral trial, нет active sub | get_referrer_telegram_id, has_referral_trial_subscription, get_latest_subscription_for_telegram |
| db.py | `has_referral_trial_subscription(telegram_user_id)` | Есть подписка с last_event_name = 'referral_free_trial_7d' | — |
| db.py | `apply_referral_rewards_for_subscription` | Начисление баллов по уровням за оплату реферала | — |
| tg_bot_runner.py | `try_give_referral_trial_7d` | add_peer, insert_subscription, send_vpn_config_to_user | Только если нет active sub и не было referral trial |
| db.py | `apply_promo_code_to_latest_subscription`, `apply_promo_code_without_subscription` | Продление или новая подписка | promo_code_usages, max_uses, valid_until |
| tg_bot_runner.py | PromoStates.waiting_for_code | Ввод кода, apply_promo | — |

Промо-подписки отличаются `last_event_name LIKE 'promo%'`. Trial — `referral_free_trial_7d`. Повторный trial — **нельзя** (has_referral_trial_subscription).

---

# 9. Тексты и UX-слой

| Расположение | Контент |
|--------------|---------|
| `app/messages.py` | REF_LINK_WELCOME_TEXT, REF_TRIAL_BUTTON_TEXT, CONNECTION_INSTRUCTION_SHORT, REF_TRIAL_CONFIG_CAPTION, CONFIG_QR_CAPTION, DEFAULT_CONFIG_CAPTION, SUPPORT_BUTTON_TEXT, SUPPORT_URL, HELP_INSTRUCTION |
| `tg_bot_runner.py` | START_TEXT, SUPPORT_TEXT, SUBSCRIPTION_TEXT, REF_INFO_TEXT, ADMIN_INFO_TEXT. Тексты no_handshake: _make_2h_text, _make_24h_text, _make_5d_text. |

**Централизация:** Частичная. `messages.py` — онбординг, инструкции, поддержка. Остальные тексты — в tg_bot_runner.

**Дублирование:** @MaxNet_VPN_Support упоминается в SUPPORT_URL, HELP_INSTRUCTION, SUPPORT_TEXT. Кнопка «Нужна помощь» — SUPPORT_BUTTON_TEXT.

**Для AI assistant:** HELP_INSTRUCTION, CONNECTION_INSTRUCTION_SHORT, REF_LINK_WELCOME_TEXT уже можно переиспользовать. Тексты напоминаний — в tg_bot_runner, не в messages.

---

# 10. Данные пользователя для AI

**Уже есть в коде/БД:**

| Данные | Источник |
|--------|----------|
| telegram_user_id | message.from_user.id, callback.from_user.id |
| username (telegram_user_name) | user_profiles, vpn_subscriptions.telegram_user_name |
| subscription status | db.get_latest_subscription_for_telegram → active, expires_at |
| expires_at | vpn_subscriptions |
| trial/promo/paid | last_event_name, channel_name |
| handshake status | wg.get_handshake_timestamps() по wg_public_key подписки |
| points balance | db.get_user_points_balance |
| referral status | db.get_referrer_telegram_id, get_or_create_referral_info |
| reminder state | subscription_notifications (no_handshake_2h, no_handshake_24h, no_handshake_5d) |

**Можно добавить:** last_config_sent_at (не найдено; можно вывести из subscription_notifications или добавить поле). last_payment — косвенно через last_event_name (yookassa/heleket ID в event_name).

**Не найдено:** support conversation history, audit trail диалогов, intent classification, support state machine.

---

# 11. Возможные точки интеграции AI assistant

| Точка | Почему подходит | Риск |
|-------|-----------------|------|
| Middleware перед `dp.start_polling` | Перехват всех message/callback до роутеров | Нужна логика fallback при отсутствии intent |
| Перед `cmd_support` | Можно заменить статичный ответ на AI | Не трогать pay/promo/config flows |
| Новый handler для произвольного текста (после всех команд) | Сообщения без команды — кандидат для AI | Конфликт с FSM (PromoStates, DemoRequest и т.д.) |
| Отдельная команда `/ask` или `/help_ai` | Явный вход в AI-диалог | Без изменения существующих handlers |
| config:resend failure | При ошибке отправки — подсказка от AI | Требует доработки callback |
| После send_vpn_config_to_user | Уже есть кнопка «Нужна помощь» — можно логировать переход | Только логирование, без изменения flow |
| Хранение состояния | FSM aiogram (StatesGroup) или отдельная таблица support_conversations | Сейчас нет — нужно добавлять |
| Handoff to human | URL SUPPORT_URL уже есть; можно добавить «Перейти к оператору» | Низкий |
| Логирование «плохих» ответов | Добавить логирование в vpn_service.log при определённых intent | Низкий |

---

# 12. Риски для AI-support

| Риск | Где | Проблема для AI-support | Критичность |
|------|-----|-------------------------|-------------|
| Логика в одном файле (~6k строк) | tg_bot_runner.py | Сложно изолировать support flow, легко затронуть pay/config | high |
| Нет единого user context | Разброс по db.get_*, wg.get_handshake | AI нужен снимок «пользователь» из нескольких источников | high |
| Нет support state / conversation | — | Нет audit trail, нет контекста диалога | high |
| FSM без разделения support | PromoStates, DemoRequest и др. | Произвольный текст может попасть в чужой FSM | medium |
| Tribute в отдельном процессе | app/main.py | Конфиг может уйти из Tribute, а бот не знать — рассинхрон | medium |
| Нет post-payment retry | yookassa/heleket webhooks | Конфиг не ушёл — только ручной resend | medium |
| Тексты напоминаний в tg_bot_runner | _make_2h_text и др. | Не в messages.py — сложнее переиспользовать для AI | low |
| send_vpn_config_to_user — единственная точка | bot.py | Хорошо для консистентности; менять нужно осторожно | low |

---

# 13. Функции и файлы для детального проектирования

```
app/tg_bot_runner.py :: cmd_start — вход пользователя, referral flow
app/tg_bot_runner.py :: cmd_support — текущий support handler
app/tg_bot_runner.py :: config_resend_callback — resend конфига
app/tg_bot_runner.py :: ref_trial_claim_callback — триал + resend
app/tg_bot_runner.py :: safe_send_message — обёртка отправки
app/tg_bot_runner.py :: auto_no_handshake_reminder — напоминания без handshake
app/tg_bot_runner.py :: router — центральный router, все handlers
app/bot.py :: send_vpn_config_to_user — единственная точка выдачи конфига
app/db.py :: get_latest_subscription_for_telegram
app/db.py :: get_subscription_by_id
app/db.py :: get_referrer_telegram_id
app/db.py :: user_can_claim_referral_trial
app/db.py :: get_subscriptions_for_no_handshake_reminder
app/db.py :: create_subscription_notification
app/wg.py :: get_handshake_timestamps
app/messages.py — все константы текстов
app/yookassa_webhook_runner.py :: process_yookassa_event
app/heleket_webhook_runner.py :: handle_heleket_webhook
app/main.py :: handle_new_subscription, handle_new_donation
```

---

# 14. Executive summary

1. **Уже подходит для AI-support:** Единая точка выдачи конфига (`send_vpn_config_to_user`), централизованные тексты в `messages.py`, кнопка «Нужна помощь» с URL.
2. **Требует доработки:** Единый user context (агрегация подписки, handshake, баллов, реферала); support conversation state; разграничение support flow от pay/promo/config.
3. **Может сломаться при неосторожной интеграции:** FSM (PromoStates, DemoRequest, AdminAddSub), callbacks pay/points/heleket, config:resend.
4. **С чего начать:** Новая команда `/ask` или middleware, который обрабатывает только «свободный» текст (не в FSM, не команда). Добавить таблицу/FSM для support conversation. Собирать user context через отдельный сервис/функцию.
5. **Tribute — отдельный процесс:** Нужна общая стратегия по рассинхрону и retry отправки конфига.
6. **Нет support history:** Полный audit trail диалогов отсутствует.
7. **Handshake и напоминания:** Данные для «подключился/не подключился» есть; интеграция с AI — через единый context.
8. **Referral/promo логика:** Жёстко в db и handlers; не смешивать с support flow.
9. **Логирование:** vpn_service.log, yookassa.log, heleket.log — можно добавить support_ai.log.
10. **Критичные точки:** cmd_support, config_resend_callback, ref_trial_claim_callback, send_vpn_config_to_user.
