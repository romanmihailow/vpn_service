# AI Support MVP — отчёт о реализации

Дата: 2025-03-12

---

## 1. Созданные и изменённые файлы

### Новые файлы (модуль `app/support/`)

| Файл | Назначение |
|------|------------|
| `app/support/__init__.py` | Экспорт `support_router`, `handle_support_message`, `process_support_message` |
| `app/support/models.py` | `IntentResult` — результат классификации намерения |
| `app/support/context_builder.py` | `build_user_context(telegram_user_id)` — сбор контекста пользователя (read-only) |
| `app/support/intents.py` | `classify_intent(text, context)` — rule-based классификация намерений |
| `app/support/guardrails.py` | Пороги уверенности, safe fallback, handoff к человеку |
| `app/support/actions.py` | Обработчики: resend_config, subscription_status, handshake_status, human_request, connect_help, missing_config_after_payment |
| `app/support/prompts.py` | Системный и пользовательский промпты для OpenAI |
| `app/support/service.py` | Оркестрация: context → intent → guardrails → actions, логирование |
| `app/support/router.py` | `support_router` + handler для свободного текста |

### Изменённые файлы

| Файл | Изменения |
|------|-----------|
| `app/logger.py` | Добавлены `SUPPORT_AI_LOG_FILE`, логгер `support_ai`, `get_support_ai_logger()` |
| `app/db.py` | Таблица `support_conversations`, функция `log_support_conversation()` |
| `app/tg_bot_runner.py` | Импорт `support_router`, `dp.include_router(support_router)` после main router |
| `requirements.txt` | Добавлена зависимость `openai>=1.0.0` |

---

## 2. Подключение Support Router

Support router подключён **после** основного router:

```python
dp = Dispatcher()
dp.include_router(router)
dp.include_router(support_router)  # AI Support — fallback для свободного текста
```

Поведение:

- Обрабатываются только **текстовые** сообщения (`F.text`)
- **Не** обрабатываются команды (фильтр `_is_not_command` отсекает строки, начинающиеся с `/`)
- **Не** обрабатываются callbacks (у support_router нет `CallbackQuery` handlers)
- **Не** перехватываются FSM-состояния: FSM-обработчики в main router используют `StateFilter` и срабатывают первыми

Итог: support срабатывает только на **свободный текст**, когда пользователь не в FSM и не отправил команду.

---

## 3. Определение Intent

Intent определяется в `app/support/intents.py` **rule-based** (регулярные выражения и ключевые фразы):

| Intent | Примеры триггеров |
|--------|-------------------|
| `human_request` | "оператор", "человек", "поддержк", "позовите", "передайте" |
| `resend_config` | "не пришел конфиг", "отправь конфиг", "перешли конфиг", "вышли конфиг" |
| `missing_config_after_payment` | "оплатил", "конфиг после оплаты", "не пришел после оплаты" |
| `connect_help` | "как подключить", "wireguard", "импорт", "qr" |
| `subscription_status` | "до какого числа", "срок подписки", "статус подписки" |
| `handshake_status` | "handshake", "подключился ли vpn", "vpn работает" |
| `unclear` | Всё остальное |

Результат: `IntentResult(intent, confidence, maybe_reason)`.

---

## 4. Action `resend_config`

Реализация в `app/support/actions.py` → `action_resend_config()`:

1. Проверка `context["can_resend_config"]` (есть активная подписка, `vpn_ip`, `wg_private_key`).
2. Если нет — безопасный ответ и кнопка поддержки.
3. Загрузка подписки через `db.get_latest_subscription_for_telegram()`.
4. Сборка конфига через `wg.build_client_config(client_private_key, client_ip)`.
5. Отправка через `send_vpn_config_to_user()` (существующий flow).

Новые подписки и peer **не создаются**; используется только переотправка конфига по уже существующим данным.

---

## 5. Логирование

### Файл `support_ai.log`

Логгер `support_ai` пишет в `{LOG_DIR}/support_ai.log`. В лог попадает строка:

```
support_ai tg_id=... intent=... conf=... action=... fallback=... handoff=... resend=...
```

### Таблица `support_conversations`

Поля: `telegram_user_id`, `user_message`, `ai_response`, `detected_intent`, `confidence`, `mode`, `handoff_to_human`, `created_at`.

Запись делается через `db.log_support_conversation()` в конце обработки каждого сообщения.

---

## 6. Места, которые НЕ изменялись

Следующие части проекта **не трогались**, чтобы сохранить текущую логику:

| Область | Файлы/модули |
|---------|--------------|
| Payment flow | `yookassa_webhook_runner.py`, `heleket_webhook_runner.py`, `yookassa_client.py`, `heleket_client.py`, payment callbacks в `tg_bot_runner.py` |
| Referral flow | `db.py` (referral-функции), `ref_trial_claim_callback`, `try_give_referral_trial_7d` |
| Promo flow | `PromoStates`, `promo_code_apply`, `promo_codes.py` |
| Admin flow | Admin FSM, callbacks `adm:`, `addsub:`, `admcmd:` и т.п. |
| Config resend | `config_resend_callback`, `ref_trial:claim` resend — логика не менялась |
| FSM сценарии | `PromoStates`, `DemoRequest`, `Broadcast`, `PromoAdmin`, `AdminAddSub` и др. |
| Webhook processing | YooKassa, Heleket webhooks, Tribute webhook |
| WireGuard | `wg.add_peer`, `wg.remove_peer`, `wg.get_handshake_timestamps`, `wg.build_client_config` |
| Subscription logic | `db.insert_subscription`, `db.update_subscription_*`, `deactivate_subscriptions` |
| Bot отправка | `send_vpn_config_to_user` — только вызов, без изменений |

---

## 7. Ограничения и замечания

1. **OpenAI** — опционально: если `OPENAI_API_KEY` не задан, AI Support работает без OpenAI (rule-based + fallback). Для неоднозначных запросов используется только безопасный fallback.
2. **Таблица `support_conversations`** — создаётся при `init_db()`. Миграций нет; при изменении схемы нужен отдельный миграционный скрипт.
3. **Фильтр `_is_not_command`** — синхронная функция; в aiogram 3 это допустимо.
4. **Parse mode** — ответы support отправляются с дефолтным `parse_mode=HTML` (от `DefaultBotProperties`), поэтому `HELP_INSTRUCTION` с `<a href>` отображается корректно.
5. **Контекст handshake** — `build_user_context` вызывает `wg.get_handshake_timestamps()`; при недоступности WG-интерфейса возможна ошибка, обработка — через `try/except` в `context_builder`.

---

## 8. Конфигурация

Для включения формулировки ответов через OpenAI:

```env
OPENAI_API_KEY=sk-...
```

Без ключа AI Support продолжает работать только на rule-based логике и fallback-ответах.

---

## 9. Структура модуля (итоговая)

```
app/support/
  __init__.py
  router.py        # support_router, handle_support_message
  service.py       # process_support_message
  context_builder.py
  intents.py
  actions.py
  guardrails.py
  prompts.py
  models.py
```
