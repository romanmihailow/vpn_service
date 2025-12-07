# MaxNet VPN Bot

Телеграм-бот для автоматической выдачи и управления VPN-доступом на базе WireGuard.

- Автовыдача конфигов WireGuard через бота
- Привязка подписок к платежам (через Tribute)
- Автоотключение доступа по окончании подписки
- Админ-панель в Telegram (команды + инлайн-кнопки)
- Автоматический деплой через GitHub Actions на сервер с Docker

---

## Возможности

### Для пользователя

- `/start` — приветственное сообщение и кнопка «Подключить VPN»
- `/help` — инструкция по подключению VPN (текст берётся из `INSTRUCTION_TEXT` в `app/bot.py`)
- `/status` — показать статус подписки:
  - IP-адрес VPN
  - дата окончания подписки
- `/subscription` — список тарифов и стоимости
- `/support` — контакты для связи с поддержкой

После оплаты через Tribute бот:
1. Создаёт конфиг WireGuard для пользователя
2. Отправляет конфиг и QR-код в Telegram
3. Автоматически отключает доступ по истечении срока подписки

### Для администратора

Админ определяется по `ADMIN_TELEGRAM_ID` из `.env`.

Основные команды:

- `/admin_cmd` — меню администратора с инлайн-кнопками
- `/admin_info` — краткое описание всех админ-команд
- `/admin_last` — показать последнюю подписку + кнопки управления
- `/admin_list` — список последних подписок (каждая — отдельная инлайн-кнопка)
- `/admin_sub <id>` — показать конкретную подписку по ID
- `/admin_activate <id>` — активировать подписку и добавить peer в WireGuard
- `/admin_deactivate <id>` — деактивировать подписку и удалить peer из WireGuard
- `/admin_delete <id>` — полностью удалить подписку и peer
- `/add_sub` — выдать подписку вручную (подарок/ручной доступ):
  - переслать сообщение от пользователя ИЛИ отправить его Telegram ID
  - выбрать срок подписки кнопками (1/3/6 месяцев, 1 год)

### Авто-деактивация подписок

В `tg_bot_runner.py` есть фоновой воркер:

```python
async def auto_deactivate_expired_subscriptions() -> None:
    ...
Он:

периодически ищет активные подписки с истёкшим expires_at

помечает их неактивными в БД

удаляет peer из WireGuard

Технологии
Python 3.12

aiogram 3 — Telegram-бот

PostgreSQL — хранение подписок

WireGuard — VPN

Docker / docker-compose — упаковка и запуск сервиса

GitHub Actions — деплой на удалённый сервер по SSH

Структура проекта (упрощённо)
app/tg_bot_runner.py — точка входа бота

app/telegram_handlers.py (или аналогичный файл с роутером) — хендлеры команд и коллбеков

app/bot.py — отправка конфигов, текст инструкции, генерация QR-кодов

app/db.py — работа с базой данных (подписки, статусы и т.д.)

app/wg.py — обёртка над WireGuard:

генерация ключей

выдача IP

добавление/удаление peer

Dockerfile — сборка образа бота

docker-compose.yml — запуск контейнера на сервере

.github/workflows/deploy.yml — GitHub Actions для деплоя

Подготовка окружения
1. Переменные окружения
Создай файл .env в корне проекта. Пример (дополнить под себя):

env
Копировать код
TELEGRAM_BOT_TOKEN=токен_бота_из_BotFather
ADMIN_TELEGRAM_ID=123456789

# Параметры БД (пример)
DB_HOST=localhost
DB_PORT=5432
DB_NAME=vpn_service
DB_USER=vpn_user
DB_PASSWORD=super_secret_password

# Параметры WireGuard (см. app/config.py)
WG_CLIENT_NETWORK_CIDR=32
WG_INTERFACE_NAME=wg0
Точный список переменных смотри в модуле app/config.py.

Локальный запуск (без Docker)
bash
Копировать код
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install --upgrade pip
pip install -r requirements.txt

python -m app.tg_bot_runner
Бот запустится и начнёт опрашивать Telegram API.

Запуск в Docker
В репозитории уже есть Dockerfile и docker-compose.yml.

Билд и запуск
bash
Копировать код
docker compose build
docker compose up -d
В docker-compose.yml:

используется network_mode: host, чтобы контейнер видел WireGuard-интерфейс хоста

монтируется /etc/wireguard:

yaml
Копировать код
volumes:
  - ./logs:/app/logs
  - /etc/wireguard:/etc/wireguard:rw
подключается .env:

yaml
Копировать код
env_file:
  - .env
Проверка, что контейнер запущен:

bash
Копировать код
docker ps | grep maxnet_vpn_bot
Логи:

bash
Копировать код
docker compose logs -f bot
Автоматический деплой через GitHub Actions
В репозитории есть workflow:

text
Копировать код
.github/workflows/deploy.yml
Он:

Триггерится на push в ветку main

Собирает проект на GitHub Actions

Логинится по SSH на твой сервер

На сервере выполняет команды вида:

cd /home/vpn_service

git pull

docker compose down

docker compose up -d --build

Настройка GitHub Secrets
В настройках репозитория на GitHub нужно добавить:

SSH_HOST — IP или домен сервера

SSH_USER — пользователь (например, root)

SSH_PORT — порт SSH (обычно 22)

SSH_KEY — приватный ключ, который ты сгенерировал для GitHub Actions
(без .pub, содержимое файла типа github_actions_key)

Публичный ключ (github_actions_key.pub) должен быть добавлен на сервер в ~/.ssh/authorized_keys.

Лицензия и пользовательское соглашение
Сервис предназначен для личного использования и предоставления VPN-доступа пользователям.

Пользователь несёт ответственность за использование VPN-подключения.

Администратор не несёт ответственности за действия пользователей в сети.

Использование сервиса предполагает согласие с правилами и законодательством страны пользователя и страны, где расположен сервер.

Точный текст пользовательского соглашения можно вынести в отдельный файл (например, TERMS.md) и добавить команду /terms в бота, которая будет отправлять этот текст.