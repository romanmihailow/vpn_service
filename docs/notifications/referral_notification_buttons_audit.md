# Audit: Referral Notification Buttons and CRM Tracking

Технический аудит кнопок, tracking и CRM-отчёта перед добавлением двух новых реферальных уведомлений с CTA-кнопками. Без изменений кода — только анализ и рекомендации.

**Планируемые уведомления:**
1. Рефереру: «Пользователь по вашей ссылке подключил VPN» + кнопка «🤝 Пригласить друга».
2. Рефереру: «Вам начислены бонусные баллы за приглашение» + кнопки «🎮 Оплатить баллами», «🤝 Пригласить друга».

---

## 1. Existing CTA Buttons

Все найденные inline-кнопки, связанные с оплатой, баллами, приглашением друга и VPN follow-up.

| Файл | Функция / константа | Текст кнопки | callback_data / url | Где используется | Tracking |
|------|---------------------|--------------|----------------------|------------------|----------|
| `tg_bot_runner.py` | `SUBSCRIBE_KEYBOARD` | 🛒 Купить подписку | `pay:open` | Подписка / главное меню | — |
| `tg_bot_runner.py` | `SUBSCRIBE_KEYBOARD` | 🎮 Оплатить баллами | `points:open` | — | — |
| `tg_bot_runner.py` | `SUBSCRIBE_KEYBOARD` | 🤝 Пригласить друга | `ref:open_from_notify` | — | fallback: ref_nudge_clicked по последней подписке |
| `tg_bot_runner.py` | `SUBSCRIBE_KEYBOARD` | 🎟 Ввести промокод | `promo:open` | — | — |
| `tg_bot_runner.py` | `SUBSCRIBE_KEYBOARD` | 🌐 Открыть сайт | url | — | — |
| `tg_bot_runner.py` | `START_KEYBOARD` | 🛒 Купить / 🎮 Оплатить баллами / 🎟 Промокод | `pay:open`, `points:open`, `promo:open` | /start (без реферала) | — |
| `tg_bot_runner.py` | `REF_SHARE_KEYBOARD` | 🤝 Пригласить друга | `ref:open_from_ref` | /ref | нет (другой handler) |
| `tg_bot_runner.py` | `SUBSCRIPTION_PAGE_KEYBOARD` | 🛒 / 🎮 / 🤝 | `pay:open`, `points:open`, `ref:open_from_ref` | /subscription | — |
| `tg_bot_runner.py` | `POINTS_KEYBOARD` | 🎮 Оплатить баллами | `points:open` | Ответ на /points (баланс + история) | — |
| `tg_bot_runner.py` | `POINTS_KEYBOARD` | 🤝 Пригласить друга | `ref:open_from_notify` | — | fallback ref_nudge_clicked |
| `tg_bot_runner.py` | `SUBSCRIPTION_RENEW_KEYBOARD` | 🔁 Продлить / 🎮 Продлить баллами / 🤝 Пригласить | `pay:open`, `points:open`, `ref:open_from_notify` | Напоминания об истечении (3d, 1d, 1h) | — |
| `tg_bot_runner.py` | `get_status_keyboard(sub_id)` | 📱 Получить настройки | `config:resend:{sub_id}` | /status | — |
| `tg_bot_runner.py` | `get_status_keyboard` | 🛒 / 🎮 / 🤝 | `pay:open`, `points:open`, `ref:open_from_notify` | — | fallback ref_nudge_clicked |
| `tg_bot_runner.py` | `HANDSHAKE_USER_CONNECTED_KEYBOARD` | 💎 Закрепить доступ — 270 ₽ | `pay:open` | Первое сообщение после handshake | — |
| `tg_bot_runner.py` | `HANDSHAKE_USER_CONNECTED_KEYBOARD` | 📅 Все тарифы | `pay:open` | — | — |
| `tg_bot_runner.py` | `POST_VPN_STATUS_OK_KEYBOARD` | 💎 Закрепить доступ — 270 ₽ | `pay:open` | «VPN уже подключён» (config_check_now) | — |
| `tg_bot_runner.py` | `_make_post_vpn_success_keyboard(sub_id)` | 💎 Закрепить / 🤝 Пригласить друга / 📅 Все тарифы | `pay:open`, `ref:open_from_notify:{sub_id}` | После «Всё работает», success-сценарии | ref_nudge_clicked по sub_id |
| `tg_bot_runner.py` | `_make_post_vpn_followup_keyboard(sub_id)` | 💎 / 🤝 / 🧑‍💻 Нужна помощь | `pay:open`, `ref:open_from_notify:{sub_id}`, url | Follow-up 2h, 24h | ref_nudge_clicked по sub_id |
| `tg_bot_runner.py` | `_make_10m_keyboard(sub_id)` | Всё работает / Нужна помощь | `vpn_ok:{sub_id}`, url | Follow-up 10 мин | vpn_ok_clicked |
| `tg_bot_runner.py` | `_make_ref_nudge_keyboard(sub_id)` | 🤝 Пригласить друга | `ref:open_from_notify:{sub_id}` | Referral nudge 3d | ref_nudge_clicked по sub_id |
| `tg_bot_runner.py` | `build_tariff_keyboard_*` | Тарифы | `pay:tariff:<code>`, `points:tariff:<code>`, `heleket:tariff:<code>` | Выбор тарифа после pay:open / points:open / heleket:open | — |
| `tg_bot_runner.py` | no-handshake reminder | 📱 Отправить настройки ещё раз | `config_check_resend:{sub_id}` | no_handshake, recently_expired_trial | — |
| `support/actions.py` | referral intent | 👥 Пригласить друга | `ref:open_from_notify` | AI support (referral_info, referral_stats, referral_balance) | fallback ref_nudge_clicked |

Кратко:
- **pay:open**, **points:open** — без параметров, открывают выбор тарифа / баллы; tracking по кнопкам не ведётся.
- **ref:open_from_notify** и **ref:open_from_notify:{sub_id}** — один handler; при наличии `sub_id` и совпадении владельца подписки с пользователем пишется **ref_nudge_clicked** по этой подписке; иначе — fallback по последней активной подписке пользователя.
- **ref:open_from_ref** — только экран /ref, без записи в subscription_notifications.
- **vpn_ok:{sub_id}** — единственная кнопка с явным tracking (vpn_ok_clicked) по подписке.

---

## 2. Existing Click Tracking

Tracking кнопок реализован через записи в **subscription_notifications** (notification_type = тип события).

| notification_type | Когда создаётся | Где (файл: handler) | Связь с отправкой уведомления |
|-------------------|-----------------|----------------------|-------------------------------|
| **vpn_ok_clicked** | Нажатие «Всё работает» в follow-up 10m | `tg_bot_runner.py`: `vpn_ok_callback`, `F.data.startswith("vpn_ok:")` | После отправки handshake_followup_10m; один раз на подписку (has_subscription_notification проверка) |
| **ref_nudge_clicked** | Нажатие «🤝 Пригласить друга» под уведомлениями | `tg_bot_runner.py`: `ref_open_from_notify`, `F.data.startswith("ref:open_from_notify")` | Если в callback есть sub_id и подписка принадлежит пользователю — пишется по этой подписке; иначе по последней подписке пользователя (fallback). Один раз на подписку. |

Особенности:
- Оба типа привязаны к **subscription_id** (подписка пользователя, нажавшего кнопку).
- В CRM считаются как COUNT(DISTINCT subscription_id) за период (см. раздел 4).
- CTR считается только для двух воронок: vpn_ok_clicked / handshake_followup_10m и ref_nudge_clicked / handshake_referral_nudge_3d.

Других «button click» notification_type в текущем списке типов для CRM нет (config_check_ok, config_checkpoint_sent и т.д. — это факты отправки сообщений, а не кликов).

---

## 3. Existing Pay / Points / Referral Flows

### pay:open
- **Handler:** `pay_open_callback` — один ответ: «Выбери тариф» + `TARIFF_KEYBOARD` (pay:tariff:&lt;code&gt;).
- **Tracking:** нет.
- **Переиспользование:** безопасно везде; не привязан к контексту.

### points:open
- **Handler:** `points_open_callback` — проверка баланса, при достаточном — «Выбери тариф» + `POINTS_TARIFF_KEYBOARD`; при нехватке — текст без кнопок оплаты баллами.
- **Tracking:** нет.
- **Переиспользование:** безопасно; при использовании под новым уведомлением «начислены баллы» нельзя будет отличить «клик из этого уведомления» от других точек входа без доработки (новый callback или параметр + запись события).

### ref:open_from_notify и ref:open_from_notify:{sub_id}
- **Handler:** `ref_open_from_notify` — короткое реферальное сообщение + кнопка «Переслать другу»; при наличии sub_id в callback и совпадении владельца подписки с callback.from_user.id пишется **ref_nudge_clicked** по этой подписке.
- **Ограничение:** подписка должна принадлежать нажавшему (telegram_user_id подписки == callback.from_user.id). Для уведомления «реферал подключился» нажатие делает **реферер**, а подписка принадлежит **приведённому** — проверка владельца не пройдёт, ref_nudge_clicked по sub_id приведённого записать так нельзя. Fallback тогда запишет ref_nudge_clicked по **последней подписке реферера** — это смешает источник (handshake nudge vs новое реферальное уведомление) в одной метрике.

### Готовые клавиатуры по составу кнопок
- **«🤝 Пригласить друг» одна кнопка:**  
  `_make_ref_nudge_keyboard(sub_id)` — одна кнопка с `ref:open_from_notify:{sub_id}`. По смыслу подходит для уведомления «реферал подключился», но sub_id там — подписка того, кому показывают клавиатуру. В нашем случае показываем рефереру, а sub_id логично привязать к подписке приведённого (контекст события). Текущий handler не даст записать клик по «чужой» подписке — нужен отдельный callback/логика.
- **«🎮 Оплатить баллами» + «🤝 Пригласить друг»:**  
  **POINTS_KEYBOARD** — уже две кнопки: `points:open`, `ref:open_from_notify`. Состав подходит для уведомления «начислены баллы», но без sub_id/контекста и без отдельного tracking’а кликов из этого уведомления.

Вывод: кнопки и handler’ы можно переиспользовать по действию (открыть оплату баллами / реферальный экран), но для корректного CRM и разделения источников нужны либо новые callback_data с контекстом, либо отдельные notification_type при сохранении текущих callback.

---

## 4. CRM Report Coverage

**Файл:** `app/tg_bot_runner.py` — команда `/crm_report`, вызывает `db.get_crm_funnel_report(days)`.  
**Файл:** `app/db.py` — `get_crm_funnel_report(days)`.

### Метрики и notification_type в отчёте

- **Список типов** в `get_crm_funnel_report`:  
  handshake_user_connected, handshake_followup_10m, **vpn_ok_clicked**, handshake_followup_2h, handshake_followup_24h, handshake_referral_nudge_3d, **ref_nudge_clicked**, no_handshake_2h, no_handshake_24h, no_handshake_5d, no_handshake_survey, welcome_after_first_payment, no_handshake_survey_answer_1..4.  
  Плюс отдельный запрос: **first_paid_with_prior_handshake**.

- **Что выводится в тексте отчёта:**
  - Оплаты: первые платные подписки (welcome_after_first_payment).
  - Подключения: первый handshake (handshake_user_connected).
  - Конверсия: оплата → VPN подключен (%).
  - Воронка подключений: follow-up 10m, **«Всё работает» нажали (vpn_ok_clicked)** с % от handshake_followup_10m, follow-up 2h/24h, referral follow-up 3d, **«Пригласить друга» нажали (ref_nudge_clicked)** с % от handshake_referral_nudge_3d.
  - Воронка без handshake: no_handshake_2h/24h/5d, опрос, ответы, response rate.
  - Причины отказа: ответы на опрос (answer_1..4).
  - Прочее: first_paid_with_prior_handshake.

- **CTR:**  
  - vpn_ok_clicked / handshake_followup_10m (только если знаменатель > 0).  
  - ref_nudge_clicked / handshake_referral_nudge_3d (аналогично).

Все перечисленные метрики считаются по **subscription_notifications**: фильтр по `sent_at` за последние `days` дней и по `notification_type = ANY(types)`; агрегат — COUNT(DISTINCT subscription_id). Идемпотентность: одна запись на (subscription_id, notification_type).

---

## 5. Recommended Integration for New Referral Notifications

### 5.1. Уведомление «реферал подключился» (одна кнопка «🤝 Пригласить друг»)

- **Текст:** например: «🔥 Есть результат! Пользователь по вашей ссылке подключил VPN 👍».
- **Кнопка:** «🤝 Пригласить друга».

Рекомендации:
- **Клавиатура:** новая константа (или фабрика) с одной кнопкой. Не использовать `ref:open_from_notify` без параметров: иначе в CRM попадёт ref_nudge_clicked по подписке реферера и смешается с остальными источниками.
- **Callback:** ввести отдельный формат для контекста «из уведомления referral_user_connected», например **ref:open_from_referral:connected:{referred_sub_id}**, где `referred_sub_id` — подписка приведённого, по которой отправили уведомление. Так можно однозначно связать клик с событием «приведённый подключился».
- **Handler:** либо расширить обработку `ref:open_from_notify` веткой по префиксу `ref:open_from_referral:connected:`, либо отдельный handler для `ref:open_from_referral:connected:`. Действие то же: показать реферальное сообщение (как в ref_open_from_notify). Дополнительно: записать **referral_user_connected_ref_clicked** в subscription_notifications с `subscription_id = referred_sub_id`, `telegram_user_id = referrer_id` (кто нажал). Проверка владельца подписки для этого callback не нужна (реферер не владелец подписки приведённого).
- **Запись «отправлено»:** при отправке уведомления рефереру уже создаётся запись с `subscription_id = referred_sub_id`, `notification_type = referral_user_connected` (как в основном аудите). Тогда в CRM можно считать: отправлено = COUNT по referral_user_connected, клик = COUNT по referral_user_connected_ref_clicked за период.

### 5.2. Уведомление «начислены баллы» (две кнопки: «🎮 Оплатить баллами», «🤝 Пригласить друг»)

- **Текст:** например: «🔥 Отличные новости! Вам начислены бонусные баллы за приглашение пользователя 👍».
- **Кнопки:** «🎮 Оплатить баллами», «🤝 Пригласить друга».

Рекомендации:
- **Клавиатура:** по составу подходит **POINTS_KEYBOARD** (две кнопки), но у неё callback без контекста. Лучше ввести фабрику с контекстом, например `_make_referral_points_awarded_keyboard(referred_sub_id)` (или иной идентификатор события), где:
  - «🎮 Оплатить баллами» → **points:open:from_referral:{referred_sub_id}** (или общий токен контекста),
  - «🤝 Пригласить друга» → **ref:open_from_referral:points:{referred_sub_id}**.
- **Запись «отправлено»:** при отправке уведомления о начислении баллов рефереру создавать запись с `subscription_id = referred_sub_id` (подписка приведённого, по которой начислили баллы), `notification_type = referral_points_awarded`. Один такой платёж = одна подписка приведённого, одна запись.
- **Клик «Пригласить друг»:** handler для `ref:open_from_referral:points:{referred_sub_id}` — показать реферальный экран и записать **referral_points_awarded_ref_clicked** (subscription_id = referred_sub_id, telegram_user_id = referrer_id).
- **Клик «Оплатить баллами»:** либо новый callback **points:open:from_referral:{referred_sub_id}**, либо общий points:open. В первом случае в handler’е points (или в ветке для этого префикса) открыть тот же экран оплаты баллами и записать **referral_points_awarded_pay_clicked** (subscription_id = referred_sub_id). Так в CRM будет отдельно видно «нажали Оплатить баллами» именно из этого уведомления.

Итог: для обоих уведомлений логично завести отдельные callback_data с контекстом (referred_sub_id или аналог) и отдельные notification_type для отправки и для кликов, чтобы не смешивать с ref_nudge_clicked и без рассинхрона с воронкой.

---

## 6. Recommended Tracking Types

Предлагаемые типы в **subscription_notifications** (или в той же таблице по текущей схеме):

| notification_type | Смысл | subscription_id | telegram_user_id |
|-------------------|--------|------------------|-------------------|
| **referral_user_connected** | Рефереру отправлено уведомление «приведённый подключился» | подписка приведённого | можно хранить реферера при необходимости |
| **referral_user_connected_ref_clicked** | Реферер нажал «Пригласить друга» в этом уведомлении | подписка приведённого | реферер |
| **referral_points_awarded** | Рефереру отправлено уведомление «начислены баллы» | подписка приведённого (по которой начислены баллы) | реферер (опционально) |
| **referral_points_awarded_ref_clicked** | Реферер нажал «Пригласить друга» в уведомлении о баллах | подписка приведённого | реферер |
| **referral_points_awarded_pay_clicked** | Реферер нажал «Оплатить баллами» в уведомлении о баллах | подписка приведённого | реферер |

Все они укладываются в текущую схему subscription_notifications (subscription_id, notification_type, telegram_user_id, sent_at). Уникальный индекс (subscription_id, notification_type) для «sent» даёт одну отправку на событие. Для «clicked» типов: если разрешить несколько кликов от одного реферера по разным подпискам приведённых — одна запись на (subscription_id, notification_type) может означать «хотя бы один клик по этому событию»; если нужен учёт нескольких кликов одного реферера по одному и тому же событию — потребуется либо снять уникальность по (subscription_id, notification_type), либо отдельная таблица кликов. Для CRM обычно достаточно «был ли клик по этому событию» — одной записи хватает.

Добавление в **get_crm_funnel_report**: расширить список `types` этими пятью типами, в SELECT добавить COUNT(DISTINCT subscription_id) FILTER по каждому, заполнить result и вывести в отчёте блок «Реферальные уведомления» (см. раздел 7).

---

## 7. Risks and Constraints

### Риск задвоить handler
- **ref:open_from_notify** уже обрабатывает и `ref:open_from_notify`, и `ref:open_from_notify:{sub_id}`. Если ввести `ref:open_from_referral:connected:...` и оставить один handler по префиксу `ref:open_from_notify`, то префикс `ref:open_from_referral` не попадёт под `F.data.startswith("ref:open_from_notify")`. Лучше явно завести префикс `ref:open_from_referral` и в одном handler’е разбирать подварианты (connected / points) и писать нужный notification_type — так не будет дублирования логики открытия реферального экрана.
- **points:open** сегодня один. Если добавить `points:open:from_referral:{referred_sub_id}`, нужна ветка в существующем handler’е (или отдельный handler с приоритетом), чтобы не обрабатывать как обычный points:open и при этом записать referral_points_awarded_pay_clicked.

### Использование кнопки с тем же callback без контекста
- Если под новыми уведомлениями поставить те же **ref:open_from_notify** и **points:open** без параметров, источник клика неизвестен: ref_nudge_clicked будет писаться по подписке реферера (fallback), и в CRM нельзя будет отделить «клик из referral_user_connected» от «клика из handshake_referral_nudge_3d» и других мест. Поэтому для новых уведомлений нужен callback с контекстом (sub_id или тип уведомления).

### Где нужен sub_id / subscription_id / context в callback_data
- **Уведомление «реферал подключился»:** нужен referred_sub_id, чтобы записать referral_user_connected_ref_clicked по правильной подписке и не проверять владельца подписки (владелец — приведённый, нажимает реферер).
- **Уведомление «начислены баллы»:** нужен идентификатор события (например referred_sub_id подписки, по которой начислили баллы), чтобы связать отправку и клики с одной и той же «историей» и считать CTR в CRM.

### Безопасное переиспользование текущего callback
- **pay:open** — можно ставить под любыми сообщениями без изменений; tracking по нему не ведётся.
- **points:open** и **ref:open_from_notify** без параметров — переиспользовать под новыми реферальными уведомлениями можно только если не нужна отдельная метрика кликов по этим уведомлениям. Для CRM по реферальным уведомлениям лучше не переиспользовать без расширения.

### Новый callback wrapper
- Имеет смысл ввести общий префикс для кнопок из реферальных уведомлений, например **ref:open_from_referral:** с суффиксами **connected:{referred_sub_id}** и **points:{referred_sub_id}**, и один handler: показ того же реферального экрана + запись соответствующего notification_type (referral_user_connected_ref_clicked / referral_points_awarded_ref_clicked). Для «Оплатить баллами» — либо **points:open:from_referral:{referred_sub_id}** в том же handler’е, что и points:open, с записью referral_points_awarded_pay_clicked.

### Рассинхрон с отправкой и кликами
- Отправка и клики считаются по одной таблице subscription_notifications и одному временному окну (sent_at за последние N дней). Чтобы не было рассинхрона:
  - Записывать «sent» сразу после успешной отправки уведомления рефереру (referral_user_connected / referral_points_awarded) с subscription_id = referred_sub_id.
  - Записывать «clicked» при нажатии кнопки в том же формате (subscription_id = referred_sub_id). Тогда знаменатель и числитель для CTR считаются по одним и тем же подпискам приведённых.
- Не смешивать в одном типе события от разных уведомлений (например не использовать ref_nudge_clicked для кликов из новых реферальных сообщений).

---

## 8. Recommended CRM Report Extension (Summary)

Добавить в отчёт блок **«Реферальные уведомления»**:

- Уведомление «пользователь подключился» отправлено: **referral_user_connected**  
  Нажали «Пригласить друга»: **referral_user_connected_ref_clicked** (и при желании % от отправленных).

- Уведомление «начислены баллы» отправлено: **referral_points_awarded**  
  Нажали «Оплатить баллами»: **referral_points_awarded_pay_clicked**  
  Нажали «Пригласить друга»: **referral_points_awarded_ref_clicked** (и при желании %).

Технически: в `get_crm_funnel_report` добавить перечисленные notification_type в список типов и в SELECT (COUNT DISTINCT subscription_id по каждому), заполнить словарь result и вывести в тексте отчёта в `cmd_crm_report`. Так все метрики остаются в одной таблице, в одном отчёте и в одном временном окне — рассинхрона не будет.

---

**Аудит завершён. Код не изменялся.**
