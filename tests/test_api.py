"""Tests for the Hoval Connect API client."""

from __future__ import annotations

import asyncio
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
    _RETRYABLE_STATUS_CODES,
    HovalApiError,
    HovalAuthError,
    HovalConnectApi,
)


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
        with pytest.raises(HovalApiError, match="Connection error"):
            await api._get_id_token()

    @pytest.mark.asyncio
    async def test_get_id_token_timeout(self):
        session = _make_session()
        session.post = MagicMock(side_effect=_real_asyncio.TimeoutError())

        api = HovalConnectApi(session, "test@example.com", "password123")
        with pytest.raises(HovalApiError, match="Connection error"):
            await api._get_id_token()

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
    async def test_set_temporary_change(self):
        session = _make_session()
        auth_resp = _make_response(200, {"id_token": "token"})
        session.post = MagicMock(return_value=auth_resp)

        # PAT fetch uses session.get
        pat_resp = _make_response(200, {"token": "pat-123"})
        session.get = MagicMock(return_value=pat_resp)

        # Control call uses session.request via _request
        control_resp = _make_response(204)
        session.request = MagicMock(return_value=control_resp)

        api = HovalConnectApi(session, "test@example.com", "pass")
        result = await api.set_temporary_change("plant-1", "1.2.3", 65, "FOUR")

        assert result is None  # 204 returns None

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


class TestRetryConstants:
    """Tests for retry configuration."""

    def test_retryable_status_codes(self):
        assert 429 in _RETRYABLE_STATUS_CODES
        assert 500 in _RETRYABLE_STATUS_CODES
        assert 502 in _RETRYABLE_STATUS_CODES
        assert 503 in _RETRYABLE_STATUS_CODES
        assert 504 in _RETRYABLE_STATUS_CODES
        # 404 should NOT be retryable
        assert 404 not in _RETRYABLE_STATUS_CODES

    def test_max_retries_is_reasonable(self):
        assert _MAX_RETRIES >= 2
        assert _MAX_RETRIES <= 5
