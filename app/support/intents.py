"""
Классификация намерений пользователя (rule-based MVP).
Порядок: human_request → missing_config_after_payment → resend_config →
vpn_not_working → privacy_policy → referral_stats → referral_balance → referral_info →
subscription_status → pricing_info → connect_help → handshake_status → smalltalk → unclear.
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
PRIVACY_POLICY_PATTERNS = [
    r"персональные данные",
    r"что с моими данными",
    r"храните ли данные",
    r"конфиденциальность",
    r"privacy",
    r"данные пользователя",
    r"вы видите мой трафик",
    r"видите ли вы трафик",
    r"храните ли вы историю сайтов",
    r"история сайтов",
    r"логи трафика",
    r"вы храните логи",
    r"что вы видите",
]
REFERRAL_STATS_PATTERNS = [
    r"сколько рефералов",
    r"сколько друзей подключилось",
    r"сколько друзей оплатили",
    r"сколько рефералов оплатили",
    r"сколько подключились",
]
REFERRAL_BALANCE_PATTERNS = [
    r"сколько баллов",
    r"сколько бонусов",
    r"мой баланс",
    r"бонусные дни",
    r"сколько бонусных дней",
    r"как посмотреть бонусы",
    r"где посмотреть бонусы",
    r"где мои бонусы",
    r"где посмотреть баллы",
]
# Только приглашение / как пригласить (без статистики и баланса)
REFERRAL_PATTERNS = [
    r"реферальная программа",
    r"как работает рефералка",
    r"как пригласить друга",
    r"реферальная ссылка",
    r"пригласить друга",
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
    r"сколько осталось подписки",
    r"когда закончится подписка",
    r"сколько дней осталось",
    r"до когда работает vpn",
    r"до когда работает подписка",
]
PRICING_PATTERNS = [
    r"сколько стоит",
    r"цена",
    r"стоимость",
    r"тариф",
    r"тарифы",
    r"сколько стоит vpn",
    r"цена vpn",
    r"сколько стоит подписка",
    r"стоимость подписки",
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

    # 5. privacy_policy
    if _match_patterns(t, PRIVACY_POLICY_PATTERNS):
        return IntentResult(intent="privacy_policy", confidence=0.85)

    # 6. referral_stats (сколько рефералов/друзей подключились/оплатили)
    if _match_patterns(t, REFERRAL_STATS_PATTERNS):
        return IntentResult(intent="referral_stats", confidence=0.85)

    # 7. referral_balance (баллы, бонусы, баланс)
    if _match_patterns(t, REFERRAL_BALANCE_PATTERNS):
        return IntentResult(intent="referral_balance", confidence=0.85)

    # 8. referral_info (только приглашение: как пригласить, ссылка)
    if _match_patterns(t, REFERRAL_PATTERNS):
        return IntentResult(intent="referral_info", confidence=0.85)

    # 9. subscription_status
    if _match_patterns(t, STATUS_PATTERNS):
        return IntentResult(intent="subscription_status", confidence=0.85)

    # 10. pricing_info (после subscription_status, перед connect_help)
    if _match_patterns(t, PRICING_PATTERNS):
        return IntentResult(intent="pricing_info", confidence=0.85)

    # 11. connect_help (узкие паттерны: без голого «подключ», чтобы не ловить «подключились»)
    if _match_patterns(t, CONNECT_HELP_PATTERNS):
        return IntentResult(intent="connect_help", confidence=0.85)

    # 12. handshake_status
    if _match_patterns(t, HANDSHAKE_PATTERNS):
        return IntentResult(intent="handshake_status", confidence=0.8)

    # 13. smalltalk
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

    # 14. unclear
    return IntentResult(intent="unclear", confidence=0.2)
