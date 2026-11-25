"""Assign buswhere devices to GTFS trips by position.

A device is scored against every trip on the routes whose snapshots list it:
the distance from the device to where the trip's schedule puts it at some time
tau, plus `_COST_PER_MIN` for each minute between tau and the fix, minimised
over tau within the trip and [fix - `_MAX_LATE`, fix + `_LEAD`]. Pairs are then
taken greedily by cost, one device per trip and one trip per device.

State carried between cycles keeps the result stable: a device stays on its
trip while it still fits and has not reached the end, a trip released near its
end is not handed out again that day, and a parked bus only takes a trip it is
waiting to start.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from gtfs_zone_rt_pollers.gtfs import distance_m

if TYPE_CHECKING:
    from gtfs_zone_rt_pollers.gtfs import GtfsResolver, TripPath

_MAX_LATE = 75 * 60  # seconds a bus may run behind its trip
_LEAD = 20 * 60  # seconds ahead a laying-over bus may take its next trip
_COST_PER_MIN = 50.0  # metres of cost per minute between tau and the fix
_MAX_DIST = 600.0  # metres from the trip's scheduled position
_KEEP_BONUS = 400.0  # metres of cost waived for last cycle's pair
_STEP = 30  # seconds between sampled taus
_STALE = 180  # seconds; older fixes are not assigned
# A device that moved less than _PARK_DIST over _PARK_WINDOW is parked.
_PARK_WINDOW = 600
_PARK_DIST = 150.0
# An earlier fix this far back and this far away fixes the direction of travel.
_BACK_MIN, _BACK_MAX, _BACK_DIST = 60, 300, 150.0
_BACK_SPAN = 1800  # seconds before tau the earlier fix must fit within
# A trip released after this long assigned, within _DONE_NEAR_END of its last
# stop, has been run and is not reassigned.
_DONE_SERVED = 300
_DONE_NEAR_END = 600
_KEEP_UNTIL_END = 60  # a kept trip is no longer held this close to its end

TripKey = tuple[str, str]  # (trip_id, start_date)


@dataclass(frozen=True)
class Fix:
    """One device's newest position this cycle."""

    key: str
    lat: float
    lon: float
    timestamp: int
    route_ids: frozenset[str]


@dataclass(frozen=True)
class _Fit:
    cost: float
    distance: float
    tau: float  # seconds since service midnight


def _fit(
    path: TripPath,
    pos: tuple[float, float],
    t: float,
    back: tuple[float, float] | None,
) -> _Fit | None:
    """Best tau for a fix at `t` (seconds since service midnight), or None.

    With `back` (an earlier fix), that fix must lie on the trip in the half
    hour before tau, which rules out a bus running the same roads the other way.
    """
    lo, hi = max(path.start, t - _MAX_LATE), min(path.end, t + _LEAD)
    if lo > hi:
        return None
    best: _Fit | None = None
    n = max(1, int((hi - lo) // _STEP))
    for i in range(n + 1):
        tau = lo + (hi - lo) * i / n
        d = distance_m(pos, path.position(tau))
        cost = d + _COST_PER_MIN * abs(t - tau) / 60
        if best is None or cost < best.cost:
            best = _Fit(cost, d, tau)
    assert best is not None
    if best.distance > _MAX_DIST:
        return None
    if back is not None:
        blo = max(path.start, best.tau - _BACK_SPAN)
        m = max(1, int((best.tau - blo) // _STEP))
        nearest = min(
            distance_m(back, path.position(blo + (best.tau - blo) * i / m))
            for i in range(m + 1)
        )
        if nearest > _MAX_DIST:
            return None
    return best


class TripAssigner:
    def __init__(self, resolver: GtfsResolver) -> None:
        self._resolver = resolver
        self._history: dict[str, list[tuple[int, tuple[float, float]]]] = {}
        self._prev: dict[str, TripKey] = {}
        self._served: dict[TripKey, float] = {}  # seconds assigned so far
        self._near_end: dict[TripKey, bool] = {}
        self._done: set[TripKey] = set()
        self._last_cycle: float | None = None

    def _observe(self, fix: Fix) -> tuple[bool, tuple[float, float] | None]:
        """Record the fix; (parked, an earlier fix for the direction check)."""
        pos = (fix.lat, fix.lon)
        h = self._history.setdefault(fix.key, [])
        if not h or fix.timestamp > h[-1][0]:
            h.append((fix.timestamp, pos))
        while h and h[0][0] < fix.timestamp - _PARK_WINDOW:
            h.pop(0)
        parked = h[-1][0] - h[0][0] >= _PARK_WINDOW - 120 and all(
            distance_m(p, pos) < _PARK_DIST for _, p in h
        )
        back = next(
            (
                p
                for ts, p in reversed(h)
                if _BACK_MIN <= fix.timestamp - ts <= _BACK_MAX
                and distance_m(p, pos) > _BACK_DIST
            ),
            None,
        )
        return parked, back

    def assign(self, fixes: list[Fix], now: float) -> dict[str, tuple[str, str, str]]:
        """device key -> (trip_id, start_date, route_id) for this cycle."""
        resolver = self._resolver
        now_dt = datetime.fromtimestamp(now, UTC)
        paths: dict[str, TripPath | None] = {}
        bases: dict[str, float] = {}

        sticky: dict[str, tuple[str, str, str]] = {}
        near_end: dict[tuple[str, TripKey], bool] = {}
        pairs: list[tuple[float, str, str, str, str]] = []
        for fix in fixes:
            if now - fix.timestamp > _STALE:
                continue
            parked, back = self._observe(fix)
            for route_id in sorted(fix.route_ids):
                for trip_id, start_date in resolver.route_trips(route_id, now_dt):
                    if trip_id not in paths:
                        paths[trip_id] = resolver.trip_path(trip_id)
                    path = paths[trip_id]
                    if path is None:
                        continue
                    if start_date not in bases:
                        bases[start_date] = resolver.service_day_start(start_date)
                    base = bases[start_date]
                    trip = (trip_id, start_date)
                    kept = self._prev.get(fix.key) == trip
                    if not kept and trip in self._done:
                        continue
                    f = _fit(path, (fix.lat, fix.lon), fix.timestamp - base, back)
                    if f is None:
                        continue
                    delay = fix.timestamp - base - f.tau
                    if parked and not kept and delay > 0:
                        continue  # a parked bus only waits to start a trip
                    if kept and f.tau < path.end - _KEEP_UNTIL_END:
                        sticky[fix.key] = (trip_id, start_date, route_id)
                    near_end[(fix.key, trip)] = f.tau >= path.end - _DONE_NEAR_END
                    cost = f.cost - (_KEEP_BONUS if kept else 0.0)
                    pairs.append((cost, fix.key, trip_id, start_date, route_id))

        chosen: dict[str, tuple[str, str, str]] = dict(sticky)
        used = {(t, d) for t, d, _ in sticky.values()}
        for _, key, trip_id, start_date, route_id in sorted(pairs):
            if key in chosen or (trip_id, start_date) in used:
                continue
            chosen[key] = (trip_id, start_date, route_id)
            used.add((trip_id, start_date))

        self._account(chosen, near_end, now)
        self._prune(fixes, now_dt)
        return chosen

    def _account(
        self,
        chosen: dict[str, tuple[str, str, str]],
        near_end: dict[tuple[str, TripKey], bool],
        now: float,
    ) -> None:
        """Mark released trips done, and carry the pairs to the next cycle."""
        running = {(t, d) for t, d, _ in chosen.values()}
        for trip in set(self._served) - running:
            if self._served.pop(trip) >= _DONE_SERVED and self._near_end.get(trip):
                self._done.add(trip)
        elapsed = now - self._last_cycle if self._last_cycle is not None else 0.0
        for key, (trip_id, start_date, _) in chosen.items():
            trip = (trip_id, start_date)
            self._near_end[trip] = near_end.get((key, trip), False)
            self._served[trip] = self._served.get(trip, 0.0) + elapsed
        self._last_cycle = now
        self._prev = {k: (t, d) for k, (t, d, _) in chosen.items()}

    def _prune(self, fixes: list[Fix], now_dt: datetime) -> None:
        """Drop state for devices gone quiet and service days already over."""
        seen = {f.key for f in fixes}
        cutoff = now_dt.timestamp() - _PARK_WINDOW
        for key in [k for k in self._history if k not in seen]:
            h = self._history[key]
            if not h or h[-1][0] < cutoff:
                del self._history[key]
        oldest = (
            now_dt.astimezone(self._resolver.timezone).date() - timedelta(days=1)
        ).strftime("%Y%m%d")
        self._done = {t for t in self._done if t[1] >= oldest}
        self._near_end = {t: v for t, v in self._near_end.items() if t[1] >= oldest}
