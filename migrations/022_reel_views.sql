-- Reel view counter + composite index for per-creator reel listings/aggregates.
-- ALTER on existing `videos` is NOT applied by init_db; run manually (additive only).
ALTER TABLE videos ADD COLUMN IF NOT EXISTS view_count BIGINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_videos_user_status_created ON videos (user_id, status, created_at);
