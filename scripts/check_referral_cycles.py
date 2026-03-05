#!/usr/bin/env python3
"""Проверка БД на циклы в реферальном дереве."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

import psycopg2

DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "postgres"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def main():
    with psycopg2.connect(**DB) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT referred_telegram_user_id, referrer_telegram_user_id FROM referrals"
        )
        rows = cur.fetchall()
        # referred -> referrer
        referrer_of: dict[int, int] = {}
        for referred, referrer in rows:
            referrer_of[int(referred)] = int(referrer)

    cycles: list[list[int]] = []

    for start in referrer_of:
        seen: set[int] = set()
        path: list[int] = []
        current = start
        while current in referrer_of:
            referrer = referrer_of[current]
            if referrer in seen:
                # Нашли цикл: от referrer до current и обратно к referrer
                cycle_start = path.index(referrer) if referrer in path else 0
                cycle = path[cycle_start:] + [current, referrer]
                cycles.append(cycle)
                break
            seen.add(current)
            path.append(current)
            current = referrer
        else:
            if current in seen:
                cycles.append(path + [current])

    # Убираем дубликаты циклов (один цикл можно начать с разных точек)
    def normalize_cycle(c: list[int]) -> tuple:
        min_idx = min(range(len(c)), key=lambda i: c[i])
        return tuple(c[min_idx:] + c[:min_idx])

    seen_cycles: set[tuple] = set()
    unique_cycles: list[list[int]] = []
    for c in cycles:
        key = normalize_cycle(c)
        if key not in seen_cycles:
            seen_cycles.add(key)
            unique_cycles.append(c)

    if not unique_cycles:
        print("Циклов не обнаружено.")
        return

    print(f"Найдено циклов: {len(unique_cycles)}\n")
    for i, cycle in enumerate(unique_cycles, 1):
        print(f"Цикл {i}: {' -> '.join(map(str, cycle))} -> ...")


if __name__ == "__main__":
    main()
