"""Constants for the AlertRoster integration."""

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
