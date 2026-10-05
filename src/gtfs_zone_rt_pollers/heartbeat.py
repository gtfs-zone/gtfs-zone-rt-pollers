"""Push upstream health to a Gatus external endpoint."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from gtfs_zone_rt_pollers.config import Config

log = logging.getLogger(__name__)

# At most one push per interval while the state is unchanged.
PUSH_INTERVAL = 60.0


class Heartbeat:
    """Reports each poll cycle's upstream result, throttled.

    A change between success and failure pushes immediately; a repeat of the
    last state pushes at most once per `PUSH_INTERVAL`. Never raises.
    """

    def __init__(self, config: Config) -> None:
        self._url = (
            f"{config.gatus_url.rstrip('/')}/api/v1/endpoints/"
            f"{config.gatus_endpoint_key}/external"
            if config.gatus_url and config.gatus_endpoint_key
            else None
        )
        self._token = config.gatus_token
        self._last_success: bool | None = None
        self._last_push = 0.0
        if self._url is None:
            log.info("GATUS_URL or GATUS_ENDPOINT_KEY unset; not pushing heartbeats")

    async def report(
        self, http: httpx.AsyncClient, success: bool, error: str | None = None
    ) -> None:
        if self._url is None:
            return
        now = time.monotonic()
        if success == self._last_success and now - self._last_push < PUSH_INTERVAL:
            return
        params = {"success": "true" if success else "false"}
        if error:
            params["error"] = error[:500]
        try:
            response = await http.post(
                self._url,
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Unrecorded so the next cycle retries.
            log.warning("gatus heartbeat push failed: %r", exc)
            return
        self._last_success = success
        self._last_push = now
