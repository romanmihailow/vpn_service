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
