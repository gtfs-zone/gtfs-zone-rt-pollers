"""Heartbeat: Gatus external-endpoint pushes, throttled per state."""

import asyncio

import httpx

from gtfs_zone_rt_pollers import heartbeat as heartbeat_mod
from gtfs_zone_rt_pollers.config import Config
from gtfs_zone_rt_pollers.heartbeat import PUSH_INTERVAL, Heartbeat

PUSH = "http://gatus:8080/api/v1/endpoints/pollers_amtrak-poller/external"


def _heartbeat(monkeypatch, **env):
    monkeypatch.setenv("GATUS_URL", "http://gatus:8080/")
    monkeypatch.setenv("GATUS_ENDPOINT_KEY", "pollers_amtrak-poller")
    monkeypatch.setenv("GATUS_TOKEN", "tok")
    for k, v in env.items():
        monkeypatch.delenv(k) if v is None else monkeypatch.setenv(k, v)
    return Heartbeat(Config())


def _run(hb, reports, status=200):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            for success, error in reports:
                await hb.report(http, success, error)

    asyncio.run(run())
    return requests


def test_push_shape(monkeypatch):
    requests = _run(_heartbeat(monkeypatch), [(False, "ConnectError('down')")])
    [req] = requests
    assert str(req.url).startswith(PUSH)
    assert req.url.params["success"] == "false"
    assert req.url.params["error"] == "ConnectError('down')"
    assert req.headers["authorization"] == "Bearer tok"


def test_unset_is_a_no_op(monkeypatch):
    hb = _heartbeat(monkeypatch, GATUS_URL=None)
    assert _run(hb, [(True, None)]) == []


def test_repeats_throttled_but_state_changes_push(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(heartbeat_mod.time, "monotonic", lambda: clock[0])
    hb = _heartbeat(monkeypatch)
    assert len(_run(hb, [(True, None), (True, None)])) == 1
    assert len(_run(hb, [(False, "x")])) == 1
    clock[0] += PUSH_INTERVAL
    assert len(_run(hb, [(False, "x"), (False, "x")])) == 1


def test_failed_push_retries_next_cycle(monkeypatch):
    hb = _heartbeat(monkeypatch)
    assert len(_run(hb, [(True, None)], status=500)) == 1
    assert len(_run(hb, [(True, None)])) == 1
