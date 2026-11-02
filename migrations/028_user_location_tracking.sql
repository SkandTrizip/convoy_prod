-- Location sharing for 48 hours after a truck post: one row per user. Additive and safe to re-run.
-- (If the API created the table itself after this change it already exists and this file does nothing.)

CREATE TABLE IF NOT EXISTS user_location_tracking (
    user_id          UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    post_id          UUID,
    started_at       TIMESTAMP NOT NULL,
    ends_at          TIMESTAMP NOT NULL,
    stopped_at       TIMESTAMP,
    stop_reason      VARCHAR(32),
    last_lat         DOUBLE PRECISION,
    last_lng         DOUBLE PRECISION,
    last_accuracy_m  DOUBLE PRECISION,
    last_reported_at TIMESTAMP,
    report_count     INTEGER NOT NULL DEFAULT 0
);
