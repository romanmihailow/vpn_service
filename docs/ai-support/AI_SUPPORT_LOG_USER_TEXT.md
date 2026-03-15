# Support analytics: логирование исходного сообщения пользователя

**Дата:** 2025-03-15  
**Цель:** добавить в `support_ai.log` поле с текстом сообщения пользователя для аналитики.

---

## Изменённый код

**Файл:** `app/support/service.py`, функция `process_support_message()`.

**Добавлено перед вызовом `log.info`:**

```python
text_for_log = (text or "").replace("\n", " ").replace("\r", " ").strip()[:300]
text_for_log = text_for_log.replace('"', '\\"')
log.info(
    "support_ai tg_id=%s intent=%s conf=%.2f action=%s fallback=%s handoff=%s resend=%s vpn_diagnosis=%s text=\"%s\"",
    user_id,
    meta["intent"],
    meta["confidence"] or 0,
    meta["action"],
    meta["fallback"],
    meta["handoff_to_human"],
    meta["resend_done"],
    meta.get("vpn_diagnosis") or "",
    text_for_log,
)
```

**Безопасность:**
- обрезка до 300 символов: `[:300]`;
- переводы строк заменяются пробелом: `.replace("\n", " ").replace("\r", " ")`;
- кавычки в тексте экранируются: `.replace('"', '\\"')`, чтобы не ломать формат лога.

---

## Пример новой строки лога

**Было:**
```
support_ai tg_id=388247897 intent=unclear conf=0.20 action=unclear fallback=True handoff=True resend=False vpn_diagnosis=
```

**Стало:**
```
support_ai tg_id=388247897 intent=unclear conf=0.20 action=unclear fallback=True handoff=True resend=False vpn_diagnosis= text="как работает trial"
```

---

## Что не менялось

- Таблица `support_conversations` и вызов `log_support_conversation` — без изменений.
- Логика AI-support, guardrails, checkpoint job, FSM, payment, referral — без изменений.
- Изменено только расширение формата строки `log.info` в `process_support_message`.
