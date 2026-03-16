#!/usr/bin/env python3
"""
Пользователи без handshake с 7 марта. Выводит никнеймы с новой строки.
Запуск: python3 scripts/list_no_handshake_since.py
Требует: доступ к БД и WireGuard (wg show wg0 latest-handshakes).
"""
import os
import subprocess

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
    with psycopg2.connect(**DB) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT s.id, s.telegram_user_id, s.telegram_user_name, s.wg_public_key
                FROM vpn_subscriptions s
                WHERE s.active = TRUE
                  AND s.expires_at > NOW()
                  AND s.telegram_user_id IS NOT NULL
                  AND s.wg_public_key IS NOT NULL
                  AND s.created_at >= '2026-03-07'
                ORDER BY s.created_at ASC;
            """)
            subs = [dict(r) for r in cur.fetchall()]

    try:
        handshakes = get_handshakes()
    except Exception as e:
        print(f"Ошибка wg: {e}", file=__import__("sys").stderr)
        return 1

    no_handshake = []
    for sub in subs:
        pk = (sub.get("wg_public_key") or "").strip()
        if pk and handshakes.get(pk, 0) == 0:
            no_handshake.append(sub)

    for sub in no_handshake:
        uname = (sub.get("telegram_user_name") or "").strip()
        if uname:
            print(uname)
        else:
            print(f"id{sub['telegram_user_id']}")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
