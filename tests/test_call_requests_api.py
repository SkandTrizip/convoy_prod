"""The call-request HTTP API end to end (real FastAPI app, scratch database).

    export CALL_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
    .venv/bin/python -m pytest tests/test_call_requests_api.py -q

Same safety rules as test_call_requests.py: local database only.
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app as app_module
from database import get_session
from db.base import CallBehaviourEvent, CallRequest
from middleware.auth import create_access_token
from services import call_requests as svc
from services import wallet as wallet_service

# The fake clock + fake Agora credentials (autouse), shared with the call-flow tests.
from tests.test_call_flow import clock  # noqa: F401

# Reuse the fixtures/helpers of the service tests.
from tests.test_call_requests import (  # noqa: F401  (fixtures are used by name)
    _asyncpg_url,
    accept_request,
    anyio_backend,
    connect_call,
    make_post,
    make_ready,
    make_user,
    needs_db,
    schema,
)

pytestmark = [needs_db, pytest.mark.anyio]
D = Decimal


@pytest.fixture
async def api(schema, monkeypatch):  # noqa: F811
    engine = create_async_engine(_asyncpg_url(__import__("os").environ["CALL_TEST_DATABASE_URL"]))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with factory() as session:
            yield session

    async def quiet_notify(*args, **kwargs):
        return None

    monkeypatch.setattr(svc, "_notify", quiet_notify)
    monkeypatch.setattr(svc, "_push_incoming_call", quiet_notify)
    monkeypatch.setattr(svc, "_push_call_cancelled", quiet_notify)
    monkeypatch.setattr(svc, "_stop_other_phones_ringing", lambda *a, **k: None)
    monkeypatch.setattr(wallet_service, "DEV_WALLET_CREDIT_ENABLED", True)
    app_module.app.dependency_overrides[get_session] = override
    transport = httpx.ASGITransport(app=app_module.app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    client.factory = factory
    yield client
    app_module.app.dependency_overrides.clear()
    await client.aclose()
    await engine.dispose()


def auth(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.mobile)}"}


async def ready_over_http(api, rid, requester, provider):
    """Accept, pay and confirm through the API: the call may be started."""
    base = f"/api/call-requests/{rid}"
    assert (await api.post(f"{base}/accept", headers=auth(provider))).status_code == 200
    assert (await api.post(f"{base}/payment/paid", json={}, headers=auth(requester))).status_code == 200
    r = await api.post(f"{base}/payment/confirm", json={"received": True}, headers=auth(provider))
    assert r.status_code == 200 and r.json()["request"]["status"] == "ready", r.text


async def test_full_flow_over_http(api):
    requester = await make_user(api.factory, "Asha")
    provider = await make_user(api.factory, "Vikram")
    post = await make_post(api.factory, provider)

    # no token -> 401
    assert (await api.get("/api/call-requests/incoming")).status_code == 401

    body = {
        "providerId": str(provider.id),
        "truckRouteId": str(post.id),
        "topic": "Route status",
        "durationMinutes": 15,
        "amount": "150",
    }
    # no wallet, no balance needed: the request just goes through
    r = await api.post("/api/call-requests", json=body, headers=auth(requester))
    assert r.status_code == 201, r.text
    req = r.json()["request"]
    assert req["status"] == "pending" and req["role"] == "requester"
    assert req["amount"] == "150.00" and req["holdAmount"] == "37.50" and req["minimumAmount"] == "37.50"
    assert req["requester"]["name"] == "Asha" and req["provider"]["name"] == "Vikram"
    assert req["payment"] is None and req["paymentOutcome"] is None

    # nothing is held anywhere
    w = (await api.get(f"/api/wallet/{requester.id}", headers=auth(requester))).json()["wallet"]
    assert (w["availableBalance"], w["reservedBalance"]) == ("0.00", "0.00")

    # the driver's queue
    q = (await api.get("/api/call-requests/incoming", headers=auth(provider))).json()
    assert q["count"] == 1 and q["requests"][0]["id"] == req["id"] and q["requests"][0]["role"] == "provider"
    assert (await api.get("/api/call-requests/incoming", headers=auth(requester))).json()["count"] == 0

    # an outsider can't see it; the requester can't accept their own request
    stranger = await make_user(api.factory, "Stranger")
    assert (await api.get(f"/api/call-requests/{req['id']}", headers=auth(stranger))).status_code == 404
    r = await api.post(f"/api/call-requests/{req['id']}/accept", headers=auth(requester))
    assert r.status_code == 403 and r.json()["code"] == "NOT_PROVIDER"

    r = await api.post(f"/api/call-requests/{req['id']}/accept", headers=auth(provider))
    assert r.status_code == 200 and r.json()["request"]["status"] == "accepted"

    # events from the requester's side
    r = await api.post(
        f"/api/call-requests/{req['id']}/events",
        json={"events": [{"type": "chat_opened"}, {"type": "mic_off", "occurredAt": datetime.utcnow().isoformat()}]},
        headers=auth(requester),
    )
    assert r.json() == {"success": True, "recorded": 2}
    r = await api.post(
        f"/api/call-requests/{req['id']}/events", json={"events": [{"type": "nope"}]}, headers=auth(requester)
    )
    assert r.status_code == 400 and r.json()["code"] == "UNKNOWN_EVENT"

    # accepted: the requester is shown whom to pay and how much, and by when
    r = await api.get(f"/api/call-requests/{req['id']}", headers=auth(requester))
    shown = r.json()["request"]
    assert shown["status"] == "accepted"
    assert shown["payment"]["upiId"] == provider.upi_id and shown["payment"]["payeeName"] == "Vikram"
    assert shown["payment"]["amount"] == "150.00" and shown["payment"]["deadline"] == shown["expiresAt"]
    # the driver never sees the "pay" block; the driver's queue shows it as waiting for payment
    mine = (await api.get("/api/call-requests/incoming", headers=auth(provider))).json()["requests"][0]
    assert mine["status"] == "accepted" and mine["payment"] is None
    assert (await api.get("/api/call-requests/outgoing?activeOnly=true", headers=auth(requester))).json()["requests"][0]["id"] == req["id"]

    # the driver can't start before they have been paid and confirmed
    r = await api.post(f"/api/call-requests/{req['id']}/start", headers=auth(provider))
    assert r.status_code == 409 and r.json()["code"] == "PAYMENT_NOT_CONFIRMED"

    # a part payment is refused, the driver cannot pay for the requester
    r = await api.post(f"/api/call-requests/{req['id']}/payment/paid", json={"amountType": "minimum"}, headers=auth(requester))
    assert r.status_code == 400 and r.json()["code"] == "INVALID_AMOUNT_TYPE"
    r = await api.post(f"/api/call-requests/{req['id']}/payment/paid", json={}, headers=auth(provider))
    assert r.status_code == 403 and r.json()["code"] == "NOT_REQUESTER"

    r = await api.post(
        f"/api/call-requests/{req['id']}/payment/paid",
        json={"amountType": "full", "upiReference": "UPI-TXN-1"},
        headers=auth(requester),
    )
    assert r.status_code == 200, r.text
    claimed = r.json()["request"]
    assert claimed["status"] == "payment_claimed" and claimed["paymentOutcome"] == "claimed"
    assert claimed["paidAmount"] == "150.00" and claimed["paymentClaimedAt"] and claimed["payment"] is None
    # ... and can't cancel any more
    r = await api.post(f"/api/call-requests/{req['id']}/cancel", headers=auth(requester))
    assert r.status_code == 409 and r.json()["code"] == "INVALID_STATE"

    # the driver answers: a no needs the "are you sure"
    r = await api.post(f"/api/call-requests/{req['id']}/payment/confirm", json={"received": True}, headers=auth(requester))
    assert r.status_code == 403 and r.json()["code"] == "NOT_PROVIDER"
    r = await api.post(f"/api/call-requests/{req['id']}/payment/confirm", json={"received": False}, headers=auth(provider))
    assert r.status_code == 409 and r.json()["code"] == "CONFIRM_REQUIRED"
    r = await api.post(f"/api/call-requests/{req['id']}/payment/confirm", json={"received": True}, headers=auth(provider))
    ready = r.json()["request"]
    assert ready["status"] == "ready" and ready["providerConfirmed"] is True and ready["paymentOutcome"] == "paid"

    # now the call can be started and happens (stand in for it)
    r = await api.post(f"/api/call-requests/{req['id']}/start", headers=auth(provider))
    assert r.status_code == 200 and r.json()["request"]["status"] == "ringing"
    await connect_call(api.factory, type("R", (), {"id": uuid.UUID(req["id"])}), seconds=240)
    done = (await api.get(f"/api/call-requests/{req['id']}", headers=auth(requester))).json()["request"]
    assert done["status"] == "completed" and done["thresholdMet"] is True and done["paidAmount"] == "150.00"

    # no wallet money moved for anyone
    for user in (requester, provider):
        w = (await api.get(f"/api/wallet/{user.id}", headers=auth(user))).json()["wallet"]
        assert (w["availableBalance"], w["reservedBalance"]) == ("0.00", "0.00")

    # the finished request no longer blocks a new one for the same pair
    r = await api.post("/api/call-requests", json=body, headers=auth(requester))
    assert r.status_code == 201


async def _request_over_http(api, requester, provider, post, amount="100"):
    r = await api.post(
        "/api/call-requests",
        json={"providerId": str(provider.id), "truckRouteId": str(post.id), "topic": "Load", "durationMinutes": 10, "amount": amount},
        headers=auth(requester),
    )
    assert r.status_code == 201, r.text
    return r.json()["request"]["id"]


async def test_a_denied_payment_closes_the_request_as_a_dispute_over_http(api):
    requester = await make_user(api.factory, "Asha")
    provider = await make_user(api.factory, "Vikram")
    post = await make_post(api.factory, provider)
    rid = await _request_over_http(api, requester, provider, post)
    base = f"/api/call-requests/{rid}"
    await api.post(f"{base}/accept", headers=auth(provider))
    await api.post(f"{base}/payment/paid", json={}, headers=auth(requester))

    r = await api.post(f"{base}/payment/confirm", json={"received": False}, headers=auth(provider))
    assert r.status_code == 409 and r.json()["code"] == "CONFIRM_REQUIRED" and isinstance(r.json()["detail"], str)
    r = await api.post(f"{base}/payment/confirm", json={"received": False, "sure": True}, headers=auth(provider))
    assert r.status_code == 200, r.text
    closed = r.json()["request"]
    assert closed["status"] == "payment_disputed" and closed["paymentOutcome"] == "disputed"
    assert closed["providerConfirmed"] is False and closed["closedReason"] == "provider_denied"

    # the call never starts, the requester sees why, and the pair may ask again
    r = await api.post(f"{base}/start", headers=auth(provider))
    assert r.status_code == 409 and r.json()["code"] == "INVALID_STATE"
    seen = (await api.get(base, headers=auth(requester))).json()["request"]
    assert seen["status"] == "payment_disputed" and seen["closedReason"] == "provider_denied"
    assert (await api.get("/api/call-requests/outgoing?activeOnly=true", headers=auth(requester))).json()["requests"] == []
    assert await _request_over_http(api, requester, provider, post)


async def test_an_accepted_request_cannot_be_withdrawn_by_the_driver(api):
    """The requester may already be paying in their UPI app when the driver would back out, and the server
    only learns of the payment when they say "paid": so after accepting there is no way out for the driver."""
    requester = await make_user(api.factory, "Asha")
    provider = await make_user(api.factory, "Vikram")
    post = await make_post(api.factory, provider)
    rid = await _request_over_http(api, requester, provider, post)
    base = f"/api/call-requests/{rid}"
    await api.post(f"{base}/accept", headers=auth(provider))

    assert (await api.post(f"{base}/withdraw", headers=auth(provider))).status_code in (404, 405)
    assert (await api.post(f"{base}/reject", headers=auth(provider))).status_code == 409  # only while pending
    status = (await api.get(base, headers=auth(provider))).json()["request"]["status"]
    assert status == "accepted"  # still waiting for the requester's payment


async def test_a_driver_without_a_upi_id_gets_a_clear_409(api):
    requester = await make_user(api.factory, "Asha")
    provider = await make_user(api.factory, "Vikram", upi=False)
    post = await make_post(api.factory, provider)
    r = await api.post(
        "/api/call-requests",
        json={"providerId": str(provider.id), "truckRouteId": str(post.id), "topic": "Load", "durationMinutes": 10, "amount": "100"},
        headers=auth(requester),
    )
    assert r.status_code == 409 and r.json()["code"] == "PROVIDER_UPI_MISSING"


async def test_bad_input_and_ids(api):
    requester = await make_user(api.factory)
    r = await api.get("/api/call-requests/not-a-uuid", headers=auth(requester))
    assert r.status_code == 400 and r.json()["code"] == "INVALID_ID"
    r = await api.post(
        "/api/call-requests",
        json={"providerId": str(requester.id), "truckRouteId": str(uuid.uuid4()), "topic": "x", "durationMinutes": 15, "amount": "100"},
        headers=auth(requester),
    )
    assert r.status_code == 400 and r.json()["code"] == "SELF_REQUEST"
    r = await api.post(
        "/api/call-requests",
        json={"providerId": str(uuid.uuid4()), "truckRouteId": str(uuid.uuid4()), "topic": "", "durationMinutes": 15, "amount": "100"},
        headers=auth(requester),
    )
    assert r.status_code == 422  # empty topic caught by request validation


async def test_dev_credit_cannot_touch_someone_elses_wallet_or_exceed_the_cap(api):
    me, other = await make_user(api.factory), await make_user(api.factory)
    r = await api.post(
        f"/api/wallet/{other.id}/dev-credit",
        json={"amount": "10", "idempotencyKey": "a"},
        headers=auth(me),
    )
    assert r.status_code == 403
    r = await api.post(
        f"/api/wallet/{me.id}/dev-credit", json={"amount": "999999", "idempotencyKey": "b"}, headers=auth(me)
    )
    assert r.status_code == 400
    # same idempotency key twice credits once
    key = str(uuid.uuid4())
    for _ in range(2):
        assert (
            await api.post(f"/api/wallet/{me.id}/dev-credit", json={"amount": "100", "idempotencyKey": key}, headers=auth(me))
        ).status_code == 200
    w = (await api.get(f"/api/wallet/{me.id}", headers=auth(me))).json()["wallet"]
    assert w["availableBalance"] == "100.00"


async def test_dev_credit_is_a_404_when_the_flag_is_off(api, monkeypatch):
    monkeypatch.setattr(wallet_service, "DEV_WALLET_CREDIT_ENABLED", False)
    me = await make_user(api.factory)
    r = await api.post(f"/api/wallet/{me.id}/dev-credit", json={"amount": "100", "idempotencyKey": "z"}, headers=auth(me))
    assert r.status_code == 404


async def test_the_call_itself_over_http(api, clock):  # noqa: F811
    requester = await make_user(api.factory, "Asha")
    provider = await make_user(api.factory, "Vikram")
    post = await make_post(api.factory, provider)

    r = await api.post(
        "/api/call-requests",
        json={"providerId": str(provider.id), "truckRouteId": str(post.id), "topic": "Load", "durationMinutes": 10, "amount": "200"},
        headers=auth(requester),
    )
    rid = r.json()["request"]["id"]
    base = f"/api/call-requests/{rid}"

    # can't start before the driver accepted
    r = await api.post(f"{base}/start", headers=auth(provider))
    assert r.status_code == 409 and r.json()["code"] == "INVALID_STATE"
    await ready_over_http(api, rid, requester, provider)

    # the requester can't start the driver's call; the driver can
    assert (await api.post(f"{base}/start", headers=auth(requester))).json()["code"] == "NOT_PROVIDER"
    r = await api.post(f"{base}/start", headers=auth(provider))
    assert r.status_code == 200
    call = r.json()["call"]
    assert r.json()["request"]["status"] == "ringing"
    # the request carries the ring deadline too, so the requester's ring screen can count down to it
    assert r.json()["request"]["ringEndsAt"] == call["ringEndsAt"]
    r2 = await api.get(base, headers=auth(requester))
    assert r2.json()["request"]["ringEndsAt"] == call["ringEndsAt"]
    assert call["role"] == "provider" and call["audioOnly"] is False
    assert {"appId", "channelName", "uid", "rtcToken", "tokenExpiresAt", "durationSeconds", "chatAvailable", "rtmUserId", "ringEndsAt"} <= set(call)
    assert "b" * 32 not in str(r.json())  # the app certificate never leaves the server

    # the requester answers: audio-only credentials on the same channel
    r = await api.post(f"{base}/join", headers=auth(requester))
    assert r.status_code == 200 and r.json()["request"]["status"] == "live"
    assert r.json()["request"]["ringEndsAt"] is None  # only shown while it is ringing
    assert r.json()["call"]["audioOnly"] is True and r.json()["call"]["channelName"] == call["channelName"]

    # 40 s of heartbeats from both apps (the apps also say whether mic / camera / video are on)
    state = None
    media = {"peerPresent": True, "micOn": True, "cameraOn": True}
    for _ in range(8):
        clock.advance(5)
        state = (await api.post(f"{base}/heartbeat", json={**media, "cameraOn": False, "peerVideoOn": True}, headers=auth(requester))).json()
        await api.post(f"{base}/heartbeat", json=media, headers=auth(provider))
    assert state["status"] == "live" and state["connectedSeconds"] >= 30 and state["remainingSeconds"] > 0
    assert (await api.post(f"{base}/heartbeat", json={}, headers=auth(provider))).status_code == 422  # peerPresent is required

    # hang up -> the call is completed (it was paid in advance)
    r = await api.post(f"{base}/end", headers=auth(provider))
    assert r.json()["request"]["status"] == "completed"
    assert (await api.post(f"{base}/end", headers=auth(requester))).json()["request"]["status"] == "completed"
    async with api.factory() as s:
        row = await s.get(CallRequest, uuid.UUID(rid))
    assert row.provider_media_on is True and row.threshold_met is True and row.requester_saw_video_beats >= 7


async def test_a_missed_ring_is_a_409_the_app_can_act_on(api, clock):  # noqa: F811
    requester = await make_user(api.factory)
    provider = await make_user(api.factory)
    post = await make_post(api.factory, provider)
    rid = (
        await api.post(
            "/api/call-requests",
            json={"providerId": str(provider.id), "truckRouteId": str(post.id), "topic": "x", "durationMinutes": 10, "amount": "100"},
            headers=auth(requester),
        )
    ).json()["request"]["id"]
    await ready_over_http(api, rid, requester, provider)
    await api.post(f"/api/call-requests/{rid}/start", headers=auth(provider))
    clock.advance(46)
    r = await api.post(f"/api/call-requests/{rid}/join", headers=auth(requester))
    assert r.status_code == 409 and r.json()["code"] == "RING_OVER"


async def test_events_with_the_timestamps_the_apps_really_send_are_stored(api, clock):  # noqa: F811
    """The apps send UTC times as "...Z" (timezone-aware); the column holds naive UTC. This used to be a 500."""
    requester = await make_user(api.factory, "Asha")
    provider = await make_user(api.factory, "Vikram")
    post = await make_post(api.factory, provider)
    r = await api.post(
        "/api/call-requests",
        json={"providerId": str(provider.id), "truckRouteId": str(post.id), "topic": "Load", "durationMinutes": 10, "amount": "100"},
        headers=auth(requester),
    )
    rid = r.json()["request"]["id"]
    now = datetime.utcnow()
    batches = [
        [{"type": "call_screen_opened", "occurredAt": now.isoformat() + "Z"}],
        [{"type": "mic_off", "occurredAt": now.isoformat(timespec="milliseconds") + "Z"}],
        [{"type": "mic_on", "occurredAt": (now).isoformat() + "+05:30"}],  # an offset, not Z
        [{"type": "call_joined", "occurredAt": "2026-11-02T06:14:01.123456+00:00"}],
        [{"type": "chat_opened"}],  # no time at all
    ]
    for batch in batches:
        resp = await api.post(f"/api/call-requests/{rid}/events", json={"events": batch}, headers=auth(requester))
        assert resp.status_code == 200, (batch, resp.text)

    async with api.factory() as s:
        rows = (await s.execute(select(CallBehaviourEvent).where(CallBehaviourEvent.call_request_id == uuid.UUID(rid), CallBehaviourEvent.source == "client"))).scalars().all()
    assert len(rows) == 5
    assert all(row.occurred_at.tzinfo is None for row in rows)  # stored as naive UTC, like everything else
    # an offset is converted to UTC, not just dropped: 12:00+05:30 is 06:30 UTC
    joined = next(row for row in rows if row.event_type == "call_joined")
    assert joined.occurred_at == datetime(2026, 11, 2, 6, 14, 1, 123456)
    offset = next(row for row in rows if row.event_type == "mic_on")
    assert abs((offset.occurred_at - (now - timedelta(hours=5, minutes=30))).total_seconds()) < 2


async def test_a_wildly_wrong_phone_clock_cannot_put_events_in_the_future(api, clock):  # noqa: F811
    requester = await make_user(api.factory, "Asha")
    provider = await make_user(api.factory, "Vikram")
    post = await make_post(api.factory, provider)
    r = await api.post(
        "/api/call-requests",
        json={"providerId": str(provider.id), "truckRouteId": str(post.id), "topic": "Load", "durationMinutes": 10, "amount": "100"},
        headers=auth(requester),
    )
    rid = r.json()["request"]["id"]
    resp = await api.post(f"/api/call-requests/{rid}/events", json={"events": [{"type": "mic_on", "occurredAt": "2099-01-01T00:00:00Z"}]}, headers=auth(requester))
    assert resp.status_code == 200
    async with api.factory() as s:
        row = (await s.execute(select(CallBehaviourEvent).where(CallBehaviourEvent.call_request_id == uuid.UUID(rid), CallBehaviourEvent.source == "client"))).scalar_one()
    assert row.occurred_at <= datetime.utcnow() + timedelta(minutes=1)



async def test_the_device_header_keeps_a_live_call_on_one_phone(api):
    requester = await make_user(api.factory, "Asha")
    provider = await make_user(api.factory, "Vikram")
    post = await make_post(api.factory, provider)
    from tests.test_call_requests import request_call

    req = await request_call(api.factory, requester, provider, post)
    phone = lambda user, device: {**auth(user), "X-Device-Id": device}  # noqa: E731
    await make_ready(api.factory, req, requester, provider)
    r = await api.post(f"/api/call-requests/{req.id}/start", headers=phone(provider, "driver-phone"))
    assert r.status_code == 200 and r.json()["request"]["onAnotherDevice"] is False
    assert (await api.post(f"/api/call-requests/{req.id}/join", headers=phone(requester, "phone-a"))).status_code == 200

    # the requester's second phone is told the call is elsewhere, and cannot join, hang up or beat for it
    seen = await api.get(f"/api/call-requests/{req.id}", headers=phone(requester, "phone-b"))
    assert seen.json()["request"]["onAnotherDevice"] is True
    assert (await api.get(f"/api/call-requests/{req.id}", headers=phone(requester, "phone-a"))).json()["request"][
        "onAnotherDevice"
    ] is False
    for path, body in (("join", None), ("end", None), ("heartbeat", {"peerPresent": True})):
        r = await api.post(f"/api/call-requests/{req.id}/{path}", json=body, headers=phone(requester, "phone-b"))
        assert r.status_code == 409 and r.json()["code"] == "CALL_ON_ANOTHER_DEVICE", path
    # while the phone that holds it carries on
    r = await api.post(f"/api/call-requests/{req.id}/heartbeat", json={"peerPresent": True}, headers=phone(requester, "phone-a"))
    assert r.status_code == 200 and r.json()["status"] == "live"


async def _paid_call(api, requester, provider, post, amount="200", *, call=True):
    """A request that was accepted, paid and confirmed (and, by default, whose call then happened)."""
    from tests.test_call_requests import request_call

    req = await request_call(api.factory, requester, provider, post, amount=amount)
    await make_ready(api.factory, req, requester, provider, reference="REF-" + amount)
    if call:
        await connect_call(api.factory, req, seconds=120)
    return req


async def test_the_payments_ledger_lists_money_received_and_sent(api, clock):  # noqa: F811
    requester = await make_user(api.factory, "Asha")
    vikram = await make_user(api.factory, "Vikram")
    ravi = await make_user(api.factory, "Ravi")
    first = await _paid_call(api, requester, vikram, await make_post(api.factory, vikram), "200")
    clock.advance(300)
    second = await _paid_call(api, requester, vikram, await make_post(api.factory, vikram), "100")
    clock.advance(300)
    third = await _paid_call(api, requester, ravi, await make_post(api.factory, ravi), "500")

    # the driver's side: newest first, each line names who paid and what was paid
    r = await api.get("/api/call-requests/payments?direction=received", headers=auth(vikram))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [p["id"] for p in body["payments"]] == [str(second.id), str(first.id)]
    assert (body["count"], body["total"], body["hasMore"]) == (2, "300.00", False)
    line = body["payments"][1]
    assert line["direction"] == "received" and line["counterparty"]["name"] == "Asha"
    assert (line["amount"], line["offerAmount"], line["amountType"]) == ("200.00", "200.00", "full")
    assert line["paidAt"] and line["callEndedAt"] and line["upiReference"] == "REF-200"
    assert line["connectedSeconds"] == 120 and line["topic"] == "Route status"
    assert line["providerConfirmed"] is True  # the driver confirmed it before the call

    # the requester's side: everything they paid, to whom; nothing about the driver's answer
    r = await api.get("/api/call-requests/payments?direction=sent", headers=auth(requester))
    body = r.json()
    assert [p["id"] for p in body["payments"]] == [str(third.id), str(second.id), str(first.id)]
    assert (body["count"], body["total"]) == (3, "800.00")
    assert [p["counterparty"]["name"] for p in body["payments"]] == ["Ravi", "Vikram", "Vikram"]
    assert all(p["direction"] == "sent" and p["providerConfirmed"] is None for p in body["payments"])

    # a person only ever sees their own side
    assert (await api.get("/api/call-requests/payments?direction=sent", headers=auth(vikram))).json()["payments"] == []
    assert (await api.get("/api/call-requests/payments?direction=received", headers=auth(requester))).json()["payments"] == []


async def test_the_ledger_lists_only_payments_the_driver_confirmed(api, clock):  # noqa: F811
    requester = await make_user(api.factory, "Asha")
    vikram = await make_user(api.factory, "Vikram")
    from tests.test_call_requests import confirm, pay, request_call

    # waiting for the requester to pay, a claim waiting for the driver, and a denied claim: none is listed
    waiting = await accept_request(api.factory, await request_call(api.factory, requester, vikram, await make_post(api.factory, vikram), amount="100"), vikram)
    other_requester = await make_user(api.factory, "Ravi")
    claimed = await accept_request(api.factory, await request_call(api.factory, other_requester, vikram, await make_post(api.factory, vikram), amount="150"), vikram)
    await pay(api.factory, claimed, other_requester)
    third_requester = await make_user(api.factory, "Meena")
    denied = await accept_request(api.factory, await request_call(api.factory, third_requester, vikram, await make_post(api.factory, vikram), amount="250"), vikram)
    await pay(api.factory, denied, third_requester)
    await confirm(api.factory, denied, vikram, False, sure=True)
    assert waiting and claimed and denied

    for who, direction in ((vikram, "received"), (requester, "sent"), (other_requester, "sent"), (third_requester, "sent")):
        body = (await api.get(f"/api/call-requests/payments?direction={direction}", headers=auth(who))).json()
        assert (body["payments"], body["count"], body["total"]) == ([], 0, "0.00")

    # once the driver confirms, it is listed, even if the call never happened (the money was paid)
    await confirm(api.factory, claimed, vikram, True)
    body = (await api.get("/api/call-requests/payments?direction=received", headers=auth(vikram))).json()
    assert [p["id"] for p in body["payments"]] == [str(claimed.id)] and body["total"] == "150.00"
    body = (await api.get("/api/call-requests/payments?direction=sent", headers=auth(other_requester))).json()
    assert body["total"] == "150.00"


async def test_the_ledger_pages(api, clock):  # noqa: F811
    requester = await make_user(api.factory, "Asha")
    vikram = await make_user(api.factory, "Vikram")
    for i in range(3):
        await _paid_call(api, requester, vikram, await make_post(api.factory, vikram), str(100 + i))
        clock.advance(60)

    first = (await api.get("/api/call-requests/payments?direction=received&limit=2", headers=auth(vikram))).json()
    assert [p["amount"] for p in first["payments"]] == ["102.00", "101.00"]
    assert (first["hasMore"], first["count"], first["total"]) == (True, 3, "303.00")
    rest = (await api.get("/api/call-requests/payments?direction=received&limit=2&offset=2", headers=auth(vikram))).json()
    assert [p["amount"] for p in rest["payments"]] == ["100.00"] and rest["hasMore"] is False


async def test_the_ledger_needs_a_login_and_a_known_direction(api):
    user = await make_user(api.factory, "Asha")
    assert (await api.get("/api/call-requests/payments?direction=sent")).status_code == 401
    r = await api.get("/api/call-requests/payments?direction=both", headers=auth(user))
    assert r.status_code == 400 and r.json()["code"] == "INVALID_DIRECTION"
    assert (await api.get("/api/call-requests/payments", headers=auth(user))).status_code == 422
    assert (await api.get("/api/call-requests/payments?direction=sent&limit=0", headers=auth(user))).status_code == 422
