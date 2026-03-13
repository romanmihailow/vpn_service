"""
Форматирование для админ-уведомлений: кликабельные никнеймы, некликабельные ID.
"""
from typing import Optional


def fmt_username_link(username: Optional[str]) -> str:
    """Кликабельная ссылка @username на t.me/username."""
    if not username or not (u := str(username).strip()):
        return ""
    return f'<a href="https://t.me/{u}">@{u}</a>'


def fmt_user_line(username: Optional[str], telegram_user_id: int) -> str:
    """Строка пользователя: кликабельный @username + некликабельный ID."""
    if username and (u := str(username).strip()):
        return f'{fmt_username_link(u)} (<code>{telegram_user_id}</code>)'
    return f"<code>{telegram_user_id}</code>"


def fmt_ref_display(ref_username: Optional[str], ref_telegram_id: int) -> str:
    """Строка реферера: кликабельный @ref или некликабельный ID."""
    if ref_username and (u := str(ref_username).strip()):
        return fmt_username_link(u)
    return f"<code>{ref_telegram_id}</code>"
