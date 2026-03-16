# UX-патч: снижение плотности сообщений после выдачи конфига

**Дата:** 2026-03-16  
**Цель:** уменьшить message density сразу после отправки конфига за счёт объединения 3-го и 4-го сообщений в одно. Архитектура, WG lifecycle, IP allocation, CRM и follow-up jobs не менялись.

---

## 1. Сообщения до патча

После `send_vpn_config_to_user()` пользователь получал **4 сообщения подряд**:

1. **document** — файл vpn.conf + caption (REF_TRIAL_CONFIG_CAPTION / DEFAULT_CONFIG_CAPTION)
2. **photo** — QR-код + CONFIG_QR_CAPTION
3. **text** — CONNECTION_INSTRUCTION_SHORT + SUPPORT_AFTER_CONFIG_HINT, кнопки: 🔍 Проверить подключение, 🧑‍💻 Нужна помощь
4. **text** — ONBOARDING_WIREGUARD_QUESTION («📱 У тебя уже установлен WireGuard?»), кнопки: ✅ Да, установлен, ⬇️ Скачать WireGuard

---

## 2. Сообщения после патча

После патча пользователь получает **3 сообщения подряд**:

1. **document** — файл vpn.conf + caption (без изменений)
2. **photo** — QR-код + CONFIG_QR_CAPTION (без изменений)
3. **text** — объединённая инструкция (POST_CONFIG_INSTRUCTION_COMBINED) с одной общей клавиатурой: 🔍 Проверить подключение, 🧑‍💻 Нужна помощь, ✅ Да, установлен, ⬇️ Скачать WireGuard

Отдельное 4-е сообщение с вопросом про WireGuard убрано; его смысл и действия перенесены в объединённый текст и кнопки.

---

## 3. Объединённый текст

Используется константа **POST_CONFIG_INSTRUCTION_COMBINED** в `app/messages.py`:

```
Готово 👌

Я отправил файл конфигурации и QR-код.

Если WireGuard уже установлен:
1. Открой приложение
2. Нажми "+"
3. Импортируй файл vpn.conf или отсканируй QR-код

Если WireGuard ещё не установлен — нажми кнопку ниже.

После подключения нажми:
🔍 Проверить подключение

Если не получится — нажми «🧑‍💻 Нужна помощь».
```

Дублирование с SUPPORT_AFTER_CONFIG_HINT убрано; один короткий блок вместо двух сообщений.

---

## 4. Кнопки под объединённым сообщением

**При наличии подписки (sub_id):**

| Кнопка | callback_data / url |
|--------|----------------------|
| 🔍 Проверить подключение | `config_check_now:{sub_id}` |
| 🧑‍💻 Нужна помощь | `SUPPORT_URL` |
| ✅ Да, установлен | `onboarding:wireguard_confirm:{sub_id}` |
| ⬇️ Скачать WireGuard | `onboarding:wireguard_download` |

Расположение: первая строка — Проверить подключение + Нужна помощь; вторая строка — Да, установлен + Скачать WireGuard.

**При отсутствии подписки:** одна строка «🧑‍💻 Нужна помощь», вторая строка «⬇️ Скачать WireGuard» (Проверить подключение и «Да, установлен» требуют sub_id и не показываются).

callback_data и логика onboarding (wireguard_confirm, wireguard_download) не менялись.

---

## 5. Изменённые файлы

| Файл | Изменения |
|------|-----------|
| `app/messages.py` | Добавлена константа `POST_CONFIG_INSTRUCTION_COMBINED`. Константы `CONNECTION_INSTRUCTION_SHORT`, `SUPPORT_AFTER_CONFIG_HINT`, `ONBOARDING_WIREGUARD_QUESTION` оставлены в модуле (используются в других местах, например в support/actions). |
| `app/bot.py` | В `send_vpn_config_to_user()`: вместо двух отправок (инструкция + вопрос WireGuard) одна отправка с `POST_CONFIG_INSTRUCTION_COMBINED` и объединённой клавиатурой. Удалены импорты `CONNECTION_INSTRUCTION_SHORT`, `SUPPORT_AFTER_CONFIG_HINT`, `ONBOARDING_WIREGUARD_QUESTION`. Обновлён докстринг функции. |

---

## 6. Что не менялось

- Отправка vpn.conf отдельным сообщением (document)
- Отправка QR отдельным сообщением (photo)
- Checkpoint (config_checkpoint_pending, отправка «Удалось подключиться к VPN?» через ~3 мин)
- Handshake flow (первое сообщение, short confirmation, 10m / 2h / 24h / 3d)
- Follow-up jobs, resend logic
- WireGuard lifecycle, IP allocation
- CRM / analytics
- Обработчики callback: `config_check_now`, `onboarding:wireguard_confirm`, `onboarding:wireguard_download` — без изменений

---

Post-config message density reduced.  
Instruction and WireGuard step merged into one message.
