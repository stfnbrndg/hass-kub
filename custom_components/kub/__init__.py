"""The KUB integration."""

from __future__ import annotations

import logging
import time

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.start import async_at_started

from .const import B2C_CLIENT_ID, B2C_SCOPES, B2C_TOKEN_URL, DOMAIN, KUB_API, KUB_COORDINATOR
from .coordinator import KUBCoordinator
from .kub import kub_utilities

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
]

_LOGGER: logging.Logger = logging.getLogger(__package__)


async def _refresh_token(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Refresh the B2C access token using the refresh_token."""
    token_data = entry.data.get("token", {})
    refresh_token = token_data.get("refresh_token")

    if not refresh_token:
        raise ConfigEntryAuthFailed("No refresh token available")

    # Check if token is still valid
    expires_at = token_data.get("expires_at", 0)
    if time.time() < expires_at - 60:
        return token_data.get("access_token", "")

    # Refresh the token
    payload = {
        "client_id": B2C_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": " ".join(B2C_SCOPES),
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(B2C_TOKEN_URL, data=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                _LOGGER.error("Token refresh failed: %s %s", resp.status, body)
                raise ConfigEntryAuthFailed("Token refresh failed")
            new_token = await resp.json()

    # Add expires_at if not present
    if "expires_at" not in new_token and "expires_in" in new_token:
        new_token["expires_at"] = time.time() + new_token["expires_in"]

    # Update stored token
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "token": new_token}
    )

    return new_token.get("access_token", "")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up KUB from a config entry."""

    try:
        access_token = await _refresh_token(hass, entry)
    except Exception as ex:
        raise ConfigEntryAuthFailed(ex) from ex

    try:
        kub = kub_utilities.KubUtility(access_token)
        await kub.retrieve_account_info()
    except kub_utilities.KUBAuthenticationError as error:
        raise ConfigEntryAuthFailed(error) from error
    except Exception as ex:
        raise ConfigEntryNotReady(ex) from ex

    try:
        coordinator = KUBCoordinator(hass, kub, entry)
    except Exception as ex:
        raise ConfigEntryNotReady(ex) from ex

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        KUB_COORDINATOR: coordinator,
        KUB_API: kub,
    }

    async def _async_finish_startup(hass: HomeAssistant) -> None:
        await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_at_started(hass, _async_finish_startup)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Update Listener."""
    await hass.config_entries.async_reload(entry.entry_id)
