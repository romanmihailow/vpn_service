#!/usr/bin/env python3
"""Проверить пользователя по username: подписка, handshake, последний ConfigResend."""
import os
import subprocess
import sys

def _load_env():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(base, ".env")
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
WG_INTERFACE = os.getenv("WG_INTERFACE_NAME", "wg0")


def get_handshakes():
    out = subprocess.run(
        ["wg", "show", WG_INTERFACE, "latest-handshakes"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"wg failed: {out.stderr}")
    result = {}
    for line in out.stdout.strip().split("\n"):
        if "\t" in line:
            pk, ts = line.split("\t", 1)
            try:
                result[pk.strip()] = int(ts.strip())
            except ValueError:
                pass
    return result


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "Anastasiya_Mrts"
    username_clean = username.replace("@", "").strip()

    with psycopg2.connect(**DB) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT s.id AS sub_id, s.telegram_user_id, s.telegram_user_name,
                       s.wg_public_key, s.vpn_ip, s.active, s.expires_at
                FROM vpn_subscriptions s
                WHERE LOWER(COALESCE(s.telegram_user_name, '')) LIKE %s
                   OR LOWER(s.telegram_user_name) = %s
                ORDER BY s.id DESC
                LIMIT 5;
            """, (f"%{username_clean}%", username_clean))
            subs = [dict(r) for r in cur.fetchall()]

    if not subs:
        print(f"Пользователь @{username_clean} не найден в vpn_subscriptions.")
        return 1

    try:
        handshakes = get_handshakes()
    except Exception as e:
        print(f"Ошибка wg: {e}")
        return 1

    for s in subs:
        pk = (s.get("wg_public_key") or "").strip()
        ts = handshakes.get(pk, 0)
        from datetime import datetime
        ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "никогда"
        has_hs = "ДА" if ts else "НЕТ"
        print(f"sub_id={s['sub_id']} tg_id={s['telegram_user_id']} @{s.get('telegram_user_name') or '-'}")
        print(f"  vpn_ip={s.get('vpn_ip')} active={s['active']} expires={s['expires_at']}")
        print(f"  Handshake: {has_hs} (последний: {ts_str})")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
