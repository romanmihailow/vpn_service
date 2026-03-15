# AI-Support: VPN Symptom Diagnostics

Отчёт о добавлении симптомной диагностики в flow «VPN не работает»: классификатор симптомов по формулировке пользователя и уточнённые детерминированные ответы при `handshake_state == "fresh"`.

---

## 1. Changed files

| File | Changes |
|------|--------|
| `app/support/symptoms.py` | **New.** Функция `classify_vpn_symptom(text) -> str`, ключевые фразы по категориям. |
| `app/support/actions.py` | Импорт `classify_vpn_symptom` и констант симптомов. `action_vpn_not_working(context, user_message=None)` возвращает 4-ку `(text, markup, vpn_diagnosis, vpn_symptom)`. При `handshake_state == "fresh"` вызывается классификатор и выбирается ответ по симптому. |
| `app/messages.py` | Константы `VPN_SYMPTOM_SITES_NOT_LOADING`, `VPN_SYMPTOM_SLOW_SPEED`, `VPN_SYMPTOM_MEDIA_PROBLEM`. |
| `app/support/service.py` | Вызов `action_vpn_not_working(context, user_message=text)`, распаковка 4-го значения `vpn_symptom`, инициализация `meta["vpn_symptom"]`, добавление `vpn_symptom` в лог `support_ai`. |
| `app/tg_bot_runner.py` | Вызов `action_vpn_not_working(context)` с распаковкой 4 значений (callback «VPN подключён, но сайты не открываются»). |

---

## 2. New symptom categories

| Symptom | Описание | Ключевые фразы (подстрока в сообщении) |
|--------|----------|----------------------------------------|
| `sites_not_loading` | Сайты/страницы не открываются или не грузятся | сайты не открываются, сайты не грузятся, браузер не открывает, страницы не грузятся, интернет не открывается |
| `slow_speed` | Медленная работа, тормоза | медленно, скорость, тормозит, долго грузит |
| `media_problem` | Видео/медиа не работают | видео не работает, ютуб не грузится, картинки не грузятся, медиа не открывается |
| `generic_problem` | Всё остальное (fallback) | — |

Порядок проверки в `classify_vpn_symptom`: сначала `sites_not_loading`, затем `slow_speed`, затем `media_problem`; при отсутствии совпадения возвращается `generic_problem`.

---

## 3. Updated vpn_not_working flow

Логика в `action_vpn_not_working(context, user_message)`:

**A. Нет активной подписки** → как раньше (no_subscription).

**B. Нет данных конфига** → как раньше (no_config_data).

**C. `handshake_state == "none"`** → как раньше (no_handshake): инструкция открыть WireGuard, добавить туннель, включить.

**D. `handshake_state == "stale"`** → как раньше (handshake_stale): переподключить, перезапустить приложение, кнопка «Проверить подключение».

**E. `handshake_state == "fresh"`** — используется классификатор симптомов по `user_message`:

- `sites_not_loading` → ответ `VPN_SYMPTOM_SITES_NOT_LOADING` (проблема после подключения, возможно DNS/доступ к сайтам; переключить туннель, перезапуск, другая сеть, повторить).
- `slow_speed` → ответ `VPN_SYMPTOM_SLOW_SPEED` (скорость от сети/устройства; переподключить VPN, другая сеть, повторить позже).
- `media_problem` → ответ `VPN_SYMPTOM_MEDIA_PROBLEM` (медиа-сервисы могут быть нестабильны; переподключить, перезапуск, другая сеть).
- `generic_problem` → прежний универсальный ответ (handshake_ok): «VPN-подключение установлено, проблема скорее всего после подключения» + общие шаги.

**F. Неизвестный статус** → как раньше (unknown).

Сетевых проверок (ping, traceroute, DNS, MTU) нет; только классификация по тексту и контексту (подписка, конфиг, handshake_state).

---

## 4. Example reply for sites_not_loading

Пользователь (handshake fresh): *«сайты не грузятся»*  
Ответ:

```
VPN у тебя подключён — значит, проблема, скорее всего, уже после подключения, возможно связано с доступом к сайтам или DNS.

Попробуй:
1. Выключить и снова включить туннель в WireGuard
2. Перезапустить приложение WireGuard
3. Проверить, открываются ли сайты через другую сеть (мобильный интернет)
4. Повторить попытку через минуту

Если не поможет — напиши в поддержку.
```

+ кнопка «🧑‍💻 Нужна помощь».

---

## 5. Example reply for slow_speed

Пользователь (handshake fresh): *«очень медленно грузит»*  
Ответ:

```
VPN подключён. Скорость может зависеть от качества сети или устройства.

Попробуй:
1. Выключить и снова включить VPN
2. Переключиться на другую сеть (Wi‑Fi / мобильный интернет)
3. Повторить попытку позже

Если не поможет — напиши в поддержку.
```

+ кнопка «🧑‍💻 Нужна помощь».

---

## 6. Example log line with vpn_symptom

```
support_ai tg_id=388247897 intent=vpn_not_working conf=0.85 source=rule action=vpn_not_working fallback=False handoff=False resend=False vpn_diagnosis=handshake_ok vpn_symptom=sites_not_loading text="сайты не открываются"
```

При `generic_problem` в логе будет `vpn_symptom=generic_problem`; для веток без симптома (no_handshake, handshake_stale и т.д.) — пустое значение (логируется как пустая строка).

---

## Summary

- Добавлен детерминированный классификатор симптомов в `app/support/symptoms.py`.
- Ответы при «VPN не работает» уточняются по формулировке пользователя только при установленном соединении (`handshake_state == "fresh"`).
- FSM, платежи, реферальная логика, checkpoint, WireGuard, архитектура AI-support, guardrails и список интентов не менялись.
- Новых таблиц в БД нет; симптом пишется только в meta и в лог `support_ai`.

VPN symptom diagnostics implemented.
