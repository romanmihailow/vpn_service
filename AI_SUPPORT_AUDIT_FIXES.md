# AI-Support Audit Fixes

Исправления по результатам production-аудита: строгие паттерны resend_config, новый intent referral_stats, админ-команда /support_stats.

---

## 1. Updated resend_config patterns

**Проблема:** Бот иногда срабатывал на resend_config при фразах вроде «как узнать сколько из моих рефералов оплатили?» и отправлял vpn.conf.

**Решение:** В `app/support/intents.py` заменён список `RESEND_PATTERNS` на строгие фразы с границами слов `\b`:

```python
RESEND_CONFIG_PATTERNS = [
    r"\bвышли конфиг\b",
    r"\bпришли конфиг\b",
    r"\bотправь конфиг\b",
    r"\bповтори конфиг\b",
    r"\bпришли vpn конфиг\b",
    r"\bпришли конфигурацию\b",
    r"\bперешли конфиг\b",
    r"\bконфиг не пришел\b",
    r"\bконфиг не пришёл\b",
    r"\bне пришел конфиг\b",
    r"\bне пришёл конфиг\b",
    r"\bконфиг пожалуйста\b",
]
```

Убраны широкие совпадения (conf, конф, config отдельно). Краткие фразы оставлены только явные: `"вышли конфиг"`, `"отправь конфиг"`, `"пришли конфиг"`, `"конфиг пожалуйста"` (без голого «конфиг»).

---

## 2. New referral_stats intent

**Проблема:** Вопросы вида «сколько у меня баллов?», «сколько бонусных дней?» уходили в fallback.

**Решение:**

- **Паттерны** в `app/support/intents.py` (перед connect_help):

```python
REFERRAL_STATS_PATTERNS = [
    r"сколько баллов",
    r"сколько бонус",
    r"сколько бонусных дней",
    r"сколько у меня бонус",
    r"мой баланс бонус",
]
```

- **Порядок интентов:** … → referral_info → **referral_stats** → connect_help → …

- **Константа** в `app/messages.py`: `REFERRAL_STATS_RESPONSE` (про начисление бонусных дней, отсутствие точной статистики в боте, кнопка «Пригласить друга»).

- **Действие** в `app/support/actions.py`: `action_referral_stats()` возвращает `(REFERRAL_STATS_RESPONSE, InlineKeyboardMarkup` с кнопкой «👥 Пригласить друга», `callback_data="ref:open_from_notify"`).

- **Обработка** в `app/support/service.py`: ветка `elif result.intent == "referral_stats"` вызывает `action_referral_stats()` и возвращает текст и клавиатуру.

---

## 3. action_referral_stats code

```python
def action_referral_stats() -> Tuple[str, InlineKeyboardMarkup]:
    """Ответ на вопросы про баллы/бонусные дни; кнопка «Пригласить друга»."""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пригласить друга", callback_data="ref:open_from_notify")],
        ]
    )
    return REFERRAL_STATS_RESPONSE, kb
```

---

## 4. Example referral_stats reply

Пользователь: *«сколько у меня баллов?»*

Ответ:

```
Бонусные дни начисляются за оплаченные подписки друзей.

Сейчас бот не показывает точную статистику бонусных дней.

Когда приглашённый друг оплачивает подписку, дни VPN автоматически добавляются на твой баланс.

Нажми кнопку ниже, чтобы получить свою реферальную ссылку.
```

+ кнопка «👥 Пригласить друга».

---

## 5. /support_stats command output example

Команда: `/support_stats` (только для админа, `ADMIN_TELEGRAM_ID`).

Пример вывода:

```
AI SUPPORT STATS (last 24h)

By source:
rule: 120
memory: 35
faq_match: 40
openai: 18
fallback: 12

Top intents:
connect_help: 60
vpn_not_working: 55
subscription_status: 20
referral_info: 15
referral_stats: 8

Top VPN diagnoses:
handshake_ok: 23
no_handshake: 22
handshake_stale: 10
```

- **By source:** разбор лога `support_ai.log` за 24 ч (поля `source=`).
- **Top intents:** агрегат по таблице `support_conversations` (поля `detected_intent`) за 24 ч.
- **Top VPN diagnoses:** разбор лога за 24 ч (поля `vpn_diagnosis=`).

Реализация: запрос к БД `get_support_conversation_intent_stats(hours=24)` и парсинг `SUPPORT_AI_LOG_FILE` в `_parse_support_ai_log_for_stats(hours=24)`.

---

## Files changed

| File | Changes |
|------|--------|
| `app/support/intents.py` | RESEND_CONFIG_PATTERNS, REFERRAL_STATS_PATTERNS, порядок referral_stats до connect_help, краткие фразы resend без голого «конфиг». |
| `app/messages.py` | REFERRAL_STATS_RESPONSE. |
| `app/support/actions.py` | action_referral_stats(), импорт REFERRAL_STATS_RESPONSE. |
| `app/support/service.py` | Ветка referral_stats, импорт action_referral_stats. |
| `app/db.py` | get_support_conversation_intent_stats(hours=24). |
| `app/tg_bot_runner.py` | Импорт SUPPORT_AI_LOG_FILE, Tuple; _parse_support_ai_log_for_stats(); cmd_support_stats (Command("support_stats")); запись в ADMIN_INFO_TEXT. |

FSM, платежи, реферальная начисляющая логика, checkpoint job, WireGuard, архитектура AI-support, semantic FAQ match, symptom diagnostics, conversation memory и guardrails не менялись.

---

Audit fixes implemented.
