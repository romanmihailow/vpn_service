# IP reuse / reservation architecture analysis (MaxNet VPN)

## 1. Executive summary

- **Текущая модель:** IP-адреса VPN выдаются из пула `vpn_ip_pool`, при деактивации подписки IP почти всегда сразу возвращается в пул, а WireGuard peer удаляется. Sticky/резервации IP по времени нет.
- **Поведение сейчас:** после истечения trial/подписки старый конфиг перестаёт работать (peer удалён, IP возвращён в пул). При новой оплате создаётся новая подписка с новым IP и новыми ключами, пользователю всегда отправляется новый конфиг.
- **Root cause проблем со старым конфигом:** не в алгоритме IP-выдачи как таковом, а в UX — пользователь продолжает использовать старый файл после истечения подписки и/или новой оплаты.
- **IP pool:** ~65k адресов в диапазоне `10.8.0.2`–`10.8.255.254`, из них занято ~384, активных подписок ~268 (по одной `vpn_ip` на активную подписку). Запас пула очень большой относительно текущей нагрузки.
- **Вывод по архитектурным вариантам:**
  - Текущая модель **Immediate reuse** (немедленное возвращение IP) безопасна и проста, но не помогает с UX вокруг старого конфига.
  - **Delayed reuse** (отложенное повторное использование IP) и **Sticky IP** (закреплённый IP) технически реализуемы и практически не создадут дефицита IP при текущем масштабе, но усложнят lifecycle и потребуют более тщательного контроля состояния.
  - Основной эффект для пользователя даёт не столько sticky IP, сколько **понятный UX**: явные сообщения, follow-up, resend, подсветка того, что нужно использовать новый конфиг (что уже реализовано в TRIAL_TO_PAID_CONFIG_LIFECYCLE_AUDIT и TRIAL_EXPIRED_PAYMENT_UX_PATCH).

**Рекомендация:** оставить текущую модель Immediate reuse как базовую, при этом держать «Sticky IP / Delayed reuse» как опцию для отдельных сценариев (например, для триала/возврата в течение N дней), но внедрять её только если статистика покажет, что даже с UX-патчами проблема старых конфигов остаётся значимой. Архитектурно Sticky IP и Delayed reuse реализуемы без радикального рефактора, но требуют аккуратной доработки IP-pool и cleanup job'ов.

---

## 2. Current lifecycle

### 2.1 IP lifecycle (текущая реализация)

**Основные сущности:**

- Таблица `vpn_ip_pool` (в `app/db.py`):
  - `ip INET PRIMARY KEY`
  - `allocated BOOLEAN NOT NULL DEFAULT FALSE`
  - `allocated_at TIMESTAMPTZ`
- Таблица `vpn_subscriptions` (в том же модуле): хранит `vpn_ip`, WG-ключи и статус подписки.

### Где и как выделяется IP

1. **Генерация IP для клиента**
   - Файл: `app/wg.py`
   - Функция: `generate_client_ip()`
   - Логика:
     - Берёт advisory lock на IP-пул: `db.acquire_ip_allocation_lock()`.
     - Вызывает `db.allocate_free_ip_from_pool()`.

2. **Выделение свободного IP из пула**
   - Файл: `app/db.py`
   - Функция: `allocate_free_ip_from_pool()`
   - SQL:
     ```sql
     SELECT ip
     FROM vpn_ip_pool
     WHERE allocated = FALSE
     ORDER BY ip
     LIMIT 1
     FOR UPDATE SKIP LOCKED;
     ```
   - После выбора IP выполняется `UPDATE vpn_ip_pool SET allocated = TRUE, allocated_at = NOW()`.
   - Если свободных IP нет → `RuntimeError("No free VPN IPs left in pool")`.

3. **Диапазон и размер пула**
   - По данным БД во время аудита:
     - Всего IP в `vpn_ip_pool`: **65 023**.
     - Диапазон: от `10.8.0.2/32` до `10.8.255.254/32` (фактически /16 без .0/.1/.255 для каждого /24).
     - `allocated = TRUE`: **384**.

### Где IP записывается в БД

IP попадает в `vpn_subscriptions.vpn_ip` при создании подписки через `db.insert_subscription(...)` (в `app/db.py`). Все места вызова `insert_subscription` используют IP, полученный от `wg.generate_client_ip()`:

- Trial: `try_give_referral_trial_7d` в `app/tg_bot_runner.py`.
- Оплата баллами: `points_tariff_callback` в `app/tg_bot_runner.py` (кейс «новая подписка»).
- Промокод → новая подписка: `promo_code_apply` в `app/tg_bot_runner.py`.
- YooKassa: `process_yookassa_event` в `app/yookassa_webhook_runner.py` (когда `base_sub is None`).
- Heleket: аналогичные места в `app/heleket_webhook_runner.py`.
- Админские сценарии: `AdminAddSub` в `app/tg_bot_runner.py`, сценарии в `app/main.py`.

### Когда создаётся peer в WireGuard

- Файл: `app/wg.py`
- Функция: `add_peer(public_key, allowed_ip, telegram_user_id=None)`:
  - Вызывает `ensure_wg_up()`.
  - Выполняет `wg set <iface> peer <pubkey> allowed-ips <ip>`.
  - После успешной команды дописывает peer в `/etc/wireguard/wg0.conf` через `_append_peer_to_config` с комментарием `# auto-added by vpn_service user=<tg_id>`.

Места вызова `wg.add_peer(...)` связаны с созданием/реанимацией подписки:

- Trial: `try_give_referral_trial_7d`.
- YooKassa/Heleket: при создании новой подписки.
- Оплата баллами/промо при reuse: когда переиспользуется старый IP/ключи.
- Админские сценарии (`admin_regenerate_vpn`, ручная выдача подписки).

### Когда удаляется peer

- Файл: `app/wg.py`
- Функция: `remove_peer(public_key)`:
  - Выполняет `wg set <iface> peer <pubkey> remove`.
  - Удаляет соответствующий блок из `wg0.conf`.

Основные места вызова:

- `auto_deactivate_expired_subscriptions` в `app/tg_bot_runner.py` — удаляет peer при авто-истечении подписки.
- `deactivate_existing_active_subscriptions` — очищает старые активные подписки перед выдачей нового доступа.
- Обработка refund/cancel в YooKassa/Heleket.
- Админские команды (`/admin_deactivate`, `delete_user_for_test.py` и т.п.).

### Когда IP возвращается в пул

- Файл: `app/db.py`
- Функция: `deactivate_subscription_by_id(sub_id, event_name, release_ip_to_pool: bool = True)`:
  - Помечает подписку `active = FALSE`, `last_event_name = event_name`.
  - Берёт `vpn_ip` подписки; если `release_ip_to_pool=True`, проверяет, нет ли других активных подписок с этим IP.
  - Если других активных подписок нет, вызывает `release_ip_in_pool(vpn_ip)`.

- `release_ip_in_pool(ip: str)`:
  - `UPDATE vpn_ip_pool SET allocated = FALSE, allocated_at = NULL WHERE ip = %s`.

Где вызывается `deactivate_subscription_by_id` с `release_ip_to_pool=True` (по умолчанию):

- Job `auto_deactivate_expired_subscriptions` — авто-истечение подписок.
- `/admin_deactivate` и админские утилиты.
- Обработка refunds/cancels.
- Trial/Promo/Points новые подписки — через `deactivate_existing_active_subscriptions` с `release_ips_to_pool=True`, если не происходит reuse IP/ключей.

Случаи, когда IP **не** возвращается сразу:

- `deactivate_existing_active_subscriptions(..., release_ips_to_pool=False)` — для reuse существующего IP/ключей (оплата баллами/промо поверх активной подписки).

---

## 2.2 Subscription lifecycle

В общих чертах (подробная карта есть в `TRIAL_TO_PAID_CONFIG_LIFECYCLE_AUDIT.md`):

- **Trial / Promo:** создаётся новая подписка через `insert_subscription` (referral trial, промокод), с новым IP и ключами.
- **Paid subscription:** создаётся через YooKassa/Heleket/баллы либо как продление существующей, либо как новая подписка.
- **Expiration:** job `auto_deactivate_expired_subscriptions`:
  - находит все `active=TRUE` с `expires_at <= NOW()` (`get_expired_active_subscriptions`),
  - вызывает `deactivate_subscription_by_id(sub_id, event_name="auto_expire")`,
  - удаляет peer через `wg.remove_peer(pub_key)` и тем самым делает старый конфиг нерабочим.
- **Renewal:**
  - Продление без смены конфига — когда есть активная подписка (оплаты баллами, продление через YooKassa/Heleket на существующей подписке): изменяется `expires_at`, IP/peer/ключи остаются.
  - Новая подписка (особенно после истёкшего trial или gap по времени) — новая запись, новый IP, новые ключи, новый конфиг.

Ключевые функции для статуса подписки:

- `get_latest_subscription_for_telegram(telegram_user_id)` — последняя активная неистёкшая подписка.
- `get_active_subscriptions_for_telegram(telegram_user_id)` — список всех активных неистёкших подписок (обычно <=1 после очистки).

---

## 3. Root cause проблемы старого конфига

Сценарий: **trial expired → user pays → user uses old config**.

### Почему старый конфиг не работает

- При expire job:
  - деактивирует подписку (active=FALSE),
  - удаляет peer (`wg.remove_peer`),
  - возвращает IP в пул (`release_ip_in_pool`).
- При новой оплате:
  - создаётся новая подписка (новый IP и/или новые ключи),
  - пользователь получает **новый** конфиг.
- Старый конфиг ссылается на peer и IP, которые больше не привязаны к пользователю (peer удалён, IP потенциально выдан другому).

### Что именно ломается

1. **peer удалён:** WireGuard не знает старый public key.
2. **IP уже в пуле:** старый IP больше не зарезервирован за пользователем.
3. **IP мог уйти другому:** новый peer может использовать тот же IP, но с другим ключом.

### Может ли старый конфиг заработать у другого пользователя

Нет, потому что новый peer использует **свой** public key. Даже при совпадении IP старый конфиг предъявляет другой public key и не будет принят.

### Есть ли риск конфликтов

Нет устойчивых конфликтов вида «двойной доступ»: конфликты с точки зрения пользовательского опыта (старый конфиг не коннектится) есть, но не с точки зрения безопасности/состязания за IP.

---

## 4. IP pool capacity

На момент аудита:

- `vpn_ip_pool`:
  - total: **65 023**;
  - allocated: **384**.
- `vpn_subscriptions`:
  - total: **593**;
  - active: **268**;
  - distinct active vpn_ip: **268**.

Даже если зарезервировать тысячи IP на месяцы, до реального дефицита далеко.

Грубые оценки заморозки IP при reservation на:

- 7 дней: условно +350 IP (итого < 1 000, ~1.5% пула).
- 30 дней: условно +1 500 IP (итого ~1 800, < 3% пула).
- 60 дней: условно +3 000 IP (итого ~3 300, ~5% пула).

---

## 5. Architecture options

### A) Immediate reuse (текущая модель)

- **Плюсы:** простота, безопасность, эффективное использование пула.
- **Минусы:** старый конфиг гарантированно умирает после expire; все UX-проблемы решаются только через сообщения и автопомощь.

### B) Delayed reuse

- **Идея:** IP не возвращается в общий пул для выдачи другим пользователям N дней, либо возвращается, но помечается как «не раздавать до X».
- **Плюсы:** снижает вероятность быстрого reuse IP другим пользователем; готовит базу для Sticky IP.
- **Минусы:** более сложный стейт IP; нужны дополнительные cleanup-джобы и мониторинг.

### C) Sticky IP

- **Идея:** IP закреплён за пользователем; при новой подписке (в окне N дней) система стремится использовать тот же IP; при более агрессивном варианте — даже те же ключи/peer.
- **Плюсы:** удобнее трассировать пользователя по IP; можно потенциально сделать сценарий, когда после новой оплаты «старый конфиг снова работает».
- **Минусы:** сложнее lifecycle; sticky keys повышают требования к безопасности и правильной деактивации; нужен аккуратный дизайн expire/cleanup.

---

## 6. Возможность сохранения старого конфига

Чтобы старый конфиг работал после новой оплаты, надо сохранять:

- тот же `vpn_ip`;
- тот же public key и peer в WG (или уметь его быстро реанимировать);
- корректный биллинговый статус (доступ только при active подписке).

Это требует:

- переработки `auto_deactivate_expired_subscriptions` (не удалять peer/не освобождать IP сразу для кандидатов на Sticky);
- условного `release_ip_to_pool=False` на expire;
- логики платежей, умеющей либо реанимировать существующую подписку, либо создавать новую с тем же IP/ключами.

Текущая реализация **специально** делает старый конфиг невалидным после истечения — без изменений кода сохранить поведение «старый конфиг продолжает работать» невозможно.

---

## 7. Recommendation

- **Сейчас:** оставаться на Immediate reuse, так как:
  - IP-пул далеко не на границе;
  - безопасность и предсказуемость lifecycle важнее, чем сохранение старого конфига;
  - UX вокруг trial expired → paid уже усилен (сообщения + follow-up).

- **Delayed reuse / Sticky IP:** рассматривать как опции «второй волны», если фактические метрики покажут, что даже с текущими UX-патчами пользователи массово продолжают спотыкаться о старые конфиги.

- **Если когда-нибудь внедрять:**
  - начинать с **Delayed reuse** (используя `allocated_at` или отдельную таблицу резерваций),
  - Sticky IP (и особенно Sticky keys) включать только для узких, хорошо контролируемых сценариев (например, короткое окно повторной оплаты после trial),
  - обязательно сопровождать это чётким логированием и мониторингом пулов IP.

