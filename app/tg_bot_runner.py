import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
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
    INSTRUCTION_TEXT,
    send_vpn_config_to_user,
    send_subscription_expired_notification,
)
from . import wg
from .logger import get_logger, get_promo_logger
from .yookassa_client import create_yookassa_payment
from .heleket_client import create_heleket_payment
from .promo_codes import (
    PromoGenerationParams,
    generate_promo_codes,
    build_insert_sql_for_postgres,
)
log = get_logger()
promo_log = get_promo_logger()


def deactivate_existing_active_subscriptions(telegram_user_id: int, reason: str) -> None:
    """
    Деактивирует ВСЕ активные подписки пользователя и удаляет их peer'ы из WireGuard.
    Используется перед выдачей нового доступа.
    """
    active_subs = db.get_active_subscriptions_for_telegram(telegram_user_id=telegram_user_id)

    for sub in active_subs:
        sub_id = sub.get("id")
        pub_key = sub.get("wg_public_key")

        if not sub_id:
            continue

        log.info(
            "[AutoCleanup] Deactivate old sub_id=%s for tg_id=%s reason=%s",
            sub_id,
            telegram_user_id,
            reason,
        )

        db.deactivate_subscription_by_id(
            sub_id=sub_id,
            event_name=reason,
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
            caption=(
                "По реферальной ссылке тебе выдан пробный доступ к MaxNet VPN на 7 дней.\n\n"
                "Ниже — конфиг WireGuard и QR для подключения."
            ),
        )

    except Exception as e:
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
                text="🔐 Подключить VPN",
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
        [
            InlineKeyboardButton(
                text="💳 Вывести",
                callback_data="withdraw:open",
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
                text="💰 Продлить криптой",
                callback_data="heleket:open",
            ),
        ],
        [
            InlineKeyboardButton(
                text="📃 Тарифы",
                callback_data="subscription:open",
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
                # 3. Регистрируем факт старта по реферальному коду
                reg_res = db.register_referral_start(
                    invited_telegram_user_id=user.id,
                    referral_code=start_param,
                    raw_start_param=text,
                )

                # 4. Если регистрация прошла успешно — пытаемся выдать триал на 7 дней
                if reg_res and reg_res.get("ok"):
                    try:
                        await try_give_referral_trial_7d(
                            telegram_user_id=user.id,
                            telegram_username=user.username,
                        )
                    except Exception as e2:
                        log.error(
                            "[ReferralTrial] Error while giving trial for tg_id=%s: %r",
                            user.id,
                            e2,
                        )
                else:
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
        START_TEXT,
        reply_markup=SUBSCRIBE_KEYBOARD,
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        INSTRUCTION_TEXT,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


SUPPORT_TEXT = (
    "Если что-то пошло не так с оплатой или подключением VPN,\n"
    "ты можешь написать в поддержку:\n\n"
    "• @MaxNet_VPN\n"
    "• @rmw_ok\n\n"
    "Опиши проблему, укажи свой @username и, по возможности, приложи скриншоты."
)


SUBSCRIPTION_TEXT = (
    "<b>Почему выгоднее брать сразу на дольше:</b>\n"
    "• 3 месяца: экономия <b>30 ₽</b> (−10% к помесячной оплате).\n"
    "• 6 месяцев: экономия <b>120 ₽</b> (−20% к помесячной оплате).\n"
    "• 1 год: экономия <b>360 ₽</b> (−30% к помесячной оплате).\n\n"
    "Оплатить доступ можно:\n"
    "• банковской картой в рублях через ЮKassa (команда /buy);\n"
    "• криптовалютой через Heleket (команда /buy_crypto).\n\n"
    "Чтобы оформить подписку, воспользуйся командами /buy или /buy_crypto "
    "или нажми нужную кнопку под этим сообщением.\n\n"
    "🌐 Официальный сайт: https://maxnetvpn.ru"
)


REF_INFO_TEXT = (
    "🤝 <b>Реферальная программа MaxNet VPN</b>\n\n"
    "Приглашай друзей по своей ссылке и получай баллы, которыми можно оплачивать подписку.\n\n"
    "<b>Как это работает</b>\n"
    "1. В команде /ref ты получаешь личную реферальную ссылку.\n"
    "2. Друг переходит по ссылке и нажимает <b>Start</b> в боте.\n"
    "3. Если у друга ещё нет активной подписки и ему ранее не выдавался реферальный триал, "
    "бот автоматически выдаёт пробный доступ к MaxNet VPN на 7 дней.\n"
    "4. Когда приглашённый пользователь оплачивает подписку, ты и вышестоящие партнёры "
    "получаете баллы.\n\n"
    "<b>Бонус для приглашённого</b>\n"
    "• 7 дней пробного доступа по реферальной ссылке при соблюдении условий.\n"
    "• Пробный период по реферальной ссылке выдаётся только один раз и не выдаётся, "
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
    "/add_sub — выдать подписку вручную (подарок/ручной доступ).\n"
    "После /add_sub бот попросит переслать сообщение от пользователя и выбрать срок подписки.\n\n"
    "/broadcast — отправить текстовую рассылку всем пользователям.\n\n"
    "/promo_admin — сгенерировать SQL для вставки промокодов в таблицу promo_codes."
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



@router.message(Command("support"))
async def cmd_support(message: Message) -> None:
    await message.answer(
        SUPPORT_TEXT,
        disable_web_page_preview=True,
    )

@router.message(Command("my_id"))
async def cmd_my_id(message: Message) -> None:
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)
    await message.answer(
        f"Твой Telegram ID: <code>{message.from_user.id}</code>\n",
        #f"ADMIN_TELEGRAM_ID из .env: <code>{admin_id}</code>",
        disable_web_page_preview=True,
    )

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
    lines.append("💳 <b>Тарифы MaxNet VPN</b>\n")

    if not tariffs:
        # fallback, если с БД что-то не так
        lines.append("Сейчас тарифы временно недоступны. Попробуй позже.\n")
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
        reply_markup=REF_SHARE_KEYBOARD,
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
    await callback.message.answer(
        "Выбери тариф для оплаты баллами (игровой баланс):",
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

    if mode == "single":
        codes_preview = "\n".join(row.get("code") for row in promo_rows)
        text = (
            f"✅ Сгенерировано и сохранено в базе <b>{len(promo_rows)}</b> одноразовых промокодов.\n\n"
            "Список кодов:\n"
            f"<code>{codes_preview}</code>"
        )
    else:
        code_preview = promo_rows[0].get("code")
        text = (
            "✅ Сгенерирован и сохранён в базе многоразовый промокод.\n"
            f"Код: <code>{code_preview}</code>\n\n"
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

    try:
        confirmation_url = create_yookassa_payment(
            telegram_user_id=telegram_user_id,
            tariff_code=tariff_code,
            amount=tariff["amount"],
            description=f"MaxNet VPN — {tariff['label']}",
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
    try:
        client_priv = None
        client_pub = None
        client_ip = None
        send_config = True

        if extend_existing and reuse_priv and reuse_pub and reuse_ip:
            # Есть последняя подписка с валидными ключами/IP —
            # "оживляем" её конфиг (даже если она была деактивирована).
            deactivate_existing_active_subscriptions(
                telegram_user_id=telegram_user_id,
                reason="auto_replace_points_payment",
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
            )

            client_priv, client_pub = wg.generate_keypair()
            client_ip = wg.generate_client_ip()
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
            config_text = wg.build_client_config(
                client_private_key=client_priv,
                client_ip=client_ip,
            )

            await send_vpn_config_to_user(
                telegram_user_id=telegram_user_id,
                config_text=config_text,
                caption=(
                    "Подписка MaxNet VPN оплачена баллами.\n\n"
                    "Ниже — конфиг WireGuard и QR для подключения."
                ),
            )
        else:
            log.info(
                "[PointsPay] Reused existing config for tg_id=%s sub_id=%s (no new config sent)",
                telegram_user_id,
                sub_id,
            )

        if isinstance(expires_at, datetime):
            expires_str = expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")
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
        expires_str = expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    else:
        expires_str = str(expires_at)

    text = (
        "🔐 Текущий статус VPN-подписки:\n\n"
        f"• VPN IP: <code>{vpn_ip}</code>\n"
        f"• Действует до: <b>{expires_str}</b>\n\n"
        "Если связь пропадёт после этой даты — просто продли подписку через Tribute "
        "или оплати новый период по команде /buy."
    )


    await message.answer(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=SUBSCRIPTION_RENEW_KEYBOARD,
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
    if is_admin(message):
        try:
            total_subscribers = db.get_total_subscribers_count()
        except Exception as e:
            log.error(
                "[Referral] Failed to get total subscribers count for tg_id=%s: %r",
                telegram_user_id,
                e,
            )
            total_subscribers = None
        if total_subscribers is not None:
            lines.append(f"• Всего подписчиков: <b>{total_subscribers}</b>")

    # Пустая строка перед уровнями
    lines.append("")

    # Блок уровней 2–5 в формате: «приглашено / оплатили»
    lines.append("Уровни 2–5 (приглашено / оплатили):")
    for level in range(2, 6):
        lvl_inv = invited_by_levels.get(level) or 0
        lvl_paid = paid_by_levels.get(level) or 0
        lines.append(f"• {level} уровень — {lvl_inv} / {lvl_paid}")

    text = "\n".join(lines)

    await message.answer(
        text,
        disable_web_page_preview=True,
    )


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


@router.callback_query(F.data == "ref:open_from_notify")
async def ref_open_from_notify(callback: CallbackQuery) -> None:
    """
    Короткий вариант реферального сообщения по кнопке
    «🤝 Пригласить друга» под уведомлениями.
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
            try:
                # На всякий случай выключим все активные подписки (если вдруг что-то есть)
                deactivate_existing_active_subscriptions(
                    telegram_user_id=user.id,
                    reason="auto_replace_promo_new_sub",
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
                    config_text = wg.build_client_config(
                        client_private_key=client_priv,
                        client_ip=client_ip,
                    )

                    await send_vpn_config_to_user(
                        telegram_user_id=user.id,
                        config_text=config_text,
                        caption=(
                            "По промокоду тебе выдан доступ к MaxNet VPN.\n\n"
                            "Ниже — конфиг WireGuard и QR для подключения."
                        ),
                    )
                else:
                    log.info(
                        "[PromoApply] Reused existing config for tg_id=%s sub_id=%s (no new config sent)",
                        user.id,
                        new_sub_id,
                    )

            except Exception as e:
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
                expires_str = expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            else:
                expires_str = str(expires_at)

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

    if isinstance(new_expires_at, datetime):
        expires_str = new_expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")
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
    success = 0
    failed = 0

    await message.answer(
        f"Начинаю рассылку по {total} пользователям...\n"
        "Это может занять некоторое время.",
        disable_web_page_preview=True,
    )

    for user in users:
        chat_id = user.get("telegram_user_id")
        if not chat_id:
            continue

        try:
            await message.bot.send_message(
                chat_id=chat_id,
                text=text,
                disable_web_page_preview=True,
            )
            success += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            failed += 1
            log.warning("[Broadcast] Bot is blocked by chat_id=%s", chat_id)
            continue
        except TelegramRetryAfter as e:
            failed += 1
            log.warning(
                "[Broadcast] RetryAfter for chat_id=%s: %s seconds",
                chat_id,
                e.retry_after,
            )
            await asyncio.sleep(e.retry_after)
            continue
        except TelegramBadRequest as e:
            failed += 1
            log.warning(
                "[Broadcast] BadRequest for chat_id=%s: %s",
                chat_id,
                repr(e),
            )
            continue
        except Exception as e:
            failed += 1
            log.error(
                "[Broadcast] Unexpected error for chat_id=%s: %s",
                chat_id,
                repr(e),
            )
            continue

    await message.answer(
        f"Рассылка завершена.\n"
        f"Успешно: {success}\n"
        f"Ошибок: {failed}",
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
        expires_str = expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")
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
        expires_str = expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")
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
            expires_str = expires_at.strftime("%Y-%m-%d")
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
        expires_str = expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")
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
    vpn_ip = sub.get("vpn_ip")

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

    # теперь активируем нужную подписку
    sub = db.activate_subscription_by_id(
        sub_id=sub_id,
        event_name="admin_activate",
    )
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
        f"Peer в WireGuard добавлен.",
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
                    InlineKeyboardButton(text="1 месяц", callback_data="addsub:period:1m"),
                    InlineKeyboardButton(text="3 месяца", callback_data="addsub:period:3m"),
                ],
                [
                    InlineKeyboardButton(text="6 месяцев", callback_data="addsub:period:6m"),
                    InlineKeyboardButton(text="1 год", callback_data="addsub:period:1y"),
                ],
            ]
        )

        if target_username:
            user_line = f"Пользователь: <code>{target_id}</code> (@{target_username}).\n\n"
        else:
            user_line = f"Пользователь с TG ID: <code>{target_id}</code>.\n\n"

        await callback.message.answer(
            "Запрос демо-доступа одобрен.\n\n" + user_line + "Выбери срок демо-подписки:",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        await callback.answer("Выбери срок демо-подписки.")
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

        await callback.message.answer(
            f"Отказ по демо-доступу для пользователя <code>{target_id}</code> отправлен.",
            disable_web_page_preview=True,
        )
        await callback.answer("Отказ отправлен.")
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
    if period_code == "1m":
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
                "Ниже — конфиг WireGuard и QR для подключения."
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
        + f"Действует до: <b>{expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}</b>"
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
        vpn_ip = sub.get("vpn_ip")

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

        # Теперь активируем нужную подписку
        sub = db.activate_subscription_by_id(
            sub_id=sub_id,
            event_name="admin_activate",
        )
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
            f"Peer в WireGuard добавлен."
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
        BotCommand(command="promo_code", description="Применить промокод"),
        BotCommand(command="buy", description="Оплатить подписку картой (ЮKassa)"),
        BotCommand(command="buy_points", description="Оплатить подписку баллами"),
        BotCommand(command="buy_crypto", description="Оплатить подписку криптой (Heleket)"),
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

                try:
                    await bot.send_message(
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

                    log.info(
                        "[AutoNotify] Sent 3d-before-expire notification sub_id=%s tg_id=%s",
                        sub_id,
                        telegram_user_id,
                    )

                except TelegramForbiddenError:
                    log.warning(
                        "[AutoNotify] Bot is blocked by tg_id=%s (3d notice)",
                        telegram_user_id,
                    )
                except TelegramRetryAfter as e:
                    log.warning(
                        "[AutoNotify] RetryAfter for tg_id=%s (3d notice): %s",
                        telegram_user_id,
                        e.retry_after,
                    )
                    await asyncio.sleep(e.retry_after)
                except TelegramBadRequest as e:
                    log.warning(
                        "[AutoNotify] BadRequest for tg_id=%s (3d notice): %r",
                        telegram_user_id,
                        e,
                    )
                except Exception as e:
                    log.error(
                        "[AutoNotify] Unexpected error for tg_id=%s (3d notice): %r",
                        telegram_user_id,
                        e,
                    )

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

                try:
                    await bot.send_message(
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

                    log.info(
                        "[AutoNotify] Sent 1d-before-expire notification sub_id=%s tg_id=%s",
                        sub_id,
                        telegram_user_id,
                    )

                except TelegramForbiddenError:
                    log.warning(
                        "[AutoNotify] Bot is blocked by tg_id=%s (1d notice)",
                        telegram_user_id,
                    )
                except TelegramRetryAfter as e:
                    log.warning(
                        "[AutoNotify] RetryAfter for tg_id=%s (1d notice): %s",
                        telegram_user_id,
                        e.retry_after,
                    )
                    await asyncio.sleep(e.retry_after)
                except TelegramBadRequest as e:
                    log.warning(
                        "[AutoNotify] BadRequest for tg_id=%s (1d notice): %r",
                        telegram_user_id,
                        e,
                    )
                except Exception as e:
                    log.error(
                        "[AutoNotify] Unexpected error for tg_id=%s (1d notice): %r",
                        telegram_user_id,
                        e,
                    )

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

                except TelegramForbiddenError:
                    log.warning(
                        "[AutoNotify] Bot is blocked by tg_id=%s (1h notice)",
                        telegram_user_id,
                    )
                except TelegramRetryAfter as e:
                    log.warning(
                        "[AutoNotify] RetryAfter for tg_id=%s (1h notice): %s",
                        telegram_user_id,
                        e.retry_after,
                    )
                    await asyncio.sleep(e.retry_after)
                except TelegramBadRequest as e:
                    log.warning(
                        "[AutoNotify] BadRequest for tg_id=%s (1h notice): %r",
                        telegram_user_id,
                        e,
                    )
                except Exception as e:
                    log.error(
                        "[AutoNotify] Unexpected error for tg_id=%s (1h notice): %r",
                        telegram_user_id,
                        e,
                    )

        except Exception as e:
            log.error(
                "[AutoNotify] Unexpected error in auto_notify_expiring_subscriptions: %r",
                e,
            )

        # Проверяем примерно раз в 10 минут
        await asyncio.sleep(600)


async def auto_deactivate_expired_subscriptions() -> None:
    """
    Периодически ищет в базе все активные подписки с истекшим expires_at,
    деактивирует их и удаляет peer из WireGuard.
    (Уведомление пользователю об окончании теперь отправляется заранее —
    за ~1 час до окончания в auto_notify_expiring_subscriptions.)
    """
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

        except Exception as e:
            log.error(
                "[AutoExpire] Unexpected error in auto_deactivate_expired_subscriptions: %s",
                repr(e),
            )

        # Проверяем раз в 60 секунд (можешь настроить под себя)
        await asyncio.sleep(60)


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

    await set_bot_commands(bot)

    # запускаем фоновые воркеры
    asyncio.create_task(auto_deactivate_expired_subscriptions())
    asyncio.create_task(auto_notify_expiring_subscriptions(bot))

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
