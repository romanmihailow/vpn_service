# Daily Summary Audit After Level-1 Filter

Точечная проверка влияния фильтра `level == 1` на daily summary для уровней 2+.

---

## 1. payments_count

**Как считается сейчас**

CTE `payments` в `get_referral_daily_summary_candidates`:

```sql
payments AS (
    SELECT l.top_ref, COUNT(DISTINCT n.subscription_id) AS cnt
    FROM level2_referrers l
    JOIN vpn_subscriptions s ON s.telegram_user_id = l.l2_user
    JOIN subscription_notifications n ON n.subscription_id = s.id
        AND n.notification_type = 'referral_points_awarded'
        AND n.sent_at >= NOW() - INTERVAL '24 hours'
    GROUP BY l.top_ref
)
```

Считается число подписок `l2_user`, по которым есть запись `referral_points_awarded` за 24 часа.

**Связь с фильтром level == 1**

`referral_points_awarded` создаётся **один раз на подписку** — при обработке уровня 1 в webhook. Подписка — это подписка **плательщика** (приведённого).

Пример: A → B → C, C платит. Создаётся `subscription_notifications(subscription_id=C, notification_type='referral_points_awarded')` при отправке уведомления B (уровень 1). Для daily summary `top_ref=A`, `l2_user=C`. Подписка C участвует в join, запись `referral_points_awarded` по этой подписке уже есть.

Итог: **payments_count не зависит от уровней 2+**. Запись создаётся при обработке уровня 1, но относится к подписке плательщика. Подписки `l2_user` по‑прежнему корректно связаны с `referral_points_awarded`.

---

## 2. points_sum

**Как считается сейчас**

CTE `points`:

```sql
points AS (
    SELECT telegram_user_id AS top_ref, COALESCE(SUM(delta), 0)::BIGINT AS pts
    FROM user_points_transactions
    WHERE reason LIKE 'ref_level_%%' AND level >= 2
      AND created_at >= NOW() - INTERVAL '24 hours'
    GROUP BY telegram_user_id
)
```

Сумма баллов из `user_points_transactions` по транзакциям уровня 2+ за 24 часа.

**Связь с фильтром**

Фильтр `level == 1` влияет только на отправку уведомлений и создание `subscription_notifications`. Начисление баллов идёт в `apply_referral_rewards_for_subscription` до цикла отправки и для всех уровней 1..5.

Итог: **points_sum считается независимо** и остаётся корректным.

---

## 3. Is daily summary still correct for 2+ levels?

**Yes.**

- **connected_count:** как и раньше — по `referral_user_connected`, который отправляется только уровню 1, но запись относится к подписке подключившегося (`l2_user`). Логика не менялась.

- **payments_count:** `referral_points_awarded` по‑прежнему создаётся один раз на подписку плательщика при обработке уровня 1. Подписка `l2_user` и связанная с ней запись учитываются в CTE `payments` как до фильтра, так и после.

- **points_sum:** считается по `user_points_transactions` с `level >= 2`, начисление баллов не затрагивается фильтром.

Условие отбора кандидатов `(connected_count > 0 OR payments_count > 0)` по‑прежнему выполняется при наличии оплат в сети 2+, так как `payments_count` остаётся заполненным.

---

## 4. Final conclusion

**SAFE**

Daily summary для уровней 2+ не нарушен:

- `payments_count` — создание `referral_points_awarded` привязано к подписке плательщика, а не к уровню реферера; подписки `l2_user` продолжают учитываться.
- `points_sum` — не зависит от уведомлений, начисление баллов для уровней 2+ без изменений.
- `connected_count` — логика как до изменений.

Фильтр `level != 1` ограничивает только отправку realtime‑уведомлений уровням 2+, но не создание `subscription_notifications` для подписок плательщиков, используемых в daily summary.
