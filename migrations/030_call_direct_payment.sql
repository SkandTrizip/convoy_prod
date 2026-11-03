-- "New on-demand call flow": the requester pays the provider's UPI ID directly after the call (no wallet hold).
-- ADDITIVE ONLY, so the classic wallet-hold backend keeps working on the same database:
--   * new nullable / defaulted columns on call_requests (the classic code never reads or writes them),
--   * the status CHECK is widened (a superset: every classic value is still allowed),
--   * one new table.
-- Safe to re-run. Needs 023_call_requests.sql applied first.

ALTER TABLE call_requests DROP CONSTRAINT IF EXISTS ck_call_requests_status;
ALTER TABLE call_requests ADD CONSTRAINT ck_call_requests_status CHECK (status IN (
    'pending', 'accepted', 'ringing', 'live', 'awaiting_feedback', 'awaiting_payment',
    'completed', 'rejected', 'cancelled', 'expired', 'failed'));

-- How the payment ended: paid | refused (>= threshold, said no) | declined (< threshold, said no, with a reason)
-- | expired (never answered). NULL while nothing has been decided (and for every classic request).
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS payment_outcome VARCHAR(16);
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS paid_amount NUMERIC(12, 2);
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS payment_decided_at TIMESTAMP;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS payment_reason TEXT;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS payee_upi_id VARCHAR(255);
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS upi_reference VARCHAR(100);
-- The provider's answer to "did you receive it?" (NULL = not answered yet).
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS provider_confirmed BOOLEAN;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS provider_confirmed_at TIMESTAMP;
-- Did the call reach the threshold, was the provider's mic AND camera on for most of it, and so does a
-- refusal count as a strike (set when the call ends / the payment is decided).
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS threshold_met BOOLEAN;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS provider_media_on BOOLEAN;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS counts_as_strike BOOLEAN NOT NULL DEFAULT FALSE;
-- What each side reported while both were in the call: one count per heartbeat (about every 5 s).
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS provider_beats INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS provider_mic_beats INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS provider_camera_beats INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS requester_beats INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS requester_mic_beats INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS requester_camera_beats INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS requester_saw_video_beats INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_call_requests_requester_outcome ON call_requests (requester_id, payment_outcome);

-- An admin "forgave" this user's strikes at this time: only strikes after it count towards the block.
CREATE TABLE IF NOT EXISTS call_payment_standing (
    user_id    UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    reset_at   TIMESTAMP NOT NULL,
    reset_by   VARCHAR(255)
);
