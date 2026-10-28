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

## Housekeeping

- [ ] Nothing from the last several sessions is committed. Review and commit when you're ready (Claude won't
  commit unless asked).
- [ ] Add `logs/*.log` and `scripts/.geocode_cache.json` to `.gitignore`.
