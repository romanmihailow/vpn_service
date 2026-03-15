# Post-config connection check (checkpoint)

**Дата:** 2025-03-15  
**Цель:** после выдачи VPN-конфига автоматически спрашивать пользователя через ~2–3 минуты, удалось ли подключиться. При отсутствии handshake показывать сообщение с кнопками; при наличии handshake — не беспокоить.

---

## 1. Изменённые файлы

| Файл | Изменения |
|------|-----------|
| `app/messages.py` | Константы: `CONFIG_CHECK_MESSAGE`, `CONFIG_CHECK_SUCCESS`, `CONFIG_CHECK_FAIL`, `CONFIG_CHECK_OPTIONS` |
| `app/bot.py` | Параметр `schedule_checkpoint` в `send_vpn_config_to_user`; функция `_run_config_checkpoint`; планирование отложенной проверки через `asyncio.create_task` |
| `app/support/actions.py` | Вызов `send_vpn_config_to_user(..., schedule_checkpoint=False)` при resend |
| `app/tg_bot_runner.py` | Импорты констант и support-actions; вызов `send_vpn_config_to_user(..., schedule_checkpoint=False)` в `config_resend_callback`; обработчики callback: `config_check_ok`, `config_check_failed`, `config_check_resend`, `config_issue_not_found`, `config_issue_import`, `config_issue_connected_no_internet`, `config_issue_support` |

---

## 2. Как планируется отложенная проверка

- В конце `send_vpn_config_to_user` (после отправки трёх сообщений: файл, QR, инструкция), если `schedule_checkpoint=True` (значение по умолчанию):
  - по `telegram_user_id` запрашивается текущая подписка: `db.get_latest_subscription_for_telegram(telegram_user_id)`;
  - при наличии у подписки `id` и `wg_public_key` создаётся фоновая задача:  
    `asyncio.create_task(_run_config_checkpoint(telegram_user_id, subscription_id, wg_public_key))`.
- В `_run_config_checkpoint`:
  - выполняется `await asyncio.sleep(CONFIG_CHECKPOINT_DELAY_SEC)` (180 сек);
  - проверяется, не отправлялось ли уже сообщение checkpoint по этой подписке:  
    `db.has_subscription_notification(subscription_id, "config_checkpoint_sent")`;
  - запрашиваются handshake: `wg.get_handshake_timestamps()`; если для данного `wg_public_key` есть handshake (timestamp > 0), функция завершается без отправки;
  - иначе бот отправляет сообщение с текстом `CONFIG_CHECK_MESSAGE` и тремя кнопками (Да / Нет / Отправить настройки ещё раз);
  - создаётся запись уведомления: `db.create_subscription_notification(..., "config_checkpoint_sent")`, чтобы не слать checkpoint повторно.

Используется один и тот же процесс (asyncio), без отдельного планировщика; хранение «уже отправлен» — через существующую таблицу `subscription_notifications`.

---

## 3. Добавленные callback-обработчики

| Callback data | Обработчик | Поведение |
|---------------|------------|-----------|
| `config_check_ok:{sub_id}` | `config_check_ok_callback` | Убирает кнопки, отправляет `CONFIG_CHECK_SUCCESS`, пишет в БД `config_check_ok`. |
| `config_check_failed:{sub_id}` | `config_check_failed_callback` | Отправляет «Понял. Что именно не получилось?» и четыре кнопки (не нашёл конфиг / не импортировать / VPN есть, нет интернета / нужна помощь). |
| `config_check_resend:{sub_id}` | `config_check_resend_callback` | Строит конфиг по подписке, вызывает `send_vpn_config_to_user(..., schedule_checkpoint=False)`, отвечает «Конфиг отправлен…». |
| `config_issue_not_found:{sub_id}` | `config_issue_not_found_callback` | Текст про «вышли конфиг» и кнопка «Отправить настройки ещё раз» (`config_check_resend:{sub_id}`). |
| `config_issue_import:{sub_id}` | `config_issue_import_callback` | Отправляет `HELP_INSTRUCTION` (как при connect_help). |
| `config_issue_connected_no_internet:{sub_id}` | `config_issue_connected_no_internet_callback` | Строит контекст `build_user_context(user_id)`, вызывает `action_vpn_not_working(context)`, отправляет полученный текст и кнопку (troubleshooting). |
| `config_issue_support` | `config_issue_support_callback` | Вызывает `action_human_request()`, отправляет текст и кнопку поддержки. |

Обработчики зарегистрированы в основном `router` в `tg_bot_runner.py`; логика resend и человеческой поддержки переиспользует существующие действия (`send_vpn_config_to_user`, `action_vpn_not_working`, `action_human_request`).

---

## 4. Подтверждение: логика AI-support не менялась

- **Intents, guardrails, service, context_builder:** без изменений.
- **Support router:** не менялся; обрабатывает только свободный текст.
- Checkpoint и новые callback работают в основном роутере; при нажатии «VPN подключён, но сайты не открываются» вызывается только `action_vpn_not_working(context)` из `support.actions` — без изменения классификации интентов и без прохождения сообщения через support-сервис.

---

## 5. Подтверждение: payment / referral и прочие сценарии не затронуты

- **Платежи (YooKassa, Heleket, Tribute и т.д.):** вызовы `send_vpn_config_to_user` без нового параметра; по умолчанию `schedule_checkpoint=True`, поведение «после оплаты отправили конфиг и запланировали checkpoint» сохраняется.
- **Реферальный триал, промокоды, админская выдача доступа:** без изменений; для них по умолчанию тоже планируется checkpoint.
- **Resend:** в двух местах явно отключён checkpoint:
  - `app/support/actions.py`: `action_resend_config` вызывает `send_vpn_config_to_user(..., schedule_checkpoint=False)`;
  - `app/tg_bot_runner.py`: `config_resend_callback` и `config_check_resend_callback` вызывают `send_vpn_config_to_user(..., schedule_checkpoint=False)`.

Никакие сценарии оплаты, рефералов, FSM или выдачи конфига по шагам не переписывались — добавлен только опциональный параметр и отключение планирования для явных resend.
