-- One-time init for vpn_ip_pool (10.8.0.0/16)
-- Excludes: 10.8.0.0, 10.8.0.1 (server), 10.8.255.255 (broadcast)

CREATE TABLE IF NOT EXISTS vpn_ip_pool (
    ip INET PRIMARY KEY,
    allocated BOOLEAN NOT NULL DEFAULT FALSE,
    allocated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_vpn_ip_pool_allocated
    ON vpn_ip_pool (allocated);

INSERT INTO vpn_ip_pool (ip, allocated)
SELECT ip, FALSE
FROM (
    SELECT ('10.8.' || i || '.' || j)::inet AS ip
    FROM generate_series(0, 255) AS i
    CROSS JOIN generate_series(1, 254) AS j
) AS ips
WHERE ip <> '10.8.0.1'::inet
ON CONFLICT (ip) DO NOTHING;

UPDATE vpn_ip_pool p
SET allocated = TRUE,
    allocated_at = NOW()
FROM vpn_subscriptions s
WHERE s.active = TRUE
  AND s.expires_at > NOW()
  AND s.vpn_ip::inet = p.ip;
