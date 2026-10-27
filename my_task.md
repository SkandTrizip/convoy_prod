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

## Housekeeping

- [ ] Nothing from the last several sessions is committed. Review and commit when you're ready (Claude won't
  commit unless asked).
- [ ] Add `logs/*.log` and `scripts/.geocode_cache.json` to `.gitignore`.
