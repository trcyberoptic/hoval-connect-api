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
SUPPORTED_CIRCUIT_TYPES = {CIRCUIT_TYPE_HV, CIRCUIT_TYPE_HK}

# Human-readable names for circuit types
CIRCUIT_TYPE_NAMES = {
    CIRCUIT_TYPE_HV: "HomeVent",
    CIRCUIT_TYPE_HK: "Heating Circuit",
    CIRCUIT_TYPE_BL: "Boiler",
    CIRCUIT_TYPE_WW: "Hot Water",
    CIRCUIT_TYPE_FRIWA: "Fresh Water",
    CIRCUIT_TYPE_SOL: "Solar",
    CIRCUIT_TYPE_SOLB: "Solar Buffer",
    CIRCUIT_TYPE_PS: "Pool",
    CIRCUIT_TYPE_GW: "Gateway",
}

# Hoval operation modes
OPERATION_MODE_REGULAR = "REGULAR"
OPERATION_MODE_STANDBY = "standby"

# Temporary change duration options (API enum)
DURATION_FOUR_HOURS = "FOUR"
DURATION_MIDNIGHT = "MIDNIGHT"
CONF_OVERRIDE_DURATION = "override_duration"
DEFAULT_OVERRIDE_DURATION = DURATION_FOUR_HOURS

# Turn-on mode options (what happens when fan is turned on from standby)
TURN_ON_RESUME = "resume"
TURN_ON_WEEK1 = "week1"
TURN_ON_WEEK2 = "week2"
CONF_TURN_ON_MODE = "turn_on_mode"
DEFAULT_TURN_ON_MODE = TURN_ON_RESUME
