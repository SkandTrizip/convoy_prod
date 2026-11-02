"""Stage 7: adding money to a wallet through Razorpay (fake Razorpay, scratch database).

    export CALL_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
    .venv/bin/python -m pytest tests/test_recharge.py -q

The fee maths and the signature checks need no database.
"""

import asyncio
import json
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

import config
from db.base import Wallet, WalletRecharge, WalletTransaction
from services import razorpay_client, recharge
from services.recharge import RechargeError, compute_fee, paise

from tests.test_call_requests import (  # noqa: F401  (fixtures are used by name)
    anyio_backend,
    expect_error,
    make_user,
    needs_db,
    schema,
    sessions,
)

D = Decimal


# --- the fee ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "credit,fee,total",
    [("100", "2.42", "102.42"), ("500", "12.09", "512.09"), ("10", "0.25", "10.25"), ("10000", "241.71", "10241.71")],
)
def test_the_fee_is_added_on_top_and_grossed_up(credit, fee, total):
    got_fee, got_total = compute_fee(D(credit))
    assert (str(got_fee), str(got_total)) == (fee, total)


@pytest.mark.parametrize("credit", ["10", "33.33", "99", "250", "777.77", "9999.99"])
def test_after_the_gateways_cut_the_wallet_amount_is_whole(credit):
    """Razorpay takes 2.36% of the whole payment; what is left must still be the amount asked for."""
    fee, total = compute_fee(D(credit))
    after_cut = total - total * config.RECHARGE_FEE_PERCENT / 100
    assert after_cut >= D(credit) - D("0.005")  # within rounding to the paisa
    assert fee >= 0 and total == D(credit) + fee


def test_no_fee_configured_means_no_fee(monkeypatch):
    monkeypatch.setattr(config, "RECHARGE_FEE_PERCENT", D("0"))
    assert compute_fee(D("250")) == (D("0.00"), D("250.00"))


def test_a_fixed_fee_is_added_too(monkeypatch):
    monkeypatch.setattr(config, "RECHARGE_FEE_PERCENT", D("0"))
    monkeypatch.setattr(config, "RECHARGE_FEE_FIXED_INR", D("3"))
    assert compute_fee(D("100")) == (D("3.00"), D("103.00"))


def test_paise_is_exact():
    assert paise(D("102.42")) == 10242 and paise(D("0.10")) == 10 and paise(D("10241.71")) == 1024171


# --- signatures ---------------------------------------------------------------------------------


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(razorpay_client, "RAZORPAY_KEY_ID", "rzp_test_key")
    monkeypatch.setattr(razorpay_client, "RAZORPAY_KEY_SECRET", "topsecret")
    monkeypatch.setattr(razorpay_client, "RAZORPAY_WEBHOOK_SECRET", "hooksecret")
    monkeypatch.setattr(config, "RAZORPAY_KEY_ID", "rzp_test_key")


def test_a_checkout_signature_matches_razorpays_documented_formula(keys):
    import hashlib
    import hmac

    expected = hmac.new(b"topsecret", b"order_1|pay_1", hashlib.sha256).hexdigest()
    assert razorpay_client.checkout_signature("order_1", "pay_1") == expected
    assert razorpay_client.verify_checkout_signature("order_1", "pay_1", expected)


def test_a_wrong_or_missing_signature_is_refused(keys):
    good = razorpay_client.checkout_signature("order_1", "pay_1")
    assert not razorpay_client.verify_checkout_signature("order_1", "pay_1", good[:-1] + "0")
    assert not razorpay_client.verify_checkout_signature("order_2", "pay_1", good)  # signed for another order
    assert not razorpay_client.verify_checkout_signature("order_1", "pay_2", good)
    assert not razorpay_client.verify_checkout_signature("order_1", "pay_1", "")
    assert not razorpay_client.verify_checkout_signature("", "pay_1", good)


def test_no_secret_means_nothing_verifies(monkeypatch):
    monkeypatch.setattr(razorpay_client, "RAZORPAY_KEY_SECRET", "")
    monkeypatch.setattr(razorpay_client, "RAZORPAY_WEBHOOK_SECRET", "")
    assert not razorpay_client.verify_checkout_signature("o", "p", "anything")
    assert not razorpay_client.verify_webhook_signature(b"{}", "anything")


def test_webhook_signature_is_over_the_raw_body(keys):
    body = b'{"event":"payment.captured"}'
    sig = razorpay_client.webhook_signature(body)
    assert razorpay_client.verify_webhook_signature(body, sig)
    assert not razorpay_client.verify_webhook_signature(body + b" ", sig)  # one byte different


# --- a fake Razorpay ------------------------------------------------------------------------------


class FakeRazorpay:
    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.payments: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.down = False
        self.order_error: int | None = None
        self.captures = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.down:
            raise httpx.ConnectError("no route")
        path = request.url.path.removeprefix("/v1")
        assert request.headers["authorization"].startswith("Basic ")  # key id + secret
        if request.method == "POST" and path == "/orders":
            if self.order_error:
                return httpx.Response(self.order_error, json={"error": {"description": "bad"}})
            body = json.loads(request.content)
            order = {"id": f"order_{uuid.uuid4().hex[:12]}", **body, "status": "created"}
            self.orders[order["id"]] = order
            return httpx.Response(200, json=order)
        if request.method == "GET" and path.startswith("/payments/"):
            payment = self.payments.get(path.rsplit("/", 1)[1])
            if payment is None:
                return httpx.Response(400, json={"error": {"description": "no such payment"}})
            return httpx.Response(200, json=payment)
        if request.method == "POST" and path.endswith("/capture"):
            payment = self.payments[path.split("/")[2]]
            payment["status"] = "captured"
            self.captures += 1
            return httpx.Response(200, json=payment)
        return httpx.Response(404)

    def pays(self, order_id: str, *, status="captured", amount=None, payment_id=None, order_override=None) -> str:
        order = self.orders[order_id]
        payment_id = payment_id or f"pay_{uuid.uuid4().hex[:12]}"
        self.payments[payment_id] = {
            "id": payment_id,
            "order_id": order_override or order_id,
            "amount": order["amount"] if amount is None else amount,
            "currency": "INR",
            "status": status,
        }
        return payment_id


@pytest.fixture
def rz(keys):
    fake = FakeRazorpay()
    razorpay_client.http.reset(httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    yield fake
    razorpay_client.http.reset()


async def new_recharge(factory, user, amount="500"):
    async with factory() as s:
        return await recharge.create_recharge(s, user.id, D(amount))


async def confirm(factory, user, row, payment_id, signature=None):
    async with factory() as s:
        return await recharge.confirm_recharge(
            s, user.id, row.razorpay_order_id, payment_id,
            signature or razorpay_client.checkout_signature(row.razorpay_order_id, payment_id),
        )


async def balance(factory, user) -> Decimal:
    async with factory() as s:
        found = (await s.execute(select(Wallet.available_balance).where(Wallet.user_id == user.id))).scalar_one_or_none()
        return D("0.00") if found is None else found  # no wallet yet = nothing in it


async def credits(factory, user) -> int:
    async with factory() as s:
        return (
            await s.execute(
                select(func.count()).select_from(WalletTransaction).where(
                    WalletTransaction.user_id == user.id, WalletTransaction.reference_type == "recharge"
                )
            )
        ).scalar_one()


def webhook_body(row, payment_id, *, event="payment.captured", amount=None, status="captured"):
    return json.dumps(
        {
            "event": event,
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "order_id": row.razorpay_order_id,
                        "amount": paise(row.total_amount) if amount is None else amount,
                        "currency": "INR",
                        "status": status,
                    }
                }
            },
        }
    ).encode()


async def hook(factory, body, signature=None):
    async with factory() as s:
        return await recharge.handle_webhook(s, body, razorpay_client.webhook_signature(body) if signature is None else signature)


# --- making the order ---------------------------------------------------------------------------------


@needs_db
@pytest.mark.anyio
async def test_an_order_is_made_for_the_total_and_the_amounts_are_remembered(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "500")

    assert (row.credit_amount, row.fee_amount, row.total_amount) == (D("500.00"), D("12.09"), D("512.09"))
    assert row.status == "created" and row.razorpay_order_id in rz.orders
    order = rz.orders[row.razorpay_order_id]
    assert order["amount"] == 51209 and order["currency"] == "INR"  # the TOTAL, in paise
    assert order["notes"]["userId"] == str(user.id)
    d = recharge.recharge_to_dict(row, with_checkout=True)
    assert d["checkout"] == {"keyId": "rzp_test_key", "orderId": row.razorpay_order_id, "amountPaise": 51209, "currency": "INR"}
    assert "topsecret" not in json.dumps(d)  # the secret never leaves the server


@needs_db
@pytest.mark.anyio
@pytest.mark.parametrize("amount", ["9.99", "0", "-5", "10000.01", "999999"])
async def test_amounts_outside_the_limits_are_refused_before_razorpay_is_asked(sessions, rz, amount):
    user = await make_user(sessions)
    async with sessions() as s:
        await expect_error("INVALID_AMOUNT", recharge.create_recharge(s, user.id, D(amount)), 400)
    assert rz.calls == []


@needs_db
@pytest.mark.anyio
async def test_the_limits_themselves_are_allowed(sessions, rz):
    user = await make_user(sessions)
    assert (await new_recharge(sessions, user, "10")).credit_amount == D("10.00")
    assert (await new_recharge(sessions, user, "10000")).credit_amount == D("10000.00")


@needs_db
@pytest.mark.anyio
async def test_recharging_is_off_without_razorpay_keys(sessions, monkeypatch):
    monkeypatch.setattr(razorpay_client, "RAZORPAY_KEY_ID", "")
    user = await make_user(sessions)
    async with sessions() as s:
        await expect_error("RECHARGE_UNAVAILABLE", recharge.create_recharge(s, user.id, D("100")), 503)


@needs_db
@pytest.mark.anyio
async def test_a_frozen_wallet_cannot_start_a_payment(sessions, rz):
    user = await make_user(sessions)
    async with sessions() as s:
        from services.wallet import get_or_create_wallet

        wallet = await get_or_create_wallet(s, user.id)
        wallet.status = "frozen"
        await s.commit()
    async with sessions() as s:
        await expect_error("WALLET_FROZEN", recharge.create_recharge(s, user.id, D("100")), 403)
    assert rz.calls == [], "nobody should be asked to pay into a wallet that can't receive it"


@needs_db
@pytest.mark.anyio
async def test_razorpay_being_down_is_a_clear_error_and_leaves_nothing_behind(sessions, rz):
    rz.down = True
    user = await make_user(sessions)
    async with sessions() as s:
        await expect_error("GATEWAY_ERROR", recharge.create_recharge(s, user.id, D("100")), 502)
    async with sessions() as s:
        assert (await s.execute(select(func.count()).select_from(WalletRecharge).where(WalletRecharge.user_id == user.id))).scalar_one() == 0


@needs_db
@pytest.mark.anyio
async def test_razorpay_refusing_the_order_is_a_gateway_error(sessions, rz):
    rz.order_error = 400
    user = await make_user(sessions)
    async with sessions() as s:
        await expect_error("GATEWAY_ERROR", recharge.create_recharge(s, user.id, D("100")), 502)


@needs_db
@pytest.mark.anyio
async def test_a_runaway_app_cannot_pile_up_unpaid_orders(sessions, rz):
    user = await make_user(sessions)
    for _ in range(recharge.MAX_OPEN_ORDERS):
        await new_recharge(sessions, user, "50")
    async with sessions() as s:
        await expect_error("TOO_MANY_RECHARGES", recharge.create_recharge(s, user.id, D("50")), 429)
    other = await make_user(sessions)  # someone else is not affected
    await new_recharge(sessions, other, "50")


# --- confirming ------------------------------------------------------------------------------------------


@needs_db
@pytest.mark.anyio
async def test_a_paid_order_credits_the_amount_without_the_fee(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "500")
    payment_id = rz.pays(row.razorpay_order_id)

    done = await confirm(sessions, user, row, payment_id)

    assert done.status == "paid" and done.razorpay_payment_id == payment_id and done.paid_at is not None
    assert await balance(sessions, user) == D("500.00")  # 500, not 512.09
    async with sessions() as s:
        txn = (await s.execute(select(WalletTransaction).where(WalletTransaction.user_id == user.id))).scalar_one()
    assert (txn.type, txn.amount, txn.reference_type, txn.reference_id) == ("credit", D("500.00"), "recharge", str(row.id))
    assert "12.09" in txn.note and "512.09" in txn.note


@needs_db
@pytest.mark.anyio
async def test_confirming_twice_credits_once(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "300")
    payment_id = rz.pays(row.razorpay_order_id)
    await confirm(sessions, user, row, payment_id)
    await confirm(sessions, user, row, payment_id)  # a double tap / retry after a timeout
    assert await balance(sessions, user) == D("300.00")
    assert await credits(sessions, user) == 1


@needs_db
@pytest.mark.anyio
async def test_a_bad_signature_credits_nothing_and_never_calls_razorpay(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "300")
    payment_id = rz.pays(row.razorpay_order_id)
    calls_before = len(rz.calls)
    async with sessions() as s:
        await expect_error(
            "INVALID_SIGNATURE",
            recharge.confirm_recharge(s, user.id, row.razorpay_order_id, payment_id, "f" * 64),
            400,
        )
    assert await balance(sessions, user) == D("0.00")
    assert len(rz.calls) == calls_before


@needs_db
@pytest.mark.anyio
async def test_someone_elses_order_cannot_be_confirmed(sessions, rz):
    owner = await make_user(sessions)
    thief = await make_user(sessions)
    row = await new_recharge(sessions, owner, "300")
    payment_id = rz.pays(row.razorpay_order_id)
    async with sessions() as s:
        await expect_error(
            "RECHARGE_NOT_FOUND",
            recharge.confirm_recharge(
                s, thief.id, row.razorpay_order_id, payment_id,
                razorpay_client.checkout_signature(row.razorpay_order_id, payment_id),
            ),
            404,
        )
    assert await balance(sessions, thief) == D("0.00")


@needs_db
@pytest.mark.anyio
async def test_a_payment_for_a_smaller_amount_is_not_credited(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "500")
    payment_id = rz.pays(row.razorpay_order_id, amount=1000)  # ₹10 instead of ₹512.09
    async with sessions() as s:
        await expect_error("NEEDS_REVIEW", recharge.confirm_recharge(
            s, user.id, row.razorpay_order_id, payment_id,
            razorpay_client.checkout_signature(row.razorpay_order_id, payment_id)), 409)
    assert await balance(sessions, user) == D("0.00")
    async with sessions() as s:
        assert (await recharge.get_recharge(s, user.id, row.razorpay_order_id)).status == "review"


@needs_db
@pytest.mark.anyio
async def test_a_payment_that_belongs_to_another_order_is_not_credited(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "500")
    other = await new_recharge(sessions, user, "50")
    payment_id = rz.pays(other.razorpay_order_id)  # a real payment, but for the cheaper order
    async with sessions() as s:
        await expect_error("NEEDS_REVIEW", recharge.confirm_recharge(
            s, user.id, row.razorpay_order_id, payment_id,
            razorpay_client.checkout_signature(row.razorpay_order_id, payment_id)), 409)
    assert await balance(sessions, user) == D("0.00")


@needs_db
@pytest.mark.anyio
async def test_an_authorized_payment_is_captured_first_then_credited(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "200")
    payment_id = rz.pays(row.razorpay_order_id, status="authorized")
    await confirm(sessions, user, row, payment_id)
    assert rz.captures == 1
    assert await balance(sessions, user) == D("200.00")


@needs_db
@pytest.mark.anyio
async def test_a_failed_payment_credits_nothing_and_the_order_can_still_be_paid_again(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "200")
    failed = rz.pays(row.razorpay_order_id, status="failed")
    async with sessions() as s:
        await expect_error("PAYMENT_FAILED", recharge.confirm_recharge(
            s, user.id, row.razorpay_order_id, failed,
            razorpay_client.checkout_signature(row.razorpay_order_id, failed)), 402)
    assert await balance(sessions, user) == D("0.00")

    retry = rz.pays(row.razorpay_order_id)  # same order, second attempt succeeds
    await confirm(sessions, user, row, retry)
    assert await balance(sessions, user) == D("200.00")


@needs_db
@pytest.mark.anyio
async def test_a_payment_still_processing_is_not_credited_yet(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "200")
    payment_id = rz.pays(row.razorpay_order_id, status="created")
    async with sessions() as s:
        await expect_error("PAYMENT_PENDING", recharge.confirm_recharge(
            s, user.id, row.razorpay_order_id, payment_id,
            razorpay_client.checkout_signature(row.razorpay_order_id, payment_id)), 409)
    assert await balance(sessions, user) == D("0.00")


@needs_db
@pytest.mark.anyio
async def test_razorpay_unreachable_while_confirming_is_retryable_not_a_loss(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "200")
    payment_id = rz.pays(row.razorpay_order_id)
    rz.down = True
    async with sessions() as s:
        await expect_error("GATEWAY_ERROR", recharge.confirm_recharge(
            s, user.id, row.razorpay_order_id, payment_id,
            razorpay_client.checkout_signature(row.razorpay_order_id, payment_id)), 502)
    rz.down = False
    await confirm(sessions, user, row, payment_id)  # the app's retry (or the webhook) completes it
    assert await balance(sessions, user) == D("200.00")


@needs_db
@pytest.mark.anyio
async def test_a_wallet_frozen_after_payment_is_parked_for_a_person_not_lost(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "200")
    payment_id = rz.pays(row.razorpay_order_id)
    async with sessions() as s:
        from services.wallet import get_or_create_wallet

        wallet = await get_or_create_wallet(s, user.id)
        wallet.status = "frozen"
        await s.commit()
    async with sessions() as s:
        await expect_error("NEEDS_REVIEW", recharge.confirm_recharge(
            s, user.id, row.razorpay_order_id, payment_id,
            razorpay_client.checkout_signature(row.razorpay_order_id, payment_id)), 409)
    async with sessions() as s:
        parked = await recharge.get_recharge(s, user.id, row.razorpay_order_id)
    assert parked.status == "review" and "frozen" in parked.note.lower()

    # once support unfreezes the wallet, the payment can complete
    async with sessions() as s:
        wallet = await s.get(Wallet, user.id)
        wallet.status = "active"
        await s.commit()
    await confirm(sessions, user, row, payment_id)
    assert await balance(sessions, user) == D("200.00")


# --- the webhook ---------------------------------------------------------------------------------------------


@needs_db
@pytest.mark.anyio
async def test_the_webhook_credits_a_payment_the_app_never_confirmed(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "400")
    payment_id = rz.pays(row.razorpay_order_id)
    result = await hook(sessions, webhook_body(row, payment_id))
    assert result == {"handled": True}
    assert await balance(sessions, user) == D("400.00")


@needs_db
@pytest.mark.anyio
async def test_order_paid_events_work_too(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "400")
    payment_id = rz.pays(row.razorpay_order_id)
    assert (await hook(sessions, webhook_body(row, payment_id, event="order.paid")))["handled"]
    assert await balance(sessions, user) == D("400.00")


@needs_db
@pytest.mark.anyio
async def test_the_same_webhook_delivered_twice_credits_once(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "400")
    payment_id = rz.pays(row.razorpay_order_id)
    body = webhook_body(row, payment_id)
    await hook(sessions, body)
    await hook(sessions, body)
    assert await balance(sessions, user) == D("400.00") and await credits(sessions, user) == 1


@needs_db
@pytest.mark.anyio
async def test_confirm_then_webhook_credits_once(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "400")
    payment_id = rz.pays(row.razorpay_order_id)
    await confirm(sessions, user, row, payment_id)
    await hook(sessions, webhook_body(row, payment_id))
    assert await balance(sessions, user) == D("400.00") and await credits(sessions, user) == 1


@needs_db
@pytest.mark.anyio
async def test_confirm_and_webhook_at_the_very_same_moment_credit_once(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "400")
    payment_id = rz.pays(row.razorpay_order_id)
    for _ in range(3):  # the race is the point: try it a few times
        await asyncio.gather(
            confirm(sessions, user, row, payment_id),
            hook(sessions, webhook_body(row, payment_id)),
            hook(sessions, webhook_body(row, payment_id)),
        )
    assert await balance(sessions, user) == D("400.00")
    assert await credits(sessions, user) == 1


@needs_db
@pytest.mark.anyio
async def test_a_forged_webhook_is_refused(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "400")
    payment_id = rz.pays(row.razorpay_order_id)
    body = webhook_body(row, payment_id)
    async with sessions() as s:
        await expect_error("INVALID_SIGNATURE", recharge.handle_webhook(s, body, "0" * 64), 400)
    async with sessions() as s:
        await expect_error("INVALID_SIGNATURE", recharge.handle_webhook(s, body, ""), 400)
    assert await balance(sessions, user) == D("0.00")


@needs_db
@pytest.mark.anyio
async def test_a_webhook_with_the_wrong_amount_credits_nothing(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "400")
    payment_id = rz.pays(row.razorpay_order_id)
    result = await hook(sessions, webhook_body(row, payment_id, amount=100))
    assert result == {"handled": False, "review": True}
    assert await balance(sessions, user) == D("0.00")


@needs_db
@pytest.mark.anyio
async def test_events_that_are_not_ours_are_acknowledged_and_ignored(sessions, rz):
    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "400")
    payment_id = rz.pays(row.razorpay_order_id)
    assert (await hook(sessions, webhook_body(row, payment_id, event="payment.failed"))) == {"handled": False}
    unknown = json.dumps(
        {"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_x", "order_id": "order_someone_else", "amount": 100, "status": "captured"}}}}
    ).encode()
    assert (await hook(sessions, unknown)) == {"handled": False}  # another product on the same Razorpay account
    assert (await hook(sessions, b'{"event": "payment.captured"}')) == {"handled": False}  # no payment inside
    assert await balance(sessions, user) == D("0.00")


@needs_db
@pytest.mark.anyio
async def test_garbage_after_a_valid_signature_is_a_clear_error(sessions, rz):
    body = b"not json at all"
    async with sessions() as s:
        await expect_error("INVALID_BODY", recharge.handle_webhook(s, body, razorpay_client.webhook_signature(body)), 400)


@needs_db
@pytest.mark.anyio
async def test_no_webhook_secret_means_the_webhook_is_off(sessions, rz, monkeypatch):
    monkeypatch.setattr(razorpay_client, "RAZORPAY_WEBHOOK_SECRET", "")
    async with sessions() as s:
        await expect_error("WEBHOOK_UNAVAILABLE", recharge.handle_webhook(s, b"{}", "x"), 503)


# --- the ledger stays honest ----------------------------------------------------------------------------------


@needs_db
@pytest.mark.anyio
async def test_several_recharges_add_up_and_the_ledger_matches_the_balance(sessions, rz):
    user = await make_user(sessions)
    for amount in ("100", "250.50", "49"):
        row = await new_recharge(sessions, user, amount)
        await confirm(sessions, user, row, rz.pays(row.razorpay_order_id))
    assert await balance(sessions, user) == D("399.50")
    async with sessions() as s:
        total = (await s.execute(select(func.sum(WalletTransaction.amount)).where(
            WalletTransaction.user_id == user.id, WalletTransaction.type == "credit"))).scalar_one()
    assert total == D("399.50")


@needs_db
@pytest.mark.anyio
async def test_recharged_money_funds_a_call_offer_like_any_other(sessions, rz):
    """The point of it all: after a recharge the requester has spendable money."""
    from services import wallet as wallet_service

    user = await make_user(sessions)
    row = await new_recharge(sessions, user, "500")
    await confirm(sessions, user, row, rz.pays(row.razorpay_order_id))
    async with sessions() as s:
        wallet = await wallet_service.get_or_create_wallet(s, user.id)
        commitments = await wallet_service.call_commitments(s, user.id)
    assert wallet.available_balance - commitments == D("500.00")
