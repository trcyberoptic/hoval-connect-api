"""Constants for the Hoval Connect integration."""

from datetime import timedelta

DOMAIN = "hoval_connect"

# API endpoints
BASE_URL = "https://azure-iot-prod.hoval.com/core"
IDP_URL = "https://akwc5scsc.accounts.ondemand.com/oauth2/token"
# Public OAuth2 client_id for the Hoval Connect mobile app (same for all users).
# Extracted from the official Android/iOS app; required by the SAP IAS identity provider.
CLIENT_ID = "991b54b2-7e67-47ef-81fe-572e21c59899"

# Identify this client by name on every request.
#
# Home Assistant's shared aiohttp session sets a session-default User-Agent of
# "HomeAssistant/<ver> aiohttp/<ver> Python/<ver>" for every integration. Since
# around 2026-09-09 the Azure Application Gateway in front of BASE_URL answers
# any request whose User-Agent contains the case-insensitive substring
# "homeassistant" with its own 403 HTML page — the request never reaches Hoval,
# so every user of this integration was locked out (issue #11, two independent
# reporters). It is Hoval's gateway, not their API, and not something this code
# can fix.
#
# It looks deliberate. `python-requests` is refused too, which first read like a
# stock bot/scanner rule set catching Home Assistant by accident — but twelve
# other common automation clients all pass: Go-http-client, Wget, urllib3,
# python-httpx, PostmanRuntime, Java, Dart, node-fetch, axios, requests,
# openhab and ioBroker. A managed bot rule set would refuse most of those;
# Wget and Go-http-client are canonical entries. Two match values, one of them
# Home Assistant by name, is the shape of a hand-written WAF custom rule —
# which is also Microsoft's own documented example for Application Gateway
# (User-Agent / Contains / Lowercase transform), and custom rules run before
# managed ones. Other vendors did the same in 2026 (Risco in April, HomGar in
# July, both resolved by exactly this change). Hoval's intent is still
# inference, not measurement, but the accident reading no longer fits.
#
# Naming the software and its source is what an API client should send anyway —
# it tells Hoval more than the generic Home Assistant string did. It is a
# workaround for a gateway rule, not a fix for it: if the rule is ever widened,
# this stops working and the answer is a conversation with Hoval, not a
# less honest User-Agent.
#
# The release version. Keep in sync with manifest.json — pinned by a source
# contract in tests/test_source_contracts.py.
INTEGRATION_VERSION = "2.0.0"
# The version the User-Agent announces. Hoval admits client versions by the
# version in the User-Agent (#24), so it stays on the last version Hoval
# admitted until Hoval admits a newer one; bump it only then (owner decision
# 2026-11-20). The cloud requests of 2.0.0 are the same as those of 1.1.0
# (cloud-only entries) or fewer (hybrid entries).
USER_AGENT_VERSION = "1.1.0"
USER_AGENT = (
    f"hoval-connect-api/{USER_AGENT_VERSION} (+https://github.com/trcyberoptic/hoval-connect-api)"
)

# Token TTLs (with safety margins)
ID_TOKEN_TTL = timedelta(minutes=25)
PLANT_TOKEN_TTL = timedelta(minutes=12)

# Polling interval. Hoval blocked this integration on 2026-11-19 for polling
# too often and asked for 15 minutes on /circuits and /live-values (issue #24).
# get_scan_interval() maps any stored value not listed here, such as a legacy
# 30 or 60, to the default, so existing installs move to it without migration.
DEFAULT_SCAN_INTERVAL = timedelta(minutes=15)
CONF_SCAN_INTERVAL = "scan_interval"
SCAN_INTERVAL_OPTIONS = {900: "15 minutes", 1800: "30 minutes", 3600: "60 minutes"}

# Local Modbus TCP mode (TTE-GW gateway). "Local only" entries carry
# CONF_MODE == MODE_LOCAL and the gateway address in their data; cloud entries
# may add a gateway in their options (hybrid), stored under the CONF_MODBUS_* keys.
CONF_MODE = "mode"
MODE_LOCAL = "local"
CONF_HOST = "host"
CONF_PORT = "port"
CONF_UNIT_ID = "unit_id"
CONF_MODBUS_HOST = "modbus_host"
CONF_MODBUS_PORT = "modbus_port"
CONF_MODBUS_UNIT_ID = "modbus_unit_id"
CONF_MODBUS_SCAN_INTERVAL = "modbus_scan_interval"
CONF_MODBUS_PLANT_ID = "modbus_plant_id"
DATA_TOPOLOGY = "topology"
# Entity keys whose value comes from Modbus; they stay available when the cloud is down.
MODBUS_BACKED_KEYS = frozenset(
    {
        "outside_temperature",
        "exhaust_temperature",
        "air_volume",
        "humidity_actual",
        "humidity_target",
        "hv_constant_modulation",
        "hv_eco_modulation",
        "air_quality_guided",
        "hv_control_state",
        "hv_operating_selection",
        "hv_air_quality_control",
        "hv_co2_extract",
        "hv_voc_extract",
        "hv_voc_outside",
        "hv_exhaust_fan_target",
        "hv_maintenance_weeks_remaining",
        "hv_filter_cleaning_interval",
        "hv_active_error",
    }
)
DEFAULT_MODBUS_PORT = 502
DEFAULT_MODBUS_UNIT_ID = 1
DEFAULT_MODBUS_SCAN_INTERVAL = 30
MODBUS_SCAN_INTERVAL_OPTIONS = {
    10: "10 seconds",
    30: "30 seconds",
    60: "1 minute",
    120: "2 minutes",
}
# Discovery probes several unit ids over a slow serial-bridged gateway; bound
# the whole validation so the setup dialog cannot hang.
MODBUS_VALIDATION_TIMEOUT_S = 60

# Program cache TTL. Time programs change rarely, and the circuit details
# (temporary-change limits) refresh on the same cadence.
PROGRAM_CACHE_TTL = timedelta(hours=1)

# A 417 may mean this circuit has no programs. Recheck later rather than
# permanently excluding it or querying it on every sensor update. Must stay
# longer than PROGRAM_CACHE_TTL, or the pause saves nothing.
PROGRAM_UNAVAILABLE_RETRY_INTERVAL = timedelta(hours=6)

# Plant-level cache TTLs. Weather changes slowly and refreshes hourly. The
# events TTL is below the 15-minute polling floor, so events refresh with every
# regular poll; it only spares the extra refresh that follows a control action.
# The last good value is reused when a fetch fails.
WEATHER_CACHE_TTL = timedelta(hours=1)
EVENTS_CACHE_TTL = timedelta(minutes=3)

# HTTP request timeout (seconds)
REQUEST_TIMEOUT = 30

# Circuit types
CIRCUIT_TYPE_HV = "HV"
CIRCUIT_TYPE_HK = "HK"
CIRCUIT_TYPE_BL = "BL"
CIRCUIT_TYPE_WW = "WW"
CIRCUIT_TYPE_FRIWA = "FRIWA"
CIRCUIT_TYPE_SOL = "SOL"
CIRCUIT_TYPE_SOLB = "SOLB"
CIRCUIT_TYPE_PS = "PS"
CIRCUIT_TYPE_GW = "GW"

# Supported circuit types for this integration
SUPPORTED_CIRCUIT_TYPES = {
    CIRCUIT_TYPE_HV,
    CIRCUIT_TYPE_HK,
    CIRCUIT_TYPE_BL,
    CIRCUIT_TYPE_WW,
    CIRCUIT_TYPE_PS,
}

# Human-readable names for circuit types
CIRCUIT_TYPE_NAMES = {
    CIRCUIT_TYPE_HV: "HomeVent",
    CIRCUIT_TYPE_HK: "Heating Circuit",
    CIRCUIT_TYPE_BL: "Boiler",
    CIRCUIT_TYPE_WW: "Hot Water",
    CIRCUIT_TYPE_FRIWA: "Fresh Water",
    CIRCUIT_TYPE_SOL: "Solar",
    CIRCUIT_TYPE_SOLB: "Solar Buffer",
    CIRCUIT_TYPE_PS: "Buffer Tank",
    CIRCUIT_TYPE_GW: "Gateway",
}

# Hoval operation modes
OPERATION_MODE_REGULAR = "REGULAR"
OPERATION_MODE_STANDBY = "standby"

# FA states for heat generator. These states map to heat generator controllers of type WFA-200 (Feuerungsautomat). Other controller types may have different FA states.
# German FA state names are original values from the controller manual. English names are best guess AI translations.
BOILER_FA_STATES: dict[str, str] = {
    "0": "hp_off",
    "1": "hp_heating",
    "2": "active_cooling",
    "3": "blocked",
    "4": "hp_hot_water",
    "5": "hp_frost_protection",
    "6": "hp_temperature_too_low",
    "7": "hp_flow_too_high",
    "8": "hp_defrost",
    "9": "hp_passive_cooling",
    "11": "hd_fault",
    "12": "low_pressure_fault",
    "16": "restart_delay",
    "17": "energy_producer_lock",
    "18": "primary_pre_run_time",
    "19": "primary_post_run_time",
    "44": "mop",
    "49": "failed_defrost",
    "51": "condenser_pump_pre_run_time",
    "55": "inverter_modbus_fault",
    "72": "groundwater_frost_protection",
    "77": "compressor_limit",
}


# Temporary change duration options
#
# v4 (`POST /v4/.../temporary-change`) takes a richer body:
#   {type: "endOfPhase" | "duration", value: <float>, duration: <hours>|null}
# Note the `duration` field is in HOURS (verified live 2026-10-06; OpenAPI
# declares it loosely as a double). We expose three user-facing choices
# and translate them into v4 bodies at the call site in api.py. The legacy
# string values "FOUR" and "MIDNIGHT" remain the canonical stored option
# values so existing user setups keep working without a config-entry migration.
DURATION_END_OF_PHASE = "endOfPhase"  # v4-native: ends at next program-phase boundary
DURATION_FOUR_HOURS = "FOUR"  # legacy stored value → v4 type=duration, duration=4 (hours)
DURATION_MIDNIGHT = "MIDNIGHT"  # legacy stored value → v4 type=duration, duration=until midnight
CONF_OVERRIDE_DURATION = "override_duration"
DEFAULT_OVERRIDE_DURATION = DURATION_END_OF_PHASE  # safest default — works for HV and HK

# Turn-on mode options (what happens when fan is turned on from standby)
TURN_ON_RESUME = "resume"
TURN_ON_WEEK1 = "week1"
TURN_ON_WEEK2 = "week2"
CONF_TURN_ON_MODE = "turn_on_mode"
DEFAULT_TURN_ON_MODE = TURN_ON_RESUME

# HV (HomeVent) air-volume operating bounds, in percent.
# The cloud/firmware rejects (or undefined-behaves on) values below the device
# minimum; fan.py clamps requests into this band before sending.
# ponytail: 15 % minimum is GMH224's empirical device observation — adjust here
# if a HomeVent model with a different band shows up.
HV_AIR_VOLUME_MIN = 15
HV_AIR_VOLUME_MAX = 100


def clamp_hv_air_volume(percentage: float) -> int:
    """Clamp a requested HV air-volume percentage into the device's valid band.

    Pure helper (no HA imports) so it is directly unit-testable.
    """
    return int(max(HV_AIR_VOLUME_MIN, min(HV_AIR_VOLUME_MAX, percentage)))


# --- Status fields of the circuit list ----------------------------------------
#
# The OpenAPI spec types circuitStatus and airQuality.actualRoomAirQuality as
# plain strings. These value sets come from the Hoval Connect app (3.5.0), which
# rejects any other value as invalid; the sensors show such a value as unknown.
CIRCUIT_STATUSES = ("heating", "cooling", "active", "inactive", "ventilation")
ROOM_AIR_QUALITIES = ("poor", "medium", "good")
# circuitControlType (enum in the spec) → HA state; translation keys must be
# snake_case.
CIRCUIT_CONTROL_TYPES = {
    "heatingOnly": "heating_only",
    "coolingOnly": "cooling_only",
    "fullyAutomatic": "fully_automatic",
    "semiAutomatic": "semi_automatic",
    "manual": "manual",
    "manualExternal": "manual_external",
    "semiAutomaticExternal": "semi_automatic_external",
    "manualCoolingOnly": "manual_cooling_only",
}


# --- Sensors retired with the raw datapoints (#24) ----------------------------
#
# Until v1.0.18 these sensors read raw controller datapoints through
# `GET /api/telemetry-data/snapshots/live/{plant}`. Hoval asked on 2026-11-19
# for that endpoint to be dropped completely. No other endpoint reports their
# values, so the sensors are gone, and setup removes their leftover entities
# from the entity registry. The constant/eco modulation sensors survived: they
# now read the circuit details. The enum labels live on in git history (v1.0.18).
RETIRED_CIRCUIT_SENSOR_KEYS = frozenset(
    {
        "hv_control_state",  # 39652 Status Lüftungsregelung
        "hv_operating_selection",  # 40650 Betriebswahl, = activeProgram on the circuit
        "hv_air_quality_control",  # 39600, see the air_quality_guided binary sensor
        "hv_co2_extract",  # 37606
        "hv_voc_extract",  # 37608
        "hv_voc_outside",  # 37611
        "hk_control_state",  # 2051 Status Heizkreisregelung (#14)
    }
)


def is_retired_circuit_sensor(unique_id: str) -> bool:
    """Return True for the unique id of a circuit sensor retired in #24.

    Circuit sensor ids are `{plant_id}_{circuit_path}_{key}`. Both ids may
    contain underscores, so match the key as a suffix after an underscore.
    """
    return any(unique_id.endswith(f"_{key}") for key in RETIRED_CIRCUIT_SENSOR_KEYS)
