"""
Сборщик контекста пользователя для AI Support.
Read-only, использует существующие функции проекта.
"""
import time
from typing import Any, Dict

from .. import db
from .. import wg

HANDSHAKE_FRESH_SEC = 300


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
        "last_handshake_ts": 0,
        "handshake_age_sec": None,
        "handshake_state": "none",
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

    # Handshake и свежесть
    pub_key = sub.get("wg_public_key")
    if pub_key:
        try:
            handshakes = wg.get_handshake_timestamps()
            ts = handshakes.get((pub_key or "").strip(), 0)
            ctx["has_handshake"] = ts > 0
            ctx["last_handshake_ts"] = ts
            if ts > 0:
                now = int(time.time())
                age = now - ts
                ctx["handshake_age_sec"] = age
                if age <= HANDSHAKE_FRESH_SEC:
                    ctx["handshake_state"] = "fresh"
                else:
                    ctx["handshake_state"] = "stale"
            else:
                ctx["handshake_state"] = "none"
        except Exception:
            ctx["has_handshake"] = False
            ctx["handshake_state"] = "none"

    # Можно ли получить триал по рефералке
    try:
        ctx["can_claim_referral_trial"] = db.user_can_claim_referral_trial(telegram_user_id)
    except Exception:
        pass

    return ctx
