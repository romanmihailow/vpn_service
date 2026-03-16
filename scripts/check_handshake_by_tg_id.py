#!/usr/bin/env python3
"""Проверить подписку и handshake по telegram_user_id."""
import os
import subprocess
import sys
from datetime import datetime

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
    if len(sys.argv) < 2:
        print("Usage: python3 check_handshake_by_tg_id.py <telegram_user_id>")
        return 1
    try:
        tg_id = int(sys.argv[1])
    except ValueError:
        print("telegram_user_id должен быть числом")
        return 1

    with psycopg2.connect(**DB) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT s.id AS sub_id, s.telegram_user_id, s.telegram_user_name,
                       s.wg_public_key, s.vpn_ip, s.active, s.expires_at, s.created_at
                FROM vpn_subscriptions s
                WHERE s.telegram_user_id = %s
                ORDER BY s.id DESC
                LIMIT 10;
            """, (tg_id,))
            subs = [dict(r) for r in cur.fetchall()]

    if not subs:
        print(f"Пользователь telegram_user_id={tg_id} не найден в vpn_subscriptions.")
        return 1

    handshakes = None
    try:
        handshakes = get_handshakes()
    except Exception as e:
        print(f"wg недоступен (handshake не проверен): {e}")
        print()

    now = datetime.utcnow()
    for s in subs:
        pk = (s.get("wg_public_key") or "").strip()
        ts = handshakes.get(pk, 0) if handshakes is not None else None
        if ts is not None:
            ts_str = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S UTC") if ts else "никогда"
            age_sec = (now.timestamp() - ts) if ts else None
            has_hs = "ДА (активная сессия)" if (ts and age_sec is not None and age_sec < 180) else ("ДА (давно)" if ts else "НЕТ")
        else:
            ts_str = "—"
            has_hs = "— (wg не проверен)"

        active = s.get("active")
        expires_at = s.get("expires_at")
        expired = expires_at and (expires_at.timestamp() if hasattr(expires_at, "timestamp") else expires_at) < now.timestamp()
        sub_ok = active and not expired

        print(f"sub_id={s['sub_id']}  telegram_user_id={s['telegram_user_id']}  @{s.get('telegram_user_name') or '-'}")
        print(f"  vpn_ip={s.get('vpn_ip')}  active={active}  expires_at={expires_at}  created_at={s.get('created_at')}")
        print(f"  Подписка активна: {'да' if sub_ok else 'нет'}")
        print(f"  Handshake: {has_hs}  (последний: {ts_str})")
        if pk and handshakes is not None and ts is not None and ts > 0:
            age = (now.timestamp() - ts) if ts else 0
            print(f"  Секунд с последнего handshake: {int(age)} (обычно <180 = активная сессия)")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
