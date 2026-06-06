-- Convoy PostGIS migration
-- Requires PostgreSQL 14+ with PostGIS extension

CREATE EXTENSION IF NOT EXISTS postgis;

-- ---------------------------------------------------------------------------
-- Core tables (also created by SQLAlchemy on startup; included for migrations)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY,
    mobile VARCHAR(20) UNIQUE NOT NULL,
    name VARCHAR(255),
    profile_photo TEXT,
    kyc_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    account_status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_date TIMESTAMP NOT NULL DEFAULT NOW(),
    push_token TEXT
);

CREATE TABLE IF NOT EXISTS locations (
    id UUID PRIMARY KEY,
    name VARCHAR(512) NOT NULL,
    lat DOUBLE PRECISION NOT NULL,
    lng DOUBLE PRECISION NOT NULL,
    pincode VARCHAR(12),
    city VARCHAR(128),
    state VARCHAR(128),
    google_place_id VARCHAR(255) UNIQUE,
    source VARCHAR(16) NOT NULL DEFAULT 'google'
);
CREATE INDEX IF NOT EXISTS idx_locations_name ON locations (name);
CREATE INDEX IF NOT EXISTS idx_locations_city ON locations (city);
CREATE INDEX IF NOT EXISTS idx_locations_pincode ON locations (pincode);

CREATE TABLE IF NOT EXISTS trucks (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    truck_number VARCHAR(32) NOT NULL,
    truck_type VARCHAR(64) NOT NULL,
    capacity DOUBLE PRECISION,
    verification_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    vahan_data JSONB,
    added_date TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trucks_user_id ON trucks (user_id);
CREATE INDEX IF NOT EXISTS idx_trucks_number ON trucks (truck_number);

CREATE TABLE IF NOT EXISTS truck_routes (
    id UUID PRIMARY KEY,
    truck_id UUID NOT NULL REFERENCES trucks(id),
    user_id UUID NOT NULL REFERENCES users(id),
    truck_number VARCHAR(32) NOT NULL,
    truck_type VARCHAR(64) NOT NULL,
    capacity DOUBLE PRECISION,
    origin_name VARCHAR(255) NOT NULL,
    destination_name VARCHAR(255) NOT NULL,
    origin_location GEOGRAPHY(POINT, 4326) NOT NULL,
    destination_location GEOGRAPHY(POINT, 4326) NOT NULL,
    origin JSONB NOT NULL,
    destination JSONB NOT NULL,
    current_location JSONB,
    available_date DATE NOT NULL,
    price NUMERIC(12, 2),
    status VARCHAR(32) NOT NULL DEFAULT 'available',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_truck_routes_origin_location
    ON truck_routes USING GIST (origin_location);
CREATE INDEX IF NOT EXISTS idx_truck_routes_destination_location
    ON truck_routes USING GIST (destination_location);
CREATE INDEX IF NOT EXISTS idx_truck_routes_status_date
    ON truck_routes (status, available_date);

CREATE TABLE IF NOT EXISTS bookings (
    id UUID PRIMARY KEY,
    truck_route_id UUID NOT NULL REFERENCES truck_routes(id),
    user_id UUID NOT NULL REFERENCES users(id),
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    price NUMERIC(12, 2),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bookings_route ON bookings (truck_route_id);
CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings (user_id);

-- ---------------------------------------------------------------------------
-- Sample data: Mumbai→Jaipur, Pune→Delhi, Nashik→Ajmer
-- Replace user/truck UUIDs with real values in production
-- ---------------------------------------------------------------------------

-- Sample owner
INSERT INTO users (id, mobile, name, kyc_status)
VALUES ('11111111-1111-1111-1111-111111111111', '+919900000001', 'Sample Owner', 'approved')
ON CONFLICT (mobile) DO NOTHING;

INSERT INTO trucks (id, user_id, truck_number, truck_type, capacity, verification_status)
VALUES
    ('22222222-2222-2222-2222-222222222222', '11111111-1111-1111-1111-111111111111', 'MH12AB1234', 'Container', 20, 'verified'),
    ('33333333-3333-3333-3333-333333333333', '11111111-1111-1111-1111-111111111111', 'MH14CD5678', 'Open Body', 16, 'verified'),
    ('44444444-4444-4444-4444-444444444444', '11111111-1111-1111-1111-111111111111', 'MH15EF9012', 'Trailer', 25, 'verified')
ON CONFLICT DO NOTHING;

INSERT INTO truck_routes (
    id, truck_id, user_id, truck_number, truck_type, capacity,
    origin_name, destination_name,
    origin_location, destination_location,
    origin, destination, available_date, price, status, expires_at
) VALUES
(
    'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    '22222222-2222-2222-2222-222222222222',
    '11111111-1111-1111-1111-111111111111',
    'MH12AB1234', 'Container', 20,
    'Mumbai', 'Jaipur',
    ST_SetSRID(ST_MakePoint(72.8777, 19.0760), 4326)::geography,
    ST_SetSRID(ST_MakePoint(75.7873, 26.9124), 4326)::geography,
    '{"name":"Mumbai","lat":19.0760,"lng":72.8777,"city":"Mumbai","state":"Maharashtra"}',
    '{"name":"Jaipur","lat":26.9124,"lng":75.7873,"city":"Jaipur","state":"Rajasthan"}',
    '2026-06-10', 45000.00, 'available', NOW() + INTERVAL '7 days'
),
(
    'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
    '33333333-3333-3333-3333-333333333333',
    '11111111-1111-1111-1111-111111111111',
    'MH14CD5678', 'Open Body', 16,
    'Pune', 'Delhi',
    ST_SetSRID(ST_MakePoint(73.8567, 18.5204), 4326)::geography,
    ST_SetSRID(ST_MakePoint(77.1025, 28.7041), 4326)::geography,
    '{"name":"Pune","lat":18.5204,"lng":73.8567,"city":"Pune","state":"Maharashtra"}',
    '{"name":"Delhi","lat":28.7041,"lng":77.1025,"city":"Delhi","state":"Delhi"}',
    '2026-06-10', 52000.00, 'available', NOW() + INTERVAL '7 days'
),
(
    'cccccccc-cccc-cccc-cccc-cccccccccccc',
    '44444444-4444-4444-4444-444444444444',
    '11111111-1111-1111-1111-111111111111',
    'MH15EF9012', 'Trailer', 25,
    'Nashik', 'Ajmer',
    ST_SetSRID(ST_MakePoint(73.7898, 19.9975), 4326)::geography,
    ST_SetSRID(ST_MakePoint(74.6399, 26.4499), 4326)::geography,
    '{"name":"Nashik","lat":19.9975,"lng":73.7898,"city":"Nashik","state":"Maharashtra"}',
    '{"name":"Ajmer","lat":26.4499,"lng":74.6399,"city":"Ajmer","state":"Rajasthan"}',
    '2026-06-10', 38000.00, 'available', NOW() + INTERVAL '7 days'
)
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- Optimized spatial search query
-- ---------------------------------------------------------------------------
-- Parameters:
--   :origin_lng, :origin_lat, :dest_lng, :dest_lat, :radius_m, :search_date
--
-- SELECT
--     tr.id AS truck_route_id,
--     tr.truck_number,
--     tr.origin_name AS origin,
--     tr.destination_name AS destination,
--     ST_Distance(tr.origin_location, ST_SetSRID(ST_MakePoint(:origin_lng, :origin_lat), 4326)::geography) / 1000.0 AS origin_distance_km,
--     ST_Distance(tr.destination_location, ST_SetSRID(ST_MakePoint(:dest_lng, :dest_lat), 4326)::geography) / 1000.0 AS destination_distance_km
-- FROM truck_routes tr
-- WHERE tr.status = 'available'
--   AND tr.available_date >= :search_date
--   AND ST_DWithin(
--         tr.origin_location,
--         ST_SetSRID(ST_MakePoint(:origin_lng, :origin_lat), 4326)::geography,
--         :radius_m
--       )
--   AND ST_DWithin(
--         tr.destination_location,
--         ST_SetSRID(ST_MakePoint(:dest_lng, :dest_lat), 4326)::geography,
--         :radius_m
--       )
-- ORDER BY origin_distance_km + destination_distance_km
-- LIMIT 100;

-- ---------------------------------------------------------------------------
-- Scaling notes (EXPLAIN ANALYZE recommendations)
-- ---------------------------------------------------------------------------
-- 10K routes: GiST indexes on origin/destination are sufficient; sub-50ms typical.
-- 100K routes: Add partial index ON truck_routes (available_date, status)
--              WHERE status = 'available'; consider BRIN on available_date.
-- 1M routes: Partition truck_routes by available_date (monthly);
--            use KNN operator <-> for pre-filtering nearest origins before ST_DWithin;
--            read replicas for search; connection pooling (PgBouncer).
