"""Constants for the AlertRoster integration."""

from collections.abc import Mapping
from types import MappingProxyType

DOMAIN = "alertroster"
DEFAULT_PORT = 4747
ZEROCONF_TYPE = "_alertroster-receiver._tcp.local."

# Config entry data keys. `host`, `port` and `token` come from
# `homeassistant.const`; these two have no standard spelling.
CONF_SOURCE_ID = "source_id"
CONF_STATION_NAME = "station_name"

# What the station's Pairing list calls this source (protocol §6.1). AHA-32
# settled this: the request sends `hass.config.location_name`, so two Home
# Assistants paired to one station are told apart in its Pairing list instead
# of both showing as "Home Assistant". This is only the fallback for an
# instance whose name is somehow empty.
PAIR_NAME_FALLBACK = "Home Assistant"
PAIR_KIND = "homeassistant"

# The station closes its pairing window after this many wrong codes, so the
# error text has to change on the last one -- retrying will not help any more.
PAIRING_ATTEMPTS = 3

# Station transition -> Home Assistant bus event (§3.4). This mapping is the
# integration's reason to exist, and one name in it is deliberately not a
# translation of the station's: `alert.expired` fires
# `alertroster_unacknowledged`, because "expired" is the station's bookkeeping
# word for a timer running out while what an automation is reacting to is that
# nobody answered. The other three keep the station's wording, which is why
# only this one needs explaining.
#
# §9 makes new station events additive, so a transition missing from here is a
# newer station talking to an older integration: `events.py` ignores it rather
# than inventing a name for it.
EVENT_TRIGGERED = f"{DOMAIN}_triggered"
EVENT_ACKNOWLEDGED = f"{DOMAIN}_acknowledged"
EVENT_RESOLVED = f"{DOMAIN}_resolved"
EVENT_UNACKNOWLEDGED = f"{DOMAIN}_unacknowledged"

BUS_EVENTS: Mapping[str, str] = MappingProxyType(
    {
        "alert.triggered": EVENT_TRIGGERED,
        "alert.acknowledged": EVENT_ACKNOWLEDGED,
        "alert.resolved": EVENT_RESOLVED,
        "alert.expired": EVENT_UNACKNOWLEDGED,
    }
)

# Keys of the event data each of those carries.
ATTR_ALERT = "alert"
ATTR_STATION = "station"
