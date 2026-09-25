"""Tests for the Hoval Connect diagnostics module."""

from __future__ import annotations

import sys
from copy import deepcopy
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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

from custom_components.hoval_connect import diagnostics  # noqa: E402
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


@dataclass
class _Circuit:
    path: str = "1.2.3"
    name: str = "Private room"
    live_values: dict = field(default_factory=lambda: {"temperature": "21.5"})


@dataclass
class _Plant:
    plant_id: str
    name: str = "Private home"
    circuits: dict = field(default_factory=lambda: {"1.2.3": _Circuit()})
    events: list = field(default_factory=list)


@dataclass
class _Data:
    plants: dict = field(default_factory=dict)


def _redact_fields(value, keys):
    """Match HA's relevant field-redaction behavior, without importing HA."""
    if isinstance(value, dict):
        return {
            key: "**REDACTED**" if key in keys else _redact_fields(item, keys)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_fields(item, keys) for item in value]
    return value


@pytest.mark.asyncio
async def test_export_removes_plant_identifiers_everywhere_without_mutating_snapshot(monkeypatch):
    monkeypatch.setattr(diagnostics, "async_redact_data", _redact_fields)
    plant = _Plant(
        plant_id="synthetic/plant-a",
        events=[
            {
                "description": "Private event text",
                "source_path": "private-event-path",
                "url": "https://example.invalid/plants/synthetic%2Fplant-a/events",
                "url_lower": "https://example.invalid/plants/synthetic%2fplant-a/events",
                "metadata": {"synthetic/plant-a": "plant synthetic/plant-a unavailable"},
            }
        ],
    )
    data = _Data({"synthetic/plant-a": plant, "synthetic-plant-b": _Plant("synthetic-plant-b")})
    original = deepcopy(data)
    entry = SimpleNamespace(
        data={"email": "private@example.invalid", "password": "private-password"},
        runtime_data=SimpleNamespace(coordinator=SimpleNamespace(data=data)),
    )

    result = await diagnostics.async_get_config_entry_diagnostics(None, entry)

    text = repr(result)
    for sensitive in (
        "synthetic/plant-a",
        "synthetic%2Fplant-a",
        "synthetic%2fplant-a",
        "synthetic-plant-b",
        "Private home",
        "Private room",
        "Private event text",
        "private-event-path",
        "private@example.invalid",
        "private-password",
    ):
        assert sensitive not in text
    assert list(result["coordinator_data"]["plants"]) == ["plant_1", "plant_2"]
    circuit = result["coordinator_data"]["plants"]["plant_1"]["circuits"]["1.2.3"]
    assert circuit["path"] == "1.2.3"
    assert circuit["live_values"] == {"temperature": "21.5"}
    assert data == original


def test_diagnostics_handles_no_plants(monkeypatch):
    monkeypatch.setattr(diagnostics, "async_redact_data", _redact_fields)
    assert diagnostics._anonymise_coordinator_data(_Data()) == {"plants": {}}
