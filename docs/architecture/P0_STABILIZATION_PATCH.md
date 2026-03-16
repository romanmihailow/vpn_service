# P0 Stabilization Patch

**Дата:** 2025-03-12  
**Контекст:** Стабилизация после инцидента connection pool exhausted и silent handlers. Три точечных P0-правки без изменения архитектуры, WG lifecycle, IP allocation, CRM, billing и UX-flow.

---

## 1. Что сделано в cmd_ref

- Вся логика после успешного `get_or_create_referral_info` (сборка ref_code, deep_link, lines, админ-блок, финальный ответ) обёрнута в **try/except**.
- При любой ошибке (get_me(), сборка данных, message.answer()):
  - вызывается **log.exception("[Referral] Failed to build or send /ref reply for tg_id=%s", telegram_user_id)**;
  - пользователю отправляется fallback: *«Не удалось загрузить реферальную информацию. Попробуй ещё раз через минуту или напиши в поддержку.»*;
  - при падении fallback-ответа пишется **log.exception("[Referral] Fallback answer also failed")**.
- Текущая логика /ref, referral mechanics, статистика и админ-блок не менялись — только добавлена обёртка и fallback.

**Файл:** `app/tg_bot_runner.py`

---

## 2. Что сделано в AI-support handler

- Entry-point: **support/router.py** — handler `handle_support_message`.
- Вызов **process_support_message(message)** и **message.answer(...)** обёрнуты в **try/except**.
- При любой ошибке:
  - вызывается **log.exception("[Support] Failed to process or send reply for chat_id=%s", message.chat.id)**;
  - пользователю отправляется fallback: *«Что-то пошло не так. Попробуй ещё раз или напиши в поддержку: @MaxNet_VPN_Support»*;
  - при падении fallback-ответа пишется **log.exception("[Support] Fallback answer also failed")**.
- Текст fallback вынесен в константу **SUPPORT_FALLBACK_TEXT**.
- AI-support intent flow, классификация, FAQ, memory, OpenAI и диагностика не менялись.

**Файл:** `app/support/router.py`

---

## 3. Batch limit в auto_handshake_followup_notifications

- Добавлена константа **HANDSHAKE_FOLLOWUP_BATCH_SIZE = 20** (в `app/tg_bot_runner.py` рядом с HANDSHAKE_FOLLOWUP_INTERVAL_SEC).
- В цикле по каждому follow-up type обрабатываются только первые 20 кандидатов за прогон:  
  **for row in candidates[:HANDSHAKE_FOLLOWUP_BATCH_SIZE]**.
- Интервалы 10m / 2h / 24h / 3d, типы уведомлений и логика follow-up не менялись — ограничено только число обрабатываемых кандидатов за один прогон джобы.

**Файл:** `app/tg_bot_runner.py`

---

## 4. Изменённые файлы

| Файл | Изменения |
|------|-----------|
| **app/tg_bot_runner.py** | cmd_ref: try/except + fallback; константа HANDSHAKE_FOLLOWUP_BATCH_SIZE = 20; цикл по candidates[:HANDSHAKE_FOLLOWUP_BATCH_SIZE] в auto_handshake_followup_notifications |
| **app/support/router.py** | try/except вокруг process_support_message и message.answer; fallback-текст; log.exception; константа SUPPORT_FALLBACK_TEXT |
| **docs/architecture/P0_STABILIZATION_PATCH.md** | Этот отчёт |

---

## 5. Что не менялось

- /start, /status
- Short confirmation flow
- Checkpoint flow, resend_config, config_check logic
- WireGuard, IP pool
- Billing, referral tracking, CRM report
- Схема БД
- Остальные handlers и jobs

---

## Итог

- **cmd_ref** защищён fallback-ответом при любой ошибке после получения referral info.
- **AI-support handler** защищён fallback-ответом при ошибке в process_support_message или отправке.
- **Handshake follow-up job** ограничен размером батча (20 кандидатов на тип за прогон) для снижения нагрузки на DB pool.
