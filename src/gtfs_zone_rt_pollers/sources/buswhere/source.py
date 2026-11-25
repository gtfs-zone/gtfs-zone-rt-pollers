"""buswhere (Columbia County) source.

buswhere reports a route and per-stop ETAs but no trip_id, so this source
resolves the trip itself against the Columbia County GTFS and rewrites
buswhere's internal stop IDs to GTFS stop IDs via the committed mapping. It
never trusts a device-supplied trip.

A route's snapshot also lists buses running other routes, and drivers log into
the wrong route, so the snapshot only narrows which routes a bus may be on.
Each device's newest fix is matched to a trip by position (`TripAssigner`).
"""

from __future__ import annotations

import json
import logging
import time
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from gtfs_zone_rt_pollers.gtfs import GtfsResolver, fetch_gtfs
from gtfs_zone_rt_pollers.sources.base import (
    Source,
    StopTimeUpdate,
    UpstreamError,
    VehicleUpdate,
)

from .assign import Fix, TripAssigner
from .client import BuswhereObservation, fetch_route

if TYPE_CHECKING:
    import httpx

    from gtfs_zone_rt_pollers.config import Config

log = logging.getLogger(__name__)

# buswhere reports the ETA to each stop's *next* visit. On a loop, stops the bus
# already passed this trip resolve to the next loop (~a loop-length away), so we
# only keep a stop when its predicted arrival lands near this trip's scheduled
# time for it. This window is comfortably below a loop length and above any
# plausible lateness, and it also assigns a twice-visited stop (loop origin =
# terminus) to the correct occurrence.
_MAX_SCHEDULE_SKEW = 1800  # seconds


def _load_mapping() -> tuple[dict[str, str], dict[str, str], set[str]]:
    """(routes: slug->route_id, stops: buswhere->GTFS stop_id, unmappable)."""
    raw = json.loads(
        resources.files(__package__).joinpath("mapping.json").read_text("utf-8")
    )
    return raw.get("routes", {}), raw.get("stops", {}), set(raw.get("unmapped", []))


def _device_key(slug: str, obs: BuswhereObservation) -> str:
    """The device id, or the slug for an observation without one."""
    return str(obs.device_id) if obs.device_id is not None else slug


class BuswhereSource(Source):
    name = "buswhere"

    def __init__(self, config: Config) -> None:
        self._config = config
        self._resolver: GtfsResolver | None = None
        self._assigner: TripAssigner | None = None
        self._routes, self._stops, self._unmappable = _load_mapping()
        # Poll the configured slugs, or every slug we have a route mapping for.
        self._slugs = config.buswhere_routes or list(self._routes)
        self._clock = time.time
        # device key -> newest fix timestamp published.
        self._last_sent: dict[str, int] = {}

    async def startup(self, http: httpx.AsyncClient) -> None:
        gtfs_path = await fetch_gtfs(
            self._config.gtfs_url, Path(self._config.gtfs_path), http
        )
        self._set_resolver(GtfsResolver(gtfs_path))

    def _set_resolver(self, resolver: GtfsResolver) -> None:
        self._resolver = resolver
        self._assigner = TripAssigner(resolver)

    def _build(
        self,
        slug: str,
        obs: BuswhereObservation,
        trip_id: str,
        start_date: str,
        unmapped_stops: set[str] | None = None,
    ) -> VehicleUpdate:
        resolver = self._resolver
        assert resolver is not None
        route_id = resolver.route_for(trip_id) or self._routes.get(slug)

        # buswhere ETA keyed by GTFS stop (min ETA if two buswhere stops collapse
        # onto one GTFS stop).
        eta_by_stop: dict[str, float] = {}
        for bid, eta_seconds in obs.stop_eta.items():
            gtfs_stop = self._stops.get(bid)
            if gtfs_stop is None:
                # Stops reviewed and found to have no GTFS counterpart at all are
                # expected; only an unreviewed one is worth a warning.
                if unmapped_stops is not None and bid not in self._unmappable:
                    unmapped_stops.add(bid)
                continue
            if gtfs_stop not in eta_by_stop or eta_seconds < eta_by_stop[gtfs_stop]:
                eta_by_stop[gtfs_stop] = eta_seconds

        # Walk the trip's scheduled stops in order; emit the ones whose predicted
        # arrival lands near their scheduled time (i.e. still ahead this loop).
        stop_time_updates: list[StopTimeUpdate] = []
        for seq, gtfs_stop, scheduled in resolver.trip_schedule(trip_id, start_date):
            eta_seconds = eta_by_stop.get(gtfs_stop)
            if eta_seconds is None:
                continue
            arrival_time = obs.timestamp + int(eta_seconds)
            if abs(arrival_time - scheduled) > _MAX_SCHEDULE_SKEW:
                continue  # next-loop arrival, not this trip's visit
            stop_time_updates.append(
                StopTimeUpdate(
                    stop_id=gtfs_stop,
                    stop_sequence=seq,
                    arrival_time=arrival_time,
                    arrival_delay=arrival_time - scheduled,
                )
            )

        # The surviving updates are this trip's remaining visits, in schedule
        # order, so the first one is the stop the bus is running towards.
        # buswhere reports an ETA to every stop and nothing about arrival, so
        # IN_TRANSIT_TO is all we can honestly claim, never STOPPED_AT.
        next_stop = stop_time_updates[0] if stop_time_updates else None

        return VehicleUpdate(
            tracker_id=self._config.tracker_id,
            # buswhere's device_id is the bus, whichever route it is logged
            # into.
            vehicle_id=_device_key(slug, obs),
            vehicle_label=obs.vehicle_name or slug,
            trip_id=trip_id,
            start_date=start_date,
            timestamp=obs.timestamp,
            lat=obs.lat,
            lon=obs.lon,
            route_id=route_id,
            # buswhere's `speed` unit is ambiguous (GPS knots vs mph) and its
            # feed carries no heading, so we publish neither rather than emit a
            # wrong-unit value.
            speed_mps=None,
            bearing=None,
            current_stop_sequence=next_stop.stop_sequence if next_stop else None,
            current_stop_id=next_stop.stop_id if next_stop else None,
            current_status="IN_TRANSIT_TO" if next_stop else None,
            stop_time_updates=stop_time_updates,
        )

    async def fetch(self, http: httpx.AsyncClient) -> list[VehicleUpdate]:
        assert self._assigner is not None, "startup() must run before fetch()"

        snapshots: dict[str, list[BuswhereObservation]] = {}
        last_error: Exception | None = None
        for slug in self._slugs:
            try:
                snapshots[slug] = await fetch_route(http, slug)
            except Exception as exc:
                # One malformed/unreachable route must not abort the whole
                # cycle and suppress the routes that parse cleanly.
                log.exception("buswhere: route %s failed, skipping", slug)
                last_error = exc
        if self._slugs and not snapshots:
            raise UpstreamError(
                f"all {len(self._slugs)} buswhere routes failed: {last_error!r}"
            )

        # One entry per device: its newest fix (richest ETAs on a tie) and every
        # slug listing it.
        devices: dict[str, tuple[str, BuswhereObservation, set[str]]] = {}
        for slug, observations in snapshots.items():
            if slug not in self._routes:
                log.warning("no route mapping for buswhere slug %s", slug)
                continue
            for obs in observations:
                key = _device_key(slug, obs)
                entry = devices.get(key)
                if entry is None:
                    devices[key] = (slug, obs, {slug})
                    continue
                entry[2].add(slug)
                best = entry[1]
                if (obs.timestamp, len(obs.stop_eta)) > (
                    best.timestamp,
                    len(best.stop_eta),
                ):
                    devices[key] = (slug, obs, entry[2])

        fixes = [
            Fix(
                key=key,
                lat=obs.lat,
                lon=obs.lon,
                timestamp=obs.timestamp,
                route_ids=frozenset(self._routes[s] for s in slugs),
            )
            for key, (_, obs, slugs) in devices.items()
            # A fix older than one already published would move the bus back.
            if obs.timestamp >= self._last_sent.get(key, 0)
        ]
        chosen = self._assigner.assign(fixes, self._clock())

        updates: list[VehicleUpdate] = []
        unmapped_stops: set[str] = set()
        for key, (trip_id, start_date, route_id) in chosen.items():
            slug, obs, slugs = devices[key]
            # Prefer a slug on the assigned route for the label fallback.
            slug = min((s for s in slugs if self._routes[s] == route_id), default=slug)
            updates.append(self._build(slug, obs, trip_id, start_date, unmapped_stops))
            self._last_sent[key] = obs.timestamp

        unassigned = len(fixes) - len(chosen)
        if unassigned:
            log.debug("buswhere: %d device(s) on no trip", unassigned)
        if unmapped_stops:
            log.warning(
                "buswhere: %d stop id(s) with no GTFS mapping this cycle (%s)",
                len(unmapped_stops),
                ", ".join(sorted(unmapped_stops)),
            )
        return updates
