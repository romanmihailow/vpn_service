# FAQ fallback fix — использование OpenAI + FAQ при unclear intent

**Дата:** 2025-03-15  
**Проблема:** Вопросы вроде «как работает trial», «можно ли на два телефона» есть в FAQ, но бот отдавал безопасный fallback, не вызывая OpenAI + FAQ.

**Причина:** При низкой уверенности guardrails делали ранний return с fallback до того, как управление доходило до блока с вызовом OpenAI.

---

## 1. Изменённый код

**Файл:** `app/support/service.py`, функция `process_support_message()`.

**Было (guardrails):**
```python
# Guardrails
can_handle, fallback_text = should_handle_directly(result.intent, result.confidence)
if not can_handle and fallback_text:
    meta["fallback"] = True
    ...
    return fallback_text, kb, meta  # или return fallback_text, None, meta
```

**Стало:**
```python
# Guardrails: для известных интентов с низкой уверенностью — fallback.
# Для unclear не возвращаемся здесь: идём в блок else → OpenAI + FAQ, затем fallback при ошибке.
can_handle, fallback_text = should_handle_directly(result.intent, result.confidence)
if not can_handle and fallback_text and result.intent != "unclear":
    meta["fallback"] = True
    # ... (далее те же return fallback_text + опционально kb)
```

Единственное изменение условия: добавлено **`and result.intent != "unclear"`**. При `intent == "unclear"` ранний return не выполняется, выполнение переходит к блоку Actions и далее в `else` (unclear), где вызываются OpenAI + FAQ и при ошибке/отсутствии ключа — fallback.

---

## 2. Новая логика потока

1. **human_request** → по‑прежнему сразу handoff, return.
2. **Guardrails:**  
   - Если `not can_handle and fallback_text` **и** intent **не** `"unclear"` → возвращаем fallback (уточнение или безопасный ответ).  
   - Если intent **`"unclear"`** → не возвращаемся, идём дальше в Actions.
3. **Actions:**  
   - Для известных интентов (`resend_config`, `missing_config_after_payment`, `subscription_status`, `handshake_status`, `connect_help`, `vpn_not_working`, `smalltalk`) выполняются те же deterministic actions, без изменений.
   - Для `else` (т.е. когда intent ни один из перечисленных — по факту только `"unclear"`):
     - Вызывается `_call_openai_for_phrase(text, context)` (с FAQ в промпте).
     - Если есть ответ от OpenAI → ответ + get_support_offer().
     - Если ответа нет (ошибка или нет ключа) → get_safe_fallback() + get_support_offer().
     - Кнопка поддержки добавляется как раньше.

Итого: **intent найден → action.** **Intent unclear → сначала OpenAI + FAQ, при ошибке — fallback.** Guardrails по-прежнему решают, отдавать ли fallback для **не‑unclear** интентов с низкой уверенностью.

---

## 3. Deterministic actions по-прежнему в приоритете

- Порядок не менялся: сначала human_request, потом guardrails (с новой проверкой на `unclear`), потом цепочка `if/elif` по конкретным интентам.
- `resend_config`, `missing_config_after_payment`, `subscription_status`, `handshake_status`, `connect_help`, `vpn_not_working`, `smalltalk` обрабатываются только через свои actions, как и раньше.
- OpenAI вызывается только в ветке `else`, т.е. только при `result.intent == "unclear"`. Классификатор по-прежнему возвращает `"unclear"` для фраз вроде «как работает trial», «можно ли на два телефона», поэтому теперь для них срабатывает OpenAI + FAQ вместо немедленного fallback.

---

## 4. Что не менялось

- Checkpoint job, FSM, payment, referral, actions, intents, guardrails (логика `should_handle_directly` и пороги) — без изменений.
- Меняется только одно место: при каком условии делается ранний return после guardrails (добавлено исключение для `intent == "unclear"`).
