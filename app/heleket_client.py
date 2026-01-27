import os
import json
import base64
import hashlib
import requests

from .config import settings
from .logger import get_heleket_logger

log = get_heleket_logger()


HELEKET_API_BASE_URL = getattr(
    settings,
    "HELEKET_API_BASE_URL",
    "https://api.heleket.com",
)

HELEKET_API_KEY = os.getenv("HELEKET_API_KEY")
HELEKET_MERCHANT_ID = os.getenv("HELEKET_MERCHANT_ID")



def _build_heleket_sign(payload: dict) -> str:
    """
    Генерация подписи для Heleket API по правилам из документации:

      hash = md5( base64_encode( json_encode(data, JSON_UNESCAPED_UNICODE) ) . apiKey )

    Важно: слэши '/' должны быть экранированы как '\/' перед base64.
    """
    # json без пробелов, без ASCII-экранирования
    json_str = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # экранируем '/'
    json_str = json_str.replace("/", "\\/")

    b64 = base64.b64encode(json_str.encode("utf-8")).decode("ascii")
    to_hash = (b64 + (HELEKET_API_KEY or "")).encode("utf-8")
    return hashlib.md5(to_hash).hexdigest()


def create_heleket_payment(
    telegram_user_id: int,
    tariff_code: str,
    amount: str,
    description: str,
) -> str:
    """
    Создаёт платёж в Heleket и возвращает URL, по которому пользователь может оплатить.

    amount — строка, например "100.00".
    Валюта и дополнительные параметры зависят от настроек в личном кабинете Heleket.
    """
    if not HELEKET_API_KEY or not HELEKET_MERCHANT_ID:
        raise RuntimeError(
            "HELEKET_API_KEY или HELEKET_MERCHANT_ID не заданы в конфиге.",
        )

    api_url = HELEKET_API_BASE_URL.rstrip("/") + "/v1/payment"

    order_id = f"maxnet_{telegram_user_id}_{tariff_code}"

    payload = {
        "merchant_id": HELEKET_MERCHANT_ID,
        "order_id": order_id,
        "amount": amount,
        # при необходимости поменяешь валюту под свои настройки (RUB / USD / USDT и т.п.)
        "currency": "USD",
        "description": description,
        "metadata": {
            "telegram_user_id": str(telegram_user_id),
            "tariff_code": tariff_code,
        },
    }

    # === формируем подпись по доке Heleket ===
    # json без sign, как JSON_UNESCAPED_UNICODE в PHP
    json_str = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),  # без пробелов, как в PHP по умолчанию
    )
    # экранируем слэши, как в их примере
    json_str = json_str.replace("/", "\\/")

    b64 = base64.b64encode(json_str.encode("utf-8")).decode("ascii")
    raw_to_hash = (b64 + HELEKET_API_KEY).encode("utf-8")
    sign = hashlib.md5(raw_to_hash).hexdigest()

    headers = {
        "merchant": HELEKET_MERCHANT_ID,
        "sign": sign,
        "Content-Type": "application/json",
    }

    log.info(
        "[Heleket] Create payment tg_id=%s tariff=%s amount=%s %s order_id=%s",
        telegram_user_id,
        tariff_code,
        amount,
        payload["currency"],
        order_id,
    )

    # логируем URL и заголовки без реального sign
    safe_headers = {
        k: ("***" if k.lower() == "sign" else v)
        for k, v in headers.items()
    }
    log.info(
        "[Heleket] Request: url=%s headers=%r payload=%r",
        api_url,
        safe_headers,
        payload,
    )

    resp = requests.post(
        api_url,
        json=payload,
        headers=headers,
        timeout=15,
    )

    # сначала проверяем статус, чтобы не пытаться парсить HTML-страницу ошибки как JSON
    if resp.status_code != 200:
        log.error(
            "[Heleket] Non-200 response: status=%s body=%r",
            resp.status_code,
            resp.text,
        )
        raise RuntimeError(f"Heleket API error: {resp.status_code}")

    try:
        data = resp.json()
    except Exception as e:
        log.error(
            "[Heleket] Failed to parse JSON response: status=%s body=%r error=%r",
            resp.status_code,
            resp.text,
            e,
        )
        raise RuntimeError("Failed to create Heleket payment (bad JSON).")

    payment_url = (
        data.get("payment_url")
        or data.get("url")
        or data.get("paymentUrl")
    )

    if not payment_url:
        log.error("[Heleket] No payment URL in response: %r", data)
        raise RuntimeError("Heleket API did not return payment URL.")

    log.info(
        "[Heleket] Payment created order_id=%s url=%s raw=%r",
        order_id,
        payment_url,
        data,
    )

    return payment_url

