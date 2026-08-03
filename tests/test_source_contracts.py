"""Source contracts: pin live-value keys and port decisions that runtime tests can't reach.

These read the component source as text. Crude, but they run without
homeassistant installed and fail loudly if a port regresses a key decision.
"""

from __future__ import annotations

import pathlib

_BASE = pathlib.Path("custom_components/hoval_connect")


def _read(module_name: str) -> str:
    return (_BASE / module_name).read_text(encoding="utf-8")


class TestClimateLiveValueKeys:
    def test_current_temperature_prefers_room_temp_actual(self):
        src = _read("climate.py")
        assert '"roomTempActual"' in src
        # legacy keys stay as fallbacks
        assert '"actualTemperature"' in src

    def test_target_temperature_prefers_room_temp_target(self):
        src = _read("climate.py")
        assert '"roomTempTarget"' in src
        assert '"targetTemperature"' in src

    def test_hvac_action_uses_status_key(self):
        src = _read("climate.py")
        assert 'live_values.get("status")' in src

    def test_pending_temperature_survived_the_port(self):
        assert "_pending_temperature" in _read("climate.py")


class TestConfigFlowHardening:
    def test_validation_has_outer_timeout(self):
        src = _read("config_flow.py")
        assert "asyncio.timeout(_VALIDATION_TIMEOUT_S)" in src
        assert src.count("asyncio.timeout(_VALIDATION_TIMEOUT_S)") == 2  # user + reauth

    def test_reauth_pins_account(self):
        assert "wrong_account" in _read("config_flow.py")

    def test_wrong_account_translated_everywhere(self):
        import json

        for f in ("strings.json", "translations/en.json", "translations/de.json"):
            data = json.loads(_read(f))
            assert "wrong_account" in data["config"]["error"], f
