# UX-патч: trial expired → paid (использование нового конфига)

**Дата:** 2025-03-12  
**Сценарий:** Пользователь получил trial → trial истёк → оплатил подписку → бот отправил новый конфиг → пользователь продолжает использовать старый trial-конфиг.

---

## 1. Как определяется `recently_expired_trial`

Флаг выставляется по наличию у пользователя **недавно истекшей** подписки:

- **Функция:** `db.has_recently_expired_subscription(telegram_user_id, within_hours=48)`  
- **Файл:** `app/db.py`

**Условия:** у пользователя есть хотя бы одна подписка, у которой:

- `telegram_user_id` = пользователь;
- `active = FALSE`;
- `expires_at < NOW()`;
- `expires_at >= NOW() - 48 часов`.

То есть подписка уже неактивна и истекла не более 48 часов назад. Окно 48 часов задаётся параметром `within_hours` (по умолчанию 48). Новая только что созданная платная подписка не попадает под этот критерий (`active = TRUE`, `expires_at` в будущем).

---

## 2. Где добавлено сообщение перед отправкой конфига

Если `recently_expired_trial = True`, **до** вызова `send_vpn_config_to_user` отправляется отдельное сообщение.

**Текст:** константа `TRIAL_EXPIRED_PAID_NOTIFICATION_TEXT` из `app/messages.py` (оплата прошла, важно использовать новый конфиг, шаги: открыть WireGuard, удалить старый туннель, импортировать новый конфиг/QR, кнопка «Нужна помощь»).

**Отправка:** `send_trial_expired_paid_notification(telegram_user_id)` в `app/bot.py` — создаётся клавиатура с кнопкой «🧑‍💻 Нужна помощь» (`SUPPORT_URL`), сообщение уходит в чат пользователя.

**Места вызова (перед отправкой конфига):**

1. **YooKassa** — `app/yookassa_webhook_runner.py`: при создании новой подписки после оплаты (ветка `base_sub is None`), перед блоком «Генерим конфиг и отправляем» вызывается `has_recently_expired_subscription(telegram_user_id, 48)`; при `True` — `send_trial_expired_paid_notification`, затем `send_vpn_config_to_user`.
2. **Оплата баллами** — `app/tg_bot_runner.py` (обработчик оплаты тарифа баллами): при `send_config is True` перед `send_vpn_config_to_user` та же проверка и при необходимости уведомление.
3. **Промокод (новая подписка)** — `app/tg_bot_runner.py` (обработчик применения промокода, ветка «новая подписка»): при `send_config is True` — та же проверка и уведомление.

После отправки конфига во всех трёх местах при `recently_expired_trial` создаётся запись в `subscription_notifications`: `notification_type='recently_expired_trial_followup'` для только что созданной подписки (чтобы через 3–5 минут запустить follow-up).

---

## 3. Где добавлен follow-up без handshake

**Джоба:** `auto_recently_expired_trial_followup(bot)` в `app/tg_bot_runner.py`.

**Логика:**

- Раз в минуту джоба берёт кандидатов из `db.get_pending_recently_expired_trial_followups(interval_seconds=180)` — подписки, по которым есть `recently_expired_trial_followup`, прошло не менее 180 секунд (3 мин) и ещё не отправлялся `recently_expired_trial_followup_sent`.
- Для каждого кандидата загружается подписка, проверяется handshake по `wg.get_handshake_timestamps()` и `wg_public_key` подписки.
- Если handshake **нет** — пользователю отправляется сообщение с текстом `TRIAL_EXPIRED_PAID_FOLLOWUP_NO_HANDSHAKE_TEXT` из `app/messages.py` и кнопками:
  - «📱 Отправить настройки ещё раз» — `callback_data="config_check_resend:{sub_id}"` (существующий resend);
  - «🧑‍💻 Нужна помощь» — `url=SUPPORT_URL`.
- После отправки создаётся запись `recently_expired_trial_followup_sent`, чтобы не слать повторно.

**Запуск джобы:** в `main()` в `app/tg_bot_runner.py` добавлен `asyncio.create_task(auto_recently_expired_trial_followup(bot))`. Используется lock `settings.DB_JOB_LOCK_RECENTLY_EXPIRED_TRIAL_FOLLOWUP` (в `app/config.py` добавлен как 2009).

---

## 4. Изменённые файлы

| Файл | Изменения |
|------|-----------|
| `app/db.py` | `has_recently_expired_subscription(telegram_user_id, within_hours=48)`; `get_pending_recently_expired_trial_followups(interval_seconds=180)`. |
| `app/messages.py` | Константы `TRIAL_EXPIRED_PAID_NOTIFICATION_TEXT`, `TRIAL_EXPIRED_PAID_FOLLOWUP_NO_HANDSHAKE_TEXT`. |
| `app/bot.py` | Импорт `TRIAL_EXPIRED_PAID_NOTIFICATION_TEXT`; функция `send_trial_expired_paid_notification(telegram_user_id)`. |
| `app/config.py` | `DB_JOB_LOCK_RECENTLY_EXPIRED_TRIAL_FOLLOWUP = 2009`. |
| `app/yookassa_webhook_runner.py` | Импорт `send_trial_expired_paid_notification`; перед отправкой конфига при новой подписке — проверка `has_recently_expired_subscription`, при True — уведомление, затем конфиг; после отправки конфига — `create_subscription_notification(..., 'recently_expired_trial_followup')`. |
| `app/tg_bot_runner.py` | Импорт `send_trial_expired_paid_notification`, `TRIAL_EXPIRED_PAID_FOLLOWUP_NO_HANDSHAKE_TEXT`; в оплате баллами и в промо (новая подписка) — та же логика уведомления и регистрации follow-up; новая джоба `auto_recently_expired_trial_followup` и её запуск в `main()`. |

---

## 5. Подтверждение ограничений

- **IP pool / allocation:** не менялись. Новые функции только читают подписки и пишут в `subscription_notifications`. Выделение и освобождение IP без изменений.
- **WireGuard / peer lifecycle:** не менялись. Добавлены только UX-сообщения и запись типов уведомлений; добавление/удаление peer и конфиг WG не трогаются.
- **resend_config переиспользован:** кнопка «Отправить настройки ещё раз» в follow-up ведёт на существующий callback `config_check_resend:{sub_id}`, обрабатываемый `config_check_resend_callback` в `tg_bot_runner.py` (сборка конфига и вызов `send_vpn_config_to_user`).

---

Trial expired → paid UX patch added.
