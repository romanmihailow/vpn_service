#!/usr/bin/env python3
"""
Диагностика пользователя по Telegram username.
Ищет подписки, проверяет дубли IP, показывает состояние.

Использование: python3 scripts/check_user_by_username.py Alexander_A_Nik
              python3 scripts/check_user_by_username.py @Alexander_A_Nik
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

env_path = os.path.join(BASE, ".env")
if os.path.isfile(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
os.environ.setdefault("LOG_DIR", "/tmp")

import psycopg2.extras

DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "postgres"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def main():
    if len(sys.argv) < 2:
        print("Использование: python3 scripts/check_user_by_username.py <username>")
        print("Пример: python3 scripts/check_user_by_username.py Alexander_A_Nik")
        sys.exit(1)

    username = sys.argv[1].lstrip("@").strip()
    if not username:
        print("Укажите username (с @ или без)")
        sys.exit(1)

    with psycopg2.connect(**DB) as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Найти пользователей по username (ILIKE — без учёта регистра)
        cur.execute(
            """
            SELECT DISTINCT telegram_user_id, telegram_user_name
            FROM vpn_subscriptions
            WHERE telegram_user_name ILIKE %s
               OR telegram_user_name ILIKE %s
            ORDER BY telegram_user_id;
            """,
            (username, f"%{username}%"),
        )
        users = cur.fetchall()
        if not users:
            print(f"Пользователь с username ~'{username}' не найден.")
            sys.exit(1)

        for u in users:
            tg_id = u["telegram_user_id"]
            uname = u["telegram_user_name"] or "-"
            print(f"\n=== tg_id={tg_id} | @{uname} ===\n")

            cur.execute(
                """
                SELECT id, vpn_ip, active, expires_at, last_event_name, wg_public_key,
                       created_at
                FROM vpn_subscriptions
                WHERE telegram_user_id = %s
                ORDER BY id DESC;
                """,
                (tg_id,),
            )
            subs = cur.fetchall()

            active_subs = [s for s in subs if s["active"]]
            ips_active = [s["vpn_ip"] for s in active_subs if s["vpn_ip"]]
            dup_ips = [ip for ip in ips_active if ips_active.count(ip) > 1]
            dup_ips_unique = list(dict.fromkeys(dup_ips))

            if dup_ips_unique:
                print("⚠️  ДУБЛИ IP среди активных подписок:")
                for ip in dup_ips_unique:
                    cnt = ips_active.count(ip)
                    print(f"   {ip} — используется в {cnt} активных подписках")
                cur.execute(
                    """
                    SELECT id, vpn_ip, active FROM vpn_subscriptions
                    WHERE vpn_ip = ANY(%s) AND active = TRUE
                    ORDER BY vpn_ip, id;
                    """,
                    (dup_ips_unique,),
                )
                for r in cur.fetchall():
                    print(f"      sub_id={r['id']} vpn_ip={r['vpn_ip']}")

            # Другие активные подписки с тем же IP (на других пользователей)
            for ip in ips_active:
                cur.execute(
                    """
                    SELECT id, telegram_user_id, telegram_user_name
                    FROM vpn_subscriptions
                    WHERE vpn_ip = %s AND active = TRUE AND telegram_user_id != %s;
                    """,
                    (ip, tg_id),
                )
                others = cur.fetchall()
                if others:
                    print(f"\n⚠️  IP {ip} также у других пользователей:")
                    for o in others:
                        print(f"   sub_id={o['id']} tg_id={o['telegram_user_id']} @{o['telegram_user_name'] or '-'}")

            print("\nПодписки:")
            for s in subs:
                status = "active" if s["active"] else "inactive"
                exp = s["expires_at"]
                exp_str = exp.strftime("%Y-%m-%d %H:%M") if exp else "-"
                print(f"  sub_id={s['id']} ip={s['vpn_ip']} {status} expires={exp_str}")
                print(f"    last_event={s['last_event_name']} pubkey={str(s['wg_public_key'])[:20]}...")

            if active_subs:
                latest = active_subs[0]
                print(f"\nРекомендации:")
                if dup_ips_unique or others:
                    print(f"  /admin_regenerate_vpn {tg_id}")
                    print("  — перегенерирует ключи и IP, отправит новый конфиг")
                else:
                    print(f"  /admin_resend_config {tg_id}")
                    print("  — переотправит текущий конфиг без изменений")


if __name__ == "__main__":
    main()
