ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_step VARCHAR(32) NOT NULL DEFAULT 'aadhaar';

-- Grandfather already-approved users so their step reflects reality
UPDATE users SET kyc_step = 'completed' WHERE kyc_status = 'approved' AND kyc_step = 'aadhaar';
