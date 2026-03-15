"""
Классификация намерений пользователя (rule-based MVP).
Порядок: human_request → missing_config_after_payment → resend_config →
vpn_not_working → referral_info → referral_stats → connect_help → subscription_status →
handshake_status → smalltalk → unclear.
"""
import re
from typing import Dict, Any

from .models import IntentResult


HUMAN_PATTERNS = [
    r"оператор", r"человек", r"поддержк", r"позовите", r"передайте",
    r"связаться", r"позвонить", r"с человеком", r"живой", r"консультант",
]
# Границы слов для оплатил/оплатила/заплатил/купил — чтобы «оплатили» не срабатывало (рефералы)
MISSING_CONFIG_PATTERNS = [
    r"\bоплатил\b", r"\bоплатила\b", r"\bзаплатил\b", r"\bкупил\b",
    r"конфиг после оплаты",
    r"не пришел после оплаты", r"после оплаты не пришел",
]
# Строгие фразы: только явный запрос конфига (избегаем ложных срабатываний на «рефералов оплатили» и т.п.)
RESEND_CONFIG_PATTERNS = [
    r"\bвышли конфиг\b",
    r"\bпришли конфиг\b",
    r"\bотправь конфиг\b",
    r"\bповтори конфиг\b",
    r"\bпришли vpn конфиг\b",
    r"\bпришли конфигурацию\b",
    r"\bперешли конфиг\b",
    r"\bконфиг не пришел\b",
    r"\bконфиг не пришёл\b",
    r"\bне пришел конфиг\b",
    r"\bне пришёл конфиг\b",
    r"\bконфиг пожалуйста\b",
]
VPN_NOT_WORKING_PATTERNS = [
    r"vpn не работает",
    r"подключен но сайты не открываются",
    r"connected есть но интернет не работает",
    r"включил vpn но ничего не открывается",
    r"vpn подключился но интернет не работает",
]
REFERRAL_PATTERNS = [
    r"реферал", r"рефераль", r"реферальная программа", r"пригласить друга",
    r"моя ссылка", r"реферальная ссылка", r"как пригласить друга",
    r"как поделиться ссылкой", r"сколько рефералов", r"сколько друзей",
    r"рефералы", r"рефералов", r"приглашения",
    r"бонусные дни", r"пригласил друга",
]
REFERRAL_STATS_PATTERNS = [
    r"сколько баллов",
    r"сколько бонус",
    r"сколько бонусных дней",
    r"сколько у меня бонус",
    r"мой баланс бонус",
]
CONNECT_HELP_PATTERNS = [
    r"как подключить vpn", r"как подключиться\b", r"помоги подключить",
    r"помоги с подключением", r"не могу подключить",
    r"как настроить vpn", r"как установить\b", r"wireguard",
    r"импорт", r"\bqr\b", r"не работает подключение",
]
STATUS_PATTERNS = [
    r"до какого", r"до какой даты", r"когда истекает", r"срок подписк",
    r"статус подписк", r"активна ли подписк", r"подписк активна",
    r"проверь подписк", r"моя подписк", r"какая подписк",
    r"подписк работает",
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
    referral_info → connect_help → subscription_status → handshake_status →
    smalltalk → unclear.
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

    # 3. resend_config (только явный запрос конфига)
    if _match_patterns(t, RESEND_CONFIG_PATTERNS):
        if context.get("has_active_subscription"):
            return IntentResult(intent="resend_config", confidence=0.9)
        return IntentResult(intent="missing_config_after_payment", confidence=0.7)

    # 4. vpn_not_working
    if _match_patterns(t, VPN_NOT_WORKING_PATTERNS):
        return IntentResult(intent="vpn_not_working", confidence=0.8)

    # 5. referral_info (до connect_help, чтобы «рефералов подключились» не попал в connect_help)
    if _match_patterns(t, REFERRAL_PATTERNS):
        return IntentResult(intent="referral_info", confidence=0.85)

    # 6. referral_stats (баллы, бонусные дни — до connect_help)
    if _match_patterns(t, REFERRAL_STATS_PATTERNS):
        return IntentResult(intent="referral_stats", confidence=0.85)

    # 7. connect_help (узкие паттерны: без голого «подключ», чтобы не ловить «подключились»)
    if _match_patterns(t, CONNECT_HELP_PATTERNS):
        return IntentResult(intent="connect_help", confidence=0.85)

    # 8. subscription_status
    if _match_patterns(t, STATUS_PATTERNS):
        return IntentResult(intent="subscription_status", confidence=0.85)

    # 9. handshake_status
    if _match_patterns(t, HANDSHAKE_PATTERNS):
        return IntentResult(intent="handshake_status", confidence=0.8)

    # 10. smalltalk
    short = t.lower().strip()
    if short in SMALLTALK_PHRASES:
        return IntentResult(intent="smalltalk", confidence=0.7)

    # Краткие фразы (только явный запрос конфига)
    if short in ("вышли конфиг", "отправь конфиг", "пришли конфиг", "конфиг пожалуйста"):
        if context.get("has_active_subscription"):
            return IntentResult(intent="resend_config", confidence=0.9)
        return IntentResult(intent="unclear", confidence=0.3)

    if short in ("подписка", "статус", "до когда"):
        return IntentResult(intent="subscription_status", confidence=0.7)

    # 10. unclear
    return IntentResult(intent="unclear", confidence=0.2)
