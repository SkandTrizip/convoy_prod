-- Support real matching criteria (radius, capacity) on saved searches, and
-- allow truck_type to be unset (meaning "any truck type").
ALTER TABLE search_demands ALTER COLUMN truck_type DROP NOT NULL;
ALTER TABLE search_demands ADD COLUMN IF NOT EXISTS radius_km DOUBLE PRECISION NOT NULL DEFAULT 150;
ALTER TABLE search_demands ADD COLUMN IF NOT EXISTS capacity DOUBLE PRECISION;
