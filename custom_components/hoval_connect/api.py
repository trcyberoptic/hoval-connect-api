"""Async API client for Hoval Connect."""

from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

from .const import (
    BASE_URL,
    CLIENT_ID,
    ID_TOKEN_TTL,
    IDP_URL,
    PLANT_TOKEN_TTL,
)

_LOGGER = logging.getLogger(__name__)


class HovalAuthError(Exception):
    """Authentication error."""


class HovalApiError(Exception):
    """General API error."""


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
                if resp.status == 401 or resp.status == 400:
                    raise HovalAuthError("Invalid credentials")
                resp.raise_for_status()
                data = await resp.json()
        except aiohttp.ClientError as err:
            raise HovalApiError(f"Connection error during authentication: {err}") from err

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
        except aiohttp.ClientError as err:
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
    ) -> Any:
        """Make an authenticated API request."""
        headers = await self._headers(plant_id)
        url = f"{BASE_URL}{path}"

        try:
            async with self._session.request(
                method, url, headers=headers, params=params, json=json_data
            ) as resp:
                if resp.status == 401:
                    self._id_token = None
                    self._pat_cache.pop(plant_id, None) if plant_id else None
                    raise HovalAuthError("Authentication failed")
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as err:
            raise HovalApiError(f"API request failed: {method} {path}: {err}") from err

    async def get_plants(self) -> list[dict[str, Any]]:
        """Get list of user's plants."""
        return await self._request("GET", "/api/my-plants", params={"size": "50", "page": "0"})

    async def get_plant_settings(self, plant_id: str) -> dict[str, Any]:
        """Get plant settings (also refreshes PAT as side effect)."""
        headers = await self._headers()
        url = f"{BASE_URL}/v1/plants/{plant_id}/settings"
        try:
            async with self._session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as err:
            raise HovalApiError(f"Failed to get plant settings: {err}") from err

    async def get_circuits(self, plant_id: str) -> list[dict[str, Any]]:
        """Get all circuits for a plant."""
        return await self._request("GET", f"/v1/plants/{plant_id}/circuits", plant_id=plant_id)

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

    async def get_weather(self, plant_id: str) -> list[dict[str, Any]]:
        """Get weather forecast for plant location."""
        return await self._request(
            "GET", f"/v2/api/weather/forecast/{plant_id}", plant_id=plant_id
        )

    async def set_circuit_mode(
        self, plant_id: str, circuit_path: str, mode: str
    ) -> Any:
        """Set circuit operation mode (constant, standby, manual, reset)."""
        return await self._request(
            "PUT",
            f"/v1/plants/{plant_id}/circuits/{circuit_path}/{mode}",
            plant_id=plant_id,
        )

    async def set_circuit_settings(
        self, plant_id: str, circuit_path: str, settings: dict[str, Any]
    ) -> Any:
        """Update circuit settings (e.g. targetAirVolume)."""
        return await self._request(
            "PUT",
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/settings",
            plant_id=plant_id,
            json_data=settings,
        )

    def invalidate_tokens(self) -> None:
        """Force token refresh on next request."""
        self._id_token = None
        self._id_token_exp = 0
        self._pat_cache.clear()
