#!/usr/bin/env python3
"""Проверка начислений баллов для пользователя @dimamihai."""
import os
import sys

def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env()
import psycopg2
import psycopg2.extras

DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "postgres"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

USERNAME = "dimamihai"

def main():
    with psycopg2.connect(**DB) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Найти tg_id по username (telegram_user_name в подписках)
            cur.execute("""
                SELECT DISTINCT telegram_user_id, telegram_user_name
                FROM vpn_subscriptions
                WHERE LOWER(telegram_user_name) = LOWER(%s)
                   OR telegram_user_name ILIKE %s
                ORDER BY telegram_user_id DESC
                LIMIT 1;
            """, (USERNAME, f"%{USERNAME}%"))
            row = cur.fetchone()
            if not row:
                # Может username без @
                cur.execute("""
                    SELECT DISTINCT telegram_user_id, telegram_user_name
                    FROM vpn_subscriptions
                    WHERE telegram_user_name ILIKE %s;
                """, (f"%{USERNAME}%",))
                row = cur.fetchone()
            if not row:
                print(f"Пользователь @{USERNAME} не найден в vpn_subscriptions")
                sys.exit(1)

            tg_id = row["telegram_user_id"]
            print(f"Найден: telegram_user_id={tg_id} telegram_user_name={row.get('telegram_user_name')}\n")

            # Баланс
            cur.execute("SELECT balance FROM user_points WHERE telegram_user_id = %s", (tg_id,))
            bal = cur.fetchone()
            print(f"Текущий баланс: {bal['balance'] if bal else 0}\n")

            # Все транзакции
            cur.execute("""
                SELECT id, delta, reason, source, related_subscription_id, related_payment_id, level, meta, created_at
                FROM user_points_transactions
                WHERE telegram_user_id = %s
                ORDER BY created_at DESC
                LIMIT 200;
            """, (tg_id,))
            rows = cur.fetchall()

            print(f"Транзакции (последние {len(rows)}):")
            print("-" * 100)
            for r in rows:
                delta_str = f"+{r['delta']}" if r['delta'] > 0 else str(r['delta'])
                meta_str = ""
                if r.get("meta"):
                    meta_str = f" meta={r['meta']}"
                print(f"  {r['created_at']}  {delta_str:>6}  reason={r['reason']:<20} source={r['source']:<15} level={r['level']} {meta_str}")

if __name__ == "__main__":
    main()
