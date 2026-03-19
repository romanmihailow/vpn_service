# Audit: Referral Notifications Integration

Полный аудит системы уведомлений и реферальной логики VPN-бота перед добавлением двух новых уведомлений:
1. Уведомление о начислении баллов (рефереру).
2. Уведомление о том, что приглашённый пользователь успешно подключился (рефереру).

**Ограничение:** только анализ и рекомендации, без изменений кода.

---

## 1. Current User Notifications

Все места, где бот отправляет сообщения пользователю (автоуведомления и реакция на действия).

| Файл | Функция / контекст | Условие | Тип уведомления | subscription_notifications | Защита от дублей |
|------|--------------------|---------|------------------|----------------------------|------------------|
| `tg_bot_runner.py` | `auto_notify_expiring_subscriptions` | Подписка в окне истечения (3d / 1d / 1h) | Напоминание продлить | `expires_3d`, `expires_1d`, `expires_1h` | `has_subscription_notification(sub_id, type, tg_id, expires_at)` |
| `tg_bot_runner.py` | `auto_notify_expiring_subscriptions` (1h) | За 1–2 ч до конца | Текст об окончании подписки | `expires_1h` | Аналогично |
| `tg_bot_runner.py` | `auto_new_handshake_admin_notification` | Есть handshake по WG, нет записи | Первое «VPN подключён» + CTA | `handshake_user_connected` | `has_subscription_notification(sub_id, "handshake_user_connected")` |
| `tg_bot_runner.py` | `auto_new_handshake_admin_notification` | После отправки пользователю | Уведомление админу (batch) | `new_handshake_admin` | По подписке: нет записи `new_handshake_admin` |
| `tg_bot_runner.py` | `auto_handshake_followup_notifications` | После `handshake_user_connected` прошло 10m/2h/24h/3d | Follow-up (тариф, реферал) | `handshake_followup_10m`, `handshake_followup_2h`, `handshake_followup_24h`, `handshake_referral_nudge_3d` | `get_handshake_followup_candidates` исключает подписки с уже отправленным типом |
| `tg_bot_runner.py` | `auto_handshake_short_confirmation` | ~60 сек после первого handshake-сообщения | Короткое подтверждение + поддержка | `handshake_short_confirmation` | `get_handshake_short_confirmation_candidates` — нет записи этого типа |
| `tg_bot_runner.py` | `auto_welcome_after_first_payment` | Первая оплата (ЮKassa/Heleket), нет welcome | Приветствие после оплаты | `welcome_after_first_payment` | `get_subscriptions_for_welcome_after_first_payment` (нет записи + есть `handshake_user_connected`) |
| `tg_bot_runner.py` | `auto_no_handshake_reminder` | Нет handshake, подписка триал/промо, прошло 2h/24h/5d/6d | Напоминание «подключись» / опрос | `no_handshake_2h`, `no_handshake_24h`, `no_handshake_5d`, `no_handshake_survey` (+ answer_1..4) | `get_subscriptions_for_no_handshake_reminder(type)` — нет записи данного типа |
| `tg_bot_runner.py` | `auto_config_checkpoint` | config_checkpoint_pending, прошло ≥180 сек, нет handshake | «Удалось подключиться к VPN?» | `config_checkpoint_sent` | `get_pending_config_checkpoints` — есть pending, нет sent |
| `tg_bot_runner.py` | `auto_recently_expired_trial_followup` | Trial истёк → оплата, конфиг выдан, нет handshake через ~3 мин | Использовать новый конфиг + resend | `recently_expired_trial_followup_sent` | `get_pending_recently_expired_trial_followups` |
| `bot.py` | `send_vpn_config_to_user` | Выдача конфига (оплата/триал/промо) | Инструкция + конфиг | `config_checkpoint_pending` (если schedule_checkpoint) | Не дублирует сам конфиг; checkpoint — одна запись на подписку |
| `bot.py` | `send_referral_reward_notification` | Вызывается из webhook после начисления баллов | Уведомление о реферальных баллах | Не используется | Нет отдельной записи в subscription_notifications |
| `bot.py` | `send_subscription_expired_notification` | Вызов из auto_notify (1h) и при деактивации | Подписка закончилась | — | Запись `expires_1h` для 1h; при деактивации — без отдельного типа |
| `yookassa_webhook_runner.py` | После оплаты/продления | apply_referral_rewards + цикл по awards | Уведомление рефереру о баллах | Нет | Зависит от корректности цикла по awards (см. раздел 3) |
| `heleket_webhook_runner.py` | После оплаты/продления | Аналогично + в одном месте другой вызов (extension) | Уведомление рефереру о баллах | Нет | См. раздел 3 (разные вызовы) |

Реакции на кнопки (записывают факт в subscription_notifications, но не «авто-рассылка»):

- `config_check_ok_callback` → `config_check_ok`
- `vpn_ok_callback` → `vpn_ok_clicked`
- «Пригласить друга» из уведомления → `ref_nudge_clicked`
- Промо/триал follow-up → `recently_expired_trial_followup` (как тип для кандидатов)

---

## 2. Referral System Overview

### Таблицы

- **referrals**  
  - `referred_telegram_user_id` (PK) — кто пришёл по ссылке  
  - `referrer_telegram_user_id` — кто привёл (1-я линия)  
  - Связь «один приведённый — один реферер».

- **referral_codes**  
  - Код (например, username), `referrer_telegram_user_id`, `is_active`.  
  - По коду из deep-link `/start <code>` определяется реферер.

- **referral_levels**  
  - Уровни 1..5, множители для начисления баллов.

### Где создаётся связь referrer → referred

- **register_referral_start** (`db.py`): вызов из обработчика `/start` с параметром (реферальный код). Ищет код в `referral_codes`, вызывает **create_referral_link**(invited_telegram_user_id, referrer_telegram_user_id).
- **create_referral_link** (`db.py`): вставляет строку в `referrals`. Проверки: не админ, не self-ref, нет цикла в даунлайне, у пользователя ещё нет реферера.

Связь создаётся один раз при первом заходе по реферальной ссылке.

### Где читается

- **get_referrer_telegram_id**(referred_telegram_user_id) — возвращает `referrer_telegram_user_id` по приведённому.
- **get_referral_upline_chain**(referred_telegram_user_id, max_levels=5) — цепочка рефереров 1..5 для начисления баллов.
- **get_referrer_with_count**(telegram_user_id) — реферер + username + referred_count, referral_ordinal (для отображения в боте и админке).
- **count_referrer_paid_referrals**(referrer_telegram_user_id) — сколько приведённых уже оплатили.

### Использование в коде

- Регистрация: `tg_bot_runner.py` (обработчик start с `start_param`), вызов `db.register_referral_start`.
- Отображение реферера: `notify_admin_new_user`, `notify_admin_trial_activated`, экран `/ref`, кнопки «Пригласить друга».
- Начисление баллов: `get_referral_upline_chain` в `apply_referral_rewards_for_subscription`.
- Проверка триала: `get_referrer_telegram_id`, `user_can_claim_referral_trial`, `has_referral_trial_subscription`.

Helper-функции: перечислены выше; отдельно — `_is_in_referral_downline` (защита от цикла при создании связи).

---

## 3. Points (начисление баллов)

### Где начисляются

- **apply_referral_rewards_for_subscription** (`db.py`): вызывается при оплате/продлении из:
  - `yookassa_webhook_runner.py` (новая подписка и продление),
  - `heleket_webhook_runner.py` (несколько сценариев: новая подписка, продление и т.д.).

Параметры: `payer_telegram_user_id` (кто оплатил), `subscription_id`, `tariff_code`, `payment_source`, `payment_id`. Внутри: тариф с `ref_enabled`/`ref_base_bonus_points`, цепочка рефереров `get_referral_upline_chain(payer_telegram_user_id)`, для каждого уровня — `add_points(referrer_id, bonus, reason="ref_level_N", ...)`. Возвращает `result["awards"]` — список словарей.

### Структура элемента в `awards`

```python
{
    "level": level_idx,                    # 1..5
    "referrer_telegram_user_id": referrer_id,
    "bonus": bonus_int,
    "add_points_result": add_res,
}
```

### Где отправляется уведомление о начислении

- **YooKassa** (после `apply_referral_rewards_for_subscription`): цикл по `awards`, для каждого award вызывается `send_referral_reward_notification(telegram_user_id=ref_tg_id, points_delta=points, level=level, tariff_code=..., payment_channel="YooKassa")`.
- **Heleket** (в двух вариантах): в одном месте цикл по `awards` и вызов с `ref_tg_id`, `points`, `level`, `tariff_code`, `payment_channel="Heleket"`; в другом (extension) — один вызов с параметрами `payer_telegram_user_id`, `payment_id`, `payment_source`, что **не совпадает** с сигнатурой `send_referral_reward_notification(telegram_user_id, points_delta, level, tariff_code, payment_channel)`.

Критично: в обоих webhook’ах при итерации по `awards` используются ключи:
- `ref_tg_id = award.get("telegram_user_id") or award.get("user_telegram_id")`
- `points = award.get("points") or award.get("delta") or 0`

В реальном ответе `apply_referral_rewards_for_subscription` ключи другие: **referrer_telegram_user_id**, **bonus**. Поэтому `ref_tg_id` часто оказывается `None`, и уведомление рефереру о баллах **может не отправляться**.

Вывод: уведомление о начислении баллов уже задумано и вызывается из webhook’ов, но из-за несовпадения ключей (и в одном месте Heleket — неверных аргументов) может не работать. Исправление ключей и вызова — минимальное изменение для включения уведомления №1.

### Можно ли из этого места отправить уведомление

Да. После `apply_referral_rewards_for_subscription` в том же webhook’е уже вызывается `send_referral_reward_notification`. Нужно только исправить маппинг полей award и вызов в Heleket (extension). Риск дублей: один платёж → один вызов apply_referral_rewards → один цикл по реферерам; дубль возможен только при повторной обработке webhook’а (идемпотентность платежа у вас решается отдельно через payment_events).

---

## 4. Подключение пользователя (handshake)

### Где фиксируется «первый handshake»

- Нет отдельной таблицы «первый handshake». Факт подключения определяется по данным WireGuard: **wg.get_handshake_timestamps()** возвращает словарь `{public_key: last_handshake_timestamp}`. Если для публичного ключа подписки `ts > 0`, считаем, что пользователь подключался.

### Где используется

- **auto_new_handshake_admin_notification** (`tg_bot_runner.py`): раз в интервал получает подписки без записи `new_handshake_admin`, для каждой проверяет `handshakes.get(wg_public_key, 0) > 0`. Для таких подписок:
  1. Пользователю (tg_id подписки) отправляется сообщение «VPN подключён» и создаётся запись **handshake_user_connected**.
  2. Админу отправляется batch-уведомление о новых handshake’ах.
  3. Создаётся запись **new_handshake_admin** по подписке.

То есть «событие» первого подключения — это момент, когда job видит handshake и отправляет пользователю сообщение + пишет `handshake_user_connected`.

### Можно ли определить реферера и отправить ему уведомление

Да. В этом же цикле по подпискам с handshake есть `tg_id` (владелец подписки = приведённый пользователь). Вызов **get_referrer_telegram_id(tg_id)** даёт реферера. Если реферер есть, можно один раз отправить ему короткое сообщение в стиле: «Ваш приглашённый пользователь успешно подключил VPN 👍» и зафиксировать отправку, чтобы не слать повторно.

### Защита от дублей

Имеет смысл завести отдельный тип в `subscription_notifications`, например **referral_user_connected** (или **referral_referred_connected**), с `subscription_id` = подписка приведённого пользователя (та, по которой только что отправили `handshake_user_connected`). Уникальный индекс `(subscription_id, notification_type)` обеспечит одну отправку «рефереру о подключении приведённого» на одну подписку. Альтернатива — отдельная маленькая таблица (referrer_id, referred_tg_id или subscription_id), если не хочется смешивать с subscription_notifications.

---

## 5. subscription_notifications

### Назначение

Хранит факт отправки уведомления по подписке. Поля: `subscription_id`, `telegram_user_id`, `expires_at`, **notification_type**, `sent_at`. Уникальный индекс: `(subscription_id, notification_type)` (для части типов также учёт `telegram_user_id` и `expires_at`).

### Уже используемые notification_type

- Истечение: `expires_3d`, `expires_1d`, `expires_1h`
- Handshake и follow-up: `handshake_user_connected`, `handshake_followup_10m`, `handshake_followup_2h`, `handshake_followup_24h`, `handshake_referral_nudge_3d`, `handshake_short_confirmation`
- Админ: `new_handshake_admin`
- Welcome: `welcome_after_first_payment`
- Нет handshake: `no_handshake_2h`, `no_handshake_24h`, `no_handshake_5d`, `no_handshake_survey`, `no_handshake_survey_answer_1`..`4`
- Конфиг: `config_checkpoint_pending`, `config_checkpoint_sent`
- Trial expired → paid: `recently_expired_trial_followup`, `recently_expired_trial_followup_sent`
- Кнопки: `config_check_ok`, `vpn_ok_clicked`, `ref_nudge_clicked`

Похожих на «реферальные баллы» или «приведённый подключился» типов нет.

### Можно ли добавить новые типы

Да.

- **referral_points_awarded**  
  Для идемпотентности уведомления о баллах можно привязать к подписке плательщика: одна запись на (subscription_id плательщика + тип), но тогда один платёж даёт одну запись, а рефереров может быть несколько. Поэтому логичнее либо не хранить в subscription_notifications (уведомление и так один раз при обработке платежа), либо ввести тип с привязкой к рефереру (например, отдельная таблица или meta). Текущая реализация не пишет в subscription_notifications при отправке `send_referral_reward_notification` — дубли возможны только при повторной доставке webhook’а (редко).

- **referral_user_connected** (или **referral_referred_connected**)  
  Один раз на подписку приведённого: при первой отправке рефереру «приведённый подключился» создавать запись с `subscription_id` = подписка приведённого, `notification_type = 'referral_user_connected'`. Так не будем слать одно и то же рефереру дважды за одну и ту же подписку.

---

## 6. Reusable Components

- **safe_send_message** (`tg_bot_runner.py`) — обёртка отправки в чат с логированием и обработкой ошибок.
- **send_referral_reward_notification** (`bot.py`) — готовая функция уведомления о баллах; текст сейчас более «технический» (уровень, канал оплаты). Для стиля «короткие человеческие сообщения» его можно заменить или дополнить коротким вариантом (см. п. 7).
- **get_referrer_telegram_id**, **get_referrer_with_count** — готовые хелперы для получения реферера по tg_id пользователя.
- **create_subscription_notification** / **has_subscription_notification** — идемпотентная запись и проверка отправки по (subscription_id, notification_type).

Отдельно: отправка из webhook’ов (YooKassa/Heleket) выполняется в async-контексте; `send_referral_reward_notification` создаёт свой Bot и session — это допустимо для разовых уведомлений.

---

## 7. Risks

- **Дубли уведомления о баллах**  
  Сейчас не записываются в subscription_notifications. Дубли возможны только при повторной обработке одного и того же платежа (двойной webhook). Идемпотентность платежей решается на уровне payment_events; при необходимости можно добавить проверку «уже слали этому рефереру за этот payment_id» (например, по meta в user_points_transactions или отдельной таблице).

- **Неверные ключи в awards**  
  В YooKassa и Heleket при обходе `awards` берутся `telegram_user_id`/`user_telegram_id` и `points`/`delta`, тогда как в коде возвращаются `referrer_telegram_user_id` и `bonus`. Это приводит к тому, что уведомление о баллах рефереру может не уходить. Исправление ключей обязательно.

- **Heleket extension**  
  В одном месте вызывается `send_referral_reward_notification(payer_telegram_user_id=..., payment_id=..., payment_source=...)` — аргументы не соответствуют сигнатуре. Результат: ошибка в рантайме или уведомление не тому пользователю. Нужно привести к тому же формату, что и в цикле по awards (ref_tg_id, points_delta, level, tariff_code, payment_channel).

- **Race conditions**  
  Несколько воркеров/рестарты: основные сценарии защищены job lock’ами и проверкой `has_subscription_notification` / уникальным индексом. Для нового уведомления «приведённый подключился» достаточно одной записи на (subscription_id приведённого, referral_user_connected) в том же job’е, где создаётся handshake_user_connected — гонок не добавляется.

- **Где опасно вставлять логику**  
  Внутри `apply_referral_rewards_for_subscription` лучше не вызывать Telegram (синхронная функция, работа с БД). Уведомления корректно вызывать снаружи, после возврата из apply_referral_rewards, как сейчас. Для «приведённый подключился» безопасно добавлять отправку в том же цикле `auto_new_handshake_admin_notification`, где отправляется handshake_user_connected — после успешной отправки пользователю и создания записи handshake_user_connected.

- **Идемпотентность**  
  subscription_notifications: INSERT с ON CONFLICT DO NOTHING по (subscription_id, notification_type). Новый тип referral_user_connected при той же схеме будет идемпотентен.

---

## 8. Recommended Integration Points

### 1) Уведомление о начислении баллов

- **Где встроить:** оставить вызов **send_referral_reward_notification** в тех же местах, где сейчас: после **apply_referral_rewards_for_subscription** в YooKassa и Heleket.
- **Что сделать:**
  - В цикле по `awards` использовать **referrer_telegram_user_id** и **bonus**:  
    `ref_tg_id = award.get("referrer_telegram_user_id")`, `points = award.get("bonus", 0)` (и при необходимости `level = award.get("level")`).
  - В Heleket (extension) убрать вызов с неверными аргументами и использовать тот же цикл по `awards`, что и в других сценариях, с теми же ключами.
  - При желании заменить или дополнить текст в **send_referral_reward_notification** на короткий человеческий вариант, например:  
    «Вам начислены бонусные баллы за приглашение пользователя 😊» или «Отличная новость 🙂 Вам начислены бонусы за приглашённого пользователя 👍» (без уровня/канала в основном тексте или в одной короткой строке).

Минимальные изменения: правка маппинга полей и аргументов вызова; опционально — текст.

### 2) Уведомление о том, что приглашённый пользователь успешно подключился

- **Где встроить:** **auto_new_handshake_admin_notification** (`tg_bot_runner.py`), в том же цикле, где по подписке с handshake:
  - отправляется пользователю сообщение и создаётся **handshake_user_connected**;
  - после успешной отправки пользователю и записи handshake_user_connected вызвать **get_referrer_telegram_id(tg_id)**;
  - если реферер есть и для этой подписки ещё не отправляли уведомление рефереру — отправить короткое сообщение рефереру (например: «Ваш приглашённый пользователь успешно подключил VPN 👍» или «Пользователь по вашей ссылке подключился — всё работает 😊») и вызвать **create_subscription_notification(subscription_id=sub_id, notification_type="referral_user_connected", telegram_user_id=referrer_telegram_user_id, ...)** (или другой уникальный тип, см. ниже).
- **Защита от дублей:** новый тип в subscription_notifications по этой же подписке (subscription_id приведённого), например **referral_user_connected**. Уникальный индекс (subscription_id, notification_type) даёт одну отправку на подписку.

Так реферер получает одно уведомление в момент первого подключения приведённого, без дублей и без лишних фоновых job’ов.

---

## Стиль текстов (ориентир)

Все новые/изменённые тексты лучше делать короткими, человечными, без технических терминов (например, без слова «handshake» в интерфейсе).

- **Начисление баллов:**  
  «Вам начислены бонусные баллы за приглашение пользователя 😊» или «Отличная новость 🙂 Вам начислены бонусы за приглашённого пользователя 👍».

- **Подключился приглашённый:**  
  «Ваш приглашённый пользователь успешно подключил VPN 👍» или «Пользователь по вашей ссылке подключился — всё работает 😊».

Текущая реализация **send_referral_reward_notification** содержит уровень реферала, тариф и канал оплаты — при желании их можно оставить одной короткой строкой или убрать для основного сценария.

---

**Аудит завершён. Код не изменялся.**
