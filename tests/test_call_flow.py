"""The call itself: ring, answer, heartbeats, drops, hang-ups, time limit.

Time is faked (a clock the tests advance), so ring timeouts and the 30-second rule
are exact. Needs the same local scratch database as test_call_requests.py:

    export CALL_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
    .venv/bin/python -m pytest tests/test_call_flow.py -q
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from config import (
    CALL_END_GRACE_SECONDS,
    CALL_HEARTBEAT_GRACE_SECONDS,
    CALL_RECONNECT_WINDOW_SECONDS,
    CALL_RING_SECONDS,
)
from db.base import CallBehaviourEvent, CallRequest
from services import agora_client
from services import call_requests as svc

from sqlalchemy import select

from tests.test_call_requests import (  # noqa: F401  (fixtures are used by name)
    anyio_backend,
    expect_error,
    fund,
    make_post,
    make_user,
    needs_db,
    request_call,
    reload,
    schema,
    sessions,
    setup_pair,
    wallet_of,
)

pytestmark = [needs_db, pytest.mark.anyio]
D = Decimal


class Clock:
    def __init__(self):
        self.t = datetime.utcnow()

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(svc, "_now", c.now)
    # Token minting is local maths, but never use the real Agora credentials in tests.
    monkeypatch.setattr(agora_client, "AGORA_APP_ID", "a" * 32)
    monkeypatch.setattr(agora_client, "AGORA_APP_CERTIFICATE", "b" * 32)
    return c


async def accepted(sessions, amount="200", duration=15, balance="1000"):
    requester, provider, post = await setup_pair(sessions, balance)
    req = await request_call(sessions, requester, provider, post, amount, duration)
    async with sessions() as s:
        await svc.accept(s, req.id, provider.id)
    return requester, provider, req


async def ring(sessions, req, provider):
    async with sessions() as s:
        return await svc.start_call(s, req.id, provider.id)


async def answer(sessions, req, requester):
    async with sessions() as s:
        return await svc.join_call(s, req.id, requester.id)


async def live(sessions, **kwargs):
    requester, provider, req = await accepted(sessions, **kwargs)
    await ring(sessions, req, provider)
    await answer(sessions, req, requester)
    return requester, provider, req


async def beat(sessions, req, user, present=True):
    async with sessions() as s:
        return await svc.heartbeat(s, req.id, user.id, present)


async def both_beat(sessions, clock, req, requester, provider, seconds, step=5, *, requester_present=True,
                    provider_present=True, requester_beats=True, provider_beats=True):
    state = None
    for _ in range(seconds // step):
        clock.advance(step)
        if requester_beats:
            state = await beat(sessions, req, requester, requester_present)
        if provider_beats:
            state = await beat(sessions, req, provider, provider_present)
    return state


async def end(sessions, req, user):
    async with sessions() as s:
        return await svc.end_call(s, req.id, user.id)


async def sweep(sessions):
    async with sessions() as s:
        return await svc.sweep(s)


async def drain(sessions):
    await sweep(sessions)


# --- ringing -------------------------------------------------------------------------------------


async def test_the_driver_cannot_start_a_call_that_is_not_accepted(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post)
    async with sessions() as s:
        await expect_error("INVALID_STATE", svc.start_call(s, req.id, provider.id), 409)


async def test_starting_rings_the_requester_and_hands_the_driver_credentials(sessions, clock):
    requester, provider, req = await accepted(sessions, duration=15)
    r, creds = await ring(sessions, req, provider)

    assert r.status == "ringing" and r.ring_attempts == 1 and r.channel_name == f"call-{req.id.hex}"
    assert creds["channelName"] == r.channel_name and creds["role"] == "provider"
    assert creds["audioOnly"] is False and creds["rtcToken"] and creds["appId"] == "a" * 32
    assert creds["ringEndsAt"] is not None
    assert (requester.id, "call_incoming") in sessions.pushes
    # tokens can't outlive the longest the call could run: ring + duration + grace
    longest = clock.now() + timedelta(seconds=CALL_RING_SECONDS + 15 * 60 + CALL_END_GRACE_SECONDS)
    assert datetime.fromisoformat(creds["tokenExpiresAt"]) <= longest.replace(tzinfo=timezone.utc)


async def test_asking_to_start_again_while_ringing_is_the_same_ring(sessions):
    requester, provider, req = await accepted(sessions)
    first, c1 = await ring(sessions, req, provider)
    second, c2 = await ring(sessions, req, provider)
    assert second.ring_attempts == 1 and c1["channelName"] == c2["channelName"]


async def test_only_the_driver_can_start_and_only_the_requester_can_answer(sessions):
    requester, provider, req = await accepted(sessions)
    async with sessions() as s:
        await expect_error("NOT_PROVIDER", svc.start_call(s, req.id, requester.id), 403)
    await ring(sessions, req, provider)
    outsider = await make_user(sessions)
    async with sessions() as s:
        await expect_error("REQUEST_NOT_FOUND", svc.join_call(s, req.id, outsider.id), 404)


async def test_answering_the_ring_makes_the_call_live_with_audio_only_credentials(sessions):
    requester, provider, req = await accepted(sessions)
    await ring(sessions, req, provider)
    r, creds = await answer(sessions, req, requester)
    assert r.status == "live" and r.call_started_at is not None
    assert creds["role"] == "requester" and creds["audioOnly"] is True
    assert creds["channelName"] == r.channel_name
    assert creds["chatAvailable"] == agora_client.chat_available()
    assert (creds["rtmToken"] is None) == (not agora_client.chat_available())


async def test_a_ring_that_is_not_answered_in_time_is_missed_and_the_driver_can_retry(sessions, clock):
    await drain(sessions)
    requester, provider, req = await accepted(sessions)
    await ring(sessions, req, provider)

    clock.advance(CALL_RING_SECONDS + 1)
    async with sessions() as s:
        await expect_error("RING_OVER", svc.join_call(s, req.id, requester.id), 409)
    await sweep(sessions)  # global: other tests' leftovers may be swept too, so check only our request
    assert (await reload(sessions, req)).status == "accepted"
    assert (provider.id, "call_missed") in sessions.pushes

    r, _ = await ring(sessions, req, provider)  # second attempt, same channel
    assert r.status == "ringing" and r.ring_attempts == 2
    await answer(sessions, req, requester)
    assert (await reload(sessions, req)).status == "live"


async def test_the_requester_declining_the_ring_cancels_and_releases_everything(sessions):
    requester, provider, req = await accepted(sessions, amount="200", balance="500")
    await ring(sessions, req, provider)
    r = await end(sessions, req, requester)
    assert r.status == "cancelled"
    w = await wallet_of(sessions, requester)
    assert (w.available_balance, w.reserved_balance) == (D("500.00"), D("0.00"))


async def test_the_driver_cancelling_the_ring_takes_the_call_screen_off_the_requesters_phone(sessions):
    requester, provider, req = await accepted(sessions)
    await ring(sessions, req, provider)
    sessions.pushes.clear()
    await end(sessions, req, provider)
    assert (requester.id, "call_cancelled") in sessions.pushes


async def test_the_requester_declining_sends_no_cancel_push_to_themselves(sessions):
    requester, provider, req = await accepted(sessions)
    await ring(sessions, req, provider)
    sessions.pushes.clear()
    await end(sessions, req, requester)
    assert (requester.id, "call_cancelled") not in sessions.pushes


async def test_the_driver_cancelling_the_ring_can_start_again(sessions):
    requester, provider, req = await accepted(sessions)
    await ring(sessions, req, provider)
    assert (await end(sessions, req, provider)).status == "accepted"
    r, _ = await ring(sessions, req, provider)
    assert r.status == "ringing" and r.ring_attempts == 2


async def test_the_whole_request_window_still_ends_a_ringing_request(sessions, clock):
    await drain(sessions)
    requester, provider, req = await accepted(sessions, amount="200", balance="500")
    await ring(sessions, req, provider)
    clock.advance(31 * 60)
    await sweep(sessions)
    assert (await reload(sessions, req)).status == "expired"
    assert (await wallet_of(sessions, requester)).reserved_balance == D("0.00")


# --- the live call: heartbeats and the 30 second rule ----------------------------------------------------


async def test_time_counts_only_while_both_sides_are_in_and_see_each_other(sessions, clock):
    requester, provider, req = await live(sessions)
    state = await both_beat(sessions, clock, req, requester, provider, 40)
    # 8 rounds of beats; the first round has nothing to compare against, so 7 x 5 s
    assert state["status"] == "live" and state["connectedSeconds"] == 35
    assert state["thresholdSeconds"] == 30 and state["remainingSeconds"] == 15 * 60 - 40


async def test_a_side_claiming_to_be_in_alone_earns_nothing(sessions, clock):
    """A driver who never joined the channel can't run up the 30 s (and the call
    amount) by heartbeating: the requester's app honestly reports an empty room."""
    requester, provider, req = await live(sessions)
    await both_beat(sessions, clock, req, requester, provider, 60, requester_present=False)
    assert (await reload(sessions, req)).connected_ms == 0
    assert (await end(sessions, req, requester)).status == "failed"


async def test_a_driver_who_stops_reporting_stops_the_clock_after_the_grace_period(sessions, clock):
    requester, provider, req = await live(sessions)
    await both_beat(sessions, clock, req, requester, provider, 20)  # 15 s counted
    # The driver goes silent; the requester keeps reporting. Up to one grace period
    # (15 s) of the silence still counts, nothing after it.
    await both_beat(sessions, clock, req, requester, provider, 30, provider_beats=False)
    r = await reload(sessions, req)
    assert r.connected_ms // 1000 == 15 + CALL_HEARTBEAT_GRACE_SECONDS


async def test_hanging_up_before_30_seconds_costs_nothing(sessions, clock):
    requester, provider, req = await live(sessions, amount="200", balance="500")
    await both_beat(sessions, clock, req, requester, provider, 20)  # 15 s
    r = await end(sessions, req, provider)
    assert r.status == "failed" and r.end_reason == "ended_by_provider"
    w = await wallet_of(sessions, requester)
    assert (w.available_balance, w.reserved_balance) == (D("500.00"), D("0.00"))
    assert (await wallet_of(sessions, provider)).available_balance == D("0.00")


async def test_a_call_past_30_seconds_asks_the_requester_how_it_went(sessions, clock):
    requester, provider, req = await live(sessions, amount="200", balance="500")
    await both_beat(sessions, clock, req, requester, provider, 40)
    r = await end(sessions, req, requester)
    assert r.status == "awaiting_feedback" and r.connected_seconds == 35
    assert (requester.id, "call_feedback_needed") in sessions.pushes

    async with sessions() as s:
        done = await svc.submit_feedback(s, req.id, requester.id, True)
    assert done.settled_amount == D("200.00")
    assert (await wallet_of(sessions, provider)).available_balance == D("200.00")


async def test_either_side_can_end_and_a_second_tap_is_harmless(sessions, clock):
    requester, provider, req = await live(sessions)
    await both_beat(sessions, clock, req, requester, provider, 40)
    first = await end(sessions, req, provider)
    again = await end(sessions, req, requester)
    assert first.status == again.status == "awaiting_feedback"
    state = await beat(sessions, req, provider)  # the other app learns it is over
    assert state["status"] == "awaiting_feedback"


async def test_ending_a_request_nobody_started_is_refused(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post)
    async with sessions() as s:
        await expect_error("INVALID_STATE", svc.end_call(s, req.id, requester.id), 409)


async def test_either_side_can_rejoin_a_live_call_after_a_drop(sessions, clock):
    requester, provider, req = await live(sessions)
    await both_beat(sessions, clock, req, requester, provider, 10)
    _, creds_r = await answer(sessions, req, requester)
    async with sessions() as s:
        _, creds_p = await svc.join_call(s, req.id, provider.id)
    assert creds_r["channelName"] == creds_p["channelName"]
    assert creds_r["uid"] != creds_p["uid"] and creds_r["audioOnly"] and not creds_p["audioOnly"]
    assert (await reload(sessions, req)).status == "live"
    events = await events_of(sessions, req)
    assert events.count("call_rejoined") == 2


async def test_live_call_tokens_end_with_the_chosen_duration(sessions, clock):
    requester, provider, req = await live(sessions, duration=10)
    _, creds = await answer(sessions, req, requester)
    started = (await reload(sessions, req)).call_started_at
    limit = (started + timedelta(minutes=10, seconds=CALL_END_GRACE_SECONDS)).replace(tzinfo=timezone.utc)
    assert datetime.fromisoformat(creds["tokenExpiresAt"]) <= limit


# --- the sweeper on live calls ---------------------------------------------------------------------------------


async def test_a_call_whose_other_side_never_comes_back_is_ended_from_the_time_so_far(sessions, clock):
    await drain(sessions)
    requester, provider, req = await live(sessions, amount="200", balance="500")
    await both_beat(sessions, clock, req, requester, provider, 40)  # 35 s connected
    clock.advance(CALL_RECONNECT_WINDOW_SECONDS + 1)  # both silent past the window
    await sweep(sessions)
    r = await reload(sessions, req)
    assert r.status == "awaiting_feedback" and r.end_reason == "disconnected" and r.connected_seconds == 35


async def test_a_drop_before_30_seconds_that_never_reconnects_costs_nothing(sessions, clock):
    await drain(sessions)
    requester, provider, req = await live(sessions, amount="200", balance="500")
    await both_beat(sessions, clock, req, requester, provider, 20)  # 15 s
    clock.advance(CALL_RECONNECT_WINDOW_SECONDS + 1)
    await sweep(sessions)
    assert (await reload(sessions, req)).status == "failed"
    assert (await wallet_of(sessions, requester)).reserved_balance == D("0.00")


async def test_a_short_drop_inside_the_reconnect_window_keeps_the_call_alive(sessions, clock):
    await drain(sessions)
    requester, provider, req = await live(sessions)
    await both_beat(sessions, clock, req, requester, provider, 20)
    clock.advance(CALL_RECONNECT_WINDOW_SECONDS - 10)
    await sweep(sessions)
    assert (await reload(sessions, req)).status == "live"  # still inside the window
    await both_beat(sessions, clock, req, requester, provider, 10)  # they came back
    assert (await reload(sessions, req)).status == "live"


async def test_the_call_is_ended_at_the_chosen_duration(sessions, clock):
    await drain(sessions)
    requester, provider, req = await live(sessions, duration=10, amount="200", balance="500")
    await both_beat(sessions, clock, req, requester, provider, 100)
    clock.advance(10 * 60)  # well past duration + grace, with beats still arriving
    state = await beat(sessions, req, requester)
    assert state["status"] == "awaiting_feedback"
    r = await reload(sessions, req)
    assert r.end_reason == "time_limit" and r.connected_seconds >= 30


async def test_the_sweeper_also_ends_a_call_that_ran_out_of_time_with_nobody_beating(sessions, clock):
    await drain(sessions)
    requester, provider, req = await live(sessions, duration=10)
    await both_beat(sessions, clock, req, requester, provider, 60)
    clock.advance(10 * 60 + CALL_END_GRACE_SECONDS)
    await sweep(sessions)
    assert (await reload(sessions, req)).end_reason == "time_limit"


# --- audit trail ------------------------------------------------------------------------------------------------------


async def events_of(sessions, req):
    async with sessions() as s:
        rows = (
            await s.execute(
                select(CallBehaviourEvent.event_type)
                .where(CallBehaviourEvent.call_request_id == req.id)
                .order_by(CallBehaviourEvent.created_at)
            )
        ).scalars().all()
    return list(rows)


async def test_the_whole_call_leaves_a_readable_trail_for_both_sides(sessions, clock):
    requester, provider, req = await live(sessions, amount="200", balance="500")
    await both_beat(sessions, clock, req, requester, provider, 40)
    await end(sessions, req, provider)
    async with sessions() as s:
        await svc.submit_feedback(s, req.id, requester.id, False)
    trail = await events_of(sessions, req)
    for expected in (
        "request_created",
        "request_accepted",
        "call_started_by_provider",
        "call_answered",
        "call_ended",
        "call_connected_enough",
        "feedback_not_satisfied",
    ):
        assert expected in trail, expected
    assert trail.index("call_started_by_provider") < trail.index("call_answered") < trail.index("call_ended")
