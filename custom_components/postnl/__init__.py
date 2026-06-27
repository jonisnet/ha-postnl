"""PostNL custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests
import urllib3
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError

from .auth import AsyncConfigEntryAuth
from .const import PLATFORMS
from .coordinator import PostNLCoordinator
from .login_api import PostNLLoginAPI

_LOGGER = logging.getLogger(__name__)


@dataclass
class PostNLData:
    """Runtime data attached to a PostNL config entry."""

    auth: AsyncConfigEntryAuth
    coordinator: PostNLCoordinator
    userinfo: dict[str, Any]


type PostNLConfigEntry = ConfigEntry[PostNLData]


async def async_setup_entry(hass: HomeAssistant, entry: PostNLConfigEntry) -> bool:
    """Set up PostNL from config entry."""
    _LOGGER.debug("Setup Entry PostNL")

    auth = AsyncConfigEntryAuth(hass, entry)

    try:
        await auth.check_and_refresh_token()
    except HomeAssistantError as exception:
        raise ConfigEntryAuthFailed("Unable to authenticate with PostNL") from exception

    postnl_login_api = PostNLLoginAPI(auth.access_token)

    try:
        userinfo = await hass.async_add_executor_job(postnl_login_api.userinfo)
    except (requests.exceptions.RequestException, urllib3.exceptions.MaxRetryError) as exception:
        raise ConfigEntryNotReady("Unable to retrieve user information from PostNL.") from exception

    if "error" in userinfo:
        raise ConfigEntryNotReady("Error in retrieving user information from PostNL.")

    coordinator = PostNLCoordinator(hass, entry)
    entry.runtime_data = PostNLData(auth=auth, coordinator=coordinator, userinfo=userinfo)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PostNLConfigEntry) -> bool:
    """Unload PostNL config entry."""
    _LOGGER.debug("Unloading PostNL integration")
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
