"""Tests for the Hoval Connect diagnostics module."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Mock homeassistant modules
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
sys.modules.setdefault("homeassistant.components.diagnostics", ha_mock)
sys.modules.setdefault("aiohttp", ha_mock)
sys.modules.setdefault("voluptuous", ha_mock)

from custom_components.hoval_connect.diagnostics import (  # noqa: E402
    REDACT_CONFIG,
    REDACT_COORDINATOR,
)


class TestRedactionSets:
    """Test that redaction sets cover PII fields."""

    def test_config_redacts_credentials(self):
        assert "password" in REDACT_CONFIG
        assert "email" in REDACT_CONFIG

    def test_coordinator_redacts_tokens(self):
        assert "token" in REDACT_COORDINATOR
        assert "id_token" in REDACT_COORDINATOR
        assert "plant_access_token" in REDACT_COORDINATOR

    def test_coordinator_redacts_plant_ids(self):
        assert "plant_id" in REDACT_COORDINATOR
        assert "plantExternalId" in REDACT_COORDINATOR

    def test_coordinator_redacts_pii(self):
        """Verify that names/descriptions that could identify the user are redacted."""
        assert "name" in REDACT_COORDINATOR
        assert "description" in REDACT_COORDINATOR
        assert "source_path" in REDACT_COORDINATOR
