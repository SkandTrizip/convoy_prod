# Convoy — Project Memory

Analysis snapshot: 2026-07-14. FastAPI backend for "Convoy" — a truck discovery & availability
marketplace (truck owners post availability, shippers search/book). ~3,900 lines of Python across
`routers/` + `services/`.

## Stack

- FastAPI + Uvicorn, Python 3.14 (venv at `.venv/`)
- PostgreSQL + PostGIS via SQLAlchemy 2.0 async (`asyncpg`), tables auto-created on startup
  (`db/session.py::init_db`) — no Alembic; `migrations/*.sql` are reference copies of the same DDL,
  applied manually, not run automatically.
- Auth: mobile OTP login → JWT bearer (HS256, `PyJWT`), `JWT_EXPIRE_HOURS=168` (7 days) by default.
- Deployed on an Azure VM via systemd (`deploy/convoy-api.service`, port **5050** in prod, path
  `/home/azureuser/convoy_prod`), pushed via GitHub Actions on `main`. Locally we run on **port 8000**.

## Architecture

- `app.py` — FastAPI app, lifespan hook calls `init_db()` then spawns a background asyncio task
  `run_post_expiry_loop()` that expires stale truck posts every `POST_EXPIRE_CHECK_INTERVAL_SECONDS`
  (default 300s). CORS wide open (`allow_origins=["*"]`). All non-auth/misc routes get
  `BearerAuth` security requirement injected into the OpenAPI schema in `_configure_openapi_security`.
- `routers/__init__.py` — mounts `auth` + `misc` as public; everything else behind
  `Depends(get_current_user)` via a `protected_router`.
- `middleware/auth.py` — JWT decode, loads `User` from DB, rejects suspended accounts (403).
  `require_path_user` dependency enforces the path's `user_id` matches the token subject (403 if not).
- `db/base.py` — SQLAlchemy models: `User`, `UserContact`, `Location`, `KYCRecord`, `Truck`,
  `TruckRoute`, `Booking`, `SearchDemand`, `Notification`, `CallLog`, `LoginOTP`.
- `db/serializers.py` — hand-written `*_to_dict` converters (camelCase API shape from snake_case
  columns). `truck_route_to_dict` computes `isExpired`/`status` live via `services/post_expiry`.

## Domain flow

1. **Auth** (`routers/auth.py`): `POST /api/auth/send-otp` generates 4-digit OTP, stores in
   `login_otps`, sends via Veup SMS (`services/sms.py`). `POST /api/auth/verify-otp` checks OTP,
   creates `User` row if new (mobile is the identity), returns JWT.
2. **KYC** (`routers/kyc.py` + `services/kyc.py`): Aadhaar via Cashfree — either OTP flow
   (`send-otp`/`verify` hitting Cashfree offline-Aadhaar OTP) or Smart OCR (`ocr` endpoint, front+back
   image upload, cross-checks UID match + fraud checks). Approval sets `User.kyc_status=approved` and
   fires an Expo push notification. `POST /kyc/submit/{user_id}` explicitly rejects
   `method=="digilocker"` (400) — only Aadhaar OTP/OCR paths are supported now.
3. **Vehicles** (`routers/vehicles.py`): requires KYC approved. Adding a vehicle calls ULIP VAHAN
   (`services/ulip.py`) to verify registration.
4. **Verification** (`routers/verification.py`): standalone vehicle/DL/batch verification via ULIP
   VAHAN/SARATHI, independent of the vehicle-add flow, writes into `User.verification_status` JSONB.
5. **Posts** (`routers/posts.py`): a truck owner posts an availability listing (`TruckRoute`) with a
   single PostGIS origin point and **1–5 destinations** (see "Multi-destination posts" below).
   Auto-expires after `POST_EXPIRE_HOURS` (24h default); `reactivate` extends an *expired* post
   another 24h (400 if still active); `edit` also resets expiry.
6. **Search** (`routers/search.py`, `services/spatial.py`): PostGIS `ST_DWithin` radius search on
   origin, and on **any of a post's destinations** (default 150km, capped 500km), filtered by truck
   type and available_date. If no results, client can call `track-demand` to persist a `SearchDemand`
   (48h TTL) for smart-match notifications later.
7. **Smart match** (`services/matching.py`): checks unexpired `SearchDemand` rows of the same truck
   type within 100km origin / 250km destination (Haversine, not PostGIS) and pushes a notification.
   **Currently disabled** — see "Multi-destination posts" below.
8. **Contacts** (`services/contacts.py`): privacy-preserving mutual-contacts feature — client hashes
   phone-book numbers (SHA256 hex) and syncs; server never sees raw numbers. Mutuals computed via
   self-join on `hashed_number`. Listings get an attached `mutuals: {count, names}` preview.
9. **Bookings** (`routers/bookings.py`): minimal — books a `TruckRoute`, flips its status to `booked`.
   No payment integration despite Cashfree creds present (those are only used for KYC OCR/Aadhaar).
10. **Admin** (`routers/admin.py`): **no auth check beyond the global bearer requirement** — any
    logged-in user's JWT can hit `/api/admin/*` (KYC approve/reject, analytics). There's no
    role/is_admin field on `User` at all.

## External integrations & dev-mode fallbacks

Every third-party integration has a "not configured → dev mode" fallback, so the whole flow works
end-to-end without real credentials:

| Service | Config flag | Dev-mode behavior |
|---|---|---|
| Veup SMS | `VEUP_PROCESS_KEY` | Returns OTP in the API JSON response instead of texting it |
| Google Places | `GOOGLE_PLACES_API_KEY` | Location autosuggest falls back to DB-only search |
| Cashfree (KYC) | `CASHFREE_CLIENT_ID/SECRET` | Mock Smart OCR response; Aadhaar OTP fixed to `111000` |
| ULIP (VAHAN/SARATHI) | `ULIP_USERNAME/PASSWORD` | Vehicle/DL always returns verified=true, mock data |

`.env` currently has `VEUP_PROCESS_KEY` **uncommented** (real SMS sends), everything else is live too
(`CASHFREE_ENVIRONMENT=production`, real ULIP creds) — **this local dev setup talks to real prod
services and the prod Azure Postgres DB** (`tmsbackend.postgres.database.azure.com/convoy_new`), not
a sandbox. Be careful with data-mutating test calls.

## Known quirks / rough edges

- **Inconsistent UUID error handling**: `parse_uuid()` raises bare `ValueError` on malformed UUIDs.
  `verification.py` and `contacts.py` explicitly catch `ValueError` → 400. `posts.py`, `vehicles.py`,
  `bookings.py`, `users.py`, `kyc.py` do **not** catch it, so a bad UUID falls through to the generic
  `except Exception` → 500 "badly formed hexadecimal UUID string" (reproduced in logs on 2026-07-14
  10:23 hitting `POST /api/posts/create/{bad_id}`). Still unfixed — only flagged so far.
- No payment/booking-confirmation integration despite Cashfree keys being for a *different* product
  (KYC verification), not payments.
- `routers/admin.py` has zero authorization beyond "any valid JWT" — no admin role check exists in
  the data model.
- Tests (`tests/backend_test.py`) point at a hardcoded preview URL
  (`ride-match-trucks.preview.emergentagent.com`) by default, not localhost — must override
  `EXPO_PUBLIC_BACKEND_URL` to run against local/prod directly.

## Current local dev state (this session)

- `.env` created from `envconvoy` (both gitignored).
- `.venv` created, `requirements.txt` installed.
- Server running via `uvicorn app:app --reload` on port 8000 (background process, log at
  `/tmp/convoy_prod_server.log`). `.claude/launch.json` configured to match (port 8000).
- Confirmed working end-to-end manually: `send-otp` (real Veup SMS now that key is uncommented),
  `verify-otp`, vehicle add, post create/search, smart-match notifications.

## Cleanup done this session (no frontend is wired up yet, confirmed by user — breaking changes safe)

- Removed `routers/trucks.py` (the legacy/duplicate truck-listing API — Postman collection itself
  labeled it "alternate routes API"; it never got the smart-match trigger `posts.py` has, and hadn't
  been touched since before `posts.py` gained its `edit` endpoint). Un-mounted it from
  `routers/__init__.py` and dropped the now-unused `CreateTruckRequest`/`TruckSearchRequest` models
  from `models.py`. Removed its folder from `Convoy_API.postman_collection.json` too.
- Removed `services/vahan.py` (confirmed dead — nothing imported it; `services/ulip.py` is what's
  actually used for VAHAN vehicle verification).
- Verified app still imports and boots cleanly, and `/api/trucks/*` now correctly 404s.
- Still open / not yet fixed: the UUID error-handling inconsistency and the missing admin role check
  (see above) — flagged but not addressed.

## Multi-destination posts (added this session)

A `TruckRoute` post now supports **1–5 destinations** instead of one. Search matches if **any** of a
post's destinations is within the shipper's search radius.

- New table `truck_route_destinations` (`db/base.py::TruckRouteDestination`) — `truck_route_id` FK
  (CASCADE delete), `position` (1-5, unique per route, checked), its own GiST-indexed
  `destination_location`. Follows this repo's existing convention of **no SQLAlchemy
  `relationship()`** — everything is plain `select()`/`delete()` via `services/destinations.py`
  (`create_destinations`, `replace_destinations`, `get_destinations_for_route(s)`,
  `destination_to_dict`), mirroring `services/contacts.py`'s batch-query style.
- `TruckRoute` no longer has `destination_name`/`destination_location`/`destination` columns —
  clean cutover, not a dual-write period (safe since no frontend is attached yet).
- `CreateTruckPostRequest.destinations` / `EditTruckPostRequest.destinations` (full-replace
  semantics on edit) — `min_length=1, max_length=5` in `models.py`. `SearchTrucksRequest.destination`
  stays **singular** — that's the shipper's one search target, not the truck's list.
- `services/spatial.py::search_truck_routes_spatial` rewritten: `JOIN truck_route_destinations`,
  `ST_DWithin` on both origin and destination, de-duped to one row per route via
  `DISTINCT ON (tr.id)` ordered by nearest matching destination, wrapped in a subquery for global
  distance ordering. Response includes the full `destinations` list plus a `matchedDestination`
  (which one matched + its distance). The `include_user_info` param was dropped (only one caller
  remained after `trucks.py`'s removal, always passing `True`).
- **Smart-match notifications are intentionally disabled** this phase (`process_smart_match_notifications`
  calls commented out in `routers/posts.py`, `services/matching.py` untouched) — its Haversine logic
  still assumes a single destination and needs multi-destination support before re-enabling. Revisit
  in a later phase.
- Migration: `migrations/005_multi_destination_posts.sql` (create table, backfill, drop old columns).

### Incident: backfill data loss during migration (2026-07-14)

While running this migration by hand, an ad-hoc Python statement-splitter (splitting the file on `;`
then filtering out chunks starting with `--`) incorrectly discarded the `CREATE TABLE` and backfill
`INSERT` statements — both happened to be preceded by a comment line, so the whole chunk got dropped
by the filter — while the final `ALTER TABLE ... DROP COLUMN` still ran. Net effect: the old
destination columns were dropped from `truck_routes` **without their data being copied into the new
table first**. Confirmed scope: 9 `truck_routes` rows (8 already-`expired` + 1 `active`, all reusing
known test vehicle numbers `UP70BN8850`/`HR16X4803`/`MH12AB1234` from prior test sessions) lost their
destination name/coordinates; `truck_route_destinations` was empty for all of them afterward. User
reviewed and confirmed this was acceptable (test data, not real customer submissions) rather than
pursuing Azure point-in-time restore.

**Why this matters going forward:** when running raw multi-statement `.sql` migration files by hand
via asyncpg (which can't execute multiple commands in one prepared statement), split on statement
boundaries carefully — don't filter chunks by whether they merely *start* with a comment line, since
a leading comment doesn't mean the whole chunk is a comment. Safer: strip `--` comment lines from
each chunk *before* checking if anything meaningful remains, or use a real SQL statement splitter.
Also: [[project-prod-env]] applies doubly hard to schema migrations, not just data-mutating API
calls — get explicit user sign-off before any `DROP COLUMN`/`DROP TABLE` step specifically, even if
the overall migration was pre-approved in a plan.

### Test artifacts left in the DB from this session's manual verification

A KYC-approved test user (mobile `+919876500123`) and a verified test truck (`TEST9999`) were created
directly via SQL to work around live Cashfree/ULIP APIs rejecting calls from this network (IP not
whitelisted) — inserting fixture rows was explicitly approved by the user after an initial
env-var-override approach was rejected in favor of direct SQL. The test post itself was deleted via
the real `DELETE /api/posts/delete/{id}` endpoint after verification; the test user and truck rows
were left in place (consistent with the ~9 other lingering test rows already in this DB).

## Search pagination (added this session)

`POST /api/search/trucks` is now paginated: fixed `SEARCH_PAGE_SIZE = 10` (not client-adjustable),
requested via a `page` field (1-indexed, `ge=1`) on `SearchTrucksRequest` in `models.py`. Response
adds `page`, `pageSize`, `totalCount`, `totalPages` alongside the existing `posts` array (now ≤10
items instead of up to 1000).

`services/spatial.py::search_truck_routes_spatial` now returns `(matches, total_count)` instead of
just a list — the distance-ranked/deduped subquery (`ranked_matches`) is shared as a SQL fragment
between a `COUNT(*)` query and an `OFFSET/LIMIT` page query, so both agree on the same underlying
result set. `total_count` is still capped at the pre-existing `SEARCH_MAX_MATCHES = 1000` safety
ceiling — pagination didn't remove that cap, just paginates within it. Requesting a page beyond the
last one returns an empty `posts` array with correct `totalCount`/`totalPages` still populated (uses
a separate count query rather than a window function, specifically to avoid the empty-result-set
gap a `COUNT(*) OVER()` window function would have on out-of-range pages).

Side benefit: `attach_mutuals_to_listings` and the per-row user lookup in `search_truck_routes_spatial`
now only run against ≤10 rows per request instead of up to 1000 — meaningful reduction in per-search
DB round-trips.

## Search capacity filter (added this session)

`POST /api/search/trucks` now takes an optional `capacity` field (`SearchTrucksRequest.capacity`,
`ge=0`) — minimum required capacity in tonnes. Omitted/null → no filter (all capacities shown).
Set → only posts with `truck_routes.capacity >= capacity` are returned. Implemented as a plain
`AND tr.capacity >= :min_capacity` clause in `services/spatial.py`'s shared `ranked_matches` SQL
fragment (`search_truck_routes_spatial(..., min_capacity=...)`), so it applies before both the count
and page queries. NULL-capacity trucks are naturally excluded whenever a minimum is requested (`NULL
>= x` is never true in SQL) — intentional, since an unspecified capacity can't be confirmed to meet
a stated requirement. `capacity`/`expiresAt` were already present in the search response from the
multi-destination/pagination work earlier this session — no response-shape change was needed for
those, just this new request-side filter.

Note: two prior asks this session ("available_date should become an auto-computed available_till",
then "pass truckType/capacity on post-create") were dropped/simplified by the user across follow-up
messages down to just this capacity filter — the user pushed back on clarifying-question batches for
requests that turned out to be simple once actually checked against the running code. Lesson: for
"does field X already show up in response Y" style asks, verify directly against a live response
first before asking design questions — only ask when a change is genuinely behavior-ambiguous.

## Search: origin/destination now independently optional (added this session)

`POST /api/search/trucks` no longer requires both `origin` and `destination` — at least one is
required (`SearchTrucksRequest` gained a `model_validator` enforcing this; both are now
`Optional[Location]`). Confirmed behavior (asked and confirmed with the user, not assumed):

- Origin-only: returns posts with origin in radius, no destination filtering at all (truck can be
  headed anywhere). Response omits `destination_distance_km`/`matchedDestination` entirely.
- Destination-only: returns posts where **any** of the route's destinations is in radius, no origin
  filtering. Response omits `origin_distance_km`.
- Both given: unchanged existing behavior (both filters apply, ranked by combined distance).
- Neither given: rejected with a 422 validation error.

`services/spatial.py::search_truck_routes_spatial`'s `origin_lat/lng`/`destination_lat/lng` params
are now all `Optional[float] = None`. The JOIN with `truck_route_destinations` is kept unconditional
even for origin-only searches (needed only to dedupe a route's up to 5 destination rows back to one
row via `DISTINCT ON (tr.id)`; `d.position` is a secondary tiebreaker so that case deterministically
keeps destination #1 rather than an arbitrary one). The SQL `WHERE`/`ORDER BY` clauses are built
conditionally based on which side(s) are present (`origin_filter`/`dest_filter`/`rank_expr`).

**Gotcha hit while implementing:** writing `:param::text` (Postgres cast shorthand) directly adjacent
to a SQLAlchemy `text()` bind parameter causes a syntax error — SQLAlchemy's parameter parser doesn't
handle `::` immediately following `:name`. Use `CAST(:param AS text)` instead when a bind param needs
an explicit type (needed here because `origin_point`/`dest_point` are sometimes `None`, and a param
that's only ever used inside a function call — `ST_GeogFromText(...)` — can otherwise fail with
"could not determine data type of parameter").

**Side-effect fixed in the same change:** `POST /search/track-demand/{user_id}` shares
`SearchTrucksRequest` as its body model. Since that endpoint directly accesses `.origin.name`/
`.destination.model_dump()` unconditionally (a tracked search demand needs a full route to be
meaningful), it would have crashed on a one-sided payload once origin/destination became optional.
Added an explicit `400` guard at the top of that endpoint requiring both — and had to add a missing
`except HTTPException: raise` above its generic `except Exception` handler, otherwise that 400 would
have been swallowed and re-raised as a 500 (that endpoint didn't have the guard other routers do).

## Search: truckType now optional too (added this session)

`SearchTrucksRequest.truckType` changed from required to `Optional[str] = None` — omitted means no
truck-type filter (all types shown), same "empty = show all" semantics `capacity` already had. The
service layer (`search_truck_routes_spatial`) already handled `truck_type=None` correctly before this
change (its `truck_type_filter` was already conditional), so no service-layer change was needed —
only the Pydantic field and one more guard.

Same `track-demand` gotcha as origin/destination: `SearchDemand.truck_type` is a `NOT NULL` column,
so `POST /search/track-demand/{user_id}` needed `truckType` added to its existing "required fields"
guard (now checks origin, destination, **and** truckType are all present) to avoid a DB constraint
violation (would otherwise 500) instead of a clean 400.

## Admin: delete-user endpoint (added this session)

`DELETE /api/admin/users/delete/{user_id}` — permanently (hard) deletes a user and everything tied
to them. Decisions confirmed with the user before building (not assumed, given how destructive/
irreversible this is):

- **Admin auth**: added a new `X-Admin-Key` header gate (`routers/admin.py::require_admin_key`),
  checked against `ADMIN_API_KEY` in `.env`/`config.py`. This applies **only to this new endpoint**,
  not retroactively to the rest of `/api/admin/*` (those still have the pre-existing gap — any valid
  JWT works, no real `is_admin`/role field exists anywhere in the data model; flagged earlier this
  session, not fixed). Still requires a normal user JWT too (router-level `get_current_user`
  dependency already applies) — the admin key is additive, not a replacement.
- **Hard delete**, not soft-delete/anonymize — rows are actually gone.
- **Scope**: truck posts (`TruckRoute`, cascades to `TruckRouteDestination` via existing FK),
  registered vehicles (`Truck`), KYC record, their own bookings, **other users' bookings on their
  posts** (via a `truck_route_id IN (their route ids)` subquery — these have no FK either, so they'd
  otherwise be orphaned pointing at a deleted route), search-demand tracking, notifications sent to
  them, call logs, synced contacts (cascades via existing FK), and pending login OTPs (matched by
  mobile, since `LoginOTP` has no `user_id` at all).
- **No blocking on active/priced bookings** — deletes regardless of booking state, per explicit
  instruction.

New `services/user_deletion.py::delete_user_cascade(session, user)` does the actual work — plain
`delete()` statements per table (this schema has almost no real FKs to `users`/`truck_routes` except
`UserContact` and `TruckRouteDestination`, confirmed by re-checking `db/base.py` before writing this,
so nothing cascades automatically besides those two). Returns a dict of per-table deleted counts,
which the endpoint echoes back in the response and also logs at `WARNING` level (`user_id`, mobile,
counts) — this is exactly the kind of action worth an audit trail even without a full admin-log table.

Verified end-to-end with a full fixture (own booking, a booking from another user on their post,
KYC record, vehicle, post + destination, notification, call log, search demand, login OTP) — all 10
categories confirmed present before, all confirmed gone after, exact counts matched. Also verified:
missing/wrong `X-Admin-Key` → 403, invalid UUID → 400, nonexistent user → 404.

## Per-post contact info (added this session)

Truck posts now carry their own `contact_name`/`contact_number` (new nullable columns on
`TruckRoute`, `migrations/006_post_contact_info.sql` — purely additive `ADD COLUMN`, no data loss
risk, unlike the earlier migration incident). `CreateTruckPostRequest` accepts optional
`contactName`/`contactNumber`; if omitted, `routers/posts.py::create_truck_post` defaults them to
the posting user's own `name`/`mobile` **at creation time** (materialized into the row, not looked
up dynamically on every read — consistent with how `truck_type`/`capacity` are already copied from
`Truck` onto `TruckRoute` at creation rather than joined live). `EditTruckPostRequest` can override
either field afterward.

**Design decision (discussed with the user, not assumed):** the search response gets `contactName`/
`contactNumber` as **new, separate fields** alongside the existing `userName`/`userMobile` — it does
NOT replace them. Reasoning: `services/contacts.py`'s mutual-contacts feature computes "mutuals"
against the post owner's *real* `userId`/phone book. If `userMobile` were replaced by an overridable
contact number, a shipper could see "2 mutual contacts" attached to a phone number that isn't
actually the person those mutuals belong to. Keeping `userName`/`userMobile` as the true account
identity (accountability + mutuals stay consistent) while `contactName`/`contactNumber` represents
"who to actually call for this listing" (often the same person, sometimes a driver) preserves both
concepts without confusion.

Verified end-to-end: post created without override → `contactName`/`contactNumber` default to the
user's own profile values; post created with override (e.g. a driver's number) → override values
stored and returned; edit updates them; search response showed both `userName`/`userMobile` (owner)
and `contactName`/`contactNumber` (driver override) simultaneously and correctly on the same result.

## Recent activity: last 10 searches + last 10 posts (added this session)

New `GET /api/history?type=search|post` (`routers/activity.py`) — always "my own" history, no
`user_id` in the URL, uses `get_current_user` directly (confirmed with the user: this is inherently
personal data, no case for viewing someone else's). `type` is required, validated via FastAPI's
`Query(..., pattern="^(search|post)$")` → 422 if missing/invalid.

Backed by a new `user_activity` table (`db/base.py::UserActivity`,
`migrations/007_user_activity.sql`, purely additive):
- `type="post"` rows are a **live reference** (`truck_route_id`, `ON DELETE CASCADE`) — confirmed
  with the user over a live-reference-vs-frozen-snapshot question. Read-time join to `TruckRoute` +
  `services/destinations.py::get_destinations_for_routes` means the response always reflects current
  state (status/expiry), and a row disappears on its own once the underlying post is deleted (cascade,
  no extra code needed — verified: deleting a post live-removed it from the history response).
- `type="search"` rows store the **search criteria used** (`search_request.model_dump(mode="json",
  exclude={"page"})` — `page` excluded since it's pagination state, not "what was searched for"), not
  the results (results go stale as posts expire/get created).
- **Dedup** (confirmed with the user): a search identical to the user's immediately preceding search
  entry just bumps that entry's `created_at` instead of adding a duplicate row — verified live,
  running the same search twice left exactly one entry with the later timestamp.
- **Trimmed to the last 10** of each type on every write (`services/activity.py::_trim_to_last_n`,
  a `DELETE ... WHERE id NOT IN (SELECT ... ORDER BY created_at DESC LIMIT 10)`) — verified by
  creating 12 posts and confirming exactly the most recent 10 remained, both via the API response
  and a direct DB count.
- Both `record_post_activity`/`record_search_activity` are **best-effort / never raise** (catch,
  rollback, log) — matches the existing fire-and-forget pattern `send_expo_push_notification`
  already uses elsewhere, so a logging failure can never break the actual search or post-creation
  request.

Also updated `services/user_deletion.py`'s cascade (from the earlier admin delete-user feature) to
explicitly delete remaining `UserActivity` rows for a deleted user — `post`-type rows already
disappear automatically once their `truck_routes` rows are deleted (cascade), but `search`-type rows
have no such FK and needed an explicit cleanup step to avoid leaving them orphaned.

## Search sorting: nearest_origin / nearest_destination (added this session)

`SearchTrucksRequest.sortBy` — optional `Literal["nearest_origin", "nearest_destination"]`. Omitted
keeps the pre-existing default ranking (combined origin+destination distance when both given,
whichever single one exists otherwise). `services/spatial.py::search_truck_routes_spatial` gained a
`sort_by` param; the `rank_expr` selection now checks `sort_by` first, falling through to the
original default logic if the requested side wasn't actually part of the search — e.g.
`sortBy=nearest_origin` with no `origin` given doesn't error, it just silently uses
`destination_distance_km` instead (per explicit instruction: "does not fail, gracefully handle it").

Verified live: two posts with deliberately different origin/destination distances (Delhi-origin/Pune-
destination vs Jaipur-origin/Mumbai-destination) — default sort favored the lower combined distance;
`sortBy=nearest_origin` and `sortBy=nearest_destination` each produced the correct, different
ordering (confirmed the ranking actually flips, not just accepted); destination-only search with
`sortBy=nearest_origin` returned `200` with results correctly ordered by destination distance instead
of erroring; invalid `sortBy` value correctly rejected with `422` (Pydantic `Literal` validation).

Also noted: two earlier messages this same turn described a **frontend** feature (location-permission
prompts on a "post page"/"search page" with "go to settings" buttons) — both were interrupted by the
user before any plan was made, so nothing was implemented for them. This project has no frontend yet
([[project-dev-stage]]), which likely explains the self-interruption.

## Admin: removed manual KYC review endpoints (added this session)

Manual KYC review via the admin panel is gone — the user confirmed KYC is now verified through the
Aadhaar OTP flow only (`routers/kyc.py`'s `send-otp`/`verify` endpoints, `services/kyc.py`). Removed
`GET /api/admin/kyc/pending`, `POST /api/admin/kyc/action`, and the now-unused `AdminKYCAction` model
from `routers/admin.py`/`models.py`.

**Investigated and confirmed with the user before removing, not assumed:** the KYC submission flow
still has a secondary path — `POST /kyc/submit/{user_id}` (Aadhaar Smart OCR via Cashfree) — that can
land a `KYCRecord` in `status="under_review"` if OCR is inconclusive (`services/kyc.py`'s comment
literally says "Manual KYC requires admin review"). Removing the admin review endpoints entirely means
any record that lands there now has **no resolution path at all**. Asked explicitly whether to keep
the endpoints reachable (e.g. via Swagger) as a manual safety net, or remove them outright accepting
that a stuck `under_review` record is a non-issue going forward — user chose full removal. `kyc_to_dict`
in `db/serializers.py` was kept (still used by `routers/kyc.py` itself for OTP/OCR responses) — only
the admin-facing endpoints and model were deleted.

## Admin: paginated user list + management API (added this session)

- `GET /api/admin/users` — `page`/`pageSize`/`search` (search matches `mobile` or `name` via `ILIKE`,
  or an exact user-ID match if the search term parses as a UUID).
- `GET /api/admin/users/{id}/profile-photo` — a new admin-JWT-authenticated proxy streaming a user's
  photo from Azure blob storage. Needed because the existing `GET /api/user/profile-photo/{id}`
  requires a **driver** JWT (`get_current_user`) — an admin token can't call it at all, since admin and
  driver JWTs are separate namespaces on separate secrets (`middleware/admin_auth.py` vs
  `middleware/auth.py`). A new `_admin_user_to_dict()` in `routers/admin.py` rewrites `profilePhoto` to
  point at this admin-scoped proxy instead of reusing `db/serializers.py::profile_photo_url()`.
- `PUT /api/admin/users/{id}` (rename only, new `AdminUpdateUserRequest` model) and
  `POST /api/admin/users/{id}/profile-photo` (multipart upload) — deliberately narrow. Mobile number
  stays locked (it's the OTP-verified identity anchor + a unique constraint), and neither endpoint
  touches `kyc_status`/`kyc_step` — unlike the driver-facing `POST /api/user/profile-photo/{id}`, which
  marks KYC complete as a side effect when `kyc_step == "photo"`. Conscious choice to keep the admin
  edit path a pure data-fix tool, fully separate from the KYC completion flow.
- `POST /api/admin/users/{id}/suspend` and `/reactivate` — toggles `User.account_status` between
  `"active"`/`"suspended"`. This mechanism already existed in the data model and was already enforced
  by `middleware/auth.py` (403 on a suspended account's own JWT calls) — there just wasn't any API to
  trigger it. Confirmed via code investigation before building: suspending does **not** block a
  server-initiated FCM push (that's a direct server→Firebase call using the stored `push_token`, not
  gated by the user's own auth token at all) — it only blocks the user's own subsequent API calls.

## Campaign Management System (added this session)

Large feature request: a marketing-team-facing Campaign Management System (create/schedule/manage/
monitor push-notification campaigns without backend code changes). **Reviewed critically before
building anything**, per explicit instruction — the codebase already had a hardcoded campaign system
(`notifications/registry.py` + `notifications/campaigns/` + `notifications/jobs/` +
`notifications/audiences/` + `notifications/templates/`, three cron-triggered slots
`"morning"`/`"afternoon"`/`"night"` via APScheduler in `notifications/scheduler/__init__.py`) that this
request needed to **replace**, not sit alongside — building the new system without addressing the old
one would have meant two parallel campaign engines and real risk of double-sending.

**Decisions confirmed with the user before building** (asked via structured questions, not assumed):
- Migrate the 3 existing hardcoded campaigns (festival greeting, inactive-user nudge, incomplete-KYC
  nudge) into the new DB-driven system as real `Campaign` rows, rather than leaving them as permanently
  separate legacy code the marketing team still can't touch.
- Scheduling model: a single poll-based "tick" (checks the DB every minute for due campaigns) instead
  of one APScheduler job per campaign. No persistent APScheduler job store is configured anywhere in
  this codebase — `AsyncIOScheduler(timezone=...)` is constructed with no `jobstores=` kwarg, so it
  defaults to an in-memory `MemoryJobStore`. Per-campaign jobs registered at runtime would silently
  vanish on every process restart/redeploy; a single recurring tick job re-reads `next_run_at` from the
  DB every minute and is restart-safe by construction.
- Advanced SQL audience mode (arbitrary admin-authored `SELECT` queries) explicitly **deferred
  entirely** for this pass — real security exposure (an "audience query" could just as easily
  `SELECT email, password_hash FROM admin_users`), and there's no role/permission system in place to
  gate a "Super Admin only" restriction at all (`AdminUser` has no role column).
- MVP scope for this pass also deferred: custom cron expressions, saved-audience reuse, and RBAC.

**New DB tables** (`db/base.py`, `migrations/013_campaigns.sql` — same "also created by SQLAlchemy on
startup, `.sql` is documentation only" convention as `011_wallet.sql`/`012_admin_users.sql`): `Campaign`,
`CampaignContent`, `CampaignSchedule`, `CampaignDeliveryRule`, `CampaignExecution`,
`CampaignNotificationLog`. Deliberately **no separate "Audience" table** — an ad-hoc campaign's filter
tree lives directly on `Campaign.audience_filter` (JSONB); a `SavedAudience` table was designed but not
built, since saved-audience reuse was deferred.

**`services/campaigns/`:**
- `audience_filters.py` — a field registry (`kyc_status`, `registration_date`, `total_vehicles`,
  `vehicle_type`, `truck_posted_recently`, `wallet_balance`, `number_of_posts`, `number_of_searches`,
  `most_searched_origin`/`destination`, `last_searched_time`, `last_active_date`) that compiles a JSON
  AND/OR condition tree into a SQLAlchemy boolean expression via **correlated scalar subqueries** — no
  joins/`GROUP BY` to reconcile between sibling conditions, so nesting AND/OR is just plain
  `and_()`/`or_()` composition. Every resolved audience query hard-filters to
  `User.account_status == "active"` underneath, matching the base filter all 3 legacy hardcoded
  audiences already used (suspended users are never a valid marketing audience).
  - **Gotcha, worth remembering:** `number_of_searches`/`last_searched_time`/`most_searched_*`
    deliberately query `SearchDemand`, **not** `UserActivity`, even though `UserActivity` already
    tracks `type="search"` rows. `UserActivity` is explicitly documented as keeping only the **last 10
    rows per (user, type)**, trimmed on write — counting against it would silently undercount total
    searches for active users. (`last_active_date` correctly still uses `UserActivity`'s
    `max(created_at)`, since the trim only removes the *oldest* rows — `max()` is unaffected by the cap.)
  - `app_version` filter field was **not** implemented — no such column exists on `User`, and nothing
    in the mobile-facing API reports one yet.
- `rotation.py` — per-user content rotation derived entirely from `CampaignNotificationLog` history, no
  separate cursor table: next content = lowest-`sort_order` variation not yet in the user's "sent" log
  for that campaign; cycles back to the top once all are exhausted (and is robust to the content pool
  being edited later — deleted variations are simply ignored, new ones show up as unseen immediately).
  Verified directly with a unit test: 4 rounds against a 3-variation pool correctly went
  unseen → unseen → unseen → cycled back to variation 1.
- `delivery_rules.py` — max/user/day and min-interval are enforced **per-campaign** only (counting just
  that campaign's own `CampaignNotificationLog` rows), not globally across every campaign a user might
  be enrolled in. Documented as a known scope limit, not a bug — true cross-campaign fatigue management
  would need a different, global rule.
- `executor.py` — the actual run-a-campaign pipeline (resolve audience → delivery rules → rotation →
  FCM `send_batch` → log `CampaignExecution` + `CampaignNotificationLog`), shared by the scheduler tick,
  "Send Now"/activate, and "Send Test" alike — only how the audience is chosen differs (resolved via
  the filter tree, vs. an explicit override list of user ids for test sends, which also bypasses
  delivery rules entirely).
- `scheduling.py` — `compute_next_run_at` stores `next_run_at`/`last_run_at` as **naive UTC** (matching
  this codebase's universal `datetime.utcnow()` convention) but interprets `time_of_day`/`day_of_week`/
  `day_of_month` in the campaign's own IANA timezone, recomputed fresh each cycle. `day_of_month` is
  capped to 1-28 specifically to sidestep month-length edge cases (no Feb-31 handling needed).
- `scheduler_tick.py` replaces the old `notifications/scheduler/morning.py`+`afternoon.py`+`night.py`
  wiring — `notifications/scheduler/__init__.py` now registers a single `IntervalTrigger(minutes=1)` job
  instead of three `CronTrigger`s.

**Removed entirely** (dead code once the above replaced it): `notifications/registry.py`,
`notifications/campaigns/`, `notifications/jobs/`, `notifications/audiences/`, `notifications/templates/`.
Confirmed via `grep` that nothing outside `notifications/` imported any of them before deleting.

**`scripts/seed_legacy_campaigns.py`** — one-time, idempotent (skips by name if already run) migration
of the 3 old hardcoded campaigns into real `Campaign` rows, preserving their exact copy variants and
audience logic: "Incomplete KYC Reminder" (daily 07:30 IST, `kyc_status != approved`, 10 variants),
"Inactive User Win-back" (daily 13:30 IST, `registration_date before (now-7d)` AND
`last_active_date not_within_last_days=7` — reproducing the old `InactiveUsersAudience`'s "created ≥7d
ago AND no activity or activity >7d old" logic; specifically checked that the `not_within_last_days`
operator's null-handling treats "never had any activity" the same as "activity older than 7 days"), and
"Independence Day Greeting 2026" (one-time, 2026-08-15 09:00 IST, all active users, 2 variants — the
only festival date that existed filled-in in the old hardcoded `FESTIVAL_CALENDAR`; future festivals now
just need a new one-time campaign created through the admin panel, no code change).

**`routers/campaigns.py`** — 17 new endpoints under `/api/admin/campaigns`. Notable points: static
routes (`/dashboard`, `/executions`, `/filter-fields`, `/audience-preview`) are declared **before** the
`/{campaign_id}` routes specifically so FastAPI doesn't try to parse them as a campaign UUID path param.
`DELETE` is blocked while status is `scheduled`/`running`/`paused` (must pause+archive first) — avoids
an admin accidentally deleting a live campaign's `CampaignNotificationLog` audit trail via cascade.
`PUT` (edit) is blocked once archived. `activate()` branches on `campaign_type=="manual"` (or
`schedule_type=="immediate"`) to run synchronously right then and mark `"completed"`, vs. scheduled
campaigns which just get `schedule.enabled=True` + a fresh `next_run_at` and move to `"scheduled"` (the
actual send happens later via the tick).

**Bug found and fixed during manual verification:** `GET .../analytics` initially reported "Last Run"
as always empty for manual/test-triggered campaigns — `CampaignSchedule.last_run_at` is only ever
updated by the scheduler tick, but manual "Send Now"/"Send Test" run through `executor.run_campaign`
directly, bypassing the tick entirely. Fixed by falling back to the most recent
`CampaignExecution.started_at` when `schedule.last_run_at` is null; re-verified live after the fix.

**Verified end-to-end via curl against the live dev DB** (not just unit-level): full CRUD, every
lifecycle guard rail (delete/edit correctly blocked in the right states), audience-preview against real
seeded data, test-send + activate against real users (one had a stale/invalid push token — correctly
logged as `"failed"`, distinct from `"no_token"`, confirming FCM's per-message success/failure is
tracked separately from "never had a token"), and the scheduler tick observed firing every minute in
the server logs cleanly across several `--reload` restarts.

**Known gaps, flagged to the user rather than silently omitted:**
- `"testing"` is a valid `Campaign.status` value in the schema/lifecycle, but **no code path ever
  transitions a campaign into it** — draft goes straight to scheduled/completed on activate. Dead
  lifecycle stage as it currently stands.
- No distinct "Validate Schedule" action — only implicit (the wizard UI disables "Next" if required
  schedule fields are missing).
- Analytics track `sent`/`failed`/`no_token`, not a separate `"delivered"` (confirmed-received) status
  — FCM acceptance isn't the same as confirmed device delivery.
- No real send queue — `executor.run_campaign` sends synchronously within the same request/tick; fine
  at current volume, would need a background worker if audience sizes grow large enough to make one
  execution slow.
- `respect_preferences` delivery rule is stored but not enforced — no per-user notification-preference
  model exists anywhere in this codebase.
- `campaign_type="triggered"` is schema-ready only — no event-listener engine exists yet for the
  "inactive users / wallet change / truck inactivity / KYC completion" future triggers from the
  original spec.

**Also observed but not root-caused:** during this session's DB testing, the local dev Postgres's total
user count changed from 36 (earlier, while building the admin panel's Users page) to 6 (later, during
campaign testing) between conversation turns. Flagged at the time, not investigated further — didn't
block the work, and this DB is a live/shared dev instance ([[project-prod-env]]), not a sandbox that
resets predictably.

The Angular admin panel frontend that drives all of the above (`convoy-admin/`, a separate project
folder/repo) has its own memory file at `convoy-admin/memories.md` — not duplicated here.

## Multi-device push notifications (added this session)

Replaced the single `User.push_token` column with a proper per-device model, prompted by the user
wanting to track multiple devices per user (device id + platform), plus a device-scoped logout and
auto-cleanup of tokens FCM reports as dead. Before building, I did a file:line-grounded impact analysis
(every `push_token` touchpoint: write endpoint, both read paths, the sender's structural assumptions,
an existing admin-panel leak, migration history), then the user reviewed it and specified most of the
schema/design directly rather than me proposing it — this section is that design, plus two gaps I found
grounding it against the real code, plus one correction the user made to my first plan draft.

**Schema — `UserDevice` (`db/base.py`)**: `device_id` and `fcm_token` are **both** globally unique (not
scoped per user). `device_id` unique lets a device row be *reassigned* to a different `user_id` on
re-sync (shared device, login-as-someone-else) instead of accumulating orphan rows. `fcm_token` unique
reflects that a real FCM token belongs to exactly one app instance at a time.

**Gap I found and closed**: with both columns globally unique, a naive "upsert by device_id" can hit
the `fcm_token` constraint if a token migrates onto a different `device_id` (reinstall, a stale row
from before). `device_repository.upsert_device` checks for this explicitly and **deletes** the stale
row outright (not soft-deactivates — nullable-token contortions aren't worth it for a row that's
provably an artifact of token reassignment, not real device history).

**Correction from the user after my first plan draft**: I initially proposed a `last_seen` timestamp
updated only by an explicit register/sync call, plus a suggestion to add a dedicated "heartbeat" ping
endpoint so a stable-token user wouldn't look "abandoned" by a staleness-based cleanup job. The user
rejected the new-endpoint idea specifically: instead, split into **`last_token_sync`** (moves only on
register/sync) and **`last_app_seen`** (moves opportunistically, via `middleware/auth.py`'s
`get_current_user` reading an optional `X-Device-Id` header on every already-authenticated request — no
new endpoint, throttled to at most once/hour per device via `device_repository.touch_last_seen`).
Cleanup (`services/device_cleanup.py`) judges staleness off `last_app_seen`, not `last_token_sync`,
since that's the one that actually reflects real app usage. **Gap I found while wiring this in**: the
JWT payload only carries `sub`/`mobile` (confirmed in `middleware/auth.py`) — there's no device concept
in the token today, so the mobile app has to start sending `X-Device-Id` as a plain header on its
normal calls; this wasn't something middleware could "already know" the way the user's first sketch of
the idea assumed.

**Per-device send tracking — a real latent bug, not just a missing feature.** Before this change,
`firebase_sender.py`'s `send_batch` deduped FCM's per-message results into a `Set[user_id]` before
returning, because 1 message per user was a safe assumption at the time. Once a user can have N
devices, that dedup means "if ANY of a user's devices succeeded, ALL of that user's messages — including
ones that individually failed — get logged as sent." Confirmed via `firebase_sender.py:77-83` that the
per-message `result.success`/`result.exception` data was already available; the bug was purely in the
return shape, not a missing SDK capability. Fixed by having `send_batch`/`send_single` return a
`SendResult` per message (`user_id`, `device_id`, `success`, `should_deactivate`), so callers can track
device-level truth instead of collapsing to a per-user set.

**FCM permanent-failure classification**: checked the installed `firebase-admin` version (7.5.0) rather
than guessing exception names — `messaging.UnregisteredError` and `firebase_admin.exceptions.InvalidArgumentError`
are the two that mean "this token is permanently dead," used to auto-deactivate the owning device.

**Campaign/admin-send metrics — track both device and user level, not one or the other.** The user
pushed back on my first framing of "pick device-count or user-count for Sent/Failed" — instead,
`CampaignExecution` (and the Quick Notification response) now carries **both**: `sent_count`/
`failed_count` are user-level ("reached/failed", deduped across a user's devices — a user counts as
reached if *any* device got the message), while new `devices_targeted`/`devices_delivered` columns are
the raw per-device-message counts. Confirmed `services/campaigns/rotation.py`'s "already seen this
content" check dedupes into a Python `set` keyed by `content_id`, so multiple `CampaignNotificationLog`
rows per user (one per device) from the fan-out don't break rotation. Also changed the in-app
`Notification` (bell-icon feed) write to fire once per user reached, not once per successful
device-message — otherwise a 2-device user would see duplicate feed entries for one logical send.

**Rollout — kept the old endpoint alive as a shim, not removed.** `POST /user/push-token/{user_id}`
still exists and still accepts the old `{pushToken}`-only body, but now internally calls
`upsert_device(..., device_id=f"legacy-{user_id}", platform="unknown", ...)` — an un-upgraded mobile app
keeps working exactly as before (one shared "legacy" device slot per user), while an upgraded app moves
to real per-device rows via the new `POST /user/devices/register/{user_id}`. Dropping `users.push_token`
itself is deliberately **not** part of this change — deferred to a later, separately-reviewed
`ALTER TABLE DROP COLUMN` once the mobile app has shipped and this has run live a while, consistent with
[[feedback-migration-safety]]'s point about verifying backfills before any drop. `scripts/backfill_user_devices.py`
(mirrors `scripts/seed_legacy_campaigns.py`'s shape) converts existing `push_token` values into legacy
device rows up front so there's no delivery gap for existing users before the shim starts writing them
on its own.

**Logout scope, clarified before building rather than assumed**: "create a logout API" could have meant
push-token deregistration only, or actual server-side JWT revocation. Grepped the codebase first
(`blacklist|revoke|token_version|session_token|refresh_token` — zero hits) confirming this app's driver
JWT is fully stateless with no revocation mechanism at all today; asked the user directly rather than
guessing given how much larger real JWT revocation would be as a change. Confirmed: push-token
deregistration only. `POST /user/devices/logout/{user_id}` takes a `deviceId` and deactivates just that
one device — never touches the JWT, never affects the user's other devices.

**`init_db()` only creates missing tables, never alters existing ones** (confirmed reading
`db/session.py`'s `_create_missing_tables`) — the new `user_devices` table appeared automatically on
next boot, but the two new `campaign_executions` columns (`devices_targeted`, `devices_delivered`)
needed a manual `ALTER TABLE ADD COLUMN`, run by hand against the live dev DB (additive/safe, unlike a
drop). `migrations/014_user_devices.sql` documents both, matching the project's existing
documentation-only migration convention.

**Verified end-to-end against the live dev DB** (real user, since there's no separate sandbox —
[[project-prod-env]]): minted JWTs directly via `create_access_token`/`create_admin_access_token` rather
than round-tripping real OTP SMS, to keep testing self-contained. Registered the same `device_id` twice
with a different token — confirmed one row updated in place, not duplicated. Registered a second real
device, sent a Quick Notification, confirmed `devicesTargeted`/`devicesDelivered` diverge from user-level
`sent` exactly as designed. Backdated a device's `last_app_seen`, hit an authenticated endpoint with
`X-Device-Id`, confirmed it advanced; hit it again immediately, confirmed the 1-hour throttle held;
omitted the header entirely, confirmed auth still succeeds. Logged out one device, confirmed 404 on an
unknown device id. Sent to devices with garbage tokens and confirmed FCM's "not a valid registration
token" response correctly auto-deactivated all three — the should_deactivate pipeline working end to
end, not just in isolation. Cleaned up the synthetic test device rows afterward; the one pre-existing
real device row's token was already a placeholder (`ExponentPushToken[xxxxxxxxxxxxxx]`), not a live
user's real token, so no actual user data was at risk during this testing.

**Also observed but not root-caused, out of scope for this change**: the long-running dev server (up
8 days, `--reload`) is logging a recurring `sqlalchemy.exc.InterfaceError: connection is closed` every
minute from the campaign scheduler tick's `run_due_campaigns` query, yet APScheduler still reports the
job as "executed successfully" each time — looks like the exception is being swallowed somewhere rather
than actually failing the tick. Pre-existing, unrelated to this session's changes; flagged to the user,
not fixed.

## Scalability audit + first fix: blocking synchronous HTTP calls (added this session)

User asked for a 10k+-concurrent-user scalability review. **Asked clarifying questions before auditing**
(per [[feedback-verify-before-asking]]'s spirit, but here genuinely product/infra decisions, not
checkable-in-code facts): confirmed "10k+ concurrent" means mostly-idle active app sessions (not literally
10k simultaneous requests), horizontal scaling is on the table (not just single-VM tuning), the video/call
feature is in scope too, and the user wanted a prioritized audit/roadmap first rather than immediate code
changes. Delivered as `docs/SCALABILITY_ROADMAP.md` (same convention as the other `docs/*.md` files),
grounded in actual code reads, not guesses — e.g. confirmed via `grep`/direct reads that prod runs a single
uvicorn process with no `--workers` flag, `db/session.py`'s engine has no explicit pool sizing (defaults to
15 total connections), there's no reverse proxy, and three `asyncio.create_task` background loops plus an
in-process `AsyncIOScheduler` campaign tick have zero cross-instance coordination — the campaign tick
specifically would **duplicate-send** a campaign's entire audience the moment a 2nd app instance is added,
a real correctness bug waiting to happen under the horizontal-scaling path the user said was on the table.

**First fix applied** (user asked for this specific item first): `services/google_places.py`,
`services/sms.py`, `services/kyc.py`, and `services/ulip.py` all call the synchronous `requests` library
directly — since this is a single-threaded asyncio event loop, any one of these blocking calls freezes
*every* concurrent request on the process for its full duration. Fixed by wrapping each call in
`await asyncio.to_thread(...)` **at the router call site**, not by converting the service files' internal
call graphs to `async def`. This was a deliberate choice: `kyc.py`/`ulip.py` have multi-level internal call
chains (public function → private helper → `requests.post`), and `ulip.py`'s token cache is already guarded
by a `threading.Lock` (not `asyncio.Lock`) — wrapping only at the call site runs the *entire* sync call
graph in one worker thread per request, which is simpler, touches far fewer lines, and is actually more
correct: that `threading.Lock` was already written for exactly this concurrent-OS-thread execution model.
12 call sites across `routers/locations.py`, `routers/auth.py`, `routers/kyc.py` (4), `routers/verification.py`
(4, including the loop inside `batch_verify`), and `routers/vehicles.py` were updated.

**Verified without touching real external services or prod data** (this dev `.env` has live Veup/Cashfree/
ULIP credentials — a real test would send a real SMS or cost a real Cashfree/ULIP call, see
[[project-prod-env]]): an isolated standalone asyncio script proved the actual mechanism — a `time.sleep()`
call made directly inside a coroutine delays an unrelated concurrent coroutine's scheduled ticks by the
full blocking duration; the same call wrapped in `asyncio.to_thread` lets the concurrent coroutine proceed
on schedule. Also confirmed the app still imports/boots cleanly against the live dev DB and every edited
endpoint still returns its correct auth-gated status code (401/422) without the request body/path reaching
far enough to trigger an actual external call.

Remaining roadmap items (not yet fixed, see the doc for full detail): Firebase push sends are also
synchronous and block the loop (same fix pattern, not yet applied); the campaign-scheduler duplicate-send
risk under horizontal scaling; no multi-worker uvicorn / load balancer yet; unconfigured DB pool; no
caching layer, rate limiting, reverse proxy, or APM.

## Video reels + 1:1 video calls, ported from BONUT-backend-POC (added this session)

A separate, unrelated sibling project (`BONUT-backend-POC-` — a short-form vertical video app: reels,
live streaming, 1:1 calls) was analyzed and partially ported in here. **Scope confirmed with the user
before building, not assumed**: reels (upload/view) + 1:1 video calls only — BONUT's 1:many live
streaming broadcast feature was explicitly excluded. Also confirmed: standalone feature (no FK into
`trucks`/`truck_routes`, reuses this app's real JWT `User` for identity instead of BONUT's own no-auth
`users` table), and restructured to match this repo's own conventions rather than keeping BONUT's
layered `controllers/services/repositories/clients` + Alembic-chain architecture.

**New tables** (`db/base.py`, `migrations/019_videos_and_calls.sql` — same "also created by SQLAlchemy
on startup, `.sql` is documentation only" convention as `011_wallet.sql`): `Video` (`status` as a plain
string + CHECK constraint, not a Python `Enum` — matches `scratch_cards`/`wallets`, not BONUT's
SQLAlchemy `Enum` type) and `OneToOneCall`.

**Reels** (`services/bunny_stream.py`, `services/videos.py`, `routers/videos.py`): direct-to-Bunny-Stream
TUS upload (backend never receives raw video bytes), encoding-status sync via either Bunny's webhook
(`routers/webhooks.py`, public — Bunny can't send our JWT) or a manual `/videos/{id}/sync-status` owner-
only fallback for environments Bunny's webhook can't reach. 30-second duration cap enforced server-side
(`MAX_VIDEO_DURATION_SECONDS`) — oversized videos get deleted from Bunny and marked `rejected`.

**1:1 calls** (`services/agora_client.py`, `services/one_to_one_calls.py`, `routers/calls.py`): Agora
RTC/RTM token minting, single-guest-slot room model, server-enforced max call duration via capping every
minted token's expiry at `started_at + AGORA_MAX_CALL_SECONDS` (never a background sweep's job to
enforce — that only affects display status, via `_reap_overdue` running inline on every read/join path).

**Bug caught and fixed during this build, not just copied from BONUT:** the original POC's `join_stream`
claimed the guest slot (committed to DB) *before* minting Agora tokens. If Agora were unconfigured, that
would permanently burn the single guest slot on a request that then 503s, blocking every future real
join. Fixed by checking `agora_client.require_configured()` before any DB mutation in both `start_call`
and `join_call` — confirmed via a no-DB-access unit check that both now raise 503 cleanly with no side
effects when `AGORA_APP_ID`/`AGORA_APP_CERTIFICATE` are unset.

**Prefetch caching — included after an explicit infra tradeoff discussion.** BONUT's reels feed has a
"fast-start" optimization: a Redis cache + RabbitMQ job queue + a separate always-running worker process
that prefetches upcoming videos' first segment during feed scrolling. This needs new production infra
(a RabbitMQ broker + a new systemd service) beyond just code — flagged explicitly to the user as "defer
vs. include now" before building; user chose to include it now. Ported as `services/redis_cache.py` +
`services/rabbitmq.py` + `services/video_prefetch.py` + a new top-level `worker.py` (consumed via
`deploy/convoy-video-worker.service`, wired into `scripts/server-setup.sh` and
`.github/workflows/deploy-backend.yml`'s restart step — the latter skips the restart gracefully if the
unit isn't installed yet, so existing deploys don't break before `server-setup.sh` is re-run).
**Deliberately best-effort, unlike the rest of this codebase's required-config integrations**: every
Redis/RabbitMQ call in `redis_cache.py`/`rabbitmq.py` catches its own exceptions and degrades to "treat
as cache miss / skip prefetch" rather than failing the request — a missing or down Redis/RabbitMQ must
never break actual video playback, only make it slightly less snappy. Matches this repo's existing
fire-and-forget pattern for non-critical paths (push notifications, activity logging).

**Not yet done — credentials are empty placeholders.** `.env` got new blank keys appended
(`BUNNY_STREAM_API_KEY`/`BUNNY_STREAM_LIBRARY_ID`/`BUNNY_CDN_HOSTNAME`, `AGORA_APP_ID`/
`AGORA_APP_CERTIFICATE`, `REDIS_URL`/`RABBITMQ_URL`) — the user said real credentials for all of these
are ready, but hadn't actually been pasted into `.env` as of this session. Until filled in,
`POST /api/videos` and `POST /api/calls` correctly 503 rather than crashing (verified directly, no DB
access needed). **Verified end-to-end as much as possible without those credentials**: booted the real
dev server against the live prod DB (`[[project-prod-env]]`) — both new tables + all their indexes
created cleanly via the existing auto-create-on-boot mechanism (purely additive, no `DROP`/`ALTER`
involved, so no backfill-safety concern per `[[feedback-migration-safety]]`); confirmed
`GET /api/videos`/`GET /api/calls` 401 without a token; confirmed the two playback endpoints
(`/videos/{id}/preview-segment`, `/videos/{id}/fast-start.m3u8`) and the Bunny webhook are genuinely
public and 404 cleanly on an unknown video, including in the generated OpenAPI schema's security
annotations (`app.py::_configure_openapi_security` gained an exclusion for these paths). Did not mint a
JWT against a real user to test the authenticated happy path — reading real user rows to do so was
blocked by the auto-mode permission classifier as a production-data read; left for the user or a
follow-up session once real Bunny/Agora/Redis/RabbitMQ credentials are in place.

## Reels: likes/comments/shares, categories, hashtags, posting location (added 2026-10-22)

Product decisions confirmed with the user up front: categories = **admin-managed fixed list + optional
free-form hashtags**; comments are **flat** (no replies, no comment likes), deletable by the commenter **or
the reel owner**; share = **counter only** (client opens native share sheet, then calls
`POST /api/reels/{id}/share`), every call logged in `video_shares` for future recommendations; location =
**app sends GPS, server reverse-geocodes city/state** (Google Geocoding API via `GOOGLE_PLACES_API_KEY`,
falling back to nearest `locations` row within 30km). Exact GPS is stored (PostGIS point, GiST-indexed for
a future "reels near me") but **never returned** — only `location: {city, state}`.

- Schema: `migrations/020_reel_social.sql` — ALTERs on `videos` (hashtags VARCHAR(50)[] + GIN,
  location/location_city/location_state, like_count/comment_count/share_count) + new tables
  `reel_categories`, `video_categories`, `video_likes` (PK video+user → idempotent), `video_comments`,
  `video_shares`, + 9 seeded starter categories. **The ALTERs are not applied by init_db** (it only
  creates missing tables) — must be run manually. **As of this session NOT yet applied**: running it
  against the prod DB was blocked by the auto-mode permission classifier; left to the user. Until
  applied, any code path loading `Video` will fail (columns missing) — apply before deploying.
- Counters are denormalized on `videos`, moved only when the underlying insert/delete actually happened
  (ON CONFLICT DO NOTHING RETURNING / DELETE RETURNING) via atomic `UPDATE ... SET n = n + 1`.
  `services/user_deletion.py` now calls `reel_social.release_user_interactions` so a deleted user's
  likes/comments decrement other people's counters (share counts intentionally left as history).
- `POST /api/videos` now requires `categoryIds` (1–5, active), optional `hashtags` (≤10, normalized
  lowercase, `[a-z0-9_]`), optional `latitude`+`longitude` (both or neither). Validation runs **before**
  the Bunny placeholder is created so bad requests don't orphan Bunny videos.
- All video/reel responses now share the enriched shape from `reel_social.serialize_reels` (author,
  categories, hashtags, location, counts, `likedByMe`, `postedAt`) — batch-loaded in 3 queries per page.
- Feed filters: `GET /api/reels?category=<slug>&hashtag=<tag>` (also on `GET /api/videos`).
- Admin: `GET/POST /api/admin/reel-categories`, `PATCH /api/admin/reel-categories/{id}` (slug immutable,
  deactivate instead of delete). Public picker: `GET /api/reel-categories` (driver JWT).
- Verification this session: syntax compile + app import / route registration only. No DB-backed
  testing (blocked, see above).

## Follow / followers (added 2026-10-22)

New `user_follows` table (PK follower+followee, no-self-follow CHECK, both-direction indexes, ON DELETE
CASCADE to users) — **new table only, so `init_db` creates it automatically on boot; no ALTER to apply**
(`migrations/021_user_follows.sql` is documentation). Counts are computed with indexed COUNTs, not
denormalized, so there's nothing to drift and `user_deletion` needs no changes.

- `POST/DELETE /api/users/{id}/follow` (idempotent; returns `followedByMe`, `followerCount`,
  `followingCount` of the target), `GET /api/users/{id}/follow-stats|followers|following`
  (paginated, newest first, each entry has `followedByMe` + `isMe`; suspended accounts hidden).
- Reel cards: `author` now also has `followedByMe`, `followerCount`, `isMe` (batched, +2 queries/page)
  so the viewer can follow/unfollow the creator straight from the reel.
- Not done: push notification to the followed user (would use the still-blocking Firebase sender, see
  SCALABILITY_ROADMAP), a "following" reels feed, and follower counts on `GET /user/profile`.
- Verified: compile + app import + route registration only; nothing run against the DB.

## Reels: category filter hardened (2026-10-23)

`GET /api/reels` and `GET /api/videos` `?category=` (the filter already existed, slug-only) now accepts a
category **id or slug**, repeated (`?category=a&category=b`) or comma-separated, matching reels with ANY of
them. Unknown value → 400 (not a silent empty feed); deactivated categories still filter. Also fixed
`list_reels`/`list_video_feed` swallowing that 400 into a 500 (`except HTTPException: raise`). Combine with
`hashtag=` as AND. Verified only by compile + OpenAPI schema; not run against the DB.

## Reels: hide own reels from feed, view counting, creator stats API (added 2026-10-23)

Decisions (asked up front): a "view" = client calls `POST /api/reels/{id}/view` after ~2-3s watched, every
qualifying call counts (replays too); counting is **Redis-buffered, batch-flushed** for scale; own-reel
exclusion applies to `GET /api/reels` and `GET /api/videos` (NOT `GET /reels/{id}`, so profile grids can
open own reels); stats API is owner-sees-all / others-see-public-parts.

- **Feed exclusion**: `videos.list_feed(exclude_user_id=...)`, both routers pass `current_user.id`.
- **Views** (`services/view_counter.py`): `record_view` = dedupe (Redis SET NX, 30s per user+reel) +
  `HINCRBY reel:views:pending`; creator's own views ignored. `run_view_flush_loop` (started in app.py
  lifespan, every `REEL_VIEW_FLUSH_INTERVAL_SECONDS`=10) drains via atomic RENAME → one executemany UPDATE
  on `videos.view_count`; safe with several API instances. Redis down → falls back to direct atomic UPDATE
  (views never dropped). At-least-once: crash mid-flush can slightly over-count. Counts lag ≤ flush
  interval. Swap backend by editing only that module. No per-view event rows are stored (add an event
  stream later if recommendations need it). The flusher is in the API process, not worker.py, so it
  works without the RabbitMQ worker deployed.
- **Creator stats**: `GET /api/users/{id}/reels?limit&offset` (`routers/creator_reels.py`,
  `services/creator_stats.py`): `summary {totalReels, totalLikes, totalViews*}` over READY reels + page of
  `{id,title,thumbnailUrl,durationSeconds,likeCount,commentCount,postedAt,viewCount*}`. *owner only.
  Backed by new index `idx_videos_user_status_created`; if SUM gets heavy, replace `get_summary` with a
  maintained stats table.
- **Migration `022_reel_views.sql` NOT applied** (ALTER videos + new index; init_db won't do either).
  Apply 020 and 022 before deploying; all video endpoints break until the columns exist.
- Verified: compile, app import, route registration, and a mock-only test of `record_view`'s 5 branches.
  Flush/drain and the SQL were not run against Redis/Postgres.
- Frontend API reference for all reels endpoints (2026-10-23): docs/REELS_API.md — update it whenever a reels endpoint changes.
