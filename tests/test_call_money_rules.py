"""Advance-payment money rules: what became of a payment once the request ended (kept / refund due /
review), who gets blocked, and the admin's disputes list and verdicts (scratch database, like the others).

    export CALL_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
    .venv/bin/python -m pytest tests/test_call_money_rules.py -q
"""

import asyncio
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app as app_module
from config import CALL_PAYMENT_STRIKE_LIMIT
from database import get_session
from db.base import CallRequest
from db.serializers import call_request_to_dict, payment_ledger_entry_to_dict
from middleware.admin_auth import get_current_admin
from services import call_disputes, call_payment_standing
from services import call_requests as svc
from services.call_requests import compute_settlement

from tests.test_call_flow import clock, drain, end, live, ring, sweep  # noqa: F401
from tests.test_call_requests import (  # noqa: F401  (fixtures are used by name)
    TEST_URL,
    _asyncpg_url,
    accept_request,
    age,
    anyio_backend,
    confirm,
    connect_call,
    expect_error,
    make_post,
    make_ready,
    make_user,
    needs_db,
    pay,
    reload,
    request_call,
    schema,
    sessions,
    setup_pair,
)

pytestmark = [needs_db, pytest.mark.anyio]
D = Decimal
LIMIT = CALL_PAYMENT_STRIKE_LIMIT


@pytest.fixture(autouse=True)
def empty_call_requests(schema):  # noqa: F811
    """These tests count open disputes and flagged people across the whole table, and the scratch database
    keeps what earlier tests left. It is a scratch database (the call tests refuse to run on anything else),
    so start every test from an empty request table."""
    if not TEST_URL:
        return

    async def clear():
        engine = create_async_engine(_asyncpg_url(TEST_URL))
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM call_requests"))
        await engine.dispose()

    asyncio.run(clear())


# --- the settlement rules themselves (no database) --------------------------------------------------


def req(status, outcome="paid", **fields):
    base = dict(
        status=status,
        payment_outcome=outcome,
        connected_seconds=0,
        end_reason=None,
        provider_media_on=True,
        ring_attempts=0,
        closed_reason=None,
    )
    base.update(fields)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    "request_,expected",
    [
        # the call counted
        (req("completed", connected_seconds=30), ("kept", "call_completed")),
        (req("completed", connected_seconds=600, end_reason="ended_by_provider", provider_media_on=False), ("kept", "call_completed")),
        # a short call: the driver hung up first, or their mic/camera weren't on, is looked at; otherwise kept
        (req("completed", connected_seconds=29, end_reason="ended_by_provider"), ("review", "short_call_ended_by_driver")),
        (req("completed", connected_seconds=10, end_reason="ended_by_requester", provider_media_on=False), ("review", "short_call_driver_media_off")),
        (req("completed", connected_seconds=10, end_reason="ended_by_requester"), ("kept", "short_call")),
        (req("completed", connected_seconds=10, end_reason="disconnected"), ("kept", "short_call")),
        # went live, never really connected
        (req("failed", closed_reason="call_not_connected"), ("review", "call_not_connected")),
        # paid and confirmed, never called / rang and was not answered
        (req("expired", ring_attempts=0), ("refund_due", "driver_never_called")),
        (req("expired", ring_attempts=2), ("kept", "requester_no_answer")),
        (req("cancelled", closed_reason="ring_declined", ring_attempts=1), ("kept", "requester_declined_ring")),
        # disputes always go to a person
        (req("payment_disputed", outcome="disputed", closed_reason="provider_denied"), ("review", "provider_denied")),
        (req("payment_disputed", outcome="disputed", closed_reason="provider_silent"), ("review", "provider_silent")),
        # no money involved, or the request is still going
        (req("expired", outcome=None, closed_reason="payment_window_lapsed"), None),
        (req("expired", outcome="claimed"), None),
        (req("rejected", outcome=None), None),
        (req("live"), None),
    ],
)
def test_the_settlement_rules(request_, expected):
    assert compute_settlement(request_) == expected


# --- settlement as the requests really end ------------------------------------------------------------


async def ready_request(sessions, amount="200"):
    requester, provider, post = await setup_pair(sessions)
    req_ = await request_call(sessions, requester, provider, post, amount)
    await make_ready(sessions, req_, requester, provider)
    return requester, provider, post, req_


async def test_a_driver_who_took_the_money_and_never_called_owes_a_refund(sessions, clock):  # noqa: F811
    await drain(sessions)
    requester, provider, post, req_ = await ready_request(sessions, "250")
    clock.advance(31 * 60)
    await sweep(sessions)

    done = await reload(sessions, req_)
    assert done.status == "expired" and done.closed_reason == "start_window_lapsed"
    assert (done.settlement, done.settlement_reason) == ("refund_due", "driver_never_called")
    assert (requester.id, "call_request_expired") in sessions.pushes
    assert (provider.id, "call_refund_due") in sessions.pushes
    # both apps can see it
    assert call_request_to_dict(done, None, None, requester.id)["refundDue"] is True
    assert call_request_to_dict(done, None, None, provider.id)["refundDue"] is True
    # and the ledger line of that payment says so, for the driver and for the requester
    assert payment_ledger_entry_to_dict(done, requester, provider, provider.id)["refundDue"] is True
    assert payment_ledger_entry_to_dict(done, requester, provider, requester.id)["refundDue"] is True


async def test_a_requester_who_never_answers_the_ring_keeps_nothing_back(sessions, clock):  # noqa: F811
    await drain(sessions)
    requester, provider, post, req_ = await ready_request(sessions)
    await ring(sessions, req_, provider)
    clock.advance(31 * 60)
    await sweep(sessions)

    done = await reload(sessions, req_)
    assert done.status == "expired"
    assert (done.settlement, done.settlement_reason) == ("kept", "requester_no_answer")
    assert (provider.id, "call_refund_due") not in sessions.pushes
    assert call_request_to_dict(done, None, None, requester.id)["refundDue"] is False
    assert payment_ledger_entry_to_dict(done, requester, provider, provider.id)["refundDue"] is False


async def test_declining_the_ring_after_paying_keeps_the_money_with_the_driver(sessions):
    requester, provider, post, req_ = await ready_request(sessions)
    await ring(sessions, req_, provider)
    async with sessions() as s:
        done = await svc.end_call(s, req_.id, requester.id)
    assert done.status == "cancelled" and (done.settlement, done.settlement_reason) == ("kept", "requester_declined_ring")


async def test_a_call_that_counted_is_kept_and_a_short_one_is_looked_at_when_the_driver_ended_it(sessions):
    for seconds, media, ended, expected in [
        (120, "both", "ended_by_provider", ("kept", "call_completed")),
        (20, "both", "ended_by_provider", ("review", "short_call_ended_by_driver")),
        (20, "none", "ended_by_provider", ("review", "short_call_ended_by_driver")),
        (0, "both", "ended_by_provider", ("review", "call_not_connected")),
    ]:
        requester, provider, post, req_ = await ready_request(sessions)
        async with sessions() as s:
            r = await s.get(CallRequest, req_.id)
            r.status, r.provider_beats = "live", 20
            r.provider_mic_beats = r.provider_camera_beats = 20 if media == "both" else 0
            await s.commit()
        async with sessions() as s:
            done = await svc.finalize_call(s, req_.id, seconds, ended)
        assert (done.settlement, done.settlement_reason) == expected, (seconds, media)


async def test_a_short_call_the_requester_ended_with_the_drivers_media_on_is_simply_kept(sessions):
    requester, provider, post, req_ = await ready_request(sessions)
    async with sessions() as s:
        r = await s.get(CallRequest, req_.id)
        r.status, r.provider_beats, r.provider_mic_beats, r.provider_camera_beats = "live", 20, 20, 20
        await s.commit()
    async with sessions() as s:
        done = await svc.finalize_call(s, req_.id, 12, "ended_by_requester")
    assert (done.settlement, done.settlement_reason) == ("kept", "short_call")


async def test_a_request_nobody_paid_for_has_no_settlement(sessions, clock):  # noqa: F811
    await drain(sessions)
    requester, provider, post = await setup_pair(sessions)
    req_ = await accept_request(sessions, await request_call(sessions, requester, provider, post), provider)
    clock.advance(61 * 60)
    await sweep(sessions)
    lapsed = await reload(sessions, req_)
    assert lapsed.status == "expired" and lapsed.closed_reason == "payment_window_lapsed"
    assert lapsed.settlement is None and lapsed.settlement_reason is None


async def test_both_kinds_of_dispute_go_to_review(sessions):
    requester, provider, post = await setup_pair(sessions)
    req_ = await accept_request(sessions, await request_call(sessions, requester, provider, post), provider)
    await pay(sessions, req_, requester)
    denied = await confirm(sessions, req_, provider, False, sure=True)
    assert (denied.settlement, denied.settlement_reason) == ("review", "provider_denied")

    await drain(sessions)
    again = await accept_request(sessions, await request_call(sessions, requester, provider, post), provider)
    await pay(sessions, again, requester)
    await age(sessions, again, expires_at=datetime.utcnow() - timedelta(minutes=1))
    async with sessions() as s:
        await svc.sweep(s)
    silent = await reload(sessions, again)
    assert (silent.settlement, silent.settlement_reason) == ("review", "provider_silent")


# --- who gets blocked -----------------------------------------------------------------------------------


async def denied_claim(sessions, requester, provider, post):
    req_ = await accept_request(sessions, await request_call(sessions, requester, provider, post), provider)
    await pay(sessions, req_, requester)
    return await confirm(sessions, req_, provider, False, sure=True)


async def unpaid_lapse(sessions, requester, provider, post):
    req_ = await accept_request(sessions, await request_call(sessions, requester, provider, post), provider)
    await age(sessions, req_, expires_at=datetime.utcnow() - timedelta(minutes=1))
    async with sessions() as s:
        await svc.sweep(s)
    return await reload(sessions, req_)


async def never_called(sessions, requester, provider, post):
    req_ = await request_call(sessions, requester, provider, post)
    await make_ready(sessions, req_, requester, provider)
    await age(sessions, req_, expires_at=datetime.utcnow() - timedelta(minutes=1))
    async with sessions() as s:
        await svc.sweep(s)
    return await reload(sessions, req_)


async def test_a_requester_whose_payments_are_denied_three_times_is_blocked(sessions):
    await drain(sessions)
    requester, provider, post = await setup_pair(sessions)
    for n in range(LIMIT):
        async with sessions() as s:
            assert (await call_payment_standing.standing(s, requester.id))["asRequester"]["deniedClaims"] == n
        await denied_claim(sessions, requester, provider, post)
    async with sessions() as s:
        mine = await call_payment_standing.standing(s, requester.id)
    assert mine["asRequester"] == {"deniedClaims": LIMIT, "unpaidLapses": 0, "blocked": True}
    assert mine["blocked"] is True and mine["strikeLimit"] == LIMIT
    async with sessions() as s:
        await expect_error("CALL_PAYMENTS_BLOCKED", svc.create_request(s, requester, provider.id, post.id, "x", 15, D("100")), 403)
    # the other side of the same dispute: the driver is not blocked by it
    assert mine["asDriver"]["blocked"] is False


async def test_a_requester_who_leaves_accepted_requests_unpaid_three_times_is_blocked(sessions):
    await drain(sessions)
    requester, provider, post = await setup_pair(sessions)
    for _ in range(LIMIT - 1):
        await unpaid_lapse(sessions, requester, provider, post)
    async with sessions() as s:
        assert (await call_payment_standing.standing(s, requester.id))["blocked"] is False
    await unpaid_lapse(sessions, requester, provider, post)
    async with sessions() as s:
        mine = await call_payment_standing.standing(s, requester.id)
        assert mine["asRequester"]["unpaidLapses"] == LIMIT and mine["blocked"] is True
        await expect_error("CALL_PAYMENTS_BLOCKED", svc.create_request(s, requester, provider.id, post.id, "x", 15, D("100")), 403)


async def test_mixed_strikes_do_not_add_up_across_the_two_kinds(sessions):
    await drain(sessions)
    requester, provider, post = await setup_pair(sessions)
    await denied_claim(sessions, requester, provider, post)
    await denied_claim(sessions, requester, provider, post)
    await unpaid_lapse(sessions, requester, provider, post)
    await unpaid_lapse(sessions, requester, provider, post)
    async with sessions() as s:
        mine = await call_payment_standing.standing(s, requester.id)
    assert (mine["asRequester"]["deniedClaims"], mine["asRequester"]["unpaidLapses"], mine["blocked"]) == (2, 2, False)


async def test_a_driver_who_took_payment_and_never_called_three_times_cannot_be_requested(sessions):
    await drain(sessions)
    requester, provider, post = await setup_pair(sessions)
    for _ in range(LIMIT):
        await never_called(sessions, requester, provider, post)
    async with sessions() as s:
        theirs = await call_payment_standing.standing(s, provider.id)
    assert theirs["asDriver"]["noShows"] == LIMIT and theirs["asDriver"]["blocked"] is True
    assert theirs["asRequester"]["blocked"] is False  # they can still ask others for calls
    async with sessions() as s:
        await expect_error("PROVIDER_BLOCKED", svc.create_request(s, requester, provider.id, post.id, "x", 15, D("100")), 409)
    other = await make_user(sessions, "Other")
    async with sessions() as s:  # and other requesters are blocked from that driver too
        await expect_error("PROVIDER_BLOCKED", svc.create_request(s, other, provider.id, post.id, "x", 15, D("100")), 409)
    # a requester-side missed ring is not the driver's no-show
    other_driver = await make_user(sessions, "Other driver")
    other_post = await make_post(sessions, other_driver)
    req_ = await request_call(sessions, requester, other_driver, other_post)
    await make_ready(sessions, req_, requester, other_driver)
    await ring(sessions, req_, other_driver)
    await age(sessions, req_, expires_at=datetime.utcnow() - timedelta(minutes=1))
    async with sessions() as s:
        await svc.sweep(s)
    async with sessions() as s:
        assert (await call_payment_standing.standing(s, other_driver.id))["asDriver"]["noShows"] == 0


async def test_a_driver_who_denies_a_lot_is_flagged_for_review_but_not_blocked(sessions):
    await drain(sessions)
    requester, provider, post = await setup_pair(sessions)
    for _ in range(3):
        await denied_claim(sessions, requester, provider, post)
    async with sessions() as s:
        theirs = (await call_payment_standing.standing(s, provider.id))["asDriver"]
    assert (theirs["deniedClaims"], theirs["answeredClaims"], theirs["denialRate"]) == (3, 3, 1.0)
    assert theirs["flagged"] is True and theirs["blocked"] is False

    # confirming plenty of payments (from other requesters: the first one is blocked now) brings the rate
    # down below the flag
    for _ in range(4):
        other = await make_user(sessions, "Payer")
        req_ = await request_call(sessions, other, provider, post)
        await make_ready(sessions, req_, other, provider)
        await connect_call(sessions, req_, seconds=60)
    async with sessions() as s:
        theirs = (await call_payment_standing.standing(s, provider.id))["asDriver"]
    assert (theirs["deniedClaims"], theirs["answeredClaims"], theirs["denialRate"]) == (3, 7, 0.43)
    assert theirs["flagged"] is False


async def test_a_driver_who_never_answers_is_counted_but_not_punished(sessions):
    await drain(sessions)
    requester, provider, post = await setup_pair(sessions)
    req_ = await accept_request(sessions, await request_call(sessions, requester, provider, post), provider)
    await pay(sessions, req_, requester)
    await age(sessions, req_, expires_at=datetime.utcnow() - timedelta(minutes=1))
    async with sessions() as s:
        await svc.sweep(s)
    async with sessions() as s:
        mine = await call_payment_standing.standing(s, requester.id)
        theirs = (await call_payment_standing.standing(s, provider.id))["asDriver"]
    assert mine["asRequester"]["deniedClaims"] == 0  # the driver said nothing: not the requester's strike
    assert theirs["unansweredClaims"] == 1 and theirs["deniedClaims"] == 0 and theirs["blocked"] is False


async def test_an_admin_forgiving_someone_only_clears_what_came_before(sessions, clock):  # noqa: F811
    await drain(sessions)
    requester, provider, post = await setup_pair(sessions)
    for _ in range(LIMIT):
        await denied_claim(sessions, requester, provider, post)
    async with sessions() as s:
        assert (await call_payment_standing.standing(s, requester.id))["blocked"] is True
    async with sessions() as s:
        after = await call_payment_standing.reset_standing(s, requester.id, "admin-1")
    assert after["blocked"] is False and after["asRequester"]["deniedClaims"] == 0 and after["resetAt"]
    clock.advance(60)  # the test clock stands still; real time moved on while the reset happened
    await denied_claim(sessions, requester, provider, post)  # a new one counts from now
    async with sessions() as s:
        assert (await call_payment_standing.standing(s, requester.id))["asRequester"]["deniedClaims"] == 1


async def test_a_verdict_decides_whose_record_a_dispute_counts_against(sessions):
    await drain(sessions)
    requester, provider, post = await setup_pair(sessions)
    one = await denied_claim(sessions, requester, provider, post)
    two = await denied_claim(sessions, requester, provider, post)
    three = await denied_claim(sessions, requester, provider, post)

    async def counts():
        async with sessions() as s:
            r = (await call_payment_standing.standing(s, requester.id))["asRequester"]["deniedClaims"]
            d = (await call_payment_standing.standing(s, provider.id))["asDriver"]["deniedClaims"]
        return r, d

    assert await counts() == (3, 3)
    async with sessions() as s:
        await call_disputes.resolve(s, one.id, "admin-1", "requester_right", "UTR matches the bank statement")
    assert await counts() == (2, 3)  # the driver was wrong: still on the driver's record, off the requester's
    async with sessions() as s:
        await call_disputes.resolve(s, two.id, "admin-1", "driver_right", None)
    assert await counts() == (2, 2)  # the requester lied: on their record, off the driver's
    async with sessions() as s:
        await call_disputes.resolve(s, three.id, "admin-1", "no_fault", None)
    assert await counts() == (1, 1)  # nobody's fault: off both
    async with sessions() as s:  # the last verdict stands
        await call_disputes.resolve(s, one.id, "admin-1", "driver_right", None)
    assert await counts() == (2, 0)


async def test_the_flagged_list_is_worst_first_and_leaves_out_the_cleared(sessions):
    await drain(sessions)
    bad, ok = await make_user(sessions, "Bad"), await make_user(sessions, "Ok")
    driver = await make_user(sessions, "Driver")
    post = await make_post(sessions, driver)
    for _ in range(3):
        await denied_claim(sessions, bad, driver, post)
    one = await denied_claim(sessions, ok, driver, post)
    async with sessions() as s:
        listed = await call_payment_standing.list_flagged(s)
    ids = [item["userId"] for item in listed]
    # blocked first, then the flagged driver (4 denials, all of their answers), then the one with a single strike
    assert ids.index(str(bad.id)) < ids.index(str(driver.id)) < ids.index(str(ok.id))
    assert next(i for i in listed if i["userId"] == str(bad.id))["blocked"] is True
    assert next(i for i in listed if i["userId"] == str(driver.id))["asDriver"]["flagged"] is True

    async with sessions() as s:
        await call_disputes.resolve(s, one.id, "admin-1", "no_fault", None)
    async with sessions() as s:
        assert str(ok.id) not in [i["userId"] for i in await call_payment_standing.list_flagged(s)]


# --- the admin's disputes ----------------------------------------------------------------------------------


async def test_disputes_lists_what_needs_a_person_and_resolving_closes_it(sessions, clock):  # noqa: F811
    await drain(sessions)
    requester, provider, post = await setup_pair(sessions)
    denied = await denied_claim(sessions, requester, provider, post)
    never = await never_called(sessions, requester, provider, post)
    fine = await request_call(sessions, requester, provider, post)
    await make_ready(sessions, fine, requester, provider)
    await connect_call(sessions, fine, seconds=60)  # kept: nothing to look at

    async with sessions() as s:
        rows, total = await call_disputes.list_disputes(s, "open", 50, 0)
    mine = [r.id for r in rows if r.requester_id == requester.id]
    assert set(mine) == {denied.id, never.id} and total >= 2
    assert fine.id not in mine

    async with sessions() as s:
        done = await call_disputes.resolve(s, never.id, "admin-1", "requester_right", "Refunded by the driver on 4 Nov")
    assert done.resolution == "requester_right" and done.resolved_by == "admin-1" and done.resolved_at
    assert done.resolution_note == "Refunded by the driver on 4 Nov"
    assert call_request_to_dict(done, None, None, requester.id)["refundDue"] is False  # settled

    async with sessions() as s:
        open_rows, _ = await call_disputes.list_disputes(s, "open", 50, 0)
        resolved, _ = await call_disputes.list_disputes(s, "resolved", 50, 0)
        everything, _ = await call_disputes.list_disputes(s, "all", 50, 0)
    assert never.id not in [r.id for r in open_rows] and never.id in [r.id for r in resolved]
    assert {never.id, denied.id} <= {r.id for r in everything}


async def test_resolving_checks_the_verdict_and_the_request(sessions):
    requester, provider, post = await setup_pair(sessions)
    denied = await denied_claim(sessions, requester, provider, post)
    async with sessions() as s:
        await expect_error("INVALID_RESOLUTION", call_disputes.resolve(s, denied.id, "a", "whatever", None), 400)
    async with sessions() as s:
        await expect_error("REQUEST_NOT_FOUND", call_disputes.resolve(s, uuid.uuid4(), "a", "no_fault", None), 404)
    fine = await request_call(sessions, requester, provider, post)
    await make_ready(sessions, fine, requester, provider)
    await connect_call(sessions, fine, seconds=60)
    async with sessions() as s:
        await expect_error("NOT_A_DISPUTE", call_disputes.resolve(s, fine.id, "a", "no_fault", None), 409)
    async with sessions() as s:
        await expect_error("INVALID_STATUS", call_disputes.list_disputes(s, "weird"), 400)


# --- over HTTP (admin + the person's own standing) -----------------------------------------------------------


@pytest.fixture
async def api(schema, monkeypatch):  # noqa: F811
    import os

    engine = create_async_engine(_asyncpg_url(os.environ["CALL_TEST_DATABASE_URL"]))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with factory() as session:
            yield session

    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr(svc, "_notify", quiet)
    app_module.app.dependency_overrides[get_session] = override
    app_module.app.dependency_overrides[get_current_admin] = lambda: SimpleNamespace(id=uuid.uuid4(), email="admin@example.com")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://test")
    client.factory = factory
    yield client
    app_module.app.dependency_overrides.clear()
    await client.aclose()
    await engine.dispose()


async def test_the_admin_endpoints_list_resolve_and_forgive(api):
    from tests.test_call_requests_api import auth

    await drain(api.factory)
    requester, provider, post = await setup_pair(api.factory)
    denied = await denied_claim(api.factory, requester, provider, post)
    for _ in range(LIMIT - 1):
        await denied_claim(api.factory, requester, provider, post)

    r = await api.get("/api/admin/call-payments/disputes?status=open&pageSize=100")
    assert r.status_code == 200, r.text
    body = r.json()
    item = next(d for d in body["disputes"] if d["id"] == str(denied.id))
    assert item["status"] == "payment_disputed" and item["settlement"] == "review"
    assert item["settlementReason"] == "provider_denied" and item["closedReason"] == "provider_denied"
    assert item["requester"]["id"] == str(requester.id) and item["provider"]["mobile"] == provider.mobile
    assert item["amount"] == "200.00" and item["paidAmount"] == "200.00" and item["payeeUpiId"] == provider.upi_id
    assert item["providerConfirmed"] is False and item["resolution"] is None
    kinds = [e["type"] for e in item["events"]]
    assert kinds.index("payment_claimed") < kinds.index("provider_denied_payment")

    flagged = (await api.get("/api/admin/call-payments/flagged")).json()["users"]
    mine = next(u for u in flagged if u["userId"] == str(requester.id))
    assert mine["blocked"] is True and mine["asRequester"]["deniedClaims"] == LIMIT

    r = await api.post(f"/api/admin/call-payments/{denied.id}/resolve", json={"resolution": "requester_right", "note": "UTR matched"})
    assert r.status_code == 200
    assert r.json()["dispute"]["resolution"] == "requester_right" and r.json()["dispute"]["resolutionNote"] == "UTR matched"
    assert r.json()["dispute"]["requester"]["name"] == "Requester"
    assert (await api.post(f"/api/admin/call-payments/{denied.id}/resolve", json={"resolution": "x"})).json()["code"] == "INVALID_RESOLUTION"
    assert (await api.post("/api/admin/call-payments/nope/resolve", json={"resolution": "no_fault"})).status_code == 400
    again = (await api.get("/api/admin/call-payments/disputes?status=open&pageSize=100")).json()["disputes"]
    assert str(denied.id) not in [d["id"] for d in again]

    # forgiving clears the rest
    r = await api.post(f"/api/admin/call-payments/{requester.id}/reset")
    assert r.status_code == 200 and r.json()["standing"]["blocked"] is False


async def test_a_person_can_see_their_own_standing_as_requester_and_driver(api):
    from tests.test_call_requests_api import auth

    requester, provider, post = await setup_pair(api.factory)
    await denied_claim(api.factory, requester, provider, post)
    mine = (await api.get("/api/call-requests/payment-standing/me", headers=auth(requester))).json()["standing"]
    assert mine["asRequester"]["deniedClaims"] == 1 and mine["blocked"] is False and mine["strikeLimit"] == LIMIT
    theirs = (await api.get("/api/call-requests/payment-standing/me", headers=auth(provider))).json()["standing"]
    assert theirs["asDriver"]["deniedClaims"] == 1 and theirs["asDriver"]["blocked"] is False
