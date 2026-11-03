# Wallet: UPI ID and withdrawing

Built on the existing wallet (`/api/wallet/...`, all need the caller's token and only work on your own `user_id`).

## What changed

| | |
|---|---|
| `GET /api/wallet/{user_id}` | `wallet` now also has `upiId` (string or `null`) and `minRedeemAmount` (string, `"250"`). |
| `PUT /api/wallet/{user_id}/upi` | Body `{"upiId": "name@bank"}`. Saves or changes the UPI ID. Returns `{"success": true, "upiId": "name@bank"}` (trimmed). `400` if it isn't shaped like `handle@bank`. |
| `POST /api/wallet/{user_id}/redeem` | Body `{"idempotencyKey": "..."}`. Pays the whole **spendable** balance to the saved UPI ID. `upiId` in the body is now optional: if sent it is checked and saved as the account's UPI ID first (older app versions). |

## Redeem refusals (`detail` carries the text)

| Status | When |
|---|---|
| 400 `Add your UPI ID before withdrawing` | No UPI ID saved (and none sent). |
| 400 `Minimum balance of ₹250 required to redeem` | Spendable balance under `MIN_REDEEM_AMOUNT_INR` (config / env, default 250). |
| 403 `Wallet requires an approved KYC status` | KYC not approved. |
| 403 `Wallet is frozen, contact support` | Frozen wallet. |

Spendable = available minus money promised to open 1:1 requests (`spendableBalance` in the wallet response).
A successful redeem moves that amount from `availableBalance` to `reservedBalance` ("pending payout") and creates a `pending` redeem request that keeps the UPI ID it was made with; an admin marks it paid or rejects it (rejecting refunds). Retrying with the same `idempotencyKey` returns the same request.

Database: `users.upi_id VARCHAR(255) NULL` (migration `029_user_upi_id.sql`).
