# CRM: минимальный фикс трекинга «Пригласить друга»

Внесены два точечных исправления, чтобы метрика «Пригласить друга» в `/crm_report` учитывала большинство реальных кликов по referral CTA. Архитектура и остальная бизнес-логика не менялись.

---

## 1. Изменённые файлы

- **app/tg_bot_runner.py**
  - **FIX 1:** в handler'е `ref_open_from_notify` добавлен fallback: при `callback_data = "ref:open_from_notify"` без sub_id используется последняя активная подписка пользователя для записи `ref_nudge_clicked`.
  - **FIX 2:** в `vpn_ok_callback` кнопка «Получить ссылку» переведена на `ref:open_from_notify:{sub_id}`.

---

## 2. Fallback sub_id в ref_open_from_notify

**Было:** событие `ref_nudge_clicked` создавалось только если `callback_data` имел вид `ref:open_from_notify:{sub_id}`.

**Стало:**
- Если `callback_data.startswith("ref:open_from_notify:")` — логика без изменений: парсим sub_id, проверяем подписку и владельца, при отсутствии записи `ref_nudge_clicked` пишем её.
- Если callback без суффикса (т.е. просто `ref:open_from_notify`):
  - вызывается `db.get_latest_subscription_for_telegram(telegram_user_id)`;
  - если подписка найдена и у неё есть `id`, проверяется `not db.has_subscription_notification(sub_id, "ref_nudge_clicked")`;
  - если записи ещё нет — вызывается `db.create_subscription_notification(..., notification_type="ref_nudge_clicked", ...)` для этой подписки;
  - при ошибке пишется warning в лог, выдача ссылки пользователю не прерывается.

В результате любая кнопка с `ref:open_from_notify` (без sub_id) при наличии у пользователя активной подписки один раз записывает клик в CRM; повторные клики не дублируют запись благодаря проверке `has_subscription_notification`.

---

## 3. Кнопка в vpn_ok_callback

**Было:** после нажатия «Всё работает» в 10m follow-up показывалась кнопка «🤝 Получить ссылку» с `callback_data="ref:open_from_notify"`.

**Стало:** та же кнопка с `callback_data=f"ref:open_from_notify:{sub_id}"`, где `sub_id` — идентификатор подписки, уже доступный в обработчике. Поведение handler'а `ref_open_from_notify` для такого callback не менялось (оно и раньше учитывало вариант с sub_id), меняется только то, что этот конкретный сценарий теперь явно передаёт sub_id и гарантированно попадает в `ref_nudge_clicked`.

---

## 4. Сценарии, где клики теперь попадают в ref_nudge_clicked

- **Уже учитывались (без изменений):**
  - Сообщение после «Всё работает» в checkpoint (config_check_ok) — кнопка с `ref:open_from_notify:{sub_id}`.
  - Referral follow-up через 3 дня — кнопка с `ref:open_from_notify:{sub_id}`.

- **Начали учитываться после фикса:**
  - **ref:open_from_notify без sub_id** (fallback по последней подписке): /start (SUBSCRIBE_KEYBOARD), клавиатура «Оплатить баллами» (POINTS_KEYBOARD), напоминания об окончании подписки (SUBSCRIPTION_RENEW_KEYBOARD), /status (get_status_keyboard), follow-up через 2 часа (HANDSHAKE_FOLLOWUP_2H_KEYBOARD), AI-support (referral_info / referral_stats / referral_balance).
  - **vpn_ok_callback:** кнопка «Получить ссылку» после нажатия «Всё работает» в 10m follow-up теперь с sub_id — учитывается так же, как раньше учитывался только явный sub_id в callback.

---

## 5. Сценарии, по-прежнему не покрытые ref_nudge_clicked

- **Команда /ref:** кнопка «Пригласить друга» под сообщением `/subscription` и сам handler **ref:open_from_ref** (`ref_open_from_ref_callback`) не изменялись. Клики по этой кнопке по-прежнему **не** создают запись `ref_nudge_clicked` и **не** попадают в метрику «Пригласить друга» в CRM. Это оставлено явно без изменений по условию задачи.

---

Referral tracking minimal fix implemented.
