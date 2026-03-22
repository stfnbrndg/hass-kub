"""Config flow for the KUB integration using Azure AD B2C OAuth2."""

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_entry_oauth2_flow

from .const import (
    B2C_AUTHORIZE_URL,
    B2C_CLIENT_ID,
    B2C_SCOPES,
    B2C_TOKEN_URL,
    CONF_WATER_STATISTICS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class KUBOAuth2Implementation(config_entry_oauth2_flow.LocalOAuth2Implementation):
    """KUB B2C OAuth2 implementation.

    Uses Azure AD B2C with PKCE. No client_secret required (public client).
    """

    def __init__(self, hass):
        """Initialize KUB OAuth2."""
        super().__init__(
            hass,
            domain=DOMAIN,
            client_id=B2C_CLIENT_ID,
            client_secret="",  # Public client, no secret
            authorize_url=B2C_AUTHORIZE_URL,
            token_url=B2C_TOKEN_URL,
        )

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Extra data for the authorize request."""
        return {
            "scope": " ".join(B2C_SCOPES),
            "response_mode": "query",
        }


class KUBConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler,
    domain=DOMAIN,
):
    """Handle a config flow for KUB via OAuth2."""

    DOMAIN = DOMAIN
    VERSION = 2

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Extra data for the authorize step."""
        return {
            "scope": " ".join(B2C_SCOPES),
            "response_mode": "query",
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step — register OAuth2 impl and start flow."""
        # Register our OAuth2 implementation if not already done
        implementations = await config_entry_oauth2_flow.async_get_implementations(
            self.hass, DOMAIN
        )
        if not implementations:
            config_entry_oauth2_flow.async_register_implementation(
                self.hass,
                KUBOAuth2Implementation(self.hass),
            )

        return await super().async_step_user(user_input)

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> FlowResult:
        """Create the config entry after OAuth2 flow completes."""
        return self.async_create_entry(title="KUB", data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        """Get the options flow for this handler."""
        return KUBOptionsFlowHandler(config_entry)


class KUBOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle KUB options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry
        self.options = dict(config_entry.options)

    async def async_step_init(self, user_input=None):
        return await self.async_step_options()

    async def async_step_options(self, user_input=None):
        if user_input is not None:
            self.options.update(user_input)
            return self.async_create_entry(title="", data=self.options)

        return self.async_show_form(
            step_id="options",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_WATER_STATISTICS,
                        default=self.config_entry.options.get(
                            CONF_WATER_STATISTICS, False
                        ),
                    ): bool,
                }
            ),
            last_step=True,
        )
