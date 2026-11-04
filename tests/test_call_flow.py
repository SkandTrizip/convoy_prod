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
    CALL_PAY_WINDOW_MINUTES,
    CALL_START_WINDOW_MINUTES,
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
    accept_request,
    anyio_backend,
    expect_error,
    fund,
    make_post,
    make_ready,
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
    """A request the driver accepted, the requester paid and the driver confirmed: ready for the call
    (the name is from before the advance payment). `balance` is ignored."""
    requester, provider, post = await setup_pair(sessions, balance)
    req = await request_call(sessions, requester, provider, post, amount, duration)
    await make_ready(sessions, req, requester, provider)
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


async def beat(sessions, req, user, present=True, **media):
    async with sessions() as s:
        return await svc.heartbeat(s, req.id, user.id, present, **media)


async def both_beat(sessions, clock, req, requester, provider, seconds, step=5, *, requester_present=True,
                    provider_present=True, requester_beats=True, provider_beats=True,
                    provider_mic=True, provider_camera=True, requester_saw_video=True):
    state = None
    for _ in range(seconds // step):
        clock.advance(step)
        if requester_beats:
            state = await beat(
                sessions, req, requester, requester_present,
                mic_on=True, camera_on=False, peer_video_on=requester_saw_video,
            )
        if provider_beats:
            state = await beat(
                sessions, req, provider, provider_present,
                mic_on=provider_mic, camera_on=provider_camera, peer_video_on=False,
            )
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


async def test_the_driver_cannot_start_a_call_that_is_not_ready(sessions):
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
    assert (await reload(sessions, req)).status == "ready"
    assert (provider.id, "call_missed") in sessions.pushes

    r, _ = await ring(sessions, req, provider)  # second attempt, same channel
    assert r.status == "ringing" and r.ring_attempts == 2
    await answer(sessions, req, requester)
    assert (await reload(sessions, req)).status == "live"


async def test_the_requester_declining_the_ring_cancels_the_request(sessions):
    requester, provider, req = await accepted(sessions, amount="200")
    await ring(sessions, req, provider)
    r = await end(sessions, req, requester)
    assert r.status == "cancelled"


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
    assert (await end(sessions, req, provider)).status == "ready"
    r, _ = await ring(sessions, req, provider)
    assert r.status == "ringing" and r.ring_attempts == 2


async def test_the_whole_request_window_still_ends_a_ringing_request(sessions, clock):
    await drain(sessions)
    requester, provider, req = await accepted(sessions, amount="200")
    await ring(sessions, req, provider)
    clock.advance(31 * 60)
    await sweep(sessions)
    assert (await reload(sessions, req)).status == "expired"
    assert (await wallet_of(sessions, requester)).reserved_balance == D("0.00")


async def test_each_stage_gets_its_own_window_from_the_moment_it_starts(sessions, clock):
    """Pay window from the acceptance, a fresh start window from the driver's confirmation: a driver who is
    slow to confirm still gets the full time to start the call."""
    requester, provider, post = await setup_pair(sessions, "1000")
    req = await request_call(sessions, requester, provider, post, "200", 15)
    clock.advance(28 * 60)  # accepted 2 minutes before the request's own 30 minutes were up
    accepted_request = await accept_request(sessions, req, provider)
    assert accepted_request.expires_at == clock.now() + timedelta(minutes=CALL_PAY_WINDOW_MINUTES)

    clock.advance(50 * 60)  # paid late in the payment window
    await sweep(sessions)
    from tests.test_call_requests import confirm, pay

    await pay(sessions, req, requester)
    clock.advance(20 * 60)  # the driver answers 20 minutes later
    ready = await confirm(sessions, req, provider, True)
    assert ready.expires_at == clock.now() + timedelta(minutes=CALL_START_WINDOW_MINUTES)

    clock.advance(25 * 60)  # more than 95 minutes after accepting: still inside the new window
    await sweep(sessions)
    r, _ = await ring(sessions, req, provider)
    assert r.status == "ringing"


async def test_a_ready_request_still_ends_30_minutes_after_the_confirmation(sessions, clock):
    await drain(sessions)
    requester, provider, post = await setup_pair(sessions, "1000")
    req = await request_call(sessions, requester, provider, post, "200", 15)
    clock.advance(10 * 60)
    await make_ready(sessions, req, requester, provider)
    clock.advance(31 * 60)
    await expect_error("REQUEST_EXPIRED", ring(sessions, req, provider), status=410)
    assert (await reload(sessions, req)).status == "expired"


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


async def test_hanging_up_before_30_seconds_completes_a_short_call(sessions, clock):
    requester, provider, req = await live(sessions, amount="200")
    await both_beat(sessions, clock, req, requester, provider, 20)  # 15 s
    r = await end(sessions, req, provider)
    assert r.status == "completed" and r.end_reason == "ended_by_provider"
    assert r.connected_seconds == 15 and r.threshold_met is False


async def test_hanging_up_when_nobody_ever_connected_is_a_failed_call(sessions, clock):
    requester, provider, req = await live(sessions, amount="200")
    r = await end(sessions, req, provider)  # not one beat from either side
    assert r.status == "failed" and r.connected_seconds == 0


async def test_a_call_past_30_seconds_is_completed_and_already_paid(sessions, clock):
    requester, provider, req = await live(sessions, amount="200")
    await both_beat(sessions, clock, req, requester, provider, 40)
    r = await end(sessions, req, requester)
    assert r.status == "completed" and r.connected_seconds == 35 and r.threshold_met is True
    assert r.paid_amount == D("200.00") and r.payment_outcome == "paid"  # paid in advance, nothing more to ask
    assert (requester.id, "call_payment_due") not in sessions.pushes


async def test_either_side_can_end_and_a_second_tap_is_harmless(sessions, clock):
    requester, provider, req = await live(sessions)
    await both_beat(sessions, clock, req, requester, provider, 40)
    first = await end(sessions, req, provider)
    again = await end(sessions, req, requester)
    assert first.status == again.status == "completed"
    state = await beat(sessions, req, provider)  # the other app learns it is over
    assert state["status"] == "completed"


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
    requester, provider, req = await live(sessions, amount="200")
    await both_beat(sessions, clock, req, requester, provider, 40)  # 35 s connected
    clock.advance(CALL_RECONNECT_WINDOW_SECONDS + 1)  # both silent past the window
    await sweep(sessions)
    r = await reload(sessions, req)
    assert r.status == "completed" and r.end_reason == "disconnected" and r.connected_seconds == 35


async def test_a_drop_before_30_seconds_that_never_reconnects_is_a_short_call(sessions, clock):
    await drain(sessions)
    requester, provider, req = await live(sessions, amount="200")
    await both_beat(sessions, clock, req, requester, provider, 20)  # 15 s
    clock.advance(CALL_RECONNECT_WINDOW_SECONDS + 1)
    await sweep(sessions)
    r = await reload(sessions, req)
    assert r.status == "completed" and r.threshold_met is False


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
    requester, provider, req = await live(sessions, duration=10, amount="200")
    await both_beat(sessions, clock, req, requester, provider, 100)
    clock.advance(10 * 60)  # well past duration + grace, with beats still arriving
    state = await beat(sessions, req, requester)
    assert state["status"] == "completed"
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
    requester, provider, req = await live(sessions, amount="200")
    await both_beat(sessions, clock, req, requester, provider, 40)
    await end(sessions, req, provider)
    trail = await events_of(sessions, req)
    for expected in (
        "request_created",
        "request_accepted",
        "payment_claimed",
        "provider_confirmed_payment",
        "call_started_by_provider",
        "call_answered",
        "call_ended",
        "call_completed",
    ):
        assert expected in trail, expected
    assert trail.index("request_accepted") < trail.index("payment_claimed") < trail.index("provider_confirmed_payment")
    assert trail.index("provider_confirmed_payment") < trail.index("call_started_by_provider")
    assert trail.index("call_started_by_provider") < trail.index("call_answered") < trail.index("call_ended")


# --- what each side reports while both are in the call -----------------------------------------------


async def test_beats_record_who_had_a_mic_and_camera_on_and_whether_the_requester_saw_video(sessions, clock):
    requester, provider, req = await live(sessions)
    await both_beat(sessions, clock, req, requester, provider, 40, provider_camera=False, requester_saw_video=False)
    r = await reload(sessions, req)
    assert (r.provider_beats, r.provider_mic_beats, r.provider_camera_beats) == (8, 8, 0)
    # The requester's very first beat comes before the driver has reported at all, so it isn't "both in" yet.
    assert (r.requester_beats, r.requester_mic_beats, r.requester_camera_beats) == (7, 7, 0)
    assert r.requester_saw_video_beats == 0


async def test_beats_before_both_are_in_the_call_are_not_counted(sessions, clock):
    requester, provider, req = await live(sessions)
    await both_beat(sessions, clock, req, requester, provider, 20, requester_present=False)
    r = await reload(sessions, req)
    assert r.provider_beats == 0 and r.requester_beats == 0


async def test_a_provider_whose_mic_and_camera_were_on_for_most_of_the_call_counts_as_media_on(sessions, clock):
    requester, provider, req = await live(sessions)
    await both_beat(sessions, clock, req, requester, provider, 60)  # 12 beats, all on
    await both_beat(sessions, clock, req, requester, provider, 25, provider_camera=False)  # 5 beats, camera off
    r = await end(sessions, req, requester)
    assert r.provider_media_on is True  # camera on for 12 of 17 beats


async def test_a_provider_whose_camera_was_mostly_off_does_not_count_as_media_on(sessions, clock):
    requester, provider, req = await live(sessions)
    await both_beat(sessions, clock, req, requester, provider, 20)  # 4 beats on
    await both_beat(sessions, clock, req, requester, provider, 60, provider_camera=False)  # 12 beats off
    r = await end(sessions, req, requester)
    assert r.threshold_met is True and r.provider_media_on is False


async def test_an_app_that_reports_nothing_about_its_media_never_counts_as_media_on(sessions, clock):
    requester, provider, req = await live(sessions)
    for _ in range(8):
        clock.advance(5)
        await beat(sessions, req, requester)
        await beat(sessions, req, provider)  # an older app: no micOn / cameraOn
    r = await end(sessions, req, requester)
    assert r.threshold_met is True and r.provider_media_on is False
