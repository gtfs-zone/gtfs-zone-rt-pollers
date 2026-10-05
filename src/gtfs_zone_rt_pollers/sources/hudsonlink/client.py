"""Hudson Link (CoachUSA) journey API client.

CoachUSA's shared platform serves an unauthenticated JSON API behind
ridehudsonlink.com. Two endpoints are used:

- `journey?origin=ext-<code>&destination=ext-<code>&serviceUuid=..&dateFrom=..`
  searches journeys between two stops (by GTFS stop_code) over the next few
  days. Each result carries the trip's full stop list with scheduled times.
- `journey/<id>/status?date=YYYY-MM-DD` is the live status of one journey,
  `204` when there is nothing live.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import date

    import httpx

BASE_URL = "https://api.prod.coachusa.com"

_HEADERS = {
    "User-Agent": (
        "gtfs-zone-rt-pollers (+https://github.com/gtfs-zone/gtfs-zone-rt-pollers)"
    ),
    "Accept": "application/json",
}


@dataclass
class JourneyStop:
    stop_code: str
    departure: datetime


@dataclass
class Journey:
    """One search result: a trip between the searched origin and destination."""

    id: str
    carrier_code: str | None
    route: str | None  # route.longName, e.g. "H05"
    trip_name: str | None  # trip.shortName, e.g. "5064-MTh"
    # The full trip, not just the searched segment.
    stops: list[JourneyStop]
    # Departure from the searched origin; its date is the status `date`.
    origin_departure: datetime


@dataclass
class TimePoint:
    scheduled: datetime | None
    estimated: datetime | None = None
    actual: datetime | None = None

    @property
    def predicted(self) -> datetime | None:
        return self.actual or self.estimated


@dataclass
class StatusStop:
    stop_code: str
    arrival: TimePoint | None
    departure: TimePoint | None


@dataclass
class VehiclePosition:
    lat: float
    lon: float
    bearing: float | None
    speed_mph: float | None
    last_updated: datetime


@dataclass
class JourneyStatus:
    vehicle_name: str | None
    status: str | None  # IN_PROGRESS | COMPLETED | CANCELLED
    # Remaining stops only.
    stops: list[StatusStop]
    position: VehiclePosition | None


def _dt(value: Any) -> datetime | None:  # noqa: ANN401
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def _time_point(raw: dict | None) -> TimePoint | None:
    if not raw:
        return None
    return TimePoint(
        scheduled=_dt(raw.get("scheduled")),
        estimated=_dt(raw.get("estimated")),
        actual=_dt(raw.get("actual")),
    )


def _float(value: Any) -> float | None:  # noqa: ANN401
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_journey(raw: dict) -> Journey | None:
    stops: list[JourneyStop] = []
    for s in sorted(raw.get("stops") or [], key=lambda s: s.get("sequence", 0)):
        code = (s.get("stop") or {}).get("externalReference")
        departure = _dt((s.get("departure") or s.get("arrival") or {}).get("time"))
        if not code or departure is None:
            return None
        stops.append(JourneyStop(code, departure))
    origin = raw.get("origin") or {}
    origin_departure = _dt((origin.get("departure") or {}).get("time"))
    if not stops or origin_departure is None or not raw.get("id"):
        return None
    return Journey(
        id=raw["id"],
        carrier_code=(raw.get("carrier") or {}).get("code"),
        route=(raw.get("route") or {}).get("longName"),
        trip_name=(raw.get("trip") or {}).get("shortName"),
        stops=stops,
        origin_departure=origin_departure,
    )


def parse_status(raw: dict) -> JourneyStatus:
    pos = raw.get("vehiclePosition") or {}
    lat, lon = _float(pos.get("latitude")), _float(pos.get("longitude"))
    last_updated = _dt(pos.get("lastUpdated"))
    position = (
        VehiclePosition(
            lat=lat,
            lon=lon,
            bearing=_float(pos.get("bearing")),
            speed_mph=_float(pos.get("speedMph")),
            last_updated=last_updated,
        )
        if lat is not None and lon is not None and last_updated is not None
        else None
    )
    return JourneyStatus(
        vehicle_name=raw.get("vehicleName") or None,
        status=raw.get("status"),
        stops=[
            StatusStop(
                stop_code=s["externalReference"],
                arrival=_time_point(s.get("arrivalTime")),
                departure=_time_point(s.get("departureTime")),
            )
            for s in raw.get("stops") or []
            if s.get("externalReference")
        ],
        position=position,
    )


async def search(
    http: httpx.AsyncClient,
    origin_code: str,
    destination_code: str,
    service_uuid: str,
    date_from: date,
    base_url: str = BASE_URL,
) -> list[Journey]:
    """Journeys from `origin_code` to `destination_code` starting `date_from`."""
    resp = await http.get(
        f"{base_url}/journey",
        params={
            "origin": f"ext-{origin_code}",
            "destination": f"ext-{destination_code}",
            "serviceUuid": service_uuid,
            "dateFrom": date_from.isoformat(),
        },
        headers=_HEADERS,
    )
    resp.raise_for_status()
    journeys = (parse_journey(r) for r in resp.json().get("results") or [])
    return [j for j in journeys if j is not None]


async def fetch_status(
    http: httpx.AsyncClient,
    journey_id: str,
    on: date,
    base_url: str = BASE_URL,
) -> JourneyStatus | None:
    """Live status of a journey on a date; None when there is no live data."""
    resp = await http.get(
        f"{base_url}/journey/{journey_id}/status",
        params={"date": on.isoformat()},
        headers=_HEADERS,
    )
    if resp.status_code == 204:
        return None
    resp.raise_for_status()
    return parse_status(resp.json())
