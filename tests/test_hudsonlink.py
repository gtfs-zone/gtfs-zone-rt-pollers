"""resolve_by_pattern + HudsonLinkSource: matching journeys to GTFS trips by
stop pattern and first departure, and building updates from live statuses."""

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from gtfs_zone_rt_pollers.config import Config
from gtfs_zone_rt_pollers.gtfs import GtfsResolver
from gtfs_zone_rt_pollers.sources.base import UpstreamError
from gtfs_zone_rt_pollers.sources.hudsonlink import source as source_mod
from gtfs_zone_rt_pollers.sources.hudsonlink.client import (
    fetch_status,
    parse_journey,
    parse_status,
)
from gtfs_zone_rt_pollers.sources.hudsonlink.source import (
    HudsonLinkSource,
    _JourneyRef,
)

TZ = ZoneInfo("America/New_York")

_AGENCY = (
    "agency_id,agency_name,agency_url,agency_timezone\n"
    "1,LHTL,https://x,America/New_York\n"
)
_CALENDAR = (
    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
    "1,1,1,1,1,0,0,0,20260601,20270628\n"
)
_STOPS = (
    "stop_id,stop_code,stop_name,stop_lat,stop_lon\n"
    "1,PALILJ,Palisades Lot J,41.1,-73.96\n"
    "2,MABRO,S Broadway at Main,41.07,-73.86\n"
    "3,MARCO,Martine Ave,41.03,-73.77\n"
    "4,HAMCON,Hamilton Ave,41.03,-73.76\n"
    "5,TARRYT,Tarrytown,41.07,-73.86\n"
    "6,ELZSTE,Elizabeth St,41.07,-73.87\n"
    "7,ARFRN,Artopee Way,41.09,-73.92\n"
)
# H03 and H05 leave MABRO at the same minute with different patterns, as do
# H07 and H07X from TARRYT. H01X is labelled H01 upstream. T24 runs past
# midnight.
_TRIPS = (
    "route_id,service_id,trip_id,trip_short_name\n"
    "H03,1,T03,\n"
    "H05,1,T05,\n"
    "H07,1,T07,\n"
    "H07X,1,T07X,\n"
    "H01X,1,T01X,\n"
    "H07,1,T24,\n"
)
_STOP_TIMES = (
    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
    "T03,18:21:00,18:21:00,2,1\n"
    "T03,18:24:00,18:24:00,3,2\n"
    "T03,18:55:00,18:55:00,1,3\n"
    "T05,18:21:00,18:21:00,2,1\n"
    "T05,18:24:00,18:24:00,3,2\n"
    "T05,18:30:00,18:30:00,4,3\n"
    "T07,16:57:00,16:57:00,5,1\n"
    "T07,17:01:00,17:01:00,6,2\n"
    "T07,17:20:00,17:20:00,7,3\n"
    "T07X,16:57:00,16:57:00,5,1\n"
    "T07X,17:20:00,17:20:00,1,2\n"
    "T01X,17:55:00,17:55:00,1,1\n"
    "T01X,18:30:00,18:30:00,3,2\n"
    "T24,24:02:00,24:02:00,1,1\n"
    "T24,24:31:00,24:31:00,5,2\n"
)


def _resolver(tmp_path):
    for name, body in (
        ("agency.txt", _AGENCY),
        ("calendar.txt", _CALENDAR),
        ("stops.txt", _STOPS),
        ("trips.txt", _TRIPS),
        ("stop_times.txt", _STOP_TIMES),
    ):
        (tmp_path / name).write_text(body)
    return GtfsResolver(tmp_path)


def _source(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE", "hudsonlink")
    monkeypatch.setenv("INGEST_TRACKER_ID", "hl")
    source = HudsonLinkSource(Config())
    source._resolver = _resolver(tmp_path)
    return source


def _ids(r, codes):
    return [r.stop_codes[c] for c in codes]


# Monday 2026-10-05.
def _at(hh, mm, d=5):
    return datetime(2026, 10, d, hh, mm, tzinfo=TZ)


def _journey(jid, route, codes_times, day="2026-10-05"):
    return {
        "id": jid,
        "carrier": {"code": "HSL"},
        "route": {"longName": route},
        "trip": {"shortName": f"{jid}-MTh"},
        "origin": {"departure": {"time": f"{day}T{codes_times[0][1]}:00.000-04:00"}},
        "stops": [
            {
                "sequence": i + 1,
                "stop": {"externalReference": code},
                "departure": {"time": f"{day}T{hhmm}:00.000-04:00"},
            }
            for i, (code, hhmm) in enumerate(codes_times)
        ],
    }


def _status(vehicle, stops, status="IN_PROGRESS", updated="2026-10-05T18:45"):
    return parse_status(
        {
            "vehicleName": vehicle,
            "status": status,
            "stops": stops,
            "vehiclePosition": {
                "latitude": 41.07,
                "longitude": -73.88,
                "bearing": 270,
                "speedMph": 61,
                "lastUpdated": f"{updated}:00.000-04:00",
            },
        }
    )


def test_pattern_disambiguates_same_minute_departures(tmp_path):
    r = _resolver(tmp_path)
    dep = _at(18, 21)
    assert r.resolve_by_pattern(_ids(r, ["MABRO", "MARCO", "PALILJ"]), dep) == (
        "T03",
        "20261005",
    )
    assert r.resolve_by_pattern(_ids(r, ["MABRO", "MARCO", "HAMCON"]), dep) == (
        "T05",
        "20261005",
    )
    dep = _at(16, 57)
    assert r.resolve_by_pattern(_ids(r, ["TARRYT", "ELZSTE", "ARFRN"]), dep)[0] == (
        "T07"
    )
    assert r.resolve_by_pattern(_ids(r, ["TARRYT", "PALILJ"]), dep)[0] == "T07X"


def test_pattern_after_midnight_is_previous_service_day(tmp_path):
    r = _resolver(tmp_path)
    # Upstream shows 24:02 on Monday as 00:02 on Tuesday.
    assert r.resolve_by_pattern(_ids(r, ["PALILJ", "TARRYT"]), _at(0, 2, d=6)) == (
        "T24",
        "20261005",
    )


def test_pattern_respects_calendar(tmp_path):
    r = _resolver(tmp_path)
    # Saturday 2026-10-10: service 1 is weekdays only.
    assert (
        r.resolve_by_pattern(_ids(r, ["MABRO", "MARCO", "PALILJ"]), _at(18, 21, 10))
        is None
    )


def test_first_stop_pairs(tmp_path):
    r = _resolver(tmp_path)
    assert r.first_stop_pairs() == {
        ("2", "3"),
        ("5", "6"),
        ("5", "1"),
        ("1", "3"),
        ("1", "5"),
    }


def test_match_tolerates_x_label_and_warns_on_other_mismatch(
    tmp_path, monkeypatch, caplog
):
    source = _source(tmp_path, monkeypatch)
    journeys = [
        parse_journey(_journey("a", "H01", [("PALILJ", "17:55"), ("MARCO", "18:30")])),
        parse_journey(
            _journey(
                "b",
                "H09",
                [("MABRO", "18:21"), ("MARCO", "18:24"), ("HAMCON", "18:30")],
            )
        ),
        parse_journey(_journey("c", "H05", [("MABRO", "18:22"), ("MARCO", "18:24")])),
    ]
    with caplog.at_level(logging.WARNING):
        matched = source._match(journeys)
    assert {k: v.journey_id for k, v in matched.items()} == {
        ("T01X", "20261005"): "a",
        ("T05", "20261005"): "b",
    }
    assert matched[("T01X", "20261005")].status_date == date(2026, 10, 5)
    warnings = [r.getMessage() for r in caplog.records]
    assert any("labelled H09" in w for w in warnings)
    assert not any("labelled H01 " in w for w in warnings)
    assert any("1 journey(s) matched no GTFS trip" in w for w in warnings)


def test_build_stop_updates_actual_over_estimated(tmp_path, monkeypatch):
    source = _source(tmp_path, monkeypatch)
    status = _status(
        "R805",
        [
            {
                "externalReference": "MARCO",
                "arrivalTime": {
                    "scheduled": "2026-10-05T18:24:00.000-04:00",
                    "estimated": "2026-10-05T18:30:00.000-04:00",
                    "actual": "2026-10-05T18:28:00.000-04:00",
                },
            },
            {
                "externalReference": "PALILJ",
                "arrivalTime": {
                    "scheduled": "2026-10-05T18:55:00.000-04:00",
                    "estimated": "2026-10-05T18:50:00.000-04:00",
                },
            },
            {
                "externalReference": "NOSUCH",
                "arrivalTime": {"scheduled": "2026-10-05T19:00:00.000-04:00"},
            },
        ],
    )
    u = source._build("T03", "20261005", status)
    assert u.vehicle_id == "R805"
    assert u.route_id == "H03"
    assert u.speed_mps == 61 * 0.44704
    assert u.timestamp == int(_at(18, 45).timestamp())
    assert (u.current_stop_id, u.current_stop_sequence, u.current_status) == (
        "3",
        2,
        "IN_TRANSIT_TO",
    )
    assert [
        (s.stop_id, s.stop_sequence, s.arrival_delay) for s in u.stop_time_updates
    ] == [
        ("3", 2, 240),
        ("1", 3, -300),
    ]
    assert u.stop_time_updates[0].arrival_time == int(_at(18, 28).timestamp())


def test_select_skips_completed_stale_and_missing(tmp_path, monkeypatch):
    source = _source(tmp_path, monkeypatch)
    stop = [
        {
            "externalReference": "MARCO",
            "arrivalTime": {"scheduled": "2026-10-05T18:24:00.000-04:00"},
        }
    ]
    no_position = parse_status({"vehicleName": "R4", "status": "IN_PROGRESS"})
    updates = source._select(
        [
            (("T03", "20261005"), None),
            (("T05", "20261005"), _status("R1", stop, status="COMPLETED")),
            (("T07", "20261005"), _status("R2", stop, status="CANCELLED")),
            (("T07X", "20261005"), _status("R3", stop, updated="2026-10-05T18:30")),
            (("T01X", "20261005"), no_position),
        ],
        _at(18, 46),
    )
    assert updates == []


def test_select_keeps_earliest_trip_per_vehicle(tmp_path, monkeypatch):
    source = _source(tmp_path, monkeypatch)
    updates = source._select(
        [
            (("T03", "20261005"), _status("R820", [])),
            (("T07", "20261005"), _status("R820", [])),
            (("T05", "20261005"), _status("R821", [])),
        ],
        _at(18, 46),
    )
    assert sorted((u.vehicle_id, u.trip_id) for u in updates) == [
        ("R820", "T07"),
        ("R821", "T05"),
    ]


def test_fetch_status_204_is_none():
    def handler(request):
        assert request.url.params["date"] == "2026-10-05"
        return httpx.Response(204)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await fetch_status(http, "abc", date(2026, 10, 5))

    assert asyncio.run(run()) is None


def test_trips_near_window(tmp_path):
    r = _resolver(tmp_path)

    near = r.trips_near(_at(16, 40), timedelta(minutes=20), timedelta(minutes=30))
    assert sorted(near) == [("T07", "20261005"), ("T07X", "20261005")]
    # 00:20 Tuesday is still inside Monday's T24.
    near = r.trips_near(_at(0, 20, d=6), timedelta(0), timedelta(0))
    assert near == [("T24", "20261005")]


def test_fetch_warns_once_per_trip_on_upstream_5xx(tmp_path, monkeypatch, caplog):
    source = _source(tmp_path, monkeypatch)
    source._journeys = {("T03", "20261005"): _JourneyRef("j3", date(2026, 10, 5))}
    source._discovered_on = date(2026, 10, 5)
    monkeypatch.setattr(
        source._resolver, "trips_near", lambda *a: [("T03", "20261005")]
    )
    monkeypatch.setattr(
        source_mod,
        "datetime",
        type("D", (), {"now": staticmethod(lambda tz: _at(18, 30))}),
    )

    def handler(request):
        return httpx.Response(500, json={"message": "Server Error"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            for _ in range(3):
                assert await source.fetch(http) == []

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    assert sum("status for T03 failed" in r.getMessage() for r in caplog.records) == 1


def _fetch_at(source, monkeypatch, handler, targets):
    monkeypatch.setattr(source._resolver, "trips_near", lambda *a: targets)
    monkeypatch.setattr(
        source_mod,
        "datetime",
        type("D", (), {"now": staticmethod(lambda tz: _at(18, 30))}),
    )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await source.fetch(http)

    return asyncio.run(run())


def test_fetch_raises_when_every_status_fails(tmp_path, monkeypatch):
    source = _source(tmp_path, monkeypatch)
    source._journeys = {
        ("T03", "20261005"): _JourneyRef("j3", date(2026, 10, 5)),
        ("T05", "20261005"): _JourneyRef("j5", date(2026, 10, 5)),
    }
    source._discovered_on = date(2026, 10, 5)

    def handler(request):
        return httpx.Response(500, json={"message": "Server Error"})

    with pytest.raises(UpstreamError, match="all 2 hudsonlink status calls"):
        _fetch_at(source, monkeypatch, handler, list(source._journeys))


def test_fetch_tolerates_lone_trip_5xx(tmp_path, monkeypatch):
    source = _source(tmp_path, monkeypatch)
    source._journeys = {("T03", "20261005"): _JourneyRef("j3", date(2026, 10, 5))}
    source._discovered_on = date(2026, 10, 5)

    def handler(request):
        return httpx.Response(500, json={"message": "Server Error"})

    assert _fetch_at(source, monkeypatch, handler, list(source._journeys)) == []


def test_fetch_raises_on_lone_transport_error(tmp_path, monkeypatch):
    source = _source(tmp_path, monkeypatch)
    source._journeys = {("T03", "20261005"): _JourneyRef("j3", date(2026, 10, 5))}
    source._discovered_on = date(2026, 10, 5)

    def handler(request):
        raise httpx.ConnectError("unreachable")

    with pytest.raises(UpstreamError):
        _fetch_at(source, monkeypatch, handler, list(source._journeys))


def test_failed_discovery_stays_failed_until_a_search_succeeds(tmp_path, monkeypatch):
    source = _source(tmp_path, monkeypatch)
    up = False

    def handler(request):
        if not up:
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, json={"results": []})

    # Discovery fails, then later cycles skip discovery but keep failing.
    with pytest.raises(UpstreamError, match="hudsonlink searches failed"):
        _fetch_at(source, monkeypatch, handler, [])
    with pytest.raises(UpstreamError, match="hudsonlink searches failed"):
        _fetch_at(source, monkeypatch, handler, [])

    up = True
    source._last_discovery = None
    assert _fetch_at(source, monkeypatch, handler, []) == []
