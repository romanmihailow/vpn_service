# Pre-Prod Audit Report: Referral & CRM Changes

**Дата аудита:** 2025-03-12  
**Область:** Реферальные уведомления, callback flows, webhook-логика, daily summary, CRM report.  
**Режим:** Только проверка, без изменений кода.

---

## 1. Синтаксис и импорты

**Статус: OK**

- **Импорты:** В `tg_bot_runner.py` подключены `send_referral_user_connected_notification`, `send_referral_daily_summary_notification`; в `yookassa_webhook_runner.py` и `heleket_webhook_runner.py` — `send_referral_reward_notification`, `send_referral_points_awarded_notification`. Битых импортов не обнаружено.
- **Конфликты имён:** Отдельные имена функций/констант (например, `ref_open_from_notify` и `ref_open_from_referral_callback`) не пересекаются.
- **Callback handlers:** Обработчики разведены по префиксам: `ref:open_from_notify` (startswith) и `ref:open_from_referral:` (startswith) — множества callback_data не пересекаются. `points:open` обрабатывается по точному совпадению (`F.data == "points:open"`), `points:open:from_referral:` — по `startswith`; обработчик `points:open:from_referral:` зарегистрирован выше (стр. 1431), чем `points:open` (стр. 1496), поэтому корректный callback всегда попадает в нужный handler.
- **Unreachable code:** Явно недостижимых веток не выявлено.
- **Модуль datetime:** В `auto_referral_daily_summary` используется `timedelta(days=1)` внутри `_sleep_until_next_noon`; `timedelta` импортирован на уровне модуля (`from datetime import datetime, timedelta, timezone` в строке 4), замыкание видит его — ошибки нет.

---

## 2. Callback flows

**Статус: OK**

| Callback | Обработчик | Примечание |
|----------|------------|------------|
| `ref:open_from_referral:connected:{id}` | `ref_open_from_referral_callback` | Парсинг `parts[3]` как `referred_sub_id`, проверка `get_subscription_by_id`, запись `referral_user_connected_ref_clicked`. |
| `ref:open_from_referral:points:{id}` | тот же | Аналогично, запись `referral_points_awarded_ref_clicked`. |
| `ref:open_from_referral:summary` | тот же | Ветка `context == "summary"`: без sub_id, без записи в subscription_notifications, только показ referral flow. |
| `points:open:from_referral:{id}` | `points_open_from_referral_callback` | Парсинг `parts[3]`, запись `referral_points_awarded_pay_clicked`, затем тот же UX, что и у `points:open`. |

- **Старые callback:** `ref:open_from_notify` и `ref:open_from_notify:{sub_id}` обрабатываются отдельно в `ref_open_from_notify`; `points:open` и `pay:open` не перекрыты новыми префиксами.
- **Парсинг sub_id:** Для `connected`/`points` при `len(parts) < 4` или нечисловом `parts[3]` возвращается alert «Ошибка данных кнопки»; при отсутствии подписки — «Подписка не найдена». Падения нет.
- **summary:** Не требует sub_id, referral flow показывается по текущему пользователю; конфликта с существующей логикой нет.

---

## 3. Notifications and idempotency

**Статус: OK (минорные риски см. в п. 9)**

- **referral_user_connected:** Создаётся в handshake job после успешной отправки `send_referral_user_connected_notification`, внутри `if ok:` (после успешной отправки пользователю). Защита от дубля: `has_subscription_notification(sub_id, "referral_user_connected")`. Запись по паре (subscription_id, notification_type) — один раз на подписку.
- **referral_user_connected_ref_clicked / referral_points_awarded_ref_clicked:** Пишутся в callback handler по referred_sub_id; перед записью проверка `has_subscription_notification(referred_sub_id, click_type)`. Дубль не создаётся.
- **referral_points_awarded:** В YooKassa и Heleket создаётся после отправки короткого уведомления; проверка `has_subscription_notification(subscription_id, "referral_points_awarded")` до отправки. Из-за уникального индекса по (subscription_id, notification_type) только первый реферер в цикле awards получает короткое уведомление и запись — задуманное поведение.
- **referral_points_awarded_pay_clicked:** Записывается в `points_open_from_referral_callback` с проверкой `has_subscription_notification(referred_sub_id, "referral_points_awarded_pay_clicked")`.
- **referral_daily_summary_sent:** Отдельная таблица с PK (telegram_user_id, sent_date). Запись через `create_referral_daily_summary_sent` с `ON CONFLICT (telegram_user_id, sent_date) DO NOTHING`. Кандидаты отфильтрованы через `sent_today` (NOT EXISTS в referral_daily_summary_sent за CURRENT_DATE). Конфликта с subscription_notifications нет — дайджест не привязан к subscription_id.

**Риск:** Если отправка рефереру (referral_user_connected или referral_points_awarded) успешна, но `create_subscription_notification` выбросит исключение, при следующем проходе проверка has не найдёт запись и уведомление может уйти повторно. Аналогично для daily summary: при успешной отправке и падении `create_referral_daily_summary_sent` возможна повторная отправка на следующий день. Оценка: низкая вероятность, допустимо для первого релиза.

---

## 4. Webhook flows

**Статус: OK**

- **YooKassa:** В циклах по `awards` везде используются `award.get("referrer_telegram_user_id")`, `award.get("bonus")`, `award.get("level")`; вызовы `send_referral_reward_notification(telegram_user_id=ref_tg_id, ...)` и `send_referral_points_awarded_notification(referrer_telegram_id=ref_tg_id, referred_sub_id=...)` согласованы. Короткое уведомление и запись referral_points_awarded выполняются внутри одного прохода по awards с проверкой `has_subscription_notification(subscription_id, "referral_points_awarded")` — одна запись на подписку.
- **Heleket:** Три сценария (новая подписка, продление по base_sub, ещё один путь) проверены: везде `referrer_telegram_user_id`, `bonus`, `level`; нигде не осталось старой сигнатуры или неверных ключей. Используются корректные subscription_id (subscription_id / base_sub_id / ext_sub_id / sub_id в зависимости от контекста — всегда подписка плательщика/приведённого).
- Двойной отправки в одном сценарии платежа нет: запись создаётся после отправки, при повторном webhook та же подписка уже имеет запись referral_points_awarded.
- Логика начисления баллов (`apply_referral_rewards_for_subscription`) не менялась; вызовы только дополнены уведомлениями и записью в subscription_notifications.

---

## 5. Handshake job

**Статус: OK**

- Уведомление пользователю при первом handshake (post-VPN message + CTA) и запись `handshake_user_connected` выполняются по-прежнему; блок реферера добавлен внутри того же `if ok:` после записи handshake_user_connected.
- Порядок: сначала отправка пользователю и создание handshake_user_connected, затем получение referrer_id, отправка рефереру и создание referral_user_connected. Последовательность соблюдена.
- При отсутствии реферера `get_referrer_telegram_id` вернёт None, блок не выполнится — падения нет.
- При ошибке отправки рефереру исключение перехватывается, логируется, запись referral_user_connected не создаётся — при следующем проходе job попытается отправить снова (идемпотентность по факту записи).
- Admin notification и остальная логика job не затронуты.

---

## 6. Daily summary job

**Статус: OK**

- Первый запуск: `_sleep_until_next_noon()` считает следующее 12:00 UTC; если уже прошло, добавляется `timedelta(days=1)`. Используется `datetime.now(timezone.utc)` и `REFERRAL_DAILY_SUMMARY_RUN_AT_HOUR_UTC = 12`. Дрифта нет при однократном sleep до следующего полдня и последующем цикле с `REFERRAL_DAILY_SUMMARY_INTERVAL_SEC = 86400`.
- Lock: `acquire_job_lock(DB_JOB_LOCK_REFERRAL_DAILY_SUMMARY)` перед обработкой, в `finally` — `release_job_lock`. При исключении внутри try lock снимается.
- Кандидаты: `get_referral_daily_summary_candidates()` — CTE level2_referrers (top_ref = уровень 1, l2_user = уровень 2); connected/payments считаются по subscription_notifications для подписок l2_user (referral_user_connected / referral_points_awarded за 24 ч); points — из user_points_transactions с reason LIKE 'ref_level_%' AND level >= 2 по telegram_user_id (получатель баллов). В дайджест попадают только рефереры с активностью уровня 2+; уровень 1 не получает дайджест вместо realtime — realtime для уровня 1 не трогается.
- Условие выдачи: `(COALESCE(c.cnt, 0) > 0 OR COALESCE(p.cnt, 0) > 0)` — при нуле подключений и нуле оплат дайджест не отправляется; нулевой points_sum допустим в тексте.
- После отправки вызывается `create_referral_daily_summary_sent(tg_id)`; при исключении в цикле для одного пользователя остальные обрабатываются, lock в finally снимается.

---

## 7. CRM report

**Статус: OK**

- В `get_crm_funnel_report` список `types` содержит все нужные типы, включая referral_user_connected, referral_user_connected_ref_clicked, referral_points_awarded, referral_points_awarded_ref_clicked, referral_points_awarded_pay_clicked. В SELECT перечислены те же типы в том же порядке; маппинг row[16]…row[20] на result совпадает с порядком столбцов.
- referral_daily_summary считается отдельным запросом к таблице referral_daily_summary_sent по sent_at за период; результат пишется в result["referral_daily_summary"].
- В тексте /crm_report проценты для реферальных блоков выводятся только при ненулевом знаменателе: `if r.get('referral_user_connected', 0)`, `if r.get('referral_points_awarded', 0)` — деления на ноль нет.
- Подписи («уведомление «пользователь подключился»», «нажали «Пригласить друга»» и т.д.) соответствуют метрикам. Блок «Реферальные дайджесты: отправлено» берёт значение из r.get('referral_daily_summary', 0).

---

## 8. UX consistency

**Статус: OK**

- Тексты реферальных уведомлений (REFERRAL_USER_CONNECTED_TEXT, REFERRAL_POINTS_AWARDED_TEXT) короткие, без технических терминов, в стиле остальных сообщений.
- Daily summary: «🔥 Обновление по вашей сети», «+N подключений», «+N оплат», «+N баллов начислено», «Продолжай делиться — это работает 👍» — читается как дайджест, не как техсообщение.
- Кнопки: «🤝 Пригласить друга» под connected/points/summary ведут в один и тот же referral flow (deep link); «🎮 Оплатить баллами» в points-уведомлении открывает выбор тарифа баллами. Соответствие кнопок и потоков соблюдено.

---

## 9. Production Risks

1. **Запись после отправки:** При успешной отправке и падении записи в subscription_notifications / referral_daily_summary_sent возможен повтор уведомления при следующем проходе job или webhook. Митигация: мониторинг ошибок БД и логов.
2. **Отправка рефереру без проверки успеха:** Запись referral_user_connected создаётся после вызова send_referral_user_connected_notification без проверки возврата (функция не возвращает успех). Если Telegram вернёт ошибку уже после успешной доставки на стороне API, теоретически возможна повторная отправка. На практике маловероятно.
3. **Daily summary — исключение при отправке:** При падении send для одного пользователя запись в referral_daily_summary_sent не создаётся; на следующий день пользователь снова в кандидатах — возможна повторная отправка. Приемлемо.
4. **Множественные инстансы бота:** При нескольких воркерах job lock гарантирует один запуск daily summary; handshake и webhook обрабатывают события с идемпотентностью по БД.
5. **Рост нагрузки:** Увеличение числа уведомлений и записей в subscription_notifications — при текущих объёмах риска нет; при росте стоит смотреть индексы и нагрузку на БД.

---

## 10. Required Smoke Tests

1. **Реферал подключился:** Создать связь A → B, от имени B первый handshake; проверить, что A получил «Пользователь по вашей ссылке подключил VPN» и кнопка «Пригласить друга» открывает реферальную ссылку; в CRM — +1 к «уведомление «пользователь подключился»».
2. **Начисление баллов (YooKassa):** Оплата от приведённого; у реферера должны прийти оба сообщения (длинное про баллы и короткое CTA); кнопки «Оплатить баллами» и «Пригласить друга» работают; в CRM — рост referral_points_awarded и кликов.
3. **Начисление баллов (Heleket):** То же для сценария Heleket (новая подписка / продление).
4. **ref:open_from_referral:summary:** От имени пользователя с сетью 2+ (или замокать кандидата) нажать кнопку «Пригласить друга» в дайджесте; должен открыться тот же referral flow без ошибок.
5. **points:open vs points:open:from_referral:** Из обычного места нажать «Оплатить баллами» (points:open) — без записи pay_clicked; из уведомления о баллах — points:open:from_referral:{id} — в CRM должен появиться referral_points_awarded_pay_clicked.
6. **ref:open_from_notify:** Убедиться, что кнопки под follow-up/nudge по-прежнему ведут в ref flow и пишут ref_nudge_clicked, не смешиваясь с ref:open_from_referral.
7. **Daily summary:** В тестовой среде дождаться 12:00 UTC или временно изменить время запуска; проверить, что дайджест получили только кандидаты с активностью 2+ за 24 ч и что повторно в тот же день не отправляется; в /crm_report — рост «Реферальные дайджесты: отправлено».
8. **/crm_report:** Вызвать от админа; проверить, что все блоки (включая реферальные уведомления и дайджесты) выводятся, проценты при нулевых знаменателях не ломаются.
9. **Неверный callback_data:** Отправить callback ref:open_from_referral:points:999999 (несуществующий sub_id) — ожидается «Подписка не найдена» или «Ошибка данных кнопки», без падения.
10. **Handshake без реферера:** Первый handshake пользователя без реферера — только сообщение пользователю, без попытки отправить referral_user_connected; лог без ошибок.

---

## 11. Final Verdict

**READY FOR PROD WITH MINOR RISKS**

Текущая реализация согласована: callback flows разделены, идемпотентность соблюдена, webhook и handshake job используют правильные ключи и не ломают старую логику. Daily summary и CRM report корректно учитывают новые метрики. Риски ограничены краевыми случаями (запись после отправки, повторная отправка дайджеста при сбое записи) и допустимы для выката при условии выполнения smoke-тестов и мониторинга логов/БД после деплоя.

---

*Pre-prod audit complete. No code changes were made.*
