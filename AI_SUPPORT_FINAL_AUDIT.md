# AI-Support Final Stabilization Audit

Финальный аудит стабилизации перед production-тестированием. Цель: проверка корректности, устранение потенциальных багов, детерминированное и безопасное поведение без изменения архитектуры.

---

## 1. Potential bugs found and fixes applied

### 1.1 Intent classification: false positive on «оплатили»

**Проблема:** Фраза «сколько рефералов оплатили» могла приводить к `missing_config_after_payment`, так как подстрока `оплатил` входит в «оплатили», и при наличии подписки бот мог предложить отправить конфиг.

**Исправление:** В `app/support/intents.py` для паттернов оплаты добавлены границы слов: `\bоплатил\b`, `\bоплатила\b`, `\bзаплатил\b`, `\bкупил\b`. Фраза «оплатили» больше не совпадает с этими паттернами.

### 1.2 Symptom classification only when handshake_state == "fresh"

**Проблема:** В `action_vpn_not_working()` классификация симптомов вызывалась при условии `handshake_state == "fresh" or has_handshake is True`, что формально допускало запуск при неявном состоянии.

**Исправление:** В `app/support/actions.py` блок с `classify_vpn_symptom()` выполняется только при `handshake_state == "fresh"`. Добавлена отдельная ветка 5b для `has_handshake is True` без `fresh` (универсальный ответ без симптома).

### 1.3 Meta for logging: vpn_diagnosis

**Проблема:** При интентах, отличных от `vpn_not_working`, в `meta` не инициализировался ключ `vpn_diagnosis`, использовался только `meta.get("vpn_diagnosis")` в логе.

**Исправление:** В `app/support/service.py` в начальный `meta` добавлено `"vpn_diagnosis": ""` для единообразия и предсказуемого формата лога.

---

## 2. Validation of intent order

Порядок в `classify_intent()` проверен и соответствует требуемому:

| # | Intent |
|---|--------|
| 1 | human_request |
| 2 | missing_config_after_payment |
| 3 | resend_config |
| 4 | vpn_not_working |
| 5 | referral_info |
| 6 | referral_stats |
| 7 | connect_help |
| 8 | subscription_status |
| 9 | handshake_status |
| 10 | smalltalk (и краткие фразы) |
| — | unclear |

Реферальные интенты (`referral_info`, `referral_stats`) идут до `connect_help`, поэтому фразы вроде «сколько друзей подключились» обрабатываются как `referral_info` (паттерн «сколько друзей»), а не как `connect_help`. Паттерн `как подключиться\b` не совпадает с «подключились».

---

## 3. Validation of action return structures

### action_vpn_not_working(context, user_message=None)

Сигнатура возврата: **(text, keyboard, vpn_diagnosis, vpn_symptom)** — 4 элемента.

**Вызовы:**

- **service.py** (два места):  
  `reply_text, reply_markup, meta["vpn_diagnosis"], meta["vpn_symptom"] = action_vpn_not_working(context, user_message=text)`  
  Распаковка на 4 значения корректна.

- **tg_bot_runner.py** (callback `config_issue_connected_no_internet`):  
  `text, reply_markup, _diagnosis, _symptom = action_vpn_not_working(context)`  
  Распаковка на 4 значения корректна.

Размер кортежа и порядок полей везде совпадают, несоответствий нет.

---

## 4. Validation of logging

В `service.py` в конце `process_support_message()` выполняется:

```python
text_for_log = (text or "").replace("\n", " ").replace("\r", " ").strip()[:300]
text_for_log = text_for_log.replace('"', '\\"')
log.info(
    "support_ai tg_id=%s intent=%s conf=%.2f source=%s action=%s fallback=%s handoff=%s resend=%s vpn_diagnosis=%s vpn_symptom=%s text=\"%s\"",
    ...
    meta.get("vpn_diagnosis") or "",
    meta.get("vpn_symptom") or "",
    text_for_log,
)
```

**Проверка:**

- В лог попадают: **intent, confidence, source, action, fallback, handoff, resend, vpn_diagnosis, vpn_symptom, text**.
- Текст: переводы строк заменены на пробел, длина ограничена 300 символами, кавычки экранированы (`"` → `\"`).

**Примечание:** Ранние выходы (пустое сообщение, `human_request`, срабатывание guardrails) возвращают управление до этого `log.info`, поэтому такие запросы не пишутся в `support_ai.log`. Запись в БД `support_conversations` тоже выполняется только при прохождении до конца пайплайна. Это текущее проектное решение, не менялось в рамках аудита.

---

## 5. Validation of admin command safety

**Команда /support_stats** (`app/tg_bot_runner.py`):

- **Доступ:** вызов обёрнут в `if not is_admin(message): return`; используется `ADMIN_TELEGRAM_ID` из настроек — выполнять может только администратор.
- **Отсутствие файла лога:** `_parse_support_ai_log_for_stats()` проверяет `if not log_path.is_file(): return dict(source_counts), dict(vpn_diagnosis_counts)` — возвращаются пустые словари, исключения нет.
- **Пустой результат БД:** `intent_rows = db.get_support_conversation_intent_stats(hours=24)` может вернуть `[]`; цикл `for intent, cnt in intent_rows[:10]` не выполняется, вывод просто пустой в блоке «Top intents» — падения нет.
- **Парсинг лога:** чтение с `encoding="utf-8", errors="replace"`; разбор по `split(" - ", 2)` и `re.search(r"source=(\S+)", msg)` / `r"vpn_diagnosis=(\S+)"`; при ошибке парсинга даты или формата строка пропускается (`continue`), общий блок в `try/except` логирует предупреждение и возвращает накопленные словари.

Команда не падает при отсутствии лога или пустой БД, парсинг лога выполняется с защитой от ошибок.

---

## 6. FAQ + OpenAI fallback

Цепочка при **unclear**:

1. В блоке `else` (intent остаётся unclear) вызывается `_semantic_faq_match(text)`.
2. При совпадении с FAQ (sites/speed/multi_device) выставляется `faq_matched_intent`, ответ формируется детерминированно, OpenAI **не вызывается**.
3. При отсутствии совпадения выполняется `_call_openai_for_phrase(text, context)` только если `OPENAI_API_KEY` задан; иначе сразу используется `get_safe_fallback()`.

OpenAI вызывается только для unclear без faq_match; для детерминированных интентов (rule, memory, faq_match) OpenAI не используется.

---

## 7. VPN diagnostic consistency

В `action_vpn_not_working()`:

- **handshake_state == "none"** или **has_handshake is False** → ветка «no_handshake», без классификации симптомов.
- **handshake_state == "stale"** → ветка «handshake_stale», без классификации симптомов.
- **handshake_state == "fresh"** → вызывается `classify_vpn_symptom(user_message)` и выбирается ответ по симптому (sites_not_loading / slow_speed / media_problem / generic_problem).
- **has_handshake is True** и state не `fresh` → ветка 5b, универсальный ответ без симптома.

Классификация симптомов выполняется **только** при `handshake_state == "fresh"`.

---

## 8. Edge cases

| Случай | Поведение |
|--------|-----------|
| Пустое сообщение | `if not text or not user_id` → `get_safe_fallback()`, `intent_source=fallback`, возврат без логирования в support_ai. |
| Очень длинное сообщение | Классификация по полному тексту; в лог пишется обрезка 300 символов. |
| Только эмодзи | Нет совпадений с паттернами → unclear → semantic FAQ match / OpenAI / fallback. |
| Только цифры | Аналогично unclear. |

Fallback и ограничение длины текста в логе обеспечивают безопасное поведение в этих сценариях.

---

## 9. Intent pattern safety (resend_config, connect_help, referral)

- **resend_config:** используются только строгие фразы с `\b` (например, `\bвышли конфиг\b`, `\bпришли конфиг\b`). Проверено: «сколько рефералов оплатили» и «как узнать сколько друзей подключились» **не** дают resend_config.
- **connect_help:** «как подключиться» с `\b` не совпадает с «подключились»; реферальные формулировки перехватываются раньше по порядку интентов.
- **referral_info / referral_stats:** паттерны заданы так, что реферальные вопросы не попадают в connect_help или resend_config.

---

## 10. Final readiness assessment

| Область | Статус |
|---------|--------|
| Intent classification safety | Исправлены границы слов для missing_config; resend/referral/connect порядок и паттерны проверены. |
| Intent order | Соответствует требуемой последовательности. |
| Action return structures | Все вызовы `action_vpn_not_working` распаковывают 4 значения корректно. |
| Logging | Формат лога соблюдён; текст ограничен и экранирован; в meta добавлен `vpn_diagnosis`. |
| Admin /support_stats | Только админ; устойчивость к отсутствию лога и пустой БД; безопасный парсинг лога. |
| FAQ + OpenAI | OpenAI не вызывается для детерминированных интентов. |
| VPN diagnostic | Симптомы учитываются только при handshake_state == "fresh". |
| Edge cases | Пустое/длинное/эмодзи/цифры обрабатываются без падений. |

Архитектура, интенты, guardrails и остальная логика не менялись; внесены только исправления для корректности и стабильности.

**Вердикт:** система AI-support готова к выводу в production-тестирование.

---

AI-support system ready for production testing.
