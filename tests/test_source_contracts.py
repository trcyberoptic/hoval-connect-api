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
