"""
Тексты сообщений бота. Централизованное хранение для удобства правок.
"""

# === Onboarding (реферальная ссылка) ===
REF_LINK_WELCOME_TEXT = (
    "<b>MaxNet VPN</b>\n\n"
    "Тестовый доступ на 7 дней по приглашению.\n\n"
    "Чтобы подключиться:\n\n"
    "1️⃣ Установи приложение WireGuard\n"
    "<a href=\"https://apps.apple.com/app/wireguard/id1441195209\">App Store</a> | "
    "<a href=\"https://play.google.com/store/apps/details?id=com.wireguard.android\">Play Market</a>\n\n"
    "2️⃣ Нажми кнопку ниже\n\n"
    "Подключение занимает около 30 секунд."
)

REF_TRIAL_BUTTON_TEXT = "🚀 Получить тестовый доступ"

# === Короткая инструкция после отправки конфига ===
CONNECTION_INSTRUCTION_SHORT = (
    "Готово 👌\n\n"
    "Я отправил файл конфигурации и QR-код.\n\n"
    "Открой WireGuard → нажми \"+\" → выбери:\n\n"
    "• Импорт из файла (vpn.conf из сообщения выше)\n"
    "или\n"
    "• Сканировать QR-код\n\n"
    "Если статус Connected — VPN работает.\n\n"
    "Если что-то не получается — нажми «🧑‍💻 Нужна помощь».\n"
    "Мы поможем подключить VPN."
)

# === Captions для конфига ===
REF_TRIAL_CONFIG_CAPTION = (
    "По реферальной ссылке тебе выдан пробный доступ к MaxNet VPN на 7 дней.\n\n"
    "Файл vpn.conf — в этом сообщении. QR-код — в следующем 👇"
)

CONFIG_QR_CAPTION = (
    "Отсканируй QR (нужен второй телефон) или импортируй файл из сообщения выше 👆"
)

# === Caption по умолчанию (Tribute и др.) ===
DEFAULT_CONFIG_CAPTION = (
    "Спасибо за подписку через Tribute!\n\n"
    "Файл vpn.conf — в этом сообщении. QR-код — в следующем 👇"
)

# === Кнопка помощи после выдачи конфига ===
SUPPORT_BUTTON_TEXT = "🧑‍💻 Нужна помощь"
SUPPORT_URL = "https://t.me/MaxNet_VPN_Support"

# === Кнопка быстрой самопроверки подключения после конфига ===
CONFIG_CHECK_NOW_BUTTON_TEXT = "🔍 Проверить подключение"

CONFIG_CHECK_NOW_OK = (
    "VPN уже подключён ✅\n\n"
    "Соединение установлено, всё работает."
)

CONFIG_CHECK_NOW_FAIL = (
    "VPN пока не подключён.\n\n"
    "Что сделать:\n"
    "1. Открой WireGuard\n"
    "2. Проверь, что туннель добавлен\n"
    "3. Включи туннель\n\n"
    "Если не получается — нажми «🧑‍💻 Нужна помощь»."
)

CONFIG_CHECK_NOW_UNKNOWN = (
    "Не удалось точно проверить подключение.\n"
    "Попробуй ещё раз через минуту или обратись в поддержку."
)

# === Подсказки, что боту можно писать вопросы текстом ===
SUPPORT_DISCOVERY_TEXT = (
    "💬 Если возникнут вопросы — просто напишите мне сообщением.\n\n"
    "Например:\n"
    "— VPN не работает\n"
    "— вышли конфиг\n"
    "— статус подписки\n"
    "— как подключиться"
)

SUPPORT_AFTER_CONFIG_HINT = (
    "Если что-то не работает — просто напишите:\n"
    "«VPN не работает»\n"
    "или\n"
    "«как подключиться»"
)

SUPPORT_AFTER_ACTIVATION_HINT = (
    "Если возникнут сложности — просто напишите мне:\n"
    "— VPN не работает\n"
    "— вышли конфиг\n"
    "— статус подписки"
)

# === Post-config connection check (checkpoint) ===
CONFIG_CHECK_MESSAGE = "Удалось подключиться к VPN?"

CONFIG_CHECK_SUCCESS = (
    "Отлично 👌\n\n"
    "Если что-то понадобится позже — просто напишите мне."
)

CONFIG_CHECK_FAIL = "Понял. Что именно не получилось?"

CONFIG_CHECK_OPTIONS = {
    "not_found": "Не нашёл конфиг",
    "import": "Не получается импортировать",
    "connected_no_internet": "VPN подключён, но сайты не открываются",
    "support": "Нужна помощь",
}

# === /help — короткая инструкция без привязки к «я отправил» ===
HELP_INSTRUCTION = (
    "📱 Подключение к VPN:\n\n"
    "1. Установи WireGuard:\n"
    "<a href=\"https://apps.apple.com/app/wireguard/id1441195209\">App Store</a> | "
    "<a href=\"https://play.google.com/store/apps/details?id=com.wireguard.android\">Play Market</a>\n\n"
    "2. Получи конфиг от бота (кнопка «Получить тестовый доступ» или после оплаты).\n\n"
    "3. В WireGuard: нажми «+» → Импорт из файла или Сканировать QR-код.\n\n"
    "Если статус Connected — VPN работает. Вопросы: @MaxNet_VPN_Support"
)
