-- "Advance payment" version of the new on-demand call flow: the requester pays the provider's UPI ID right
-- after the provider accepts, the provider confirms they got it, and only then can the call start.
--   pending -> accepted (waiting for payment) -> payment_claimed -> ready -> ringing -> live -> completed
--   exits: payment_disputed (provider said it never arrived / never answered), rejected, cancelled, expired, failed
-- ADDITIVE ONLY (a superset CHECK, new nullable columns, a wider partial unique index), so the classic
-- wallet-hold backend keeps working on the same database. Safe to re-run. Needs 030 applied first.

ALTER TABLE call_requests DROP CONSTRAINT IF EXISTS ck_call_requests_status;
ALTER TABLE call_requests ADD CONSTRAINT ck_call_requests_status CHECK (status IN (
    'pending', 'accepted', 'ringing', 'live', 'awaiting_feedback', 'awaiting_payment',
    'payment_claimed', 'ready', 'payment_disputed',
    'completed', 'rejected', 'cancelled', 'expired', 'failed'));

ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS payment_claimed_at TIMESTAMP;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS disputed_at TIMESTAMP;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS closed_reason VARCHAR(32);

-- One open request per requester/provider pair now also covers the two new open states.
DROP INDEX IF EXISTS uq_call_requests_open_pair;
CREATE UNIQUE INDEX uq_call_requests_open_pair ON call_requests (requester_id, provider_id)
    WHERE status IN ('pending', 'accepted', 'payment_claimed', 'ready', 'ringing', 'live');

-- Requests left waiting for an after-the-call payment by the previous version of this flow can't be paid
-- any more (that step is gone): close them. Their payment_outcome (if any) stays as it was.
UPDATE call_requests SET status = 'completed', closed_reason = 'legacy_post_call_payment'
 WHERE status = 'awaiting_payment';
