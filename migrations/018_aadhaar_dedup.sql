-- Cross-account Aadhaar dedup: HMAC of the normalized Aadhaar number (never
-- the raw number itself). Partial unique index means only an *approved*
-- record's hash counts as "taken" — pending/rejected attempts never block
-- anything, and a user re-verifying their own Aadhaar (updating their own
-- single kyc_records row) can never self-conflict.
ALTER TABLE kyc_records ADD COLUMN IF NOT EXISTS aadhaar_hash VARCHAR(64);

CREATE UNIQUE INDEX IF NOT EXISTS ux_kyc_records_aadhaar_hash_approved
    ON kyc_records (aadhaar_hash)
    WHERE status = 'approved';
