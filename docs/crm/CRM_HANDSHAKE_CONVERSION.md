# CRM: конверсия оплата → подключение к VPN

Метрика в отчёте `/crm_report`: доля платящих пользователей, которые фактически подключились к VPN (первый handshake).

---

## 1. Метрики

- **payments_count** — число первых платных подписок за период.  
  Источник: существующее поле отчёта `welcome_after_first_payment` (COUNT(DISTINCT subscription_id) по типу `welcome_after_first_payment` в `subscription_notifications` за выбранные дни).

- **handshake_count** — число уникальных подписок с первым handshake за период.  
  Источник: существующее поле `handshake_user_connected` (COUNT(DISTINCT subscription_id) по типу `handshake_user_connected` в `subscription_notifications` за те же дни).

---

## 2. Формула конверсии

```
conversion = handshake_count / payments_count * 100
```

При `payments_count == 0` конверсия выводится как 0%.

---

## 3. Изменение вывода /crm_report

В начало текста отчёта добавлен блок:

**Оплаты:**
- первые платные подписки: N

**Подключения:**
- первый handshake: N

**Конверсия подключения:**
- оплата → VPN подключен: X%

Далее без изменений идут блоки «Воронка подключений», «Воронка без handshake» и прочее (в т.ч. «первые оплаты после handshake»).

---

## 4. Пример

```
CRM-отчёт за 7 дней

Оплаты:
• первые платные подписки: 111

Подключения:
• первый handshake: 48

Конверсия подключения:
• оплата → VPN подключен: 43%

Воронка подключений:
• follow-up через 10 минут: ...
...
```

---

## 5. Реализация

- **Новые таблицы в БД:** нет. Используются только уже записываемые события в `subscription_notifications` (`welcome_after_first_payment`, `handshake_user_connected`).
- **Изменённый код:** вывод команды `/crm_report` в `app/tg_bot_runner.py`: в начале отчёта считаются `payments_count` и `handshake_count` из `report`, вычисляется `conversion_pct` и выводится новый блок. Функция `db.get_crm_funnel_report()` не менялась.

Архитектура и логика записи событий не менялись.
