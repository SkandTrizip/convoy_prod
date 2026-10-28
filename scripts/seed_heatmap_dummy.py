#!/usr/bin/env python3
"""Seed dummy trucks and reels across India to try the heatmaps, and remove them again.

    python scripts/seed_heatmap_dummy.py --status                  # how many dummy rows exist
    python scripts/seed_heatmap_dummy.py --reset                   # remove old dummy rows, then seed fresh
    python scripts/seed_heatmap_dummy.py                           # seed (adds to what's there)
    python scripts/seed_heatmap_dummy.py --cleanup                 # REMOVE every dummy row (one command)

By default it only runs against a local database. The dev .env points at the shared
Azure database (the same one production uses), so add --allow-remote to use it:

    python scripts/seed_heatmap_dummy.py --reset --allow-remote
    python scripts/seed_heatmap_dummy.py --cleanup --allow-remote

How the dummy rows are tagged, and therefore exactly what --cleanup deletes:
  * a pool of dummy users whose mobile starts with "hm-dummy-" (marked `suspended`, so they
    never join a notification campaign's audience and can never log in),
  * every dummy truck, truck post and reel belongs to one of those users.
Nothing else is touched. Real users' rows are never matched by --cleanup. (A post's destination
rows are removed automatically with the post.)

While dummy rows exist they also show up in places other than the heatmap: dummy truck
posts appear in truck search results, and dummy reels (no playable video) appear in the
reels feed. Run --cleanup when you're done testing.

The seed also plants rows the heatmaps must IGNORE (expired posts, unfinished/old/location-less
reels, malformed locations) and, after writing, checks the heatmap service reports exactly the
expected totals.
"""
import argparse
import asyncio
import math
import random
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, func, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from config import HEATMAP_REEL_WINDOW_DAYS  # noqa: E402
from db import async_session, close_db, init_db  # noqa: E402
from db.base import Truck, TruckRoute, TruckRouteDestination, User, Video  # noqa: E402
from db.session import DATABASE_URL  # noqa: E402
from services.heatmap import build_heatmap  # noqa: E402
from services.spatial import make_geography_point  # noqa: E402

DUMMY_MOBILE_PREFIX = "hm-dummy-"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
FAR_FUTURE = datetime(2099, 1, 1)

# (name, state, lat, lng, weight, spread_km) — weight = share of city points, spread_km =
# gaussian sigma of the jitter around the centre.
CITIES = [
    # metros / big freight cities
    ("Delhi", "Delhi", 28.6139, 77.2090, 14, 9),
    ("Mumbai", "Maharashtra", 19.0760, 72.8777, 13, 8),
    ("Bengaluru", "Karnataka", 12.9716, 77.5946, 9, 8),
    ("Chennai", "Tamil Nadu", 13.0827, 80.2707, 8, 7),
    ("Kolkata", "West Bengal", 22.5726, 88.3639, 7, 7),
    ("Hyderabad", "Telangana", 17.3850, 78.4867, 8, 8),
    ("Pune", "Maharashtra", 18.5204, 73.8567, 6, 6),
    ("Ahmedabad", "Gujarat", 23.0225, 72.5714, 6, 6),
    ("Bhiwandi", "Maharashtra", 19.2813, 73.0483, 7, 4),
    # north
    ("Ludhiana", "Punjab", 30.9010, 75.8573, 3, 5),
    ("Amritsar", "Punjab", 31.6340, 74.8723, 2, 4),
    ("Chandigarh", "Chandigarh", 30.7333, 76.7794, 3, 5),
    ("Jalandhar", "Punjab", 31.3260, 75.5762, 2, 4),
    ("Jammu", "Jammu and Kashmir", 32.7266, 74.8570, 1.5, 4),
    ("Srinagar", "Jammu and Kashmir", 34.0837, 74.7973, 1, 4),
    ("Dehradun", "Uttarakhand", 30.3165, 78.0322, 1.5, 4),
    ("Haridwar", "Uttarakhand", 29.9457, 78.1642, 1, 3),
    ("Shimla", "Himachal Pradesh", 31.1048, 77.1734, 0.8, 3),
    ("Meerut", "Uttar Pradesh", 28.9845, 77.7064, 2, 4),
    ("Agra", "Uttar Pradesh", 27.1767, 78.0081, 2.5, 4),
    ("Lucknow", "Uttar Pradesh", 26.8467, 80.9462, 3.5, 6),
    ("Kanpur", "Uttar Pradesh", 26.4499, 80.3319, 3.5, 5),
    ("Varanasi", "Uttar Pradesh", 25.3176, 82.9739, 2, 4),
    ("Prayagraj", "Uttar Pradesh", 25.4358, 81.8463, 2, 4),
    ("Gorakhpur", "Uttar Pradesh", 26.7606, 83.3732, 1.5, 4),
    # rajasthan / gujarat / mp
    ("Jaipur", "Rajasthan", 26.9124, 75.7873, 4, 6),
    ("Jodhpur", "Rajasthan", 26.2389, 73.0243, 2, 5),
    ("Udaipur", "Rajasthan", 24.5854, 73.7125, 1.5, 4),
    ("Kota", "Rajasthan", 25.2138, 75.8648, 1.5, 4),
    ("Ajmer", "Rajasthan", 26.4499, 74.6399, 1, 3),
    ("Surat", "Gujarat", 21.1702, 72.8311, 4, 5),
    ("Vadodara", "Gujarat", 22.3072, 73.1812, 3, 5),
    ("Rajkot", "Gujarat", 22.3039, 70.8022, 2.5, 5),
    ("Kandla", "Gujarat", 23.0333, 70.2167, 2, 4),
    ("Indore", "Madhya Pradesh", 22.7196, 75.8577, 3.5, 5),
    ("Bhopal", "Madhya Pradesh", 23.2599, 77.4126, 2.5, 5),
    ("Jabalpur", "Madhya Pradesh", 23.1815, 79.9864, 1.5, 4),
    ("Gwalior", "Madhya Pradesh", 26.2183, 78.1828, 1.5, 4),
    # maharashtra / goa
    ("Nagpur", "Maharashtra", 21.1458, 79.0882, 4, 6),
    ("Nashik", "Maharashtra", 19.9975, 73.7898, 2.5, 5),
    ("Aurangabad", "Maharashtra", 19.8762, 75.3433, 2, 5),
    ("Kolhapur", "Maharashtra", 16.7050, 74.2433, 1.5, 4),
    ("Solapur", "Maharashtra", 17.6599, 75.9064, 1.5, 4),
    ("Panaji", "Goa", 15.4909, 73.8278, 1, 4),
    # south
    ("Visakhapatnam", "Andhra Pradesh", 17.6868, 83.2185, 2.5, 5),
    ("Vijayawada", "Andhra Pradesh", 16.5062, 80.6480, 2.5, 5),
    ("Tirupati", "Andhra Pradesh", 13.6288, 79.4192, 1, 3),
    ("Warangal", "Telangana", 17.9689, 79.5941, 1, 4),
    ("Mysuru", "Karnataka", 12.2958, 76.6394, 1.5, 4),
    ("Hubballi", "Karnataka", 15.3647, 75.1240, 1.5, 4),
    ("Mangaluru", "Karnataka", 12.9141, 74.8560, 1.5, 4),
    ("Coimbatore", "Tamil Nadu", 11.0168, 76.9558, 2.5, 5),
    ("Madurai", "Tamil Nadu", 9.9252, 78.1198, 2, 4),
    ("Tiruchirappalli", "Tamil Nadu", 10.7905, 78.7047, 1.5, 4),
    ("Salem", "Tamil Nadu", 11.6643, 78.1460, 1.5, 4),
    ("Kochi", "Kerala", 9.9312, 76.2673, 2.5, 5),
    ("Thiruvananthapuram", "Kerala", 8.5241, 76.9366, 1.5, 4),
    ("Kozhikode", "Kerala", 11.2588, 75.7804, 1.5, 4),
    # east / north-east
    ("Patna", "Bihar", 25.5941, 85.1376, 2.5, 5),
    ("Ranchi", "Jharkhand", 23.3441, 85.3096, 2, 4),
    ("Jamshedpur", "Jharkhand", 22.8046, 86.2029, 2, 4),
    ("Dhanbad", "Jharkhand", 23.7957, 86.4304, 1.5, 4),
    ("Bhubaneswar", "Odisha", 20.2961, 85.8245, 2.5, 5),
    ("Cuttack", "Odisha", 20.4625, 85.8830, 1.5, 3),
    ("Raipur", "Chhattisgarh", 21.2514, 81.6296, 2.5, 5),
    ("Bhilai", "Chhattisgarh", 21.1938, 81.3509, 1.5, 4),
    ("Siliguri", "West Bengal", 26.7271, 88.3953, 1.5, 4),
    ("Durgapur", "West Bengal", 23.5204, 87.3119, 1.5, 4),
    ("Guwahati", "Assam", 26.1445, 91.7362, 2, 5),
    ("Dimapur", "Nagaland", 25.9042, 93.7266, 0.7, 3),
    ("Agartala", "Tripura", 23.8315, 91.2868, 0.7, 3),
]

# Highway corridors (start, end); a share of points lie along these lines.
CORRIDORS = [
    ((28.6139, 77.2090), (26.9124, 75.7873)),  # Delhi - Jaipur
    ((28.6139, 77.2090), (27.1767, 78.0081)),  # Delhi - Agra
    ((28.6139, 77.2090), (30.7333, 76.7794)),  # Delhi - Chandigarh
    ((28.6139, 77.2090), (26.8467, 80.9462)),  # Delhi - Lucknow
    ((30.7333, 76.7794), (30.9010, 75.8573)),  # Chandigarh - Ludhiana
    ((26.8467, 80.9462), (25.5941, 85.1376)),  # Lucknow - Patna
    ((19.0760, 72.8777), (18.5204, 73.8567)),  # Mumbai - Pune
    ((19.0760, 72.8777), (19.9975, 73.7898)),  # Mumbai - Nashik
    ((23.0225, 72.5714), (21.1702, 72.8311)),  # Ahmedabad - Surat
    ((21.1702, 72.8311), (19.0760, 72.8777)),  # Surat - Mumbai
    ((26.9124, 75.7873), (23.0225, 72.5714)),  # Jaipur - Ahmedabad
    ((22.7196, 75.8577), (23.2599, 77.4126)),  # Indore - Bhopal
    ((21.1458, 79.0882), (17.3850, 78.4867)),  # Nagpur - Hyderabad
    ((21.1458, 79.0882), (23.1815, 79.9864)),  # Nagpur - Jabalpur
    ((17.3850, 78.4867), (12.9716, 77.5946)),  # Hyderabad - Bengaluru
    ((18.5204, 73.8567), (12.9716, 77.5946)),  # Pune - Bengaluru
    ((12.9716, 77.5946), (13.0827, 80.2707)),  # Bengaluru - Chennai
    ((13.0827, 80.2707), (17.3850, 78.4867)),  # Chennai - Hyderabad
    ((12.9716, 77.5946), (11.0168, 76.9558)),  # Bengaluru - Coimbatore
    ((22.5726, 88.3639), (20.2961, 85.8245)),  # Kolkata - Bhubaneswar
    ((22.5726, 88.3639), (23.5204, 87.3119)),  # Kolkata - Durgapur
    ((20.2961, 85.8245), (17.6868, 83.2185)),  # Bhubaneswar - Visakhapatnam
    ((17.6868, 83.2185), (16.5062, 80.6480)),  # Visakhapatnam - Vijayawada
]
CORRIDOR_SHARE = 0.18

# Tight clusters (transport hubs, mandis, ports) so the finest zoom levels, which only
# show cells holding at least 3 items, have something to display.
HUBS = [
    ("Bhiwandi", "Maharashtra", 19.2813, 73.0483, 0.30),
    ("Delhi", "Delhi", 28.7041, 77.1700, 0.30),  # Azadpur mandi
    ("Delhi", "Delhi", 28.5355, 77.2790, 0.25),  # Okhla
    ("Navi Mumbai", "Maharashtra", 18.9500, 72.9500, 0.25),  # JNPT port
    ("Ludhiana", "Punjab", 30.9010, 75.8573, 0.30),
    ("Chennai", "Tamil Nadu", 13.1700, 80.2900, 0.30),  # Manali
    ("Kolkata", "West Bengal", 22.5400, 88.2800, 0.30),  # Kolkata port
    ("Bengaluru", "Karnataka", 13.0400, 77.5000, 0.30),  # Peenya
    ("Hyderabad", "Telangana", 17.4400, 78.5800, 0.30),
    ("Ahmedabad", "Gujarat", 22.9700, 72.6400, 0.30),  # Naroda
    ("Kandla", "Gujarat", 23.0333, 70.2167, 0.30),
    ("Nagpur", "Maharashtra", 21.1000, 79.1000, 0.30),  # zero-mile hub
    ("Jaipur", "Rajasthan", 26.8500, 75.8000, 0.30),
    ("Kochi", "Kerala", 9.9700, 76.2600, 0.25),
]
HUB_SHARE = 0.14


def _jitter(lat: float, lng: float, sigma_km: float, rng: random.Random) -> tuple[float, float]:
    dy = rng.gauss(0, sigma_km) / 110.574
    dx = rng.gauss(0, sigma_km) / (111.320 * math.cos(math.radians(lat)))
    return lat + dy, lng + dx


def _random_point(rng: random.Random) -> tuple[float, float, str, str]:
    roll = rng.random()
    if roll < HUB_SHARE:
        name, state, lat, lng, sigma_km = rng.choice(HUBS)
        lat, lng = _jitter(lat, lng, sigma_km, rng)
        return lat, lng, name, state
    if roll < HUB_SHARE + CORRIDOR_SHARE:
        (lat1, lng1), (lat2, lng2) = rng.choice(CORRIDORS)
        t = rng.random()
        lat, lng = _jitter(lat1 + (lat2 - lat1) * t, lng1 + (lng2 - lng1) * t, 2.5, rng)
        return lat, lng, "Highway", ""
    city = rng.choices(CITIES, weights=[c[4] for c in CITIES])[0]
    lat, lng = _jitter(city[2], city[3], city[5], rng)
    return lat, lng, city[0], city[1]


def _check_target(allow_remote: bool) -> None:
    host = make_url(DATABASE_URL).host
    if host not in LOCAL_HOSTS and not allow_remote:
        sys.exit(
            f"Refusing to run: DATABASE_URL points at {host!r}, not a local database.\n"
            "That is the shared database, so dummy rows would be visible to real users.\n"
            "If that is what you want, add --allow-remote. Otherwise point DATABASE_URL at a scratch DB."
        )
    print(f"Target database host: {host}")


async def _dummy_user_ids(session) -> list[uuid.UUID]:
    rows = await session.execute(select(User.id).where(User.mobile.like(f"{DUMMY_MOBILE_PREFIX}%")))
    return list(rows.scalars())


async def status(session) -> dict[str, int]:
    user_ids = await _dummy_user_ids(session)
    if not user_ids:
        return {"users": 0, "trucks": 0, "posts": 0, "reels": 0}

    async def count(model) -> int:
        return await session.scalar(select(func.count()).select_from(model).where(model.user_id.in_(user_ids))) or 0

    return {
        "users": len(user_ids),
        "trucks": await count(Truck),
        "posts": await count(TruckRoute),
        "reels": await count(Video),
    }


async def cleanup(session) -> dict[str, int]:
    user_ids = await _dummy_user_ids(session)
    removed = {"users": len(user_ids), "trucks": 0, "posts": 0, "reels": 0}
    if user_ids:
        # truck_route_destinations rows cascade with their route; videos also cascade
        # with the user, but are counted/deleted explicitly to report them.
        removed["posts"] = (await session.execute(delete(TruckRoute).where(TruckRoute.user_id.in_(user_ids)))).rowcount
        removed["trucks"] = (await session.execute(delete(Truck).where(Truck.user_id.in_(user_ids)))).rowcount
        removed["reels"] = (await session.execute(delete(Video).where(Video.user_id.in_(user_ids)))).rowcount
        await session.execute(delete(User).where(User.id.in_(user_ids)))
    await session.commit()
    return removed


def _engagement(rng: random.Random) -> dict[str, int]:
    """Heavy-tailed like/comment/share/view counters, like real reels: most get little,
    a few get a lot. Lets the "popular" ordering of the area feed be tried for real."""
    base = min(int(rng.paretovariate(1.3)) - 1, 400)
    return {
        "like_count": base,
        "comment_count": base // 5,
        "share_count": base // 8,
        "view_count": int(base * rng.uniform(8, 30)),
    }


def _location_json(name: str, state: str, lat: float, lng: float, kind: str = "current") -> dict:
    return {"name": name, "lat": lat, "lng": lng, "type": kind, "city": name, "state": state}


CONTACT_NAMES = [
    "Ramesh Yadav", "Suresh Patel", "Mohammed Iqbal", "Gurpreet Singh", "Anil Sharma", "Vijay Kumar",
    "Rajesh Nair", "Sanjay Deshmukh", "Harish Reddy", "Manoj Tiwari", "Imran Khan", "Balwinder Singh",
    "Prakash Jadhav", "Dinesh Verma", "Karthik Subramanian", "Amit Chauhan", "Ravi Shankar", "Sunil Das",
]


async def seed(session, trucks: int, reels: int, users: int, rng: random.Random) -> dict[str, int]:
    now = datetime.utcnow()
    expected = {
        "live_trucks": 0,
        "reels_in_window": 0,
        "ignored_expired_posts": 0,
        "ignored_old_reels": 0,
        "ignored_unfinished_reels": 0,
        "ignored_reels_without_location": 0,
        "ignored_out_of_range_posts": 0,
        "fallback_to_origin_posts": 0,
    }

    # A small shared pool of dummy users owns everything, so the real `users` table
    # doesn't fill up with thousands of fake rows. suspended = never in a campaign
    # audience, can never log in (the mobile is not a real number anyway).
    pool = [
        User(
            id=uuid.uuid4(),
            mobile=f"{DUMMY_MOBILE_PREFIX}{uuid.uuid4().hex[:10]}",
            name=f"Heatmap Dummy {i + 1}",
            kyc_status="approved",
            kyc_step="done",
            account_status="suspended",
        )
        for i in range(users)
    ]
    session.add_all(pool)
    await session.flush()

    def add_truck_post(lat: float, lng: float, name: str, state: str, **overrides) -> None:
        owner = rng.choice(pool)
        truck = Truck(
            id=uuid.uuid4(),
            user_id=owner.id,
            truck_number=f"HMDUMMY{uuid.uuid4().hex[:8].upper()}",
            truck_type=rng.choice(["Open Body", "Container", "Trailer", "Tanker"]),
            capacity=rng.choice([9, 16, 20, 25]),
            verification_status="verified",
        )
        session.add(truck)
        fields = dict(
            id=uuid.uuid4(),
            truck_id=truck.id,
            user_id=owner.id,
            truck_number=truck.truck_number,
            truck_type=truck.truck_type,
            capacity=truck.capacity,
            origin_name=name,
            # An obviously-not-real number (Indian mobiles never start with 0), so the
            # Call button on a dummy truck can't ring a real person.
            contact_name=rng.choice(CONTACT_NAMES),
            contact_number=f"00000{rng.randint(0, 99999):05d}",
            origin_location=make_geography_point(lng, lat),
            origin=_location_json(name, state, lat, lng, "local"),
            current_location=_location_json(name, state, lat, lng),
            status="active",
            created_at=now,
            expires_at=FAR_FUTURE,
        )
        fields.update(overrides)
        session.add(TruckRoute(**fields))
        # 1-3 destinations, picked from the cities list (never the origin city).
        picks = [c for c in rng.sample(CITIES, 4) if c[0] != name][: rng.randint(1, 3)]
        for position, (d_name, d_state, d_lat, d_lng, *_rest) in enumerate(picks, start=1):
            session.add(
                TruckRouteDestination(
                    id=uuid.uuid4(),
                    truck_route_id=fields["id"],
                    position=position,
                    destination_name=d_name,
                    destination_location=make_geography_point(d_lng, d_lat),
                    destination=_location_json(d_name, d_state, d_lat, d_lng, "local"),
                )
            )

    # --- trucks -------------------------------------------------------------------
    for _ in range(trucks):
        lat, lng, name, state = _random_point(rng)
        add_truck_post(lat, lng, name, state)
        expected["live_trucks"] += 1

    # Rows the trucks heatmap must ignore or handle gracefully:
    for _ in range(max(1, trucks // 20)):  # expired by status and by time
        lat, lng, name, state = _random_point(rng)
        status_and_expiry = rng.choice(
            [{"status": "expired"}, {"status": "active", "expires_at": now - timedelta(hours=1)}]
        )
        add_truck_post(lat, lng, name, state, **status_and_expiry)
        expected["ignored_expired_posts"] += 1
    for _ in range(3):  # malformed current_location -> falls back to the origin point
        lat, lng, name, state = _random_point(rng)
        add_truck_post(lat, lng, name, state, current_location={"lat": "n/a", "lng": None})
        expected["live_trucks"] += 1
        expected["fallback_to_origin_posts"] += 1
    add_truck_post(  # coordinates outside the valid range -> dropped, must not break the query
        28.6, 77.2, "Delhi", "Delhi", current_location=_location_json("Bad", "", 999, 999)
    )
    expected["ignored_out_of_range_posts"] += 1

    # --- reels --------------------------------------------------------------------
    def add_reel(lat: float | None, lng: float | None, city: str, state: str, *, status: str, age_days: float) -> None:
        session.add(
            Video(
                id=uuid.uuid4(),
                user_id=rng.choice(pool).id,
                bunny_video_id=f"hm-dummy-{uuid.uuid4()}",
                title=f"Dummy reel from {city or 'India'}",
                status=status,
                location=make_geography_point(lng, lat) if lat is not None else None,
                location_city=city or None,
                location_state=state or None,
                created_at=now - timedelta(days=age_days),
                **_engagement(rng),
            )
        )

    for _ in range(reels):
        lat, lng, city, state = _random_point(rng)
        add_reel(lat, lng, city, state, status="ready", age_days=rng.uniform(0, HEATMAP_REEL_WINDOW_DAYS - 1))
        expected["reels_in_window"] += 1

    for _ in range(max(1, reels // 20)):  # ready but older than the window
        lat, lng, city, state = _random_point(rng)
        add_reel(lat, lng, city, state, status="ready", age_days=HEATMAP_REEL_WINDOW_DAYS + rng.uniform(2, 60))
        expected["ignored_old_reels"] += 1
    for _ in range(max(1, reels // 20)):  # not playable yet / failed
        lat, lng, city, state = _random_point(rng)
        add_reel(lat, lng, city, state, status=rng.choice(["processing", "pending_upload", "failed", "rejected"]), age_days=1)
        expected["ignored_unfinished_reels"] += 1
    for _ in range(max(1, reels // 20)):  # posted without GPS
        add_reel(None, None, "", "", status="ready", age_days=1)
        expected["ignored_reels_without_location"] += 1

    await session.commit()
    return expected


async def verify(session, expected: dict[str, int]) -> bool:
    """Ask the heatmap service itself and compare with what was seeded. Real rows (if
    any) can only add to the totals, so the totals must be >= the seeded counts."""
    ok = True
    for kind, key in (("trucks", "live_trucks"), ("reels", "reels_in_window")):
        data = await build_heatmap(session, kind, 4)
        total = sum(c["count"] for c in data["cells"])
        good = total >= expected[key] and not data["truncated"]
        ok &= good
        print(f"  heatmap/{kind} @res4: {data['cellCount']} cells, total {total} (seeded {expected[key]}) -> {'OK' if good else 'MISMATCH'}")
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trucks", type=int, default=3000, help="live truck posts to create (default 3000)")
    parser.add_argument("--reels", type=int, default=4000, help="in-window ready reels to create (default 4000)")
    parser.add_argument("--users", type=int, default=60, help="dummy users that own them all (default 60)")
    parser.add_argument("--seed", type=int, default=42, help="random seed, for repeatable data (default 42)")
    parser.add_argument("--status", action="store_true", help="only count the dummy rows that exist")
    parser.add_argument("--cleanup", action="store_true", help="only remove every dummy row")
    parser.add_argument("--reset", action="store_true", help="remove earlier dummy rows, then seed")
    parser.add_argument("--allow-remote", action="store_true", help="allow a non-local (shared) database")
    args = parser.parse_args()

    _check_target(args.allow_remote)
    await init_db()
    code = 0
    try:
        async with async_session() as session:
            if args.status:
                print("Dummy rows present:", await status(session))
                return 0
            if args.cleanup or args.reset:
                print("Removed:", await cleanup(session))
            if args.cleanup:
                print("Dummy rows left:", await status(session))
                return 0
            expected = await seed(session, args.trucks, args.reels, max(1, args.users), rng=random.Random(args.seed))
            print("Seeded:")
            for key, value in expected.items():
                print(f"  {key:34} {value}")
            print("Dummy rows present:", await status(session))
            print("Checking the heatmap service:")
            code = 0 if await verify(session, expected) else 1
    finally:
        await close_db()
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
