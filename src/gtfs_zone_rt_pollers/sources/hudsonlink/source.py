"""Hudson Link source.

Hudson Link's journey API has no id that matches the GTFS, so trips are matched
by their ordered stops and first departure (`resolve_by_pattern`). Discovery
searches every distinct (first stop, second stop) pair of the GTFS once per
service day and keeps the journey id of each matched trip. Each poll then asks
for the live status of the trips scheduled around now.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from gtfs_zone_rt_pollers.gtfs import GtfsResolver, fetch_gtfs
from gtfs_zone_rt_pollers.sources.base import (
    Source,
    StopTimeUpdate,
    UpstreamError,
    VehicleUpdate,
)

from .client import Journey, JourneyStatus, TimePoint, fetch_status, search

if TYPE_CHECKING:
    from gtfs_zone_rt_pollers.config import Config

log = logging.getLogger(__name__)

_CARRIER = "HSL"
# Status is polled for trips scheduled within this margin of now.
_BEFORE_START = timedelta(minutes=20)
_AFTER_END = timedelta(minutes=30)
_MAX_POSITION_AGE = timedelta(minutes=5)
_DISCOVERY_RETRY = timedelta(minutes=15)
_CONCURRENCY = 5
_MPH_TO_MPS = 0.44704


@dataclass(frozen=True)
class _JourneyRef:
    journey_id: str
    status_date: date  # the `date` the status endpoint expects


def _epoch(dt: datetime | None) -> int | None:
    return int(dt.timestamp()) if dt is not None else None


def _prediction(point: TimePoint | None) -> tuple[int | None, int | None]:
    """(predicted epoch, delay vs scheduled), actual over estimated."""
    if point is None:
        return None, None
    predicted = _epoch(point.predicted)
    scheduled = _epoch(point.scheduled)
    if predicted is None or scheduled is None:
        return predicted, None
    return predicted, predicted - scheduled


def _same_route(api_route: str | None, gtfs_route: str | None) -> bool:
    """Routes match up to a trailing X (H01X trips are labelled H01)."""
    if not api_route or not gtfs_route:
        return True
    return api_route.removesuffix("X") == gtfs_route.removesuffix("X")


def _one_trip_5xx(errors: list[Exception]) -> bool:
    """A lone upstream 5xx, which some trips return on every poll."""
    return (
        len(errors) == 1
        and isinstance(errors[0], httpx.HTTPStatusError)
        and errors[0].response.is_server_error
    )


class HudsonLinkSource(Source):
    name = "hudsonlink"

    def __init__(self, config: Config) -> None:
        self._config = config
        self._resolver: GtfsResolver | None = None
        self._journeys: dict[tuple[str, str], _JourneyRef] = {}
        self._discovered_on: date | None = None
        self._last_discovery: datetime | None = None
        # Trips whose status already returned an upstream 5xx, warned once.
        self._failing: set[tuple[str, str]] = set()
        # Set while the last discovery had every search fail.
        self._discovery_error: str | None = None

    async def startup(self, http: httpx.AsyncClient) -> None:
        gtfs_path = await fetch_gtfs(
            self._config.gtfs_url, Path(self._config.gtfs_path), http
        )
        self._resolver = GtfsResolver(gtfs_path)
        await self._discover(http, datetime.now(UTC))

    def _match(self, journeys: list[Journey]) -> dict[tuple[str, str], _JourneyRef]:
        """Map each journey to its GTFS (trip_id, start_date)."""
        resolver = self._resolver
        assert resolver is not None
        codes = resolver.stop_codes
        matched: dict[tuple[str, str], _JourneyRef] = {}
        unmatched: list[str] = []
        for j in journeys:
            stop_ids = [codes.get(s.stop_code) for s in j.stops]
            resolved = (
                resolver.resolve_by_pattern(
                    [s for s in stop_ids if s is not None], j.stops[0].departure
                )
                if None not in stop_ids
                else None
            )
            if resolved is None:
                unmatched.append(f"{j.trip_name} {j.stops[0].departure.isoformat()}")
                continue
            gtfs_route = resolver.route_for(resolved[0])
            if not _same_route(j.route, gtfs_route):
                log.warning(
                    "hudsonlink: %s labelled %s matched trip %s on %s",
                    j.trip_name,
                    j.route,
                    resolved[0],
                    gtfs_route,
                )
            matched.setdefault(resolved, _JourneyRef(j.id, j.origin_departure.date()))
        if unmatched:
            log.warning(
                "hudsonlink: %d journey(s) matched no GTFS trip (e.g. %s)",
                len(unmatched),
                ", ".join(sorted(unmatched)[:5]),
            )
        return matched

    async def _discover(self, http: httpx.AsyncClient, now: datetime) -> None:
        """Search every first-stop pair for yesterday and today's journeys."""
        resolver = self._resolver
        assert resolver is not None
        self._last_discovery = now
        today = now.astimezone(resolver.timezone).date()
        code_for = {stop_id: code for code, stop_id in resolver.stop_codes.items()}

        journeys: dict[str, Journey] = {}
        searched = 0
        failed = 0
        last_error: Exception | None = None
        for first, second in sorted(resolver.first_stop_pairs()):
            if first not in code_for or second not in code_for:
                continue
            for day in (today - timedelta(days=1), today):
                searched += 1
                try:
                    results = await search(
                        http,
                        code_for[first],
                        code_for[second],
                        self._config.hudsonlink_service_uuid,
                        day,
                    )
                except Exception as exc:
                    log.warning(
                        "hudsonlink: search %s-%s from %s failed: %r",
                        code_for[first],
                        code_for[second],
                        day,
                        exc,
                    )
                    failed += 1
                    last_error = exc
                    continue
                for j in results:
                    if j.carrier_code == _CARRIER:
                        journeys.setdefault(j.id, j)

        matched = self._match(list(journeys.values()))
        # Keep earlier matches so a failed search doesn't drop live trips;
        # anything before yesterday can no longer be running.
        oldest = (today - timedelta(days=1)).strftime("%Y%m%d")
        self._journeys = {
            key: ref
            for key, ref in {**self._journeys, **matched}.items()
            if key[1] >= oldest
        }
        self._failing &= self._journeys.keys()
        if not failed:
            self._discovered_on = today
        self._discovery_error = (
            f"all {searched} hudsonlink searches failed: {last_error!r}"
            if searched and failed == searched
            else None
        )
        log.info(
            "hudsonlink: discovery found %d journeys, matched %d trips "
            "(%d searches failed)",
            len(journeys),
            len(matched),
            failed,
        )

    def _stop_time_updates(
        self, trip_id: str, start_date: str, status: JourneyStatus
    ) -> list[StopTimeUpdate]:
        resolver = self._resolver
        assert resolver is not None
        codes = resolver.stop_codes
        schedule = resolver.trip_schedule(trip_id, start_date)
        updates: list[StopTimeUpdate] = []
        for s in status.stops:
            stop_id = codes.get(s.stop_code)
            visits = [(seq, at) for seq, sid, at in schedule if sid == stop_id]
            if not visits:
                continue
            # A stop visited twice takes the visit nearest its scheduled time.
            scheduled = _epoch((s.arrival or s.departure or TimePoint(None)).scheduled)
            seq = min(visits, key=lambda v: abs(v[1] - (scheduled or v[1])))[0]
            arrival_time, arrival_delay = _prediction(s.arrival)
            departure_time, departure_delay = _prediction(s.departure)
            update = StopTimeUpdate(
                stop_id=stop_id,
                stop_sequence=seq,
                arrival_time=arrival_time,
                arrival_delay=arrival_delay,
                departure_time=departure_time,
                departure_delay=departure_delay,
            )
            updates.append(update)
        return updates

    def _build(
        self, trip_id: str, start_date: str, status: JourneyStatus
    ) -> VehicleUpdate | None:
        resolver = self._resolver
        assert resolver is not None
        pos = status.position
        if pos is None:
            return None
        stop_time_updates = self._stop_time_updates(trip_id, start_date, status)
        # Only the remaining stops are listed, so the first is the next one.
        # Nothing says the bus is at it, so never STOPPED_AT.
        next_stop = stop_time_updates[0] if stop_time_updates else None
        return VehicleUpdate(
            tracker_id=self._config.tracker_id,
            vehicle_id=status.vehicle_name or f"{trip_id}:{start_date}",
            vehicle_label=status.vehicle_name,
            trip_id=trip_id,
            start_date=start_date,
            timestamp=int(pos.last_updated.timestamp()),
            lat=pos.lat,
            lon=pos.lon,
            route_id=resolver.route_for(trip_id),
            speed_mps=(
                pos.speed_mph * _MPH_TO_MPS if pos.speed_mph is not None else None
            ),
            bearing=pos.bearing,
            current_stop_sequence=next_stop.stop_sequence if next_stop else None,
            current_stop_id=next_stop.stop_id if next_stop else None,
            current_status="IN_TRANSIT_TO" if next_stop else None,
            # Only predicted stops make a trip update.
            stop_time_updates=[
                u
                for u in stop_time_updates
                if u.arrival_time is not None or u.departure_time is not None
            ],
        )

    def _select(
        self,
        statuses: list[tuple[tuple[str, str], JourneyStatus | None]],
        now: datetime,
    ) -> list[VehicleUpdate]:
        """Build updates from live statuses, one per vehicle."""
        resolver = self._resolver
        assert resolver is not None
        candidates: list[tuple[int, VehicleUpdate]] = []
        for (trip_id, start_date), status in statuses:
            if status is None or status.status == "COMPLETED":
                continue
            if status.status == "CANCELLED":
                log.info("hudsonlink: trip %s on %s cancelled", trip_id, start_date)
                continue
            pos = status.position
            if pos is None or now - pos.last_updated > _MAX_POSITION_AGE:
                continue
            update = self._build(trip_id, start_date, status)
            if update is None:
                continue
            schedule = resolver.trip_schedule(trip_id, start_date)
            candidates.append((schedule[0][2] if schedule else 0, update))

        # A bus can show as in progress on its next trip while finishing the
        # current one; the earlier trip is the one it is running.
        by_vehicle: dict[str, VehicleUpdate] = {}
        for _, update in sorted(candidates, key=lambda c: c[0]):
            by_vehicle.setdefault(update.vehicle_id, update)
        return list(by_vehicle.values())

    async def fetch(self, http: httpx.AsyncClient) -> list[VehicleUpdate]:
        resolver = self._resolver
        assert resolver is not None, "startup() must run before fetch()"
        now = datetime.now(UTC)
        today = now.astimezone(resolver.timezone).date()
        if self._discovered_on != today and (
            self._last_discovery is None
            or now - self._last_discovery >= _DISCOVERY_RETRY
        ):
            await self._discover(http, now)

        targets = [
            (key, ref)
            for key in resolver.trips_near(now, _BEFORE_START, _AFTER_END)
            if (ref := self._journeys.get(key)) is not None
        ]
        semaphore = asyncio.Semaphore(_CONCURRENCY)

        errors: list[Exception] = []

        async def poll(
            key: tuple[str, str], ref: _JourneyRef
        ) -> tuple[tuple[str, str], JourneyStatus | None]:
            async with semaphore:
                try:
                    return key, await fetch_status(
                        http, ref.journey_id, ref.status_date
                    )
                except httpx.HTTPStatusError as exc:
                    errors.append(exc)
                    # Some trips return 500 on every poll; warn on the first.
                    if exc.response.is_server_error:
                        if key in self._failing:
                            log.debug(
                                "hudsonlink: status for %s failed: %r", key[0], exc
                            )
                            return key, None
                        self._failing.add(key)
                    log.warning("hudsonlink: status for %s failed: %r", key[0], exc)
                    return key, None
                except Exception as exc:
                    errors.append(exc)
                    log.warning("hudsonlink: status for %s failed: %r", key[0], exc)
                    return key, None

        statuses = await asyncio.gather(*(poll(k, r) for k, r in targets))
        updates = self._select(list(statuses), now)
        if not updates:
            if self._discovery_error:
                raise UpstreamError(self._discovery_error)
            if targets and len(errors) == len(targets) and not _one_trip_5xx(errors):
                raise UpstreamError(
                    f"all {len(targets)} hudsonlink status calls failed: {errors[-1]!r}"
                )
        log.debug(
            "hudsonlink: polled %d scheduled trips, %d live", len(targets), len(updates)
        )
        return updates
