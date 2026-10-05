# gtfs-zone-rt-pollers

[![CI](https://img.shields.io/github/actions/workflow/status/gtfs-zone/gtfs-zone-rt-pollers/check.yml?branch=main&label=CI)](https://github.com/gtfs-zone/gtfs-zone-rt-pollers/actions/workflows/check.yml?query=branch%3Amain) [![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue)](LICENSE.txt) [![Container image](https://img.shields.io/badge/image-ghcr.io-blue?logo=docker&logoColor=white)](https://github.com/gtfs-zone/gtfs-zone-rt-pollers/pkgs/container/gtfs-zone-rt-pollers)

Sidecar worker that polls an upstream live tracker (Amtrak, buswhere/Columbia
County or Hudson Link), resolves each vehicle to a GTFS trip, and POSTs positions + per-stop
trip-updates to the rt-api ingest API. Select the source with `SOURCE`.

## Overview

One process runs one source (`SOURCE=amtrak|buswhere|hudsonlink`); deploy one container per
source. Each source polls its upstream tracker, resolves the vehicle to a GTFS
trip instance via the shared `GtfsResolver`, and hands provider-neutral
`VehicleUpdate`s to `publisher.py`, which POSTs them to rt-api. rt-api serves
the resulting GTFS-RT feeds; static-importer loads the static schedules the
resolver matches against.

## Running Locally


```bash
cp .env.example .env  # edit as needed
uv sync

python -m gtfs_zone_rt_pollers.main

```


## Development


```bash
uv sync              # install dependencies
ruff check .         # lint
ruff format .        # format
pre-commit install   # install git hooks (run once after clone)
```


## Environment variables

| Variable | Description |
|---|---|
| `SOURCE` | `amtrak` (default), `buswhere` or `hudsonlink`; one source per process |
| `POLL_INTERVAL` | Seconds between poll cycles (default 15, 60 for hudsonlink) |
| `HTTP_TIMEOUT` | httpx timeout seconds (default 20) |
| `RT_API_INGEST_URL` | rt-api ingest base URL; publishing no-ops if unset |
| `INGEST_API_TOKEN` | Bearer token for the ingest API |
| `INGEST_TRACKER_ID` | Must equal an rt-api `Tracker.id` (the surrogate, not the `device_key`). `INGEST_VEHICLE_ID` is the old name and still works |
| `GTFS_URL` | GTFS zip URL (defaults per source) |
| `GTFS_PATH` | GTFS cache path (dir or `.zip`) |
| `ROUTE_FILTER` | amtrak-only: comma-separated RouteName allowlist |
| `BUSWHERE_ROUTES` | buswhere-only: route slugs to poll (blank = all mapped) |
| `HUDSONLINK_SERVICE_UUID` | hudsonlink-only: CoachUSA service uuid that scopes journey searches (defaults to Hudson Link's) |
