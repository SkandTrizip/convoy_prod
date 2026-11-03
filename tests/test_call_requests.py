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


async def pay(factory, request, requester, amount_type="full", reference=None):
    async with factory() as s:
        return await svc.pay(s, request.id, requester.id, amount_type, reference)


async def decline(factory, request, requester, reason=None):
    async with factory() as s:
        return await svc.decline(s, request.id, requester.id, reason)


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


# --- after the call: what is owed -----------------------------------------------------------


async def test_a_call_that_never_connected_at_all_is_not_payable(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"), seconds=0)
    assert req.status == "failed" and req.threshold_met is None and req.feedback_deadline is None
    async with sessions() as s:
        await expect_error("INVALID_STATE", svc.pay(s, req.id, requester.id, "full"), 409)


async def test_a_short_call_still_asks_for_payment_but_is_not_a_counted_call(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"), seconds=29)
    assert req.status == "awaiting_payment" and req.threshold_met is False
    assert req.feedback_deadline is not None


async def test_a_call_of_30_seconds_is_a_counted_call_and_remembers_whom_to_pay(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"), seconds=30)
    assert req.status == "awaiting_payment" and req.threshold_met is True and req.provider_media_on is True
    assert req.payee_upi_id == provider.upi_id



async def test_a_change_of_the_drivers_upi_id_after_the_call_does_not_redirect_the_payment(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"))
    async with sessions() as s:
        (await s.get(User, provider.id)).upi_id = "someone.else@ybl"
        await s.commit()
    assert (await reload(sessions, req)).payee_upi_id == provider.upi_id


# --- paying ----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind,expected", [("full", "200.00"), ("minimum", "50.00")])
async def test_paying_the_full_offer_or_the_minimum(sessions, kind, expected):
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"))
    req = await pay(sessions, req, requester, kind, reference="UPI123")

    assert req.status == "completed" and req.payment_outcome == "paid"
    assert req.paid_amount == D(expected) and req.upi_reference == "UPI123"
    assert req.counts_as_strike is False and req.provider_confirmed is None
    assert (provider.id, "call_payment") in sessions.pushes  # the driver is asked to confirm
    async with sessions() as s:  # still no wallet movement for either
        assert await s.scalar(select(WalletTransaction.id).where(WalletTransaction.reference_id == str(req.id))) is None


async def test_a_short_call_can_be_paid_too(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"), seconds=10)
    assert (await pay(sessions, req, requester, "minimum")).payment_outcome == "paid"


async def test_retrying_the_same_payment_is_safe_but_changing_it_is_refused(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"))
    await pay(sessions, req, requester, "full")
    assert (await pay(sessions, req, requester, "full")).status == "completed"
    await expect_error("ALREADY_SETTLED", pay(sessions, req, requester, "minimum"), 409)
    await expect_error("ALREADY_SETTLED", decline(sessions, req, requester, "changed my mind"), 409)


async def test_two_simultaneous_payment_taps_settle_once(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"))
    results = await asyncio.gather(
        pay(sessions, req, requester, "full"), pay(sessions, req, requester, "full"), return_exceptions=True
    )
    assert all(isinstance(r, CallRequest) for r in results)
    assert sum(1 for who, kind in sessions.pushes if who == provider.id and kind == "call_payment") == 1


async def test_payment_rules_about_who_and_when(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await request_call(sessions, requester, provider, post, "200")
    await expect_error("INVALID_STATE", pay(sessions, req, requester, "full"), 409)  # not called yet
    req = await connect_call(sessions, req)
    await expect_error("INVALID_AMOUNT_TYPE", pay(sessions, req, requester, "half"), 400)
    await expect_error("NOT_REQUESTER", pay(sessions, req, provider, "full"), 403)
    outsider = await make_user(sessions)
    await expect_error("REQUEST_NOT_FOUND", pay(sessions, req, outsider, "full"), 404)
    await expect_error("NOT_REQUESTER", decline(sessions, req, provider, "x" * 5), 403)


# --- refusing --------------------------------------------------------------------------------


async def test_refusing_after_a_counted_call_flags_the_requester_and_is_a_strike_when_media_was_on(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"))
    req = await decline(sessions, req, requester)  # no reason needed after a counted call

    assert req.status == "completed" and req.payment_outcome == "refused"
    assert req.counts_as_strike is True and req.paid_amount is None
    assert (provider.id, "call_not_paid") in sessions.pushes
    async with sessions() as s:
        assert await call_payment_standing.standing(s, requester.id) == {
            "flaggedCalls": 1, "strikes": 1, "strikeLimit": 3, "blocked": False, "resetAt": None,
        }


@pytest.mark.parametrize("media", ["mic_only", "camera_only", "none"])
async def test_a_refusal_is_flagged_but_not_a_strike_when_the_drivers_mic_or_camera_was_off(sessions, media):
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"), provider_media=media)
    assert req.provider_media_on is False
    req = await decline(sessions, req, requester, "could not see him")
    assert req.payment_outcome == "refused" and req.counts_as_strike is False
    async with sessions() as s:
        standing = await call_payment_standing.standing(s, requester.id)
    assert (standing["flaggedCalls"], standing["strikes"]) == (1, 0)


async def test_a_short_call_can_only_be_declined_with_a_reason_which_the_driver_is_told(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"), seconds=12)
    await expect_error("REASON_REQUIRED", decline(sessions, req, requester), 400)
    await expect_error("REASON_REQUIRED", decline(sessions, req, requester, "  ok "), 400)  # too short once trimmed
    assert (await reload(sessions, req)).status == "awaiting_payment"  # nothing changed

    req = await decline(sessions, req, requester, "The call dropped after a few seconds")
    assert req.payment_outcome == "declined" and req.payment_reason == "The call dropped after a few seconds"
    assert req.counts_as_strike is False
    assert (provider.id, "call_not_paid") in sessions.pushes
    async with sessions() as s:  # not flagged, not a strike
        standing = await call_payment_standing.standing(s, requester.id)
    assert (standing["flaggedCalls"], standing["strikes"]) == (0, 0)


async def test_declining_twice_is_harmless(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"))
    await decline(sessions, req, requester)
    assert (await decline(sessions, req, requester)).payment_outcome == "refused"
    async with sessions() as s:
        assert (await call_payment_standing.standing(s, requester.id))["strikes"] == 1


# --- three strikes ------------------------------------------------------------------------------


async def refuse_a_counted_call(sessions, requester, media="both"):
    provider = await make_user(sessions, "Provider")
    post = await make_post(sessions, provider)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "100"), provider_media=media)
    return await decline(sessions, req, requester, "no")


async def test_three_strikes_block_new_requests_and_an_admin_can_forgive_them(sessions):
    requester = await make_user(sessions, "Refuser")
    provider = await make_user(sessions, "Driver")
    post = await make_post(sessions, provider)
    for _ in range(2):
        await refuse_a_counted_call(sessions, requester)
    await request_call(sessions, requester, provider, post, "100")  # two strikes: still allowed

    await refuse_a_counted_call(sessions, requester)
    async with sessions() as s:
        assert (await call_payment_standing.standing(s, requester.id))["blocked"] is True
    other_post = await make_post(sessions, await make_user(sessions))
    err = await expect_error("CALL_PAYMENTS_BLOCKED", request_call(sessions, requester, provider, post, "100"), 403)
    assert "can't request calls" in err.message

    async with sessions() as s:
        after = await call_payment_standing.reset_standing(s, requester.id, "admin-1")
    assert after["blocked"] is False and after["strikes"] == 0 and after["flaggedCalls"] == 3  # flags stay on record
    other_provider = await make_user(sessions)
    assert (await request_call(sessions, requester, other_provider, await make_post(sessions, other_provider), "100")).status == "pending"
    await refuse_a_counted_call(sessions, requester)  # new strikes count from the reset
    async with sessions() as s:
        assert (await call_payment_standing.standing(s, requester.id))["strikes"] == 1


async def test_refusals_without_the_drivers_media_never_block(sessions):
    requester = await make_user(sessions, "Refuser")
    for _ in range(4):
        await refuse_a_counted_call(sessions, requester, media="none")
    async with sessions() as s:
        standing = await call_payment_standing.standing(s, requester.id)
    assert (standing["flaggedCalls"], standing["strikes"], standing["blocked"]) == (4, 0, False)


async def test_paid_and_short_calls_never_count_against_anyone(sessions):
    requester = await make_user(sessions, "Good")
    for _ in range(3):
        provider = await make_user(sessions)
        post = await make_post(sessions, provider)
        req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "100"))
        await pay(sessions, req, requester, "minimum")
        provider = await make_user(sessions)
        post = await make_post(sessions, provider)
        short = await connect_call(sessions, await request_call(sessions, requester, provider, post, "100"), seconds=5)
        await decline(sessions, short, requester, "too short to be useful")
    async with sessions() as s:
        standing = await call_payment_standing.standing(s, requester.id)
    assert (standing["flaggedCalls"], standing["strikes"], standing["blocked"]) == (0, 0, False)


async def test_the_flagged_list_is_worst_first(sessions):
    one, two = await make_user(sessions, "One"), await make_user(sessions, "Two")
    await refuse_a_counted_call(sessions, one, media="none")
    await refuse_a_counted_call(sessions, two)
    await refuse_a_counted_call(sessions, two)
    async with sessions() as s:
        listed = [row for row in await call_payment_standing.list_flagged(s) if row["userId"] in (str(one.id), str(two.id))]
    assert [row["name"] for row in listed] == ["Two", "One"]
    assert listed[0]["strikes"] == 2 and listed[1]["strikes"] == 0


# --- the driver confirms ------------------------------------------------------------------------


async def confirm(factory, request, user, received):
    async with factory() as s:
        return await svc.confirm_received(s, request.id, user.id, received)


async def test_the_driver_can_confirm_or_deny_a_payment_and_it_is_only_recorded(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"))
    await expect_error("NO_PAYMENT_TO_CONFIRM", confirm(sessions, req, provider, True), 409)  # nothing paid yet
    await pay(sessions, req, requester, "full")
    await expect_error("NOT_PROVIDER", confirm(sessions, req, requester, True), 403)

    req = await confirm(sessions, req, provider, False)
    assert req.provider_confirmed is False and req.provider_confirmed_at is not None
    req = await confirm(sessions, req, provider, True)  # people mis-tap: the last answer stands
    assert req.provider_confirmed is True and req.status == "completed" and req.payment_outcome == "paid"
    async with sessions() as s:
        types = [e.event_type for e in (await s.execute(select(CallBehaviourEvent).where(CallBehaviourEvent.call_request_id == req.id).order_by(CallBehaviourEvent.created_at))).scalars()]
    assert types.count("provider_denied_payment") == 1 and types.count("provider_confirmed_payment") == 1


async def test_nothing_to_confirm_after_a_refusal(sessions):
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"))
    await decline(sessions, req, requester)
    await expect_error("NO_PAYMENT_TO_CONFIRM", confirm(sessions, req, provider, True), 409)


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


async def test_sweep_expires_old_requests_and_closes_unanswered_payments_once(sessions):
    await drain_sweep(sessions)
    requester, provider, post = await setup_pair(sessions)
    stale = await request_call(sessions, requester, provider, post, "100")
    other_provider = await make_user(sessions)
    other_post = await make_post(sessions, other_provider)
    awaiting = await connect_call(sessions, await request_call(sessions, requester, other_provider, other_post, "200"))

    async with sessions() as s:
        a = await s.get(CallRequest, stale.id)
        a.expires_at = datetime.utcnow() - timedelta(minutes=1)
        b = await s.get(CallRequest, awaiting.id)
        b.feedback_deadline = datetime.utcnow() - timedelta(minutes=1)
        await s.commit()

    async with sessions() as s:
        counts = await svc.sweep(s)
    assert (counts["expired"], counts["settled"]) == (1, 1)
    async with sessions() as s:
        assert not any((await svc.sweep(s)).values())  # nothing left, nothing repeated

    assert (await reload(sessions, stale)).status == "expired"
    closed = await reload(sessions, awaiting)
    assert closed.status == "completed" and closed.payment_outcome == "expired" and closed.paid_amount is None
    assert closed.counts_as_strike is True  # a counted call nobody answered is the same as refusing
    assert (other_provider.id, "call_not_paid") in sessions.pushes


async def test_a_short_call_nobody_answered_is_not_a_strike(sessions):
    await drain_sweep(sessions)
    requester, provider, post = await setup_pair(sessions)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"), seconds=8)
    async with sessions() as s:
        (await s.get(CallRequest, req.id)).feedback_deadline = datetime.utcnow() - timedelta(minutes=1)
        await s.commit()
    async with sessions() as s:
        await svc.sweep(s)
    closed = await reload(sessions, req)
    assert closed.payment_outcome == "expired" and closed.counts_as_strike is False
    async with sessions() as s:
        assert (await call_payment_standing.standing(s, requester.id))["flaggedCalls"] == 0


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
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        await svc.accept(s, req.id, provider.id)
    async with sessions() as s:
        n = await svc.record_client_events(
            s,
            req.id,
            requester.id,
            [{"type": "mic_off"}, {"type": "chat_opened"}, {"type": "payment_sheet_opened"}, {"type": "refusal_warning_shown"}],
        )
    assert n == 4
    req = await connect_call(sessions, req)
    await decline(sessions, req, requester, "no")

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
    assert by_type["payment_due"].role == "system" and by_type["payment_due"].details["thresholdMet"] is True
    assert by_type["payment_refused"].role == "requester" and by_type["payment_refused"].details["strike"] is True
    assert by_type["requester_flagged"].role == "system"


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
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"))
    async with sessions() as s:
        user = await s.get(User, requester.id)
        await delete_user_cascade(s, user)
        await s.commit()
    assert await reload(sessions, req) is None
