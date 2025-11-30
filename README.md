source venv/bin/activate
ps aux | grep uvicorn


curl http://127.0.0.1:8000/health

python -m app.tg_bot_runner




SELECT * FROM vpn_subscriptions

SELECT id,
       tribute_user_id,
       telegram_user_id,
       subscription_id,
       period,
       channel_name,
       vpn_ip,
       active,
       last_event_name,
       expires_at
FROM vpn_subscriptions
ORDER BY id DESC
LIMIT 5;


UPDATE vpn_subscriptions
SET expires_at = '2025-11-30 15:30:08.68074+00'
WHERE id = 2;
