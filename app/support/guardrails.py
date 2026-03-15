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
