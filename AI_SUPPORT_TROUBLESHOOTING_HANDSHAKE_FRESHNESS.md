# Troubleshooting: учёт свежести handshake

**Дата:** 2025-03-15  
**Цель:** уточнить диагностику vpn_not_working по «свежести» handshake: различать отсутствие handshake, устаревший (stale) и свежий (fresh).

---

## 1. Изменённые файлы

| Файл | Изменения |
|------|-----------|
| `app/support/context_builder.py` | Константа `HANDSHAKE_FRESH_SEC = 300`. В контекст добавлены поля `last_handshake_ts`, `handshake_age_sec`, `handshake_state`. Логика: при ts=0 → `handshake_state="none"`; при ts>0 и age≤300 → `"fresh"`; при ts>0 и age>300 → `"stale"`. |
| `app/support/actions.py` | Импорт `CONFIG_CHECK_NOW_BUTTON_TEXT`. Функция `_stale_keyboard(subscription_id)`. В `action_vpn_not_working()` добавлено использование `handshake_state`, новая ветка для `handshake_state == "stale"` с ответом и кнопками «Проверить подключение» + поддержка; диагноз `handshake_stale`. |

---

## 2. Новые поля контекста

| Поле | Тип | Описание |
|------|-----|----------|
| `last_handshake_ts` | int | Unix‑время последнего handshake (0 если нет). |
| `handshake_age_sec` | int \| None | Возраст handshake в секундах (now - ts); None если ts=0. |
| `handshake_state` | str | `"none"` — нет handshake или ts=0; `"fresh"` — ts>0 и age≤300 сек; `"stale"` — ts>0 и age>300 сек. |

Существующие поля `has_handshake`, `has_active_subscription`, `can_resend_config`, `vpn_ip`, `wg_public_key` и остальные не менялись.

---

## 3. Ветки action_vpn_not_working()

| Ветка | Условие | vpn_diagnosis | Поведение |
|-------|---------|----------------|-----------|
| A | Нет активной подписки | `no_subscription` | Без изменений. |
| B | Нет данных для конфига | `no_config_data` | Без изменений. |
| C | `handshake_state == "none"` или `has_handshake is False` | `no_handshake` | Без изменений (туннель не установлен). |
| D | `handshake_state == "stale"` | **`handshake_stale`** | **Новая ветка:** сообщение, что VPN подключался раньше, но сейчас не активен; совет выключить/включить туннель, перезапустить WireGuard, нажать «Проверить подключение»; кнопки «🔍 Проверить подключение» (если есть subscription_id) и «Нужна помощь». |
| E | `handshake_state == "fresh"` или `has_handshake is True` | `handshake_ok` | Без изменений (подключение установлено). |
| F | Иначе | `unknown` | Без изменений. |

---

## 4. Пример ответа для handshake_stale

Текст пользователю:

```
VPN подключался раньше, но сейчас соединение не выглядит активным.

Попробуй:
1. Выключить и снова включить туннель в WireGuard
2. Перезапустить приложение WireGuard
3. Нажать «🔍 Проверить подключение» и проверить снова

Если не поможет — напиши в поддержку.
```

Под сообщением: кнопка «🔍 Проверить подключение» (callback `config_check_now:{sub_id}`) и кнопка «🧑‍💻 Нужна помощь».

В логе support_ai: `vpn_diagnosis=handshake_stale` (как и другие диагнозы, через существующий `meta["vpn_diagnosis"]`).

---

## 5. Подтверждение: остальная логика не менялась

- Intents, guardrails, FSM, payment, referral, checkpoint job, кнопка самопроверки подключения — без изменений.
- Менялись только `context_builder` (добавлены поля и расчёт `handshake_state`) и `action_vpn_not_working` (ветка stale и использование `handshake_state`). Новых таблиц в БД нет.
