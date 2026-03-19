# Pre-Prod Audit Report: User Notification Preferences

**Дата:** 2025-03-12  
**Область:** Настройки реферальных уведомлений (user_notification_preferences, /notify_ref_*).  
**Режим:** Только анализ, без изменений кода.

---

## 1. Синтаксис и импорты

**Статус: OK**

- Импорты: в `tg_bot_runner.py` используются `db.get_or_create_user_notification_preferences`, `db.set_ref_connected_notification_enabled`, `db.set_ref_points_notification_enabled`, `db.is_ref_connected_notification_enabled`, `db.is_ref_points_notification_enabled`. В `yookassa_webhook_runner.py` и `heleket_webhook_runner.py` — только `db.is_ref_points_notification_enabled` (модуль `db` уже импортирован). Циклических импортов нет.
- Все пять DB-функций используются: get_or_create — в cmd_notify_ref_status; set_* — в cmd_notify_ref_connected / cmd_notify_ref_points; is_ref_connected — в handshake job; is_ref_points — в 5 местах (yookassa x2, heleket x3). Конфликтов имён нет.

---

## 2. DB слой

**Статус: OK**

- Таблица `user_notification_preferences` создаётся в `init_db()` в одном блоке `create_table_sql`: поля `telegram_user_id BIGINT PRIMARY KEY`, `ref_connected_enabled BOOLEAN NOT NULL DEFAULT TRUE`, `ref_points_enabled BOOLEAN NOT NULL DEFAULT TRUE`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`. Отдельной таблицы с таким именем больше нет, конфликтов с существующими таблицами нет.
- **get_or_create_user_notification_preferences:** выполняется `INSERT ... ON CONFLICT (telegram_user_id) DO NOTHING`, затем `SELECT`. При одновременном первом вызове от одного пользователя оба потока могут выполнить INSERT; один получит conflict и DO NOTHING, оба затем прочитают одну и ту же строку. Двойная вставка не возникает, race даёт только лишний no-op INSERT.
- **set_ref_connected_notification_enabled / set_ref_points_notification_enabled:** используют `INSERT ... ON CONFLICT (telegram_user_id) DO UPDATE SET ...`. Идемпотентно, блокировок помимо стандартного row-level lock при UPDATE нет.
- **is_ref_connected_notification_enabled / is_ref_points_notification_enabled:** один `SELECT` по `telegram_user_id`; при отсутствии строки возвращается `True`. Дополнительных блокировок нет.

---

## 3. Notification logic

**Статус: OK**

### referral_user_connected

- Единственное место: `tg_bot_runner.py`, job `auto_new_handshake_admin_notification`.
- Условие: `referrer_id and await asyncio.to_thread(db.is_ref_connected_notification_enabled, referrer_id) and not await asyncio.to_thread(db.has_subscription_notification, sub_id, "referral_user_connected")`.
- Проверка флага выполняется **до** вызова `send_referral_user_connected_notification` и **до** `create_subscription_notification`. При `is_ref_connected_notification_enabled == False` ни send, ни create не вызываются.

### referral_points_awarded

- **YooKassa (2 места):** основной платёж (subscription_id) и продление (base_sub_id). В обоих: условие `not db.has_subscription_notification(..., "referral_points_awarded") and db.is_ref_points_notification_enabled(ref_tg_id)`; при True выполняются send и create. Пропусков нет.
- **Heleket (3 места):** ext_sub_id, sub_id (продление), subscription_id (новая подписка). Во всех трёх добавлено `and db.is_ref_points_notification_enabled(ref_tg_id)`. Логика `has_subscription_notification` не менялась, условие только расширено.

---

## 4. CRM

**Статус: OK**

- Запись в `subscription_notifications` (referral_user_connected / referral_points_awarded) создаётся только внутри того же блока, где вызывается send. При выключенной настройке условие не выполняется, блок не входит → ни send, ни create не вызываются.
- «Ложных отправок» нет: запись создаётся только при реальной отправке. Ситуации «send не произошёл, create произошёл» нет: оба вызова в одном условном блоке.

---

## 5. Callback flows

**Статус: OK**

- Обработчики `ref:open_from_referral:*` и `points:open:from_referral:*` не менялись. Логика настроек читает/пишет только `user_notification_preferences` и не участвует в обработке callback.
- Старые callback не перекрыты, новые команды не влияют на callback-логику.

---

## 6. Commands

**Статус: OK**

- В `set_bot_commands` добавлена одна команда: `BotCommand(command="notify_ref_status", description="Настройки реферальных уведомлений")`. Список остальных команд не менялся.
- Парсинг: `text.split(maxsplit=1)`, аргумент `parts[1].strip().lower().replace(" ", "")` при `len(parts) == 2`, иначе `""`. При пустом аргументе или без аргумента получается `arg not in ("on", "off")` → показ подсказки «Неверный формат команды» и использование. Падений нет.
- Оба обработчика (/notify_ref_connected и /notify_ref_points) при неверном аргументе возвращают один и тот же текст с подсказкой по всем четырём вариантам команд.

---

## 7. Async / event loop

**Статус: OK с замечанием**

- **tg_bot_runner.py:** все вызовы db для настроек в async-контексте обёрнуты в `asyncio.to_thread`: `get_or_create_user_notification_preferences`, `is_ref_connected_notification_enabled`, `set_ref_connected_notification_enabled`, `set_ref_points_notification_enabled`. Event loop не блокируется.
- **yookassa_webhook_runner.py / heleket_webhook_runner.py:** `db.is_ref_points_notification_enabled(ref_tg_id)` вызывается синхронно (без to_thread). В тех же обработчиках уже используются синхронные вызовы `db.has_subscription_notification` и `db.create_subscription_notification` — добавлен один короткий SELECT на один award. По объёму это тот же класс нагрузки, что и раньше; при высокой частоте webhook-запросов теоретически возможна кратковременная блокировка event loop. Рекомендация: при росте нагрузки рассмотреть вынос этих проверок в to_thread/executor; для текущего объёма приёмлемо.

---

## 8. Production risks

- **Race при первом get_or_create:** два одновременных запроса от одного пользователя могут оба выполнить INSERT; один получит conflict. Результат: одна запись, оба вызова получают одинаковые настройки. На логику не влияет.
- **Блокировка event loop в webhooks:** синхронные вызовы `db.is_ref_points_notification_enabled` в async-обработчиках; один быстрый SELECT на один award. Риск низкий при умеренной частоте webhook; при очень высокой нагрузке стоит вынести в executor.
- **Потеря уведомления:** при выключенной настройке уведомление не отправляется по задумке; при включённой логика отправки и записи не менялась — дополнительных сценариев потери нет.
- **Дубли уведомлений:** идемпотентность по-прежнему обеспечивается `has_subscription_notification`; флаг настроек только сужает условие, дублей не добавляет.
- **Silent failure:** при исключении в send блок try/except логирует и не создаёт запись; при сбое БД в set_* или is_* исключение всплывает. Отдельного «тихого» подавления ошибок для новых путей нет.

---

## 9. Smoke tests

1. **/notify_ref_status** — без аргументов; в ответе «Подключение приглашённых» и «Начисление баллов» (включено/выключено), блок «Команды» с двумя строками.
2. **/notify_ref_connected off** — ответ «Готово 👍» и текст про отключение; повторный /notify_ref_status — «Подключение приглашённых: выключено».
3. **/notify_ref_connected on** — ответ про включение; /notify_ref_status — «включено».
4. **/notify_ref_points off** и **/notify_ref_points on** — аналогично, проверка по статусу «Начисление баллов».
5. **/notify_ref_connected** без on/off (или с неверным аргументом) — ответ «Неверный формат команды» и подсказка по использованию; бот не падает.
6. **/notify_ref_points** с лишним аргументом (например `foo`) — та же подсказка.
7. **referral_user_connected при выключенной настройке:** у пользователя A выключить «Подключение приглашённых», приглашённый B делает первый handshake — A не получает уведомление «Есть результат!»; в /crm_report счётчик «уведомление «пользователь подключился»» не увеличивается.
8. **referral_points_awarded при выключенной настройке:** у реферера выключить «Начисление баллов», приглашённый платит — реферер не получает короткое CTA-уведомление о баллах; в CRM счётчик «уведомление «начислены баллы»» не увеличивается.
9. **При включённых настройках:** сценарии «приглашённый подключился» и «начислены баллы» по одному разу — уведомления приходят, в CRM соответствующие счётчики +1.
10. **Кнопки из старых уведомлений:** сообщения с кнопками ref:open_from_referral:connected/points и points:open:from_referral по нажатию открывают тот же flow, без ошибок.

---

## 10. Final verdict

**READY FOR PROD WITH MINOR RISKS**

- Логика настроек и мест проверки согласована; CRM и callback не затронуты; запись в subscription_notifications только при реальной отправке.
- Риски: возможная кратковременная блокировка в webhook при очень высокой нагрузке (один дополнительный синхронный SELECT на award) и теоретический race при самом первом get_or_create (без последствий для корректности). Для типичного объёма допустимо.
- Рекомендуется после выката проверить smoke-тесты 1–10.

---

*Pre-prod audit complete. No code changes were made.*
