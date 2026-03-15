# Onboarding: шаг «У тебя уже установлен WireGuard?»

**Дата:** 2025-03-15  
**Цель:** после выдачи конфига спрашивать, установлен ли WireGuard, и вести пользователя к установке или к импорту конфига.

---

## 1. Изменённые файлы

| Файл | Изменения |
|------|-----------|
| `app/messages.py` | Константы: `ONBOARDING_WIREGUARD_QUESTION`, `ONBOARDING_WG_YES_BUTTON`, `ONBOARDING_WG_DOWNLOAD_BUTTON`, `ONBOARDING_WG_DOWNLOAD_MESSAGE`, `ONBOARDING_WG_CONFIRM_MESSAGE`, `WG_APP_STORE_URL`, `WG_PLAY_MARKET_URL`, `WG_DESKTOP_URL` |
| `app/bot.py` | После сообщения с инструкцией отправляется сообщение «📱 У тебя уже установлен WireGuard?» с кнопками «✅ Да, установлен» и «⬇️ Скачать WireGuard». Лог `[Onboarding] tg_id=... step=wireguard_check`. |
| `app/tg_bot_runner.py` | Импорт новых констант. Обработчики: `onboarding_wireguard_download_callback` (`onboarding:wireguard_download`), `onboarding_wireguard_confirm_callback` (`onboarding:wireguard_confirm:{sub_id}`). Логи `[Onboarding] step=wireguard_download` и `step=wireguard_confirm`. |

---

## 2. Поток после send_vpn_config_to_user()

1. Файл конфига  
2. QR-код  
3. Короткая инструкция + кнопки «🔍 Проверить подключение», «🧑‍💻 Нужна помощь»  
4. **Новое:** сообщение «📱 У тебя уже установлен WireGuard?» с кнопками:
   - **✅ Да, установлен** → `onboarding:wireguard_confirm:{sub_id}`
   - **⬇️ Скачать WireGuard** → `onboarding:wireguard_download`

---

## 3. Поведение кнопок

**⬇️ Скачать WireGuard**  
- Ответ: текст «Скачай приложение WireGuard: 🍏 App Store, 🤖 Play Market, 💻 Windows / Mac».  
- Три inline-кнопки со ссылками: App Store, Play Market, Windows/Mac (официальные URL WireGuard).  
- Лог: `[Onboarding] tg_id=... step=wireguard_download`.

**✅ Да, установлен**  
- Ответ: «Отлично 👍 Теперь сделай одно из двух: 1️⃣ Импортируй файл vpn.conf из сообщения выше или 2️⃣ Сканируй QR-код».  
- Если есть валидный `sub_id` и подписка принадлежит пользователю — под сообщением кнопка **«🔍 Проверить подключение»** (`config_check_now:{sub_id}`), переиспользуется существующий handler.  
- Лог: `[Onboarding] tg_id=... step=wireguard_confirm`.

---

## 4. Логирование

- При отправке вопроса после конфига: `[Onboarding] tg_id=... step=wireguard_check`  
- При нажатии «Скачать WireGuard»: `[Onboarding] tg_id=... step=wireguard_download`  
- При нажатии «Да, установлен»: `[Onboarding] tg_id=... step=wireguard_confirm`  

Отдельная таблица в БД не создаётся.

---

## 5. Что не менялось

- AI-support, checkpoint job, FSM, payments, referral, WireGuard provisioning — без изменений.  
- Добавлен только UX-шаг после инструкции и два callback-обработчика для кнопок.
