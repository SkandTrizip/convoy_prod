# Location sharing for 48 hours after a truck post

Not live tracking. After a user posts a truck (or reactivates an expired post) the phone sends its location
**now and then** (about every 30 minutes, from a background job on Android; whenever the app is opened on any
phone) for **48 hours**, so people who want to send a service request can find them. The server keeps only the
last position, and forgets it when the window ends.

## What the server does by itself
- `POST /api/posts/create/{userId}` and `POST /api/posts/reactivate/{postId}` open (or restart) the window:
  `LOCATION_TRACKING_HOURS` (48) from now. A failure here never fails the post.
- Each stored report moves the pin of the user's **live** posts (`truck_routes.current_location`, so the heatmap,
  search and 1:1 requests see it) and is kept as the user's last known position (`user_location_tracking`).
  Posts that have expired keep their old pin (hours 24 to 48: only the user's own last position updates).
- The window ends: after 48 h; when the user logs out (`/stop`); when the post that started it is deleted and no
  other post is live. Ending it clears the stored position. A sweep (every `POST_EXPIRE_CHECK_INTERVAL_SECONDS`)
  clears windows that ran out even if the phone never reports again.

## Endpoints (all need the login)
### `GET /api/location-tracking/status`
`{ "success": true, "active": true, "startedAt": "...", "endsAt": "...", "lastReportedAt": "...|null", "intervalMinutes": 30, "hours": 48 }`.
When not active: `active: false` and the times are `null`.

### `POST /api/location-tracking/location`
`{ "lat": 19.07, "lng": 72.87, "accuracy": 30, "name": "Pune, Maharashtra", "city": "Pune", "state": "...", "pincode": "..." }`
(`lat`, `lng` required and range-checked; the rest optional: the place parts replace the post's label when given).
Always `200` with the status fields plus `recorded`:
- `active: false` = the window is over (or never opened): the app must stop its background job; nothing is stored.
- `recorded: false` with `active: true` = too soon after the last one (`LOCATION_REPORT_MIN_SECONDS`, 60): acknowledged, not stored.

### `POST /api/location-tracking/stop`
Ends the window now and clears the position (the app calls it on logout). Safe when nothing is on.

## Config (env)
`LOCATION_TRACKING_HOURS=48`, `LOCATION_REPORT_INTERVAL_MINUTES=30` (told to the app), `LOCATION_REPORT_MIN_SECONDS=60`.

## The app (convoynew)
- Before a new post (and before reactivating) a dialog says the location is shared about every 30 minutes for
  48 hours and how to stop it; agreeing is what starts the system permission questions. Posting goes ahead
  whatever the person answers to them.
- Android: a WorkManager job (`workmanager`) every 30 minutes (the system may delay it on low battery), needing
  "Allow all the time" location (`ACCESS_BACKGROUND_LOCATION`). Without it, or on iPhone, the location is sent
  when the app is opened (at most once per 15 minutes).
- The job stops itself after 48 h, when the server says `active: false`, or when logged out.
