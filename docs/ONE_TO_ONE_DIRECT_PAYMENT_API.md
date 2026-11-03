# 1:1 on-demand call: the NEW flow (direct UPI payment, no wallet)

Branch `feat/new-demand-call-flow`. The classic flow (wallet hold, "were you satisfied?") lives on the other branch and is described in `ONE_TO_ONE_CALL_API.md`. Same base URL and endpoints; what differs is below. Migration `030_call_direct_payment.sql`.

## Status flow

`pending -> accepted -> ringing -> live -> awaiting_payment -> completed`
Side exits: `rejected` (driver), `cancelled` (requester), `expired` (30 min window), `failed` (the call never connected at all: 0 s).
`completed` is the end of every payment story; `paymentOutcome` says which one: `paid`, `refused` (a call of 30 s or more, said no), `declined` (a short call, said no with a reason), `expired` (never answered, 24 h).

## What changed in the requests

| | New flow |
|---|---|
| Creating a request | No wallet and nothing held. `409 PROVIDER_UPI_MISSING` if the driver has no UPI ID (profile page). `403 CALL_PAYMENTS_BLOCKED` after 3 strikes. |
| `holdAmount` / `minimumAmount` | The least the requester must pay: max(25 % of the offer, Rs 25), at most the offer. Nothing is frozen. |
| Ending a call | Under 1 s connected: `failed`. Otherwise `awaiting_payment`, with `thresholdMet` = connected for 30 s or more. The requester gets push `call_payment_due`. |
| `GET /api/call-requests/{id}` | New fields: `thresholdMet`, `minimumAmount`, `paymentOutcome`, `paidAmount`, `paymentReason`, `paymentDecidedAt`, `providerConfirmed`, and `payment` (only for the requester, only while `awaiting_payment`): `{upiId, payeeName, fullAmount, minimumAmount, thresholdMet, deadline}`. |
| Heartbeat | Body also takes optional `micOn`, `cameraOn` (this phone's own) and `peerVideoOn` (is the other person's video showing). Counted per beat while both are in the call. |
| Wallet | `callCommitments` is always `"0.00"` and `spendableBalance` equals `availableBalance`; the whole balance can be withdrawn. Money never moves because of a call. |
| Removed | `POST /{id}/feedback`, `satisfied`, `settledAmount`. |

## Paying

The app opens a UPI app (`upi://pay?pa=<upiId>&pn=<name>&am=<amount>&cu=INR&tn=<note>`). The server cannot see that payment, so the app asks "Did you pay?" when the person comes back, then:

- `POST /api/call-requests/{id}/payment/paid` `{"amountType": "full" | "minimum", "upiReference": "..."?}` records the claim (`completed`, `paymentOutcome: paid`, `paidAmount`). The driver gets push `call_payment` ("please confirm"). Retrying the same answer is harmless; a different answer is `409 ALREADY_SETTLED`.
- `POST /api/call-requests/{id}/payment/decline` `{"reason": "..."?}`:
  - call of 30 s or more: a **refusal**. The reason is optional. The call is flagged; it is also a **strike** if the driver's mic and camera were on (below). The driver gets push `call_not_paid`.
  - shorter call: `400 REASON_REQUIRED` unless `reason` has 3+ characters. Outcome `declined`, not flagged, never a strike. The driver is told the reason (push `call_not_paid`).
- `POST /api/call-requests/{id}/payment/confirm` `{"received": true|false}` (driver, only after `paid`): only recorded (`providerConfirmed`); the driver may change their answer.
- Not answering within `CALL_FEEDBACK_TTL_HOURS` (24): the sweep closes it as `expired`, the same as a refusal for a call that counted.
- The driver's UPI ID is copied onto the request when the call ends (`payee_upi_id`), so changing it later cannot redirect a payment.

## Flagged, strikes, block

* **flagged**: a call of 30 s or more that ended `refused` or `expired`.
* **strike**: a flagged call where the driver's mic **and** camera were on for at least `CALL_MEDIA_ON_MIN_FRACTION` (0.5) of the beats they sent while both were in the call. What an app doesn't report counts as off.
* `CALL_PAYMENT_STRIKE_LIMIT` (3) strikes block `POST /api/call-requests` (`403 CALL_PAYMENTS_BLOCKED`). Calls that were paid, short, or whose driver media wasn't on never count.
* `GET /api/call-requests/payment-standing/me` -> `{flaggedCalls, strikes, strikeLimit, blocked, resetAt}`.
* Admin: `GET /api/admin/call-payments/flagged`; `POST /api/admin/call-payments/{user_id}/reset` forgives the strikes so far (flags stay on record; only newer strikes count).

## What is recorded for the later "who is lying" work

* `call_behaviour_events` as before, plus server events `payment_due` (with `thresholdMet`, `providerMediaOn`, mic/camera beat ratios), `payment_claimed`, `payment_refused`, `payment_declined`, `payment_expired`, `requester_flagged`, `provider_confirmed_payment`, `provider_denied_payment`, and client events `payment_sheet_opened`, `upi_app_opened`, `upi_app_returned`, `refusal_warning_shown`, `refusal_warning_dismissed`.
* On `call_requests`: `provider_beats`, `provider_mic_beats`, `provider_camera_beats`, `requester_beats`, `requester_mic_beats`, `requester_camera_beats`, `requester_saw_video_beats` (one count per heartbeat, about every 5 s), `threshold_met`, `provider_media_on`, `counts_as_strike`, `payment_*`, `upi_reference`, `provider_confirmed`.
* The mic/camera numbers are what each phone *says*; `requester_saw_video_beats` is the requester's own observation of the driver's video. Neither is proof, which is the point of keeping both.

## Database

Additive only (`migrations/030_call_direct_payment.sql`): new nullable/defaulted columns, a widened status CHECK and one table, so the classic backend still runs on the same schema. A request left open by one flow can still confuse the other backend's sweep (the classic one would try to release a hold that was never taken), so the demo uses a **separate database** for this branch: `scripts/new_flow_db.sh`.
