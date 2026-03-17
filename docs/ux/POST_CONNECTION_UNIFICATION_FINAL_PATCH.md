# Финальный патч post-connection messaging после аудита унификации

**Дата:** 2025-03-12  
**Основа:** Аудит `POST_CONNECTION_UNIFICATION_AUDIT.md`  
**Цель:** Сохранить унификацию, вернуть контекст в ключевые сценарии (success, status_ok, 24h) и вернуть referral nudge в success/followup.

---

## 1. Тексты: добавлены/заменены

### Контексты в `app/messages.py`

Используется один общий блок тарифа `POST_VPN_TARIFF_BLOCK` (270 ₽, экономия 30 ₽ ≈10%, /buy) и пять полных сообщений в `POST_VPN_CONNECTED_MESSAGES`:

| Контекст | Назначение |
|----------|------------|
| **initial** | VPN подключён, стабильно, пробный 7 дней → «закрепить уже сейчас» + тариф. |
| **success** | Отлично, рады что всё работает → «если нужен на постоянной основе» + тариф + **реферальная строка**: «Кстати, можно приглашать друзей и получать баллы на оплату VPN.» |
| **status_ok** | **Короткий ответ:** «VPN уже подключён ✅», «Соединение установлено, всё работает.» (без тарифа и без COMMON). |
| **followup** | «Если VPN работает стабильно» → закрепить заранее после тестового периода + тариф. |
| **followup_24h** | «Напоминаем» → «если VPN нужен и дальше» + тариф. |

- **Добавлены:** контексты `status_ok` и `followup_24h`.  
- **Заменены:** старые интро + COMMON на полные тексты с тем же блоком тарифа; скидка везде: **30 ₽ (≈10%)**.  
- **Функция:** `get_post_vpn_message(context: str)` принимает `"initial" | "success" | "status_ok" | "followup" | "followup_24h"`.

---

## 2. Привязка сценариев к контекстам

| Сценарий | Контекст | Файл/место |
|----------|----------|------------|
| Первый handshake (авто-job) | **initial** | `auto_new_handshake_admin_notification`, отправка при `handshake_user_connected` |
| Checkpoint «Да, всё работает» | **success** | `config_check_ok_callback` |
| 10m «Всё работает» | **success** | `vpn_ok_callback` |
| config_check_now при наличии handshake | **status_ok** | `config_check_now_callback` (короткий ответ) |
| Follow-up 2h | **followup** | job `auto_handshake_followup`, тип `handshake_followup_2h` |
| Follow-up 24h | **followup_24h** | тот же job, тип `handshake_followup_24h` |

---

## 3. Клавиатуры

| Контекст | Клавиатура | Кнопки |
|----------|------------|--------|
| **initial** | `HANDSHAKE_USER_CONNECTED_KEYBOARD` | 💎 Закрепить доступ — 270 ₽; 📅 Все тарифы |
| **success** | `_make_post_vpn_success_keyboard(sub_id)` | 💎 Закрепить доступ — 270 ₽; 🤝 Пригласить друга; 📅 Все тарифы |
| **status_ok** | `POST_VPN_STATUS_OK_KEYBOARD` | 💎 Закрепить доступ — 270 ₽ |
| **followup** | `_make_post_vpn_followup_keyboard(sub_id)` | 💎 Закрепить доступ — 270 ₽; 🤝 Пригласить друга; 🧑‍💻 Нужна помощь |
| **followup_24h** | `_make_post_vpn_followup_keyboard(sub_id)` | те же три кнопки |

- **Referral nudge возвращён:** в **success** (строка в тексте + кнопка «Пригласить друга») и в **followup** / **followup_24h** (кнопка «Пригласить друга» + «Нужна помощь»).  
- **status_ok снова короткий:** только две строки текста и одна кнопка «Закрепить доступ», без тарифного блока.  
- Для 24h добавлена та же клавиатура, что и для 2h (раньше у 24h клавиатуры не было).

---

## 4. Что сохранено

- Архитектура post-connection flow не менялась: те же callback_data (`pay:open`, `ref:open_from_notify:{sub_id}`), те же jobs и условия отправки.  
- WG / IP / CRM / логика проверки handshake не трогались.  
- Математика скидки: везде **30 ₽ (≈10%)** в одном блоке `POST_VPN_TARIFF_BLOCK`.  
- Унификация сохранена: один источник текстов и тарифа, контексты только меняют интро и наличие реферальной строки/короткого ответа.

---

## 5. Удалённый код

- В `tg_bot_runner.py` удалена константа `HANDSHAKE_FOLLOWUP_2H_KEYBOARD`; вместо неё используется `_make_post_vpn_followup_keyboard(sub_id)` для 2h и 24h.

---

Final post-connection messaging patch applied.  
Context restored without losing unification.
