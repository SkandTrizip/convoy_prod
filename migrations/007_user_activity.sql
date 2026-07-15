-- Recent-activity feed: last 10 searches + last 10 posts per user. Additive, new table only.

CREATE TABLE IF NOT EXISTS user_activity (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    type VARCHAR(16) NOT NULL,
    truck_route_id UUID REFERENCES truck_routes(id) ON DELETE CASCADE,
    search_criteria JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_user_activity_type CHECK (type IN ('search', 'post'))
);

CREATE INDEX IF NOT EXISTS idx_user_activity_user_type_created
    ON user_activity (user_id, type, created_at);
