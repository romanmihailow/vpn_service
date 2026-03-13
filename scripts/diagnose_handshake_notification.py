#!/usr/bin/env python3
"""
Диагностика уведомлений о handshake (new_handshake_admin).
Запуск: python3 scripts/diagnose_handshake_notification.py

Проверяет:
1. Сколько подписок ждут уведомления
2. Сколько из них имеют handshake
3. Длину сообщения (лимит Telegram 4096)
4. ADMIN_TELEGRAM_ID
"""
import os
import subprocess
import sys

# Load env
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
TELEGRAM_LIMIT = 4096


def get_handshakes():
    """wg show <iface> latest-handshakes -> dict pubkey -> timestamp."""
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
    print("=" * 60)
    print("Диагностика уведомлений new_handshake_admin")
    print("=" * 60)

    admin_id = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
    print(f"\n1. ADMIN_TELEGRAM_ID: {admin_id or '(не задан!)'}")

    with psycopg2.connect(**DB) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT s.id, s.telegram_user_id, s.telegram_user_name, s.wg_public_key,
                       s.last_event_name, s.expires_at
                FROM vpn_subscriptions s
                WHERE s.active = TRUE
                  AND s.expires_at > NOW()
                  AND s.telegram_user_id IS NOT NULL
                  AND s.wg_public_key IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM subscription_notifications n
                    WHERE n.subscription_id = s.id AND n.notification_type = 'new_handshake_admin'
                  )
                ORDER BY s.created_at ASC;
            """)
            subs = [dict(r) for r in cur.fetchall()]

    print(f"\n2. Подписки без уведомления: {len(subs)}")

    if not subs:
        print("   Нет подписок в очереди — всё уже уведомлено.")
        return

    try:
        handshakes = get_handshakes()
        print(f"\n3. Ключей в WireGuard (latest-handshakes): {len(handshakes)}")
    except Exception as e:
        print(f"\n3. Ошибка wg: {e}")
        return

    with_handshake = []
    for sub in subs:
        pk = (sub.get("wg_public_key") or "").strip()
        if pk and handshakes.get(pk, 0) > 0:
            with_handshake.append(sub)

    print(f"   С handshake (будут в уведомлении): {len(with_handshake)}")

    if not with_handshake:
        print("\n   Нет подписок с handshake — уведомление не отправляется.")
        return

    # Эмуляция построения сообщения (упрощённо, без ref_info)
    def fmt_line(sub):
        uname = (sub.get("telegram_user_name") or "").strip()
        uid = sub.get("telegram_user_id")
        ln = f"@{uname} (ID {uid})" if uname else f"ID {uid}"
        exp = sub.get("expires_at")
        exp_str = exp.strftime("%d.%m.%Y") if exp and hasattr(exp, "strftime") else "?"
        ev = sub.get("last_event_name") or ""
        if ev == "referral_free_trial_7d":
            return f"• {ln} | Реферер @ref (N) | До {exp_str}"
        if ev.startswith("promo"):
            return f"• {ln} | PROMO | До {exp_str}"
        return f"• {ln} | оплата | До {exp_str}"

    header = f"🟢 Новых подписчиков с handshake: <b>{len(with_handshake)}</b>\n\nТриал:\n"
    body = "\n".join(fmt_line(s) for s in with_handshake)
    text = header + body
    text_len = len(text)

    print(f"\n4. Длина сообщения: {text_len} символов (лимит {TELEGRAM_LIMIT})")
    if text_len > TELEGRAM_LIMIT:
        print(f"   ⚠️  ПРЕВЫШЕН ЛИМИТ на {text_len - TELEGRAM_LIMIT} — send_message FAIL!")
        print("   -> Уведомления НЕ приходят из‑за слишком длинного сообщения.")
    else:
        print("   OK")

    print("\n5. Первые 3 подписки:")
    for i, s in enumerate(with_handshake[:3], 1):
        print(f"   {i}. sub_id={s['id']} tg_id={s['telegram_user_id']} event={s.get('last_event_name')}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
