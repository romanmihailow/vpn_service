# Pre-Prod Audit Report: Referral Reward Notification Consolidation

Проверка текущей реализации после объединения уведомления о реферальных баллах в одно сообщение (удаление второго короткого, обновление основного).

---

## 1. Signature & Calls

**OK**

- `send_referral_reward_notification` определена в `app/bot.py` с аргументами: `telegram_user_id`, `points_delta`, `level`, `tariff_code`, `payment_channel`, `referred_sub_id`.
- Все вызовы передают `referred_sub_id`:
  - **yookassa_webhook_runner.py**: два места — `referred_sub_id=subscription_id` (новая подписка), `referred_sub_id=base_sub_id` (продление).
  - **heleket_webhook_runner.py**: три места — `referred_sub_id=ext_sub_id`, `referred_sub_id=sub_id`, `referred_sub_id=subscription_id`.
- Старых вызовов с прежней сигнатурой (без `referred_sub_id`) нет.
- Импорты: в `yookassa_webhook_runner.py` и `heleket_webhook_runner.py` импортируется только `send_referral_reward_notification`; импорт `send_referral_points_awarded_notification` удалён, битых импортов нет.

---

## 2. Removal of second notification

**OK**

- `send_referral_points_awarded_notification` нигде не вызывается (поиск по коду — 0 вхождений).
- `REFERRAL_POINTS_AWARDED_TEXT` нигде не используется (константа удалена).
- Второе сообщение «🔥 Отличные новости!» не отправляется ни в одном payment flow: в webhook’ах остаётся только вызов `send_referral_reward_notification` и при необходимости `create_subscription_notification`.

---

## 3. Main notification text and buttons

**OK**

- Текст в `send_referral_reward_notification`:
  - «🎁 Тебе начислены реферальные баллы!»
  - «Из-за оплаты подписки по твоей реферальной цепочке.»
  - `Начислено: {sign}{points_delta} баллов.` — `sign = "+"` при `points_delta >= 0`, иначе пустая строка; формат корректен.
  - `Уровень реферала: {level_str}` — `level_str = str(level)` или `"—"` при `level is None`; обработано безопасно.
  - `Тариф: {tariff_code}` — подставляется как есть.
- Строка «Канал оплаты» убрана.
- Клавиатура: `reply_markup=_make_referral_points_awarded_keyboard(referred_sub_id)` передаётся в `bot.send_message`, кнопки привязаны к одному сообщению.

**Замечание (низкий риск):** при `parse_mode="HTML"` значения `tariff_code` и `level_str` не экранируются; при появлении в данных символов `<`, `>`, `&` возможны артефакты отображения. Для типичных кодов тарифов риск минимален.

---

## 4. Callback flows

**OK**

- Клавиатура основного уведомления:
  - «🎮 Оплатить баллами» → `points:open:from_referral:{referred_sub_id}`
  - «🤝 Пригласить друга» → `ref:open_from_referral:points:{referred_sub_id}`
- Обработчики:
  - `points:open:from_referral:` — `points_open_from_referral_callback` в `tg_bot_runner.py`: парсит `referred_sub_id`, при отсутствии записи создаёт `referral_points_awarded_pay_clicked`, затем показывает flow оплаты баллами.
  - `ref:open_from_referral:` — `ref_open_from_referral_callback`: для контекста `points` записывает `referral_points_awarded_ref_clicked`, затем тот же referral flow.
- Обработка неверного/битого `referred_sub_id`: проверка `len(parts) < 4`, `int(parts[3])` в try/except, при ошибке — ответ пользователю «Ошибка данных кнопки.»; падений нет.
- Tracking `referral_points_awarded_pay_clicked` и `referral_points_awarded_ref_clicked` не изменён, логика записи по `referred_sub_id` сохранена.

---

## 5. Webhook flows

**OK**

- Логика начисления баллов (цикл по `awards`, `apply_referral_rewards_for_subscription`, ключи `referrer_telegram_user_id`, `bonus`, `level`) не менялась.
- Для каждой награды вызывается `send_referral_reward_notification(..., referred_sub_id=...)`; затем, если ещё нет записи по этой подписке и включены уведомления, выполняется `create_subscription_notification(..., "referral_points_awarded", ...)`. Запись создаётся один раз на подписку (первый проход по циклу, когда `!has_subscription_notification` и `is_ref_points_notification_enabled(ref_tg_id)`).
- Нет сценария «create без send»: запись создаётся только после того, как сообщение уже отправлено в том же цикле (сначала send, потом при необходимости create).
- Отправка основного сообщения не обёрнута проверкой `is_ref_points_notification_enabled`: уведомление уходит всем реферерам (как раньше длинное сообщение). Проверка `is_ref_points_notification_enabled` применяется только к созданию записи `referral_points_awarded` для CRM. Поведение согласовано с прежней схемой (длинное всегда, короткое — по настройке).

---

## 6. CRM consistency

**OK**

- В `get_crm_funnel_report` (db.py) типы `referral_points_awarded`, `referral_points_awarded_ref_clicked`, `referral_points_awarded_pay_clicked` учтены; подсчёт по `COUNT(DISTINCT subscription_id) FILTER (WHERE notification_type = ...)`.
- В `/crm_report` (tg_bot_runner.py) блок «Реферальные уведомления» выводит «уведомление «начислены баллы»», «нажали «Оплатить баллами»», «нажали «Пригласить друга»» и проценты при ненулевом знаменателе; деление только при `r.get("referral_points_awarded", 0) > 0`, ошибок деления нет.
- Метрика `referral_points_awarded` по-прежнему соответствует одному «событию на подписку» (одна запись на подписку), а клики привязаны к тому же сообщению с кнопками, рассинхрона нет.

---

## 7. Production Risks

- **Сигнатура:** риска нет — все вызовы обновлены, лишних или забытых аргументов нет.
- **Silent failure:** при исключении в `send_referral_reward_notification` webhook логирует через внешний `try/except` и не падает; запись `referral_points_awarded` может не создаться при падении до неё — допустимо, идемпотентность при повторной доставке сохраняется.
- **Битые callback:** `referred_sub_id` берётся из той же подписки, что и в webhook; при удалении подписки позже callback может не найти `sub` и показать «Подписка не найдена.» — штатное поведение, без падения.
- **Отправка при выключенной настройке:** основное уведомление уходит всем реферерам независимо от `is_ref_points_notification_enabled` (как раньше длинное). Кто отключил «уведомления о начислении баллов», по-прежнему получает одно сообщение о баллах; в CRM учитывается только при включённой настройке. Изменения по сравнению с предыдущим поведением нет.

---

## 8. Smoke Tests

1. **Оплата с рефералом (YooKassa):** оплата по реферальной ссылке → реферер получает ровно одно сообщение с текстом про баллы, уровнем, тарифом и кнопками «Оплатить баллами» и «Пригласить друга».
2. **Продление с рефералом (YooKassa):** продление подписки с цепочкой рефералов → каждый реферер получает одно такое же сообщение.
3. **Heleket (новая подписка и продление):** то же для сценариев Heleket — одно сообщение на награду, с кнопками.
4. **Кнопка «Оплатить баллами»:** нажатие из уведомления открывает экран оплаты баллами; в CRM увеличивается «нажали «Оплатить баллами»».
5. **Кнопка «Пригласить друга»:** нажатие открывает реферальную ссылку; в CRM увеличивается «нажали «Пригласить друга»» в блоке баллов.
6. **/crm_report:** блок «Реферальные уведомления» отображается без ошибок, числа и проценты по баллам считаются.
7. **Настройка «уведомления о баллах» выключена:** при выключенной настройке реферер по-прежнему получает одно сообщение о начислении баллов; в отчёте запись по этой подписке может не появиться (как и раньше для короткого уведомления).

---

## 9. Final Verdict

**READY FOR PROD WITH MINOR RISKS**

- Второе уведомление удалено, основное обновлено (текст + кнопки), все вызовы и callback согласованы.
- Риски минимальны: возможное некорректное отображение при спецсимволах в `tariff_code`/`level` в HTML; поведение при отключённой настройке не изменилось.
- Рекомендуется после деплоя выполнить smoke-проверки из п. 8 (минимум 1–2 и 4–6).
