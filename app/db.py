import psycopg2
import psycopg2.extras
import psycopg2.pool
from contextlib import contextmanager
import contextvars
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import json
from .config import settings


_ip_lock_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ip_allocation_lock_ctx",
    default=None,
)


_POOL = psycopg2.pool.ThreadedConnectionPool(
    minconn=settings.DB_POOL_MIN,
    maxconn=settings.DB_POOL_MAX,
    host=settings.DB_HOST,
    port=settings.DB_PORT,
    dbname=settings.DB_NAME,
    user=settings.DB_USER,
    password=settings.DB_PASSWORD,
    sslmode="disable",
)


def acquire_ip_allocation_lock() -> None:
    ctx = _ip_lock_ctx.get()
    if ctx is not None:
        ctx["count"] += 1
        return

    conn = _POOL.getconn()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s);", (settings.DB_IP_ALLOC_LOCK_ID,))
    _ip_lock_ctx.set({"conn": conn, "count": 1})


def release_ip_allocation_lock() -> None:
    ctx = _ip_lock_ctx.get()
    if ctx is None:
        return

    ctx["count"] -= 1
    if ctx["count"] > 0:
        return

    conn = ctx["conn"]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s);", (settings.DB_IP_ALLOC_LOCK_ID,))
    finally:
        _POOL.putconn(conn)
        _ip_lock_ctx.set(None)


@contextmanager
def get_conn():
    ctx = _ip_lock_ctx.get()
    if ctx is not None:
        conn = ctx["conn"]
        try:
            yield conn
        finally:
            pass
        return

    conn = _POOL.getconn()
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        finally:
            _POOL.putconn(conn)


def init_db() -> None:
    create_table_sql = """
    CREATE TABLE IF NOT EXISTS vpn_subscriptions (
        id SERIAL PRIMARY KEY,
        tribute_user_id BIGINT NOT NULL,
        telegram_user_id BIGINT NOT NULL,
        telegram_user_name TEXT,
        subscription_id BIGINT NOT NULL,
        period_id BIGINT NOT NULL,

        period VARCHAR(64) NOT NULL,
        channel_id BIGINT NOT NULL,
        channel_name TEXT NOT NULL,

        vpn_ip VARCHAR(64) NOT NULL,
        wg_private_key TEXT NOT NULL,
        wg_public_key TEXT NOT NULL,

        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        last_event_name VARCHAR(64) NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_vpn_subscriptions_telegram
        ON vpn_subscriptions (telegram_user_id);

    CREATE INDEX IF NOT EXISTS idx_vpn_subscriptions_active
        ON vpn_subscriptions (active);

    CREATE INDEX IF NOT EXISTS idx_vpn_subscriptions_user_period
        ON vpn_subscriptions (tribute_user_id, period_id, channel_id);

    --------------------------------------------------------------------
    -- Таблица тарифов
    --------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS tariffs (
        id SERIAL PRIMARY KEY,
        code VARCHAR(32) NOT NULL UNIQUE,         -- "1m", "3m", "6m", "1y", "forever"
        title TEXT NOT NULL,                      -- надпись на кнопке / человекочитаемое имя
        duration_days INTEGER NOT NULL,           -- срок подписки в днях

        -- Цена для ЮKassa (рубли)
        yookassa_amount NUMERIC(10, 2),

        -- Цена для Heleket (USDT / доллары)
        heleket_amount NUMERIC(10, 2),

        -- Опционально: цена в баллах (на будущее)
        points_cost INTEGER,

        -- Базовый бонус в баллах для 1-й линии рефералки
        ref_base_bonus_points INTEGER,

        -- Включена ли рефералка для этого тарифа
        ref_enabled BOOLEAN NOT NULL DEFAULT TRUE,

        -- Можно временно выключать тариф
        is_active BOOLEAN NOT NULL DEFAULT TRUE,

        -- Порядок сортировки кнопок
        sort_order INTEGER NOT NULL DEFAULT 100,

        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_tariffs_code
        ON tariffs (code);

    CREATE INDEX IF NOT EXISTS idx_tariffs_active
        ON tariffs (is_active);

    CREATE INDEX IF NOT EXISTS idx_tariffs_sort
        ON tariffs (sort_order);

    --------------------------------------------------------------------
    -- Баланс поинтов пользователя
    --------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS user_points (
        telegram_user_id BIGINT PRIMARY KEY,
        balance BIGINT NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    --------------------------------------------------------------------
    -- Журнал операций с поинтами
    --------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS user_points_transactions (
        id BIGSERIAL PRIMARY KEY,
        telegram_user_id BIGINT NOT NULL,
        delta BIGINT NOT NULL,
        reason VARCHAR(64) NOT NULL,             -- 'ref_level_1', 'ref_level_2', 'promo', 'admin', 'pay_tariff_points'
        source VARCHAR(64) NOT NULL,             -- 'yookassa', 'heleket', 'tribute', 'manual', ...
        related_subscription_id BIGINT,          -- ссылка на vpn_subscriptions.id (если есть)
        related_payment_id VARCHAR(128),         -- id платежа из ЮKassa / Heleket, если есть
        level INTEGER,                           -- уровень рефералки (1-5), если применимо
        meta JSONB,                              -- доп. инфа: {"tariff_code": "1m", "referrer_id": 123}
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_user_points_transactions_user
        ON user_points_transactions (telegram_user_id, created_at DESC);

    CREATE INDEX IF NOT EXISTS idx_user_points_transactions_payment
        ON user_points_transactions (related_payment_id);

    --------------------------------------------------------------------
    -- Связь кто кого привёл (прямой реферер)
    --------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS referrals (
        referred_telegram_user_id BIGINT PRIMARY KEY,   -- тот, кто пришёл
        referrer_telegram_user_id BIGINT NOT NULL,      -- его прямой реферер (1-я линия)
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT referrals_no_self_ref
            CHECK (referred_telegram_user_id <> referrer_telegram_user_id)
    );

    CREATE INDEX IF NOT EXISTS idx_referrals_referrer
        ON referrals (referrer_telegram_user_id);

    --------------------------------------------------------------------
    -- Реферальные коды
    --------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS referral_codes (
        code VARCHAR(64) PRIMARY KEY,                 -- 'MAXNET123', 'ROMAN_VPN', ...
        referrer_telegram_user_id BIGINT NOT NULL,    -- чей это код
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_referral_codes_referrer
        ON referral_codes (referrer_telegram_user_id);

    CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_codes_referrer_active
        ON referral_codes (referrer_telegram_user_id)
        WHERE is_active = TRUE;

    --------------------------------------------------------------------
    -- Настройка уровней рефералки
    --------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS referral_levels (
        level INTEGER PRIMARY KEY,                 -- 1..5
        multiplier NUMERIC(10, 4) NOT NULL,        -- 1.0000, 0.5000, ...
        is_active BOOLEAN NOT NULL DEFAULT TRUE
    );

    --------------------------------------------------------------------
    -- Профиль пользователя (флаги блокировок)
    --------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS user_profiles (
        telegram_user_id BIGINT PRIMARY KEY,
        is_referral_blocked BOOLEAN NOT NULL DEFAULT FALSE,   -- фрод / мошенник
        is_banned BOOLEAN NOT NULL DEFAULT FALSE,             -- бан для бота (на будущее)
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    --------------------------------------------------------------------
    -- Уведомления по подпискам (о скором окончании / окончании)
    --------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS subscription_notifications (
        id BIGSERIAL PRIMARY KEY,
        subscription_id BIGINT NOT NULL REFERENCES vpn_subscriptions(id) ON DELETE CASCADE,
        telegram_user_id BIGINT, -- для дедупликации по пользователю
        expires_at TIMESTAMPTZ, -- время окончания подписки на момент уведомления
        notification_type VARCHAR(32) NOT NULL, -- 'expires_3d', 'expires_1d', 'expired'
        sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    ALTER TABLE subscription_notifications
        ADD COLUMN IF NOT EXISTS telegram_user_id BIGINT;
    ALTER TABLE subscription_notifications
        ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
    ALTER TABLE subscription_notifications
        ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

    CREATE UNIQUE INDEX IF NOT EXISTS idx_subscription_notifications_unique
        ON subscription_notifications (subscription_id, notification_type);

    CREATE UNIQUE INDEX IF NOT EXISTS idx_subscription_notifications_user_expiry
        ON subscription_notifications (telegram_user_id, notification_type, expires_at);

    CREATE INDEX IF NOT EXISTS idx_subscription_notifications_type
        ON subscription_notifications (notification_type);
    """



    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(create_table_sql)
        conn.commit()


def get_active_subscription(
    tribute_user_id: int,
    period_id: int,
    channel_id: int,
) -> Optional[Dict[str, Any]]:
    sql = """
    SELECT *
    FROM vpn_subscriptions
    WHERE tribute_user_id = %s
      AND period_id = %s
      AND channel_id = %s
      AND active = TRUE
      AND expires_at > NOW()
    ORDER BY expires_at DESC
    LIMIT 1;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (tribute_user_id, period_id, channel_id))
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)

def get_subscription_by_tribute_and_subscription(
    tribute_user_id: int,
    subscription_id: int,
) -> Optional[Dict[str, Any]]:
    """
    Возвращает последнюю подписку по паре (tribute_user_id, subscription_id),
    чтобы обрабатывать повторные уведомления Tribute идемпотентно.
    """
    sql = """
    SELECT *
    FROM vpn_subscriptions
    WHERE tribute_user_id = %s
      AND subscription_id = %s
    ORDER BY id DESC
    LIMIT 1;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (tribute_user_id, subscription_id))
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)

def insert_subscription(
    tribute_user_id: int,
    telegram_user_id: int,
    telegram_user_name: Optional[str],
    subscription_id: int,
    period_id: int,
    period: str,
    channel_id: int,
    channel_name: str,
    vpn_ip: str,
    wg_private_key: str,
    wg_public_key: str,
    expires_at: datetime,
    event_name: str,
) -> int:
    sql = """
    INSERT INTO vpn_subscriptions (
        tribute_user_id,
        telegram_user_id,
        telegram_user_name,
        subscription_id,
        period_id,
        period,
        channel_id,
        channel_name,
        vpn_ip,
        wg_private_key,
        wg_public_key,
        expires_at,
        active,
        last_event_name
    ) VALUES (
        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s
    )
    RETURNING id;
    """
    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        tribute_user_id,
                        telegram_user_id,
                        telegram_user_name,
                        subscription_id,
                        period_id,
                        period,
                        channel_id,
                        channel_name,
                        vpn_ip,
                        wg_private_key,
                        wg_public_key,
                        expires_at,
                        event_name,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            release_ip_allocation_lock()

    if not row:
        raise RuntimeError("Failed to insert subscription and get id")

    # row[0] — это значение SERIAL PRIMARY KEY id
    return row[0]




def update_subscription_expiration(
    sub_id: int,
    expires_at: datetime,
    event_name: str,
) -> None:
    sql = """
    UPDATE vpn_subscriptions
    SET expires_at = %s,
        last_event_name = %s
    WHERE id = %s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (expires_at, event_name, sub_id))
        conn.commit()


def deactivate_subscriptions_for_period(
    tribute_user_id: int,
    period_id: int,
    channel_id: int,
    event_name: str,
) -> List[Dict[str, Any]]:
    """
    Деактивируем все активные подписки этого пользователя
    на этот период/канал и возвращаем их (чтобы убрать peer в WG).
    """
    select_sql = """
    SELECT *
    FROM vpn_subscriptions
    WHERE tribute_user_id = %s
      AND period_id = %s
      AND channel_id = %s
      AND active = TRUE;
    """

    update_sql = """
    UPDATE vpn_subscriptions
    SET active = FALSE,
        last_event_name = %s
    WHERE tribute_user_id = %s
      AND period_id = %s
      AND channel_id = %s
      AND active = TRUE;
    """

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(select_sql, (tribute_user_id, period_id, channel_id))
            rows = cur.fetchall()
            subs = [dict(r) for r in rows]

            cur.execute(update_sql, (event_name, tribute_user_id, period_id, channel_id))
        conn.commit()

    return subs

def deactivate_subscription_by_id(
    sub_id: int,
    event_name: str,
) -> Optional[Dict[str, Any]]:
    """
    Деактивирует одну подписку по id (если она ещё активна) и возвращает её данные.
    Нужно, чтобы из админки отключать конкретный ключ.
    """
    select_sql = """
    SELECT *
    FROM vpn_subscriptions
    WHERE id = %s
      AND active = TRUE;
    """

    update_sql = """
    UPDATE vpn_subscriptions
    SET active = FALSE,
        last_event_name = %s
    WHERE id = %s
      AND active = TRUE;
    """

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(select_sql, (sub_id,))
            row = cur.fetchone()
            if not row:
                return None
            sub = dict(row)

            cur.execute(update_sql, (event_name, sub_id))
        conn.commit()

    return sub

def activate_subscription_by_id(
    sub_id: int,
    event_name: str,
) -> Optional[Dict[str, Any]]:
    """
    Активирует одну подписку по id (если она сейчас неактивна) и возвращает её данные.
    Нужно, чтобы из админки включать ключ обратно.
    """
    select_sql = """
    SELECT *
    FROM vpn_subscriptions
    WHERE id = %s
      AND active = FALSE;
    """

    update_sql = """
    UPDATE vpn_subscriptions
    SET active = TRUE,
        last_event_name = %s
    WHERE id = %s
      AND active = FALSE;
    """

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(select_sql, (sub_id,))
            row = cur.fetchone()
            if not row:
                return None
            sub = dict(row)

            cur.execute(update_sql, (event_name, sub_id))
        conn.commit()

    return sub


def get_subscription_by_id(
    sub_id: int,
) -> Optional[Dict[str, Any]]:
    """
    Возвращает подписку по её id (активную или нет).
    """
    sql = """
    SELECT *
    FROM vpn_subscriptions
    WHERE id = %s;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (sub_id,))
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)


def delete_subscription_by_id(
    sub_id: int,
) -> bool:
    """
    Полностью удаляет подписку по id из базы.
    Перед удалением обнуляет ссылки на эту подписку
    в зависимых таблицах (если они есть).
    Возвращает True, если строка была удалена.
    """
    sql_clear_points = """
    UPDATE user_points_transactions
    SET related_subscription_id = NULL
    WHERE related_subscription_id = %s;
    """
    sql_clear_promo_usages = """
    UPDATE promo_code_usages
    SET subscription_id = NULL
    WHERE subscription_id = %s;
    """
    sql_delete = """
    DELETE FROM vpn_subscriptions
    WHERE id = %s;
    """

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Пытаемся обнулить ссылки в user_points_transactions
            try:
                cur.execute(sql_clear_points, (sub_id,))
            except Exception:
                # Если таблицы нет или нет такого поля — просто идём дальше.
                # Важнее не завалить удаление подписки.
                pass

            # Пытаемся обнулить ссылки в promo_code_usages
            try:
                cur.execute(sql_clear_promo_usages, (sub_id,))
            except Exception:
                # Та же логика — безопасно игнорируем, если таблицы нет
                pass

            # Теперь удаляем саму подписку
            cur.execute(sql_delete, (sub_id,))
            deleted = cur.rowcount

        conn.commit()

    return deleted > 0


def get_max_client_ip_last_octet() -> int:
    """
    Смотрим максимальный последний октет в vpn_ip из таблицы, чтобы выдавать следующий.
    Ожидаем формат 10.8.0.X
    """
    sql = """
    SELECT vpn_ip
    FROM vpn_subscriptions
    ORDER BY id DESC
    LIMIT 100;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()

    max_octet = 0
    prefix = settings.WG_CLIENT_NETWORK_PREFIX
    for (ip,) in rows:
        if isinstance(ip, str) and ip.startswith(prefix):
            try:
                last = int(ip.split(".")[-1])
                if last > max_octet:
                    max_octet = last
            except ValueError:
                continue

    return max_octet


def is_vpn_ip_used(vpn_ip: str) -> bool:
    """
    Проверяет, используется ли указанный vpn_ip в активной не истёкшей подписке.
    Возвращает True, если IP уже занят.
    """
    sql = """
    SELECT 1
    FROM vpn_subscriptions
    WHERE vpn_ip = %s
      AND active = TRUE
      AND expires_at > NOW()
    LIMIT 1;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (vpn_ip,))
            row = cur.fetchone()
            return row is not None


def get_last_subscriptions(limit: int = 50):
    """
    Возвращает последние N подписок для админки.
    """
    sql = """
    SELECT *
    FROM vpn_subscriptions
    ORDER BY id DESC
    LIMIT %s;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (limit,))
            return cur.fetchall()


def get_active_tariffs() -> List[Dict[str, Any]]:
    """
    Возвращает список активных тарифов из таблицы tariffs.
    Используется для формирования текста /subscription.
    """
    sql = """
    SELECT
        code,
        title,
        duration_days,
        sort_order,
        yookassa_amount,
        heleket_amount,
        points_cost
    FROM tariffs
    WHERE is_active = TRUE
    ORDER BY sort_order ASC;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            return rows


def get_all_telegram_users() -> List[Dict[str, Any]]:
    """
    Возвращает список уникальных Telegram-пользователей,
    которые есть в таблице vpn_subscriptions.
    Формат элементов списка: {"telegram_user_id": 123456789}
    """
    sql = """
    SELECT DISTINCT telegram_user_id
    FROM vpn_subscriptions
    WHERE telegram_user_id IS NOT NULL
    ORDER BY telegram_user_id;
    """

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            return rows


def get_total_subscribers_count() -> int:
    """
    Возвращает количество уникальных Telegram-пользователей в подписках.
    """
    sql = """
    SELECT COUNT(DISTINCT telegram_user_id) AS cnt
    FROM vpn_subscriptions
    WHERE telegram_user_id IS NOT NULL;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            if not row or row[0] is None:
                return 0
            return int(row[0])



def get_latest_subscription_for_telegram(
    telegram_user_id: int,
) -> Optional[Dict[str, Any]]:
    """
    Возвращает последнюю ДЕЙСТВУЮЩУЮ подписку для данного Telegram-пользователя.
    Учитываем и active = TRUE, и expires_at > NOW().
    """
    sql = """
    SELECT *
    FROM vpn_subscriptions
    WHERE telegram_user_id = %s
      AND active = TRUE
      AND expires_at > NOW()
    ORDER BY expires_at DESC, id DESC
    LIMIT 1;
    """

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (telegram_user_id,))
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)
        

def pay_subscription_with_points(
    telegram_user_id: int,
    tariff_code: str,
) -> Dict[str, Any]:
    """
    Оплачивает продление текущей активной подписки пользователя бонусными баллами.

    Логика:
    - находит тариф по коду с points_cost и duration_days;
    - проверяет, что есть активная подписка;
    - проверяет, что у пользователя хватает баллов;
    - списывает баллы и пишет запись в user_points и user_points_transactions;
    - продлевает expires_at у подписки.

    Возвращает dict:
        {
            "ok": True/False,
            "error": ... или None,
            "error_message": ... или None,
            "subscription_id": ...,
            "tariff_code": ...,
            "points_cost": ...,
            "duration_days": ...,
            "new_expires_at": <datetime или None>,
            "new_balance": <int или None>,
        }
    """
    result: Dict[str, Any] = {
        "ok": False,
        "error": None,
        "error_message": None,
        "subscription_id": None,
        "tariff_code": tariff_code,
        "points_cost": None,
        "duration_days": None,
        "new_expires_at": None,
        "new_balance": None,
    }

    normalized_code = (tariff_code or "").strip()
    if not normalized_code:
        result["error"] = "empty_tariff_code"
        result["error_message"] = "Код тарифа не задан."
        return result

    with get_conn() as conn:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 1) Ищем тариф по коду, который можно оплатить баллами
                sql_tariff = """
                SELECT
                    code,
                    title,
                    duration_days,
                    points_cost
                FROM tariffs
                WHERE code = %s
                  AND is_active = TRUE
                  AND points_cost IS NOT NULL
                LIMIT 1;
                """
                cur.execute(sql_tariff, (normalized_code,))
                tariff_row = cur.fetchone()
                if tariff_row is None:
                    result["error"] = "tariff_not_found_or_inactive"
                    result["error_message"] = "Тариф не найден или недоступен для оплаты баллами."
                    return result

                points_cost_raw = tariff_row.get("points_cost")
                duration_days_raw = tariff_row.get("duration_days")

                try:
                    points_cost_int = int(points_cost_raw)
                except (TypeError, ValueError):
                    points_cost_int = 0

                try:
                    duration_days_int = int(duration_days_raw)
                except (TypeError, ValueError):
                    duration_days_int = 0

                if points_cost_int <= 0 or duration_days_int <= 0:
                    result["error"] = "invalid_tariff_points_or_duration"
                    result["error_message"] = "Для этого тарифа не задана корректная цена в баллах или длительность."
                    return result

                result["points_cost"] = points_cost_int
                result["duration_days"] = duration_days_int

                # 2) Ищем последнюю активную подписку пользователя и блокируем её строку
                sql_sub = """
                SELECT *
                FROM vpn_subscriptions
                WHERE telegram_user_id = %s
                  AND active = TRUE
                  AND expires_at > NOW()
                ORDER BY expires_at DESC, id DESC
                LIMIT 1
                FOR UPDATE;
                """
                cur.execute(sql_sub, (telegram_user_id,))
                sub_row = cur.fetchone()
                if sub_row is None:
                    result["error"] = "no_active_subscription"
                    result["error_message"] = "У тебя нет активной подписки, которую можно продлить баллами."
                    return result

                sub_id = sub_row["id"]
                result["subscription_id"] = sub_id

                # 3) Получаем и блокируем баланс поинтов пользователя
                sql_points_select = """
                SELECT balance
                FROM user_points
                WHERE telegram_user_id = %s
                FOR UPDATE;
                """
                cur.execute(sql_points_select, (telegram_user_id,))
                points_row = cur.fetchone()
                if points_row is None or points_row.get("balance") is None:
                    current_balance = 0
                else:
                    try:
                        current_balance = int(points_row["balance"])
                    except (TypeError, ValueError):
                        current_balance = 0

                if current_balance < points_cost_int:
                    result["error"] = "insufficient_points"
                    result["error_message"] = "Недостаточно баллов для продления подписки."
                    result["new_balance"] = current_balance
                    return result

                new_balance = current_balance - points_cost_int

                # 4) Обновляем баланс в user_points (UPSERT)
                sql_points_upsert = """
                INSERT INTO user_points (telegram_user_id, balance, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (telegram_user_id) DO UPDATE
                SET balance = EXCLUDED.balance,
                    updated_at = NOW()
                RETURNING balance;
                """
                cur.execute(sql_points_upsert, (telegram_user_id, new_balance))
                balance_row = cur.fetchone()
                if balance_row is None:
                    raise RuntimeError("Failed to update user_points balance")

                try:
                    final_balance = int(balance_row["balance"])
                except (TypeError, ValueError):
                    final_balance = new_balance

                result["new_balance"] = final_balance

                # 5) Пишем транзакцию по поинтам
                sql_insert_tx = """
                INSERT INTO user_points_transactions (
                    telegram_user_id,
                    delta,
                    reason,
                    source,
                    related_subscription_id,
                    related_payment_id,
                    level,
                    meta
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                """
                meta: Dict[str, Any] = {
                    "tariff_code": normalized_code,
                    "duration_days": duration_days_int,
                    "points_cost": points_cost_int,
                }
                meta_json = psycopg2.extras.Json(meta)

                cur.execute(
                    sql_insert_tx,
                    (
                        telegram_user_id,
                        -points_cost_int,
                        "subscription_extend",
                        "points",
                        sub_id,
                        None,
                        None,
                        meta_json,
                    ),
                )

                # 6) Продлеваем подписку
                sql_update_sub = """
                UPDATE vpn_subscriptions
                SET expires_at = GREATEST(expires_at, NOW()) + (%s || ' days')::interval,
                    last_event_name = %s
                WHERE id = %s
                RETURNING expires_at;
                """
                last_event_name = f"points_extend_{normalized_code}"
                cur.execute(sql_update_sub, (duration_days_int, last_event_name, sub_id))
                updated_sub = cur.fetchone()
                if updated_sub is None:
                    raise RuntimeError("Failed to update subscription expiration")

                new_expires_at = updated_sub["expires_at"]
                result["new_expires_at"] = new_expires_at

            conn.commit()

            result["ok"] = True
            return result

        except Exception as e:
            conn.rollback()
            result["error"] = result["error"] or "db_error"
            if result.get("error_message") is None:
                result["error_message"] = f"Ошибка при оплате подписки баллами: {e!r}"
            return result


def get_active_subscriptions_for_telegram(
    telegram_user_id: int,
) -> List[Dict[str, Any]]:
    """
    Возвращает все активные НЕ истёкшие подписки для данного Telegram-пользователя.
    Используется для автоочистки перед выдачей нового доступа.
    """
    sql = """
    SELECT *
    FROM vpn_subscriptions
    WHERE telegram_user_id = %s
      AND active = TRUE
      AND expires_at > NOW()
    ORDER BY expires_at DESC, id DESC;
    """

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (telegram_user_id,))
            rows = cur.fetchall()
            return [dict(r) for r in rows]


def has_referral_trial_subscription(
    telegram_user_id: int,
) -> bool:
    """
    Проверяет, есть ли у пользователя хотя бы одна подписка,
    выданная как реферальный пробный доступ (last_event_name='referral_free_trial_7d').
    """
    sql = """
    SELECT 1
    FROM vpn_subscriptions
    WHERE telegram_user_id = %s
      AND last_event_name = 'referral_free_trial_7d'
    LIMIT 1;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (telegram_user_id,))
            row = cur.fetchone()
            return row is not None


def get_expired_active_subscriptions() -> List[Dict[str, Any]]:
    """
    Возвращает все подписки, которые ещё помечены active=TRUE,
    но у которых expires_at <= NOW().
    Нужны для автоматической деактивации по истечению срока.
    """
    sql = """
    SELECT *
    FROM vpn_subscriptions
    WHERE active = TRUE
      AND expires_at <= NOW();
    """

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            return [dict(r) for r in rows]


def create_subscription_notification(
    subscription_id: int,
    notification_type: str,
    telegram_user_id: Optional[int] = None,
    expires_at: Optional[datetime] = None,
) -> None:
    """
    Регистрирует факт отправки уведомления по подписке.

    Идемпотентно: при повторном вызове с теми же (subscription_id, notification_type)
    запись не будет дублироваться за счёт UNIQUE-индекса.
    """
    sql = """
    INSERT INTO subscription_notifications (
        subscription_id,
        notification_type,
        telegram_user_id,
        expires_at,
        sent_at
    )
    VALUES (%s, %s, %s, %s, NOW())
    ON CONFLICT DO NOTHING;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    subscription_id,
                    notification_type,
                    telegram_user_id,
                    expires_at,
                ),
            )
        conn.commit()


def has_subscription_notification(
    subscription_id: int,
    notification_type: str,
    telegram_user_id: Optional[int] = None,
    expires_at: Optional[datetime] = None,
) -> bool:
    """
    Проверяет, отправляли ли уже уведомление нужного типа по этой подписке.
    """
    if telegram_user_id is not None and expires_at is not None:
        sql = """
        SELECT 1
        FROM subscription_notifications
        WHERE notification_type = %s
          AND (
            subscription_id = %s
            OR (telegram_user_id = %s AND expires_at = %s)
          )
        LIMIT 1;
        """
        params = (notification_type, subscription_id, telegram_user_id, expires_at)
    else:
        sql = """
        SELECT 1
        FROM subscription_notifications
        WHERE subscription_id = %s
          AND notification_type = %s
        LIMIT 1;
        """
        params = (subscription_id, notification_type)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row is not None


def get_subscriptions_expiring_in_window(
    from_hours: int,
    to_hours: int,
) -> List[Dict[str, Any]]:
    """
    Возвращает активные подписки, у которых expires_at попадает
    в окно (NOW() + from_hours .. NOW() + to_hours), в часах.

    Используется для уведомлений о скором окончании подписки.
    Пример:
        get_subscriptions_expiring_in_window(72, 73)  # примерно за 3 дня
        get_subscriptions_expiring_in_window(24, 25)  # примерно за 1 день
    """
    sql = """
    SELECT *
    FROM vpn_subscriptions
    WHERE active = TRUE
      AND expires_at > NOW() + (%s || ' hours')::interval
      AND expires_at <= NOW() + (%s || ' hours')::interval;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (from_hours, to_hours))
            rows = cur.fetchall()
            return [dict(r) for r in rows]


def subscription_exists_by_event(event_name: str) -> bool:
    """
    Проверяет, есть ли в базе хотя бы одна запись с таким last_event_name.
    Используется для идемпотентной обработки вебхуков ЮKassa.
    """
    sql = """
    SELECT 1
    FROM vpn_subscriptions
    WHERE last_event_name = %s
    LIMIT 1;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (event_name,))
            row = cur.fetchone()
            return row is not None
        
def get_subscription_by_event(event_name: str) -> Optional[Dict[str, Any]]:
    """
    Возвращает последнюю подписку с заданным last_event_name.
    Используем, чтобы найти подписку по платежу YooKassa
    (например, yookassa_payment_succeeded_<payment_id>).
    """
    sql = """
    SELECT *
    FROM vpn_subscriptions
    WHERE last_event_name = %s
    ORDER BY id DESC
    LIMIT 1;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (event_name,))
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)
        
        
def execute_sql(sql: str) -> None:
    """
    Выполняет произвольный SQL-запрос без возврата результата.
    Используется, например, для вставки сгенерированных промокодов.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        
def add_points(
    telegram_user_id: int,
    delta: int,
    reason: str,
    source: str,
    related_subscription_id: Optional[int] = None,
    related_payment_id: Optional[str] = None,
    level: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
    allow_negative: bool = False,
) -> Dict[str, Any]:
    """
    Универсальная точка изменения баланса поинтов.

    delta > 0  -> начисление поинтов
    delta < 0  -> списание поинтов

    Пишет:
      - актуальный баланс в user_points
      - запись в user_points_transactions

    Если allow_negative = False и баланс ушёл бы в минус — операция не выполняется.
    """
    result: Dict[str, Any] = {
        "ok": False,
        "error": None,
        "error_message": None,
        "balance": None,
    }

    if delta == 0:
        result["error"] = "zero_delta"
        result["error_message"] = "Изменение баланса не может быть нулевым."
        return result

    with get_conn() as conn:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 1) Получаем текущий баланс (если нет записи — считаем, что 0)
                sql_select = """
                SELECT balance
                FROM user_points
                WHERE telegram_user_id = %s
                FOR UPDATE;
                """
                cur.execute(sql_select, (telegram_user_id,))
                row = cur.fetchone()

                if row is None or row.get("balance") is None:
                    old_balance = 0
                else:
                    old_balance = int(row["balance"])

                new_balance = old_balance + int(delta)

                if not allow_negative and new_balance < 0:
                    result["error"] = "insufficient_funds"
                    result["error_message"] = "Недостаточно баллов для списания."
                    result["balance"] = old_balance
                    return result

                # 2) Обновляем (или создаём) запись в user_points
                sql_upsert = """
                INSERT INTO user_points (telegram_user_id, balance, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (telegram_user_id) DO UPDATE
                SET balance = EXCLUDED.balance,
                    updated_at = NOW()
                RETURNING balance;
                """
                cur.execute(sql_upsert, (telegram_user_id, new_balance))
                row_balance = cur.fetchone()
                if row_balance is None:
                    raise RuntimeError("Failed to upsert user_points")

                final_balance = int(row_balance["balance"])

                # 3) Пишем транзакцию в журнал
                sql_insert_tx = """
                INSERT INTO user_points_transactions (
                    telegram_user_id,
                    delta,
                    reason,
                    source,
                    related_subscription_id,
                    related_payment_id,
                    level,
                    meta
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                """
                meta_json = psycopg2.extras.Json(meta) if meta is not None else None

                cur.execute(
                    sql_insert_tx,
                    (
                        telegram_user_id,
                        delta,
                        reason,
                        source,
                        related_subscription_id,
                        related_payment_id,
                        level,
                        meta_json,
                    ),
                )

            conn.commit()

            result["ok"] = True
            result["balance"] = final_balance
            return result

        except Exception as e:
            conn.rollback()
            result["error"] = "db_error"
            result["error_message"] = f"Ошибка при работе с базой данных: {e!r}"
            return result


def get_user_points_balance(
    telegram_user_id: int,
) -> int:
    """
    Возвращает текущий баланс поинтов пользователя.
    Если записи нет — возвращает 0.
    """
    sql = """
    SELECT balance
    FROM user_points
    WHERE telegram_user_id = %s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (telegram_user_id,))
            row = cur.fetchone()
            if not row:
                return 0
            balance = row[0]
            if balance is None:
                return 0
            try:
                return int(balance)
            except (TypeError, ValueError):
                return 0


def get_user_points_last_transactions(
    telegram_user_id: int,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Возвращает последние N операций по поинтам пользователя
    (для отображения в /points).
    """
    sql = """
    SELECT
        id,
        telegram_user_id,
        delta,
        reason,
        source,
        related_subscription_id,
        related_payment_id,
        level,
        meta,
        created_at
    FROM user_points_transactions
    WHERE telegram_user_id = %s
    ORDER BY created_at DESC, id DESC
    LIMIT %s;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (telegram_user_id, limit))
            rows = cur.fetchall()
            return list(rows)

        
def link_promo_usage_to_subscription(
    usage_id: int,
    subscription_id: int,
) -> None:
    """
    Привязывает запись об использовании промокода к конкретной подписке.
    Используется для сценария: промокод выдаёт НОВУЮ подписку.
    """
    sql = """
    UPDATE promo_code_usages
    SET subscription_id = %s
    WHERE id = %s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (subscription_id, usage_id))
        conn.commit()



def apply_promo_code_to_latest_subscription(
    telegram_user_id: int,
    code: str,
) -> Dict[str, Any]:
    """
    Пытается применить промокод к последней активной подписке пользователя.

    Возвращает dict вида:
        {
            "ok": True,
            "promo_code": "MAXNET7DAYS",
            "extra_days": 7,
            "new_expires_at": <datetime>,
        }

    либо:
        {
            "ok": False,
            "error": "not_found" | "expired_or_inactive" | "no_active_subscription"
                     | "user_not_allowed" | "no_uses_left" | "per_user_limit_reached"
                     | "db_error",
            "error_message": "Человекочитаемое описание",
        }
    """
    result: Dict[str, Any] = {
        "ok": False,
        "error": "unknown",
        "error_message": "Неизвестная ошибка.",
    }

    normalized_code = (code or "").strip().upper()
    if not normalized_code:
        result["error"] = "empty_code"
        result["error_message"] = "Промокод не должен быть пустым."
        return result

    with get_conn() as conn:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 1) Ищем активный промокод (сразу с блокировкой строки)
                sql_select_promo = """
                SELECT *
                FROM promo_codes
                WHERE code = %s
                  AND is_active = TRUE
                  AND (valid_from IS NULL OR valid_from <= NOW())
                  AND (valid_until IS NULL OR valid_until >= NOW())
                FOR UPDATE;
                """
                cur.execute(sql_select_promo, (normalized_code,))
                promo_row = cur.fetchone()

                if promo_row is None:
                    result["error"] = "not_found"
                    result["error_message"] = "Промокод не найден или его срок действия истёк."
                    return result

                promo_id = promo_row["id"]
                max_uses = promo_row.get("max_uses")
                used_count = promo_row.get("used_count") or 0
                per_user_limit = promo_row.get("per_user_limit") or 1
                extra_days = promo_row.get("extra_days") or 0
                allowed_telegram_id = promo_row.get("allowed_telegram_id")

                # Проверка, что промо вообще что-то добавляет
                if extra_days <= 0:
                    result["error"] = "invalid_extra_days"
                    result["error_message"] = "Этот промокод не даёт дополнительных дней."
                    return result

                # Проверка на конкретного пользователя (если промо ограничено по TG ID)
                if allowed_telegram_id is not None and int(allowed_telegram_id) != int(telegram_user_id):
                    result["error"] = "user_not_allowed"
                    result["error_message"] = "Этот промокод предназначен для другого пользователя."
                    return result

                # Проверка глобального лимита использований
                if max_uses is not None and used_count >= max_uses:
                    result["error"] = "no_uses_left"
                    result["error_message"] = "Лимит использований этого промокода уже исчерпан."
                    return result

                # 2) Считаем, сколько раз КОНКРЕТНЫЙ пользователь уже использовал этот промокод
                sql_user_usage = """
                SELECT COUNT(*) AS cnt
                FROM promo_code_usages
                WHERE promo_code_id = %s
                  AND telegram_user_id = %s;
                """
                cur.execute(sql_user_usage, (promo_id, telegram_user_id))
                row_usage = cur.fetchone()
                user_usage_count = row_usage["cnt"] if row_usage is not None else 0

                if user_usage_count >= per_user_limit:
                    result["error"] = "per_user_limit_reached"
                    result["error_message"] = "Ты уже использовал этот промокод максимально возможное количество раз."
                    return result

                # 3) Ищем последнюю активную подписку пользователя
                sql_select_sub = """
                SELECT *
                FROM vpn_subscriptions
                WHERE telegram_user_id = %s
                  AND active = TRUE
                  AND expires_at > NOW()
                ORDER BY expires_at DESC, id DESC
                LIMIT 1;
                """
                cur.execute(sql_select_sub, (telegram_user_id,))
                sub_row = cur.fetchone()
                if sub_row is None:
                    result["error"] = "no_active_subscription"
                    result["error_message"] = "У тебя нет активной подписки, к которой можно применить промокод."
                    return result

                sub_id = sub_row["id"]

                # 4) Продлеваем подписку на extra_days: GREATEST(expires_at, NOW()) + interval
                sql_update_sub = """
                UPDATE vpn_subscriptions
                SET expires_at = GREATEST(expires_at, NOW()) + (%s || ' days')::interval
                WHERE id = %s
                RETURNING expires_at;
                """
                cur.execute(sql_update_sub, (extra_days, sub_id))
                updated_sub = cur.fetchone()
                if updated_sub is None:
                    result["error"] = "db_error"
                    result["error_message"] = "Не удалось обновить срок действия подписки."
                    return result

                new_expires_at = updated_sub["expires_at"]

                # 5) Записываем факт использования промокода
                sql_insert_usage = """
                INSERT INTO promo_code_usages (promo_code_id, telegram_user_id, subscription_id)
                VALUES (%s, %s, %s);
                """
                cur.execute(sql_insert_usage, (promo_id, telegram_user_id, sub_id))

                # 6) Увеличиваем used_count и, при необходимости, отключаем промокод
                sql_update_promo = """
                UPDATE promo_codes
                SET used_count = used_count + 1,
                    is_active = CASE
                        WHEN max_uses IS NOT NULL AND used_count + 1 >= max_uses THEN FALSE
                        ELSE is_active
                    END
                WHERE id = %s
                RETURNING used_count, max_uses, is_active;
                """
                cur.execute(sql_update_promo, (promo_id,))
                updated_promo = cur.fetchone()
                if updated_promo is None:
                    result["error"] = "db_error"
                    result["error_message"] = "Не удалось обновить статистику по промокоду."
                    return result

                # Всё прошло успешно — фиксируем транзакцию
                conn.commit()

                result["ok"] = True
                result["error"] = None
                result["error_message"] = None
                result["promo_code"] = promo_row["code"]
                result["extra_days"] = extra_days
                result["new_expires_at"] = new_expires_at
                return result

        except Exception as e:
            # В случае исключения просто откатываем транзакцию
            conn.rollback()
            result["error"] = "db_error"
            result["error_message"] = f"Ошибка при работе с базой данных: {e!r}"
            return result
        
def apply_promo_code_without_subscription(
    telegram_user_id: int,
    code: str,
) -> Dict[str, Any]:
    """
    Применяет промокод для пользователя, у которого ещё НЕТ активной подписки.

    ВАЖНО:
    - Эта функция не создаёт запись в vpn_subscriptions.
      Она только проверяет и "списывает" промокод, возвращая extra_days и new_expires_at.
    - Создание самой подписки (WireGuard-ключи, IP, insert_subscription) делает код бота.
    - Для последующей привязки usage -> subscription возвращает usage_id.
    """
    result: Dict[str, Any] = {
        "ok": False,
        "error": "unknown",
        "error_message": "Неизвестная ошибка.",
    }

    normalized_code = (code or "").strip().upper()
    if not normalized_code:
        result["error"] = "empty_code"
        result["error_message"] = "Промокод не должен быть пустым."
        return result

    with get_conn() as conn:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 1) Ищем активный промокод (с блокировкой строки)
                sql_select_promo = """
                SELECT *
                FROM promo_codes
                WHERE code = %s
                  AND is_active = TRUE
                  AND (valid_from IS NULL OR valid_from <= NOW())
                  AND (valid_until IS NULL OR valid_until >= NOW())
                FOR UPDATE;
                """
                cur.execute(sql_select_promo, (normalized_code,))
                promo_row = cur.fetchone()

                if promo_row is None:
                    result["error"] = "not_found"
                    result["error_message"] = "Промокод не найден или его срок действия истёк."
                    return result

                promo_id = promo_row["id"]
                max_uses = promo_row.get("max_uses")
                used_count = promo_row.get("used_count") or 0
                per_user_limit = promo_row.get("per_user_limit") or 1
                extra_days = promo_row.get("extra_days") or 0
                allowed_telegram_id = promo_row.get("allowed_telegram_id")

                # Проверка, что промо вообще что-то даёт
                if extra_days <= 0:
                    result["error"] = "invalid_extra_days"
                    result["error_message"] = "Этот промокод не даёт дополнительных дней."
                    return result

                # Проверка на конкретного пользователя (если промо ограничено по TG ID)
                if allowed_telegram_id is not None and int(allowed_telegram_id) != int(telegram_user_id):
                    result["error"] = "user_not_allowed"
                    result["error_message"] = "Этот промокод предназначен для другого пользователя."
                    return result

                # Проверка глобального лимита использований
                if max_uses is not None and used_count >= max_uses:
                    result["error"] = "no_uses_left"
                    result["error_message"] = "Лимит использований этого промокода уже исчерпан."
                    return result

                # 2) Количество использований ЭТИМ пользователем
                sql_user_usage = """
                SELECT COUNT(*) AS cnt
                FROM promo_code_usages
                WHERE promo_code_id = %s
                  AND telegram_user_id = %s;
                """
                cur.execute(sql_user_usage, (promo_id, telegram_user_id))
                row_usage = cur.fetchone()
                user_usage_count = row_usage["cnt"] if row_usage is not None else 0

                if user_usage_count >= per_user_limit:
                    result["error"] = "per_user_limit_reached"
                    result["error_message"] = "Ты уже использовал этот промокод максимально возможное количество раз."
                    return result

                # 3) Для новой подписки просто берём now + extra_days
                new_expires_at = datetime.utcnow() + timedelta(days=extra_days)

                # 4) Пишем факт использования промокода
                #    subscription_id здесь ещё нет — подписку создаст бот.
                #    Сохраняем usage без subscription_id, но сразу получаем его id.
                sql_insert_usage = """
                INSERT INTO promo_code_usages (promo_code_id, telegram_user_id, subscription_id)
                VALUES (%s, %s, NULL)
                RETURNING id;
                """
                cur.execute(sql_insert_usage, (promo_id, telegram_user_id))
                usage_row = cur.fetchone()
                if not usage_row or "id" not in usage_row:
                    result["error"] = "db_error"
                    result["error_message"] = "Не удалось записать использование промокода."
                    return result

                usage_id = usage_row["id"]

                # 5) Обновляем used_count и is_active
                sql_update_promo = """
                UPDATE promo_codes
                SET used_count = used_count + 1,
                    is_active = CASE
                        WHEN max_uses IS NOT NULL AND used_count + 1 >= max_uses THEN FALSE
                        ELSE is_active
                    END
                WHERE id = %s
                RETURNING used_count, max_uses, is_active;
                """
                cur.execute(sql_update_promo, (promo_id,))
                updated_promo = cur.fetchone()
                if updated_promo is None:
                    result["error"] = "db_error"
                    result["error_message"] = "Не удалось обновить статистику по промокоду."
                    return result

                conn.commit()

                result["ok"] = True
                result["error"] = None
                result["error_message"] = None
                result["promo_code"] = promo_row["code"]
                result["extra_days"] = extra_days
                result["new_expires_at"] = new_expires_at
                result["usage_id"] = usage_id
                return result

        except Exception as e:
            conn.rollback()
            result["error"] = "db_error"
            result["error_message"] = f"Ошибка при работе с базой данных: {e!r}"
            return result

def get_tariffs_for_yookassa() -> List[Dict[str, Any]]:
    """
    Возвращает список активных тарифов, у которых задана цена для ЮKassa.
    Используется для построения кнопок оплаты картой и получения сумм.
    """
    sql = """
    SELECT
        code,
        title,
        duration_days,
        yookassa_amount,
        sort_order
    FROM tariffs
    WHERE is_active = TRUE
      AND yookassa_amount IS NOT NULL
    ORDER BY sort_order ASC, id ASC;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            return rows


def get_tariffs_for_heleket() -> List[Dict[str, Any]]:
    """
    Возвращает список активных тарифов, у которых задана цена для Heleket.
    Используется для построения кнопок оплаты криптой и получения сумм.
    """
    sql = """
    SELECT
        code,
        title,
        duration_days,
        heleket_amount,
        sort_order
    FROM tariffs
    WHERE is_active = TRUE
      AND heleket_amount IS NOT NULL
    ORDER BY sort_order ASC, id ASC;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            return rows


def get_tariffs_for_points() -> List[Dict[str, Any]]:
    """
    Возвращает список активных тарифов, у которых задана цена в бонусных баллах.
    Используется для построения кнопок оплаты подписки баллами.
    """
    sql = """
    SELECT
        code,
        title,
        duration_days,
        points_cost,
        sort_order
    FROM tariffs
    WHERE is_active = TRUE
      AND points_cost IS NOT NULL
    ORDER BY sort_order ASC, id ASC;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            return rows


def get_yookassa_tariff_by_code(code: str) -> Optional[Dict[str, Any]]:
    """
    Возвращает один тариф по code для оплаты ЮKassa.
    """
    sql = """
    SELECT
        code,
        title,
        duration_days,
        yookassa_amount
    FROM tariffs
    WHERE code = %s
      AND is_active = TRUE
      AND yookassa_amount IS NOT NULL
    LIMIT 1;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (code,))
            row = cur.fetchone()
            if not row:
                return None
            return row


def get_heleket_tariff_by_code(code: str) -> Optional[Dict[str, Any]]:
    """
    Возвращает один тариф по code для оплаты в Heleket.
    """
    sql = """
    SELECT
        code,
        title,
        duration_days,
        heleket_amount
    FROM tariffs
    WHERE code = %s
      AND is_active = TRUE
      AND heleket_amount IS NOT NULL
    LIMIT 1;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (code,))
            row = cur.fetchone()
            if not row:
                return None
            return row


def get_points_tariff_by_code(code: str) -> Optional[Dict[str, Any]]:
    """
    Возвращает один тариф по code для оплаты бонусными баллами.
    """
    sql = """
    SELECT
        code,
        title,
        duration_days,
        points_cost
    FROM tariffs
    WHERE code = %s
      AND is_active = TRUE
      AND points_cost IS NOT NULL
    LIMIT 1;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (code,))
            row = cur.fetchone()
            if not row:
                return None
            return row


def get_tariff_for_referral_by_code(code: str) -> Optional[Dict[str, Any]]:
    """
    Возвращает тариф по code для расчёта реферальных бонусов.
    Нужны поля ref_base_bonus_points и ref_enabled.
    """
    sql = """
    SELECT
        code,
        title,
        duration_days,
        ref_base_bonus_points,
        ref_enabled
    FROM tariffs
    WHERE code = %s
      AND is_active = TRUE
    LIMIT 1;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (code,))
            row = cur.fetchone()
            if not row:
                return None
            return row


def get_referral_levels() -> Dict[int, Dict[str, Any]]:
    """
    Возвращает словарь уровней рефералки:
    {
        1: {"multiplier": 1.0, "is_active": True},
        2: {"multiplier": 0.5, "is_active": True},
        ...
    }
    """
    sql = """
    SELECT level, multiplier, is_active
    FROM referral_levels
    ORDER BY level ASC;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()

    levels: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        try:
            level_int = int(row["level"])
        except (TypeError, ValueError):
            continue

        multiplier_val = row.get("multiplier")
        try:
            multiplier_float = float(multiplier_val) if multiplier_val is not None else 0.0
        except (TypeError, ValueError):
            multiplier_float = 0.0

        levels[level_int] = {
            "multiplier": multiplier_float,
            "is_active": bool(row.get("is_active")),
        }
    return levels


def get_referrer_telegram_id(
    referred_telegram_user_id: int,
) -> Optional[int]:
    """
    Возвращает telegram_user_id прямого реферера (1-я линия) для указанного пользователя,
    либо None, если реферера нет.
    """
    sql = """
    SELECT referrer_telegram_user_id
    FROM referrals
    WHERE referred_telegram_user_id = %s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (referred_telegram_user_id,))
            row = cur.fetchone()
            if not row:
                return None
            referrer_id = row[0]
            if referrer_id is None:
                return None
            try:
                return int(referrer_id)
            except (TypeError, ValueError):
                return None


def get_referral_upline_chain(
    referred_telegram_user_id: int,
    max_levels: int = 5,
) -> List[int]:
    """
    Строит цепочку рефереров сверху вниз для указанного пользователя.

    Пример:
        user -> [ref_lvl_1, ref_lvl_2, ref_lvl_3, ...]

    Возвращает список telegram_user_id для уровней 1..max_levels.
    Если кто-то в цепочке отсутствует — дальше не идём.
    """
    chain: List[int] = []
    current_id: Optional[int] = referred_telegram_user_id

    with get_conn() as conn:
        with conn.cursor() as cur:
            for _ in range(max_levels):
                if current_id is None:
                    break

                sql = """
                SELECT referrer_telegram_user_id
                FROM referrals
                WHERE referred_telegram_user_id = %s;
                """
                cur.execute(sql, (current_id,))
                row = cur.fetchone()
                if not row:
                    break

                referrer_id = row[0]
                if referrer_id is None:
                    break

                try:
                    referrer_int = int(referrer_id)
                except (TypeError, ValueError):
                    break

                chain.append(referrer_int)
                current_id = referrer_int

    return chain


def create_referral_link(
    referred_telegram_user_id: int,
    referrer_telegram_user_id: int,
) -> Dict[str, Any]:
    """
    Создаёт запись в referrals (кто кого привёл).

    Ограничения:
    - один referred_telegram_user_id может иметь только одного реферера;
    - нельзя указывать себя самого в качестве реферера;
    - если запись уже есть — возвращаем ошибку "already_has_referrer".
    """
    result: Dict[str, Any] = {
        "ok": False,
        "error": None,
        "error_message": None,
    }

    if referred_telegram_user_id == referrer_telegram_user_id:
        result["error"] = "self_ref"
        result["error_message"] = "Нельзя указать себя самого как реферера."
        return result

    with get_conn() as conn:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Проверяем, что у пользователя ещё нет реферера
                sql_check = """
                SELECT referrer_telegram_user_id
                FROM referrals
                WHERE referred_telegram_user_id = %s
                FOR UPDATE;
                """
                cur.execute(sql_check, (referred_telegram_user_id,))
                row = cur.fetchone()
                if row is not None:
                    result["error"] = "already_has_referrer"
                    result["error_message"] = "Реферер для этого пользователя уже задан."
                    return result

                # Вставляем запись
                sql_insert = """
                INSERT INTO referrals (
                    referred_telegram_user_id,
                    referrer_telegram_user_id,
                    created_at
                )
                VALUES (%s, %s, NOW());
                """
                cur.execute(
                    sql_insert,
                    (referred_telegram_user_id, referrer_telegram_user_id),
                )

            conn.commit()
            result["ok"] = True
            return result

        except Exception as e:
            conn.rollback()
            result["error"] = "db_error"
            result["error_message"] = f"Ошибка при работе с базой данных: {e!r}"
            return result


def get_referral_code_by_code(code: str) -> Optional[Dict[str, Any]]:
    """
    Возвращает запись из referral_codes по коду (только активные).
    """
    normalized_code = (code or "").strip()
    if not normalized_code:
        return None

    sql = """
    SELECT
        code,
        referrer_telegram_user_id,
        is_active,
        created_at
    FROM referral_codes
    WHERE code = %s
      AND is_active = TRUE
    LIMIT 1;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (normalized_code,))
            row = cur.fetchone()
            if not row:
                return None
            return row


def create_or_get_referral_code(
    referrer_telegram_user_id: int,
) -> Dict[str, Any]:
    """
    Возвращает существующий активный реферальный код для пользователя
    или создаёт новый.

    Сейчас код генерится в формате "REF<telegram_id>".
    Если вдруг такой код уже занят другим пользователем (маловероятно),
    добавляем числовой суффикс.
    """
    result: Dict[str, Any] = {
        "ok": False,
        "error": None,
        "error_message": None,
        "code": None,
        "referrer_telegram_user_id": referrer_telegram_user_id,
    }

    with get_conn() as conn:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 1) Пытаемся найти уже существующий активный код этого пользователя
                sql_select = """
                SELECT code
                FROM referral_codes
                WHERE referrer_telegram_user_id = %s
                  AND is_active = TRUE
                ORDER BY created_at ASC
                LIMIT 1;
                """
                cur.execute(sql_select, (referrer_telegram_user_id,))
                row = cur.fetchone()
                if row is not None:
                    result["ok"] = True
                    result["code"] = row["code"]
                    return result

                # 2) Активного кода нет — генерим новый
                base_code = f"REF{referrer_telegram_user_id}"
                candidate = base_code
                attempt = 0

                while True:
                    try:
                        sql_insert = """
                        INSERT INTO referral_codes (
                            code,
                            referrer_telegram_user_id,
                            is_active,
                            created_at
                        )
                        VALUES (%s, %s, TRUE, NOW())
                        RETURNING code;
                        """
                        cur.execute(
                            sql_insert,
                            (candidate, referrer_telegram_user_id),
                        )
                        inserted = cur.fetchone()
                        if inserted is None:
                            raise RuntimeError("Failed to insert referral code")
                        final_code = inserted["code"]
                        break
                    except psycopg2.IntegrityError:
                        # Конфликт по PRIMARY KEY (code) — генерим новый вариант
                        conn.rollback()
                        attempt += 1
                        candidate = f"{base_code}_{attempt}"
                        continue

            conn.commit()
            result["ok"] = True
            result["code"] = final_code
            return result

        except Exception as e:
            conn.rollback()
            result["error"] = "db_error"
            result["error_message"] = f"Ошибка при работе с базой данных: {e!r}"
            return result


def ensure_user_profile(
    telegram_user_id: int,
) -> None:
    """
    Гарантирует наличие записи в user_profiles для указанного пользователя.
    Если записи нет — создаёт её с дефолтными значениями.
    """
    sql = """
    INSERT INTO user_profiles (telegram_user_id, is_referral_blocked, is_banned, created_at, updated_at)
    VALUES (%s, FALSE, FALSE, NOW(), NOW())
    ON CONFLICT (telegram_user_id) DO NOTHING;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (telegram_user_id,))
        conn.commit()


def get_user_profile(
    telegram_user_id: int,
) -> Optional[Dict[str, Any]]:
    """
    Возвращает профиль пользователя из user_profiles.
    Если записи нет — возвращает None.
    """
    sql = """
    SELECT
        telegram_user_id,
        is_referral_blocked,
        is_banned,
        created_at,
        updated_at
    FROM user_profiles
    WHERE telegram_user_id = %s;
    """
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (telegram_user_id,))
            row = cur.fetchone()
            if not row:
                return None
            return row


def is_user_referral_blocked(
    telegram_user_id: int,
) -> bool:
    """
    Возвращает True, если для пользователя включён флаг is_referral_blocked.
    Если записи в user_profiles нет — считаем, что блокировки нет (False).
    """
    profile = get_user_profile(telegram_user_id=telegram_user_id)
    if profile is None:
        return False
    return bool(profile.get("is_referral_blocked"))


def set_user_referral_blocked(
    telegram_user_id: int,
    blocked: bool,
) -> None:
    """
    Устанавливает флаг is_referral_blocked для пользователя.
    При необходимости создаёт запись в user_profiles.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            sql = """
            INSERT INTO user_profiles (telegram_user_id, is_referral_blocked, is_banned, created_at, updated_at)
            VALUES (%s, %s, FALSE, NOW(), NOW())
            ON CONFLICT (telegram_user_id) DO UPDATE
            SET is_referral_blocked = EXCLUDED.is_referral_blocked,
                updated_at = NOW();
            """
            cur.execute(sql, (telegram_user_id, blocked))
        conn.commit()


def apply_referral_rewards_for_subscription(
    payer_telegram_user_id: int,
    subscription_id: int,
    tariff_code: str,
    payment_source: str,
    payment_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Начисляет реферальные бонусы (поинты) за оплату подписки.

    Логика:
    - берём тариф по коду, проверяем ref_enabled и ref_base_bonus_points;
    - строим цепочку рефереров 1..5 уровней;
    - берём коэффициенты из referral_levels;
    - для каждого уровня считаем бонус и вызываем add_points(...) с reason='ref_level_<N>'.

    ВАЖНО:
    - если тариф не даёт бонусов или нет рефереров — возвращаем ok=True, но без начислений;
    - если payer помечен is_referral_blocked = TRUE — бонусы не начисляем.
    """
    result: Dict[str, Any] = {
        "ok": True,
        "skipped": None,
        "awards": [],  # список начислений по уровням
        "error": None,
        "error_message": None,
    }

    # 1) Проверяем, не заблокирован ли плательщик для рефералки
    if is_user_referral_blocked(payer_telegram_user_id):
        result["skipped"] = "payer_referral_blocked"
        return result

    # 2) Тариф для рефералки
    tariff = get_tariff_for_referral_by_code(code=tariff_code)
    if not tariff:
        result["skipped"] = "tariff_not_found_or_inactive"
        return result

    ref_enabled = bool(tariff.get("ref_enabled"))
    base_bonus = tariff.get("ref_base_bonus_points")

    try:
        base_bonus_int = int(base_bonus) if base_bonus is not None else 0
    except (TypeError, ValueError):
        base_bonus_int = 0

    if not ref_enabled or base_bonus_int <= 0:
        result["skipped"] = "tariff_referral_disabled_or_zero_bonus"
        return result

    # 3) Цепочка рефереров
    upline = get_referral_upline_chain(
        referred_telegram_user_id=payer_telegram_user_id,
        max_levels=5,
    )
    if not upline:
        result["skipped"] = "no_referrers"
        return result

    # 4) Уровни рефералки
    levels_cfg = get_referral_levels()
    if not levels_cfg:
        result["skipped"] = "no_referral_levels"
        return result

    awards: List[Dict[str, Any]] = []

    for level_idx, referrer_id in enumerate(upline, start=1):
        level_cfg = levels_cfg.get(level_idx)
        if level_cfg is None:
            # уровень не настроен — пропускаем
            continue

        if not level_cfg.get("is_active"):
            # уровень выключен
            continue

        multiplier = level_cfg.get("multiplier") or 0.0
        try:
            multiplier_float = float(multiplier)
        except (TypeError, ValueError):
            multiplier_float = 0.0

        if multiplier_float <= 0.0:
            continue

        # Считаем бонус для этого уровня
        bonus_raw = base_bonus_int * multiplier_float
        try:
            bonus_int = int(round(bonus_raw))
        except (TypeError, ValueError):
            bonus_int = 0

        if bonus_int <= 0:
            continue

        # Начисляем поинты рефереру
        meta: Dict[str, Any] = {
            "tariff_code": tariff_code,
            "payer_telegram_user_id": payer_telegram_user_id,
        }

        add_res = add_points(
            telegram_user_id=referrer_id,
            delta=bonus_int,
            reason=f"ref_level_{level_idx}",
            source=payment_source,
            related_subscription_id=subscription_id,
            related_payment_id=payment_id,
            level=level_idx,
            meta=meta,
            allow_negative=False,
        )

        awards.append(
            {
                "level": level_idx,
                "referrer_telegram_user_id": referrer_id,
                "bonus": bonus_int,
                "add_points_result": add_res,
            }
        )

    result["awards"] = awards
    return result


def register_referral_start(
    invited_telegram_user_id: int,
    referral_code: str,
    raw_start_param: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Регистрирует факт захода по реферальному коду (deep-link /start <code>).

    Логика:
    - по коду ищем запись в referral_codes (только активные);
    - проверяем, что приглашённый не равен рефереру;
    - создаём связь в referrals (кто кого привёл) через create_referral_link;
    - если реферер уже есть, self-ref и т.п. — не падаем, а просто возвращаем ошибку в result.

    raw_start_param сейчас никуда не пишется, оставлен на будущее для логов.
    """
    result: Dict[str, Any] = {
        "ok": False,
        "error": None,
        "error_message": None,
        "referrer_telegram_user_id": None,
    }

    normalized_code = (referral_code or "").strip()
    if not normalized_code:
        result["error"] = "empty_code"
        result["error_message"] = "Пустой реферальный код."
        return result

    # Находим код в таблице referral_codes (используем уже готовую функцию)
    code_row = get_referral_code_by_code(normalized_code)
    if code_row is None:
        result["error"] = "code_not_found_or_inactive"
        result["error_message"] = "Реферальный код не найден или неактивен."
        return result

    referrer_id = code_row.get("referrer_telegram_user_id")
    if referrer_id is None:
        result["error"] = "invalid_referrer"
        result["error_message"] = "У реферального кода не указан владелец."
        return result

    try:
        referrer_int = int(referrer_id)
    except (TypeError, ValueError):
        result["error"] = "invalid_referrer"
        result["error_message"] = "Невалидный идентификатор реферера."
        return result

    # Нельзя быть реферером самому себе
    if int(invited_telegram_user_id) == referrer_int:
        result["error"] = "self_ref"
        result["error_message"] = "Пользователь не может быть своим же реферером."
        return result

    # Пытаемся создать связь в referrals
    link_res = create_referral_link(
        referred_telegram_user_id=invited_telegram_user_id,
        referrer_telegram_user_id=referrer_int,
    )

    if not link_res.get("ok"):
        # Если уже есть реферер — это не критическая ошибка, просто возвращаем инфу
        result["error"] = link_res.get("error") or "link_failed"
        result["error_message"] = link_res.get("error_message") or "Не удалось создать реферальную связь."
        result["referrer_telegram_user_id"] = referrer_int
        return result

    result["ok"] = True
    result["referrer_telegram_user_id"] = referrer_int
    return result


def get_or_create_referral_info(
    telegram_user_id: int,
    telegram_username: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Возвращает информацию для /ref:

    {
        "ok": True,
        "ref_code": "REF123456789",
        "invited_count": 10,
        "paid_referrals_count": 3,
        "invited_by_levels": {1: 7, 2: 3},
        "paid_by_levels": {1: 2, 2: 1},
    }

    Логика:
    - гарантируем наличие user_profile;
    - получаем (или создаём) активный реферальный код;
    - считаем, сколько людей пользователь привёл по 1-й линии;
    - считаем, сколько из них оплатили (по 1-й линии);
    - дополнительно строим дерево downline до 5-го уровня и считаем по уровням:
      * invited_by_levels[level]  — сколько приглашённых на уровне;
      * paid_by_levels[level]     — сколько из них оплатили.
    """
    # На всякий случай гарантируем наличие записи профиля
    try:
        ensure_user_profile(telegram_user_id=telegram_user_id)
    except Exception:
        # Профиль — необязательная часть, не блокируем работу /ref
        pass

    # Получаем или создаём реферальный код
    code_res = create_or_get_referral_code(referrer_telegram_user_id=telegram_user_id)
    ref_code: Optional[str] = None
    if code_res.get("ok"):
        ref_code = code_res.get("code")

    invited_count = 0
    paid_referrals_count = 0

    # Для уровней
    max_levels = 5
    invited_by_levels: Dict[int, int] = {}
    paid_by_levels: Dict[int, int] = {}

    with get_conn() as conn:
        with conn.cursor() as cur:
            # --------- 1. Сколько людей по 1-й линии ---------
            sql_invited = """
            SELECT COUNT(*) AS cnt
            FROM referrals
            WHERE referrer_telegram_user_id = %s;
            """
            cur.execute(sql_invited, (telegram_user_id,))
            row = cur.fetchone()
            if row is not None and row[0] is not None:
                try:
                    invited_count = int(row[0])
                except (TypeError, ValueError):
                    invited_count = 0

            # --------- 2. Сколько из приглашённых оплатили (1-я линия) ---------
            sql_paid = """
            SELECT COUNT(DISTINCT r.referred_telegram_user_id) AS cnt
            FROM referrals r
            JOIN vpn_subscriptions s
              ON s.telegram_user_id = r.referred_telegram_user_id
            WHERE r.referrer_telegram_user_id = %s
              AND (
                    s.last_event_name LIKE 'yookassa_payment_succeeded_%%'
                 OR s.last_event_name LIKE 'heleket_payment_paid_%%'
              );
            """
            cur.execute(sql_paid, (telegram_user_id,))
            row2 = cur.fetchone()
            if row2 is not None and row2[0] is not None:
                try:
                    paid_referrals_count = int(row2[0])
                except (TypeError, ValueError):
                    paid_referrals_count = 0

        # --------- 3. Строим дерево рефералов до 5-го уровня ---------
        # Загрузим все связи в память: referrer -> [referred...]
        children_map: Dict[int, List[int]] = {}

        with conn.cursor() as cur2:
            sql_all_ref = """
            SELECT referred_telegram_user_id, referrer_telegram_user_id
            FROM referrals;
            """
            cur2.execute(sql_all_ref)
            rows = cur2.fetchall()

            for referred_id, referrer_id in rows:
                try:
                    referrer_int = int(referrer_id)
                    referred_int = int(referred_id)
                except (TypeError, ValueError):
                    continue

                children = children_map.get(referrer_int)
                if children is None:
                    children = []
                    children_map[referrer_int] = children
                children.append(referred_int)

        # BFS по уровням
        visited: set[int] = set()
        current_level_users: List[int] = children_map.get(telegram_user_id, []).copy()
        level = 1

        # Подготовим данные для подсчёта оплат по уровням:
        users_by_level: Dict[int, List[int]] = {}

        while level <= max_levels and current_level_users:
            # убираем дубликаты и уже пройденных
            unique_users: List[int] = []
            for uid in current_level_users:
                if uid in visited:
                    continue
                visited.add(uid)
                unique_users.append(uid)

            if not unique_users:
                break

            invited_by_levels[level] = len(unique_users)
            users_by_level[level] = unique_users

            # формируем следующий уровень
            next_level_users: List[int] = []
            for uid in unique_users:
                childs = children_map.get(uid)
                if childs:
                    next_level_users.extend(childs)

            current_level_users = next_level_users
            level += 1

        # --------- 4. Считаем, сколько оплатили на каждом уровне ---------
        with conn.cursor() as cur3:
            for lvl, uids in users_by_level.items():
                if not uids:
                    paid_by_levels[lvl] = 0
                    continue

                # используем ANY(%s) для списка
                sql_paid_lvl = """
                SELECT COUNT(DISTINCT s.telegram_user_id) AS cnt
                FROM vpn_subscriptions s
                WHERE s.telegram_user_id = ANY(%s)
                  AND (
                        s.last_event_name LIKE 'yookassa_payment_succeeded_%%'
                     OR s.last_event_name LIKE 'heleket_payment_paid_%%'
                  );
                """
                cur3.execute(sql_paid_lvl, (uids,))
                row = cur3.fetchone()
                lvl_cnt = 0
                if row is not None and row[0] is not None:
                    try:
                        lvl_cnt = int(row[0])
                    except (TypeError, ValueError):
                        lvl_cnt = 0
                paid_by_levels[lvl] = lvl_cnt

    return {
        "ok": True,
        "ref_code": ref_code,
        "invited_count": invited_count,
        "paid_referrals_count": paid_referrals_count,
        "invited_by_levels": invited_by_levels,
        "paid_by_levels": paid_by_levels,
    }
