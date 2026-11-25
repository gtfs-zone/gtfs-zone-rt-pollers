"""BuswhereSource: position-based trip assignment and loop-aware stop-time
construction against the Columbia County GTFS shape."""

import asyncio
import gzip
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from gtfs_zone_rt_pollers.config import Config
from gtfs_zone_rt_pollers.gtfs import GtfsResolver
from gtfs_zone_rt_pollers.sources.base import UpstreamError
from gtfs_zone_rt_pollers.sources.buswhere import source as buswhere_source_mod
from gtfs_zone_rt_pollers.sources.buswhere.assign import Fix, TripAssigner
from gtfs_zone_rt_pollers.sources.buswhere.client import (
    BuswhereObservation,
    fetch_route,
)
from gtfs_zone_rt_pollers.sources.buswhere.source import BuswhereSource

TZ = ZoneInfo("America/New_York")

_AGENCY = (
    "agency_id,agency_name,agency_url,agency_timezone\n"
    "CCPT,CC,https://x,America/New_York\n"
)
_CALENDAR = (
    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
    "WK,1,1,1,1,1,0,0,20240101,20241231\n"
)


def _write(tmp_path, trips: str, stop_times: str, calendar_dates: str | None = None):
    (tmp_path / "agency.txt").write_text(_AGENCY)
    (tmp_path / "calendar.txt").write_text(_CALENDAR)
    (tmp_path / "trips.txt").write_text(trips)
    (tmp_path / "stop_times.txt").write_text(stop_times)
    if calendar_dates is not None:
        (tmp_path / "calendar_dates.txt").write_text(calendar_dates)
    return GtfsResolver(tmp_path)


# One 60-minute loop that returns to its origin stop A (seq 0 and seq 3).
_LOOP_TRIP = "route_id,service_id,trip_id,trip_short_name,shape_id\nR,WK,LOOP,loop,S\n"
_LOOP_TIMES = (
    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
    "LOOP,08:00:00,08:00:00,A,0\n"
    "LOOP,08:20:00,08:20:00,B,1\n"
    "LOOP,08:40:00,08:40:00,C,2\n"
    "LOOP,09:00:00,09:00:00,A,3\n"
)


_LOOP_STOPS = (
    "stop_id,stop_name,stop_lat,stop_lon\n"
    "A,A,42.25,-73.79\n"
    "B,B,42.25,-73.70\n"
    "C,C,42.30,-73.70\n"
)


def _buswhere_source(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE", "buswhere")
    monkeypatch.setenv("INGEST_TRACKER_ID", "ccbus")
    src = BuswhereSource(Config())
    (tmp_path / "stops.txt").write_text(_LOOP_STOPS)
    src._set_resolver(_write(tmp_path, _LOOP_TRIP, _LOOP_TIMES))
    src._routes = {"testslug": "R"}
    src._stops = {"bwA": "A", "bwB": "B", "bwC": "C"}
    src._unmappable = set()
    return src


def test_buswhere_build_loop_keeps_upcoming_only(tmp_path, monkeypatch):
    src = _buswhere_source(tmp_path, monkeypatch)
    # Now = 08:25 (just left B, heading to C). buswhere ETAs to each stop's *next*
    # visit: C in 15m (+3m late), A(origin) next reached at the 09:00 terminus,
    # B not until the next loop (~09:20).
    now = datetime(2024, 1, 2, 8, 25, tzinfo=TZ)
    ts = int(now.timestamp())
    obs = BuswhereObservation(
        lat=42.25,
        lon=-73.79,
        timestamp=ts,
        stop_eta={
            "bwC": 15 * 60 + 180,  # 08:43 (scheduled 08:40 → +180s)
            "bwA": 35 * 60,  # 09:00 terminus
            "bwB": 55 * 60,  # 09:20 next loop → dropped
        },
    )

    v = src._build("testslug", obs, "LOOP", "20240102")
    assert v is not None
    assert (v.trip_id, v.start_date, v.route_id) == ("LOOP", "20240102", "R")
    assert v.speed_mps is None and v.bearing is None

    by_seq = {u.stop_sequence: u for u in v.stop_time_updates}
    # Passed origin (seq 0) and next-loop B (seq 1) are filtered out; the upcoming
    # C (seq 2) and the terminus A (seq 3) remain.
    assert set(by_seq) == {2, 3}
    assert by_seq[2].stop_id == "C"
    assert by_seq[2].arrival_delay == 180
    assert by_seq[2].arrival_time == ts + 15 * 60 + 180
    assert by_seq[3].stop_id == "A"
    assert by_seq[3].arrival_delay == 0


def test_buswhere_build_labels_from_device_name(tmp_path, monkeypatch):
    # The device name is display-only; device_id identifies the bus whichever
    # route it is logged into.
    src = _buswhere_source(tmp_path, monkeypatch)
    now = datetime(2024, 1, 2, 8, 25, tzinfo=TZ)
    obs = BuswhereObservation(
        lat=42.25,
        lon=-73.79,
        timestamp=int(now.timestamp()),
        stop_eta={"bwC": 900},
        vehicle_name="C5",
        device_id=54742,
    )

    v = src._build("testslug", obs, "LOOP", "20240102")
    assert v is not None
    assert v.vehicle_id == "54742"
    assert v.vehicle_label == "C5"

    obs_unnamed = BuswhereObservation(
        lat=42.25, lon=-73.79, timestamp=int(now.timestamp()), stop_eta={"bwC": 900}
    )
    v2 = src._build("testslug", obs_unnamed, "LOOP", "20240102")
    assert v2 is not None
    assert v2.vehicle_label == "testslug"
    # No device: the bare slug, never None. rt-api keys its live record on the
    # vehicle_id, so a missing one would collapse the fleet onto one record.
    assert v2.vehicle_id == "testslug"


def test_buswhere_build_current_stop_is_next_visit(tmp_path, monkeypatch):
    # Same 08:25 fix as above: the first surviving update (C, seq 2) is the stop
    # the bus is running towards, and an ETA feed can only claim IN_TRANSIT_TO.
    src = _buswhere_source(tmp_path, monkeypatch)
    now = datetime(2024, 1, 2, 8, 25, tzinfo=TZ)
    ts = int(now.timestamp())
    obs = BuswhereObservation(
        lat=42.25,
        lon=-73.79,
        timestamp=ts,
        stop_eta={"bwC": 15 * 60 + 180, "bwA": 35 * 60, "bwB": 55 * 60},
    )

    v = src._build("testslug", obs, "LOOP", "20240102")
    assert v is not None
    assert (v.current_stop_sequence, v.current_stop_id, v.current_status) == (
        2,
        "C",
        "IN_TRANSIT_TO",
    )


def test_buswhere_build_no_current_stop_without_predictions(tmp_path, monkeypatch):
    # Every ETA belongs to the next loop, so nothing survives the filter and the
    # bus is published with no claim about where it is.
    src = _buswhere_source(tmp_path, monkeypatch)
    now = datetime(2024, 1, 2, 8, 25, tzinfo=TZ)
    obs = BuswhereObservation(
        lat=42.25,
        lon=-73.79,
        timestamp=int(now.timestamp()),
        stop_eta={"bwB": 55 * 60},
    )

    v = src._build("testslug", obs, "LOOP", "20240102")
    assert v is not None
    assert v.stop_time_updates == []
    assert v.current_stop_sequence is None
    assert v.current_stop_id is None
    assert v.current_status is None


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self.is_redirect = False
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self, payload):
        self._payload = payload

    async def get(self, *args, **kwargs):
        return _FakeResponse(self._payload)


def test_fetch_route_tolerates_non_numeric_stop_eta():
    # Upstream mixes numbers, null, and sentinel strings like "departed" in
    # stop_eta; only numeric ETAs (including numeric strings) survive.
    payload = {
        "active": True,
        "devices": [
            {"position": {"lat": 42.25, "lon": -73.79}, "updated_at": 1700000000}
        ],
        "stop_eta": {
            "150769331": "departed",
            "150769332": None,
            "150769333": 120,
            "150769334": "300",
            "150769335": "arriving",
        },
    }
    observations = asyncio.run(fetch_route(_FakeHttp(payload), "hudson__albany_b_pm"))
    assert len(observations) == 1
    assert observations[0].stop_eta == {"150769333": 120.0, "150769334": 300.0}


def test_fetch_failure_on_one_route_does_not_abort_cycle(tmp_path, monkeypatch, caplog):
    # A route whose fetch raises must be skipped, not kill the whole poll cycle.
    src = _buswhere_source(tmp_path, monkeypatch)
    src._routes = {"bad": "R", "good": "R"}
    src._slugs = ["bad", "good"]
    now = datetime(2024, 1, 2, 8, 25, tzinfo=TZ)
    src._clock = now.timestamp
    good_obs = BuswhereObservation(
        lat=42.2625,
        lon=-73.70,
        timestamp=int(now.timestamp()),
        stop_eta={"bwC": 900},
    )

    async def fake_fetch_route(http, slug, base_url=None):
        if slug == "bad":
            raise ValueError("could not convert string to float: 'departed'")
        return [good_obs]

    monkeypatch.setattr(buswhere_source_mod, "fetch_route", fake_fetch_route)
    updates = asyncio.run(src.fetch(None))
    assert [u.trip_id for u in updates] == ["LOOP"]
    assert "route bad failed" in caplog.text


def test_fetch_raises_when_every_route_fails(tmp_path, monkeypatch):
    src = _buswhere_source(tmp_path, monkeypatch)
    src._slugs = ["a", "b"]

    async def fake_fetch_route(http, slug, base_url=None):
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(buswhere_source_mod, "fetch_route", fake_fetch_route)
    with pytest.raises(UpstreamError, match="all 2 buswhere routes failed"):
        asyncio.run(src.fetch(None))


def test_fetch_route_dormant_redirect_is_empty_and_5xx_raises():
    def handler(request):
        if request.url.path.endswith("dormant"):
            return httpx.Response(302, headers={"location": "/elsewhere"})
        return httpx.Response(503)

    async def run(slug):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await fetch_route(http, slug, "https://bw.test/routes")

    assert asyncio.run(run("dormant")) == []
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run("down"))


# A snapshot's `devices` list carries buses running other routes. These payloads
# are the shape of the live 2026-09-15 shopping_shuttle/hudson__albany_b_pm pair:
# C1 runs the Albany commuter but is echoed into the shuttle's snapshot, and
# `current` tracks each route's own bus.
_C1 = 55170
_C12 = 54742


def _shuttle_payload():
    return {
        "active": True,
        "current": {"lat": 42.255656, "lon": -73.791474},
        "devices": [
            {
                "device_id": _C1,
                "name": " C1",
                "position": {"lat": 42.677562, "lon": -73.810432},
                "updated_at": 1700000000,
            },
            {
                "device_id": _C12,
                "name": "C12",
                "position": {"lat": 42.255656, "lon": -73.791474},
                "updated_at": 1700000000,
            },
        ],
        # The flat dict is an aggregate over both buses: per stop it is roughly
        # whichever bus is closer, so it belongs to neither.
        "stop_eta": {"bwB": 41, "bwC": 132},
        "other_routes_stop_eta": {
            "bwB": [
                {"eta": 41, "device_id": _C1},
                {"eta": 6584, "device_id": _C12},
            ],
            "bwC": [
                {"eta": 981, "device_id": _C1},
                {"eta": 132, "device_id": _C12},
            ],
        },
    }


def test_fetch_route_returns_one_observation_per_device():
    observations = asyncio.run(fetch_route(_FakeHttp(_shuttle_payload()), "shuttle"))
    assert [o.device_id for o in observations] == [_C1, _C12]
    # Per-device ETAs, not the blended aggregate.
    assert observations[0].stop_eta == {"bwB": 41.0, "bwC": 981.0}
    assert observations[1].stop_eta == {"bwB": 6584.0, "bwC": 132.0}
    # The padded upstream name never reaches the VehicleDescriptor label.
    assert [o.vehicle_name for o in observations] == ["C1", "C12"]


def test_fetch_route_single_device_uses_aggregate_stop_eta():
    # With one bus the flat dict is unambiguous, and is the only source when the
    # per-device breakdown is missing.
    payload = {
        "active": True,
        "current": {"lat": 42.67, "lon": -73.81},
        "devices": [
            {
                "device_id": _C1,
                "name": " C1",
                "position": {"lat": 42.677567, "lon": -73.81043},
                "updated_at": 1700000000,
            }
        ],
        "stop_eta": {"bwB": 300},
    }
    observations = asyncio.run(fetch_route(_FakeHttp(payload), "hudson__albany_b_pm"))
    assert len(observations) == 1
    assert observations[0].stop_eta == {"bwB": 300.0}


def test_unmappable_stops_do_not_warn(tmp_path, monkeypatch, caplog):
    src = _buswhere_source(tmp_path, monkeypatch)
    src._unmappable = {"bwGreenport"}
    now = datetime(2024, 1, 2, 8, 25, tzinfo=TZ)
    unmapped: set[str] = set()
    obs = BuswhereObservation(
        lat=42.25,
        lon=-73.79,
        timestamp=int(now.timestamp()),
        stop_eta={"bwC": 900, "bwGreenport": 300, "bwUnknown": 300},
    )

    src._build("testslug", obs, "LOOP", "20240102", unmapped)
    assert unmapped == {"bwUnknown"}


def test_trip_path_without_shape_joins_the_stops(tmp_path):
    (tmp_path / "stops.txt").write_text(_LOOP_STOPS)
    r = _write(tmp_path, _LOOP_TRIP, _LOOP_TIMES)
    path = r.trip_path("LOOP")
    assert path is not None

    def at(h, m):
        return path.position(h * 3600 + m * 60)

    assert at(8, 10) == pytest.approx((42.25, -73.745))
    assert at(8, 50) == pytest.approx((42.275, -73.745))
    assert at(7, 0) == (42.25, -73.79)
    assert at(10, 0) == pytest.approx((42.25, -73.79))
    assert r.trip_path("NOPE") is None


def test_trip_path_follows_the_shape(tmp_path):
    # A trip from A to C whose shape runs via B: halfway in time is halfway
    # along the shape (at B), not on the straight line A-C.
    (tmp_path / "stops.txt").write_text(_LOOP_STOPS)
    (tmp_path / "shapes.txt").write_text(
        "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
        "S,42.25,-73.79,0\nS,42.25,-73.70,1\nS,42.30,-73.70,2\n"
    )
    r = _write(
        tmp_path,
        "route_id,service_id,trip_id,trip_short_name,shape_id\nR,WK,AC,ac,S\n",
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "AC,08:00:00,08:00:00,A,0\nAC,09:00:00,09:00:00,C,1\n",
    )
    path = r.trip_path("AC")
    assert path is not None
    ab = path.cum[1]
    half = path.position(8.5 * 3600)
    expected = path.point_at(path.cum[-1] / 2)
    assert half == pytest.approx(expected)
    # The halfway point lies on the A-B leg or the B-C leg, never off the shape.
    assert half[0] == pytest.approx(42.25) or half[1] == pytest.approx(-73.70)
    assert path.stops[0][1] == 0 and path.stops[-1][1] == pytest.approx(path.cum[-1])
    assert ab > 0


def test_trip_path_places_a_loop_terminus_at_the_end(tmp_path):
    # The loop's origin and terminus are the same stop; the terminus must land
    # at the end of the shape, not back at its start.
    (tmp_path / "stops.txt").write_text(_LOOP_STOPS)
    (tmp_path / "shapes.txt").write_text(
        "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
        "S,42.25,-73.79,0\nS,42.25,-73.70,1\nS,42.30,-73.70,2\nS,42.25,-73.79,3\n"
    )
    r = _write(tmp_path, _LOOP_TRIP, _LOOP_TIMES)
    path = r.trip_path("LOOP")
    assert path is not None
    alongs = [a for _, a in path.stops]
    assert alongs == sorted(alongs)
    assert alongs[0] == 0
    assert alongs[1] == pytest.approx(path.cum[1])
    assert alongs[-1] == pytest.approx(path.cum[-1])


# Route R runs OUT (A to B, 08:00-08:20) then BACK (B to A, 08:30-08:50) over
# the same road; route R2 has its own trip elsewhere.
_A, _B = (42.25, -73.79), (42.25, -73.70)
_MID = (42.25, -73.745)
_TOWARDS_B = (42.25, -73.7316)  # ~1.1 km from _MID towards B
_TOWARDS_A = (42.25, -73.7584)  # ~1.1 km from _MID towards A
_OUT_BACK_TRIPS = (
    "route_id,service_id,trip_id,trip_short_name,shape_id\n"
    "R,WK,OUT,out,\nR,WK,BACK,back,\nR2,WK,OTHER,other,\n"
)
_OUT_BACK_TIMES = (
    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
    "OUT,08:00:00,08:00:00,A,0\nOUT,08:20:00,08:20:00,B,1\n"
    "BACK,08:30:00,08:30:00,B,0\nBACK,08:50:00,08:50:00,A,1\n"
    "OTHER,08:00:00,08:00:00,C,0\nOTHER,08:30:00,08:30:00,D,1\n"
)
_OUT_BACK_STOPS = (
    "stop_id,stop_name,stop_lat,stop_lon\n"
    "A,A,42.25,-73.79\nB,B,42.25,-73.70\nC,C,42.40,-73.79\nD,D,42.40,-73.70\n"
)


def _assigner(tmp_path):
    (tmp_path / "stops.txt").write_text(_OUT_BACK_STOPS)
    return TripAssigner(_write(tmp_path, _OUT_BACK_TRIPS, _OUT_BACK_TIMES))


def _t(h, m, s=0):
    return int(datetime(2024, 1, 2, h, m, s, tzinfo=TZ).timestamp())


def _fix(key, pos, ts, routes=("R",)):
    return Fix(key, pos[0], pos[1], ts, frozenset(routes))


def _trip(chosen, key):
    entry = chosen.get(key)
    return entry[0] if entry else None


def test_assign_picks_the_trip_by_position_not_the_slug(tmp_path):
    # Listed by both routes' snapshots (a wrong-route login or an echo), the bus
    # goes to the trip where it actually is.
    a = _assigner(tmp_path)
    chosen = a.assign([_fix("1", _MID, _t(8, 10), ("R", "R2"))], _t(8, 10))
    assert chosen == {"1": ("OUT", "20240102", "R")}


def test_assign_direction_decides_between_opposite_trips(tmp_path):
    # At 08:25 a bus at the midpoint is 15 minutes off both OUT and BACK; the
    # fix three minutes earlier says which way it is going.
    for earlier, expected in ((_TOWARDS_B, "BACK"), (_TOWARDS_A, "OUT")):
        a = _assigner(tmp_path)
        a.assign([_fix("1", earlier, _t(8, 22))], _t(8, 22))
        chosen = a.assign([_fix("1", _MID, _t(8, 25))], _t(8, 25))
        assert _trip(chosen, "1") == expected


def test_assign_one_device_per_trip(tmp_path):
    a = _assigner(tmp_path)
    off = (42.2535, -73.745)  # ~390 m off the road
    chosen = a.assign(
        [_fix("1", off, _t(8, 10)), _fix("2", _MID, _t(8, 10))], _t(8, 10)
    )
    assert chosen == {"2": ("OUT", "20240102", "R")}


def test_assign_parked_bus_only_waits_to_start(tmp_path):
    # Parked at A from 07:50: at 07:58 it is waiting for OUT, but a bus first
    # seen parked there at 08:06 is not running OUT six minutes late.
    a = _assigner(tmp_path)
    for m in range(50, 58):
        a.assign([_fix("1", _A, _t(7, m))], _t(7, m))
    assert _trip(a.assign([_fix("1", _A, _t(7, 58))], _t(7, 58)), "1") == "OUT"

    # History without candidates: parked at A from 07:56, on no route yet.
    b = _assigner(tmp_path)
    for m in range(56, 66):
        ts = _t(7, 0) + m * 60
        b.assign([_fix("2", _A, ts, routes=())], ts)
    assert b.assign([_fix("2", _A, _t(8, 6))], _t(8, 6)) == {}

    # A bus pulling in to A (from ~300 m away) at 08:06 does take OUT.
    c = _assigner(tmp_path)
    c.assign([_fix("3", (42.2527, -73.79), _t(8, 4))], _t(8, 4))
    assert _trip(c.assign([_fix("3", _A, _t(8, 6))], _t(8, 6)), "3") == "OUT"


def test_assign_does_not_reassign_a_finished_trip(tmp_path):
    # Bus 1 runs OUT to the end and drops out; bus 2, heading for B at 08:26,
    # fits OUT 16 minutes late but OUT has been run.
    a = _assigner(tmp_path)
    for m in range(0, 21, 2):
        lon = _A[1] + (_B[1] - _A[1]) * m / 20
        a.assign([_fix("1", (42.25, lon), _t(8, m))], _t(8, m))
    a.assign([], _t(8, 22))
    a.assign([_fix("2", _TOWARDS_A, _t(8, 23))], _t(8, 23))
    assert a.assign([_fix("2", _MID, _t(8, 26))], _t(8, 26)) == {}


def test_assign_skips_stale_fixes(tmp_path):
    a = _assigner(tmp_path)
    assert a.assign([_fix("1", _MID, _t(8, 10))], _t(8, 14)) == {}


def test_assign_start_date_is_the_local_service_day(tmp_path):
    # A fix as a UTC epoch late in the local evening stays on the local day.
    (tmp_path / "stops.txt").write_text(_OUT_BACK_STOPS)
    r = _write(
        tmp_path,
        "route_id,service_id,trip_id,trip_short_name,shape_id\nR,WK,LATE,late,\n",
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "LATE,22:00:00,22:00:00,A,0\nLATE,22:20:00,22:20:00,B,1\n",
    )
    ts = _t(22, 10)  # 03:10 UTC on 01-03
    chosen = TripAssigner(r).assign([_fix("1", _MID, ts)], ts)
    assert chosen == {"1": ("LATE", "20240102", "R")}


def _source_fetch(src, monkeypatch, snapshots):
    async def fake_fetch_route(http, slug, base_url=None):
        return snapshots.get(slug, [])

    monkeypatch.setattr(buswhere_source_mod, "fetch_route", fake_fetch_route)
    return asyncio.run(src.fetch(None))


def test_fetch_one_vehicle_per_device_across_slugs(tmp_path, monkeypatch):
    # The same bus in two snapshots is one vehicle, published from its newest
    # fix under its device id.
    src = _buswhere_source(tmp_path, monkeypatch)
    src._routes = {"am": "R", "pm": "R"}
    src._slugs = ["am", "pm"]
    ts = _t(8, 25)
    src._clock = lambda: ts

    def obs(t):
        return BuswhereObservation(
            lat=42.2625, lon=-73.70, timestamp=t, stop_eta={}, device_id=7
        )

    updates = _source_fetch(src, monkeypatch, {"am": [obs(ts - 30)], "pm": [obs(ts)]})
    assert [(u.vehicle_id, u.trip_id, u.timestamp) for u in updates] == [
        ("7", "LOOP", ts)
    ]


def test_fetch_never_publishes_an_older_fix(tmp_path, monkeypatch):
    src = _buswhere_source(tmp_path, monkeypatch)
    src._slugs = ["testslug"]
    ts = _t(8, 25)
    src._clock = lambda: ts

    def obs(t):
        return BuswhereObservation(
            lat=42.2625, lon=-73.70, timestamp=t, stop_eta={}, device_id=7
        )

    assert len(_source_fetch(src, monkeypatch, {"testslug": [obs(ts)]})) == 1
    assert _source_fetch(src, monkeypatch, {"testslug": [obs(ts - 60)]}) == []
    assert len(_source_fetch(src, monkeypatch, {"testslug": [obs(ts)]})) == 1


_FIXTURES = Path(__file__).parent / "fixtures"


def _replay(monkeypatch, name):
    """Run recorded 2026-11-24 snapshots through the source.

    Returns {poll: {vehicle_id: trip_id}} and the published updates.
    """
    monkeypatch.setenv("SOURCE", "buswhere")
    monkeypatch.setenv("INGEST_TRACKER_ID", "ccbus")
    src = BuswhereSource(Config())
    src._set_resolver(GtfsResolver(_FIXTURES / "columbia_county_gtfs.zip"))
    polls: dict[int, dict[str, dict]] = {}
    with gzip.open(_FIXTURES / f"buswhere_20261124_{name}.jsonl.gz", "rt") as f:
        for line in f:
            row = json.loads(line)
            polls.setdefault(row["poll"], {})[row["slug"]] = row["body"]

    current: dict[str, dict] = {}

    def handler(request):
        body = current.get(request.url.path.rsplit("/", 1)[-1])
        if body is None:
            return httpx.Response(302, headers={"location": "/"})
        return httpx.Response(200, json=body)

    async def run():
        out: dict[int, dict[str, str]] = {}
        published = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            for poll in sorted(polls):
                current.clear()
                current.update(polls[poll])
                src._clock = lambda poll=poll: poll
                updates = await src.fetch(http)
                out[poll] = {u.vehicle_id: u.trip_id for u in updates}
                published.extend(updates)
        return out, published

    return asyncio.run(run())


def _at(out, h, m):
    """The assignments of the last poll at or before h:m on 2026-11-24."""
    t = datetime(2026, 11, 24, h, m, tzinfo=TZ).timestamp()
    return out[max(p for p in out if p <= t)]


def _check_invariants(out, published):
    for trips in out.values():
        assert len(set(trips.values())) == len(trips), "a trip on two buses"
    last: dict[str, int] = {}
    for u in published:
        assert ":" not in u.vehicle_id
        assert u.timestamp >= last.get(u.vehicle_id, 0)
        last[u.vehicle_id] = u.timestamp


def test_replay_morning_wrong_slug_logins(monkeypatch):
    # C7 (55234) ran the C AM commute logged into the A AM slug; C13 (55168),
    # on the shopping shuttle, showed up in the C AM snapshot.
    out, published = _replay(monkeypatch, "morning")
    _check_invariants(out, published)
    at_0730 = _at(out, 7, 30)
    assert at_0730.get("55234") == "HUD_ALB_C_AM_NB"
    assert at_0730.get("55168", "").startswith("SHOPPING_WK_")
    assert _at(out, 8, 0).get("55170") == "HUD_ALB_A_AM_SB"


def test_replay_afternoon_parked_bus_and_handover(monkeypatch):
    # C7 sat at the depot through the 15:07 shuttle; C13 handed B PM to C1.
    out, published = _replay(monkeypatch, "afternoon")
    _check_invariants(out, published)
    assert _at(out, 15, 0).get("55168") == "HUD_ALB_B_PM_NB"
    assert "55234" not in _at(out, 15, 30)
    assert _at(out, 15, 45).get("55170") == "HUD_ALB_B_PM_NB"
