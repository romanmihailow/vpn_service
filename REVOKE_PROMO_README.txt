ОТЗЫВ НЕИСПОЛЬЗОВАННЫХ ПРОМО-БАЛЛОВ (never_connected_100)

Фоновая задача auto_revoke_unused_promo_points раз в 24 часа списывает 100 баллов
у пользователей, которым 30+ дней назад начислили бонус по кампании never_connected_100
и которые так и не потратили баллы.

ПО ИСТЕЧЕНИИ ДАТЫ КАМПАНИИ СКРИПТ НАДО УДАЛИТЬ:

1. app/tg_bot_runner.py:
   - удалить вызов asyncio.create_task(auto_revoke_unused_promo_points());
   - удалить функцию auto_revoke_unused_promo_points() и константы
     REVOKE_UNUSED_PROMO_*, REVOKE_REASON, REVOKE_SOURCE.

2. app/config.py:
   - удалить DB_JOB_LOCK_REVOKE_UNUSED_PROMO.

3. app/db.py:
   - удалить функцию get_users_with_unused_promo_to_revoke().

После удаления этот файл (REVOKE_PROMO_README.txt) тоже можно удалить.
