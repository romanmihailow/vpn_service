"""
Форматирование для админ-уведомлений: кликабельные никнеймы, некликабельные ID.
Все даты выводятся в МСК (UTC+3) для удобства пользователей.
"""
from datetime import timezone
from typing import Optional
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")


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


def _to_msk(dt):
    """Переводит datetime/date в МСК (UTC+3). Naive считаем UTC."""
    if not hasattr(dt, "strftime"):
        return None
    from datetime import datetime as dt_cls
    if not isinstance(dt, dt_cls):
        # date без времени — считаем полночь UTC
        dt = dt_cls.combine(dt, dt_cls.min.time(), tzinfo=timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK)


def fmt_date(dt, with_time: bool = True) -> str:
    """Единый формат даты для уведомлений в МСК: dd.mm.yyyy [HH:MM]"""
    msk = _to_msk(dt)
    if msk is None:
        return str(dt)[:16] if dt else ""
    if with_time:
        return msk.strftime("%d.%m.%Y %H:%M")
    return msk.strftime("%d.%m.%Y")
