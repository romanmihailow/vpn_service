# Аудит CRM-метрик и трекинга реферального флоу (MaxNet VPN Bot)

Дата: 2025-03. Цель: выяснить, почему в `/crm_report` метрика «Пригласить друга» = 0, и проверить остальные CRM-метрики на полноту трекинга.

---

## 1. Executive summary

**Проблема подтверждена.** Метрика «Пригласить друга» в CRM считает **только** клики по кнопке с `callback_data="ref:open_from_notify:{sub_id}"` (обязателен суффикс `sub_id`). При этом в коде **большинство** кнопок «Пригласить друга» / «Получить ссылку» используют:
- `ref:open_from_notify` **без** `sub_id`, либо  
- `ref:open_from_ref`  

и **ни в одном из этих случаев** не создаётся запись `ref_nudge_clicked` в `subscription_notifications`. В отчёт попадают только два сценария:
1. Сообщение после нажатия «Всё работает» в **checkpoint** (config_check_ok) — кнопка с `ref:open_from_notify:{sub_id}`.
2. Сообщение **referral follow-up через 3 дня** (handshake_referral_nudge_3d) — кнопка с `ref:open_from_notify:{sub_id}`.

Все остальные места (старт, /status, /subscription, продление, баллы, 10m follow-up «Получить ссылку», AI-support, follow-up 2h) **не пишут** `ref_nudge_clicked`, поэтому метрика «Пригласить друга» в CRM занижена или равна 0.

**Похожих дыр в других метриках:**  
- Остальные метрики воронки (handshake, follow-up, «Всё работает», welcome_after_first_payment, no_handshake_*) привязаны к **отправке** сообщения или к одному конкретному callback с записью в БД. Расхождение «несколько кнопок — один счётчик» есть только у **«Пригласить друга»**.

---

## 2. Таблица всех CRM-метрик

| Метрика в /crm_report | Event / notification_type | Где создаётся | Авто / user action | Покрывает ли все flow | Проблема / риск |
|------------------------|---------------------------|----------------|---------------------|------------------------|------------------|
| первые платные подписки | welcome_after_first_payment | tg_bot_runner: auto_welcome_after_first_payment (job) | авто (job после оплаты) | да, одна точка входа | нет |
| первый handshake | handshake_user_connected | tg_bot_runner: auto_new_handshake_admin_notification (job) | авто (job при появлении handshake) | да | нет |
| конверсия оплата→handshake | (handshake_user_connected / welcome_after_first_payment) | расчёт в отчёте | — | да | нет |
| follow-up через 10 минут | handshake_followup_10m | tg_bot_runner: auto_handshake_followup (job) | авто | да | нет |
| «Всё работает» нажали | vpn_ok_clicked | tg_bot_runner: vpn_ok_callback | user (клик по кнопке в 10m follow-up) | да, один callback | нет |
| follow-up через 2 часа | handshake_followup_2h | auto_handshake_followup | авто | да | нет |
| follow-up через 24 часа | handshake_followup_24h | auto_handshake_followup | авто | да | нет |
| referral follow-up через 3 дня | handshake_referral_nudge_3d | auto_handshake_followup | авто | да | нет |
| **«Пригласить друга» нажали** | **ref_nudge_clicked** | **tg_bot_runner: ref_open_from_notify** — только если callback_data = **ref:open_from_notify:{sub_id}** | user | **нет** — считается только 2 из многих кнопок | **занижение / 0** |
| напоминание 2h/24h/5d без handshake | no_handshake_2h, 24h, 5d | auto_no_handshake_reminder | авто | да | нет |
| опрос причины отказа | no_handshake_survey | auto_no_handshake_reminder | авто | да | нет |
| ответили на опрос / причины 1–4 | no_handshake_survey_answer_* | support/service: try_record_survey_answer | user | да | нет |
| первые оплаты после handshake | (отдельный запрос: welcome_after_first_payment + handshake_user_connected) | расчёт в отчёте | — | да | нет |

---

## 3. Таблица всех referral CTA / кнопок

| Место в UI | Текст кнопки | callback_data / handler | Что делает | Пишет tracking event? | Учитывается в «Пригласить друг» в CRM? |
|------------|--------------|--------------------------|------------|------------------------|----------------------------------------|
| /start (без триала) | 🤝 Пригласить друга | ref:open_from_notify | ref_open_from_notify, выдача ссылки | нет | **нет** |
| /ref (команда) | 🤝 Пригласить друга | ref:open_from_ref | ref_open_from_ref_callback, выдача ссылки | нет | **нет** (другой callback) |
| Клавиатура «Оплатить баллами» | 🤝 Пригласить друга | ref:open_from_notify | ref_open_from_notify | нет | **нет** |
| Напоминания об окончании подписки (3d/1d/1h) | 🤝 Пригласить друга | ref:open_from_notify | ref_open_from_notify | нет | **нет** |
| /status | 🤝 Пригласить друга | ref:open_from_notify | ref_open_from_notify | нет | **нет** |
| После «Всё работает» в 10m follow-up | 🤝 Получить ссылку | ref:open_from_notify | ref_open_from_notify | нет | **нет** (sub_id есть в контексте, но в callback не передан) |
| После «Всё работает» в **checkpoint** | 👥 Пригласить друга | ref:open_from_notify:**{sub_id}** | ref_open_from_notify | **да** (ref_nudge_clicked) | **да** |
| Referral follow-up через 3 дня | 🤝 Пригласить друга | ref:open_from_notify:**{sub_id}** | ref_open_from_notify | **да** (ref_nudge_clicked) | **да** |
| AI-support (referral_info / referral_stats / referral_balance) | 👥 Пригласить друг | ref:open_from_notify | ref_open_from_notify | нет | **нет** |
| Follow-up через 2 часа (handshake) | 🤝 Пригласить друга | ref:open_from_notify | ref_open_from_notify | нет | **нет** |
| /subscription (тарифы) | — | REF_SHARE_KEYBOARD: ref:open_from_ref | под сообщением /subscription | нет | **нет** |

**Итог:** из всех кнопок «Пригласить друга» / «Получить ссылку» в CRM попадают только клики из **двух** мест: checkpoint success и 3d referral nudge. Остальные пути **не увеличивают** метрику.

---

## 4. Полный referral flow map

1. **Показ CTA**  
   Пользователь видит кнопку «Пригласить друга» или «Получить ссылку» в одном из мест из таблицы выше (start, /ref, /status, /subscription, продление, баллы, 10m/2h/3d follow-up, checkpoint, AI-support).

2. **Клик**  
   Один из двух handlers:  
   - `ref:open_from_ref` → ref_open_from_ref_callback (только под /ref).  
   - `ref:open_from_notify` или `ref:open_from_notify:{sub_id}` → ref_open_from_notify.  
   Только при `ref:open_from_notify:{sub_id}` создаётся запись `ref_nudge_clicked` (и нужен валидный sub_id).

3. **Выдача ссылки**  
   get_or_create_referral_info → формирование deep_link → отправка сообщения «Привет! Я пользуюсь MaxNet VPN…» с ссылкой. Пользователь копирует/пересылает.

4. **Приход по реф-ссылке**  
   /start {ref_code} → register_referral_start (отдельная логика, не в subscription_notifications). В CRM по этому шагу отдельной метрики нет.

5. **Trial / onboarding / claim**  
   Реферальный триал, получение конфига — без отдельных CRM-событий для «пришёл по ссылке».

6. **Handshake / оплата реферала**  
   handshake_user_connected и welcome_after_first_payment пишутся своими jobs. Начисление бонусов рефереру — в логике баллов/рефералов, не в CRM-отчёте.

**Где считается клик «Пригласить друга» в CRM:** только при callback_data `ref:open_from_notify:{sub_id}` и успешной записи `ref_nudge_clicked`. Остальные клики по реф-кнопкам в этот счётчик не попадают.

---

## 5. Найденные расхождения

- **Кнопки без трекинга в CRM:** все кнопки с `ref:open_from_notify` (без sub_id) и `ref:open_from_ref` не создают `ref_nudge_clicked`. В их числе: /start, /status, /subscription (REF_SHARE_KEYBOARD), продление подписки, баллы, 10m follow-up «Получить ссылку», 2h follow-up, AI-support (3 интента).
- **Дублирования событий:** нет; один клик с sub_id даёт одну запись ref_nudge_clicked (идемпотентность по sub_id + notification_type).
- **События в коде, не в отчёте:** config_check_ok, config_checkpoint_sent, config_checkpoint_pending, new_handshake_admin, expires_3d, expires_1d, expires_1h — намеренно не входят в текущий CRM-отчёт (отчёт заточен под воронку подключений и реф-nudge).
- **Неполная метрика:** «Пригласить друга» в CRM учитывает только два из многих сценариев показа кнопки, поэтому показатель занижен или равен 0.

---

## 6. Minimal fix proposal

Без изменения архитектуры и без массового рефакторинга:

**Фикс 1 (главный): унифицировать запись ref_nudge_clicked при любом клике «Пригласить друга»**

- **Где:** `tg_bot_runner.py`, handler `ref_open_from_notify` (callback `F.data.startswith("ref:open_from_notify")`).
- **Что:** при **любом** вызове (с sub_id и без) пытаться записать `ref_nudge_clicked` для подписки пользователя, если её можно однозначно определить:
  - если в callback есть `ref:open_from_notify:{sub_id}` — использовать этот sub_id (как сейчас).
  - если callback просто `ref:open_from_notify` — взять **текущую/последнюю активную подписку** пользователя: `db.get_latest_subscription_for_telegram(telegram_user_id)`, и если есть — записать `ref_nudge_clicked` для неё (с проверкой `has_subscription_notification`, чтобы не дублировать).
- **Итог:** все кнопки «Пригласить друга» с `ref:open_from_notify` начнут попадать в CRM. Кнопка под /ref (`ref:open_from_ref`) по-прежнему не будет давать ref_nudge_clicked (нет sub_id и контекст «подписка» размыт), что допустимо или может быть отдельным решением.

**Фикс 2 (опционально): кнопка «Получить ссылку» после 10m follow-up**

- **Где:** `vpn_ok_callback` — клавиатура после нажатия «Всё работает» в 10m follow-up.
- **Что:** заменить `callback_data="ref:open_from_notify"` на `callback_data=f"ref:open_from_notify:{sub_id}"` (sub_id уже есть в обработчике).
- **Итог:** этот конкретный путь тоже будет давать ref_nudge_clicked без изменения логики handler’а.

**Фикс 3 (опционально): /ref в CRM**

- Если нужно учитывать и клики под /ref: в `ref_open_from_ref_callback` по аналогии с фиксом 1 получать `get_latest_subscription_for_telegram(telegram_user_id)` и при наличии подписки писать `ref_nudge_clicked`. Тогда метрика «Пригласить друга» будет включать и команду /ref.

**Рекомендуемый порядок:** сначала **Фикс 1** (покрывает большинство кнопок), затем при необходимости **Фикс 2** (точечно 10m) и **Фикс 3** (учёт /ref).

---

*Аудит выполнен по коду без массовых правок; предложены точечные изменения для выравнивания трекинга с фактическими путями пользователя.*

---

## Краткий итог (для консоли)

- **Проблема подтверждена:** метрика «Пригласить друга» считает только клики по кнопкам с `ref:open_from_notify:{sub_id}` (2 сценария: checkpoint success и 3d referral nudge). Все остальные кнопки «Пригласить друга» / «Получить ссылку» используют `ref:open_from_notify` без sub_id или `ref:open_from_ref` и не пишут `ref_nudge_clicked`.
- **Самая подозрительная метрика:** «Пригласить друга» нажали (ref_nudge_clicked). Остальные метрики воронки привязаны к одному источнику события и дыр не имеют.
- **Минимальные фиксы в первую очередь:**  
  1) В handler’е `ref_open_from_notify`: при callback без sub_id вызывать `get_latest_subscription_for_telegram(telegram_user_id)` и при наличии подписки записывать `ref_nudge_clicked` (с проверкой на дубликат).  
  2) В `vpn_ok_callback` заменить кнопку «Получить ссылку» на `ref:open_from_notify:{sub_id}`.  
  3) По желанию: в `ref_open_from_ref_callback` аналогично писать ref_nudge_clicked по текущей подписке пользователя.
