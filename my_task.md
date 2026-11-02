# My tasks

Do these **in order**. Updated by Claude after every change.

Setup for now: development stays **local**, local and production **share one database**
(Azure Postgres from `.env`), so a migration you run once applies to both. Everything
runs the same way it will in production (same DB, same Redis, same code path).

> Because the DB is shared, every step that writes data writes **real production data**.
> That is why Step 1 is a backup.

---

## Part A: Database (do once, in this order)

Run all commands from the project root (`convoy_prod/`). `psql` is at
`/opt/homebrew/opt/postgresql@16/bin/psql`; if the command isn't found, run
`export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"` first.

- [ ] **Step 1: Back up the database.** Takes about a minute and lets you undo anything.
  ```bash
  set -a; source .env; set +a
  pg_dump "$DATABASE_URL" -Fc -f ~/convoy_backup_$(date +%Y%m%d_%H%M).dump
  ```
  Check the file exists and isn't empty: `ls -lh ~/convoy_backup_*.dump`

- [ ] **Step 2: See what's already applied (read-only).** If the second query below already lists some
  of the new columns, part of this was applied before; tell Claude before continuing.
  ```bash
  psql "$DATABASE_URL" -c "SELECT count(*) AS videos_columns FROM information_schema.columns WHERE table_name='videos';" \
                       -c "SELECT column_name FROM information_schema.columns WHERE table_name='videos' AND column_name IN ('hashtags','location','like_count','comment_count','share_count','view_count');"
  ```
  Expected right now: **no rows** from the second query (none of the new columns exist yet).

- [x] **Step 3: Apply the migrations.** (DONE, ran clean on first retry) (If you already tried this and `020` failed with a `NOT NULL` error on
  `reel_categories.created_at`: that was a bug in the migration, now fixed. The failed run rolled back, nothing
  changed. Just run the command below again.)
  All three are additive only (no DROP, no backfill) and safe to
  re-run. `--single-transaction` means if anything fails, nothing is applied.
  ```bash
  for f in 020_reel_social 021_user_follows 022_reel_views; do
    echo "== applying $f"
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction -f migrations/$f.sql || break
  done
  ```
  - `020`: new reel columns, likes/comments/shares/category tables, 9 starter categories
  - `021`: `user_follows` table (also auto-created on startup, harmless to run)
  - `022`: `view_count` column + index

- [ ] **Step 4: Verify.**
  ```bash
  psql "$DATABASE_URL" -c "SELECT slug FROM reel_categories ORDER BY sort_order;" \
                       -c "SELECT column_name FROM information_schema.columns WHERE table_name='videos' AND column_name IN ('hashtags','location','like_count','comment_count','share_count','view_count') ORDER BY 1;" \
                       -c "SELECT to_regclass('user_follows'), to_regclass('video_likes'), to_regclass('video_comments'), to_regclass('video_shares');"
  ```
  Expected: 9 category slugs, 6 column names, 4 table names (no NULLs).

> **From now on, every new migration Claude writes will be added here as a copy-paste command.**
> `migrations/*.sql` are not applied automatically for existing tables.

---

## Part B: Local environment (match production)

- [ ] **Step 5: Make sure Redis is running locally.** (`REDIS_URL` is already set in `.env`.)
  ```bash
  brew services start redis
  redis-cli -u "$REDIS_URL" ping        # should print PONG
  ```
  Reel views are buffered in Redis, and the same Redis will exist in production.

- [ ] **Step 6: Enable the Google Geocoding API** on the key in `GOOGLE_PLACES_API_KEY`
  (Google Cloud Console → APIs & Services → Library → "Geocoding API" → Enable).
  Without it, a reel's city/state falls back to the nearest saved place.

- [ ] **Step 7: Start the API and look for errors.** Use the same server entrypoint as production
  (`server:app`), not `app.py`:
  ```bash
  .venv/bin/uvicorn server:app --port 5050
  ```
  It should start with no errors, and `http://localhost:5050/docs` should open.

- [ ] **Step 8: Skip RabbitMQ for now** (leave `RABBITMQ_URL` empty). Prefetch is simply off locally, and
  playback still works. See decision 1 below.

- [ ] **Step 9: Bunny webhook can't reach your laptop.** After uploading a reel, call
  `POST /api/videos/{id}/sync-status` (owner only) to move it from `processing` to `ready`.
  (A tunnel like ngrok is the alternative if you want the real webhook locally.)

---

## Part C: Test it (use the Swagger page at `/docs`, or Postman)

- [ ] **Step 10:** `GET /api/reel-categories` returns the 9 categories.
- [ ] **Step 11:** `POST /api/videos` with `categoryIds`, optional `hashtags`, `latitude`/`longitude`; upload
  the file to the returned TUS URL, then `sync-status` until `ready`.
- [ ] **Step 12:** From a **second account**, check the reel in `GET /api/reels` (it must **not** appear for the
  uploader), then like, comment, share, view (`POST /reels/{id}/view`) and follow the creator.
- [ ] **Step 13:** Wait ~10 seconds, then `GET /api/users/{creatorId}/reels` as the creator: `totalViews`,
  `totalLikes`, `totalReels` should match what you just did.
- [ ] **Step 14:** Try `GET /api/reels?category=<slug>` and confirm only that category comes back.
- [ ] **Step 15:** Start a 1:1 call (`POST /api/calls`) between two accounts.

---

## Part D: Decisions I need from you (tell Claude)

1. **RabbitMQ:** replace it with a Redis queue (recommended, one less service) / keep it / drop prefetch.
2. **Docker:** build stage 1 (a compose file running Redis, and RabbitMQ if kept) so local matches production?
3. **Follow notifications:** push a notification when someone gets a new follower? (not built)
4. **Like/comment notifications** to the reel owner, and **admin comment moderation**? (not built)

---

## Part E: Frontend work (for the app developer)

**Full reference with payloads and responses: [`docs/REELS_API.md`](docs/REELS_API.md). Give this file to the frontend.**

- Upload: send `categoryIds` (1–5, required), optional `hashtags`, `latitude` + `longitude` (both or neither).
- Reels feed: category chips from `GET /api/reel-categories`, then `?category=<id or slug>` (repeatable).
- Each reel: `likedByMe`, counts, `author.followedByMe` / `author.isMe` for the follow button.
- Actions: `POST/DELETE /reels/{id}/like`, comments endpoints, `POST /reels/{id}/share` (after the share sheet),
  `POST /reels/{id}/view` (after ~2–3s watched), `POST/DELETE /users/{id}/follow`.
- Creator profile: `GET /api/users/{id}/reels`.

---

## Part F: Later, when you move to the launch phase (don't do now)

- [ ] Create a **separate production database**: `pg_dump` the shared one, restore into a new DB, point only the
  VM's `.env` at it, and re-run the migration loop from Step 3 there.
- [ ] Install Redis on the VM (append-only persistence on), then re-run `scripts/server-setup.sh` to install the
  worker service.
- [ ] In Bunny's dashboard set the webhook to `https://<your-host>/api/webhooks/bunny`.
- [ ] Before adding a second app instance: the campaign scheduler would send every campaign twice.
- [ ] Open scalability items in `docs/SCALABILITY_ROADMAP.md` (Firebase sends block the server, no DB pool
  sizing, no rate limiting).

## Part G: Heatmap (Trucks + Reels tabs in the app)

**Migrations: none.** The only new backend dependency is the `h3` package. Full API reference for the app
developer: [`docs/HEATMAP_API.md`](docs/HEATMAP_API.md).

- [ ] **Step G1: Install the new dependency locally.**
  ```bash
  .venv/bin/pip install -r requirements.txt
  ```
  Production needs nothing extra: the deploy workflow already runs `pip install -r requirements.txt`.

- [ ] **Step G2: Run the heatmap unit tests** (no database needed). Expect `16 passed`.
  ```bash
  .venv/bin/python -m pytest tests/test_heatmap.py -q
  ```

- [x] **Step G3: Dummy data is IN the shared dev database right now** (seeded 2026-10-27: about 3,000 live trucks and
  4,000 reels, spread over ~70 cities, 22 highway corridors and 14 transport hubs across India, owned by 60 fake users
  whose mobile starts with `hm-dummy-`). Because local and production share this database, real users can also see them
  until removed: dummy truck posts show in truck search, and dummy reels (no playable video) show in the reels feed.
  The seed script refuses a non-local database unless you pass `--allow-remote`.
  ```bash
  .venv/bin/python scripts/seed_heatmap_dummy.py --status --allow-remote    # how many dummy rows exist
  .venv/bin/python scripts/seed_heatmap_dummy.py --reset --allow-remote     # replace them with a fresh set
  .venv/bin/python scripts/seed_heatmap_dummy.py --trucks 10000 --reels 15000 --reset --allow-remote   # more data
  ```

  Each dummy truck post now has 1 to 3 destinations and a contact name plus a clearly fake `0000…` number, so tapping
  a pin in the app shows a full details sheet. (Re-seeded 2026-10-27: 3,154 posts, 6,321 destinations.)

- [ ] **Step G3b: Try the real truck pins in the app.** Open the Heatmap tab, stay on **Trucks**, zoom in until a city
  fills the screen (zoom 12 or more): the heat becomes truck pins, nearby ones grouped in numbered bubbles. Tap a bubble
  to zoom in, tap a pin for the poster, route, truck and contact. Delhi, Mumbai and the hub areas have the most data.

- [ ] **Step G3c: Try "tap a Reels hotspot".** Heatmap tab, **Reels**, tap a red/orange blob: the app jumps to the Reels
  tab showing an orange area chip (for example "Delhi, Delhi ✕") and that area's reels, most popular first. Tap the
  chip's ✕ to go back to the normal feed. On the Reels heatmap you can't zoom in past a city. The dummy reels now have
  uneven likes/comments/shares/views so the popular order is visible (the dummy reels have no playable video, so the
  player just spins; the title, author, counts and place are real). API reference: `docs/REELS_API.md` ("Area feed").
  Re-seeded 2026-10-27 so the shared dev DB has these counters.

- [ ] **Step G3d: Check the map stays on India.** Heatmap tab (either tab): it opens fitted to India and you can't pan
  or zoom out past the India box (`indiaBounds` in `map_zoom_levels.dart`: 6.5–37.2 N, 68–97.5 E). Neighbouring
  countries that fall inside that box (Pakistan, Nepal, Bangladesh, Sri Lanka, part of Tibet) are still drawn by the map
  tiles; hiding them would need an India outline mask. App-only change, no backend change.

- [ ] **Step G3e: Check the new truck details sheet.** Heatmap tab, Trucks, zoom in to pins and tap one (the Truck Map
  screen uses the same sheet). Top row: poster photo/icon, name, and a close (X) button; no "Truck Details" title.
  Grey card: truck type and capacity, masked number, then the route with full locations (name, city, state, pincode),
  length/height and mutuals when present. Buttons: **Request 1:1** (shows "coming soon" until its form is built) and
  **Call Driver to Book** (opens the dialer, same as the old call icon). App-only change.

- [ ] **Step G3f: Check the Home / Profile / Search changes (app only).** Home no longer has "Manage Post & Searches".
  Profile > Account has **Recent Post & Searches** between My Uploads and KYC Verification; it opens the full
  Recent Posts / Recent Search screen (the old "View All" target). The Search tab shows your 5 most recent searches
  under the form instead of the live truck map; tap one to repeat it, and the list refreshes when you come back.

- [ ] **Step G4: REMOVE all the dummy data (one command).** It deletes only rows owned by the `hm-dummy-` users and
  prints what it removed; real data is never matched.
  ```bash
  .venv/bin/python scripts/seed_heatmap_dummy.py --cleanup --allow-remote
  ```

- [ ] **Step G5: Try it in the app.** The app's `.env` already points at your local API (`API_BASE_URL`). Start the API
  (`.venv/bin/uvicorn server:app --port 8000`, or use the port your `.env` expects), log in, open the **Heatmap** tab.

- [ ] **Optional: use a scratch database instead of the shared one** (nothing visible to real users). Local Postgres 17
  on port 5433 has PostGIS:
  ```bash
  psql -h 127.0.0.1 -p 5433 -U postgres -c "CREATE DATABASE convoy_heatmap_test;"
  psql -h 127.0.0.1 -p 5433 -U postgres -d convoy_heatmap_test -c "CREATE EXTENSION IF NOT EXISTS postgis;"
  export DATABASE_URL=postgresql://postgres@127.0.0.1:5433/convoy_heatmap_test
  .venv/bin/python scripts/seed_heatmap_dummy.py --reset            # no --allow-remote needed for a local DB
  DATABASE_URL=$DATABASE_URL VEUP_PROCESS_KEY= PORT=8010 .venv/bin/uvicorn server:app --port 8010   # empty key = dev OTP, no SMS
  psql -h 127.0.0.1 -p 5433 -U postgres -c "DROP DATABASE convoy_heatmap_test;"                     # when done
  ```

- [ ] **In production:** deploy as usual (nothing else to run). The Trucks heatmap shows live (unexpired) truck posts;
  the Reels heatmap shows ready reels from the last 30 days that were posted **with GPS** (change the window with the
  `HEATMAP_REEL_WINDOW_DAYS` env var). It will look empty until there is such data. At zoom 12+ cells with fewer than 3
  items are hidden on purpose, for privacy.

---

## Part H: Reels delete, view-own-reel, camera-only upload (backend + convoynew app)

**Migrations: none.** New endpoint: `DELETE /api/reels/{id}` (owner only, hard delete; docs in `docs/REELS_API.md`
section 5). It deletes the video on Bunny first, then the DB row (likes, comments, shares and category tags cascade).
The app changes are in `convoynew` (`lib/features/reels/...`).

- [ ] **Step H1: Run the new backend tests** (no database needed). Expect `3 passed`.
  ```bash
  .venv/bin/python -m pytest tests/test_reel_delete.py -q
  ```

- [ ] **Step H2: Start the API** (the app talks to it):
  ```bash
  .venv/bin/uvicorn server:app --port 8000
  ```

- [ ] **Step H3: Try the delete endpoint without the app** (optional; use a throwaway reel, this is the shared
  production DB and the delete is permanent). In Swagger at `/docs`, log in, then `DELETE /api/reels/{reelId}`:
  your own reel returns `200 {"success": true}`; someone else's returns `403`; an unknown or already deleted id `404`.

- [ ] **Step H4: Check the app** (run it, then Profile > My Uploads). Everything below needs a throwaway reel:
  1. The bottom button now says **Upload Reel**. Tapping it (or **New Post**, or the **+** on the Reels tab) opens the
     camera directly. There is no "Choose from gallery" option any more.
  2. Tap a **ready** reel card: a full-screen player opens with title, tags, place, comment/share counts, mute and a
     delete icon (top right). The like count shows but can't be tapped (it's your own reel).
  3. Tap a card that is still **Processing**, **Failed** or **Rejected**: a "can't be played yet" message appears.
  4. The card's **⋮** menu now has only **Delete** (Edit is gone). Delete asks to confirm, then the card disappears
     and the totals at the top update. Deleting from the full-screen player does the same and returns to the list.

- [ ] **Step H5: Delete the Bunny test video check** (optional). In the Bunny Stream dashboard, the deleted reel's video
  should be gone too.

> Not done on purpose: **Edit** was removed from the menu (no edit feature exists). `reel_source_sheet.dart` and its
> `reelCreate*` strings were deleted. The generated Riverpod file `my_uploads_notifier.g.dart` was edited by hand
> (build_runner is unreliable in this repo); the hash in it is a placeholder and harmless.

---

## Part I: Paid 1:1 calls (backend + the `convoynew` app, stages 1 to 7)

Request -> driver accepts/rejects -> wallet freeze -> driver starts call (rings) -> requester answers -> heartbeats ->
end -> "were you satisfied?" -> settle -> behaviour log. The app side (request form, driver review, call screens) is stages 3-5, below. Full API for the app developer: [`docs/ONE_TO_ONE_CALL_API.md`](docs/ONE_TO_ONE_CALL_API.md).
Run everything from the project root (`convoy_prod/`).

- [ ] **Step I1: Run the tests that need no database.** Expect `69 passed`.
  **Don't run plain `pytest tests`**: `tests/backend_test.py` is an old script that calls an external host
  (`ride-match-trucks.preview.emergentagent.com`) and fails.
  ```bash
  .venv/bin/python -m pytest tests/test_call_hold.py tests/test_heatmap.py tests/test_reel_ranking.py tests/test_reel_delete.py -q
  ```

- [ ] **Step I2: Run the money tests on a scratch database** (nothing touches the shared Azure DB; the test file
  refuses any non-local URL). The scratch DB `convoy_calls_test` already exists on your local Postgres 17 (port 5433).
  Expect `72 passed` (this includes the Stage 2 call tests).
  ```bash
  export CALL_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
  .venv/bin/python -m pytest tests/test_call_hold.py tests/test_call_requests.py tests/test_call_requests_api.py tests/test_call_flow.py -q
  ```
  If it fails with `column ... does not exist`, the scratch DB predates Step I8: apply `migrations/024_call_request_presence.sql` to it
  (`psql "$CALL_TEST_DATABASE_URL" -f migrations/024_call_request_presence.sql`).
  If the database is missing, create it first:
  ```bash
  psql -h 127.0.0.1 -p 5433 -U postgres -c "CREATE DATABASE convoy_calls_test;"
  psql -h 127.0.0.1 -p 5433 -U postgres -d convoy_calls_test -c "CREATE EXTENSION IF NOT EXISTS postgis;"
  ```
  When you're done with it: `psql -h 127.0.0.1 -p 5433 -U postgres -c "DROP DATABASE convoy_calls_test;"`

- [ ] **Step I3: Apply the migration to the shared database** (2 new tables, nothing existing is changed, safe to
  re-run; `--single-transaction` means all or nothing). Do Step 1 (backup) from Part A first. Starting the API
  would also create these tables by itself, so run this before the next deploy to have it done on purpose.
  ```bash
  set -a; source .env; set +a
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction -f migrations/023_call_requests.sql
  ```
  Check: `psql "$DATABASE_URL" -c "SELECT to_regclass('call_requests'), to_regclass('call_behaviour_events');"` shows both names.

- [ ] **Step I4: Know what changes for existing users once this is deployed** (nothing to run, just read):
  1. **Wallets no longer need approved KYC to exist.** Redeem now checks KYC itself instead (same result for them).
  2. **Redeem withdraws `available - open 1:1 offers`,** not the whole available balance. Same as before when no call
     request is open.
  3. `GET /api/wallet/{userId}` has two extra fields (`callCommitments`, `spendableBalance`); nothing removed.
  4. A background job now runs every 30 s (expires old requests, settles unanswered "were you satisfied?").

- [ ] **Step I5 (optional): Try it by hand on a scratch copy.** Use the same scratch-DB recipe as Part G (throwaway DB,
  API on port 8010, `VEUP_PROCESS_KEY=` empty for dev OTP) and add `DEV_WALLET_CREDIT_ENABLED=true`:
  ```bash
  DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test DEV_WALLET_CREDIT_ENABLED=true \
    VEUP_PROCESS_KEY= .venv/bin/uvicorn server:app --port 8010
  ```
  In Swagger (`http://127.0.0.1:8010/docs`): `POST /api/wallet/{userId}/dev-credit`, then `POST /api/call-requests`.
  **Never set `DEV_WALLET_CREDIT_ENABLED=true` while `.env` points at the shared database** (it would mint fake
  money in the real ledger). It is off by default.

> Decisions I made on your behalf (tell me if any is wrong):
> - **No answer to "were you satisfied?" within 24 h** = "no" (driver gets only the call amount).
> - **Minimum offer ₹50**, maximum ₹10,000, no cooldown after a rejection, topic is one required text field
>   (no separate description), a request must be made from a **live truck post** of that driver.
> - **A deleted driver's open requests are released; a deleted requester's connected call still pays the driver the
>   call amount** (admin account deletion).
> - A driver's **frozen wallet still receives** call earnings (refusing would strand money the requester already paid).
> - Behaviour events never store chat text, only that chat was used.

### Stage 0 result: Agora chat token spike (done on the iOS simulator, partly blocked)

Tested with a throwaway Flutter app (outside your repos) using the same packages as `reels_app`
(`agora_rtc_engine 6.6.4`, `agora_rtm 2.2.5`) and the tokens your backend issues today.

| Test | Result |
|---|---|
| Control: garbage RTM / RTC token | Rejected. Tokens **are** enforced, so the other results mean something |
| RTC join with the backend's current token | **Works** |
| RTM login with the backend's current token | **Fails: `Invalid token`** |
| Chat round trip | Not run (needs a working RTM login) |

**First finding:** `agora-token-builder 1.0.0` only makes old `006` tokens. RTM 2.x (the version `reels_app` uses) rejects them.
`reels_app` never noticed because it ignores the result of `login()`, so its viewer count would silently never work.
So chat needs Agora's newer **AccessToken2 (`007`)** token builder. It also lets Stage 2 make a requester's token
**audio-only on the server** (no video publishing at all), which the old library can't.

- [ ] **Step I6: Chat (Agora RTM) is still rejected. Please check the Agora Console.** The newer `007` token builder is now in
  `services/agora_token2/` (you added it; I checked it is byte-identical to the file I reviewed) and the backend uses it
  (`chatAvailable` is now `true`). I re-ran the simulator test with the backend's real token: **RTM login still fails with
  `Invalid token`**, for the plain RTM token and for a combined RTC+RTM token. The token is well-formed (parses with Agora's own
  parser, right app id, 1 h expiry) and the same project's RTC join works, so the likely cause is on the Agora project side,
  which I can't see:
  1. Open the Agora Console -> your project -> make sure **Signaling (RTM)** is enabled for it (and that the project's app
     certificate is the one in `AGORA_APP_CERTIFICATE`). Tell me when it is, and I'll re-run the chat test in a minute.
  2. If you don't want to chase this, say so and I'll build chat on our own backend instead (a small table + polling, no Agora
     RTM). The call itself (video/audio) is not affected either way.
  Until then the apps should treat `chatAvailable: true` as "try RTM, and hide chat if login fails".

- [ ] **Clean up (optional):** `rm -rf` the scratch folder `/private/tmp/claude-501/-Users-ankit-Documents-sharkship-others-convoy-prod/*/scratchpad/{tokgen,spike_app,spike_server}`
  (it is temporary and outside your repos; `tokgen` holds the downloaded Agora files, never executed).

### Stage 2 (the call itself): what you need to do

- [ ] **Step I7: Re-run the tests** (command in Step I2, expect `72 passed`).

- [ ] **Step I8: Apply migration 024** (6 new columns on `call_requests`, additive, safe to re-run). It needs 023 applied first
  and must be applied **before the new code is deployed**: the API only creates missing tables, it does not add columns to
  existing ones, so without this the call endpoints would fail with "column does not exist". Back up first (Part A, Step 1).
  ```bash
  set -a; source .env; set +a
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction -f migrations/023_call_requests.sql
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction -f migrations/024_call_request_presence.sql
  ```
  Check: `psql "$DATABASE_URL" -c "SELECT column_name FROM information_schema.columns WHERE table_name='call_requests' AND column_name IN ('connected_ms','last_tick_at');"` returns 2 rows.

- [ ] **Step I9: Know what Stage 2 changed** (nothing to run):
  1. **The old open-room `/api/calls` endpoints are removed** (anyone could claim the "guest" slot and there was no payment).
     The app never used them. The old `one_to_one_calls` table is untouched (nothing dropped).
  2. The env var `AGORA_MAX_CALL_SECONDS` is no longer used; call length now comes from what the requester picks
     (10/15/30 min). `AGORA_APP_ID` and `AGORA_APP_CERTIFICATE` must be set on the server, otherwise the start/join
     endpoints answer `503 Video calls are not configured`.
  3. New optional env vars (defaults in `config.py`): `CALL_HEARTBEAT_GRACE_SECONDS=15`, `CALL_RECONNECT_WINDOW_SECONDS=60`,
     `CALL_END_GRACE_SECONDS=30`, `CALL_RING_SECONDS=45`.
  4. The requester's token currently lets them publish video too (the old token format can't forbid it); their app must
     not. The newer token builder in Step I6 lets the server enforce audio-only.

- [ ] **Step I10 (optional): Refresh the shared API file** so the app developer sees the new endpoints:
  `.venv/bin/python scripts/export_openapi.py` (rewrites `docs/openapi.json`; I didn't, to avoid unrelated churn).

> Decisions I made for the call stage (tell me if any is wrong):
> - **A dropped call can reconnect for 60 s**, no matter how long it had run. After that it ends and is settled from the time
>   connected so far (under 30 s costs nothing).
> - **Time only counts while both apps report in and both say they can see each other**, so nobody can run up the 30 s alone.
>   The very first 5 s round isn't counted (conservative, in the requester's favour).
> - **The requester declining a ring cancels the request** (your diagram); an **unanswered** ring (45 s) goes back to
>   `accepted` so the driver can try again.
> - **The call is force-ended at the chosen duration + 30 s**; its Agora tokens expire at the same moment.

### Stage 3 (the app: request form, status sheet, wallet balance) — in `convoynew`

Built and checked on the iOS simulator against a scratch backend: tap a truck pin -> **Request 1:1** -> form (topic, 10/15/30 min,
offer, wallet balance) -> recharge prompt when short -> request sent -> status sheet (waiting -> accepted updates by itself ->
cancel releases the money). **Off by default**: both new switches in `lib/core/constants/feature_flags.dart` are `false`, so the
app behaves exactly as before (the button still says "coming soon").

- [ ] **Step I11: Run the app tests.** From `~/Documents/Sharkship-apps/convoynew`. Expect everything to pass except
  `test/widget_test.dart` ("Counter increments"), which is Flutter's untouched template test and was already failing
  (delete that file when you like).
  ```bash
  flutter test test/features/call_request      # the new ones: 49 passed
  flutter test                                 # everything: 126 passed, 1 failed (the template test)
  ```

- [ ] **Step I12: Try it yourself on a scratch backend** (never against the shared database; the dev `.env` points at it).
  Everything below is temporary and you undo it at the end. Backend, from `convoy_prod/`:
  ```bash
  export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
  psql -h 127.0.0.1 -p 5433 -U postgres -d convoy_calls_test -f migrations/024_call_request_presence.sql   # once, if the DB predates it
  .venv/bin/python scripts/seed_call_demo.py        # one driver + one live truck post near Connaught Place, Delhi
  DEV_WALLET_CREDIT_ENABLED=true VEUP_PROCESS_KEY= REDIS_URL= RABBITMQ_URL= .venv/bin/uvicorn server:app --port 8010
  ```
  App: in `convoynew/.env` change the **active** `API_BASE_URL` line to `http://127.0.0.1:8010/` (keep the original to put back), and in
  `lib/core/constants/feature_flags.dart` set `oneToOneEnabled = true` and `devWalletCreditEnabled = true`. Then
  `xcrun simctl location booted set 28.6275,77.2105`, run the app, log in with test number `9876543210`, OTP `1234`,
  open **Heatmap**, tap the location button, zoom in past city level until the truck pin shows, tap it, tap **Request 1:1**.
  The form's **Add money** button (dev only) credits test money. To play the driver, log in as `9000000001`
  (the API returns its OTP in dev mode) and call `POST /api/call-requests/{id}/accept`.
  **To undo:** put the `.env` line and both flags back, stop uvicorn, then
  `.venv/bin/python scripts/seed_call_demo.py --cleanup`.

- [ ] **Step I13: Have the Hindi, Kannada and Marathi texts checked.** I wrote them (about 45 strings, all starting `oneToOne…`
  or `walletTxnCall…` in `lib/l10n/app_hi|kn|mr.arb`); they need a native speaker's eye before release.

- [ ] **Step I14: Order of go-live** (when you decide to ship it): apply migrations 023 + 024 -> deploy the backend -> *then* set
  `oneToOneEnabled = true` in a new app build. Keep `devWalletCreditEnabled = false` in any build that talks to the real
  database; the 1:1 form then tells a short-of-money requester "Wallet recharge is coming soon" (no way to add money until
  Razorpay, Stage 7).

> What Stage 3 does **not** do (later stages): the driver's Home banner and accepting from the app (4), the ringing / call screen /
> "were you satisfied?" prompt (5; until then those statuses show as plain text), CallKit (6), Razorpay (7). The wallet screen is
> still behind `walletEnabled = false`; the new ledger types are already labelled for when you turn it on.
> Notes: generated Riverpod files for this feature (`call_request_notifier.g.dart`, plus one provider in
> `wallet_notifier.g.dart`) are **hand-written** because `build_runner` is broken in this repo (it stops at a prompt about deleting 26
> generated files and then crashes; nothing was deleted). The hash lines in them are placeholders; a working `build_runner` will
> regenerate them cleanly.

### Stage 4 (the driver side: Home banner, review, accept / reject, push handling) — in `convoynew`

Built and checked on the iOS simulator against a scratch backend, logged in as a driver: the **"1:1 Video Request Pending"**
pill floats above the bottom bar on Home (oldest waiting request, a count badge when several wait) -> **Review** opens the request
(who, topic, offer, what the driver earns) -> **Accept**, or **Reject** (asks first). Accepting and rejecting moved the money
correctly (accepted: the hold stays frozen; rejected: the requester's hold is released), and the banner moved on to the next
request. Still **off by default** (`oneToOneEnabled = false`).

- [ ] **Step I15: Run the app tests.** From `~/Documents/Sharkship-apps/convoynew`:
  ```bash
  flutter test test/features/call_request      # 88 passed
  flutter test                                 # 165 passed, 1 failed (the old template test, see Step I11)
  ```

- [ ] **Step I16: Try it as the driver** (scratch backend only, same temporary edits and undo as Step I12). From `convoy_prod/`:
  ```bash
  export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_calls_test
  .venv/bin/python scripts/seed_call_demo.py --driver-mobile +917408681257        # the driver, a QA number (OTP 1234)
  DEV_WALLET_CREDIT_ENABLED=true VEUP_PROCESS_KEY= REDIS_URL= RABBITMQ_URL= .venv/bin/uvicorn server:app --port 8010 &
  .venv/bin/python scripts/demo_call_requests.py --driver-mobile +917408681257    # 3 people send that driver a request
  ```
  App: same `.env` / `oneToOneEnabled = true` edits as Step I12, then log in with `7408681257`, OTP `1234`. The banner shows on Home.
  (If the app already holds a login, use Profile -> Logout first.) **Undo** exactly as in Step I12, then
  `.venv/bin/python scripts/seed_call_demo.py --driver-mobile +917408681257 --cleanup`.

- [x] **Step I17: (superseded by Stage 5, Step I23)** Starting the call is built now; keep the flag off only until the two-phone test (Step I22).

- [ ] **Step I18: Test the push on a real phone** (the simulator can't receive Firebase pushes, so this part is only unit-tested).
  With a build that has `oneToOneEnabled = true` and a backend that has the Stage 1-2 code: send the driver a request and check
  (a) the phone shows the notification "New 1:1 call request"; (b) tapping it opens the app on Home with that request's review sheet
  already open; (c) with the app open, the banner appears within a moment (a push refreshes it; otherwise it refreshes every 20 s).
  The push carries `type: call_request` and `requestId` (see `docs/ONE_TO_ONE_CALL_API.md`).

> Decisions I made (tell me if any is wrong): the banner is **Home only** and shows **only requests still waiting** for an answer
> (accepted ones don't clutter it); it asks the server every 20 s **only while Home is the visible tab**; **Reject asks for
> confirmation** because it closes the request; a requester with no name shows as "the requester".
> The Hindi / Kannada / Marathi texts added here (21 more strings) also need a native speaker's check (Step I13).

### Stage 5 (the call screens: ring, video call, chat, "were you satisfied?") — in `convoynew` (+ one small backend change)

Built, with every rule covered by automated tests using a fake phone/Agora/server (**not yet run on two real phones — that is Step I22**):

- **Driver:** accepted request -> **Start call** (first a "don't use the phone while driving / I'm parked" reminder) -> the camera starts and
  the requester's phone rings for 45 s -> live call (own camera full-screen, mute / camera on-off / flip / chat / End, time left, and a
  "Counts as a call" chip that turns on after 30 s really connected). Afterwards: what they earn. If nobody answers: "didn't pick up, try again".
  The Home banner now shows accepted requests as **"1:1 call ready to start"** so the driver can find Start again.
- **Requester:** a full-screen **"{driver} is calling you"** screen (Answer / Decline, 45 s countdown) -> audio-only call (sees the driver's
  camera, sends only the microphone) -> after a call that counted, **"Were you satisfied?"** (Yes pays the full offer, No only the call
  amount, Decide later = asked again when the app next opens; no answer in 24 h = call amount).
- **Back in a call:** if the app was closed / crashed mid-call, the next launch (or coming back to the app) offers **"You're still in a call — Rejoin / End call"**.
- **Chat** during the call, over the call's own Agora data stream (see the decision below). Never stored, never reported (only "a message of N letters").
- **Behaviour log:** mic / camera / speaker / chat / background / network / reconnect events are sent in batches (never chat text).
- **Backend change:** the request object now carries `ringEndsAt` while ringing (the ring screen counts down to it). 30 tests pass.

- [ ] **Step I19: Backend — deploy the small change** (nothing in the database changes; migrations 023/024 are still the only ones, Steps I3/I4).
  Run the call tests on the scratch DB first:
  ```bash
  CALL_TEST_DATABASE_URL="postgresql+asyncpg://postgres:postgres@127.0.0.1:5433/convoy_calls_test" \
    .venv/bin/python -m pytest tests/test_call_requests_api.py tests/test_call_flow.py -q      # 30 passed
  ```

- [ ] **Step I20: App — install the new packages** (Agora video SDK, `permission_handler`, `wakelock_plus`). From `~/Documents/Sharkship-apps/convoynew`:
  ```bash
  flutter pub get
  cd ios && LANG=en_US.UTF-8 pod install && cd ..       # the LANG part avoids a CocoaPods "Unicode Normalization" crash in some terminals
  ```
  Files changed for this: `pubspec.yaml`, `ios/Podfile` (switches the camera / microphone permission checks on), `ios/Runner/Info.plist`
  (camera / microphone texts now mention calls; `audio` background mode so a call keeps going if the screen locks),
  `android/app/src/main/AndroidManifest.xml` (audio routing, network state, bluetooth), `android/app/build.gradle.kts` (`compileSdk` at least 37, which the permissions plugin requires; needs Android platform 37 installed, which this Mac has).
  **Before a Play Store release:** Agora's SDK adds a few permissions on its own (`READ_PHONE_STATE`, `FOREGROUND_SERVICE_MEDIA_PROJECTION`, `WRITE_EXTERNAL_STORAGE`); check the Play Console permission / foreground-service declarations. The app is **bigger** now (Agora's video SDK is large; check the size of a release build before you ship).

- [ ] **Step I21: Run the app tests.**
  ```bash
  flutter test test/features/call_request      # 225 passed
  flutter test                                 # 302 passed, 1 failed (the old template test, Step I11)
  ```
  The iOS app compiles with the new SDK (checked: `flutter build ios --simulator --no-codesign --debug` succeeds), and the Android debug APK builds too (`flutter build apk --debug`) once `compileSdk` is 37.

- [ ] **Step I22: The real test — two phones.** The simulator has no camera, so the video call can only be checked on real devices
  (two phones, or one phone + one simulator for the requester side; Agora needs a real network). Use the scratch backend so nothing touches
  the shared database, with `AGORA_APP_ID` / `AGORA_APP_CERTIFICATE` set in `.env` as for Stage 2 (same temporary edits and undo as Steps I12 / I16;
  both phones must reach your Mac, so set `API_BASE_URL=http://<your Mac's LAN IP>:8010/` and start uvicorn with `--host 0.0.0.0`).
  Phone A = driver (`7408681257`), phone B = requester (`9876543210`), OTP `1234` for both; credit B with the dev-credit button.
  Check, in this order:
  1. B: Heatmap -> tap A's truck -> Request 1:1 (₹50, 10 min). A: banner -> Review -> Accept.
  2. A: **Start call** -> parked reminder -> allows camera + microphone. B's phone rings (app open: ring screen at once).
  3. B: **Answer** (allows microphone). Both hear each other; **B sees A's camera; A sees only their own** (B sends no video).
  4. After ~35 s the "Counts as a call" chip turns on (both phones). Try mute, camera off, flip (A), speaker (B), chat both ways.
  5. A: End -> B gets **"Were you satisfied?"** -> Yes -> check A's wallet got the full ₹50 and B's was charged ₹50.
  6. Repeat and: B declines the ring (request closes, money back); B ignores the ring (A: "didn't pick up", A can Start again); end a call under 30 s (nothing charged);
     kill B's app mid-call and reopen it (Rejoin prompt); lock / background a phone for ~20 s and come back; switch one phone to airplane mode for 10 s and back.
  Tell me what looks or sounds wrong — I'll fix it.

- [ ] **Step I23: Keep `oneToOneEnabled = false` for real users until Step I22 passes.** After it passes you can switch it on for testers.
  With Stage 6 (below) a ring reaches a **closed or locked** phone as the system's call screen once Steps I24–I25 are done; before that (or on a phone without a VoIP token)
  it is a normal notification: tapping it opens the ring screen if the 45 s haven't run out. With the app open the ring screen appears by itself.

> Decisions I made (tell me if any is wrong):
> - **Chat uses Agora's in-call data stream, not RTM.** RTM login is still blocked on the Agora Console setting (Step I6), it needs a second SDK
>   (with an iOS pod workaround and a Windows-plugin pin), and in-call chat needs none of RTM's extras (history, offline messages). Messages exist only
>   while both are in the call. If you later want RTM, only `lib/features/call_request/media/` changes.
> - **Permissions are asked before anyone is rung** (the driver's phone must be able to show video before the requester's phone rings).
> - **Declining the ring cancels the whole request** (money released), and the screen says so.
> - **"Decide later" is asked again the next time the app opens** (and after 24 h the server settles it as "No").
> - **If the other person drops out, the call is not ended for you**: the screen says they lost connection, and **End** is always there.
>   (The server ends it by itself when an app goes silent for 60 s; a person who stays in the app but left the channel is the case it can't tell.)
> - The ring countdown uses the phone's clock; if a phone's clock is minutes off, the number is off but the server still decides.
> - New texts: 70 more strings in English / Hindi / Kannada / Marathi — the Hindi, Kannada and Marathi ones need a native speaker's check (Step I13).

> **Known limits (not built / not verified):** the Agora connection code (`media/agora_call_media_engine.dart`) is only compile-checked until Step I22;
> **Android keeps the microphone alive in the background only with a foreground service** (Stage 6, below, adds it: the "Call in progress" notification);
> the requester is *asked* for audio-only but the Agora token doesn't yet *forbid* a camera (optional hardening).

### First two-phone test: what went wrong and what was fixed (2 Nov)

Reported: (1) the call looked like it ended on both phones and asked to "Rejoin" before/as soon as it was answered; (2) after Rejoin the screen said
"waiting for the other user" forever; (3) hanging up before 30 s needed "Done" tapped twice. Found by reproducing the exact sequence with the real
screens, plus your server's access log:

- **Cause of 1, 2 and 3 (app):** the coordinator decided "am I already on a call screen?" by reading the router's location, which does **not** change when a screen is *pushed* (it kept saying `/home`).
  So when the phone came back from the camera / microphone permission dialogs, the 20-second list refresh found the call "ringing / live" and put the **Rejoin prompt on top of the working call**.
  Tapping Rejoin then opened a second call screen on the same call (its rejoin did nothing, so it sat on "waiting"), and after hanging up there were two "Done" screens to close.
  **Fixed:** open call screens are now tracked explicitly; no prompt is ever shown over one; the Rejoin prompt refuses to start a second call screen; a call that has just ended on this phone is never offered for rejoin from a stale list (the list is only refreshed every 8 s).
- **Server bug (500 on every behaviour event):** the apps send times as `...Z`, the database stores naive UTC, and the insert crashed. My tests had only sent naive times. **Fixed** (and a future time from a wrong phone clock now counts as "now").
- **Hardening:** the heartbeat now starts the moment the server puts the call live (before the audio/video connects), joining the audio/video times out after 20 s with a clear message instead of "waiting" forever, a requester whose audio can't start ends the call (nothing charged) instead of leaving the driver waiting, and unexpected API errors are printed in the debug console instead of looking like "no connection".
- **Not found (unknown, needs your console):** in your run the requester's phone never sent a heartbeat after answering, and neither phone ever saw the other in the audio/video channel (`connected_ms` 0). The fixes above remove the Rejoin interference, but **if the two phones still don't connect, the cause is in the Agora audio/video join**, and the new `[call xxxxxxxx] ...` lines in the `flutter run` console on **both** phones show exactly where it stops (permissions, server, "media: joining channel", "media: this phone is in the channel", "the other person joined"). Please send me those lines.

How to retest: restart the app on both phones (`flutter run`), keep the backend running, and look for `[call ...]` lines in each console.

### Second two-phone test: both on the call screen, never connecting (2 Nov)

The Rejoin / double-Done problems were gone. New symptom: both phones on the call screen, "Waiting for X to join…", chat dead. Evidence from the server:
the requester's phone **did** join the Agora room (`call_joined` logged, SDK said "this phone is in the channel"), the **driver's phone never did**: it never reported `call_joined`,
and its behaviour events only reached the server when the call ended (that upload only starts after the join step finishes) - so the driver's audio/video setup was stuck,
silently. Both are Android phones, same channel name, same app id, so it is not a token / channel mix-up. The exact stuck step could not be seen from here (that phone isn't on USB).

- **What was changed:** every Agora setup step (`initialize`, `enableVideo`, speaker route, `joinChannel`) now has a time limit and reports how it went; the driver's camera preview now starts **after** joining
  (a camera that is slow to open can no longer keep the driver out of the room); leaving / releasing the engine has time limits; a join failure shows the failure at once instead of waiting for a stuck engine;
  a join the SDK never confirms within 20 s now shows "Reconnect" instead of waiting for ever; and every step, SDK error, camera/microphone state and connection change is **saved with the call's events as `media_diagnostic`**
  (server accepts the new type, migration not needed), so the driver phone's story can be read from the database without its console.
- [ ] **Step I31: Retest, then read the result.** Restart the app fully on both phones (stop `flutter run`, run it again - not hot restart), make a call, and let it sit ~40 s. Then, from `convoy_prod/`:
  ```bash
  /opt/homebrew/opt/postgresql@17/bin/psql postgresql://postgres:postgres@127.0.0.1:5433/convoy_dev -c "select user_id, event_type, details, occurred_at from call_behaviour_events where call_request_id = (select id from call_requests order by created_at desc limit 1) order by occurred_at"
  ```
  (or just tell me "tested" and I will read it). Backend checks: 43 call tests pass; app: 438 pass (+4 new), only the old template test fails.
- **Result of the retest (2 Nov, call 474e7ea5):** Apoorv's phone (Samsung SM-M146B, Android 14) joined the room in 0.3 s (initialize 1.2 s, camera and microphone fine).
  Skand's phone (Samsung SM-A566E, Android 15/16) reported `initialize TIMED OUT after 12 s`: Agora's very first call never finishes on that phone, in **both** roles (it was also the one that never joined in the first test).
  So the flow bugs are fixed, but this is a device-specific hang inside Agora's engine start-up. Cause not yet known: it needs that phone's `adb logcat` (plug the A566E in, I capture while a call is made).
- [ ] **Step I32: Capture the A566E's log.** Plug in the **SM-A566E (Skand's phone)** and tell me; I will start `adb -s RZGL5278P7J logcat` capture, you make one call, I read where Agora's start-up stops.

- Note (corrected below): push notifications cannot be sent from this Mac **as started before**, because the Firebase key path in `.env` is the server's `/home/azureuser/...`. Step I33 fixes that for local testing.

### Ringing a closed app, and one account on several phones (2 Nov)

**Why the ring only appeared with the app open:** the local backend had no working Firebase key (path `/home/azureuser/credentials/...` doesn't exist on this Mac), so **no push was ever sent** from it
(the log has errors, not one success). What you saw was the app polling. The repo's `credentials/firebase-admin.json` is the same Firebase project as the app (`convoy-app-4aa95`) and a dry-run send with it succeeded.

**What was built (backend + app):**
- **Missed-call note for the requester.** When a ring runs out, or the driver hangs up while it rings, the requester now gets a normal notification "Missed 1:1 call" (before, only the driver was told). It works even if the ring push never arrived.
- **A phone holds its call.** The server remembers which phone answered the ring (requester) or started it (driver) (migration 027, from the app's `X-Device-Id`). While the call is on, another phone of the same account:
  cannot answer/join it (it used to be treated as a rejoin, same Agora uid, so the two phones knocked each other off), cannot hang it up (declining on a stale ring screen used to end the *live* call), and its heartbeats are refused (`409 CALL_ON_ANOTHER_DEVICE`).
  If the holding phone has been silent for 20 s (dead battery) the other phone may take the call over, and the first phone then leaves quietly without ending it.
- **The other phones stop ringing** when one answers or declines (a silent `call_cancelled` push to every phone except the one that acted).
- **App:** a call on your other phone is never offered as "Rejoin"; the ring screen says "You answered this call on your other device"; a refused join/heartbeat shows "This call is on another of your devices" (hi/kn/mr texts are mine: please have a native speaker check them, like the others).
- Older apps (no device id) behave exactly as before. Backend: 244 tests pass (15 new in `tests/test_call_devices.py`, 1 push, 1 HTTP); app: 306 call tests pass.

- [ ] **Step I33: Restart the local backend with the Firebase key** (also has the dev wallet switch). From `convoy_prod/`; migration 027 is already applied to your local `convoy_dev` and the scratch test DB:
  ```bash
  FIREBASE_CREDENTIALS_PATH=credentials/firebase-admin.json DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/convoy_dev DEV_WALLET_CREDIT_ENABLED=true .venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000 --reload
  ```
  Then test on the phones: (a) close the requester's app (swipe it away), driver taps Start: the full-screen ring should appear; (b) let a ring run out: the requester gets "Missed 1:1 call".
  If (a) doesn't ring on Samsung: Settings > Apps > Bonut > Battery > "Unrestricted", allow notifications and "Full-screen notifications", and never use Force stop (Android blocks all pushes to a force-stopped app).
- [ ] **Step I34: Test one account on two phones.** Sign the same user in on two phones. Driver starts a call: both ring. Answer on one: the other should stop ringing within a few seconds; if its screen is still up, Answer must show
  "This call is on another of your devices" and Decline must **not** end the call.
- [ ] **Step I35 (before deploying): apply migration 027 to production.** Back up first, then:
  ```bash
  pg_dump "$DATABASE_URL" -Fc -f ~/convoy_backup_$(date +%Y%m%d_%H%M).dump
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction -f migrations/027_call_request_devices.sql   # adds 2 empty columns, safe to run twice
  ```
  Deploy the backend only after this (the new code reads those columns). Not needed for the iPhone: a call screen already shown by a VoIP push cannot be dismissed by a silent push (iOS limit), so on iPhone the other phone keeps ringing until the ring ends; answering there gets the "on another device" message.

### Part J: Location sharing for 48 hours after a truck post (2 Nov)

Built (backend + `convoynew`), tested with fakes, **not yet tried on a real phone**. After a truck is posted (or an expired post is reactivated) the phone sends its location now and then for 48 hours
(about every 30 minutes, not continuous); each report moves the pin of the live post and is kept as the user's last known position. Details: `docs/LOCATION_TRACKING_API.md`.

Your answers I built to: (1) update the live post's pin **and** keep a per-user last location (so hours 24-48, after the post expired, still update the user); (2) Android = a WorkManager job, iPhone = update when the app is opened;
(3) never block posting: ask for "Allow all the time", and if refused just update when the app is opened; (4) stops after 48 h, on logout, or when the post that started it is deleted (and no other post is live).

- **Notice:** a dialog before every new post and before reactivating ("Share your location for 48 hours?"), a line under the form, and a note on the "posted" screen. hi/kn/mr texts are mine: native-speaker check please.
- **Server:** new table `user_location_tracking` (migration 028, already applied to your local `convoy_dev` and the scratch DB), new endpoints `/api/location-tracking/status|location|stop`, hooks in post create / reactivate / delete, a sweep that forgets positions of finished windows. 262 backend tests pass (18 new).
- **App:** `lib/features/location_tracking/`; the job runs in its own isolate (`locationWorkerDispatcher`, started in `main.dart`); app open/resume syncs with the server; logout stops it. New package `workmanager`, new Android permission `ACCESS_BACKGROUND_LOCATION`, new wording in the iOS location text.
- **Not done on purpose:** iPhone background updates. iOS has no reliable 30-minute timer: it needs "Always" location plus native "significant location change" code, which can't be written or tested without a real iPhone. For now an iPhone updates when the app is opened. Say if you want that later.
- **Known limits:** the name on the post's pin is only refreshed when the phone can look up the address (it can fail offline), otherwise the old label stays with new coordinates; Android may delay the job on low battery or "deep sleep" (Samsung); one window per person, so a second phone signed into the same account also reports (the server drops anything faster than once a minute).

- [ ] **Step J1: Try it on the Android phone** (needs a fresh install, not hot reload, because of the new package and permission): `flutter run` on the phone, post a truck, read the dialog, tap "Agree and continue", then on the permission screens choose "Allow all the time".
  To see the job run without waiting 30 minutes, with the phone on USB (job name `convoy-location-sharing`):
  ```bash
  adb shell dumpsys jobscheduler | grep -i -B2 -A8 "convoy"      # shows the job is scheduled
  ```
  then check the server got positions:
  ```bash
  /opt/homebrew/opt/postgresql@17/bin/psql postgresql://postgres:postgres@127.0.0.1:5433/convoy_dev -c "select user_id, started_at, ends_at, last_lat, last_lng, last_reported_at, report_count, stopped_at, stop_reason from user_location_tracking order by started_at desc limit 5"
  ```
  Tell me what you see and I will read the phone's log if the job did not run.
- [ ] **Step J2: Try the stops:** log out (the row should show `stop_reason = logout` and no position), delete the post (`post_deleted`), and check "Reactivate" shows the dialog again.
- [ ] **Step J3 (before deploying): apply migration 028 to production.** Back up first. (The API also creates the table by itself at startup, so this only makes it explicit.)
  ```bash
  pg_dump "$DATABASE_URL" -Fc -f ~/convoy_backup_$(date +%Y%m%d_%H%M).dump
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction -f migrations/028_user_location_tracking.sql
  ```
- [ ] **Step J4 (before the Play Store release): background location needs a declaration.** In Play Console > App content > Sensitive permissions, declare background location for "sharing the truck's location for 48 hours after a post so people can send service requests", and upload the short screen recording Google asks for (the consent dialog is the in-app disclosure). Also update your privacy policy: location is collected in the background for 48 hours after posting and kept only as the last position.

### Stage 6 (the phone rings like a phone call: CallKit on iPhone, full-screen call on Android)

Built, not yet tried on real phones (it can't be tried on the simulator): when the driver starts a call, the requester's phone shows the
**system's own incoming-call screen** even if the app is closed or the phone is locked. **iPhone:** a VoIP push wakes the app, which puts up CallKit
at once (own native code in `ios/Runner/AppDelegate.swift`, deliberately independent of Flutter being started, because iOS punishes an app that is
slow to report the call). **Android:** a data-only high-priority push shows a full-screen call (`flutter_callkit_incoming`). Answer opens the call
(microphone only); Decline cancels the request (same as Decline in the app). If the driver hangs up while it rings, a silent push takes the call
screen down. Also new: a **"Call in progress" notification service on Android** so the microphone / camera keep working when the person switches app.
Everything stays **off** (`oneToOneEnabled = false`), and an iPhone without a VoIP token (or with Apple VoIP not configured) simply gets the normal
notification, as before.

- [ ] **Step I24: Database — add the VoIP token column. Do this BEFORE deploying the backend** (the API never adds columns by itself).
  Back up first, as in Step I3. Then, from `convoy_prod/`:
  ```bash
  psql "$DATABASE_URL" -f migrations/025_device_voip_token.sql        # safe to run twice
  ```
  and `.venv/bin/pip install -r requirements.txt` where the API runs (HTTP/2 for Apple, ES256 signing).

- [ ] **Step I25: Apple (only you can do this).**
  1. developer.apple.com → Certificates, Identifiers & Profiles → **Keys** → **+** → tick **Apple Push Notifications service (APNs)** → Continue → Register → **Download the `.p8` once** (Apple won't show it again). Write down the **Key ID** (10 characters) and your **Team ID** (top right of the page).
  2. Identifiers → `com.sharkship.bonut` → make sure **Push Notifications** is ticked.
  3. In Xcode (`ios/Runner.xcworkspace`) → target **Runner** → **Signing & Capabilities** → **+ Capability** → **Push Notifications**, and under **Background Modes** tick **Voice over IP** and **Audio, AirPlay and Picture in Picture**
     (the project has **no entitlements file** today, so this step also creates `Runner.entitlements`; if Firebase notifications ever worked on iPhone, this was done on another machine).
  4. Firebase console → Project settings → Cloud Messaging → upload the same `.p8` for the normal notifications (if not already).
  5. On the server set (e.g. in the `.env` the API reads), then restart it:
     ```
     APNS_KEY_ID=ABCDE12345
     APNS_TEAM_ID=TEAM123456
     APNS_KEY_PATH=/home/azureuser/credentials/AuthKey_ABCDE12345.p8      # or put the whole file text in APNS_KEY_P8 with \n for line breaks
     APNS_BUNDLE_ID=com.sharkship.bonut                                   # the default; only set if different
     ```
     Never commit the `.p8` or these values.

- [ ] **Step I26: App — install, then run the tests.** From `~/Documents/Sharkship-apps/convoynew`:
  ```bash
  flutter pub get
  cd ios && LANG=en_US.UTF-8 pod install && cd ..
  flutter test test/features/call_request test/features/wallet test/features/devices     # 346 passed
  flutter test                                                                           # 423 passed, 1 failed (the old template test, Step I11)
  ```
  Both apps compile (checked): `flutter build ios --simulator --no-codesign --debug` and `flutter build apk --debug`.
  (The Podfile now pins every pod to iOS 15.6: the CallKit package's own pod said 15.0 while its dependency needs 15.6.)

- [ ] **Step I27: Real-phone test of the ring** (needs Steps I24–I25 done, the app on two real phones, `oneToOneEnabled = true` in a test build,
  and the scratch backend or a test deployment; use TestFlight or Xcode for the iPhone). Things to check:
  1. **iPhone, app closed (swipe it away) and phone locked:** driver starts a call → the iPhone shows the system call screen with the driver's name → **Answer** → the app opens into the call, microphone only → you hear the driver and see their camera. After the call ends the system call disappears.
  2. **iPhone, app open:** one call screen only (the system one), not two.
  3. **Decline** on the system screen: the request is cancelled and the money released. **Ignore it:** it stops ringing after the 45 s.
  4. The driver **cancels** while it rings: the call screen disappears (a few seconds' delay is normal on iPhone).
  5. **Audio after answering from the lock screen:** both directions work. If it is silent or one-way, tell me (this is the one thing I couldn't confirm: how CallKit's audio session and Agora share the microphone).
  6. **Android, app closed:** full-screen call appears (Android 14 asks once for "full-screen notifications"; Xiaomi/Oppo/Vivo may also need "auto-start" / battery "no restrictions" for the app). Answer → call opens. Decline / ignore / driver cancels as above.
  7. **Android, switch to another app during a call** for 30 s and come back: the other person kept hearing you ("Call in progress" notification is visible meanwhile).
  8. A phone with **no VoIP token** (or with the APNs variables unset): still gets the normal "Incoming 1:1 call" notification; tapping it opens the ring screen.
  9. An iPhone **debug build vs TestFlight build:** both ring (the app tells the server which Apple server each token belongs to).

> Decisions I made (tell me if any is wrong):
> - **iPhone uses my own small native CallKit layer, not the plugin.** This project starts with the newer "scenes" lifecycle, so on a call that wakes a closed phone the plugin might not be loaded yet when iOS needs the call reported; mine doesn't depend on Flutter. (The plugin is used on Android.)
> - **Declining on the system screen cancels the request** (like in-app Decline). It is one tap on a lock screen, so a mistaken Decline costs the requester the request; the driver can send a new one.
> - **A VoIP push is never used to cancel a call** (iOS requires every VoIP push to show a call); cancellation is a silent data push, best effort, plus the ring timing out.
> - **Android asks for notification / full-screen permissions once, after login** (not at launch, and never again).
> - The ring push waits at most 6 s for Apple / Google, so a slow push service can't eat the 45 s ring.
> - **Play Store / App Store:** Apple needs the "Voice over IP" background mode justified (it is a real calling feature); Google asks you to declare the full-screen-intent and foreground-service (microphone / camera) permissions in the Play Console.

### Stage 7 (adding money to the wallet with Razorpay)

Built, not yet tried against Razorpay: **Add money** on the wallet card and in the 1:1 form's "you need ₹X more" notice → type the amount you want
**in the wallet** → the sheet shows **"Added to your wallet ₹500 + Payment gateway fee ₹12.09 = You pay ₹512.09"** → Razorpay's checkout (UPI, cards, ...) →
the server checks the payment with Razorpay itself and credits ₹500. Money is credited **only after the server has verified the payment** (signature + a lookup at
Razorpay: right order, right amount, captured), never because the app said so, and **exactly once** even when the app's confirmation, Razorpay's webhook and retries
all arrive (even at the same moment). If the person pays and then loses signal or closes the app, the webhook credits it. After paying, the person is never asked to pay again.
It is off until you switch it on (`rechargeEnabled = false`).

- [ ] **Step I28: Database** (the API also creates this table itself at startup; this just makes the shared database ready first). Back up, then:
  ```bash
  psql "$DATABASE_URL" -f migrations/026_wallet_recharges.sql          # safe to run twice
  ```

- [ ] **Step I29: Razorpay (only you can do this).** Start with **Test Mode** (the toggle at the top of the Razorpay dashboard).
  1. Settings → **API Keys** → Generate **Test** Key → note the **Key Id** (`rzp_test_…`) and **Key Secret**.
  2. Settings → **Payment Capture** → set to **Automatic**.
  3. Settings → **Webhooks** → Add New Webhook: URL `https://<your API>/api/webhooks/razorpay`, a secret you invent (write it down), events **payment.captured** and **order.paid**. (For a local test use a tunnel such as `ngrok http 8000`.)
  4. On the server set, then restart:
     ```
     RAZORPAY_KEY_ID=rzp_test_xxxxxxxx
     RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxx
     RAZORPAY_WEBHOOK_SECRET=the-secret-from-step-3
     # optional, defaults shown:
     RECHARGE_FEE_PERCENT=2.36      # 2% + 18% GST on it: set it to what YOUR Razorpay contract says (UPI may differ)
     RECHARGE_FEE_FIXED_INR=0
     RECHARGE_MIN_INR=10
     RECHARGE_MAX_INR=10000
     ```
     Check: `GET /api/wallet/recharge/config` now says `"enabled": true`.
  5. Going live later = swap in the **Live** keys and a live webhook (needs Razorpay KYC activation). Never put keys in the app or in git.

- [ ] **Step I30: Test it** (test build with `rechargeEnabled = true` and `devWalletCreditEnabled = false` in `lib/core/constants/feature_flags.dart`):
  1. Wallet → **Add money** → ₹500 → shows the fee and ₹512.09 → **Pay** → choose UPI / card in Razorpay's test checkout (test UPI id `success@razorpay`; test card `4111 1111 1111 1111`, any future date, any CVV; `failure@razorpay` fails) → "₹500.00 added" and the balance shows ₹500.
  2. **Close the checkout** without paying: nothing changes, no error. **Failed payment:** message, nothing charged, try again works.
  3. Pay, then **turn on airplane mode right as it finishes** (or kill the app): later the balance still goes up by ₹500 (the webhook), exactly once.
  4. In the **1:1 request form** with too little money: "Add money" opens the same sheet pre-filled with the shortfall; after paying, the request can be sent.
  5. Look at `select status, credit_amount, fee_amount, total_amount from wallet_recharges order by created_at desc;` — one `paid` row per payment. A row stuck on **`review`** means "paid but not credited" (frozen wallet, or an amount that didn't match): fix by hand, then ask me for the small admin step if you want one.

> Decisions I made (tell me if any is wrong):
> - **The fee is grossed up** (`total = amount / (1 − 2.36%)`, rounded up), so after Razorpay takes its cut the wallet gets exactly what was asked and you lose nothing. The plain alternative (`amount × 1.0236`) leaves you slightly short on big amounts.
> - **Whole rupees only** in the app (the server accepts paise); ₹10 – ₹10,000 per recharge; at most 10 unpaid orders in 10 minutes.
> - **One failed attempt doesn't kill the order**: the person can retry on the same order in Razorpay's checkout (up to 3 times).
> - The amounts the server stored when the order was made are what is checked against the payment; nothing the app sends afterwards can change them.

> **Doubts I want YOU to decide before going live with Razorpay (not code problems, rules):**
> 1. **Recharged money can currently be redeemed to UPI** (after KYC), like earnings. That lets someone put ₹10,000 in and take it out again (you lose the gateway fee both ways, and it looks like money movement through you). Safer: only **earned** money (call earnings, rewards) is redeemable. Say the word and I'll add that rule (it needs the ledger to remember where each rupee came from).
> 2. **A wallet that holds users' money and pays other users** can fall under **RBI's prepaid payment instrument (PPI) rules**. Ask your CA / lawyer before going live; this is why the scope so far is small limits and a dev-friendly switch.
> 3. **Refunds:** unspent wallet money can't be refunded to the original card/UPI yet, and chargebacks aren't handled. Decide the policy (e.g. "wallet money is non-refundable" in the terms), then I can build it.
> 4. **GST / invoices:** you collect the fee on top; check whether you must issue anything for it.

> **Known limits:** the Razorpay and iPhone/Android call-screen layers are only compile-checked and tested with fakes until Steps I27 / I30; the stale-result guard in
> the Razorpay wrapper (ignoring a payment result left over from an earlier order) can't be unit-tested against the real plugin.
> **Housekeeping I noticed (not mine):** the Android release keystore password is written in `android/app/build.gradle.kts` (committed to git): move it to a
> local, untracked `key.properties`.

> **Coming next (not built):** nothing in the plan. Open items: Step I6 (Agora chat via RTM, not needed any more), optional "requester can't publish video" hardening in the Agora token, admin tooling for `review` recharges.

---

## Housekeeping

- [ ] Nothing from the last several sessions is committed. Review and commit when you're ready (Claude won't
  commit unless asked).
- [ ] Add `logs/*.log` and `scripts/.geocode_cache.json` to `.gitignore`.
