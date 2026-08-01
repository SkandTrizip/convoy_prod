-- Optional vehicle dimensions, in feet. Not every truck type specifies these
-- (e.g. capacity-only vehicles) — both independently nullable, same as capacity.
ALTER TABLE trucks ADD COLUMN IF NOT EXISTS length_ft DOUBLE PRECISION;
ALTER TABLE trucks ADD COLUMN IF NOT EXISTS height_ft DOUBLE PRECISION;
