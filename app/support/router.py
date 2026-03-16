"""
Router для AI Support.
Обрабатывает только свободные текстовые сообщения (не команды, не callbacks, не FSM).
Должен подключаться ПОСЛЕДНИМ, чтобы срабатывать как fallback.
"""
import logging

from aiogram import F, Router
from aiogram.types import Message

from .service import process_support_message

log = logging.getLogger(__name__)
support_router = Router(name="support")

SUPPORT_FALLBACK_TEXT = (
    "Что-то пошло не так.\n"
    "Попробуй ещё раз или напиши в поддержку: @MaxNet_VPN_Support"
)


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
    try:
        reply_text, reply_markup, _meta = await process_support_message(message)
        await message.answer(
            reply_text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception(
            "[Support] Failed to process or send reply for chat_id=%s",
            message.chat.id if message.chat else None,
        )
        try:
            await message.answer(
                SUPPORT_FALLBACK_TEXT,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("[Support] Fallback answer also failed")
