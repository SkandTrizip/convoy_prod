-- Multi-destination truck posts: up to 5 destinations per truck_routes row.
-- Run manually against the DB (this repo's migrations are not auto-applied;
-- the new table below IS auto-created by init_db() on next app startup, but the
-- backfill + column drop on the existing truck_routes table are not — run this
-- file by hand).

CREATE TABLE IF NOT EXISTS truck_route_destinations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    truck_route_id UUID NOT NULL REFERENCES truck_routes(id) ON DELETE CASCADE,
    position SMALLINT NOT NULL,
    destination_name VARCHAR(255) NOT NULL,
    destination_location GEOGRAPHY(POINT, 4326) NOT NULL,
    destination JSONB NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_truck_route_destinations_position UNIQUE (truck_route_id, position),
    CONSTRAINT ck_truck_route_destinations_position_range CHECK (position BETWEEN 1 AND 5)
);

CREATE INDEX IF NOT EXISTS idx_truck_route_destinations_route
    ON truck_route_destinations (truck_route_id);
CREATE INDEX IF NOT EXISTS idx_truck_route_destinations_location
    ON truck_route_destinations USING GIST (destination_location);

-- Backfill: every existing single-destination route becomes destination #1.
INSERT INTO truck_route_destinations (truck_route_id, position, destination_name, destination_location, destination)
SELECT id, 1, destination_name, destination_location, destination
FROM truck_routes
ON CONFLICT (truck_route_id, position) DO NOTHING;

-- Clean cutover: drop the old single-destination columns (and their GiST index) from truck_routes.
DROP INDEX IF EXISTS idx_truck_routes_destination_location;
ALTER TABLE truck_routes
    DROP COLUMN IF EXISTS destination_name,
    DROP COLUMN IF EXISTS destination_location,
    DROP COLUMN IF EXISTS destination;
