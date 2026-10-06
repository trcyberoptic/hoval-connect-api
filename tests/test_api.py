"""Tests for the Hoval Connect API client."""

from __future__ import annotations

import asyncio
import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Preserve the real asyncio module
_real_asyncio = asyncio

# Mock homeassistant modules so we can import without HA installed
ha_mock = MagicMock()
sys.modules.setdefault("homeassistant", ha_mock)
sys.modules.setdefault("homeassistant.config_entries", ha_mock)
sys.modules.setdefault("homeassistant.const", ha_mock)
sys.modules.setdefault("homeassistant.core", ha_mock)
sys.modules.setdefault("homeassistant.exceptions", ha_mock)
sys.modules.setdefault("homeassistant.helpers", ha_mock)
sys.modules.setdefault("homeassistant.helpers.update_coordinator", ha_mock)
sys.modules.setdefault("homeassistant.helpers.aiohttp_client", ha_mock)
sys.modules.setdefault("homeassistant.helpers.device_registry", ha_mock)
sys.modules.setdefault("homeassistant.helpers.dispatcher", ha_mock)
sys.modules.setdefault("homeassistant.util", ha_mock)
sys.modules.setdefault("homeassistant.util.dt", ha_mock)
sys.modules.setdefault("voluptuous", ha_mock)

import aiohttp  # noqa: E402

from custom_components.hoval_connect.api import (  # noqa: E402
    _MAX_RETRIES,
    HovalApiError,
    HovalAuthError,
    HovalConnectApi,
    _hours_until_local_midnight,
    _is_gateway_block,
    _is_retryable_status,
    build_v4_temporary_change_body,
)
from custom_components.hoval_connect.const import (  # noqa: E402
    DURATION_END_OF_PHASE,
    DURATION_FOUR_HOURS,
    DURATION_MIDNIGHT,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

# Retrying token endpoints means the transport-failure tests now walk the whole
# backoff ladder; patch the sleep or every one of them costs 3 real seconds.
_SLEEP = "custom_components.hoval_connect.api.asyncio.sleep"


def _make_response(status: int, json_data=None, text: str = "") -> MagicMock:
    """Create a mock aiohttp response."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text)
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = aiohttp.ClientResponseError(
            request_info=MagicMock(),
            history=(),
            status=status,
        )
    # Make it work as async context manager
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_session() -> MagicMock:
    """Create a mock aiohttp session."""
    session = MagicMock(spec=aiohttp.ClientSession)
    return session


class TestHovalConnectApiAuth:
    """Tests for authentication logic."""

    @pytest.mark.asyncio
    async def test_get_id_token_success(self):
        session = _make_session()
        resp = _make_response(200, {"id_token": "test-token-123"})
        session.post = MagicMock(return_value=resp)

        api = HovalConnectApi(session, "test@example.com", "password123")
        token = await api._get_id_token()

        assert token == "test-token-123"
        session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_id_token_caches(self):
        session = _make_session()
        resp = _make_response(200, {"id_token": "test-token-123"})
        session.post = MagicMock(return_value=resp)

        api = HovalConnectApi(session, "test@example.com", "password123")
        token1 = await api._get_id_token()
        token2 = await api._get_id_token()

        assert token1 == token2
        # Should only call post once due to caching
        assert session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_get_id_token_invalid_credentials(self):
        session = _make_session()
        for status in (400, 401, 403):
            resp = _make_response(status)
            session.post = MagicMock(return_value=resp)

            api = HovalConnectApi(session, "test@example.com", "wrong")
            with pytest.raises(HovalAuthError, match="Invalid credentials"):
                await api._get_id_token()

    @pytest.mark.asyncio
    async def test_get_id_token_missing_token_in_response(self):
        session = _make_session()
        resp = _make_response(200, {"access_token": "wrong-field"})
        session.post = MagicMock(return_value=resp)

        api = HovalConnectApi(session, "test@example.com", "password123")
        with pytest.raises(HovalApiError, match="missing id_token"):
            await api._get_id_token()

    @pytest.mark.asyncio
    async def test_get_id_token_connection_error(self):
        session = _make_session()
        session.post = MagicMock(side_effect=aiohttp.ClientError("connection failed"))

        api = HovalConnectApi(session, "test@example.com", "password123")
        with (
            patch(_SLEEP, new_callable=AsyncMock),
            pytest.raises(HovalApiError, match="Connection error"),
        ):
            await api._get_id_token()
        # Transport failures exhaust the retry budget before giving up.
        assert session.post.call_count == _MAX_RETRIES

    @pytest.mark.asyncio
    async def test_get_id_token_timeout(self):
        session = _make_session()
        session.post = MagicMock(side_effect=_real_asyncio.TimeoutError())

        api = HovalConnectApi(session, "test@example.com", "password123")
        with (
            patch(_SLEEP, new_callable=AsyncMock),
            pytest.raises(HovalApiError, match="Connection error"),
        ):
            await api._get_id_token()
        assert session.post.call_count == _MAX_RETRIES

    @pytest.mark.asyncio
    async def test_get_id_token_retries_transient_idp_error(self):
        """A 503 at the IDP must not fail the whole login.

        Before this, the two token endpoints were the only calls in the client
        with no retry budget: one hiccup at SAP IAS aborted the config flow with
        a bare `cannot_connect`, or cost a full coordinator refresh at runtime,
        while the identical hiccup on a data request was ridden out silently.
        """
        session = _make_session()
        session.post = MagicMock(
            side_effect=[_make_response(503), _make_response(200, {"id_token": "recovered"})]
        )

        api = HovalConnectApi(session, "test@example.com", "password123")
        with patch(_SLEEP, new_callable=AsyncMock):
            assert await api._get_id_token() == "recovered"
        assert session.post.call_count == 2

    @pytest.mark.asyncio
    async def test_get_id_token_retries_rate_limit(self):
        """429 is retryable here too — a user re-trying the dialog can trip it."""
        session = _make_session()
        session.post = MagicMock(
            side_effect=[_make_response(429), _make_response(200, {"id_token": "recovered"})]
        )

        api = HovalConnectApi(session, "test@example.com", "password123")
        with patch(_SLEEP, new_callable=AsyncMock):
            assert await api._get_id_token() == "recovered"

    @pytest.mark.asyncio
    async def test_get_id_token_persistent_server_error_names_the_status(self):
        """A server that answered must not be reported as a connection error.

        `raise_for_status()` folded every non-auth HTTP status into "Connection
        error during authentication", which reads like a dead network and sent
        at least one bug report chasing firewalls instead of an HTTP 5xx.
        """
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(500))

        api = HovalConnectApi(session, "test@example.com", "password123")
        with (
            patch(_SLEEP, new_callable=AsyncMock),
            pytest.raises(HovalApiError, match="authentication failed: HTTP 500"),
        ):
            await api._get_id_token()

    @pytest.mark.asyncio
    async def test_get_id_token_permanent_client_error_is_not_retried(self):
        """404 is neither an auth rejection nor transient — fail on the first answer."""
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(404))

        api = HovalConnectApi(session, "test@example.com", "password123")
        with pytest.raises(HovalApiError, match="HTTP 404"):
            await api._get_id_token()
        assert session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_get_id_token_auth_rejection_is_not_retried(self):
        """Wrong credentials are a permanent answer; retrying only risks a lockout."""
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(401))

        api = HovalConnectApi(session, "test@example.com", "wrong")
        with pytest.raises(HovalAuthError):
            await api._get_id_token()
        assert session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_get_id_token_non_dict_response(self):
        """A JSON array body must not escape as AttributeError from `.keys()`.

        Anything that is not HovalAuthError/HovalApiError bypasses the config
        flow's handlers and degrades the dialog to "unknown error occurred".
        """
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, ["not", "an", "object"]))

        api = HovalConnectApi(session, "test@example.com", "password123")
        with pytest.raises(HovalApiError, match="expected a JSON object"):
            await api._get_id_token()

    @pytest.mark.asyncio
    async def test_token_requests_are_bounded_by_request_timeout(self):
        """Both token calls must pass their own timeout.

        Neither did, so they inherited aiohttp's 5-minute default: a stalled
        IDP could hold a coordinator refresh far past the 60 s scan interval
        (`_request`'s own timeout does not cover the nested token fetch).
        """
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))

        api = HovalConnectApi(session, "test@example.com", "password123")
        await api._get_id_token()

        timeout = session.post.call_args.kwargs["timeout"]
        assert timeout.total == REQUEST_TIMEOUT

    @pytest.mark.asyncio
    async def test_invalidate_tokens(self):
        session = _make_session()
        resp = _make_response(200, {"id_token": "token-1"})
        session.post = MagicMock(return_value=resp)

        api = HovalConnectApi(session, "test@example.com", "password123")
        await api._get_id_token()
        assert api._id_token == "token-1"

        api.invalidate_tokens()
        assert api._id_token is None
        assert api._id_token_exp == 0
        assert api._pat_cache == {}

    @pytest.mark.asyncio
    async def test_concurrent_id_token_requests_single_flight(self):
        session = _make_session()
        resp = _make_response(200, {"id_token": "test-token-123"})

        async def _json_with_suspension() -> dict:
            # A plain AsyncMock never yields to the event loop, so the five
            # gathered tasks would run to completion one after another and
            # mask a missing lock. sleep(0) forces a real suspension point,
            # interleaving the callers like genuine network I/O does.
            await _real_asyncio.sleep(0)
            return {"id_token": "test-token-123"}

        resp.json = _json_with_suspension
        session.post = MagicMock(return_value=resp)

        api = HovalConnectApi(session, "test@example.com", "password123")
        tokens = await _real_asyncio.gather(*(api._get_id_token() for _ in range(5)))

        assert set(tokens) == {"test-token-123"}
        # Without the single-flight lock every concurrent caller fires its
        # own IDP login; with it, exactly one request goes out.
        assert session.post.call_count == 1


class TestHovalConnectApiRequest:
    """Tests for the _request method."""

    @pytest.mark.asyncio
    async def test_request_success(self):
        session = _make_session()
        # Mock auth
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        # Mock API response
        api_resp = _make_response(200, {"data": "test"})
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api._request("GET", "/api/test")

        assert result == {"data": "test"}

    @pytest.mark.asyncio
    async def test_request_204_returns_none(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        api_resp = _make_response(204)
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api._request("POST", "/api/test")

        assert result is None

    @pytest.mark.asyncio
    async def test_request_401_retries_with_fresh_token(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        # First call returns 401, second succeeds
        resp_401 = _make_response(401)
        resp_ok = _make_response(200, {"data": "ok"})
        session.request = MagicMock(side_effect=[resp_401, resp_ok])

        api = HovalConnectApi(session, "test@example.com", "pass")
        # Need to prime the token first
        await api._get_id_token()
        result = await api._request("GET", "/api/test")

        assert result == {"data": "ok"}

    @pytest.mark.asyncio
    async def test_request_401_twice_raises_auth_error(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        resp_401 = _make_response(401)
        session.request = MagicMock(return_value=resp_401)

        api = HovalConnectApi(session, "test@example.com", "pass")
        with pytest.raises(HovalAuthError, match="Authentication failed"):
            await api._request("GET", "/api/test")

    @pytest.mark.asyncio
    async def test_request_4xx_raises_api_error(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        resp_404 = _make_response(404, text="not found")
        session.request = MagicMock(return_value=resp_404)

        api = HovalConnectApi(session, "test@example.com", "pass")
        with pytest.raises(HovalApiError, match="HTTP 404"):
            await api._request("GET", "/api/test")

    @pytest.mark.asyncio
    async def test_request_retries_on_transient_errors(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        # First returns 503, second succeeds
        resp_503 = _make_response(503)
        resp_ok = _make_response(200, {"data": "recovered"})
        session.request = MagicMock(side_effect=[resp_503, resp_ok])

        api = HovalConnectApi(session, "test@example.com", "pass")
        with patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock):
            result = await api._request("GET", "/api/test")

        assert result == {"data": "recovered"}

    @pytest.mark.asyncio
    async def test_request_retries_on_nonstandard_gateway_status(self):
        """Hoval's gateway sporadically answers 599 ("network connect timeout").

        Observed live on 2026-08-09: ten one-poll outages in a day, each logged as
        `Circuits endpoint failed ... HTTP 599`, each blanking every entity for the
        60 s until the next refresh. 599 is not a standard code and was missing from
        the old enumerated retry set.
        """
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.request = MagicMock(
            side_effect=[_make_response(599), _make_response(200, {"data": "recovered"})]
        )

        api = HovalConnectApi(session, "test@example.com", "pass")
        with patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock):
            result = await api._request("GET", "/api/test")

        assert result == {"data": "recovered"}
        assert session.request.call_count == 2

    @pytest.mark.asyncio
    async def test_request_retries_exhausted_raises(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        resp_503 = _make_response(503)
        session.request = MagicMock(return_value=resp_503)

        api = HovalConnectApi(session, "test@example.com", "pass")
        with (
            patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(HovalApiError, match="HTTP 503"),
        ):
            await api._request("GET", "/api/test")

    @pytest.mark.asyncio
    async def test_request_timeout_retries(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        # First call times out, second succeeds
        resp_ok = _make_response(200, {"data": "ok"})

        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _real_asyncio.TimeoutError()
            return resp_ok

        session.request = MagicMock(side_effect=_side_effect)

        api = HovalConnectApi(session, "test@example.com", "pass")
        with patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock):
            result = await api._request("GET", "/api/test")

        assert result == {"data": "ok"}

    @pytest.mark.asyncio
    async def test_request_timeout_all_retries_raises(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        session.request = MagicMock(side_effect=_real_asyncio.TimeoutError())

        api = HovalConnectApi(session, "test@example.com", "pass")
        with (
            patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(HovalApiError, match="timeout"),
        ):
            await api._request("GET", "/api/test")

    @pytest.mark.asyncio
    async def test_request_connection_error_retries(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        resp_ok = _make_response(200, {"data": "ok"})
        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise aiohttp.ClientError("conn refused")
            return resp_ok

        session.request = MagicMock(side_effect=_side_effect)

        api = HovalConnectApi(session, "test@example.com", "pass")
        with patch("custom_components.hoval_connect.api.asyncio.sleep", new_callable=AsyncMock):
            result = await api._request("GET", "/api/test")

        assert result == {"data": "ok"}


class TestHovalConnectApiEndpoints:
    """Tests for specific API endpoint methods."""

    @pytest.mark.asyncio
    async def test_get_plants(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        plants_data = [{"plantExternalId": "p1", "description": "My Plant"}]
        api_resp = _make_response(200, plants_data)
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_plants()

        assert result == plants_data

    @pytest.mark.asyncio
    async def test_get_plants_paginated_single_page(self):
        """get_plants handles Spring Page wrapper {"content": [...], "last": True}."""
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        plants_data = [{"plantExternalId": "p1"}, {"plantExternalId": "p2"}]
        page_resp = _make_response(200, {"content": plants_data, "last": True, "totalPages": 1})
        session.request = MagicMock(return_value=page_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_plants()

        assert result == plants_data
        assert session.request.call_count == 1

    @pytest.mark.asyncio
    async def test_get_plants_paginated_multiple_pages(self):
        """get_plants fetches all pages and returns a flat list."""
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        page0 = [{"plantExternalId": f"p{i}"} for i in range(12)]
        page1 = [{"plantExternalId": "p12"}]
        resp_page0 = _make_response(200, {"content": page0, "last": False, "totalPages": 2})
        resp_page1 = _make_response(200, {"content": page1, "last": True, "totalPages": 2})
        session.request = MagicMock(side_effect=[resp_page0, resp_page1])

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_plants()

        assert len(result) == 13
        assert result[0]["plantExternalId"] == "p0"
        assert result[12]["plantExternalId"] == "p12"
        assert session.request.call_count == 2

    @pytest.mark.asyncio
    async def test_get_circuits_paginated_wrapper(self):
        """get_circuits extracts 'content' when API returns a paginated wrapper."""
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)
        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        circuits = [{"type": "HK", "path": "1.1.0"}, {"type": "BL", "path": "1.10.1"}]
        paginated = {"content": circuits, "totalElements": 2, "totalPages": 1, "last": True}
        api_resp = _make_response(200, paginated)
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_circuits("plant-1")

        assert result == circuits

    @pytest.mark.asyncio
    async def test_get_live_values_paginated_wrapper(self):
        """get_live_values extracts 'content' when the API returns a paginated wrapper.

        Regression: Hoval's May 2026 API change introduced pagination on this
        endpoint.  Without the fix the coordinator would receive a dict, iterate
        over its string keys, and crash with TypeError inside _fetch_circuit —
        causing BL to be silently dropped from plant_data.circuits.
        """
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)
        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        lv = [{"key": "tempActual", "value": "24.5"}, {"key": "operatingHours", "value": "13751"}]
        paginated = {"content": lv, "totalElements": 2, "size": 12, "last": True}
        api_resp = _make_response(200, paginated)
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_live_values("plant-1", "1.10.1", "BL")

        assert result == lv

    @pytest.mark.asyncio
    async def test_get_live_values_none_returns_empty_list(self):
        """HTTP 204 or empty body (→ None from _request) must return [] not None.

        If get_live_values returned None, the coordinator dict comprehension
        'for v in lv_raw' would raise TypeError and crash _fetch_circuit.
        """
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        # Simulate a 204 response (_request returns None)
        api_resp = _make_response(204)
        session.request = MagicMock(return_value=api_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_live_values("plant-1", "1.10.1", "BL")

        assert result == []

    @pytest.mark.asyncio
    async def test_get_plant_settings_uses_request(self):
        """Verify get_plant_settings goes through _request (not raw session.get)."""
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        # PAT fetch uses session.get directly (in _get_plant_access_token)
        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        # Actual settings call goes through _request → session.request
        settings_resp = _make_response(200, {"token": "pat-123", "setting1": "val"})
        session.request = MagicMock(return_value=settings_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_plant_settings("plant-1")

        assert result["setting1"] == "val"
        # Verify _request was used (session.request called)
        session.request.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_temporary_change_posts_v4_with_end_of_phase(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        control_resp = _make_response(204)
        session.request = MagicMock(return_value=control_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.set_temporary_change("plant-1", "1.2.3", 65, DURATION_END_OF_PHASE)

        assert result is None  # 204 returns None
        # Verify the request was sent to v4 with the right body
        session.request.assert_called_once()
        call = session.request.call_args
        # First positional arg is method, second is url
        assert call.args[0] == "POST"
        assert "/v4/plants/plant-1/circuits/1.2.3/temporary-change" in call.args[1]
        assert call.kwargs.get("json") == {"type": "endOfPhase", "value": 65}

    @pytest.mark.asyncio
    async def test_set_temporary_change_translates_legacy_four_to_v4_duration(self):
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "tok"}))
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat"}))
        session.request = MagicMock(return_value=_make_response(204))

        api = HovalConnectApi(session, "u", "p")
        await api.set_temporary_change("plant-1", "1.2.3", 21.5, DURATION_FOUR_HOURS)

        body = session.request.call_args.kwargs.get("json")
        # v4 duration is in HOURS — 240 (the old minutes value) is a 424
        assert body == {"type": "duration", "value": 21.5, "duration": 4}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [424, 400])
    async def test_set_temporary_change_explains_only_a_424(self, status):
        """424 is the cloud's whole answer to an out-of-range value (issue #15)."""
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "tok"}))
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat"}))
        session.request = MagicMock(
            return_value=_make_response(status, {"detail": "Failed to activate temporary change"})
        )

        api = HovalConnectApi(session, "u", "p")
        with pytest.raises(HovalApiError) as exc:
            await api.set_temporary_change("plant-1", "1.2.0", 55, DURATION_END_OF_PHASE)

        assert exc.value.status == status
        assert ("refused value 55" in str(exc.value)) is (status == 424)
        assert f"HTTP {status}" in str(exc.value)
        session.request.assert_called_once()


class TestBuildV4TemporaryChangeBody:
    """Tests for build_v4_temporary_change_body — pure function, no I/O.

    The cloud's v4 temporary-change endpoint takes `duration` in HOURS,
    verified live on 2026-10-06 against an HV circuit by reading back the
    reported `temporaryChange.end`: 0.5 → +30 min, 2 → +120 min, 8.25 →
    +495 min, 24 and 25 accepted; 240 and 498 → HTTP 424 "Failed to activate
    temporary change" (issue #15). The app sends the same unit
    (`convertTimeToHours`, rounded to two decimals, picker range 0.5..24).
    """

    def test_end_of_phase(self):
        body = build_v4_temporary_change_body(70, DURATION_END_OF_PHASE)
        assert body == {"type": "endOfPhase", "value": 70}

    def test_four_hours_is_4(self):
        body = build_v4_temporary_change_body(21.5, DURATION_FOUR_HOURS)
        assert body == {"type": "duration", "value": 21.5, "duration": 4}

    def test_midnight_hours_pinned_to_now(self):
        # Pin "now" to 22:30 → 1.5 hours until 00:00
        from datetime import datetime as _dt

        now = _dt(2026, 5, 23, 22, 30, 0)
        body = build_v4_temporary_change_body(22, DURATION_MIDNIGHT, now=now)
        assert body == {"type": "duration", "value": 22, "duration": 1.5}

    def test_midnight_clamps_to_half_hour_when_too_close(self):
        """The app's picker starts at 30 minutes; the helper clamps to it."""
        from datetime import datetime as _dt

        now = _dt(2026, 5, 23, 23, 59, 0)  # 1 minute to midnight
        body = build_v4_temporary_change_body(22, DURATION_MIDNIGHT, now=now)
        assert body["duration"] == 0.5  # clamped

    def test_midnight_at_midnight_is_24(self):
        from datetime import datetime as _dt

        now = _dt(2026, 5, 23, 0, 0, 0)
        body = build_v4_temporary_change_body(22, DURATION_MIDNIGHT, now=now)
        assert body["duration"] == 24

    def test_duration_never_leaves_accepted_range(self):
        """Regression for #15: every option must stay within 0.5..24 hours."""
        from datetime import datetime as _dt

        bodies = [build_v4_temporary_change_body(50, DURATION_FOUR_HOURS)]
        bodies += [
            build_v4_temporary_change_body(50, DURATION_MIDNIGHT, now=_dt(2026, 10, 6, h, m, 0))
            for h in range(24)
            for m in (0, 1, 29, 30, 59)
        ]
        for body in bodies:
            assert 0.5 <= body["duration"] <= 24, body

    def test_unknown_duration_falls_back_to_end_of_phase(self):
        body = build_v4_temporary_change_body(50, "weirdOption")
        assert body == {"type": "endOfPhase", "value": 50}

    def test_hours_until_local_midnight_basic(self):
        from datetime import datetime as _dt

        assert _hours_until_local_midnight(_dt(2026, 5, 23, 0, 0, 0)) == 24
        assert _hours_until_local_midnight(_dt(2026, 5, 23, 12, 0, 0)) == 12
        assert _hours_until_local_midnight(_dt(2026, 5, 23, 23, 0, 0)) == 1
        # 15:05 → 535 min → rounded to two decimals, like the app
        assert _hours_until_local_midnight(_dt(2026, 10, 6, 15, 5, 0)) == 8.92

    def test_hours_until_local_midnight_clamped(self):
        from datetime import datetime as _dt

        # too close to midnight → clamped to lower bound 0.5
        assert _hours_until_local_midnight(_dt(2026, 5, 23, 23, 59, 30)) == 0.5

    @pytest.mark.asyncio
    async def test_invalidate_plant_token(self):
        api = HovalConnectApi(MagicMock(), "test@example.com", "pass")
        api._pat_cache["plant-1"] = ("token", 9999999999)

        api.invalidate_plant_token("plant-1")
        assert "plant-1" not in api._pat_cache

    @pytest.mark.asyncio
    async def test_invalidate_nonexistent_plant_token(self):
        """Should not raise when invalidating non-cached plant."""
        api = HovalConnectApi(MagicMock(), "test@example.com", "pass")
        api.invalidate_plant_token("nonexistent")  # Should not raise


_GATEWAY_403 = (
    "<html>\n<head><title>403 Forbidden</title></head>\n<body>\n"
    "<center><h1>403 Forbidden</h1></center>\n"
    "<hr><center>Microsoft-Azure-Application-Gateway/v2</center>\n</body>\n</html>"
)


class TestGatewayBlockIsToldApartFromHoval:
    """A 403 from the gateway and a 403 from Hoval need different answers.

    Hoval's API always speaks JSON, so an HTML body naming the Azure
    Application Gateway means the request was refused in front of Hoval and
    never reached them: not the account, not the password, not the network.
    v1.0.7 called this "no_plant_access" and told both issue #11 reporters to
    check their plant assignment — advice that could not have helped.
    """

    def test_signature_detection(self):
        assert _is_gateway_block(_GATEWAY_403)
        # case-insensitive: the header casing has varied across probes
        assert _is_gateway_block("server: microsoft-azure-application-gateway/v2")
        # Hoval's own errors are JSON and must never be mistaken for it
        assert not _is_gateway_block('{"detail":"No static resource foo"}')
        assert not _is_gateway_block("")

    @pytest.mark.asyncio
    async def test_data_request_flags_it(self):
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.request = MagicMock(return_value=_make_response(403, text=_GATEWAY_403))

        api = HovalConnectApi(session, "test@example.com", "pass")
        with pytest.raises(HovalApiError) as excinfo:
            await api._request("GET", "/api/my-plants")

        assert excinfo.value.gateway_blocked is True
        assert excinfo.value.status == 403

    @pytest.mark.asyncio
    async def test_hoval_own_403_is_not_flagged(self):
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.request = MagicMock(
            return_value=_make_response(403, text='{"detail":"not your plant"}')
        )

        api = HovalConnectApi(session, "test@example.com", "pass")
        with pytest.raises(HovalApiError) as excinfo:
            await api._request("GET", "/api/my-plants")

        assert excinfo.value.gateway_blocked is False
        assert excinfo.value.status == 403

    @pytest.mark.asyncio
    async def test_plant_token_fetch_flags_it_too(self):
        """The PAT fetch goes to BASE_URL, so it sits behind the same gateway."""
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.get = MagicMock(return_value=_make_response(403, text=_GATEWAY_403))

        api = HovalConnectApi(session, "test@example.com", "pass")
        with pytest.raises(HovalApiError) as excinfo:
            await api._get_plant_access_token("plant-1")

        assert excinfo.value.gateway_blocked is True

    @pytest.mark.asyncio
    async def test_token_endpoint_logs_the_body(self, caplog):
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(404, text='{"detail":"gone"}'))

        api = HovalConnectApi(session, "test@example.com", "pass")
        with caplog.at_level(logging.WARNING), pytest.raises(HovalApiError):
            await api._get_id_token()

        assert "gone" in caplog.text

    @pytest.mark.asyncio
    async def test_transport_failure_is_not_flagged(self):
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.request = MagicMock(side_effect=aiohttp.ClientError("boom"))

        api = HovalConnectApi(session, "test@example.com", "pass")
        with patch(_SLEEP, new_callable=AsyncMock), pytest.raises(HovalApiError) as excinfo:
            await api._request("GET", "/api/my-plants")
        assert excinfo.value.gateway_blocked is False


class TestUserAgentIsSentOnEveryRequest:
    """Every call must carry this client's own User-Agent.

    Home Assistant sets "HomeAssistant/<ver> aiohttp/<ver> Python/<ver>" as a
    session default for all integrations, and since ~2026-09-09 the Azure
    Application Gateway in front of the Hoval API answers anything containing
    the substring "homeassistant" with its own 403 HTML page — the request never
    reaches Hoval, which locked out every user (issue #11, two reporters).
    A per-request header is what overrides a session default in aiohttp, so the
    override has to be present on each of the three call sites, not set once.
    """

    @pytest.mark.asyncio
    async def test_data_requests_carry_it(self):
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.request = MagicMock(return_value=_make_response(200, {"ok": True}))

        api = HovalConnectApi(session, "test@example.com", "pass")
        await api._request("GET", "/api/my-plants")

        assert session.request.call_args.kwargs["headers"]["User-Agent"] == USER_AGENT

    @pytest.mark.asyncio
    async def test_id_token_request_carries_it(self):
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))

        api = HovalConnectApi(session, "test@example.com", "pass")
        await api._get_id_token()

        assert session.post.call_args.kwargs["headers"]["User-Agent"] == USER_AGENT

    @pytest.mark.asyncio
    async def test_plant_token_request_carries_it(self):
        """The PAT fetch goes to BASE_URL, so it is behind the same gateway."""
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat"}))

        api = HovalConnectApi(session, "test@example.com", "pass")
        await api._get_plant_access_token("plant-1")

        assert session.get.call_args.kwargs["headers"]["User-Agent"] == USER_AGENT

    @pytest.mark.asyncio
    async def test_it_survives_a_token_refresh(self):
        """_request rebuilds headers after a 401; the override must not be lost."""
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.request = MagicMock(
            side_effect=[_make_response(401), _make_response(200, {"ok": True})]
        )

        api = HovalConnectApi(session, "test@example.com", "pass")
        await api._get_id_token()
        await api._request("GET", "/api/my-plants")

        for call in session.request.call_args_list:
            assert call.kwargs["headers"]["User-Agent"] == USER_AGENT

    def test_it_does_not_contain_the_blocked_substring(self):
        """The whole point: "homeassistant" in any casing is what the gateway drops."""
        assert "homeassistant" not in USER_AGENT.lower()
        assert "python-requests" not in USER_AGENT.lower()

    def test_it_identifies_the_software_honestly(self):
        """Not a disguise — it names this client and where its source lives."""
        assert USER_AGENT.startswith("hoval-connect-api/")
        assert "github.com/trcyberoptic/hoval-connect-api" in USER_AGENT


class TestErrorsIdentifyTheEndpoint:
    """A failure must name the call that failed and carry its status.

    Issue #11 was reported as `API request failed: HTTP 403` — no endpoint, no
    reason. Identifying the call needed the traceback's line number plus the
    knowledge that the config flow makes exactly one request; at runtime, where
    the coordinator hits a dozen endpoints, the same message says nothing at all.
    """

    @pytest.mark.asyncio
    async def test_message_names_method_and_path(self):
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.request = MagicMock(return_value=_make_response(403, text='{"detail":"nope"}'))

        api = HovalConnectApi(session, "test@example.com", "pass")
        with pytest.raises(HovalApiError, match=r"HTTP 403 on GET /api/my-plants"):
            await api._request("GET", "/api/my-plants")

    @pytest.mark.asyncio
    async def test_status_is_attached(self):
        """Callers branch on `.status`, not on substrings of the message."""
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.request = MagicMock(return_value=_make_response(403))

        api = HovalConnectApi(session, "test@example.com", "pass")
        with pytest.raises(HovalApiError) as excinfo:
            await api._request("GET", "/api/my-plants")
        assert excinfo.value.status == 403

    @pytest.mark.asyncio
    async def test_error_body_is_logged_above_debug(self, caplog):
        """The body is where the cloud explains itself, and a bug reporter has
        no reason to have debug logging switched on."""
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.request = MagicMock(
            return_value=_make_response(403, text='{"detail":"plant not assigned"}')
        )

        api = HovalConnectApi(session, "test@example.com", "pass")
        with caplog.at_level(logging.WARNING), pytest.raises(HovalApiError):
            await api._request("GET", "/api/my-plants")

        assert "plant not assigned" in caplog.text
        assert "/api/my-plants" in caplog.text

    @pytest.mark.asyncio
    async def test_transport_failure_has_no_status(self):
        """`None` distinguishes "never got an answer" from "answered with 4xx"."""
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.request = MagicMock(side_effect=aiohttp.ClientError("boom"))

        api = HovalConnectApi(session, "test@example.com", "pass")
        with patch(_SLEEP, new_callable=AsyncMock), pytest.raises(HovalApiError) as excinfo:
            await api._request("GET", "/api/my-plants")
        assert excinfo.value.status is None

    @pytest.mark.asyncio
    async def test_token_endpoint_status_is_attached(self):
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(500))

        api = HovalConnectApi(session, "test@example.com", "pass")
        with patch(_SLEEP, new_callable=AsyncMock), pytest.raises(HovalApiError) as excinfo:
            await api._get_id_token()
        assert excinfo.value.status == 500


class TestProgramEndpointErrors:
    """Only actual program-endpoint 417s are delegated to the coordinator."""

    @pytest.mark.asyncio
    async def test_program_417_preserves_origin_without_duplicate_warning(self, caplog):
        session = _make_session()
        session.request = MagicMock(return_value=_make_response(417))
        api = HovalConnectApi(session, "test@example.com", "pass")
        api._headers = AsyncMock(return_value={})

        with caplog.at_level(logging.DEBUG), pytest.raises(HovalApiError) as excinfo:
            await api.get_programs("plant-a", "1.10.1")

        assert excinfo.value.status == 417
        assert excinfo.value.request_path == "/v3/plants/plant-a/circuits/1.10.1/programs"
        assert not excinfo.value.gateway_blocked
        session.request.assert_called_once()
        assert "HTTP 417" in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/v3/plants/plant-a/circuits"),
            ("POST", "/v3/plants/plant-a/circuits/1.10.1/programs/week1"),
        ],
    )
    async def test_417_on_other_operations_still_warns(self, caplog, method, path):
        session = _make_session()
        session.request = MagicMock(return_value=_make_response(417))
        api = HovalConnectApi(session, "test@example.com", "pass")
        api._headers = AsyncMock(return_value={})

        with caplog.at_level(logging.WARNING), pytest.raises(HovalApiError):
            await api._request(method, path)

        assert "HTTP 417" in caplog.text
        assert path in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [403, 500, 599])
    async def test_other_program_errors_still_warn_and_retry(self, caplog, status):
        session = _make_session()
        session.request = MagicMock(return_value=_make_response(status))
        api = HovalConnectApi(session, "test@example.com", "pass")
        api._headers = AsyncMock(return_value={})

        with (
            caplog.at_level(logging.WARNING),
            patch(_SLEEP, new_callable=AsyncMock),
            pytest.raises(HovalApiError) as excinfo,
        ):
            await api.get_programs("plant-a", "1.10.1")

        assert excinfo.value.status == status
        assert session.request.call_count == (3 if status >= 500 else 1)
        assert f"HTTP {status}" in caplog.text

    @pytest.mark.asyncio
    async def test_gateway_417_remains_visible(self, caplog):
        session = _make_session()
        session.request = MagicMock(
            return_value=_make_response(417, text="Microsoft-Azure-Application-Gateway/v2")
        )
        api = HovalConnectApi(session, "test@example.com", "pass")
        api._headers = AsyncMock(return_value={})

        with caplog.at_level(logging.WARNING), pytest.raises(HovalApiError) as excinfo:
            await api.get_programs("plant-a", "1.10.1")

        assert excinfo.value.gateway_blocked
        assert "HTTP 417" in caplog.text
        assert "gateway refused" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failed_step", ["authentication", "plant token fetch"])
    async def test_token_417_is_not_attributed_to_program_endpoint(self, caplog, failed_step):
        session = _make_session()
        session.post = MagicMock(
            return_value=_make_response(
                417 if failed_step == "authentication" else 200, {"id_token": "token"}
            )
        )
        session.get = MagicMock(return_value=_make_response(417))
        api = HovalConnectApi(session, "test@example.com", "pass")

        with caplog.at_level(logging.WARNING), pytest.raises(HovalApiError) as excinfo:
            await api.get_programs("plant-a", "1.10.1")

        assert excinfo.value.status == 417
        assert excinfo.value.request_path is None
        session.request.assert_not_called()
        assert f"{failed_step} failed: HTTP 417" in caplog.text


class TestPlantAccessTokenHardening:
    """The PAT fetch shares the token-endpoint retry path with the IDP login.

    It is the second of the two calls that used to run with neither a retry
    budget nor a timeout of its own, and it runs on every coordinator refresh
    once the 12-minute cache expires.
    """

    @pytest.mark.asyncio
    async def test_retries_transient_error(self):
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.get = MagicMock(
            side_effect=[_make_response(503), _make_response(200, {"token": "pat-123"})]
        )

        api = HovalConnectApi(session, "test@example.com", "pass")
        with patch(_SLEEP, new_callable=AsyncMock):
            assert await api._get_plant_access_token("plant-1") == "pat-123"
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_401_drops_the_id_token(self):
        """The ID token we just sent was rejected — replaying it only fails again."""
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.get = MagicMock(return_value=_make_response(401))

        api = HovalConnectApi(session, "test@example.com", "pass")
        with pytest.raises(HovalAuthError, match="ID token rejected"):
            await api._get_plant_access_token("plant-1")

        assert api._id_token is None
        # A rejection is permanent; it must not burn the retry budget.
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_missing_token_field_raises_api_error(self):
        """`data["token"]` used to raise KeyError straight past the coordinator's
        HovalApiError handler, landing as "Unexpected error fetching data" with
        nothing naming Hoval or the plant."""
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.get = MagicMock(return_value=_make_response(200, {"other": "field"}))

        api = HovalConnectApi(session, "test@example.com", "pass")
        with pytest.raises(HovalApiError, match="no token"):
            await api._get_plant_access_token("plant-1")

    @pytest.mark.asyncio
    async def test_bounded_by_request_timeout(self):
        session = _make_session()
        session.post = MagicMock(return_value=_make_response(200, {"id_token": "token"}))
        session.get = MagicMock(return_value=_make_response(200, {"token": "pat-123"}))

        api = HovalConnectApi(session, "test@example.com", "pass")
        await api._get_plant_access_token("plant-1")

        assert session.get.call_args.kwargs["timeout"].total == REQUEST_TIMEOUT


class TestRetryConstants:
    """Tests for retry configuration."""

    def test_retryable_status_codes(self):
        for status in (429, 500, 502, 503, 504):
            assert _is_retryable_status(status), status
        # Non-standard proxy codes from Hoval's gateway must retry too.
        for status in (598, 599, 520, 524):
            assert _is_retryable_status(status), status
        # Client errors must NOT be retried — they never fix themselves.
        for status in (400, 401, 403, 404, 422):
            assert not _is_retryable_status(status), status

    def test_max_retries_is_reasonable(self):
        assert _MAX_RETRIES >= 2
        assert _MAX_RETRIES <= 5


class TestEventEndpointNormalisation:
    """get_events/get_latest_event must normalise shape drift in the client.

    Without wrapper handling, a paginated response would reach list slicing in
    the coordinator's plant loop (outside per-circuit exception isolation) and
    fail the whole poll.
    """

    def _api_with_response(self, json_data) -> HovalConnectApi:
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)
        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)
        api_resp = _make_response(200, json_data)
        session.request = MagicMock(return_value=api_resp)
        return HovalConnectApi(session, "test@example.com", "pass")

    @pytest.mark.asyncio
    async def test_get_events_plain_list(self):
        events = [{"eventType": "warning"}, {"eventType": "info"}]
        api = self._api_with_response(events)
        assert await api.get_events("p1") == events

    @pytest.mark.asyncio
    async def test_get_events_paginated_wrapper(self):
        events = [{"eventType": "warning"}]
        api = self._api_with_response({"content": events, "last": True})
        assert await api.get_events("p1") == events

    @pytest.mark.asyncio
    async def test_get_events_wrapper_with_non_list_content(self):
        api = self._api_with_response({"content": "garbage"})
        assert await api.get_events("p1") == []

    @pytest.mark.asyncio
    async def test_get_events_non_list_returns_empty(self):
        api = self._api_with_response("garbage")
        assert await api.get_events("p1") == []

    @pytest.mark.asyncio
    async def test_get_latest_event_plain_dict_passthrough(self):
        event = {"eventType": "blocking", "description": "Fault"}
        api = self._api_with_response(event)
        assert await api.get_latest_event("p1") == event

    @pytest.mark.asyncio
    async def test_get_latest_event_wrapper_takes_first_element(self):
        event = {"eventType": "warning"}
        api = self._api_with_response({"content": [event, {"eventType": "info"}]})
        assert await api.get_latest_event("p1") == event

    @pytest.mark.asyncio
    async def test_get_latest_event_wrapper_empty_content(self):
        api = self._api_with_response({"content": []})
        assert await api.get_latest_event("p1") == {}

    @pytest.mark.asyncio
    async def test_get_latest_event_non_dict_returns_empty(self):
        api = self._api_with_response(["not", "a", "dict"])
        assert await api.get_latest_event("p1") == {}


class TestGetPlantsPageCap:
    """A server that never reports last=True must not loop forever."""

    @pytest.mark.asyncio
    async def test_endless_pagination_rejects_partial_topology(self):
        from custom_components.hoval_connect.api import _MAX_PLANT_PAGES

        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        counter = {"n": 0}

        def _endless_page(*_args, **_kwargs):
            n = counter["n"]
            counter["n"] += 1
            return _make_response(200, {"content": [{"plantExternalId": f"p{n}"}], "last": False})

        session.request = MagicMock(side_effect=_endless_page)

        api = HovalConnectApi(session, "test@example.com", "pass")
        with pytest.raises(HovalApiError, match="partial account topology"):
            await api.get_plants()

        # The page cap bounds work without publishing an incomplete account.
        assert session.request.call_count == _MAX_PLANT_PAGES

    @pytest.mark.asyncio
    async def test_cap_does_not_affect_normal_pagination(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)
        resp0 = _make_response(200, {"content": [{"plantExternalId": "p0"}], "last": False})
        resp1 = _make_response(200, {"content": [{"plantExternalId": "p1"}], "last": True})
        session.request = MagicMock(side_effect=[resp0, resp1])

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.get_plants()
        assert [p["plantExternalId"] for p in result] == ["p0", "p1"]
        assert session.request.call_count == 2


class TestReadOnlyRetryPolicy:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"])
    @pytest.mark.parametrize("failure", [429, 500, 599, "timeout", "transport"])
    async def test_only_reads_repeat_ambiguous_failures(self, method, failure):
        session = _make_session()
        if failure == "timeout":
            session.request.side_effect = TimeoutError("response lost after commit")
        elif failure == "transport":
            session.request.side_effect = aiohttp.ClientConnectionError("response lost")
        else:
            session.request.return_value = _make_response(failure)
        api = HovalConnectApi(session, "account", "password")
        api._headers = AsyncMock(return_value={})
        with patch(_SLEEP, new_callable=AsyncMock), pytest.raises(HovalApiError):
            await api._request(method, "/test")
        assert session.request.call_count == (3 if method in {"GET", "HEAD"} else 1)

    @pytest.mark.asyncio
    async def test_write_can_retry_explicit_401_rejection(self):
        session = _make_session()
        session.request.side_effect = [_make_response(401), _make_response(204)]
        api = HovalConnectApi(session, "account", "password")
        api._headers = AsyncMock(return_value={})
        assert await api._request("POST", "/test") is None
        assert session.request.call_count == 2


class TestTopologyValidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [None, 1, "oops", {}, {"content": None}, {"content": {}}])
    @pytest.mark.parametrize("endpoint", ["plants", "circuits"])
    async def test_invalid_response_is_not_an_empty_topology(self, payload, endpoint):
        api = HovalConnectApi(_make_session(), "account", "password")
        api._request = AsyncMock(return_value=payload)
        with pytest.raises(HovalApiError, match="expected a list"):
            if endpoint == "plants":
                await api.get_plants()
            else:
                await api.get_circuits("P1")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "second_page", [None, {}, {"content": {}}, [], {"content": [], "last": False}]
    )
    async def test_bad_later_page_never_returns_earlier_plants(self, second_page):
        api = HovalConnectApi(_make_session(), "account", "password")
        api._request = AsyncMock(
            side_effect=[{"content": [{"plantExternalId": "P1"}], "last": False}, second_page]
        )
        with pytest.raises(HovalApiError):
            await api.get_plants()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "plant", [None, [], {}, {"plantExternalId": []}, {"plantExternalId": "../other"}]
    )
    async def test_malformed_plant_is_not_silently_lost(self, plant):
        api = HovalConnectApi(_make_session(), "account", "password")
        api._request = AsyncMock(return_value=[{"plantExternalId": "P1"}, plant])
        with pytest.raises(HovalApiError):
            await api.get_plants()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", ["plants", "circuits"])
    @pytest.mark.parametrize("payload", [[], {"content": [], "last": True}])
    async def test_genuinely_empty_topology_is_valid(self, endpoint, payload):
        api = HovalConnectApi(_make_session(), "account", "password")
        api._request = AsyncMock(return_value=payload)
        result = await (api.get_plants() if endpoint == "plants" else api.get_circuits("P1"))
        assert result == []


class TestControlInputValidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "value", [float("nan"), float("inf"), float("-inf"), "nan", True, None, {}, "invalid"]
    )
    async def test_nonfinite_or_invalid_override_never_reaches_network(self, value):
        api = HovalConnectApi(_make_session(), "account", "password")
        api._request = AsyncMock()
        with pytest.raises(HovalApiError, match="finite number"):
            await api.set_temporary_change("P1", "1.2.3", value)
        api._request.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("program", [None, [], "../reset", "week1?other=value", "week3"])
    async def test_invalid_program_never_changes_the_request_path(self, program):
        api = HovalConnectApi(_make_session(), "account", "password")
        api._request = AsyncMock()
        with pytest.raises(HovalApiError, match="Invalid circuit program"):
            await api.set_program("P1", "1.2.3", program)
        api._request.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "plant,path",
        [("P1/other", "1.2.3"), ("P1", "1.2.3?other=true"), ("P1", ".."), ("P1", "1%2f2")],
    )
    async def test_malformed_control_identifiers_never_reach_network(self, plant, path):
        api = HovalConnectApi(_make_session(), "account", "password")
        api._request = AsyncMock()
        with pytest.raises(HovalApiError, match="Invalid"):
            await api.set_program(plant, path, "week1")
        api._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_duration_keeps_existing_end_of_phase_fallback(self):
        api = HovalConnectApi(_make_session(), "account", "password")
        api._request = AsyncMock()
        await api.set_temporary_change("P1", "1.2.3", 21.5, "legacy-unknown")
        assert api._request.call_args.kwargs["json_data"] == {"type": "endOfPhase", "value": 21.5}
