# AI-Support: безопасное подключение /my_id (intent my_id_info)

Функциональность команды `/my_id` доступна через AI-support: при запросе своего Telegram ID естественным языком бот отвечает тем же текстом, что и по команде, без дублирования логики.

---

## 1. Существующий обработчик /my_id

**Файл:** `app/tg_bot_runner.py`

**Было:** ответ формировался напрямую в хендлере:
`f"Твой Telegram ID: <code>{message.from_user.id}</code>\n"`

**Помощника не было.** Текст вынесен в общие константы, ответ собирается через них.

---

## 2. Переиспользуемая логика ответа

**Файл:** `app/messages.py`

Добавлены константы (единый источник формулировок для команды и для AI-support):

- **MY_ID_RESPONSE_TEMPLATE** — `"Твой Telegram ID: <code>{id}</code>\n"`
- **MY_ID_UNAVAILABLE** — «Не удалось определить твой Telegram ID. Попробуй ещё раз через команду /my_id.»

**Файл:** `app/tg_bot_runner.py`

`cmd_my_id` переведён на использование этих констант:

- `uid = message.from_user.id if message.from_user else None`
- `text = MY_ID_RESPONSE_TEMPLATE.format(id=uid) if uid else MY_ID_UNAVAILABLE`
- Поведение команды не изменилось, изменился только способ формирования текста.

---

## 3. Интент my_id_info

**Файл:** `app/support/intents.py`

**Паттерны** `MY_ID_PATTERNS`:

- мой id  
- мой айди  
- какой у меня id  
- какой у меня айди  
- покажи мой id  
- покажи мой айди  
- мой telegram id  
- мой telegram айди  
- мой телеграм id  
- мой телеграм айди  
- мой идентификатор  

**Порядок в классификации:** после `handshake_status`, перед `smalltalk`:

`… → handshake_status → my_id_info → smalltalk → unclear`

Confidence для `my_id_info`: 0.85 (проходит guardrails).

---

## 4. Действие action_my_id_info

**Файл:** `app/support/actions.py`

**Сигнатура:** `action_my_id_info(user_id: int) -> Tuple[str, Optional[InlineKeyboardMarkup]]`

**Логика:**

- При переданном `user_id` возвращается тот же текст, что и в /my_id: `MY_ID_RESPONSE_TEMPLATE.format(id=user_id)`.
- При отсутствии/нуле — `MY_ID_UNAVAILABLE`.
- Клавиатура не возвращается (`None`).
- Без вызовов OpenAI, FAQ и дополнительной логики.

---

## 5. Роутинг в process_support_message

**Файл:** `app/support/service.py`

Добавлена ветка:

```python
elif result.intent == "my_id_info":
    meta["action"] = "my_id_info"
    reply_text, reply_markup = action_my_id_info(user_id)
```

`user_id` берётся из `message.from_user.id` в начале обработки. Дальше используется общий блок логирования и возврат ответа.

---

## 6. Логирование

Стандартное логирование support_ai применяется к `my_id_info`:

- В БД (`log_support_conversation`): `detected_intent=my_id_info`.
- В лог-файл support_ai: `intent=my_id_info`, `action=my_id_info` и остальные поля (source=rule, fallback=False, handoff=False и т.д.).

Дополнительных изменений в логировании не требуется.

---

## 7. Безопасность и неизменность остальных частей

- **Команда /my_id** — только рефакторинг на константы из `messages.py`, видимое пользователю поведение то же.
- **Guardrails, semantic FAQ, conversation memory, vpn diagnostics** — не менялись.
- **Платежи, рефералы, checkpoint, FSM, выдача конфигов** — не затрагивались.
- Добавлен только один интент и одна ветка в сервисе, переиспользуется существующая логика ответа.

---

## Изменённые файлы

| Файл | Изменения |
|------|-----------|
| `app/messages.py` | Константы MY_ID_RESPONSE_TEMPLATE, MY_ID_UNAVAILABLE. |
| `app/tg_bot_runner.py` | Импорт констант; cmd_my_id формирует ответ через них. |
| `app/support/intents.py` | MY_ID_PATTERNS, проверка my_id_info после handshake_status. |
| `app/support/actions.py` | action_my_id_info(user_id). |
| `app/support/service.py` | Импорт action_my_id_info, ветка для intent my_id_info. |

---

My ID safely integrated into AI-support.
