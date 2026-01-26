import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from datetime import datetime
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
) -> None:
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
    );
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
        conn.commit()



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
