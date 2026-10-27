# Wallet + Scratch Card — Frontend Integration Guide

All endpoints below require the normal driver JWT (`Authorization: Bearer <accessToken>`
from `POST /api/auth/verify-otp`), same as every other protected endpoint in the app.
`{user_id}` in the path must always be the logged-in user's own id — the backend 403s
otherwise.

Money is always a rupee string with 2 decimal places, e.g. `"50.00"`, never a number
(avoids float rounding in JS). Parse with a decimal-safe method if you do math on it.

---

## 1. Wallet

### `GET /api/wallet/{user_id}`
Get balance. Creates a zero-balance wallet on first call (only works if the user's
`kycStatus` is `"approved"` — see Errors below).

```json
{
  "success": true,
  "wallet": {
    "availableBalance": "150.00",
    "reservedBalance": "0.00",
    "status": "active"
  }
}
```

- `status`: `"active"` or `"frozen"`. If frozen, redeem calls 403 — show a
  "contact support" state, don't let the user retry.

### `GET /api/wallet/{user_id}/transactions`
Ledger, most recent first (max 50). Good for a "transaction history" screen.

```json
{
  "success": true,
  "transactions": [
    {
      "id": "uuid",
      "type": "credit",
      "amount": "50.00",
      "availableAfter": "150.00",
      "reservedAfter": "0.00",
      "referenceType": "scratch_card",
      "referenceId": "uuid-of-scratch-card",
      "note": "Scratch card reward",
      "createdAt": "2026-07-30T10:48:00.924168"
    }
  ]
}
```

`type` values you'll see: `credit` (scratch card / manual), `reserve_redeem` (user
redeemed), `confirm_payout` (admin confirmed payout), `release_reserve` (admin
rejected a redeem, money returned).

### `POST /api/wallet/{user_id}/redeem`
Redeems the user's **entire** available balance to a UPI ID — there's no partial
redeem, and no client-supplied amount (server always uses the current balance).

Request:
```json
{
  "upiId": "9876543210@ybl",
  "idempotencyKey": "client-generated-uuid-v4"
}
```

Response:
```json
{
  "success": true,
  "message": "Redeem request submitted",
  "redeemRequest": {
    "id": "uuid",
    "amount": "150.00",
    "upiId": "9876543210@ybl",
    "status": "pending",
    "createdAt": "..."
  }
}
```

**`idempotencyKey`**: generate one fresh UUID v4 the moment the user taps "Redeem",
and reuse the *same* key if you retry after a timeout/network error. A retry with
the same key returns the original result instead of reserving the balance twice.
Never reuse a key across two different redeem attempts.

After this call the balance moves from `availableBalance` to `reservedBalance` —
show that immediately (don't wait for a push). `redeemRequest.status` goes
`pending` → `paid` (admin confirmed, done via the admin panel — no push notification
is wired up for this yet, so the UI should poll `GET /wallet/{user_id}/transactions`
or the balance to notice a `confirm_payout` row) or `pending` → `rejected` (money
auto-returns to `availableBalance`).

**Errors specific to redeem:**
| Status | When |
|---|---|
| 400 | Invalid UPI ID format, or balance below the minimum redeem amount (currently ₹500) |
| 403 | Wallet frozen |

---

## 2. Scratch Cards

A card is earned automatically — **at most one per user per UTC calendar day** —
the moment a user creates a truck post (`POST /api/posts/create/{user_id}`), and
only if their KYC is approved (which is already required to post at all, so no
extra check needed on your end). That response now includes:

```json
{
  "success": true,
  "post": { ... },
  "scratchCardEarned": true,
  "scratchCard": {
    "id": "uuid",
    "status": "unscratched",
    "earnedAt": "2026-07-30T12:00:24.711775",
    "expiresAt": "2026-08-28T12:00:24.711775",
    "scratchedAt": null,
    "rewardRupees": null
  }
}
```

`scratchCard` is the full new card object (same shape as the list endpoint below) —
**no follow-up `GET /scratch-cards/{user_id}` call needed** to find it. It's `null`
whenever `scratchCardEarned` is `false` (user already earned today's card, or
something upstream prevented it — either way, don't show a banner).

### `GET /api/scratch-cards/{user_id}`
List the user's cards, most recently earned first.

```json
{
  "success": true,
  "scratchCards": [
    {
      "id": "uuid",
      "status": "unscratched",
      "earnedAt": "2026-07-30T10:47:45.380602",
      "expiresAt": "2026-08-28T10:47:45.380602",
      "scratchedAt": null,
      "rewardRupees": "50.00"
    }
  ]
}
```

- `status`: `unscratched` | `scratched` | `expired`.
- **`rewardRupees` is already populated even on an `unscratched` card** — the
  reward is decided the moment the card is earned (issuance time), not when the
  user taps it. This means you can render the exact reward amount instantly,
  entirely from data you already have client-side, with zero network wait —
  the scratch/reveal animation is purely cosmetic; the number behind it never
  changes between "card returned" and "card scratched." The `scratch` API call
  below is only there to actually commit that amount to the wallet.
- Cards expire **29 days** after being earned if never scratched — filter for
  `status === "unscratched"` to show an actionable list; still show `scratched`/
  `expired` ones in a history view if you want.

### `POST /api/scratch-cards/{user_id}/{card_id}/scratch`
Commits the card's already-known reward to the wallet and flips its status to
`scratched`. It does **not** compute anything — the amount was decided at
issuance — so this call is fast and safe to fire the instant the scratch
gesture/animation completes, with no loading spinner needed for the number
itself (only for the network round-trip of actually crediting it, if you want
one at all).

Request:
```json
{ "idempotencyKey": "client-generated-uuid-v4" }
```

Response:
```json
{
  "success": true,
  "cardId": "uuid",
  "rewardPaise": 5000,
  "rewardRupees": "50.00",
  "walletAvailableBalance": "50.00"
}
```

Same idempotency-key rule as redeem: one fresh UUID per scratch attempt (generate
it right when the user starts the scratch animation, before the API call), reuse
on retry. `walletAvailableBalance` is the balance *after* this credit — use it to
update the wallet UI directly instead of making a second `GET /wallet` call.

**Errors specific to scratch:**
| Status | When | UI suggestion |
|---|---|---|
| 404 | Card doesn't exist / isn't this user's | shouldn't happen from normal UI flow |
| 409 | Already scratched (different idempotency key than the original attempt) | refresh the card list, show it as already-revealed |
| 410 | Expired before the user tapped | show "This card expired" and refresh the list |

The very first card a brand-new user ever scratches always pays a fixed **₹50**
bonus — every scratch after that is a random amount within a monthly-declining
ceiling. Don't hardcode ₹50 as the "typical" reward anywhere in the UI copy.

---

## 3. Suggested screens

1. **Post-creation banner**: read `scratchCardEarned`/`scratchCard` from the
   create-post response → show "🎉 You've earned a scratch card!" with a CTA
   straight into the reveal flow for that card (you already have its `id`, no
   extra fetch needed).
2. **Scratch Cards screen**: `GET /scratch-cards/{user_id}` → show unscratched
   cards as tappable cards with `expiresAt` as a countdown; tapping one calls
   the scratch endpoint and plays the reveal animation once the response lands.
3. **Wallet screen**: `GET /wallet/{user_id}` for the balance header,
   `GET /wallet/{user_id}/transactions` for history below it, and a "Redeem"
   button that opens a UPI-ID input → calls redeem → shows the `pending` state.

## 4. Things NOT built yet (don't design UI assuming these exist)

- No push notification when a redeem is marked paid/rejected by an admin — the
  app has to notice via polling transactions/balance, not a push.
- No automated bank payout — "paid" purely means an admin manually sent the
  money and confirmed it on the admin panel; there's no live payment status API.
- No partial redeem — it's always the full available balance.
