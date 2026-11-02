-- Paid 1:1 calls, Stage 7: wallet recharges through Razorpay.
-- Additive and safe to re-run. (The API also creates this table by itself at startup if it is
-- missing; running this first just makes the shared database ready before the code is deployed.)

CREATE TABLE IF NOT EXISTS wallet_recharges (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    razorpay_order_id VARCHAR(64) NOT NULL,
    razorpay_payment_id VARCHAR(64),
    credit_amount NUMERIC(12, 2) NOT NULL,   -- what lands in the wallet
    fee_amount NUMERIC(12, 2) NOT NULL,      -- the gateway fee, paid on top
    total_amount NUMERIC(12, 2) NOT NULL,    -- what the person actually pays
    status VARCHAR(16) NOT NULL DEFAULT 'created',  -- created | paid | review
    wallet_transaction_id UUID,
    note TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    paid_at TIMESTAMP,
    CONSTRAINT uq_wallet_recharges_order UNIQUE (razorpay_order_id),
    CONSTRAINT uq_wallet_recharges_payment UNIQUE (razorpay_payment_id),
    CONSTRAINT ck_wallet_recharges_amounts CHECK (credit_amount > 0 AND fee_amount >= 0 AND total_amount = credit_amount + fee_amount)
);
CREATE INDEX IF NOT EXISTS idx_wallet_recharges_user ON wallet_recharges (user_id, created_at);
