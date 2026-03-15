"""
AI Support MVP — первая линия поддержки внутри бота MaxNet VPN.
Обрабатывает свободные текстовые сообщения, не вмешиваясь в команды и FSM.
"""

from .router import support_router, handle_support_message
from .service import process_support_message

__all__ = ["support_router", "handle_support_message", "process_support_message"]
