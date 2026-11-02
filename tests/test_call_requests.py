"""Money and state-machine tests for paid 1:1 call requests.

The pure rules run anywhere. The rest need a real Postgres+PostGIS (row locks and
the partial unique index are the point), so they only run when you name a LOCAL
scratch database:

    export CALL_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
    .venv/bin/python -m pytest tests/test_call_requests.py -q

They refuse any non-local URL: local dev shares the production database, and
these tests create users, wallets and money.
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


async def make_user(factory, name="User", kyc="approved") -> User:
    async with factory() as s:
        user = User(mobile=f"9{uuid.uuid4().int % 10**9:09d}", name=name, kyc_status=kyc)
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


async def setup_pair(factory, balance="1000"):
    requester = await make_user(factory, "Requester")
    provider = await make_user(factory, "Provider")
    post = await make_post(factory, provider)
    await fund(factory, requester, balance)
    return requester, provider, post


async def expect_error(code, awaitable, status=None):
    with pytest.raises(CallRequestError) as exc:
        await awaitable
    assert exc.value.code == code
    if status:
        assert exc.value.status_code == status
    return exc.value


async def connect_call(factory, request, seconds=120):
    """Stand-in for the call stage: the request is live, then ends."""
    async with factory() as s:
        r = await s.get(CallRequest, request.id)
        r.status = "live"
        await s.commit()
    async with factory() as s:
        return await svc.finalize_call(s, request.id, seconds, "ended_by_provider")


pytestmark = [needs_db, pytest.mark.anyio]


# --- creating a request ----------------------------------------------------------------


async def test_create_freezes_only_the_call_amount_but_commits_the_rest(sessions):
    requester, provider, post = await setup_pair(sessions, "1000")
    req = await request_call(sessions, requester, provider, post, "200")

    assert req.status == "pending"
    assert req.hold_amount == D("50.00")
    w = await wallet_of(sessions, requester)
    assert (w.available_balance, w.reserved_balance) == (D("950.00"), D("50.00"))
    async with sessions() as s:
        assert await wallet_service.call_commitments(s, requester.id) == D("150.00")
        rows = (
            await s.execute(select(WalletTransaction).where(WalletTransaction.reference_id == str(req.id)))
        ).scalars().all()
    assert [(r.type, r.amount) for r in rows] == [("call_hold", D("50.00"))]
    assert rows[0].available_after == D("950.00") and rows[0].reserved_after == D("50.00")
    assert (provider.id, "call_request") in sessions.pushes


async def test_insufficient_balance_says_how_much_to_add_and_changes_nothing(sessions):
    requester, provider, post = await setup_pair(sessions, "120")
    err = await expect_error("INSUFFICIENT_BALANCE", request_call(sessions, requester, provider, post, "200"), 402)
    assert err.extra["shortfall"] == "80.00" and err.extra["requiredAmount"] == "200.00"
    w = await wallet_of(sessions, requester)
    assert (w.available_balance, w.reserved_balance) == (D("120.00"), D("0.00"))
    async with sessions() as s:
        assert await s.scalar(select(CallRequest.id).where(CallRequest.requester_id == requester.id)) is None


async def test_offers_to_several_drivers_can_never_add_up_to_more_than_the_balance(sessions):
    requester = await make_user(sessions, "Requester")
    await fund(sessions, requester, "100")
    p1, p2 = await make_user(sessions), await make_user(sessions)
    post1, post2 = await make_post(sessions, p1), await make_post(sessions, p2)

    await request_call(sessions, requester, p1, post1, "100")  # freezes 25, commits the other 75
    await expect_error("INSUFFICIENT_BALANCE", request_call(sessions, requester, p2, post2, "50"))


async def test_two_requests_racing_for_one_balance_only_one_wins(sessions):
    requester = await make_user(sessions, "Requester")
    await fund(sessions, requester, "100")
    p1, p2 = await make_user(sessions), await make_user(sessions)
    post1, post2 = await make_post(sessions, p1), await make_post(sessions, p2)

    results = await asyncio.gather(
        request_call(sessions, requester, p1, post1, "100"),
        request_call(sessions, requester, p2, post2, "100"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, CallRequest) for r in results) == 1
    assert sum(isinstance(r, CallRequestError) and r.code == "INSUFFICIENT_BALANCE" for r in results) == 1


async def test_one_open_request_per_pair_until_it_ends(sessions):
    requester, provider, post = await setup_pair(sessions, "1000")
    first = await request_call(sessions, requester, provider, post, "100")
    err = await expect_error("ALREADY_OPEN", request_call(sessions, requester, provider, post, "100"), 409)
    assert err.extra["requestId"] == str(first.id)

    async with sessions() as s:
        await svc.reject(s, first.id, provider.id)
    again = await request_call(sessions, requester, provider, post, "100")  # no cooldown
    assert again.id != first.id


async def test_the_same_pair_cannot_get_two_open_requests_even_if_the_check_is_raced(sessions):
    requester, provider, post = await setup_pair(sessions, "1000")
    results = await asyncio.gather(
        request_call(sessions, requester, provider, post, "100"),
        request_call(sessions, requester, provider, post, "100"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, CallRequest) for r in results) == 1
    w = await wallet_of(sessions, requester)
    assert w.reserved_balance == D("25.00")  # only one hold was ever taken


@pytest.mark.parametrize("amount", ["49.99", "10000.01", "0", "-5"])
async def test_amount_limits(sessions, amount):
    requester, provider, post = await setup_pair(sessions, "20000")
    await expect_error("INVALID_AMOUNT", request_call(sessions, requester, provider, post, amount), 400)


async def test_max_amount_is_allowed(sessions):
    requester, provider, post = await setup_pair(sessions, "20000")
    req = await request_call(sessions, requester, provider, post, "10000")
    assert req.hold_amount == D("2500.00")


async def test_other_validation(sessions):
    requester, provider, post = await setup_pair(sessions, "1000")
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


async def test_a_user_who_never_did_kyc_can_still_have_a_wallet_and_ask_for_a_call(sessions):
    requester = await make_user(sessions, "No KYC", kyc="pending")
    provider = await make_user(sessions)
    post = await make_post(sessions, provider)
    await fund(sessions, requester, "500")
    assert (await request_call(sessions, requester, provider, post, "100")).status == "pending"


# --- provider decisions -----------------------------------------------------------------


async def test_reject_releases_the_hold(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        req = await svc.reject(s, req.id, provider.id)
    assert req.status == "rejected"
    w = await wallet_of(sessions, requester)
    assert (w.available_balance, w.reserved_balance) == (D("500.00"), D("0.00"))
    async with sessions() as s:
        assert await wallet_service.call_commitments(s, requester.id) == 0
    await expect_error("INVALID_STATE", _reject(sessions, req, provider), 409)  # twice


async def _reject(factory, req, user):
    async with factory() as s:
        return await svc.reject(s, req.id, user.id)


async def test_only_the_right_party_can_act(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
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


async def test_accept_then_requester_can_still_cancel_with_full_refund(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        req = await svc.accept(s, req.id, provider.id)
    assert req.status == "accepted"
    async with sessions() as s:
        req = await svc.cancel(s, req.id, requester.id)
    assert req.status == "cancelled"
    w = await wallet_of(sessions, requester)
    assert (w.available_balance, w.reserved_balance) == (D("500.00"), D("0.00"))


async def test_cancel_is_not_allowed_once_a_call_is_live(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        r = await s.get(CallRequest, req.id)
        r.status = "live"
        await s.commit()
    async with sessions() as s:
        await expect_error("INVALID_STATE", svc.cancel(s, req.id, requester.id), 409)


async def test_accepting_a_request_past_its_window_expires_it_and_releases_the_money(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        r = await s.get(CallRequest, req.id)
        r.expires_at = datetime.utcnow() - timedelta(seconds=1)
        await s.commit()
    async with sessions() as s:
        await expect_error("REQUEST_EXPIRED", svc.accept(s, req.id, provider.id), 410)
    assert (await reload(sessions, req)).status == "expired"
    assert (await wallet_of(sessions, requester)).reserved_balance == D("0.00")


async def test_provider_queue_is_oldest_first_and_hides_stale_requests(sessions):
    provider = await make_user(sessions, "Provider")
    post = await make_post(sessions, provider)
    made = []
    for _ in range(3):
        r = await make_user(sessions)
        await fund(sessions, r, "500")
        made.append(await request_call(sessions, r, provider, post, "100"))
    async with sessions() as s:
        stale = await s.get(CallRequest, made[0].id)
        stale.expires_at = datetime.utcnow() - timedelta(seconds=1)
        await s.commit()
    async with sessions() as s:
        queue = await svc.list_incoming(s, provider.id)
    assert [q.id for q in queue] == [made[1].id, made[2].id]


# --- call outcome and settlement ------------------------------------------------------------


async def test_call_that_never_connected_for_30_seconds_costs_nothing(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
    req = await request_call(sessions, requester, provider, post, "200")
    req = await connect_call(sessions, req, seconds=29)
    assert req.status == "failed"
    w = await wallet_of(sessions, requester)
    assert (w.available_balance, w.reserved_balance) == (D("500.00"), D("0.00"))
    assert (await wallet_of(sessions, provider)).available_balance == D("0.00")


async def test_connected_call_keeps_the_money_frozen_until_the_requester_answers(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
    req = await request_call(sessions, requester, provider, post, "200")
    req = await connect_call(sessions, req, seconds=30)
    assert req.status == "awaiting_feedback" and req.feedback_deadline is not None
    w = await wallet_of(sessions, requester)
    assert w.reserved_balance == D("50.00")
    async with sessions() as s:
        assert await wallet_service.call_commitments(s, requester.id) == D("150.00")  # still spoken for


async def test_satisfied_requester_pays_the_full_offer(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
    before = await total_money(sessions, requester, provider)
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"))

    async with sessions() as s:
        req = await svc.submit_feedback(s, req.id, requester.id, True)

    assert req.status == "completed" and req.settled_amount == D("200.00") and req.satisfied is True
    rw, pw = await wallet_of(sessions, requester), await wallet_of(sessions, provider)
    assert (rw.available_balance, rw.reserved_balance) == (D("300.00"), D("0.00"))
    assert (pw.available_balance, pw.reserved_balance) == (D("200.00"), D("0.00"))
    assert await total_money(sessions, requester, provider) == before  # money moved, none created or lost
    async with sessions() as s:
        types = {
            (t.user_id, t.type): t.amount
            for t in (
                await s.execute(select(WalletTransaction).where(WalletTransaction.reference_id == str(req.id)))
            ).scalars()
        }
    assert types[(requester.id, "call_hold")] == D("50.00")
    assert types[(requester.id, "call_debit")] == D("200.00")
    assert types[(provider.id, "call_earning")] == D("200.00")
    assert (provider.id, "call_payment") in sessions.pushes


async def test_unsatisfied_requester_pays_only_the_call_amount(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"))
    async with sessions() as s:
        req = await svc.submit_feedback(s, req.id, requester.id, False)

    assert req.status == "completed" and req.settled_amount == D("50.00") and req.satisfied is False
    rw, pw = await wallet_of(sessions, requester), await wallet_of(sessions, provider)
    assert (rw.available_balance, rw.reserved_balance) == (D("450.00"), D("0.00"))
    assert pw.available_balance == D("50.00")
    async with sessions() as s:
        assert await wallet_service.call_commitments(s, requester.id) == 0  # the rest is free again


async def test_retrying_the_same_answer_is_safe_but_changing_it_is_refused(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"))
    async with sessions() as s:
        await svc.submit_feedback(s, req.id, requester.id, True)
    async with sessions() as s:
        again = await svc.submit_feedback(s, req.id, requester.id, True)
    assert again.status == "completed"
    async with sessions() as s:
        await expect_error("ALREADY_SETTLED", svc.submit_feedback(s, req.id, requester.id, False), 409)
    assert (await wallet_of(sessions, provider)).available_balance == D("200.00")  # paid once


async def test_two_simultaneous_answers_settle_once(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
    req = await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"))

    async def answer():
        async with sessions() as s:
            return await svc.submit_feedback(s, req.id, requester.id, True)

    results = await asyncio.gather(answer(), answer(), return_exceptions=True)
    assert all(isinstance(r, CallRequest) for r in results)
    assert (await wallet_of(sessions, provider)).available_balance == D("200.00")
    assert (await wallet_of(sessions, requester)).available_balance == D("300.00")


async def test_feedback_only_after_a_connected_call(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        await expect_error("INVALID_STATE", svc.submit_feedback(s, req.id, requester.id, True), 409)


async def test_requester_who_spends_the_committed_rest_still_cannot_underpay_the_provider(sessions):
    """Redeem can't touch money promised to an open request, so 'satisfied' is
    always payable in full."""
    requester = await make_user(sessions, "Requester")
    provider = await make_user(sessions, "Provider")
    post = await make_post(sessions, provider)
    await fund(sessions, requester, "2000")
    req = await request_call(sessions, requester, provider, post, "1000")  # freezes 250, commits 750

    async with sessions() as s:
        redeem = await wallet_service.initiate_redeem(s, requester.id, "me@ybl", f"redeem-{uuid.uuid4()}")
    assert redeem.amount == D("1000.00")  # 2000 - 250 frozen - 750 committed
    w = await wallet_of(sessions, requester)
    assert w.available_balance == D("750.00")

    req = await connect_call(sessions, req)
    async with sessions() as s:
        req = await svc.submit_feedback(s, req.id, requester.id, True)
    assert req.settled_amount == D("1000.00")


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


async def test_sweep_expires_old_requests_and_settles_unanswered_feedback_once(sessions):
    await drain_sweep(sessions)
    requester, provider, post = await setup_pair(sessions, "1000")
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
    settled = await reload(sessions, awaiting)
    assert settled.status == "completed" and settled.satisfied is None and settled.settled_amount == D("50.00")
    assert (await wallet_of(sessions, other_provider)).available_balance == D("50.00")
    rw = await wallet_of(sessions, requester)
    assert (rw.available_balance, rw.reserved_balance) == (D("950.00"), D("0.00"))


async def test_a_sweep_running_in_two_places_at_once_does_not_double_release(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        r = await s.get(CallRequest, req.id)
        r.expires_at = datetime.utcnow() - timedelta(minutes=1)
        await s.commit()

    async def run():
        async with sessions() as s:
            return await svc.sweep(s)

    await asyncio.gather(run(), run())
    w = await wallet_of(sessions, requester)
    assert (w.available_balance, w.reserved_balance) == (D("500.00"), D("0.00"))


# --- behaviour events --------------------------------------------------------------------------------


async def test_behaviour_events_record_both_sides(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
    req = await request_call(sessions, requester, provider, post, "200")
    async with sessions() as s:
        await svc.accept(s, req.id, provider.id)
    async with sessions() as s:
        n = await svc.record_client_events(
            s,
            req.id,
            requester.id,
            [{"type": "mic_off"}, {"type": "chat_opened"}, {"type": "chat_message_sent", "details": {"length": 12}}],
        )
    assert n == 3
    req = await connect_call(sessions, req)
    async with sessions() as s:
        await svc.submit_feedback(s, req.id, requester.id, False)

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
    assert by_type["feedback_not_satisfied"].role == "requester"


async def test_behaviour_events_reject_unknown_types_and_outsiders(sessions):
    requester, provider, post = await setup_pair(sessions, "500")
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


async def test_deleting_a_driver_gives_requesters_their_money_back(sessions):
    from services.user_deletion import delete_user_cascade

    requester, provider, post = await setup_pair(sessions, "500")
    await request_call(sessions, requester, provider, post, "200")
    assert (await wallet_of(sessions, requester)).reserved_balance == D("50.00")

    async with sessions() as s:
        user = await s.get(User, provider.id)
        summary = await delete_user_cascade(s, user)
        await s.commit()
    assert summary["callHoldsSettled"] == 1
    w = await wallet_of(sessions, requester)
    assert (w.available_balance, w.reserved_balance) == (D("500.00"), D("0.00"))


async def test_deleting_a_requester_still_pays_a_driver_for_a_connected_call(sessions):
    from services.user_deletion import delete_user_cascade

    requester, provider, post = await setup_pair(sessions, "500")
    await connect_call(sessions, await request_call(sessions, requester, provider, post, "200"))

    async with sessions() as s:
        user = await s.get(User, requester.id)
        await delete_user_cascade(s, user)
        await s.commit()
    assert (await wallet_of(sessions, provider)).available_balance == D("50.00")
