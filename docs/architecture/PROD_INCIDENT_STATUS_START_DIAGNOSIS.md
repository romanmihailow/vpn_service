# Production Incident: /start без кнопок, /status fallback

**Дата:** 2026-03-16  
**Симптомы:** /start отвечает текстом без кнопок; /status — fallback «Не удалось загрузить статус подписки…»; после short confirmation и стабилизационных патчей бот работает некорректно.

---

## 1. Где падает /start (get_start_keyboard)

- **Цепочка:** `cmd_start` → `get_start_keyboard(user_id)` → **db.user_can_claim_referral_trial(telegram_user_id)**.
- **user_can_claim_referral_trial** делает подряд **3 вызова БД** (каждый со своим `get_conn()`):
  1. `get_referrer_telegram_id(telegram_user_id)`
  2. `has_referral_trial_subscription(telegram_user_id)`
  3. `get_latest_subscription_for_telegram(telegram_user_id)`
- При **PoolError** (connection pool exhausted) исключение летит из `get_start_keyboard()` → срабатывает `except` в `cmd_start` → пользователю уходит **текст без кнопок** (fallback без reply_markup).
- **Вывод:** падение именно в **получении соединения из пула** при вызове БД внутри `get_start_keyboard`, а не в «сборке» клавиатуры.

---

## 2. Где падает /status

- **Цепочка:** `cmd_status` → **db.get_latest_subscription_for_telegram(user_id)** → при наличии подписки → `get_status_keyboard(sub_id)` (без БД) → `message.answer(...)`.
- Fallback «Не удалось загрузить статус подписки» означает срабатывание **except** в `cmd_status`.
- Падать может:
  - **get_latest_subscription_for_telegram** — при `get_conn()` (PoolError);
  - реже — **message.answer** (сеть/Telegram).
- **get_status_keyboard** БД не использует, только строит InlineKeyboardMarkup по `sub_id`.
- **Вывод:** в проде падение почти наверняка в **get_latest_subscription_for_telegram** из‑за нехватки соединения в пуле.

---

## 3. Логи: PoolError / connection pool exhausted

По логам **logs/vpn_service.log** (2026-03-16):

- Массово: **PoolError('connection pool exhausted')** в фоновых джобах и в пользовательских хендлерах.
- Примеры:
  - `[Start] Failed to send start reply tg_id=388247897: PoolError('connection pool exhausted')`
  - `[Status] Failed tg_id=388247897: PoolError('connection pool exhausted')`
  - `[Referral] Failed to get referral info for tg_id=388247897: PoolError('connection pool exhausted')`
- Джобы с той же ошибкой: **NewHandshakeAdmin**, **HandshakeFollowup**, **ConfigCheckpoint**, **AutoExpire**, **HandshakeShortConfirm**, **RecentExpiredTrialFollowup**, **AutoNotify**, **WelcomeFirstPayment**, **NoHandshakeRemind**.

**Вывод:** корневая причина — исчерпание **connection pool**, а не отдельный баг в логике /start или /status.

---

## 4. Short confirmation и нагрузка на пул

- **auto_handshake_short_confirmation** уже ограничена: **batch_size=10**, **max_age_seconds=900**, интервал 60 с, sleep(1) между отправками.
- В логах она падает с PoolError **вместе с остальными джобами** — не как единственный виновник, а как один из потребителей пула.
- Дополнительная нагрузка от неё — не больше, чем у других джобов; проблема в **общем количестве джобов и размере пула**, а не только в short confirmation.

**Вывод:** short confirmation не создаёт «лишней» нагрузки сверх уже введённых лимитов; отключать её имеет смысл только как **временную разгрузку** при неизменном размере пула.

---

## 5. Какие jobs работают одновременно и конкурируют за пул

Все задачи создаются в **main()** и крутятся параллельно:

| Job | Интервал (примерно) | Держит соединение |
|-----|----------------------|--------------------|
| auto_deactivate_expired_subscriptions | 60 с | acquire_job_lock → 1 conn на время прогона |
| auto_notify_expiring_subscriptions | 600 с | 1 conn |
| auto_revoke_unused_promo_points | 86400 с | 1 conn |
| auto_new_handshake_admin_notification | 120 с | 1 conn |
| auto_handshake_followup_notifications | 120 с | 1 conn |
| **auto_handshake_short_confirmation** | 60 с | 1 conn |
| auto_welcome_after_first_payment | 600 с | 1 conn |
| auto_no_handshake_reminder | 3600 с | 1 conn |
| auto_config_checkpoint | 60 с | 1 conn |
| auto_recently_expired_trial_followup | 60 с | 1 conn |

- **DB_POOL_MAX = 10** (по умолчанию в config).
- В пике **до 9–10 джобов** могут одновременно держать по соединению (при совпадении прогонов по времени). На **запросы пользователей** (start, status, ref и т.д.) соединений уже не остаётся → **PoolError** в хендлерах.
- **Вывод:** пул в 10 соединений недостаточен при текущем наборе фоновых задач и трафике.

---

## 6. Что безопаснее: отключить short confirmation, поднять пул, откатить один job

| Вариант | Эффект | Риск |
|--------|--------|------|
| **Временно отключить short confirmation** | Минус один постоянный потребитель пула при прогоне; в моменты, когда она не крутится, пул чуть свободнее. | Низкий: потеря только одного follow-up сообщения; логика остальных джобов не трогается. |
| **Увеличить DB_POOL_MAX** | Больше соединений — джобы и пользовательские запросы перестают упираться в лимит. | Низкий при разумном значении (20–25): БД обычно держит десятки соединений. |
| **Откатить только один конкретный job** | Аналогично «отключить» — меньше конкуренции за пул. | Зависит от того, какой job откатывать; откат кода без увеличения пула проблему не решает. |

**Рекомендация:** самый предсказуемый и безопасный шаг — **увеличить DB_POOL_MAX** (например до 20). При необходимости можно **дополнительно** временно отключить short confirmation, пока пул не увеличен или пока не внедрены дополнительные batch limits. Откат кода не нужен — логика handlers и jobs корректна, сбой из‑за размера пула.

---

## Root cause hypothesis

**Гипотеза:** Исчерпание **connection pool** (DB_POOL_MAX=10) при одновременной работе **9 фоновых джобов** и запросах пользователей. Каждая джоба при прогоне занимает минимум одно соединение (job lock); пользовательские команды (/start, /status, /ref) тоже запрашивают соединения. При 10 соединениях в пике все они заняты джобами → **PoolError** в хендлерах → /start отдаёт текст без кнопок (fallback), /status — fallback «Не удалось загрузить статус подписки».

---

## Safest hotfix right now

1. **Увеличить DB_POOL_MAX** до **20** (в config по умолчанию и/или в .env на проде).
2. Перезапустить приложение, чтобы новый размер пула применился.
3. При желании **временно** не запускать **auto_handshake_short_confirmation** (закомментировать `asyncio.create_task(auto_handshake_short_confirmation(bot))` в main или выйти из функции в начале), чтобы снизить конкуренцию за пул до применения нового лимита.

---

## Нужен ли rollback

**Нет.** Откат коммитов не требуется: поведение /start и /status соответствует коду (fallback при ошибке), причина сбоя — недостаточный размер пула и конкуренция джобов, а не регрессия после short confirmation или стабилизационных патчей.

---

## Первые 3 действия

1. **Поднять пул БД:** в проде задать **DB_POOL_MAX=20** (через .env или переменные окружения) и перезапустить воркер бота (или весь сервис), чтобы поднялся новый пул с maxconn=20.
2. **Проверить config по умолчанию:** в коде (app/config.py) при необходимости сменить значение по умолчанию с 10 на 20, чтобы без .env пул тоже был 20: `DB_POOL_MAX: int = int(os.getenv("DB_POOL_MAX", "20"))`.
3. **Опционально — временно отключить short confirmation:** в `main()` закомментировать строку `asyncio.create_task(auto_handshake_short_confirmation(bot))` (или в начале `auto_handshake_short_confirmation` сразу делать `return`), задеплоить, затем после стабилизации и при необходимости снова включить задачу.

После выполнения п.1–2 и рестарта логи должны перестать показывать PoolError в /start и /status; кнопки и нормальный ответ /status вернутся без отката кода.
