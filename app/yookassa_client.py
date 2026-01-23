import os
import uuid
import logging
from typing import Dict, Any

import requests


logger = logging.getLogger(__name__)

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "https://t.me/MaxNet_VPN_bot")
YOOKASSA_API_URL = "https://api.yookassa.ru/v3/payments"


def create_yookassa_payment(
    telegram_user_id: int,
    tariff_code: str,
    amount: str,
    description: str,
) -> str:
    """
    Создаёт платёж в ЮKassa и возвращает confirmation_url,
    на который нужно отправить пользователя для оплаты.
    """
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise RuntimeError(
            "YOOKASSA_SHOP_ID и/или YOOKASSA_SECRET_KEY не заданы в переменных окружения"
        )

    idempotence_key = uuid.uuid4().hex

    payload: Dict[str, Any] = {
        "amount": {
            "value": amount,          # строка, например "200.00"
            "currency": "RUB",
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": YOOKASSA_RETURN_URL,
        },
        "description": description[:128],
        "metadata": {
            "telegram_user_id": telegram_user_id,
            "tariff_code": tariff_code,
        },
    }

    headers = {
        "Content-Type": "application/json",
        "Idempotence-Key": idempotence_key,
    }

    auth = (YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

    response = requests.post(
        YOOKASSA_API_URL,
        json=payload,
        headers=headers,
        auth=auth,
        timeout=10,
    )

    if response.status_code not in (200, 201):
        logger.error(
            "[YooKassa] create_payment failed: status=%s body=%s",
            response.status_code,
            response.text,
        )
        raise RuntimeError("ЮKassa вернула ошибку при создании платежа")

    data = response.json()
    confirmation = data.get("confirmation") or {}
    confirmation_url = confirmation.get("confirmation_url")

    if not confirmation_url:
        logger.error(
            "[YooKassa] No confirmation_url in response: %s",
            response.text,
        )
        raise RuntimeError("Не удалось получить ссылку на оплату из ответа ЮKassa")

    logger.info(
        "[YooKassa] Payment created: id=%s tg_id=%s tariff=%s amount=%s",
        data.get("id"),
        telegram_user_id,
        tariff_code,
        amount,
    )

    return confirmation_url
