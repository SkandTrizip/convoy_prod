# 1:1 on-demand call: the NEW flow (ADVANCE payment by direct UPI, no wallet)

Branch `feat/new-demand-call-flow`. The classic flow (wallet hold, "were you satisfied?") lives on the other branch and is described in `ONE_TO_ONE_CALL_API.md`. Same base URL and endpoints; what differs is below. Migrations `030_call_direct_payment.sql`, `031_advance_payment.sql` and `032_call_settlement.sql`.

The requester pays the driver's UPI ID **before** the call, the driver confirms they got it, and only then can the call start.

## Status flow

```
pending -> accepted -> payment_claimed -> ready -> ringing -> live -> completed
           (waiting      (requester says   (driver
            for payment)  they paid)        confirmed)
```
Side exits: `rejected` (the driver rejected a pending request), `cancelled` (the requester, only until they say they paid), `expired` (a window ran out), `failed` (the call never connected at all: 0 s), `payment_disputed` (the driver said twice that the payment never arrived, or never answered for 24 h).

`closedReason` says why a request ended without a call or a payment: `driver_rejected`, `requester_cancelled`, `no_response` (pending, nobody answered), `payment_window_lapsed` (accepted, never paid), `start_window_lapsed` (paid and confirmed, never started), `provider_denied`, `provider_silent`, `ring_declined`, `call_not_connected`.

### Windows (`expiresAt` is always the deadline of the stage the request is in)

| Stage | Window | When it runs out |
|---|---|---|
| `pending` | `CALL_REQUEST_TTL_MINUTES` (30) from the request | `expired` / `no_response` |
| `accepted` | `CALL_PAY_WINDOW_MINUTES` (60) from the acceptance | `expired` / `payment_window_lapsed`, both told |
| `payment_claimed` | `CALL_CLAIM_CONFIRM_TTL_HOURS` (24) from the claim | `payment_disputed` / `provider_silent`, both told, for admin review (never closed quietly: the requester says they paid) |
| `ready` / `ringing` | `CALL_START_WINDOW_MINUTES` (30) from the driver's confirmation | `expired` / `start_window_lapsed`, the requester told |

## The steps

1. **Driver accepts** (`POST /{id}/accept`): the driver's UPI ID is copied onto the request (`payee_upi_id`: changing it later cannot redirect the payment). The requester gets push `call_request_accepted` ("pay now"). `409 PROVIDER_UPI_MISSING` if the driver has no UPI ID.
2. **Requester pays** with a UPI app (below), then says so: `POST /{id}/payment/paid` `{"upiReference": "..."?}` (the whole offer; `amountType`, if sent, must be `full`). Status `payment_claimed`, `paymentOutcome: claimed`. The driver gets push `call_payment`. Safe to retry. **The requester can no longer cancel** (`POST /{id}/cancel` is `409 INVALID_STATE`).
3. **Driver answers** `POST /{id}/payment/confirm` `{"received": true|false, "sure": false}`:
   - `received: true` -> `ready`, `paymentOutcome: paid`, fresh 30-minute start window; the requester gets push `call_payment_confirmed`.
   - `received: false` **without** `sure: true` -> `409 CONFIRM_REQUIRED` ("Are you sure?") and nothing changes (the first no is kept in the behaviour log as `provider_denied_payment_unsure`). The app shows the question and sends the answer again.
   - `received: false, sure: true` -> `payment_disputed`, `paymentOutcome: disputed`, `closedReason: provider_denied`; both are told (push `call_payment_disputed`); the call never starts.
   - Answered once: the same answer again is harmless; anything else is `409 NO_PAYMENT_TO_CONFIRM`.
4. **Driver starts the call** (`POST /{id}/start`): only from `ready` (or `ringing` to retry a missed ring, which returns the request to `ready`). Before that: `409 PAYMENT_NOT_CONFIRMED`.
5. **Call ends**: under 1 s connected -> `failed`; otherwise `completed` (`thresholdMet` says whether it reached 30 s). Nothing is asked of the requester afterwards.

**The driver cannot back out after accepting** (there is no withdraw endpoint): the requester may already be paying in their UPI app while the server still sees `accepted`, so a withdrawal could leave them paid with a closed request. The driver decides at `pending` (accept or reject); an accepted request nobody pays lapses by itself after `CALL_PAY_WINDOW_MINUTES` (`payment_window_lapsed`), and the requester can still cancel until they say they paid.

## What changed in the requests

| | New flow |
|---|---|
| Creating a request | No wallet and nothing held. `409 PROVIDER_UPI_MISSING` if the driver has no UPI ID (profile page). `403 CALL_PAYMENTS_BLOCKED` for a blocked requester and `409 PROVIDER_BLOCKED` for a blocked driver (see Strikes and blocks). |
| `GET /api/call-requests/{id}` | Fields: `closedReason`, `thresholdMet`, `paymentOutcome` (`claimed` / `paid` / `disputed`), `paidAmount`, `paymentClaimedAt`, `paymentDecidedAt`, `providerConfirmed`, and `payment` (only for the requester, only while `accepted`): `{upiId, payeeName, amount, deadline}`. |
| The driver's queue | `GET /incoming` holds every open stage that needs the driver, oldest first: `pending` (review), `accepted` (waiting for the requester's payment), `payment_claimed` (answer "did you receive it?"), `ready` (start the call), plus `ringing` / `live`. |
| `holdAmount` / `minimumAmount` | Still sent, no longer used: the whole offer is paid. |
| Heartbeat | Body also takes optional `micOn`, `cameraOn` (this phone's own) and `peerVideoOn` (is the other person's video showing). Counted per beat while both are in the call. |
| Wallet | `callCommitments` is always `"0.00"` and `spendableBalance` equals `availableBalance`; the whole balance can be withdrawn. Money never moves because of a call. |
| Removed | `POST /{id}/payment/decline`, the after-call `awaiting_payment` status, `call_payment_due` and `call_not_paid` pushes, `POST /{id}/feedback`. |

## The driver's UPI ID

* `PUT /api/user/upi/{user_id}` `{"upiId": "name@bank"}` adds or changes it (`400` if badly shaped; only your own id). Returns `{success, upiId}`.
* `GET /api/user/profile/{user_id}` (and the login response) now carry `upiId` (null until added). It is sent to the person themselves and to admins only; a requester sees a driver's UPI ID only inside `payment`, while a payment is due.
* The profile page has no wallet in this flow; the old `/api/wallet/...` endpoints are untouched (scratch-card rewards still use them).

## Paying

The app opens a UPI app (`upi://pay?pa=<upiId>&pn=<name>&am=<amount>&cu=INR&tn=<note>`). The server cannot see that payment, so the app asks "Did you pay?" when the person comes back and only then calls `payment/paid`. Everything the requester says is a claim, and the driver's answer is the second claim: neither is proof. That is why a denial is closed for admin review instead of settled automatically.

## Payments ledger (Profile > Transactions History page)

`GET /api/call-requests/payments?direction=received|sent&limit=20&offset=0` (limit 1 to 100): payments the driver confirmed receiving (`paymentOutcome: paid`), newest first. `received` = the caller was the driver, `sent` = the caller was the requester. Requests whose payment is still waiting, unconfirmed or disputed are not listed; a confirmed payment stays listed whatever became of the call afterwards.

Response: `{success, direction, payments: [...], count, total, hasMore}`. `count` and `total` (rupees, "225.00") cover every row of that direction, not only the page.

Each payment: `id` (the call request id), `direction`, `counterparty {id, name, photo}` (the other person), `amount` (what was paid), `offerAmount`, `amountType` (always `full` now), `topic`, `durationMinutes`, `connectedSeconds`, `paidAt` (when the driver confirmed), `callEndedAt`, `upiId` (the UPI ID that was to be paid), `upiReference`, `providerConfirmed` (`true` on the driver's own side, always `null` for the requester), `refundDue` (`true` while the driver owes the money back and the team hasn't resolved it; both sides see it). The app no longer reads `amountType`, `upiReference` or `providerConfirmed`.

There is no "mark as not received" in the ledger: the driver answers once, in the flow (see Paying).

## What becomes of the money (settlement)

UPI payments can't be reversed by us, so the server only decides **what the record says** and who has to act. When a request that had a payment ends, `settlement` / `settlementReason` are set (see `compute_settlement` in `services/call_requests.py`):

| Ended as | `settlement` | `settlementReason` | What happens |
|---|---|---|---|
| `completed`, call reached 30 s | `kept` | `call_completed` | The driver keeps it. |
| `completed`, under 30 s, the requester (or the connection) ended it, driver's mic and camera were on | `kept` | `short_call` | The driver keeps it. |
| `completed`, under 30 s, **the driver hung up first** | `review` | `short_call_ended_by_driver` | Admin looks (possible refund). |
| `completed`, under 30 s, the driver's mic or camera were not on | `review` | `short_call_driver_media_off` | Admin looks. |
| `failed` (went live, never really connected) | `review` | `call_not_connected` | Admin looks. |
| `expired`, the driver **never rang** | `refund_due` | `driver_never_called` | The driver must refund; both are told (push `call_refund_due` to the driver, `call_request_expired` to the requester). Counts as a driver no-show. |
| `expired`, the driver rang and the requester never answered | `kept` | `requester_no_answer` | The driver keeps it; the requester is told. |
| `cancelled` by the requester declining the ring | `kept` | `requester_declined_ring` | The driver keeps it. |
| `payment_disputed` | `review` | `provider_denied` / `provider_silent` | Admin looks. |

A request nobody paid for (cancelled, rejected, lapsed unpaid) has no settlement. `refundDue` (bool) is in `GET /api/call-requests/{id}` for both people while a `refund_due` has no admin verdict yet.

## Strikes and blocks

Counted from the requests every time (nothing stored to drift). `CALL_PAYMENT_STRIKE_LIMIT` is 3.

* **Requester blocked** (`403 CALL_PAYMENTS_BLOCKED` on `POST /api/call-requests`) at 3 **denied claims** (payments they said they made and the driver twice said never arrived) **or** 3 **unpaid lapses** (accepted requests that ran out of time unpaid). The two kinds are counted separately.
* **Driver blocked** (`409 PROVIDER_BLOCKED`: requesters can't request them) at 3 **no-shows** (`driver_never_called`).
* **Driver flagged** (not blocked) when they denied at least `CALL_DENIAL_FLAG_MIN` (3) payments and at least `CALL_DENIAL_FLAG_RATE` (50 %) of the claims they answered. A driver who never answers is counted (`unansweredClaims`) but never blamed: that is not the requester's strike either.
* `GET /api/call-requests/payment-standing/me` -> `{strikeLimit, resetAt, blocked, asRequester: {deniedClaims, unpaidLapses, blocked}, asDriver: {noShows, deniedClaims, answeredClaims, denialRate, flagged, unansweredClaims, blocked}}` (`blocked` at the top is the requester side).

### Admin (admin token)

* `GET /api/admin/call-payments/flagged`: everyone with something against them, worst first (same shape as the standing above plus `userId`, `name`, `mobile`). Left out once everything was forgiven or ruled in their favour.
* `GET /api/admin/call-payments/disputes?status=open|resolved|all&page=1&pageSize=20`: requests that need a person (`payment_disputed`, `refund_due`, `review`), newest first, each with both people (name, mobile), amounts, `payeeUpiId`, `upiReference`, ring attempts, connected seconds, the driver's mic/camera beat counts, every timestamp and the whole behaviour log.
* `POST /api/admin/call-payments/{request_id}/resolve` `{"resolution": "requester_right" | "driver_right" | "no_fault", "note": "..."}`: records the verdict (moves no money: the refund is chased outside the app). The last verdict stands. It decides whose record the request counts against: `no_fault` removes it from both people's counts, `requester_right` removes it from the requester's (a denial then counts against the driver, a no-show stays), `driver_right` removes it from the driver's (and a denial counts against the requester).
* `POST /api/admin/call-payments/{user_id}/reset`: forgives everything counted so far, as requester and as driver. Only what happens after it counts.

## What is recorded for the later "who is lying" work

* `call_behaviour_events` as before, plus server events `payment_claimed` (amount, reference), `provider_confirmed_payment`, `provider_denied_payment_unsure` (the first no), `provider_denied_payment` (the sure no), `payment_unanswered` (the 24 h sweep), `call_completed` (with `thresholdMet`, `providerMediaOn`, mic/camera beat ratios), and client events `payment_sheet_opened`, `upi_app_opened`, `upi_app_returned`, `payment_confirm_sheet_opened`, `payment_confirm_sure_shown`.
* On `call_requests`: `provider_beats`, `provider_mic_beats`, `provider_camera_beats`, `requester_beats`, `requester_mic_beats`, `requester_camera_beats`, `requester_saw_video_beats` (one count per heartbeat, about every 5 s), `threshold_met`, `provider_media_on`, `payment_outcome`, `paid_amount`, `payment_claimed_at`, `provider_confirmed(_at)`, `disputed_at`, `closed_reason`, `settlement`, `settlement_reason`, `resolution`, `resolved_at/by`, `resolution_note`, `payee_upi_id`, `upi_reference`.
* The mic/camera numbers are what each phone *says*; `requester_saw_video_beats` is the requester's own observation of the driver's video. Neither is proof, which is the point of keeping both.

## What the app does with it (convoynew, same branch)

* **Requester:** the pay sheet opens by itself after `call_request_accepted` while `status == accepted` and `expiresAt` is in the future. It has Pay (the whole offer) and Cancel request, and can't be dismissed. After Yes it calls `payment/paid`. The request status sheet has a text and buttons for every status; no Cancel once `payment_claimed`.
* **Driver:** Home banner and request sheet follow `accepted` (waiting for payment, nothing to do until the requester pays), `payment_claimed` (**Confirm payment** -> `payment/confirm`, the second No sends `sure: true`) and `ready` (**Start call** is only offered here, and the app still blocks starting while another call is ringing or live). Pushes open the same questions: `call_payment`, `call_payment_confirmed`, `call_payment_disputed`, `call_refund_due`.
* **Ledger page:** lists the confirmed payments of both tabs; a line with `refundDue` gets a red note. The app ignores `amountType`, `upiReference` and `providerConfirmed`.

## Open points

* UPI payments are self-reported (see Paying). Reading the UPI app's result on Android and a gateway (Razorpay Route VPA settlements) are possible later steps, neither is built.
* The app opens "Did the payment go through?" as soon as the UPI app chooser opens (before the payment). Not changed on purpose for now.
* Refunds are not moved by the system: `refundDue` and the admin disputes list tell a person whom to chase.

## Database

Additive only (`migrations/030_call_direct_payment.sql`, then `031_advance_payment.sql`: three more statuses, three nullable columns and a wider open-pair unique index; it also closes any request an earlier build left in `awaiting_payment`), so the classic backend still runs on the same schema. `032_call_settlement.sql` adds six nullable columns and one partial index. A request left open by one flow can still confuse the other backend's sweep (the classic one would try to release a hold that was never taken), so the demo uses a **separate database** for this branch: `scripts/new_flow_db.sh`.
