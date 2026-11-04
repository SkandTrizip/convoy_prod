"""State-machine and payment tests for paid 1:1 call requests (new flow: the requester pays the driver's UPI ID).

The pure rules run anywhere. The rest need a real Postgres+PostGIS (row locks and
the partial unique index are the point), so they only run when you name a LOCAL
scratch database:

    export CALL_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
    .venv/bin/python -m pytest tests/test_call_requests.py -q

They refuse any non-local URL: local dev shares the production database, and
these tests create users and calls.
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlparse

import pytest
from geoalchemy2 import WKTElement
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config import CALL_CLAIM_CONFIRM_TTL_HOURS, CALL_PAY_WINDOW_MINUTES, CALL_START_WINDOW_MINUTES
from db.base import (
    CallBehaviourEvent,
    CallRequest,
    RedeemRequest,
    TruckRoute,
    User,
    Wallet,
    WalletTransaction,
)
from db.session import _create_missing_tables
from services import call_payment_standing
from services import call_requests as svc
from services import wallet as wallet_service
from services.call_requests import CallRequestError, compute_hold

D = Decimal


# --- DB fixtures -------------------------------------------------------------------

TEST_URL = os.environ.get("CALL_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_URL, reason="set CALL_TEST_DATABASE_URL to a local scratch database")


def _asyncpg_url(url: str) -> str:
    host = urlparse(url).hostname
    assert host in ("127.0.0.1", "localhost", "::1"), (
        f"refusing to run money tests against non-local database host {host!r}"
    )
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="session")
def schema():
    if not TEST_URL:
        return

    async def create():
        engine = create_async_engine(_asyncpg_url(TEST_URL))
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
            await conn.run_sync(_create_missing_tables)  # same path the app uses at startup
        await engine.dispose()

    asyncio.run(create())


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def sessions(schema, monkeypatch):
    pushes = []

    async def fake_notify(session, user_id, title, body, data):
        pushes.append((user_id, data.get("type")))

    monkeypatch.setattr(svc, "_notify", fake_notify)

    async def fake_incoming(session, request, caller_name):
        pushes.append((request.requester_id, "call_incoming"))

    async def fake_cancelled(session, request):
        pushes.append((request.requester_id, "call_cancelled"))

    def fake_stop_others(session, request, except_device_id):
        if except_device_id is not None:
            pushes.append((request.requester_id, f"ring_stopped_except:{except_device_id}"))

    monkeypatch.setattr(svc, "_push_incoming_call", fake_incoming)
    monkeypatch.setattr(svc, "_push_call_cancelled", fake_cancelled)
    monkeypatch.setattr(svc, "_stop_other_phones_ringing", fake_stop_others)
    engine = create_async_engine(_asyncpg_url(TEST_URL))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    factory.pushes = pushes
    yield factory
    await engine.dispose()


async def make_user(factory, name="User", kyc="approved", upi: bool = True) -> User:
    async with factory() as s:
        mobile = f"9{uuid.uuid4().int % 10**9:09d}"
        user = User(mobile=mobile, name=name, kyc_status=kyc, upi_id=f"{mobile}@ybl" if upi else None)
        s.add(user)
        await s.commit()
        return user


async def make_post(factory, user: User, expires_in=timedelta(days=1)) -> TruckRoute:
    async with factory() as s:
        post = TruckRoute(
            truck_id=uuid.uuid4(),
            user_id=user.id,
            truck_number="DL01AB1234",
            truck_type="Open",
            origin_name="Delhi",
            origin_location=WKTElement("POINT(77.2090 28.6139)", srid=4326),
            origin={"name": "Delhi"},
            expires_at=datetime.utcnow() + expires_in,
        )
        s.add(post)
        await s.commit()
        return post


async def fund(factory, user: User, amount: str):
    async with factory() as s:
        await wallet_service.credit_wallet(s, user.id, D(amount), "test", "fund", f"fund-{uuid.uuid4()}")


async def wallet_of(factory, user: User) -> Wallet:
    async with factory() as s:
        return await wallet_service.get_or_create_wallet(s, user.id)


async def request_call(factory, requester, provider, post, amount="200", duration=15, topic="Route status"):
    async with factory() as s:
        return await svc.create_request(s, requester, provider.id, post.id, topic, duration, D(amount))


async def reload(factory, request: CallRequest) -> CallRequest:
    async with factory() as s:
        return await s.get(CallRequest, request.id)


async def total_money(factory, *users) -> Decimal:
    total = D(0)
    for user in users:
        w = await wallet_of(factory, user)
        total += w.available_balance + w.reserved_balance
    return total


async def setup_pair(factory, balance=None):
    """A requester, a provider (with a UPI ID) and the provider's live post. `balance` is ignored:
    nothing needs money in the wallet any more (kept so older callers still work)."""
    requester = await make_user(factory, "Requester")
    provider = await make_user(factory, "Provider")
    post = await make_post(factory, provider)
    return requester, provider, post


async def expect_error(code, awaitable, status=None):
    with pytest.raises(CallRequestError) as exc:
        await awaitable
    assert exc.value.code == code
    if status:
        assert exc.value.status_code == status
    return exc.value


MEDIA = {  # (mic beats, camera beats) out of 20 the provider reported
    "both": (20, 20),
    "mic_only": (20, 0),
    "camera_only": (0, 20),
    "none": (0, 0),
}


async def connect_call(factory, request, seconds=120, provider_media="both"):
    """Stand-in for the call stage: the request is live, then ends. `provider_media` says what the
    provider's heartbeats reported (see MEDIA)."""
    mic, camera = MEDIA[provider_media]
    async with factory() as s:
        r = await s.get(CallRequest, request.id)
        r.status = "live"
        r.provider_beats, r.provider_mic_beats, r.provider_camera_beats = 20, mic, camera
        await s.commit()
    async with factory() as s:
        return await svc.finalize_call(s, request.id, seconds, "ended_by_provider")


async def accept_request(factory, request, provider):
    async with factory() as s:
        return await svc.accept(s, request.id, provider.id)


async def pay(factory, request, requester, reference=None):
    async with factory() as s:
        return await svc.pay(s, request.id, requester.id, reference)


async def confirm(factory, request, user, received=True, sure=False):
    async with factory() as s:
        return await svc.confirm_received(s, request.id, user.id, received, sure)


async def make_ready(factory, request, requester, provider, reference=None):
    """Accepted, paid, and the driver said they got it: the call may start."""
    await accept_request(factory, request, provider)
    await pay(factory, request, requester, reference)
    return await confirm(factory, request, provider, True)


async def age(factory, request, **fields):
    """Move one of a request's timestamps into the past (or set any column)."""
    async with factory() as s:
        r = await s.get(CallRequest, request.id)
        for name, value in fields.items():
            setattr(r, name, value)
        await s.commit()


pytestmark = [needs_db, pytest.mark.anyio]


# --- creating a request ----------------------------------------------------------------


async def test_create_holds_nothing_and_sets_the_minimum_payable(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post, "200")

    assert req.status == "pending"
    assert req.amount == D("200.00") and req.hold_amount == D("50.00")  # hold_amount = the minimum payable
    assert (provider.id, "call_request") in sessions.pushes
    async with sessions() as s:  # the wallet is not involved at all
        assert await s.scalar(select(WalletTransaction.id).where(WalletTransaction.reference_id == str(req.id))) is None
        assert await s.scalar(select(Wallet.user_id).where(Wallet.user_id == requester.id)) is None


async def test_a_driver_without_a_upi_id_cannot_be_asked_for_a_call(sessions):
    requester = await make_user(sessions, "Requester")
    provider = await make_user(sessions, "No UPI", upi=False)
    post = await make_post(sessions, provider)
    await expect_error("PROVIDER_UPI_MISSING", request_call(sessions, requester, provider, post), 409)
    async with sessions() as s:
        assert await s.scalar(select(CallRequest.id).where(CallRequest.requester_id == requester.id)) is None


async def test_one_open_request_per_pair_until_it_ends(sessions):
    requester, provider, post = await setup_pair(sessions)
    first = await request_call(sessions, requester, provider, post, "100")
    err = await expect_error("ALREADY_OPEN", request_call(sessions, requester, provider, post, "100"), 409)
    assert err.extra["requestId"] == str(first.id)

    async with sessions() as s:
        await svc.reject(s, first.id, provider.id)
    again = await request_call(sessions, requester, provider, post, "100")  # no cooldown
    assert again.id != first.id


async def test_the_same_pair_cannot_get_two_open_requests_even_if_the_check_is_raced(sessions):
    requester, provider, post = await setup_pair(sessions)
    results = await asyncio.gather(
        request_call(sessions, requester, provider, post, "100"),
        request_call(sessions, requester, provider, post, "100"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, CallRequest) for r in results) == 1


async def test_a_call_waiting_to_be_paid_does_not_stop_the_same_pair_asking_again(sessions):
    requester, provider, post = await setup_pair(sessions)
    await connect_call(sessions, await request_call(sessions, requester, provider, post, "100"))
    assert (await request_call(sessions, requester, provider, post, "100")).status == "pending"


@pytest.mark.parametrize("amount", ["49.99", "10000.01", "0", "-5"])
async def test_amount_limits(sessions, amount):
    requester, provider, post = await setup_pair(sessions)
    await expect_error("INVALID_AMOUNT", request_call(sessions, requester, provider, post, amount), 400)


async def test_max_amount_is_allowed(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post, "10000")
    assert req.hold_amount == D("2500.00")


async def test_other_validation(sessions):
    requester, provider, post = await setup_pair(sessions)
    await expect_error("INVALID_DURATION", request_call(sessions, requester, provider, post, duration=20))
    await expect_error("INVALID_TOPIC", request_call(sessions, requester, provider, post, topic="   "))

    own_post = await make_post(sessions, requester)
    await expect_error("SELF_REQUEST", request_call(sessions, requester, requester, own_post))

    stranger_post = await make_post(sessions, await make_user(sessions))
    await expect_error("TRUCK_POST_UNAVAILABLE", request_call(sessions, requester, provider, stranger_post))

    old_post = await make_post(sessions, provider, expires_in=timedelta(seconds=-1))
    await expect_error("TRUCK_POST_UNAVAILABLE", request_call(sessions, requester, provider, old_post))

    async with sessions() as s:
        u = await s.get(User, provider.id)
        u.account_status = "suspended"
        await s.commit()
    await expect_error("PROVIDER_UNAVAILABLE", request_call(sessions, requester, provider, post))


async def test_a_user_who_never_did_kyc_can_still_ask_for_a_call(sessions):
    requester = await make_user(sessions, "No KYC", kyc="pending")
    provider = await make_user(sessions)
    post = await make_post(sessions, provider)
    assert (await request_call(sessions, requester, provider, post, "100")).status == "pending"


# --- provider decisions -----------------------------------------------------------------


async def test_reject(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        req = await svc.reject(s, req.id, provider.id)
    assert req.status == "rejected"
    await expect_error("INVALID_STATE", _reject(sessions, req, provider), 409)  # twice


async def _reject(factory, req, user):
    async with factory() as s:
        return await svc.reject(s, req.id, user.id)


async def test_only_the_right_party_can_act(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post, "100")
    outsider = await make_user(sessions)

    async with sessions() as s:
        await expect_error("NOT_PROVIDER", svc.accept(s, req.id, requester.id), 403)
    async with sessions() as s:
        await expect_error("REQUEST_NOT_FOUND", svc.accept(s, req.id, outsider.id), 404)
    async with sessions() as s:
        await expect_error("NOT_REQUESTER", svc.cancel(s, req.id, provider.id), 403)
    async with sessions() as s:
        await expect_error("REQUEST_NOT_FOUND", svc.get_request(s, req.id, outsider.id))


async def test_accept_then_requester_can_still_cancel(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        req = await svc.accept(s, req.id, provider.id)
    assert req.status == "accepted"
    async with sessions() as s:
        req = await svc.cancel(s, req.id, requester.id)
    assert req.status == "cancelled"


async def test_cancel_is_not_allowed_once_a_call_is_live(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        r = await s.get(CallRequest, req.id)
        r.status = "live"
        await s.commit()
    async with sessions() as s:
        await expect_error("INVALID_STATE", svc.cancel(s, req.id, requester.id), 409)


async def test_accepting_a_request_past_its_window_expires_it(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        r = await s.get(CallRequest, req.id)
        r.expires_at = datetime.utcnow() - timedelta(seconds=1)
        await s.commit()
    async with sessions() as s:
        await expect_error("REQUEST_EXPIRED", svc.accept(s, req.id, provider.id), 410)
    assert (await reload(sessions, req)).status == "expired"


async def test_provider_queue_is_oldest_first_and_hides_stale_requests(sessions):
    provider = await make_user(sessions, "Provider")
    post = await make_post(sessions, provider)
    made = []
    for _ in range(3):
        r = await make_user(sessions)
        made.append(await request_call(sessions, r, provider, post, "100"))
    async with sessions() as s:
        stale = await s.get(CallRequest, made[0].id)
        stale.expires_at = datetime.utcnow() - timedelta(seconds=1)
        await s.commit()
    async with sessions() as s:
        queue = await svc.list_incoming(s, provider.id)
    assert [q.id for q in queue] == [made[1].id, made[2].id]


# --- accepting: now the requester has to pay -----------------------------------------------------


async def test_accepting_asks_the_requester_to_pay_within_the_payment_window(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    assert req.status == "accepted" and req.payee_upi_id == provider.upi_id
    assert abs((req.expires_at - datetime.utcnow()) - timedelta(minutes=CALL_PAY_WINDOW_MINUTES)) < timedelta(seconds=10)
    assert (requester.id, "call_request_accepted") in sessions.pushes


async def test_the_upi_id_to_pay_is_fixed_when_the_driver_accepts(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    async with sessions() as s:
        (await s.get(User, provider.id)).upi_id = "someone.else@ybl"
        await s.commit()
    assert (await reload(sessions, req)).payee_upi_id == provider.upi_id


async def test_a_driver_who_lost_their_upi_id_cannot_accept(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        (await s.get(User, provider.id)).upi_id = None
        await s.commit()
    await expect_error("PROVIDER_UPI_MISSING", accept_request(sessions, req, provider), 409)
    assert (await reload(sessions, req)).status == "pending"


async def test_the_driver_cannot_start_the_call_before_the_payment_is_confirmed(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    async with sessions() as s:
        await expect_error("PAYMENT_NOT_CONFIRMED", svc.start_call(s, req.id, provider.id), 409)
    await pay(sessions, req, requester)
    async with sessions() as s:
        await expect_error("PAYMENT_NOT_CONFIRMED", svc.start_call(s, req.id, provider.id), 409)


@pytest.mark.parametrize("stage", ["accepted", "payment_claimed", "ready"])
async def test_every_open_stage_blocks_a_second_request_from_the_same_pair(sessions, stage):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post, "200")
    await accept_request(sessions, req, provider)
    if stage != "accepted":
        await pay(sessions, req, requester)
    if stage == "ready":
        await confirm(sessions, req, provider, True)
    existing = await expect_error("ALREADY_OPEN", request_call(sessions, requester, provider, post, "200"), 409)
    assert existing.extra["requestId"] == str(req.id)


async def test_the_drivers_queue_holds_every_stage_that_needs_them_oldest_first(sessions):
    provider = await make_user(sessions, "Provider")
    post = await make_post(sessions, provider)
    made = {}
    for stage in ("pending", "accepted", "payment_claimed", "ready"):
        requester = await make_user(sessions, stage)
        req = await request_call(sessions, requester, provider, post, "100")
        if stage != "pending":
            await accept_request(sessions, req, provider)
        if stage in ("payment_claimed", "ready"):
            await pay(sessions, req, requester)
        if stage == "ready":
            await confirm(sessions, req, provider, True)
        made[stage] = req
    async with sessions() as s:
        queue = await svc.list_incoming(s, provider.id)
    assert [(q.id, q.status) for q in queue] == [(made[k].id, k) for k in ("pending", "accepted", "payment_claimed", "ready")]


# --- the driver backing out ---------------------------------------------------------------------------


async def test_the_driver_can_back_out_of_an_accepted_request_until_it_is_paid(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    async with sessions() as s:
        req = await svc.withdraw(s, req.id, provider.id)
    assert req.status == "rejected" and req.closed_reason == "driver_withdrew"
    assert (requester.id, "call_request_rejected") in sessions.pushes
    # the pair is free to ask again
    assert (await request_call(sessions, requester, provider, post, "200")).status == "pending"


async def test_the_driver_cannot_back_out_once_the_requester_says_they_paid(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    await pay(sessions, req, requester)
    async with sessions() as s:
        await expect_error("INVALID_STATE", svc.withdraw(s, req.id, provider.id), 409)


async def test_only_the_driver_can_withdraw_and_a_pending_request_is_rejected_not_withdrawn(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        await expect_error("INVALID_STATE", svc.withdraw(s, req.id, provider.id), 409)
    req = await accept_request(sessions, req, provider)
    async with sessions() as s:
        await expect_error("NOT_PROVIDER", svc.withdraw(s, req.id, requester.id), 403)


# --- the requester pays in advance -----------------------------------------------------------------


async def test_paying_records_a_claim_and_asks_the_driver_to_confirm(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    req = await pay(sessions, req, requester, reference="UPI123")

    assert req.status == "payment_claimed" and req.payment_outcome == "claimed"
    assert req.paid_amount == D("200.00") and req.upi_reference == "UPI123"
    assert req.payment_claimed_at is not None and req.provider_confirmed is None
    # the driver now has up to 24 hours to answer
    assert abs((req.expires_at - datetime.utcnow()) - timedelta(hours=CALL_CLAIM_CONFIRM_TTL_HOURS)) < timedelta(seconds=10)
    assert (provider.id, "call_payment") in sessions.pushes
    async with sessions() as s:  # no wallet movement for anyone
        assert await s.scalar(select(WalletTransaction.id).where(WalletTransaction.reference_id == str(req.id))) is None


async def test_the_whole_offer_is_paid_whatever_it_is(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "1234"), provider)
    assert (await pay(sessions, req, requester)).paid_amount == D("1234.00")


async def test_retrying_the_same_payment_claim_is_safe(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    await pay(sessions, req, requester)
    assert (await pay(sessions, req, requester)).status == "payment_claimed"
    await confirm(sessions, req, provider, True)
    assert (await pay(sessions, req, requester)).status == "ready"  # even after it moved on
    assert sum(1 for who, kind in sessions.pushes if who == provider.id and kind == "call_payment") == 1


async def test_two_simultaneous_payment_taps_claim_once(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    results = await asyncio.gather(pay(sessions, req, requester), pay(sessions, req, requester), return_exceptions=True)
    assert all(isinstance(r, CallRequest) for r in results)
    assert sum(1 for who, kind in sessions.pushes if who == provider.id and kind == "call_payment") == 1


async def test_payment_rules_about_who_and_when(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post, "200")
    await expect_error("INVALID_STATE", pay(sessions, req, requester), 409)  # not accepted yet
    await accept_request(sessions, req, provider)
    await expect_error("NOT_REQUESTER", pay(sessions, req, provider), 403)
    outsider = await make_user(sessions)
    await expect_error("REQUEST_NOT_FOUND", pay(sessions, req, outsider), 404)


async def test_the_time_to_pay_runs_out(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    await age(sessions, req, expires_at=datetime.utcnow() - timedelta(seconds=1))
    await expect_error("REQUEST_EXPIRED", pay(sessions, req, requester), 410)
    assert (await reload(sessions, req)).status == "expired"


async def test_the_requester_can_cancel_until_they_say_they_paid(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    async with sessions() as s:
        cancelled = await svc.cancel(s, req.id, requester.id)
    assert cancelled.status == "cancelled" and cancelled.closed_reason == "requester_cancelled"

    again = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    await pay(sessions, again, requester)
    async with sessions() as s:
        await expect_error("INVALID_STATE", svc.cancel(s, again.id, requester.id), 409)


# --- the driver confirms (or not) ------------------------------------------------------------------


async def test_the_driver_confirming_opens_the_call_with_a_fresh_start_window(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    await pay(sessions, req, requester)
    req = await confirm(sessions, req, provider, True)

    assert req.status == "ready" and req.payment_outcome == "paid" and req.provider_confirmed is True
    assert req.provider_confirmed_at is not None
    assert abs((req.expires_at - datetime.utcnow()) - timedelta(minutes=CALL_START_WINDOW_MINUTES)) < timedelta(seconds=10)
    assert (requester.id, "call_payment_confirmed") in sessions.pushes
    assert (await confirm(sessions, req, provider, True)).status == "ready"  # a second tap is harmless
    assert sum(1 for who, kind in sessions.pushes if who == requester.id and kind == "call_payment_confirmed") == 1


async def test_a_driver_who_says_no_must_be_asked_to_be_sure(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    await pay(sessions, req, requester)

    await expect_error("CONFIRM_REQUIRED", confirm(sessions, req, provider, False), 409)
    still = await reload(sessions, req)
    assert still.status == "payment_claimed" and still.provider_confirmed is None  # nothing changed
    async with sessions() as s:
        types = [e.event_type for e in (await s.execute(select(CallBehaviourEvent).where(CallBehaviourEvent.call_request_id == req.id))).scalars()]
    assert "provider_denied_payment_unsure" in types  # but the first no is on record

    # and the driver can still change their mind
    assert (await confirm(sessions, req, provider, True)).status == "ready"


async def test_saying_no_twice_closes_the_request_as_a_dispute_and_the_call_never_starts(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    await pay(sessions, req, requester)
    await expect_error("CONFIRM_REQUIRED", confirm(sessions, req, provider, False), 409)
    req = await confirm(sessions, req, provider, False, sure=True)

    assert req.status == "payment_disputed" and req.payment_outcome == "disputed"
    assert req.provider_confirmed is False and req.disputed_at is not None and req.closed_reason == "provider_denied"
    assert (requester.id, "call_payment_disputed") in sessions.pushes
    assert (provider.id, "call_payment_disputed") in sessions.pushes
    async with sessions() as s:
        await expect_error("INVALID_STATE", svc.start_call(s, req.id, provider.id), 409)
    assert (await confirm(sessions, req, provider, False, sure=True)).status == "payment_disputed"  # harmless
    await expect_error("NO_PAYMENT_TO_CONFIRM", confirm(sessions, req, provider, True), 409)  # no reversing it
    # the pair may ask again
    assert (await request_call(sessions, requester, provider, post, "200")).status == "pending"


async def test_confirming_rules_about_who_and_when(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    await expect_error("NO_PAYMENT_TO_CONFIRM", confirm(sessions, req, provider, True), 409)  # nothing paid yet
    await pay(sessions, req, requester)
    await expect_error("NOT_PROVIDER", confirm(sessions, req, requester, True), 403)
    outsider = await make_user(sessions)
    await expect_error("REQUEST_NOT_FOUND", confirm(sessions, req, outsider, True), 404)


async def test_confirming_after_the_claim_window_lapsed_still_works_until_the_sweep_runs(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    await pay(sessions, req, requester)
    await age(sessions, req, expires_at=datetime.utcnow() - timedelta(minutes=1))
    assert (await confirm(sessions, req, provider, True)).status == "ready"  # the money was paid: don't punish a late yes


# --- what a call that ends looks like ----------------------------------------------------------------


async def test_a_call_that_never_connected_at_all_fails(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await make_ready(sessions, await request_call(sessions, requester, provider, post, "200"), requester, provider)
    req = await connect_call(sessions, req, seconds=0)
    assert req.status == "failed" and req.closed_reason == "call_not_connected" and req.threshold_met is None


async def test_a_short_call_is_completed_but_not_a_counted_call(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await make_ready(sessions, await request_call(sessions, requester, provider, post, "200"), requester, provider)
    req = await connect_call(sessions, req, seconds=29)
    assert req.status == "completed" and req.threshold_met is False and req.payment_outcome == "paid"


async def test_a_call_of_30_seconds_is_a_counted_call(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await make_ready(sessions, await request_call(sessions, requester, provider, post, "200"), requester, provider)
    req = await connect_call(sessions, req, seconds=30)
    assert req.status == "completed" and req.threshold_met is True and req.provider_media_on is True
    assert req.payee_upi_id == provider.upi_id


# --- the redeem rule that used to depend on calls -------------------------------------------------


async def test_the_whole_wallet_can_be_withdrawn_whatever_calls_are_open(sessions):
    requester, provider, post = await setup_pair(sessions)
    await fund(sessions, requester, "1000")
    await request_call(sessions, requester, provider, post, "800")
    async with sessions() as s:
        redeem = await wallet_service.initiate_redeem(s, requester.id, "me@ybl", f"redeem-{uuid.uuid4()}")
    assert redeem.amount == D("1000.00")  # no call holds or commitments any more


async def test_redeem_still_needs_approved_kyc(sessions):
    user = await make_user(sessions, "No KYC", kyc="pending")
    await fund(sessions, user, "1000")
    async with sessions() as s:
        with pytest.raises(Exception) as exc:
            await wallet_service.initiate_redeem(s, user.id, "me@ybl", f"redeem-{uuid.uuid4()}")
    assert getattr(exc.value, "status_code", None) == 403


# --- background sweep -----------------------------------------------------------------------------


async def drain_sweep(factory):
    """The sweep is global: clear what earlier tests left stale so counts are exact."""
    async with factory() as s:
        await svc.sweep(s)


async def test_sweep_expires_each_stage_when_its_own_window_runs_out(sessions):
    await drain_sweep(sessions)
    requester = await make_user(sessions, "Requester")
    stages = {}
    for name in ("pending", "accepted", "ready"):
        provider = await make_user(sessions, name)
        post = await make_post(sessions, provider)
        req = await request_call(sessions, requester, provider, post, "100")
        if name == "accepted":
            req = await accept_request(sessions, req, provider)
        if name == "ready":
            req = await make_ready(sessions, req, requester, provider)
        await age(sessions, req, expires_at=datetime.utcnow() - timedelta(minutes=1))
        stages[name] = (req, provider)

    async with sessions() as s:
        counts = await svc.sweep(s)
    assert counts["expired"] == 3
    async with sessions() as s:
        assert not any((await svc.sweep(s)).values())  # nothing left, nothing repeated

    reasons = {name: (await reload(sessions, req)) for name, (req, _) in stages.items()}
    assert {n: (r.status, r.closed_reason) for n, r in reasons.items()} == {
        "pending": ("expired", "no_response"),
        "accepted": ("expired", "payment_window_lapsed"),
        "ready": ("expired", "start_window_lapsed"),
    }
    # the requester and driver are told what fits: unpaid -> both; paid but never called -> the requester
    accepted_provider = stages["accepted"][1]
    assert (accepted_provider.id, "call_request_expired") in sessions.pushes
    assert (requester.id, "call_request_expired") in sessions.pushes


async def test_a_payment_claim_the_driver_never_answers_goes_to_admin_review_after_24_hours(sessions):
    await drain_sweep(sessions)
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    await pay(sessions, req, requester)

    async with sessions() as s:
        assert (await svc.sweep(s))["settled"] == 0  # still inside the 24 hours: keep waiting
    assert (await reload(sessions, req)).status == "payment_claimed"

    await age(sessions, req, expires_at=datetime.utcnow() - timedelta(minutes=1))
    async with sessions() as s:
        assert (await svc.sweep(s))["settled"] == 1
    closed = await reload(sessions, req)
    assert closed.status == "payment_disputed" and closed.closed_reason == "provider_silent"
    assert closed.payment_outcome == "disputed" and closed.disputed_at is not None
    assert (requester.id, "call_payment_disputed") in sessions.pushes
    assert (provider.id, "call_payment_disputed") in sessions.pushes
    async with sessions() as s:
        assert not any((await svc.sweep(s)).values())


async def test_a_sweep_running_in_two_places_at_once_closes_a_request_once(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        r = await s.get(CallRequest, req.id)
        r.expires_at = datetime.utcnow() - timedelta(minutes=1)
        await s.commit()

    async def run():
        async with sessions() as s:
            return await svc.sweep(s)

    first, second = await asyncio.gather(run(), run())
    assert (await reload(sessions, req)).status == "expired"
    assert first["expired"] + second["expired"] >= 1


# --- behaviour events --------------------------------------------------------------------------------


async def test_behaviour_events_record_both_sides_and_the_payment(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await accept_request(sessions, await request_call(sessions, requester, provider, post, "200"), provider)
    async with sessions() as s:
        n = await svc.record_client_events(
            s,
            req.id,
            requester.id,
            [{"type": "mic_off"}, {"type": "chat_opened"}, {"type": "payment_sheet_opened"}, {"type": "upi_app_opened"}],
        )
    assert n == 4
    await pay(sessions, req, requester)
    async with sessions() as s:
        assert await svc.record_client_events(s, req.id, provider.id, [{"type": "payment_confirm_sheet_opened"}]) == 1
    await confirm(sessions, req, provider, True)
    req = await connect_call(sessions, req)

    async with sessions() as s:
        events = (
            await s.execute(
                select(CallBehaviourEvent).where(CallBehaviourEvent.call_request_id == req.id).order_by(CallBehaviourEvent.created_at)
            )
        ).scalars().all()
    by_type = {e.event_type: e for e in events}
    assert by_type["request_created"].role == "requester"
    assert by_type["request_accepted"].role == "provider"
    assert by_type["mic_off"].source == "client" and by_type["mic_off"].role == "requester"
    assert by_type["payment_sheet_opened"].source == "client"
    assert by_type["payment_confirm_sheet_opened"].role == "provider"
    assert by_type["payment_claimed"].role == "requester" and by_type["payment_claimed"].details["amount"] == "200.00"
    assert by_type["provider_confirmed_payment"].role == "provider"
    assert by_type["call_completed"].role == "system" and by_type["call_completed"].details["thresholdMet"] is True


async def test_behaviour_events_reject_unknown_types_and_outsiders(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        await expect_error("UNKNOWN_EVENT", svc.record_client_events(s, req.id, requester.id, [{"type": "typed_a_secret"}]), 400)
    async with sessions() as s:
        outsider = await make_user(sessions)
        await expect_error("REQUEST_NOT_FOUND", svc.record_client_events(s, req.id, outsider.id, [{"type": "mic_on"}]))
    async with sessions() as s:
        await expect_error(
            "TOO_MANY_EVENTS",
            svc.record_client_events(s, req.id, requester.id, [{"type": "mic_on"}] * 51),
            400,
        )


async def test_dev_credit_is_off_by_default(sessions):
    user = await make_user(sessions)
    async with sessions() as s:
        with pytest.raises(Exception) as exc:
            await wallet_service.dev_credit_wallet(s, user.id, D("100"), "x")
    assert getattr(exc.value, "status_code", None) == 404


# --- account deletion ---------------------------------------------------------------------------------


async def test_deleting_a_user_with_calls_works_and_takes_their_calls_with_them(sessions):
    from services.user_deletion import delete_user_cascade

    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(
        sessions,
        await make_ready(sessions, await request_call(sessions, requester, provider, post, "200"), requester, provider),
    )
    async with sessions() as s:
        user = await s.get(User, requester.id)
        await delete_user_cascade(s, user)
        await s.commit()
    assert await reload(sessions, req) is None
