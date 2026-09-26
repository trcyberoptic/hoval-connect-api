"""Home Assistant version compatibility without raising our minimum version."""

__all__ = ["DEVICE_INFO_HAS_VIA_DEVICE_ID", "HovalOptionsFlowBase", "OPTIONS_FLOW_RELOADS"]

from homeassistant.helpers.device_registry import DeviceInfo

try:
    from homeassistant.config_entries import OptionsFlowWithReload as HovalOptionsFlowBase
except ImportError:  # Home Assistant versions before OptionsFlowWithReload existed
    from homeassistant.config_entries import OptionsFlow as HovalOptionsFlowBase

    OPTIONS_FLOW_RELOADS = False
else:
    OPTIONS_FLOW_RELOADS = True

# Home Assistant 2026.8 added DeviceInfo["via_device_id"] (the parent's registry
# id); 2026.9 deprecates the via_device identifier tuple, which stops working in
# 2027.8. Older versions only know the tuple.
DEVICE_INFO_HAS_VIA_DEVICE_ID = "via_device_id" in (
    getattr(DeviceInfo, "__required_keys__", frozenset())
    | getattr(DeviceInfo, "__optional_keys__", frozenset())
)
