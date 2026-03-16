# Safety-fix: универсальные тексты UX-патча «expired subscription → paid»

**Дата:** 2025-03-12  
**Цель:** убрать узкие формулировки про «trial» из пользовательских сообщений, чтобы текст был корректен для любого сценария недавно истёкшей подписки (trial, promo, paid).

---

## 1. Какие тексты были слишком узкими

- **TRIAL_EXPIRED_PAID_NOTIFICATION_TEXT** (сообщение перед отправкой нового конфига):
  - «Старый trial-конфиг больше не работает» → сужало до trial.
  - «Удали старый trial-туннель» → сужало до trial.

- **TRIAL_EXPIRED_PAID_FOLLOWUP_NO_HANDSHAKE_TEXT** (follow-up при отсутствии handshake):
  - «старый trial-конфиг больше не работает» → сужало до trial.

Детект `has_recently_expired_subscription(telegram_user_id, within_hours=48)` срабатывает для **любой** недавно истёкшей подписки (active=FALSE, expires_at в прошлом, но не более 48 ч назад), в том числе для истёкшего платного доступа или промо. Поэтому пользовательские формулировки не должны указывать только на trial.

---

## 2. Какие новые универсальные формулировки используются

- **Основное уведомление (перед конфигом):**
  - «Старый конфиг больше не подходит» (вместо «Старый trial-конфиг больше не работает»).
  - «Удали старый туннель» (вместо «Удали старый trial-туннель»).
  - Остальной текст без изменений по смыслу; акцент на «НОВЫЙ конфиг» и шаги (WireGuard → удалить старый туннель → импортировать новый конфиг/QR).

- **Follow-up без handshake:**
  - «старый конфиг больше не работает» (вместо «старый trial-конфиг больше не работает»).
  - «Подключи новый конфиг из последних сообщений» — без изменений.

Имена констант (TRIAL_EXPIRED_PAID_*), типы уведомлений (recently_expired_trial_followup / recently_expired_trial_followup_sent), переменные (recently_expired_trial) и flow не менялись — только содержимое двух строк, отображаемых пользователю.

---

## 3. Какие файлы изменены

| Файл | Изменения |
|------|-----------|
| **app/messages.py** | Обновлены константы `TRIAL_EXPIRED_PAID_NOTIFICATION_TEXT` и `TRIAL_EXPIRED_PAID_FOLLOWUP_NO_HANDSHAKE_TEXT`: убраны упоминания «trial», использованы формулировки «старый конфиг» / «новый конфиг» / «старый туннель». Комментарии к константам приведены к формулировке «Expired subscription → paid» и пометке «универсально: trial/promo/paid». |

Другие файлы (bot.py, tg_bot_runner.py, yookassa_webhook_runner.py, db.py) не менялись: вызовы, логи, notification_type и callback_data остались прежними.

---

## 4. Подтверждение

- **Детект recently expired subscription не менялся:** `has_recently_expired_subscription(telegram_user_id, within_hours=48)` и все её вызовы без изменений.
- **Follow-up логика не менялась:** условия отправки, типы уведомлений (`recently_expired_trial_followup`, `recently_expired_trial_followup_sent`), джоба `auto_recently_expired_trial_followup`, тайминги и проверка handshake без изменений.
- **Resend / handshake / IP pool / WG lifecycle не менялись:** кнопки «Отправить настройки ещё раз» (config_check_resend), проверка handshake, пул IP и управление peer в WireGuard не затрагивались.

Expired subscription → paid UX texts made universal.
