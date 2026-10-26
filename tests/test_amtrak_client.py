"""fetch_trains: retries getTrainsData bodies that decrypt to an empty payload."""

import asyncio
import json
from base64 import b64encode

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
