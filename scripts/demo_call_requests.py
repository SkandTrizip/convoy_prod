#!/usr/bin/env python3
"""Local demo helper for the paid 1:1 call flow: makes a few people send the demo
driver (see scripts/seed_call_demo.py) a call request, through the real API, so
the driver's Home banner has something to show.

Needs, all LOCAL:
  * DATABASE_URL pointing at your local scratch database (checked: this refuses
    anything that is not on this machine, because the dev .env points at the
    shared production database),
  * the API running against that same database on this machine with
    DEV_WALLET_CREDIT_ENABLED=true (each requester is topped up with test money),
  * the driver seeded with the same --driver-mobile.

    export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
    .venv/bin/python scripts/seed_call_demo.py --driver-mobile +917408681257
    DEV_WALLET_CREDIT_ENABLED=true VEUP_PROCESS_KEY= REDIS_URL= RABBITMQ_URL= \
        .venv/bin/uvicorn server:app --port 8010 &
    .venv/bin/python scripts/demo_call_requests.py --driver-mobile +917408681257

Re-running it adds one more request per person who has none open (a person can
have only one open request with the same driver).
"""

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update  # noqa: E402

from db import async_session, engine  # noqa: E402
from db.base import TruckRoute, User  # noqa: E402

REQUESTS = [
    # mobile, name, topic, amount, minutes
    ("+919876543210", "Asha Test", "Route status and load negotiation", 150, 15),
    ("+919111111111", "Ravi Kumar", "Delhi to Jaipur rate for a 32 ton container", 400, 30),
    ("+919222222222", "Vikramjeet Singh Rathore", "Full-load rate and dispatch schedule for next week", 10000, 30),
]


def refuse_unless_local(base_url: str) -> None:
    if engine.url.host not in ("127.0.0.1", "localhost", "::1") or not os.environ.get("DATABASE_URL"):
        sys.exit("Refusing: set DATABASE_URL to a database on this machine (never the shared one).")
    host = urllib.parse.urlparse(base_url).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        sys.exit(f"Refusing: the API at {host!r} is not on this machine.")


def call(base: str, method: str, path: str, body=None, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        base + path, method=method, headers=headers, data=json.dumps(body).encode() if body is not None else None
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        return {"_error": e.code, **json.load(e)}


def login(base: str, mobile: str) -> tuple[str, str]:
    # In dev mode (no SMS key) the API returns the OTP it generated.
    otp = call(base, "POST", "/auth/send-otp", {"mobile": mobile}).get("otp")
    if not otp:
        sys.exit("The API did not return an OTP: start it with VEUP_PROCESS_KEY= (empty) for dev mode.")
    data = call(base, "POST", "/auth/verify-otp", {"mobile": mobile, "otp": otp})
    return data.get("token") or data["accessToken"], data["user"]["_id"]


async def main(driver_mobile: str, base: str) -> None:
    async with async_session() as session:
        driver = (await session.execute(select(User).where(User.mobile == driver_mobile))).scalar_one_or_none()
        if driver is None:
            sys.exit(f"No driver with mobile {driver_mobile}: run scripts/seed_call_demo.py --driver-mobile {driver_mobile} first.")
        post_id = (
            await session.execute(
                select(TruckRoute.id).where(TruckRoute.user_id == driver.id).order_by(TruckRoute.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if post_id is None:
            sys.exit("The driver has no truck post: run scripts/seed_call_demo.py first.")

        for mobile, name, topic, amount, minutes in REQUESTS:
            token, user_id = login(base, mobile)
            await session.execute(update(User).where(User.id == uuid.UUID(user_id)).values(name=name))
            await session.commit()
            call(base, "POST", f"/wallet/{user_id}/dev-credit", {"amount": "10000", "idempotencyKey": str(uuid.uuid4())}, token)
            result = call(
                base,
                "POST",
                "/call-requests",
                {"providerId": str(driver.id), "truckRouteId": str(post_id), "topic": topic, "durationMinutes": minutes, "amount": str(amount)},
                token,
            )
            outcome = result.get("request", {}).get("status") or f"not sent: {result.get('detail')}"
            print(f"{name:26} ₹{amount:<6} -> {outcome}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--driver-mobile", default="9000000001", help="the seeded driver's mobile as stored")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010/api", help="the local API")
    args = parser.parse_args()
    refuse_unless_local(args.base_url)
    asyncio.run(main(args.driver_mobile, args.base_url.rstrip("/")))
