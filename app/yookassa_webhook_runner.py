import json
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import base64


import requests
from aiohttp import web
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from . import db, wg
from .bot import send_vpn_config_to_user, send_subscription_extended_notification, send_referral_reward_notification
from .config import settings

from .logger import get_yookassa_logger
from .tg_bot_runner import deactivate_existing_active_subscriptions


YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

log = get_yookassa_logger()


async def process_yookassa_event(data: dict, remote_ip: str) -> None:
    try:
        event = data.get("event")
        obj = data.get("object") or {}

        payment_id = obj.get("id")
        status = obj.get("status")
        metadata = obj.get("metadata") or {}
        telegram_user_name = metadata.get("telegram_user_name")
        created_at = obj.get("created_at")
        is_test = obj.get("test")

        log.info(
            "[YooKassaWebhook] ip=%s event=%r status=%r payment_id=%r created_at=%r test=%r metadata=%r",
            remote_ip,
            event,
            status,
            payment_id,
            created_at,
            is_test,
            metadata,
        )

        # Обработка разных типов событий от YooKassa
        if event != "payment.succeeded" or status != "succeeded":
            # Дополнительная обработка отменённых платежей (до списания)
            if event == "payment.canceled":
                log.info(
                    "[YooKassaWebhook] payment.canceled received payment_id=%r metadata=%r",
                    payment_id,
                    metadata,
                )

                if payment_id:
                    # Ищем подписку, созданную на основе успешного платежа
                    success_event_name = f"yookassa_payment_succeeded_{payment_id}"
                    sub = db.get_subscription_by_event(success_event_name)
                    if sub is not None:
                        sub_id = sub.get("id")
                        pub_key = sub.get("wg_public_key")
                        telegram_user_id = sub.get("telegram_user_id")

                        log.info(
                            "[YooKassaWebhook] cancel payment: found subscription id=%s for tg_id=%s, deactivating",
                            sub_id,
                            telegram_user_id,
                        )

                        # Деактивируем подписку в БД
                        deactivated = db.deactivate_subscription_by_id(
                            sub_id=sub_id,
                            event_name=f"yookassa_payment_canceled_{payment_id}",
                        )

                        if deactivated and pub_key:
                            try:
                                log.info(
                                    "[YooKassaWebhook] Remove peer pubkey=%s for canceled payment_id=%s sub_id=%s",
                                    pub_key,
                                    payment_id,
                                    sub_id,
                                )
                                wg.remove_peer(pub_key)
                            except Exception as e:
                                log.error(
                                    "[YooKassaWebhook] Failed to remove peer for canceled payment_id=%s sub_id=%s: %r",
                                    payment_id,
                                    sub_id,
                                    e,
                                )
                    else:
                        log.info(
                            "[YooKassaWebhook] cancel payment: no subscription found for event_name=%s",
                            f"yookassa_payment_succeeded_{payment_id}",
                        )

                return

            # Дополнительная обработка возвратов (refund.succeeded)
            if event == "refund.succeeded":
                # Для refund.succeeded объект — это возврат, а не платёж.
                # В поле object.payment_id лежит id исходного платежа.
                refund_id = payment_id  # текущее payment_id — это id возврата
                refund_payment_id = obj.get("payment_id")

                # Идемпотентность по refund_id: один и тот же возврат не должен применяться дважды
                refund_event_name = f"yookassa_refund_succeeded_{refund_id}"
                if refund_id and db.subscription_exists_by_event(refund_event_name):
                    log.info(
                        "[YooKassaWebhook] refund: refund_id=%s already processed (event_name=%s)",
                        refund_id,
                        refund_event_name,
                    )
                    return

                refund_amount_obj = obj.get("amount") or {}

                refund_amount_raw = refund_amount_obj.get("value") or "0.00"
                refund_currency = refund_amount_obj.get("currency")

                try:
                    refund_amount = Decimal(str(refund_amount_raw))
                except Exception:
                    refund_amount = Decimal("0.00")

                log.info(
                    "[YooKassaWebhook] refund.succeeded received refund_id=%r payment_id=%r refund_amount=%s %s",
                    refund_id,
                    refund_payment_id,
                    refund_amount,
                    refund_currency,
                )

                if refund_payment_id:
                    # Пытаемся вытащить оригинальный платёж, чтобы понять тариф и сумму
                    api_payment = fetch_payment_from_yookassa(refund_payment_id)
                    if not api_payment:
                        log.error(
                            "[YooKassaWebhook] refund: failed to fetch original payment %s for refund_id=%s",
                            refund_payment_id,
                            refund_id,
                        )
                        return

                    api_metadata = api_payment.get("metadata") or {}
                    api_amount_obj = api_payment.get("amount") or {}
                    api_amount_value_raw = api_amount_obj.get("value") or "0.00"
                    api_currency = api_amount_obj.get("currency")

                    try:
                        total_amount = Decimal(str(api_amount_value_raw))
                    except Exception:
                        total_amount = Decimal("0.00")

                    tariff_code_from_payment = api_metadata.get("tariff_code")

                    log.info(
                        "[YooKassaWebhook] refund: original payment_id=%s total_amount=%s %s tariff_code=%r",
                        refund_payment_id,
                        total_amount,
                        api_currency,
                        tariff_code_from_payment,
                    )

                    # Ищем подписку, созданную на основе успешного платежа
                    success_event_name = f"yookassa_payment_succeeded_{refund_payment_id}"
                    sub = db.get_subscription_by_event(success_event_name)

                    # Если по event_name не нашли (случай старого платежа),
                    # пробуем найти активную YooKassa-подписку по telegram_user_id из metadata
                    if sub is None:
                        telegram_user_id = None
                        api_tg_raw = api_metadata.get("telegram_user_id")
                        try:
                            if api_tg_raw is not None:
                                telegram_user_id = int(api_tg_raw)
                        except ValueError:
                            telegram_user_id = None

                        if telegram_user_id is not None:
                            active_subs = db.get_active_subscriptions_for_telegram(telegram_user_id)
                            yookassa_sub = None
                            for candidate in active_subs:
                                if candidate.get("channel_name") == "YooKassa" or str(candidate.get("period") or "").startswith("yookassa_"):
                                    yookassa_sub = candidate
                                    break
                            sub = yookassa_sub

                    if sub is not None:
                        sub_id = sub.get("id")
                        pub_key = sub.get("wg_public_key")
                        telegram_user_id = sub.get("telegram_user_id")
                        old_expires_at = sub.get("expires_at")

                        log.info(
                            "[YooKassaWebhook] refund: found subscription id=%s for tg_id=%s with expires_at=%s",
                            sub_id,
                            telegram_user_id,
                            old_expires_at,
                        )

                        # Определяем, сколько дней дал этот платёж
                        days_for_tariff = None

                        # 1) пытаемся взять по tariff_code из оригинального платежа
                        if tariff_code_from_payment:
                            days_for_tariff, _ = get_tariff_days_and_amount_from_db(tariff_code_from_payment)

                        # 2) если tariff_code не дали или не нашли в БД — пробуем вытащить из sub["period"],
                        #    если там формат "yookassa_1m"
                        if days_for_tariff is None:
                            period = str(sub.get("period") or "")
                            if period.startswith("yookassa_"):
                                suffix = period[len("yookassa_") :]
                                if suffix:
                                    days_for_tariff, _ = get_tariff_days_and_amount_from_db(suffix)

                        if days_for_tariff is None:
                            log.error(
                                "[YooKassaWebhook] refund: cannot determine tariff days for refund_id=%s payment_id=%s",
                                refund_id,
                                refund_payment_id,
                            )
                            # Фоллбэк: деактивируем подписку целиком, как раньше
                            deactivated = db.deactivate_subscription_by_id(
                                sub_id=sub_id,
                                event_name=f"yookassa_refund_succeeded_{refund_id}",
                            )
                            if deactivated and pub_key:
                                try:
                                    log.info(
                                        "[YooKassaWebhook] Remove peer pubkey=%s for refund refund_id=%s sub_id=%s (fallback full deactivate)",
                                        pub_key,
                                        refund_id,
                                        sub_id,
                                    )
                                    wg.remove_peer(pub_key)
                                except Exception as e:
                                    log.error(
                                        "[YooKassaWebhook] Failed to remove peer for refund refund_id=%s sub_id=%s: %r",
                                        refund_id,
                                        sub_id,
                                        e,
                                    )
                            return

                        # Если нет суммы или валюты, откатываем весь тариф
                        if total_amount <= Decimal("0.00") or refund_amount <= Decimal("0.00"):
                            days_to_revert = days_for_tariff
                        else:
                            # Считаем долю возврата и пропорциональное кол-во дней
                            ratio = refund_amount / total_amount
                            if ratio > Decimal("1"):
                                ratio = Decimal("1")
                            days_to_revert = int(days_for_tariff * ratio)
                            if days_to_revert <= 0 and refund_amount > Decimal("0.00"):
                                days_to_revert = 1

                        now = datetime.now(timezone.utc)
                        new_expires_at = old_expires_at - timedelta(days=days_to_revert)

                        log.info(
                            "[YooKassaWebhook] refund: days_for_tariff=%s days_to_revert=%s old_expires_at=%s new_expires_at=%s now=%s",
                            days_for_tariff,
                            days_to_revert,
                            old_expires_at,
                            new_expires_at,
                            now,
                        )

                        if new_expires_at <= now:
                            # Подписка по факту "съедена" возвратом — деактивируем
                            deactivated = db.deactivate_subscription_by_id(
                                sub_id=sub_id,
                                event_name=f"yookassa_refund_succeeded_{refund_id}",
                            )
                            if deactivated and pub_key:
                                try:
                                    log.info(
                                        "[YooKassaWebhook] Remove peer pubkey=%s for refund refund_id=%s sub_id=%s (full deactivate after revert)",
                                        pub_key,
                                        refund_id,
                                        sub_id,
                                    )
                                    wg.remove_peer(pub_key)
                                except Exception as e:
                                    log.error(
                                        "[YooKassaWebhook] Failed to remove peer for refund refund_id=%s sub_id=%s: %r",
                                        refund_id,
                                        sub_id,
                                        e,
                                    )
                        else:
                            # Просто сокращаем срок подписки
                            try:
                                db.update_subscription_expiration(
                                    sub_id=sub_id,
                                    expires_at=new_expires_at,
                                    event_name=f"yookassa_refund_succeeded_{refund_id}",
                                )
                                log.info(
                                    "[YooKassaWebhook] refund: shortened subscription id=%s for tg_id=%s: old_expires=%s new_expires=%s (-%s days)",
                                    sub_id,
                                    telegram_user_id,
                                    old_expires_at,
                                    new_expires_at,
                                    days_to_revert,
                                )
                            except Exception as e:
                                log.error(
                                    "[YooKassaWebhook] Failed to shorten subscription id=%s for tg_id=%s on refund_id=%s: %r",
                                    sub_id,
                                    telegram_user_id,
                                    refund_id,
                                    e,
                                )
                    else:
                        log.info(
                            "[YooKassaWebhook] refund: no subscription found for event_name=%s and active YooKassa subscription",
                            success_event_name,
                        )

                return

            # Логируем другие события для анализа
            log.info(
                "[YooKassaWebhook] non-success event=%r status=%r payment_id=%r metadata=%r",
                event,
                status,
                payment_id,
                metadata,
            )
            return

        if not payment_id:
            log.error("[YooKassaWebhook] No payment_id in object")
            return

        telegram_user_id_raw = metadata.get("telegram_user_id")
        tariff_code = metadata.get("tariff_code")

        if not telegram_user_id_raw or not tariff_code:
            log.error(
                "[YooKassaWebhook] Missing telegram_user_id or tariff_code in metadata: %r",
                metadata,
            )
            return

        try:
            telegram_user_id = int(telegram_user_id_raw)
        except ValueError:
            log.error(
                "[YooKassaWebhook] Invalid telegram_user_id in metadata: %r",
                telegram_user_id_raw,
            )
            return

        days, expected_amount = get_tariff_days_and_amount_from_db(tariff_code)
        if not days:
            log.error("[YooKassaWebhook] Unknown tariff_code=%r", tariff_code)
            return

        if not expected_amount:
            log.error(
                "[YooKassaWebhook] No expected amount configured for tariff_code=%r",
                tariff_code,
            )
            return

        # 🔍 ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА ЧЕРЕЗ API ЮKassa
        api_payment = fetch_payment_from_yookassa(payment_id)
        if not api_payment:
            # Не смогли проверить платёж — не рискуем, просто отвечаем ok,
            # чтобы ЮKassa не дудосила ретраями, но доступ не выдаём.
            return

        api_status = api_payment.get("status")
        api_paid = api_payment.get("paid")
        api_metadata = api_payment.get("metadata") or {}
        api_test = api_payment.get("test")
        api_amount_obj = api_payment.get("amount") or {}
        api_amount_value = str(api_amount_obj.get("value"))
        api_currency = api_amount_obj.get("currency")
        api_refunded_obj = api_payment.get("refunded_amount") or {}
        api_refunded_value_raw = api_refunded_obj.get("value") or "0.00"
        try:
            api_refunded_value = Decimal(str(api_refunded_value_raw))
        except Exception:
            api_refunded_value = Decimal("0.00")

        api_created_at_str = api_payment.get("created_at")
        api_created_at_dt = parse_yookassa_datetime(api_created_at_str)

        log.info(
            "[YooKassaWebhook] API check payment_id=%s status=%s paid=%s test=%r api_metadata=%r amount=%s currency=%s refunded_amount=%s created_at=%s",
            payment_id,
            api_status,
            api_paid,
            api_test,
            api_metadata,
            api_amount_value,
            api_currency,
            api_refunded_value,
            api_created_at_str,
        )

        # Статус в API должен быть succeeded и paid == True
        if api_status != "succeeded" or not api_paid:
            log.warning(
                "[YooKassaWebhook] API payment not succeeded or not paid: id=%s status=%s paid=%s",
                payment_id,
                api_status,
                api_paid,
            )
            return

        # Проверяем только валюту (сумму логируем, но не блокируем обработку)
        if api_currency != "RUB":
            log.error(
                "[YooKassaWebhook] Wrong currency for payment %s: expected RUB, got %s (amount=%s)",
                payment_id,
                api_currency,
                api_amount_value,
            )
            return

        # Если по этому платежу уже есть возврат — не продлеваем и не создаём подписку
        if api_refunded_value > Decimal("0.00"):
            log.warning(
                "[YooKassaWebhook] Payment %s has refunded_amount=%s — treat as refunded, skip VPN granting",
                payment_id,
                api_refunded_value,
            )
            return

        # Метаданные в API должны совпадать с тем, что пришло в вебхуке
        api_tg_id_raw = api_metadata.get("telegram_user_id")
        api_tariff_code = api_metadata.get("tariff_code")

        if str(api_tg_id_raw) != str(telegram_user_id) or api_tariff_code != tariff_code:
            log.error(
                "[YooKassaWebhook] API metadata mismatch for payment %s: webhook(tg_id=%r, tariff=%r) api(tg_id=%r, tariff=%r)",
                payment_id,
                telegram_user_id,
                tariff_code,
                api_tg_id_raw,
                api_tariff_code,
            )
            return

        # Можно при желании отсеивать test-платежи тут, если живёшь в бою
        # if api_test:
        #     log.info("[YooKassaWebhook] Test payment %s — игнорируем в бою", payment_id)
        #     return

        # Идемпотентность: если уже создавали подписку с таким event_name, ничего не делаем
        event_name = f"yookassa_payment_succeeded_{payment_id}"
        if payment_id and db.subscription_exists_by_event(event_name):
            log.info(
                "[YooKassaWebhook] Payment %s already processed (event_name=%s)",
                payment_id,
                event_name,
            )
            return

        # =========================
        # ЛОГИКА ПРОДЛЕНИЯ ПОДПИСКИ
        # =========================

        now = datetime.now(timezone.utc)
        active_subs = db.get_active_subscriptions_for_telegram(telegram_user_id)
        log.info(
            "[YooKassaWebhook] active_subscriptions_for_tg_id=%s: %r",
            telegram_user_id,
            active_subs,
        )
        yookassa_sub = None
        for sub in active_subs:
            if sub.get("channel_name") == "YooKassa" or str(sub.get("period", "")).startswith("yookassa_"):
                yookassa_sub = sub
                break

        base_sub = None

        if yookassa_sub is not None:
            last_event_name = str(yookassa_sub.get("last_event_name") or "")
            prefix = "yookassa_payment_succeeded_"
            if last_event_name.startswith(prefix):
                last_payment_id = last_event_name[len(prefix):]
                if last_payment_id and last_payment_id != payment_id:
                    last_payment = fetch_payment_from_yookassa(last_payment_id)
                    if last_payment:
                        last_created_at_str = last_payment.get("created_at")
                        last_created_at_dt = parse_yookassa_datetime(last_created_at_str)

                        if api_created_at_dt and last_created_at_dt and api_created_at_dt <= last_created_at_dt:
                            log.warning(
                                "[YooKassaWebhook] Payment %s is older or same as already processed payment %s (created_at=%s, last_created_at=%s) — skip extension",
                                payment_id,
                                last_payment_id,
                                api_created_at_dt,
                                last_created_at_dt,
                            )
                            return

            base_sub = yookassa_sub
        elif active_subs:
            base_sub = active_subs[0]

        if base_sub is None:
            # Нет активной подписки — создаём новую
            expires_at = now + timedelta(days=days)
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
                return

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
                try:
                    db.release_ip_in_pool(client_ip)
                except Exception:
                    pass
                log.error(
                    "[YooKassaWebhook] Failed to add peer to WireGuard for tg_id=%s: %r",
                    telegram_user_id,
                    e,
                )
                return

            # Пишем подписку в БД
            try:
                subscription_id = db.insert_subscription(
                    tribute_user_id=0,
                    telegram_user_id=telegram_user_id,
                    telegram_user_name=telegram_user_name,
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

                # Реферальные бонусы за новую платную подписку
                try:
                    rewards_result = db.apply_referral_rewards_for_subscription(
                        payer_telegram_user_id=telegram_user_id,
                        subscription_id=subscription_id,
                        tariff_code=tariff_code,
                        payment_source="yookassa",
                        payment_id=payment_id,
                    )
                    log.info(
                        "[YooKassaWebhook] Referral rewards result for payment_id=%s: %r",
                        payment_id,
                        rewards_result,
                    )
                    try:
                        awards = rewards_result.get("awards") if isinstance(rewards_result, dict) else None
                        if awards:
                            for award in awards:
                                ref_tg_id = award.get("telegram_user_id") or award.get("user_telegram_id")
                                points = award.get("points") or award.get("delta") or 0
                                level = award.get("level")
                                if not ref_tg_id or not points:
                                    continue
                                await send_referral_reward_notification(
                                    telegram_user_id=ref_tg_id,
                                    points_delta=points,
                                    level=level,
                                    tariff_code=tariff_code,
                                    payment_channel="YooKassa",
                                )
                    except Exception as e:
                        log.error(
                            "[YooKassaWebhook] Failed to send referral reward notifications for payment_id=%s: %r",
                            payment_id,
                            e,
                        )
                except Exception as e:
                    log.error(
                        "[YooKassaWebhook] Failed to apply referral rewards for payment_id=%s tg_id=%s: %r",
                        payment_id,
                        telegram_user_id,
                        e,
                    )

            except Exception as e:
                try:
                    db.release_ip_in_pool(client_ip)
                except Exception:
                    pass
                log.error(
                    "[YooKassaWebhook] Failed to insert subscription for tg_id=%s: %r",
                    telegram_user_id,
                    e,
                )
                return

            log.info(
                "[YooKassaWebhook] issuing VPN config tg_id=%s payment_id=%s",
                telegram_user_id,
                payment_id,
            )

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
                        "Спасибо за оплату подписки MaxNet VPN через ЮKassa!\n\n"
                        "Ниже — конфиг WireGuard и QR для подключения."
                    ),
                )
            except Exception as e:
                log.error(
                    "[YooKassaWebhook] Failed to send config to tg_id=%s for payment_id=%s: %r",
                    telegram_user_id,
                    payment_id,
                    e,
                )
            try:
                await send_admin_payment_notification(
                    telegram_user_id=telegram_user_id,
                    telegram_user_name=telegram_user_name,
                    tariff_code=tariff_code,
                    amount=api_amount_value,
                    expires_at=expires_at,
                    is_extension=False,
                )
            except Exception as e:
                log.error(
                    "[YooKassaWebhook] Failed to send admin notification about new subscription for tg_id=%s: %r",
                    telegram_user_id,
                    e,
                )

            return

        # Продление подписки (если base_sub найден)
        base_sub_id = base_sub.get("id")
        base_sub_tg_id = base_sub.get("telegram_user_id")
        base_sub_expires = base_sub.get("expires_at")
        base_sub_event = base_sub.get("last_event_name")

        if base_sub_id is None:
            log.error("[YooKassaWebhook] base_sub has no id for tg_id=%s", telegram_user_id)
            return

        old_expires_at = base_sub_expires
        base_dt = old_expires_at if old_expires_at and old_expires_at > now else now
        new_expires_at = base_dt + timedelta(days=days)

        try:
            db.update_subscription_expiration(
                sub_id=base_sub_id,
                expires_at=new_expires_at,
                event_name=event_name,
            )
        except Exception as e:
            log.error(
                "[YooKassaWebhook] Failed to extend subscription for tg_id=%s: %r",
                telegram_user_id,
                e,
            )
            return

        log.info(
            "[YooKassaWebhook] Extended subscription sub_id=%s for tg_id=%s to %s",
            base_sub_id,
            base_sub_tg_id,
            new_expires_at,
        )

        # Реферальные бонусы при продлении
        try:
            rewards_result = db.apply_referral_rewards_for_subscription(
                payer_telegram_user_id=base_sub_tg_id,
                subscription_id=base_sub_id,
                tariff_code=tariff_code,
                payment_source="yookassa",
                payment_id=payment_id,
            )
            log.info(
                "[YooKassaWebhook] Referral rewards result for payment_id=%s: %r",
                payment_id,
                rewards_result,
            )
            try:
                awards = rewards_result.get("awards") if isinstance(rewards_result, dict) else None
                if awards:
                    for award in awards:
                        ref_tg_id = award.get("telegram_user_id") or award.get("user_telegram_id")
                        points = award.get("points") or award.get("delta") or 0
                        level = award.get("level")
                        if not ref_tg_id or not points:
                            continue
                        await send_referral_reward_notification(
                            telegram_user_id=ref_tg_id,
                            points_delta=points,
                            level=level,
                            tariff_code=tariff_code,
                            payment_channel="YooKassa",
                        )
            except Exception as e:
                log.error(
                    "[YooKassaWebhook] Failed to send referral reward notifications for payment_id=%s: %r",
                    payment_id,
                    e,
                )
        except Exception as e:
            log.error(
                "[YooKassaWebhook] Failed to apply referral rewards for payment_id=%s tg_id=%s: %r",
                payment_id,
                base_sub_tg_id,
                e,
            )
        try:
            await send_admin_payment_notification(
                telegram_user_id=telegram_user_id,
                telegram_user_name=telegram_user_name,
                tariff_code=tariff_code,
                amount=api_amount_value,
                expires_at=new_expires_at,
                is_extension=True,
            )
        except Exception as e:
            log.error(
                "[YooKassaWebhook] Failed to send admin notification about extension for tg_id=%s: %r",
                telegram_user_id,
                e,
            )
        try:
            await send_subscription_extended_notification(
                telegram_user_id=base_sub_tg_id,
                new_expires_at=new_expires_at,
                tariff_code=tariff_code,
                payment_channel="YooKassa",
            )
        except Exception as e:
            log.error(
                "[YooKassaWebhook] Failed to send extend notification to tg_id=%s: %r",
                base_sub_tg_id,
                e,
            )

    except Exception as e:
        log.error("[YooKassaWebhook] Unexpected error in process: %r", e)

# Сколько дней даёт каждый тариф ЮKassa (fallback, если БД недоступна)
TARIFF_DAYS_FALLBACK = {
    "1m": 30,
    "3m": 90,
    "6m": 180,
    "1y": 365,
    # формально "навсегда" — здесь ставим большой срок, например 10 лет
    "forever": 3650,
}

# Ожидаемые суммы по тарифам (fallback, если БД недоступна)
TARIFF_AMOUNTS_FALLBACK = {
    "1m": "100.00",
    "3m": "270.00",
    "6m": "480.00",
    "1y": "840.00",
    "forever": "1990.00",
}


def get_tariff_days_and_amount_from_db(tariff_code: str) -> tuple[int | None, str | None]:
    """
    Возвращает (duration_days, yookassa_amount) для тарифа из БД.
    При ошибке/отсутствии тарифа — использует fallback-словарь.
    """
    try:
        rows = db.get_tariffs_for_yookassa()
    except Exception as e:
        log.error(
            "[YooKassaWebhook] Failed to load tariffs from DB, using fallback for code=%s: %r",
            tariff_code,
            e,
        )
        return (
            TARIFF_DAYS_FALLBACK.get(tariff_code),
            TARIFF_AMOUNTS_FALLBACK.get(tariff_code),
        )

    for row in rows:
        if row.get("code") != tariff_code:
            continue

        duration_days = row.get("duration_days")
        amount = row.get("yookassa_amount")

        if duration_days is None:
            days_value: int | None = None
        else:
            try:
                days_value = int(duration_days)
            except Exception:
                log.error(
                    "[YooKassaWebhook] Bad duration_days=%r for code=%s, using fallback days",
                    duration_days,
                    tariff_code,
                )
                days_value = None

        if amount is None:
            amount_str: str | None = None
        else:
            try:
                amount_str = format(amount, ".2f")
            except Exception:
                amount_str = str(amount)

        if days_value is None:
            days_value = TARIFF_DAYS_FALLBACK.get(tariff_code)
        if amount_str is None:
            amount_str = TARIFF_AMOUNTS_FALLBACK.get(tariff_code)

        return days_value, amount_str

    # если тариф с таким кодом не нашли в БД
    return (
        TARIFF_DAYS_FALLBACK.get(tariff_code),
        TARIFF_AMOUNTS_FALLBACK.get(tariff_code),
    )




def verify_yookassa_basic_auth(request: web.Request) -> bool:
    """
    Проверка HTTP Basic-авторизации от ЮKassa.
    ЮKassa присылает:
    Authorization: Basic base64(shop_id:secret_key)

    ⚠️ Сейчас НЕ используется в handle_yookassa_webhook,
    но оставляем на будущее, если включишь защищённые вебхуки.
    """
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        log.error("[YooKassaWebhook] SHOP_ID or SECRET_KEY not set")
        return False

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Basic "):
        return False

    try:
        encoded = auth_header.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8")
    except Exception:
        return False

    if ":" not in decoded:
        return False

    shop_id, secret = decoded.split(":", 1)
    return shop_id == YOOKASSA_SHOP_ID and secret == YOOKASSA_SECRET_KEY



import hmac
import hashlib


def verify_yookassa_signature(raw_body: bytes, signature: str | None) -> bool:
    """
    Проверка подписи вебхука ЮKassa (HMAC-SHA256).

    ⚠️ Сейчас НЕ используется в handle_yookassa_webhook,
    т.к. HTTP-уведомления из ЛК ЮKassa её не присылают.
    Оставляем на будущее для "настоящих" вебхуков.
    """
    if not signature:
        return False

    secret = settings.YOOKASSA_WEBHOOK_SECRET
    if not secret:
        log.error("[YooKassaWebhook] YOOKASSA_WEBHOOK_SECRET not set")
        return False

    digest = hmac.new(
        key=secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(digest, signature)


def fetch_payment_from_yookassa(payment_id: str) -> dict | None:
    """
    Тянем платёж из API ЮKassa по payment_id и проверяем его "по-настоящему".

    Возвращаем dict с данными платежа ИЛИ None, если что-то пошло не так.
    """
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        log.error("[YooKassaWebhook] Cannot fetch payment: SHOP_ID or SECRET_KEY not set")
        return None

    url = f"https://api.yookassa.ru/v3/payments/{payment_id}"

    try:
        resp = requests.get(
            url,
            auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            timeout=10,
        )
    except Exception as e:
        log.error(
            "[YooKassaWebhook] Failed to call YooKassa API for payment %s: %r",
            payment_id,
            e,
        )
        return None

    if resp.status_code != 200:
        log.error(
            "[YooKassaWebhook] YooKassa API returned %s for payment %s: %s",
            resp.status_code,
            payment_id,
            resp.text,
        )
        return None

    try:
        data = resp.json()
    except Exception as e:
        log.error(
            "[YooKassaWebhook] Failed to parse YooKassa API JSON for payment %s: %r",
            payment_id,
            e,
        )
        return None

    log.info(
        "[YooKassaWebhook] API payment fetched id=%s status=%s paid=%s test=%r metadata=%r",
        data.get("id"),
        data.get("status"),
        data.get("paid"),
        data.get("test"),
        data.get("metadata"),
    )

    return data

def parse_yookassa_datetime(dt_str: str) -> datetime | None:
    """
    Парсим дату/время из ЮKassa (например, '2026-01-24T11:18:39.321Z')
    в timezone-aware datetime с UTC.
    """
    if not dt_str:
        return None
    try:
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None

async def send_admin_payment_notification(
    telegram_user_id: int,
    telegram_user_name: str | None,
    tariff_code: str,
    amount: str,
    expires_at: datetime,
    is_extension: bool,
) -> None:
    """
    Отправляет админу уведомление о новой оплате / продлении подписки через ЮKassa.
    """
    admin_id = getattr(settings, "ADMIN_TELEGRAM_ID", 0)
    if not admin_id:
        log.warning("[YooKassaWebhook] ADMIN_TELEGRAM_ID is not set, skip admin notification")
        return

    if not settings.TELEGRAM_BOT_TOKEN:
        log.error("[YooKassaWebhook] TELEGRAM_BOT_TOKEN is not set, cannot send admin notification")
        return

    if telegram_user_name:
        username_line = f"@{telegram_user_name}"
    else:
        username_line = "—"

    ref_info = db.get_referrer_with_count(telegram_user_id)
    user_payment_count = db.count_user_paid_subscriptions(telegram_user_id)

    if ref_info:
        ref_username = ref_info.get("referrer_username")
        ref_id = ref_info.get("referrer_telegram_user_id")
        ref_name = f"@{ref_username}" if ref_username else f"ID {ref_id}"
        referred_count = int(ref_info.get("referred_count") or 0)
        paid_count = db.count_referrer_paid_referrals(ref_info["referrer_telegram_user_id"])
        referrer_line = f"{ref_name} ({referred_count}/{paid_count})"
    else:
        referrer_line = "—"

    if is_extension:
        title = "♻️ Продление подписки через ЮKassa"
    else:
        title = "💳 Новая платная подписка через ЮKassa"

    text = (
        f"{title}\n\n"
        f"• Пользователь: <code>{username_line}</code> (ID {telegram_user_id})\n"
        f"• Тариф: <b>{tariff_code}</b> | Сумма: <b>{amount} ₽</b>\n"
        f"• Действует до: <b>{expires_at.strftime('%Y-%m-%d %H:%M:%S %Z')}</b>\n"
        f"• Реферер: {referrer_line}\n"
        f"• Оплат пользователя: ({user_payment_count})\n"
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
            "[YooKassaWebhook] Sent admin notification for payment tg_id=%s tariff=%s amount=%s",
            telegram_user_id,
            tariff_code,
            amount,
        )
    except Exception as e:
        log.error(
            "[YooKassaWebhook] Failed to send admin notification for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
    finally:
        await bot.session.close()



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
    remote_ip = request.remote
    raw_body = await request.read()

    log.info(
        "[YooKassaWebhook] received from %s headers=%r body=%s",
        remote_ip,
        dict(request.headers),
        raw_body.decode("utf-8", errors="replace"),
    )

    log.debug(
        "[YooKassaWebhook] raw_body=%r headers=%r from %s",
        raw_body,
        dict(request.headers),
        remote_ip,
    )

    # ⚠️ Здесь сознательно НЕ проверяем подпись и Basic Auth,
    # т.к. HTTP-уведомления из ЛК ЮKassa их не присылают.
    # Безопасность обеспечим через запрос в API по payment_id.
    try:
        data = json.loads(raw_body.decode("utf-8"))

        log.info(
            "[YooKassaWebhook] parsed event=%s payment_id=%s status=%s metadata=%r",
            data.get("event"),
            (data.get("object") or {}).get("id"),
            (data.get("object") or {}).get("status"),
            (data.get("object") or {}).get("metadata"),
        )

    except Exception as e:
        log.error(
            "[YooKassaWebhook] Failed to parse JSON from %s: %r",
            remote_ip,
            e,
        )
        return web.Response(text="bad json")

    event = data.get("event")
    obj = data.get("object") or {}
    payment_id = obj.get("id")

    if not payment_id or not event:
        log.error("[YooKassaWebhook] No payment_id or event in object")
        return web.Response(text="ok (no payment id)")

    event_id = f"{event}:{payment_id}"
    is_new_event = db.try_register_payment_event("yookassa", str(event_id))
    if not is_new_event:
        log.info(
            "[YooKassaWebhook] Payment %s already processed (payment_events)",
            event_id,
        )
        return web.Response(text="ok (already processed)")

    asyncio.create_task(process_yookassa_event(data, remote_ip))
    return web.Response(text="ok")

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

    days, expected_amount = get_tariff_days_and_amount_from_db(tariff_code)
    if not days:
        log.error("[YooKassaWebhook] Unknown tariff_code=%r", tariff_code)
        return web.Response(text="ok (unknown tariff)")

    if not expected_amount:
        log.error(
            "[YooKassaWebhook] No expected amount configured for tariff_code=%r",
            tariff_code,
        )
        return web.Response(text="ok (no amount for tariff)")


    # 🔍 ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА ЧЕРЕЗ API ЮKassa
    api_payment = fetch_payment_from_yookassa(payment_id)
    if not api_payment:
        # Не смогли проверить платёж — не рискуем, просто отвечаем ok,
        # чтобы ЮKassa не дудосила ретраями, но доступ не выдаём.
        return web.Response(text="ok (cannot verify payment)")

    api_status = api_payment.get("status")
    api_paid = api_payment.get("paid")
    api_metadata = api_payment.get("metadata") or {}
    api_test = api_payment.get("test")
    api_amount_obj = api_payment.get("amount") or {}
    api_amount_value = str(api_amount_obj.get("value"))
    api_currency = api_amount_obj.get("currency")
    api_refunded_obj = api_payment.get("refunded_amount") or {}
    api_refunded_value_raw = api_refunded_obj.get("value") or "0.00"
    try:
        api_refunded_value = Decimal(str(api_refunded_value_raw))
    except Exception:
        api_refunded_value = Decimal("0.00")

    api_created_at_str = api_payment.get("created_at")
    api_created_at_dt = parse_yookassa_datetime(api_created_at_str)

    log.info(
        "[YooKassaWebhook] API check payment_id=%s status=%s paid=%s test=%r api_metadata=%r amount=%s currency=%s refunded_amount=%s created_at=%s",
        payment_id,
        api_status,
        api_paid,
        api_test,
        api_metadata,
        api_amount_value,
        api_currency,
        api_refunded_value,
        api_created_at_str,
    )



    # Статус в API должен быть succeeded и paid == True
    if api_status != "succeeded" or not api_paid:
        log.warning(
            "[YooKassaWebhook] API payment not succeeded or not paid: id=%s status=%s paid=%s",
            payment_id,
            api_status,
            api_paid,
        )
        return web.Response(text="ok (api not succeeded)")

    # Проверяем только валюту (сумму логируем, но не блокируем обработку)
    if api_currency != "RUB":
        log.error(
            "[YooKassaWebhook] Wrong currency for payment %s: expected RUB, got %s (amount=%s)",
            payment_id,
            api_currency,
            api_amount_value,
        )
        return web.Response(text="ok (wrong currency)")


    # Если по этому платежу уже есть возврат — не продлеваем и не создаём подписку
    if api_refunded_value > Decimal("0.00"):
        log.warning(
            "[YooKassaWebhook] Payment %s has refunded_amount=%s — treat as refunded, skip VPN granting",
            payment_id,
            api_refunded_value,
        )
        return web.Response(text="ok (payment refunded)")

    # Метаданные в API должны совпадать с тем, что пришло в вебхуке
    api_tg_id_raw = api_metadata.get("telegram_user_id")
    api_tariff_code = api_metadata.get("tariff_code")


    if str(api_tg_id_raw) != str(telegram_user_id) or api_tariff_code != tariff_code:
        log.error(
            "[YooKassaWebhook] API metadata mismatch for payment %s: webhook(tg_id=%r, tariff=%r) api(tg_id=%r, tariff=%r)",
            payment_id,
            telegram_user_id,
            tariff_code,
            api_tg_id_raw,
            api_tariff_code,
        )
        return web.Response(text="ok (metadata mismatch)")

    # Можно при желании отсеивать test-платежи тут, если живёшь в бою
    # if api_test:
    #     log.info("[YooKassaWebhook] Test payment %s — игнорируем в бою", payment_id)
    #     return web.Response(text="ok (test payment ignored)")

    # Идемпотентность по payment_id
    if not payment_id:
        log.error("[YooKassaWebhook] No payment_id in object")
        return web.Response(text="ok (no payment id)")

    is_new_event = db.try_register_payment_event("yookassa", str(payment_id))
    if not is_new_event:
        log.info(
            "[YooKassaWebhook] Payment %s already processed (payment_events)",
            payment_id,
        )
        return web.Response(text="ok (already processed)")

    # Идемпотентность: если уже создавали подписку с таким event_name, ничего не делаем
    event_name = f"yookassa_payment_succeeded_{payment_id}"
    if payment_id and db.subscription_exists_by_event(event_name):
        log.info(
            "[YooKassaWebhook] Payment %s already processed (event_name=%s)",
            payment_id,
            event_name,
        )
        return web.Response(text="ok (already processed)")

    # =========================
    # ЛОГИКА ПРОДЛЕНИЯ ПОДПИСКИ
    # =========================

    now = datetime.now(timezone.utc)

    # Ищем активные НЕ истёкшие подписки этого tg-пользователя
    active_subs = db.get_active_subscriptions_for_telegram(telegram_user_id)

    log.info(
        "[YooKassaWebhook] active_subscriptions_for_tg_id=%s: %r",
        telegram_user_id,
        active_subs,
    )

    # Среди них ищем именно YooKassa-подписку
    yookassa_sub = None
    for sub in active_subs:
        if sub.get("channel_name") == "YooKassa" or str(sub.get("period", "")).startswith("yookassa_"):
            yookassa_sub = sub
            break

    # Базовая подписка для продления:
    # - если есть активная подписка YooKassa — продлеваем её;
    # - иначе, если есть любая другая активная подписка (например, Heleket) —
    #   продлеваем самую "длинную" (первая в списке).
    base_sub = None

    if yookassa_sub is not None:
        # Дополнительная защита от ретраев старых платежей:
        # если по подписке уже был обработан другой, более "свежий" платёж,
        # а текущий payment_id старше или того же времени — пропускаем.
        last_event_name = str(yookassa_sub.get("last_event_name") or "")
        prefix = "yookassa_payment_succeeded_"
        if last_event_name.startswith(prefix):
            last_payment_id = last_event_name[len(prefix):]
            if last_payment_id and last_payment_id != payment_id:
                last_payment = fetch_payment_from_yookassa(last_payment_id)
                if last_payment:
                    last_created_at_str = last_payment.get("created_at")
                    last_created_at_dt = parse_yookassa_datetime(last_created_at_str)

                    if api_created_at_dt and last_created_at_dt and api_created_at_dt <= last_created_at_dt:
                        log.warning(
                            "[YooKassaWebhook] Payment %s is older or same as already processed payment %s (created_at=%s, last_created_at=%s) — skip extension",
                            payment_id,
                            last_payment_id,
                            api_created_at_str,
                            last_created_at_str,
                        )
                        return web.Response(text="ok (stale payment, not extended)")

        base_sub = yookassa_sub
    elif active_subs:
        # get_active_subscriptions_for_telegram уже сортирует по expires_at DESC, id DESC
        base_sub = active_subs[0]

    if base_sub is not None:
        old_expires_at = base_sub["expires_at"]

        # Если подписка ещё не истекла — добавляем дни к текущей дате окончания,
        # если вдруг expires_at в прошлом (или почти), считаем от now
        base_dt = old_expires_at if old_expires_at > now else now
        new_expires_at = base_dt + timedelta(days=days)

        try:
            db.update_subscription_expiration(
                sub_id=base_sub["id"],
                expires_at=new_expires_at,
                event_name=event_name,
            )
            log.info(
                "[YooKassaWebhook] Extended subscription id=%s (channel=%s) for tg_id=%s: old_expires=%s new_expires=%s (+%s days)",
                base_sub["id"],
                base_sub.get("channel_name"),
                telegram_user_id,
                old_expires_at,
                new_expires_at,
                days,
            )
        except Exception as e:
            log.error(
                "[YooKassaWebhook] Failed to extend subscription id=%s for tg_id=%s: %r",
                base_sub["id"],
                telegram_user_id,
                e,
            )
            return web.Response(text="ok (db extend error)")

        # Реферальные бонусы за продление подписки
        sub_id = base_sub["id"]
        try:
            rewards_result = db.apply_referral_rewards_for_subscription(
                payer_telegram_user_id=telegram_user_id,
                subscription_id=sub_id,
                tariff_code=tariff_code,
                payment_source="yookassa",
                payment_id=payment_id,
            )
            log.info(
                "[YooKassaWebhook] referral_rewards_result for payment_id=%s tg_id=%s: %r",
                payment_id,
                telegram_user_id,
                rewards_result,
            )

            # Уведомляем рефереров о начисленных баллах
            try:
                awards = rewards_result.get("awards") if isinstance(rewards_result, dict) else None
                if awards:
                    for award in awards:
                        ref_tg_id = award.get("telegram_user_id") or award.get("user_telegram_id")
                        points = award.get("points") or award.get("delta") or 0
                        level = award.get("level")

                        if not ref_tg_id or not points:
                            continue

                        await send_referral_reward_notification(
                            telegram_user_id=ref_tg_id,
                            points_delta=points,
                            level=level,
                            tariff_code=tariff_code,
                            payment_channel="YooKassa",
                        )
            except Exception as e:
                log.error(
                    "[YooKassaWebhook] Failed to send referral reward notifications for payment_id=%s: %r",
                    payment_id,
                    e,
                )

        except Exception as e:
            log.error(
                "[YooKassaWebhook] Failed to apply referral rewards for payment_id=%s tg_id=%s: %r",
                payment_id,
                telegram_user_id,
                e,
            )


        # Уведомляем админа о продлении платной подписки
        try:
            await send_admin_payment_notification(
                telegram_user_id=telegram_user_id,
                telegram_user_name=telegram_user_name,
                tariff_code=tariff_code,
                amount=api_amount_value,
                expires_at=new_expires_at,
                is_extension=True,
            )
        except Exception as e:
            log.error(
                "[YooKassaWebhook] Failed to send admin notification about extension for tg_id=%s: %r",
                telegram_user_id,
                e,
            )

        # Уведомляем пользователя о продлении без повторной отправки конфига
        try:
            await send_subscription_extended_notification(
                telegram_user_id=telegram_user_id,
                new_expires_at=new_expires_at,
                tariff_code=tariff_code,
                payment_channel="YooKassa",
            )
        except Exception as e:
            log.error(
                "[YooKassaWebhook] Failed to send extension notification to tg_id=%s: %r",
                telegram_user_id,
                e,
            )
            # Не считаем это критичной ошибкой: подписка уже продлена

        return web.Response(text="ok (extended)")




    # Если активной подписки нет вообще — работаем по старой схеме:

    # создаём новую подписку, новый peer и шлём конфиг.

    # Считаем дату окончания от текущего момента
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
        try:
            db.release_ip_in_pool(client_ip)
        except Exception:
            pass
        log.error(
            "[YooKassaWebhook] Failed to add peer to WireGuard for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        return web.Response(text="ok (wg add error)")

    # Пишем подписку в БД
    try:
        subscription_id = db.insert_subscription(
            tribute_user_id=0,
            telegram_user_id=telegram_user_id,
            telegram_user_name=telegram_user_name,
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

        # Реферальные бонусы за новую платную подписку
        try:
            rewards_result = db.apply_referral_rewards_for_subscription(
                payer_telegram_user_id=telegram_user_id,
                subscription_id=subscription_id,
                tariff_code=tariff_code,
                payment_source="yookassa",
                payment_id=payment_id,
            )
            log.info(
                "[YooKassaWebhook] referral_rewards_result for payment_id=%s tg_id=%s: %r",
                payment_id,
                telegram_user_id,
                rewards_result,
            )

            # Уведомляем рефереров о начисленных баллах
            try:
                awards = rewards_result.get("awards") if isinstance(rewards_result, dict) else None
                if awards:
                    for award in awards:
                        ref_tg_id = award.get("telegram_user_id") or award.get("user_telegram_id")
                        points = award.get("points") or award.get("delta") or 0
                        level = award.get("level")

                        if not ref_tg_id or not points:
                            continue

                        await send_referral_reward_notification(
                            telegram_user_id=ref_tg_id,
                            points_delta=points,
                            level=level,
                            tariff_code=tariff_code,
                            payment_channel="YooKassa",
                        )
            except Exception as e:
                log.error(
                    "[YooKassaWebhook] Failed to send referral reward notifications for payment_id=%s: %r",
                    payment_id,
                    e,
                )

        except Exception as e:
            log.error(
                "[YooKassaWebhook] Failed to apply referral rewards for payment_id=%s tg_id=%s: %r",
                payment_id,
                telegram_user_id,
                e,
            )

    except Exception as e:
        try:
            db.release_ip_in_pool(client_ip)
        except Exception:
            pass
        log.error(
            "[YooKassaWebhook] Failed to insert subscription for tg_id=%s: %r",
            telegram_user_id,
            e,
        )
        return web.Response(text="ok (db error)")


    log.info(
        "[YooKassaWebhook] issuing VPN config tg_id=%s payment_id=%s",
        telegram_user_id,
        payment_id,
    )

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

    # Уведомляем админа о новой платной подписке
    try:
        await send_admin_payment_notification(
            telegram_user_id=telegram_user_id,
            telegram_user_name=telegram_user_name,
            tariff_code=tariff_code,
            amount=api_amount_value,
            expires_at=expires_at,
            is_extension=False,
        )
    except Exception as e:
        log.error(
            "[YooKassaWebhook] Failed to send admin notification about new subscription for tg_id=%s: %r",
            telegram_user_id,
            e,
        )

    return web.Response(text="ok")




def create_app() -> web.Application:
    # импортируем обработчик Heleket локально, чтобы не ловить циклические импорты
    from .heleket_webhook_runner import handle_heleket_webhook

    app = web.Application()
    # путь вебхука — совпадает с тем, что ты указал в ЮKassa:
    # https://pay.maxnetvpn.ru/yookassa/webhook
    app.router.add_post("/yookassa/webhook", handle_yookassa_webhook)

    # вебхук для Heleket (оплата криптой):
    # https://pay.maxnetvpn.ru/heleket/webhook
    app.router.add_post("/heleket/webhook", handle_heleket_webhook)

    return app



if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=8000)
