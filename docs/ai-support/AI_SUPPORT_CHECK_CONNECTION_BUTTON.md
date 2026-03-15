# Кнопка быстрой самопроверки подключения после конфига

**Дата:** 2025-03-15  
**Цель:** добавить кнопку «🔍 Проверить подключение» в последнее сообщение после выдачи VPN-конфига, чтобы пользователь мог сразу проверить наличие handshake.

---

## 1. Изменённые файлы

| Файл | Изменения |
|------|-----------|
| `app/messages.py` | Константы: `CONFIG_CHECK_NOW_BUTTON_TEXT`, `CONFIG_CHECK_NOW_OK`, `CONFIG_CHECK_NOW_FAIL`, `CONFIG_CHECK_NOW_UNKNOWN` |
| `app/bot.py` | Импорт `CONFIG_CHECK_NOW_BUTTON_TEXT`. Перед отправкой инструкции запрашивается подписка; клавиатура: при наличии sub_id — две кнопки (Проверить подключение, Нужна помощь), иначе одна (Нужна помощь). Переиспользуется `sub` для `schedule_checkpoint`. |
| `app/tg_bot_runner.py` | Импорт `CONFIG_CHECK_NOW_OK`, `CONFIG_CHECK_NOW_FAIL`, `CONFIG_CHECK_NOW_UNKNOWN`. Обработчик `config_check_now_callback` для `config_check_now:{sub_id}`. |

---

## 2. Клавиатура после конфига

После выдачи конфига (файл + QR + инструкция) в последнем сообщении отображаются кнопки:

- **Первая строка:** «🔍 Проверить подключение» (callback_data: `config_check_now:{subscription_id}`) — показывается только если у пользователя найдена активная подписка с `id`.
- **Вторая строка:** «🧑‍💻 Нужна помощь» (URL на поддержку).

Если подписку получить не удалось (или нет `id`), остаётся одна кнопка: «🧑‍💻 Нужна помощь».

---

## 3. Callback data и handler

- **Callback data:** `config_check_now:{sub_id}` (например, `config_check_now:12345`).
- **Handler:** `config_check_now_callback` в `app/tg_bot_runner.py`, регистрируется по `F.data.startswith("config_check_now:")`.

**Логика:**
1. Парсинг `sub_id` из `callback.data`.
2. Загрузка подписки `db.get_subscription_by_id(sub_id)`; проверка, что `sub.telegram_user_id == callback.from_user.id`.
3. Чтение `wg_public_key` из подписки.
4. Вызов `wg.get_handshake_timestamps()` (без изменений peer/подписки).
5. Три ветки ответа (см. ниже).

---

## 4. Определение handshake

Используется существующая логика:

- `wg.get_handshake_timestamps()` возвращает словарь `public_key -> unix timestamp` последнего handshake.
- Для ключа подписки берётся `handshakes.get(pub_key, 0)`.
- **ts > 0** → handshake есть → ответ «VPN уже подключён ✅».
- **ts == 0** → handshake нет → ответ «VPN пока не подключён» + кнопка поддержки.
- Нет `wg_public_key` или ошибка при вызове `get_handshake_timestamps()` → ответ «Не удалось точно проверить подключение».

Подписка и peer не создаются и не меняются.

---

## 5. Логирование

В handler пишутся строки:

- `[ConfigCheckNow] tg_id=... sub_id=... result=ok` — handshake есть.
- `[ConfigCheckNow] tg_id=... sub_id=... result=no_handshake` — handshake нет.
- `[ConfigCheckNow] tg_id=... sub_id=... result=unknown (no wg_public_key)` — нет ключа.
- `[ConfigCheckNow] tg_id=... sub_id=... handshake check failed: ...` — исключение при проверке.

Отдельная БД-таблица не вводилась.

---

## 6. Подтверждение: остальная логика не затронута

- **Checkpoint job** — без изменений; по-прежнему регистрируется `config_checkpoint_pending` и выполняется `auto_config_checkpoint`.
- **AI-support** — intents, guardrails, service, actions не менялись.
- **FSM, payment, referral** — не менялись.
- **send_vpn_config_to_user** — добавлено только формирование другой клавиатуры и повторное использование уже получаемой подписки для checkpoint; вызовы и порядок отправки сообщений те же.

Кнопка и handler — отдельная UX-функция самопроверки, без изменения существующей бизнес-логики.
