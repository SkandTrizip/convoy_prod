CREATE TABLE IF NOT EXISTS login_otps (
    id UUID PRIMARY KEY,
    mobile VARCHAR(20) NOT NULL,
    otp VARCHAR(4) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL,
    verified BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_login_otps_mobile ON login_otps (mobile);
CREATE INDEX IF NOT EXISTS idx_login_otps_mobile_expires ON login_otps (mobile, expires_at);
