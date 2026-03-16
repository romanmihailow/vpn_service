import asyncio
import io
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from collections import defaultdict
from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    CallbackQuery,
    FSInputFile,
)
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from .config import settings
from . import db
from .bot import (
    send_vpn_config_to_user,
    send_subscription_expired_notification,
    send_config_checkpoint_message,
    send_trial_expired_paid_notification,
)
from .messages import (
    CONFIG_CHECK_FAIL,
    CONFIG_CHECK_NOW_FAIL,
    CONFIG_CHECK_NOW_OK,
    CONFIG_CHECK_NOW_UNKNOWN,
    CONFIG_CHECK_OPTIONS,
    CONFIG_CHECK_SUCCESS,
    CONFIG_CHECK_NOW_BUTTON_TEXT,
    HANDSHAKE_SHORT_CONFIRMATION_TEXT,
    HELP_INSTRUCTION,
    ONBOARDING_DEVICE_ANDROID,
    ONBOARDING_DEVICE_COMPUTER,
    ONBOARDING_DEVICE_IPHONE,
    ONBOARDING_DEVICE_QUESTION,
    ONBOARDING_IMPORT_CONFIG,
    ONBOARDING_INSTALL_MOBILE,
    ONBOARDING_READY_BUTTON,
    ONBOARDING_WG_CONFIRM_MESSAGE,
    ONBOARDING_WG_DOWNLOAD_MESSAGE,
    MY_ID_RESPONSE_TEMPLATE,
    MY_ID_UNAVAILABLE,
    PRICING_HEADER,
    REFERRAL_PROMPT_AFTER_CONNECTION_SUCCESS,
    REF_LINK_WELCOME_TEXT,
    REF_TRIAL_BUTTON_TEXT,
    REF_TRIAL_CONFIG_CAPTION,
    SUBSCRIPTION_TEXT,
    SUPPORT_BUTTON_TEXT,
    SUPPORT_DISCOVERY_TEXT,
    SUPPORT_URL,
    TRIAL_EXPIRED_PAID_FOLLOWUP_NO_HANDSHAKE_TEXT,
    TARIFFS_UNAVAILABLE,
    WG_APP_STORE_URL,
    WG_DESKTOP_URL,
    WG_PLAY_MARKET_URL,
)
from . import wg
from .format_admin import fmt_date, fmt_ref_display, fmt_user_line
from .logger import get_logger, get_promo_logger, SUPPORT_AI_LOG_FILE
from .yookassa_client import create_yookassa_payment
from .heleket_client import create_heleket_payment
from .promo_codes import (
    PromoGenerationParams,
    generate_promo_codes,
    build_insert_sql_for_postgres,
)
from .support.router import support_router
from .support.context_builder import build_user_context
from .support.actions import (
    action_connect_help,
    action_human_request,
    action_vpn_not_working,
)

log = get_logger()
promo_log = get_promo_logger()

BROADCAST_BATCH_SIZE = 25
BROADCAST_BATCH_SLEEP = 1.0
MAX_BROADCAST_USERS = 5000
NOTIFY_BATCH_SIZE = 25
NOTIFY_BATCH_SLEEP = 1.0
NO_HANDSHAKE_REMINDER_SLEEP = 5.0  # секунд между отправками (защита от бана Telegram)
NO_HANDSHAKE_REFRESH_EVERY_N = 20  # обновлять handshakes каждые N подписок
NO_HANDSHAKE_PAUSE_BETWEEN_TYPES = 5.0  # пауза между батчами 24h и 5d (сек)
TELEGRAM_GLOBAL_SEMAPHORE = asyncio.Semaphore(20)


async def safe_send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> bool:
    async def _send_once() -> None:
        async with TELEGRAM_GLOBAL_SEMAPHORE:
            await bot.send_message(chat_id=chat_id, text=text, **kwargs)

    try:
        await _send_once()
        return True
    except TelegramRetryAfter as e:
        log.warning(
            "[SafeSend] RetryAfter for chat_id=%s: %s",
            chat_id,
            e.retry_after,
        )
        await asyncio.sleep(e.retry_after)
        try:
            await _send_once()
            return True
        except TelegramRetryAfter as e2:
            log.warning(
                "[SafeSend] RetryAfter again for chat_id=%s: %s",
                chat_id,
                e2.retry_after,
            )
            return False
        except TelegramForbiddenError:
            log.warning("[SafeSend] Bot is blocked by chat_id=%s", chat_id)
            return False
        except TelegramBadRequest as e2:
            log.warning(
                "[SafeSend] BadRequest for chat_id=%s: %r",
                chat_id,
                e2,
            )
            return False
        except Exception as e2:
            log.error(
                "[SafeSend] Unexpected error for chat_id=%s: %r",
                chat_id,
                e2,
            )
            return False
    except TelegramForbiddenError:
        log.warning("[SafeSend] Bot is blocked by chat_id=%s", chat_id)
        return False
    except TelegramBadRequest as e:
        log.warning("[SafeSend] BadRequest for chat_id=%s: %r", chat_id, e)
        return False
    except Exception as e:
        log.error("[SafeSend] Unexpected error for chat_id=%s: %r", chat_id, e)
        return False


async def _send_admin_new_user_notification(
    bot: Bot,
    telegram_user_id: int,
    telegram_username: Optional[str],
    expires_at: Optional[datetime] = None,
) -> None:
    """
    Уведомление админу о новом пользователе (при получении тестового доступа).
    """
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)
    if not admin_id:
        return
    user_line = fmt_user_line(telegram_username, telegram_user_id)
    if expires_at is None:
        sub = db.get_latest_subscription_for_telegram(telegram_user_id)
        expires_at = sub.get("expires_at") if sub else datetime.now(timezone.utc) + timedelta(days=7)
    expires_str = fmt_date(expires_at)
    ref_info = db.get_referrer_with_count(telegram_user_id)
    if ref_info:
        ref_tg = ref_info.get("referrer_telegram_user_id")
        ref_name = ref_info.get("referrer_username")
        ref_display = fmt_ref_display(ref_name, ref_tg)
        # Для «Новый пользователь» показываем ordinal (номер этого приглашённого), чтобы счётчик
        # гарантированно включал текущего пользователя (избегаем рассинхрона 336/336)
        display_count = int(ref_info.get("referral_ordinal") or ref_info.get("referred_count") or 0)
        paid_count = db.count_referrer_paid_referrals(ref_info["referrer_telegram_user_id"])
        ref_line = f"Реферер: {ref_display} ({display_count}/{paid_count})"
    else:
        ref_line = "Реферер: —"
    text = (
        "🆕 <b>Новый пользователь</b>\n\n"
        f"• Пользователь: {user_line}\n"
        "• Источник: Реферальный триал\n"
        f"• {ref_line}\n"
        f"• До: {expires_str}"
    )
    ok = await safe_send_message(
        bot=bot,
        chat_id=admin_id,
        text=text,
        disable_web_page_preview=True,
    )
    if ok:
        log.info("[NewUserNotify] Sent for tg_id=%s", telegram_user_id)


async def _send_admin_promo_used_notification(
    bot: Bot,
    telegram_user_id: int,
    telegram_username: Optional[str],
    promo_code: str,
    extra_days: int,
    expires_at: datetime,
) -> None:
    """
    Уведомление админу об использовании промокода.
    """
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)
    if not admin_id:
        return
    user_line = fmt_user_line(telegram_username, telegram_user_id)
    expires_str = fmt_date(expires_at)
    ref_info = db.get_referrer_with_count(telegram_user_id)
    if ref_info:
        ref_tg = ref_info.get("referrer_telegram_user_id")
        ref_name = ref_info.get("referrer_username")
        ref_display = fmt_ref_display(ref_name, ref_tg)
        referred_count = int(ref_info.get("referred_count") or 0)
        paid_count = db.count_referrer_paid_referrals(ref_info["referrer_telegram_user_id"])
        ref_line = f"Реферер: {ref_display} ({referred_count}/{paid_count})"
    else:
        ref_line = "Реферер: —"
    text = (
        "🎟 <b>Промокод использован</b>\n\n"
        f"• Пользователь: {user_line}\n"
        f"• Промокод: {promo_code}\n"
        f"• Дней: +{extra_days}\n"
        f"• {ref_line}\n"
        f"• До: {expires_str}"
    )
    ok = await safe_send_message(
        bot=bot,
        chat_id=admin_id,
        text=text,
        disable_web_page_preview=True,
    )
    if ok:
        log.info("[PromoUsedNotify] Sent for tg_id=%s code=%s", telegram_user_id, promo_code)


def pluralize_points(n: int) -> str:
    """Склонение слова 'балл' в зависимости от числа."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} балл"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} балла"
    else:
        return f"{n} баллов"


def deactivate_existing_active_subscriptions(
    telegram_user_id: int,
    reason: str,
    release_ips_to_pool: bool = True,
) -> None:
    """
    Деактивирует ВСЕ активные подписки пользователя и удаляет их peer'ы из WireGuard.
    Используется перед выдачей нового доступа.
    При release_ips_to_pool=False IP не возвращаются в пул (для reuse — новая подписка
    того же пользователя переиспользует ключи и IP).
    """
    active_subs = db.get_active_subscriptions_for_telegram(telegram_user_id=telegram_user_id)

    for sub in active_subs:
        sub_id = sub.get("id")
        pub_key = sub.get("wg_public_key")

        if not sub_id:
            continue

        log.info(
            "[AutoCleanup] Deactivate old sub_id=%s for tg_id=%s reason=%s release_ip=%s",
            sub_id,
            telegram_user_id,
            reason,
            release_ips_to_pool,
        )

        db.deactivate_subscription_by_id(
            sub_id=sub_id,
            event_name=reason,
            release_ip_to_pool=release_ips_to_pool,
        )

        if pub_key:
            try:
                wg.remove_peer(pub_key)
            except Exception as e:
                log.error(
                    "[AutoCleanup] Failed to remove old peer pubkey=%s for sub_id=%s: %s",
                    pub_key,
                    sub_id,
                    repr(e),
                )


router = Router()


async def try_give_referral_trial_7d(
    telegram_user_id: int,
    telegram_username: Optional[str],
) -> None:
    """
    Пытается выдать пробный реферальный доступ на 7 дней.

    Условия:
    - у пользователя НЕТ активной подписки;
    - пользователь ЕЩЁ НЕ получал реферальный триал (last_event_name='referral_free_trial_7d').
    """
    client_ip: Optional[str] = None
    subscription_created = False

    try:
        # 1) Проверяем, нет ли уже активной подписки
        active_sub = db.get_latest_subscription_for_telegram(
            telegram_user_id=telegram_user_id,
        )
        if active_sub:
            log.info(
                "[ReferralTrial] Skip trial for tg_id=%s: already has active subscription id=%s",
                telegram_user_id,
                active_sub.get("id"),
            )
            return

        # 2) Проверяем, не выдавали ли уже реферальный триал ранее
        if db.has_referral_trial_subscription(telegram_user_id=telegram_user_id):
            log.info(
                "[ReferralTrial] Skip trial for tg_id=%s: referral trial already given earlier",
                telegram_user_id,
            )
            return

        # 3) На всякий случай выключим все активные подписки (если вдруг есть мусор)
        deactivate_existing_active_subscriptions(
            telegram_user_id=telegram_user_id,
            reason="auto_replace_referral_trial_7d",
        )

        # 4) Генерим WG-ключи и IP
        client_priv, client_pub = wg.generate_keypair()
        client_ip = wg.generate_client_ip()
        allowed_ip = f"{client_ip}/{settings.WG_CLIENT_NETWORK_CIDR}"

        log.info(
            "[ReferralTrial] Add peer (trial) pubkey=%s ip=%s for tg_id=%s",
            client_pub,
            allowed_ip,
            telegram_user_id,
        )
        wg.add_peer(
            public_key=client_pub,
            allowed_ip=allowed_ip,
            telegram_user_id=telegram_user_id,
        )

        # 5) Срок действия триала
        expires_at = datetime.utcnow() + timedelta(days=7)

        # 6) Пишем подписку в БД
        sub_id = db.insert_subscription(
            tribute_user_id=0,
            telegram_user_id=telegram_user_id,
            telegram_user_name=telegram_username,
            subscription_id=0,
            period_id=0,
            period="referral_trial_7d",
            channel_id=0,
            channel_name="Referral trial",
            vpn_ip=client_ip,
            wg_private_key=client_priv,
            wg_public_key=client_pub,
            expires_at=expires_at,
            event_name="referral_free_trial_7d",
        )
        subscription_created = True

        log.info(
            "[ReferralTrial] Trial subscription created: sub_id=%s tg_id=%s vpn_ip=%s expires_at=%s",
            sub_id,
            telegram_user_id,
            client_ip,
            expires_at,
        )

        # 7) Собираем конфиг и отправляем пользователю
        config_text = wg.build_client_config(
            client_private_key=client_priv,
            client_ip=client_ip,
        )

        await send_vpn_config_to_user(
            telegram_user_id=telegram_user_id,
            config_text=config_text,
            caption=REF_TRIAL_CONFIG_CAPTION,
        )

    except Exception as e:
        if client_ip and not subscription_created:
            try:
                db.release_ip_in_pool(client_ip)
            except Exception:
                pass
        log.error(
            "[ReferralTrial] Failed to issue referral trial for tg_id=%s: %r",
            telegram_user_id,
            e,
        )


BASE_DIR = Path(__file__).resolve().parent.parent
TERMS_FILE_PATH = BASE_DIR / "TERMS.md"
PRIVACY_FILE_PATH = BASE_DIR / "PRIVACY.md"


class AdminAddSub(StatesGroup):
    waiting_for_target = State()
    waiting_for_period = State()


class DemoRequest(StatesGroup):
    waiting_for_message = State()


class Broadcast(StatesGroup):
    waiting_for_text = State()


class BroadcastList(StatesGroup):
    """Рассылка по списку ID из файла (например, пользователи без handshake)."""
    waiting_for_file = State()
    waiting_for_text = State()


class BonusList(StatesGroup):
    """Начислить 100 баллов каждому из списка ID и отправить сообщение."""
    waiting_for_file = State()
    waiting_for_text = State()


class PromoStates(StatesGroup):
    waiting_for_code = State()


class PromoAdmin(StatesGroup):
    """
    FSM для админского мастера генерации промокодов.
    """
    waiting_for_mode = State()
    waiting_for_extra_days = State()
    waiting_for_valid_days = State()
    waiting_for_code_count = State()      # для одноразовых
    waiting_for_manual_code = State()     # для многоразового
    waiting_for_max_uses = State()        # для многоразового
    waiting_for_per_user_limit = State()  # для многоразового
    waiting_for_comment = State()
    waiting_for_confirm = State()


# Справочники тарифов для оплаты.
# Теперь основным источником является таблица tariffs в PostgreSQL.
# При ошибке загрузки из БД используется fallback на значения по умолчанию
# (как было захардкожено раньше), чтобы бот не упал.

def load_yookassa_tariffs_from_db() -> Dict[str, Dict[str, str]]:
    """
    Загружает тарифы для ЮKassa из БД и возвращает dict вида:
    {
        "1m": {"amount": "100.00", "label": "1 месяц — 100 ₽"},
        ...
    }
    """
    tariffs: Dict[str, Dict[str, str]] = {}

    try:
        rows = db.get_tariffs_for_yookassa()
    except Exception as e:
        log.error(
            "[Tariffs] Failed to load Yookassa tariffs from DB, will use defaults: %r",
            e,
        )
        return {
            "1m": {
                "amount": "100.00",
                "label": "1 месяц — 100 ₽",
            },
            "3m": {
                "amount": "270.00",
                "label": "3 месяца — 270 ₽",
            },
            "6m": {
                "amount": "480.00",
                "label": "6 месяцев — 480 ₽",
            },
            "1y": {
                "amount": "840.00",
                "label": "1 год — 840 ₽",
            },
            "forever": {
                "amount": "1990.00",
                "label": "Навсегда — 1990 ₽",
            },
        }

    for row in rows:
        code = row.get("code")
        title = row.get("title")
        amount = row.get("yookassa_amount")

        if not code or title is None or amount is None:
            continue

        # amount из БД (NUMERIC) приводим к строке с двумя знаками после запятой
        try:
            amount_str = format(amount, ".2f")
        except Exception:
            amount_str = str(amount)

        # делаем подпись как раньше: "1 месяц — 100 ₽"
        try:
            amount_int = int(amount)
        except (ValueError, TypeError):
            amount_int = amount

        label = f"{title} — {amount_int} ₽"

        tariffs[code] = {
            "amount": amount_str,
            "label": label,
        }


    # Если по какой-то причине получилось пусто — тоже fallback
    if not tariffs:
        log.error("[Tariffs] Yookassa tariffs from DB are empty, using defaults.")
        return {
            "1m": {
                "amount": "100.00",
                "label": "1 месяц — 100 ₽",
            },
            "3m": {
                "amount": "270.00",
                "label": "3 месяца — 270 ₽",
            },
            "6m": {
                "amount": "480.00",
                "label": "6 месяцев — 480 ₽",
            },
            "1y": {
                "amount": "840.00",
                "label": "1 год — 840 ₽",
            },
            "forever": {
                "amount": "1990.00",
                "label": "Навсегда — 1990 ₽",
            },
        }

    return tariffs


def load_heleket_tariffs_from_db() -> Dict[str, Dict[str, str]]:
    """
    Загружает тарифы для Heleket из БД и возвращает dict вида:
    {
        "1m": {"amount": "1.00", "label": "1 месяц — 1 $"},
        ...
    }
    """
    tariffs: Dict[str, Dict[str, str]] = {}

    try:
        rows = db.get_tariffs_for_heleket()
    except Exception as e:
        log.error(
            "[Tariffs] Failed to load Heleket tariffs from DB, will use defaults: %r",
            e,
        )
        return {
            "1m": {
                "amount": "1.00",
                "label": "1 месяц — 1 $",
            },
            "3m": {
                "amount": "3.00",
                "label": "3 месяца — 3 $",
            },
            "6m": {
                "amount": "6.00",
                "label": "6 месяцев — 6 $",
            },
            "1y": {
                "amount": "12.00",
                "label": "1 год — 12 $",
            },
            "forever": {
                "amount": "25.00",
                "label": "Навсегда — 25 $",
            },
        }

    for row in rows:
        code = row.get("code")
        title = row.get("title")
        amount = row.get("heleket_amount")

        if not code or title is None or amount is None:
            continue

        try:
            amount_str = format(amount, ".2f")
        except Exception:
            amount_str = str(amount)

        # подпись в стиле: "1 месяц — 1 $"
        try:
            amount_int = int(amount)
        except (ValueError, TypeError):
            amount_int = amount

        label = f"{title} — {amount_int} $"

        tariffs[code] = {
            "amount": amount_str,
            "label": label,
        }


    if not tariffs:
        log.error("[Tariffs] Heleket tariffs from DB are empty, using defaults.")
        return {
            "1m": {
                "amount": "1.00",
                "label": "1 месяц — 1 $",
            },
            "3m": {
                "amount": "3.00",
                "label": "3 месяца — 3 $",
            },
            "6m": {
                "amount": "6.00",
                "label": "6 месяцев — 6 $",
            },
            "1y": {
                "amount": "12.00",
                "label": "1 год — 12 $",
            },
            "forever": {
                "amount": "25.00",
                "label": "Навсегда — 25 $",
            },
        }

    return tariffs


def load_points_tariffs_from_db() -> Dict[str, Dict[str, object]]:
    """
    Загружает тарифы для оплаты баллами из БД и возвращает dict вида:
    {
        "1m": {
            "label": "1 месяц — 100 баллов",
            "points_cost": 100,
            "duration_days": 30,
        },
        ...
    }
    Берём данные из таблицы tariffs (поле points_cost).
    """
    tariffs: Dict[str, Dict[str, object]] = {}

    try:
        rows = db.get_active_tariffs()
    except Exception as e:
        log.error(
            "[Tariffs] Failed to load points tariffs from DB: %r",
            e,
        )
        return tariffs  # без fallback, цены для баллов задаёшь в БД

    for row in rows:
        code = row.get("code")
        title = row.get("title")
        duration_days = row.get("duration_days")
        points_cost = row.get("points_cost")

        if not code or title is None or points_cost is None:
            continue

        try:
            points_int = int(points_cost)
        except (TypeError, ValueError):
            continue

        try:
            duration_int = int(duration_days)
        except (TypeError, ValueError):
            duration_int = 30

        label = f"{title} — {points_int} баллов"

        tariffs[code] = {
            "label": label,
            "points_cost": points_int,
            "duration_days": duration_int,
        }

    return tariffs


def build_tariff_keyboard_from_dict(
    tariffs: Dict[str, Dict[str, str]],
    prefix: str,
) -> InlineKeyboardMarkup:
    """
    Строит InlineKeyboardMarkup из словаря тарифов.
    prefix:
        - "pay"     -> callback_data="pay:tariff:<code>"
        - "heleket" -> callback_data="heleket:tariff:<code>"
    """
    inline_keyboard: List[List[InlineKeyboardButton]] = []

    for code, tariff in tariffs.items():
        label = tariff.get("label") or code
        callback_data = f"{prefix}:tariff:{code}"

        inline_keyboard.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=callback_data,
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


# Загружаем тарифы из БД (или используем дефолты при ошибке)
TARIFFS = load_yookassa_tariffs_from_db()
HELEKET_TARIFFS = load_heleket_tariffs_from_db()

# Клавиатуры для оплаты
TARIFF_KEYBOARD = build_tariff_keyboard_from_dict(
    tariffs=TARIFFS,
    prefix="pay",
)

HELEKET_TARIFF_KEYBOARD = build_tariff_keyboard_from_dict(
    tariffs=HELEKET_TARIFFS,
    prefix="heleket",
)

# Тарифы и клавиатура для оплаты баллами
TARIFFS_POINTS = load_points_tariffs_from_db()

POINTS_TARIFF_KEYBOARD = build_tariff_keyboard_from_dict(
    tariffs=TARIFFS_POINTS,
    prefix="points",
)


# Кнопки оплаты и промокода (без Tribute, Heleket и демо-запроса)
SUBSCRIBE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🛒 Купить подписку",
                callback_data="pay:open",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🎮 Оплатить баллами",
                callback_data="points:open",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🤝 Пригласить друга",
                callback_data="ref:open_from_notify",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🎟 Ввести промокод",
                callback_data="promo:open",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🌐 Открыть сайт",
                url="https://maxnetvpn.ru",
            ),
        ],
    ]
)

# Клавиатура только для /start: без реферала и сайта, чтобы не отвлекать от trial/покупки (P0 UX)
START_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🛒 Купить подписку",
                callback_data="pay:open",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🎮 Оплатить баллами",
                callback_data="points:open",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🎟 Ввести промокод",
                callback_data="promo:open",
            ),
        ],
    ]
)


REF_SHARE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🤝 Пригласить друга",
                callback_data="ref:open_from_ref",
            ),
        ],
    ]
)

# Клавиатура для экрана /subscription: оплата главная, реферал второстепенный (P0 UX)
SUBSCRIPTION_PAGE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🛒 Купить подписку",
                callback_data="pay:open",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🎮 Оплатить баллами",
                callback_data="points:open",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🤝 Пригласить друга",
                callback_data="ref:open_from_ref",
            ),
        ],
    ]
)

REF_TRIAL_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text=REF_TRIAL_BUTTON_TEXT,
                callback_data="ref_trial:claim",
            ),
        ],
    ]
)

POINTS_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Оплатить баллами", callback_data="points:open")],
        [InlineKeyboardButton(text="🤝 Пригласить друга", callback_data="ref:open_from_notify")],
    ]
)


def get_start_keyboard(telegram_user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для /start: только главные действия (trial, купить, баллы, промо). Без реферала и сайта (P0 UX)."""
    if not db.user_can_claim_referral_trial(telegram_user_id):
        return START_KEYBOARD
    rows = [
        [
            InlineKeyboardButton(
                text=REF_TRIAL_BUTTON_TEXT,
                callback_data="ref_trial:claim",
            ),
        ],
    ] + [r for r in START_KEYBOARD.inline_keyboard]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Клавиатура для напоминаний / окончания подписки
SUBSCRIPTION_RENEW_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔁 Продлить подписку",
                callback_data="pay:open",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🎮 Продлить баллами",
                callback_data="points:open",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🤝 Пригласить друга",
                callback_data="ref:open_from_notify",
            ),
        ],
    ]
)

# Клавиатура для /status (упрощённая). sub_id в кнопке — гарантирует тот же конфиг, что в статусе.
def get_status_keyboard(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📱 Получить настройки",
                    callback_data=f"config:resend:{sub_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔁 Продлить подписку",
                    callback_data="pay:open",
                ),
                InlineKeyboardButton(
                    text="🎮 Продлить баллами",
                    callback_data="points:open",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🤝 Пригласить друга",
                    callback_data="ref:open_from_notify",
                ),
            ],
        ]
    )

REF_TRIAL_BUTTON_CALLBACK = "ref_trial:claim"

START_TEXT = (
    "MaxNet VPN | Быстрый VPN на WireGuard\n\n"
    "⚡ Подключение к серверам в Европе\n"
    "🔐 Шифрование трафика для работы и личных задач\n"
    "📲 Профили WireGuard для телефона и ПК\n"
    "🤖 Автоматическая выдача доступа через бота и автодеактивация по окончании срока\n\n"
    "🤝 Приглашай друзей — они получают пробный доступ, а ты копишь баллы и продлеваешь VPN.\n"
    "Твоя реферальная ссылка и статистика: команда /ref\n\n"
    "Чтобы подключить VPN или пригласить друга, воспользуйся кнопками ниже 👇\n\n"
    "🌐 Официальный сайт: https://maxnetvpn.ru\n\n"
    "Используя бота MaxNet VPN, ты подтверждаешь, что ознакомился и согласен с "
    "Пользовательским соглашением (/terms) и Политикой конфиденциальности (/privacy)."
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """
    Стартовый экран бота.

    Если команда /start пришла с параметром (deep-link / реферальный код),
    пытаемся зарегистрировать переход по реферальной ссылке.
    При успешной регистрации и если у пользователя нет подписки,
    выдаём пробный реферальный доступ на 7 дней.
    """
    user = message.from_user
    log.info("[Start] cmd_start tg_id=%s has_param=%s", user.id if user else None, bool(message.text and len((message.text or "").split(maxsplit=1)) > 1))

    # Пытаемся вытащить параметр после /start (deep-link)
    text = message.text or ""
    parts = text.split(maxsplit=1)
    start_param = None
    if len(parts) == 2:
        start_param = parts[1].strip()

    if user is not None and start_param:
        try:
            # 1. Пытаемся получить собственный реферальный код пользователя
            #    (используем тот же метод, что и в /ref)
            try:
                ref_info = db.get_or_create_referral_info(
                    telegram_user_id=user.id,
                    telegram_username=user.username,
                )
                own_ref_code = ref_info.get("ref_code")
            except Exception as e:
                log.error(
                    "[Referral] Failed to get own ref_code for tg_id=%s: %r",
                    user.id,
                    e,
                )
                own_ref_code = None

            # 2. Если параметр /start совпадает с его собственным реф-кодом —
            #    НЕ регистрируем переход по реферальной ссылке
            if own_ref_code and own_ref_code == start_param:
                log.info(
                    "[Referral] Skip self-referral for tg_id=%s ref_code=%s",
                    user.id,
                    start_param,
                )
            else:
                # 3. Регистрируем факт старта по реферальному коду (реферер определяется сразу)
                reg_res = db.register_referral_start(
                    invited_telegram_user_id=user.id,
                    referral_code=start_param,
                    raw_start_param=text,
                )

                # 4. Если регистрация успешна — показываем onboarding и кнопку триала
                if reg_res and reg_res.get("ok"):
                    await message.answer(
                        REF_LINK_WELCOME_TEXT + "\n\n" + SUPPORT_DISCOVERY_TEXT,
                        reply_markup=REF_TRIAL_KEYBOARD,
                        parse_mode="HTML",
                    )
                    return
                # 4b. Уже есть реферер, но есть активная подписка — показываем onboarding,
                #     чтобы пользователь мог повторно получить конфиг
                if reg_res and reg_res.get("error") == "already_has_referrer":
                    active_sub = db.get_latest_subscription_for_telegram(
                        telegram_user_id=user.id,
                    )
                    if active_sub and active_sub.get("active"):
                        await message.answer(
                            REF_LINK_WELCOME_TEXT + "\n\n" + SUPPORT_DISCOVERY_TEXT,
                            reply_markup=REF_TRIAL_KEYBOARD,
                            parse_mode="HTML",
                        )
                        return
                log.info(
                    "[Referral] register_referral_start returned not ok for tg_id=%s param=%r res=%r",
                    user.id,
                    start_param,
                    reg_res,
                )

        except Exception as e:
            log.error(
                "[Referral] Failed to register referral start tg_id=%s param=%r: %r",
                user.id,
                start_param,
                e,
            )

    await message.answer(
        START_TEXT + "\n\n" + SUPPORT_DISCOVERY_TEXT,
        reply_markup=get_start_keyboard(user.id if user else 0),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        HELP_INSTRUCTION,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


SUPPORT_TEXT = (
    "Если что-то пошло не так с оплатой или подключением VPN,\n"
    "ты можешь написать в поддержку:\n\n"
    "• @MaxNet_VPN_Support\n\n"
    "Опиши проблему, укажи свой @username и, по возможности, приложи скриншоты."
)


REF_INFO_TEXT = (
    "🤝 <b>Реферальная программа MaxNet VPN</b>\n\n"
    "Приглашай друзей по своей ссылке и получай баллы, которыми можно оплачивать подписку.\n\n"
    "<b>Как это работает</b>\n"
    "1. В команде /ref ты получаешь личную реферальную ссылку.\n"
    "2. Друг переходит по ссылке и нажимает <b>Start</b> в боте.\n"
    "3. Друг нажимает кнопку «Получить тестовый доступ» — получает 7 дней пробного периода "
    "(если у него ещё нет активной подписки и он ранее не получал реферальный триал).\n"
    "4. Когда приглашённый пользователь оплачивает подписку, ты и вышестоящие партнёры "
    "получаете баллы.\n\n"
    "<b>Бонус для приглашённого</b>\n"
    "• 7 дней пробного доступа по реферальной ссылке (по нажатию кнопки).\n"
    "• Пробный период выдаётся только один раз и не выдаётся, "
    "если уже есть активная подписка.\n\n"
    "<b>Баллы и уровни</b>\n"
    "Баллы начисляются за оплаты приглашённых пользователей по партнёрским уровням "
    "(до 5 линий). Конкретные размеры вознаграждений зависят от тарифа и уровня и "
    "могут меняться.\n\n"
    "<b>Что можно делать с баллами</b>\n"
    "• Оплачивать баллами подписку (полностью или частично — по условиям сервиса).\n"
    "• Баллы не обмениваются на деньги и не выводятся за пределы сервиса.\n\n"
    "<b>Ограничения и антифрод</b>\n"
    "• Нельзя быть рефералом самому себе.\n"
    "• У пользователя может быть только один реферер.\n"
    "• Попытки накрутки, использование фейковых аккаунтов, массовых регистраций и любые "
    "подозрительные схемы могут привести к отключению участия в реферальной программе и "
    "обнулению баллов.\n\n"
    "Подробная статистика по приглашённым и уровням доступна в команде /ref."
)


@router.message(Command("terms"))
async def cmd_terms(message: Message) -> None:
    try:
        with TERMS_FILE_PATH.open("r", encoding="utf-8") as f:
            terms_text = f.read()
    except Exception as e:
        log.error("Failed to read TERMS.md: %s", repr(e))
        await message.answer(
            "Не удалось прочитать файл TERMS.md. Сообщи, пожалуйста, админу.",
            disable_web_page_preview=True,
        )
        return

    max_len = 3900
    if len(terms_text) <= max_len:
        await message.answer(
            terms_text,
            parse_mode=None,
            disable_web_page_preview=True,
        )
    else:
        for i in range(0, len(terms_text), max_len):
            await message.answer(
                terms_text[i : i + max_len],
                parse_mode=None,
                disable_web_page_preview=True,
            )

    try:
        doc = FSInputFile(str(TERMS_FILE_PATH))
        await message.answer_document(
            document=doc,
            caption="Полная версия пользовательского соглашения в файле TERMS.md",
        )
    except Exception as e:
        log.error("Failed to send TERMS.md: %s", repr(e))
        await message.answer(
            "Не удалось отправить файл TERMS.md. Сообщи, пожалуйста, админу.",
            disable_web_page_preview=True,
        )


@router.message(Command("privacy"))
async def cmd_privacy(message: Message) -> None:
    try:
        with PRIVACY_FILE_PATH.open("r", encoding="utf-8") as f:
            privacy_text = f.read()
    except Exception as e:
        log.error("Failed to read PRIVACY.md: %s", repr(e))
        await message.answer(
            "Не удалось прочитать файл PRIVACY.md. Сообщи, пожалуйста, админу.",
            disable_web_page_preview=True,
        )
        return

    await message.answer(
        privacy_text,
        parse_mode=None,
        disable_web_page_preview=True,
    )

    try:
        doc = FSInputFile(str(PRIVACY_FILE_PATH))
        await message.answer_document(
            document=doc,
            caption="Полная версия политики конфиденциальности в файле PRIVACY.md",
        )
    except Exception as e:
        log.error("Failed to send PRIVACY.md: %s", repr(e))
        await message.answer(
            "Не удалось отправить файл PRIVACY.md. Сообщи, пожалуйста, админу.",
            disable_web_page_preview=True,
        )



ADMIN_INFO_TEXT = (
    "🛠 <b>Админ-команды MaxNet VPN</b>\n\n"

    "/admin_cmd — меню админа с кнопками.\n"
    "/admin_info — это описание команд.\n\n"
    "/admin_last — показать последнюю подписку.\n"
    "/admin_list — последние N подписок.\n"
    "/admin_sub &lt;id&gt; — показать подписку по ID с кнопками.\n\n"
    "/admin_activate &lt;id&gt; — активировать подписку и добавить peer в WireGuard.\n"
    "/admin_deactivate &lt;id&gt; — деактивировать подписку и удалить peer.\n"
    "/admin_delete &lt;id&gt; — полностью удалить подписку из БД и из WireGuard.\n\n"
    "/admin_regenerate_vpn &lt;telegram_user_id&gt; — восстановить VPN-доступ: новые ключи WG, тот же IP, конфиг отправить пользователю в TG.\n"
    "/admin_resend_config &lt;telegram_user_id&gt; — переотправить текущий конфиг без перегенерации ключей.\n\n"
    "/add_sub — выдать подписку вручную (подарок/ручной доступ).\n"
    "После /add_sub бот попросит переслать сообщение от пользователя и выбрать срок подписки.\n\n"
    "/broadcast — отправить текстовую рассылку всем пользователям.\n"
    "/broadcast_list — рассылка по списку ID из файла (один telegram_user_id на строку).\n"
    "/bonus_list — файл с ID: каждому +100 баллов и отправка сообщения.\n\n"
    "/promo_admin — сгенерировать SQL для вставки промокодов в таблицу promo_codes.\n\n"
    "/admin_stats — диагностика IP-пула и активных подписок.\n"
    "/support_stats — статистика AI-support за 24ч (intents, source, vpn_diagnosis).\n"
    "/crm_report [дней] — CRM-отчёт по воронке (по умолчанию 7 дней)."
)



def is_admin(message: Message) -> bool:
    """
    Проверяем, что команда пришла от администратора.
    ID администратора берём из настроек (ADMIN_TELEGRAM_ID).

    Важно:
    - для обычных команд (/admin_last, /admin_list, ...) проверяем, что это именно админ;
    - для сообщений бота (которые вызываются из инлайн-кнопок) считаем их "админскими",
      потому что реальный админ уже проверен в callback-хендлере.
    """
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)

    if admin_id == 0 or message.from_user is None:
        return False

    # обычный случай: команда напрямую от админа
    if message.from_user.id == admin_id:
        return True

    # случай, когда handler вызывается на сообщении бота (message.from_user.is_bot = True),
    # но сюда мы попадаем только из inline-хендлеров, где уже проверен callback.from_user.id == admin_id
    if message.from_user.is_bot:
        return True

    return False


async def send_admin_stats(message: Message) -> None:
    try:
        stats = db.get_admin_stats()
    except Exception as e:
        log.error("[AdminStats] Failed to get admin stats: %r", e)
        await message.answer("Не удалось получить статистику. См. логи.")
        return

    text = (
        "📊 <b>Admin stats</b>\n\n"
        "<pre>"
        "IP pool:\n"
        f"  total:      {stats['pool_total']}\n"
        f"  allocated:  {stats['pool_allocated']}\n"
        f"  free:       {stats['pool_free']}\n\n"
        "Subscriptions:\n"
        f"  active_subs: {stats['active_subs']}\n"
        f"  active_ips:  {stats['active_ips']}\n\n"
        "Consistency:\n"
        f"  subs_with_ip_not_in_pool:      {stats['subs_with_ip_not_in_pool']}\n"
        f"  allocated_without_active_sub:  {stats['allocated_without_active_sub']}\n"
        "</pre>"
    )

    await message.answer(
        text,
        disable_web_page_preview=True,
    )



@router.message(Command("support"))
async def cmd_support(message: Message) -> None:
    await message.answer(
        SUPPORT_TEXT,
        disable_web_page_preview=True,
    )

@router.message(Command("my_id"))
async def cmd_my_id(message: Message) -> None:
    uid = message.from_user.id if message.from_user else None
    text = (
        MY_ID_RESPONSE_TEMPLATE.format(id=uid)
        if uid
        else MY_ID_UNAVAILABLE
    )
    await message.answer(text, disable_web_page_preview=True)

@router.message(Command("subscription"))
async def cmd_subscription(message: Message) -> None:
    # Подтягиваем тарифы из БД
    try:
        tariffs = db.get_active_tariffs()
    except Exception as e:
        log.error("[Subscription] Failed to load tariffs from DB: %s", repr(e))
        tariffs = []

    lines = []

    # Шапка
    lines.append(PRICING_HEADER)

    if not tariffs:
        # fallback, если с БД что-то не так
        lines.append(TARIFFS_UNAVAILABLE)
    else:
        # Формируем список тарифов из таблицы tariffs
        for t in tariffs:
            title = t.get("title") or ""
            amount = t.get("yookassa_amount")

            # Красиво приводим сумму к целому числу, как у тебя было "100 ₽"
            if amount is not None:
                try:
                    amount_str = str(int(amount))
                except (ValueError, TypeError):
                    amount_str = str(amount)
            else:
                amount_str = "?"

            line = f"🔹 <b>{title}</b> — <b>{amount_str} ₽</b>"
            lines.append(line)

        lines.append("")  # пустая строка между списком и хвостом

    # Приклеиваем хвост (об экономии, способах оплаты и т.д.)
    lines.append(SUBSCRIPTION_TEXT)

    text = "\n".join(lines)

    await message.answer(
        text,
        disable_web_page_preview=True,
        reply_markup=SUBSCRIPTION_PAGE_KEYBOARD,
    )


@router.callback_query(F.data == "subscription:open")
async def subscription_open_callback(callback: CallbackQuery) -> None:
    # просто переиспользуем уже готовый хендлер
    await cmd_subscription(callback.message)
    await callback.answer()


@router.message(Command("promo_code"))
async def cmd_promo_code(message: Message, state: FSMContext) -> None:
    """
    Запускает диалог ввода промокода.
    Промокод добавляет дополнительные дни к подписке или выдаёт новую.
    """
    await state.set_state(PromoStates.waiting_for_code)
    await message.answer(
        "Отправь промокод одним сообщением.\n\n"
        "Промокод добавит дополнительные дни к твоей активной подписке, "
        "а если подписки ещё нет — выдаст новую на срок промокода.",
        disable_web_page_preview=True,
    )



@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    await message.answer(
        "Выбери тариф для оплаты через банковскую карту (ЮKassa):",
        reply_markup=TARIFF_KEYBOARD,
        disable_web_page_preview=True,
    )


@router.message(Command("buy_points"))
async def cmd_buy_points(message: Message) -> None:
    await message.answer(
        "Выбери тариф для оплаты баллами (игровой баланс):",
        reply_markup=POINTS_TARIFF_KEYBOARD,
        disable_web_page_preview=True,
    )


@router.message(Command("buy_crypto"))
async def cmd_buy_crypto(message: Message) -> None:
    await message.answer(
        "Выбери тариф для оплаты криптовалютой (Heleket):",
        reply_markup=HELEKET_TARIFF_KEYBOARD,
        disable_web_page_preview=True,
    )

@router.callback_query(F.data == "pay:open")
async def pay_open_callback(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "Выбери тариф для оплаты через банковскую карту (ЮKassa):",
        reply_markup=TARIFF_KEYBOARD,
        disable_web_page_preview=True,
    )
    await callback.answer()
    
    
@router.callback_query(F.data == "withdraw:open")
async def withdraw_open_callback(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "Данный раздел находится в разработке.",
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data == "points:open")
async def points_open_callback(callback: CallbackQuery) -> None:
    user = callback.from_user
    if user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    # Получаем баланс пользователя
    balance = db.get_user_points_balance(user.id)

    # Находим минимальную стоимость тарифа
    min_cost = None
    for tariff in TARIFFS_POINTS.values():
        cost = tariff.get("points_cost")
        if cost is not None and (min_cost is None or cost < min_cost):
            min_cost = cost

    if min_cost is None:
        min_cost = 100  # fallback

    if balance < min_cost:
        await callback.message.answer(
            f"💰 Твой баланс: <b>{pluralize_points(balance)}</b>\n\n"
            f"❌ Недостаточно баллов для оплаты подписки.\n"
            f"Минимальная стоимость: <b>{pluralize_points(min_cost)}</b>.\n\n"
            "Приглашай друзей по реферальной ссылке (/ref) и получай баллы за их оплаты!",
            disable_web_page_preview=True,
        )
        await callback.answer()
        return

    await callback.message.answer(
        f"💰 Твой баланс: <b>{pluralize_points(balance)}</b>\n\n"
        "Выбери тариф для оплаты баллами:",
        reply_markup=POINTS_TARIFF_KEYBOARD,
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data == "heleket:open")
async def heleket_open_callback(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "Выбери тариф для оплаты криптовалютой (Heleket):",
        reply_markup=HELEKET_TARIFF_KEYBOARD,
        disable_web_page_preview=True,
    )
    await callback.answer()
    
    
@router.callback_query(F.data == "promo:open")
async def promo_open_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Открывает диалог ввода промокода по кнопке из главного меню.
    Использует то же состояние, что и команда /promo_code.
    """
    await state.set_state(PromoStates.waiting_for_code)
    await callback.message.answer(
        "Отправь промокод одним сообщением.\n\n"
        "Промокод добавит дополнительные дни к твоей активной подписке, "
        "а если подписки ещё нет — выдаст новую на срок промокода.",
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.message(Command("demo"))
async def cmd_demo(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    user_id = user.id

    # Проверяем, есть ли активная подписка
    active_sub = db.get_latest_subscription_for_telegram(telegram_user_id=user_id)
    if active_sub:
        expires_at = active_sub.get("expires_at")
        if isinstance(expires_at, datetime):
            expires_str = fmt_date(expires_at, with_time=False)
        else:
            expires_str = str(expires_at)
        await message.answer(
            f"У тебя уже есть активная подписка до <b>{expires_str}</b>.\n\n"
            "Демо-доступ не требуется.",
            disable_web_page_preview=True,
        )
        return

    # Проверяем, получал ли пользователь демо ранее
    had_demo = db.has_demo_subscription(telegram_user_id=user_id)
    if had_demo:
        await message.answer(
            "Ты уже получал демо-доступ ранее.\n\n"
            "Для продолжения использования VPN оформи подписку через /buy или /buy_crypto.",
            disable_web_page_preview=True,
        )
        return

    await state.set_state(DemoRequest.waiting_for_message)
    await message.answer(
        "Ты можешь запросить тестовый демо-доступ к MaxNet VPN.\n\n"
        "Напиши в одном сообщении, зачем тебе нужен доступ и как планируешь использовать VPN "
        "(например: «хочу протестировать скорость и стабильность», «нужно временно для поездки», "
        "«показать сервис друзьям»).\n\n"
        "Я перешлю твой текст админу, и он решит, выдавать ли демо-доступ.",
        disable_web_page_preview=True,
    )

@router.callback_query(PromoAdmin.waiting_for_mode, F.data.startswith("promo_admin:mode:"))
async def promo_admin_choose_mode(callback: CallbackQuery, state: FSMContext) -> None:
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)
    if callback.from_user is None or callback.from_user.id != admin_id:
        await callback.answer("Эта кнопка только для администратора.", show_alert=True)
        return

    data = callback.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    _, _, mode = parts
    if mode not in ("multi", "single"):
        await callback.answer("Неизвестный режим промокода.", show_alert=True)
        return

    await state.update_data(mode=mode)

    # убираем клаву выбора режима
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        log.error("[PromoAdmin] Failed to clear mode keyboard: %s", repr(e))

    await state.set_state(PromoAdmin.waiting_for_extra_days)
    await callback.message.answer(
        "Шаг 1.\n\n"
        "Сколько <b>дополнительных дней</b> даёт промокод?\n"
        "Отправь целое число &gt; 0 (например: <code>7</code>).",
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.message(PromoAdmin.waiting_for_extra_days)
async def promo_admin_extra_days(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        await state.clear()
        return

    text = (message.text or "").strip()
    try:
        extra_days = int(text)
    except ValueError:
        await message.answer(
            "Нужно целое число дней &gt; 0. Например: <code>7</code>.",
            disable_web_page_preview=True,
        )
        return

    if extra_days <= 0:
        await message.answer(
            "Число дней должно быть &gt; 0. Попробуй ещё раз.",
            disable_web_page_preview=True,
        )
        return

    await state.update_data(extra_days=extra_days)
    await state.set_state(PromoAdmin.waiting_for_valid_days)
    await message.answer(
        "Шаг 2.\n\n"
        "На сколько дней сделать промокод <b>действительным</b> с текущего момента?\n"
        "Отправь целое число дней (например: <code>30</code>).\n"
        "Если хочешь без ограничения по дате — отправь <code>0</code>.",
        disable_web_page_preview=True,
    )


@router.message(PromoAdmin.waiting_for_valid_days)
async def promo_admin_valid_days(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        await state.clear()
        return

    text = (message.text or "").strip()
    try:
        valid_days = int(text)
    except ValueError:
        await message.answer(
            "Нужно целое число дней (0 или больше). Например: <code>30</code> или <code>0</code>.",
            disable_web_page_preview=True,
        )
        return

    if valid_days < 0:
        await message.answer(
            "Число дней не может быть отрицательным. Попробуй ещё раз.",
            disable_web_page_preview=True,
        )
        return

    await state.update_data(valid_days=valid_days)
    data = await state.get_data()
    mode = data.get("mode")

    if mode == "single":
        await state.set_state(PromoAdmin.waiting_for_code_count)
        await message.answer(
            "Шаг 3.\n\n"
            "Сколько <b>одноразовых</b> промокодов нужно сгенерировать?\n"
            "Отправь целое число &gt; 0 (например: <code>20</code>).",
            disable_web_page_preview=True,
        )

    elif mode == "multi":
        await state.set_state(PromoAdmin.waiting_for_manual_code)
        await message.answer(
            "Шаг 3.\n\n"
            "Введи <b>имя многоразового промокода</b>.\n"
            "Допускаются буквы/цифры, пробелы будут автоматически заменены на подчёркивания.\n"
            "Например: <code>MAXNET7DAYS</code> или <code>MAXNET FRIENDS</code>.",
            disable_web_page_preview=True,
        )

    else:
        await message.answer(
            "Режим промокода не определён. Начни заново с /promo_admin.",
            disable_web_page_preview=True,
        )
        await state.clear()

@router.message(PromoAdmin.waiting_for_code_count)
async def promo_admin_code_count(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        await state.clear()
        return

    text = (message.text or "").strip()
    try:
        code_count = int(text)
    except ValueError:
        await message.answer(
            "Нужно целое число &gt; 0. Например: <code>20</code>.",
            disable_web_page_preview=True,
        )
        return

    if code_count <= 0:
        await message.answer(
            "Число кодов должно быть &gt; 0. Попробуй ещё раз.",
            disable_web_page_preview=True,
        )
        return

    await state.update_data(code_count=code_count)
    await state.set_state(PromoAdmin.waiting_for_comment)
    await message.answer(
        "Шаг 4.\n\n"
        "Добавь комментарий для этих промокодов (для себя / других админов).\n"
        "Например: <code>Розыгрыш в чате 01.03</code>.\n\n"
        "Если комментарий не нужен — отправь <code>-</code>.",
        disable_web_page_preview=True,
    )


@router.message(PromoAdmin.waiting_for_manual_code)
async def promo_admin_manual_code(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        await state.clear()
        return

    manual_code = (message.text or "").strip()
    if not manual_code:
        await message.answer(
            "Имя промокода не должно быть пустым. Введи что-нибудь, например: <code>MAXNET7DAYS</code>.",
            disable_web_page_preview=True,
        )
        return

    await state.update_data(manual_code=manual_code)
    await state.set_state(PromoAdmin.waiting_for_max_uses)
    await message.answer(
        "Шаг 4.\n\n"
        "Укажи <b>общий лимит использований</b> этого промокода.\n"
        "Например: <code>100</code>.\n"
        "Если не хочешь ограничивать общее число применений — отправь <code>0</code>.",
        disable_web_page_preview=True,
    )


@router.message(PromoAdmin.waiting_for_max_uses)
async def promo_admin_max_uses(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        await state.clear()
        return

    text = (message.text or "").strip()
    try:
        max_uses_raw = int(text)
    except ValueError:
        await message.answer(
            "Нужно целое число ≥ 0. Например: <code>100</code> или <code>0</code>.",
            disable_web_page_preview=True,
        )
        return

    if max_uses_raw < 0:
        await message.answer(
            "Число не может быть отрицательным. Попробуй ещё раз.",
            disable_web_page_preview=True,
        )
        return

    max_uses = None if max_uses_raw == 0 else max_uses_raw
    await state.update_data(max_uses=max_uses)

    await state.set_state(PromoAdmin.waiting_for_per_user_limit)
    await message.answer(
        "Шаг 5.\n\n"
        "Сколько раз <b>один пользователь</b> может применить этот промокод?\n"
        "Отправь целое число &gt; 0. Например: <code>1</code>.",
        disable_web_page_preview=True,
    )


@router.message(PromoAdmin.waiting_for_per_user_limit)
async def promo_admin_per_user_limit(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        await state.clear()
        return

    text = (message.text or "").strip()
    try:
        per_user_limit = int(text)
    except ValueError:
        await message.answer(
            "Нужно целое число &gt; 0. Например: <code>1</code> или <code>3</code>.",
            disable_web_page_preview=True,
        )
        return

    if per_user_limit <= 0:
        await message.answer(
            "Число должно быть &gt; 0. Попробуй ещё раз.",
            disable_web_page_preview=True,
        )
        return

    await state.update_data(per_user_limit=per_user_limit)
    await state.set_state(PromoAdmin.waiting_for_comment)
    await message.answer(
        "Шаг 6.\n\n"
        "Добавь комментарий для этого промокода (для себя / других админов).\n"
        "Например: <code>Промо-день рождения сервиса</code>.\n\n"
        "Если комментарий не нужен — отправь <code>-</code>.",
        disable_web_page_preview=True,
    )

@router.message(PromoAdmin.waiting_for_comment)
async def promo_admin_comment_and_generate(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        await state.clear()
        return

    # сохраняем комментарий в state
    comment_raw = (message.text or "").strip()
    comment = None if comment_raw == "-" else comment_raw
    await state.update_data(comment=comment)

    data = await state.get_data()
    mode = data.get("mode")
    extra_days = data.get("extra_days")
    valid_days = data.get("valid_days")

    if extra_days is None or valid_days is None or mode not in ("single", "multi"):
        await message.answer(
            "Не удалось собрать параметры промокода. Начни заново с /promo_admin.",
            disable_web_page_preview=True,
        )
        await state.clear()
        return

    # готовим человекочитаемое описание срока действия
    if valid_days == 0:
        valid_text = "без ограничения по дате (неограниченный срок действия)"
    else:
        valid_text = f"{valid_days} дн. с момента создания"

    summary_lines = [
        "🧩 <b>Параметры промокода</b>\n",
        f"• Дополнительные дни подписки: <b>{extra_days}</b>",
        f"• Срок действия промокода: <b>{valid_text}</b>",
    ]

    if mode == "single":
        code_count = data.get("code_count")
        if not code_count:
            await message.answer(
                "Не найдено количество одноразовых кодов. Начни заново с /promo_admin.",
                disable_web_page_preview=True,
            )
            await state.clear()
            return

        summary_lines.append("• Тип: <b>несколько одноразовых кодов</b>")
        summary_lines.append(f"• Количество кодов: <b>{code_count}</b>")
    else:
        manual_code = data.get("manual_code")
        max_uses = data.get("max_uses")
        per_user_limit = data.get("per_user_limit")

        if not manual_code or per_user_limit is None:
            await message.answer(
                "Не все параметры многоразового промокода заданы. Начни заново с /promo_admin.",
                disable_web_page_preview=True,
            )
            await state.clear()
            return

        if max_uses is None:
            max_uses_text = "без ограничения по общему числу использований"
        else:
            max_uses_text = f"{max_uses} раз"

        summary_lines.append("• Тип: <b>многоразовый промокод</b>")
        summary_lines.append(f"• Имя промокода: <code>{manual_code}</code>")
        summary_lines.append(f"• Общий лимит использований: <b>{max_uses_text}</b>")
        summary_lines.append(
            f"• Лимит на одного пользователя: <b>{per_user_limit} раз(а)</b>"
        )

    if comment:
        summary_lines.append(f"• Комментарий: <i>{comment}</i>")
    else:
        summary_lines.append("• Комментарий: <i>нет</i>")

    text = (
        "\n".join(summary_lines)
        + "\n\n"
        "Если всё верно — подтверди генерацию промокодов.\n"
        "Или отменись, если нужно начать заново."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Сгенерировать и сохранить в БД",
                    callback_data="promo_admin:confirm:yes",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data="promo_admin:confirm:cancel",
                ),
            ],
        ]
    )

    await state.set_state(PromoAdmin.waiting_for_confirm)
    await message.answer(
        text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@router.callback_query(PromoAdmin.waiting_for_confirm, F.data.startswith("promo_admin:confirm:"))
async def promo_admin_confirm_callback(callback: CallbackQuery, state: FSMContext) -> None:
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)
    if callback.from_user is None or callback.from_user.id != admin_id:
        await callback.answer("Эта кнопка только для администратора.", show_alert=True)
        return

    data_raw = callback.data or ""
    parts = data_raw.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    _, _, action = parts

    # убираем клавиатуру подтверждения
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        log.error("[PromoAdmin] Failed to clear confirm keyboard: %s", repr(e))

    if action == "cancel":
        await state.clear()
        await callback.message.answer(
            "Генерация промокодов отменена.\n"
            "Если нужно — запусти мастер заново командой /promo_admin.",
            disable_web_page_preview=True,
        )
        await callback.answer("Отменено.")
        return

    if action != "yes":
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    # action == "yes" — реально генерируем промокоды и пишем в БД
    data = await state.get_data()
    mode = data.get("mode")
    extra_days = data.get("extra_days")
    valid_days = data.get("valid_days")
    comment = data.get("comment")

    if extra_days is None or valid_days is None or mode not in ("single", "multi"):
        await callback.message.answer(
            "Не удалось собрать параметры промокода. Начни заново с /promo_admin.",
            disable_web_page_preview=True,
        )
        await state.clear()
        await callback.answer("Ошибка параметров.")
        return

    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", None)

    try:
        if mode == "single":
            code_count = data.get("code_count")
            if not code_count:
                await callback.message.answer(
                    "Не найдено количество одноразовых кодов. Начни заново с /promo_admin.",
                    disable_web_page_preview=True,
                )
                await state.clear()
                await callback.answer("Ошибка параметров.")
                return

            params = PromoGenerationParams(
                action_type="extra_days",
                extra_days=extra_days,
                is_multi_use=False,
                code_count=code_count,
                manual_code=None,
                valid_days=valid_days,
                max_uses=None,
                per_user_limit=1,
                tariff_scope="all",
                allowed_tariffs=None,
                allowed_telegram_id=None,
                comment=comment,
                created_by_admin_id=admin_id,
                code_length=10,
            )
        else:
            manual_code = data.get("manual_code")
            max_uses = data.get("max_uses")
            per_user_limit = data.get("per_user_limit")

            if not manual_code or per_user_limit is None:
                await callback.message.answer(
                    "Не все параметры многоразового промокода заданы. Начни заново с /promo_admin.",
                    disable_web_page_preview=True,
                )
                await state.clear()
                await callback.answer("Ошибка параметров.")
                return

            params = PromoGenerationParams(
                action_type="extra_days",
                extra_days=extra_days,
                is_multi_use=True,
                code_count=1,
                manual_code=manual_code,
                valid_days=valid_days,
                max_uses=max_uses,
                per_user_limit=per_user_limit,
                tariff_scope="all",
                allowed_tariffs=None,
                allowed_telegram_id=None,
                comment=comment,
                created_by_admin_id=admin_id,
                code_length=10,
            )
            
        promo_log.info(
            "[PromoAdmin] Start generate promo codes: mode=%s extra_days=%s valid_days=%s admin_id=%s params=%r",
            mode,
            extra_days,
            valid_days,
            admin_id,
            params,
        )

        promo_rows = generate_promo_codes(params)
        sql = build_insert_sql_for_postgres(promo_rows, table_name="promo_codes")
        promo_log.info(
            "[PromoAdmin] Generated promo rows: count=%s first_codes=%r",
            len(promo_rows),
            [row.get("code") for row in promo_rows[:5]],
        )

        db.execute_sql(sql)
        promo_log.info(
            "[PromoAdmin] Promo codes inserted into DB: count=%s",
            len(promo_rows),
        )


    except Exception as e:
        promo_log.error(
            "[PromoAdmin] Failed to generate promo codes on confirm: mode=%s extra_days=%s valid_days=%s admin_id=%s error=%r",
            mode,
            extra_days,
            valid_days,
            admin_id,
            e,
        )

        await callback.message.answer(
            "Произошла ошибка при генерации промокодов. Подробности смотри в логах.",
            disable_web_page_preview=True,
        )
        await state.clear()
        await callback.answer("Ошибка генерации.")
        return


    await state.clear()

    # Формируем информацию о сроке действия
    valid_until_row = promo_rows[0].get("valid_until") if promo_rows else None
    if valid_until_row:
        valid_until_str = fmt_date(valid_until_row)
        valid_info = f"⏰ Применить до: <b>{valid_until_str}</b>"
    else:
        valid_info = "⏰ Срок применения: <b>без ограничения</b>"

    # Информация о бонусе
    bonus_info = f"🎁 Даёт: <b>+{extra_days} дней</b> к подписке"

    # Комментарий
    comment_info = ""
    if comment and comment.strip() and comment.strip() != "-":
        comment_info = f"\n📝 Комментарий: <i>{comment}</i>"

    if mode == "single":
        codes_preview = "\n".join(row.get("code") for row in promo_rows)
        text = (
            f"✅ Сгенерировано и сохранено в базе <b>{len(promo_rows)}</b> одноразовых промокодов.\n\n"
            f"{bonus_info}\n"
            f"{valid_info}"
            f"{comment_info}\n\n"
            "Список кодов:\n"
            f"<code>{codes_preview}</code>"
        )
    else:
        code_preview = promo_rows[0].get("code")
        max_uses_info = ""
        if max_uses and max_uses > 0:
            max_uses_info = f"\n🔢 Макс. использований: <b>{max_uses}</b>"
        text = (
            "✅ Сгенерирован и сохранён в базе многоразовый промокод.\n\n"
            f"Код: <code>{code_preview}</code>\n\n"
            f"{bonus_info}\n"
            f"{valid_info}"
            f"{max_uses_info}"
            f"{comment_info}\n\n"
            "Промокод уже добавлен в таблицу <code>promo_codes</code> и готов к использованию."
        )

    await callback.message.answer(
        text,
        disable_web_page_preview=True,
    )
    await callback.answer("Промокоды созданы.")


@router.callback_query(F.data == "demo_request")
async def demo_request_button(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(DemoRequest.waiting_for_message)
    await callback.message.answer(
        "Ты можешь запросить тестовый демо-доступ к MaxNet VPN.\n\n"
        "Напиши в одном сообщении, зачем тебе нужен доступ и как планируешь использовать VPN "
        "(например: «хочу протестировать скорость и стабильность», «нужно временно для поездки», "
        "«показать сервис друзьям»).\n\n"
        "Я перешлю твой текст админу, и он решит, выдавать ли демо-доступ.",
        disable_web_page_preview=True,
    )
    await callback.answer()

@router.callback_query(F.data.startswith("pay:tariff:"))
async def pay_tariff_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    _, _, tariff_code = parts
    tariff = TARIFFS.get(tariff_code)

    if tariff is None:
        await callback.answer("Неизвестный тариф.", show_alert=True)
        return

    if callback.from_user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    telegram_user_id = callback.from_user.id
    telegram_user_name = getattr(callback.from_user, "username", None) if callback.from_user else None

    try:
        confirmation_url = create_yookassa_payment(
            telegram_user_id=telegram_user_id,
            tariff_code=tariff_code,
            amount=tariff["amount"],
            description=f"MaxNet VPN — {tariff['label']}",
            telegram_user_name=telegram_user_name,
        )
    except Exception as e:
        log.error(
            "[YooKassa] Failed to create payment for tg_id=%s tariff=%s: %s",
            telegram_user_id,
            tariff_code,
            repr(e),
        )
        await callback.answer("Ошибка при создании платежа. Попробуй позже.", show_alert=True)
        return

    pay_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Перейти к оплате",
                    url=confirmation_url,
                )
            ]
        ]
    )

    await callback.message.answer(
        "Перейди по кнопке ниже на защищённую платёжную страницу ЮKassa.\n\n"
        "После успешной оплаты бот автоматически выдаст доступ к VPN.",
        reply_markup=pay_keyboard,
        disable_web_page_preview=True,
    )

    await callback.answer()


@router.callback_query(F.data.startswith("points:tariff:"))
async def points_tariff_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    log.info(
        "[PointsPay] Received callback: data=%r from_user_id=%s",
        data,
        callback.from_user.id if callback.from_user else None,
    )

    parts = data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    _, _, tariff_code = parts
    tariff = TARIFFS_POINTS.get(tariff_code)

    if tariff is None:
        log.warning(
            "[PointsPay] Unknown tariff code %r in callback data=%r",
            tariff_code,
            data,
        )
        await callback.answer("Неизвестный тариф.", show_alert=True)
        return

    if callback.from_user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    telegram_user_id = callback.from_user.id

    points_cost = tariff.get("points_cost")
    duration_days = tariff.get("duration_days")

    try:
        points_cost_int = int(points_cost)
    except (TypeError, ValueError):
        log.error(
            "[PointsPay] Bad points_cost=%r for tariff_code=%s",
            points_cost,
            tariff_code,
        )
        await callback.answer("Некорректная цена тарифа в баллах.", show_alert=True)
        return

    try:
        duration_int = int(duration_days)
    except (TypeError, ValueError):
        log.warning(
            "[PointsPay] Bad duration_days=%r for tariff_code=%s, fallback 30",
            duration_days,
            tariff_code,
        )
        duration_int = 30

    # Мини-уведомление, чтобы пользователь видел, что что-то происходит
    try:
        await callback.answer("Проверяю баланс и оформляю подписку…")
    except Exception as e:
        log.warning(
            "[PointsPay] Failed to answer callback briefly for tg_id=%s: %r",
            telegram_user_id,
            e,
        )

    # Проверяем баланс
    try:
        balance = db.get_user_points_balance(telegram_user_id=telegram_user_id)
        log.info(
            "[PointsPay] Balance check: tg_id=%s balance=%s need=%s",
            telegram_user_id,
            balance,
            points_cost_int,
        )
    except Exception as e:
        log.error(
            "[PointsPay] Failed to get balance for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        await callback.message.answer(
            "❌ Не удалось получить баланс баллов. Попробуй позже или напиши в поддержку.",
            disable_web_page_preview=True,
        )
        return

    if balance < points_cost_int:
        await callback.answer(
            f"Недостаточно баллов: нужно {points_cost_int}, у тебя {balance}.",
            show_alert=True,
        )
        return

    # Вычисляем базовую дату окончания: либо с текущего момента,
    # либо от уже оплаченного срока, если он ещё в будущем.
    now_utc = datetime.now(timezone.utc)
    base_expires_at = now_utc

    latest_sub = None
    extend_existing = False
    reuse_priv = None
    reuse_pub = None
    reuse_ip = None

    try:
        latest_sub = db.get_latest_subscription_for_telegram(
            telegram_user_id=telegram_user_id,
        )
        log.info(
            "[PointsPay] Latest subscription for tg_id=%s: %r",
            telegram_user_id,
            latest_sub,
        )
    except Exception as e:
        log.error(
            "[PointsPay] Failed to get latest subscription for extend tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        latest_sub = None

    if latest_sub:
        old_expires_at = latest_sub.get("expires_at")

        # Нормализуем дату для расчёта продления
        if isinstance(old_expires_at, datetime):
            if old_expires_at.tzinfo is not None:
                old_expires_at = old_expires_at.astimezone(timezone.utc)
            else:
                old_expires_at = old_expires_at.replace(tzinfo=timezone.utc)

            # Если срок ещё в будущем — продлеваем от него
            if old_expires_at > base_expires_at:
                base_expires_at = old_expires_at

        # 🔁 ВАЖНОЕ ИЗМЕНЕНИЕ:
        # Если у последней подписки есть ключи и IP — переиспользуем их,
        # НЕ важно, активна она сейчас или уже деактивирована.
        if (
            latest_sub.get("wg_private_key")
            and latest_sub.get("wg_public_key")
            and latest_sub.get("vpn_ip")
        ):
            extend_existing = True
            reuse_priv = latest_sub.get("wg_private_key")
            reuse_pub = latest_sub.get("wg_public_key")
            reuse_ip = latest_sub.get("vpn_ip")

    # Выдаём подписку за баллы
    allocated_new_ip = False
    subscription_created = False
    try:
        client_priv = None
        client_pub = None
        client_ip = None
        send_config = True

        if extend_existing and reuse_priv and reuse_pub and reuse_ip:
            # Есть последняя подписка с валидными ключами/IP —
            # "оживляем" её конфиг (даже если она была деактивирована).
            # release_ips_to_pool=False — переиспользуем IP, не отдавать в пул.
            deactivate_existing_active_subscriptions(
                telegram_user_id=telegram_user_id,
                reason="auto_replace_points_payment",
                release_ips_to_pool=False,
            )

            client_priv = reuse_priv
            client_pub = reuse_pub
            client_ip = reuse_ip
            allowed_ip = f"{client_ip}/{settings.WG_CLIENT_NETWORK_CIDR}"

            log.info(
                "[PointsPay] Reuse existing peer (points) pubkey=%s ip=%s for tg_id=%s",
                client_pub,
                allowed_ip,
                telegram_user_id,
            )
            wg.add_peer(
                public_key=client_pub,
                allowed_ip=allowed_ip,
                telegram_user_id=telegram_user_id,
            )

            # Конфиг у пользователя уже есть, повторно не шлём
            send_config = False
        else:
            # Обычный путь: новая подписка за баллы, выдаём новый конфиг
            deactivate_existing_active_subscriptions(
                telegram_user_id=telegram_user_id,
                reason="auto_replace_points_payment",
                release_ips_to_pool=True,
            )

            client_priv, client_pub = wg.generate_keypair()
            client_ip = wg.generate_client_ip()
            allocated_new_ip = True
            allowed_ip = f"{client_ip}/{settings.WG_CLIENT_NETWORK_CIDR}"

            log.info(
                "[PointsPay] Add peer (points) pubkey=%s ip=%s for tg_id=%s",
                client_pub,
                allowed_ip,
                telegram_user_id,
            )
            wg.add_peer(
                public_key=client_pub,
                allowed_ip=allowed_ip,
                telegram_user_id=telegram_user_id,
            )

        # ВАЖНО: продлеваем от base_expires_at, а не от "сейчас"
        expires_at = base_expires_at + timedelta(days=duration_int)

        sub_id = db.insert_subscription(
            tribute_user_id=0,
            telegram_user_id=telegram_user_id,
            telegram_user_name=callback.from_user.username,
            subscription_id=0,
            period=f"points_{tariff_code}",
            period_id=0,
            channel_id=0,
            channel_name="Points balance",
            vpn_ip=client_ip,
            wg_private_key=client_priv,
            wg_public_key=client_pub,
            expires_at=expires_at,
            event_name=f"points_payment_{tariff_code}",
        )
        subscription_created = True

        log.info(
            "[PointsPay] Subscription created from points: sub_id=%s tg_id=%s ip=%s expires_at=%s",
            sub_id,
            telegram_user_id,
            client_ip,
            expires_at,
        )

        meta = {
            "tariff_code": tariff_code,
        }

        add_res = db.add_points(
            telegram_user_id=telegram_user_id,
            delta=-points_cost_int,
            reason="pay_tariff_points",
            source="points",
            related_subscription_id=sub_id,
            related_payment_id=None,
            level=None,
            meta=meta,
            allow_negative=False,
        )

        if not add_res.get("ok"):
            log.error(
                "[PointsPay] Failed to charge points for tg_id=%s sub_id=%s: %r",
                telegram_user_id,
                sub_id,
                add_res,
            )
            await callback.message.answer(
                "Подписка создана, но не удалось корректно списать баллы. "
                "Свяжись с поддержкой, чтобы уточнить баланс.",
                disable_web_page_preview=True,
            )
        else:
            log.info(
                "[PointsPay] Points charged: tg_id=%s sub_id=%s cost=%s new_balance=%s",
                telegram_user_id,
                sub_id,
                points_cost_int,
                add_res.get("balance"),
            )

        if send_config:
            recently_expired_trial = db.has_recently_expired_subscription(
                telegram_user_id, within_hours=48
            )
            if recently_expired_trial:
                try:
                    await send_trial_expired_paid_notification(telegram_user_id)
                except Exception as e:
                    log.warning(
                        "[PointsPay] Failed to send trial-expired-paid notification tg_id=%s: %r",
                        telegram_user_id,
                        e,
                    )
            config_text = wg.build_client_config(
                client_private_key=client_priv,
                client_ip=client_ip,
            )

            await send_vpn_config_to_user(
                telegram_user_id=telegram_user_id,
                config_text=config_text,
                caption=(
                    "Подписка MaxNet VPN оплачена баллами.\n\n"
                    "Файл vpn.conf — в этом сообщении. QR-код — в следующем."
                ),
            )
            if recently_expired_trial:
                try:
                    db.create_subscription_notification(
                        subscription_id=sub_id,
                        notification_type="recently_expired_trial_followup",
                        telegram_user_id=telegram_user_id,
                        expires_at=expires_at,
                    )
                except Exception as e:
                    log.warning(
                        "[PointsPay] Failed to register recently_expired_trial_followup sub_id=%s: %r",
                        sub_id,
                        e,
                    )
        else:
            log.info(
                "[PointsPay] Reused existing config for tg_id=%s sub_id=%s (no new config sent)",
                telegram_user_id,
                sub_id,
            )

        if isinstance(expires_at, datetime):
            expires_str = fmt_date(expires_at)
        else:
            expires_str = str(expires_at)

        await callback.message.answer(
            "✅ Подписка успешно оформлена за баллы.\n\n"
            f"Списано: <b>{points_cost_int}</b> баллов.\n"
            f"Срок действия до: <b>{expires_str}</b>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    except Exception as e:
        if allocated_new_ip and client_ip and not subscription_created:
            try:
                db.release_ip_in_pool(client_ip)
            except Exception:
                pass
        log.error(
            "[PointsPay] Failed to create subscription for tg_id=%s tariff=%s: %r",
            telegram_user_id,
            tariff_code,
            e,
        )
        await callback.message.answer(
            "❌ Произошла ошибка при оформлении подписки за баллы. "
            "Попробуй позже или напиши в поддержку.",
            disable_web_page_preview=True,
        )
        try:
            await callback.answer(
                "Ошибка при оформлении подписки за баллы.",
                show_alert=True,
            )
        except Exception:
            # если второй answer упадёт — просто игнорируем
            pass
        return


@router.callback_query(F.data.startswith("heleket:tariff:"))
async def heleket_tariff_callback(callback: CallbackQuery) -> None:
    data = callback.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    _, _, tariff_code = parts
    tariff = HELEKET_TARIFFS.get(tariff_code)

    if tariff is None:
        await callback.answer("Неизвестный тариф.", show_alert=True)
        return

    if callback.from_user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    telegram_user_id = callback.from_user.id

    try:
        payment_url = create_heleket_payment(
            telegram_user_id=telegram_user_id,
            tariff_code=tariff_code,
            amount=tariff["amount"],
            description=f"MaxNet VPN — {tariff['label']}",
        )
    except Exception as e:
        log.error(
            "[Heleket] Failed to create payment for tg_id=%s tariff=%s: %s",
            telegram_user_id,
            tariff_code,
            repr(e),
        )
        await callback.answer(
            "Ошибка при создании крипто-платежа. Попробуй позже.",
            show_alert=True,
        )
        return

    pay_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💰 Перейти к оплате в Heleket",
                    url=payment_url,
                )
            ]
        ]
    )

    await callback.message.answer(
        "Перейди по кнопке ниже на платёжную страницу Heleket.\n\n"
        "После успешной оплаты бот автоматически обработает платёж и выдаст доступ к VPN.",
        reply_markup=pay_keyboard,
        disable_web_page_preview=True,
    )

    await callback.answer()


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    user_id = message.from_user.id

    sub = db.get_latest_subscription_for_telegram(telegram_user_id=user_id)
    if not sub:
        await message.answer(
            "У тебя пока нет активной VPN-подписки.\n\n"
            "Оформи подписку командами /buy или /buy_crypto, "
            "либо воспользуйся кнопками под этим сообщением.",
            reply_markup=SUBSCRIBE_KEYBOARD,
        )
        return


    vpn_ip = sub.get("vpn_ip")
    expires_at = sub.get("expires_at")

    if isinstance(expires_at, datetime):
        expires_str = fmt_date(expires_at)
    else:
        expires_str = str(expires_at)

    text = (
        "🔐 Текущий статус VPN-подписки:\n\n"
        f"• VPN IP: <code>{vpn_ip}</code>\n"
        f"• Действует до: <b>{expires_str}</b>"
    )


    await message.answer(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=get_status_keyboard(sub.get("id")),
    )


@router.message(Command("ref"))
async def cmd_ref(message: Message) -> None:
    """
    Показывает реферальную ссылку и статистику приглашённых.
    В том числе по линиям (1–5).
    """
    user = message.from_user
    if user is None:
        await message.answer(
            "Не удалось определить твой Telegram ID. Попробуй ещё раз позже.",
            disable_web_page_preview=True,
        )
        return

    telegram_user_id = user.id
    username = user.username

    try:
        info = db.get_or_create_referral_info(
            telegram_user_id=telegram_user_id,
            telegram_username=username,
        )
    except Exception as e:
        log.error(
            "[Referral] Failed to get referral info for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        await message.answer(
            "Не удалось получить реферальную информацию. Попробуй позже или напиши в поддержку.",
            disable_web_page_preview=True,
        )
        return

    ref_code = info.get("ref_code")
    invited_count = info.get("invited_count") or 0
    paid_referrals_count = info.get("paid_referrals_count") or 0

    invited_by_levels = info.get("invited_by_levels") or {}
    paid_by_levels = info.get("paid_by_levels") or {}
    paid_points_count = info.get("paid_points_count") or 0
    paid_points_by_levels = info.get("paid_points_by_levels") or {}

    # Пытаемся получить username бота, чтобы собрать полноценную ссылку
    try:
        me = await message.bot.get_me()
        bot_username = me.username
    except Exception as e:
        log.error(
            "[Referral] Failed to get bot username for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        bot_username = None

    if bot_username and ref_code:
        deep_link = f"https://t.me/{bot_username}?start={ref_code}"
    elif ref_code:
        deep_link = f"/start {ref_code}"
    else:
        deep_link = None

    lines: List[str] = []

    # Заголовок
    lines.append("👥 <b>Твоя реферальная ссылка</b>\n")
    lines.append("Приглашай друзей и получай бонусные дни VPN,\nкогда они подключаются и оплачивают подписку.\n")

    # Код
    if ref_code:
        lines.append(f"Код: <code>{ref_code}</code>")
    else:
        lines.append("Код: <i>не удалось сгенерировать</i>")

    # Ссылка
    if deep_link:
        lines.append(f'Ссылка: <a href="{deep_link}">{deep_link}</a>')
    else:
        lines.append("Ссылка: <i>недоступна</i>")

    # Пустая строка
    lines.append("")

    # Сводка по первой линии (без дублирования ниже)
    lines.append("📊 <b>Сводка:</b>")
    lines.append(f"• 1-я линия — приглашено: <b>{invited_count}</b>")
    lines.append(f"• 1-я линия — оплатили: <b>{paid_referrals_count}</b>")
    if paid_points_count:
        lines.append(f"• 1-я линия — оплатили баллами: <b>{paid_points_count}</b>")

    # Пустая строка перед уровнями
    lines.append("")

    # Блок уровней 2–5 в формате: «приглашено / оплатили» и отдельно оплатили баллами
    lines.append("Уровни 2–5 (приглашено / оплатили):")
    for level in range(2, 6):
        lvl_inv = invited_by_levels.get(level) or 0
        lvl_paid = paid_by_levels.get(level) or 0
        lvl_pts = paid_points_by_levels.get(level) or 0
        if lvl_pts:
            lines.append(f"• {level} уровень — {lvl_inv} / {lvl_paid} (баллами: {lvl_pts})")
        else:
            lines.append(f"• {level} уровень — {lvl_inv} / {lvl_paid}")

    if is_admin(message):
        lines.append("")
        lines.append("🔧 <b>Админ:</b>")
        try:
            stats = db.get_referral_admin_stats()
        except Exception as e:
            log.error(
                "[Referral] Failed to get referral admin stats for tg_id=%s: %r",
                telegram_user_id,
                e,
            )
            stats = {}
        if stats:
            lines.append(
                f"• Активных подписчиков: <b>{stats.get('active_subscribers', 0)}</b>"
            )
            connected_7d = None
            try:
                pairs = db.get_all_active_public_keys_with_users()
                if pairs:
                    if hasattr(asyncio, "to_thread"):
                        handshakes = await asyncio.to_thread(wg.get_handshake_timestamps)
                    else:
                        loop = asyncio.get_running_loop()
                        handshakes = await loop.run_in_executor(
                            None, wg.get_handshake_timestamps
                        )
                    cutoff = int(time.time()) - 7 * 24 * 3600
                    connected_7d = sum(
                        1 for uid, pk in pairs if handshakes.get(pk, 0) >= cutoff
                    )
            except Exception as e:
                log.warning(
                    "[Referral] Failed to get handshakes for connected_7d: %r",
                    e,
                )
            if connected_7d is not None:
                lines.append(
                    f"• Handshake за 7 дн.: <b>{connected_7d}</b>"
                )
            lines.append(
                f"• По промокодам: <b>{stats.get('promo_subscribers', 0)}</b>"
            )
            lines.append(
                f"• Всего когда-либо: <b>{stats.get('total_unique_ever', 0)}</b>"
            )
            new_today = stats.get("new_active_today", 0)
            connected_today = None
            if new_today > 0:
                try:
                    pubkeys = db.get_new_active_today_public_keys()
                    if pubkeys:
                        if hasattr(asyncio, "to_thread"):
                            handshakes = await asyncio.to_thread(wg.get_handshake_timestamps)
                        else:
                            loop = asyncio.get_running_loop()
                            handshakes = await loop.run_in_executor(
                                None, wg.get_handshake_timestamps
                            )
                        connected_today = sum(
                            1 for pk in pubkeys if handshakes.get(pk, 0) > 0
                        )
                except Exception as e:
                    log.warning(
                        "[Referral] Failed to get handshakes for new_today: %r",
                        e,
                    )
            if connected_today is not None:
                lines.append(
                    f"• Новых за сегодня: <b>{new_today}</b> (подключились: <b>{connected_today}</b>)"
                )
            else:
                lines.append(
                    f"• Новых за сегодня: <b>{new_today}</b>"
                )
            lines.append(
                f"• Оплатили / триал+промо: <b>{stats.get('paid_active', 0)}</b> / <b>{stats.get('trial_promo_active', 0)}</b>"
            )

    text = "\n".join(lines)
    await message.answer(
        text,
        disable_web_page_preview=True,
        reply_markup=REF_SHARE_KEYBOARD,
    )


@router.callback_query(F.data == "ref_trial:claim")
async def ref_trial_claim_callback(callback: CallbackQuery) -> None:
    """
    Кнопка «Получить тестовый доступ»:
    - если уже есть активная подписка (trial) — повторно отправить конфиг;
    - иначе если можно получить триал — создать и отправить;
    - иначе — отказать.
    """
    user = callback.from_user
    if user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    telegram_user_id = user.id

    # 1. Уже есть активная подписка и реферер — повторно отправить конфиг
    if db.get_referrer_telegram_id(telegram_user_id) is not None:
        active_sub = db.get_latest_subscription_for_telegram(
            telegram_user_id=telegram_user_id,
        )
        if active_sub and active_sub.get("active"):
            try:
                config_text = wg.build_client_config(
                    client_private_key=active_sub.get("wg_private_key"),
                    client_ip=active_sub.get("vpn_ip"),
                )
                await send_vpn_config_to_user(
                    telegram_user_id=telegram_user_id,
                    config_text=config_text,
                    caption=REF_TRIAL_CONFIG_CAPTION,
                )
                await callback.answer("Настройки отправлены! Проверь сообщения выше.")
                return
            except Exception as e:
                log.error(
                    "[ReferralTrial] Failed to resend config for tg_id=%s: %r",
                    telegram_user_id,
                    e,
                )
                await callback.answer(
                    "Не удалось отправить настройки. Попробуй позже.",
                    show_alert=True,
                )
                return

    # 2. Не может получить триал — отказать
    if not db.user_can_claim_referral_trial(telegram_user_id):
        await callback.answer(
            "Ты уже получал реферальный триал или у тебя есть активная подписка.",
            show_alert=True,
        )
        return

    # 3. Создать триал и отправить конфиг
    try:
        await try_give_referral_trial_7d(
            telegram_user_id=telegram_user_id,
            telegram_username=user.username,
        )
        if db.is_user_first_subscription(telegram_user_id):
            try:
                await _send_admin_new_user_notification(
                    bot=callback.bot,
                    telegram_user_id=telegram_user_id,
                    telegram_username=user.username,
                )
            except Exception as e:
                log.warning(
                    "[NewUserNotify] Failed to send admin notification for tg_id=%s: %r",
                    telegram_user_id,
                    e,
                )
        await callback.answer("Доступ выдан! Проверь сообщения выше.")
    except Exception as e:
        log.error(
            "[ReferralTrial] Error on claim button for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        await callback.answer(
            "Не удалось выдать доступ. Попробуй позже или напиши в поддержку.",
            show_alert=True,
        )


@router.callback_query(F.data.startswith("vpn_ok:"))
async def vpn_ok_callback(callback: CallbackQuery) -> None:
    """
    Кнопка «Всё работает» в handshake_followup_10m.
    Отвечаем, убираем кнопки, записываем vpn_ok_clicked.
    """
    try:
        sub_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка.", show_alert=True)
        return

    if db.has_subscription_notification(sub_id, "vpn_ok_clicked"):
        await callback.answer("Уже учтено 👍", show_alert=False)
        return

    sub = db.get_subscription_by_id(sub_id)
    if not sub or sub.get("telegram_user_id") != (callback.from_user.id if callback.from_user else None):
        await callback.answer("Подписка не найдена.", show_alert=True)
        return

    await callback.answer()
    buy_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить подписку", callback_data="pay:open")],
            [InlineKeyboardButton(text="🤝 Пригласить друга", callback_data=f"ref:open_from_notify:{sub_id}")],
        ]
    )
    await callback.message.answer(
        VPN_OK_ANSWER_TEXT,
        disable_web_page_preview=True,
        reply_markup=buy_kb,
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        db.create_subscription_notification(
            subscription_id=sub_id,
            notification_type="vpn_ok_clicked",
            telegram_user_id=sub.get("telegram_user_id"),
            expires_at=sub.get("expires_at"),
        )
    except Exception as e:
        log.warning("[VpnOk] Failed to record vpn_ok_clicked sub_id=%s: %r", sub_id, e)


@router.callback_query(F.data == "ref:open_from_ref")
async def ref_open_from_ref_callback(callback: CallbackQuery) -> None:
    """
    Кнопка «Пригласить друга» под /ref.
    Отправляет пользователю короткое сообщение, которое удобно переслать другу.
    """
    user = callback.from_user
    if user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    telegram_user_id = user.id
    username = user.username

    try:
        info = db.get_or_create_referral_info(
            telegram_user_id=telegram_user_id,
            telegram_username=username,
        )
    except Exception as e:
        log.error(
            "[Referral] Failed to get referral info (callback) for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        await callback.answer("Не удалось получить реферальную ссылку.", show_alert=True)
        return

    ref_code = info.get("ref_code")

    try:
        me = await callback.bot.get_me()
        bot_username = me.username
    except Exception as e:
        log.error(
            "[Referral] Failed to get bot username (callback) for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        bot_username = None

    if bot_username and ref_code:
        deep_link = f"https://t.me/{bot_username}?start={ref_code}"
    elif ref_code:
        deep_link = f"/start {ref_code}"
    else:
        deep_link = None

    if not deep_link:
        await callback.answer("Не удалось собрать ссылку.", show_alert=True)
        return

    share_text = (
        "Привет! Я пользуюсь MaxNet VPN — удобный VPN на WireGuard.\n\n"
        "Вот моя реферальная ссылка, по ней тебе выдадут пробный доступ, "
        "а мне начислят бонусные дни за оплату:\n"
        f"{deep_link}"
    )

    await callback.message.answer(
        share_text,
        disable_web_page_preview=True,
    )

    await callback.answer("Скопируй или перешли это сообщение другу 🙂")


@router.callback_query(F.data.startswith("ref:open_from_notify"))
async def ref_open_from_notify(callback: CallbackQuery) -> None:
    """
    Короткий вариант реферального сообщения по кнопке
    «🤝 Пригласить друга» под уведомлениями.
    Поддерживает ref:open_from_notify и ref:open_from_notify:{sub_id} (для tracking).
    """
    user = callback.from_user
    if user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    telegram_user_id = user.id
    username = user.username

    # Tracking ref_nudge_clicked для CRM (callback с sub_id или fallback по последней подписке)
    data = callback.data or ""
    if data.startswith("ref:open_from_notify:"):
        parts = data.split(":", 2)
        if len(parts) >= 3 and parts[2].isdigit():
            try:
                sub_id = int(parts[2])
                sub = db.get_subscription_by_id(sub_id)
                if sub and sub.get("telegram_user_id") == telegram_user_id:
                    if not db.has_subscription_notification(sub_id, "ref_nudge_clicked"):
                        try:
                            db.create_subscription_notification(
                                subscription_id=sub_id,
                                notification_type="ref_nudge_clicked",
                                telegram_user_id=sub.get("telegram_user_id"),
                                expires_at=sub.get("expires_at"),
                            )
                        except Exception as e:
                            log.warning(
                                "[Referral] Failed to record ref_nudge_clicked sub_id=%s: %r",
                                sub_id,
                                e,
                            )
            except (ValueError, TypeError):
                pass
    else:
        # Fallback: callback без sub_id — берём последнюю активную подписку пользователя
        try:
            sub = db.get_latest_subscription_for_telegram(telegram_user_id)
            if sub:
                sub_id = sub.get("id")
                if sub_id and not db.has_subscription_notification(sub_id, "ref_nudge_clicked"):
                    db.create_subscription_notification(
                        subscription_id=sub_id,
                        notification_type="ref_nudge_clicked",
                        telegram_user_id=telegram_user_id,
                        expires_at=sub.get("expires_at"),
                    )
        except Exception as e:
            log.warning("[Referral] Failed to record ref_nudge_clicked (fallback) tg_id=%s: %r", telegram_user_id, e)

    try:
        info = db.get_or_create_referral_info(
            telegram_user_id=telegram_user_id,
            telegram_username=username,
        )
    except Exception as e:
        log.error(
            "[Referral] Failed to get referral info (notify) for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        await callback.answer("Ошибка, попробуй позже.", show_alert=True)
        return

    ref_code = info.get("ref_code")

    # Пытаемся получить username бота, чтобы собрать ссылку
    try:
        me = await callback.bot.get_me()
        bot_username = me.username
    except Exception as e:
        log.error(
            "[Referral] Failed to get bot username (notify) for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        bot_username = None

    if bot_username and ref_code:
        deep_link = f"https://t.me/{bot_username}?start={ref_code}"
    elif ref_code:
        deep_link = f"/start {ref_code}"
    else:
        deep_link = None

    if not deep_link:
        await callback.message.answer(
            "Не удалось сформировать реферальную ссылку. Попробуй написать /ref или обратись в поддержку.",
            disable_web_page_preview=True,
        )
        await callback.answer()
        return

    text = (
        "🤝 Пригласи друга и продли подписку дешевле.\n\n"
        "Отправь эту ссылку другу. Когда он подключится и оплатит подписку, "
        "ты получишь баллы по реферальной программе:\n\n"
        f"<a href=\"{deep_link}\">{deep_link}</a>"
    )

    await callback.message.answer(
        text,
        disable_web_page_preview=True,
    )
    await callback.answer("Ссылку можно переслать другу.")


@router.callback_query(F.data.startswith("config:resend:"))
async def config_resend_callback(callback: CallbackQuery) -> None:
    """
    Повторная отправка VPN-настроек по кнопке «📱 Получить настройки».
    sub_id зашит в callback_data — берём подписку по id. Отправляем только в чат, где нажата кнопка.
    """
    if callback.from_user is None or callback.message is None or callback.message.chat is None:
        await callback.answer("Ошибка.", show_alert=True)
        return

    chat_id = callback.message.chat.id
    if chat_id <= 0:
        await callback.answer("Кнопка работает только в личном чате с ботом.", show_alert=True)
        return

    data = callback.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        await callback.answer("Ошибка данных кнопки.", show_alert=True)
        return
    try:
        sub_id = int(parts[2])
    except ValueError:
        await callback.answer("Ошибка данных кнопки.", show_alert=True)
        return

    sub = db.get_subscription_by_id(sub_id=sub_id)
    if not sub:
        await callback.answer(
            "Подписка не найдена. Напиши /status заново.",
            show_alert=True,
        )
        return

    sub_owner_id = sub.get("telegram_user_id")
    if sub_owner_id != chat_id:
        log.warning(
            "[ConfigResend] SECURITY: sub_id=%s owner=%s but button in chat_id=%s — rejected",
            sub_id,
            sub_owner_id,
            chat_id,
        )
        await callback.answer("Эта кнопка из чужого статуса. Напиши /status в этом чате.", show_alert=True)
        return

    if callback.from_user.id != chat_id:
        await callback.answer("Эта кнопка только для владельца чата.", show_alert=True)
        return

    if not sub.get("active") or not sub.get("expires_at"):
        await callback.answer(
            "Подписка истекла. Оформи новую через /buy.",
            show_alert=True,
        )
        return
    if sub.get("expires_at") <= datetime.now(timezone.utc):
        await callback.answer(
            "Подписка истекла. Оформи новую через /buy.",
            show_alert=True,
        )
        return

    vpn_ip = sub.get("vpn_ip")
    private_key = sub.get("wg_private_key")
    log.info(
        "[ConfigResend] chat_id=%s sub_id=%s vpn_ip=%r (owner check passed)",
        chat_id,
        sub_id,
        vpn_ip,
    )

    if not vpn_ip or not private_key:
        log.warning(
            "[ConfigResend] Missing vpn_ip or wg_private_key for chat_id=%s sub_id=%s",
            chat_id,
            sub.get("id"),
        )
        await callback.answer(
            "Не удалось получить настройки. Обратись в поддержку.",
            show_alert=True,
        )
        return

    config_text = wg.build_client_config(
        client_private_key=private_key,
        client_ip=vpn_ip,
    )
    for line in config_text.splitlines():
        if "Address =" in line:
            log.info("[ConfigResend] DEBUG config Address: %r", line.strip())
            break

    try:
        await send_vpn_config_to_user(
            telegram_user_id=chat_id,
            config_text=config_text,
            caption="Повторная отправка конфига MaxNet VPN.\n\nФайл vpn.conf — в этом сообщении. QR-код — в следующем.",
            schedule_checkpoint=False,
        )
        log.info(
            "[ConfigResend] Config sent to chat_id=%s sub_id=%s ip=%s",
            chat_id,
            sub_id,
            vpn_ip,
        )
        await callback.answer("Настройки отправлены!")
    except Exception as e:
        log.error(
            "[ConfigResend] Failed to resend config to chat_id=%s: %r",
            chat_id,
            e,
        )
        await callback.answer(
            "Не удалось отправить настройки. Попробуй позже или обратись в поддержку.",
            show_alert=True,
        )


# ---- Post-config connection check (checkpoint) callbacks ----

@router.callback_query(F.data.startswith("config_check_now:"))
async def config_check_now_callback(callback: CallbackQuery) -> None:
    """Кнопка «Проверить подключение» после выдачи конфига — быстрая проверка handshake."""
    if callback.from_user is None or callback.message is None:
        await callback.answer("Ошибка.", show_alert=True)
        return
    try:
        sub_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка.", show_alert=True)
        return
    sub = db.get_subscription_by_id(sub_id)
    if not sub or sub.get("telegram_user_id") != callback.from_user.id:
        await callback.answer("Подписка не найдена.", show_alert=True)
        return
    await callback.answer()
    pub_key = (sub.get("wg_public_key") or "").strip()
    if not pub_key:
        log.info("[ConfigCheckNow] tg_id=%s sub_id=%s result=unknown (no wg_public_key)", callback.from_user.id, sub_id)
        await callback.message.answer(CONFIG_CHECK_NOW_UNKNOWN)
        return
    try:
        handshakes = wg.get_handshake_timestamps()
        ts = handshakes.get(pub_key, 0)
    except Exception as e:
        log.warning("[ConfigCheckNow] tg_id=%s sub_id=%s handshake check failed: %r", callback.from_user.id, sub_id, e)
        await callback.message.answer(CONFIG_CHECK_NOW_UNKNOWN)
        return
    if ts > 0:
        log.info("[ConfigCheckNow] tg_id=%s sub_id=%s result=ok", callback.from_user.id, sub_id)
        await callback.message.answer(CONFIG_CHECK_NOW_OK)
    else:
        log.info("[ConfigCheckNow] tg_id=%s sub_id=%s result=no_handshake", callback.from_user.id, sub_id)
        support_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=SUPPORT_BUTTON_TEXT, url=SUPPORT_URL)],
            ]
        )
        await callback.message.answer(CONFIG_CHECK_NOW_FAIL, reply_markup=support_kb)


@router.callback_query(F.data.startswith("config_check_ok:"))
async def config_check_ok_callback(callback: CallbackQuery) -> None:
    """Кнопка «Да, всё работает» после checkpoint."""
    try:
        sub_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка.", show_alert=True)
        return
    sub = db.get_subscription_by_id(sub_id)
    if not sub or (callback.from_user and sub.get("telegram_user_id") != callback.from_user.id):
        await callback.answer("Подписка не найдена.", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(CONFIG_CHECK_SUCCESS)
    try:
        db.create_subscription_notification(
            subscription_id=sub_id,
            notification_type="config_check_ok",
            telegram_user_id=sub.get("telegram_user_id"),
            expires_at=sub.get("expires_at"),
        )
    except Exception as e:
        log.warning("[ConfigCheck] Failed to record config_check_ok sub_id=%s: %r", sub_id, e)

    ref_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 Пригласить друга",
                    callback_data=f"ref:open_from_notify:{sub_id}",
                ),
            ],
        ]
    )
    await callback.message.answer(
        REFERRAL_PROMPT_AFTER_CONNECTION_SUCCESS,
        reply_markup=ref_kb,
    )
    log.info("[ReferralPrompt] tg_id=%s source=config_check_ok", callback.from_user.id if callback.from_user else None)


@router.callback_query(F.data.startswith("config_check_failed:"))
async def config_check_failed_callback(callback: CallbackQuery) -> None:
    """Кнопка «Нет, не получилось» — показываем варианты."""
    try:
        sub_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка.", show_alert=True)
        return
    sub = db.get_subscription_by_id(sub_id)
    if not sub or (callback.from_user and sub.get("telegram_user_id") != callback.from_user.id):
        await callback.answer("Подписка не найдена.", show_alert=True)
        return
    await callback.answer()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=CONFIG_CHECK_OPTIONS["not_found"],
                callback_data=f"config_issue_not_found:{sub_id}",
            )],
            [InlineKeyboardButton(
                text=CONFIG_CHECK_OPTIONS["import"],
                callback_data=f"config_issue_import:{sub_id}",
            )],
            [InlineKeyboardButton(
                text=CONFIG_CHECK_OPTIONS["connected_no_internet"],
                callback_data=f"config_issue_connected_no_internet:{sub_id}",
            )],
            [InlineKeyboardButton(
                text=CONFIG_CHECK_OPTIONS["support"],
                callback_data="config_issue_support",
            )],
        ]
    )
    await callback.message.answer(CONFIG_CHECK_FAIL, reply_markup=keyboard)


@router.callback_query(F.data.startswith("config_check_resend:"))
async def config_check_resend_callback(callback: CallbackQuery) -> None:
    """Кнопка «Отправить настройки ещё раз» — повторная отправка конфига."""
    if callback.from_user is None or callback.message is None or callback.message.chat is None:
        await callback.answer("Ошибка.", show_alert=True)
        return
    chat_id = callback.message.chat.id
    try:
        sub_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка.", show_alert=True)
        return
    sub = db.get_subscription_by_id(sub_id)
    if not sub or sub.get("telegram_user_id") != chat_id:
        await callback.answer("Подписка не найдена.", show_alert=True)
        return
    if not sub.get("active") or not sub.get("vpn_ip") or not sub.get("wg_private_key"):
        await callback.answer("Конфиг недоступен. Обратись в поддержку.", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    config_text = wg.build_client_config(
        client_private_key=sub["wg_private_key"],
        client_ip=sub["vpn_ip"],
    )
    await send_vpn_config_to_user(
        telegram_user_id=chat_id,
        config_text=config_text,
        caption="Повторная отправка конфига MaxNet VPN.\n\nФайл vpn.conf — в этом сообщении. QR-код — в следующем.",
        schedule_checkpoint=False,
    )
    await callback.message.answer("Конфиг отправлен. Проверь сообщения выше.")


@router.callback_query(F.data.startswith("config_issue_not_found:"))
async def config_issue_not_found_callback(callback: CallbackQuery) -> None:
    """Не нашёл конфиг — предлагаем отправить ещё раз."""
    try:
        sub_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка.", show_alert=True)
        return
    sub = db.get_subscription_by_id(sub_id)
    if not sub or (callback.from_user and sub.get("telegram_user_id") != callback.from_user.id):
        await callback.answer("Подписка не найдена.", show_alert=True)
        return
    await callback.answer()
    resend_btn = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="📱 Отправить настройки ещё раз",
                callback_data=f"config_check_resend:{sub_id}",
            )],
        ]
    )
    await callback.message.answer(
        "Напиши мне «вышли конфиг» или нажми кнопку ниже — отправлю настройки ещё раз.",
        reply_markup=resend_btn,
    )


@router.callback_query(F.data.startswith("config_issue_import:"))
async def config_issue_import_callback(callback: CallbackQuery) -> None:
    """Не получается импортировать — инструкция по подключению."""
    await callback.answer()
    await callback.message.answer(
        HELP_INSTRUCTION,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("config_issue_connected_no_internet:"))
async def config_issue_connected_no_internet_callback(callback: CallbackQuery) -> None:
    """VPN подключён, но сайты не открываются — troubleshooting (vpn_not_working)."""
    try:
        sub_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка.", show_alert=True)
        return
    sub = db.get_subscription_by_id(sub_id)
    if not sub or (callback.from_user and sub.get("telegram_user_id") != callback.from_user.id):
        await callback.answer("Подписка не найдена.", show_alert=True)
        return
    await callback.answer()
    user_id = callback.from_user.id if callback.from_user else 0
    context = build_user_context(user_id)
    text, reply_markup, _diagnosis, _symptom = action_vpn_not_working(context)
    await callback.message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data == "config_issue_support")
async def config_issue_support_callback(callback: CallbackQuery) -> None:
    """Нужна помощь — human handoff."""
    await callback.answer()
    text, reply_markup = action_human_request()
    await callback.message.answer(text, reply_markup=reply_markup)


# ---- Onboarding пошаговое подключение (кнопка «Подключить VPN») ----

@router.callback_query(F.data == "onboarding:start")
async def onboarding_start_callback(callback: CallbackQuery) -> None:
    """Шаг 1: выбор устройства."""
    user_id = callback.from_user.id if callback.from_user else 0
    log.info("[Onboarding] step=start tg_id=%s", user_id)
    await callback.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ONBOARDING_DEVICE_IPHONE, callback_data="onboarding:device:iphone")],
            [InlineKeyboardButton(text=ONBOARDING_DEVICE_ANDROID, callback_data="onboarding:device:android")],
            [InlineKeyboardButton(text=ONBOARDING_DEVICE_COMPUTER, callback_data="onboarding:device:computer")],
        ]
    )
    await callback.message.answer(ONBOARDING_DEVICE_QUESTION, reply_markup=kb)


def _onboarding_step3_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура шага 3: Проверить подключение (если есть sub_id)."""
    sub = None
    try:
        sub = db.get_latest_subscription_for_telegram(user_id)
    except Exception:
        pass
    if sub and sub.get("id"):
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=CONFIG_CHECK_NOW_BUTTON_TEXT,
                        callback_data=f"config_check_now:{sub['id']}",
                    ),
                ],
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=[])


@router.callback_query(F.data == "onboarding:device:iphone")
@router.callback_query(F.data == "onboarding:device:android")
async def onboarding_device_mobile_callback(callback: CallbackQuery) -> None:
    """Шаг 2 (iPhone/Android): установи WireGuard, затем «Готово»."""
    user_id = callback.from_user.id if callback.from_user else 0
    log.info("[Onboarding] step=device_selected tg_id=%s device=%s", user_id, callback.data)
    await callback.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ONBOARDING_READY_BUTTON, callback_data="onboarding:ready")],
        ]
    )
    await callback.message.answer(ONBOARDING_INSTALL_MOBILE, reply_markup=kb)


@router.callback_query(F.data == "onboarding:device:computer")
async def onboarding_device_computer_callback(callback: CallbackQuery) -> None:
    """Шаг 3 для компьютера: импорт конфига + Проверить подключение."""
    user_id = callback.from_user.id if callback.from_user else 0
    log.info("[Onboarding] step=ready_for_import tg_id=%s device=computer", user_id)
    await callback.answer()
    kb = _onboarding_step3_keyboard(user_id)
    await callback.message.answer(ONBOARDING_IMPORT_CONFIG, reply_markup=kb)


@router.callback_query(F.data == "onboarding:ready")
async def onboarding_ready_callback(callback: CallbackQuery) -> None:
    """После «Готово» на мобильном: шаг 3 — импорт конфига + Проверить подключение."""
    user_id = callback.from_user.id if callback.from_user else 0
    log.info("[Onboarding] step=ready_for_import tg_id=%s", user_id)
    await callback.answer()
    kb = _onboarding_step3_keyboard(user_id)
    await callback.message.answer(ONBOARDING_IMPORT_CONFIG, reply_markup=kb)


# ---- Onboarding после конфига: WireGuard установлен? ----

@router.callback_query(F.data == "onboarding:wireguard_download")
async def onboarding_wireguard_download_callback(callback: CallbackQuery) -> None:
    """Кнопка «Скачать WireGuard» — ссылки на официальные загрузки."""
    if callback.from_user:
        log.info("[Onboarding] tg_id=%s step=wireguard_download", callback.from_user.id)
    await callback.answer()
    download_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🍏 App Store", url=WG_APP_STORE_URL)],
            [InlineKeyboardButton(text="🤖 Play Market", url=WG_PLAY_MARKET_URL)],
            [InlineKeyboardButton(text="💻 Windows / Mac", url=WG_DESKTOP_URL)],
        ]
    )
    await callback.message.answer(
        ONBOARDING_WG_DOWNLOAD_MESSAGE,
        reply_markup=download_kb,
    )


@router.callback_query(F.data.startswith("onboarding:wireguard_confirm:"))
async def onboarding_wireguard_confirm_callback(callback: CallbackQuery) -> None:
    """Кнопка «Да, установлен» — напомнить импорт/QR и кнопка «Проверить подключение»."""
    if callback.from_user:
        log.info("[Onboarding] tg_id=%s step=wireguard_confirm", callback.from_user.id)
    await callback.answer()
    data = callback.data or ""
    parts = data.split(":", 2)
    sub_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
    if sub_id > 0:
        sub = db.get_subscription_by_id(sub_id)
        if sub and callback.from_user and sub.get("telegram_user_id") == callback.from_user.id:
            check_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=CONFIG_CHECK_NOW_BUTTON_TEXT,
                            callback_data=f"config_check_now:{sub_id}",
                        ),
                    ],
                ]
            )
            await callback.message.answer(ONBOARDING_WG_CONFIRM_MESSAGE, reply_markup=check_kb)
            return
    await callback.message.answer(ONBOARDING_WG_CONFIRM_MESSAGE)


@router.message(Command("ref_info"))
async def cmd_ref_info(message: Message) -> None:
    await message.answer(
        REF_INFO_TEXT,
        disable_web_page_preview=True,
    )


def _humanize_points_reason(reason: str, source: str, level: Optional[int]) -> str:
    """
    Преобразует внутренние reason/source в человекочитаемый текст.
    """
    if reason == "pay_tariff_points":
        return "оплата подписки"

    if reason.startswith("ref_level_"):
        if level is not None:
            return f"реферал (уровень {level})"
        return "реферальный бонус"

    if reason in ("manual_test_bonus", "ref_level_1_manual_fix"):
        return "бонус от администрации"

    if source in ("manual", "manual_fix"):
        return "бонус от администрации"

    return reason


@router.message(Command("points"))
async def cmd_points(message: Message) -> None:
    """
    Показывает текущий баланс поинтов и последние операции
    в формате:
    🎮 Твои игровые баллы

    💰 Баланс: <b>XXX</b>

    Сегодня:
    🔴 −600 — оплата подписки
    🟢 +6 — реферал (3 уровень)

    Ранее:
    🎁 +1000 — бонус от администрации

    ℹ️ Баллы можно тратить на оплату подписки.
    """

    user = message.from_user
    if user is None:
        await message.answer(
            "Не удалось определить твой Telegram ID. Попробуй ещё раз позже.",
            disable_web_page_preview=True,
        )
        return

    telegram_user_id = user.id

    try:
        balance = db.get_user_points_balance(telegram_user_id=telegram_user_id)
        transactions = db.get_user_points_last_transactions(
            telegram_user_id=telegram_user_id,
            limit=10,
        )
    except Exception as e:
        log.error(
            "[Points] Failed to fetch points for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        await message.answer(
            "Не удалось получить информацию по баллам. Попробуй позже или напиши в поддержку.",
            disable_web_page_preview=True,
        )
        return

    lines: List[str] = []
    lines.append("🎮 <b>Твои игровые баллы</b>\n")
    lines.append(f"💰 Баланс: <b>{balance}</b> баллов.\n")

    # Если операций нет — показываем простой текст + подсказку
    if not transactions:
        lines.append("Пока у тебя нет операций по баллам.")
        lines.append("")
        lines.append("ℹ️ Баллы можно тратить на оплату подписки.")
        text = "\n".join(lines)
        await message.answer(
            text,
            disable_web_page_preview=True,
            reply_markup=POINTS_KEYBOARD,
        )
        return

    now_utc = datetime.utcnow()

    # Группируем операции: отдельно "Сегодня" и "Ранее",
    # внутри — по типу (оплата подписки / реферал / бонус и т.д.)
    today_groups: Dict[tuple, int] = defaultdict(int)
    earlier_groups: Dict[tuple, int] = defaultdict(int)

    for tx in transactions:
        delta_raw = tx.get("delta") or 0
        reason = tx.get("reason") or "-"
        source = tx.get("source") or "-"
        created_at = tx.get("created_at")
        level = tx.get("level")

        # Человекочитаемое название причины
        label = _humanize_points_reason(
            reason=reason,
            source=source,
            level=level,
        )

        # Аккуратно приводим delta к int
        if isinstance(delta_raw, (int, float)):
            delta = int(delta_raw)
        else:
            try:
                delta = int(delta_raw)
            except Exception:
                delta = 0

        # Определяем, относится ли операция к "Сегодня"
        is_today = False
        if isinstance(created_at, datetime):
            if created_at.tzinfo is not None:
                created_dt = created_at.astimezone(timezone.utc)
            else:
                created_dt = created_at.replace(tzinfo=timezone.utc)
            is_today = (created_dt.date() == now_utc.date())

        group_key = (label, "income" if delta >= 0 else "spend")

        if is_today:
            today_groups[group_key] += delta
        else:
            earlier_groups[group_key] += delta

    # Блок "Сегодня"
    lines.append("Сегодня:")

    if not today_groups:
        lines.append("• нет операций за сегодня")
    else:
        for (label, _kind), total in today_groups.items():
            if total > 0:
                emoji = "🟢"
                amount_str = f"+{total}"
            elif total < 0:
                emoji = "🔴"
                amount_str = str(total)
            else:
                emoji = "⚪"
                amount_str = str(total)
            lines.append(f"{emoji} {amount_str} — {label}")

    # Пустая строка между блоками
    lines.append("")
    lines.append("Ранее:")

    if not earlier_groups:
        lines.append("• нет более ранних операций")
    else:
        for (label, _kind), total in earlier_groups.items():
            if total > 0:
                emoji = "🟢"
                amount_str = f"+{total}"
            elif total < 0:
                emoji = "🔴"
                amount_str = str(total)
            else:
                emoji = "⚪"
                amount_str = str(total)
            lines.append(f"{emoji} {amount_str} — {label}")

    lines.append("")
    lines.append("ℹ️ Баллы можно тратить на оплату подписки.")

    text = "\n".join(lines)

    await message.answer(
        text,
        disable_web_page_preview=True,
        reply_markup=POINTS_KEYBOARD,
    )


@router.message(PromoStates.waiting_for_code)
async def promo_code_apply(message: Message, state: FSMContext) -> None:
    """
    Обработка введённого промокода.
    """
    user = message.from_user
    if user is None:
        await message.answer(
            "Не удалось определить твой Telegram ID. Попробуй ещё раз.",
            disable_web_page_preview=True,
        )
        await state.clear()
        return

    code_raw = (message.text or "").strip()
    if not code_raw:
        await message.answer(
            "Промокод не должен быть пустым. Отправь, пожалуйста, код ещё раз.",
            disable_web_page_preview=True,
        )
        return
    
    promo_log.info(
        "[PromoApply] Try apply promo: tg_id=%s code=%r",
        user.id,
        code_raw,
    )

    result = db.apply_promo_code_to_latest_subscription(
        telegram_user_id=user.id,
        code=code_raw,
    )

    # Завершаем FSM в любом случае
    await state.clear()

    if not result.get("ok"):
        error = result.get("error")
        promo_log.warning(
            "[PromoApply] Failed to apply promo: tg_id=%s code=%r error=%s result=%r",
            user.id,
            code_raw,
            error,
            result,
        )

        # Подбираем человекочитаемое сообщение
        if error in ("not_found", "expired_or_inactive"):
            text = "Такой промокод не найден или срок его действия истёк."
        elif error == "no_active_subscription":
            # Попробуем использовать промокод как выдачу новой подписки
            promo_new_result = db.apply_promo_code_without_subscription(
                telegram_user_id=user.id,
                code=code_raw,
            )

            if not promo_new_result.get("ok"):
                # Если даже для новой подписки промокод не подошёл — ведём себя по-старому
                text = (
                    "У тебя сейчас нет активной подписки, к которой можно применить промокод.\n\n"
                    "Сначала оформи подписку, а затем повторно введи промокод."
                )
                await message.answer(
                    text,
                    disable_web_page_preview=True,
                )
                return

            extra_days = promo_new_result.get("extra_days")
            new_expires_at = promo_new_result.get("new_expires_at")
            promo_code = promo_new_result.get("promo_code")
            usage_id = promo_new_result.get("usage_id")

            promo_log.info(
                "[PromoApply] Promo used for new subscription: tg_id=%s code=%r extra_days=%s new_expires_at=%r usage_id=%r",
                user.id,
                promo_code,
                extra_days,
                new_expires_at,
                usage_id,
            )

            # Попробуем реанимировать последнюю деактивированную подписку (переиспользовать конфиг)
            latest_sub = None
            reuse_priv = None
            reuse_pub = None
            reuse_ip = None

            try:
                latest_sub = db.get_latest_subscription_for_telegram(
                    telegram_user_id=user.id,
                )
                promo_log.info(
                    "[PromoApply] Latest subscription for revive tg_id=%s: %r",
                    user.id,
                    latest_sub,
                )
            except Exception as e:
                promo_log.error(
                    "[PromoApply] Failed to get latest subscription for revive tg_id=%s: %r",
                    user.id,
                    e,
                )
                latest_sub = None

            if latest_sub:
                if (
                    latest_sub.get("wg_private_key")
                    and latest_sub.get("wg_public_key")
                    and latest_sub.get("vpn_ip")
                ):
                    reuse_priv = latest_sub.get("wg_private_key")
                    reuse_pub = latest_sub.get("wg_public_key")
                    reuse_ip = latest_sub.get("vpn_ip")

            # Пытаемся создать новую подписку (с реюзом конфига, если он есть)
            allocated_new_ip = False
            subscription_created = False
            try:
                # На всякий случай выключим все активные подписки (если вдруг что-то есть)
                # release_ips_to_pool=False при reuse — иначе race: отпустим IP, другой юзер его возьмёт.
                deactivate_existing_active_subscriptions(
                    telegram_user_id=user.id,
                    reason="auto_replace_promo_new_sub",
                    release_ips_to_pool=not (reuse_priv and reuse_pub and reuse_ip),
                )

                send_config = True

                if reuse_priv and reuse_pub and reuse_ip:
                    client_priv = reuse_priv
                    client_pub = reuse_pub
                    client_ip = reuse_ip
                    allowed_ip = f"{client_ip}/{settings.WG_CLIENT_NETWORK_CIDR}"

                    log.info(
                        "[PromoApply] Reuse peer (new sub) pubkey=%s ip=%s for tg_id=%s",
                        client_pub,
                        allowed_ip,
                        user.id,
                    )
                    wg.add_peer(
                        public_key=client_pub,
                        allowed_ip=allowed_ip,
                        telegram_user_id=user.id,
                    )

                    # Конфиг уже есть у пользователя, повторно не шлём
                    send_config = False
                else:
                    client_priv, client_pub = wg.generate_keypair()
                    client_ip = wg.generate_client_ip()
                    allocated_new_ip = True
                    allowed_ip = f"{client_ip}/{settings.WG_CLIENT_NETWORK_CIDR}"

                    log.info(
                        "[PromoApply] Add peer (new sub) pubkey=%s ip=%s for tg_id=%s",
                        client_pub,
                        allowed_ip,
                        user.id,
                    )
                    wg.add_peer(
                        public_key=client_pub,
                        allowed_ip=allowed_ip,
                        telegram_user_id=user.id,
                    )

                if isinstance(new_expires_at, datetime):
                    expires_at = new_expires_at
                else:
                    expires_at = datetime.utcnow() + timedelta(days=extra_days or 0)

                # создаём подписку и получаем её ID
                new_sub_id = db.insert_subscription(
                    tribute_user_id=0,
                    telegram_user_id=user.id,
                    telegram_user_name=user.username,
                    subscription_id=0,
                    period_id=0,
                    period="promo_code",
                    channel_id=0,
                    channel_name="Promo code",
                    vpn_ip=client_ip,
                    wg_private_key=client_priv,
                    wg_public_key=client_pub,
                    expires_at=expires_at,
                    event_name="promo_new_subscription",
                )
                subscription_created = True

                # если знаем usage_id — линкуем usage к созданной подписке
                if usage_id is not None:
                    try:
                        db.link_promo_usage_to_subscription(
                            usage_id=usage_id,
                            subscription_id=new_sub_id,
                        )
                    except Exception as e:
                        log.error(
                            "[PromoApply] Failed to link promo usage %s to subscription %s for tg_id=%s: %r",
                            usage_id,
                            new_sub_id,
                            user.id,
                            e,
                        )

                if send_config:
                    recently_expired_trial = db.has_recently_expired_subscription(
                        user.id, within_hours=48
                    )
                    if recently_expired_trial:
                        try:
                            await send_trial_expired_paid_notification(user.id)
                        except Exception as e:
                            log.warning(
                                "[PromoApply] Failed to send trial-expired-paid notification tg_id=%s: %r",
                                user.id,
                                e,
                            )
                    config_text = wg.build_client_config(
                        client_private_key=client_priv,
                        client_ip=client_ip,
                    )

                    await send_vpn_config_to_user(
                        telegram_user_id=user.id,
                        config_text=config_text,
                        caption=(
                            "По промокоду тебе выдан доступ к MaxNet VPN.\n\n"
                            "Файл vpn.conf — в этом сообщении. QR-код — в следующем."
                        ),
                    )
                    if recently_expired_trial:
                        try:
                            db.create_subscription_notification(
                                subscription_id=new_sub_id,
                                notification_type="recently_expired_trial_followup",
                                telegram_user_id=user.id,
                                expires_at=expires_at,
                            )
                        except Exception as e:
                            log.warning(
                                "[PromoApply] Failed to register recently_expired_trial_followup sub_id=%s: %r",
                                new_sub_id,
                                e,
                            )
                else:
                    log.info(
                        "[PromoApply] Reused existing config for tg_id=%s sub_id=%s (no new config sent)",
                        user.id,
                        new_sub_id,
                    )

            except Exception as e:
                if allocated_new_ip and client_ip and not subscription_created:
                    try:
                        db.release_ip_in_pool(client_ip)
                    except Exception:
                        pass
                log.error(
                    "[PromoApply] Failed to create new subscription from promo for tg_id=%s: %r",
                    user.id,
                    e,
                )
                await message.answer(
                    "При попытке выдать подписку по промокоду произошла ошибка.\n"
                    "Попробуй ещё раз позже или напиши в поддержку.",
                    disable_web_page_preview=True,
                )
                return

            if isinstance(expires_at, datetime):
                expires_str = fmt_date(expires_at)
            else:
                expires_str = str(expires_at)

            try:
                await _send_admin_promo_used_notification(
                    bot=message.bot,
                    telegram_user_id=user.id,
                    telegram_username=user.username,
                    promo_code=promo_code or "",
                    extra_days=extra_days or 0,
                    expires_at=expires_at,
                )
            except Exception as e:
                log.warning(
                    "[PromoUsedNotify] Failed for tg_id=%s: %r",
                    user.id,
                    e,
                )

            await message.answer(
                "✅ Промокод успешно применён.\n\n"
                f"Тебе выдана новая VPN-подписка на <b>{extra_days} дн.</b>\n"
                f"Срок действия до: <b>{expires_str}</b>\n\n"
                f"Промокод: <code>{promo_code}</code>",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
        elif error == "user_not_allowed":
            text = "Этот промокод привязан к другому пользователю и не может быть применён."

        elif error == "no_uses_left":
            text = "Лимит использований этого промокода уже исчерпан."
        elif error == "per_user_limit_reached":
            text = "Ты уже использовал этот промокод максимально возможное количество раз."
        elif error == "invalid_extra_days":
            text = "Этот промокод сейчас не даёт дополнительных дней."
        elif error == "empty_code":
            text = "Промокод не должен быть пустым."
        elif error == "db_error":
            # Можно показать более общий текст без подробностей
            text = (
                "При обработке промокода произошла ошибка.\n"
                "Попробуй ещё раз чуть позже или напиши в поддержку."
            )
        else:
            # fallback — либо используем error_message, либо общий текст
            text = result.get("error_message") or (
                "Не удалось применить промокод. Попробуй ещё раз или напиши в поддержку."
            )

        await message.answer(
            text,
            disable_web_page_preview=True,
        )
        return


    extra_days = result.get("extra_days")
    new_expires_at = result.get("new_expires_at")
    promo_code = result.get("promo_code")
    
    promo_log.info(
        "[PromoApply] Success apply promo: tg_id=%s code=%r extra_days=%s new_expires_at=%r",
        user.id,
        promo_code,
        extra_days,
        new_expires_at,
    )

    try:
        exp_dt = new_expires_at if isinstance(new_expires_at, datetime) else None
        await _send_admin_promo_used_notification(
            bot=message.bot,
            telegram_user_id=user.id,
            telegram_username=user.username,
            promo_code=promo_code or "",
            extra_days=extra_days or 0,
            expires_at=exp_dt or datetime.now(timezone.utc),
        )
    except Exception as e:
        log.warning(
            "[PromoUsedNotify] Failed for tg_id=%s: %r",
            user.id,
            e,
        )

    if isinstance(new_expires_at, datetime):
        expires_str = new_fmt_date(expires_at)
    else:
        expires_str = str(new_expires_at)

    await message.answer(
        "✅ Промокод успешно применён.\n\n"
        f"К твоей активной подписке добавлено <b>{extra_days} дн.</b>\n"
        f"Новый срок действия: <b>{expires_str}</b>\n\n"
        f"Промокод: <code>{promo_code}</code>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.message(DemoRequest.waiting_for_message)
async def demo_request_get_message(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None:
        await message.answer(
            "Не удалось определить твой аккаунт. Попробуй ещё раз позже.",
            disable_web_page_preview=True,
        )
        await state.clear()
        return

    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)
    if admin_id == 0:
        await message.answer(
            "Сейчас запросы на демо-доступ временно недоступны. Попробуй позже или оформи подписку через /buy или /buy_crypto.",
            disable_web_page_preview=True,
        )
        await state.clear()
        return

    user_id = user.id
    username = user.username
    full_name = user.full_name

    request_text = message.text or ""
    request_text = request_text.strip()
    if not request_text:
        request_text = "— (пустое сообщение)"

    if len(request_text) > 1000:
        request_text = request_text[:1000] + "…"

    if username:
        username_line = f"@{username}"
    else:
        username_line = "—"

    admin_text = (
        "⚡️ <b>Запрос демо-доступа к MaxNet VPN</b>\n\n"
        f"Пользователь:\n"
        f"• Имя: <code>{full_name}</code>\n"
        f"• Username: <code>{username_line}</code>\n"
        f"• Telegram ID: <code>{user_id}</code>\n\n"
        f"Сообщение пользователя:\n"
        f"<code>{request_text}</code>\n\n"
        "Выдать этому пользователю демо-доступ?"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Выдать демо-доступ",
                    callback_data=f"demo:approve:{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отказать",
                    callback_data=f"demo:deny:{user_id}",
                ),
            ],
        ]
    )

    try:
        await message.bot.send_message(
            chat_id=admin_id,
            text=admin_text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.error("[Demo] Failed to send demo request to admin %s: %s", admin_id, repr(e))
        await message.answer(
            "Не удалось отправить запрос админу. Попробуй позже или оформи подписку через Tribute.",
            disable_web_page_preview=True,
        )
        await state.clear()
        return

    await message.answer(
        "Спасибо! Я отправил твой запрос на демо-доступ админу.\n\n"
        "Когда он примет решение, я пришлю сюда уведомление.",
        disable_web_page_preview=True,
    )

    await state.clear()  
    
@router.message(Command("admin_info"))
async def cmd_admin_info(message: Message) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    await message.answer(
        ADMIN_INFO_TEXT,
        disable_web_page_preview=True,
    )


@router.message(Command("admin_stats"))
async def cmd_admin_stats(message: Message) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    await send_admin_stats(message)


def _parse_support_ai_log_for_stats(hours: int = 24) -> Tuple[Dict[str, int], Dict[str, int]]:
    """
    Парсит support_ai.log за последние hours часов.
    Возвращает (source_counts, vpn_diagnosis_counts).
    """
    import re
    source_counts: Dict[str, int] = defaultdict(int)
    vpn_diagnosis_counts: Dict[str, int] = defaultdict(int)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    log_path = Path(SUPPORT_AI_LOG_FILE)
    if not log_path.is_file():
        return dict(source_counts), dict(vpn_diagnosis_counts)
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "support_ai " not in line:
                    continue
                # Формат: "2025-03-12 10:00:00,000 - INFO - support_ai tg_id=..."
                parts = line.split(" - ", 2)
                if len(parts) < 3:
                    continue
                try:
                    ts_str = parts[0].strip()
                    if "," in ts_str:
                        ts_str = ts_str.split(",")[0]
                    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < cutoff:
                        continue
                except (ValueError, TypeError):
                    continue
                msg = parts[2]
                m = re.search(r"source=(\S+)", msg)
                if m:
                    source_counts[m.group(1)] += 1
                m = re.search(r"vpn_diagnosis=(\S+)", msg)
                if m:
                    vpn_diagnosis_counts[m.group(1)] += 1
    except Exception as e:
        log.warning("[SupportStats] Log parse error: %r", e)
    return dict(source_counts), dict(vpn_diagnosis_counts)


def _parse_support_ai_log_intent_fallback(hours: int = 24) -> Dict[str, int]:
    """
    Парсит support_ai.log за последние hours часов, считает по каждому intent
    количество записей с fallback=True. Для /support_stats — блок «Проблемные интенты (по fallback)».
    """
    import re
    fallback_by_intent: Dict[str, int] = defaultdict(int)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    log_path = Path(SUPPORT_AI_LOG_FILE)
    if not log_path.is_file():
        return dict(fallback_by_intent)
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "support_ai " not in line or "fallback=True" not in line:
                    continue
                parts = line.split(" - ", 2)
                if len(parts) < 3:
                    continue
                try:
                    ts_str = parts[0].strip()
                    if "," in ts_str:
                        ts_str = ts_str.split(",")[0]
                    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < cutoff:
                        continue
                except (ValueError, TypeError):
                    continue
                msg = parts[2]
                m = re.search(r"intent=(\S+)", msg)
                if m:
                    fallback_by_intent[m.group(1)] += 1
    except Exception as e:
        log.warning("[SupportStats] Log parse intent_fallback error: %r", e)
    return dict(fallback_by_intent)


# Русские подписи для /support_stats (только вывод, ключи и логика без изменений)
SUPPORT_STATS_HEADER = "Статистика AI-поддержки (за 24 часа)"
SUPPORT_STATS_BY_SOURCE = "По источнику ответа:"
SUPPORT_STATS_TOP_INTENTS = "Популярные интенты:"
SUPPORT_STATS_TOP_VPN = "Диагностика VPN:"
SUPPORT_STATS_PROBLEM_HANDOFF = "Проблемные интенты (по handoff):"
SUPPORT_STATS_PROBLEM_FALLBACK = "Проблемные интенты (по fallback):"
SUPPORT_STATS_NONE = "(нет)"
SUPPORT_STATS_INTENT_LABELS = {
    "unclear": "не распознано",
    "referral_info": "реферальная программа",
    "connect_help": "помощь с подключением",
    "subscription_status": "статус подписки",
    "pricing_info": "тарифы и стоимость",
    "missing_config_after_payment": "нет конфига после оплаты",
    "privacy_policy": "персональные данные",
    "smalltalk": "обычный разговор",
    "referral_stats": "статистика рефералов",
    "resend_config": "повторная отправка конфига",
}
SUPPORT_STATS_VPN_DIAG_LABELS = {
    "handshake_ok": "VPN подключён (есть handshake)",
}


@router.message(Command("support_stats"))
async def cmd_support_stats(message: Message) -> None:
    """Админ-команда: краткая статистика AI-support за последние 24ч (intents, source, vpn_diagnosis)."""
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    try:
        intent_rows = db.get_support_conversation_intent_stats(hours=24)
        source_counts, vpn_diagnosis_counts = _parse_support_ai_log_for_stats(hours=24)
        handoff_rows = db.get_support_conversation_handoff_by_intent(hours=24)
        fallback_by_intent = _parse_support_ai_log_intent_fallback(hours=24)
    except Exception as e:
        log.error("[SupportStats] Failed to get stats: %r", e)
        await message.answer("Не удалось получить статистику. См. логи.")
        return

    lines = [f"<b>{SUPPORT_STATS_HEADER}</b>\n"]
    lines.append(f"<b>{SUPPORT_STATS_BY_SOURCE}</b>")
    for key in ("rule", "memory", "faq_match", "openai", "fallback"):
        lines.append(f"{key}: {source_counts.get(key, 0)}")
    lines.append("")
    lines.append(f"<b>{SUPPORT_STATS_TOP_INTENTS}</b>")
    for intent, cnt in intent_rows[:10]:
        label = SUPPORT_STATS_INTENT_LABELS.get(intent, intent)
        lines.append(f"{label}: {cnt}")
    lines.append("")
    lines.append(f"<b>{SUPPORT_STATS_TOP_VPN}</b>")
    sorted_diag = sorted(vpn_diagnosis_counts.items(), key=lambda x: -x[1])[:10]
    for diag, cnt in sorted_diag:
        label = SUPPORT_STATS_VPN_DIAG_LABELS.get(diag, diag)
        lines.append(f"{label}: {cnt}")
    if not sorted_diag:
        lines.append(SUPPORT_STATS_NONE)

    total_by_intent = {intent: cnt for intent, cnt in intent_rows}
    handoff_map = dict(handoff_rows)
    handoff_rates = [
        (intent, total, handoff_map.get(intent, 0))
        for intent, total in total_by_intent.items()
        if total > 0
    ]
    handoff_rates.sort(key=lambda x: -round(x[2] * 100 / x[1]))
    lines.append("")
    lines.append(f"<b>{SUPPORT_STATS_PROBLEM_HANDOFF}</b>")
    for intent, total, handoff in handoff_rates[:5]:
        rate = round(handoff * 100 / total)
        label = SUPPORT_STATS_INTENT_LABELS.get(intent, intent)
        lines.append(f"• {label}: {total} / handoff {handoff} ({rate}%)")
    if not handoff_rates:
        lines.append(SUPPORT_STATS_NONE)

    fallback_rates = [
        (intent, total, fallback_by_intent.get(intent, 0))
        for intent, total in total_by_intent.items()
        if total > 0
    ]
    fallback_rates.sort(key=lambda x: -round(x[2] * 100 / x[1]))
    lines.append("")
    lines.append(f"<b>{SUPPORT_STATS_PROBLEM_FALLBACK}</b>")
    for intent, total, fallback in fallback_rates[:5]:
        rate = round(fallback * 100 / total)
        label = SUPPORT_STATS_INTENT_LABELS.get(intent, intent)
        lines.append(f"• {label}: {total} / fallback {fallback} ({rate}%)")
    if not fallback_rates:
        lines.append(SUPPORT_STATS_NONE)

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("crm_report"))
async def cmd_crm_report(message: Message) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    days = 7
    parts = (message.text or "").strip().split()
    if len(parts) >= 2:
        try:
            days = int(parts[1])
            days = max(1, min(days, 90))
        except ValueError:
            pass

    try:
        report = db.get_crm_funnel_report(days=days)
    except Exception as e:
        log.error("[CrmReport] Failed to get report: %r", e)
        await message.answer("Не удалось получить отчёт. См. логи.")
        return

    r = report
    payments_count = r["welcome_after_first_payment"]
    handshake_count = r["handshake_user_connected"]
    conversion_pct = (handshake_count * 100 // payments_count) if payments_count > 0 else 0

    surveys_sent = r.get("no_handshake_survey", 0)
    answers_total = r.get("no_handshake_survey_answers_total", 0)
    response_rate = round(answers_total / surveys_sent * 100) if surveys_sent > 0 else 0

    vpn_ok_pct = ""
    if r["handshake_followup_10m"] > 0:
        vpn_ok_pct = f" ({100 * r['vpn_ok_clicked'] // r['handshake_followup_10m']}%)"
    ref_pct = ""
    if r["handshake_referral_nudge_3d"] > 0:
        ref_pct = f" ({100 * r['ref_nudge_clicked'] // r['handshake_referral_nudge_3d']}%)"

    text = (
        f"<b>CRM-отчёт за {days} дней</b>\n\n"
        "<b>Оплаты:</b>\n"
        f"• первые платные подписки: {payments_count}\n\n"
        "<b>Подключения:</b>\n"
        f"• первый handshake: {handshake_count}\n\n"
        "<b>Конверсия подключения:</b>\n"
        f"• оплата → VPN подключен: {conversion_pct}%\n\n"
        "<b>Воронка подключений:</b>\n"
        f"• follow-up через 10 минут: {r['handshake_followup_10m']}\n"
        f"• «Всё работает» нажали: {r['vpn_ok_clicked']}{vpn_ok_pct}\n"
        f"• follow-up через 2 часа: {r['handshake_followup_2h']}\n"
        f"• follow-up через 24 часа: {r['handshake_followup_24h']}\n"
        f"• referral follow-up через 3 дня: {r['handshake_referral_nudge_3d']}\n"
        f"• «Пригласить друга» нажали: {r['ref_nudge_clicked']}{ref_pct}\n\n"
        "<b>Воронка без handshake:</b>\n"
        f"• напоминание через 2 часа: {r['no_handshake_2h']}\n"
        f"• напоминание через 24 часа: {r['no_handshake_24h']}\n"
        f"• напоминание через 5 дней: {r['no_handshake_5d']}\n"
        f"• опрос причины отказа: {r['no_handshake_survey']}\n"
        f"• ответили на опрос: {answers_total}\n"
        f"• response rate: {response_rate}%\n\n"
        "<b>Причины отказа:</b>\n"
        f"• не разобрался с настройкой: {r.get('no_handshake_survey_answer_1', 0)}\n"
        f"• пока не нужен: {r.get('no_handshake_survey_answer_2', 0)}\n"
        f"• пользуюсь другим VPN: {r.get('no_handshake_survey_answer_3', 0)}\n"
        f"• дорого: {r.get('no_handshake_survey_answer_4', 0)}\n\n"
        "<b>Прочее:</b>\n"
        f"• первые оплаты после handshake: {r['first_paid_with_prior_handshake']}\n"
    )

    await message.answer(
        text,
        disable_web_page_preview=True,
    )


@router.message(Command("admin_cmd"))
async def cmd_admin_cmd(message: Message) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    text = (
        "🛠 <b>Админ-меню</b>\n\n"
        "Здесь можно посмотреть команды и выдать подписку вручную.\n\n"
        "Выбери действие кнопками ниже:"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="ℹ️ Описание команд",
                    callback_data="admcmd:info",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="➕ Выдать подписку (/add_sub)",
                    callback_data="admcmd:add_sub",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🕘 Последняя подписка",
                    callback_data="admcmd:last",
                ),
                InlineKeyboardButton(
                    text="📃 Список подписок",
                    callback_data="admcmd:list",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статистика IP-пула",
                    callback_data="admcmd:stats",
                ),
            ],
        ]
    )

    await message.answer(
        text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    await state.set_state(Broadcast.waiting_for_text)
    await message.answer(
        "Пришли текст рассылки одним сообщением.\n\n"
        "⚠️ Внимание: он будет отправлен всем пользователям, которые есть в базе.",
        disable_web_page_preview=True,
    )

@router.message(Command("promo_admin"))
async def cmd_promo_admin(message: Message, state: FSMContext) -> None:
    """
    Запускает мастер генерации промокодов для администратора.
    В конце мастер покажет сводку параметров и попросит подтверждение,
    после чего промокоды будут сгенерированы и сразу сохранены в таблицу promo_codes.
    """
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return
    
    promo_log.info(
        "[PromoAdmin] Wizard started by tg_id=%s",
        message.from_user.id if message.from_user else None,
    )

    await state.clear()
    await state.set_state(PromoAdmin.waiting_for_mode)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="♾ Многоразовый промокод (ручное имя)",
                    callback_data="promo_admin:mode:multi",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔑 Несколько одноразовых кодов",
                    callback_data="promo_admin:mode:single",
                ),
            ],
        ]
    )

    await message.answer(
        "Мастер генерации промокодов.\n\n"
        "Выбери тип промокода:\n"
        "• ♾ Многоразовый код (одно имя, лимиты по использованию).\n"
        "• 🔑 Пачка одноразовых случайных кодов.\n\n"
        "Нажми на нужный вариант ниже.",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )



@router.message(Broadcast.waiting_for_text)
async def broadcast_send(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        await state.clear()
        return

    text = message.text or ""
    text = text.strip()
    if not text:
        await message.answer("Текст пустой, рассылку отменяю.")
        await state.clear()
        return

    await state.clear()

    try:
        users = db.get_all_telegram_users()
    except Exception as e:
        log.error("[Broadcast] Failed to fetch users: %s", repr(e))
        await message.answer(
            "Не удалось получить список пользователей для рассылки. Проверь логи сервера.",
            disable_web_page_preview=True,
        )
        return

    if not users:
        await message.answer(
            "Список пользователей пуст. Некому отправлять рассылку.",
            disable_web_page_preview=True,
        )
        return

    total = len(users)
    if total > MAX_BROADCAST_USERS:
        log.warning(
            "[Broadcast] User count %s exceeds limit %s, truncating",
            total,
            MAX_BROADCAST_USERS,
        )
        users = users[:MAX_BROADCAST_USERS]
        total = len(users)
        await message.answer(
            f"Слишком много пользователей для одной рассылки. Отправлю первые {total}.",
            disable_web_page_preview=True,
        )

    success = 0
    failed = 0
    batch_count = 0

    await message.answer(
        f"Начинаю рассылку по {total} пользователям...\n"
        "Это может занять некоторое время.",
        disable_web_page_preview=True,
    )

    for user in users:
        chat_id = user.get("telegram_user_id")
        if not chat_id:
            continue

        ok = await safe_send_message(
            bot=message.bot,
            chat_id=chat_id,
            text=text,
            disable_web_page_preview=True,
        )
        if ok:
            success += 1
        else:
            failed += 1

        batch_count += 1
        if batch_count >= BROADCAST_BATCH_SIZE:
            await asyncio.sleep(BROADCAST_BATCH_SLEEP)
            batch_count = 0

    await message.answer(
        f"Рассылка завершена.\n"
        f"Успешно: {success}\n"
        f"Ошибок: {failed}",
        disable_web_page_preview=True,
    )


@router.message(Command("broadcast_list"))
async def cmd_broadcast_list(message: Message, state: FSMContext) -> None:
    """Рассылка по списку telegram_user_id из файла (например, 155 пользователей без handshake)."""
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return
    await state.clear()
    await state.set_state(BroadcastList.waiting_for_file)
    await message.answer(
        "Пришли файл (.txt), в котором на каждой строке — один <code>telegram_user_id</code>.\n\n"
        "Можно выгрузить список из БД и сохранить в текстовый файл.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@router.message(BroadcastList.waiting_for_file, F.document)
async def broadcast_list_file(message: Message, state: FSMContext) -> None:
    if not message.document or not message.bot:
        return
    try:
        file = await message.bot.get_file(message.document.file_id)
        buf = io.BytesIO()
        await message.bot.download_file(file.file_path, buf)
        buf.seek(0)
        raw = buf.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log.error("[BroadcastList] Failed to download file: %s", repr(e))
        await message.answer("Не удалось прочитать файл. Пришли другой файл.")
        return

    ids: List[int] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ids.append(int(line))
        except ValueError:
            continue

    if not ids:
        await message.answer("В файле не найдено ни одного числового ID. Пришли другой файл.")
        return

    await state.update_data(broadcast_list_ids=ids)
    await state.set_state(BroadcastList.waiting_for_text)
    await message.answer(
        f"Принято <b>{len(ids)}</b> ID. Теперь пришли текст сообщения одной штукой.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@router.message(BroadcastList.waiting_for_text, F.text)
async def broadcast_list_send(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await state.clear()
        return
    data = await state.get_data()
    ids: List[int] = data.get("broadcast_list_ids") or []
    await state.clear()

    if not ids:
        await message.answer("Список ID потерян. Начни заново: /broadcast_list")
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст пустой, рассылку отменяю.")
        return

    total = len(ids)
    if total > MAX_BROADCAST_USERS:
        ids = ids[:MAX_BROADCAST_USERS]
        total = len(ids)
        await message.answer(f"Список обрезан до {total} пользователей.")

    success = 0
    failed = 0
    batch_count = 0

    await message.answer(
        f"Начинаю рассылку по {total} пользователям...",
        disable_web_page_preview=True,
    )

    for chat_id in ids:
        ok = await safe_send_message(
            bot=message.bot,
            chat_id=chat_id,
            text=text,
            disable_web_page_preview=True,
        )
        if ok:
            success += 1
        else:
            failed += 1
        batch_count += 1
        if batch_count >= BROADCAST_BATCH_SIZE:
            await asyncio.sleep(BROADCAST_BATCH_SLEEP)
            batch_count = 0

    await message.answer(
        f"Рассылка по списку завершена.\nУспешно: {success}\nОшибок: {failed}",
        disable_web_page_preview=True,
    )


# --- /bonus_list: начислить 100 баллов списку и отправить сообщение ---

BONUS_LIST_POINTS = 100
BONUS_LIST_REASON = "promo"
BONUS_LIST_SOURCE = "admin"
BONUS_LIST_META = {"campaign": "never_connected_100"}


@router.message(Command("bonus_list"))
async def cmd_bonus_list(message: Message, state: FSMContext) -> None:
    """Начислить каждому из списка 100 баллов и отправить сообщение (например, 155 юзерам без handshake)."""
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return
    await state.clear()
    await state.set_state(BonusList.waiting_for_file)
    await message.answer(
        "Пришли файл (.txt) с одним <code>telegram_user_id</code> на строку.\n\n"
        f"Каждому будет начислено <b>{BONUS_LIST_POINTS} баллов</b> и отправлено твоё сообщение.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@router.message(BonusList.waiting_for_file, F.document)
async def bonus_list_file(message: Message, state: FSMContext) -> None:
    if not message.document or not message.bot:
        return
    try:
        file = await message.bot.get_file(message.document.file_id)
        buf = io.BytesIO()
        await message.bot.download_file(file.file_path, buf)
        buf.seek(0)
        raw = buf.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log.error("[BonusList] Failed to download file: %s", repr(e))
        await message.answer("Не удалось прочитать файл. Пришли другой файл.")
        return

    ids = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ids.append(int(line))
        except ValueError:
            continue

    if not ids:
        await message.answer("В файле не найдено ни одного числового ID. Пришли другой файл.")
        return

    await state.update_data(bonus_list_ids=ids)
    await state.set_state(BonusList.waiting_for_text)
    await message.answer(
        f"Принято <b>{len(ids)}</b> ID. Каждому начислится {BONUS_LIST_POINTS} баллов.\n"
        "Теперь пришли текст сообщения одной штукой.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@router.message(BonusList.waiting_for_text, F.text)
async def bonus_list_send(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await state.clear()
        return
    data = await state.get_data()
    ids: List[int] = data.get("bonus_list_ids") or []
    await state.clear()

    if not ids:
        await message.answer("Список ID потерян. Начни заново: /bonus_list")
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст пустой. Начислю баллы без сообщения? Пришли текст или /cancel.")
        return

    total = len(ids)
    if total > MAX_BROADCAST_USERS:
        ids = ids[:MAX_BROADCAST_USERS]
        total = len(ids)
        await message.answer(f"Список обрезан до {total} пользователей.")

    points_ok = 0
    points_fail = 0
    msg_ok = 0
    msg_fail = 0
    batch_count = 0

    await message.answer(
        f"Начисляю {BONUS_LIST_POINTS} баллов и отправляю сообщение по {total} пользователям...",
        disable_web_page_preview=True,
    )

    for chat_id in ids:
        res = db.add_points(
            chat_id,
            BONUS_LIST_POINTS,
            BONUS_LIST_REASON,
            BONUS_LIST_SOURCE,
            meta=BONUS_LIST_META,
        )
        if res.get("ok"):
            points_ok += 1
        else:
            points_fail += 1

        ok = await safe_send_message(
            bot=message.bot,
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        if ok:
            msg_ok += 1
        else:
            msg_fail += 1

        batch_count += 1
        if batch_count >= BROADCAST_BATCH_SIZE:
            await asyncio.sleep(BROADCAST_BATCH_SLEEP)
            batch_count = 0

    await message.answer(
        f"Готово.\n"
        f"Баллы: начислено {points_ok}, ошибок {points_fail}.\n"
        f"Сообщение: доставлено {msg_ok}, ошибок {msg_fail}.",
        disable_web_page_preview=True,
    )


@router.message(Command("admin_last"))
async def cmd_admin_last(message: Message) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    subs = db.get_last_subscriptions(limit=1)
    if not subs:
        await message.answer("Подписок в базе пока нет.")
        return

    sub = subs[0]
    sub_id = sub.get("id")
    telegram_user_id = sub.get("telegram_user_id")
    telegram_user_name = sub.get("telegram_user_name")
    vpn_ip = sub.get("vpn_ip")
    active = sub.get("active")
    expires_at = sub.get("expires_at")
    last_event_name = sub.get("last_event_name")

    if isinstance(expires_at, datetime):
        expires_str = fmt_date(expires_at)
    else:
        expires_str = str(expires_at)

    if telegram_user_name:
        tg_display = f"{telegram_user_id} ({telegram_user_name})"
    else:
        tg_display = str(telegram_user_id)

    text = (
        "Последняя подписка:\n\n"
        f"ID: {sub_id}\n"
        f"TG: {tg_display}\n"
        f"IP: {vpn_ip}\n"
        f"active={active}\n"
        f"до {expires_str}\n"
        f"event={last_event_name}\n\n"
        "Можно управлять этой подпиской кнопками ниже или командами:\n"
        f"/admin_activate {sub_id}\n"
        f"/admin_deactivate {sub_id}\n"
        f"/admin_delete {sub_id}"
    )


    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Активировать",
                    callback_data=f"adm:act:{sub_id}",
                ),
                InlineKeyboardButton(
                    text="⛔ Деактивировать",
                    callback_data=f"adm:deact:{sub_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"adm:del:{sub_id}",
                )
            ],
        ]
    )

    await message.answer(
        text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

@router.message(Command("admin_sub"))
async def cmd_admin_sub(message: Message) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /admin_sub ID_подписки")
        return

    try:
        sub_id = int(parts[1])
    except ValueError:
        await message.answer("ID подписки должен быть числом.")
        return

    sub = db.get_subscription_by_id(sub_id=sub_id)
    if not sub:
        await message.answer("Подписка не найдена.")
        return

    telegram_user_id = sub.get("telegram_user_id")
    telegram_user_name = sub.get("telegram_user_name")
    vpn_ip = sub.get("vpn_ip")
    active = sub.get("active")
    expires_at = sub.get("expires_at")
    last_event_name = sub.get("last_event_name")

    if isinstance(expires_at, datetime):
        expires_str = fmt_date(expires_at)
    else:
        expires_str = str(expires_at)

    if telegram_user_name:
        tg_display = f"{telegram_user_id} ({telegram_user_name})"
    else:
        tg_display = str(telegram_user_id)

    text = (
        "Подписка:\n\n"
        f"ID: {sub_id}\n"
        f"TG: {tg_display}\n"
        f"IP: {vpn_ip}\n"
        f"active={active}\n"
        f"до {expires_str}\n"
        f"event={last_event_name}\n\n"
        "Можно управлять этой подпиской кнопками ниже или командами:\n"
        f"/admin_activate {sub_id}\n"
        f"/admin_deactivate {sub_id}\n"
        f"/admin_delete {sub_id}"
    )


    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Активировать",
                    callback_data=f"adm:act:{sub_id}",
                ),
                InlineKeyboardButton(
                    text="⛔ Деактивировать",
                    callback_data=f"adm:deact:{sub_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"adm:del:{sub_id}",
                )
            ],
        ]
    )

    await message.answer(
        text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

@router.message(Command("admin_list"))
async def cmd_admin_list(message: Message) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    # Берём последние 30 подписок
    subs = db.get_last_subscriptions(limit=30)
    if not subs:
        await message.answer("Подписок в базе пока нет.")
        return

    keyboard_rows = []

    for sub in subs:
        sub_id = sub.get("id")
        telegram_user_id = sub.get("telegram_user_id")
        telegram_user_name = sub.get("telegram_user_name")
        vpn_ip = sub.get("vpn_ip")
        active = sub.get("active")
        expires_at = sub.get("expires_at")

        if isinstance(expires_at, datetime):
            expires_str = fmt_date(expires_at, with_time=False)
        else:
            expires_str = str(expires_at)

        if telegram_user_name:
            tg_display = f"{telegram_user_id} ({telegram_user_name})"
        else:
            tg_display = str(telegram_user_id)

        ip_display = vpn_ip if vpn_ip else "-"

        status_text = "активна" if active else "неактивна"

        # строка 1: ID + TG
        line1 = f"ID {sub_id} | TG {tg_display}"
        # строка 2: IP + дата + статус
        line2 = f"IP {ip_display} | до {expires_str} | {status_text}"

        # первая кнопка — ID и TG
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=line1,
                    callback_data=f"adminlist:sub:{sub_id}",
                )
            ]
        )
        # вторая кнопка — IP, дата, статус
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=line2,
                    callback_data=f"adminlist:sub:{sub_id}",
                )
            ]
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    await message.answer(
        "Последние подписки (нажми на нужную, чтобы открыть подробности):",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )



@router.callback_query(F.data.startswith("adminlist:sub:"))
async def admin_list_sub_details(callback: CallbackQuery) -> None:
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)
    if callback.from_user is None or callback.from_user.id != admin_id:
        await callback.answer("Эта кнопка только для администратора.", show_alert=True)
        return

    data = callback.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    _, _, sub_id_str = parts

    try:
        sub_id = int(sub_id_str)
    except ValueError:
        await callback.answer("Некорректный ID.", show_alert=True)
        return

    sub = db.get_subscription_by_id(sub_id=sub_id)
    if not sub:
        await callback.answer("Подписка не найдена.", show_alert=True)
        return

    telegram_user_id = sub.get("telegram_user_id")
    telegram_user_name = sub.get("telegram_user_name")
    vpn_ip = sub.get("vpn_ip")
    active = sub.get("active")
    expires_at = sub.get("expires_at")
    last_event_name = sub.get("last_event_name")

    if isinstance(expires_at, datetime):
        expires_str = fmt_date(expires_at)
    else:
        expires_str = str(expires_at)

    if telegram_user_name:
        tg_display = f"{telegram_user_id} ({telegram_user_name})"
    else:
        tg_display = str(telegram_user_id)

    text = (
        "Подписка:\n\n"
        f"ID: {sub_id}\n"
        f"TG: {tg_display}\n"
        f"IP: {vpn_ip}\n"
        f"active={active}\n"
        f"до {expires_str}\n"
        f"event={last_event_name}\n\n"
        "Можно управлять этой подпиской кнопками ниже или командами:\n"
        f"/admin_activate {sub_id}\n"
        f"/admin_deactivate {sub_id}\n"
        f"/admin_delete {sub_id}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Активировать",
                    callback_data=f"adm:act:{sub_id}",
                ),
                InlineKeyboardButton(
                    text="⛔ Деактивировать",
                    callback_data=f"adm:deact:{sub_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=f"adm:del:{sub_id}",
                )
            ],
        ]
    )

    await callback.message.answer(
        text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

    await callback.answer()
 

@router.message(Command("add_sub"))
async def cmd_add_sub(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    await state.set_state(AdminAddSub.waiting_for_target)
    await message.answer(
        "Перешли сюда <b>любое сообщение</b> от пользователя, которому нужно выдать VPN-доступ.\n\n"
        "Либо отправь его <b>числовой Telegram ID</b> вручную.",
        disable_web_page_preview=True,
    )


@router.message(AdminAddSub.waiting_for_target)
async def admin_add_sub_get_target(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        await state.clear()
        return

    target_id = None
    target_username = None

    # 1) Админ ответил на сообщение пользователя (reply в чате, где есть бот и пользователь)
    if (
        message.reply_to_message
        and message.reply_to_message.from_user
        and not message.reply_to_message.from_user.is_bot
    ):
        target_id = message.reply_to_message.from_user.id
        target_username = message.reply_to_message.from_user.username
        log.info(
            "[AdminAddSub] target from reply: id=%s username=%s",
            target_id,
            target_username,
        )

    # 2) Пересланное сообщение от пользователя
    if target_id is None and message.forward_from and message.forward_from.id:
        target_id = message.forward_from.id
        target_username = message.forward_from.username
        log.info(
            "[AdminAddSub] target from forward: id=%s username=%s",
            target_id,
            target_username,
        )

    # 3) Попробуем вытащить числовой Telegram ID из текста сообщения
    if target_id is None and message.text:
        raw_text = message.text.strip()

        # вариант "чисто цифры"
        if raw_text.isdigit():
            try:
                target_id = int(raw_text)
                log.info("[AdminAddSub] target from pure digits text: %s", target_id)
            except ValueError:
                target_id = None
        else:
            # иногда админ копирует строку вида:
            # "Твой Telegram ID: 123456789"
            # вытащим из неё все цифры подряд
            digits_only = "".join(ch for ch in raw_text if ch.isdigit())
            if digits_only:
                try:
                    target_id = int(digits_only)
                    log.info("[AdminAddSub] target from mixed text digits: %s", target_id)
                except ValueError:
                    target_id = None

    # 4) Спецкейс: forward_sender_name есть, а forward_from нет — у пользователя включена приватность пересылки
    if (
        target_id is None
        and message.forward_from is None
        and getattr(message, "forward_sender_name", None)
    ):
        log.info(
            "[AdminAddSub] forward_sender_name=%r, но forward_from=None — включена приватность пересылки, id недоступен",
            message.forward_sender_name,
        )

    if not target_id:
        await message.answer(
            "Не смог определить пользователя.\n\n"
            "Возможные причины:\n"
            "• У пользователя включена приватность пересланных сообщений — бот не видит его ID.\n"
            "• Либо не было пересланного сообщения / числового ID.\n\n"
            "Попроси пользователя написать боту (например, /start или /my_id) и перешли мне его числовой Telegram ID.",
            disable_web_page_preview=True,
        )
        return


    await state.update_data(
        target_telegram_user_id=target_id,
        target_telegram_user_name=target_username,
    )


    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="1 месяц",
                    callback_data="addsub:period:1m",
                ),
                InlineKeyboardButton(
                    text="3 месяца",
                    callback_data="addsub:period:3m",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="6 месяцев",
                    callback_data="addsub:period:6m",
                ),
                InlineKeyboardButton(
                    text="1 год",
                    callback_data="addsub:period:1y",
                ),
            ],
        ]
    )

    await state.set_state(AdminAddSub.waiting_for_period)

    if target_username:
        user_line = (
            f"Определён пользователь: <code>{target_id}</code> "
            f"(@{target_username}).\n\n"
        )
    else:
        user_line = (
            f"Определён пользователь с TG ID: <code>{target_id}</code>.\n\n"
        )

    await message.answer(
        user_line + "Теперь выбери срок подписки:",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )




@router.message(Command("admin_deactivate"))
async def cmd_admin_deactivate(message: Message) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /admin_deactivate ID_подписки")
        return

    try:
        sub_id = int(parts[1])
    except ValueError:
        await message.answer("ID подписки должен быть числом.")
        return

    sub = db.deactivate_subscription_by_id(
        sub_id=sub_id,
        event_name="admin_deactivate",
    )
    if not sub:
        await message.answer("Подписка не найдена или уже деактивирована.")
        return

    pub_key = sub.get("wg_public_key")
    if pub_key:
        try:
            log.info("[TelegramAdmin] Remove peer pubkey=%s for sub_id=%s", pub_key, sub_id)
            wg.remove_peer(pub_key)
        except Exception as e:
            log.error(
                "[TelegramAdmin] Failed to remove peer from WireGuard for sub_id=%s: %s",
                sub_id,
                repr(e),
            )

    telegram_user_id = sub.get("telegram_user_id")
    telegram_user_name = sub.get("telegram_user_name")

    if telegram_user_name:
        tg_display = f"{telegram_user_id} ({telegram_user_name})"
    else:
        tg_display = str(telegram_user_id)

    await message.answer(
        f"Подписка с ID {sub_id} деактивирована.\n"
        f"Пользователь TG: {tg_display}\n"
        f"VPN IP: {vpn_ip}\n"
        f"Peer в WireGuard удалён (или его не было).",
        disable_web_page_preview=True,
    )

    # уведомляем пользователя о ручной деактивации
    if telegram_user_id:
        try:
            await send_text_message(
                telegram_user_id=telegram_user_id,
                text=(
                    "⛔️ Доступ к MaxNet VPN был отключён администратором.\n\n"
                    "Если это произошло по ошибке — напиши в поддержку."
                ),
            )
        except Exception as e:
            log.error(
                "[AdminDeactivate] Failed to notify user %s: %r",
                telegram_user_id,
                e,
            )



@router.message(Command("admin_activate"))
async def cmd_admin_activate(message: Message) -> None:
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /admin_activate ID_подписки")
        return

    try:
        sub_id = int(parts[1])
    except ValueError:
        await message.answer("ID подписки должен быть числом.")
        return

    # сначала берём подписку, чтобы узнать telegram_user_id
    sub_before = db.get_subscription_by_id(sub_id=sub_id)
    if not sub_before:
        await message.answer("Подписка не найдена.")
        return

    telegram_user_id = sub_before.get("telegram_user_id")

    # ⚠️ СНАЧАЛА отключаем все старые активные подписки пользователя
    if telegram_user_id:
        deactivate_existing_active_subscriptions(
            telegram_user_id=telegram_user_id,
            reason="auto_replace_admin_activate",
        )

    # теперь активируем нужную подписку (при реактивации выделяется новый IP)
    try:
        sub = db.activate_subscription_by_id(
            sub_id=sub_id,
            event_name="admin_activate",
        )
    except RuntimeError as e:
        if "No free VPN IPs" in str(e):
            await message.answer("Нет свободных IP в пуле. Активация невозможна.")
        else:
            raise
        return

    if not sub:
        await message.answer("Подписка не найдена или уже активна.")
        return

    pub_key = sub.get("wg_public_key")
    vpn_ip = sub.get("vpn_ip")
    telegram_user_id = sub.get("telegram_user_id")
    telegram_user_name = sub.get("telegram_user_name")

    if not pub_key or not vpn_ip:
        await message.answer("У подписки нет wg_public_key или vpn_ip, не могу добавить peer.")
        return

    allowed_ip = f"{vpn_ip}/{settings.WG_CLIENT_NETWORK_CIDR}"

    try:
        log.info(
            "[TelegramAdmin] Add peer pubkey=%s ip=%s for sub_id=%s",
            pub_key,
            allowed_ip,
            sub_id,
        )
        wg.add_peer(
            public_key=pub_key,
            allowed_ip=allowed_ip,
            telegram_user_id=telegram_user_id,
        )
    except Exception as e:
        log.error(
            "[TelegramAdmin] Failed to add peer to WireGuard for sub_id=%s: %s",
            sub_id,
            repr(e),
        )
        await message.answer(
            "Подписка в базе активирована, но при добавлении peer в WireGuard произошла ошибка.\n"
            "Проверь логи и состояние wg вручную.",
            disable_web_page_preview=True,
        )
        return

    if telegram_user_name:
        tg_display = f"{telegram_user_id} ({telegram_user_name})"
    else:
        tg_display = str(telegram_user_id)

    await message.answer(
        f"Подписка с ID {sub_id} активирована.\n"
        f"Пользователь TG: {tg_display}\n"
        f"VPN IP: {vpn_ip}\n"
        f"Peer в WireGuard добавлен.\n"
        f"⚠️ Клиент должен заново скачать конфиг (IP изменился).",
        disable_web_page_preview=True,
    )


@router.message(Command("admin_delete"))
async def cmd_admin_delete(message: Message) -> None:

    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /admin_delete ID_подписки")
        return

    try:
        sub_id = int(parts[1])
    except ValueError:
        await message.answer("ID подписки должен быть числом.")
        return

    sub = db.get_subscription_by_id(sub_id=sub_id)
    if not sub:
        await message.answer("Подписка не найдена.")
        return

    pub_key = sub.get("wg_public_key")
    vpn_ip = sub.get("vpn_ip")
    telegram_user_id = sub.get("telegram_user_id")
    telegram_user_name = sub.get("telegram_user_name")

    if pub_key:
        try:
            log.info("[TelegramAdmin] Remove peer (delete) pubkey=%s for sub_id=%s", pub_key, sub_id)
            wg.remove_peer(pub_key)
        except Exception as e:
            log.error(
                "[TelegramAdmin] Failed to remove peer (delete) from WireGuard for sub_id=%s: %s",
                sub_id,
                repr(e),
            )

    deleted = db.delete_subscription_by_id(sub_id=sub_id)
    if not deleted:
        await message.answer(
            "Не удалось удалить подписку из базы (возможно, её уже удалили). "
            "Peer в WireGuard, если был, мы уже попытались удалить.",
            disable_web_page_preview=True,
        )
        return

    if telegram_user_name:
        tg_display = f"{telegram_user_id} ({telegram_user_name})"
    else:
        tg_display = str(telegram_user_id)

    await message.answer(
        f"Подписка с ID {sub_id} полностью удалена.\n"
        f"Пользователь TG: {tg_display}\n"
        f"VPN IP: {vpn_ip}\n"
        f"Peer в WireGuard удалён (если был).",
        disable_web_page_preview=True,
    )


@router.message(Command("admin_regenerate_vpn"))
async def cmd_admin_regenerate_vpn(message: Message) -> None:
    """
    Восстановление VPN-доступа по Telegram ID: новые WG-ключи, тот же IP,
    конфиг отправляется пользователю в Telegram.
    """
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer(
            "Использование: /admin_regenerate_vpn <telegram_user_id>\n"
            "Пример: /admin_regenerate_vpn 8519013399",
            disable_web_page_preview=True,
        )
        return

    try:
        telegram_user_id = int(parts[1])
    except ValueError:
        await message.answer("telegram_user_id должен быть числом.")
        return

    sub = db.get_latest_subscription_for_telegram(telegram_user_id=telegram_user_id)
    if not sub:
        await message.answer(
            f"У пользователя {telegram_user_id} нет активной подписки "
            "(active = true и expires_at > now).",
            disable_web_page_preview=True,
        )
        return

    sub_id = sub["id"]
    old_public_key = sub.get("wg_public_key")
    vpn_ip = sub.get("vpn_ip")
    if not vpn_ip or not old_public_key:
        await message.answer(
            f"Подписка id={sub_id}: нет vpn_ip или wg_public_key, восстановление невозможно.",
            disable_web_page_preview=True,
        )
        return

    try:
        new_private_key, new_public_key = await asyncio.to_thread(wg.generate_keypair)
        log.info(
            "[AdminRegenerateVPN] tg_id=%s sub_id=%s: new keys generated",
            telegram_user_id,
            sub_id,
        )
    except Exception as e:
        log.error("[AdminRegenerateVPN] tg_id=%s generate_keypair failed: %r", telegram_user_id, e)
        await message.answer(f"Ошибка генерации ключей: {e!r}")
        return

    db.update_subscription_wg_keys(
        sub_id=sub_id,
        wg_private_key=new_private_key,
        wg_public_key=new_public_key,
    )
    log.info(
        "[AdminRegenerateVPN] tg_id=%s sub_id=%s: keys updated in DB",
        telegram_user_id,
        sub_id,
    )

    try:
        await asyncio.to_thread(wg.remove_peer, old_public_key)
        log.info(
            "[AdminRegenerateVPN] tg_id=%s sub_id=%s: old peer removed",
            telegram_user_id,
            sub_id,
        )
    except Exception as e:
        log.info(
            "[AdminRegenerateVPN] tg_id=%s sub_id=%s: remove old peer skipped (peer absent or error): %r",
            telegram_user_id,
            sub_id,
            e,
        )

    allowed_ip = f"{vpn_ip}/{settings.WG_CLIENT_NETWORK_CIDR}"
    try:
        await asyncio.to_thread(
            wg.add_peer,
            new_public_key,
            allowed_ip,
            telegram_user_id,
        )
        log.info(
            "[AdminRegenerateVPN] tg_id=%s sub_id=%s: new peer added ip=%s",
            telegram_user_id,
            sub_id,
            allowed_ip,
        )
    except Exception as e:
        log.error(
            "[AdminRegenerateVPN] tg_id=%s sub_id=%s add_peer failed: %r",
            telegram_user_id,
            sub_id,
            e,
        )
        await message.answer(
            f"Ключи в БД обновлены, но не удалось добавить peer в WireGuard: {e!r}. "
            "Проверьте логи и wg0.",
            disable_web_page_preview=True,
        )
        return

    config_text = wg.build_client_config(
        client_private_key=new_private_key,
        client_ip=vpn_ip,
    )
    try:
        await send_vpn_config_to_user(
            telegram_user_id=telegram_user_id,
            config_text=config_text,
            caption="Восстановленный доступ к MaxNet VPN. Файл vpn.conf — в этом сообщении. QR-код — в следующем.",
        )
        log.info(
            "[AdminRegenerateVPN] tg_id=%s sub_id=%s: config sent to user",
            telegram_user_id,
            sub_id,
        )
    except Exception as e:
        log.warning(
            "[AdminRegenerateVPN] tg_id=%s sub_id=%s: failed to send config to user: %r",
            telegram_user_id,
            sub_id,
            e,
        )
        await message.answer(
            f"VPN доступ восстановлен (ключи и peer обновлены). "
            f"Не удалось отправить конфиг пользователю в TG: {e!r}",
            disable_web_page_preview=True,
        )
        return

    await message.answer(
        f"Готово. Пользователь {telegram_user_id} (sub_id={sub_id}, ip={vpn_ip}): "
        "новые ключи, peer обновлён, конфиг отправлен в Telegram.",
        disable_web_page_preview=True,
    )


@router.message(Command("admin_resend_config"))
async def cmd_admin_resend_config(message: Message) -> None:
    """
    Переотправка текущего конфига пользователю без перегенерации ключей.
    Полезно, если конфиг не дошёл при создании подписки.
    """
    if not is_admin(message):
        await message.answer("Эта команда доступна только администратору.")
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer(
            "Использование: /admin_resend_config <telegram_user_id>\n"
            "Пример: /admin_resend_config 5996761590",
            disable_web_page_preview=True,
        )
        return

    try:
        telegram_user_id = int(parts[1])
    except ValueError:
        await message.answer("telegram_user_id должен быть числом.")
        return

    sub = db.get_latest_subscription_for_telegram(telegram_user_id=telegram_user_id)
    if not sub:
        await message.answer(
            f"У пользователя {telegram_user_id} нет активной подписки "
            "(active = true и expires_at > now).",
            disable_web_page_preview=True,
        )
        return

    sub_id = sub["id"]
    vpn_ip = sub.get("vpn_ip")
    private_key = sub.get("wg_private_key")

    if not vpn_ip or not private_key:
        await message.answer(
            f"Подписка id={sub_id}: нет vpn_ip или wg_private_key, переотправка невозможна.",
            disable_web_page_preview=True,
        )
        return

    config_text = wg.build_client_config(
        client_private_key=private_key,
        client_ip=vpn_ip,
    )

    try:
        await send_vpn_config_to_user(
            telegram_user_id=telegram_user_id,
            config_text=config_text,
            caption="Повторная отправка конфига MaxNet VPN. Файл vpn.conf — в этом сообщении. QR-код — в следующем.",
        )
        log.info(
            "[AdminResendConfig] tg_id=%s sub_id=%s: config resent",
            telegram_user_id,
            sub_id,
        )
    except Exception as e:
        log.warning(
            "[AdminResendConfig] tg_id=%s sub_id=%s: failed to resend config: %r",
            telegram_user_id,
            sub_id,
            e,
        )
        await message.answer(
            f"Не удалось отправить конфиг пользователю {telegram_user_id}: {e!r}",
            disable_web_page_preview=True,
        )
        return

    await message.answer(
        f"Конфиг переотправлен пользователю {telegram_user_id} (sub_id={sub_id}, ip={vpn_ip}).",
        disable_web_page_preview=True,
    )


# Обработчик кнопок "✅ Выдать демо-доступ" / "❌ Отказать"
@router.callback_query(F.data.startswith("demo:"))
async def demo_request_admin_callback(callback: CallbackQuery, state: FSMContext) -> None:
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)
    if callback.from_user is None or callback.from_user.id != admin_id:
        await callback.answer("Эта кнопка только для администратора.", show_alert=True)
        return

    data = callback.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    _, action, user_id_str = parts

    try:
        target_id = int(user_id_str)
    except ValueError:
        await callback.answer("Некорректный ID пользователя.", show_alert=True)
        return

    if action == "approve":
        # Проверяем, нет ли у пользователя уже активной подписки
        existing_sub = db.get_latest_subscription_for_telegram(telegram_user_id=target_id)
        if existing_sub:
            expires_at = existing_sub.get("expires_at")
            if isinstance(expires_at, datetime):
                expires_str = fmt_date(expires_at)
            else:
                expires_str = str(expires_at)
            await callback.message.edit_text(
                f"⚠️ У пользователя <code>{target_id}</code> уже есть активная подписка до <b>{expires_str}</b>.\n\n"
                "Демо-доступ не выдан.",
            )
            await callback.answer("У пользователя уже есть подписка", show_alert=True)
            return

        target_username = None
        try:
            chat = await callback.bot.get_chat(target_id)
            target_username = getattr(chat, "username", None)
        except Exception as e:
            log.error("[Demo] Failed to fetch username for %s: %s", target_id, repr(e))

        await state.set_state(AdminAddSub.waiting_for_period)
        await state.update_data(
            target_telegram_user_id=target_id,
            target_telegram_user_name=target_username,
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="3 дня", callback_data="addsub:period:3d"),
                    InlineKeyboardButton(text="7 дней", callback_data="addsub:period:7d"),
                ],
            ]
        )

        if target_username:
            user_line = f"Пользователь: <code>{target_id}</code> (@{target_username}).\n\n"
        else:
            user_line = f"Пользователь с TG ID: <code>{target_id}</code>.\n\n"

        await callback.message.edit_text(
            "✅ Запрос демо-доступа одобрен.\n\n" + user_line + "Выбери срок демо-подписки:",
            reply_markup=keyboard,
        )
        await callback.answer()
        return

    if action == "deny":
        deny_text = (
            "Привет!\n\n"
            "Спасибо за интерес к MaxNet VPN. "
            "К сожалению, в текущем месяце все бесплатные демо-доступы уже израсходованы.\n\n"
            "Ты можешь оформить платную подписку командами /buy или /buy_crypto "
            "или вернуться позже — возможно, появятся новые свободные слоты."
        )


        try:
            await callback.bot.send_message(
                chat_id=target_id,
                text=deny_text,
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.error("[Demo] Failed to send deny message to user %s: %s", target_id, repr(e))

        await callback.message.edit_text(
            f"❌ Отказ по демо-доступу для пользователя <code>{target_id}</code> отправлен.",
        )
        await callback.answer()
        return

    await callback.answer("Неизвестное действие.", show_alert=True)

    
@router.callback_query(AdminAddSub.waiting_for_period, F.data.startswith("addsub:period:"))
async def admin_add_sub_choose_period(callback: CallbackQuery, state: FSMContext) -> None:
    data = callback.data
    parts = data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    _, _, period_code = parts

    # Определяем период подписки
    if period_code == "3d":
        days = 3
        period_label = "3 дня"
    elif period_code == "7d":
        days = 7
        period_label = "7 дней"
    elif period_code == "1m":
        days = 30
        period_label = "1 месяц"
    elif period_code == "3m":
        days = 90
        period_label = "3 месяца"
    elif period_code == "6m":
        days = 180
        period_label = "6 месяцев"
    elif period_code == "1y":
        days = 365
        period_label = "1 год"
    else:
        await callback.answer("Неизвестный срок подписки.", show_alert=True)
        return

    # убираем инлайн-кнопки выбора срока с исходного сообщения
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        log.error(
            "[TelegramAdmin] Failed to clear inline keyboard for addsub period: %s",
            repr(e),
        )

    state_data = await state.get_data()
    target_id = state_data.get("target_telegram_user_id")
    target_username = state_data.get("target_telegram_user_name")

    if not target_id:
        await callback.answer("Не удалось получить данные пользователя, начни /add_sub заново.", show_alert=True)
        await state.clear()
        return

    now = datetime.utcnow()
    expires_at = now + timedelta(days=days)

    # ⚠️ Автоматически отключаем старые активные подписки пользователя
    deactivate_existing_active_subscriptions(
        telegram_user_id=target_id,
        reason="auto_replace_manual",
    )

    # Генерим ключи и IP
    client_priv, client_pub = wg.generate_keypair()
    client_ip = wg.generate_client_ip()
    allowed_ip = f"{client_ip}/{settings.WG_CLIENT_NETWORK_CIDR}"

    # Добавляем peer в WireGuard
    try:
        log.info(
            "[TelegramAdmin] Add peer (manual) pubkey=%s ip=%s for tg_id=%s",
            client_pub,
            allowed_ip,
            target_id,
        )
        wg.add_peer(
            public_key=client_pub,
            allowed_ip=allowed_ip,
            telegram_user_id=target_id,
        )
    except Exception as e:
        try:
            db.release_ip_in_pool(client_ip)
        except Exception:
            pass
        log.error(
            "[TelegramAdmin] Failed to add peer (manual) to WireGuard for tg_id=%s: %s",
            target_id,
            repr(e),
        )
        await callback.answer("Ошибка при добавлении peer в WireGuard. Подписка не создана.", show_alert=True)
        await state.clear()
        return

    # Записываем подписку в БД (правильная сигнатура)
    try:
        db.insert_subscription(
            tribute_user_id=0,
            telegram_user_id=target_id,
            telegram_user_name=target_username,
            subscription_id=0,
            period_id=0,
            period=f"admin_{period_code}",
            channel_id=0,
            channel_name="Admin manual",
            vpn_ip=client_ip,
            wg_private_key=client_priv,
            wg_public_key=client_pub,
            expires_at=expires_at,
            event_name="admin_manual_add",
        )

        log.info(
            "[DB] Inserted manual subscription for tg_id=%s vpn_ip=%s expires_at=%s",
            target_id,
            client_ip,
            expires_at,
        )
    except Exception as e:
        try:
            db.release_ip_in_pool(client_ip)
        except Exception:
            pass
        log.error(
            "[DB] Failed to insert manual subscription for tg_id=%s: %s",
            target_id,
            repr(e),
        )
        await callback.answer("Ошибка при записи подписки в базу. Проверь логи.", show_alert=True)
        await state.clear()
        return

    # Генерим конфиг и отправляем пользователю
    config_text = wg.build_client_config(
        client_private_key=client_priv,
        client_ip=client_ip,
    )

    sent_ok = True
    try:
        await send_vpn_config_to_user(
            telegram_user_id=target_id,
            config_text=config_text,
            caption=(
                "Администратор выдал тебе доступ к MaxNet VPN.\n\n"
                "Файл vpn.conf — в этом сообщении. QR-код — в следующем."
            ),
        )
        log.info("[Telegram] Manual config sent to %s", target_id)
    except Exception as e:
        sent_ok = False
        log.error(
            "[Telegram] Failed to send manual config to %s: %s",
            target_id,
            repr(e),
        )


    # Сообщаем админу
    if target_username:
        user_line = (
            f"Пользователь TG: <code>{target_id}</code> "
            f"(@{target_username})\n"
        )
    else:
        user_line = f"Пользователь TG: <code>{target_id}</code>\n"

    warning = ""
    if not sent_ok:
        warning = (
            "⚠️ ВАЖНО: Бот НЕ смог отправить пользователю конфиг.\n"
            "Обычно это значит, что пользователь не нажал /start или заблокировал бота.\n\n"
        )

    text = (
        warning
        + "✅ Ручная подписка создана.\n\n"
        + user_line
        + f"VPN IP: <code>{client_ip}</code>\n"
        + f"Срок: <b>{period_label}</b>\n"
        + f"Действует до: <b>{fmt_date(expires_at)}</b>"
    )


    await callback.message.answer(
        text,
        disable_web_page_preview=True,
    )

    await callback.answer("Подписка выдана.")
    await state.clear()


    
@router.callback_query(F.data.startswith("admcmd:"))
async def admin_cmd_inline(callback: CallbackQuery, state: FSMContext) -> None:
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)

    # логируем, кого считаем админом и кто нажал кнопку
    log.info(
        "[AdminInline admcmd] admin_id=%s callback_from_user_id=%s",
        admin_id,
        callback.from_user.id if callback.from_user else None,
    )

    if callback.from_user is None or callback.from_user.id != admin_id:
        await callback.answer("Эта кнопка только для администратора.", show_alert=True)
        return

    data = callback.data or ""
    parts = data.split(":")
    if len(parts) != 2:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    _, action = parts


    if action == "info":
        await callback.message.answer(
            ADMIN_INFO_TEXT,
            disable_web_page_preview=True,
        )
        await callback.answer()
        return

    if action == "add_sub":
        # Запускаем тот же процесс, что и по /add_sub
        await state.set_state(AdminAddSub.waiting_for_target)
        await callback.message.answer(
            "Перешли сюда <b>любое сообщение</b> от пользователя, которому нужно выдать VPN-доступ.\n\n"
            "Либо отправь его <b>числовой Telegram ID</b> вручную.",
            disable_web_page_preview=True,
        )
        await callback.answer()
        return

    if action == "last":
        await cmd_admin_last(callback.message)
        await callback.answer()
        return

    if action == "list":
        await cmd_admin_list(callback.message)
        await callback.answer()
        return

    if action == "stats":
        await send_admin_stats(callback.message)
        await callback.answer()
        return

    await callback.answer("Неизвестное действие.", show_alert=True)
    
@router.callback_query(F.data.startswith("adm:"))
async def admin_inline_callback(callback: CallbackQuery) -> None:
    # Проверяем админа по пользователю, который НАЖАЛ кнопку
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)
    if callback.from_user is None or callback.from_user.id != admin_id:
        await callback.answer("Эта кнопка только для администратора.", show_alert=True)
        return

    data = callback.data or ""
    parts = data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    _, action, sub_id_str = parts

    try:
        sub_id = int(sub_id_str)
    except ValueError:
        await callback.answer("Некорректный ID.", show_alert=True)
        return

    # ДЕАКТИВАЦИЯ
    if action == "deact":
        sub = db.deactivate_subscription_by_id(
            sub_id=sub_id,
            event_name="admin_deactivate",
        )
        if not sub:
            await callback.answer("Подписка не найдена или уже деактивирована.", show_alert=True)
            return

        pub_key = sub.get("wg_public_key")
        if pub_key:
            try:
                log.info("[TelegramAdmin] Remove peer (inline) pubkey=%s for sub_id=%s", pub_key, sub_id)
                wg.remove_peer(pub_key)
            except Exception as e:
                log.error(
                    "[TelegramAdmin] Failed to remove peer (inline) from WireGuard for sub_id=%s: %s",
                    sub_id,
                    repr(e),
                )

        telegram_user_id = sub.get("telegram_user_id")
        telegram_user_name = sub.get("telegram_user_name")
        vpn_ip = sub.get("vpn_ip", "")

        if telegram_user_name:
            tg_display = f"{telegram_user_id} ({telegram_user_name})"
        else:
            tg_display = str(telegram_user_id)

        text = (
            f"Подписка с ID {sub_id} деактивирована.\n"
            f"Пользователь TG: {tg_display}\n"
            f"VPN IP: {vpn_ip}\n"
            f"Peer в WireGuard удалён (или его не было)."
        )
        await callback.message.answer(text)
        await callback.answer("Подписка деактивирована.")
        return

    # АКТИВАЦИЯ
    if action == "act":
        # Сначала берём подписку, чтобы узнать telegram_user_id
        sub_before = db.get_subscription_by_id(sub_id=sub_id)
        if not sub_before:
            await callback.answer("Подписка не найдена.", show_alert=True)
            return

        telegram_user_id = sub_before.get("telegram_user_id")

        # ⚠️ СНАЧАЛА отключаем старые активные подписки пользователя
        if telegram_user_id:
            deactivate_existing_active_subscriptions(
                telegram_user_id=telegram_user_id,
                reason="auto_replace_inline_activate",
            )

        # Теперь активируем нужную подписку (при реактивации выделяется новый IP)
        try:
            sub = db.activate_subscription_by_id(
                sub_id=sub_id,
                event_name="admin_activate",
            )
        except RuntimeError as e:
            if "No free VPN IPs" in str(e):
                await callback.answer("Нет свободных IP в пуле. Активация невозможна.", show_alert=True)
            else:
                raise
            return

        if not sub:
            await callback.answer("Подписка не найдена или уже активна.", show_alert=True)
            return

        pub_key = sub.get("wg_public_key")
        vpn_ip = sub.get("vpn_ip")
        telegram_user_id = sub.get("telegram_user_id")
        telegram_user_name = sub.get("telegram_user_name")

        if not pub_key or not vpn_ip:
            await callback.answer("Нет wg_public_key или vpn_ip, не могу добавить peer.", show_alert=True)
            return

        if telegram_user_name:
            tg_display = f"{telegram_user_id} ({telegram_user_name})"
        else:
            tg_display = str(telegram_user_id)

        allowed_ip = f"{vpn_ip}/{settings.WG_CLIENT_NETWORK_CIDR}"

        try:
            log.info(
                "[TelegramAdmin] Add peer (inline) pubkey=%s ip=%s for sub_id=%s",
                pub_key,
                allowed_ip,
                sub_id,
            )
            wg.add_peer(
                public_key=pub_key,
                allowed_ip=allowed_ip,
                telegram_user_id=telegram_user_id,
            )
        except Exception as e:
            log.error(
                "[TelegramAdmin] Failed to add peer (inline) to WireGuard for sub_id=%s: %s",
                sub_id,
                repr(e),
            )
            await callback.answer(
                "Подписка активирована в базе, но peer в WireGuard не добавлен — смотри логи.",
                show_alert=True,
            )
            return

        text = (
            f"Подписка с ID {sub_id} активирована.\n"
            f"Пользователь TG: {tg_display}\n"
            f"VPN IP: {vpn_ip}\n"
            f"Peer в WireGuard добавлен.\n"
            f"⚠️ Клиент должен заново скачать конфиг (IP изменился)."
        )
        await callback.message.answer(text)
        await callback.answer("Подписка активирована.")
        return

    # УДАЛЕНИЕ
    if action == "del":
        sub = db.get_subscription_by_id(sub_id=sub_id)
        if not sub:
            await callback.answer("Подписка не найдена.", show_alert=True)
            return

        pub_key = sub.get("wg_public_key")
        vpn_ip = sub.get("vpn_ip")
        telegram_user_id = sub.get("telegram_user_id")
        telegram_user_name = sub.get("telegram_user_name")

        if pub_key:
            try:
                log.info("[TelegramAdmin] Remove peer (inline delete) pubkey=%s for sub_id=%s", pub_key, sub_id)
                wg.remove_peer(pub_key)
            except Exception as e:
                log.error(
                    "[TelegramAdmin] Failed to remove peer (inline delete) from WireGuard for sub_id=%s: %s",
                    sub_id,
                    repr(e),
                )

        deleted = db.delete_subscription_by_id(sub_id=sub_id)
        if not deleted:
            await callback.answer(
                "Не удалось удалить подписку из базы (возможно, её уже удалили).",
                show_alert=True,
            )
            return

        if telegram_user_name:
            tg_display = f"{telegram_user_id} ({telegram_user_name})"
        else:
            tg_display = str(telegram_user_id)

        text = (
            f"Подписка с ID {sub_id} полностью удалена.\n"
            f"Пользователь TG: {tg_display}\n"
            f"VPN IP: {vpn_ip}\n"
            f"Peer в WireGuard удалён (если был)."
        )
        await callback.message.answer(text)
        await callback.answer("Подписка удалена.")
        return

    await callback.answer("Неизвестное действие.", show_alert=True)


async def set_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Начать / подключить VPN"),
        BotCommand(command="help", description="Инструкция по подключению"),
        BotCommand(command="status", description="Статус VPN-подписки"),
        BotCommand(command="points", description="Мой баланс баллов"),
        BotCommand(command="ref", description="Моя реферальная ссылка"),
        BotCommand(command="ref_info", description="Правила реферальной программы"),
        BotCommand(command="subscription", description="Тарифы и стоимость подписки"),
        BotCommand(command="demo", description="Запросить демо-доступ"),
        BotCommand(command="support", description="Связаться с поддержкой"),
        BotCommand(command="privacy", description="Политика конфиденциальности"),
        BotCommand(command="terms", description="Пользовательское соглашение"),
    ]
    await bot.set_my_commands(commands)



async def auto_notify_expiring_subscriptions(bot: Bot) -> None:
    """
    Периодически проверяет подписки, срок которых скоро истекает,
    и отправляет напоминания пользователям (за 3 дня, за 1 день и за 1 час).

    Дополнительно:
    - не шлём уведомления ночью (по UTC: только 09–22);
    - добавляем inline-клавиатуру SUBSCRIPTION_RENEW_KEYBOARD.
    """
    if not db.acquire_job_lock(settings.DB_JOB_LOCK_NOTIFY_EXPIRING):
        log.info("[AutoNotify] Job already running in another instance")
        return

    try:
        while True:
            try:
                now = datetime.now(timezone.utc)
                # Опциональное правило "не слать ночью"
                if not (9 <= now.hour <= 22):
                    log.debug(
                        "[AutoNotify] Skip notifications at this hour (utc_hour=%s)",
                        now.hour,
                    )
                    await asyncio.sleep(600)
                    continue

                batch_count = 0

                # --- Напоминание за 3 дня до окончания ---
                subs_3d = db.get_subscriptions_expiring_in_window(60, 73)
                for sub in subs_3d:
                    sub_id = sub.get("id")
                    telegram_user_id = sub.get("telegram_user_id")
                    expires_at = sub.get("expires_at")

                    if not sub_id or not telegram_user_id:
                        continue

                    if db.has_subscription_notification(
                        sub_id,
                        "expires_3d",
                        telegram_user_id=telegram_user_id,
                        expires_at=expires_at,
                    ):
                        continue

                    ok = await safe_send_message(
                        bot=bot,
                        chat_id=telegram_user_id,
                        text=(
                            "⏳ Срок действия VPN скоро закончится\n\n"
                            "До окончания подписки осталось 3 дня.\n\n"
                            "Ты можешь продлить доступ:\n"
                            "• оплатив картой или криптой;\n"
                            "• используя баллы (если хватает).\n\n"
                            "Нажми «Продлить подписку», чтобы выбрать вариант 👇"
                        ),
                        reply_markup=SUBSCRIPTION_RENEW_KEYBOARD,
                        disable_web_page_preview=True,
                    )
                    db.create_subscription_notification(
                        subscription_id=sub_id,
                        notification_type="expires_3d",
                        telegram_user_id=telegram_user_id,
                        expires_at=expires_at,
                    )
                    if ok:
                        log.info(
                            "[AutoNotify] Sent 3d-before-expire notification sub_id=%s tg_id=%s",
                            sub_id,
                            telegram_user_id,
                        )

                    batch_count += 1
                    if batch_count >= NOTIFY_BATCH_SIZE:
                        await asyncio.sleep(NOTIFY_BATCH_SLEEP)
                        batch_count = 0

                # --- Напоминание за 1 день до окончания ---
                subs_1d = db.get_subscriptions_expiring_in_window(12, 25)
                for sub in subs_1d:
                    sub_id = sub.get("id")
                    telegram_user_id = sub.get("telegram_user_id")
                    expires_at = sub.get("expires_at")

                    if not sub_id or not telegram_user_id:
                        continue

                    if db.has_subscription_notification(
                        sub_id,
                        "expires_1d",
                        telegram_user_id=telegram_user_id,
                        expires_at=expires_at,
                    ):
                        continue

                    ok = await safe_send_message(
                        bot=bot,
                        chat_id=telegram_user_id,
                        text=(
                            "⚠️ VPN доступ скоро закончится\n\n"
                            "Подписка истекает через 24 часа.\n\n"
                            "Чтобы не потерять доступ к интернету:\n"
                            "• продли подписку заранее;\n"
                            "• выбери удобный способ оплаты.\n\n"
                            "Нажми кнопку ниже 👇"
                        ),
                        reply_markup=SUBSCRIPTION_RENEW_KEYBOARD,
                        disable_web_page_preview=True,
                    )
                    db.create_subscription_notification(
                        subscription_id=sub_id,
                        notification_type="expires_1d",
                        telegram_user_id=telegram_user_id,
                        expires_at=expires_at,
                    )
                    if ok:
                        log.info(
                            "[AutoNotify] Sent 1d-before-expire notification sub_id=%s tg_id=%s",
                            sub_id,
                            telegram_user_id,
                        )

                    batch_count += 1
                    if batch_count >= NOTIFY_BATCH_SIZE:
                        await asyncio.sleep(NOTIFY_BATCH_SLEEP)
                        batch_count = 0

                # --- Напоминание за 1 час до окончания ---
                # Окно примерно от 1 до 2 часов до окончания (как и выше — в "часах", а не минутах)
                subs_1h = db.get_subscriptions_expiring_in_window(1, 2)
                for sub in subs_1h:
                    sub_id = sub.get("id")
                    telegram_user_id = sub.get("telegram_user_id")
                    expires_at = sub.get("expires_at")

                    if not sub_id or not telegram_user_id:
                        continue

                    if db.has_subscription_notification(
                        sub_id,
                        "expires_1h",
                        telegram_user_id=telegram_user_id,
                        expires_at=expires_at,
                    ):
                        continue

                    try:
                        # Используем уже готовую функцию уведомления об окончании,
                        # но вызываем её ЗА час до деактивации.
                        await send_subscription_expired_notification(
                            telegram_user_id=telegram_user_id,
                        )

                        db.create_subscription_notification(
                            subscription_id=sub_id,
                            notification_type="expires_1h",
                            telegram_user_id=telegram_user_id,
                            expires_at=expires_at,
                        )

                        log.info(
                            "[AutoNotify] Sent 1h-before-expire notification sub_id=%s tg_id=%s",
                            sub_id,
                            telegram_user_id,
                        )
                    except TelegramRetryAfter as e:
                        log.warning(
                            "[AutoNotify] RetryAfter for tg_id=%s (1h notice): %s",
                            telegram_user_id,
                            e.retry_after,
                        )
                        await asyncio.sleep(e.retry_after)
                    except Exception as e:
                        log.error(
                            "[AutoNotify] Unexpected error for tg_id=%s (1h notice): %r",
                            telegram_user_id,
                            e,
                        )
                        # Записываем, чтобы не повторять попытки (бот заблокирован и т.п.)
                        db.create_subscription_notification(
                            subscription_id=sub_id,
                            notification_type="expires_1h",
                            telegram_user_id=telegram_user_id,
                            expires_at=expires_at,
                        )

                    batch_count += 1
                    if batch_count >= NOTIFY_BATCH_SIZE:
                        await asyncio.sleep(NOTIFY_BATCH_SLEEP)
                        batch_count = 0

            except Exception as e:
                log.error(
                    "[AutoNotify] Unexpected error in auto_notify_expiring_subscriptions: %r",
                    e,
                )

            # Проверяем примерно раз в 10 минут
            await asyncio.sleep(600)
    finally:
        db.release_job_lock(settings.DB_JOB_LOCK_NOTIFY_EXPIRING)


async def auto_deactivate_expired_subscriptions() -> None:
    """
    Периодически ищет в базе все активные подписки с истекшим expires_at,
    деактивирует их и удаляет peer из WireGuard.
    (Уведомление пользователю об окончании теперь отправляется заранее —
    за ~1 час до окончания в auto_notify_expiring_subscriptions.)
    """
    if not db.acquire_job_lock(settings.DB_JOB_LOCK_DEACTIVATE_EXPIRED):
        log.info("[AutoExpire] Job already running in another instance")
        return

    try:
        while True:
            try:
                expired_subs = db.get_expired_active_subscriptions()
                for sub in expired_subs:
                    sub_id = sub.get("id")
                    pub_key = sub.get("wg_public_key")

                    if not sub_id:
                        continue

                    # помечаем неактивной в базе
                    deactivated = db.deactivate_subscription_by_id(
                        sub_id=sub_id,
                        event_name="auto_expire",
                    )

                    if not deactivated:
                        continue

                    if pub_key:
                        try:
                            log.info(
                                "[AutoExpire] Remove peer pubkey=%s for sub_id=%s",
                                pub_key,
                                sub_id,
                            )
                            wg.remove_peer(pub_key)
                        except Exception as e:
                            log.error(
                                "[AutoExpire] Failed to remove peer from WireGuard for sub_id=%s: %s",
                                sub_id,
                                repr(e),
                            )

                    # IP возвращается в пул внутри deactivate_subscription_by_id

            except Exception as e:
                log.error(
                    "[AutoExpire] Unexpected error in auto_deactivate_expired_subscriptions: %s",
                    repr(e),
                )

            # Проверяем раз в 60 секунд (можешь настроить под себя)
            await asyncio.sleep(60)
    finally:
        db.release_job_lock(settings.DB_JOB_LOCK_DEACTIVATE_EXPIRED)


# Отзыв неиспользованных промо-баллов (например never_connected_100) через N дней
REVOKE_UNUSED_PROMO_CAMPAIGN = "never_connected_100"
REVOKE_UNUSED_PROMO_AFTER_DAYS = 30
REVOKE_UNUSED_PROMO_POINTS = 100
REVOKE_REASON = "promo_revoke"
REVOKE_SOURCE = "admin"


async def auto_revoke_unused_promo_points() -> None:
    """
    Раз в сутки ищет пользователей, которым 30+ дней назад начислили промо-баллы
    по кампании never_connected_100 и которые так и не потратили баллы.
    Списывает у них 100 баллов (отзыв неиспользованного бонуса).
    """
    if not db.acquire_job_lock(settings.DB_JOB_LOCK_REVOKE_UNUSED_PROMO):
        log.info("[RevokePromo] Job already running in another instance")
        return

    try:
        while True:
            try:
                users = db.get_users_with_unused_promo_to_revoke(
                    campaign=REVOKE_UNUSED_PROMO_CAMPAIGN,
                    after_days=REVOKE_UNUSED_PROMO_AFTER_DAYS,
                    min_balance_to_revoke=REVOKE_UNUSED_PROMO_POINTS,
                )
                for row in users:
                    uid = row.get("telegram_user_id")
                    if not uid:
                        continue
                    res = db.add_points(
                        uid,
                        -REVOKE_UNUSED_PROMO_POINTS,
                        REVOKE_REASON,
                        REVOKE_SOURCE,
                        meta={
                            "campaign": REVOKE_UNUSED_PROMO_CAMPAIGN,
                            "revoke_after_days": REVOKE_UNUSED_PROMO_AFTER_DAYS,
                        },
                        allow_negative=False,
                    )
                    if res.get("ok"):
                        log.info(
                            "[RevokePromo] Revoked %s points from tg_id=%s (unused after %s days)",
                            REVOKE_UNUSED_PROMO_POINTS,
                            uid,
                            REVOKE_UNUSED_PROMO_AFTER_DAYS,
                        )
                    else:
                        log.warning(
                            "[RevokePromo] Failed to revoke for tg_id=%s: %s",
                            uid,
                            res.get("error"),
                        )
                if users:
                    log.info("[RevokePromo] Processed %s users", len(users))
            except Exception as e:
                log.error(
                    "[RevokePromo] Unexpected error in auto_revoke_unused_promo_points: %s",
                    repr(e),
                )

            # Проверяем раз в 24 часа
            await asyncio.sleep(86400)
    finally:
        db.release_job_lock(settings.DB_JOB_LOCK_REVOKE_UNUSED_PROMO)


NEW_HANDSHAKE_ADMIN_INTERVAL_SEC = 120  # 2 минуты — чтобы уведомления о handshake приходили быстрее

HANDSHAKE_USER_CONNECTED_TEXT = (
    "VPN подключён 👍\n\n"
    "Соединение работает.\n\n"
    "Пробный доступ активен 7 дней.\n\n"
    "Чтобы VPN не отключился после теста,\n"
    "можно закрепить доступ уже сейчас.\n\n"
    "🔥 Самый популярный тариф\n"
    "3 месяца — 270 ₽\n\n"
    "Это на 30% дешевле помесячной оплаты."
)

# CTA-клавиатура под первым handshake-сообщением (upsell)
HANDSHAKE_USER_CONNECTED_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💎 Закрепить доступ — 270 ₽",
                callback_data="pay:open",
            ),
        ],
        [
            InlineKeyboardButton(text="📅 Все тарифы", callback_data="pay:open"),
        ],
    ]
)

HANDSHAKE_FOLLOWUP_10M_TEXT = (
    "VPN подключён 👍\n\n"
    "Всё открывается нормально?\n\n"
    "Если что-то не работает или нужна помощь с настройкой — "
    "просто напишите сюда, поможем."
)

HANDSHAKE_FOLLOWUP_2H_TEXT = (
    "Если VPN работает стабильно 👍\n\n"
    "Можно закрепить доступ заранее, чтобы он не отключился\n"
    "после тестового периода.\n\n"
    "Самый популярный тариф:\n\n"
    "• 3 месяца — 270 ₽\n\n"
    "Это дешевле, чем оплачивать помесячно.\n\n"
    "Оформить можно здесь:\n/buy"
)

HANDSHAKE_FOLLOWUP_24H_TEXT = (
    "Если VPN оказался полезным, можно закрепить доступ, "
    "чтобы он не отключился после тестового периода.\n\n"
    "Самый популярный тариф:\n"
    "• 3 месяца — 270 ₽\n\n"
    "Это дешевле помесячной оплаты.\n\n"
    "Оформить можно здесь:\n/buy"
)

VPN_OK_ANSWER_TEXT = (
    "Отлично 👍\n\n"
    "Рады, что всё работает.\n\n"
    "Если VPN будет нужен на постоянной основе, можно закрепить доступ заранее, "
    "чтобы он не отключился после теста.\n\n"
    "Самый популярный тариф:\n"
    "• 3 месяца — 270 ₽\n\n"
    "Оформить можно здесь:\n/buy\n\n"
    "Кстати, вы можете приглашать друзей и получать баллы. "
    "Баллы можно тратить на оплату VPN."
)

SUPPORT_URL = "https://t.me/maxnet_vpn_support"

HANDSHAKE_FOLLOWUP_INTERVAL_SEC = 120

WELCOME_AFTER_FIRST_PAYMENT_TEXT = (
    "Спасибо за подключение к MaxNet VPN 🙌\n\n"
    "Если будут вопросы или что-то перестанет открываться — "
    "пишите в поддержку:\n"
    "@maxnet_vpn_support\n\n"
    "VPN можно использовать и на других устройствах — "
    "телефоне, ноутбуке и т.д.\n\n"
    "Кстати, у MaxNet есть реферальная программа. "
    "Можно приглашать друзей и получать бонусы для продления VPN.\n\n"
    "Получить свою ссылку:\n/ref"
)

HANDSHAKE_REFERRAL_NUDGE_3D_TEXT = (
    "Если VPN вам понравился 👍\n\n"
    "Можно пригласить друзей и получать бонусы\n"
    "за каждого пользователя.\n\n"
    "Бонусами можно продлевать VPN бесплатно.\n\n"
    "Получить свою ссылку:\n/ref"
)

HANDSHAKE_FOLLOWUP_2H_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Закрепить доступ", callback_data="pay:open")],
        [InlineKeyboardButton(text="🤝 Пригласить друга", callback_data="ref:open_from_notify")],
        [InlineKeyboardButton(text=SUPPORT_BUTTON_TEXT, url=SUPPORT_URL)],
    ]
)

NO_HANDSHAKE_SURVEY_TEXT = (
    "Подскажите, пожалуйста, почему не стали пользоваться VPN?\n\n"
    "1️⃣ Не разобрался с настройкой\n"
    "2️⃣ Пока не нужен\n"
    "3️⃣ Пользуюсь другим VPN\n"
    "4️⃣ Дорого\n\n"
    "Если ответите цифрой, это поможет нам улучшить сервис.\n\n"
    "Напишите в чат цифру 1–4."
)

WELCOME_AFTER_FIRST_PAYMENT_INTERVAL_SEC = 600


async def auto_new_handshake_admin_notification(bot: Bot) -> None:
    """
    Раз в 10 минут проверяет подписки (триал/промо), у которых появился handshake,
    и отправляет админу одно сводное уведомление.
    """
    if not db.acquire_job_lock(settings.DB_JOB_LOCK_NEW_HANDSHAKE_ADMIN):
        log.info("[NewHandshakeAdmin] Job already running in another instance")
        return

    try:
        while True:
            try:
                admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)
                if not admin_id:
                    await asyncio.sleep(NEW_HANDSHAKE_ADMIN_INTERVAL_SEC)
                    continue

                handshakes = {}
                try:
                    if hasattr(asyncio, "to_thread"):
                        handshakes = await asyncio.to_thread(wg.get_handshake_timestamps)
                    else:
                        loop = asyncio.get_running_loop()
                        handshakes = await loop.run_in_executor(
                            None, wg.get_handshake_timestamps
                        )
                except Exception as e:
                    log.warning("[NewHandshakeAdmin] Failed to get handshakes: %r", e)
                    await asyncio.sleep(NEW_HANDSHAKE_ADMIN_INTERVAL_SEC)
                    continue

                subs = db.get_subscriptions_for_new_handshake_admin()
                with_handshake = []
                for sub in subs:
                    pk = (sub.get("wg_public_key") or "").strip()
                    if pk and handshakes.get(pk, 0) > 0:
                        with_handshake.append(sub)

                if not with_handshake:
                    await asyncio.sleep(NEW_HANDSHAKE_ADMIN_INTERVAL_SEC)
                    continue

                def _fmt_exp(dt):
                    return fmt_date(dt, with_time=False) if dt else "?"

                trial_lines = []
                promo_lines = []
                paid_lines = []
                to_notify = []

                for sub in with_handshake:
                    sub_id = sub.get("id")
                    tg_id = sub.get("telegram_user_id")
                    username = sub.get("telegram_user_name")
                    user_line = fmt_user_line(username, tg_id)
                    expires_str = _fmt_exp(sub.get("expires_at"))
                    event = sub.get("last_event_name") or ""

                    # Уведомление пользователю при первом handshake (CTA-кнопки для upsell)
                    if tg_id and not db.has_subscription_notification(sub_id, "handshake_user_connected"):
                        ok = await safe_send_message(
                            bot=bot,
                            chat_id=tg_id,
                            text=HANDSHAKE_USER_CONNECTED_TEXT,
                            disable_web_page_preview=True,
                            reply_markup=HANDSHAKE_USER_CONNECTED_KEYBOARD,
                        )
                        if ok:
                            try:
                                db.create_subscription_notification(
                                    subscription_id=sub_id,
                                    notification_type="handshake_user_connected",
                                    telegram_user_id=tg_id,
                                    expires_at=sub.get("expires_at"),
                                )
                            except Exception as e:
                                log.warning(
                                    "[HandshakeUser] Failed to record notification sub_id=%s: %r",
                                    sub_id,
                                    e,
                                )
                        await asyncio.sleep(1)

                    if event == "referral_free_trial_7d":
                        ref_info = db.get_referrer_with_count(tg_id)
                        if ref_info:
                            ref_tg = ref_info.get("referrer_telegram_user_id")
                            ref_name = ref_info.get("referrer_username")
                            ref_display = fmt_ref_display(ref_name, ref_tg)
                            referred_count = int(ref_info.get("referred_count") or 0)
                            paid_count = db.count_referrer_paid_referrals(ref_info["referrer_telegram_user_id"])
                            trial_lines.append(
                                f"• {user_line} | Реферер {ref_display} ({referred_count}/{paid_count}) | До: {expires_str}"
                            )
                        else:
                            trial_lines.append(f"• {user_line} | До: {expires_str}")
                    elif event.startswith("promo"):
                        promo_info = db.get_promo_info_for_subscription(sub_id)
                        code = promo_info.get("code", "?") if promo_info else "?"
                        promo_lines.append(f"• {user_line} | {code} | До: {expires_str}")
                    else:
                        if event.startswith("yookassa"):
                            source = "ЮKassa"
                        elif event.startswith("heleket"):
                            source = "Heleket"
                        elif "points" in event:
                            source = "баллы"
                        else:
                            source = "оплата"
                        paid_lines.append(f"• {user_line} | {source} | До: {expires_str}")

                    to_notify.append((sub_id, tg_id, sub.get("expires_at")))

                parts = [
                    f"🟢 Новых подписчиков с handshake: <b>{len(with_handshake)}</b>",
                    "",
                ]
                if trial_lines:
                    parts.append("Триал:")
                    parts.extend(trial_lines)
                    parts.append("")
                if promo_lines:
                    parts.append("Промо:")
                    parts.extend(promo_lines)
                    parts.append("")
                if paid_lines:
                    parts.append("Оплата:")
                    parts.extend(paid_lines)

                text = "\n".join(parts)
                text_len = len(text)
                TELEGRAM_LIMIT = 4096
                if text_len > TELEGRAM_LIMIT:
                    log.warning(
                        "[NewHandshakeAdmin] Message too long (%s > %s), splitting",
                        text_len,
                        TELEGRAM_LIMIT,
                    )
                    # Разбиваем по строкам, чтобы каждый chunk < 4096
                    lines = text.split("\n")
                    chunks = []
                    buf = []
                    buflen = 0
                    for ln in lines:
                        add = len(ln) + 1
                        if buf and buflen + add > TELEGRAM_LIMIT:
                            chunks.append("\n".join(buf))
                            buf = []
                            buflen = 0
                        buf.append(ln)
                        buflen += add
                    if buf:
                        chunks.append("\n".join(buf))
                    texts = chunks
                else:
                    texts = [text]

                all_ok = True
                for part in texts:
                    ok = await safe_send_message(
                        bot=bot,
                        chat_id=admin_id,
                        text=part,
                        disable_web_page_preview=True,
                    )
                    if not ok:
                        all_ok = False

                if all_ok:
                    for sub_id, tg_id, exp in to_notify:
                        try:
                            db.create_subscription_notification(
                                subscription_id=sub_id,
                                notification_type="new_handshake_admin",
                                telegram_user_id=tg_id,
                                expires_at=exp,
                            )
                        except Exception as e:
                            log.warning(
                                "[NewHandshakeAdmin] Failed to record notification sub_id=%s: %r",
                                sub_id,
                                e,
                            )
                    log.info(
                        "[NewHandshakeAdmin] Sent notification for %s subs",
                        len(to_notify),
                    )
                else:
                    log.warning(
                        "[NewHandshakeAdmin] send_message FAILED (batch=%s subs, len=%s). "
                        "Check [SafeSend] logs for Telegram error.",
                        len(to_notify),
                        text_len,
                    )

            except Exception as e:
                log.error("[NewHandshakeAdmin] Unexpected error: %r", e)

            await asyncio.sleep(NEW_HANDSHAKE_ADMIN_INTERVAL_SEC)

    finally:
        db.release_job_lock(settings.DB_JOB_LOCK_NEW_HANDSHAKE_ADMIN)


async def auto_welcome_after_first_payment(bot: Bot) -> None:
    """
    Отправляет welcome-сообщение после первой оплаты (ЮKassa/Heleket).
    Без баллов. Запись notification только после успешной отправки.
    """
    if not db.acquire_job_lock(settings.DB_JOB_LOCK_WELCOME_AFTER_FIRST_PAYMENT):
        log.info("[WelcomeFirstPayment] Job already running in another instance")
        return

    try:
        while True:
            try:
                candidates = db.get_subscriptions_for_welcome_after_first_payment()
                for row in candidates:
                    sub_id = row.get("subscription_id")
                    tg_id = row.get("telegram_user_id")
                    if not tg_id:
                        continue
                    ok = await safe_send_message(
                        bot=bot,
                        chat_id=tg_id,
                        text=WELCOME_AFTER_FIRST_PAYMENT_TEXT,
                        disable_web_page_preview=True,
                    )
                    if ok:
                        try:
                            db.create_subscription_notification(
                                subscription_id=sub_id,
                                notification_type="welcome_after_first_payment",
                                telegram_user_id=tg_id,
                                expires_at=row.get("expires_at"),
                            )
                            log.info(
                                "[WelcomeFirstPayment] Sent to tg_id=%s sub_id=%s",
                                tg_id,
                                sub_id,
                            )
                        except Exception as e:
                            log.warning(
                                "[WelcomeFirstPayment] Failed to record sub_id=%s: %r",
                                sub_id,
                                e,
                            )
                    await asyncio.sleep(1)

            except Exception as e:
                log.error("[WelcomeFirstPayment] Unexpected error: %r", e)

            await asyncio.sleep(WELCOME_AFTER_FIRST_PAYMENT_INTERVAL_SEC)

    finally:
        db.release_job_lock(settings.DB_JOB_LOCK_WELCOME_AFTER_FIRST_PAYMENT)


async def auto_handshake_followup_notifications(bot: Bot) -> None:
    """
    Follow-up уведомления пользователю после первого handshake:
    - через 10 мин: handshake_followup_10m
    - через 2 часа: handshake_followup_2h (только триал/промо)
    """
    if not db.acquire_job_lock(settings.DB_JOB_LOCK_HANDSHAKE_FOLLOWUP):
        log.info("[HandshakeFollowup] Job already running in another instance")
        return

    def _make_10m_keyboard(sub_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Всё работает", callback_data=f"vpn_ok:{sub_id}"),
                ],
                [
                    InlineKeyboardButton(text=SUPPORT_BUTTON_TEXT, url=SUPPORT_URL),
                ],
            ]
        )

    def _make_ref_nudge_keyboard(sub_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🤝 Пригласить друга",
                        callback_data=f"ref:open_from_notify:{sub_id}",
                    ),
                ],
            ]
        )

    try:
        FOLLOWUPS = [
            ("handshake_followup_10m", HANDSHAKE_FOLLOWUP_10M_TEXT, True),
            ("handshake_followup_2h", HANDSHAKE_FOLLOWUP_2H_TEXT, False),
            ("handshake_followup_24h", HANDSHAKE_FOLLOWUP_24H_TEXT, False),
            ("handshake_referral_nudge_3d", HANDSHAKE_REFERRAL_NUDGE_3D_TEXT, False),
        ]
        while True:
            try:
                for followup_type, text, has_buttons in FOLLOWUPS:
                    candidates = db.get_handshake_followup_candidates(followup_type)
                    for row in candidates:
                        sub_id = row.get("subscription_id")
                        tg_id = row.get("telegram_user_id")
                        if not tg_id:
                            continue
                        kwargs = {"disable_web_page_preview": True}
                        if has_buttons:
                            kwargs["reply_markup"] = _make_10m_keyboard(sub_id)
                        elif followup_type == "handshake_followup_2h":
                            kwargs["reply_markup"] = HANDSHAKE_FOLLOWUP_2H_KEYBOARD
                        elif followup_type == "handshake_referral_nudge_3d":
                            kwargs["reply_markup"] = _make_ref_nudge_keyboard(sub_id)
                        ok = await safe_send_message(
                            bot=bot,
                            chat_id=tg_id,
                            text=text,
                            **kwargs,
                        )
                        if ok:
                            try:
                                db.create_subscription_notification(
                                    subscription_id=sub_id,
                                    notification_type=followup_type,
                                    telegram_user_id=tg_id,
                                    expires_at=row.get("expires_at"),
                                )
                            except Exception as e:
                                log.warning(
                                    "[HandshakeFollowup] Failed to record %s sub_id=%s: %r",
                                    followup_type,
                                    sub_id,
                                    e,
                                )
                        await asyncio.sleep(1)

            except Exception as e:
                log.error("[HandshakeFollowup] Unexpected error: %r", e)

            await asyncio.sleep(HANDSHAKE_FOLLOWUP_INTERVAL_SEC)

    finally:
        db.release_job_lock(settings.DB_JOB_LOCK_HANDSHAKE_FOLLOWUP)


HANDSHAKE_SHORT_CONFIRMATION_INTERVAL_SEC = 60
HANDSHAKE_SHORT_CONFIRMATION_DELAY_SEC = 60
# Не слать short confirmation, если первое handshake-сообщение было давно (избегаем рассылки при /start старым пользователям)
HANDSHAKE_SHORT_CONFIRMATION_MAX_AGE_SEC = 900  # 15 минут
# Максимум отправок за один прогон job (снижает нагрузку на connection pool)
HANDSHAKE_SHORT_CONFIRMATION_BATCH_SIZE = 10


async def auto_handshake_short_confirmation(bot: Bot) -> None:
    """
    Short confirmation follow-up ~60 сек после первого handshake-сообщения.
    Только подтверждение «всё ок» + кнопка поддержки, без продажи.
    """
    if not db.acquire_job_lock(settings.DB_JOB_LOCK_HANDSHAKE_SHORT_CONFIRMATION):
        log.info("[HandshakeShortConfirm] Job already running in another instance")
        return

    try:
        while True:
            try:
                candidates = db.get_handshake_short_confirmation_candidates(
                    interval_seconds=HANDSHAKE_SHORT_CONFIRMATION_DELAY_SEC,
                    max_age_seconds=HANDSHAKE_SHORT_CONFIRMATION_MAX_AGE_SEC,
                )
                for row in candidates[:HANDSHAKE_SHORT_CONFIRMATION_BATCH_SIZE]:
                    tg_id = row.get("telegram_user_id")
                    sub_id = row.get("subscription_id")
                    if not tg_id:
                        continue
                    support_kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text=SUPPORT_BUTTON_TEXT, url=SUPPORT_URL)],
                        ]
                    )
                    ok = await safe_send_message(
                        bot=bot,
                        chat_id=tg_id,
                        text=HANDSHAKE_SHORT_CONFIRMATION_TEXT,
                        reply_markup=support_kb,
                    )
                    if ok:
                        try:
                            db.create_subscription_notification(
                                subscription_id=sub_id,
                                notification_type="handshake_short_confirmation",
                                telegram_user_id=tg_id,
                                expires_at=row.get("expires_at"),
                            )
                        except Exception as e:
                            log.warning(
                                "[HandshakeShortConfirm] Failed to record sub_id=%s: %r",
                                sub_id,
                                e,
                            )
                    await asyncio.sleep(1)
            except Exception as e:
                log.error("[HandshakeShortConfirm] Unexpected error: %r", e)

            await asyncio.sleep(HANDSHAKE_SHORT_CONFIRMATION_INTERVAL_SEC)

    finally:
        db.release_job_lock(settings.DB_JOB_LOCK_HANDSHAKE_SHORT_CONFIRMATION)


async def auto_no_handshake_reminder(bot: Bot) -> None:
    """
    Раз в час проверяет подписки, по которым пользователь ещё не подключался (нет handshake),
    и отправляет напоминание: через 2h, через 24h и через 5 дней.
    Пауза 5 сек между отправками, обработка блокировки/удаления бота.
    """
    if not db.acquire_job_lock(settings.DB_JOB_LOCK_NO_HANDSHAKE_REMINDER):
        log.info("[NoHandshakeRemind] Job already running in another instance")
        return

    try:
        while True:
            try:
                handshakes = {}
                try:
                    if hasattr(asyncio, "to_thread"):
                        handshakes = await asyncio.to_thread(wg.get_handshake_timestamps)
                    else:
                        loop = asyncio.get_running_loop()
                        handshakes = await loop.run_in_executor(
                            None, wg.get_handshake_timestamps
                        )
                except Exception as e:
                    log.error(
                        "[NoHandshakeRemind] Failed to get wg handshakes: %r, skip run",
                        e,
                    )
                    await asyncio.sleep(3600)
                    continue

                def _format_expires(exp) -> str:
                    return fmt_date(exp, with_time=False) if exp else "?"

                def _days_until_expiry(exp) -> int:
                    """Оставшиеся полные дни до expires_at. 0 если уже истекло."""
                    if exp is None:
                        return 0
                    try:
                        from datetime import date
                        now_d = datetime.now(timezone.utc).date()
                        exp_d = exp.date() if hasattr(exp, "date") else date.fromisoformat(str(exp)[:10])
                        return max(0, (exp_d - now_d).days)
                    except (AttributeError, TypeError, ValueError):
                        return 0

                def _days_text(days: int) -> str:
                    if days <= 0:
                        return "скоро"
                    if days == 1:
                        return "1 день"
                    if 2 <= days <= 4:
                        return f"{days} дня"
                    return f"{days} дней"

                def _not_connected(pubkey: str) -> bool:
                    return handshakes.get(pubkey, 0) == 0

                def _make_2h_text(sub: dict) -> str:
                    return (
                        "Ты получил пробный доступ к MaxNet VPN.\n\n"
                        "Не подключался ещё? Нажми «📱 Получить настройки» — пришлю конфиг заново.\n\n"
                        "Не получается подключиться? Нажми «🧑‍💻 Нужна помощь» — поможем."
                    )

                def _make_24h_text(sub: dict) -> str:
                    exp = sub.get("expires_at")
                    return (
                        f"Ты получил доступ к MaxNet VPN, но пока не подключался.\n\n"
                        f"Подписка действует до {_format_expires(exp)}.\n\n"
                        f"Нажми «📱 Получить настройки» — пришлю конфиг заново.\n\n"
                        f"Не получается? Нажми «🧑‍💻 Нужна помощь»."
                    )

                def _make_5d_text(sub: dict) -> str:
                    days = _days_until_expiry(sub.get("expires_at"))
                    return (
                        f"Подписка MaxNet VPN истекает через {_days_text(days)}.\n\n"
                        f"Ты ещё не подключался. Нажми «📱 Получить настройки» — пришлю конфиг.\n\n"
                        f"Не получается? Нажми «🧑‍💻 Нужна помощь»."
                    )

                def _make_survey_text(sub: dict) -> str:
                    return NO_HANDSHAKE_SURVEY_TEXT

                async def _fetch_handshakes():
                    if hasattr(asyncio, "to_thread"):
                        return await asyncio.to_thread(wg.get_handshake_timestamps)
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(None, wg.get_handshake_timestamps)

                BATCHES = [
                    ("no_handshake_2h", _make_2h_text, True),
                    ("no_handshake_24h", _make_24h_text, True),
                    ("no_handshake_5d", _make_5d_text, True),
                    ("no_handshake_survey", _make_survey_text, False),
                ]

                for batch_idx, (reminder_type, make_text, use_keyboard) in enumerate(BATCHES):
                    if batch_idx > 0:
                        await asyncio.sleep(NO_HANDSHAKE_PAUSE_BETWEEN_TYPES)
                    handshakes = await _fetch_handshakes()
                    subs = db.get_subscriptions_for_no_handshake_reminder(reminder_type)
                    stats_sent = 0
                    stats_send_failed = 0
                    stats_db_error = 0
                    stats_skipped_handshake = 0
                    for idx, sub in enumerate(subs):
                        if idx > 0 and idx % NO_HANDSHAKE_REFRESH_EVERY_N == 0:
                            try:
                                handshakes = await _fetch_handshakes()
                            except Exception as e:
                                log.warning(
                                    "[NoHandshakeRemind] Mid-batch handshake refresh failed: %r",
                                    e,
                                )
                        pubkey = (sub.get("wg_public_key") or "").strip()
                        if not pubkey or not _not_connected(pubkey):
                            stats_skipped_handshake += 1
                            continue

                        sub_id = sub.get("id")
                        telegram_user_id = sub.get("telegram_user_id")
                        if not sub_id or not telegram_user_id:
                            continue

                        # Для survey — запись только после успешной отправки; для остальных — до (идемпотентность)
                        if reminder_type != "no_handshake_survey":
                            try:
                                db.create_subscription_notification(
                                    subscription_id=sub_id,
                                    notification_type=reminder_type,
                                    telegram_user_id=telegram_user_id,
                                    expires_at=sub.get("expires_at"),
                                )
                            except Exception as db_err:
                                log.error(
                                    "[NoHandshakeRemind] Failed to save notification sub_id=%s: %r",
                                    sub_id,
                                    db_err,
                                )
                                stats_db_error += 1
                                continue

                        text = make_text(sub)
                        if use_keyboard:
                            keyboard = InlineKeyboardMarkup(
                                inline_keyboard=[
                                    [
                                        InlineKeyboardButton(
                                            text="📱 Получить настройки",
                                            callback_data=f"config:resend:{sub_id}",
                                        ),
                                        InlineKeyboardButton(
                                            text=SUPPORT_BUTTON_TEXT,
                                            url=SUPPORT_URL,
                                        ),
                                    ],
                                ]
                            )
                            reply_markup = keyboard
                        else:
                            reply_markup = None
                        ok = await safe_send_message(
                            bot=bot,
                            chat_id=telegram_user_id,
                            text=text,
                            disable_web_page_preview=True,
                            reply_markup=reply_markup,
                        )
                        if ok:
                            stats_sent += 1
                            if reminder_type == "no_handshake_survey":
                                try:
                                    db.create_subscription_notification(
                                        subscription_id=sub_id,
                                        notification_type=reminder_type,
                                        telegram_user_id=telegram_user_id,
                                        expires_at=sub.get("expires_at"),
                                    )
                                except Exception as db_err:
                                    log.warning(
                                        "[NoHandshakeRemind] Failed to record survey sub_id=%s: %r",
                                        sub_id,
                                        db_err,
                                    )
                            log.info(
                                "[NoHandshakeRemind] Sent %s sub_id=%s tg_id=%s",
                                reminder_type,
                                sub_id,
                                telegram_user_id,
                            )
                        else:
                            stats_send_failed += 1

                        await asyncio.sleep(NO_HANDSHAKE_REMINDER_SLEEP)

                    log.info(
                        "[NoHandshakeRemind] batch=%s sent=%s send_failed=%s db_error=%s skipped_handshake=%s",
                        reminder_type,
                        stats_sent,
                        stats_send_failed,
                        stats_db_error,
                        stats_skipped_handshake,
                    )

            except Exception as e:
                log.error(
                    "[NoHandshakeRemind] Unexpected error: %r",
                    e,
                )

            await asyncio.sleep(3600)

    finally:
        db.release_job_lock(settings.DB_JOB_LOCK_NO_HANDSHAKE_REMINDER)


CONFIG_CHECKPOINT_DELAY_SEC = 180
CONFIG_CHECKPOINT_JOB_INTERVAL_SEC = 60


async def auto_config_checkpoint(bot: Bot) -> None:
    """
    Background job: периодически проверяет подписки с config_checkpoint_pending,
    у которых прошло >= CONFIG_CHECKPOINT_DELAY_SEC с момента выдачи конфига.
    Если handshake ещё нет — отправляет сообщение «Удалось подключиться к VPN?».
    Устойчив к рестарту процесса: состояние хранится в subscription_notifications.
    """
    if not db.acquire_job_lock(settings.DB_JOB_LOCK_CONFIG_CHECKPOINT):
        log.info("[ConfigCheckpoint] Job already running in another instance")
        return

    try:
        while True:
            try:
                candidates = db.get_pending_config_checkpoints(
                    interval_seconds=CONFIG_CHECKPOINT_DELAY_SEC,
                )
                if not candidates:
                    await asyncio.sleep(CONFIG_CHECKPOINT_JOB_INTERVAL_SEC)
                    continue

                if hasattr(asyncio, "to_thread"):
                    handshakes = await asyncio.to_thread(wg.get_handshake_timestamps)
                else:
                    loop = asyncio.get_running_loop()
                    handshakes = await loop.run_in_executor(
                        None, wg.get_handshake_timestamps
                    )

                for row in candidates:
                    sub_id = row.get("subscription_id")
                    tg_id = row.get("telegram_user_id")
                    if not sub_id or not tg_id:
                        continue
                    try:
                        sub = db.get_subscription_by_id(sub_id)
                        if not sub or not sub.get("active"):
                            continue
                        pub_key = (sub.get("wg_public_key") or "").strip()
                        if not pub_key:
                            continue
                        if handshakes.get(pub_key, 0) > 0:
                            continue
                        if hasattr(asyncio, "to_thread"):
                            handshakes_refresh = await asyncio.to_thread(
                                wg.get_handshake_timestamps
                            )
                        else:
                            handshakes_refresh = await asyncio.get_running_loop().run_in_executor(
                                None, wg.get_handshake_timestamps
                            )
                        if handshakes_refresh.get(pub_key, 0) > 0:
                            continue
                        await send_config_checkpoint_message(
                            telegram_user_id=tg_id,
                            subscription_id=sub_id,
                        )
                        db.create_subscription_notification(
                            subscription_id=sub_id,
                            notification_type="config_checkpoint_sent",
                            telegram_user_id=tg_id,
                            expires_at=sub.get("expires_at"),
                        )
                    except Exception as e:
                        log.warning(
                            "[ConfigCheckpoint] Failed for sub_id=%s tg_id=%s: %r",
                            sub_id,
                            tg_id,
                            e,
                        )
                    await asyncio.sleep(1)

                await asyncio.sleep(CONFIG_CHECKPOINT_JOB_INTERVAL_SEC)
            except Exception as e:
                log.error("[ConfigCheckpoint] Unexpected error in loop: %r", e)
                await asyncio.sleep(CONFIG_CHECKPOINT_JOB_INTERVAL_SEC)
    finally:
        db.release_job_lock(settings.DB_JOB_LOCK_CONFIG_CHECKPOINT)


RECENTLY_EXPIRED_TRIAL_FOLLOWUP_DELAY_SEC = 180
RECENTLY_EXPIRED_TRIAL_FOLLOWUP_JOB_INTERVAL_SEC = 60


async def auto_recently_expired_trial_followup(bot: Bot) -> None:
    """
    Follow-up для сценария trial expired → paid: через ~3 мин после отправки конфига
    проверяем handshake; если нет — напоминаем использовать новый конфиг и даём resend.
    """
    if not db.acquire_job_lock(settings.DB_JOB_LOCK_RECENTLY_EXPIRED_TRIAL_FOLLOWUP):
        log.info("[RecentExpiredTrialFollowup] Job already running in another instance")
        return

    try:
        while True:
            try:
                candidates = db.get_pending_recently_expired_trial_followups(
                    interval_seconds=RECENTLY_EXPIRED_TRIAL_FOLLOWUP_DELAY_SEC,
                )
                if not candidates:
                    await asyncio.sleep(RECENTLY_EXPIRED_TRIAL_FOLLOWUP_JOB_INTERVAL_SEC)
                    continue

                if hasattr(asyncio, "to_thread"):
                    handshakes = await asyncio.to_thread(wg.get_handshake_timestamps)
                else:
                    loop = asyncio.get_running_loop()
                    handshakes = await loop.run_in_executor(
                        None, wg.get_handshake_timestamps
                    )

                for row in candidates:
                    sub_id = row.get("subscription_id")
                    tg_id = row.get("telegram_user_id")
                    if not sub_id or not tg_id:
                        continue
                    try:
                        sub = db.get_subscription_by_id(sub_id)
                        if not sub or not sub.get("active"):
                            continue
                        pub_key = (sub.get("wg_public_key") or "").strip()
                        if not pub_key:
                            continue
                        if handshakes.get(pub_key, 0) > 0:
                            continue
                        keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="📱 Отправить настройки ещё раз",
                                        callback_data=f"config_check_resend:{sub_id}",
                                    ),
                                ],
                                [
                                    InlineKeyboardButton(
                                        text=SUPPORT_BUTTON_TEXT,
                                        url=SUPPORT_URL,
                                    ),
                                ],
                            ]
                        )
                        await bot.send_message(
                            chat_id=tg_id,
                            text=TRIAL_EXPIRED_PAID_FOLLOWUP_NO_HANDSHAKE_TEXT,
                            reply_markup=keyboard,
                        )
                        db.create_subscription_notification(
                            subscription_id=sub_id,
                            notification_type="recently_expired_trial_followup_sent",
                            telegram_user_id=tg_id,
                            expires_at=sub.get("expires_at"),
                        )
                        log.info(
                            "[RecentExpiredTrialFollowup] Sent followup tg_id=%s sub_id=%s",
                            tg_id,
                            sub_id,
                        )
                    except Exception as e:
                        log.warning(
                            "[RecentExpiredTrialFollowup] Failed sub_id=%s tg_id=%s: %r",
                            sub_id,
                            tg_id,
                            e,
                        )
                    await asyncio.sleep(1)

                await asyncio.sleep(RECENTLY_EXPIRED_TRIAL_FOLLOWUP_JOB_INTERVAL_SEC)
            except Exception as e:
                log.error(
                    "[RecentExpiredTrialFollowup] Unexpected error: %r",
                    e,
                )
                await asyncio.sleep(RECENTLY_EXPIRED_TRIAL_FOLLOWUP_JOB_INTERVAL_SEC)
    finally:
        db.release_job_lock(settings.DB_JOB_LOCK_RECENTLY_EXPIRED_TRIAL_FOLLOWUP)


async def main() -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")
    
    # Инициализируем БД (создаём таблицы, если их ещё нет)
    db.init_db()
    
    from aiohttp import web
    from .yookassa_webhook_runner import create_app
    from aiogram.client.default import DefaultBotProperties

    bot = Bot(
        token=settings.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(support_router)  # AI Support — fallback для свободного текста

    # Снимаем webhook, чтобы polling получал апдейты (webhook и polling взаимоисключают друг друга)
    try:
        wh_info = await bot.get_webhook_info()
        if wh_info.url:
            log.warning("[Startup] Webhook was set (url=%s), deleting to use polling", wh_info.url)
            await bot.delete_webhook()
        else:
            log.info("[Startup] No webhook set, polling will receive updates")
    except Exception as e:
        log.error("[Startup] Failed to check/delete webhook: %r", e)

    await set_bot_commands(bot)

    # запускаем фоновые воркеры
    asyncio.create_task(auto_deactivate_expired_subscriptions())
    asyncio.create_task(auto_notify_expiring_subscriptions(bot))
    asyncio.create_task(auto_revoke_unused_promo_points())
    asyncio.create_task(auto_new_handshake_admin_notification(bot))
    asyncio.create_task(auto_handshake_followup_notifications(bot))
    asyncio.create_task(auto_handshake_short_confirmation(bot))
    asyncio.create_task(auto_welcome_after_first_payment(bot))
    asyncio.create_task(auto_no_handshake_reminder(bot))
    asyncio.create_task(auto_config_checkpoint(bot))
    asyncio.create_task(auto_recently_expired_trial_followup(bot))

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
