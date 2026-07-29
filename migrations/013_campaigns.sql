-- Campaign Management System: data-driven notification campaigns (audience,
-- content pool, schedule, delivery rules, execution + notification history).
-- (also created by SQLAlchemy on startup; included here for migrations)

CREATE TABLE IF NOT EXISTS campaigns (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    campaign_type VARCHAR(16) NOT NULL DEFAULT 'manual',
    status VARCHAR(16) NOT NULL DEFAULT 'draft',
    audience_filter JSONB NOT NULL DEFAULT '{}',
    created_by UUID REFERENCES admin_users(id) ON DELETE SET NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    activated_at TIMESTAMP,
    archived_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS campaign_contents (
    id UUID PRIMARY KEY,
    campaign_id UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    body TEXT NOT NULL,
    data_payload JSONB NOT NULL DEFAULT '{}',
    sort_order SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_campaign_contents_campaign ON campaign_contents (campaign_id, sort_order);

CREATE TABLE IF NOT EXISTS campaign_schedules (
    campaign_id UUID PRIMARY KEY REFERENCES campaigns(id) ON DELETE CASCADE,
    schedule_type VARCHAR(16) NOT NULL DEFAULT 'immediate',
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
    start_date TIMESTAMP NOT NULL,
    end_date TIMESTAMP,
    time_of_day VARCHAR(5),
    day_of_week SMALLINT,
    day_of_month SMALLINT,
    cron_expression VARCHAR(64),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    next_run_at TIMESTAMP,
    last_run_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS campaign_delivery_rules (
    campaign_id UUID PRIMARY KEY REFERENCES campaigns(id) ON DELETE CASCADE,
    max_per_user_per_day SMALLINT NOT NULL DEFAULT 1,
    min_interval_minutes INTEGER NOT NULL DEFAULT 0,
    quiet_hours_start VARCHAR(5),
    quiet_hours_end VARCHAR(5),
    respect_preferences BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS campaign_executions (
    id UUID PRIMARY KEY,
    campaign_id UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    status VARCHAR(16) NOT NULL DEFAULT 'running',
    triggered_by VARCHAR(16) NOT NULL DEFAULT 'schedule',
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMP,
    audience_size INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    no_token_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_campaign_executions_campaign ON campaign_executions (campaign_id, started_at);

CREATE TABLE IF NOT EXISTS campaign_notification_logs (
    id UUID PRIMARY KEY,
    campaign_id UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    execution_id UUID REFERENCES campaign_executions(id) ON DELETE CASCADE,
    content_id UUID REFERENCES campaign_contents(id) ON DELETE SET NULL,
    user_id UUID NOT NULL,
    status VARCHAR(16) NOT NULL,
    sent_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_campaign_notif_log_campaign_user ON campaign_notification_logs (campaign_id, user_id, sent_at);
CREATE INDEX IF NOT EXISTS ix_campaign_notification_logs_user_id ON campaign_notification_logs (user_id);
