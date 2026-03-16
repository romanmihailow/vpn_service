#!/usr/bin/env python3
"""
Проверка корректности начислений баллов по всем пользователям:
1. Баланс: сумма delta == текущий balance
2. ref_level_N: bonus = round(ref_base_bonus * multiplier) для тарифа
3. pay_tariff_points: |delta| == points_cost тарифа
"""
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
from decimal import Decimal

DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "postgres"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

def main():
    conn = psycopg2.connect(**DB)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Тарифы: ref_base_bonus, points_cost
    cur.execute("""
        SELECT code, ref_base_bonus_points, points_cost
        FROM tariffs WHERE is_active = TRUE
    """)
    tariffs = {r["code"]: r for r in cur.fetchall()}

    # Уровни рефералки
    cur.execute("SELECT level, multiplier, is_active FROM referral_levels ORDER BY level")
    levels = {int(r["level"]): {"multiplier": float(r["multiplier"] or 0), "is_active": r["is_active"]}
              for r in cur.fetchall() if r["level"]}

    errors = []
    warnings = []

    # Все транзакции по пользователям
    cur.execute("""
        SELECT telegram_user_id, delta, reason, source, level, meta, created_at
        FROM user_points_transactions
        ORDER BY telegram_user_id, created_at ASC, id ASC
    """)
    rows = cur.fetchall()

    # Группируем по пользователю
    by_user = {}
    for r in rows:
        uid = r["telegram_user_id"]
        if uid not in by_user:
            by_user[uid] = []
        by_user[uid].append(dict(r))

    # Текущие балансы
    cur.execute("SELECT telegram_user_id, balance FROM user_points")
    balances = {r["telegram_user_id"]: int(r["balance"]) for r in cur.fetchall()}

    for tg_id, txs in by_user.items():
        running = 0
        for t in txs:
            running += t["delta"]

        expected_balance = balances.get(tg_id, 0)
        if running != expected_balance:
            errors.append(f"tg_id={tg_id}: сумма delta={running}, balance в user_points={expected_balance}")

        # Проверка ref_level
        for t in txs:
            if t["reason"] and t["reason"].startswith("ref_level_"):
                try:
                    level = int(t["reason"].split("_")[-1])
                except ValueError:
                    continue
                meta = t["meta"] or {}
                tariff_code = meta.get("tariff_code")
                if not tariff_code:
                    warnings.append(f"tg_id={tg_id} {t['created_at']}: ref_level_{level} без tariff_code в meta")
                    continue
                tr = tariffs.get(tariff_code)
                if not tr:
                    warnings.append(f"tg_id={tg_id}: тариф {tariff_code} не найден")
                    continue
                base = tr.get("ref_base_bonus_points") or 0
                lvl = levels.get(level, {})
                mult = lvl.get("multiplier", 0) if lvl.get("is_active") else 0
                expected_bonus = int(round(base * mult))
                if t["delta"] != expected_bonus:
                    errors.append(
                        f"tg_id={tg_id} {t['created_at']}: ref_level_{level} delta={t['delta']} "
                        f"ожидалось {expected_bonus} (base={base} mult={mult} tariff={tariff_code})"
                    )

        # Проверка pay_tariff_points
        for t in txs:
            if t["reason"] == "pay_tariff_points" and t["delta"] < 0:
                meta = t["meta"] or {}
                tariff_code = meta.get("tariff_code")
                if not tariff_code:
                    continue
                tr = tariffs.get(tariff_code)
                if not tr:
                    continue
                cost = tr.get("points_cost")
                if cost is not None:
                    try:
                        cost_int = int(cost)
                    except (TypeError, ValueError):
                        continue
                    if abs(t["delta"]) != cost_int:
                        errors.append(
                            f"tg_id={tg_id} {t['created_at']}: pay_tariff_points delta={t['delta']} "
                            f"ожидалось -{cost_int} (tariff {tariff_code} points_cost={cost_int})"
                        )

    # Доп. проверка: дубли реферальных бонусов по одному payment_id
    cur.execute("""
        SELECT related_payment_id, COUNT(*) AS cnt, SUM(delta) AS total
        FROM user_points_transactions
        WHERE reason LIKE 'ref_level_%%' AND related_payment_id IS NOT NULL
        GROUP BY related_payment_id
        HAVING COUNT(*) > 5
    """)
    dup_refs = cur.fetchall()
    if dup_refs:
        for r in dup_refs:
            errors.append(f"payment_id={r['related_payment_id']}: {r['cnt']} ref-начислений (ожидалось <=5 уровней)")

    conn.close()

    print("=== Проверка корректности начислений баллов ===\n")
    print(f"Пользователей с транзакциями: {len(by_user)}")
    print(f"Тарифов: {len(tariffs)}")
    print(f"Уровней рефералки: {len(levels)}\n")

    if errors:
        print(f"ОШИБКИ ({len(errors)}):")
        for e in errors[:50]:
            print(f"  {e}")
        if len(errors) > 50:
            print(f"  ... и ещё {len(errors) - 50}")
    else:
        print("Ошибок не найдено.")

    if warnings:
        print(f"\nПредупреждения ({len(warnings)}):")
        for w in warnings[:20]:
            print(f"  {w}")
        if len(warnings) > 20:
            print(f"  ... и ещё {len(warnings) - 20}")

    return 1 if errors else 0

if __name__ == "__main__":
    sys.exit(main())
