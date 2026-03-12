import io
import logging
from typing import Optional
from datetime import datetime

from aiogram import Bot
from aiogram.types import BufferedInputFile

from .config import settings
import qrcode

log = logging.getLogger(__name__)



INSTRUCTION_TEXT = """
📱 Инструкция: подключение телефона к VPN (WireGuard)

<b>Шаг 1.</b> Установить приложение WireGuard

<b>Android</b>:
<a href="https://play.google.com/store/apps/details?id=com.wireguard.android">Перейти в Play Маркет</a>

<b>iPhone</b>:
<a href="https://apps.apple.com/app/wireguard/id1441195209">Перейти в App Store</a>

<b>Шаг 2.</b> Получить конфигурацию VPN
Тебе пришли файл vpn.conf и QR-код от бота.

<b>Вариант A — из файла</b> (для одного телефона)

<b>Android</b>:
1. Скачать файл vpn.conf из сообщения бота
2. Открыть WireGuard → «+»
3. <b>Импорт из файла или архива</b>
4. Выбрать файл → Добавить
5. Ввести имя туннеля (например, MaxNet_VPN)
6. Сохранить
7. Включить VPN

<b>iPhone</b>:
1. Открыть файл vpn.conf (Файлы или Telegram)
2. «Поделиться» → WireGuard
3. Создать туннель из файла или архива
4. Ввести имя туннеля (например, MaxNet_VPN)
5. Сохранить
6. Включить VPN

<b>Вариант B — QR-код</b>
(удобно при двух устройствах: WG сканирует только с камеры, из фото — нельзя)

1. WireGuard → «+» → «Сканировать QR-код»
2. Навести камеру на QR (на другом экране)
3. Ввести имя туннеля (например, MaxNet_VPN)
4. Сохранить
5. Включить VPN

Если всё ок — зелёный статус <b>Connected</b>.

<b>Шаг 3.</b> Проверить VPN
Открыть в браузере: https://ifconfig.me
Увидишь IP сервера — значит VPN работает.

⚠️ Частые ошибки:

• <b>QR не сканируется</b> — используй импорт из файла (вариант A)
• <b>Не включается</b> — проверь интернет (Wi-Fi / 4G)
• <b>Нет интернета после VPN</b> — перезапусти телефон или VPN
• <b>Ошибка соединения</b> — перезапусти приложение. Не помогло — @MaxNet_Support
""".strip()


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
    3) инструкцию
    """
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)

    try:
        if caption is None:
            caption = (
                "Спасибо за подписку через Tribute!\n\n"
                "Файл vpn.conf — в этом сообщении. QR-код — в следующем 👇"
            )

        # 1. Конфиг как файл
        cfg_bytes = config_text.encode("utf-8")
        cfg_file = BufferedInputFile(cfg_bytes, filename="vpn.conf")
        await bot.send_document(
            chat_id=telegram_user_id,
            document=cfg_file,
            caption=caption,
        )
        log.info("[SendConfig] Document sent to tg_id=%s", telegram_user_id)

        # 2. QR-код
        qr_bytes = generate_qr_image_bytes(config_text)
        qr_file = BufferedInputFile(qr_bytes, filename="vpn_qr.png")
        await bot.send_photo(
            chat_id=telegram_user_id,
            photo=qr_file,
            caption="Отсканируй QR (нужен второй телефон) или импортируй файл из сообщения выше 👆",
        )
        log.info("[SendConfig] QR photo sent to tg_id=%s", telegram_user_id)

        # 3. Инструкция
        await bot.send_message(
            chat_id=telegram_user_id,
            text=INSTRUCTION_TEXT,
            parse_mode="HTML",
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
        # Можно потом заменить формат на локальный, если захочешь
        expires_str = new_expires_at.strftime("%d.%m.%Y %H:%M")

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
