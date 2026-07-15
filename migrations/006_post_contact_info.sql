-- Per-post contact info: truck owner can post under their own name/mobile (default) or
-- an override (e.g. a driver's number). Additive, non-destructive.

ALTER TABLE truck_routes ADD COLUMN IF NOT EXISTS contact_name VARCHAR(255);
ALTER TABLE truck_routes ADD COLUMN IF NOT EXISTS contact_number VARCHAR(20);
