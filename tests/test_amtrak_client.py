"""fetch_trains: empty-payload retries and LastValTS time zones."""

import asyncio
import json
from base64 import b64encode
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from gtfs_zone_rt_pollers.sources.amtrak import client
from gtfs_zone_rt_pollers.sources.amtrak.client import EmptyTrainsData, fetch_trains

_CRYPTO = {"public_key": "public", "salt": b"\x01" * 8, "iv": b"\x02" * 16}
# 52 bytes pads to 64, which base64-encodes to the 88-char master segment.
_PRIVATE_KEY = "k" * 50


def _encrypt(plaintext: str, password: str) -> str:
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    key = client._derive_key(password, _CRYPTO["salt"])
    encryptor = Cipher(algorithms.AES(key), modes.CBC(_CRYPTO["iv"])).encryptor()
    return b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


def _body(plaintext: str) -> str:
    master = _encrypt(f"{_PRIVATE_KEY}|0", _CRYPTO["public_key"])
    assert len(master) == client._MASTER_SEGMENT
    return _encrypt(plaintext, _PRIVATE_KEY) + master


_FEATURE = {
    "geometry": {"coordinates": [-76.7, 39.0]},
    "properties": {
        "TrainNum": "123",
        "RouteName": "Northeast Regional",
        "Heading": "NE",
        "Velocity": "60",
        "LastValTS": "8/2/2026 9:45:39 AM",
    },
}
_FULL = json.dumps({"features": [_FEATURE]})


def _fetch(monkeypatch, bodies):
    monkeypatch.setattr(client, "_crypto_cache", _CRYPTO)
    monkeypatch.setattr(client, "_EMPTY_RETRY_DELAY", 0)
    queue = list(bodies)

    def handler(request):
        return httpx.Response(200, text=_body(queue.pop(0)))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await fetch_trains(http)

    return asyncio.run(run()), queue


def test_full_payload(monkeypatch):
    trains, _ = _fetch(monkeypatch, [_FULL])
    assert [t.train_num for t in trains] == ["123"]


def test_empty_payload_is_retried(monkeypatch):
    trains, left = _fetch(monkeypatch, ["", "", _FULL, _FULL])
    assert [t.train_num for t in trains] == ["123"]
    assert len(left) == 1


def test_empty_on_every_attempt_raises(monkeypatch):
    with pytest.raises(EmptyTrainsData):
        _fetch(monkeypatch, [""] * client._EMPTY_ATTEMPTS)


_NOW = datetime(2026, 11, 13, 18, 30, tzinfo=UTC)


def _feature(lon, lat, last_val, stations=()):
    props = {"TrainNum": "1", "LastValTS": last_val}
    for i, (code, tz, postdep) in enumerate(stations, 1):
        props[f"Station{i}"] = json.dumps({"code": code, "tz": tz, "postdep": postdep})
    return {"geometry": {"coordinates": [lon, lat]}, "properties": props}


@pytest.mark.parametrize(
    ("lon", "lat", "utc_hour"),
    [
        (-73.99, 40.75, 15),  # New York, EST
        (-87.63, 41.88, 16),  # Chicago, CST
        (-104.99, 39.74, 17),  # Denver, MST
        (-111.65, 35.20, 17),  # Flagstaff, Arizona MST without DST
        (-118.24, 34.05, 18),  # Los Angeles, PST
        (-30.0, 30.0, 15),  # open ocean falls back to Eastern
    ],
)
def test_last_val_read_in_local_zone(lon, lat, utc_hour):
    train = client._parse_feature(_feature(lon, lat, "11/13/2026 10:16:40 AM"), _NOW)
    assert train.timestamp == datetime(2026, 11, 13, utc_hour, 16, 40, tzinfo=UTC)


def test_last_val_around_dst_fall_back():
    # 2026-11-01 01:30 happens twice in Los Angeles; the first (PDT) is taken.
    now = datetime(2026, 11, 1, 12, tzinfo=UTC)
    feature = _feature(-118.24, 34.05, "11/1/2026 1:30:00 AM")
    assert client._parse_feature(feature, now).timestamp == datetime(
        2026, 11, 1, 8, 30, tzinfo=UTC
    )
    feature = _feature(-118.24, 34.05, "11/1/2026 3:00:00 AM")
    assert client._parse_feature(feature, now).timestamp == datetime(
        2026, 11, 1, 11, 0, tzinfo=UTC
    )


def test_eastern_stamp_in_pacific_zone():
    # Pacific would put the fix three hours after the poll.
    feature = _feature(-121.94, 38.32, "11/13/2026 1:25:40 PM")
    assert client._parse_feature(feature, _NOW).timestamp == datetime(
        2026, 11, 13, 18, 25, 40, tzinfo=UTC
    )


def test_next_stop_zone_near_zone_line():
    # Williston ND is Central; the train already stamps the next stop's Mountain.
    stations = [("WTN", "C", "11/13/2026 11:59:00"), ("WPT", "M", None)]
    feature = _feature(-103.68, 48.13, "11/13/2026 11:25:39 AM", stations)
    assert client._parse_feature(feature, _NOW).timestamp == datetime(
        2026, 11, 13, 18, 25, 39, tzinfo=UTC
    )


def test_stale_fix_keeps_local_zone():
    feature = _feature(-118.24, 34.05, "11/13/2026 7:00:00 AM")
    assert client._parse_feature(feature, _NOW).timestamp == datetime(
        2026, 11, 13, 15, 0, tzinfo=UTC
    )


def test_future_fix_keeps_local_zone():
    feature = _feature(-71.06, 42.35, "11/13/2026 4:45:00 PM")
    assert client._parse_feature(feature, _NOW).timestamp == datetime(
        2026, 11, 13, 21, 45, tzinfo=UTC
    )
