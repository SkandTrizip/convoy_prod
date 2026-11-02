-- Paid 1:1 calls: requests (state machine + money amounts) and the behaviour log.
-- Additive only (two new tables, no change to existing ones) and safe to re-run.
-- (also created by SQLAlchemy on startup; included so you can apply it by hand,
-- same convention as migrations/011_wallet.sql / 019_videos_and_calls.sql)
--
-- Money itself lives in the existing wallets / wallet_transactions tables; the new
-- ledger types (call_hold, call_release, call_debit, call_earning) need no schema
-- change because wallet_transactions.type has no CHECK constraint.

CREATE TABLE IF NOT EXISTS call_requests (
    id UUID PRIMARY KEY,
    requester_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    truck_route_id UUID,
    topic VARCHAR(255) NOT NULL,
    duration_minutes INTEGER NOT NULL,
    amount NUMERIC(12, 2) NOT NULL,
    hold_amount NUMERIC(12, 2) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    expires_at TIMESTAMP NOT NULL,
    responded_at TIMESTAMP,
    channel_name VARCHAR(64) UNIQUE,
    ring_started_at TIMESTAMP,
    ring_attempts INTEGER NOT NULL DEFAULT 0,
    call_started_at TIMESTAMP,
    call_ended_at TIMESTAMP,
    connected_seconds INTEGER,
    end_reason VARCHAR(32),
    feedback_deadline TIMESTAMP,
    satisfied BOOLEAN,
    settled_amount NUMERIC(12, 2),
    settled_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_call_requests_status CHECK (status IN ('pending', 'accepted', 'ringing', 'live', 'awaiting_feedback', 'completed', 'rejected', 'cancelled', 'expired', 'failed')),
    CONSTRAINT ck_call_requests_amount_positive CHECK (amount > 0),
    CONSTRAINT ck_call_requests_hold_range CHECK (hold_amount > 0 AND hold_amount <= amount),
    CONSTRAINT ck_call_requests_duration CHECK (duration_minutes IN (10, 15, 30)),
    CONSTRAINT ck_call_requests_not_self CHECK (requester_id <> provider_id)
);
CREATE INDEX IF NOT EXISTS idx_call_requests_provider_status ON call_requests (provider_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_call_requests_requester_status ON call_requests (requester_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_call_requests_status_expires ON call_requests (status, expires_at);
-- "One open request per requester/provider pair" is enforced by the database itself.
CREATE UNIQUE INDEX IF NOT EXISTS uq_call_requests_open_pair
    ON call_requests (requester_id, provider_id)
    WHERE status IN ('pending', 'accepted', 'ringing', 'live');

CREATE TABLE IF NOT EXISTS call_behaviour_events (
    id UUID PRIMARY KEY,
    call_request_id UUID NOT NULL REFERENCES call_requests(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role VARCHAR(16) NOT NULL,
    event_type VARCHAR(48) NOT NULL,
    source VARCHAR(16) NOT NULL DEFAULT 'server',
    occurred_at TIMESTAMP NOT NULL DEFAULT NOW(),
    details JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_call_behaviour_events_request ON call_behaviour_events (call_request_id, created_at);
CREATE INDEX IF NOT EXISTS idx_call_behaviour_events_user_type ON call_behaviour_events (user_id, event_type, created_at);

-- ORM `default=` is client-side only; make sure the same defaults exist at the DB
-- level even if SQLAlchemy's startup auto-create made the tables first.
ALTER TABLE call_requests ALTER COLUMN status SET DEFAULT 'pending';
ALTER TABLE call_requests ALTER COLUMN ring_attempts SET DEFAULT 0;
ALTER TABLE call_requests ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE call_requests ALTER COLUMN updated_at SET DEFAULT NOW();
ALTER TABLE call_behaviour_events ALTER COLUMN source SET DEFAULT 'server';
ALTER TABLE call_behaviour_events ALTER COLUMN occurred_at SET DEFAULT NOW();
ALTER TABLE call_behaviour_events ALTER COLUMN created_at SET DEFAULT NOW();
