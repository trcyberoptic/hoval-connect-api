"""Constants for the Hoval Connect integration."""

from datetime import timedelta

DOMAIN = "hoval_connect"

# API endpoints
BASE_URL = "https://azure-iot-prod.hoval.com/core"
IDP_URL = "https://akwc5scsc.accounts.ondemand.com/oauth2/token"
# Public OAuth2 client_id for the Hoval Connect mobile app (same for all users).
# Extracted from the official Android/iOS app; required by the SAP IAS identity provider.
CLIENT_ID = "991b54b2-7e67-47ef-81fe-572e21c59899"

# Token TTLs (with safety margins)
ID_TOKEN_TTL = timedelta(minutes=25)
PLANT_TOKEN_TTL = timedelta(minutes=12)

# Polling interval
DEFAULT_SCAN_INTERVAL = timedelta(seconds=60)
CONF_SCAN_INTERVAL = "scan_interval"
SCAN_INTERVAL_OPTIONS = {30: "30 seconds", 60: "60 seconds", 120: "2 minutes", 300: "5 minutes"}

# Program cache TTL — programs change rarely, no need to fetch every poll
PROGRAM_CACHE_TTL = timedelta(minutes=5)

# Plant-level cache TTLs — weather and events are slow-changing, so fetching
# them on every (default 60 s) poll wastes round-trips and risks rate limits.
# They refresh on their own cadence; the last good value is reused in between.
WEATHER_CACHE_TTL = timedelta(minutes=15)
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
#   {type: "endOfPhase" | "duration", value: <float>, duration: <minutes>|null}
# Note the `duration` field is in MINUTES, not seconds (verified empirically;
# OpenAPI declares it loosely as a double). We expose three user-facing choices
# and translate them into v4 bodies at the call site in api.py. The legacy
# string values "FOUR" and "MIDNIGHT" remain the canonical stored option
# values so existing user setups keep working without a config-entry migration.
DURATION_END_OF_PHASE = "endOfPhase"  # v4-native: ends at next program-phase boundary
DURATION_FOUR_HOURS = "FOUR"  # legacy stored value → v4 type=duration, duration=240 min
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


# --- Raw controller datapoints -------------------------------------------------
#
# `GET /api/telemetry-data/snapshots/live/{plant}?dataPoints=…` reads values
# straight off the controller. Addresses are
# `<UnitId>.<FunctionGroup>.<FunctionNumber>.<DatapointId>` — the first three
# groups are exactly the circuit path, so a fixed id per module type works for
# any unit. The Modbus *register* number is NOT the identifier: it shifts with
# the CAN unit id (HV 513 → 23540, 520 → 23631) while the DatapointId stays
# 39652. An unknown address is answered with a missing key, never an error.
#
# Only ids carrying something the circuit DTO and live-values do not already
# report are listed. Deliberately omitted because they are byte-identical to
# existing sensors: 0 (outside temp), 37602 (exhaust temp), 37600 (humidity
# actual), 40687 (humidity target), 38606 (modulation = airVolume).
CIRCUIT_DATAPOINT_IDS: dict[str, tuple[str, ...]] = {
    CIRCUIT_TYPE_HV: (
        "39652",  # Status Lüftungsregelung — the operating state, reported nowhere else
        "40650",  # Betriebswahl Lüftung
        "39600",  # Luftqualität Regulierung (LIST, but Hoval ships no labels for it)
        "40651",  # Normal-Lüftungsmodulation (setpoint for constant mode)
        "40686",  # Spar-Lüftungsmodulation (setpoint for eco mode)
        "37606",  # CO2 Abluft
        "37608",  # VOC Abluft
        "37611",  # VOC Aussenluft
    ),
}

# Datapoint 39652. Hoval's own datapoint list spells entry 5 "CoolVet" — a typo
# for CoolVent, corrected here.
HV_CONTROL_STATES: dict[str, str] = {
    "0": "off",
    "1": "normal",
    "2": "voc",
    "3": "humidity",
    "4": "frost_protection",
    "5": "coolvent",
    "6": "fault",
    "7": "summer_humidity",
    "8": "switch_off_stop",
}

# Datapoint 40650.
HV_OPERATING_SELECTIONS: dict[str, str] = {
    "0": "standby",
    "1": "week1",
    "2": "week2",
    "3": "constant",
    "4": "eco",
}

# U8 percentage datapoints are defined over 0..100; the controller reports 255
# when the sensor is not fitted at all (observed on all three CO2/VOC inputs of
# a HomeVent without an air-quality sensor).
DATAPOINT_U8_UNAVAILABLE = "255"
