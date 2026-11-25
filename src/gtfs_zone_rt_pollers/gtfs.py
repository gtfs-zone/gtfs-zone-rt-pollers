from __future__ import annotations

import bisect
import csv
import io
import itertools
import logging
import math
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    import httpx

log = logging.getLogger(__name__)


async def fetch_gtfs(url: str, cache_path: Path, client: httpx.AsyncClient) -> Path:
    try:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with cache_path.open("wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)
        log.info("fetched GTFS from %s → %s", url, cache_path)
    except Exception as exc:
        if cache_path.exists():
            log.warning("GTFS fetch failed (%s), using cached %s", exc, cache_path)
        else:
            raise RuntimeError(
                f"GTFS fetch failed and no cache at {cache_path}"
            ) from exc
    return cache_path


def _parse_gtfs_time(s: str) -> int:
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Equirectangular distance in metres, accurate at a county's scale."""
    lat = math.radians((a[0] + b[0]) / 2)
    dx = math.radians(b[1] - a[1]) * math.cos(lat)
    dy = math.radians(b[0] - a[0])
    return 6_371_000 * math.hypot(dx, dy)


def _project(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> tuple[float, float]:
    """(distance in metres from p to segment ab, fraction along ab)."""
    k = math.cos(math.radians(p[0]))
    ax, ay = (a[1] - p[1]) * k, a[0] - p[0]
    bx, by = (b[1] - p[1]) * k, b[0] - p[0]
    dx, dy = bx - ax, by - ay
    seg = dx * dx + dy * dy
    f = 0.0 if seg == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg))
    return math.radians(math.hypot(ax + f * dx, ay + f * dy)) * 6_371_000, f


# Metres of distance-to-shape a stop's projection trades for each metre it
# skips ahead, so a stop on a road the shape covers twice lands on the first
# pass after the previous stop.
_SKIP_PENALTY = 0.02


@dataclass(frozen=True)
class TripPath:
    """A trip's geometry with its stops placed along it.

    `stops` is [(scheduled arrival, seconds since service midnight; metres
    along the path)], in stop order and never moving backwards along the path.
    """

    points: list[tuple[float, float]]
    cum: list[float]  # metres along the path at each point
    stops: list[tuple[int, float]]

    @property
    def start(self) -> int:
        return self.stops[0][0]

    @property
    def end(self) -> int:
        return self.stops[-1][0]

    def point_at(self, along: float) -> tuple[float, float]:
        """(lat, lon) `along` metres into the path, clamped to its ends."""
        if len(self.points) == 1:
            return self.points[0]
        j = bisect.bisect_right(self.cum, along) - 1
        j = max(0, min(j, len(self.points) - 2))
        seg = self.cum[j + 1] - self.cum[j]
        f = max(0.0, min(1.0, (along - self.cum[j]) / seg)) if seg > 0 else 0.0
        (lat0, lon0), (lat1, lon1) = self.points[j], self.points[j + 1]
        return lat0 + f * (lat1 - lat0), lon0 + f * (lon1 - lon0)

    def position(self, secs: float) -> tuple[float, float]:
        """Where the schedule puts the trip `secs` after service midnight.

        Interpolated along the path between consecutive stops by scheduled
        arrival, and clamped to the first or last stop outside the trip.
        """
        stops = self.stops
        if secs <= stops[0][0]:
            return self.point_at(stops[0][1])
        i = bisect.bisect_left(stops, (secs, -math.inf))
        if i >= len(stops):
            return self.point_at(stops[-1][1])
        (t0, a0), (t1, a1) = stops[i - 1], stops[i]
        f = (secs - t0) / (t1 - t0) if t1 > t0 else 1.0
        return self.point_at(a0 + f * (a1 - a0))


def _build_path(
    shape: list[tuple[float, float]] | None,
    stops: list[tuple[int, tuple[float, float]]],
) -> TripPath:
    """Place a trip's stops along its shape, or join the stops when it has none."""
    if not shape or len(shape) < 2:
        points = [c for _, c in stops]
        cum = [0.0]
        for a, b in itertools.pairwise(points):
            cum.append(cum[-1] + distance_m(a, b))
        return TripPath(points, cum, [(t, cum[i]) for i, (t, _) in enumerate(stops)])

    cum = [0.0]
    for a, b in itertools.pairwise(shape):
        cum.append(cum[-1] + distance_m(a, b))
    placed: list[tuple[int, float]] = []
    j0, along0 = 0, 0.0
    for t, c in stops:
        best: tuple[float, int, float] | None = None  # (cost, segment, along)
        for j in range(j0, len(shape) - 1):
            if best is not None and (cum[j] - along0) * _SKIP_PENALTY > best[0]:
                break  # every later segment costs more in skipping alone
            d, f = _project(c, shape[j], shape[j + 1])
            along = cum[j] + f * (cum[j + 1] - cum[j])
            if along < along0:
                # The projection falls behind the previous stop: hold it there.
                seg = cum[j + 1] - cum[j]
                f = (along0 - cum[j]) / seg if seg > 0 else 0.0
                (lat0, lon0), (lat1, lon1) = shape[j], shape[j + 1]
                d = distance_m(c, (lat0 + f * (lat1 - lat0), lon0 + f * (lon1 - lon0)))
                along = along0
            cost = d + (along - along0) * _SKIP_PENALTY
            if best is None or cost < best[0]:
                best = (cost, j, along)
        if best is None:
            best = (0.0, j0, along0)
        _, j0, along0 = best
        placed.append((t, along0))
    return TripPath(shape, cum, placed)


def _normalize_route_name(name: str) -> str:
    """Casefold, collapse whitespace (incl. NBSP/narrow-NBSP), drop an "amtrak" prefix.

    Amtrak's scraped alert page and its own GTFS route names disagree on
    exactly this kind of cosmetic noise (e.g. "Amtrak Hartford Line" on the
    alerts page vs. "Hartford Line" in routes.txt, or a narrow no-break space
    between two route names run together).
    """
    normalized = " ".join(name.replace("\u202f", " ").replace("\xa0", " ").split())
    normalized = normalized.casefold()
    if normalized.startswith("amtrak "):
        normalized = normalized[len("amtrak ") :]
    return normalized


@dataclass
class _CalendarRow:
    days_bitmask: int  # bit i = weekday i (0=Mon…6=Sun), matching date.weekday()
    start_date: int  # YYYYMMDD
    end_date: int  # YYYYMMDD


class GtfsResolver:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        self._trips: dict[str, list[tuple[str, str]]] = {}
        # trip_id -> service_id, and route_id -> [trip_id], for resolving a
        # vehicle by route (buswhere) rather than by trip_short_name (Amtrak).
        self._trip_service: dict[str, str] = {}
        self._trips_by_route: dict[str, list[str]] = {}
        self._calendar: dict[str, _CalendarRow] = {}
        # service_id -> {YYYYMMDD: exception_type} (1=added, 2=removed).
        self._calendar_exceptions: dict[str, dict[int, int]] = {}
        self._windows: dict[str, tuple[int, int]] = {}
        # trip_id -> GTFS route_id, so the feed can carry the static route_id
        # (which joins to routes.txt) rather than Amtrak's display route name.
        self._trip_routes: dict[str, str] = {}
        # normalized route name (long or short) -> route_id, for matching
        # Amtrak's scraped alert route names (e.g. "Amtrak Hartford Line")
        # against the static feed.
        self._route_names: dict[str, str] = {}
        # trip_id -> {stop_id: stop_sequence}. Amtrak GTFS stop_id == station code
        # (CHI, NYP, …), so this doubles as the station-code validity check when
        # building trip-updates.
        self._trip_stops: dict[str, dict[str, int]] = {}
        # trip_id -> ordered [(stop_sequence, stop_id, arrival_secs)], agency-local
        # seconds-since-service-midnight. A list (not a dict) so loop routes,
        # which visit the same stop twice, keep both occurrences, needed to place
        # a predicted arrival on the correct scheduled visit.
        self._trip_schedule: dict[str, list[tuple[int, str, int]]] = {}
        # stop_code -> stop_id, for providers that name stops by their public
        # code (Hudson Link).
        self._stop_codes: dict[str, str] = {}
        # stop_id -> (lat, lon), for placing a trip where its schedule says it is.
        self._stop_coords: dict[str, tuple[float, float]] = {}
        # shape_id -> ordered [(lat, lon)], and trip_id -> shape_id.
        self._shapes: dict[str, list[tuple[float, float]]] = {}
        self._trip_shapes: dict[str, str] = {}
        self._paths: dict[str, TripPath | None] = {}
        # (ordered stop_id tuple, first departure secs) -> [trip_id], for
        # providers that identify a trip only by its pattern and start time.
        self._trips_by_pattern: dict[tuple[tuple[str, ...], int], list[str]] = {}
        # stop_times.txt values are agency-local, so the service day must be
        # anchored in the agency's zone, not UTC.
        self._tz: ZoneInfo = ZoneInfo("America/New_York")
        self._load(path)

    def _read_file(self, path: Path, name: str) -> str:
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                return zf.read(name).decode("utf-8")
        return (path / name).read_text(encoding="utf-8")

    def _load(self, path: Path) -> None:
        day_names = [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]

        try:
            agencies = csv.DictReader(io.StringIO(self._read_file(path, "agency.txt")))
            tz_name = next(
                (r["agency_timezone"] for r in agencies if r.get("agency_timezone")),
                None,
            )
            if tz_name:
                self._tz = ZoneInfo(tz_name)
        except (KeyError, OSError, ValueError) as exc:
            log.warning("no agency_timezone (%s), assuming %s", exc, self._tz)

        for row in csv.DictReader(io.StringIO(self._read_file(path, "calendar.txt"))):
            bitmask = sum((1 << i) for i, d in enumerate(day_names) if row[d] == "1")
            self._calendar[row["service_id"]] = _CalendarRow(
                days_bitmask=bitmask,
                start_date=int(row["start_date"]),
                end_date=int(row["end_date"]),
            )

        # calendar_dates.txt is optional (Amtrak omits it; the Columbia County
        # feed uses it for holiday exceptions), so tolerate its absence.
        try:
            for row in csv.DictReader(
                io.StringIO(self._read_file(path, "calendar_dates.txt"))
            ):
                self._calendar_exceptions.setdefault(row["service_id"], {})[
                    int(row["date"])
                ] = int(row["exception_type"])
        except (KeyError, OSError, ValueError) as exc:
            log.debug("no calendar_dates.txt (%s)", exc)

        try:
            for row in csv.DictReader(io.StringIO(self._read_file(path, "routes.txt"))):
                route_id = row["route_id"]
                for name in (row.get("route_long_name"), row.get("route_short_name")):
                    normalized = _normalize_route_name(name) if name else ""
                    if normalized:
                        self._route_names.setdefault(normalized, route_id)
        except (KeyError, OSError) as exc:
            log.warning("no routes.txt (%s), route-name lookup disabled", exc)

        try:
            for row in csv.DictReader(io.StringIO(self._read_file(path, "stops.txt"))):
                if row.get("stop_code"):
                    self._stop_codes[row["stop_code"]] = row["stop_id"]
                if row.get("stop_lat") and row.get("stop_lon"):
                    self._stop_coords[row["stop_id"]] = (
                        float(row["stop_lat"]),
                        float(row["stop_lon"]),
                    )
        except (KeyError, OSError, ValueError) as exc:
            log.debug("no stops.txt (%s)", exc)

        for row in csv.DictReader(io.StringIO(self._read_file(path, "trips.txt"))):
            trip_id = row["trip_id"]
            service_id = row["service_id"]
            self._trips.setdefault(row["trip_short_name"], []).append(
                (trip_id, service_id)
            )
            self._trip_service[trip_id] = service_id
            if row.get("route_id"):
                self._trip_routes[trip_id] = row["route_id"]
                self._trips_by_route.setdefault(row["route_id"], []).append(trip_id)
            if row.get("shape_id"):
                self._trip_shapes[trip_id] = row["shape_id"]

        # shapes.txt is optional; a trip without one is placed on straight
        # lines between its stops.
        try:
            raw: dict[str, list[tuple[int, float, float]]] = {}
            for row in csv.DictReader(io.StringIO(self._read_file(path, "shapes.txt"))):
                raw.setdefault(row["shape_id"], []).append(
                    (
                        int(row["shape_pt_sequence"]),
                        float(row["shape_pt_lat"]),
                        float(row["shape_pt_lon"]),
                    )
                )
            for shape_id, pts in raw.items():
                self._shapes[shape_id] = [(lat, lon) for _, lat, lon in sorted(pts)]
        except (KeyError, OSError, ValueError) as exc:
            log.debug("no shapes.txt (%s)", exc)

        first_dep: dict[str, int] = {}
        last_arr: dict[str, int] = {}
        for row in csv.DictReader(io.StringIO(self._read_file(path, "stop_times.txt"))):
            tid = row["trip_id"]
            dep = _parse_gtfs_time(row["departure_time"])
            arr = _parse_gtfs_time(row["arrival_time"])
            if tid not in first_dep or dep < first_dep[tid]:
                first_dep[tid] = dep
            if tid not in last_arr or arr > last_arr[tid]:
                last_arr[tid] = arr
            seq = int(row["stop_sequence"])
            self._trip_stops.setdefault(tid, {})[row["stop_id"]] = seq
            self._trip_schedule.setdefault(tid, []).append((seq, row["stop_id"], arr))

        for tid in first_dep:
            self._windows[tid] = (first_dep[tid], last_arr[tid])
        for tid, sched in self._trip_schedule.items():
            sched.sort()
            pattern = tuple(stop_id for _, stop_id, _ in sched)
            self._trips_by_pattern.setdefault((pattern, first_dep[tid]), []).append(tid)

    def _is_active(self, service_id: str, d: date) -> bool:
        date_int = int(d.strftime("%Y%m%d"))
        # calendar_dates exceptions override calendar.txt: type 1 adds service on
        # a date, type 2 removes it (holidays, one-offs).
        exc = self._calendar_exceptions.get(service_id, {}).get(date_int)
        if exc is not None:
            return exc == 1
        cal = self._calendar.get(service_id)
        if cal is None:
            return False
        if not (cal.start_date <= date_int <= cal.end_date):
            return False
        return bool(cal.days_bitmask & (1 << d.weekday()))

    def _service_midnight(self, d: date) -> datetime:
        """Service-day midnight in the agency zone (DST-honest: noon minus 12h).

        Keeping the anchor at noon-minus-12h means DST-transition days stay an
        honest 23 or 25 hours long.
        """
        return datetime(d.year, d.month, d.day, 12, tzinfo=self._tz) - timedelta(
            hours=12
        )

    @property
    def timezone(self) -> ZoneInfo:
        return self._tz

    def stop_sequences(self, trip_id: str) -> dict[str, int]:
        """{stop_id: stop_sequence} for a resolved trip; empty if unknown."""
        return self._trip_stops.get(trip_id, {})

    @property
    def stop_codes(self) -> dict[str, str]:
        """{stop_code: stop_id} from stops.txt."""
        return self._stop_codes

    def first_stop_pairs(self) -> set[tuple[str, str]]:
        """Distinct (first stop_id, second stop_id) across all trip patterns."""
        return {
            (pattern[0], pattern[1])
            for pattern, _ in self._trips_by_pattern
            if len(pattern) >= 2
        }

    def resolve_by_pattern(
        self, stop_ids: list[str], departure: datetime
    ) -> tuple[str, str] | None:
        """Resolve a trip by its ordered stops and first departure time.

        For providers with no trip id that matches the GTFS (Hudson Link). A
        departure after midnight may belong to the previous service day (GTFS
        times past 24:00), so both days are tried. Returns
        (trip_id, start_date), or None when no active trip matches.
        """
        pattern = tuple(stop_ids)
        local_day = departure.astimezone(self._tz).date()
        for lookback in range(2):
            d = local_day - timedelta(days=lookback)
            secs = int((departure - self._service_midnight(d)).total_seconds())
            matches = [
                trip_id
                for trip_id in self._trips_by_pattern.get((pattern, secs), [])
                if self._is_active(self._trip_service.get(trip_id, ""), d)
            ]
            if matches:
                return min(matches), d.strftime("%Y%m%d")
        return None

    def trips_near(
        self, now: datetime, before: timedelta, after: timedelta
    ) -> list[tuple[str, str]]:
        """(trip_id, start_date) for active trips scheduled around `now`.

        A trip qualifies when `now` falls within [first_dep - before,
        last_arr + after] on an active service day (today or yesterday).
        """
        local_today = now.astimezone(self._tz).date()
        trips: list[tuple[str, str]] = []
        for lookback in range(2):
            d = local_today - timedelta(days=lookback)
            service_midnight = self._service_midnight(d)
            start_date = d.strftime("%Y%m%d")
            for trip_id, (first_dep, last_arr) in self._windows.items():
                start = service_midnight + timedelta(seconds=first_dep)
                end = service_midnight + timedelta(seconds=last_arr)
                if not (start - before <= now <= end + after):
                    continue
                if self._is_active(self._trip_service.get(trip_id, ""), d):
                    trips.append((trip_id, start_date))
        return trips

    def route_for(self, trip_id: str) -> str | None:
        """GTFS route_id for a resolved trip; None if unknown."""
        return self._trip_routes.get(trip_id)

    def route_id_for_name(self, name: str) -> str | None:
        """Best-effort match of a display route name to a GTFS route_id.

        For mapping scraped alert route names (e.g. "Amtrak Hartford Line")
        onto routes.txt, which the alerts page and the static feed don't
        always spell identically. Tries an exact normalized match first, then
        falls back to substring matching in either direction. None if nothing
        matches closely enough to trust.
        """
        normalized = _normalize_route_name(name)
        if not normalized:
            return None
        exact = self._route_names.get(normalized)
        if exact is not None:
            return exact
        for candidate_name, route_id in self._route_names.items():
            if normalized in candidate_name or candidate_name in normalized:
                return route_id
        return None

    def trip_schedule(
        self, trip_id: str, start_date: str
    ) -> list[tuple[int, str, int]]:
        """Ordered [(stop_sequence, stop_id, scheduled_arrival_epoch)] for a trip.

        Absolute epochs are anchored to `start_date` (YYYYMMDD). Repeated stops
        (loop routes) appear once per visit, so a predicted arrival can be placed
        on the matching scheduled occurrence. Empty if the trip is unknown.
        """
        sched = self._trip_schedule.get(trip_id)
        if not sched:
            return []
        d = date(int(start_date[:4]), int(start_date[4:6]), int(start_date[6:8]))
        midnight = self._service_midnight(d)
        return [
            (seq, stop_id, int((midnight + timedelta(seconds=arr)).timestamp()))
            for seq, stop_id, arr in sched
        ]

    def service_day_start(self, start_date: str) -> float:
        """Epoch seconds of service midnight for `start_date` (YYYYMMDD)."""
        d = date(int(start_date[:4]), int(start_date[4:6]), int(start_date[6:8]))
        return self._service_midnight(d).timestamp()

    def trip_path(self, trip_id: str) -> TripPath | None:
        """The trip's path with its stops placed along it; cached.

        None if the trip is unknown or a stop on it has no coordinates.
        """
        if trip_id in self._paths:
            return self._paths[trip_id]
        stops: list[tuple[int, tuple[float, float]]] = []
        for _, stop_id, arrival in self._trip_schedule.get(trip_id, []):
            coords = self._stop_coords.get(stop_id)
            if coords is None:
                stops = []
                break
            stops.append((arrival, coords))
        path = None
        if stops:
            shape = self._shapes.get(self._trip_shapes.get(trip_id, ""))
            path = _build_path(shape, stops)
        self._paths[trip_id] = path
        return path

    def route_trips(self, route_id: str, now: datetime) -> list[tuple[str, str]]:
        """(trip_id, start_date) for every trip on the route active today or
        yesterday (a run can cross midnight), agency-local."""
        local_today = now.astimezone(self._tz).date()
        trips: list[tuple[str, str]] = []
        for lookback in range(2):
            d = local_today - timedelta(days=lookback)
            start_date = d.strftime("%Y%m%d")
            for trip_id in self._trips_by_route.get(route_id, []):
                if self._is_active(self._trip_service.get(trip_id, ""), d):
                    trips.append((trip_id, start_date))
        return trips

    def resolve(
        self, train_num: str, now: datetime, origin: datetime | None = None
    ) -> tuple[str, str] | None:
        """Resolve to (trip_id, start_date), the GTFS-RT trip instance.

        Amtrak models a >24h daily train as one trip_id running every day, so
        several instances of that trip_id are en route at once. start_date
        (YYYYMMDD, the service day the train departed its origin) is what tells
        them apart, both here and in the Redis key downstream. Returns None when
        the train number isn't in the GTFS or no instance is currently running.
        """
        candidates = self._trips.get(train_num)
        if not candidates:
            return None

        # The service day the train actually departed its origin on is the most
        # reliable discriminator, so match that first when we have it.
        if origin is not None:
            service_date = origin.astimezone(self._tz).date()
            day_matches = [
                trip_id
                for trip_id, service_id in candidates
                if self._is_active(service_id, service_date)
            ]
            if day_matches:
                return min(day_matches), service_date.strftime("%Y%m%d")

        local_today = now.astimezone(self._tz).date()
        for lookback in range(5):
            d = local_today - timedelta(days=lookback)
            matches: list[str] = []
            for trip_id, service_id in candidates:
                if not self._is_active(service_id, d):
                    continue
                first_dep, last_arr = self._windows.get(trip_id, (0, 0))
                service_midnight = self._service_midnight(d)
                window_start = service_midnight + timedelta(seconds=first_dep)
                window_end = service_midnight + timedelta(seconds=last_arr)
                if window_start <= now <= window_end:
                    matches.append(trip_id)
            if matches:
                return min(matches), d.strftime("%Y%m%d")

        return None
