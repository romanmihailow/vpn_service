"""
Тест идемпотентности вебхука ЮKassa: один и тот же payment_id не должен приводить
к двойному продлению подписки. Идемпотентность обеспечивается через
payment_events (try_register_payment_event) и subscription_exists_by_event в process_yookassa_event.

Запуск: PYTHONPATH=. pytest tests/test_yookassa_idempotency.py -v
(conftest подменяет app.db и app.wg моками, БД не требуется)
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_process_yookassa_event_skips_when_subscription_already_has_event():
    """Если подписка уже помечена этим payment_id (subscription_exists_by_event=True), обработка сразу выходит."""
    from app import yookassa_webhook_runner

    process_yookassa_event = yookassa_webhook_runner.process_yookassa_event
    mock_db = yookassa_webhook_runner.db

    payment_id = "test-payment-uuid-123"
    data = {
        "event": "payment.succeeded",
        "object": {
            "id": payment_id,
            "status": "succeeded",
            "metadata": {
                "telegram_user_id": "123456789",
                "tariff_code": "1m",
            },
        },
    }

    mock_db.subscription_exists_by_event.return_value = True  # уже обработан
    mock_db.update_subscription_expiration.reset_mock()
    mock_db.insert_subscription.reset_mock()

    with patch.object(yookassa_webhook_runner, "fetch_payment_from_yookassa"):
        await process_yookassa_event(data, "127.0.0.1")

    mock_db.subscription_exists_by_event.assert_called()
    call_args = mock_db.subscription_exists_by_event.call_args[0][0]
    assert call_args == f"yookassa_payment_succeeded_{payment_id}"
    mock_db.update_subscription_expiration.assert_not_called()
    mock_db.insert_subscription.assert_not_called()


@pytest.mark.asyncio
async def test_handle_webhook_second_request_returns_early_without_spawning_task():
    """Повторный вебхук с тем же event_id не создаёт задачу обработки (try_register возвращает False)."""
    from app.yookassa_webhook_runner import handle_yookassa_webhook

    payment_id = "duplicate-payment-uuid"
    body = {
        "event": "payment.succeeded",
        "object": {
            "id": payment_id,
            "status": "succeeded",
            "metadata": {"telegram_user_id": "111", "tariff_code": "1m"},
        },
    }
    raw = json.dumps(body).encode("utf-8")

    from app.yookassa_webhook_runner import db as mock_db

    mock_db.try_register_payment_event.return_value = False  # уже в payment_events

    request = MagicMock()
    request.remote = "127.0.0.1"
    request.read = AsyncMock(return_value=raw)
    request.headers = {}

    response = await handle_yookassa_webhook(request)

    assert response.text == "ok (already processed)"
    mock_db.try_register_payment_event.assert_called_once()
    event_id = mock_db.try_register_payment_event.call_args[0][1]
    assert event_id == f"payment.succeeded:{payment_id}"
