# AI Support MVP — технический отчёт для архитектурного ревью

**Проект:** MaxNet VPN  
**Дата:** 2025-03-12  
**Цель:** собрать все ключевые части реализации AI-support для проведения архитектурного ревью внешним архитектором.

---

## 1. Краткое описание архитектуры AI-support

AI-support реализован как **отдельный модуль** `app/support/`, который:

1. Обрабатывает только **свободные текстовые сообщения** (не команды, не callbacks).
2. Подключается **последним** в цепочку роутеров и работает как **fallback**.
3. Цепочка обработки: **context** → **intent** → **guardrails** → **actions**.
4. Использует **существующие функции** проекта (`db`, `wg`, `bot.send_vpn_config_to_user`) — не создаёт дублирующую логику.
5. OpenAI используется **опционально** и только для формулировки ответа при unclear intent.
6. Логирование: файл `support_ai.log` и таблица `support_conversations`.

**Точка вмешательства AI:** сообщение передаётся в AI-support только если:
- это текстовое сообщение (`F.text`);
- текст не начинается с `/` (не команда);
- ни один из обработчиков main router не совпал (в т.ч. FSM-обработчики имеют приоритет за счёт StateFilter).

---

## 2. Новые файлы

| Путь | Назначение |
|------|------------|
| `app/support/__init__.py` | Экспорт модуля |
| `app/support/models.py` | `IntentResult` |
| `app/support/context_builder.py` | `build_user_context()` |
| `app/support/intents.py` | `classify_intent()` |
| `app/support/guardrails.py` | Пороги, fallback, handoff |
| `app/support/actions.py` | Обработчики действий |
| `app/support/prompts.py` | Промпты OpenAI |
| `app/support/service.py` | Оркестрация |
| `app/support/router.py` | Support router и handler |

---

## 3. Изменённые файлы

| Путь | Изменения |
|------|-----------|
| `app/logger.py` | Логгер `support_ai`, `SUPPORT_AI_LOG_FILE`, `get_support_ai_logger()` |
| `app/db.py` | Таблица `support_conversations`, функция `log_support_conversation()` |
| `app/tg_bot_runner.py` | Импорт `support_router`, `dp.include_router(support_router)` |
| `requirements.txt` | Зависимость `openai>=1.0.0` |

---

## 4. Support Router

### app/support/router.py

```python
"""
Router для AI Support.
Обрабатывает только свободные текстовые сообщения (не команды, не callbacks, не FSM).
Должен подключаться ПОСЛЕДНИМ, чтобы срабатывать как fallback.
"""
from aiogram import F, Router
from aiogram.types import Message

from .service import process_support_message

support_router = Router(name="support")


def _is_not_command(message: Message) -> bool:
    """Фильтр: текст есть и не начинается с /"""
    t = (message.text or "").strip()
    return bool(t) and not t.startswith("/")


@support_router.message(F.text, _is_not_command)
async def handle_support_message(message: Message) -> None:
    """
    Handler для свободного текста.
    Срабатывает только на обычные сообщения (не команды).
    FSM-обработчики в main router имеют приоритет за счёт StateFilter.
    """
    reply_text, reply_markup, _meta = await process_support_message(message)
    await message.answer(
        reply_text,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
```

### Объяснение

- **Подключение:** router подключается в `tg_bot_runner.py` через `dp.include_router(support_router)` после main router (см. раздел 8).
- **Фильтры:** `F.text` — только текстовые сообщения; `_is_not_command` — текст не начинается с `/`.
- **Исключение команд:** `_is_not_command` возвращает `False`, если `t.startswith("/")`; такие сообщения не обрабатываются.
- **Исключение FSM:** FSM-обработчики в main router используют `StateFilter(SomeState)` и срабатывают первыми; support router подключён последним и обрабатывает только то, что не было обработано.
- **Когда сообщение идёт в AI-support:** когда это текстовое сообщение, не команда, и ни один handler main router не совпал (в т.ч. пользователь не в FSM).

---

## 5. Intent Classifier

### app/support/intents.py

```python
"""
Классификация намерений пользователя (rule-based MVP).
"""
import re
from typing import Dict, Any

from .models import IntentResult


HUMAN_PATTERNS = [
    r"оператор", r"человек", r"поддержк", r"позовите", r"передайте",
    r"связаться", r"позвонить", r"с человеком", r"живой", r"консультант",
]
RESEND_PATTERNS = [
    r"не пришел", r"не пришёл", r"не пришел конфиг", r"конфиг не пришел",
    r"отправь конфиг", r"перешли конфиг", r"повторно отправь", r"вышли конфиг",
]
MISSING_CONFIG_PATTERNS = [
    r"оплатил", r"оплатила", r"заплатил", r"купил", r"конфиг после оплаты",
    r"не пришел после оплаты", r"после оплаты не пришел",
]
CONNECT_HELP_PATTERNS = [
    r"как подключить", r"как установить", r"wireguard", r"настроить",
    r"импорт", r"qr", r"подключиться", r"не работает подключение",
]
STATUS_PATTERNS = [
    r"до какого", r"до какой даты", r"когда истекает", r"срок подписки",
    r"статус подписки", r"активна ли подписка", r"подписка активна",
]
HANDSHAKE_PATTERNS = [
    r"handshake", r"подключился ли", r"подключилась ли", r"есть ли подключение",
    r"vpn работает", r"работает ли vpn", r"соединение установлено",
]


def _match_patterns(text: str, patterns: list) -> bool:
    t = (text or "").lower().strip()
    return any(re.search(p, t, re.I) for p in patterns)


def classify_intent(text: str, context: Dict[str, Any]) -> IntentResult:
    """
    Rule-based классификация намерения.
    """
    t = (text or "").strip()
    if not t or len(t) < 2:
        return IntentResult(intent="unclear", confidence=0.0, maybe_reason="empty")

    if _match_patterns(t, HUMAN_PATTERNS):
        return IntentResult(intent="human_request", confidence=0.95)

    if _match_patterns(t, RESEND_PATTERNS):
        if context.get("has_active_subscription"):
            return IntentResult(intent="resend_config", confidence=0.9)
        return IntentResult(intent="missing_config_after_payment", confidence=0.7)

    if _match_patterns(t, MISSING_CONFIG_PATTERNS):
        if context.get("has_active_subscription"):
            return IntentResult(intent="resend_config", confidence=0.85)
        return IntentResult(intent="missing_config_after_payment", confidence=0.8)

    if _match_patterns(t, CONNECT_HELP_PATTERNS):
        return IntentResult(intent="connect_help", confidence=0.85)

    if _match_patterns(t, STATUS_PATTERNS):
        return IntentResult(intent="subscription_status", confidence=0.85)

    if _match_patterns(t, HANDSHAKE_PATTERNS):
        return IntentResult(intent="handshake_status", confidence=0.8)

    # Краткие фразы
    short = t.lower()
    if short in ("конфиг", "конфиг пожалуйста", "вышли конфиг", "отправь конфиг"):
        if context.get("has_active_subscription"):
            return IntentResult(intent="resend_config", confidence=0.9)
        return IntentResult(intent="unclear", confidence=0.3)

    if short in ("подписка", "статус", "до когда"):
        return IntentResult(intent="subscription_status", confidence=0.7)

    return IntentResult(intent="unclear", confidence=0.2)
```

### Объяснение

- **Список intents:** `human_request`, `resend_config`, `missing_config_after_payment`, `connect_help`, `subscription_status`, `handshake_status`, `unclear`.
- **Правила классификации:** проверка по порядку; первый совпавший набор правил определяет intent.
- **Регулярные выражения:** используются в `re.search(p, t, re.I)` через `_match_patterns`.
- **Confidence logic:** фиксированные значения 0.95, 0.9, 0.85, 0.8, 0.7, 0.3, 0.2, 0.0 в зависимости от паттерна и контекста.

---

## 6. Guardrails

### app/support/guardrails.py

```python
"""
Guardrails: анти-галлюцинации, безопасные fallback-ответы.
"""
from typing import Tuple, Optional

# Пороги уверенности
CONF_HIGH = 0.8
CONF_MED = 0.5
CONF_LOW = 0.3


def get_safe_fallback() -> str:
    """Безопасный ответ при низкой уверенности."""
    return (
        "Я не до конца понял, что именно произошло.\n\n"
        "Подскажи, что ближе: не пришёл конфиг / не получается импортировать / "
        "VPN не работает / нужен оператор."
    )


def get_clarification_prompt() -> str:
    """Уточняющий вопрос."""
    return (
        "Можешь уточнить? Например:\n"
        "• конфиг не пришёл после оплаты\n"
        "• не получается импортировать в WireGuard\n"
        "• VPN подключается, но интернет не работает\n"
        "• нужна помощь оператора"
    )


def get_support_offer() -> str:
    """Предложение обратиться в поддержку."""
    return "Если удобнее, можно сразу обратиться в поддержку — нажми кнопку ниже."


def should_handle_directly(intent: str, confidence: float) -> Tuple[bool, Optional[str]]:
    """
    Решает: обрабатывать intent напрямую или давать fallback.
    Возвращает (можно_обработать, fallback_текст или None).
    """
    if intent == "human_request":
        return False, None  # специальная обработка — human handoff

    if confidence >= CONF_HIGH:
        return True, None

    if confidence >= CONF_MED:
        # Можно задать уточняющий вопрос
        return False, get_clarification_prompt()

    if confidence >= CONF_LOW:
        return False, get_safe_fallback()

    return False, get_safe_fallback()


def should_handoff_to_human(intent: str, confidence: float) -> bool:
    """Нужно ли предлагать передачу оператору."""
    if intent == "human_request":
        return True
    if confidence < CONF_LOW and intent == "unclear":
        return True
    return False


def is_out_of_scope(intent: str) -> bool:
    """Вопрос вне зоны знаний VPN-поддержки."""
    if intent == "unclear":
        return True
    return False
```

### Объяснение

- **Пороги confidence:** `CONF_HIGH=0.8`, `CONF_MED=0.5`, `CONF_LOW=0.3`.
- **Fallback logic:** при `confidence >= CONF_HIGH` — обрабатываем; при `CONF_MED` — уточняющий вопрос; при `CONF_LOW` и ниже — безопасный fallback.
- **Anti-hallucination:** при низкой уверенности не выполняем action; выдаём предопределённые тексты `get_safe_fallback()` или `get_clarification_prompt()`; `human_request` всегда ведёт к handoff, без ответа по существу.

---

## 7. Actions

### app/support/actions.py

```python
"""
Обработчики действий AI Support.
Переиспользуют существующую логику: send_vpn_config_to_user, wg, db.
"""
from typing import Any, Dict, Optional, Tuple

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import db, wg
from ..bot import send_vpn_config_to_user
from ..messages import (
    CONNECTION_INSTRUCTION_SHORT,
    HELP_INSTRUCTION,
    SUPPORT_BUTTON_TEXT,
    SUPPORT_URL,
)


def _support_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=SUPPORT_BUTTON_TEXT, url=SUPPORT_URL)],
        ]
    )


async def action_resend_config(
    telegram_user_id: int, context: Dict[str, Any]
) -> Tuple[str, bool, Optional[InlineKeyboardMarkup]]:
    """
    Переотправка конфига.
    Возвращает (текст_ответа, успех, reply_markup).
    """
    if not context.get("can_resend_config"):
        return (
            "У тебя нет активной подписки или конфиг пока не создан. "
            "Если ты оплатил подписку — лучше обратиться в поддержку, они проверят.",
            False,
            _support_keyboard(),
        )

    sub = db.get_latest_subscription_for_telegram(telegram_user_id)
    if not sub:
        return (
            "Не нашёл активную подписку. Если оплата прошла — напиши в поддержку.",
            False,
            _support_keyboard(),
        )

    private_key = sub.get("wg_private_key")
    vpn_ip = sub.get("vpn_ip")
    if not private_key or not vpn_ip:
        return (
            "Конфиг для твоей подписки недоступен. Обратись в поддержку.",
            False,
            _support_keyboard(),
        )

    config_text = wg.build_client_config(
        client_private_key=private_key,
        client_ip=vpn_ip,
    )
    await send_vpn_config_to_user(
        telegram_user_id=telegram_user_id,
        config_text=config_text,
        caption="Повторная отправка конфига MaxNet VPN. Файл vpn.conf — в этом сообщении. QR-код — в следующем.",
    )
    return (
        "Конфиг отправлен. Проверь сообщения выше.",
        True,
        None,
    )


def action_subscription_status(context: Dict[str, Any]) -> str:
    """Показать статус подписки."""
    if not context.get("has_active_subscription"):
        return "У тебя нет активной подписки."

    exp = context.get("expires_at")
    sub_type = context.get("subscription_type", "unknown")
    type_label = {
        "trial": "пробный доступ",
        "promo": "промокод",
        "paid": "оплаченная",
        "other": "подписка",
    }.get(sub_type, "подписка")

    if exp:
        from datetime import datetime
        try:
            if hasattr(exp, "strftime"):
                date_str = exp.strftime("%d.%m.%Y")
            else:
                date_str = str(exp)[:10]
        except Exception:
            date_str = str(exp)[:10]
        return f"Подписка активна ({type_label}). Действует до {date_str}."
    return f"Подписка активна ({type_label})."


def action_handshake_status(context: Dict[str, Any]) -> str:
    """Показать статус handshake."""
    if not context.get("has_active_subscription"):
        return "У тебя нет активной подписки — проверять подключение нечего."

    pub_key = context.get("wg_public_key")
    if not pub_key:
        return "Статус подключения пока неизвестен."

    try:
        handshakes = wg.get_handshake_timestamps()
        ts = handshakes.get((pub_key or "").strip(), 0)
    except Exception:
        return "Не удалось проверить статус подключения. Попробуй ещё раз или напиши в поддержку."

    if ts > 0:
        return "Подключение установлено — VPN работает."
    return "Подключение ещё не установлено. Импортируй конфиг в WireGuard и включи туннель."


def action_human_request() -> Tuple[str, InlineKeyboardMarkup]:
    """Передать оператору."""
    return (
        "Сейчас подключу тебя к оператору. Нажми кнопку ниже — откроется чат поддержки.",
        _support_keyboard(),
    )


def action_connect_help() -> str:
    """Инструкция по подключению."""
    return HELP_INSTRUCTION


def action_missing_config_after_payment(context: Dict[str, Any]) -> Tuple[str, bool, Optional[InlineKeyboardMarkup]]:
    """
    Конфиг не пришёл после оплаты.
    Возвращает (текст, нужно_выполнить_resend, markup).
    """
    if context.get("has_active_subscription") and context.get("can_resend_config"):
        return (
            "Сейчас отправлю конфиг ещё раз.",
            True,  # service вызовет action_resend_config
            None,
        )
    return (
        "Я не нашёл активную подписку по твоему аккаунту. Лучше передам вопрос в поддержку — они проверят оплату.",
        False,
        _support_keyboard(),
    )
```

### Использование существующих функций проекта

| Action | Используемые функции |
|--------|----------------------|
| `action_resend_config` | `db.get_latest_subscription_for_telegram`, `wg.build_client_config`, `send_vpn_config_to_user` |
| `action_subscription_status` | Контекст от `build_user_context` (из `db.get_latest_subscription_for_telegram`) |
| `action_handshake_status` | `wg.get_handshake_timestamps`, контекст с `wg_public_key` из подписки |
| `action_human_request` | `SUPPORT_BUTTON_TEXT`, `SUPPORT_URL` из `messages` |
| `action_connect_help` | `HELP_INSTRUCTION` из `messages` |
| `action_missing_config_after_payment` | Контекст; при `do_resend=True` вызывается `action_resend_config` |

Новая логика выдачи конфигов не создаётся; используется существующий `send_vpn_config_to_user`.

---

## 8. AI Service

### app/support/service.py

```python
"""
AI Support: оркестрация обработки сообщения.
Использует context, intents, guardrails, actions. Опционально — OpenAI для формулировки.
"""
import os
from typing import Any, Dict, Optional, Tuple

from aiogram.types import InlineKeyboardMarkup, Message

from .. import db
from ..logger import get_support_ai_logger

from .context_builder import build_user_context
from .intents import classify_intent
from .guardrails import (
    get_safe_fallback,
    get_support_offer,
    should_handle_directly,
    should_handoff_to_human,
)
from .actions import (
    action_resend_config,
    action_subscription_status,
    action_handshake_status,
    action_human_request,
    action_connect_help,
    action_missing_config_after_payment,
)

log = get_support_ai_logger()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
_openai_client = None


def _get_openai_client():
    global _openai_client
    if _openai_client is None and OPENAI_API_KEY:
        try:
            from openai import OpenAI
            _openai_client = OpenAI(api_key=OPENAI_API_KEY)
        except Exception:
            _openai_client = False
    return _openai_client if _openai_client else None


def _format_context_summary(ctx: Dict[str, Any]) -> str:
    parts = []
    if ctx.get("has_active_subscription"):
        parts.append("активная подписка")
        if ctx.get("expires_at"):
            parts.append(f"до {ctx.get('expires_at')}")
        parts.append(f"тип: {ctx.get('subscription_type', 'unknown')}")
    else:
        parts.append("нет активной подписки")
    if ctx.get("can_resend_config"):
        parts.append("можно resend config")
    if ctx.get("has_handshake"):
        parts.append("handshake есть")
    return "; ".join(parts)


async def _call_openai_for_phrase(user_message: str, context: Dict[str, Any]) -> Optional[str]:
    """Опционально: сформулировать ответ через OpenAI."""
    client = _get_openai_client()
    if not client:
        return None
    try:
        from .prompts import SYSTEM_PROMPT, build_user_prompt
        summary = _format_context_summary(context)
        user_prompt = build_user_prompt(user_message, summary)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=300,
            temperature=0.3,
        )
        if resp.choices:
            return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("OpenAI phrase failed: %r", e)
    return None


async def process_support_message(message: Message) -> Tuple[str, Optional[InlineKeyboardMarkup], Dict[str, Any]]:
    """
    Обрабатывает support-сообщение.
    Возвращает (текст_ответа, reply_markup, meta для логирования).
    """
    text = (message.text or "").strip()
    user_id = message.from_user.id if message.from_user else 0
    meta: Dict[str, Any] = {
        "intent": None,
        "confidence": None,
        "action": None,
        "fallback": False,
        "handoff_to_human": False,
        "resend_done": False,
    }

    if not text or not user_id:
        return get_safe_fallback(), None, meta

    context = build_user_context(user_id)
    result = classify_intent(text, context)
    meta["intent"] = result.intent
    meta["confidence"] = result.confidence

    # Human request — сразу handoff
    if result.intent == "human_request":
        meta["handoff_to_human"] = True
        txt, km = action_human_request()
        return txt, km, meta

    # Guardrails
    can_handle, fallback_text = should_handle_directly(result.intent, result.confidence)
    if not can_handle and fallback_text:
        meta["fallback"] = True
        if should_handoff_to_human(result.intent, result.confidence):
            meta["handoff_to_human"] = True
            fallback_text += "\n\n" + get_support_offer()
            kb = None
            try:
                from ..messages import SUPPORT_URL, SUPPORT_BUTTON_TEXT
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=SUPPORT_BUTTON_TEXT, url=SUPPORT_URL)],
                ])
            except Exception:
                pass
            return fallback_text, kb, meta
        return fallback_text, None, meta

    # Actions
    reply_text = ""
    reply_markup = None

    if result.intent == "resend_config":
        meta["action"] = "resend_config"
        reply_text, ok, reply_markup = await action_resend_config(user_id, context)
        meta["resend_done"] = ok

    elif result.intent == "missing_config_after_payment":
        meta["action"] = "missing_config_after_payment"
        txt, do_resend, km = action_missing_config_after_payment(context)
        if do_resend:
            reply_text, ok, reply_markup = await action_resend_config(user_id, context)
            meta["resend_done"] = ok
            if ok:
                reply_text = "Конфиг отправлен. Проверь сообщения выше."
        else:
            reply_text = txt
            reply_markup = km
            meta["handoff_to_human"] = True

    elif result.intent == "subscription_status":
        meta["action"] = "subscription_status"
        reply_text = action_subscription_status(context)

    elif result.intent == "handshake_status":
        meta["action"] = "handshake_status"
        reply_text = action_handshake_status(context)

    elif result.intent == "connect_help":
        meta["action"] = "connect_help"
        reply_text = action_connect_help()

    else:
        meta["action"] = "unclear"
        meta["fallback"] = True
        meta["handoff_to_human"] = True
        if OPENAI_API_KEY:
            ai_text = await _call_openai_for_phrase(text, context)
            if ai_text:
                reply_text = ai_text + "\n\n" + get_support_offer()
            else:
                reply_text = get_safe_fallback() + "\n\n" + get_support_offer()
        else:
            reply_text = get_safe_fallback() + "\n\n" + get_support_offer()

        try:
            from ..messages import SUPPORT_URL, SUPPORT_BUTTON_TEXT
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
            reply_markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=SUPPORT_BUTTON_TEXT, url=SUPPORT_URL)],
            ])
        except Exception:
            pass

    # Логирование
    try:
        db.log_support_conversation(
            telegram_user_id=user_id,
            user_message=text,
            ai_response=reply_text[:500] if reply_text else None,
            detected_intent=meta["intent"],
            confidence=meta["confidence"],
            mode="ai",
            handoff_to_human=meta["handoff_to_human"],
        )
    except Exception as e:
        log.warning("Failed to log support conversation: %r", e)

    log.info(
        "support_ai tg_id=%s intent=%s conf=%.2f action=%s fallback=%s handoff=%s resend=%s",
        user_id,
        meta["intent"],
        meta["confidence"] or 0,
        meta["action"],
        meta["fallback"],
        meta["handoff_to_human"],
        meta["resend_done"],
    )

    return reply_text, reply_markup, meta
```

### Объяснение

- **Оркестрация:** `build_user_context` → `classify_intent` → для `human_request` сразу handoff → иначе `should_handle_directly` (guardrails) → при `can_handle` выполняются actions.
- **OpenAI:** вызывается только при `intent == "unclear"` через `_call_openai_for_phrase`; используется `gpt-4o-mini`, промпты из `prompts.py`.
- **Без OPENAI_API_KEY:** при unclear — всегда `get_safe_fallback() + get_support_offer()` и кнопка поддержки.
- **Guardrails:** `should_handle_directly` — если не обрабатываем, возвращаем `fallback_text`; `should_handoff_to_human` — добавляем кнопку поддержки.

---

## 9. Context Builder

### app/support/context_builder.py

```python
"""
Сборщик контекста пользователя для AI Support.
Read-only, использует существующие функции проекта.
"""
from typing import Any, Dict

from .. import db
from .. import wg


def build_user_context(telegram_user_id: int) -> Dict[str, Any]:
    """
    Собирает единый контекст пользователя для AI.
    Возвращает dict с данными подписки, handshake, баллов и т.д.
    Не бросает исключений — при ошибках возвращает None/False/unknown.
    """
    ctx: Dict[str, Any] = {
        "telegram_user_id": telegram_user_id,
        "username": None,
        "has_active_subscription": False,
        "subscription_id": None,
        "expires_at": None,
        "subscription_type": "none",
        "last_event_name": None,
        "points_balance": 0,
        "has_referrer": False,
        "has_handshake": False,
        "vpn_ip": None,
        "wg_public_key": None,
        "can_resend_config": False,
        "can_claim_referral_trial": False,
    }

    try:
        sub = db.get_latest_subscription_for_telegram(telegram_user_id=telegram_user_id)
    except Exception:
        sub = None

    if not sub:
        return ctx

    ctx["has_active_subscription"] = True
    ctx["subscription_id"] = sub.get("id")
    ctx["expires_at"] = sub.get("expires_at")
    ctx["username"] = sub.get("telegram_user_name")
    ctx["last_event_name"] = sub.get("last_event_name") or "unknown"
    ctx["vpn_ip"] = sub.get("vpn_ip")
    ctx["wg_public_key"] = sub.get("wg_public_key")

    # Тип подписки
    event = ctx["last_event_name"]
    if event and "referral_free_trial" in str(event):
        ctx["subscription_type"] = "trial"
    elif event and str(event).startswith("promo"):
        ctx["subscription_type"] = "promo"
    elif event and any(
        x in str(event) for x in ["yookassa", "heleket", "points_payment", "points_extend"]
    ):
        ctx["subscription_type"] = "paid"
    else:
        ctx["subscription_type"] = "other"

    # Можно ли переотправить конфиг
    if sub.get("vpn_ip") and sub.get("wg_private_key"):
        ctx["can_resend_config"] = True

    # Реферер
    try:
        referrer = db.get_referrer_telegram_id(telegram_user_id)
        ctx["has_referrer"] = referrer is not None
    except Exception:
        pass

    # Баллы
    try:
        balance = db.get_user_points_balance(telegram_user_id=telegram_user_id)
        ctx["points_balance"] = balance
    except Exception:
        pass

    # Handshake
    pub_key = sub.get("wg_public_key")
    if pub_key:
        try:
            handshakes = wg.get_handshake_timestamps()
            ts = handshakes.get((pub_key or "").strip(), 0)
            ctx["has_handshake"] = ts > 0
        except Exception:
            ctx["has_handshake"] = False

    # Можно ли получить триал по рефералке
    try:
        ctx["can_claim_referral_trial"] = db.user_can_claim_referral_trial(telegram_user_id)
    except Exception:
        pass

    return ctx
```

### Используемые функции проекта

| Функция | Использование |
|---------|----------------|
| `db.get_latest_subscription_for_telegram` | Основной источник подписки и её полей |
| `db.get_referrer_telegram_id` | Флаг `has_referrer` |
| `db.get_user_points_balance` | Баланс баллов |
| `db.user_can_claim_referral_trial` | Возможность claim referral trial |
| `wg.get_handshake_timestamps` | Проверка handshake по `wg_public_key` |

---

## 10. Логирование

### app/logger.py — участок для support_ai.log

```python
SUPPORT_AI_LOG_FILE = os.path.join(LOG_DIR, "support_ai.log")
```

```python
# ===== логгер AI support =====
support_ai_logger = logging.getLogger("support_ai")
support_ai_logger.setLevel(logging.INFO)

if not support_ai_logger.handlers:
    sup_fh = logging.FileHandler(SUPPORT_AI_LOG_FILE, encoding="utf-8")
    sup_fh.setLevel(logging.INFO)
    sup_fh.setFormatter(formatter)
    support_ai_logger.addHandler(sup_fh)


def get_support_ai_logger():
    return support_ai_logger
```

### app/db.py — функция log_support_conversation()

```python
def log_support_conversation(
    telegram_user_id: int,
    user_message: str,
    ai_response: Optional[str],
    detected_intent: Optional[str],
    confidence: Optional[float],
    mode: str = "ai",
    handoff_to_human: bool = False,
) -> None:
    """Записывает диалог AI Support в support_conversations."""
    sql = """
    INSERT INTO support_conversations
        (telegram_user_id, user_message, ai_response, detected_intent, confidence, mode, handoff_to_human)
    VALUES (%s, %s, %s, %s, %s, %s, %s);
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    telegram_user_id,
                    user_message[:10000] if user_message else "",
                    ai_response[:10000] if ai_response else None,
                    detected_intent[:64] if detected_intent else None,
                    confidence,
                    mode[:16] if mode else "ai",
                    handoff_to_human,
                ),
            )
        conn.commit()
```

---

## 11. Подключение Support Router

### app/tg_bot_runner.py — импорт

```python
from .support.router import support_router
```

### app/tg_bot_runner.py — порядок подключения

```python
    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(support_router)  # AI Support — fallback для свободного текста
```

Порядок подключения:

1. `router` — основной router (команды, callbacks, FSM).
2. `support_router` — последним, как fallback.

Support обрабатывает сообщение только если ни один handler основного router его не обработал.

---

## 12. Requirements

### requirements.txt — openai

```txt
openai>=1.0.0
```

Полный фрагмент:

```txt
requests==2.32.3
openai>=1.0.0
# тесты (опционально: pytest tests/)
```

---

## 13. Комментарии: где AI вмешивается в обработку сообщений

AI-support вмешивается **только** когда выполняются все условия:

1. Пользователь отправил **текстовое** сообщение (`F.text`).
2. Текст **не начинается** с `/`.
3. **Ни один handler** основного router не обработал сообщение (в т.ч. FSM).

В этом случае сообщение обрабатывается `handle_support_message` → `process_support_message`, где используется intent-классификация, guardrails и actions. OpenAI вызывается только при `intent == "unclear"` и при наличии `OPENAI_API_KEY`.

Команды (`/start`, `/help` и т.д.), callbacks, FSM-состояния (промокод, демо, админка и т.п.) обрабатываются основным router и **не проходят** через AI-support.
