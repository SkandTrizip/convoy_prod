-- Copy vehicle length/height onto the post at create/edit time, so search
-- results can display them. Display-only — not a search filter, and
-- deliberately not read by smart-match notification matching.
ALTER TABLE truck_routes ADD COLUMN IF NOT EXISTS length_ft DOUBLE PRECISION;
ALTER TABLE truck_routes ADD COLUMN IF NOT EXISTS height_ft DOUBLE PRECISION;
