import asyncio
from datetime import datetime, timedelta
import json
import hmac
import hashlib

from aiohttp import web

from . import db, wg
from .bot import send_vpn_config_to_user
from .config import settings
from .logger import get_logger
from .tg_bot_runner import deactivate_existing_active_subscriptions


log = get_logger()

# Сколько дней даёт каждый тариф ЮKassa
TARIFF_DAYS = {
    "1m": 30,
    "3m": 90,
    "6m": 180,
    "1y": 365,
    # формально "навсегда" — здесь ставим большой срок, например 10 лет
    "forever": 3650,
}




def verify_yookassa_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """
    Проверка подписи вебхука ЮKassa.
    """
    if not signature_header:
        return False

    secret = settings.YOOKASSA_WEBHOOK_SECRET
    if not secret:
        return False

    digest = hmac.new(
        key=secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(digest, signature_header)



async def handle_yookassa_webhook(request: web.Request) -> web.Response:
    """
    Обработчик вебхука ЮKassa.

    Ожидаем JSON от ЮKassa формата:
    {
      "event": "payment.succeeded",
      "object": {
        "id": "...",
        "status": "succeeded",
        "metadata": {
          "telegram_user_id": "123456789",
          "tariff_code": "1m"
        },
        ...
      }
    }
    """
    raw_body = await request.read()

    # Подпись вебхука ЮKassa (заголовок должен совпадать с тем, что ты настроишь в личном кабинете)
    signature = request.headers.get("X-Content-Signature")

    if not verify_yookassa_signature(raw_body, signature):
        log.warning("[YooKassaWebhook] Invalid webhook signature")
        return web.Response(status=403, text="invalid signature")

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        log.error("[YooKassaWebhook] Failed to parse JSON: %r", e)
        return web.Response(text="bad json")

    event = data.get("event")
    obj = data.get("object") or {}


    payment_id = obj.get("id")
    status = obj.get("status")
    metadata = obj.get("metadata") or {}

    log.info(
        "[YooKassaWebhook] event=%r status=%r payment_id=%r metadata=%r",
        event,
        status,
        payment_id,
        metadata,
    )

    # Нас интересует только успешный платёж
    if event != "payment.succeeded" or status != "succeeded":
        # Для остальных событий просто отвечаем OK, чтобы ЮKassa не ретраила.
        return web.Response(text="ok (ignored)")

    telegram_user_id_raw = metadata.get("telegram_user_id")
    tariff_code = metadata.get("tariff_code")

    if not telegram_user_id_raw or not tariff_code:
        log.error(
            "[YooKassaWebhook] Missing telegram_user_id or tariff_code in metadata: %r",
            metadata,
        )
        return web.Response(text="ok (no user or tariff)")

    try:
        telegram_user_id = int(telegram_user_id_raw)
    except ValueError:
        log.error(
            "[YooKassaWebhook] Invalid telegram_user_id in metadata: %r",
            telegram_user_id_raw,
        )
        return web.Response(text="ok (bad user id)")

    days = TARIFF_DAYS.get(tariff_code)
    if not days:
        log.error("[YooKassaWebhook] Unknown tariff_code=%r", tariff_code)
        return web.Response(text="ok (unknown tariff)")

    # Идемпотентность: если уже создавали подписку с таким event_name, ничего не делаем
    event_name = f"yookassa_payment_succeeded_{payment_id}"
    if payment_id and db.subscription_exists_by_event(event_name):
        log.info(
            "[YooKassaWebhook] Payment %s already processed (event_name=%s)",
            payment_id,
            event_name,
        )
        return web.Response(text="ok (already processed)")

    # Считаем дату окончания
    now = datetime.utcnow()
    expires_at = now + timedelta(days=days)

    # Отключаем старые активные подписки для этого Telegram-пользователя
    try:
        deactivate_existing_active_subscriptions(
            telegram_user_id=telegram_user_id,
            reason="auto_replace_yookassa",
        )
    except Exception as e:
        log.error(
            "[YooKassaWebhook] Failed to deactivate old subs for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        # Всё равно продолжим, чтобы не зависнуть в странном состоянии

    # Генерим ключи и IP
    try:
        client_priv, client_pub = wg.generate_keypair()
        client_ip = wg.generate_client_ip()
        allowed_ip = f"{client_ip}/{settings.WG_CLIENT_NETWORK_CIDR}"
    except Exception as e:
        log.error(
            "[YooKassaWebhook] Failed to generate keys/ip for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        return web.Response(text="ok (wg gen error)")

    # Добавляем peer в WireGuard
    try:
        log.info(
            "[YooKassaWebhook] Add peer pubkey=%s ip=%s for tg_id=%s",
            client_pub,
            allowed_ip,
            telegram_user_id,
        )
        wg.add_peer(
            public_key=client_pub,
            allowed_ip=allowed_ip,
            telegram_user_id=telegram_user_id,
        )
    except Exception as e:
        log.error(
            "[YooKassaWebhook] Failed to add peer to WireGuard for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        return web.Response(text="ok (wg add error)")

    # Пишем подписку в БД
    try:
        db.insert_subscription(
            tribute_user_id=0,
            telegram_user_id=telegram_user_id,
            telegram_user_name=None,  # username можем не знать на этом этапе
            subscription_id=0,
            period_id=0,
            period=f"yookassa_{tariff_code}",
            channel_id=0,
            channel_name="YooKassa",
            vpn_ip=client_ip,
            wg_private_key=client_priv,
            wg_public_key=client_pub,
            expires_at=expires_at,
            event_name=event_name,
        )
        log.info(
            "[YooKassaWebhook] Inserted subscription for tg_id=%s ip=%s until %s",
            telegram_user_id,
            client_ip,
            expires_at,
        )
    except Exception as e:
        log.error(
            "[YooKassaWebhook] Failed to insert subscription for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        return web.Response(text="ok (db error)")

    # Генерим конфиг и отправляем пользователю в Telegram
    config_text = wg.build_client_config(
        client_private_key=client_priv,
        client_ip=client_ip,
    )

    try:
        await send_vpn_config_to_user(
            telegram_user_id=telegram_user_id,
            config_text=config_text,
            caption=(
                "Оплата через ЮKassa прошла успешно.\n\n"
                "Ниже — конфиг WireGuard и QR для подключения к MaxNet VPN."
            ),
        )
        log.info("[YooKassaWebhook] Config sent to tg_id=%s", telegram_user_id)
    except Exception as e:
        log.error(
            "[YooKassaWebhook] Failed to send config to tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        # Ошибка отправки не должна ломать обработку вебхука

    return web.Response(text="ok")


def create_app() -> web.Application:
    app = web.Application()
    # путь вебхука — можешь поменять на свой
    app.router.add_post("/yookassa/webhook", handle_yookassa_webhook)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=8000)
