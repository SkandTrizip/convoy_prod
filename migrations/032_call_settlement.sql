-- Money rules of the advance-payment call flow: what became of a payment once the request ended
-- (kept by the driver / refund due from the driver / needs an admin's review), and how an admin resolved it.
-- ADDITIVE ONLY (nullable columns and one partial index), safe to re-run. Needs 031 applied first.

ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS settlement VARCHAR(16);          -- kept | refund_due | review
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS settlement_reason VARCHAR(48);   -- e.g. driver_never_called
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS resolution VARCHAR(16);          -- requester_right | driver_right | no_fault
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS resolved_by VARCHAR(255);
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS resolution_note TEXT;

CREATE INDEX IF NOT EXISTS idx_call_requests_needs_attention ON call_requests (updated_at DESC)
    WHERE settlement IN ('review', 'refund_due') OR status = 'payment_disputed';
