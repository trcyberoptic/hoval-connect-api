"""Config flow for Hoval Connect integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HovalApiError, HovalAuthError, HovalConnectApi
from .const import (
    CONF_OVERRIDE_DURATION,
    CONF_TURN_ON_MODE,
    DEFAULT_OVERRIDE_DURATION,
    DEFAULT_TURN_ON_MODE,
    DOMAIN,
    DURATION_FOUR_HOURS,
    DURATION_MIDNIGHT,
    TURN_ON_RESUME,
    TURN_ON_WEEK1,
    TURN_ON_WEEK2,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("email"): str,
        vol.Required("password"): str,
    }
)


class HovalConnectConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hoval Connect."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        """Get the options flow handler."""
        return HovalConnectOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = HovalConnectApi(session, user_input["email"], user_input["password"])

            try:
                await api.get_plants()
            except HovalAuthError as err:
                _LOGGER.warning("Hoval auth failed: %s", err)
                errors["base"] = "invalid_auth"
            except HovalApiError as err:
                _LOGGER.error("Hoval API error during setup: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during config flow")
                errors["base"] = "unknown"
            else:
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
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth when tokens are rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = HovalConnectApi(session, user_input["email"], user_input["password"])

            try:
                await api.get_plants()
            except HovalAuthError:
                errors["base"] = "invalid_auth"
            except HovalApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during reauth")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data={
                        "email": user_input["email"],
                        "password": user_input["password"],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class HovalConnectOptionsFlow(OptionsFlow):
    """Handle options for Hoval Connect."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_duration = self.config_entry.options.get(
            CONF_OVERRIDE_DURATION, DEFAULT_OVERRIDE_DURATION
        )
        current_turn_on = self.config_entry.options.get(
            CONF_TURN_ON_MODE, DEFAULT_TURN_ON_MODE
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TURN_ON_MODE,
                        default=current_turn_on,
                    ): vol.In({
                        TURN_ON_RESUME: "Resume time program",
                        TURN_ON_WEEK1: "Activate week 1",
                        TURN_ON_WEEK2: "Activate week 2",
                    }),
                    vol.Required(
                        CONF_OVERRIDE_DURATION,
                        default=current_duration,
                    ): vol.In({
                        DURATION_FOUR_HOURS: "4 hours",
                        DURATION_MIDNIGHT: "Until midnight",
                    }),
                }
            ),
        )
