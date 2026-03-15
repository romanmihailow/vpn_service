# FAQ / knowledge base слой в AI-support

**Дата:** 2025-03-15  
**Цель:** добавить простой FAQ-слой, чтобы при unclear intent OpenAI опирался на реальные знания о сервисе MaxNet VPN.

---

## 1. Изменённые и созданные файлы

| Файл | Действие |
|------|----------|
| `docs/ai-support/faq.md` | **Создан** — база знаний в Markdown |
| `app/support/prompts.py` | **Изменён** — добавлены `get_faq_text()`, `USER_TEMPLATE_WITH_FAQ`, обновлён `build_user_prompt(user_message, context_summary, faq_text=None)` |
| `app/support/service.py` | **Изменён** — при вызове OpenAI для unclear передаётся FAQ: импорт `get_faq_text`, вызов `build_user_prompt(..., faq_text=get_faq_text())` |

---

## 2. Разделы FAQ (docs/ai-support/faq.md)

1. **Как подключить VPN** — шаги: WireGuard, конфиг из бота, импорт/QR, включение туннеля  
2. **Где скачать WireGuard** — ссылки App Store, Play Market, компьютер  
3. **Как получить конфиг** — триал, после оплаты, повторная отправка  
4. **VPN не подключается (нет Connected)** — проверка конфига, туннеля, интернета, срока подписки  
5. **VPN подключён, но сайты не открываются** — перезапуск туннеля/приложения, другая сеть, поддержка  
6. **Как проверить подписку** — «статус подписки» / «проверь подписку», /status  
7. **Как получить конфиг повторно** — «вышли конфиг», кнопка в боте  
8. **После оплаты не пришёл конфиг** — проверка оплаты, «вышли конфиг», обращение в поддержку  
9. **Как обратиться в поддержку** — кнопка в боте, @MaxNet_VPN_Support  
10. **Как работает тестовый доступ (trial)** — реф-ссылка, 7 дней, кнопка в боте  
11. **Как продлить подписку** — /buy, оплата, тот же конфиг  
12. **Смена телефона** — WireGuard на новом устройстве, «вышли конфиг»  
13. **Можно ли запросить настройки ещё раз** — да, «вышли конфиг» или кнопка  

Тексты короткие, в стиле поддержки, без маркетинга.

---

## 3. Загрузка FAQ

**Где:** `app/support/prompts.py`, функция `get_faq_text()`.

**Как:**  
- Путь к файлу: от `Path(__file__).resolve()` (файл `prompts.py`) поднимаемся на три уровня вверх (support → app → корень проекта), затем `docs/ai-support/faq.md`.  
- При первом вызове файл читается через `faq_path.read_text(encoding="utf-8")`, результат сохраняется в модульной переменной `_FAQ_CACHE`.  
- При последующих вызовах возвращается кэш, файл не перечитывается.  
- Если файл отсутствует, не файл, или при любой ошибке чтения/декодирования возвращается пустая строка `""`, исключения не пробрасываются.

**Фрагмент кода:**

```python
def get_faq_text() -> str:
    global _FAQ_CACHE
    if _FAQ_CACHE is not None:
        return _FAQ_CACHE
    try:
        base = Path(__file__).resolve().parent.parent.parent
        faq_path = base / "docs" / "ai-support" / "faq.md"
        if not faq_path.is_file():
            _FAQ_CACHE = ""
            return ""
        raw = faq_path.read_text(encoding="utf-8")
        _FAQ_CACHE = (raw or "").strip()
        return _FAQ_CACHE
    except Exception:
        _FAQ_CACHE = ""
        return ""
```

---

## 4. Подключение FAQ к промпту OpenAI

**Где:**  
- Формирование промпта: `app/support/prompts.py`, функция `build_user_prompt(user_message, context_summary, faq_text=None)`.  
- Вызов: `app/support/service.py`, в `_call_openai_for_phrase()`.

**Логика:**  
- Если передан непустой `faq_text`, используется шаблон `USER_TEMPLATE_WITH_FAQ`: в него подставляются FAQ (обрезанный до 4000 символов), сообщение пользователя и контекст. В инструкции указано: использовать FAQ только как справку, не выдумывать факты.  
- Если `faq_text` не передан или пустой, используется прежний `USER_TEMPLATE` (без FAQ).  
- В `_call_openai_for_phrase()` вызываются `get_faq_text()` и `build_user_prompt(user_message, summary, faq_text=faq_text)`, так что при наличии файла FAQ всегда попадает в запрос к OpenAI для unclear.

---

## 5. Что не менялось

- **Intents** (`app/support/intents.py`) — без изменений  
- **Guardrails** (`app/support/guardrails.py`) — без изменений  
- **Actions** (`app/support/actions.py`) — без изменений  
- **Router** (`app/support/router.py`) — без изменений  
- **process_support_message** — порядок и условия те же: сначала intent/guardrails/actions; только в ветке `else` (unclear) при вызове OpenAI теперь передаётся FAQ. Остальная логика не тронута.  
- **Checkpoint** (job, callbacks, регистрация pending) — без изменений  
- **FSM, payment, referral, admin, WireGuard** — не трогались  

FAQ используется только как контекст для формулировки ответа при **unclear** intent; решения по подписке, оплате, handshake и действиям по-прежнему принимаются детерминированной логикой и actions.

---

## 6. Поведение при отсутствии faq.md

- `get_faq_text()` возвращает `""`.  
- `build_user_prompt(..., faq_text="")` из-за проверки `if faq_text and faq_text.strip()` использует старый `USER_TEMPLATE` без FAQ.  
- Ответ пользователю по-прежнему формируется через OpenAI (если ключ задан) или через безопасный fallback. Ошибок и падений из-за отсутствия файла нет.

---

## 7. Итог

- FAQ лежит в `docs/ai-support/faq.md`, загружается и кэшируется в `get_faq_text()`.  
- При unclear intent в промпт OpenAI добавляется FAQ; deterministic actions и порядок обработки не менялись.  
- При отсутствии или ошибке чтения faq.md работа идёт без FAQ, без падений.
