"""Config flow for AlertRoster (skeleton: zeroconf discovery → pairing code)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class AlertRosterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AlertRoster."""

    VERSION = 1
