import asyncio
from datetime import datetime, timedelta
from pathlib import Path


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
from .logger import get_logger
from .yookassa_client import create_yookassa_payment
log = get_logger()




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

BASE_DIR = Path(__file__).resolve().parent.parent
TERMS_FILE_PATH = BASE_DIR / "TERMS.md"
PRIVACY_FILE_PATH = BASE_DIR / "PRIVACY.md"




class AdminAddSub(StatesGroup):
    waiting_for_target = State()
    waiting_for_period = State()


class DemoRequest(StatesGroup):
    waiting_for_message = State()

# Справочник тарифов для оплаты через ЮKassa.
# Цены указаны в РУБЛЯХ.
TARIFFS = {
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




TARIFF_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="1 месяц — 100 ₽",
                callback_data="pay:tariff:1m",
            ),
        ],
        [
            InlineKeyboardButton(
                text="3 месяца — 270 ₽",
                callback_data="pay:tariff:3m",
            ),
        ],
        [
            InlineKeyboardButton(
                text="6 месяцев — 480 ₽",
                callback_data="pay:tariff:6m",
            ),
        ],
        [
            InlineKeyboardButton(
                text="1 год — 840 ₽",
                callback_data="pay:tariff:1y",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Навсегда — 1990 ₽",
                callback_data="pay:tariff:forever",
            ),
        ],
    ]
)





# Кнопка "Подключить VPN" и кнопка "Запросить демо доступ"
SUBSCRIBE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔐 Подключить VPN (Tribute)",
                url="https://t.me/tribute/app?startapp=dAUr",
            ),
        ],
        [
            InlineKeyboardButton(
                text="💳 Оплатить картой (ЮKassa)",
                callback_data="pay:open",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🎁 Запросить демо доступ",
                callback_data="demo_request",  # изменен callback_data
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





START_TEXT = (
    "MaxNet VPN | Сервис защищённого подключения\n\n"
    "⚡ Подключение к серверам в Европе\n"
    "🔐 Шифрованное соединение для работы и личных задач\n"
    "📲 Настройки WireGuard для телефона и ПК\n"
    "🤖 Автоматическая выдача доступа через бота, автодеактивация по окончании срока\n\n"
    "Чтобы оформить доступ, нажми кнопку ниже 👇\n\n"
    "🌐 Официальный сайт: https://maxnetvpn.ru\n\n"
    "Используя бота MaxNet VPN, ты подтверждаешь, что ознакомился с Пользовательским "
    "соглашением (/terms) и принимаешь его."
)





@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
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
    "💳 <b>Тарифы MaxNet VPN</b>\n\n"
    "🔹 <b>1 месяц</b> — <b>100 ₽</b>\n"
    "🔹 <b>3 месяца</b> — <b>270 ₽</b>\n"
    "🔹 <b>6 месяцев</b> — <b>480 ₽</b>\n"
    "🔹 <b>1 год</b> — <b>840 ₽</b>\n"
    "🔹 <b>Навсегда</b> — <b>1990 ₽</b>\n\n"
    "<b>Почему выгоднее брать сразу на дольше:</b>\n"
    "• 3 месяца: экономия <b>30 ₽</b> (−10% к помесячной оплате).\n"
    "• 6 месяцев: экономия <b>120 ₽</b> (−20% к помесячной оплате).\n"
    "• 1 год: экономия <b>360 ₽</b> (−30% к помесячной оплате).\n\n"
    "Оплатить доступ можно:\n"
    "• через Tribute (кнопка «Подключить VPN»);\n"
    "• банковской картой в рублях через ЮKassa (команда /buy).\n\n"
    "Чтобы оформить подписку, нажми кнопку «Подключить VPN» под этим сообщением или используй /start, "
    "либо выбери /buy для оплаты картой.\n\n"
    "🌐 Официальный сайт: https://maxnetvpn.ru"
)


PROMO_TEXT = (
    "🎯 <b>Как сэкономить на подписке MaxNet VPN</b>\n\n"
    "Базовая цена — <b>100 ₽ в месяц</b> при оплате помесячно.\n\n"
    "Если брать сразу на дольше, получается выгоднее:\n\n"
    "• <b>3 месяца за 270 ₽</b>\n"
    "  Вместо 300 ₽ при помесячной оплате — экономия <b>30 ₽</b> (−10%).\n\n"
    "• <b>6 месяцев за 480 ₽</b>\n"
    "  Вместо 600 ₽ при помесячной оплате — экономия <b>120 ₽</b> (−20%).\n\n"
    "• <b>1 год за 840 ₽</b>\n"
    "  Вместо 1200 ₽ при помесячной оплате — экономия <b>360 ₽</b> (−30%).\n\n"
    "Тариф <b>«Навсегда» за 1990 ₽</b> окупается примерно за 2 года активного использования.\n\n"
    "Выбрать и оплатить подходящий тариф можно командой /buy или кнопками в /start."
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

    await message.answer(
        terms_text,
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
    "После /add_sub бот попросит переслать сообщение от пользователя и выбрать срок подписки."
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
    await message.answer(
        SUBSCRIPTION_TEXT,
        disable_web_page_preview=True,
    )

@router.message(Command("promo"))
async def cmd_promo(message: Message) -> None:
    await message.answer(
        PROMO_TEXT,
        disable_web_page_preview=True,
    )


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    await message.answer(
        "Выбери тариф для оплаты через банковскую карту (ЮKassa):",
        reply_markup=TARIFF_KEYBOARD,
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


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    user_id = message.from_user.id

    sub = db.get_latest_subscription_for_telegram(telegram_user_id=user_id)
    if not sub:
        await message.answer(
            "У тебя пока нет активной VPN-подписки.\n\n"
            "Нажми кнопку «Подключить VPN» в меню или используй /start.",
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
            "Сейчас запросы на демо-доступ временно недоступны. Попробуй позже или оформи подписку через Tribute.",
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
            "Ты можешь оформить платную подписку через кнопку «Подключить VPN» в боте "
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
        BotCommand(command="subscription", description="Тарифы и стоимость подписки"),
        BotCommand(command="promo", description="Выгодные варианты подписки"),
        BotCommand(command="buy", description="Оплатить подписку картой (ЮKassa)"),
        BotCommand(command="demo", description="Запросить демо-доступ"),
        BotCommand(command="support", description="Связаться с поддержкой"),
        BotCommand(command="privacy", description="Политика конфиденциальности"),
        BotCommand(command="terms", description="Пользовательское соглашение"),
    ]
    await bot.set_my_commands(commands)






async def auto_deactivate_expired_subscriptions() -> None:
    """
    Периодически ищет в базе все активные подписки с истекшим expires_at,
    деактивирует их, удаляет peer из WireGuard и шлёт пользователю уведомление.
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

                telegram_user_id = deactivated.get("telegram_user_id")

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

                # Пытаемся отправить уведомление о том, что подписка закончилась
                if telegram_user_id:
                    try:
                        await send_subscription_expired_notification(
                            telegram_user_id=telegram_user_id,
                        )
                        log.info(
                            "[AutoExpire] Sent expiration notification to tg_id=%s for sub_id=%s",
                            telegram_user_id,
                            sub_id,
                        )
                    except Exception as e:
                        log.error(
                            "[AutoExpire] Failed to send expiration notification to tg_id=%s for sub_id=%s: %s",
                            telegram_user_id,
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

    # запускаем фоновый воркер авто-деактивации
    asyncio.create_task(auto_deactivate_expired_subscriptions())

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()

    await dp.start_polling(bot)





if __name__ == "__main__":
    asyncio.run(main())
