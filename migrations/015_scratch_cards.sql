-- Scratch-card reward feature: earn on a qualifying post, reveal on tap.
-- (also created by SQLAlchemy on startup; included here for migrations)

CREATE TABLE IF NOT EXISTS user_reward_stats (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    total_scratches INTEGER NOT NULL DEFAULT 0,
    total_reward_received_paise BIGINT NOT NULL DEFAULT 0,
    first_scratch_at TIMESTAMP,
    updated_at TIMESTAMP,
    CONSTRAINT ck_reward_stats_scratches_nonneg CHECK (total_scratches >= 0),
    CONSTRAINT ck_reward_stats_reward_nonneg CHECK (total_reward_received_paise >= 0)
);

CREATE TABLE IF NOT EXISTS scratch_cards (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id VARCHAR(36) NOT NULL UNIQUE,
    status VARCHAR(16) NOT NULL DEFAULT 'unscratched',
    reward_amount_paise BIGINT,
    earned_at TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL,
    scratched_at TIMESTAMP,
    CONSTRAINT ck_scratch_cards_status CHECK (status IN ('unscratched', 'scratched', 'expired'))
);
CREATE INDEX IF NOT EXISTS idx_scratch_cards_user_earned ON scratch_cards (user_id, earned_at);
CREATE INDEX IF NOT EXISTS idx_scratch_cards_status_expiry ON scratch_cards (status, expires_at);

CREATE TABLE IF NOT EXISTS platform_stats (
    id INTEGER PRIMARY KEY DEFAULT 1,
    total_scratch_count BIGINT NOT NULL DEFAULT 0,
    total_reward_sum_paise BIGINT NOT NULL DEFAULT 0
);

-- ORM `default=` is client-side only — make sure the same defaults exist at
-- the DB level regardless of whether SQLAlchemy's auto-create or this file
-- creates the tables first (see migrations/011_wallet.sql for the same issue).
ALTER TABLE user_reward_stats ALTER COLUMN total_scratches SET DEFAULT 0;
ALTER TABLE user_reward_stats ALTER COLUMN total_reward_received_paise SET DEFAULT 0;
ALTER TABLE scratch_cards ALTER COLUMN status SET DEFAULT 'unscratched';
ALTER TABLE scratch_cards ALTER COLUMN earned_at SET DEFAULT NOW();
ALTER TABLE platform_stats ALTER COLUMN id SET DEFAULT 1;
ALTER TABLE platform_stats ALTER COLUMN total_scratch_count SET DEFAULT 0;
ALTER TABLE platform_stats ALTER COLUMN total_reward_sum_paise SET DEFAULT 0;

-- Singleton row — always id=1, locked with SELECT ... FOR UPDATE on every reveal.
INSERT INTO platform_stats (id, total_scratch_count, total_reward_sum_paise)
VALUES (1, 0, 0)
ON CONFLICT (id) DO NOTHING;
