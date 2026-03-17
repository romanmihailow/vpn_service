# P1 Stability Improvements

**Дата:** 2026-03-16  
**Контекст:** Точечные улучшения устойчивости после стабилизации production (DB pool fix). Без изменений архитектуры, бизнес-логики, UX, WG lifecycle, billing, CRM.

---

## 1. Batch limit в auto_new_handshake_admin_notification

- Добавлена константа **HANDSHAKE_ADMIN_BATCH_SIZE = 20** (app/tg_bot_runner.py, рядом с NEW_HANDSHAKE_ADMIN_INTERVAL_SEC).
- Обработка ограничена: **for sub in with_handshake[:HANDSHAKE_ADMIN_BATCH_SIZE]**.
- SQL, логика и тексты не менялись — только ограничен размер батча за один прогон.

---

## 2. Fallback в cmd_subscription

- Финальный **message.answer(...)** обёрнут в **try/except**.
- При ошибке: **log.exception("[Subscription] Failed to send reply")** и fallback: *«Не удалось загрузить тарифы. Попробуй ещё раз через минуту или напиши в поддержку.»*
- При падении fallback-отправки: вложенный try/except с **log.exception("[Subscription] Fallback answer also failed")**.
- Текст тарифов и логика не менялись — добавлена только защита от «тишины».

---

## 3. Fallback в cmd_points

- Оба **message.answer(...)** (при пустом списке операций и при полном ответе) обёрнуты в **try/except**.
- При ошибке: **log.exception(...)** и fallback: *«Не удалось загрузить баланс. Попробуй ещё раз через минуту.»*
- При падении fallback-отправки: вложенный try/except с **log.exception("[Points] Fallback answer also failed")**.
- Логика не менялась — только защита от «тишины».

---

## 4. Изменённые файлы

| Файл | Изменения |
|------|-----------|
| **app/tg_bot_runner.py** | HANDSHAKE_ADMIN_BATCH_SIZE = 20; batch limit в auto_new_handshake_admin_notification; try/except + fallback в cmd_subscription; try/except + fallback в cmd_points (оба пути) |
| **docs/architecture/P1_STABILITY_IMPROVEMENTS.md** | Этот отчёт |

---

## 5. Что не менялось

- /start, /status, /ref, AI-support
- short confirmation (уже отключён)
- остальные jobs, DB pool, config
- SQL-запросы, тексты тарифов, логика подсчёта баллов
- WG lifecycle, billing, CRM, UX

---

- handshake admin job limited by batch size
- subscription handler protected with fallback
- points handler protected with fallback
