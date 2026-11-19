"""Config flow for Hoval Connect integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HovalApiError, HovalAuthError, HovalConnectApi
from .compat import HovalOptionsFlowBase
from .const import (
    CONF_HOST,
    CONF_MODBUS_HOST,
    CONF_MODBUS_PLANT_ID,
    CONF_MODBUS_PORT,
    CONF_MODBUS_SCAN_INTERVAL,
    CONF_MODBUS_UNIT_ID,
    CONF_MODE,
    CONF_OVERRIDE_DURATION,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_TURN_ON_MODE,
    CONF_UNIT_ID,
    DEFAULT_MODBUS_PORT,
    DEFAULT_MODBUS_SCAN_INTERVAL,
    DEFAULT_MODBUS_UNIT_ID,
    DEFAULT_OVERRIDE_DURATION,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TURN_ON_MODE,
    DOMAIN,
    DURATION_END_OF_PHASE,
    DURATION_FOUR_HOURS,
    DURATION_MIDNIGHT,
    MODBUS_SCAN_INTERVAL_OPTIONS,
    MODBUS_VALIDATION_TIMEOUT_S,
    MODE_LOCAL,
    SCAN_INTERVAL_OPTIONS,
    TURN_ON_RESUME,
    TURN_ON_WEEK1,
    TURN_ON_WEEK2,
)
from .modbus import HovalModbusClient, HovalModbusError
from .modbus_hv import HovalModbusPoller
from .options import (
    ModbusSettings,
    get_modbus_plant_id,
    get_modbus_scan_interval,
    get_modbus_settings,
    get_override_duration,
    get_scan_interval,
    get_turn_on_mode,
    is_local_entry,
)

_LOGGER = logging.getLogger(__name__)

# Outer bound on credential validation. The per-request timeouts in the API
# client do not bound the whole get_plants() call (pagination loop, retries),
# and the config flow has no coordinator watchdog — without this a
# byte-dripping server hangs the setup dialog indefinitely.
_VALIDATION_TIMEOUT_S = 30


def _api_error_key(err: HovalApiError) -> str:
    """Map an API failure to the error the dialog should show.

    Three outcomes, because they need three different actions from the user:

    - Hoval's gateway refused the client before the request reached Hoval. Not
      the account, not the network, nothing the user can fix — the integration
      needs an update. Checked first: it also arrives as a 403, and issue #11
      showed the cost of conflating it with the case below.
    - A 403 from Hoval itself: the login worked and the account was refused the
      plant list. An authorisation answer that no amount of retrying fixes.
    - Anything else: `cannot_connect`, which is the only one where "try again
      later" is honest advice.
    """
    if err.gateway_blocked:
        return "gateway_blocked"
    if err.status == 403:
        return "no_plant_access"
    return "cannot_connect"


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("email"): str,
        vol.Required("password"): str,
    }
)


async def _modbus_error_key(host: str, port: int, unit_id: int) -> str | None:
    """Probe the gateway; return the error key to show, or None when HV units answer.

    `modbus_no_answer` when the connection opened but the requests timed out
    (what the TTE-GW does while Modbus is switched off), `modbus_cannot_connect`
    when the gateway could not be reached at all.
    """
    client = HovalModbusClient(host, port, unit_id)
    try:
        async with asyncio.timeout(MODBUS_VALIDATION_TIMEOUT_S):
            units = await HovalModbusPoller(client).discover()
    except (HovalModbusError, OSError, TimeoutError) as err:
        _LOGGER.warning("Modbus validation of %s:%s (unit %s) failed: %r", host, port, unit_id, err)
        if isinstance(err, HovalModbusError) and err.no_answer:
            return "modbus_no_answer"
        return "modbus_cannot_connect"
    finally:
        await client.close()
    return None if units else "modbus_no_units"


def _hybrid_gateway_schema(
    plants: dict[str, str], user_input: dict[str, Any] | None = None
) -> vol.Schema:
    """Gateway form of the hybrid setup; the plant selector only for several plants."""
    defaults = user_input or {}
    schema: dict[Any, Any] = {
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
        vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_MODBUS_PORT)): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
        vol.Required(
            CONF_UNIT_ID, default=defaults.get(CONF_UNIT_ID, DEFAULT_MODBUS_UNIT_ID)
        ): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Required(
            CONF_MODBUS_SCAN_INTERVAL,
            default=defaults.get(CONF_MODBUS_SCAN_INTERVAL, DEFAULT_MODBUS_SCAN_INTERVAL),
        ): vol.All(vol.Coerce(int), vol.In(MODBUS_SCAN_INTERVAL_OPTIONS)),
    }
    if len(plants) > 1:
        schema[
            vol.Required(
                CONF_MODBUS_PLANT_ID,
                default=defaults.get(CONF_MODBUS_PLANT_ID, next(iter(plants))),
            )
        ] = vol.In(plants)
    return vol.Schema(schema)


def _plant_choices(plants: Any) -> dict[str, str]:
    """Plant id -> display name from the get_plants() result (naming as in the coordinator)."""
    choices: dict[str, str] = {}
    for plant in plants if isinstance(plants, list) else []:
        plant_id = str(plant.get("plantExternalId"))
        choices[plant_id] = str(plant.get("description") or plant_id)
    return choices


def _local_unique_id(host: str, port: int, unit_id: int) -> str:
    return f"modbus:{host.lower()}:{port}:{unit_id}"


def _local_schema(
    host: str = "", port: int = DEFAULT_MODBUS_PORT, unit_id: int = DEFAULT_MODBUS_UNIT_ID
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=host): str,
            vol.Required(CONF_PORT, default=port): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(CONF_UNIT_ID, default=unit_id): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=255)
            ),
        }
    )


class HovalConnectConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hoval Connect."""

    VERSION = 1

    # Carried from the hybrid login step to the gateway step.
    _hybrid_credentials: dict[str, str]
    _hybrid_plants: dict[str, str]

    @staticmethod
    def async_get_options_flow(config_entry):
        """Get the options flow handler."""
        return HovalConnectOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Let the user choose between cloud, cloud + Modbus (hybrid) and a local gateway."""
        return self.async_show_menu(step_id="user", menu_options=["cloud", "hybrid", "local"])

    async def _async_validate_login(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> list[dict[str, Any]] | None:
        """Log in and list the plants; None, with `errors` set, when that fails.

        Shared by the cloud and the hybrid setup so both show the same errors.
        """
        session = async_get_clientsession(self.hass)
        api = HovalConnectApi(session, user_input["email"], user_input["password"])

        try:
            async with asyncio.timeout(_VALIDATION_TIMEOUT_S):
                return await api.get_plants()
        except TimeoutError:
            _LOGGER.warning("Hoval validation timed out after %d s", _VALIDATION_TIMEOUT_S)
            errors["base"] = "cannot_connect"
        except HovalAuthError as err:
            _LOGGER.warning("Hoval auth failed: %s", err)
            errors["base"] = "invalid_auth"
        except HovalApiError as err:
            _LOGGER.error("Hoval API error during setup: %s", err)
            errors["base"] = _api_error_key(err)
        return None

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the Hoval Connect cloud login."""
        errors: dict[str, str] = {}

        if (
            user_input is not None
            and await self._async_validate_login(user_input, errors) is not None
        ):
            await self.async_set_unique_id(user_input["email"].lower())
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=user_input["email"],
                data={
                    "email": user_input["email"],
                    "password": user_input["password"],
                },
            )

        return self.async_show_form(
            step_id="cloud",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_hybrid(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Cloud login of a hybrid setup; the gateway follows in `hybrid_gateway`."""
        errors: dict[str, str] = {}

        if user_input is not None:
            plants = await self._async_validate_login(user_input, errors)
            if plants is not None:
                await self.async_set_unique_id(user_input["email"].lower())
                self._abort_if_unique_id_configured()
                self._hybrid_credentials = {
                    "email": user_input["email"],
                    "password": user_input["password"],
                }
                self._hybrid_plants = _plant_choices(plants)
                return await self.async_step_hybrid_gateway()

        return self.async_show_form(
            step_id="hybrid",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_hybrid_gateway(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Gateway of a hybrid setup. The entry is a cloud entry whose options hold the gateway."""
        errors: dict[str, str] = {}
        credentials = self._hybrid_credentials
        plants = self._hybrid_plants

        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            port = int(user_input[CONF_PORT])
            unit_id = int(user_input[CONF_UNIT_ID])
            if not host:
                error: str | None = "modbus_cannot_connect"
            else:
                error = await _modbus_error_key(host, port, unit_id)
            if error is None:
                # The same keys the options flow writes, defaults included.
                options: dict[str, Any] = {
                    CONF_TURN_ON_MODE: DEFAULT_TURN_ON_MODE,
                    CONF_OVERRIDE_DURATION: DEFAULT_OVERRIDE_DURATION,
                    CONF_SCAN_INTERVAL: int(DEFAULT_SCAN_INTERVAL.total_seconds()),
                    CONF_MODBUS_HOST: host,
                    CONF_MODBUS_PORT: port,
                    CONF_MODBUS_UNIT_ID: unit_id,
                    CONF_MODBUS_SCAN_INTERVAL: int(
                        user_input.get(CONF_MODBUS_SCAN_INTERVAL, DEFAULT_MODBUS_SCAN_INTERVAL)
                    ),
                }
                if len(plants) > 1 and user_input.get(CONF_MODBUS_PLANT_ID) in plants:
                    options[CONF_MODBUS_PLANT_ID] = user_input[CONF_MODBUS_PLANT_ID]
                return self.async_create_entry(
                    title=credentials["email"], data=dict(credentials), options=options
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="hybrid_gateway",
            data_schema=_hybrid_gateway_schema(plants, user_input),
            errors=errors,
        )

    async def async_step_local(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Set up a local-only entry that talks to the TTE-GW over Modbus TCP."""
        errors: dict[str, str] = {}
        defaults = user_input or {}

        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            port = int(user_input[CONF_PORT])
            unit_id = int(user_input[CONF_UNIT_ID])
            if not host:
                errors["base"] = "modbus_cannot_connect"
            else:
                await self.async_set_unique_id(_local_unique_id(host, port, unit_id))
                self._abort_if_unique_id_configured()
                error = await _modbus_error_key(host, port, unit_id)
                if error is None:
                    return self.async_create_entry(
                        title=f"Hoval {host}",
                        data={
                            CONF_MODE: MODE_LOCAL,
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_UNIT_ID: unit_id,
                        },
                    )
                errors["base"] = error

        return self.async_show_form(
            step_id="local",
            data_schema=_local_schema(
                defaults.get(CONF_HOST, ""),
                defaults.get(CONF_PORT, DEFAULT_MODBUS_PORT),
                defaults.get(CONF_UNIT_ID, DEFAULT_MODBUS_UNIT_ID),
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the gateway address of a local entry."""
        entry = self._get_reconfigure_entry()
        if not is_local_entry(entry.data):
            return self.async_abort(reason="reconfigure_not_supported")

        errors: dict[str, str] = {}
        defaults = user_input or entry.data

        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            port = int(user_input[CONF_PORT])
            unit_id = int(user_input[CONF_UNIT_ID])
            unique_id = _local_unique_id(host, port, unit_id)
            if not host:
                errors["base"] = "modbus_cannot_connect"
            elif any(
                other.unique_id == unique_id and other.entry_id != entry.entry_id
                for other in self._async_current_entries()
            ):
                return self.async_abort(reason="already_configured")
            else:
                error = await _modbus_error_key(host, port, unit_id)
                if error is None:
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=unique_id,
                        title=f"Hoval {host}",
                        data_updates={CONF_HOST: host, CONF_PORT: port, CONF_UNIT_ID: unit_id},
                    )
                errors["base"] = error

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_local_schema(
                defaults.get(CONF_HOST, ""),
                defaults.get(CONF_PORT, DEFAULT_MODBUS_PORT),
                defaults.get(CONF_UNIT_ID, DEFAULT_MODBUS_UNIT_ID),
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauth when tokens are rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            reauth_entry = self._get_reauth_entry()
            # Pin reauth to the original account: a reauth must not silently
            # rebind the entry to a different Hoval login (unique_id is the
            # lowercased account email since the first release).
            if user_input["email"].lower() != (reauth_entry.unique_id or "").lower():
                errors["base"] = "wrong_account"
            else:
                session = async_get_clientsession(self.hass)
                api = HovalConnectApi(session, user_input["email"], user_input["password"])

                try:
                    async with asyncio.timeout(_VALIDATION_TIMEOUT_S):
                        await api.get_plants()
                except TimeoutError:
                    _LOGGER.warning(
                        "Hoval reauth validation timed out after %d s", _VALIDATION_TIMEOUT_S
                    )
                    errors["base"] = "cannot_connect"
                except HovalAuthError:
                    errors["base"] = "invalid_auth"
                except HovalApiError as err:
                    _LOGGER.error("Hoval API error during reauth: %s", err)
                    errors["base"] = _api_error_key(err)
                else:
                    # data_updates, not data: data= would replace the whole
                    # entry data and drop a hybrid entry's stored topology.
                    return self.async_update_reload_and_abort(
                        reauth_entry,
                        data_updates={
                            "email": user_input["email"],
                            "password": user_input["password"],
                        },
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


# The frontend submits dict-select keys as strings, hence the coercion.
_SCAN_INTERVAL_VALIDATOR = vol.All(vol.Coerce(int), vol.In(SCAN_INTERVAL_OPTIONS))

_TURN_ON_CHOICES = {
    TURN_ON_RESUME: "Resume time program",
    TURN_ON_WEEK1: "Activate week 1",
    TURN_ON_WEEK2: "Activate week 2",
}


class HovalConnectOptionsFlow(HovalOptionsFlowBase):
    """Handle options for Hoval Connect."""

    def _plant_choices(self) -> dict[str, str]:
        """Plants of the loaded cloud entry, or {} when they are not available."""
        try:
            plants = self.config_entry.runtime_data.coordinator.data.plants
            return {str(pid): str(plant.name) for pid, plant in plants.items()}
        except (AttributeError, TypeError):
            return {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if is_local_entry(self.config_entry.data):
            return await self._async_step_local_options(user_input)
        return await self._async_step_cloud_options(user_input)

    async def _async_step_local_options(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TURN_ON_MODE, default=get_turn_on_mode(options)): vol.In(
                        _TURN_ON_CHOICES
                    ),
                    vol.Required(
                        CONF_MODBUS_SCAN_INTERVAL, default=get_modbus_scan_interval(options)
                    ): vol.All(vol.Coerce(int), vol.In(MODBUS_SCAN_INTERVAL_OPTIONS)),
                }
            ),
        )

    async def _async_step_cloud_options(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        plants = self._plant_choices()
        show_plants = len(plants) > 1
        errors: dict[str, str] = {}
        stored: Any = self.config_entry.options

        if user_input is not None:
            data = dict(user_input)
            host = str(data.get(CONF_MODBUS_HOST) or "").strip()
            data[CONF_MODBUS_HOST] = host
            # Take a plant from the form only when the selector was shown;
            # otherwise (single plant, or entry not loaded) keep the stored
            # binding so saving cannot silently unbind Modbus.
            if not show_plants:
                data.pop(CONF_MODBUS_PLANT_ID, None)
                kept = get_modbus_plant_id(self.config_entry.options)
                if kept:
                    data[CONF_MODBUS_PLANT_ID] = kept
            if not host:
                return self.async_create_entry(title="", data=data)
            port = int(data.get(CONF_MODBUS_PORT, DEFAULT_MODBUS_PORT))
            unit_id = int(data.get(CONF_MODBUS_UNIT_ID, DEFAULT_MODBUS_UNIT_ID))
            # Probe only a changed address. The probe opens a second connection
            # next to the running entry's: with the gateway briefly offline, or
            # if it accepts one client only, unrelated options could not be saved.
            if ModbusSettings(host, port, unit_id) == get_modbus_settings(
                {}, self.config_entry.options
            ):
                return self.async_create_entry(title="", data=data)
            error = await _modbus_error_key(host, port, unit_id)
            if error is None:
                return self.async_create_entry(title="", data=data)
            errors["base"] = error
            stored = data

        modbus = get_modbus_settings({}, stored)
        schema: dict[Any, Any] = {
            vol.Required(CONF_TURN_ON_MODE, default=get_turn_on_mode(stored)): vol.In(
                _TURN_ON_CHOICES
            ),
            vol.Required(CONF_OVERRIDE_DURATION, default=get_override_duration(stored)): vol.In(
                {
                    DURATION_END_OF_PHASE: "Until end of current phase",
                    DURATION_FOUR_HOURS: "4 hours",
                    DURATION_MIDNIGHT: "Until midnight",
                }
            ),
            vol.Required(CONF_SCAN_INTERVAL, default=get_scan_interval(stored)): (
                _SCAN_INTERVAL_VALIDATOR
            ),
            # suggested_value, not default: clearing the field must disable Modbus.
            vol.Optional(
                CONF_MODBUS_HOST,
                description={"suggested_value": modbus.host if modbus else ""},
            ): str,
            vol.Required(
                CONF_MODBUS_PORT, default=modbus.port if modbus else DEFAULT_MODBUS_PORT
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Required(
                CONF_MODBUS_UNIT_ID, default=modbus.unit_id if modbus else DEFAULT_MODBUS_UNIT_ID
            ): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
            vol.Required(
                CONF_MODBUS_SCAN_INTERVAL, default=get_modbus_scan_interval(stored)
            ): vol.All(vol.Coerce(int), vol.In(MODBUS_SCAN_INTERVAL_OPTIONS)),
        }
        if show_plants:
            chosen = get_modbus_plant_id(stored)
            schema[
                vol.Required(
                    CONF_MODBUS_PLANT_ID,
                    default=chosen if chosen in plants else next(iter(plants)),
                )
            ] = vol.In(plants)
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema), errors=errors)
