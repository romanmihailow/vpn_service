#!/usr/bin/env python3
"""Удаляет пользователя из БД для тестирования с нуля. Аргумент: telegram_user_id."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Загрузка .env
env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
if os.path.isfile(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from app.config import settings
from app import db
from app import wg

TG_ID = 7997651640


def main():
    tg_id = int(sys.argv[1]) if len(sys.argv) > 1 else TG_ID
    print(f"Удаление пользователя {tg_id} из БД...")

    # 1. Подписки — удалить peer из WG, вернуть IP, удалить запись
    sql_subs = "SELECT id, wg_public_key, vpn_ip FROM vpn_subscriptions WHERE telegram_user_id = %s"
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_subs, (tg_id,))
            subs = cur.fetchall()

    for sub_id, pubkey, vpn_ip in subs:
        if pubkey:
            try:
                wg.remove_peer(pubkey.strip())
                print(f"  Peer удалён из WireGuard (sub_id={sub_id})")
            except Exception as e:
                print(f"  WG remove_peer sub_id={sub_id}: {e}")
        if vpn_ip:
            try:
                db.release_ip_in_pool(str(vpn_ip))
                print(f"  IP {vpn_ip} возвращён в пул")
            except Exception as e:
                print(f"  release_ip sub_id={sub_id}: {e}")
        db.delete_subscription_by_id(sub_id)
        print(f"  Подписка {sub_id} удалена")

    # 2. Реферал, баллы, профиль, промо-использование
    tables = [
        ("referrals", "referred_telegram_user_id"),
        ("promo_code_usages", "telegram_user_id"),
        ("user_points_transactions", "telegram_user_id"),
        ("user_points", "telegram_user_id"),
        ("user_profiles", "telegram_user_id"),
    ]
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for table, col in tables:
                cur.execute(f"DELETE FROM {table} WHERE {col} = %s", (tg_id,))
                n = cur.rowcount
                if n:
                    print(f"  {table}: удалено {n} строк")
        conn.commit()

    print("Готово. Пользователь удалён.")


if __name__ == "__main__":
    main()
