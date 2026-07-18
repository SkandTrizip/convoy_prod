-- Remove the deprecated available_date column from truck_routes.
--
-- available_date was originally meant to represent "when the truck becomes
-- available", but it duplicated/conflicted with expires_at (which already
-- governs whether a post is live via status + expires_at > NOW()). Search
-- was filtering on `available_date >= :search_date`, which excluded posts
-- whose available_date was in the past relative to the search date even
-- though they were still active and unexpired. expires_at is the correct
-- and only signal for post liveness going forward.

DROP INDEX IF EXISTS idx_truck_routes_status_date;

ALTER TABLE truck_routes DROP COLUMN IF EXISTS available_date;
