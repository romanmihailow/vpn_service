# /home/vpn_service/app/promo_codes.py

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from .logger import get_heleket_logger


log = get_heleket_logger()

# Алфавит для случайных промокодов (без похожих символов типа O/0, I/1)
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@dataclass
class PromoGenerationParams:
    """
    Параметры генерации промокодов.

    action_type:
        тип действия промокода, сейчас используем "extra_days"
    extra_days:
        сколько дополнительных дней даёт промокод
    is_multi_use:
        True  -> один многоразовый код (manual_code обязателен)
        False -> много одноразовых кодов (code_count штук)
    code_count:
        для одноразовых промокодов — сколько штук сгенерировать
    manual_code:
        для многоразового промокода — конкретное имя кода (например MAXNET7DAYS)
    valid_days:
        срок действия промокода в днях с текущего момента
        0 -> без ограничения по дате (valid_until = NULL)
    max_uses:
        общий лимит применений промокода
        None -> без лимита по общему числу применений
    per_user_limit:
        сколько раз один пользователь может применить промокод
    tariff_scope:
        "all"      -> на все тарифы
        "selected" -> только на указанные в allowed_tariffs
    allowed_tariffs:
        список кодов тарифов (например ["1m", "3m"]), если tariff_scope="selected"
    allowed_telegram_id:
        если задан, промокод можно применить только с этим telegram_user_id
    comment:
        комментарий для админа
    created_by_admin_id:
        Telegram ID админа, который создавал промокод (для аудита)
    code_length:
        длина случайного промокода для одноразовых кодов
    """

    action_type: str
    extra_days: int
    is_multi_use: bool
    code_count: int
    manual_code: Optional[str]
    valid_days: int
    max_uses: Optional[int]
    per_user_limit: int
    tariff_scope: str
    allowed_tariffs: Optional[Sequence[str]]
    allowed_telegram_id: Optional[int]
    comment: Optional[str]
    created_by_admin_id: Optional[int]
    code_length: int = 10


def generate_random_code(length: int) -> str:
    """
    Генерация одного случайного промокода указанной длины.
    """
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def normalize_manual_code(code: str) -> str:
    """
    Нормализация ручного кода:
      - обрезаем пробелы по краям
      - переводим в верхний регистр
      - пробелы внутри заменяем на подчёркивания
    """
    result = code.strip().upper().replace(" ", "_")
    return result


def generate_promo_codes(params: PromoGenerationParams) -> List[dict]:
    """
    Генерирует список словарей с данными промокодов, которые
    можно напрямую класть в БД или использовать для построения INSERT-ов.

    Каждый элемент списка соответствует одной строке в таблице promo_codes
    (без поля id).
    """
    if params.extra_days <= 0:
        raise ValueError("extra_days must be > 0")

    if not params.is_multi_use and params.code_count <= 0:
        raise ValueError("code_count must be > 0 for one-time promo codes")

    if params.per_user_limit <= 0:
        raise ValueError("per_user_limit must be > 0")

    if params.tariff_scope not in ("all", "selected"):
        raise ValueError("tariff_scope must be 'all' or 'selected'")

    now = datetime.now(timezone.utc)

    if params.valid_days > 0:
        valid_until = now + timedelta(days=params.valid_days)
    else:
        valid_until = None

    codes: List[dict] = []

    if params.is_multi_use:
        # МНОГОРАЗОВЫЙ ПРОМОКОД (одна запись в БД, один код)
        if not params.manual_code:
            raise ValueError("manual_code is required for multi-use promo code")

        code_value = normalize_manual_code(params.manual_code)

        row = {
            "code": code_value,
            "action_type": params.action_type,
            "extra_days": params.extra_days,
            "is_multi_use": True,
            "max_uses": params.max_uses,
            "per_user_limit": params.per_user_limit,
            "used_count": 0,
            "valid_from": now,
            "valid_until": valid_until,
            "tariff_scope": params.tariff_scope,
            "allowed_tariffs": list(params.allowed_tariffs) if params.allowed_tariffs is not None else None,
            "allowed_telegram_id": params.allowed_telegram_id,
            "is_active": True,
            "comment": params.comment,
            "created_at": now,
            "created_by_admin_id": params.created_by_admin_id,
        }
        codes.append(row)
    else:
        # ОДНОРАЗОВЫЕ ПРОМОКОДЫ (несколько записей в БД, каждый со своим случайным code)
        for _ in range(params.code_count):
            code_value = generate_random_code(params.code_length)
            row = {
                "code": code_value,
                "action_type": params.action_type,
                "extra_days": params.extra_days,
                "is_multi_use": False,
                "max_uses": 1,
                "per_user_limit": 1,
                "used_count": 0,
                "valid_from": now,
                "valid_until": valid_until,
                "tariff_scope": params.tariff_scope,
                "allowed_tariffs": list(params.allowed_tariffs) if params.allowed_tariffs is not None else None,
                "allowed_telegram_id": params.allowed_telegram_id,
                "is_active": True,
                "comment": params.comment,
                "created_at": now,
                "created_by_admin_id": params.created_by_admin_id,
            }
            codes.append(row)

    log.info(
        "[PromoCodes] Generated %s promo codes (multi_use=%s action_type=%s extra_days=%s)",
        len(codes),
        params.is_multi_use,
        params.action_type,
        params.extra_days,
    )

    return codes


def _quote_pg_value(value: object) -> str:
    """
    Простая экранизация значений для генерации INSERT-ов Postgres.
    Не для произвольного юзерского ввода, а чтобы админ мог
    быстро получить корректный SQL для вставки в таблицу promo_codes.
    """
    if value is None:
        return "NULL"

    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"

    if isinstance(value, int):
        return str(value)

    if isinstance(value, float):
        return repr(value)

    if isinstance(value, datetime):
        iso_str = value.isoformat()
        safe = iso_str.replace("'", "''")
        return "'" + safe + "'::timestamptz"

    if isinstance(value, list):
        if len(value) == 0:
            return "ARRAY[]::text[]"

        escaped_items: List[str] = []
        for item in value:
            if item is None:
                escaped_items.append("NULL")
            else:
                text_item = str(item)
                text_item = text_item.replace("\\", "\\\\")
                text_item = text_item.replace('"', '\\"')
                escaped_items.append('"' + text_item + '"')

        # Пример результата: '{"1m","3m"}'::text[]
        return "'{" + ",".join(escaped_items) + "}'::text[]"

    text = str(value)
    safe = text.replace("'", "''")
    return "'" + safe + "'"


def build_insert_sql_for_postgres(promo_rows: List[dict], table_name: str = "promo_codes") -> str:
    """
    Принимает список словарей (как вернул generate_promo_codes)
    и строит один большой INSERT INTO ... VALUES (...),(...);

    Возвращает строку с SQL, которую можно отдать админу Postgres.
    """
    columns = [
        "code",
        "action_type",
        "extra_days",
        "is_multi_use",
        "max_uses",
        "per_user_limit",
        "used_count",
        "valid_from",
        "valid_until",
        "tariff_scope",
        "allowed_tariffs",
        "allowed_telegram_id",
        "is_active",
        "comment",
        "created_at",
        "created_by_admin_id",
    ]

    lines: List[str] = []
    lines.append("INSERT INTO " + table_name + " (" + ", ".join(columns) + ")")

    values_sql_parts: List[str] = []

    for row in promo_rows:
        row_values_sql: List[str] = []
        for column_name in columns:
            value = row.get(column_name)
            row_values_sql.append(_quote_pg_value(value))
        values_sql_parts.append("    (" + ", ".join(row_values_sql) + ")")

    lines.append("VALUES")
    lines.append(",\n".join(values_sql_parts) + ";")

    sql = "\n".join(lines)
    return sql


if __name__ == "__main__":
    """
    Пример самостоятельного запуска:

      python -m app.promo_codes

    Выведет в stdout готовый INSERT для 5 одноразовых промокодов.
    """
    example_params = PromoGenerationParams(
        action_type="extra_days",
        extra_days=7,
        is_multi_use=False,
        code_count=5,
        manual_code=None,
        valid_days=30,
        max_uses=None,
        per_user_limit=1,
        tariff_scope="all",
        allowed_tariffs=None,
        allowed_telegram_id=None,
        comment="Пример генерации 5 одноразовых промокодов +7 дней, действует 30 дней.",
        created_by_admin_id=None,
        code_length=10,
    )

    promo_rows = generate_promo_codes(example_params)
    sql = build_insert_sql_for_postgres(promo_rows)
    print(sql)
