#!/usr/bin/env python3
"""Проверка: оплатил ли кто-то из 154 (broadcast_155.txt) подписку баллами."""
import os

def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
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

LIST_PATH = os.path.join(os.path.dirname(__file__), "broadcast_155.txt")

def main():
    with open(LIST_PATH) as f:
        ids_154 = [int(line.strip()) for line in f if line.strip()]

    sql = """
    SELECT telegram_user_id, id AS sub_id, last_event_name, created_at, expires_at, active
    FROM vpn_subscriptions
    WHERE telegram_user_id = ANY(%s)
      AND (
        last_event_name LIKE 'points_payment_%%'
        OR last_event_name LIKE 'points_extend_%%'
      )
    ORDER BY telegram_user_id, created_at DESC;
    """
    with psycopg2.connect(**DB) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (ids_154,))
            rows = cur.fetchall()

    if not rows:
        print("Из 154 пользователей никто не оплачивал подписку баллами (нет записей с points_payment_* / points_extend_*).")
        return

    print(f"Найдено пользователей из списка 154, оплативших подписку баллами: {len(set(r[0] for r in rows))}")
    print(f"Всего записей подписок по баллам: {len(rows)}\n")
    for r in rows:
        tg_id, sub_id, event, created, expires, active = r
        print(f"  tg_id={tg_id}  sub_id={sub_id}  event={event}  created={created}  active={active}")

if __name__ == "__main__":
    main()
