# Dashboard Notify — TODO

All three original TODOs have been implemented. See follow-up notes below.

## Follow-up: Document thread boundary in README

The threading model has been implemented (TODO #1 complete) but the README does not yet document the background thread boundary. Future agents should not regress this by moving provider calls back into the AppDaemon callback thread. A note in `README.md` explaining the two-phase generation pattern would prevent this.

## Follow-up: Optional low-frequency safety reconcile timer

TODO #3 removed the 60-second `_tick` poller and replaced it with explicit boundary timers. If AppDaemon timer edge cases are ever observed in production (e.g., timers silently dropped on restart), consider adding an optional hourly reconciliation pass controlled by a `safety_reconcile_interval_s` config key (disabled by default). This was mentioned in the original plan as a fallback integrity check.

## Follow-up: Startup backfill via DETECTION_SUMMARY_STORE

The current startup backfill (`_backfill_detection_bundles`) scans on-disk `summary.json` files. The original plan noted that `DETECTION_SUMMARY_STORE` could provide structured bundle data without filesystem scanning. This lookup path was left as a future optimization — the on-disk scan is sufficient for correctness today.
