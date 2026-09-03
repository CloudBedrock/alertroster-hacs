Adds the one setting that lets somebody answer from their phone.

`ack_timeout_seconds` now accepts **0**, which the station reads as "no timeout — keep paging
until a person acknowledges this". It was previously refused.

That matters more than it sounds. The station treats this field as its grace period: it does not
escalate off-site until the time has run out, so nobody's phone rings while somebody might still
be walking to the panel. The consequence, with any positive value, is that the phone rings at the
exact moment the panel gives up — so an answer from a phone always arrives after Home Assistant
has already fired `alertroster_unacknowledged`. With `0` the phone rings straight away, the panel
keeps sounding, and whoever reaches it first stops it.

Use a real timeout for an alert meant to be noticed locally, with "nobody answered" as the
fallback. Use `0` for one meant to reach somebody who is not in the building. **Nothing will ever
fire `alertroster_unacknowledged` for an alert raised with `0`** — it does not expire.

A fractional value like `0.5` is now refused rather than quietly truncated to `0`.
