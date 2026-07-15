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
