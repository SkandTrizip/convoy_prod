#!/usr/bin/env python3
"""Seeds a LOCAL scratch database for trying the paid 1:1 call flow by hand:
one driver ("Vikramjeet Singh") with one live truck post near Connaught Place,
Delhi. The person who requests the call is whoever logs in to the app.

Refuses any database that is not on this machine (the dev .env points at the
shared production database, so this must never run against it).

    export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
    .venv/bin/python scripts/seed_call_demo.py            # add the demo driver + post
    .venv/bin/python scripts/seed_call_demo.py --cleanup  # remove it again

To log in to the app AS the driver (to try accepting requests), seed them under
the app's own number format and a QA test number (OTP 1234):

    .venv/bin/python scripts/seed_call_demo.py --driver-mobile +917408681257

See my_task.md Part I ("Try the 1:1 request in the app") for the whole recipe.
"""

import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geoalchemy2 import WKTElement  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from db import async_session, engine  # noqa: E402
from db.base import TruckRoute, User  # noqa: E402

DEFAULT_DRIVER_MOBILE = "9000000001"
LAT, LNG = 28.6315, 77.2167


def refuse_unless_local() -> None:
    host = engine.url.host
    if host not in ("127.0.0.1", "localhost", "::1"):
        sys.exit(f"Refusing: DATABASE_URL points at {host!r}, not this machine. Set DATABASE_URL to a local scratch database.")
    if not os.environ.get("DATABASE_URL"):
        sys.exit("Refusing: set DATABASE_URL explicitly so this never falls back to the .env database.")


async def seed(driver_mobile: str) -> None:
    async with async_session() as session:
        driver = (await session.execute(select(User).where(User.mobile == driver_mobile))).scalar_one_or_none()
        if driver is None:
            driver = User(mobile=driver_mobile, name="Vikramjeet Singh", kyc_status="approved", kyc_step="done")
            session.add(driver)
            await session.flush()
        post = TruckRoute(
            truck_id=uuid.uuid4(),
            user_id=driver.id,
            truck_number="DL01AB1234",
            truck_type="Container",
            capacity=32,
            origin_name="Connaught Place",
            contact_name="Vikramjeet Singh",
            contact_number=driver_mobile,
            origin_location=WKTElement(f"POINT({LNG} {LAT})", srid=4326),
            origin={"name": "Connaught Place", "lat": LAT, "lng": LNG, "type": "city", "city": "New Delhi", "state": "Delhi"},
            current_location={"lat": LAT, "lng": LNG},
            status="available",
            expires_at=datetime.utcnow() + timedelta(days=2),
        )
        session.add(post)
        await session.commit()
        print(f"driver {driver.id} (mobile {driver_mobile}), live post {post.id} at {LAT},{LNG}")


async def cleanup(driver_mobile: str) -> None:
    async with async_session() as session:
        driver = (await session.execute(select(User).where(User.mobile == driver_mobile))).scalar_one_or_none()
        if driver is None:
            print("nothing to remove")
            return
        await session.execute(delete(TruckRoute).where(TruckRoute.user_id == driver.id))
        await session.execute(delete(User).where(User.id == driver.id))  # call requests cascade
        await session.commit()
        print("removed the demo driver and their posts")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cleanup", action="store_true", help="remove the demo driver and their posts")
    parser.add_argument("--driver-mobile", default=DEFAULT_DRIVER_MOBILE, help="the driver's mobile as stored (the app uses +91XXXXXXXXXX)")
    args = parser.parse_args()
    refuse_unless_local()
    asyncio.run(cleanup(args.driver_mobile) if args.cleanup else seed(args.driver_mobile))
