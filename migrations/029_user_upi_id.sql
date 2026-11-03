-- The UPI ID a user withdraws their wallet money to. Additive and safe to re-run.
ALTER TABLE users ADD COLUMN IF NOT EXISTS upi_id VARCHAR(255);
