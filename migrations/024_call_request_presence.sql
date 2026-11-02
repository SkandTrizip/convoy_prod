-- Paid 1:1 calls, Stage 2: live-call presence columns on call_requests.
-- Additive and safe to re-run. Needs 023_call_requests.sql applied first.
-- (If the API created the tables itself after this change they already have these
-- columns and this file does nothing.)

ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS requester_last_seen_at TIMESTAMP;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS provider_last_seen_at TIMESTAMP;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS requester_peer_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS provider_peer_present BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS last_tick_at TIMESTAMP;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS connected_ms BIGINT NOT NULL DEFAULT 0;
