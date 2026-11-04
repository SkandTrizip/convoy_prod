# 1:1 on-demand call: the NEW flow (ADVANCE payment by direct UPI, no wallet)

Branch `feat/new-demand-call-flow`. The classic flow (wallet hold, "were you satisfied?") lives on the other branch and is described in `ONE_TO_ONE_CALL_API.md`. Same base URL and endpoints; what differs is below. Migrations `030_call_direct_payment.sql` and `031_advance_payment.sql`.

The requester pays the driver's UPI ID **before** the call, the driver confirms they got it, and only then can the call start.

## Status flow

```
pending -> accepted -> payment_claimed -> ready -> ringing -> live -> completed
           (waiting      (requester says   (driver
            for payment)  they paid)        confirmed)
```
Side exits: `rejected` (the driver rejected a pending request, or **withdrew** an accepted one before being paid: `closedReason: driver_withdrew`), `cancelled` (the requester, only until they say they paid), `expired` (a window ran out), `failed` (the call never connected at all: 0 s), `payment_disputed` (the driver said twice that the payment never arrived, or never answered for 24 h).

`closedReason` says why a request ended without a call or a payment: `driver_rejected`, `driver_withdrew`, `requester_cancelled`, `no_response` (pending, nobody answered), `payment_window_lapsed` (accepted, never paid), `start_window_lapsed` (paid and confirmed, never started), `provider_denied`, `provider_silent`, `ring_declined`, `call_not_connected`.

### Windows (`expiresAt` is always the deadline of the stage the request is in)

| Stage | Window | When it runs out |
|---|---|---|
| `pending` | `CALL_REQUEST_TTL_MINUTES` (30) from the request | `expired` / `no_response` |
| `accepted` | `CALL_PAY_WINDOW_MINUTES` (60) from the acceptance | `expired` / `payment_window_lapsed`, both told |
| `payment_claimed` | `CALL_CLAIM_CONFIRM_TTL_HOURS` (24) from the claim | `payment_disputed` / `provider_silent`, both told, for admin review (never closed quietly: the requester says they paid) |
| `ready` / `ringing` | `CALL_START_WINDOW_MINUTES` (30) from the driver's confirmation | `expired` / `start_window_lapsed`, the requester told |

## The steps

1. **Driver accepts** (`POST /{id}/accept`): the driver's UPI ID is copied onto the request (`payee_upi_id`: changing it later cannot redirect the payment). The requester gets push `call_request_accepted` ("pay now"). `409 PROVIDER_UPI_MISSING` if the driver has no UPI ID.
2. **Requester pays** with a UPI app (below), then says so: `POST /{id}/payment/paid` `{"upiReference": "..."?}` (the whole offer; `amountType`, if sent, must be `full`). Status `payment_claimed`, `paymentOutcome: claimed`. The driver gets push `call_payment`. Safe to retry. **The requester can no longer cancel** (`POST /{id}/cancel` is `409 INVALID_STATE`), and the driver can no longer withdraw.
3. **Driver answers** `POST /{id}/payment/confirm` `{"received": true|false, "sure": false}`:
   - `received: true` -> `ready`, `paymentOutcome: paid`, fresh 30-minute start window; the requester gets push `call_payment_confirmed`.
   - `received: false` **without** `sure: true` -> `409 CONFIRM_REQUIRED` ("Are you sure?") and nothing changes (the first no is kept in the behaviour log as `provider_denied_payment_unsure`). The app shows the question and sends the answer again.
   - `received: false, sure: true` -> `payment_disputed`, `paymentOutcome: disputed`, `closedReason: provider_denied`; both are told (push `call_payment_disputed`); the call never starts.
   - Answered once: the same answer again is harmless; anything else is `409 NO_PAYMENT_TO_CONFIRM`.
4. **Driver starts the call** (`POST /{id}/start`): only from `ready` (or `ringing` to retry a missed ring, which returns the request to `ready`). Before that: `409 PAYMENT_NOT_CONFIRMED`.
5. **Call ends**: under 1 s connected -> `failed`; otherwise `completed` (`thresholdMet` says whether it reached 30 s). Nothing is asked of the requester afterwards.

The driver can back out before being paid: `POST /{id}/withdraw` (only from `accepted`) -> `rejected`, `closedReason: driver_withdrew`, the requester told (push `call_request_rejected`).

## What changed in the requests

| | New flow |
|---|---|
| Creating a request | No wallet and nothing held. `409 PROVIDER_UPI_MISSING` if the driver has no UPI ID (profile page). `403 CALL_PAYMENTS_BLOCKED` after 3 strikes (being reworked: see Flagged below). |
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

Each payment: `id` (the call request id), `direction`, `counterparty {id, name, photo}` (the other person), `amount` (what was paid), `offerAmount`, `amountType` (always `full` now), `topic`, `durationMinutes`, `connectedSeconds`, `paidAt` (when the driver confirmed), `callEndedAt`, `upiId` (the UPI ID that was to be paid), `upiReference`, `providerConfirmed` (`true` on the driver's own side, always `null` for the requester).

There is no "mark as not received" in the ledger: the driver answers once, in the flow (see Paying).

## Flagged, strikes, block

**Being reworked (stage 2 of the advance-payment change).** Until then the old counters (`call_payment_standing`, `/payment-standing/me`, the admin `flagged` and `reset` endpoints) still exist but nothing in the new flow produces strikes. Planned: requester blocked after 3 payment claims the driver denied, or 3 accepted requests left unpaid; a driver whose paid requests were never called 3 times (or who denies claims unusually often) can no longer be requested; an admin list of disputes and refunds due.

## What is recorded for the later "who is lying" work

* `call_behaviour_events` as before, plus server events `payment_claimed` (amount, reference), `provider_confirmed_payment`, `provider_denied_payment_unsure` (the first no), `provider_denied_payment` (the sure no), `payment_unanswered` (the 24 h sweep), `request_withdrawn`, `call_completed` (with `thresholdMet`, `providerMediaOn`, mic/camera beat ratios), and client events `payment_sheet_opened`, `upi_app_opened`, `upi_app_returned`, `payment_confirm_sheet_opened`, `payment_confirm_sure_shown`.
* On `call_requests`: `provider_beats`, `provider_mic_beats`, `provider_camera_beats`, `requester_beats`, `requester_mic_beats`, `requester_camera_beats`, `requester_saw_video_beats` (one count per heartbeat, about every 5 s), `threshold_met`, `provider_media_on`, `payment_outcome`, `paid_amount`, `payment_claimed_at`, `provider_confirmed(_at)`, `disputed_at`, `closed_reason`, `payee_upi_id`, `upi_reference`.
* The mic/camera numbers are what each phone *says*; `requester_saw_video_beats` is the requester's own observation of the driver's video. Neither is proof, which is the point of keeping both.

## Database

Additive only (`migrations/030_call_direct_payment.sql`, then `031_advance_payment.sql`: three more statuses, three nullable columns and a wider open-pair unique index; it also closes any request an earlier build left in `awaiting_payment`), so the classic backend still runs on the same schema. A request left open by one flow can still confuse the other backend's sweep (the classic one would try to release a hold that was never taken), so the demo uses a **separate database** for this branch: `scripts/new_flow_db.sh`.
