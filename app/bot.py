import asyncio
import io
import logging
from typing import Optional
from datetime import datetime

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from .config import settings
from .format_admin import fmt_date
from .messages import (
    CONFIG_CHECK_MESSAGE,
    CONFIG_CHECK_NOW_BUTTON_TEXT,
    CONFIG_QR_CAPTION,
    CONNECTION_INSTRUCTION_SHORT,
    DEFAULT_CONFIG_CAPTION,
    ONBOARDING_WG_DOWNLOAD_BUTTON,
    ONBOARDING_WIREGUARD_QUESTION,
    ONBOARDING_WG_YES_BUTTON,
    SUPPORT_AFTER_CONFIG_HINT,
    SUPPORT_BUTTON_TEXT,
    SUPPORT_URL,
    TRIAL_EXPIRED_PAID_NOTIFICATION_TEXT,
)
import qrcode

from . import db

log = logging.getLogger(__name__)

CONFIG_SEND_DELAY_SEC = 0.7
CONFIG_CHECKPOINT_DELAY_SEC = 180


def generate_qr_image_bytes(config_text: str) -> bytes:
    """
    Генерим QR по тексту конфигурации WireGuard.
    """
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(config_text)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer.read()


def _make_config_checkpoint_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для сообщения «Удалось подключиться к VPN?»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, всё работает",
                    callback_data=f"config_check_ok:{subscription_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Нет, не получилось",
                    callback_data=f"config_check_failed:{subscription_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📱 Отправить настройки ещё раз",
                    callback_data=f"config_check_resend:{subscription_id}",
                ),
            ],
        ]
    )


async def send_config_checkpoint_message(
    telegram_user_id: int,
    subscription_id: int,
) -> None:
    """
    Отправляет пользователю сообщение «Удалось подключиться к VPN?» с кнопками.
    Вызывается из background job после проверки handshake.
    Запись config_checkpoint_sent выполняет вызывающий код после успешной отправки.
    """
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    try:
        keyboard = _make_config_checkpoint_keyboard(subscription_id)
        await bot.send_message(
            chat_id=telegram_user_id,
            text=CONFIG_CHECK_MESSAGE,
            reply_markup=keyboard,
        )
        log.info(
            "[ConfigCheckpoint] Sent checkpoint to tg_id=%s sub_id=%s",
            telegram_user_id,
            subscription_id,
        )
    finally:
        await bot.session.close()


async def send_trial_expired_paid_notification(telegram_user_id: int) -> None:
    """
    Отправляет сообщение «используй НОВЫЙ конфиг» перед отправкой конфига
    в сценарии trial expired → paid. Вызывается только при recently_expired_trial.
    """
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    try:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=SUPPORT_BUTTON_TEXT, url=SUPPORT_URL)],
            ]
        )
        await bot.send_message(
            chat_id=telegram_user_id,
            text=TRIAL_EXPIRED_PAID_NOTIFICATION_TEXT,
            reply_markup=keyboard,
        )
        log.info(
            "[TrialExpiredPaid] Sent notification to tg_id=%s",
            telegram_user_id,
        )
    finally:
        await bot.session.close()


async def send_vpn_config_to_user(
    telegram_user_id: int,
    config_text: str,
    caption: Optional[str] = None,
    schedule_checkpoint: bool = True,
) -> None:
    """
    Отправляем пользователю:
    1) конфиг файлом
    2) QR-код
    3) короткую инструкцию

    Между сообщениями задержка ~0.7 сек для последовательного чтения.
    Если schedule_checkpoint=True (по умолчанию), через ~3 мин отправляется
    проверка «Удалось подключиться к VPN?» при отсутствии handshake.
    """
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)

    try:
        if caption is None:
            caption = DEFAULT_CONFIG_CAPTION

        # 1. Конфиг как файл
        cfg_bytes = config_text.encode("utf-8")
        cfg_file = BufferedInputFile(cfg_bytes, filename="vpn.conf")
        await bot.send_document(
            chat_id=telegram_user_id,
            document=cfg_file,
            caption=caption,
        )
        log.info("[SendConfig] Document sent to tg_id=%s", telegram_user_id)
        await asyncio.sleep(CONFIG_SEND_DELAY_SEC)

        # 2. QR-код
        qr_bytes = generate_qr_image_bytes(config_text)
        qr_file = BufferedInputFile(qr_bytes, filename="vpn_qr.png")
        await bot.send_photo(
            chat_id=telegram_user_id,
            photo=qr_file,
            caption=CONFIG_QR_CAPTION,
        )
        log.info("[SendConfig] QR photo sent to tg_id=%s", telegram_user_id)
        await asyncio.sleep(CONFIG_SEND_DELAY_SEC)

        # 3. Короткая инструкция + кнопки (проверить подключение, нужна помощь)
        sub = None
        try:
            sub = db.get_latest_subscription_for_telegram(telegram_user_id)
        except Exception:
            pass
        if sub and sub.get("id"):
            # Post-config: только «Проверить подключение» и «Нужна помощь» (без кнопки «Подключить VPN»)
            instruction_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=CONFIG_CHECK_NOW_BUTTON_TEXT,
                            callback_data=f"config_check_now:{sub['id']}",
                        ),
                    ],
                    [
                        InlineKeyboardButton(text=SUPPORT_BUTTON_TEXT, url=SUPPORT_URL),
                    ],
                ]
            )
        else:
            instruction_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text=SUPPORT_BUTTON_TEXT, url=SUPPORT_URL),
                    ],
                ]
            )
        instruction_with_hint = CONNECTION_INSTRUCTION_SHORT + "\n\n" + SUPPORT_AFTER_CONFIG_HINT
        await bot.send_message(
            chat_id=telegram_user_id,
            text=instruction_with_hint,
            parse_mode=None,
            disable_web_page_preview=True,
            reply_markup=instruction_keyboard,
        )
        log.info("[SendConfig] Instruction sent to tg_id=%s", telegram_user_id)

        sub_id = sub.get("id") if sub and sub.get("id") else 0
        onboarding_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=ONBOARDING_WG_YES_BUTTON,
                        callback_data=f"onboarding:wireguard_confirm:{sub_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=ONBOARDING_WG_DOWNLOAD_BUTTON,
                        callback_data="onboarding:wireguard_download",
                    ),
                ],
            ]
        )
        await bot.send_message(
            chat_id=telegram_user_id,
            text=ONBOARDING_WIREGUARD_QUESTION,
            reply_markup=onboarding_keyboard,
        )
        log.info("[Onboarding] tg_id=%s step=wireguard_check", telegram_user_id)

        if schedule_checkpoint:
            try:
                if not sub:
                    sub = db.get_latest_subscription_for_telegram(telegram_user_id)
                if sub and sub.get("id"):
                    db.create_subscription_notification(
                        subscription_id=sub["id"],
                        notification_type="config_checkpoint_pending",
                        telegram_user_id=telegram_user_id,
                        expires_at=sub.get("expires_at"),
                    )
                    log.debug(
                        "[SendConfig] Registered config_checkpoint_pending for tg_id=%s sub_id=%s",
                        telegram_user_id,
                        sub["id"],
                    )
            except Exception as e:
                log.warning(
                    "[SendConfig] Failed to register checkpoint for tg_id=%s: %r",
                    telegram_user_id,
                    e,
                )

    except Exception as e:
        log.error(
            "[SendConfig] Failed to send config to tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        raise
    finally:
        await bot.session.close()


async def send_subscription_extended_notification(
    telegram_user_id: int,
    new_expires_at: datetime,
    tariff_code: str,
    payment_channel: str,
) -> None:
    """
    Короткое уведомление о продлении подписки без повторной отправки конфига.
    """
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    try:
        expires_str = fmt_date(new_expires_at)

        text = (
            "✅ Ваша подписка MaxNet VPN продлена.\n\n"
            f"Тариф: <b>{tariff_code}</b>\n"
            f"Доступ активен до: <b>{expires_str}</b>\n\n"
            f"Спасибо за оплату через {payment_channel}!"
        )

        await bot.send_message(
            chat_id=telegram_user_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    finally:
        await bot.session.close()


async def send_referral_reward_notification(
    telegram_user_id: int,
    points_delta: int,
    level: int | None,
    tariff_code: str,
    payment_channel: str,
) -> None:
    """
    Уведомление пользователю о начислении реферальных баллов.
    """
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    try:
        sign = "+" if points_delta >= 0 else ""
        level_part: str
        if level is None:
            level_part = ""
        else:
            level_part = f"\nУровень реферала: <b>{level}</b>"

        text = (
            "🎁 Тебе начислены реферальные баллы!\n\n"
            f"Из-за оплаты подписки по твоей реферальной цепочке.\n"
            f"Начислено: <b>{sign}{points_delta}</b> баллов.{level_part}\n\n"
            f"Тариф: <b>{tariff_code}</b>\n"
            f"Канал оплаты: <b>{payment_channel}</b>"
        )

        await bot.send_message(
            chat_id=telegram_user_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    finally:
        await bot.session.close()


async def send_subscription_expired_notification(
    telegram_user_id: int,
) -> None:
    """
    Уведомление пользователю о том, что его подписка закончилась.
    """
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    try:
        text = (
            "⏳ Ваша подписка MaxNet VPN закончилась.\n\n"
            "Доступ сейчас отключён.\n\n"
            "Чтобы продолжить пользоваться VPN, оформите новую подписку в боте."
        )

        await bot.send_message(
            chat_id=telegram_user_id,
            text=text,
        )
    finally:
        await bot.session.close()


async def send_text_message(
    telegram_user_id: int,
    text: str,
) -> None:
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    try:
        await bot.send_message(chat_id=telegram_user_id, text=text)
    finally:
        await bot.session.close()




async def get_telegram_username(
    telegram_user_id: int,
) -> Optional[str]:
    """
    Пытаемся получить username пользователя по его telegram_user_id
    через Telegram Bot API.
    """
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
    try:
        chat = await bot.get_chat(chat_id=telegram_user_id)
        username = getattr(chat, "username", None)
        return username
    except Exception as e:
        # Логировать можно здесь, но чтобы не плодить логгер, оставим тихо.
        # При желании можешь добавить логирование.
        return None
    finally:
        await bot.session.close()
