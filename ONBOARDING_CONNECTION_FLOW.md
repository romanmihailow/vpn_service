# Onboarding Connection Flow

Пошаговый онбординг после выдачи VPN-конфига: кнопка «Подключить VPN» и сценарии по типу устройства.

---

## Цель

Повысить долю успешных подключений за счёт пошаговой подсказки: выбор устройства → установка WireGuard (на мобильных) → импорт конфига → проверка подключения.

---

## 1. Кнопка после отправки конфига

В `send_vpn_config_to_user()` (app/bot.py) в клавиатуру сообщения с инструкцией добавлена кнопка:

- **Текст:** 🚀 Подключить VPN  
- **callback_data:** `onboarding:start`

Она показывается первой строкой; далее идут «🔍 Проверить подключение» (при наличии подписки) и «🧑‍💻 Нужна помощь».

---

## 2. Сценарий онбординга

### Шаг 1 — выбор устройства

- **Сообщение:** «Какое у тебя устройство?»
- **Кнопки:**
  - 🍏 iPhone → `onboarding:device:iphone`
  - 🤖 Android → `onboarding:device:android`
  - 💻 Компьютер → `onboarding:device:computer`

### Шаг 2 — только iPhone / Android

После выбора iPhone или Android:

- **Сообщение:**  
  «1️⃣ Установи приложение WireGuard  
  2️⃣ Вернись сюда и нажми «Готово»»
- **Кнопка:** ✔ Готово → `onboarding:ready`

Для **Компьютер** шаг 2 пропускается, сразу показывается шаг 3.

### Шаг 3 — импорт конфига

После «Готово» (мобильные) или выбора «Компьютер»:

- **Сообщение:**  
  «Теперь импортируй конфиг:  
  • файл vpn.conf  
  или  
  • QR-код»
- **Кнопка:** 🔍 Проверить подключение → `config_check_now:{subscription_id}`  
  (subscription_id берётся из последней подписки пользователя)

---

## 3. Логирование

В лог пишутся события:

- `[Onboarding] step=start tg_id=...` — нажата «Подключить VPN»
- `[Onboarding] step=device_selected tg_id=... device=onboarding:device:iphone|android` — выбрано устройство (мобильное)
- `[Onboarding] step=ready_for_import tg_id=...` — показан шаг импорта (после «Готово» или выбор «Компьютер»)
- `[Onboarding] step=ready_for_import tg_id=... device=computer` — выбран «Компьютер»

---

## 4. Файлы

| Файл | Изменения |
|------|-----------|
| app/messages.py | Константы: ONBOARDING_START_BUTTON, ONBOARDING_DEVICE_QUESTION, ONBOARDING_DEVICE_IPHONE/ANDROID/COMPUTER, ONBOARDING_INSTALL_MOBILE, ONBOARDING_READY_BUTTON, ONBOARDING_IMPORT_CONFIG |
| app/bot.py | Импорт ONBOARDING_START_BUTTON; в клавиатуру инструкции после конфига добавлена кнопка «🚀 Подключить VPN» (onboarding:start) |
| app/tg_bot_runner.py | Обработчики: onboarding:start (шаг 1), onboarding:device:iphone/android (шаг 2), onboarding:device:computer и onboarding:ready (шаг 3); хелпер _onboarding_step3_keyboard(user_id) |

AI-support, платежи, реферальная система, checkpoint job и выдача конфигов WireGuard не менялись.
