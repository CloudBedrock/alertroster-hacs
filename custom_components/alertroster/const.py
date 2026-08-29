"""Constants for the AlertRoster integration."""

DOMAIN = "alertroster"
DEFAULT_PORT = 4747
ZEROCONF_TYPE = "_alertroster-receiver._tcp.local."

# Config entry data keys. `host`, `port` and `token` come from
# `homeassistant.const`; these two have no standard spelling.
CONF_SOURCE_ID = "source_id"
CONF_STATION_NAME = "station_name"

# What the station's Pairing list calls this source (protocol §6.1). A literal
# for now; whether it should be the HA instance name instead is AHA-32.
PAIR_NAME = "Home Assistant"
PAIR_KIND = "homeassistant"

# The station closes its pairing window after this many wrong codes, so the
# error text has to change on the last one -- retrying will not help any more.
PAIRING_ATTEMPTS = 3
