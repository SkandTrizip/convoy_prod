"""One account on several phones: the phone that holds a call keeps it.

Needs the local scratch database, like test_call_requests.py:

    export CALL_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
    .venv/bin/python -m pytest tests/test_call_devices.py -q
"""

import pytest
from sqlalchemy import select

from config import CALL_DEVICE_TAKEOVER_SECONDS, CALL_RING_SECONDS
from db.base import CallBehaviourEvent
from services import call_requests as svc

from tests.test_call_flow import (  # noqa: F401  (fixtures are used by name)
    accepted,
    clock,
    sweep,
)
from tests.test_call_requests import (  # noqa: F401
    anyio_backend,
    expect_error,
    needs_db,
    reload,
    schema,
    sessions,
)

pytestmark = [needs_db, pytest.mark.anyio]

PHONE_A, PHONE_B = "phone-a", "phone-b"


async def start(sessions, req, provider, device=PHONE_A):
    async with sessions() as s:
        return await svc.start_call(s, req.id, provider.id, device)


async def join(sessions, req, user, device):
    async with sessions() as s:
        return await svc.join_call(s, req.id, user.id, device)


async def beat(sessions, req, user, device):
    async with sessions() as s:
        return await svc.heartbeat(s, req.id, user.id, True, device)


async def end(sessions, req, user, device):
    async with sessions() as s:
        return await svc.end_call(s, req.id, user.id, device)


async def live_on_a(sessions):
    """Driver on PHONE_A rings; the requester answers on PHONE_A."""
    requester, provider, req = await accepted(sessions)
    await start(sessions, req, provider)
    await join(sessions, req, requester, PHONE_A)
    return requester, provider, req


# --- the requester answers on one of two phones --------------------------------------------------


async def test_answering_remembers_the_phone_and_stops_the_other_phones_ringing(sessions):
    requester, provider, req = await accepted(sessions)
    await start(sessions, req, provider)
    sessions.pushes.clear()
    await join(sessions, req, requester, PHONE_A)

    assert (await reload(sessions, req)).requester_device_id == PHONE_A
    assert (requester.id, f"ring_stopped_except:{PHONE_A}") in sessions.pushes


async def test_a_second_phone_answering_a_call_already_answered_is_refused(sessions):
    requester, provider, req = await live_on_a(sessions)
    async with sessions() as s:
        await expect_error("CALL_ON_ANOTHER_DEVICE", svc.join_call(s, req.id, requester.id, PHONE_B), 409)
    assert (await reload(sessions, req)).requester_device_id == PHONE_A  # still phone A's


async def test_the_phone_that_holds_the_call_can_rejoin_it(sessions):
    requester, provider, req = await live_on_a(sessions)
    _, creds = await join(sessions, req, requester, PHONE_A)
    assert creds["role"] == "requester"


async def test_declining_on_a_phone_that_is_still_ringing_does_not_end_the_live_call(sessions):
    requester, provider, req = await live_on_a(sessions)
    async with sessions() as s:
        await expect_error("CALL_ON_ANOTHER_DEVICE", svc.end_call(s, req.id, requester.id, PHONE_B), 409)
    assert (await reload(sessions, req)).status == "live"
    assert (await end(sessions, req, requester, PHONE_A)).status != "live"  # the holder can still hang up


async def test_heartbeats_from_the_other_phone_do_not_count_for_the_call(sessions, clock):
    requester, provider, req = await live_on_a(sessions)
    clock.advance(5)
    async with sessions() as s:
        await expect_error("CALL_ON_ANOTHER_DEVICE", svc.heartbeat(s, req.id, requester.id, True, PHONE_B), 409)
    assert (await beat(sessions, req, requester, PHONE_A))["status"] == "live"


async def test_the_other_phone_may_take_over_once_the_first_has_gone_quiet(sessions, clock):
    requester, provider, req = await live_on_a(sessions)
    clock.advance(CALL_DEVICE_TAKEOVER_SECONDS + 1)  # phone A's battery died: no heartbeats
    await join(sessions, req, requester, PHONE_B)

    assert (await reload(sessions, req)).requester_device_id == PHONE_B
    async with sessions() as s:
        events = (
            await s.execute(select(CallBehaviourEvent).where(CallBehaviourEvent.call_request_id == req.id))
        ).scalars().all()
    assert any(e.event_type == "call_rejoined" and "takeover" in (e.details or "") for e in events)
    # ... and now phone A is the one that is kept out.
    async with sessions() as s:
        await expect_error("CALL_ON_ANOTHER_DEVICE", svc.heartbeat(s, req.id, requester.id, True, PHONE_A), 409)


async def test_a_phone_that_is_still_beating_cannot_be_taken_over(sessions, clock):
    requester, provider, req = await live_on_a(sessions)
    clock.advance(CALL_DEVICE_TAKEOVER_SECONDS - 5)
    await beat(sessions, req, requester, PHONE_A)
    clock.advance(CALL_DEVICE_TAKEOVER_SECONDS - 5)  # long after the answer, but A beat recently
    async with sessions() as s:
        await expect_error("CALL_ON_ANOTHER_DEVICE", svc.join_call(s, req.id, requester.id, PHONE_B), 409)


async def test_declining_the_ring_on_one_phone_silences_the_others(sessions):
    requester, provider, req = await accepted(sessions)
    await start(sessions, req, provider)
    sessions.pushes.clear()
    r = await end(sessions, req, requester, PHONE_B)

    assert r.status == "cancelled"
    assert (requester.id, f"ring_stopped_except:{PHONE_B}") in sessions.pushes


# --- the driver on two phones --------------------------------------------------------------------


async def test_the_driver_starting_the_ring_on_a_second_phone_is_refused_while_it_rings(sessions):
    requester, provider, req = await accepted(sessions)
    await start(sessions, req, provider, PHONE_A)
    async with sessions() as s:
        await expect_error("CALL_ON_ANOTHER_DEVICE", svc.start_call(s, req.id, provider.id, PHONE_B), 409)
    r, _ = await start(sessions, req, provider, PHONE_A)  # the same phone asking again is fine
    assert r.provider_device_id == PHONE_A


async def test_after_a_missed_ring_the_driver_can_try_again_from_another_phone(sessions, clock):
    requester, provider, req = await accepted(sessions)
    await start(sessions, req, provider, PHONE_A)
    clock.advance(CALL_RING_SECONDS + 1)
    r, _ = await start(sessions, req, provider, PHONE_B)
    assert r.provider_device_id == PHONE_B and r.ring_attempts == 2


async def test_the_drivers_other_phone_cannot_join_or_end_the_live_call(sessions):
    requester, provider, req = await accepted(sessions)
    await start(sessions, req, provider, PHONE_A)
    await join(sessions, req, requester, "requester-phone")
    await join(sessions, req, provider, PHONE_A)
    async with sessions() as s:
        await expect_error("CALL_ON_ANOTHER_DEVICE", svc.join_call(s, req.id, provider.id, PHONE_B), 409)
    async with sessions() as s:
        await expect_error("CALL_ON_ANOTHER_DEVICE", svc.end_call(s, req.id, provider.id, PHONE_B), 409)


# --- older apps and the app's view ---------------------------------------------------------------


async def test_an_app_that_sends_no_device_id_is_never_blocked(sessions):
    requester, provider, req = await live_on_a(sessions)
    async with sessions() as s:
        await svc.join_call(s, req.id, requester.id, None)  # no header: nothing to compare
    assert (await beat(sessions, req, requester, None))["status"] == "live"


async def test_the_app_is_told_when_a_call_is_on_its_other_phone(sessions):
    requester, provider, req = await live_on_a(sessions)
    r = await reload(sessions, req)
    assert svc.on_another_device(r, requester.id, PHONE_B) is True
    assert svc.on_another_device(r, requester.id, PHONE_A) is False
    assert svc.on_another_device(r, requester.id, None) is False
    assert svc.on_another_device(r, provider.id, PHONE_B) is True  # the driver started it on phone A
    assert svc.on_another_device(r, provider.id, PHONE_A) is False


# --- the requester hears about a missed call -----------------------------------------------------


async def test_an_unanswered_ring_is_reported_to_both_people(sessions, clock):
    requester, provider, req = await accepted(sessions)
    await start(sessions, req, provider)
    sessions.pushes.clear()
    clock.advance(CALL_RING_SECONDS + 1)
    await sweep(sessions)

    assert (provider.id, "call_missed") in sessions.pushes
    assert (requester.id, "call_missed") in sessions.pushes


async def test_the_driver_hanging_up_a_ringing_call_leaves_the_requester_a_missed_call_note(sessions):
    requester, provider, req = await accepted(sessions)
    await start(sessions, req, provider)
    sessions.pushes.clear()
    await end(sessions, req, provider, PHONE_A)
    await svc.drain_background()

    assert (requester.id, "call_cancelled") in sessions.pushes
    assert (requester.id, "call_missed") in sessions.pushes
