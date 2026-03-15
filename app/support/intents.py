"""
Классификация намерений пользователя (rule-based MVP).
Порядок проверки: human_request → missing_config_after_payment → resend_config →
vpn_not_working → connect_help → subscription_status → handshake_status →
smalltalk → unclear.
"""
import re
from typing import Dict, Any

from .models import IntentResult


HUMAN_PATTERNS = [
    r"оператор", r"человек", r"поддержк", r"позовите", r"передайте",
    r"связаться", r"позвонить", r"с человеком", r"живой", r"консультант",
]
MISSING_CONFIG_PATTERNS = [
    r"оплатил", r"оплатила", r"заплатил", r"купил", r"конфиг после оплаты",
    r"не пришел после оплаты", r"после оплаты не пришел",
]
RESEND_PATTERNS = [
    r"не пришел", r"не пришёл", r"не пришел конфиг", r"конфиг не пришел",
    r"отправь конфиг", r"перешли конфиг", r"повторно отправь", r"вышли конфиг",
]
VPN_NOT_WORKING_PATTERNS = [
    r"vpn не работает",
    r"подключен но сайты не открываются",
    r"connected есть но интернет не работает",
    r"включил vpn но ничего не открывается",
    r"vpn подключился но интернет не работает",
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
SMALLTALK_PHRASES = (
    "кто ты",
    "что ты умеешь",
    "ты бот",
    "что ты можешь",
    "привет",
    "здравствуйте",
)


def _match_patterns(text: str, patterns: list) -> bool:
    t = (text or "").lower().strip()
    return any(re.search(p, t, re.I) for p in patterns)


def classify_intent(text: str, context: Dict[str, Any]) -> IntentResult:
    """
    Rule-based классификация намерения.
    Порядок: human → missing_config → resend → vpn_not_working →
    connect_help → subscription_status → handshake_status → smalltalk → unclear.
    """
    t = (text or "").strip()
    if not t or len(t) < 2:
        return IntentResult(intent="unclear", confidence=0.0, maybe_reason="empty")

    # 1. human_request
    if _match_patterns(t, HUMAN_PATTERNS):
        return IntentResult(intent="human_request", confidence=0.95)

    # 2. missing_config_after_payment (раньше resend!)
    if _match_patterns(t, MISSING_CONFIG_PATTERNS):
        if context.get("has_active_subscription") and context.get("can_resend_config"):
            return IntentResult(intent="missing_config_after_payment", confidence=0.85)
        return IntentResult(intent="missing_config_after_payment", confidence=0.9)

    # 3. resend_config
    if _match_patterns(t, RESEND_PATTERNS):
        if context.get("has_active_subscription"):
            return IntentResult(intent="resend_config", confidence=0.9)
        return IntentResult(intent="missing_config_after_payment", confidence=0.7)

    # 4. vpn_not_working
    if _match_patterns(t, VPN_NOT_WORKING_PATTERNS):
        return IntentResult(intent="vpn_not_working", confidence=0.8)

    # 5. connect_help
    if _match_patterns(t, CONNECT_HELP_PATTERNS):
        return IntentResult(intent="connect_help", confidence=0.85)

    # 6. subscription_status
    if _match_patterns(t, STATUS_PATTERNS):
        return IntentResult(intent="subscription_status", confidence=0.85)

    # 7. handshake_status
    if _match_patterns(t, HANDSHAKE_PATTERNS):
        return IntentResult(intent="handshake_status", confidence=0.8)

    # 8. smalltalk
    short = t.lower().strip()
    if short in SMALLTALK_PHRASES:
        return IntentResult(intent="smalltalk", confidence=0.7)

    # Краткие фразы (resend / status)
    if short in ("конфиг", "конфиг пожалуйста", "вышли конфиг", "отправь конфиг"):
        if context.get("has_active_subscription"):
            return IntentResult(intent="resend_config", confidence=0.9)
        return IntentResult(intent="unclear", confidence=0.3)

    if short in ("подписка", "статус", "до когда"):
        return IntentResult(intent="subscription_status", confidence=0.7)

    # 9. unclear
    return IntentResult(intent="unclear", confidence=0.2)
