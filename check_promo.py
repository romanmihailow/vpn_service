#!/usr/bin/env python3
"""Проверка промокода по базе."""
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
from datetime import datetime, timezone

DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "postgres"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

CODE = sys.argv[1] if len(sys.argv) > 1 else "8SBMFXPCE6"

def main():
    with psycopg2.connect(**DB) as conn:
        with conn.cursor() as cur:
            # Промокод (ищем точное совпадение и UPPER)
            cur.execute(
                """
                SELECT id, code, is_active, valid_from, valid_until, max_uses, used_count,
                       per_user_limit, created_at
                FROM promo_codes
                WHERE UPPER(TRIM(code)) = UPPER(TRIM(%s));
                """,
                (CODE,),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "SELECT id, code FROM promo_codes WHERE code ILIKE %s LIMIT 5;",
                    (f"%{CODE}%",),
                )
                similar = cur.fetchall()
                print(f"Промокод '{CODE}' НЕ НАЙДЕН в базе.")
                if similar:
                    print("Похожие коды:", [r[1] for r in similar])
                return

            (pid, code, is_active, valid_from, valid_until, max_uses, used_count,
             per_user_limit, created_at) = row

            print("=" * 60)
            print("ПРОМОКОД:", code)
            print("=" * 60)
            print("id:", pid)
            print("is_active:", is_active)
            print("valid_from:", valid_from, "(если NULL — действует с начала)")
            print("valid_until:", valid_until, "(если NULL — без срока)")
            print("max_uses:", max_uses, "(если NULL — без лимита)")
            print("used_count:", used_count)
            print("per_user_limit:", per_user_limit)
            print("created_at:", created_at)
            print()

            now = datetime.now(timezone.utc)
            issues = []
            if not is_active:
                issues.append("Код деактивирован (is_active=FALSE)")
            if valid_from and valid_from > now:
                issues.append(f"Код ещё не действует (valid_from={valid_from})")
            if valid_until and valid_until < now:
                issues.append(f"Срок действия истёк (valid_until={valid_until})")
            if max_uses is not None and used_count >= max_uses:
                issues.append(f"Лимит использований исчерпан ({used_count}/{max_uses})")

            if issues:
                print("ПОЧЕМУ КОД МОЖЕТ НЕ РАБОТАТЬ:")
                for i in issues:
                    print("  •", i)
                print()

            # Кто использовал
            cur.execute(
                """
                SELECT u.id, u.telegram_user_id, u.subscription_id, u.created_at,
                       s.vpn_ip, s.expires_at, s.active, s.last_event_name
                FROM promo_code_usages u
                LEFT JOIN vpn_subscriptions s ON s.id = u.subscription_id
                WHERE u.promo_code_id = %s
                ORDER BY u.created_at DESC;
                """,
                (pid,),
            )
            usages = cur.fetchall()
            print("ИСПОЛЬЗОВАНИЯ (кто применил код):")
            if not usages:
                print("  Никто ещё не использовал этот промокод.")
            else:
                for u in usages:
                    uid, tg_id, sub_id, u_created, vpn_ip, exp, active, evt = u
                    print(f"  tg_id={tg_id}  sub_id={sub_id}  создано={u_created}  active={active}  expires={exp}")

if __name__ == "__main__":
    main()
