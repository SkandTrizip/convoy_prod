# Heatmap API: frontend reference

Read-only endpoints for the app's Heatmap screen:

- two that return **aggregated H3 hexagon cells** for a map heat layer: where live trucks are, and where reels were
  posted;
- two for **real truck pins** (Trucks tab only, once the map is zoomed in on a city): the live trucks inside the
  visible map box, and the full details of one truck when its pin is tapped.

Live schema: `/docs` (Swagger) on the running API.

## Conventions

- **Base path:** `/api`. **Auth:** `Authorization: Bearer <JWT>` (missing/invalid gives `401`).
- **JSON keys are camelCase.** `generatedAt` is ISO-8601 in UTC without a timezone suffix.
- **Errors:** `{"detail": "message"}`. `422` for a bad `resolution`, `500` if the aggregation fails.

## Endpoints

| Endpoint | What is counted |
|---|---|
| `GET /api/heatmap/trucks` | Live truck posts (active status and not past `expiresAt`), at the post's **current location**. A post whose current location is missing or malformed falls back to its origin point. Out-of-range coordinates are ignored. |
| `GET /api/heatmap/reels` | Reels with `status = ready` that were posted **with a location**, within the last `HEATMAP_REEL_WINDOW_DAYS` days (default 30). Reels posted without GPS are not counted. |

Query parameter (both): `resolution`, an integer **3 to 9**, default **6**. Anything else is a `422`.

```json
{
  "success": true,
  "type": "trucks",
  "resolution": 6,
  "minCellActivity": 1,
  "cellCount": 243,
  "truncated": false,
  "generatedAt": "2026-10-27T05:12:09.481233",
  "cells": [
    { "h3Cell": "863da1147ffffff", "centerLat": 28.6241, "centerLng": 77.2219, "count": 7 }
  ]
}
```

- `cells` are sorted by `count` descending. `count` is the number of trucks/reels in that cell.
- `minCellActivity` is the smallest `count` a returned cell can have (see privacy, below).
- `truncated` is `true` only if more than 5,000 cells matched and the weakest were dropped.
- `type` is `"trucks"` or `"reels"`.

## Reels: tapping a hotspot opens that area's reels

The Reels tab of the app never zooms in past city level (zoom 11.5) and has no pins. Tapping a hotspot takes the
user to the Reels tab narrowed to that cell: `GET /api/reels?h3Cell=<cell>&sort=popular` (see
[`REELS_API.md`](REELS_API.md#area-feed-h3cell)). The `h3Cell` is the one from the tapped cell of
`GET /api/heatmap/reels`; the feed holds exactly the reels that cell counted.

## Real truck pins (Trucks tab, zoomed in on a city)

Reels never switch to pins; they stay a heatmap at every zoom.

### `GET /api/heatmap/trucks/pins`

The live trucks inside a map box. Query: `minLat`, `minLng`, `maxLat`, `maxLng` (the visible map area; all four
required) and `limit` (1 to 1000, default 500).

```json
{
  "success": true,
  "count": 2,
  "truncated": false,
  "generatedAt": "2026-10-27T05:12:09.481233",
  "pins": [
    { "id": "3ed9c13d-…", "lat": 28.6139, "lng": 77.2090, "truckType": "Container", "capacity": 9.0 }
  ]
}
```

- `id` is the truck **post** id. Pass it to the details endpoint below.
- Same rules as the heat cells: a live post (active status, not past `expiresAt`), at its current location (origin
  as fallback), invalid coordinates ignored. A position is **where the truck was when the post was created or edited**,
  not live tracking, so it can be up to 24 hours old.
- Pins are sorted nearest the **centre of the box** first. `truncated: true` means the cap was hit and farther pins
  were left out.
- `400` if the box is inverted/empty or larger than 3 degrees per side (zoom in further). `422` if a parameter is
  missing or out of range.
- No contact details are in this response; they come from the details endpoint, only when a pin is tapped.

### `GET /api/heatmap/trucks/posts/{post_id}`

Full details of one live post, in the **same shape as an item of `POST /api/search/trucks`** (poster `userName`,
`userPhoto`, `userMobile`, `contactName`/`contactNumber`, `truck_type`, `capacity`, `length`/`height`,
`truck_number`, `origin`, `originLocation`, `currentLocation`, `destinations`, `createdAt`, `expiresAt`, `mutuals`),
wrapped as `{ "success": true, "post": { ... } }`, so the app parses it with the model it already has.

- `404` if the post is gone or expired since its pin was drawn (the app shows "no longer available" and refreshes
  the pins). `400` for an id that isn't a UUID.

### Suggested zoom levels (Trucks tab)

| Map zoom | Shows |
|---|---|
| below 12 | heat (resolution 4 to 7, table below) |
| 12 and up (a whole city fits on screen) | real pins; the app switches back to heat only below 11.8 so it can't flicker |
| 12 to <15 | nearby pins grouped into numbered bubbles (tap one to zoom in) |
| 15 to <17.5 | only pins that visually overlap are grouped |
| 17.5 and up | every pin on its own |

## Choosing the resolution from the map zoom

Coarse cells zoomed out give a regional glow; fine cells zoomed in give neighbourhood detail. Re-request when the
zoom crosses a step, and cache the response per resolution.

| Map zoom | `resolution` | Average cell edge |
|---|---|---|
| below 6 | 4 | ~22.6 km |
| 6 to <8 | 5 | ~8.5 km |
| 8 to <10 | 6 | ~3.2 km |
| 10 to <12 | 7 | ~1.2 km |
| 12 to <14 | 8 | ~0.46 km |
| 14 and above | 9 | ~0.17 km |

(Resolution 3 is also accepted, for a whole-country view.)

## Privacy

- The **heat** responses contain only the cell id, the cell centre and a count. **No user id and no exact
  coordinate** is ever returned. (Pins are different on purpose: they show a truck's posted position, the same
  information the existing nearby-trucks map and search already show to any logged-in user.)
- At resolution **8 and 9** a cell is only returned if it holds **at least 3** items (`minCellActivity: 3`), so one
  truck or one reel can't be pinpointed. Zoomed far in, areas with fewer than 3 items are therefore blank: an
  empty response at a fine resolution is normal, not an error.

## Notes for operators

- No database migration is needed. Aggregation runs in SQL (distinct points rounded to ~11 m, with a count) and the
  H3 cell is computed in Python with the `h3` package (added to `requirements.txt`).
- To try it without touching real data, seed a **scratch** database: `scripts/seed_heatmap_dummy.py` (it refuses to
  run against a non-local `DATABASE_URL`).
