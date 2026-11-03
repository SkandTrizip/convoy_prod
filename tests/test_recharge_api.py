"""The recharge HTTP endpoints end to end (real FastAPI app, scratch database, fake Razorpay).

    export CALL_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
    .venv/bin/python -m pytest tests/test_recharge_api.py -q
"""

from decimal import Decimal

import pytest

from services import razorpay_client

from tests.test_call_requests_api import api, auth  # noqa: F401  (fixtures are used by name)
from tests.test_call_flow import clock  # noqa: F401
from tests.test_call_requests import anyio_backend, make_user, needs_db, schema  # noqa: F401
from tests.test_recharge import FakeRazorpay, keys, rz, webhook_body  # noqa: F401

pytestmark = [needs_db, pytest.mark.anyio]


async def start(api, user, amount="500"):
    r = await api.post(f"/api/wallet/{user.id}/recharges", json={"amount": amount}, headers=auth(user))
    assert r.status_code == 201, r.text
    return r.json()["recharge"]


def paid_confirmation(rz, recharge):
    payment_id = rz.pays(recharge["orderId"])
    return {
        "orderId": recharge["orderId"],
        "paymentId": payment_id,
        "signature": razorpay_client.checkout_signature(recharge["orderId"], payment_id),
    }


async def test_config_tells_the_app_the_limits_and_the_fee(api, rz):
    user = await make_user(api.factory)
    r = await api.get("/api/wallet/recharge/config", headers=auth(user))
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and body["minAmount"] == "10.00" and body["maxAmount"] == "10000.00"
    assert body["feePercent"] == "2.36" and body["feeFixed"] == "0.00"
    assert "topsecret" not in r.text and "rzp_test_key" not in r.text  # not even the public key: it comes with an order


async def test_config_says_disabled_without_keys(api, monkeypatch):
    monkeypatch.setattr(razorpay_client, "RAZORPAY_KEY_ID", "")
    user = await make_user(api.factory)
    assert (await api.get("/api/wallet/recharge/config", headers=auth(user))).json()["enabled"] is False


async def test_the_config_needs_a_login(api):
    assert (await api.get("/api/wallet/recharge/config")).status_code in (401, 403)


async def test_creating_an_order_returns_what_the_checkout_needs_and_the_breakdown(api, rz):
    user = await make_user(api.factory)
    r = await start(api, user, "500")
    assert (r["creditAmount"], r["feeAmount"], r["totalAmount"]) == ("500.00", "12.09", "512.09")
    assert r["status"] == "created"
    assert r["checkout"] == {"keyId": "rzp_test_key", "orderId": r["orderId"], "amountPaise": 51209, "currency": "INR"}


async def test_a_bad_amount_is_a_400_with_the_limits(api, rz):
    user = await make_user(api.factory)
    r = await api.post(f"/api/wallet/{user.id}/recharges", json={"amount": "5"}, headers=auth(user))
    assert r.status_code == 400 and r.json()["code"] == "INVALID_AMOUNT"
    assert r.json()["minAmount"] == "10.00" and isinstance(r.json()["detail"], str)
    for bad in ({"amount": "abc"}, {}, {"amount": -3}, {"amount": 0}):
        assert (await api.post(f"/api/wallet/{user.id}/recharges", json=bad, headers=auth(user))).status_code in (400, 422)


async def test_nobody_can_recharge_or_confirm_for_someone_else(api, rz):
    owner = await make_user(api.factory)
    other = await make_user(api.factory)
    r = await api.post(f"/api/wallet/{owner.id}/recharges", json={"amount": "100"}, headers=auth(other))
    assert r.status_code in (401, 403)
    recharge = await start(api, owner, "100")
    body = paid_confirmation(rz, recharge)
    assert (await api.post(f"/api/wallet/{owner.id}/recharges/confirm", json=body, headers=auth(other))).status_code in (401, 403)
    # and even with their own id in the path, the order is not theirs
    r = await api.post(f"/api/wallet/{other.id}/recharges/confirm", json=body, headers=auth(other))
    assert r.status_code == 404 and r.json()["code"] == "RECHARGE_NOT_FOUND"


async def test_paying_and_confirming_credits_the_wallet(api, rz):
    user = await make_user(api.factory)
    recharge = await start(api, user, "500")
    r = await api.post(f"/api/wallet/{user.id}/recharges/confirm", json=paid_confirmation(rz, recharge), headers=auth(user))
    assert r.status_code == 200 and r.json()["recharge"]["status"] == "paid"
    wallet = (await api.get(f"/api/wallet/{user.id}", headers=auth(user))).json()["wallet"]
    assert wallet["availableBalance"] == "500.00" and wallet["spendableBalance"] == "500.00"
    txns = (await api.get(f"/api/wallet/{user.id}/transactions", headers=auth(user))).json()
    entry = next(t for t in txns["transactions"] if t["referenceType"] == "recharge")
    assert entry["type"] == "credit" and entry["amount"] == "500.00"


async def test_a_forged_confirmation_is_a_400_and_credits_nothing(api, rz):
    user = await make_user(api.factory)
    recharge = await start(api, user, "500")
    body = paid_confirmation(rz, recharge)
    body["signature"] = "0" * 64
    r = await api.post(f"/api/wallet/{user.id}/recharges/confirm", json=body, headers=auth(user))
    assert r.status_code == 400 and r.json()["code"] == "INVALID_SIGNATURE"
    wallet = (await api.get(f"/api/wallet/{user.id}", headers=auth(user))).json()["wallet"]
    assert wallet["availableBalance"] == "0.00"


async def test_the_status_can_be_polled(api, rz):
    user = await make_user(api.factory)
    recharge = await start(api, user, "100")
    r = await api.get(f"/api/wallet/{user.id}/recharges/{recharge['orderId']}", headers=auth(user))
    assert r.json()["recharge"]["status"] == "created"
    await api.post(f"/api/wallet/{user.id}/recharges/confirm", json=paid_confirmation(rz, recharge), headers=auth(user))
    r = await api.get(f"/api/wallet/{user.id}/recharges/{recharge['orderId']}", headers=auth(user))
    assert r.json()["recharge"]["status"] == "paid"
    assert (await api.get(f"/api/wallet/{user.id}/recharges/order_nope", headers=auth(user))).status_code == 404


async def test_the_webhook_needs_no_login_only_razorpays_signature(api, rz):
    user = await make_user(api.factory)
    recharge = await start(api, user, "250")
    payment_id = rz.pays(recharge["orderId"])

    class Row:  # webhook_body only needs these two attributes
        razorpay_order_id = recharge["orderId"]
        total_amount = Decimal(recharge["totalAmount"])

    body = webhook_body(Row, payment_id)
    signature = razorpay_client.webhook_signature(body)

    bad = await api.post("/api/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": "nope"})
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_SIGNATURE"
    missing = await api.post("/api/webhooks/razorpay", content=body)
    assert missing.status_code == 400

    ok = await api.post("/api/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": signature})
    assert ok.status_code == 200 and ok.json()["handled"] is True
    again = await api.post("/api/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": signature})
    assert again.status_code == 200  # Razorpay may deliver it again: harmless

    wallet = (await api.get(f"/api/wallet/{user.id}", headers=auth(user))).json()["wallet"]
    assert wallet["availableBalance"] == "250.00"
