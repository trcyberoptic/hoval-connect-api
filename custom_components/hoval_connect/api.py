"""Async API client for Hoval Connect."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from .const import (
    BASE_URL,
    CLIENT_ID,
    DURATION_END_OF_PHASE,
    DURATION_FOUR_HOURS,
    DURATION_MIDNIGHT,
    ID_TOKEN_TTL,
    IDP_URL,
    PLANT_TOKEN_TTL,
    REQUEST_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

# Retry configuration for transient errors
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds, doubled on each retry
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class HovalAuthError(Exception):
    """Authentication error."""


class HovalApiError(Exception):
    """General API error."""


def _minutes_until_local_midnight(now: datetime | None = None) -> int:
    """Minutes from `now` (default: naive local now) until the next 00:00.

    Used by build_v4_temporary_change_body for the MIDNIGHT legacy option.
    Naive local datetime is the right choice here: the Hoval controller schedules
    in its local wall clock, which on Home Assistant Operating System is the
    same as the host's local time. Clamped to the 30-1440 minute window the
    cloud accepts.
    """
    if now is None:
        now = datetime.now()
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = int((next_midnight - now).total_seconds() // 60)
    return max(30, min(1440, minutes))


def build_v4_temporary_change_body(
    value: float, duration: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Build the v4 temporary-change request body for the given user option.

    The v4 endpoint takes `{type: "endOfPhase"|"duration", value: <float>,
    duration: <minutes>|null}`. Empirically, the `duration` field is in
    MINUTES (not seconds, despite OpenAPI showing it as a `double`), and the
    cloud accepts roughly 30..1440 (the same range the Hoval Connect Android
    app's CustomDuration picker exposes). HK and HV both accept the format;
    earlier reports of HV-only failure traced back to a duration value out of
    range, not to a circuit-type limitation.

    Pure function — broken out for unit testing. `now` is only used when
    `duration == DURATION_MIDNIGHT` and exists so tests can pin time.
    """
    if duration == DURATION_END_OF_PHASE:
        return {"type": "endOfPhase", "value": value}
    if duration == DURATION_FOUR_HOURS:
        return {"type": "duration", "value": value, "duration": 4 * 60}
    if duration == DURATION_MIDNIGHT:
        return {
            "type": "duration",
            "value": value,
            "duration": _minutes_until_local_midnight(now),
        }
    # Unknown option — degrade to the safest mode that works for both HV and HK.
    _LOGGER.warning("Unknown override duration %r; falling back to endOfPhase", duration)
    return {"type": "endOfPhase", "value": value}


class HovalConnectApi:
    """Async client for the Hoval Connect cloud API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self._email = email
        self._password = password
        self._id_token: str | None = None
        self._id_token_exp: float = 0
        self._pat_cache: dict[str, tuple[str, float]] = {}

    async def _get_id_token(self) -> str:
        """Get or refresh the ID token via OAuth2 password grant."""
        if self._id_token and time.time() < self._id_token_exp:
            return self._id_token

        try:
            async with self._session.post(
                IDP_URL,
                data={
                    "grant_type": "password",
                    "client_id": CLIENT_ID,
                    "username": self._email,
                    "password": self._password,
                    "scope": "openid",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                if resp.status in (400, 401, 403):
                    _LOGGER.warning("IDP auth failed (HTTP %s)", resp.status)
                    raise HovalAuthError(f"Invalid credentials (HTTP {resp.status})")
                resp.raise_for_status()
                data = await resp.json()
        except HovalAuthError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HovalApiError(f"Connection error during authentication: {err}") from err

        if "id_token" not in data:
            _LOGGER.error("IDP response missing id_token. Keys: %s", list(data.keys()))
            raise HovalApiError("IDP response missing id_token")

        self._id_token = data["id_token"]
        self._id_token_exp = time.time() + ID_TOKEN_TTL.total_seconds()
        return self._id_token

    async def _get_plant_access_token(self, plant_id: str) -> str:
        """Get or refresh the plant access token."""
        cached = self._pat_cache.get(plant_id)
        if cached and time.time() < cached[1]:
            return cached[0]

        id_token = await self._get_id_token()
        try:
            async with self._session.get(
                f"{BASE_URL}/v1/plants/{plant_id}/settings",
                headers={"Authorization": f"Bearer {id_token}"},
            ) as resp:
                if resp.status == 401:
                    self._id_token = None
                    raise HovalAuthError("ID token rejected")
                resp.raise_for_status()
                data = await resp.json()
        except (HovalAuthError, HovalApiError):
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HovalApiError(f"Connection error fetching plant token: {err}") from err

        token = data["token"]
        self._pat_cache[plant_id] = (token, time.time() + PLANT_TOKEN_TTL.total_seconds())
        return token

    async def _headers(self, plant_id: str | None = None) -> dict[str, str]:
        """Build request headers with auth tokens."""
        id_token = await self._get_id_token()
        headers = {"Authorization": f"Bearer {id_token}"}
        if plant_id:
            pat = await self._get_plant_access_token(plant_id)
            headers["X-Plant-Access-Token"] = pat
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        plant_id: str | None = None,
        params: dict[str, str] | None = None,
        json_data: Any = None,
        _retry: bool = True,
    ) -> Any:
        """Make an authenticated API request with token retry and transient error backoff."""
        headers = await self._headers(plant_id)
        url = f"{BASE_URL}{path}"
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

        for attempt in range(_MAX_RETRIES):
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_data,
                    timeout=timeout,
                ) as resp:
                    _LOGGER.debug("API %s %s → HTTP %s", method, path, resp.status)
                    if resp.status == 401:
                        self._id_token = None
                        if plant_id:
                            self._pat_cache.pop(plant_id, None)
                        if _retry:
                            _LOGGER.debug("Token expired, refreshing and retrying")
                            return await self._request(
                                method,
                                path,
                                plant_id,
                                params,
                                json_data,
                                _retry=False,
                            )
                        raise HovalAuthError("Authentication failed")
                    if resp.status in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES - 1:
                        delay = _RETRY_BASE_DELAY * (2**attempt)
                        _LOGGER.warning(
                            "Transient error HTTP %s on %s %s, retrying in %.1fs (%d/%d)",
                            resp.status,
                            method,
                            path,
                            delay,
                            attempt + 1,
                            _MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        continue
                    if resp.status >= 400:
                        body = await resp.text()
                        _LOGGER.debug("API error body: %s", body[:500])
                        raise HovalApiError(f"API request failed: HTTP {resp.status}")
                    if resp.status == 204 or resp.content_length == 0:
                        return None
                    return await resp.json()
            except (HovalAuthError, HovalApiError):
                raise
            except TimeoutError as err:
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_BASE_DELAY * (2**attempt)
                    _LOGGER.warning(
                        "Request timeout on %s %s, retrying in %.1fs (%d/%d)",
                        method,
                        path,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise HovalApiError(f"Request timeout: {err}") from err
            except aiohttp.ClientError as err:
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_BASE_DELAY * (2**attempt)
                    _LOGGER.warning(
                        "Connection error on %s %s, retrying in %.1fs (%d/%d)",
                        method,
                        path,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise HovalApiError(f"Connection error: {err}") from err

        raise HovalApiError(f"Request failed after {_MAX_RETRIES} retries")

    async def get_plants(self) -> list[dict[str, Any]]:
        """Get list of user's plants."""
        return await self._request("GET", "/api/my-plants", params={"size": "12", "page": "0"})

    async def get_plant_settings(self, plant_id: str) -> dict[str, Any]:
        """Get plant settings (also refreshes PAT as side effect)."""
        return await self._request("GET", f"/v1/plants/{plant_id}/settings", plant_id=plant_id)

    async def get_circuits(self, plant_id: str) -> list[dict[str, Any]]:
        """Get all circuits for a plant.

        Hoval removed the v1 endpoint around 2026-04-21; v3 is the only path that
        still works. Response shape changed: see coordinator field mapping.
        """
        return await self._request("GET", f"/v3/plants/{plant_id}/circuits", plant_id=plant_id)

    async def get_programs(self, plant_id: str, circuit_path: str) -> Any:
        """Get time programs for a circuit."""
        return await self._request(
            "GET",
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/programs",
            plant_id=plant_id,
        )

    async def get_live_values(
        self, plant_id: str, circuit_path: str, circuit_type: str
    ) -> list[dict[str, str]]:
        """Get live sensor values for a circuit."""
        return await self._request(
            "GET",
            f"/v3/api/statistics/live-values/{plant_id}",
            plant_id=plant_id,
            params={"circuitPath": circuit_path, "circuitType": circuit_type},
        )

    async def get_events(self, plant_id: str) -> list[dict[str, Any]]:
        """Get plant error events."""
        return await self._request("GET", f"/v1/plant-events/{plant_id}")

    async def get_latest_event(self, plant_id: str) -> dict[str, Any]:
        """Get latest plant event."""
        return await self._request("GET", f"/v1/plant-events/latest/{plant_id}")

    async def get_weather(self, plant_id: str) -> list[dict[str, Any]]:
        """Get weather forecast for plant location."""
        return await self._request("GET", f"/v2/api/weather/forecast/{plant_id}", plant_id=plant_id)

    async def set_circuit_mode(self, plant_id: str, circuit_path: str, mode: str) -> Any:
        """Set circuit operation mode (standby or manual).

        v1 had separate endpoints per mode (.../standby, .../manual, .../reset).
        v3 unifies them under .../programs/{program}. The 'reset' mode no longer
        exists; use reset_circuit() to resume the schedule.
        """
        if mode == "reset":
            raise HovalApiError(
                "set_circuit_mode('reset') is no longer supported by the cloud API; "
                "call reset_circuit() to resume the time program."
            )
        return await self.set_program(plant_id, circuit_path, mode)

    async def set_temporary_change(
        self,
        plant_id: str,
        circuit_path: str,
        value: float,
        duration: str = DURATION_END_OF_PHASE,
    ) -> Any:
        """Activate a temporary value override on a circuit.

        v4: POST /v4/plants/{plantId}/circuits/{circuitPath}/temporary-change with
            {"type": "endOfPhase"|"duration", "value": <float>,
             "duration": <seconds>|null}
        For HV the value is the air volume percentage (15..100); for HK it is the
        temperature in degrees Celsius (e.g. 21.5).

        `duration` accepts the user-facing enum from CONF_OVERRIDE_DURATION:
        - DURATION_END_OF_PHASE ("endOfPhase") — body type=endOfPhase, no
          duration. Safest default — overrides the current schedule until the
          next program phase boundary.
        - DURATION_FOUR_HOURS ("FOUR") — body type=duration, duration=240
          (minutes; v4 uses minutes, not seconds, despite the loose OpenAPI).
        - DURATION_MIDNIGHT ("MIDNIGHT") — body type=duration, duration=minutes
          until next local midnight, clamped to 30..1440.

        v3 (`/v3/.../temporary-change`) still works at the time of writing but
        is marked legacy by the cloud (operationId `activateTemporaryChange_1`).
        Reset is still v3-only: see `reset_temporary_change`.
        """
        body = build_v4_temporary_change_body(value, duration)
        _LOGGER.debug(
            "set_temporary_change: plant=%s circuit=%s duration=%s body=%s",
            plant_id,
            circuit_path,
            duration,
            body,
        )
        result = await self._request(
            "POST",
            f"/v4/plants/{plant_id}/circuits/{circuit_path}/temporary-change",
            plant_id=plant_id,
            json_data=body,
        )
        _LOGGER.debug("set_temporary_change: completed successfully")
        return result

    async def reset_temporary_change(self, plant_id: str, circuit_path: str) -> Any:
        """Cancel an active temporary override and resume the underlying program.

        v3: DELETE /v3/plants/{plantId}/circuits/{circuitPath}/temporary-change
        Replaces the removed v1 .../temporary-change/reset POST.
        """
        _LOGGER.debug(
            "reset_temporary_change: plant=%s circuit=%s",
            plant_id,
            circuit_path,
        )
        result = await self._request(
            "DELETE",
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/temporary-change",
            plant_id=plant_id,
        )
        _LOGGER.debug("reset_temporary_change: completed successfully")
        return result

    async def reset_circuit(self, plant_id: str, circuit_path: str, program: str = "week1") -> Any:
        """Resume a configured time program (defaults to week1).

        The v1 POST .../{circuitPath}/reset endpoint that auto-picked the active
        time program no longer exists. v3 requires the caller to choose a specific
        program. Pass program="week2" to switch to the second weekly schedule.
        """
        return await self.set_program(plant_id, circuit_path, program)

    async def set_program(self, plant_id: str, circuit_path: str, program: str) -> Any:
        """Activate a specific program on a circuit.

        POST /v3/plants/{plantExternalId}/circuits/{circuitPath}/programs/{program}
        Program enum: constant, ecoMode, standby, week1, week2, manual, externalConstant.
        """
        _LOGGER.debug(
            "set_program: plant=%s circuit=%s program=%s",
            plant_id,
            circuit_path,
            program,
        )
        result = await self._request(
            "POST",
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/programs/{program}",
            plant_id=plant_id,
        )
        _LOGGER.debug("set_program: completed successfully")
        return result

    def invalidate_plant_token(self, plant_id: str) -> None:
        """Invalidate the cached PAT for a specific plant."""
        self._pat_cache.pop(plant_id, None)

    def invalidate_tokens(self) -> None:
        """Force token refresh on next request."""
        self._id_token = None
        self._id_token_exp = 0
        self._pat_cache.clear()
