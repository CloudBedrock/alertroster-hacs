Corrects the minimum Home Assistant version. No change to the integration itself.

v1.0.0 said it ran on **2025.1**. It does not — it fails at import there, and on 2025.2, because
`helpers.service_info.zeroconf` arrived in 2025.2 and `AddConfigEntryEntitiesCallback` in 2025.3.
Anyone below 2025.3 got a stack trace at setup rather than a clear "needs a newer Home
Assistant".

**The floor is now 2025.3**, and it is measured rather than asserted: CI runs the whole test suite
against Home Assistant 2025.3.1 on every push, so the number in `hacs.json` cannot quietly drift
away from what the code needs.

If you are on 2025.3 or newer, nothing about this release changes how the integration behaves.
