# Pool and Silent Handler Stabilization Audit

**Дата:** 2025-03-12  
**Контекст:** Инцидент connection pool exhausted и «тишина» по командам /start и /status. Цель — выявить слабые места без большого рефакторинга и зафиксировать точечные стабилизационные правки.

---

## 1. Executive summary

- **Silent handlers:** После фиксов `/start` и `/status` защищены fallback-ответом. Остаются риски: `/ref`, `/subscription`, `/points`, AI-support entry, несколько callback-хендлеров (ref_trial:claim, config:resend, vpn_ok и др.) — при ошибке БД или отправки пользователь может не получить ответ.
- **Background jobs:** Short confirmation после фиксов (max_age_seconds=900, batch_size=10) ограничена. Опасны для пула: **auto_new_handshake_admin_notification** (много DB вызовов на подписку, без batch limit), **auto_handshake_followup_notifications** (без batch limit), **auto_config_checkpoint** и **auto_recently_expired_trial_followup** (get_subscription_by_id в цикле, без batch limit), **auto_no_handshake_reminder** (без batch limit), **auto_welcome_after_first_payment** (без batch limit). **auto_notify_expiring_subscriptions** и **auto_deactivate_expired_subscriptions** уже используют batch/sleep или единичные выборки.
- **DB pool:** `DB_POOL_MAX=10` (по умолчанию в коде). При 9+ фоновых джобах и одновременных запросах пользователей пула может не хватать.
- **Откат:** Не требуется. Систему можно стабилизировать точечными фиксами (fallback в ключевых handlers, batch limits в джобах, при необходимости увеличение пула).
- **Первые три действия:** (1) Добавить fallback в cmd_ref и в AI-support handler. (2) Ввести batch limit в auto_handshake_followup_notifications. (3) Ввести batch limit в auto_new_handshake_admin_notification (или ограничить выборку в БД).

---

## 2. Silent handlers audit

### Критерии

- Есть ли try/except вокруг основной логики (включая отправку ответа).
- Есть ли fallback-ответ пользователю при ошибке.
- Есть ли log.exception / log.error.
- Может ли пользователь получить «тишину» (никакого ответа).

### SAFE handlers (полностью защищены)

| Handler | try/except | fallback | logging |
|--------|------------|----------|---------|
| **cmd_start** | Да | Да (текст без клавиатуры) | log.exception |
| **cmd_status** | Да | Да (сообщение «попробуй позже») | log.exception |
| **cmd_buy**, **cmd_buy_points**, **cmd_buy_crypto** | Нет БД до ответа | — | — |
| **pay:open** | Нет БД | — | — |

### Partially protected (DB ошибка даёт ответ, сбой отправки — тишина)

| Handler | Защита | Где остаётся риск |
|--------|--------|--------------------|
| **cmd_ref** | try/except + fallback только для get_or_create_referral_info | get_me(), построение текста, message.answer() — при падении любого ответа не будет |
| **cmd_points** | try/except + fallback для balance/transactions | message.answer() после построения текста |
| **cmd_subscription** | try/except для get_active_tariffs(), fallback tariffs=[] | message.answer() после формирования текста |
| **subscription:open** | Вызывает cmd_subscription | тот же риск |

### RISKY handlers

| Handler | Где падает → тишина | Рекомендация |
|---------|----------------------|--------------|
| **cmd_ref** | get_me() или message.answer(); админ-блок с get_referral_admin_stats, get_all_active_public_keys_with_users и т.д. | Обернуть всю отправку в try/except, при ошибке — короткий fallback («Не удалось загрузить. Попробуй позже.») + log.exception. |
| **cmd_subscription** | message.answer() после формирования текста | try/except вокруг answer + fallback текст. |
| **cmd_points** | message.answer() после формирования текста | try/except вокруг answer + fallback. |
| **AI-support** (`handle_support_message` в support/router.py) | process_support_message() или message.answer(); внутри — build_user_context (DB), действия с DB | try/except вокруг вызова и answer, при ошибке — ответ вида «Что-то пошло не так. Напиши в поддержку @…» + log.exception. |
| **ref_trial:claim** | get_referrer_telegram_id, get_latest_subscription_for_telegram, user_can_claim_referral_trial до первого callback.answer() | try/except в начале с callback.answer(show_alert=True) + log.exception. |
| **config:resend:** | get_subscription_by_id до try-блока с send_vpn_config_to_user | При падении get_subscription_by_id — callback.answer() не вызывается. Обернуть получение sub в try, при ошибке — callback.answer("Ошибка. Попробуй позже.", show_alert=True). |
| **vpn_ok:** | has_subscription_notification, get_subscription_by_id, callback.message.answer() | try/except с callback.answer(show_alert=True) при ошибке. |
| **config_check_now:**, **config_check_ok:**, **config_check_failed:** и т.д. | Аналогично: DB + answer; при исключении до answer — тишина | Точечно оборачивать в try/except с callback.answer(show_alert=True). |

Админ-команды (admin_*, broadcast, promo_admin и т.д.) не рассматривались как приоритет для массового пользователя; при желании их можно защитить по тому же шаблону.

---

## 3. Background jobs audit

| Job | Интервал | Batched? | Sleep в цикле | DB вызовов на кандидата | Удерживает соединения? | Риск pool exhaustion |
|-----|----------|----------|----------------|--------------------------|------------------------|----------------------|
| **auto_notify_expiring_subscriptions** | 600 с | Да, NOTIFY_BATCH_SIZE=25, NOTIFY_BATCH_SLEEP=1 | После каждых 25 | 2 (has + create) на подписку | Умеренно (batch + sleep) | Низкий |
| **auto_deactivate_expired_subscriptions** | 60 с | Нет лимита выборки | Нет в цикле | 2 на подписку (get_expired, deactivate) | Короткий прогон | Средний при большом числе истёкших |
| **auto_revoke_unused_promo_points** | 86400 с | Нет лимита | Нет | 1 выборка + N × add_points | Один прогон в сутки | Низкий |
| **auto_new_handshake_admin_notification** | 120 с | **Нет** | sleep(1) после отправки юзеру | Много: get_subscriptions + на каждую has_subscription_notification, create, get_referrer_with_count, count_referrer_paid_referrals, get_promo_info_for_subscription | Долго при большом with_handshake | **Высокий** |
| **auto_handshake_followup_notifications** | 120 с | **Нет** | sleep(1) | 1 выборка по типу + на каждую create | Долго при многих кандидатах | **Высокий** |
| **auto_handshake_short_confirmation** | 60 с | **Да**, batch_size=10, max_age=900 | sleep(1) | 1 выборка + до 10×(send + create) | Ограничено | Низкий после фиксов |
| **auto_welcome_after_first_payment** | 600 с | **Нет** | sleep(1) | 1 выборка + на каждую create | Может быть много | **Средний/высокий** |
| **auto_no_handshake_reminder** | 3600 с | **Нет** | 5 с между отправками, пауза между типами | get_handshake (wg) + выборка + на каждую create (или после send) + refresh каждые 20 | Долго при большом списке | **Высокий** |
| **auto_config_checkpoint** | 60 с | **Нет** | sleep(1) | get_pending + на каждого get_subscription_by_id + send + create; внутри цикла ещё get_handshake_timestamps | Много соединений в одном прогоне | **Высокий** |
| **auto_recently_expired_trial_followup** | 60 с | **Нет** | sleep(1) | get_pending + на каждого get_subscription_by_id + send + create | Аналогично | **Высокий** |

Уже защищены batch/max_age: **auto_handshake_short_confirmation** (batch 10, max_age 900), **auto_notify_expiring_subscriptions** (batch 25 + sleep).

Стоит ограничить: **auto_handshake_followup_notifications**, **auto_new_handshake_admin_notification**, **auto_config_checkpoint**, **auto_recently_expired_trial_followup**, **auto_no_handshake_reminder**, **auto_welcome_after_first_payment** (batch limit или LIMIT в SQL + sleep между батчами).

---

## 4. DB pool risk analysis

- **Использование пула:** В `app/db.py` — `psycopg2.pool.ThreadedConnectionPool(minconn=settings.DB_POOL_MIN, maxconn=settings.DB_POOL_MAX)`. Каждый вызов `get_conn()` берёт соединение из пула; контекстный менеджер возвращает его в пул.
- **Настройки:** В `app/config.py`: `DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))`, `DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))`. В репозитории нет `env.example`; значение по умолчанию в коде — 10.
- **Одновременные соединения:** До 10 соединений. При нескольких джобах (каждая держит минимум 1 соединение на время выборки/цикла) плюс запросы пользователей (start, status, ref, callbacks) пул может исчерпываться. Рекомендация: для текущего набора джобов и трафика рассмотреть увеличение до 15–20 с мониторингом; при добавлении batch limits необходимость может отпасть.
- **Паттерны риска:**
  - Один запрос пользователя: несколько последовательных `get_conn()` (например start: get_start_keyboard → user_can_claim_referral_trial) — нормально, если пул не перегружен.
  - Джобы: в одном прогоне много последовательных вызовов (get_candidates, затем в цикле has/create или get_subscription_by_id) — при большом числе кандидатов один job может долго удерживать соединения по очереди и усугублять конкуренцию за пул.

---

## 5. Short confirmation re-check

**auto_handshake_short_confirmation** после фиксов:

- **max_age_seconds = 900** — кандидаты только в окне 1–15 минут после первого handshake. Старым пользователям при каждом прогоне не слать.
- **batch_size = 10** — за один прогон обрабатывается не более 10 кандидатов.
- **DB на кандидата:** один запрос выборки (общий), затем на каждого из до 10: send_message + create_subscription_notification (один DB вызов).
- **sleep(1)** между отправками есть.
- **Конфликт по времени:** интервал 60 с; другие джобы с интервалами 60–120 с. Возможна одновременная работа нескольких джобов — риск исчерпания пула остаётся общим, но short confirmation сама по себе ограничена и не является главным источником риска.

Вывод: short confirmation после фиксов безопасна; дополнительно ограничивать не обязательно. Риск повторно захватить «слишком много» кандидатов устранён.

---

## 6. Rollback recommendation

**Нужен ли откат на несколько коммитов назад?**

**Ответ: A. Нет, система стабилизируема точечными фиксами.**

- Код `/start` и `/status` не ломали логику; проблема была в отсутствии обработки ошибок и в перегрузке пула джобой short confirmation. Short confirmation уже ограничена (max_age + batch); пул можно разгрузить batch limits в других джобах и при необходимости увеличить DB_POOL_MAX.
- Частичный или полный откат не требуется по текущему коду.

---

## 7. Stabilization plan (P0 / P1 / P2)

### P0 — срочно

1. **cmd_ref:** обернуть всю логику после проверки user в try/except; при любой ошибке отправлять короткий fallback («Не удалось загрузить реферальную информацию. Попробуй позже или напиши в поддержку.») и вызывать log.exception.
2. **AI-support handler** (support/router.py): обернуть `process_support_message` и `message.answer` в try/except; при ошибке — ответ пользователю с текстом вроде «Что-то пошло не так. Напиши в поддержку: @…» и log.exception.
3. **auto_handshake_followup_notifications:** ввести batch limit (например 15–20 за прогон): брать `candidates[:BATCH_SIZE]`, после цикла по батчу оставить как есть; при необходимости добавить sleep между батчами (уже есть sleep(1) между отправками).

### P1 — желательно

4. **cmd_subscription:** try/except вокруг `message.answer(...)`; при исключении — отправить короткий fallback (например TARIFFS_UNAVAILABLE или «Попробуй позже») и log.exception.
5. **cmd_points:** try/except вокруг финального `message.answer(...)`; при ошибке — fallback «Не удалось показать баллы. Попробуй позже.» + log.exception.
6. **auto_new_handshake_admin_notification:** ограничить число обрабатываемых подписок за прогон (например LIMIT в SQL или срез списка with_handshake[:N], N=20–30) и оставить sleep(1) после отправки пользователю.
7. **auto_config_checkpoint:** ограничить число кандидатов за прогон (candidates[:N], N=15–20); оставить sleep(1) между отправками.
8. **auto_recently_expired_trial_followup:** то же — candidates[:N], N=15–20.
9. **ref_trial:claim:** в начале обработчика try/except вокруг первых вызовов БД (get_referrer_telegram_id, get_latest_subscription_for_telegram, user_can_claim_referral_trial); при ошибке — callback.answer(show_alert=True) + log.exception.
10. **config:resend:** обернуть вызов db.get_subscription_by_id (и последующую проверку) в try/except; при ошибке — callback.answer("Ошибка. Попробуй позже.", show_alert=True).

### P2 — потом

11. **auto_no_handshake_reminder:** ввести batch limit по типам (например не более 30–50 за тип за прогон) или LIMIT в SQL.
12. **auto_welcome_after_first_payment:** ограничить выборку (LIMIT в SQL или срез списка, например 30).
13. **auto_deactivate_expired_subscriptions:** при росте числа истёкших подписок рассмотреть batch (например обрабатывать не более 50 за прогон) и sleep между батчами.
14. **DB_POOL_MAX:** поднять до 15–20 при сохранении текущего набора джобов и отсутствии batch limits; после внедрения batch limits перепроверить и при необходимости оставить 15 или вернуть 10.
15. **callback handlers config_check_*, vpn_ok и др.:** добавить try/except с callback.answer(show_alert=True) при ошибке, чтобы пользователь не оставался без реакции.

---

## 8. Итог

- **Риск silent failure остаётся у:** cmd_ref (отправка/ get_me), cmd_subscription (отправка), cmd_points (отправка), AI-support (весь handler), callbacks ref_trial:claim, config:resend, vpn_ok и части config_check_*.
- **Джобы, опасные для pool exhaustion:** auto_new_handshake_admin_notification, auto_handshake_followup_notifications, auto_config_checkpoint, auto_recently_expired_trial_followup, auto_no_handshake_reminder, auto_welcome_after_first_payment (все без batch limit или с большим числом DB вызовов в цикле).
- **Откат:** не нужен; стабилизация — точечными фиксами.
- **Первые три действия:** (1) Fallback в cmd_ref и в AI-support handler. (2) Batch limit в auto_handshake_followup_notifications. (3) Batch limit (или LIMIT в БД) в auto_new_handshake_admin_notification.
