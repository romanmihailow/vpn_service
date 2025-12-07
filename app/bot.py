import io
from typing import Optional

from aiogram import Bot
from aiogram.types import BufferedInputFile

from .config import settings
import qrcode


INSTRUCTION_TEXT = """
📱 Инструкция: подключение телефона к VPN (WireGuard)

Шаг 1. Установить приложение WireGuard
Выберите вашу платформу:

<b>Android</b>:
1. Открыть Google Play
2. Ввести: <code>WireGuard</code>
3. Установить приложение (разработчик: WireGuard Development Team)

<b>iPhone (iOS)</b>:
1. Открыть App Store
2. Ввести: <code>WireGuard</code>
3. Установить приложение (разработчик: WireGuard Development Team)

Шаг 2. Получить конфигурацию VPN
Тебе пришли QR-код и файл <code>.conf</code> от бота.

<b>Вариант A — QR-код (самый простой)</b>:
1. Открыть приложение WireGuard
2. Нажать «+»
3. Выбрать «Scan from QR code»
4. Навести камеру на QR-код
5. Назвать туннель, например: <code>MaxNet_VPN</code>
6. Сохранить

<b>Вариант B — импорт файла</b>:

<b>Android</b>:
1. Сохранить файл <code>vpn.conf</code> на телефон
2. Открыть WireGuard
3. Нажать «+» → <b>Import from file</b>
4. Найти файл → Выбрать → Добавить

<b>iPhone</b>:
1. Открыть файл через «Файлы» / Telegram
2. Нажать «Поделиться»
3. Выбрать WireGuard
4. Импортировать конфигурацию

Шаг 3. Включить VPN
1. Открыть WireGuard
2. Нажать переключатель рядом с туннелем → «Включено»
3. Подождать 1–2 секунды
Если всё ок — появится зелёный статус <b>Connected</b>.

Шаг 4. Проверить, что VPN работает
1. Открыть в браузере: https://ifconfig.me
2. Если подключение успешно — ты увидишь IP из Латвии или другой страны, где стоит сервер.

Теперь весь интернет-трафик телефона идёт через безопасный VPN-туннель.

⚠️ Частые ошибки:

• <b>Не сканируется QR</b> — увеличь яркость экрана, почисти экран, приблизь/отдали камеру.  
• <b>Подключение не включается</b> — проверь интернет на телефоне (Wi-Fi / 4G).  
• <b>Нет интернета после включения</b> — перезапусти телефон или отключи/включи VPN, переключи Wi-Fi/4G.  
• <b>Пишет “Handshake timeout”</b> — сервер недоступен или неверные ключи (напиши админу).
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
                "Ниже — VPN-конфиг WireGuard и QR-код для подключения 👇"
            )

        # 1. Конфиг как файл
        cfg_bytes = config_text.encode("utf-8")
        cfg_file = BufferedInputFile(cfg_bytes, filename="vpn.conf")
        await bot.send_document(
            chat_id=telegram_user_id,
            document=cfg_file,
            caption=caption,
        )

        # 2. QR-код
        qr_bytes = generate_qr_image_bytes(config_text)
        qr_file = BufferedInputFile(qr_bytes, filename="vpn_qr.png")
        await bot.send_photo(
            chat_id=telegram_user_id,
            photo=qr_file,
            caption="Отсканируй этот QR в приложении WireGuard 👆",
        )

        # 3. Инструкция
        await bot.send_message(
            chat_id=telegram_user_id,
            text=INSTRUCTION_TEXT,
            parse_mode="HTML",
            disable_web_page_preview=True,
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
