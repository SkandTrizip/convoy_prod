# Reels API: frontend reference

Everything the frontend needs for the reels feature, as of 2026-10-23. Covers new endpoints, changed
existing ones, request/response shapes and error cases. Live schema: `/docs` (Swagger) on the running API.

## Conventions

- **Base path:** `/api`. Example: `GET /api/reels`.
- **Auth:** `Authorization: Bearer <JWT>` (the user JWT from `POST /api/auth/verify-otp`) on every endpoint
  below unless marked **Public** or **Admin**. Missing/invalid token gives `401`.
- **Admin endpoints** need the separate admin token from `POST /api/admin-auth/login`. Not for the user app.
- **JSON keys are camelCase.** IDs are UUID strings.
- **Timestamps** (`createdAt`, `postedAt`, `updatedAt`) are ISO-8601 **without a timezone suffix but in
  UTC**, for example `"2026-10-23T10:15:30.123456"`. Parse them as UTC, not local time.
- **Errors:** `{"detail": "message"}` with status 400 (bad input), 401, 403, 404, 409, 422 (request body
  failed validation; `detail` is then a list), 502 (Bunny returned an error), 503 (a required service such as Bunny isn't configured).
  An id that isn't a valid UUID returns `400`.
- **Pagination:** `limit` (1–100, default 20) and `offset` (default 0). Lists return `total` (the full count
  matching the query, not the page size), `limit` and `offset`.
- **"Reel" = "video" with `status: "ready"`.** The same object is returned from `/videos` and `/reels`.

## The reel object

Returned by `GET /reels`, `GET /reels/{id}`, `GET /videos`, `GET /videos/{id}`, `POST /videos`,
`POST /videos/{id}/sync-status` and `GET /users/{id}/videos`.

```json
{
  "id": "6b0e…",
  "userId": "a1c4…",
  "title": "Night run to Pune",
  "description": "Optional text, or null",
  "status": "ready",
  "durationSeconds": 24,
  "playbackUrl": "https://<bunny-cdn>/…/playlist.m3u8",
  "thumbnailUrl": "https://<bunny-cdn>/…/thumbnail.jpg",
  "streamUrl": "/api/videos/6b0e…/fast-start.m3u8",
  "rejectionReason": null,
  "hashtags": ["trucklife", "night_run"],
  "location": { "city": "Nagpur", "state": "Maharashtra" },
  "categories": [{ "id": "9f2…", "slug": "on-the-road", "name": "On the Road" }],
  "likeCount": 12,
  "commentCount": 3,
  "shareCount": 5,
  "likedByMe": false,
  "author": {
    "id": "a1c4…",
    "name": "Ramesh",
    "profilePhoto": "/api/user/profile-photo/a1c4…",
    "followedByMe": true,
    "followerCount": 87,
    "isMe": false
  },
  "postedAt": "2026-10-23T10:15:30.123456",
  "createdAt": "2026-10-23T10:15:30.123456",
  "updatedAt": "2026-10-23T10:16:02.000000"
}
```

Notes:
- **Play `streamUrl`**, not `playbackUrl`. It is a relative path (prefix your API host) served by our API
  with a warm first segment. It is **public** (no token), because HLS players can't send headers. It is
  `null` until `status` is `ready`. `playbackUrl` / `thumbnailUrl` are Bunny CDN links, also `null` until ready.
- `status` is one of `pending_upload`, `processing`, `ready`, `rejected`, `failed`. `rejectionReason` is set
  for `rejected` (for example longer than 30 seconds) and `failed`.
- `location` is only city and state, or `null` if the uploader didn't send GPS or it couldn't be resolved.
  Exact coordinates are never returned. `city` or `state` may individually be `null`.
- `author` is `null` only if the uploader's account no longer exists. Hide the follow button when
  `author.isMe` is true. `author.profilePhoto` is a relative path (prefix your host) or a full URL or `null`.
- `viewCount` is **not** part of this object (views are private to the owner; see creator stats below).
- `likedByMe` and `followedByMe` are relative to the logged-in viewer.

---

## 1. Categories

### `GET /api/reel-categories` (new)
Active categories in display order. Use for the upload picker and the feed filter chips.

Response `200`:
```json
{ "success": true,
  "categories": [
    { "id": "9f2…", "slug": "truck-tour", "name": "Truck Tour", "sortOrder": 10, "isActive": true }
  ] }
```
Starter list: Truck Tour, On the Road, Loading & Unloading, Maintenance & Repair, Driver Life, Dhaba & Food,
Tips & Advice, Funny, Other.

---

## 2. Upload a reel

Flow: (1) `POST /videos` → (2) upload the file straight to Bunny with TUS using the returned URL and headers
→ (3) wait for `ready` (Bunny calls our webhook; in local dev call `sync-status`) → (4) poll `GET /videos/{id}`
or `sync-status` until `status` is `ready` or `rejected`/`failed`. Maximum length **30 seconds**.

### `POST /api/videos` (CHANGED: new required/optional fields, richer response)
Body:
```json
{
  "title": "Night run to Pune",            // required, 1-255 chars
  "description": "optional",                // optional, max 2000
  "categoryIds": ["9f2…", "c41…"],          // REQUIRED (new), 1-5 ids from /reel-categories, active only
  "hashtags": ["#TruckLife", "night_run"],  // optional (new), max 10; '#' optional, case-insensitive;
                                            //   only letters, digits, underscore; duplicates collapsed
  "latitude": 21.1458,                      // optional (new), -90..90
  "longitude": 79.0882                      // optional (new), -180..180; send BOTH or NEITHER
}
```
Response `201`:
```json
{ "success": true,
  "video": { /* reel object, status "pending_upload", streamUrl null */ },
  "tusUploadUrl": "https://video.bunnycdn.com/tusupload",
  "tusHeaders": { "AuthorizationSignature": "…", "AuthorizationExpire": "…", "VideoId": "…", "LibraryId": "…" },
  "expiresAt": "2026-10-23T11:15:30+00:00" }
```
Use `tusUploadUrl` + `tusHeaders` as-is with a TUS client. They expire at `expiresAt`.
Errors: `400` (no categories / more than 5 / unknown or inactive category id / bad hashtag / more than 10
hashtags), `422` (only one of latitude/longitude, out-of-range values, missing title), `503` (Bunny not
configured). Send location only after the user grants GPS; it is optional and a reel is never rejected
because the city lookup failed.

### `POST /api/videos/{videoId}/sync-status` (response shape CHANGED: now the full reel object)
Owner only (`403` otherwise). Re-checks Bunny and updates the status. Use it when the webhook can't reach the
server (local dev) or to refresh. Response `200`: `{ "success": true, "video": { reel object } }`.

### `GET /api/videos/{videoId}` (response shape CHANGED: full reel object)
Any status, any user (so the uploader can poll). Response `200`: `{ "success": true, "video": { reel object } }`.

### `GET /api/users/{userId}/videos` (response shape CHANGED: full reel objects)
Owner only (`403` for any other user id). **All** of the caller's uploads in any status, newest first, for a
"my uploads / processing" screen. Response: `{ success, videos: [reel object], total, limit, offset }`.

---

## 3. Feed

### `GET /api/reels` (CHANGED)
Ready reels, newest first. **The caller's own reels are no longer included** (new).

Query (all optional):
| param | meaning |
|---|---|
| `limit`, `offset` | pagination |
| `category` | category **id or slug** (new). Repeat it (`?category=a&category=b`) or comma-separate for several; a reel matching **any** is returned. Unknown value gives `400`. |
| `hashtag` | one hashtag, with or without `#`, case-insensitive (new) |

`category` and `hashtag` together mean reels matching both. Response `200`:
```json
{ "success": true, "reels": [ /* reel objects */ ], "total": 120, "limit": 20, "offset": 0 }
```
Without any filter you get the unfiltered feed. The feed chips come from `GET /reel-categories`.

### `GET /api/videos` (CHANGED)
Same data, same filters and the same own-reels exclusion as `/reels`, but the list key is `videos` instead
of `reels`. Prefer `/reels` for the watch feed.

### `GET /api/reels/{reelId}` (new)
One **ready** reel (404 `"Reel not found"` otherwise). Not filtered: a user can open their own reel from
their profile. Response `200`: `{ "success": true, "reel": { reel object } }`.

### `POST /api/videos/{videoId}/watching` (unchanged, still used for prefetch)
Body `{ "feedPosition": 4 }` (index of the reel on screen). Returns `202`. Optional, makes the next reels
start faster.

---

## 4. Views and creator stats

### `POST /api/reels/{reelId}/view` (new)
Call **once per watch**, after the viewer has watched about 2–3 seconds (or the whole reel if shorter).
Don't call it on a timer. Counts can lag a few seconds.

No body. Response `202`: `{ "success": true, "counted": true }`.
`counted` is `false` when the viewer is the creator, or the same user reported the same reel within the last
30 seconds. The client doesn't need to react to it.

### `GET /api/users/{userId}/reels` (new)
A creator's ready reels, newest first, plus totals. Works for your own profile and anyone else's.
Query: `limit`, `offset`.

Response `200` for **your own** profile (`isOwner: true`):
```json
{ "success": true,
  "isOwner": true,
  "summary": { "totalReels": 14, "totalLikes": 230, "totalViews": 5120 },
  "reels": [
    { "id": "6b0e…", "title": "Night run", "thumbnailUrl": "https://…/thumbnail.jpg",
      "durationSeconds": 24, "likeCount": 12, "commentCount": 3,
      "postedAt": "2026-10-23T10:15:30.123456", "viewCount": 410 }
  ],
  "total": 14, "limit": 20, "offset": 0 }
```
For **another user's** profile the same shape is returned, but **`summary.totalViews` and each reel's
`viewCount` are omitted** (views are private to the owner). `summary.totalReels` equals `total`.
`404` if the user doesn't exist or is suspended. To open a reel from the grid, call `GET /reels/{id}`.

---

## 5. Likes

### `POST /api/reels/{reelId}/like` (new) / `DELETE /api/reels/{reelId}/like` (new)
No body. Both are safe to repeat (liking twice does nothing extra). Response `200`:
```json
{ "success": true, "likedByMe": true, "likeCount": 13 }
```
(`likedByMe` is `false` for `DELETE`.) Update the UI optimistically, then reconcile with `likeCount`.
`404` if the reel isn't ready or doesn't exist.

---

## 6. Comments

Comments are flat (no replies). Newest first.

### `GET /api/reels/{reelId}/comments` (new)
Query: `limit`, `offset`. Response `200`:
```json
{ "success": true,
  "comments": [
    { "id": "d3e…", "reelId": "6b0e…", "body": "Nice route!",
      "author": { "id": "a1c4…", "name": "Suresh", "profilePhoto": "/api/user/profile-photo/a1c4…" },
      "canDelete": true,
      "createdAt": "2026-10-23T11:00:00.000000" }
  ],
  "total": 3, "limit": 20, "offset": 0 }
```
`canDelete` is `true` on the viewer's own comments, and on **every** comment if the viewer owns the reel.
`author` can be `null` if that account was deleted. Comment authors do **not** include the follow fields.

### `POST /api/reels/{reelId}/comments` (new)
Body: `{ "body": "Nice route!" }` (1–1000 characters, whitespace-only is rejected with `400`).
Response `201`:
```json
{ "success": true, "comment": { /* same comment object, canDelete true */ }, "commentCount": 4 }
```

### `DELETE /api/reels/{reelId}/comments/{commentId}` (new)
Allowed for the comment's author or the reel's owner (`403` otherwise, `404` if it isn't a comment of that
reel). Response `200`: `{ "success": true, "commentCount": 3 }`.

---

## 7. Shares

### `POST /api/reels/{reelId}/share` (new)
Call **after** the native share sheet completes. Each call counts (repeat shares included).
Body is optional: `{ "channel": "whatsapp" }` (max 32 chars, for analytics: e.g. `whatsapp`, `copy_link`).
Response `200`: `{ "success": true, "shareCount": 6 }`.

---

## 8. Follow / followers

Any logged-in user can follow any active user (not themselves).

### `POST /api/users/{userId}/follow` (new) / `DELETE /api/users/{userId}/follow` (new)
No body. Both are safe to repeat. Response `200` (counts are for the **target** user):
```json
{ "success": true, "followedByMe": true, "followerCount": 88, "followingCount": 12 }
```
Errors: `400` `"You cannot follow yourself"`, `404` `"User not found"` (unknown or suspended; `DELETE` always
works so you can clean up).

### `GET /api/users/{userId}/follow-stats` (new)
Same response as above, without changing anything.

### `GET /api/users/{userId}/followers` (new) / `GET /api/users/{userId}/following` (new)
Query: `limit`, `offset`. Newest first. Suspended accounts are hidden. Response `200`:
```json
{ "success": true,
  "users": [ { "id": "a1c4…", "name": "Ramesh", "profilePhoto": "…", "followedByMe": false, "isMe": false } ],
  "total": 88, "limit": 20, "offset": 0 }
```
Use `followedByMe` / `isMe` to draw the right button in each row.

---

## 9. Admin: category management (admin token, not the user app)

| Endpoint | Purpose |
|---|---|
| `GET /api/admin/reel-categories` | All categories including deactivated ones |
| `POST /api/admin/reel-categories` | Create. Body `{ "slug": "road-trip", "name": "Road Trip", "sortOrder": 15, "isActive": true }`. Slug: lowercase letters/digits/hyphens. `409` if the slug exists. Response `201` `{ success, category }` |
| `PATCH /api/admin/reel-categories/{id}` | Update any of `name`, `sortOrder`, `isActive`. Slug can't change. Deactivate instead of deleting: old reels keep the tag, it just disappears from the picker. Response `{ success, category }` |

---

## Public (no token) playback endpoints, unchanged

`GET /api/videos/{id}/fast-start.m3u8` (what `streamUrl` points at) and
`GET /api/videos/{id}/preview-segment`. No changes; just don't send auth.

## Quick summary: what changed vs. what's new

| | Endpoint |
|---|---|
| **Changed** | `POST /videos` (new required `categoryIds`; optional hashtags and GPS; richer response) |
| **Changed** | `GET /reels`, `GET /videos` (own reels hidden; `category`/`hashtag` filters; richer reel object) |
| **Changed** | `GET /videos/{id}`, `POST /videos/{id}/sync-status`, `GET /users/{id}/videos` (now return the full reel object) |
| **New** | `GET /reel-categories`, `GET /reels/{id}` |
| **New** | `POST/DELETE /reels/{id}/like`; `GET/POST /reels/{id}/comments`; `DELETE /reels/{id}/comments/{commentId}`; `POST /reels/{id}/share`; `POST /reels/{id}/view` |
| **New** | `GET /users/{id}/reels` (creator stats) |
| **New** | `POST/DELETE /users/{id}/follow`; `GET /users/{id}/follow-stats|followers|following` |
| **New (admin)** | `GET/POST /admin/reel-categories`, `PATCH /admin/reel-categories/{id}` |

## Not built yet (don't expect these)

Notifications for likes, comments or new followers; replies and comment likes; a "following only" feed;
search by text; a "reels near me" feed (the location is stored for it but no endpoint yet).
