# /home/vpn_service/app/heleket_webhook_runner.py
import json
import base64
import hmac
import hashlib
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from aiohttp import web
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from . import db, wg
from .bot import send_vpn_config_to_user, send_subscription_extended_notification
from .config import settings
from .logger import get_heleket_logger
from .tg_bot_runner import deactivate_existing_active_subscriptions



log = get_heleket_logger()


HELEKET_API_PAYMENT_KEY = os.getenv("HELEKET_API_PAYMENT_KEY")

# используем те же длительности, что и для YooKassa
TARIFF_DAYS = {
    "1m": 30,
    "3m": 90,
    "6m": 180,
    "1y": 365,
    "forever": 3650,
}


def parse_heleket_datetime(dt_str: str) -> datetime | None:
    """
    Heleket в webhook прямо дату не шлёт, но если будешь где-то использовать created_at —
    можно парсить ISO-строки по аналогии с YooKassa.
    """
    if not dt_str:
        return None
    try:
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


def verify_heleket_ip(request: web.Request) -> bool:
    """
    Heleket официально шлёт webhook только с IP 31.133.220.8.
    """

    trusted_ip = "31.133.220.8"

    # Режим отладки: если включена переменная окружения,
    # пропускаем проверку IP (для локальных тестов curl).
    if os.getenv("HELEKET_WEBHOOK_DISABLE_IP_CHECK") == "1":
        log.warning(
            "[HeleketWebhook] IP check is disabled by HELEKET_WEBHOOK_DISABLE_IP_CHECK=1, "
            "request from=%r X-Real-IP=%r X-Forwarded-For=%r (trusted=%s)",
            request.remote,
            request.headers.get("X-Real-IP"),
            request.headers.get("X-Forwarded-For"),
            trusted_ip,
        )
        return True

    x_real_ip = request.headers.get("X-Real-IP")
    x_forwarded_for = request.headers.get("X-Forwarded-For")
    remote_ip = request.remote

    candidates: list[str] = []
    if x_real_ip:
        candidates.append(x_real_ip)
    if x_forwarded_for:
        # берём первый IP из списка
        candidates.append(x_forwarded_for.split(",")[0].strip())
    if remote_ip:
        candidates.append(remote_ip)

    for ip in candidates:
        if ip == trusted_ip:
            return True

    log.warning(
        "[HeleketWebhook] unexpected IP, candidates=%r (trusted=%s)",
        candidates,
        trusted_ip,
    )
    return False




def verify_heleket_signature(raw_body: bytes) -> bool:
    """
    Проверка подписи Heleket webhook.

    Алгоритм из доки:
      hash = md5( base64_encode( json_encode(data_without_sign, JSON_UNESCAPED_UNICODE) ) . apiPaymentKey )

    Важно: нужно экранировать слэши '/' как '\/', иначе подпись не совпадёт.
    """
    if not HELEKET_API_PAYMENT_KEY:
        log.error("[HeleketWebhook] HELEKET_API_PAYMENT_KEY is not set")
        # В отладочном режиме можно временно отключить проверку подписи.
        if os.getenv("HELEKET_WEBHOOK_DISABLE_SIGNATURE_CHECK") == "1":
            log.warning(
                "[HeleketWebhook] Signature check is disabled by "
                "HELEKET_WEBHOOK_DISABLE_SIGNATURE_CHECK=1 while HELEKET_API_PAYMENT_KEY is not set"
            )
            return True
        return False

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        log.error("[HeleketWebhook] failed to parse json for signature: %r", e)
        return False

    sign = data.pop("sign", None)
    if not sign:
        log.error("[HeleketWebhook] no sign field in webhook")
        return False

    # json без sign, как в php json_encode(..., JSON_UNESCAPED_UNICODE)
    json_str = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # вручную экранируем слэши, чтобы совпасть с php-поведение
    json_str = json_str.replace("/", "\\/")

    b64 = base64.b64encode(json_str.encode("utf-8")).decode("ascii")

    to_hash = (b64 + HELEKET_API_PAYMENT_KEY).encode("utf-8")
    expected = hashlib.md5(to_hash).hexdigest()

    if not hmac.compare_digest(expected, str(sign)):
        log.error(
            "[HeleketWebhook] signature mismatch: expected=%s got=%s",
            expected,
            sign,
        )
        return False

    return True



async def send_admin_payment_notification_heleket(
    telegram_user_id: int,
    tariff_code: str,
    payment_amount: str | None,
    currency: str | None,
    expires_at: datetime,
    is_extension: bool,
) -> None:
    """
    Отправляет админу уведомление о новой оплате / продлении подписки через Heleket.
    """
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)
    if not admin_id:
        log.warning("[HeleketWebhook] ADMIN_TELEGRAM_ID is not set, skip admin notification")
        return

    if not settings.TELEGRAM_BOT_TOKEN:
        log.error("[HeleketWebhook] TELEGRAM_BOT_TOKEN is not set, cannot send admin notification")
        return

    amount_line = payment_amount or "—"
    currency_line = currency or ""

    if is_extension:
        title = "♻️ Продление подписки через Heleket"
    else:
        title = "💳 Новая платная подписка через Heleket"

    text = (
        f"{title}\n\n"
        f"Пользователь:\n"
        f"• TG ID: <code>{telegram_user_id}</code>\n\n"
        f"Тариф: <b>{tariff_code}</b>\n"
        f"Сумма: <b>{amount_line} {currency_line}</b>\n"
        f"Действует до: <b>{expires_at.strftime('%Y-%m-%d %H:%M:%S %Z')}</b>\n"
    )

    bot = Bot(
        token=settings.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        await bot.send_message(
            chat_id=admin_id,
            text=text,
            disable_web_page_preview=True,
        )

        log.info(
            "[HeleketWebhook] Sent admin notification for payment tg_id=%s tariff=%s amount=%s %s",
            telegram_user_id,
            tariff_code,
            amount_line,
            currency_line,
        )
    except Exception as e:
        log.error(
            "[HeleketWebhook] Failed to send admin notification for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
    finally:
        await bot.session.close()


async def handle_heleket_webhook(request: web.Request) -> web.Response:

    """
    Обработчик вебхуков Heleket.

    Ожидаем JSON, похожий на пример из доки:

    {
      "type": "payment",
      "uuid": "...",
      "order_id": "...",
      "amount": "3.00000000",
      "payment_amount": "3.00000000",
      "payment_amount_usd": "0.23",
      "merchant_amount": "2.94000000",
      "commission": "0.06000000",
      "is_final": true,
      "status": "paid",
      "from": "...",
      "network": "tron",
      "currency": "TRX",
      "payer_currency": "TRX",
      "additional_data": "{\"telegram_user_id\": \"123\", \"tariff_code\": \"1m\"}",
      "txid": "...",
      "sign": "...."
    }
    """
    raw_body = await request.read()

    log.info(
        "[HeleketWebhook] received from %s headers=%r body=%s",
        request.remote,
        dict(request.headers),
        raw_body.decode("utf-8", errors="replace"),
    )

    # 1) проверка IP
    if not verify_heleket_ip(request):
        return web.Response(text="ok (ip mismatch)")

    # 2) проверка подписи
    if not verify_heleket_signature(raw_body):
        return web.Response(text="ok (bad signature)")

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        log.error("[HeleketWebhook] failed to parse json: %r", e)
        return web.Response(text="bad json")

    event_type = data.get("type")
    uuid = data.get("uuid")
    order_id = data.get("order_id")
    status = data.get("status")
    payment_status = data.get("payment_status")
    is_final = data.get("is_final")
    currency = data.get("currency")
    payment_amount = data.get("payment_amount")
    additional_data_raw = data.get("additional_data")

    # эффективный статус — в приоритете payment_status, затем status
    effective_status = payment_status or status

    log.info(
        "[HeleketWebhook] type=%r uuid=%r order_id=%r status=%r payment_status=%r effective_status=%r is_final=%r currency=%r payment_amount=%r additional_data=%r",
        event_type,
        uuid,
        order_id,
        status,
        payment_status,
        effective_status,
        is_final,
        currency,
        payment_amount,
        additional_data_raw,
    )

    # нас интересуют только финальные успешные платежи
    # если is_final не пришёл (None) — считаем финальным, если статус уже paid / paid_over
    is_final_bool = bool(is_final) if is_final is not None else effective_status in ("paid", "paid_over")

    if not is_final_bool or effective_status not in ("paid", "paid_over"):
        log.info(
            "[HeleketWebhook] ignore non-final or non-paid status uuid=%r status=%r payment_status=%r effective_status=%r is_final=%r",
            uuid,
            status,
            payment_status,
            effective_status,
            is_final,
        )
        return web.Response(text="ok (ignored)")


    # достаём мету из additional_data
    telegram_user_id = None
    tariff_code = None

    if isinstance(additional_data_raw, str) and additional_data_raw.strip():
        try:
            meta = json.loads(additional_data_raw)
            telegram_user_id_raw = meta.get("telegram_user_id")
            tariff_code = meta.get("tariff_code")
            if telegram_user_id_raw is not None:
                telegram_user_id = int(telegram_user_id_raw)
        except Exception as e:
            log.error(
                "[HeleketWebhook] failed to parse additional_data json=%r: %r",
                additional_data_raw,
                e,
            )

    if telegram_user_id is None or not tariff_code:
        log.error(
            "[HeleketWebhook] missing telegram_user_id or tariff_code in additional_data=%r",
            additional_data_raw,
        )
        return web.Response(text="ok (no user or tariff)")

    days = TARIFF_DAYS.get(tariff_code)
    if not days:
        log.error("[HeleketWebhook] unknown tariff_code=%r", tariff_code)
        return web.Response(text="ok (unknown tariff)")

    # идемпотентность по uuid
    event_name = f"heleket_payment_paid_{uuid}"
    if uuid and db.subscription_exists_by_event(event_name):
        log.info(
            "[HeleketWebhook] payment uuid=%s already processed (event_name=%s)",
            uuid,
            event_name,
        )
        return web.Response(text="ok (already processed)")

    now = datetime.now(timezone.utc)

    # ищем активные подписки этого пользователя
    active_subs = db.get_active_subscriptions_for_telegram(telegram_user_id)

    log.info(
        "[HeleketWebhook] active_subscriptions_for_tg_id=%s: %r",
        telegram_user_id,
        active_subs,
    )

    # Ищем в активных подписках Heleket-подписку (если пользователь уже платил через Heleket)
    heleket_sub = None
    for sub in active_subs:
        if sub.get("channel_name") == "Heleket" or str(sub.get("period", "")).startswith("heleket_"):
            heleket_sub = sub
            break

    # Базовая подписка для продления:
    # - если уже есть Heleket-подписка — продлеваем её
    # - иначе, если есть любая другая активная подписка (например, YooKassa) — продлеваем самую "длинную"
    base_sub = None
    if heleket_sub is not None:
        base_sub = heleket_sub
    elif active_subs:
        # get_active_subscriptions_for_telegram уже сортирует по expires_at DESC, id DESC
        base_sub = active_subs[0]

    if base_sub is not None:
        old_expires_at = base_sub["expires_at"]
        base_dt = old_expires_at if old_expires_at > now else now
        new_expires_at = base_dt + timedelta(days=days)

        try:
            db.update_subscription_expiration(
                sub_id=base_sub["id"],
                expires_at=new_expires_at,
                event_name=event_name,
            )
            log.info(
                "[HeleketWebhook] extended subscription id=%s (channel=%s) for tg_id=%s: old_expires=%s new_expires=%s (+%s days)",
                base_sub["id"],
                base_sub.get("channel_name"),
                telegram_user_id,
                old_expires_at,
                new_expires_at,
                days,
            )
        except Exception as e:
            log.error(
                "[HeleketWebhook] failed to extend subscription id=%s for tg_id=%s: %r",
                base_sub["id"],
                telegram_user_id,
                e,
            )
            return web.Response(text="ok (db extend error)")

        try:
            await send_admin_payment_notification_heleket(
                telegram_user_id=telegram_user_id,
                tariff_code=tariff_code,
                payment_amount=str(payment_amount) if payment_amount is not None else None,
                currency=str(currency) if currency is not None else None,
                expires_at=new_expires_at,
                is_extension=True,
            )
        except Exception as e:
            log.error(
                "[HeleketWebhook] failed to send admin notification about extension for tg_id=%s: %r",
                telegram_user_id,
                e,
            )

        try:
            await send_subscription_extended_notification(
                telegram_user_id=telegram_user_id,
                new_expires_at=new_expires_at,
                tariff_code=tariff_code,
                payment_channel="Heleket",
            )
        except Exception as e:
            log.error(
                "[HeleketWebhook] failed to send extension notification to tg_id=%s: %r",
                telegram_user_id,
                e,
            )

        return web.Response(text="ok (extended)")

    # если вообще нет активных подписок — создаём новую, как в YooKassa


    expires_at = now + timedelta(days=days)

    try:
        deactivate_existing_active_subscriptions(
            telegram_user_id=telegram_user_id,
            reason="auto_replace_heleket",
        )
    except Exception as e:
        log.error(
            "[HeleketWebhook] failed to deactivate old subs for tg_id=%s: %r",
            telegram_user_id,
            e,
        )

    # генерим ключи и IP
    try:
        client_priv, client_pub = wg.generate_keypair()
        client_ip = wg.generate_client_ip()
        allowed_ip = f"{client_ip}/{settings.WG_CLIENT_NETWORK_CIDR}"
    except Exception as e:
        log.error(
            "[HeleketWebhook] failed to generate keys/ip for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        return web.Response(text="ok (wg gen error)")

    # добавляем peer
    try:
        log.info(
            "[HeleketWebhook] add peer pubkey=%s ip=%s for tg_id=%s",
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
            "[HeleketWebhook] failed to add peer to WireGuard for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        return web.Response(text="ok (wg add error)")

    # пишем подписку в БД
    try:
        db.insert_subscription(
            tribute_user_id=0,
            telegram_user_id=telegram_user_id,
            telegram_user_name=None,
            subscription_id=0,
            period_id=0,
            period=f"heleket_{tariff_code}",
            channel_id=0,
            channel_name="Heleket",
            vpn_ip=client_ip,
            wg_private_key=client_priv,
            wg_public_key=client_pub,
            expires_at=expires_at,
            event_name=event_name,
        )

        log.info(
            "[HeleketWebhook] inserted subscription for tg_id=%s ip=%s until %s",
            telegram_user_id,
            client_ip,
            expires_at,
        )
    except Exception as e:
        log.error(
            "[HeleketWebhook] failed to insert subscription for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        return web.Response(text="ok (db error)")

    # шлём конфиг
    config_text = wg.build_client_config(
        client_private_key=client_priv,
        client_ip=client_ip,
    )

    try:
        await send_vpn_config_to_user(
            telegram_user_id=telegram_user_id,
            config_text=config_text,
            caption=(
                "Оплата через Heleket прошла успешно.\n\n"
                "Ниже — конфиг WireGuard и QR для подключения к MaxNet VPN."
            ),
        )
        log.info("[HeleketWebhook] config sent to tg_id=%s", telegram_user_id)
    except Exception as e:
        log.error(
            "[HeleketWebhook] failed to send config to tg_id=%s: %r",
            telegram_user_id,
            e,
        )

    try:
        await send_admin_payment_notification_heleket(
            telegram_user_id=telegram_user_id,
            tariff_code=tariff_code,
            payment_amount=str(payment_amount) if payment_amount is not None else None,
            currency=str(currency) if currency is not None else None,
            expires_at=expires_at,
            is_extension=False,
        )
    except Exception as e:
        log.error(
            "[HeleketWebhook] failed to send admin notification about new subscription for tg_id=%s: %r",
            telegram_user_id,
            e,
        )

    return web.Response(text="ok")



def create_heleket_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/heleket/webhook", handle_heleket_webhook)
    return app
