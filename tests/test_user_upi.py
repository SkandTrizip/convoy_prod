"""The UPI ID on the profile: what people are paid on after a call.

    export CALL_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
    .venv/bin/python -m pytest tests/test_user_upi.py -q
"""

import pytest

from tests.test_call_requests_api import api, auth  # noqa: F401  (fixtures are used by name)
from tests.test_call_flow import clock  # noqa: F401
from tests.test_call_requests import anyio_backend, make_user, needs_db, schema  # noqa: F401

pytestmark = [needs_db, pytest.mark.anyio]


async def profile(api, user):
    r = await api.get(f"/api/user/profile/{user.id}", headers=auth(user))
    assert r.status_code == 200, r.text
    return r.json()["user"]


async def test_the_profile_shows_no_upi_id_until_one_is_added(api):
    user = await make_user(api.factory, upi=False)
    assert (await profile(api, user))["upiId"] is None


async def test_adding_and_changing_the_upi_id_shows_on_the_profile(api):
    user = await make_user(api.factory, upi=False)
    r = await api.put(f"/api/user/upi/{user.id}", json={"upiId": "  asha@ybl "}, headers=auth(user))
    assert r.status_code == 200 and r.json() == {"success": True, "upiId": "asha@ybl"}
    assert (await profile(api, user))["upiId"] == "asha@ybl"
    await api.put(f"/api/user/upi/{user.id}", json={"upiId": "asha.k@okaxis"}, headers=auth(user))
    assert (await profile(api, user))["upiId"] == "asha.k@okaxis"


@pytest.mark.parametrize("bad", ["", "   ", "asha", "asha@", "@ybl", "a@b", "asha ybl@ok"])
async def test_a_badly_shaped_upi_id_is_refused_and_keeps_the_old_one(api, bad):
    user = await make_user(api.factory, upi=False)
    await api.put(f"/api/user/upi/{user.id}", json={"upiId": "asha@ybl"}, headers=auth(user))
    r = await api.put(f"/api/user/upi/{user.id}", json={"upiId": bad}, headers=auth(user))
    assert r.status_code == 400
    assert (await profile(api, user))["upiId"] == "asha@ybl"


async def test_nobody_can_set_someone_elses_upi_id(api):
    owner, other = await make_user(api.factory, upi=False), await make_user(api.factory, upi=False)
    r = await api.put(f"/api/user/upi/{owner.id}", json={"upiId": "evil@ybl"}, headers=auth(other))
    assert r.status_code in (401, 403)
    assert (await profile(api, owner))["upiId"] is None


async def test_a_driver_who_adds_a_upi_id_can_then_be_asked_for_a_call(api):
    from tests.test_call_requests import make_post

    requester = await make_user(api.factory, "Asha")
    driver = await make_user(api.factory, "Vikram", upi=False)
    post = await make_post(api.factory, driver)
    body = {"providerId": str(driver.id), "truckRouteId": str(post.id), "topic": "Load", "durationMinutes": 10, "amount": "100"}
    assert (await api.post("/api/call-requests", json=body, headers=auth(requester))).json()["code"] == "PROVIDER_UPI_MISSING"
    await api.put(f"/api/user/upi/{driver.id}", json={"upiId": "vikram@ybl"}, headers=auth(driver))
    assert (await api.post("/api/call-requests", json=body, headers=auth(requester))).status_code == 201
