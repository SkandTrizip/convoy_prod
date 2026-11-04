# Paid 1:1 calls — API (requests, wallet, settlement, the call itself)

A requester offers money for a live 1:1 call with a driver. The driver accepts or
rejects, starts the call (the requester's phone rings), the two talk over Agora, and
after a call that really connected the requester says whether they were satisfied.
The old open-room `/api/calls` endpoints are gone.

All endpoints are under `/api`, need the normal `Authorization: Bearer <jwt>`, and
return `{"success": true, ...}` on success.

## The money rules (what the UI has to explain)

- The requester picks an **offer** (₹50 – ₹10,000), a **duration** (10 / 15 / 30 min) and a **topic**.
- The wallet must be able to cover the **whole offer**. If it can't: `402` with
  `shortfall` — show "Add ₹X to your wallet" and send them to recharge.
- Only the **call amount** is frozen: `holdAmount = max(25% of the offer, ₹25)`.
  Examples: ₹50 → ₹25, ₹100 → ₹25, ₹150 → ₹37.50, ₹1000 → ₹250.
- The rest of the offer (`amount − holdAmount`) is not frozen but is **committed**:
  new offers and redeem can't use it while the request is open.
  `GET /wallet/{userId}` returns `callCommitments` and `spendableBalance` for this.
- Rejected / cancelled / expired / call never connected for 30 s → **everything released**.
- Call connected ≥ 30 s → the requester is asked **"were you satisfied?"**
  - yes → pays the full `amount`
  - no → pays only `holdAmount` (the cost of connecting)
  - no answer in 24 h → same as "no"
- No platform commission. The driver gets exactly what the requester pays.
- Any user can have a wallet (no KYC needed). **Redeem still needs approved KYC.**

## Status flow

```
pending ──accept──▶ accepted ──start──▶ ringing ──join──▶ live ──end/drop/time──▶ awaiting_feedback ──feedback──▶ completed
   │                   │  ▲                │                │                           (≥ 30 s connected)
 reject/expire     cancel/expire   missed/cancelled     failed (< 30 s connected, hold released)
   ▼                   ▼              ring (45 s)
rejected / expired / cancelled
```

- A ring nobody answers within **45 s** goes back to `accepted`; the driver can `start` again
  (as often as they like) until the request's window ends (30 minutes after the driver accepted; in the advance-payment flow see ONE_TO_ONE_DIRECT_PAYMENT_API.md).
- The requester **declining** a ring (`end` while ringing) cancels the request and releases the money.

- `pending`, `accepted`, `ringing`, `live` are **open**: at most one per requester/driver pair.
- A request lives **30 minutes** from creation while it is `pending`. **Accepting restarts the clock: it then lives 30 minutes from the acceptance** (accepted + ringing), so a driver can take several requests and call them one by one; `expiresAt` moves accordingly. Then it's `expired`.
- `awaiting_feedback` still holds money but doesn't block a new request to the same driver.

## Endpoints

### `POST /call-requests` — ask for a call (requester)

```json
{
  "providerId": "<userId from the truck post>",
  "truckRouteId": "<truckRouteId of the live post>",
  "topic": "Route status, load negotiation",
  "durationMinutes": 15,
  "amount": "150.00"
}
```

`201` → `{ "success": true, "request": { ...see below } }`. The driver gets a push
(`data.type = "call_request"`, `data.requestId`).

### `GET /call-requests/incoming` — the driver's queue (oldest first)

`{ "count": 2, "requests": [...] }`. Includes `pending` ones to review and
`accepted`/`ringing`/`live` ones. Poll it while Home is visible and whenever a push
arrives; the Home banner shows `requests[0]` and the `count`.

### `GET /call-requests/outgoing?activeOnly=true` — the requester's requests

Newest first. `activeOnly=true` keeps open ones and ones waiting for the
"were you satisfied?" answer (`awaiting_feedback`) — use it to resume that prompt
after an app restart.

### `GET /call-requests/{id}` — one request (either side; anyone else gets `404`)

### `POST /call-requests/{id}/accept` · `/reject` (driver) · `/cancel` (requester)

`cancel` works while `pending` or `accepted`. Accepting a request that is past its
window returns `410 REQUEST_EXPIRED` and releases the money. The other side gets a
push (`call_request_accepted`, `call_request_rejected`).

### The call

All of these also return the usual `request` object. `start` and `join` add a `call` object.

#### `POST /call-requests/{id}/start` (driver)
Only from `accepted`. Rings the requester (push `data.type = "call_incoming"`) and returns the
driver's credentials. Join the Agora channel and start the camera while it rings.
Calling it again while it is still ringing returns the same credentials, so it is safe after an
app restart. `410 REQUEST_EXPIRED` if the window (30 minutes after acceptance) is over.

#### `POST /call-requests/{id}/join` (requester answers; either side rejoins)
Requester, while `ringing`: the call goes `live` and they get credentials with `audioOnly: true`.
`409 RING_OVER` if the 45 s ran out. Either side while `live`: fresh credentials to rejoin after a
drop (also fine for the driver while it rings).

#### One account on several phones
The app sends its device id on every request (`X-Device-Id`). The phone that answers the ring (requester) or starts it
(driver) is remembered, and while the call is `ringing`/`live` another phone of the same account gets
`409 CALL_ON_ANOTHER_DEVICE` from `start`, `join`, `end` and `heartbeat` (it must not join with the same Agora uid, hang up, or
count presence). It may take the call over with `join` only once the holder has been silent for `CALL_DEVICE_TAKEOVER_SECONDS`
(20 s); the first phone's next heartbeat then gets the 409 and should leave **without** calling `end`. The request object
carries `onAnotherDevice: true` for the phone that is not the holder, so the app does not offer "Rejoin" there.
No `X-Device-Id` = no check (older apps). Answering or declining a ring also sends `call_cancelled` to the account's other phones.

#### The `call` object
```json
{
  "requestId": "…", "role": "requester", "appId": "…", "channelName": "call-<id>",
  "uid": 123456789, "rtcToken": "…", "tokenExpiresAt": "2026-10-30T10:30:00+00:00",
  "durationSeconds": 900, "audioOnly": true, "ringEndsAt": null,
  "chatAvailable": false, "rtmUserId": "<userId>", "rtmToken": null
}
```
- Join with `uid` and `rtcToken`. **Tokens stop working at the end of the chosen duration** (plus
  30 s grace): Agora disconnects the app, so the time limit holds even if an app misbehaves.
- `audioOnly: true` (requester): publish the microphone only, **never a camera track**. The driver
  publishes camera and microphone.
- `chatAvailable` / `rtmToken`: chat over Agora RTM. Until the backend has the newer token builder
  `rtmToken` is `null` and `chatAvailable` is `false` (hide the chat button). When it is `true`, log in to
  RTM with `rtmUserId` and `rtmToken`, then subscribe to `channelName`. The old tokens are rejected by
  RTM 2.x, which is why this is separate.

#### `POST /call-requests/{id}/heartbeat` (both apps, every ~5 s on the call screen)
```json
{ "peerPresent": true }
```
`peerPresent` = "I can currently see the other person in the Agora channel" (use the SDK's
user-joined / user-offline events). Response:

```json
{ "success": true, "status": "live", "connectedSeconds": 35, "remainingSeconds": 860,
  "thresholdSeconds": 30, "peerOnline": true }
```
- **Time only counts while both apps are reporting (heard from in the last 15 s) and both say
  `peerPresent: true`.** Heartbeating from outside the channel earns nothing.
- When `status` is no longer `live`, the call is over: leave the channel. If it is
  `awaiting_feedback`, show the requester the satisfaction prompt; `failed` means nothing is owed.
- `connectedSeconds` vs `thresholdSeconds` (30) drives a "call counted" indicator if you want one.

#### `POST /call-requests/{id}/end` (either side)
- While **ringing**: the driver's `end` cancels the ring (status back to `accepted`, can start
  again); the requester's `end` declines it (status `cancelled`, money released).
- While **live**: ends the call and settles from the connected time (< 30 s costs nothing,
  otherwise `awaiting_feedback`; the requester gets a push `call_feedback_needed`).
- Already over: returned unchanged (double taps are harmless).

#### What the server does by itself
A background job (every 30 s) returns unanswered rings to `accepted`, ends live calls that ran past
their time (`end_reason: time_limit`) or where either side has been silent for **60 s**
(`disconnected`, settled from the connected time so far), expires requests after 30 minutes, and
settles unanswered satisfaction prompts after 24 h. **A drop under 60 s is never fatal:** the app
calls `join` again and carries on.

### `POST /call-requests/{id}/feedback` (requester)

```json
{ "satisfied": true }
```

Only valid in `awaiting_feedback`. Safe to retry with the same answer; a different
answer after settling is `409 ALREADY_SETTLED`. The driver gets a push
(`call_payment`).

### `POST /call-requests/{id}/events` — behaviour log (either side)

```json
{ "events": [ { "type": "mic_off", "occurredAt": "2026-10-29T10:15:00Z" },
              { "type": "chat_message_sent", "details": { "length": 12 } } ] }
```

Up to 50 per call. **Never send chat text.** Allowed types: `request_viewed`,
`call_screen_opened`, `ring_answered`, `ring_declined`, `call_joined`, `call_left`,
`call_cut`, `mic_on`, `mic_off`, `camera_on`, `camera_off`, `camera_switched`,
`speaker_on`, `speaker_off`, `chat_opened`, `chat_closed`, `chat_message_sent`,
`reconnect_offered`, `reconnect_accepted`, `reconnect_declined`, `network_lost`,
`network_restored`, `app_backgrounded`, `app_foregrounded`. The server records its
own lifecycle events (created, accepted, rejected, expired, feedback…) by itself.

### The request object

```json
{
  "id": "…", "role": "requester", "status": "pending",
  "topic": "Route status", "durationMinutes": 15,
  "amount": "150.00", "holdAmount": "37.50",
  "requester": { "id": "…", "name": "Asha", "photo": null },
  "provider":  { "id": "…", "name": "Vikram", "photo": "/api/user/profile-photo/…" },
  "truckRouteId": "…",
  "expiresAt": "2026-10-29T10:45:00", "createdAt": "…", "respondedAt": null,
  "callStartedAt": null, "ringEndsAt": null, "callEndedAt": null, "connectedSeconds": null,
  "feedbackDeadline": null, "satisfied": null, "settledAmount": null
}
```

`ringEndsAt` is set only while the status is `ringing` (when the 45 s ring stops): the requester's
"incoming call" screen counts down to it. `role` is the caller's side. Amounts are strings (INR, 2 decimals); timestamps are UTC.
`holdAmount` is the "call amount"; `settledAmount` is what was actually paid.

## Errors

Every error has a plain-string `detail` (show it) plus a stable `code` (switch on it):

| Status | `code` | Meaning / extra fields |
|---|---|---|
| 402 | `INSUFFICIENT_BALANCE` | `requiredAmount`, `spendableBalance`, `shortfall` → recharge prompt |
| 400 | `INVALID_AMOUNT` | `minAmount`, `maxAmount` |
| 400 | `INVALID_DURATION`, `INVALID_TOPIC`, `SELF_REQUEST`, `INVALID_ID`, `UNKNOWN_EVENT`, `TOO_MANY_EVENTS`, `EVENT_TOO_LARGE` | |
| 403 | `WALLET_FROZEN`, `NOT_PROVIDER`, `NOT_REQUESTER` | |
| 404 | `REQUEST_NOT_FOUND` | also for requests you aren't part of |
| 404 | `PROVIDER_UNAVAILABLE`, `TRUCK_POST_UNAVAILABLE` | driver suspended / post expired or not theirs |
| 409 | `ALREADY_OPEN` | `requestId` of the existing open request |
| 409 | `INVALID_STATE` | `status` it is in now |
| 409 | `RING_OVER` | the requester answered after the 45 s ring |
| 409 | `CALL_ON_ANOTHER_DEVICE` | another phone of this account holds the call (see above) |
| 503 | (plain `detail`) | Agora isn't configured on the server |
| 409 | `ALREADY_SETTLED` | |
| 410 | `REQUEST_EXPIRED` | |
| 429 | `EVENT_LIMIT` | |

## Wallet changes the app needs to know

- `GET /wallet/{userId}` now also returns `callCommitments` and `spendableBalance`.
- New ledger types in `GET /wallet/{userId}/transactions` (all with
  `referenceType: "call_request"`, `referenceId: <request id>`):

  | `type` | Whose | Effect | Show as |
  |---|---|---|---|
  | `call_hold` | requester | available → reserved | "Held for call" (neutral) |
  | `call_release` | requester | reserved → available | "Released" (neutral/+) |
  | `call_debit` | requester | money leaves | "Paid for call" (−) |
  | `call_earning` | driver | money arrives | "Call earning" (+) |

  Note `isCredit` in the Flutter wallet entity currently treats `release_reserve` as a
  credit; add the new types there (`call_earning`, `call_release` positive,
  `call_debit` negative, `call_hold` neutral).
- **Dev only:** `POST /wallet/{userId}/dev-credit` `{ "amount": "500", "idempotencyKey": "<uuid>" }`
  adds test money to your own wallet. It returns `404` unless the server runs with
  `DEV_WALLET_CREDIT_ENABLED=true`. It disappears when Razorpay recharge is built.

## Ringing a phone (Stage 6): pushes and device registration

When the driver starts a call, every active device of the requester is rung. What each gets:

| Device | Push | App does |
|---|---|---|
| iPhone with a VoIP token (and Apple VoIP configured on the server) | **VoIP push** (APNs, topic `<bundle id>.voip`, expires with the ring) | puts up the system call screen (CallKit) at once, even closed / locked |
| Android | **data-only, high-priority FCM** (no visible notification) | shows the full-screen call screen (`flutter_callkit_incoming`) |
| iPhone without a VoIP token, web | the normal notification "Incoming 1:1 call" | tapping it opens the app's own ring screen |

If Apple refuses a VoIP push, that iPhone gets the normal notification instead.

When a ring ends unanswered, or the driver hangs up while it rings, the requester also gets a normal visible notification,
`{ "type": "call_missed", "missedBy": "requester", "requestId": "..." }` ("Missed 1:1 call"), so they find out even if the ring push never reached the phone.

Push data for `call_incoming` (all values are strings; **no money details**):
`{ "type": "call_incoming", "requestId": "<uuid>", "callerName": "Vikram", "topic": "...", "ringEndsAt": "2026-10-30T10:30:45+00:00" }`.
The request id is a UUID so the app can use it directly as the CallKit call id.

`call_cancelled` (`{ "type": "call_cancelled", "requestId": "..." }`): a silent data push sent when the **driver** hangs up while it rings, or when the
requester answers/declines on one phone (then to their other phones only), so the phone drops its call screen. Best effort (the screen also ends by itself when the ring does). It is never a VoIP push: iOS requires every
VoIP push to show a call.

**Device registration** (`POST /api/user/devices/register/{userId}`) takes two more optional fields:
`voipToken` (the iPhone's PushKit token; omit to leave it unchanged, `""` to clear) and `voipSandbox` (`true` when the token belongs to Apple's
sandbox server, i.e. an install from Xcode; the app reads it from its provisioning profile). A token that moves to another device is removed
from the old row. A token Apple reports dead is cleared automatically.

## Adding money to the wallet (Stage 7): Razorpay

The person says how much should land **in the wallet**; the gateway fee is added **on top** (grossed up, because Razorpay takes its percentage
of the whole payment; rounded up to the paisa). `fee = total - credit`, `total = ceil((credit + fixed) / (1 - percent))`. Defaults: 2.36%
(2% + 18% GST on it), ₹10 – ₹10,000 per recharge; all set by environment variables (see my_task.md Stage 7).

### `GET /wallet/recharge/config`
`{ "enabled": true, "minAmount": "10.00", "maxAmount": "10000.00", "feePercent": "2.36", "feeFixed": "0.00" }` (`enabled` is false until the Razorpay keys are set).
The app shows "You pay" from these; the order below is what is actually charged.

### `POST /wallet/{userId}/recharges` — make an order
`{ "amount": "500" }` → `201`
```json
{ "success": true, "recharge": {
  "id": "…", "orderId": "order_Nx…", "status": "created",
  "creditAmount": "500.00", "feeAmount": "12.09", "totalAmount": "512.09", "paidAt": null,
  "checkout": { "keyId": "rzp_…", "orderId": "order_Nx…", "amountPaise": 51209, "currency": "INR" } } }
```
Open Razorpay's checkout with `checkout`. Errors: `400 INVALID_AMOUNT` (`minAmount`, `maxAmount`), `403 WALLET_FROZEN`,
`429 TOO_MANY_RECHARGES` (10 unpaid orders in 10 minutes), `502 GATEWAY_ERROR`, `503 RECHARGE_UNAVAILABLE`.

### `POST /wallet/{userId}/recharges/confirm` — "I paid"
`{ "orderId": "…", "paymentId": "pay_…", "signature": "…" }` (what the checkout returned). The server verifies the signature, then asks Razorpay
about the payment itself (right order, right amount, captured; an authorized payment is captured first) and credits the wallet **once**.
Safe to repeat. Errors: `400 INVALID_SIGNATURE`, `404 RECHARGE_NOT_FOUND` (also someone else's order), `402 PAYMENT_FAILED`,
`409 PAYMENT_PENDING` (still processing: ask again shortly), `409 NEEDS_REVIEW` (paid, but it needs a person: amount mismatch or a frozen wallet),
`502 GATEWAY_ERROR` (retry).

### `GET /wallet/{userId}/recharges/{orderId}` — status
`recharge.status` is `created` | `paid` | `review`. Poll this when a confirmation could not be completed.

### `POST /webhooks/razorpay` — Razorpay tells the server
Public, authenticated only by Razorpay's `X-Razorpay-Signature` over the raw body. Handles `payment.captured` and `order.paid`, so a payment is credited
even if the app never confirmed it (lost signal, app killed). `400` for a bad signature, `503` if no webhook secret is set, `200` otherwise.
The credit is idempotent (ledger key `razorpay-<payment id>`), so the app's confirm, the webhook and repeats can all happen: it is credited once.

The wallet ledger row is a normal `credit` with `referenceType: "recharge"`, `referenceId: <recharge id>`; the note says what was paid and the fee.

