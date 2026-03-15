"""
Router для AI Support.
Обрабатывает только свободные текстовые сообщения (не команды, не callbacks, не FSM).
Должен подключаться ПОСЛЕДНИМ, чтобы срабатывать как fallback.
"""
from aiogram import F, Router
from aiogram.types import Message

from .service import process_support_message

support_router = Router(name="support")


def _is_not_command(message: Message) -> bool:
    """Фильтр: текст есть и не начинается с /"""
    t = (message.text or "").strip()
    return bool(t) and not t.startswith("/")


@support_router.message(F.text, _is_not_command)
async def handle_support_message(message: Message) -> None:
    """
    Handler для свободного текста.
    Срабатывает только на обычные сообщения (не команды).
    FSM-обработчики в main router имеют приоритет за счёт StateFilter.
    """
    reply_text, reply_markup, _meta = await process_support_message(message)
    await message.answer(
        reply_text,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
