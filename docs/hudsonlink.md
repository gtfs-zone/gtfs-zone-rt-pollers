# hudsonlink source

Polls the journey API behind ridehudsonlink.com (CoachUSA's shared platform,
operator `HSL`) and resolves each trip against Hudson Link's 511NY GTFS by its
ordered stops and first departure (`resolve_by_pattern`).

## Endpoints

Base `https://api.prod.coachusa.com/`, no auth, responses cached for 60s.

- `GET journey?origin=ext-<code>&destination=ext-<code>&serviceUuid=<uuid>&dateFrom=YYYY-MM-DD`
  searches journeys between two stops. `<code>` is the GTFS `stop_code`.
  Returns up to 300 results spanning a few days, each with a journey `id`, the
  trip's full `stops[]` and scheduled times. Without `serviceUuid` other
  CoachUSA brands leak in.
- `GET journey/<id>/status?date=YYYY-MM-DD` is the live status of a journey:
  `204` when nothing is live, else `vehicleName`, `status`
  (`IN_PROGRESS` | `COMPLETED` | `CANCELLED`), the remaining `stops[]` with
  `{scheduled, estimated?, actual?}` times and `vehiclePosition`. `date` is
  the date of the searched origin's departure; the wrong date returns `204`.

A journey id is per (trip, searched origin, searched destination), and every
id of a trip returns the same live status, so one per trip is enough.

## Discovery

Once per service day the source searches every distinct (first stop, second
stop) pair of the GTFS, from yesterday and from today, and keeps
`(trip_id, start_date) -> journey id` for each matched result. Each poll then
asks for the status of the trips scheduled from 20 minutes before their first
departure to 30 minutes after their last arrival.

## Matching

GTFS trips have no `trip_short_name` and the API's `trip.shortName`
(`5064-MTh`) appears nowhere in the GTFS, so the key is the ordered stop_id
tuple plus first departure on an active service day. A departure after midnight
is tried against the previous service day too (GTFS times past 24:00).

Route is not part of the key: H01X trips are labelled `H01`, and H03/H05 (at
MABRO) and H07/H07X (at TARRYT) leave the same stop at the same minute. A match
whose API route differs from the GTFS route by more than a trailing `X` is
logged.

## Quirks

- `stops[]` in a status lists only the remaining stops, and the final stop is
  sometimes missing. Trip updates carry what is listed (`actual` if present,
  else `estimated`) and nothing for passed stops.
- The first listed stop is published as `IN_TRANSIT_TO`, never `STOPPED_AT`.
- A bus can show `IN_PROGRESS` on its next trip while still finishing the
  current one. Each vehicle is kept on its earliest scheduled trip only.
- `vehiclePosition.lastUpdated` has minute resolution. Positions older than 5
  minutes are dropped.
- `CANCELLED` is logged only.
