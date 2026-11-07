"""Config flow for the KUB integration using Azure AD B2C OAuth2 PKCE.

KUB's B2C only allows redirect_uris registered on their app:
  - https://www.kub.org/auth-callback
  - https://oauth.pstmn.io/v1/callback (Postman, dev use)

Since HA's OAuth2 callback URL isn't registered, we use a two-step flow:
  1. Show the user a link to KUB's B2C login page
  2. After login, KUB redirects to kub.org/auth-callback with an auth code
  3. The user copies the code from the URL bar
  4. We exchange the code for tokens via KUB's token proxy

This is similar to how many CLI tools handle OAuth2 with restricted redirect URIs.
"""

import hashlib
import logging
import secrets
from typing import Any
from base64 import urlsafe_b64encode
from urllib.parse import quote

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import (
    B2C_AUTHORIZE_URL,
    B2C_CLIENT_ID,
    B2C_SCOPES,
    B2C_TOKEN_URL,
    CONF_WATER_STATISTICS,
    DOMAIN,
)
from .kub import kub_utilities

_LOGGER = logging.getLogger(__name__)

# KUB's registered redirect URI (from their SPA source)
# Using Postman's registered callback — it displays the code cleanly
# without KUB's SPA intercepting and failing nonce validation.
# This URI is registered on KUB's B2C app registration.
KUB_REDIRECT_URI = "https://oauth.pstmn.io/v1/callback"


def _generate_pkce():
    """Generate PKCE code_verifier and code_challenge."""
    code_verifier = secrets.token_urlsafe(96)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


class KUBConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for KUB via B2C OAuth2 PKCE."""

    VERSION = 2

    def __init__(self) -> None:
        self._code_verifier: str = ""
        self._code_challenge: str = ""
        self._authorize_url: str = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        return KUBOptionsFlowHandler(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Show the user the B2C login link."""
        self._code_verifier, self._code_challenge = _generate_pkce()

        scope = quote(" ".join(B2C_SCOPES))
        state = secrets.token_urlsafe(32)
        redirect_uri = quote(KUB_REDIRECT_URI, safe="")

        self._authorize_url = (
            f"{B2C_AUTHORIZE_URL}"
            f"?client_id={B2C_CLIENT_ID}"
            f"&response_type=code"
            f"&redirect_uri={redirect_uri}"
            f"&response_mode=query"
            f"&scope={scope}"
            f"&state={state}"
            f"&code_challenge={self._code_challenge}"
            f"&code_challenge_method=S256"
        )

        return await self.async_step_auth_code()

    async def async_step_auth_code(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: User logs in at B2C, pastes the auth code."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input.get("auth_code", "").strip()
            if not code:
                errors["base"] = "no_code"
            else:
                try:
                    token_data = await self._exchange_code(code)
                    return self.async_create_entry(
                        title="KUB",
                        data={
                            "token": token_data,
                            "auth_implementation": DOMAIN,
                        },
                    )
                except KUBAuthError:
                    errors["base"] = "invalid_auth"
                except Exception:
                    _LOGGER.exception("Token exchange failed")
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="auth_code",
            data_schema=vol.Schema(
                {vol.Required("auth_code"): str}
            ),
            description_placeholders={
                "authorize_url": self._authorize_url,
            },
            errors=errors,
        )

    async def _exchange_code(self, code: str) -> dict:
        """Exchange authorization code for tokens via KUB's token proxy."""
        payload = {
            "client_id": B2C_CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": KUB_REDIRECT_URI,
            "code_verifier": self._code_verifier,
            "scope": " ".join(B2C_SCOPES),
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(B2C_TOKEN_URL, data=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    _LOGGER.error("Token exchange failed: %s %s", resp.status, body)
                    raise KUBAuthError(f"Token exchange failed: {resp.status}")
                token_data = await resp.json()

        # Validate token by fetching account info
        access_token = token_data.get("access_token", "")
        kub = kub_utilities.KubUtility(access_token)
        await kub.verify_access()

        return token_data


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


class KUBAuthError(HomeAssistantError):
    """Error during KUB authentication."""
