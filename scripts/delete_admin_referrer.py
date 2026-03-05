#!/usr/bin/env python3
"""Удаляет реферера у админа (ADMIN_TELEGRAM_ID). Запускать один раз."""
import os
import sys

# Добавляем корень проекта в path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

from app.config import settings
from app.db import delete_referrer_for_user


def main():
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0) or 0
    if not admin_id:
        print("ADMIN_TELEGRAM_ID не задан в .env")
        sys.exit(1)

    deleted = delete_referrer_for_user(telegram_user_id=admin_id)
    if deleted:
        print(f"Реферер для админа {admin_id} удалён.")
    else:
        print(f"У админа {admin_id} не было реферера.")


if __name__ == "__main__":
    main()
