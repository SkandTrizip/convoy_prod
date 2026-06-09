-- Hashed phone contacts for mutual-connection discovery (privacy-preserving).

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS contacts_last_updated TIMESTAMP;

CREATE TABLE IF NOT EXISTS user_contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    hashed_number VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_user_contacts_user_hash UNIQUE (user_id, hashed_number)
);

CREATE INDEX IF NOT EXISTS idx_user_contacts_user_id ON user_contacts(user_id);
CREATE INDEX IF NOT EXISTS idx_user_contacts_hashed_number ON user_contacts(hashed_number);
