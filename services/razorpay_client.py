"""Razorpay, over plain HTTPS (no SDK): make an order, look a payment up, capture it, and check the
signatures Razorpay puts on a finished checkout and on a webhook.

Money is only ever credited after the *server* has seen the payment (signature + a lookup at
Razorpay), never because the app said so.
"""

import hashlib
import hmac

import httpx

from config import RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET, logger

API_BASE = "https://api.razorpay.com/v1"


class RazorpayError(Exception):
    """Razorpay refused a request or could not be reached. `retriable` = a passing problem."""

    def __init__(self, message: str, *, status: int | None = None, retriable: bool = False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retriable = retriable


def is_configured() -> bool:
    return bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)


def webhook_configured() -> bool:
    return bool(RAZORPAY_WEBHOOK_SECRET)


class _Http:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def reset(self, client: httpx.AsyncClient | None = None) -> None:
        """For tests: swap the HTTP client."""
        self._client = client

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._client


http = _Http()


async def _request(method: str, path: str, body: dict | None = None) -> dict:
    if not is_configured():
        raise RazorpayError("Razorpay is not configured")
    try:
        response = await http.client().request(
            method, f"{API_BASE}{path}", json=body, auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
        )
    except httpx.HTTPError as e:
        logger.error("Razorpay %s %s unreachable: %s", method, path, e)
        raise RazorpayError("Razorpay could not be reached", retriable=True) from e

    if response.status_code >= 500:
        logger.error("Razorpay %s %s -> %s", method, path, response.status_code)
        raise RazorpayError("Razorpay is having trouble", status=response.status_code, retriable=True)
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        description = (data.get("error") or {}).get("description") or "Razorpay refused the request"
        logger.error("Razorpay %s %s -> %s %s", method, path, response.status_code, description)
        raise RazorpayError(description, status=response.status_code)
    return data


async def create_order(amount_paise: int, receipt: str, notes: dict | None = None) -> dict:
    return await _request(
        "POST",
        "/orders",
        {"amount": amount_paise, "currency": "INR", "receipt": receipt, "notes": notes or {}},
    )


async def fetch_payment(payment_id: str) -> dict:
    return await _request("GET", f"/payments/{payment_id}")


async def capture_payment(payment_id: str, amount_paise: int) -> dict:
    return await _request("POST", f"/payments/{payment_id}/capture", {"amount": amount_paise, "currency": "INR"})


def _same(expected: str, given: str) -> bool:
    return hmac.compare_digest(expected.encode(), (given or "").encode())


def checkout_signature(order_id: str, payment_id: str) -> str:
    """What Razorpay's checkout signs a successful payment with."""
    return hmac.new(RAZORPAY_KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    if not (RAZORPAY_KEY_SECRET and order_id and payment_id and signature):
        return False
    return _same(checkout_signature(order_id, payment_id), signature)


def webhook_signature(body: bytes) -> str:
    return hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    if not (RAZORPAY_WEBHOOK_SECRET and signature):
        return False
    return _same(webhook_signature(body), signature)
