
MaxNet VPN — backend + Telegram-бот
Сервис для автоматической выдачи VPN-ключей WireGuard через платежи в Tribute и через ручную выдачу из админки Telegram.
Состав проекта


PostgreSQL — хранит подписки (vpn_subscriptions).


WireGuard — сам VPN-сервер, пирами управляет код (wg.py).


FastAPI backend (app/main.py)


принимает вебхуки Tribute /tribute/webhook;


управляет подписками в БД и WireGuard;


отдаёт простой health-чек.




Telegram-бот (app/tg_bot_runner.py)


клиентский кабинет (команды /start, /status, и т.п.);


админка (команды /admin_*, /add_sub, инлайн-меню);


фоновой воркер авто-деактивации подписок.




Отправка конфигов и инструкций (app/bot.py)


отправка .conf файлика;


генерация и отправка QR-кода;


отправка инструкции.





1. Как запускать проект и что за что отвечает
1.1. Переменные окружения (.env)
Используются настройки из app/config.py. В .env должны быть:
DB_HOST=localhost
DB_PORT=5432
DB_NAME=postgres
DB_USER=postgres
DB_PASSWORD=пароль_от_бд

WG_INTERFACE_NAME=wg0
WG_SERVER_PUBLIC_KEY=...
WG_SERVER_ENDPOINT=your_server_ip_or_domain:51820
WG_CLIENT_NETWORK_PREFIX=10.8.0.
WG_CLIENT_NETWORK_CIDR=24
WG_CLIENT_IP_START=10

TRIBUTE_WEBHOOK_SECRET=секрет_для_webhook'а_tribute

TELEGRAM_BOT_TOKEN=токен_бота_от_BotFather
ADMIN_TELEGRAM_ID=123456789  # твой Telegram user id


WG_CLIENT_NETWORK_PREFIX и WG_CLIENT_NETWORK_CIDR должны соответствовать конфигу wg0.conf.


1.2. Подготовка PostgreSQL


Создаёшь базу:


CREATE DATABASE postgres; -- или своё имя, и прописать в DB_NAME



Пользователь и доступ (если нужно):


CREATE USER vpn_user WITH PASSWORD 'strong_password';
GRANT ALL PRIVILEGES ON DATABASE postgres TO vpn_user;



Таблица создаётся автоматически при старте FastAPI (в on_startup вызывается db.init_db()), но ты уже её создал и ALTER делал — всё ок.


Структура актуальная (важные поля):


id — PK


tribute_user_id — user ID в Tribute


telegram_user_id — Telegram ID пользователя


telegram_user_name — username (может быть NULL)


subscription_id — id подписки/доната в Tribute


period_id, period, channel_id, channel_name — данные из Tribute


vpn_ip — IP клиента в сети WireGuard


wg_private_key, wg_public_key — ключи клиента


created_at, expires_at — даты


active — активна подписка или нет


last_event_name — последнее событие, которое изменило строку (new_donation, auto_expire, admin_manual_add и т.п.)



1.3. WireGuard
Файл конфига: /etc/wireguard/wg0.conf.
Скрипт app/wg.py:


add_peer(public_key, allowed_ip, telegram_user_id)


вызывает:
wg set wg0 peer <pubkey> allowed-ips <ip/cidr>



дописывает в wg0.conf блок вида:
# auto-added by vpn_service user=<telegram_id>
[Peer]
PublicKey = ...
AllowedIPs = 10.8.0.X/24





remove_peer(public_key)


вызывает:
wg set wg0 peer <pubkey> remove



вырезает соответствующий блок # auto-added by vpn_service ... из wg0.conf.




generate_keypair() — генерирует приватный и публичный ключи для клиента.


generate_client_ip()


смотрит последние IP в БД (db.get_max_client_ip_last_octet()),


берёт максимальный последний октет и выдаёт следующий.





1.4. Запуск backend (FastAPI)
Из корня проекта:
uvicorn app.main:app --host 0.0.0.0 --port 8000

Функционал:


GET / — простой ответ "MaxNet VPN backend is alive".


GET /health — health-чек.


POST /tribute/webhook — точка входа для Tribute-вебхука.


Проверяет подпись trbt-signature через TRIBUTE_WEBHOOK_SECRET.


Разбирает name события:


new_subscription → handle_new_subscription()


new_donation → handle_new_donation()


cancelled_subscription → handle_cancelled_subscription()






GET /admin/subscriptions — JSON со списком последних подписок (db.get_last_subscriptions).


POST /admin/subscriptions/{sub_id}/deactivate — деактивация подписки + удаление peer в WireGuard (ручной API).



1.5. Логика обработки Tribute-вебхуков
new_subscription
Функция handle_new_subscription(payload):


Достаёт:


user_id (Tribute) → tribute_user_id


telegram_user_id


subscription_id, period_id, period


channel_id, channel_name


expires_at (строка → datetime)




Проверяет, есть ли уже активная подписка для этой тройки (tribute_user_id, period_id, channel_id):


db.get_active_subscription(...).




Если есть:


продлевает expires_at через db.update_subscription_expiration(...);


отправляет в Telegram текст: «Подписка продлена…».


новых ключей/пиров не создаёт.




Если нет:


генерирует пару ключей и IP (wg.generate_keypair, wg.generate_client_ip);


добавляет peer в WireGuard (wg.add_peer(...));


пишет строку в БД db.insert_subscription(...);


генерирует конфиг wg.build_client_config(...);


отправляет пользователю .conf + QR + инструкцию через bot.send_vpn_config_to_user(...).





new_donation
Функция handle_new_donation(payload, created_at_str):


Берёт:


donation_request_id → subscription_id;


user_id → tribute_user_id;


telegram_user_id;


period, donation_name (как channel_name).




Считает expires_at = created_at + 30 дней.


Проверяет, есть ли запись в БД с такой парой (tribute_user_id, subscription_id):


db.get_subscription_by_tribute_and_subscription(...).




Если есть и:


active = True


last_event_name = "new_donation"
→ считаем, что это повторный вебхук:


не создаём новую подписку и peer;


переотправляем конфиг по уже сохранённому ключу/адресу;


логируем.




Если нет:


генерируем ключи и IP;


создаём peer в WireGuard;


создаём запись в БД с event_name="new_donation";


шлём конфиг/QR/инструкцию пользователю.





cancelled_subscription
Функция handle_cancelled_subscription(payload):


Берёт: user_id, telegram_user_id, period_id, channel_id.


Деактивирует подписки этого пользователя на этот период/канал:


db.deactivate_subscriptions_for_period(...) → возвращает список подписок.




Для каждой подписки пытается удалить peer из WireGuard по wg_public_key.


Отправляет пользователю сообщение о том, что подписка отменена, VPN отключён.



1.6. Запуск Telegram-бота
Бот запускается отдельным процессом:
python -m app.tg_bot_runner

Внутри:


настраивается Bot с parse_mode=HTML;


регистрируется router;


выставляются команды /start, /help, /status, /subscription, /support;


создаётся фоновой таск auto_deactivate_expired_subscriptions();


запускается dp.start_polling(bot).


Фоновая авто-деактивация
Функция auto_deactivate_expired_subscriptions():


раз в 60 секунд:


берёт из БД все подписки, где:


active = TRUE;


expires_at <= NOW() (db.get_expired_active_subscriptions()).




для каждой:


вызывает db.deactivate_subscription_by_id(..., event_name="auto_expire");


если есть wg_public_key — вызывает wg.remove_peer(pub_key).






логирует, что именно удалено.


Таким образом:


подписка как запись в БД остаётся, но active = FALSE, last_event_name="auto_expire";


peer из WireGuard удаляется и из рантайма, и из конфига wg0.conf.



1.7. Логи
Всё логируется в:
/home/vpn_service/logs/vpn_service.log

Через logger из logger.py:
logger = logging.getLogger("vpn_service")


2. Клиентский кабинет в Telegram
Клиент работает только через Telegram-бота. Основные команды:
2.1. /start
Хендлер:
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        START_TEXT,
        reply_markup=SUBSCRIBE_KEYBOARD,
    )

Показывает промо-текст + кнопку:
SUBSCRIBE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔐 Подключить VPN",
                url="https://t.me/tribute/app?startapp=dAUr",
            )
        ]
    ]
)

То есть клиент:


Заходит к боту → жмёт «Подключить VPN».


Открывается Tribute WebApp → клиент оформляет донат/подписку.


Tribute шлёт вебхук → backend создаёт peer, записывает в БД, шлёт конфиг/QR/инструкцию.



2.2. /help
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        INSTRUCTION_TEXT,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

Отсылает большую инструкцию по установке WireGuard и импорту конфига/QR.

2.3. /subscription
Показывает тарифы:
@router.message(Command("subscription"))
async def cmd_subscription(message: Message) -> None:
    await message.answer(
        SUBSCRIPTION_TEXT,
        disable_web_page_preview=True,
    )

Там список тарифов и пояснение про первый месяц.

2.4. /support
@router.message(Command("support"))
async def cmd_support(message: Message) -> None:
    await message.answer(
        SUPPORT_TEXT,
        disable_web_page_preview=True,
    )

Показывает контакты поддержки (@MaxNet_VPN, @rmw_ok) и просьбу указать username и скрины.

2.5. /status
Хендлер:
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
    ...

Логика:


Ищет последнюю действующую подписку для этого telegram_user_id:


active = TRUE


expires_at > NOW()


сортировка по expires_at DESC, id DESC.




Если нет — предлагает оформить подписку.


Если есть — показывает:


VPN IP


срок действия до <дата>.




Это и есть «клиентский кабинет» — пользователь в любой момент может проверить, активен ли у него доступ и до какой даты.

3. Админка Telegram
Админка работает через того же бота, но функционал доступен только пользователю с ADMIN_TELEGRAM_ID из .env.
3.1. Проверка прав администратора
Функция:
def is_admin(message: Message) -> bool:
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)
    return admin_id != 0 and message.from_user is not None and message.from_user.id == admin_id

Во всех админ-командах в начале:
if not is_admin(message):
    await message.answer("Эта команда доступна только администратору.")
    return

В инлайн-кнопках то же самое по callback.from_user.id.

3.2. Справка по админ-командам: /admin_info
@router.message(Command("admin_info"))
async def cmd_admin_info(message: Message) -> None:
    ...
    await message.answer(
        ADMIN_INFO_TEXT,
        disable_web_page_preview=True,
    )

Текст ADMIN_INFO_TEXT:


описывает:


/admin_cmd


/admin_last


/admin_list


/admin_sub <id>


/admin_activate <id>


/admin_deactivate <id>


/admin_delete <id>


/add_sub (ручная выдача)





3.3. Главное админ-меню: /admin_cmd
@router.message(Command("admin_cmd"))
async def cmd_admin_cmd(message: Message) -> None:
    ...
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

Инлайн-хендлер:
@router.callback_query(F.data.startswith("admcmd:"))
async def admin_cmd_inline(callback: CallbackQuery, state: FSMContext) -> None:
    ...
    if action == "info":
        ...  # выводит ADMIN_INFO_TEXT

    if action == "add_sub":
        ...  # запускает процесс /add_sub

    if action == "last":
        await cmd_admin_last(callback.message)

    if action == "list":
        await cmd_admin_list(callback.message)

То есть ты можешь:


смотреть описание команд;


запускать выдачу подписки в пару кликов;


смотреть последнюю/список подписок.



3.4. Просмотр последней подписки: /admin_last
@router.message(Command("admin_last"))
async def cmd_admin_last(message: Message) -> None:
    subs = db.get_last_subscriptions(limit=1)
    ...

Показывает последнюю запись в таблице с управлением:


ID


TG


IP


active


до <expires_at>


event=<last_event_name>


И инлайн-клавиатуру:


✅ Активировать


⛔ Деактивировать


🗑 Удалить


Кнопки шлют callback'и adm:act:<id>, adm:deact:<id>, adm:del:<id>.

3.5. Список подписок: /admin_list
@router.message(Command("admin_list"))
async def cmd_admin_list(message: Message) -> None:
    subs = db.get_last_subscriptions(limit=30)
    ...

Для каждой подписки:


берёт id, telegram_user_id, telegram_user_name, vpn_ip, active, expires_at, last_event_name;


если expires_at <= now → помечает (истекла);


если telegram_user_name есть → показывает как TG: 123456789 (username).


Пример строки:
ID: 13 | TG: 970389187 (rmw_ok) | IP: 10.8.0.57 | active=True | до 2025-12-30 14:38:52 UTC | event=admin_manual_add
ID: 2  | TG: 388247897 (user123) | IP: 10.8.0.50 | active=False | до 2025-11-30 15:30:08 UTC (истекла) | event=auto_expire


3.6. Просмотр конкретной подписки: /admin_sub <id>
@router.message(Command("admin_sub"))
async def cmd_admin_sub(message: Message) -> None:
    ...
    sub = db.get_subscription_by_id(sub_id=sub_id)
    ...

Показывает:


ID


TG


IP


active


до <expires>


event=<last_event_name>


Плюс инлайн-кнопки для активации/деактивации/удаления той же логикой adm:*.

3.7. Ручное управление подписками (команды)
/admin_deactivate <id>


Находит активную подписку с этим id;


ставит active = FALSE, last_event_name = "admin_deactivate";


если есть wg_public_key — вызывает wg.remove_peer(pub_key);


шлёт текст «Подписка деактивирована…».


/admin_activate <id>


Находит неактивную подписку с этим id;


ставит active = TRUE, last_event_name = "admin_activate";


достаёт wg_public_key, vpn_ip, telegram_user_id;


вычисляет allowed_ip = vpn_ip/<WG_CLIENT_NETWORK_CIDR>;


вызывает wg.add_peer(...);


шлёт текст «Подписка активирована…».


/admin_delete <id>


Берёт подписку id из БД;


если есть wg_public_key — удаляет peer в WireGuard;


удаляет запись из БД db.delete_subscription_by_id;


шлёт текст «Подписка полностью удалена…».



3.8. Инлайн-управление подписками (кнопки) — adm:*
Хендлер:
@router.callback_query(F.data.startswith("adm:"))
async def admin_inline_callback(callback: CallbackQuery) -> None:
    ...
    _, action, sub_id_str = parts

Поддерживает:


adm:deact:<id> — деактивация (аналог /admin_deactivate).


adm:act:<id> — активация (аналог /admin_activate).


adm:del:<id> — удаление (аналог /admin_delete).


Поведение то же самое, только результат присылается в чат, откуда была нажата кнопка.

3.9. Ручная выдача подписки: /add_sub
Это ключевая штука для «выдать другу» / теста.
Шаг 1. Запуск команды
@router.message(Command("add_sub"))
async def cmd_add_sub(message: Message, state: FSMContext) -> None:
    ...
    await state.set_state(AdminAddSub.waiting_for_target)
    await message.answer(
        "Перешли сюда <b>любое сообщение</b> от пользователя, которому нужно выдать VPN-доступ.\n\n"
        "Либо отправь его <b>числовой Telegram ID</b> вручную.",
        ...
    )

Админ вводит /add_sub — бот просит:


либо переслать любое сообщение от пользователя;


либо отправить просто его Telegram ID числом.


Шаг 2. Определение пользователя
Хендлер:
@router.message(AdminAddSub.waiting_for_target)
async def admin_add_sub_get_target(message: Message, state: FSMContext) -> None:
    ...
    if message.forward_from and message.forward_from.id:
        target_id = message.forward_from.id
        target_username = message.forward_from.username
    elif message.text and message.text.isdigit():
        target_id = int(message.text)
    ...
    await state.update_data(
        target_telegram_user_id=target_id,
        target_telegram_user_name=target_username,
    )

Если переслано — бот умеет взять:


forward_from.id — настоящий Telegram ID пользователя;


forward_from.username — username для записи в telegram_user_name.


После этого бот показывает клавиатуру выбора срока:
[ 1 месяц | 3 месяца ]
[ 6 месяцев | 1 год ]

Шаг 3. Выбор срока и создание подписки
Инлайн-хендлер:
@router.callback_query(AdminAddSub.waiting_for_period, F.data.startswith("addsub:period:"))
async def admin_add_sub_choose_period(callback: CallbackQuery, state: FSMContext) -> None:
    ...
    if period_code == "1m": days = 30, "1 месяц"
    if period_code == "3m": days = 90, "3 месяца"
    if period_code == "6m": days = 180, "6 месяцев"
    if period_code == "1y": days = 365, "1 год"
    ...
    state_data = await state.get_data()
    target_id = state_data.get("target_telegram_user_id")
    target_username = state_data.get("target_telegram_user_name")
    ...
    now = datetime.utcnow()
    expires_at = now + timedelta(days=days)

Дальше:


Генерирует ключи и IP.


Добавляет peer в WireGuard:
wg.add_peer(
    public_key=client_pub,
    allowed_ip=allowed_ip,
    telegram_user_id=target_id,
)



Пишет запись в БД как «ручная админская»:
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



Собирает конфиг и пытается отправить пользователю:
await send_vpn_config_to_user(
    telegram_user_id=target_id,
    config_text=config_text,
    caption=...,
)


Важно: если бот никогда не писал этому пользователю, а пользователь не инициировал диалог с ботом — Telegram не даст отправить (Forbidden: bot can't initiate conversation). Ты это уже видел в логах.



Админ получает подтверждение:


✅ Ручная подписка создана.

Пользователь TG: <id>
VPN IP: <ip>
Срок: <1 месяц/3 месяца/...>
Действует до: <дата>


3.10. Авто-удаление по истечению срока
Любая подписка (Tribute/донат/ручная):


при создании получает expires_at;


пока active = TRUE и expires_at > NOW() — считается действующей;


как только код авто-деактивации увидит, что expires_at <= NOW():


деактивирует запись (active = FALSE, last_event_name="auto_expire");


удаляет peer из WireGuard.




При этом:


в БД запись остаётся — можно её посмотреть через /admin_list, /admin_sub;


в wg0.conf и в рантайме WireGuard peer исчезает;


в /admin_list будет видна пометка (истекла) рядом с датой.



Если хочешь — дальше можно отдельно оформить README по деплою (systemd-юнит для uvicorn и для tg_bot_runner, backup БД, ротация логов) — но с этим описанием уже понятно:


что запускается и как;


откуда берётся VPN-ключ;


как работает клиентский кабинет;


как устроена админка и ручная выдача/управление ключами.

