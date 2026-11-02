"""Stage 6: the pushes that ring a phone (Apple VoIP, data-only for Android, normal notification).

The Apple client is tested against a fake HTTP server (no network). The routing and device
rules need the local scratch database, like test_call_requests.py:

    export CALL_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
    .venv/bin/python -m pytest tests/test_call_push.py -q
"""

import time
import uuid
from datetime import datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from db.base import UserDevice
from notifications.repositories import device_repository
from notifications.sender import firebase_sender
from services import apns_client, call_push

from tests.test_call_requests import (  # noqa: F401  (fixtures are used by name)
    anyio_backend,
    make_user,
    needs_db,
    schema,
    sessions,
    setup_pair,
    request_call,
)

# --- a throw-away Apple key so real signing runs ---------------------------------------------

_KEY = ec.generate_private_key(ec.SECP256R1())
_PRIVATE_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
_PUBLIC = _KEY.public_key()


@pytest.fixture
def apns(monkeypatch):
    """Apple VoIP push configured with the throw-away key, talking to a fake server."""
    monkeypatch.setattr(apns_client, "APNS_KEY_ID", "KEY123")
    monkeypatch.setattr(apns_client, "APNS_TEAM_ID", "TEAM123")
    monkeypatch.setattr(apns_client, "APNS_KEY_P8", _PRIVATE_PEM)
    monkeypatch.setattr(apns_client, "APNS_KEY_PATH", "")
    monkeypatch.setattr(apns_client, "APNS_BUNDLE_ID", "com.example.app")
    seen: list[httpx.Request] = []
    replies: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.pop(0) if replies else httpx.Response(200)

    apns_client.provider.reset(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    yield seen, replies
    apns_client.provider.reset()


def _reply(status, reason=None):
    return httpx.Response(status, json={"reason": reason} if reason else None)


# --- the Apple client ------------------------------------------------------------------------


@pytest.mark.anyio
async def test_not_configured_means_no_network_at_all(monkeypatch):
    monkeypatch.setattr(apns_client, "APNS_KEY_ID", "")
    result = await apns_client.send_voip("tok", {"aps": {}}, sandbox=False)
    assert result.outcome == apns_client.ApnsOutcome.NOT_CONFIGURED


@pytest.mark.anyio
async def test_a_voip_push_is_signed_and_addressed_the_way_apple_requires(apns):
    seen, _ = apns
    before = int(time.time())
    result = await apns_client.send_voip("abc123", {"aps": {"alert": "x"}, "requestId": "r"}, sandbox=False, ttl_seconds=45)

    assert result.ok
    request = seen[0]
    assert request.url == "https://api.push.apple.com/3/device/abc123"
    assert request.headers["apns-topic"] == "com.example.app.voip"
    assert request.headers["apns-push-type"] == "voip"
    assert request.headers["apns-priority"] == "10"
    assert before + 44 <= int(request.headers["apns-expiration"]) <= before + 47  # a ring that is over must not ring late
    token = request.headers["authorization"].removeprefix("bearer ")
    claims = jwt.decode(token, _PUBLIC, algorithms=["ES256"], options={"verify_iat": False})
    assert claims["iss"] == "TEAM123"
    assert jwt.get_unverified_header(token)["kid"] == "KEY123"
    assert b'"requestId"' in request.content


@pytest.mark.anyio
async def test_debug_builds_use_apples_sandbox_server(apns):
    seen, _ = apns
    await apns_client.send_voip("abc", {"aps": {}}, sandbox=True)
    assert seen[0].url.host == "api.sandbox.push.apple.com"


@pytest.mark.anyio
async def test_the_signed_token_is_reused_not_remade_for_every_push(apns):
    seen, _ = apns
    await apns_client.send_voip("a", {"aps": {}}, sandbox=False)
    await apns_client.send_voip("b", {"aps": {}}, sandbox=False)
    assert seen[0].headers["authorization"] == seen[1].headers["authorization"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,reason",
    [(410, "Unregistered"), (400, "BadDeviceToken"), (400, "DeviceTokenNotForTopic")],
)
async def test_a_dead_device_token_is_reported_so_it_can_be_dropped(apns, status, reason):
    _, replies = apns
    replies.append(_reply(status, reason))
    result = await apns_client.send_voip("dead", {"aps": {}}, sandbox=False)
    assert result.outcome == apns_client.ApnsOutcome.INVALID_TOKEN


@pytest.mark.anyio
async def test_an_expired_provider_token_is_renewed_and_the_push_retried_once(apns, monkeypatch):
    seen, replies = apns
    replies.extend([_reply(403, "ExpiredProviderToken"), httpx.Response(200)])
    ticks = iter([1000.0, 2000.0])
    monkeypatch.setattr(apns_client.time, "time", lambda: next(ticks, 2000.0))
    result = await apns_client.send_voip("tok", {"aps": {}}, sandbox=False)
    assert result.ok and len(seen) == 2
    assert seen[0].headers["authorization"] != seen[1].headers["authorization"]


@pytest.mark.anyio
async def test_a_provider_token_refused_twice_is_a_failure_not_a_loop(apns):
    seen, replies = apns
    replies.extend([_reply(403, "InvalidProviderToken")] * 5)
    result = await apns_client.send_voip("tok", {"aps": {}}, sandbox=False)
    assert result.outcome == apns_client.ApnsOutcome.FAILED
    assert len(seen) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("status,reason", [(429, "TooManyRequests"), (500, "InternalServerError"), (503, None)])
async def test_passing_apple_problems_fall_back_rather_than_drop_the_token(apns, status, reason):
    _, replies = apns
    replies.append(_reply(status, reason))
    result = await apns_client.send_voip("tok", {"aps": {}}, sandbox=False)
    assert result.outcome == apns_client.ApnsOutcome.FAILED


@pytest.mark.anyio
async def test_a_network_error_is_a_failure_never_an_exception(apns):
    def boom(request):
        raise httpx.ConnectError("no route")

    apns_client.provider.reset(httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    result = await apns_client.send_voip("tok", {"aps": {}}, sandbox=False)
    assert result.outcome == apns_client.ApnsOutcome.FAILED


# --- the data-only FCM message ----------------------------------------------------------------


def test_a_data_only_message_has_no_visible_notification_and_wakes_the_phone():
    msg = firebase_sender._to_fcm_message(
        firebase_sender.PreparedMessage("", "d", "tok", "", "", {"type": "call_incoming", "n": 5}),
        data_only=True, high_priority=True, ttl_seconds=45,
    )
    assert msg.notification is None
    assert msg.data == {"type": "call_incoming", "n": "5"}
    assert msg.android.priority == "high" and msg.android.ttl == timedelta(seconds=45)
    assert msg.apns.payload.aps.content_available is True


def test_an_ordinary_message_is_unchanged():
    msg = firebase_sender._to_fcm_message(firebase_sender.PreparedMessage("", "d", "tok", "T", "B", {"a": 1}))
    assert (msg.notification.title, msg.notification.body) == ("T", "B")
    assert msg.android is None and msg.apns is None


# --- who gets which push -----------------------------------------------------------------------


@pytest.fixture
def sent(monkeypatch):
    """Records every push instead of sending it."""
    log = {"voip": [], "fcm": []}
    state = {"voip_result": apns_client.ApnsResult(apns_client.ApnsOutcome.SENT)}

    async def fake_voip(token, payload, **kw):
        log["voip"].append((token, payload, kw))
        return state["voip_result"]

    def fake_fcm(token, title, body, data=None, device_id="", **kw):
        log["fcm"].append({"token": token, "title": title, "data": data, "device_id": device_id, **kw})
        return firebase_sender.SendResult("", device_id, success=True)

    monkeypatch.setattr(apns_client, "send_voip", fake_voip)
    monkeypatch.setattr(apns_client, "is_configured", lambda: True)
    monkeypatch.setattr(firebase_sender, "send_single", fake_fcm)
    log["state"] = state
    return log


async def add_device(factory, user, platform, *, voip=None, sandbox=False):
    async with factory() as s:
        await device_repository.upsert_device(
            s, user.id, f"dev-{uuid.uuid4()}", platform, f"fcm-{uuid.uuid4()}",
            voip_token=voip, voip_sandbox=sandbox,
        )
        await s.commit()


async def ringing_request(factory):
    requester, provider, post = await setup_pair(factory)
    req = await request_call(factory, requester, provider, post)
    return requester, req


@needs_db
@pytest.mark.anyio
async def test_an_iphone_with_a_voip_token_gets_only_the_voip_push(sessions, sent):
    requester, req = await ringing_request(sessions)
    await add_device(sessions, requester, "ios", voip="vtok", sandbox=True)
    async with sessions() as s:
        summary = await call_push.notify_incoming_call(s, req, "Vikram", datetime.utcnow() + timedelta(seconds=45))

    assert (summary.voip, summary.data_only, summary.notification) == (1, 0, 0)
    token, payload, kw = sent["voip"][0]
    assert token == "vtok" and kw["sandbox"] is True
    assert payload["type"] == "call_incoming" and payload["requestId"] == str(req.id)
    assert payload["callerName"] == "Vikram" and payload["ringEndsAt"].endswith("+00:00")
    assert "." not in payload["ringEndsAt"]  # no fractional seconds (the phone's date parser)
    assert "amount" not in payload and "holdAmount" not in payload  # no money details on the lock screen
    assert sent["fcm"] == []  # no second, duplicate banner


@needs_db
@pytest.mark.anyio
async def test_an_android_phone_gets_a_data_only_high_priority_push(sessions, sent):
    requester, req = await ringing_request(sessions)
    await add_device(sessions, requester, "android")
    async with sessions() as s:
        summary = await call_push.notify_incoming_call(s, req, "Vikram", datetime.utcnow() + timedelta(seconds=45))

    assert (summary.voip, summary.data_only, summary.notification) == (0, 1, 0)
    msg = sent["fcm"][0]
    assert msg["data_only"] is True and msg["high_priority"] is True and msg["ttl_seconds"] > 0
    assert msg["data"]["type"] == "call_incoming" and msg["data"]["callerName"] == "Vikram"


@needs_db
@pytest.mark.anyio
async def test_an_iphone_without_a_voip_token_gets_the_normal_notification(sessions, sent):
    requester, req = await ringing_request(sessions)
    await add_device(sessions, requester, "ios")
    async with sessions() as s:
        summary = await call_push.notify_incoming_call(s, req, "Vikram", datetime.utcnow() + timedelta(seconds=45))
    assert (summary.voip, summary.data_only, summary.notification) == (0, 0, 1)
    assert sent["fcm"][0]["title"] == "Incoming 1:1 call"
    assert sent["voip"] == []


@needs_db
@pytest.mark.anyio
async def test_when_apple_voip_is_not_configured_the_iphone_still_gets_a_notification(sessions, sent, monkeypatch):
    monkeypatch.setattr(apns_client, "is_configured", lambda: False)
    requester, req = await ringing_request(sessions)
    await add_device(sessions, requester, "ios", voip="vtok")
    async with sessions() as s:
        summary = await call_push.notify_incoming_call(s, req, "Vikram", datetime.utcnow() + timedelta(seconds=45))
    assert (summary.voip, summary.notification) == (0, 1)


@needs_db
@pytest.mark.anyio
async def test_a_failed_voip_push_falls_back_to_the_notification_and_keeps_the_token(sessions, sent):
    sent["state"]["voip_result"] = apns_client.ApnsResult(apns_client.ApnsOutcome.FAILED, "503")
    requester, req = await ringing_request(sessions)
    await add_device(sessions, requester, "ios", voip="vtok")
    async with sessions() as s:
        summary = await call_push.notify_incoming_call(s, req, "Vikram", datetime.utcnow() + timedelta(seconds=45))
    assert (summary.voip, summary.notification) == (0, 1)
    async with sessions() as s:
        assert (await s.execute(select(UserDevice.voip_token).where(UserDevice.user_id == requester.id))).scalar_one() == "vtok"


@needs_db
@pytest.mark.anyio
async def test_a_dead_voip_token_is_forgotten_and_the_notification_still_goes(sessions, sent):
    sent["state"]["voip_result"] = apns_client.ApnsResult(apns_client.ApnsOutcome.INVALID_TOKEN, "Unregistered")
    requester, req = await ringing_request(sessions)
    await add_device(sessions, requester, "ios", voip="vtok")
    async with sessions() as s:
        summary = await call_push.notify_incoming_call(s, req, "Vikram", datetime.utcnow() + timedelta(seconds=45))
    assert summary.notification == 1
    async with sessions() as s:
        assert (await s.execute(select(UserDevice.voip_token).where(UserDevice.user_id == requester.id))).scalar_one() is None


@needs_db
@pytest.mark.anyio
async def test_every_device_is_rung_and_one_failing_never_stops_the_others(sessions, sent, monkeypatch):
    requester, req = await ringing_request(sessions)
    await add_device(sessions, requester, "ios", voip="vtok")
    await add_device(sessions, requester, "android")
    calls = {"n": 0}
    real = apns_client.send_voip

    async def exploding_voip(*a, **k):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(apns_client, "send_voip", exploding_voip)
    async with sessions() as s:
        summary = await call_push.notify_incoming_call(s, req, "Vikram", datetime.utcnow() + timedelta(seconds=45))
    assert calls["n"] == 1
    assert summary.data_only == 1  # the Android phone was still rung


@needs_db
@pytest.mark.anyio
async def test_a_logged_out_device_is_not_rung(sessions, sent):
    requester, req = await ringing_request(sessions)
    await add_device(sessions, requester, "android")
    async with sessions() as s:
        await s.execute(UserDevice.__table__.update().values(is_active=False).where(UserDevice.user_id == requester.id))
        await s.commit()
        summary = await call_push.notify_incoming_call(s, req, "Vikram", datetime.utcnow() + timedelta(seconds=45))
    assert (summary.voip, summary.data_only, summary.notification) == (0, 0, 0)


@needs_db
@pytest.mark.anyio
async def test_cancelling_sends_a_silent_data_push_to_every_device(sessions, sent):
    requester, req = await ringing_request(sessions)
    await add_device(sessions, requester, "ios", voip="vtok")
    await add_device(sessions, requester, "android")
    async with sessions() as s:
        count = await call_push.notify_call_cancelled(s, req)
    assert count == 2
    assert all(m["data_only"] and m["data"] == {"type": "call_cancelled", "requestId": str(req.id)} for m in sent["fcm"])
    assert sent["voip"] == []  # a VoIP push must always show a call, so never used to cancel


# --- device registration -----------------------------------------------------------------------


@needs_db
@pytest.mark.anyio
async def test_the_voip_token_is_stored_left_alone_or_cleared_as_asked(sessions):
    user = await make_user(sessions)
    async with sessions() as s:
        await device_repository.upsert_device(s, user.id, "d1", "ios", "f1", voip_token="v1", voip_sandbox=True)
        await s.commit()
    async with sessions() as s:
        await device_repository.upsert_device(s, user.id, "d1", "ios", "f1")  # a plain sync: unchanged
        await s.commit()
        d = (await s.execute(select(UserDevice).where(UserDevice.device_id == "d1"))).scalar_one()
        assert (d.voip_token, d.voip_sandbox) == ("v1", True)
    async with sessions() as s:
        await device_repository.upsert_device(s, user.id, "d1", "ios", "f1", voip_token="")  # cleared
        await s.commit()
        d = (await s.execute(select(UserDevice).where(UserDevice.device_id == "d1"))).scalar_one()
        assert d.voip_token is None


@needs_db
@pytest.mark.anyio
async def test_a_voip_token_that_moves_to_another_device_leaves_the_old_row(sessions):
    user = await make_user(sessions)
    async with sessions() as s:
        await device_repository.upsert_device(s, user.id, "old", "ios", "f-old", voip_token="vX")
        await device_repository.upsert_device(s, user.id, "new", "ios", "f-new", voip_token="vX")
        await s.commit()
        rows = {d.device_id: d.voip_token for d in (await s.execute(select(UserDevice).where(UserDevice.user_id == user.id))).scalars()}
    assert rows == {"old": None, "new": "vX"}
