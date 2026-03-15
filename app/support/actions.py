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
    CONNECTION_INSTRUCTION_SHORT,
    HELP_INSTRUCTION,
    SUPPORT_BUTTON_TEXT,
    SUPPORT_URL,
)

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
