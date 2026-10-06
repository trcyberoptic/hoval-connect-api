"""Async API client for Hoval Connect."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from math import isfinite
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
    USER_AGENT,
)
from .privacy import redact_remote_error_body

_LOGGER = logging.getLogger(__name__)

# Retry configuration for transient errors
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds, doubled on each retry
_SAFE_RETRY_METHODS = frozenset({"GET", "HEAD"})
_VALID_PROGRAMS = frozenset(
    {"constant", "ecoMode", "standby", "week1", "week2", "manual", "externalConstant"}
)


def _is_retryable_status(status: int) -> bool:
    """Return True for rate limiting and every server-side error.

    Deliberately a range, not an enumeration. Hoval's gateway emits non-standard
    proxy codes — 599 ("network connect timeout") was observed ten times in a
    single day on 2026-08-09, and because it was absent from the previous
    enumerated set `{429, 500, 502, 503, 504}` it raised straight through, failing
    the whole coordinator refresh and blanking every entity until the next poll.
    Enumerating codes turns each new proxy quirk into the same bug.
    """
    return status == 429 or status >= 500


# Signature of the Azure Application Gateway's own block page. Hoval's API always
# answers with JSON, so an HTML body naming the gateway means the request was
# refused *in front of* Hoval and never reached them. Worth telling apart: a 403
# from Hoval is about the account, a 403 from here is about the client — and
# issue #11 showed how badly the two get confused when they share a message.
_GATEWAY_BLOCK_SIGNATURE = "microsoft-azure-application-gateway"


def _is_gateway_block(body: str) -> bool:
    """Return True if `body` is the Azure Application Gateway's block page."""
    return _GATEWAY_BLOCK_SIGNATURE in body.lower()


# Hard upper bound on my-plants pagination. 50 pages x 12 plants/page = 600
# plants — far beyond any real account. Without a cap, a server that keeps
# answering `"last": false` would loop get_plants() forever; the config-flow
# validation path (30 s outer timeout) is the tightest caller, but the cap
# belongs in the client.
_MAX_PLANT_PAGES = 50


class HovalAuthError(Exception):
    """Authentication error."""


class HovalApiError(Exception):
    """General API error.

    Carries the HTTP status when the cloud actually answered, so callers can
    branch on it instead of re-parsing the message. `None` means the request
    never got a status (transport failure, timeout, unusable body).
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        gateway_blocked: bool = False,
        request_path: str | None = None,
    ) -> None:
        """Initialize with an optional HTTP status."""
        super().__init__(message)
        self.status = status
        self.gateway_blocked = gateway_blocked
        # Only data-endpoint responses carry this. Token fetch errors happen
        # before that request and must not be mistaken for missing capabilities.
        self.request_path = request_path


def _require_identifier(value: Any, field: str) -> str:
    """Validate one URL segment without imposing model-specific identifiers."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(char.isspace() or ord(char) < 32 or char in "/\\?#%" for char in value)
        or value in {".", ".."}
    ):
        raise HovalApiError(f"Invalid {field}")
    return value


def _topology_list(result: Any, endpoint: str) -> list[Any]:
    """Require a complete list response, accepting the cloud's page wrapper."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        return result["content"]
    raise HovalApiError(f"Unexpected {endpoint} response: expected a list or list 'content'")


def _hours_until_local_midnight(now: datetime | None = None) -> float:
    """Hours from `now` (default: naive local now) until the next 00:00.

    Used by build_v4_temporary_change_body for the MIDNIGHT legacy option.
    Naive local datetime is the right choice here: the Hoval controller schedules
    in its local wall clock, which on Home Assistant Operating System is the
    same as the host's local time. Clamped to the 0.5..24 h range of the app's
    duration picker and rounded to two decimals, as the app does
    (`convertTimeToHours`).
    """
    if now is None:
        now = datetime.now()
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = int((next_midnight - now).total_seconds() // 60)
    return round(max(30, min(1440, minutes)) / 60, 2)


def build_v4_temporary_change_body(
    value: float, duration: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Build the v4 temporary-change request body for the given user option.

    The v4 endpoint takes `{type: "endOfPhase"|"duration", value: <float>,
    duration: <hours>|null}`. The `duration` field is in HOURS (OpenAPI only
    says `double`): verified live on 2026-10-06 by reading back the reported
    `temporaryChange.end` (0.5 → +30 min, 2 → +120 min, 24 accepted), and it
    is what the app sends. Through v1.0.15 this function sent minutes, so
    "4 hours" went out as 240 and "until midnight" as e.g. 537 — both answered
    `424 "Failed to activate temporary change"` (issue #15). The app's
    CustomDuration picker spans 0.5..24 h; stay inside that range.

    Pure function — broken out for unit testing. `now` is only used when
    `duration == DURATION_MIDNIGHT` and exists so tests can pin time.
    """
    if isinstance(value, bool):
        raise HovalApiError("Temporary-change value must be a finite number")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError) as err:
        raise HovalApiError("Temporary-change value must be a finite number") from err
    if not isfinite(value):
        raise HovalApiError("Temporary-change value must be a finite number")
    if duration == DURATION_END_OF_PHASE:
        return {"type": "endOfPhase", "value": value}
    if duration == DURATION_FOUR_HOURS:
        return {"type": "duration", "value": value, "duration": 4}
    if duration == DURATION_MIDNIGHT:
        return {
            "type": "duration",
            "value": value,
            "duration": _hours_until_local_midnight(now),
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
        # Single-flight locks: the coordinator fans out one task per circuit,
        # so a burst of concurrent 401s must trigger at most ONE token refresh
        # instead of a thundering herd against the rate-limited IDP. Separate
        # locks because _get_plant_access_token() calls _get_id_token().
        self._id_token_lock = asyncio.Lock()
        self._pat_lock = asyncio.Lock()

    async def _fetch_json_with_retry(
        self,
        description: str,
        make_request: Callable[[], Any],
        *,
        auth_statuses: tuple[int, ...],
        auth_message: str,
    ) -> Any:
        """Run a token-endpoint request, retrying transient failures like `_request`.

        The two token endpoints were the only calls in this client with neither
        a retry budget nor a timeout of their own, so a single 429 or 5xx from
        the IDP — or a byte-dripping connection — became a hard failure where
        the identical hiccup on a *data* request would have been ridden out over
        three attempts. At setup that surfaces as a bare `cannot_connect` in the
        config flow with no hint of which of six causes it was (issue #11 came
        in exactly that shape, undiagnosable from the message alone); at runtime
        it costs a whole coordinator refresh and blanks every entity until the
        next poll — the same failure mode as the 2026-08-09 HTTP 599 outage.

        `make_request` is called once per attempt and must return a *fresh*
        response context manager. `auth_statuses` are the statuses that mean the
        credentials or token were rejected — a permanent answer, never retried.
        """
        attempt = 0
        while attempt < _MAX_RETRIES:
            try:
                async with make_request() as resp:
                    if resp.status in auth_statuses:
                        _LOGGER.warning("%s rejected (HTTP %s)", description, resp.status)
                        raise HovalAuthError(f"{auth_message} (HTTP {resp.status})")
                    if _is_retryable_status(resp.status) and attempt < _MAX_RETRIES - 1:
                        delay = _RETRY_BASE_DELAY * (2**attempt)
                        _LOGGER.warning(
                            "Transient error HTTP %s during %s, retrying in %.1fs (%d/%d)",
                            resp.status,
                            description,
                            delay,
                            attempt + 1,
                            _MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        attempt += 1
                        continue
                    if resp.status >= 400:
                        # Distinct from the transport message below on purpose:
                        # `raise_for_status()` used to fold every HTTP error into
                        # "Connection error during authentication", which reads
                        # like a dead network when the server in fact answered.
                        # The body is read here for the same reason as in
                        # `_request`: it is the only place the far end explains
                        # itself, and the PAT fetch sits behind the same gateway.
                        body = await resp.text()
                        _LOGGER.warning(
                            "%s failed: HTTP %s, body: %s",
                            description,
                            resp.status,
                            redact_remote_error_body(body) or "<empty>",
                        )
                        raise HovalApiError(
                            f"{description} failed: HTTP {resp.status}",
                            status=resp.status,
                            gateway_blocked=_is_gateway_block(body),
                        )
                    return await resp.json()
            except (HovalAuthError, HovalApiError):
                raise
            except (aiohttp.ClientError, TimeoutError) as err:
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_BASE_DELAY * (2**attempt)
                    _LOGGER.warning(
                        "Connection error during %s, retrying in %.1fs (%d/%d): %s",
                        description,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                        err,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise HovalApiError(f"Connection error during {description}: {err}") from err

        raise HovalApiError(f"{description} failed after {_MAX_RETRIES} retries")

    async def _get_id_token(self) -> str:
        """Get or refresh the ID token via OAuth2 password grant.

        Double-checked locking: the fast path returns the cached token without
        the lock; only a refresh serialises through _id_token_lock.
        """
        if self._id_token and time.time() < self._id_token_exp:
            return self._id_token

        async with self._id_token_lock:
            if self._id_token and time.time() < self._id_token_exp:
                return self._id_token

            data = await self._fetch_json_with_retry(
                "authentication",
                lambda: self._session.post(
                    IDP_URL,
                    data={
                        "grant_type": "password",
                        "client_id": CLIENT_ID,
                        "username": self._email,
                        "password": self._password,
                        "scope": "openid",
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": USER_AGENT,
                    },
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ),
                auth_statuses=(400, 401, 403),
                auth_message="Invalid credentials",
            )

            # A non-dict body would make the `.keys()` log below raise
            # AttributeError, which escapes the config flow's HovalApiError
            # handler and degrades the dialog to "unknown error".
            if not isinstance(data, dict):
                _LOGGER.error("IDP returned %s, expected a JSON object", type(data).__name__)
                raise HovalApiError(
                    f"IDP response is a {type(data).__name__}, expected a JSON object"
                )
            if "id_token" not in data:
                _LOGGER.error("IDP response missing id_token. Keys: %s", list(data.keys()))
                raise HovalApiError("IDP response missing id_token")

            self._id_token = data["id_token"]
            self._id_token_exp = time.time() + ID_TOKEN_TTL.total_seconds()
            return self._id_token

    async def _get_plant_access_token(self, plant_id: str) -> str:
        """Get or refresh the plant access token.

        Double-checked locking mirrors _get_id_token: the cached fast path
        stays lock-free; only a refresh serialises through _pat_lock, with the
        cache re-checked inside the lock.
        """
        cached = self._pat_cache.get(plant_id)
        if cached and time.time() < cached[1]:
            return cached[0]

        async with self._pat_lock:
            cached = self._pat_cache.get(plant_id)
            if cached and time.time() < cached[1]:
                return cached[0]

            id_token = await self._get_id_token()
            try:
                data = await self._fetch_json_with_retry(
                    "plant token fetch",
                    lambda: self._session.get(
                        f"{BASE_URL}/v1/plants/{plant_id}/settings",
                        headers={
                            "Authorization": f"Bearer {id_token}",
                            "User-Agent": USER_AGENT,
                        },
                        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    ),
                    auth_statuses=(401,),
                    auth_message="ID token rejected",
                )
            except HovalAuthError:
                # The ID token we just sent is dead; drop it so the next call
                # re-logs in instead of replaying the rejected token.
                self._id_token = None
                raise

            # `data["token"]` used to raise KeyError straight through the
            # coordinator's HovalApiError handler and land as "Unexpected
            # error fetching data" with no mention of Hoval.
            if not isinstance(data, dict) or "token" not in data:
                _LOGGER.error(
                    "Plant settings for %s carried no access token (keys: %s)",
                    plant_id,
                    list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                )
                raise HovalApiError(f"Plant settings response for {plant_id} has no token")

            token = data["token"]
            self._pat_cache[plant_id] = (token, time.time() + PLANT_TOKEN_TTL.total_seconds())
            return token

    async def _headers(self, plant_id: str | None = None) -> dict[str, str]:
        """Build request headers with auth tokens.

        The User-Agent is set per request on purpose. Home Assistant assigns its
        own as a *session* default, and aiohttp lets a per-request header of the
        same name win — which is the only way to override it while still using
        HA's shared session (and with it HA's connector, SSL context and
        cleanup). See USER_AGENT in const.py for why it matters here.
        """
        id_token = await self._get_id_token()
        headers = {"Authorization": f"Bearer {id_token}", "User-Agent": USER_AGENT}
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
        *,
        quiet_statuses: tuple[int, ...] = (),
    ) -> Any:
        """Make an authenticated API request with token retry and transient error backoff.

        At most one 401-driven token refresh is allowed per call, and it
        does not consume an attempt from the transient-error budget. The
        previous implementation recursed on 401 with its own full retry
        budget, so combined 401 + 429/5xx flows could fire up to
        ``2 * _MAX_RETRIES`` requests.
        """
        url = f"{BASE_URL}{path}"
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        token_refreshed = False
        attempt = 0
        # A lost response can follow an already committed control action.
        # Only reads are repeated on ambiguous failures; 401 remains a
        # definite rejection and gets its one token-refresh retry below.
        safe_to_retry = method.upper() in _SAFE_RETRY_METHODS

        while attempt < _MAX_RETRIES:
            # Re-fetch headers every iteration: after a 401 we cleared the
            # cached tokens, and a stale header dict would re-send the
            # expired token.
            headers = await self._headers(plant_id)
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
                        if token_refreshed:
                            raise HovalAuthError("Authentication failed")
                        token_refreshed = True
                        _LOGGER.debug("Token expired, refreshing and retrying")
                        # Do not increment `attempt` — token refresh is a
                        # one-shot extra request, not a transient retry.
                        continue
                    if (
                        safe_to_retry
                        and _is_retryable_status(resp.status)
                        and attempt < _MAX_RETRIES - 1
                    ):
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
                        attempt += 1
                        continue
                    if resp.status >= 400:
                        body = await resp.text()
                        blocked = _is_gateway_block(body)
                        # WARNING, not DEBUG: this body is the only place the
                        # cloud explains itself, and a user reporting a bug has
                        # no reason to have debug logging on. Issue #11 cost two
                        # round trips for exactly that reason — the report said
                        # "API request failed: HTTP 403" with neither the
                        # endpoint nor Hoval's own reason for refusing.
                        # Optional endpoints can let their caller handle a
                        # specific status and report its retry policy once.
                        log = (
                            _LOGGER.debug
                            if resp.status in quiet_statuses and not blocked
                            else _LOGGER.warning
                        )
                        log(
                            "API %s %s → HTTP %s, body: %s",
                            method,
                            path,
                            resp.status,
                            redact_remote_error_body(body) or "<empty>",
                        )
                        if blocked:
                            _LOGGER.error(
                                "Hoval's gateway refused this client on %s %s (HTTP %s) — the "
                                "request never reached Hoval. This is not an account problem.",
                                method,
                                path,
                                resp.status,
                            )
                        raise HovalApiError(
                            f"API request failed: HTTP {resp.status} on {method} {path}",
                            status=resp.status,
                            gateway_blocked=blocked,
                            request_path=path,
                        )
                    if resp.status == 204 or resp.content_length == 0:
                        return None
                    return await resp.json()
            except (HovalAuthError, HovalApiError):
                raise
            except TimeoutError as err:
                if safe_to_retry and attempt < _MAX_RETRIES - 1:
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
                    attempt += 1
                    continue
                raise HovalApiError(f"Request timeout: {err}") from err
            except aiohttp.ClientError as err:
                if safe_to_retry and attempt < _MAX_RETRIES - 1:
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
                    attempt += 1
                    continue
                raise HovalApiError(f"Connection error: {err}") from err

        raise HovalApiError(f"Request failed after {_MAX_RETRIES} retries")

    async def get_plants(self) -> list[dict[str, Any]]:
        """Get list of user's plants, fetching all pages.

        Hoval's /api/my-plants endpoint was updated in May 2026 to enforce a
        maximum page size of 12 items.  The response may be:
          - A plain list (old API shape) — returned as-is.
          - A Spring/Page wrapper {"content": [...], "last": bool, ...} — the
            integration iterates all pages and returns a flat list.
        """
        all_plants: list[dict[str, Any]] = []
        for page in range(_MAX_PLANT_PAGES):
            result = await self._request(
                "GET", "/api/my-plants", params={"size": "12", "page": str(page)}
            )
            content = _topology_list(result, "get_plants")
            for plant in content:
                if not isinstance(plant, dict):
                    raise HovalApiError("get_plants returned a non-object plant")
                _require_identifier(plant.get("plantExternalId"), "plant ID")
            if isinstance(result, list):
                # Old (pre-pagination) API shape: plain list, no further pages.
                if page:
                    raise HovalApiError("get_plants changed response shape during pagination")
                return content
            last = result.get("last", True)
            if not isinstance(last, bool) or (not last and not content):
                raise HovalApiError("get_plants returned inconsistent pagination metadata")
            all_plants.extend(content)
            if last:
                return all_plants
        raise HovalApiError(
            f"get_plants pagination exceeded {_MAX_PLANT_PAGES} pages; "
            "refusing to return partial account topology"
        )

    async def get_plant_settings(self, plant_id: str) -> dict[str, Any]:
        """Get plant settings (also refreshes PAT as side effect)."""
        return await self._request("GET", f"/v1/plants/{plant_id}/settings", plant_id=plant_id)

    async def get_circuits(self, plant_id: str) -> list[dict[str, Any]]:
        """Get all circuits for a plant.

        Hoval removed the v1 endpoint around 2026-04-21; v3 is the only path that
        still works. Response shape changed: see coordinator field mapping.
        A Spring-Page wrapper {"content": [...], ...} is normalised to its
        content list; malformed or incomplete topology raises an API error.
        """
        result = await self._request("GET", f"/v3/plants/{plant_id}/circuits", plant_id=plant_id)
        content = _topology_list(result, "get_circuits")
        if isinstance(result, dict) and result.get("last", True) is not True:
            raise HovalApiError("get_circuits returned incomplete topology")
        return content

    async def get_programs(self, plant_id: str, circuit_path: str) -> Any:
        """Get time programs for a circuit."""
        return await self._request(
            "GET",
            f"/v3/plants/{plant_id}/circuits/{circuit_path}/programs",
            plant_id=plant_id,
            quiet_statuses=(417,),
        )

    async def get_circuit_details(self, plant_id: str, circuit_path: str) -> Any:
        """Get circuit details, incl. `temporaryChangeLimits` ({min, max, step}).

        The limits are what the controller accepts for a temporary change right
        now; the cloud answers 424 outside them. They are not constant: a DHW
        circuit's max moved between 51 and 49 °C within one day (issue #15).
        """
        return await self._request(
            "GET",
            f"/v3/plants/{plant_id}/circuits/{circuit_path}",
            plant_id=plant_id,
            quiet_statuses=(417,),
        )

    async def get_datapoints(self, plant_id: str, addresses: list[str]) -> dict[str, str]:
        """Read raw controller datapoints straight off the device.

        `addresses` are `<circuitPath>.<DatapointId>`, e.g. "520.50.0.39652".
        The Modbus *register* number is not accepted here — see
        CIRCUIT_DATAPOINT_IDS in const.py.

        The endpoint never rejects an address: an unknown one is simply missing
        from the returned map, and asking only for unknown addresses yields
        `{}`. A short result therefore means "address not understood", not
        "device has no data", and must not be treated as an error.
        """
        if not addresses:
            return {}
        result = await self._request(
            "GET",
            f"/api/telemetry-data/snapshots/live/{plant_id}",
            plant_id=plant_id,
            params={"dataPoints": ",".join(addresses)},
        )
        if not isinstance(result, dict):
            return {}
        return {k: v for k, v in result.items() if isinstance(v, str)}

    async def get_live_values(
        self, plant_id: str, circuit_path: str, circuit_type: str
    ) -> list[dict[str, str]]:
        """Get live sensor values for a circuit.

        A Spring-Page wrapper {"content": [...], ...} is normalised to its
        content list; any other non-list shape degrades to [].
        """
        result = await self._request(
            "GET",
            f"/v3/api/statistics/live-values/{plant_id}",
            plant_id=plant_id,
            params={"circuitPath": circuit_path, "circuitType": circuit_type},
        )
        if isinstance(result, dict):
            _LOGGER.debug(
                "get_live_values returned paginated wrapper for circuit %s; extracting 'content'",
                circuit_path,
            )
            return result.get("content", [])
        return result if isinstance(result, list) else []

    async def get_events(self, plant_id: str) -> list[dict[str, Any]]:
        """Get plant error events.

        Normalised to a plain list like get_circuits()/get_live_values(): a
        Spring-Page wrapper's 'content' is extracted and any non-list shape
        degrades to [].
        """
        result = await self._request("GET", f"/v1/plant-events/{plant_id}")
        if isinstance(result, dict):
            content = result.get("content", [])
            return content if isinstance(content, list) else []
        return result if isinstance(result, list) else []

    async def get_latest_event(self, plant_id: str) -> dict[str, Any]:
        """Get latest plant event.

        Always returns a dict; {} means "no event available". A Spring-Page
        wrapper is unwrapped to its first content element so callers keep
        receiving a single event dict.
        """
        result = await self._request("GET", f"/v1/plant-events/latest/{plant_id}")
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            content = result["content"]
            return content[0] if content and isinstance(content[0], dict) else {}
        return result if isinstance(result, dict) else {}

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
             "duration": <hours>|null}
        For HV the value is the air volume percentage (15..100); for HK it is the
        temperature in degrees Celsius (e.g. 21.5).

        `duration` accepts the user-facing enum from CONF_OVERRIDE_DURATION:
        - DURATION_END_OF_PHASE ("endOfPhase") — body type=endOfPhase, no
          duration. Safest default — overrides the current schedule until the
          next program phase boundary.
        - DURATION_FOUR_HOURS ("FOUR") — body type=duration, duration=4 (hours).
        - DURATION_MIDNIGHT ("MIDNIGHT") — body type=duration, duration=hours
          until next local midnight, clamped to 0.5..24.

        v3 (`/v3/.../temporary-change`) still works at the time of writing but
        is marked legacy by the cloud (operationId `activateTemporaryChange_1`).
        Reset is still v3-only: see `reset_temporary_change`.
        """
        plant_id = _require_identifier(plant_id, "plant ID")
        circuit_path = _require_identifier(circuit_path, "circuit path")
        body = build_v4_temporary_change_body(value, duration)
        _LOGGER.debug(
            "set_temporary_change: plant=%s circuit=%s duration=%s body=%s",
            plant_id,
            circuit_path,
            duration,
            body,
        )
        try:
            result = await self._request(
                "POST",
                f"/v4/plants/{plant_id}/circuits/{circuit_path}/temporary-change",
                plant_id=plant_id,
                json_data=body,
            )
        except HovalApiError as err:
            if err.status != 424:
                raise
            # 424 "Failed to activate temporary change" is all the cloud says.
            # Measured on HV (limits 15..100): 14 and 101 → 424, 50 → 204. A
            # duration outside 0.5..24 h gives the same 424, but we never send one.
            raise HovalApiError(
                f"{err} — the controller refused value {body['value']:g}; it is most likely "
                "outside the range the controller accepts right now",
                status=err.status,
                request_path=err.request_path,
            ) from err
        _LOGGER.debug("set_temporary_change: completed successfully")
        return result

    async def reset_temporary_change(self, plant_id: str, circuit_path: str) -> Any:
        """Cancel an active temporary override and resume the underlying program.

        v3: DELETE /v3/plants/{plantId}/circuits/{circuitPath}/temporary-change
        Replaces the removed v1 .../temporary-change/reset POST.
        """
        plant_id = _require_identifier(plant_id, "plant ID")
        circuit_path = _require_identifier(circuit_path, "circuit path")
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
        plant_id = _require_identifier(plant_id, "plant ID")
        circuit_path = _require_identifier(circuit_path, "circuit path")
        if not isinstance(program, str) or program not in _VALID_PROGRAMS:
            raise HovalApiError("Invalid circuit program")
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
