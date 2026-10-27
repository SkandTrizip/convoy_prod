-- Video reels (Bunny Stream) + 1:1 video calls (Agora).
-- (also created by SQLAlchemy on startup; included here for documentation, same
-- convention as migrations/011_wallet.sql / 015_scratch_cards.sql)

CREATE TABLE IF NOT EXISTS videos (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bunny_video_id VARCHAR(64) NOT NULL UNIQUE,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    status VARCHAR(16) NOT NULL DEFAULT 'pending_upload',
    duration_seconds INTEGER,
    playback_url TEXT,
    thumbnail_url TEXT,
    rejection_reason TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_videos_status CHECK (status IN ('pending_upload', 'processing', 'ready', 'rejected', 'failed'))
);
CREATE INDEX IF NOT EXISTS idx_videos_user_created ON videos (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_videos_status_created ON videos (status, created_at);

CREATE TABLE IF NOT EXISTS one_to_one_calls (
    id UUID PRIMARY KEY,
    host_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    guest_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    channel_name VARCHAR(64) NOT NULL UNIQUE,
    title VARCHAR(255) NOT NULL,
    host_name VARCHAR(255) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'live',
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_one_to_one_calls_status CHECK (status IN ('live', 'ended'))
);
CREATE INDEX IF NOT EXISTS idx_one_to_one_calls_status_started ON one_to_one_calls (status, started_at);
CREATE INDEX IF NOT EXISTS idx_one_to_one_calls_host ON one_to_one_calls (host_user_id);
CREATE INDEX IF NOT EXISTS idx_one_to_one_calls_guest ON one_to_one_calls (guest_user_id);

-- ORM `default=` is client-side only — make sure the same defaults exist at the
-- DB level regardless of whether SQLAlchemy's auto-create or this file creates
-- the tables first (see migrations/011_wallet.sql for the same issue).
ALTER TABLE videos ALTER COLUMN status SET DEFAULT 'pending_upload';
ALTER TABLE videos ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE videos ALTER COLUMN updated_at SET DEFAULT NOW();
ALTER TABLE one_to_one_calls ALTER COLUMN status SET DEFAULT 'live';
ALTER TABLE one_to_one_calls ALTER COLUMN started_at SET DEFAULT NOW();
ALTER TABLE one_to_one_calls ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE one_to_one_calls ALTER COLUMN updated_at SET DEFAULT NOW();
