# Причина дублирования IP-адресов

## Описание бага

При **переиспользовании (reuse)** IP и ключей для новой подписки того же пользователя возникает гонка:

1. `latest_sub = get_latest_subscription_for_telegram(user_id)` — получаем активную подписку с IP X
2. `deactivate_existing_active_subscriptions(user_id)` — деактивируем старые подписки
3. **Внутри** `deactivate_subscription_by_id()` вызывается `release_ip_in_pool(IP X)` — IP возвращается в пул (allocated=FALSE)
4. Мы используем `client_ip = reuse_ip` (строка X), **не** вызывая `allocate_free_ip_from_pool()`
5. `insert_subscription(..., vpn_ip=client_ip)` — создаём новую подписку с IP X

**Между шагами 3 и 5** другой запрос (другой пользователь, другой поток/процесс) может вызвать `allocate_free_ip_from_pool()` и получить тот же IP X, т.к. он уже в пуле как свободный.

В итоге две активные подписки разных пользователей получают один и тот же IP.

## Где происходит

Пути с reuse:
- **Оплата баллами** (`tg_bot_runner.py`, callback points payment) — строки ~2192–2245
- **Промокод на новую подписку** (`tg_bot_runner.py`, promo apply) — строки ~3250–3315
- (Проверить аналогичные пути: YooKassa, Heleket — есть ли там reuse)

## Решение

При **reuse** не освобождать IP в пуле при деактивации старой подписки:

1. Добавить в `deactivate_subscription_by_id(sub_id, event_name, release_ip_to_pool=True)` параметр `release_ip_to_pool`
2. Добавить в `deactivate_existing_active_subscriptions(telegram_user_id, reason, release_ips_to_pool=True)` параметр `release_ips_to_pool`
3. В промо- и points-путях с reuse вызывать `deactivate_existing_active_subscriptions(..., release_ips_to_pool=False)`

Или выделить отдельную функцию `deactivate_existing_active_subscriptions_for_reuse(...)`, которая не вызывает release IP.

## Дубли 391/519 (10.8.0.70)

Оба — `referral_free_trial_7d`. Реферальный триал **не** использует reuse — всегда новый IP. Источник дубля пока неясен (нужны логи/аудит).

## Защита на уровне БД (после устранения дублей)

После исправления дублей выполнить:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_vpn_subscriptions_active_ip
ON vpn_subscriptions (vpn_ip)
WHERE active = TRUE AND expires_at > NOW();
```
При попытке создать вторую активную подписку с тем же IP БД вернёт ошибку.
