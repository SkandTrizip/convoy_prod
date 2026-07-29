-- Admin login (email + password), replacing the old shared X-Admin-Key header.
-- (also created by SQLAlchemy on startup; included here for migrations)

CREATE TABLE IF NOT EXISTS admin_users (
    id UUID PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- The dev server's --reload auto-creates tables from the ORM models before
-- this file is ever run by hand, and that path doesn't carry column-level DB
-- defaults (ORM `default=` is client-side only).
ALTER TABLE admin_users ALTER COLUMN is_active SET DEFAULT TRUE;
ALTER TABLE admin_users ALTER COLUMN created_at SET DEFAULT NOW();
