#!/usr/bin/env python3
"""
Устранение дублей IP и применение UNIQUE constraint.

Действия:
1. Деактивирует подписки 513 (@rmw_ok) и 380 (tg 646642544)
2. Выдаёт новые IP подпискам 391 и 519 (сейчас оба на 10.8.0.70)
3. Создаёт UNIQUE constraint на (vpn_ip) для активных подписок

Запуск: python3 scripts/fix_duplicate_ips.py
Требования: доступ к БД, WireGuard (wg), запуск на сервере.
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
os.environ.setdefault("LOG_DIR", "/tmp")
import psycopg2
import psycopg2.extras

# Используем app.wg для корректного обновления wg0.conf
from app import wg as app_wg
from app.config import settings

DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "postgres"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}
WG_CIDR = getattr(settings, "WG_CLIENT_NETWORK_CIDR", 24)

# sub_id -> действие: "deactivate" | "new_ip"
ACTIONS = {
    513: "deactivate",  # rmw_ok
    380: "deactivate",  # мама 646642544
    391: "new_ip",
    519: "new_ip",
}


def wg_remove_peer(public_key: str) -> None:
    app_wg.remove_peer(public_key)


def wg_add_peer(public_key: str, allowed_ip: str, telegram_user_id: int) -> None:
    app_wg.add_peer(public_key, allowed_ip, telegram_user_id)


def main():
    print("=== Fix duplicate IPs ===\n")

    with psycopg2.connect(**DB) as conn:
        conn.autocommit = False
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        try:
            # 1. Деактивация 513 и 380
            for sub_id in (513, 380):
                cur.execute(
                    "SELECT id, telegram_user_id, wg_public_key, vpn_ip FROM vpn_subscriptions WHERE id = %s AND active = TRUE",
                    (sub_id,),
                )
                row = cur.fetchone()
                if not row:
                    print(f"  sub {sub_id}: уже неактивна или не найдена, пропуск")
                    continue
                pub = (row["wg_public_key"] or "").strip()
                vpn_ip = row["vpn_ip"]
                cur.execute(
                    "UPDATE vpn_subscriptions SET active = FALSE, last_event_name = 'admin_fix_duplicate_ip' WHERE id = %s",
                    (sub_id,),
                )
                # Освобождать IP только если нет других активных подписок с этим IP
                cur.execute(
                    "SELECT COUNT(*) FROM vpn_subscriptions WHERE vpn_ip = %s AND active = TRUE",
                    (vpn_ip,),
                )
                others = (cur.fetchone() or (0,))[0]
                if others == 0:
                    cur.execute(
                        "UPDATE vpn_ip_pool SET allocated = FALSE, allocated_at = NULL WHERE ip = %s::inet",
                        (vpn_ip,),
                    )
                    print(f"  sub {sub_id} tg={row['telegram_user_id']}: деактивация, release IP {vpn_ip}")
                else:
                    print(f"  sub {sub_id} tg={row['telegram_user_id']}: деактивация, IP {vpn_ip} не release ({others} др. активных)")
                if pub:
                    try:
                        wg_remove_peer(pub)
                        print(f"    peer удалён из WG")
                    except Exception as e:
                        print(f"    wg remove: {e}")

            conn.commit()
            print("  Деактивация 513, 380 — OK\n")

            # 2. Новые IP для 391 и 519
            for sub_id in (391, 519):
                cur.execute(
                    "SELECT id, telegram_user_id, wg_public_key, wg_private_key, vpn_ip FROM vpn_subscriptions WHERE id = %s AND active = TRUE",
                    (sub_id,),
                )
                row = cur.fetchone()
                if not row:
                    print(f"  sub {sub_id}: не найдена или неактивна, пропуск")
                    continue

                old_ip = row["vpn_ip"]
                pub = (row["wg_public_key"] or "").strip()
                priv = row["wg_private_key"]

                # allocate new IP
                cur.execute(
                    """
                    SELECT ip FROM vpn_ip_pool
                    WHERE allocated = FALSE
                    ORDER BY ip LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                )
                ip_row = cur.fetchone()
                if not ip_row:
                    raise RuntimeError("Нет свободных IP в пуле")
                new_ip = str(ip_row[0])
                cur.execute(
                    "UPDATE vpn_ip_pool SET allocated = TRUE, allocated_at = NOW() WHERE ip = %s::inet",
                    (new_ip,),
                )
                conn.commit()

                print(f"  sub {sub_id} tg={row['telegram_user_id']}: {old_ip} -> {new_ip}")

                if pub:
                    try:
                        wg_remove_peer(pub)
                    except Exception:
                        pass
                allowed = f"{new_ip}/{WG_CIDR}"
                wg_add_peer(pub, allowed, row["telegram_user_id"])
                cur.execute(
                    "UPDATE vpn_subscriptions SET vpn_ip = %s, last_event_name = 'admin_fix_duplicate_ip' WHERE id = %s",
                    (new_ip, sub_id),
                )
                conn.commit()
                print(f"    БД и WG обновлены. Конфиг: /admin_resend_config {row['telegram_user_id']}")

            print("\n  391, 519 — новые IP выданы\n")

            # 3. UNIQUE constraint (без NOW() — иначе "functions must be IMMUTABLE")
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_vpn_subscriptions_active_ip
                ON vpn_subscriptions (vpn_ip)
                WHERE active = TRUE
            """)
            conn.commit()
            print("  UNIQUE constraint idx_vpn_subscriptions_active_ip — создан\n")

        except Exception as e:
            conn.rollback()
            print(f"Ошибка: {e}")
            raise

    print("=== Готово ===")
    print("\nДальше: переотправь конфиги пользователям 6505943791 и 2083494596:")
    print("  /admin_resend_config 6505943791")
    print("  /admin_resend_config 2083494596")


if __name__ == "__main__":
    main()
