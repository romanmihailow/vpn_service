# AI Support — диагностический сценарий vpn_not_working (V1)

Дата: 2025-03-12

---

## 1. Изменённые файлы

| Файл | Изменения |
|------|-----------|
| `app/support/actions.py` | `action_vpn_not_working(context)` — диагностический flow с пятью ветками, возврат `(text, markup, vpn_diagnosis)` |
| `app/support/service.py` | Вызов `action_vpn_not_working(context)`, запись `meta["vpn_diagnosis"]`, добавление `vpn_diagnosis` в лог |

**Не изменялись:** router, intents, guardrails, FSM, payment/referral flows, tg_bot_runner.py, схема БД.

---

## 2. Новый код action_vpn_not_working()

Сигнатура:

```python
def action_vpn_not_working(context: Dict[str, Any]) -> Tuple[str, Optional[InlineKeyboardMarkup], str]:
```

Возвращает: `(текст ответа, кнопка поддержки или None, метка ветки для лога)`.

Используемые поля контекста: `has_active_subscription`, `can_resend_config`, `has_handshake`, `vpn_ip`, `wg_public_key`.

### Реализация (actions.py)

```python
def action_vpn_not_working(context: Dict[str, Any]) -> Tuple[str, Optional[InlineKeyboardMarkup], str]:
    """
    Диагностический flow для intent vpn_not_working.
    Анализирует контекст и возвращает (текст, кнопка, vpn_diagnosis для лога).
    """
    has_sub = context.get("has_active_subscription")
    can_resend = context.get("can_resend_config")
    has_handshake = context.get("has_handshake")
    vpn_ip = context.get("vpn_ip")
    wg_public_key = context.get("wg_public_key")

    # Ветка 1 — нет активной подписки
    if not has_sub:
        return (
            "У тебя не найдена активная подписка. Без неё VPN не подключается. "
            "Если ты уже оплатил — лучше напиши в поддержку, они проверят.",
            _support_keyboard(),
            "no_subscription",
        )

    # Ветка 2 — подписка есть, но нет данных для конфига
    if not can_resend or not vpn_ip or not wg_public_key:
        return (
            "Настройки подключения для твоей подписки сейчас недоступны. "
            "Обратись в поддержку — они помогут.",
            _support_keyboard(),
            "no_config_data",
        )

    # Ветка 3 — подписка есть, handshake нет (туннель не установлен)
    if has_handshake is False:
        return (
            "Подключение к VPN ещё не установлено — скорее всего, туннель не включён или конфиг не добавлен.\n\n"
            "Что сделать:\n"
            "1. Открой WireGuard\n"
            "2. Проверь, что туннель добавлен (конфиг из бота)\n"
            "3. Включи туннель (переключатель в положение «вкл»)\n\n"
            "Если не получится — нажми кнопку ниже.",
            _support_keyboard(),
            "no_handshake",
        )

    # Ветка 4 — handshake есть (подключение установлено)
    if has_handshake is True:
        return (
            "VPN-подключение у тебя установлено. Значит, проблема, скорее всего, уже после подключения.\n\n"
            "Попробуй:\n"
            "1. Выключить и снова включить туннель в WireGuard\n"
            "2. Перезапустить приложение WireGuard\n"
            "3. Проверить, открываются ли сайты через другую сеть (мобильный интернет)\n\n"
            "Если не поможет — напиши в поддержку.",
            _support_keyboard(),
            "handshake_ok",
        )

    # Ветка 5 — неизвестный / неполный статус
    return (
        "Не удалось точно определить причину. Лучше напиши в поддержку — они разберутся.",
        _support_keyboard(),
        "unknown",
    )
```

---

## 3. Диагностические ветки

| Ветка | Условие | Метка лога | Ответ |
|-------|---------|------------|--------|
| 1 | `has_active_subscription == False` | `no_subscription` | Нет активной подписки, не обещаем работу VPN, кнопка поддержки |
| 2 | Подписка есть, но нет `can_resend_config` или нет `vpn_ip` / `wg_public_key` | `no_config_data` | Настройки недоступны, предложение поддержки |
| 3 | Подписка есть, `has_handshake == False` | `no_handshake` | Подключение не установлено, шаги: WireGuard → туннель добавлен → включить, кнопка поддержки |
| 4 | Подписка есть, `has_handshake == True` | `handshake_ok` | Подключение есть, шаги: переподключить туннель, перезапустить WireGuard, проверить другую сеть, кнопка поддержки |
| 5 | Остальные случаи (неполные/неясные данные) | `unknown` | Честный ответ «не удалось точно определить», кнопка поддержки |

Во всех ветках используется одна и та же кнопка: `SUPPORT_BUTTON_TEXT` → `SUPPORT_URL`.

---

## 4. Логирование

- В **service.py** при `intent == "vpn_not_working"` в `meta` пишется результат диагностики:
  - `meta["vpn_diagnosis"]` = одна из меток: `no_subscription`, `no_config_data`, `no_handshake`, `handshake_ok`, `unknown`.

- В **support_ai.log** в существующую строку `log.info(...)` добавлено поле:
  - `vpn_diagnosis=%s` — для всех сообщений; для не-vpn_not_working передаётся пустая строка.

Формат строки лога:

```
support_ai tg_id=... intent=... conf=... action=... fallback=... handoff=... resend=... vpn_diagnosis=...
```

Для intent `vpn_not_working` в `vpn_diagnosis` попадает метка ветки; по ней можно смотреть, какие сценарии срабатывают чаще.

---

## 5. Подтверждение неизменности архитектуры

- **app/support/router.py** — без изменений.
- **app/support/intents.py** — без изменений.
- **app/support/guardrails.py** — без изменений.
- **app/tg_bot_runner.py** — без изменений.
- Платежи, рефералы, админка, FSM — не трогались.
- Новых таблиц БД и сетевых проверок (ping/DNS/traceroute) нет.
- Сценарий остаётся одношаговым: один ответ по контексту, без многошагового диалога.
