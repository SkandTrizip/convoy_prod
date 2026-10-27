-- Reels: categories, hashtags, posting location, likes/comments/shares.
-- New tables are also created by SQLAlchemy on startup; the ALTERs on the
-- existing `videos` table are NOT (init_db only creates missing tables) and
-- must be applied manually. Purely additive — no DROP/backfill involved.

ALTER TABLE videos ADD COLUMN IF NOT EXISTS hashtags VARCHAR(50)[] NOT NULL DEFAULT '{}';
ALTER TABLE videos ADD COLUMN IF NOT EXISTS location GEOGRAPHY(POINT, 4326);
ALTER TABLE videos ADD COLUMN IF NOT EXISTS location_city VARCHAR(128);
ALTER TABLE videos ADD COLUMN IF NOT EXISTS location_state VARCHAR(128);
ALTER TABLE videos ADD COLUMN IF NOT EXISTS like_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS comment_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS share_count INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_videos_hashtags ON videos USING gin (hashtags);
CREATE INDEX IF NOT EXISTS idx_videos_location ON videos USING gist (location);

CREATE TABLE IF NOT EXISTS reel_categories (
    id UUID PRIMARY KEY,
    slug VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS video_categories (
    video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    category_id UUID NOT NULL REFERENCES reel_categories(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, category_id)
);
CREATE INDEX IF NOT EXISTS idx_video_categories_category ON video_categories (category_id, video_id);

CREATE TABLE IF NOT EXISTS video_likes (
    video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (video_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_video_likes_user_created ON video_likes (user_id, created_at);

CREATE TABLE IF NOT EXISTS video_comments (
    id UUID PRIMARY KEY,
    video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_video_comments_video_created ON video_comments (video_id, created_at);
CREATE INDEX IF NOT EXISTS idx_video_comments_user ON video_comments (user_id);

CREATE TABLE IF NOT EXISTS video_shares (
    id UUID PRIMARY KEY,
    video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel VARCHAR(32),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_video_shares_video_created ON video_shares (video_id, created_at);
CREATE INDEX IF NOT EXISTS idx_video_shares_user_created ON video_shares (user_id, created_at);

-- init_db may already have created reel_categories from the SQLAlchemy model
-- (before this migration was run), which has no DB-level timestamp defaults, so
-- CREATE TABLE IF NOT EXISTS above would not add them. Make sure they exist.
ALTER TABLE reel_categories
    ALTER COLUMN created_at SET DEFAULT NOW(),
    ALTER COLUMN updated_at SET DEFAULT NOW();

-- Starter categories; manage further via /api/admin/reel-categories.
-- Timestamps are supplied explicitly so this never depends on column defaults.
INSERT INTO reel_categories (id, slug, name, sort_order, created_at, updated_at) VALUES
    (gen_random_uuid(), 'truck-tour', 'Truck Tour', 10, NOW(), NOW()),
    (gen_random_uuid(), 'on-the-road', 'On the Road', 20, NOW(), NOW()),
    (gen_random_uuid(), 'loading-unloading', 'Loading & Unloading', 30, NOW(), NOW()),
    (gen_random_uuid(), 'maintenance-repair', 'Maintenance & Repair', 40, NOW(), NOW()),
    (gen_random_uuid(), 'driver-life', 'Driver Life', 50, NOW(), NOW()),
    (gen_random_uuid(), 'dhaba-food', 'Dhaba & Food', 60, NOW(), NOW()),
    (gen_random_uuid(), 'tips-advice', 'Tips & Advice', 70, NOW(), NOW()),
    (gen_random_uuid(), 'funny', 'Funny', 80, NOW(), NOW()),
    (gen_random_uuid(), 'other', 'Other', 1000, NOW(), NOW())
ON CONFLICT (slug) DO NOTHING;
