"""Saving a UPI ID and withdrawing to it (real FastAPI app, scratch database).

    export CALL_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
    .venv/bin/python -m pytest tests/test_wallet_upi.py -q
"""

import uuid

import pytest

from tests.test_call_requests_api import api, auth  # noqa: F401  (fixtures are used by name)
from tests.test_call_flow import clock  # noqa: F401
from tests.test_call_requests import anyio_backend, fund, make_user, needs_db, schema  # noqa: F401

pytestmark = [needs_db, pytest.mark.anyio]


async def wallet(api, user):
    r = await api.get(f"/api/wallet/{user.id}", headers=auth(user))
    assert r.status_code == 200, r.text
    return r.json()["wallet"]


async def withdraw(api, user):
    return await api.post(
        f"/api/wallet/{user.id}/redeem", json={"idempotencyKey": f"k-{uuid.uuid4()}"}, headers=auth(user)
    )


async def save_upi(api, user, upi):
    return await api.put(f"/api/wallet/{user.id}/upi", json={"upiId": upi}, headers=auth(user))


async def test_the_wallet_says_no_upi_yet_and_the_minimum(api):
    user = await make_user(api.factory)
    w = await wallet(api, user)
    assert w["upiId"] is None and w["minRedeemAmount"] == "250"


async def test_saving_and_changing_the_upi_id(api):
    user = await make_user(api.factory)
    r = await save_upi(api, user, "  asha@ybl ")
    assert r.status_code == 200 and r.json()["upiId"] == "asha@ybl"
    assert (await wallet(api, user))["upiId"] == "asha@ybl"
    assert (await save_upi(api, user, "asha.k@okaxis")).status_code == 200
    assert (await wallet(api, user))["upiId"] == "asha.k@okaxis"


@pytest.mark.parametrize("bad", ["", "   ", "asha", "asha@", "@ybl", "a@b", "asha ybl@ok", "x" * 70 + "@ybl"])
async def test_a_badly_shaped_upi_id_is_refused_and_keeps_the_old_one(api, bad):
    user = await make_user(api.factory)
    await save_upi(api, user, "asha@ybl")
    r = await save_upi(api, user, bad)
    assert r.status_code == 400
    assert (await wallet(api, user))["upiId"] == "asha@ybl"


async def test_nobody_can_save_a_upi_id_for_someone_else(api):
    owner = await make_user(api.factory)
    other = await make_user(api.factory)
    r = await api.put(f"/api/wallet/{owner.id}/upi", json={"upiId": "evil@ybl"}, headers=auth(other))
    assert r.status_code in (401, 403)
    assert (await wallet(api, owner))["upiId"] is None


async def test_without_a_upi_id_the_money_cannot_be_withdrawn(api):
    user = await make_user(api.factory)
    await fund(api.factory, user, "1000")
    r = await withdraw(api, user)
    assert r.status_code == 400 and "UPI" in r.json()["detail"]
    w = await wallet(api, user)
    assert w["availableBalance"] == "1000.00" and w["reservedBalance"] == "0.00"


async def test_under_the_minimum_cannot_be_withdrawn_but_exactly_the_minimum_can(api):
    user = await make_user(api.factory)
    await save_upi(api, user, "asha@ybl")
    await fund(api.factory, user, "249.99")
    r = await withdraw(api, user)
    assert r.status_code == 400 and "250" in r.json()["detail"]
    await fund(api.factory, user, "0.01")
    r = await withdraw(api, user)
    assert r.status_code == 200, r.text
    assert r.json()["redeemRequest"]["amount"] == "250.00" and r.json()["redeemRequest"]["upiId"] == "asha@ybl"
    w = await wallet(api, user)
    assert w["availableBalance"] == "0.00" and w["reservedBalance"] == "250.00"


async def test_a_changed_upi_id_applies_to_the_next_withdrawal_only(api):
    user = await make_user(api.factory)
    await save_upi(api, user, "old@ybl")
    await fund(api.factory, user, "300")
    first = (await withdraw(api, user)).json()["redeemRequest"]
    await save_upi(api, user, "new@ybl")
    assert first["upiId"] == "old@ybl"
    await fund(api.factory, user, "300")
    assert (await withdraw(api, user)).json()["redeemRequest"]["upiId"] == "new@ybl"


async def test_withdrawing_still_needs_approved_kyc(api):
    user = await make_user(api.factory, kyc="pending")
    await save_upi(api, user, "asha@ybl")
    await fund(api.factory, user, "1000")
    assert (await withdraw(api, user)).status_code == 403


async def test_a_retry_with_the_same_key_does_not_withdraw_twice(api):
    user = await make_user(api.factory)
    await save_upi(api, user, "asha@ybl")
    await fund(api.factory, user, "600")
    body = {"idempotencyKey": f"k-{uuid.uuid4()}"}
    a = await api.post(f"/api/wallet/{user.id}/redeem", json=body, headers=auth(user))
    b = await api.post(f"/api/wallet/{user.id}/redeem", json=body, headers=auth(user))
    assert a.status_code == b.status_code == 200
    assert a.json()["redeemRequest"]["id"] == b.json()["redeemRequest"]["id"]
    assert (await wallet(api, user))["reservedBalance"] == "600.00"


async def test_an_older_app_sending_the_upi_id_with_the_withdrawal_still_works_and_saves_it(api):
    user = await make_user(api.factory)
    await fund(api.factory, user, "400")
    r = await api.post(
        f"/api/wallet/{user.id}/redeem",
        json={"upiId": "legacy@ybl", "idempotencyKey": f"k-{uuid.uuid4()}"},
        headers=auth(user),
    )
    assert r.status_code == 200 and r.json()["redeemRequest"]["upiId"] == "legacy@ybl"
    assert (await wallet(api, user))["upiId"] == "legacy@ybl"
