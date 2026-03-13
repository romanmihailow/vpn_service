import asyncio
import io
import logging
from typing import Optional
from datetime import datetime

from aiogram import Bot
from aiogram.types import BufferedInputFile

from .config import settings
from .format_admin import fmt_date
from .messages import (
    CONFIG_QR_CAPTION,
    CONNECTION_INSTRUCTION_SHORT,
    DEFAULT_CONFIG_CAPTION,
)
import qrcode

log = logging.getLogger(__name__)

CONFIG_SEND_DELAY_SEC = 0.7


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


async def send_vpn_config_to_user(
    telegram_user_id: int,
    config_text: str,
    caption: Optional[str] = None,
) -> None:
    """
    Отправляем пользователю:
    1) конфиг файлом
    2) QR-код
    3) короткую инструкцию

    Между сообщениями задержка ~0.7 сек для последовательного чтения.
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

        # 3. Короткая инструкция
        await bot.send_message(
            chat_id=telegram_user_id,
            text=CONNECTION_INSTRUCTION_SHORT,
            parse_mode=None,
            disable_web_page_preview=True,
        )
        log.info("[SendConfig] Instruction sent to tg_id=%s", telegram_user_id)

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
