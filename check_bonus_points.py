#!/usr/bin/env python3
"""Проверка: всем ли 154 из broadcast_155.txt начислены баллы по кампании never_connected_100."""
import os
import sys

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
        expected_ids = set(int(line.strip()) for line in f if line.strip())

    sql = """
    SELECT DISTINCT telegram_user_id
    FROM user_points_transactions
    WHERE reason = 'promo'
      AND source = 'admin'
      AND meta->>'campaign' = 'never_connected_100'
      AND delta = 100;
    """
    with psycopg2.connect(**DB) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            credited = set(row[0] for row in cur.fetchall())

    in_both = expected_ids & credited
    only_list = expected_ids - credited
    only_db = credited - expected_ids

    print(f"В списке broadcast_155.txt: {len(expected_ids)}")
    print(f"В БД начислено по never_connected_100 (+100): {len(credited)}")
    print(f"Пересечение (и в списке, и начислено): {len(in_both)}")
    if only_list:
        print(f"\nВ списке, но НЕТ начисления ({len(only_list)}):")
        for uid in sorted(only_list):
            print(f"  {uid}")
    if only_db:
        print(f"\nНачислено, но НЕТ в списке ({len(only_db)}):")
        for uid in sorted(only_db)[:20]:
            print(f"  {uid}")
        if len(only_db) > 20:
            print(f"  ... и ещё {len(only_db) - 20}")
    if not only_list and len(in_both) == len(expected_ids):
        print("\nOK: всем 154 начислены баллы.")
    else:
        print(f"\nПроверка не пройдена: без начисления {len(only_list)} пользователей.")

if __name__ == "__main__":
    main()
