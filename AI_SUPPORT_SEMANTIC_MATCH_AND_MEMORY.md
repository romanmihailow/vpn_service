# AI-Support: Semantic FAQ Match and Conversation Memory

Отчёт о внедрении трёх улучшений без изменения существующей архитектуры: semantic FAQ match при unclear, short-term conversation memory, антигаллюцинация в промпте, логирование `intent_source`.

---

## 1. Modified `process_support_message()` flow

Текущий порядок шагов:

1. **Инициализация**  
   `meta["intent_source"] = "rule"`. При пустом сообщении / нет user_id → `intent_source = "fallback"`, возврат safe fallback.

2. **Классификация**  
   `result = classify_intent(text, context)`, `meta["intent"]`, `meta["confidence"]`.

3. **Conversation memory (только при unclear)**  
   Если `result.intent == "unclear"` и `result.confidence < 0.7`:  
   - запрос `get_last_support_conversation(user_id, 300)`;  
   - если последнее сообщение не старше 5 минут и `detected_intent in {vpn_not_working, connect_help, referral_info}`:  
     переопределение `result` на этот intent с `confidence=0.85`, `meta["intent_source"] = "memory"`.

4. **Human request**  
   При `human_request` — handoff, возврат.

5. **Guardrails**  
   Для не-unclear интентов с низкой уверенностью — возврат fallback (без смены `intent_source`).

6. **Детерминированные действия**  
   По `result.intent`: resend_config, missing_config_after_payment, subscription_status, handshake_status, connect_help, referral_info, vpn_not_working, smalltalk.

7. **Блок unclear**  
   - **Semantic FAQ match**: `_semantic_faq_match(text)`.  
     При совпадении: ответ по FAQ (vpn_not_working / speed_issue / multi_device), `meta["intent_source"] = "faq_match"`.  
   - При отсутствии совпадения: вызов OpenAI (при успехе `intent_source = "openai"`, при неуспехе/нет ключа `intent_source = "fallback"`).

8. **Логирование**  
   `log_support_conversation(...)` и `log.info(..., source=meta["intent_source"], ...)`.

---

## 2. Semantic FAQ matching logic

Функция `_semantic_faq_match(text)` в `app/support/service.py`:

- Нормализация: `text.strip().lower()`.
- Проверка ключевых слов в одном порядке (первое совпадение задаёт intent):

| Ключевые слова | Сопоставленный intent   | Ответ |
|----------------|-------------------------|--------|
| «сайты», «не открываются», «не грузятся» | `vpn_not_working` | `action_vpn_not_working(context)` (диагностика по контексту) |
| «скорость», «медленно», «тормозит»        | `speed_issue`     | `SPEED_ISSUE_FAQ_RESPONSE` из `messages.py` |
| «два устройства», «два телефона», «несколько устройств», «несколько телефонов», «другое устройство», «второй телефон» | `multi_device` | `MULTI_DEVICE_FAQ_RESPONSE` из `messages.py` |

- Возврат: `"vpn_not_working"` | `"speed_issue"` | `"multi_device"` | `None`.
- Вызывается только в блоке `intent == "unclear"` **до** вызова OpenAI. При совпадении OpenAI не вызывается.

---

## 3. Conversation memory reuse logic

- **Источник данных**: таблица `support_conversations`.
- **Функция**: `db.get_last_support_conversation(telegram_user_id, within_seconds=300)`  
  - Выбирает последнюю запись пользователя с `created_at >= NOW() - within_seconds` и непустым `detected_intent`.  
  - Возвращает `{"detected_intent": str, "created_at": ...}` или `None`.

- **Когда применяется**: сразу после `classify_intent`, только если:
  - `result.intent == "unclear"` и  
  - `result.confidence < 0.7`.

- **Допустимые интенты для переиспользования**: `vpn_not_working`, `connect_help`, `referral_info` (`MEMORY_REUSE_INTENTS`).

- **Действие**: подмена `result` на `IntentResult(intent=last["detected_intent"], confidence=0.85)`, обновление `meta["intent"]`, `meta["confidence"]`, `meta["intent_source"] = "memory"`. Дальше обработка идёт как для этого intent (guardrails и действия).

- **Переопределение не делается**, если уверенность классификатора ≥ 0.7 (для unclear при низкой уверенности память используется).

---

## 4. Updated SYSTEM_PROMPT

В `app/support/prompts.py` в конец правил добавлено:

- Если запрашиваемой информации **нет в FAQ** — не придумывать ответ.  
- Ответить, что точной информации нет, и предложить обратиться в поддержку.  
- Пример: «У меня нет точной информации по этому вопросу. Лучше уточнить в поддержке.»  
- Не выдумывать детали про серверы, инфраструктуру, локации или внутренние политики.

Тем самым снижается риск галлюцинаций вне пределов FAQ.

---

## 5. Example logs

Формат строки `support_ai` с полем `source`:

```
support_ai tg_id=388247897 intent=vpn_not_working conf=0.20 source=memory action=vpn_not_working fallback=False handoff=False resend=False vpn_diagnosis=handshake_stale text="на телефоне"
```

Примеры по источникам:

| source     | Ситуация |
|-----------|----------|
| `rule`    | Интент определён классификатором и обработан действием (resend, subscription_status, connect_help, vpn_not_working и т.д.) или отказ по guardrails. |
| `memory`  | Классификатор вернул unclear с низкой уверенностью; intent взят из последнего сообщения пользователя (в пределах 5 мин). |
| `faq_match` | Классификатор вернул unclear; сработал semantic FAQ match (сайты/скорость/устройства) → выдан детерминированный FAQ-ответ. |
| `openai`  | Unclear, FAQ match не сработал, ответ сформирован через OpenAI. |
| `fallback`| Пустое сообщение, нет ключа OpenAI или ошибка OpenAI → безопасный fallback. |

---

## Files changed

- `app/db.py` — добавлена `get_last_support_conversation(telegram_user_id, within_seconds=300)`.
- `app/messages.py` — добавлены `SPEED_ISSUE_FAQ_RESPONSE`, `MULTI_DEVICE_FAQ_RESPONSE`.
- `app/support/prompts.py` — расширен `SYSTEM_PROMPT` правилом про отсутствие информации в FAQ и примером фразы.
- `app/support/service.py` — память по последнему диалогу, `_semantic_faq_match`, использование faq_match перед OpenAI, поле `meta["intent_source"]` и вывод `source` в лог.

FSM, платежи, реферальная логика, checkpoint job, WireGuard, список интентов и пороги guardrails не изменялись.

---

Semantic FAQ matching and conversation memory implemented.
