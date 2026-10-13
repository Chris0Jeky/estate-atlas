# Injected ledger readers: timestamp failures

The live `TrafficIndex` journal and event callbacks return rows whose `at` is a numeric Unix
timestamp. Every admitted numeric timestamp is validated before any row from that source read
is ingested. NaN, either infinity and integers outside the finite-float range fail the entire
admitted read. The affected source reports `ok: false` with a timestamp error. Its cursor does
not advance, and none of that read's new crossings, latest timestamps or ring entries are added.
Other sources may still complete. Existing cached rows retain normal time-window expiry.

A later eligible refresh retries from the last successful cursor. Once the producer repairs the
invalid row, the rejected batch is counted once, not once before the failure and again on retry.
Validation covers all pages admitted in that drain. A row beyond the refresh's `MAX_ROWS` cap
is deferred to a later read; it is not silently skipped and does not invalidate an earlier,
complete admitted prefix. The callback's dictionaries are not modified by validation.

Missing, boolean and nonnumeric ledger timestamps retain their existing skip behavior. In
particular, injected ledger callbacks must normalize ISO timestamp strings themselves. This is
different from the file adapter, which already parses ISO timestamps and rejects invalid strict
JSON or unrepresentable numeric input before producing a snapshot. The `parse_at` helper and
links callback behavior are unchanged by the ledger validation rule.

This is atomic rejection of invalid numeric timestamps within one admitted source read, not a
transaction spanning every source or protection against arbitrary failures during user-defined
callbacks. It neither repairs existing historical counts nor authenticates producer timestamps.
See [the traffic design](DESIGN.md#4-traffic-engine-trafficpy) for paging, source health, refresh
throttling and complete-day rollup contracts.
