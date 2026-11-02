"""Location sharing for 48 hours after a truck post.

Needs the local scratch database, like test_call_requests.py:

    export CALL_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
    .venv/bin/python -m pytest tests/test_location_tracking.py -q
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from config import LOCATION_REPORT_MIN_SECONDS, LOCATION_TRACKING_HOURS
from db.base import TruckRoute, UserLocationTracking
from services import location_tracking as svc

from tests.test_call_requests import (  # noqa: F401  (fixtures are used by name)
    anyio_backend,
    make_post,
    make_user,
    needs_db,
    schema,
    sessions,
)
from tests.test_call_requests_api import api, auth  # noqa: F401

pytestmark = [needs_db, pytest.mark.anyio]


class Clock:
    def __init__(self):
        self.t = datetime.utcnow()

    def now(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(svc, "_now", c.now)
    return c


async def start(sessions, user, post=None):
    async with sessions() as s:
        return await svc.start_tracking(s, user.id, post.id if post else None)


async def report(sessions, user, lat=19.07, lng=72.87, **kw):
    async with sessions() as s:
        return await svc.record_location(s, user.id, lat, lng, **kw)


async def row_of(sessions, user):
    async with sessions() as s:
        return await s.get(UserLocationTracking, user.id)


async def pin_of(sessions, post):
    async with sessions() as s:
        return (await s.get(TruckRoute, post.id)).current_location


# --- the window ---------------------------------------------------------------------------------


async def test_a_post_opens_a_48_hour_window(sessions, clock):
    user = await make_user(sessions)
    status = await start(sessions, user)
    row = await row_of(sessions, user)
    assert status["active"] is True and status["intervalMinutes"] == 30
    assert row.ends_at - row.started_at == timedelta(hours=LOCATION_TRACKING_HOURS) == timedelta(hours=48)


async def test_no_window_means_not_active_and_reports_are_ignored(sessions, clock):
    user = await make_user(sessions)
    async with sessions() as s:
        assert (await svc.get_status(s, user.id))["active"] is False
    result = await report(sessions, user)
    assert result["active"] is False and result["recorded"] is False
    assert await row_of(sessions, user) is None


async def test_the_window_ends_after_48_hours(sessions, clock):
    user = await make_user(sessions)
    await start(sessions, user)
    clock.advance(hours=47, minutes=59)
    assert (await report(sessions, user))["recorded"] is True
    clock.advance(minutes=2)  # now 48 h 1 min after the post
    result = await report(sessions, user, 11.0, 77.0)
    assert result["active"] is False and result["recorded"] is False
    row = await row_of(sessions, user)
    assert row.stop_reason == "expired" and row.last_lat is None  # forgotten
    async with sessions() as s:
        assert (await svc.get_status(s, user.id))["active"] is False


async def test_a_new_post_restarts_the_window_after_it_ended(sessions, clock):
    user = await make_user(sessions)
    await start(sessions, user)
    clock.advance(hours=49)
    await report(sessions, user)  # notices it ran out
    status = await start(sessions, user)  # posts again
    assert status["active"] is True
    assert (await report(sessions, user))["recorded"] is True


# --- a report ------------------------------------------------------------------------------------


async def test_a_report_is_kept_and_moves_the_live_post_keeping_its_label(sessions, clock):
    user = await make_user(sessions)
    post = await make_post(sessions, user)
    async with sessions() as s:
        p = await s.get(TruckRoute, post.id)
        p.current_location = {"name": "Delhi", "lat": 28.6, "lng": 77.2, "type": "local"}
        await s.commit()
    await start(sessions, user, post)

    result = await report(sessions, user, 19.07, 72.87, accuracy=35.0)
    assert result["recorded"] is True
    row = await row_of(sessions, user)
    assert (row.last_lat, row.last_lng, row.last_accuracy_m, row.report_count) == (19.07, 72.87, 35.0, 1)
    assert await pin_of(sessions, post) == {"name": "Delhi", "lat": 19.07, "lng": 72.87, "type": "local"}


async def test_the_place_the_phone_names_replaces_the_label(sessions, clock):
    user = await make_user(sessions)
    post = await make_post(sessions, user)
    await start(sessions, user, post)
    await report(sessions, user, place={"name": "Pune, Maharashtra", "city": "Pune", "state": "", "pincode": None})
    pin = await pin_of(sessions, post)
    assert pin["name"] == "Pune, Maharashtra" and pin["city"] == "Pune"
    assert "state" not in pin and "pincode" not in pin  # empty ones are not written


async def test_an_expired_post_is_not_moved_but_the_user_is_still_tracked(sessions, clock):
    user = await make_user(sessions)
    old = await make_post(sessions, user, expires_in=timedelta(hours=-1))
    async with sessions() as s:
        p = await s.get(TruckRoute, old.id)
        p.current_location = {"name": "Delhi", "lat": 28.6, "lng": 77.2}
        await s.commit()
    await start(sessions, user, old)
    assert (await report(sessions, user, 12.0, 76.0))["recorded"] is True
    assert (await row_of(sessions, user)).last_lat == 12.0  # hours 24-48: the user's own last position
    assert (await pin_of(sessions, old))["lat"] == 28.6  # the gone post is left alone


async def test_a_report_right_after_the_last_is_acknowledged_but_not_stored(sessions, clock):
    user = await make_user(sessions)
    await start(sessions, user)
    assert (await report(sessions, user, 1.0, 1.0))["recorded"] is True
    clock.advance(seconds=LOCATION_REPORT_MIN_SECONDS - 1)
    again = await report(sessions, user, 2.0, 2.0)
    assert again["active"] is True and again["recorded"] is False
    assert (await row_of(sessions, user)).last_lat == 1.0
    clock.advance(seconds=2)
    assert (await report(sessions, user, 3.0, 3.0))["recorded"] is True
    assert (await row_of(sessions, user)).report_count == 2


# --- stopping ------------------------------------------------------------------------------------


async def test_stopping_ends_it_and_forgets_the_position(sessions, clock):
    user = await make_user(sessions)
    await start(sessions, user)
    await report(sessions, user)
    async with sessions() as s:
        status = await svc.stop_tracking(s, user.id, svc.LOGOUT)
    assert status["active"] is False
    row = await row_of(sessions, user)
    assert row.stop_reason == "logout" and (row.last_lat, row.last_lng) == (None, None)
    clock.advance(minutes=31)
    assert (await report(sessions, user))["recorded"] is False


async def test_stopping_when_there_is_nothing_to_stop_is_harmless(sessions, clock):
    user = await make_user(sessions)
    async with sessions() as s:
        assert (await svc.stop_tracking(s, user.id, svc.LOGOUT))["active"] is False


async def test_deleting_the_post_that_started_it_stops_it(sessions, clock):
    user = await make_user(sessions)
    post = await make_post(sessions, user, expires_in=timedelta(hours=-1))  # expired: nothing live
    await start(sessions, user, post)
    async with sessions() as s:
        await svc.on_post_deleted(s, user.id, post.id)
    row = await row_of(sessions, user)
    assert row.stopped_at is not None and row.stop_reason == "post_deleted"


async def test_deleting_some_other_post_does_not_stop_it(sessions, clock):
    user = await make_user(sessions)
    started_by = await make_post(sessions, user)
    other = await make_post(sessions, user, expires_in=timedelta(hours=-5))
    await start(sessions, user, started_by)
    async with sessions() as s:
        await svc.on_post_deleted(s, user.id, other.id)
    assert (await row_of(sessions, user)).stopped_at is None


async def test_deleting_the_post_keeps_it_going_if_another_post_is_live(sessions, clock):
    user = await make_user(sessions)
    gone = await make_post(sessions, user, expires_in=timedelta(hours=-1))
    await make_post(sessions, user)  # a live one
    await start(sessions, user, gone)
    async with sessions() as s:
        await svc.on_post_deleted(s, user.id, gone.id)
    assert (await row_of(sessions, user)).stopped_at is None


async def test_the_sweep_ends_windows_that_ran_out_and_forgets_their_position(sessions, clock):
    user = await make_user(sessions)
    other = await make_user(sessions)
    await start(sessions, user)
    await start(sessions, other)
    await report(sessions, user)
    clock.advance(hours=49)
    async with sessions() as s:
        assert await svc.finish_expired(s) >= 2
    row = await row_of(sessions, user)
    assert row.stop_reason == "expired" and row.last_lat is None


# --- over HTTP -----------------------------------------------------------------------------------


async def test_the_endpoints_need_a_login_and_check_the_numbers(api, clock):
    for method, path in (("GET", "status"), ("POST", "stop")):
        assert (await api.request(method, f"/api/location-tracking/{path}")).status_code == 401
    assert (await api.post("/api/location-tracking/location", json={"lat": 1, "lng": 1})).status_code == 401

    user = await make_user(api.factory)
    for bad in ({"lat": 91, "lng": 0}, {"lat": 0, "lng": 181}, {"lat": "x", "lng": 0}, {"lat": 0}):
        r = await api.post("/api/location-tracking/location", json=bad, headers=auth(user))
        assert r.status_code == 422, bad


async def test_the_app_reports_over_http_until_it_logs_out(api, clock):
    user = await make_user(api.factory)
    h = auth(user)
    assert (await api.get("/api/location-tracking/status", headers=h)).json()["active"] is False
    r = await api.post("/api/location-tracking/location", json={"lat": 19.0, "lng": 72.8}, headers=h)
    assert r.status_code == 200 and r.json()["active"] is False  # nothing to report into: the app stops its job

    await start(api.factory, user)
    status = (await api.get("/api/location-tracking/status", headers=h)).json()
    assert status["active"] is True and status["hours"] == 48 and status["endsAt"]
    r = await api.post(
        "/api/location-tracking/location", json={"lat": 19.0, "lng": 72.8, "accuracy": 20}, headers=h
    )
    assert r.json()["active"] is True and r.json()["recorded"] is True

    assert (await api.post("/api/location-tracking/stop", headers=h)).json()["active"] is False
    r = await api.post("/api/location-tracking/location", json={"lat": 19.1, "lng": 72.9}, headers=h)
    assert r.json()["active"] is False


async def test_deleting_the_post_over_http_stops_the_sharing(api, clock):
    user = await make_user(api.factory)
    post = await make_post(api.factory, user)
    await start(api.factory, user, post)
    r = await api.delete(f"/api/posts/delete/{post.id}", headers=auth(user))
    assert r.status_code == 200
    assert (await api.get("/api/location-tracking/status", headers=auth(user))).json()["active"] is False


async def _verified_truck(factory, user):
    from db.base import Truck

    async with factory() as s:
        truck = Truck(user_id=user.id, truck_number="MH12AB1234", truck_type="Open", capacity=10.0,
                      verification_status="verified")
        s.add(truck)
        await s.commit()
        return truck


@pytest.fixture
def quiet_matching(monkeypatch):
    import routers.posts as posts_router

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(posts_router, "process_smart_match_notifications", nothing)


async def test_posting_a_truck_opens_the_window_and_reactivating_restarts_it(api, clock, quiet_matching):
    user = await make_user(api.factory)
    truck = await _verified_truck(api.factory, user)
    here = {"name": "Mumbai, Maharashtra", "lat": 19.07, "lng": 72.87}
    body = {
        "vehicleId": str(truck.id),
        "origin": here,
        "destinations": [{"name": "Delhi", "lat": 28.61, "lng": 77.2}],
        "currentLocation": here,
    }
    h = auth(user)
    assert (await api.get("/api/location-tracking/status", headers=h)).json()["active"] is False

    r = await api.post(f"/api/posts/create/{user.id}", json=body, headers=h)
    assert r.status_code == 200, r.text
    post_id = r.json()["post"]["_id"]
    status = (await api.get("/api/location-tracking/status", headers=h)).json()
    assert status["active"] is True and status["hours"] == 48

    # The sharing runs out at 48 h (the post itself expired at 24 h); posting again after
    # reactivating the old post opens a fresh window.
    async with api.factory() as s:
        post = await s.get(TruckRoute, __import__("uuid").UUID(post_id))
        post.status, post.expires_at = "expired", datetime.utcnow() - timedelta(minutes=1)
        await s.commit()
    clock.advance(hours=49)
    assert (await api.get("/api/location-tracking/status", headers=h)).json()["active"] is False
    r = await api.post(f"/api/posts/reactivate/{post_id}", headers=h)
    assert r.status_code == 200, r.text
    assert (await api.get("/api/location-tracking/status", headers=h)).json()["active"] is True
