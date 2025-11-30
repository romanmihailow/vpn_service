import hmac
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Dict

from fastapi import FastAPI, Request, HTTPException
from aiogram.exceptions import TelegramBadRequest


from .config import settings
from . import db
from . import wg
from . import bot
from .logger import get_logger
log = get_logger()



app = FastAPI(title="VPN Service with Tribute")

@app.get("/")
async def root():
    return {"status": "ok", "message": "MaxNet VPN backend is alive"}

@app.get("/health")
async def health():
    return {"status": "ok"}


def parse_iso8601(dt_str: str) -> datetime:
    # У Tribute формат вида 2025-03-20T01:15:58.33246Z
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    return datetime.fromisoformat(dt_str)


def verify_tribute_signature(body: bytes, signature: str | None) -> bool:
    """
    Подпись: HMAC-SHA256(body, api_key)
    trbt-signature — строка с hex-значением.
    """
    if not signature:
        return False

    secret = settings.TRIBUTE_WEBHOOK_SECRET
    if not secret:
        return False

    digest = hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(digest, signature)


@app.get("/admin/subscriptions")
def admin_list():
    subs = db.get_last_subscriptions(limit=50)
    return {"items": subs}

@app.post("/admin/subscriptions/{sub_id}/deactivate")
def admin_deactivate_subscription(sub_id: int):
    """
    Админ-эндпойнт: деактивировать подписку и попытаться удалить peer из WireGuard.
    Используется для ручного отключения ключа.
    """
    sub = db.deactivate_subscription_by_id(
        sub_id=sub_id,
        event_name="admin_deactivate",
    )
    if not sub:
        raise HTTPException(
            status_code=404,
            detail="Subscription not found or already inactive",
        )

    pub_key = sub.get("wg_public_key")
    if pub_key:
        try:
            log.info("[Admin] Remove peer pubkey=%s for sub_id=%s", pub_key, sub_id)
            wg.remove_peer(pub_key)
        except Exception as e:
            log.error(
                "[Admin] Failed to remove peer from WireGuard for sub_id=%s: %s",
                sub_id,
                repr(e),
            )

    return {"status": "ok", "id": sub_id}

async def handle_new_subscription(payload: Dict[str, Any]) -> None:
    """
    new_subscription:
    {
        "subscription_name": "...",
        "subscription_id": 1644,
        "period_id": 1547,
        "period": "monthly",
        "price": 1000,
        "amount": 700,
        "currency": "eur",
        "user_id": 31326,
        "telegram_user_id": 12321321,
        "channel_id": 614,
        "channel_name": "lbs",
        "expires_at": "2025-04-20T01:15:57.305733Z"
    }
    """
    tribute_user_id = int(payload["user_id"])
    telegram_user_id = int(payload["telegram_user_id"])

    log.info(
        "[new_subscription] tribute_user_id=%s telegram_id=%s",
        tribute_user_id,
        telegram_user_id,
    )

    subscription_id = int(payload["subscription_id"])
    period_id = int(payload["period_id"])
    period = str(payload["period"])
    channel_id = int(payload["channel_id"])
    channel_name = str(payload["channel_name"])
    expires_at = parse_iso8601(payload["expires_at"])


    # 1. Проверяем, есть ли уже активная подписка на этот период/канал
    existing = db.get_active_subscription(
        tribute_user_id=tribute_user_id,
        period_id=period_id,
        channel_id=channel_id,
    )

    if existing:
        # Просто продлеваем подписку (не выдаём новый конфиг)
        db.update_subscription_expiration(
            sub_id=existing["id"],
            expires_at=expires_at,
            event_name="new_subscription",
        )
        await bot.send_text_message(
            telegram_user_id,
            "Подписка продлена, VPN-доступ уже активен. Можешь пользоваться как раньше.",
        )
        return

    # 2. Новый пользователь или новая подписка на этот период
    client_priv, client_pub = wg.generate_keypair()
    client_ip = wg.generate_client_ip()

    log.info("[WG] Add peer IP=%s pubkey=%s", client_ip, client_pub)

    allowed_ip = f"{client_ip}/{settings.WG_CLIENT_NETWORK_CIDR}"

    # 2. Добавляем peer в WireGuard
    wg.add_peer(
        public_key=client_pub,
        allowed_ip=allowed_ip,
        telegram_user_id=telegram_user_id,
    )



    # Записываем в БД
    db.insert_subscription(
        tribute_user_id=tribute_user_id,
        telegram_user_id=telegram_user_id,
        subscription_id=subscription_id,
        period_id=period_id,
        period=period,
        channel_id=channel_id,
        channel_name=channel_name,
        vpn_ip=client_ip,
        wg_private_key=client_priv,
        wg_public_key=client_pub,
        expires_at=expires_at,
        event_name="new_subscription",
    )

    log.info(
        "[DB] Inserted subscription tribute_user_id=%s vpn_ip=%s",
        tribute_user_id,
        client_ip,
    )

    # 4. Генерим конфиг и шлём в Telegram
    config_text = wg.build_client_config(
        client_private_key=client_priv,
        client_ip=client_ip,
    )

    try:
        await bot.send_vpn_config_to_user(
            telegram_user_id=telegram_user_id,
            config_text=config_text,
            caption=(
                "Спасибо за поддержку через Tribute!\n\n"
                "Ниже — конфиг WireGuard и QR для подключения к VPN."
            ),
        )
        log.info("[Telegram] Config sent (donation) to %s", telegram_user_id)
    except TelegramBadRequest as e:
        # Например: Bad Request: chat not found
        log.error(
            "TelegramBadRequest while sending donation config to %s: %s",
            telegram_user_id,
            e,
        )

    except Exception as e:
        log.error(
            "[Telegram] Failed to send config to %s: %s",
            telegram_user_id,
            repr(e),
        )

    
    
async def handle_new_donation(payload: Dict[str, Any], created_at_str: str) -> None:
    """
    new_donation:
    {
        "donation_request_id": 141883,
        "donation_name": "MaxNet VPN | Быстрый доступ без блокировок",
        "period": "monthly",
        "amount": 100,
        "currency": "eur",
        "anonymously": false,
        "web_app_link": "...",
        "user_id": 41703348,
        "telegram_user_id": 7083630429
    }

    Мы трактуем донат как покупку доступа на 30 дней.
    """
    tribute_user_id = int(payload["user_id"])
    telegram_user_id = int(payload["telegram_user_id"])
    donation_request_id = int(payload["donation_request_id"])
    period = str(payload.get("period", "monthly"))
    channel_name = str(payload.get("donation_name", "donation"))

    # Синтетические значения, чтобы уложиться в текущую схему БД
    subscription_id = donation_request_id
    period_id = 0
    channel_id = 0

    # Считаем срок действия: +30 дней от created_at
    if created_at_str:
        created_at = parse_iso8601(created_at_str)
    else:
        created_at = datetime.utcnow()

    expires_at = created_at + timedelta(days=30)

    log.info(
        "[new_donation] tribute_user_id=%s telegram_id=%s",
        tribute_user_id,
        telegram_user_id,
    )

    # Проверка на повторное уведомление от Tribute (идемпотентность).
    # Если уже есть активная подписка с таким subscription_id (donation_request_id),
    # считаем это ретраем и просто переотправляем конфиг.
    existing = db.get_subscription_by_tribute_and_subscription(
        tribute_user_id=tribute_user_id,
        subscription_id=subscription_id,
    )
    if existing and existing.get("active") and existing.get("last_event_name") == "new_donation":
        log.info(
            "[new_donation] Duplicate webhook for tribute_user_id=%s subscription_id=%s, resending config",
            tribute_user_id,
            subscription_id,
        )

        try:
            existing_priv_key = existing["wg_private_key"]
            existing_ip = existing["vpn_ip"]
        except KeyError:
            log.error(
                "[new_donation] Existing subscription %s has no wg_private_key or vpn_ip, cannot resend config",
                existing.get("id"),
            )
            return

        config_text = wg.build_client_config(
            client_private_key=existing_priv_key,
            client_ip=existing_ip,
        )

        try:
            await bot.send_vpn_config_to_user(
                telegram_user_id=telegram_user_id,
                config_text=config_text,
                caption=(
                    "Повторно отправляем VPN-конфиг WireGuard и QR-код.\n\n"
                    "Если он уже был у тебя — можно просто использовать старый."
                ),
            )
            log.info("[Telegram] Config re-sent (donation duplicate) to %s", telegram_user_id)
        except TelegramBadRequest as e:
            log.error(
                "TelegramBadRequest while re-sending donation config to %s: %s",
                telegram_user_id,
                e,
            )
        except Exception as e:
            log.error(
                "[Telegram] Failed to re-send config (donation duplicate) to %s: %s",
                telegram_user_id,
                repr(e),
            )

        return

    # 1. Генерим ключи и IP
    client_priv, client_pub = wg.generate_keypair()
    client_ip = wg.generate_client_ip()


    log.info("[WG] Add peer IP=%s pubkey=%s", client_ip, client_pub)

    allowed_ip = f"{client_ip}/{settings.WG_CLIENT_NETWORK_CIDR}"

    # 2. Добавляем peer в WireGuard
    wg.add_peer(
        public_key=client_pub,
        allowed_ip=allowed_ip,
        telegram_user_id=telegram_user_id,
    )


    # 3. Пишем в БД (используем уже существующую функцию)
    db.insert_subscription(
        tribute_user_id=tribute_user_id,
        telegram_user_id=telegram_user_id,
        subscription_id=subscription_id,
        period_id=period_id,
        period=period,
        channel_id=channel_id,
        channel_name=channel_name,
        vpn_ip=client_ip,
        wg_private_key=client_priv,
        wg_public_key=client_pub,
        expires_at=expires_at,
        event_name="new_donation",
    )

    log.info(
        "[DB] Inserted donation subscription tribute_user_id=%s vpn_ip=%s",
        tribute_user_id,
        client_ip,
    )

    # 4. Генерим конфиг и шлём в Telegram
    config_text = wg.build_client_config(
        client_private_key=client_priv,
        client_ip=client_ip,
    )

    try:
        await bot.send_vpn_config_to_user(
            telegram_user_id=telegram_user_id,
            config_text=config_text,
            caption=(
                "Спасибо за поддержку через Tribute!\n\n"
                "Ниже — конфиг WireGuard и QR для подключения к VPN."
            ),
        )
        log.info("[Telegram] Config sent (donation) to %s", telegram_user_id)
    except TelegramBadRequest as e:
        log.error(
            "TelegramBadRequest while sending donation config to %s: %s",
            telegram_user_id,
            e,
        )
    except Exception as e:
        log.error(
            "[Telegram] Failed to send config (donation) to %s: %s",
            telegram_user_id,
            repr(e),
        )





async def handle_cancelled_subscription(payload: Dict[str, Any]) -> None:
    """
    cancelled_subscription:
    {
        "subscription_name": "...",
        "subscription_id": 1646,
        "period_id": 1549,
        "period": "monthly",
        "price": 1000,
        "amount": 1000,
        "currency": "eur",
        "user_id": 31326,
        "telegram_user_id": 12321321,
        "channel_id": 614,
        "channel_name": "lbs",
        "cancel_reason": "",
        "expires_at": "2025-03-20T11:13:44.737Z"
    }
    """
    tribute_user_id = int(payload["user_id"])
    telegram_user_id = int(payload["telegram_user_id"])
    period_id = int(payload["period_id"])
    channel_id = int(payload["channel_id"])
    log.info("[cancelled_subscription] tribute_user_id=%s", tribute_user_id)

    # expires_at = parse_iso8601(payload["expires_at"])  # при желании можно использовать

    # Деактивируем записи в БД и получаем их, чтобы убрать peer-ов
    subs = db.deactivate_subscriptions_for_period(
        tribute_user_id=tribute_user_id,
        period_id=period_id,
        channel_id=channel_id,
        event_name="cancelled_subscription",
    )

    log.info("[DB] Deactivated subscriptions count=%s", len(subs))

    # Удаляем peer в WireGuard
    for sub in subs:
        pub_key = sub["wg_public_key"]
        try:
            log.info("[WG] Remove peer pubkey=%s", pub_key)
            wg.remove_peer(pub_key)
        except Exception:
            continue

    await bot.send_text_message(
        telegram_user_id,
        "Подписка в Tribute отменена. VPN-доступ отключён.\n"
        "Если захочешь вернуться — просто оформляй новую подписку.",
    )



@app.on_event("startup")
def on_startup() -> None:
    db.init_db()


@app.post("/tribute/webhook")
async def tribute_webhook(request: Request):
    raw_body = await request.body()

    # DEBUG print
    print("\n=== NEW WEBHOOK ===")
    print("Headers:", dict(request.headers))
    print("Body:", raw_body.decode("utf-8"))
    print("====================\n")

    signature = request.headers.get("trbt-signature")
    log.info("=== Tribute Webhook Received ===")
    log.info("Headers: %s", dict(request.headers))
    log.info("Body: %s", raw_body.decode("utf-8"))

    if not verify_tribute_signature(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    name = payload.get("name")
    event_payload = payload.get("payload", {})
    created_at_str = payload.get("created_at")

    if name == "new_subscription":
        await handle_new_subscription(event_payload)
    elif name == "new_donation":
        await handle_new_donation(event_payload, created_at_str)
    elif name == "cancelled_subscription":
        await handle_cancelled_subscription(event_payload)
    else:
        # Игнорируем другие типы событий (physical_order_* и т.д.)
        log.info("Ignored webhook event name=%s", name)

    return {"status": "ok"}
