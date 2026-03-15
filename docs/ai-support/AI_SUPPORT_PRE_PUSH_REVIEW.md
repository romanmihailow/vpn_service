# Pre-push AI Support Architecture Review

**Дата:** 2025-03-15  
**Объект:** MVP AI-support в проекте MaxNet VPN  
**Цель:** техническое ревью перед push/release без реализации и рефакторинга.

---

## 1. Router and FSM safety

### Где подключён support_router

- **Файл:** `app/tg_bot_runner.py`  
- **Код:** `dp.include_router(router)` затем `dp.include_router(support_router)`.  
- Support router подключён **последним**.

### Может ли перехватывать то, что должно идти в FSM

- В aiogram 3 обработка идёт по порядку регистрации, **первый совпавший handler** выполняется и поиск прекращается.
- В main router все FSM-обработчики завязаны на состояние, например:  
  `@router.message(PromoAdmin.waiting_for_extra_days)`, `@router.message(PromoStates.waiting_for_code)`,  
  `@router.message(Broadcast.waiting_for_text)`, `@router.message(AdminAddSub.waiting_for_target)` и т.д.
- Если пользователь **в FSM state** и шлёт текст — совпадает handler с соответствующим `StateFilter` в main router, support_router не вызывается.
- Если пользователь **не в state** (state = None) — ни один state-фильтр в main router не срабатывает, событие доходит до support_router.

Вывод: при текущем порядке support **не** перехватывает сообщения, которые должны обрабатываться FSM.

### Явная проверка FSM в support

- В **support handler нет проверки FSM state**.
- Безопасность строится **только на порядке** `include_router`: main первый, support последний.
- Риск: при изменении порядка роутеров или добавлении нового router между main и support возможен перехват FSM-сообщений. Защиты на уровне кода support нет.

### Вердикт по разделу 1

- **Router order:** safe при текущем коде (support последний).
- **FSM safety:** **partially safe** — явной проверки state в support нет, всё держится на порядке роутеров.

---

## 2. FSM safety (отдельный вердикт)

- **Явная проверка state в support handler:** нет.
- **Зависимость только от порядка роутеров:** да.
- **Может ли support случайно перехватить текст в FSM:** при текущей структуре — нет; при изменении порядка или добавлении хендлеров без state — да.
- **Надёжность в aiogram при текущей структуре:** достаточная, пока порядок не меняют.

**Вердикт: partially safe.** Рекомендация: при любом изменении подключения роутеров проверять, что support по-прежнему идёт последним; при желании усилить — добавить в support handler явный фильтр «не обрабатывать, если state is not None».

---

## 3. Intent classifier review

### Порядок intents

Порядок соблюдён и логичен: human_request → missing_config_after_payment → resend_config → vpn_not_working → connect_help → subscription_status → handshake_status → smalltalk → unclear. Фраза «оплатил, конфиг не пришёл» обрабатывается как missing_config, а не resend.

### Пересечения и путаница

- **missing_config vs resend:** разведены порядком и разными паттернами; при наличии оплаты и подписки явно выбирается missing_config (conf 0.85/0.9). Пересечение под контролем.
- **vpn_not_working vs connect_help:** разные паттерны; vpn_not_working проверяется раньше. «не работает подключение» (connect_help) и «vpn не работает» (vpn_not_working) не конфликтуют.
- **smalltalk vs unclear:** smalltalk — только точное совпадение с кортежем фраз; «привет как дела» и т.п. уходят в unclear. Ложный smalltalk маловероятен.
- **human_request vs unclear:** human_request первый, паттерны широкие («поддержк», «оператор» и т.д.). Риск: общие фразы вроде «нужна поддержка по деньгам» дадут human_request, а не другой intent — приемлемо для MVP.

### Широта паттернов

- **Широкие:** «поддержк», «оплатил», «не пришел» — могут ловить лишнее (например, «я не оплачивал» даст missing_config), но для первой линии допустимо.
- **HANDSHAKE_PATTERNS:** «vpn работает», «работает ли vpn» — про «работает» в позитивном смысле; vpn_not_working идёт раньше и ловит «vpn не работает». Конфликта нет.

### Непокрытые типы запросов

- Явный запрос статуса оплаты («проверь оплату», «пришла ли оплата») — нет отдельного intent, уйдёт в unclear или в missing_config.
- «Восстановить доступ», «забыл конфиг», «сменить устройство» — частично попадают в resend/connect_help, частично в unclear.
- Оскорбления, оффтоп, длинный поток сознания — unclear.

### Confidence

- Фиксированные значения (0.95, 0.9, 0.85, 0.8, 0.7, 0.2) без калибровки под реальные данные.
- Риск: для части фраз уверенность может быть завышена (например, короткое «оплатил» без контекста всё равно 0.9). Для MVP приемлемо при условии, что guardrails режут низкую уверенность.

**Сильные стороны:** порядок, разделение missing_config/resend, маленький и контролируемый smalltalk.  
**Слабые стороны:** нет intent для «статус оплаты», широкие паттерны без нормализации текста, confidence не калиброван.  
**До push обязательно:** нет; текущего набора достаточно для MVP.  
**Можно отложить:** intent payment_status, донастройка паттернов и confidence по логам.

---

## 4. Guardrails / anti-bullshit

### Реальность ограничений

- **should_handle_directly:** при confidence &lt; 0.8 не вызываются actions, возвращается fallback (уточнение при ≥0.5, safe_fallback при &lt;0.5). Ограничение «бреда» по уверенности есть.
- **human_request:** не отдаётся в actions по смыслу, сразу handoff — корректно.
- **unclear:** при низкой уверенности добавляется handoff и кнопка поддержки.

### Критическая ошибка в guardrails

- **CONF_HIGH = 0.8.** Intent **smalltalk** имеет confidence **0.7**.
- В `should_handle_directly`: при 0.7 не выполняется `confidence >= CONF_HIGH`, выполняется `confidence >= CONF_MED` (0.5) → возвращается `(False, get_clarification_prompt())`.
- В результате для «привет», «кто ты» и т.п. **никогда не вызывается action_smalltalk()** — пользователь получает «Можешь уточнить? Например: …» вместо «Я помощник MaxNet VPN…».
- Аналогично: краткие фразы «подписка», «статус», «до когда» дают subscription_status с confidence **0.7** — они тоже режутся guardrails и не доходят до action.

**Вывод:** guardrails не декоративные, но порог 0.8 **ломает** smalltalk и часть subscription_status (короткие формулировки). Это баг.

### Чего нет

- Нет лимита на подряд идущие fallback/unclear.
- Нет эскалации после 2–3 неудачных ответов подряд.
- Нет ограничения на частый вызов OpenAI при повторяющихся unclear (спам/мусор будут каждый раз слать запрос в API).

**Итог по разделу 4:** защита от галлюцинаций по confidence есть, но **обязательно исправить** порог или список intents, чтобы smalltalk и subscription_status с 0.7 реально обрабатывались. Остальное (счётчики fallback, лимиты OpenAI) можно отложить.

---

## 5. Actions review

| Action | Использует существующую логику | Дублирование | Риск для VPN flows | Безопасность |
|--------|--------------------------------|--------------|--------------------|--------------|
| resend_config | db.get_latest_subscription_for_telegram, wg.build_client_config, send_vpn_config_to_user | Нет | Не создаёт peer/подписку, только resend | OK; есть cooldown 30 сек |
| subscription_status | context из build_user_context (данные из db) | Нет | Нет | OK |
| handshake_status | wg.get_handshake_timestamps(), context | Нет | Нет | OK |
| connect_help | HELP_INSTRUCTION из messages | Нет | Нет | OK |
| human_request | SUPPORT_URL, кнопка | Нет | Нет | OK |
| missing_config_after_payment | context; при do_resend вызывается action_resend_config | Нет | Нет | OK |
| vpn_not_working | context, только чтение | Нет | Нет | OK |
| smalltalk | Статичный текст | Нет | Нет | OK (но не вызывается из-за guardrails) |

**resend_config cooldown:** in-memory dict `RESEND_COOLDOWN`, 30 сек. При рестарте процесса сбрасывается. Для одного инстанса бота достаточно против флуда; при нескольких воркерах каждый будет иметь свой счётчик — возможен суммарный flood. Для типичного single-instance деплоя **достаточно**; «отложить»: перенос cooldown в Redis/БД при масштабировании.

---

## 6. Troubleshooting flow (vpn_not_working)

- **Ветки:** no_subscription → no_config_data → no_handshake → handshake_ok → unknown. Условия по контексту однозначные, дырки в порядке проверок нет.
- **Данные контекста:** has_active_subscription, can_resend_config, has_handshake, vpn_ip, wg_public_key — достаточно для веток.
- **Платформа (iPhone/Android):** не различается; советы универсальные (WireGuard, туннель, перезапуск). Для MVP приемлемо.
- **Ложные выводы:** при недоступности `get_handshake_timestamps()` (exception) в context_builder ставится `has_handshake = False` — пользователь получит ветку «туннель не включён», что может быть неточно. Приемлемый компромисс без усложнения.
- **unknown:** срабатывает при неполных данных (например, has_handshake не True и не False). Текст общий: «Не удалось точно определить… поддержку» — безопасно.

**Вердикт: good MVP.** До push доработка не обязательна; при желании можно позже добавить ветку «не удалось проверить handshake» (например, при ошибке wg).

---

## 7. Context builder

- **Источники:** get_latest_subscription_for_telegram, get_referrer_telegram_id, get_user_points_balance, user_can_claim_referral_trial, wg.get_handshake_timestamps(). Всё read-only, существующие функции.
- **Исключения:** обёрнуты в try/except, при ошибке — None/False/пусто, падений нет.
- **Дорогие операции:** один запрос подписки, один запрос реферера, один баланс, один вызов handshake. На одно сообщение — приемлемо.
- **Полнота для support и диагностики:** подписка, тип, срок, handshake, баллы, реферер, can_resend — достаточно для текущих actions и troubleshooting.
- **Чего нет:** истории диалога, последнего платежа (только last_event_name), платформы. Для MVP не критично.

**Итог:** реализовано хорошо; критичных пробелов для push нет.

---

## 8. OpenAI and knowledge base

### Где используется OpenAI

- Только в **unclear**: при отсутствии совпадения по intents вызывается `_call_openai_for_phrase` (если задан OPENAI_API_KEY). Ответ используется только как **формулировка**; решение (fallback + handoff) не отдаётся модели.

### Без OPENAI_API_KEY

- При unclear возвращаются get_safe_fallback() + get_support_offer() и кнопка поддержки. OpenAI не вызывается, flow не ломается.

### Риск для support flow

- При падении или таймауте API в _call_openai_for_phrase возвращается None, подставляется safe_fallback. Поведение предсказуемое.

### Источник решений

- Решения (intent, действия, resend, handoff) принимаются по правилам и контексту; модель используется только для текста ответа при unclear. Галлюцинации по фактам (оплата, подписка) не внедряются в логику.

### Knowledge base / FAQ

- **Отдельной базы знаний (faq.md, retrieval) нет.** В промпт передаётся только краткий context_summary (подписка, handshake, можно resend). HELP_INSTRUCTION и статические тексты в messages/actions используются как замена FAQ. Архитектурно knowledge base **не реализована**; для MVP ответы строятся на правилах и статичных текстах.

**Вывод по OpenAI:** интеграция **meaningful** в узком месте (формулировка при unclear); без ключа всё работает. **Knowledge base:** не реализована; для push не блокер, можно отложить.

---

## 9. Logging and analytics

- **support_ai.log:** логгер `support_ai` пишет в `LOG_DIR/support_ai.log`. Файл создаётся при первой записи. В коде каждая обработка сообщения логируется в `log.info(...)`.
- **support_conversations:** таблица создаётся в `init_db()` в том же `create_table_sql`, что и остальные таблицы. При старте `tg_bot_runner` вызывается `db.init_db()` — таблица будет создана в том же окружении, где поднимается бот.
- **Поля в логе:** tg_id, intent, conf, action, fallback, handoff, resend, vpn_diagnosis — достаточно для аналитики и доработки intents.
- **Почему в прошлом отчёте лог был пуст, а таблицы не было:**
  - **support_ai.log пуст:** в том окружении либо не было трафика в support (пользователи не писали свободный текст), либо бот не был запущен с этим кодом, либо LOG_DIR указывал в другое место/не было прав на запись. Это **не ошибка кода** — код пишет при каждой обработке.
  - **support_conversations отсутствовала:** в БД, к которой подключался скрипт анализа, не выполнялся `init_db()` после добавления таблицы (другая БД, тестовый стенд без миграций). В проде при запуске бота `init_db()` создаёт таблицу. Это **окружение/миграции**, а не отсутствие создания таблицы в коде.

**Итог:** логирование и таблица реализованы корректно; пустой лог и отсутствие таблицы в прошлом отчёте объясняются отсутствием трафика и/или другим окружением БД, а не багом в коде.

---

## 10. Production readiness (оценки 1–10)

| Категория | Оценка | Комментарий |
|-----------|--------|-------------|
| Router safety | 8 | Порядок правильный; явной проверки FSM нет. |
| FSM safety | 7 | Зависит от порядка; при рефакторинге легко сломать. |
| Intent quality | 7 | Порядок и набор ок; есть широкие паттерны и некалиброванный confidence. |
| Guardrails | 4 | Порог 0.8 блокирует smalltalk и часть subscription_status — баг. |
| Actions | 8 | Переиспользуют логику, без дублирования и риска для VPN. |
| Troubleshooting | 8 | Ветки понятные, без логических дыр. |
| Logging | 8 | Нужные поля пишутся в лог и БД. |
| OpenAI integration | 6 | Используется только при unclear для формулировки; без ключа ок. |
| Knowledge base | 2 | Нет; только статика и контекст. |
| Overall architecture | 7 | Модуль изолирован, порядок роутеров и guardrails требуют правки. |

---

## 11. Implemented vs missing

### Реализовано

- Support router как fallback для текста (не команды).
- Подключение support_router последним после main router.
- Фильтр «не команда» по началу строки с `/`.
- Rule-based intent classifier с фиксированным порядком и 9 intents.
- Guardrails по confidence (с порогами 0.8 / 0.5 / 0.3).
- Actions: resend_config (с cooldown), subscription_status, handshake_status, connect_help, human_request, missing_config_after_payment, vpn_not_working (5 веток), smalltalk.
- Переиспользование send_vpn_config_to_user, wg.build_client_config, get_latest_subscription_for_telegram и др.
- Логирование в support_ai.log и в таблицу support_conversations.
- Создание таблицы support_conversations в init_db().
- Опциональная интеграция OpenAI только для формулировки при unclear.
- Документация в docs/ai-support.

### Не реализовано

- Явная проверка FSM state в support handler.
- Knowledge base / FAQ (файл, retrieval, передача в промпт).
- Диагностика оплаты (отдельный intent/action по платежам).
- Многошаговый troubleshooting (state machine диалога).
- Режим «передача человеку» (takeover) в интерфейсе.
- Счётчики эскалации (N fallback подряд → принудительный handoff).
- Ограничение частоты вызовов OpenAI (rate limit по пользователю/сессии).
- Cooldown resend в Redis/БД для multi-instance.
- Различие платформ (iOS/Android) в troubleshooting.
- Калибровка confidence по логам.

---

## 12. Must-fix before push

- **[SHOULD FIX BEFORE PUSH] Guardrails блокируют smalltalk и часть subscription_status.**  
  Intent smalltalk (и краткие «подписка»/«статус»/«до когда») имеют confidence 0.7, порог обработки 0.8 — эти intents не доходят до actions, пользователь получает уточняющий вопрос вместо ответа. Варианты: понизить CONF_HIGH до 0.65, либо в should_handle_directly явно разрешать обработку для intent in (smalltalk, subscription_status) при confidence >= 0.7.

- **[CAN WAIT] Явная проверка FSM в support.**  
  Добавить фильтр «не обрабатывать, если FSM state is not None» для устойчивости к изменению порядка роутеров. Не блокирует push при текущем порядке.

- **[CAN WAIT] Лимиты на fallback и вызовы OpenAI.**  
  Нет защиты от множественных подряд unclear и от спама в API. Можно ввести после накопления трафика.

---

## 13. Final verdict

**SAFE TO PUSH WITH MINOR FIXES**

Почему не «SAFE TO PUSH» без оговорок: в guardrails порог 0.8 приводит к тому, что на «привет» и «кто ты» бот отвечает уточняющим вопросом вместо ответа smalltalk, а на «подписка»/«статус»/«до когда» — тем же уточнением вместо реального статуса подписки. Это заметный UX-дефект и легко исправляется (одно изменение порога или списка intents в guardrails).

Почему не «SHOULD FIX SEVERAL ISSUES»: архитектура модуля, порядок роутеров, использование существующей логики, отсутствие вмешательства в платежи/рефералы/FSM, логирование и таблица — в порядке. Риски в основном связаны с одним багом в guardrails и с отсутствием явной FSM-проверки (смягчается текущим порядком роутеров).

**Рекомендация:** исправить обработку smalltalk и subscription_status с confidence 0.7 (одно изменение в guardrails или порогах), затем пушить. Остальное (явный FSM-фильтр, лимиты, knowledge base) можно отложить на следующие итерации.
