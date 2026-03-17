# Удаление anti-DPI прототипа с репозитория / сервера Латвии (Литвы)

**Дата:** 2025-03-12  
**Цель:** Убрать неиспользуемый anti-DPI прототип (Xray/REALITY) из репозитория, не затрагивая основной VPN-сервис (WireGuard + bot + nginx).

---

## 1. Что было удалено

### Каталог `antidpi/` (полностью)

| Удалённый объект | Назначение |
|------------------|------------|
| `antidpi/config.json.template` | Шаблон конфига Xray VLESS+REALITY (порт 443) |
| `antidpi/generate-config.sh` | Скрипт генерации UUID, x25519, shortId и config.json |
| `antidpi/docker-compose.yml` | Запуск контейнера teddysun/xray на 443/tcp, 443/udp |
| `antidpi/config.json` | Генерируемый конфиг (в .gitignore; при наличии на диске — удалён) |
| Каталог `antidpi/` | Удалён после удаления файлов |

### Каталог `docs/antidpi/` (полностью)

| Удалённый объект | Назначение |
|------------------|------------|
| `docs/antidpi/PHASE1_XRAY_SETUP.md` | Инструкция по ручному тесту Phase 1 (generate-config, docker-compose, v2rayNG/Shadowrocket) |
| `docs/antidpi/PHASE1_XRAY_TECH_AUDIT.md` | Технический аудит прототипа (конфиг, скрипт, docker-compose, риски) |
| `docs/antidpi/LITHUANIA_ANTIDPI_AUDIT.md` | Полный аудит anti-DPI в репозитории и на сервере Литвы |
| Каталог `docs/antidpi/` | Удалён после удаления файлов |

### Прочее

| Изменение | Причина |
|-----------|---------|
| Строка `antidpi/config.json` в `.gitignore` | Удалена: каталог `antidpi/` больше не существует, правило не нужно |

---

## 2. Что оставлено

| Объект | Причина |
|--------|---------|
| `docs/architecture/BLOCKING_BYPASS_MODE_ARCHITECTURE.md` | Архитектурный план режима «Обход блокировок»: модель данных, UX, Phase 1–5, метрики; пригоден для будущего R&D на USA VPS |
| `app/` | Не изменялся |
| Корневой `docker-compose.yml` | Не изменялся (сервис bot, WireGuard) |
| Конфиги WireGuard, nginx, .env, платежи, CRM, UX, БД | Не изменялись |

Документов в `docs/antidpi/` с отдельным решением «оставить для USA R&D» не было: все три файла относились к прототипу и аудиту на Латвии/Литве и удалены.

---

## 3. Почему это безопасно

- **Нет зависимостей в коде:** В `app/` и остальном коде репозитория нет импортов, вызовов или ссылок на `antidpi/` или `docs/antidpi/`. Проверка по шаблонам `antidpi`, `antidpi/`, `docs/antidpi/` в `*.py`, `*.yml`, `*.yaml`, `*.sh` ничего не нашла.
- **Прототип не использовался в проде:** На сервере Латвии/Литвы порт 443 занят nginx; Xray/REALITY из `antidpi/` не интегрированы в бота, БД, платежи, CRM и не запускались как сервис.
- **Основной сервис не затронут:** Удалены только каталоги прототипа и связанная с ним узкая документация; бот, WireGuard, docker-compose основного проекта, nginx и всё production-окружение не менялись.

---

## 4. Перенос anti-DPI R&D на USA VPS

Эксперименты с anti-DPI (Xray/VLESS/REALITY и при необходимости Hysteria) будут продолжаться отдельно на USA VPS. Текущий репозиторий и сервер Латвии/Литвы используются только для основного VPN (WireGuard + bot). Для будущей реализации режима «Обход блокировок» остаётся ориентир в виде `docs/architecture/BLOCKING_BYPASS_MODE_ARCHITECTURE.md`.

---

## 5. Основной VPN-сервис не затронут

- Не изменялись: `app/`, основной `docker-compose`, WireGuard, nginx, платежи, CRM, UX, БД, CI/CD и прочие production-компоненты.
- После удаления проверено: в коде нет битых импортов или путей к `antidpi/` или `docs/antidpi/`; архитектурный документ `BLOCKING_BYPASS_MODE_ARCHITECTURE.md` на месте; репозиторий остаётся консистентным.

---

Anti-DPI prototype cleaned from Latvia/Lithuania repo.  
Main VPN service remains untouched.  
Future anti-DPI experiments will continue on USA VPS.
