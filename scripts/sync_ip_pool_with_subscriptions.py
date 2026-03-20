#!/usr/bin/env python3
"""
Синхронизация vpn_ip_pool с vpn_subscriptions.
Устанавливает allocated=TRUE для IP, которые уже в активных подписках (рассинхрон).

Запуск: python3 scripts/sync_ip_pool_with_subscriptions.py
"""
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

import psycopg2

DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "postgres"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def main():
    print("=== Синхронизация vpn_ip_pool с активными подписками ===\n")
    with psycopg2.connect(**DB) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE vpn_ip_pool p
                SET allocated = TRUE, allocated_at = COALESCE(allocated_at, NOW())
                WHERE p.allocated = FALSE
                  AND EXISTS (
                    SELECT 1 FROM vpn_subscriptions s
                    WHERE s.vpn_ip::inet = p.ip AND s.active = TRUE
                  )
                RETURNING p.ip;
            """)
            updated = cur.fetchall()
            conn.commit()
    if updated:
        print(f"Обновлено {len(updated)} IP: {[str(r[0]) for r in updated]}")
    else:
        print("Рассинхрона не найдено.")
    print("\nГотово.")


if __name__ == "__main__":
    main()
