Fixes pairing for anyone who types the code the way the station shows it.

The station displays the 8-digit pairing code grouped — `9615 5668`. Typed that way into Home
Assistant, it was rejected: the code went to the station with the space still in it, and the
station wants eight bare digits. The failure was worse than one retry, because three refused
codes close the station's pairing window — so trusting the grouping meant walking back to the
station for a fresh code.

Home Assistant now accepts the code as displayed. Spaces and hyphens between the digits are
ignored; the digits themselves still have to be right.

If pairing worked for you before, nothing here changes.
