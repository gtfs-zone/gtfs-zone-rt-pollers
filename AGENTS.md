# AGENTS.md

Sidecar worker that polls an upstream live tracker, resolves each vehicle to a
GTFS trip instance, and POSTs positions and per-stop trip updates to rt-api's
ingest API. One process runs one source (`SOURCE=amtrak|buswhere|hudsonlink`), one
container per source. A `v*` tag publishes the image.

## Commands

```bash
uv run python scripts/build_buswhere_map.py --watch   # extend the buswhere stop map
```

## Architecture

`main.py` picks a `Source` (`sources/base.py`) by `SOURCE`, runs the poll loop,
and hands provider-neutral `VehicleUpdate`s to `publisher.py`, which POSTs each
cycle in chunks to `/ingest/positions` and `/ingest/trip-updates`. Env vars are
in [README.md](README.md).

- **amtrak** decrypts Amtrak's getTrainsData feed and resolves by train number
  (`GtfsResolver.resolve()`).
- **buswhere** polls Columbia County (NY) and resolves by route and scheduled
  window (`resolve_by_route()`), mapping stop IDs through
  `sources/buswhere/mapping.json`. The map's keys and the device attribution
  rules are in [docs/buswhere.md](docs/buswhere.md).
- **hudsonlink** polls CoachUSA's journey API and resolves by ordered stops and
  first departure (`resolve_by_pattern()`). Endpoints and quirks are in
  [docs/hudsonlink.md](docs/hudsonlink.md).
- `INGEST_TRACKER_ID` is an rt-api `Tracker.id` (the surrogate, never the
  `device_key`). rt-api's `scripts/provision_source.py` creates the tracker and
  prints it.

## Conventions

- **Commits**: Conventional Commits, enforced by the `commit-msg` hook. Never add
  Co-Authored-By trailers. Setup and release are in [CONTRIBUTING.md](CONTRIBUTING.md).
- **Logging**: module loggers are named `log`, never `logger`.
- **Cross-repo work**: sibling gtfs.zone repos live under the same parent
  directory and may be read and edited when a change spans repos.
- **Plans**: write plans to `CURRENT_PLAN.md` at the repo root as a
  checklist (`- [ ]`), ticked off as work lands. It is neither tracked nor
  gitignored: never stage or commit it.
