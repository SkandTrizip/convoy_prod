-- Wallet + immutable ledger, ahead of the scratch-card reward feature.
-- (also created by SQLAlchemy on startup; included here for migrations)

CREATE TABLE IF NOT EXISTS wallets (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    available_balance NUMERIC(12, 2) NOT NULL DEFAULT 0,
    reserved_balance NUMERIC(12, 2) NOT NULL DEFAULT 0,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_wallets_available_nonneg CHECK (available_balance >= 0),
    CONSTRAINT ck_wallets_reserved_nonneg CHECK (reserved_balance >= 0)
);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type VARCHAR(32) NOT NULL,
    amount NUMERIC(12, 2) NOT NULL,
    available_after NUMERIC(12, 2) NOT NULL,
    reserved_after NUMERIC(12, 2) NOT NULL,
    reference_type VARCHAR(32) NOT NULL,
    reference_id VARCHAR(36),
    note TEXT,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wallet_transactions_user_created
    ON wallet_transactions (user_id, created_at);

CREATE TABLE IF NOT EXISTS redeem_requests (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount NUMERIC(12, 2) NOT NULL,
    upi_id VARCHAR(255) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMP,
    processed_by VARCHAR(255),
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_redeem_requests_status ON redeem_requests (status);
CREATE INDEX IF NOT EXISTS idx_redeem_requests_user ON redeem_requests (user_id);

-- The dev server's --reload auto-creates tables from the ORM models (see
-- db/session.py::_create_missing_tables) before this file is ever run by hand,
-- and that path doesn't carry column-level DB defaults (ORM `default=` is
-- client-side only). Make sure the defaults exist regardless of which path
-- created the table first.
ALTER TABLE wallets ALTER COLUMN available_balance SET DEFAULT 0;
ALTER TABLE wallets ALTER COLUMN reserved_balance SET DEFAULT 0;
ALTER TABLE wallets ALTER COLUMN status SET DEFAULT 'active';
ALTER TABLE wallets ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE wallets ALTER COLUMN updated_at SET DEFAULT NOW();
ALTER TABLE wallet_transactions ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE redeem_requests ALTER COLUMN status SET DEFAULT 'pending';
ALTER TABLE redeem_requests ALTER COLUMN created_at SET DEFAULT NOW();

-- Backfill: every existing user gets a zero-balance wallet so credit/redeem
-- code never has to special-case "wallet row doesn't exist yet" for old users.
-- (all wallet columns are ORM-side defaults, not DB server defaults — the
-- auto-created table from SQLAlchemy has no DEFAULT clause on them, so they
-- must be listed explicitly here.)
INSERT INTO wallets (user_id, available_balance, reserved_balance, status, created_at, updated_at)
SELECT id, 0, 0, 'active', NOW(), NOW() FROM users
ON CONFLICT (user_id) DO NOTHING;
