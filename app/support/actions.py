"""
Обработчики действий AI Support.
Переиспользуют существующую логику: send_vpn_config_to_user, wg, db.
"""
import time
from typing import Any, Dict, Optional, Tuple

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import db, wg
from ..bot import send_vpn_config_to_user
from ..messages import (
    CONFIG_CHECK_NOW_BUTTON_TEXT,
    CONNECTION_INSTRUCTION_SHORT,
    HELP_INSTRUCTION,
    PRIVACY_POLICY_RESPONSE,
    REFERRAL_BALANCE_RESPONSE,
    REFERRAL_INFO_RESPONSE,
    REFERRAL_STATS_RESPONSE,
    SUPPORT_BUTTON_TEXT,
    SUPPORT_URL,
    VPN_SYMPTOM_MEDIA_PROBLEM,
    VPN_SYMPTOM_SITES_NOT_LOADING,
    VPN_SYMPTOM_SLOW_SPEED,
)

from .symptoms import classify_vpn_symptom

RESEND_COOLDOWN_SEC = 30
RESEND_COOLDOWN: Dict[int, float] = {}


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
    Cooldown 30 сек — защита от flood.
    Возвращает (текст_ответа, успех, reply_markup).
    """
    if not context.get("can_resend_config"):
        return (
            "У тебя нет активной подписки или конфиг пока не создан. "
            "Если ты оплатил подписку — лучше обратиться в поддержку, они проверят.",
            False,
            _support_keyboard(),
        )

    now = time.monotonic()
    last = RESEND_COOLDOWN.get(telegram_user_id, 0)
    if now - last < RESEND_COOLDOWN_SEC:
        return (
            "Я уже отправил конфиг недавно. Проверь сообщения выше.",
            False,
            None,
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
        schedule_checkpoint=False,
    )
    RESEND_COOLDOWN[telegram_user_id] = now
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


def action_privacy_policy() -> str:
    """Ответ про персональные данные и конфиденциальность."""
    return PRIVACY_POLICY_RESPONSE


def action_referral_info() -> Tuple[str, InlineKeyboardMarkup]:
    """Ответ на вопросы про реферальную программу; кнопка «Пригласить друга»."""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пригласить друга", callback_data="ref:open_from_notify")],
        ]
    )
    return REFERRAL_INFO_RESPONSE, kb


def action_referral_stats() -> Tuple[str, InlineKeyboardMarkup]:
    """Ответ на вопросы про статистику рефералов (сколько подключились/оплатили); кнопка «Пригласить друга»."""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пригласить друга", callback_data="ref:open_from_notify")],
        ]
    )
    return REFERRAL_STATS_RESPONSE, kb


def action_referral_balance() -> Tuple[str, InlineKeyboardMarkup]:
    """Ответ на вопросы про баланс баллов/бонусных дней; кнопка «Пригласить друга»."""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пригласить друга", callback_data="ref:open_from_notify")],
        ]
    )
    return REFERRAL_BALANCE_RESPONSE, kb


def action_smalltalk() -> str:
    """Ответ на smalltalk (кто ты, привет и т.д.)."""
    return (
        "Я помощник MaxNet VPN.\n"
        "Могу помочь:\n\n"
        "• отправить конфиг\n"
        "• проверить подписку\n"
        "• помочь с подключением\n"
        "• позвать оператора"
    )


def _stale_keyboard(subscription_id: Any) -> InlineKeyboardMarkup:
    """Клавиатура для ветки handshake_stale: проверить подключение + поддержка."""
    sub_id = subscription_id if subscription_id is not None else 0
    rows = []
    if sub_id:
        rows.append([
            InlineKeyboardButton(
                text=CONFIG_CHECK_NOW_BUTTON_TEXT,
                callback_data=f"config_check_now:{sub_id}",
            ),
        ])
    rows.append([InlineKeyboardButton(text=SUPPORT_BUTTON_TEXT, url=SUPPORT_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def action_vpn_not_working(
    context: Dict[str, Any],
    user_message: Optional[str] = None,
) -> Tuple[str, Optional[InlineKeyboardMarkup], str, str]:
    """
    Диагностический flow для intent vpn_not_working.
    Возвращает (текст, кнопка, vpn_diagnosis, vpn_symptom).
    При handshake_state == "fresh" использует классификатор симптомов по формулировке пользователя.
    """
    has_sub = context.get("has_active_subscription")
    can_resend = context.get("can_resend_config")
    has_handshake = context.get("has_handshake")
    handshake_state = context.get("handshake_state") or "none"
    vpn_ip = context.get("vpn_ip")
    wg_public_key = context.get("wg_public_key")
    subscription_id = context.get("subscription_id")
    empty_symptom = ""

    # Ветка 1 — нет активной подписки
    if not has_sub:
        return (
            "У тебя не найдена активная подписка. Без неё VPN не подключается. "
            "Если ты уже оплатил — лучше напиши в поддержку, они проверят.",
            _support_keyboard(),
            "no_subscription",
            empty_symptom,
        )

    # Ветка 2 — подписка есть, но нет данных для конфига
    if not can_resend or not vpn_ip or not wg_public_key:
        return (
            "Настройки подключения для твоей подписки сейчас недоступны. "
            "Обратись в поддержку — они помогут.",
            _support_keyboard(),
            "no_config_data",
            empty_symptom,
        )

    # Ветка 3 — handshake нет (туннель не установлен)
    if handshake_state == "none" or has_handshake is False:
        return (
            "Подключение к VPN ещё не установлено — скорее всего, туннель не включён или конфиг не добавлен.\n\n"
            "Что сделать:\n"
            "1. Открой WireGuard\n"
            "2. Проверь, что туннель добавлен (конфиг из бота)\n"
            "3. Включи туннель (переключатель в положение «вкл»)\n\n"
            "Если не получится — нажми кнопку ниже.",
            _support_keyboard(),
            "no_handshake",
            empty_symptom,
        )

    # Ветка 4 — handshake есть, но устарел (stale)
    if handshake_state == "stale":
        return (
            "VPN подключался раньше, но сейчас соединение не выглядит активным.\n\n"
            "Попробуй:\n"
            "1. Выключить и снова включить туннель в WireGuard\n"
            "2. Перезапустить приложение WireGuard\n"
            "3. Нажать «🔍 Проверить подключение» и проверить снова\n\n"
            "Если не поможет — напиши в поддержку.",
            _stale_keyboard(subscription_id),
            "handshake_stale",
            empty_symptom,
        )

    # Ветка 5 — handshake свежий: ответ по симптому только при handshake_state == "fresh"
    if handshake_state == "fresh":
        symptom = classify_vpn_symptom(user_message or "")
        if symptom == "sites_not_loading":
            return (
                VPN_SYMPTOM_SITES_NOT_LOADING,
                _support_keyboard(),
                "handshake_ok",
                "sites_not_loading",
            )
        if symptom == "slow_speed":
            return (
                VPN_SYMPTOM_SLOW_SPEED,
                _support_keyboard(),
                "handshake_ok",
                "slow_speed",
            )
        if symptom == "media_problem":
            return (
                VPN_SYMPTOM_MEDIA_PROBLEM,
                _support_keyboard(),
                "handshake_ok",
                "media_problem",
            )
        # generic_problem — прежний универсальный ответ
        return (
            "VPN-подключение у тебя установлено. Значит, проблема, скорее всего, уже после подключения.\n\n"
            "Попробуй:\n"
            "1. Выключить и снова включить туннель в WireGuard\n"
            "2. Перезапустить приложение WireGuard\n"
            "3. Проверить, открываются ли сайты через другую сеть (мобильный интернет)\n\n"
            "Если не поможет — напиши в поддержку.",
            _support_keyboard(),
            "handshake_ok",
            "generic_problem",
        )

    # Ветка 5b — handshake есть, но не fresh (напр. state не задан): универсальный ответ без симптома
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
            "",
        )

    # Ветка 6 — неизвестный / неполный статус
    return (
        "Не удалось точно определить причину. Лучше напиши в поддержку — они разберутся.",
        _support_keyboard(),
        "unknown",
        empty_symptom,
    )


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
