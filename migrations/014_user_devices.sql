-- Multi-device push notifications: user_devices replaces the single
-- users.push_token column (kept temporarily for un-upgraded app versions —
-- see routers/users.py's legacy /push-token endpoint).
--
-- Documentation only, matching this project's convention: the app's
-- init_db() auto-creates missing tables/indexes on boot (db/session.py),
-- so this table appears automatically. The two new columns on
-- campaign_executions are NOT auto-applied (init_db only creates whole
-- missing tables, never alters existing ones) and must be run by hand.
--
-- users.push_token is deliberately NOT dropped here. It's a separate,
-- later, carefully-reviewed change once the mobile app has shipped
-- deviceId/platform reporting and this has run live for a while (see the
-- migration-safety lesson about verifying backfills before any DROP).

CREATE TABLE IF NOT EXISTS user_devices (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id VARCHAR(255) NOT NULL,
    platform VARCHAR(16) NOT NULL,
    fcm_token TEXT NOT NULL,
    device_name VARCHAR(255),
    app_version VARCHAR(32),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_token_sync TIMESTAMP NOT NULL DEFAULT NOW(),
    last_app_seen TIMESTAMP NOT NULL DEFAULT NOW(),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_user_devices_device_id UNIQUE (device_id),
    CONSTRAINT uq_user_devices_fcm_token UNIQUE (fcm_token)
);

CREATE INDEX IF NOT EXISTS idx_user_devices_user_id ON user_devices (user_id);

ALTER TABLE campaign_executions ADD COLUMN IF NOT EXISTS devices_targeted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE campaign_executions ADD COLUMN IF NOT EXISTS devices_delivered INTEGER NOT NULL DEFAULT 0;
