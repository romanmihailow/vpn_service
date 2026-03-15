"""
AI Support: оркестрация обработки сообщения.
Использует context, intents, guardrails, actions. Опционально — OpenAI для формулировки.
Расширения: semantic FAQ match при unclear, short-term conversation memory, intent_source в логах.
"""
import os
from typing import Any, Dict, List, Optional, Tuple

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
    action_referral_info,
    action_missing_config_after_payment,
    action_smalltalk,
    action_vpn_not_working,
)
from .models import IntentResult

log = get_support_ai_logger()

# --- Semantic FAQ match: ключевые слова для unclear → детерминированный ответ ---
# Порядок проверки: более специфичные первыми (multi_device, speed, sites)
_FAQ_SITES_KEYWORDS: List[str] = ["сайты", "не открываются", "не грузятся"]
_FAQ_SPEED_KEYWORDS: List[str] = ["скорость", "медленно", "тормозит"]
_FAQ_MULTI_DEVICE_KEYWORDS: List[str] = [
    "два устройства", "два телефона", "несколько устройств",
    "несколько телефонов", "другое устройство", "второй телефон",
]

MEMORY_REUSE_INTENTS = frozenset({"vpn_not_working", "connect_help", "referral_info"})
MEMORY_WINDOW_SEC = 300

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


def _semantic_faq_match(text: str) -> Optional[str]:
    """
    При unclear: проверяет, совпадает ли сообщение с темами FAQ по ключевым словам.
    Возвращает intent для детерминированного ответа: vpn_not_working | speed_issue | multi_device.
    """
    if not text or not text.strip():
        return None
    lower = text.strip().lower()
    for kw in _FAQ_SITES_KEYWORDS:
        if kw in lower:
            return "vpn_not_working"
    for kw in _FAQ_SPEED_KEYWORDS:
        if kw in lower:
            return "speed_issue"
    for kw in _FAQ_MULTI_DEVICE_KEYWORDS:
        if kw in lower:
            return "multi_device"
    return None


async def _call_openai_for_phrase(user_message: str, context: Dict[str, Any]) -> Optional[str]:
    """Опционально: сформулировать ответ через OpenAI."""
    client = _get_openai_client()
    if not client:
        return None
    try:
        from .prompts import SYSTEM_PROMPT, build_user_prompt, get_faq_text
        summary = _format_context_summary(context)
        faq_text = get_faq_text()
        user_prompt = build_user_prompt(user_message, summary, faq_text=faq_text)
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
        "intent_source": "rule",
    }

    if not text or not user_id:
        meta["intent_source"] = "fallback"
        return get_safe_fallback(), None, meta

    context = build_user_context(user_id)
    result = classify_intent(text, context)
    meta["intent"] = result.intent
    meta["confidence"] = result.confidence

    # Short-term conversation memory: при unclear с низкой уверенностью — переиспользовать последний intent
    if result.intent == "unclear" and (result.confidence or 0) < 0.7:
        try:
            last = db.get_last_support_conversation(user_id, MEMORY_WINDOW_SEC)
            if last and last.get("detected_intent") in MEMORY_REUSE_INTENTS:
                result = IntentResult(intent=last["detected_intent"], confidence=0.85)
                meta["intent"] = result.intent
                meta["confidence"] = result.confidence
                meta["intent_source"] = "memory"
        except Exception as e:
            log.warning("Conversation memory fetch failed: %r", e)

    # Human request — сразу handoff
    if result.intent == "human_request":
        meta["handoff_to_human"] = True
        txt, km = action_human_request()
        return txt, km, meta

    # Guardrails: для известных интентов с низкой уверенностью — fallback.
    # Для unclear не возвращаемся здесь: идём в блок else → OpenAI + FAQ, затем fallback при ошибке.
    can_handle, fallback_text = should_handle_directly(result.intent, result.confidence)
    if not can_handle and fallback_text and result.intent != "unclear":
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

    elif result.intent == "referral_info":
        meta["action"] = "referral_info"
        reply_text, reply_markup = action_referral_info()

    elif result.intent == "vpn_not_working":
        meta["action"] = "vpn_not_working"
        reply_text, reply_markup, meta["vpn_diagnosis"] = action_vpn_not_working(context)

    elif result.intent == "smalltalk":
        meta["action"] = "smalltalk"
        reply_text = action_smalltalk()

    else:
        meta["action"] = "unclear"
        meta["fallback"] = True
        meta["handoff_to_human"] = True

        # Semantic FAQ match before OpenAI: сайты/скорость/устройства → детерминированный ответ
        faq_matched_intent = _semantic_faq_match(text)
        if faq_matched_intent:
            meta["intent_source"] = "faq_match"
            meta["fallback"] = False
            meta["handoff_to_human"] = False
            if faq_matched_intent == "vpn_not_working":
                meta["action"] = "vpn_not_working"
                reply_text, reply_markup, meta["vpn_diagnosis"] = action_vpn_not_working(context)
            elif faq_matched_intent == "speed_issue":
                from ..messages import SPEED_ISSUE_FAQ_RESPONSE
                reply_text = SPEED_ISSUE_FAQ_RESPONSE
                reply_markup = None
            elif faq_matched_intent == "multi_device":
                from ..messages import MULTI_DEVICE_FAQ_RESPONSE
                reply_text = MULTI_DEVICE_FAQ_RESPONSE
                reply_markup = None

        if not faq_matched_intent:
            meta["intent_source"] = "openai" if OPENAI_API_KEY else "fallback"
            if OPENAI_API_KEY:
                ai_text = await _call_openai_for_phrase(text, context)
                if ai_text:
                    reply_text = ai_text + "\n\n" + get_support_offer()
                    meta["intent_source"] = "openai"
                else:
                    reply_text = get_safe_fallback() + "\n\n" + get_support_offer()
                    meta["intent_source"] = "fallback"
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

    text_for_log = (text or "").replace("\n", " ").replace("\r", " ").strip()[:300]
    text_for_log = text_for_log.replace('"', '\\"')
    log.info(
        "support_ai tg_id=%s intent=%s conf=%.2f source=%s action=%s fallback=%s handoff=%s resend=%s vpn_diagnosis=%s text=\"%s\"",
        user_id,
        meta["intent"],
        meta["confidence"] or 0,
        meta.get("intent_source", "rule"),
        meta["action"],
        meta["fallback"],
        meta["handoff_to_human"],
        meta["resend_done"],
        meta.get("vpn_diagnosis") or "",
        text_for_log,
    )

    return reply_text, reply_markup, meta
