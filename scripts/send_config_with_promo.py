#!/usr/bin/env python3
"""
Отправка конфига с кастомным caption (объяснение + промокод) конкретным пользователям.

Пример: после fix_duplicate_ips.py — отправить обновлённый конфиг 391 и 519 с промокодами.

Запуск:
  python3 scripts/send_config_with_promo.py

Редактируй список USERS ниже перед запуском.
"""
import asyncio
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


def _load_env():
    env_path = os.path.join(BASE, ".env")
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
os.environ.setdefault("LOG_DIR", "/tmp")

from app import db, wg
from app.bot import send_vpn_config_to_user

# tg_id -> промокод
USERS = {
    6505943791: "H23CG37SLR",
    2083494596: "TAHHSXD43K",
}

CAPTION_TEMPLATE = """Здравствуйте! Устранили техническую неоптимальность, из‑за которой могли быть сбои подключения. Установите новый конфиг вместо старого.

Промокод в подарок: {promo_code}"""


async def main():
    for tg_id, promo_code in USERS.items():
        sub = db.get_latest_subscription_for_telegram(telegram_user_id=tg_id)
        if not sub:
            print(f"tg_id {tg_id}: нет активной подписки, пропуск")
            continue
        vpn_ip = sub.get("vpn_ip")
        private_key = sub.get("wg_private_key")
        if not vpn_ip or not private_key:
            print(f"tg_id {tg_id}: нет vpn_ip или ключей, пропуск")
            continue

        config_text = wg.build_client_config(
            client_private_key=private_key,
            client_ip=vpn_ip,
        )
        caption = CAPTION_TEMPLATE.format(promo_code=promo_code)

        try:
            await send_vpn_config_to_user(
                telegram_user_id=tg_id,
                config_text=config_text,
                caption=caption,
            )
            print(f"tg_id {tg_id}: конфиг отправлен, промокод {promo_code}")
        except Exception as e:
            print(f"tg_id {tg_id}: ошибка {e}")


if __name__ == "__main__":
    asyncio.run(main())
