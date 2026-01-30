import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from .config import settings


@contextmanager
def get_conn():
    conn = psycopg2.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        dbname=settings.DB_NAME,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        sslmode="disable",  # добавили эту строчку
    )
    try:
        yield conn
    finally:
        conn.close()



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
    Возвращает True, если строка была удалена.
    """
    sql = """
    DELETE FROM vpn_subscriptions
    WHERE id = %s;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (sub_id,))
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
