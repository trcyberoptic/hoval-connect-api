"""Home Assistant lifecycle compatibility without raising our minimum version."""

__all__ = ["HovalOptionsFlowBase", "OPTIONS_FLOW_RELOADS"]

try:
    from homeassistant.config_entries import OptionsFlowWithReload as HovalOptionsFlowBase
except ImportError:  # Home Assistant versions before OptionsFlowWithReload existed
    from homeassistant.config_entries import OptionsFlow as HovalOptionsFlowBase

    OPTIONS_FLOW_RELOADS = False
else:
    OPTIONS_FLOW_RELOADS = True
