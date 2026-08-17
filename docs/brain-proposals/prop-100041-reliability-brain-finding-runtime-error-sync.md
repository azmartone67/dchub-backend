<!-- fingerprint:529c20ed06dfdd30ce3445057c15eda4 -->
# Brain proposal — [reliability] Brain finding: runtime_error:Sync log error: current transaction is aborted, commands igno

> Auto-captured from an **approved** brain prop item (#100041). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-17T20:15:36.771326Z · prop #100041_

## The approved recommendation

Approve a refactor of db_utils transaction handling (savepoint+rollback-to-savepoint discipline, autocommit sync-log channel, pool checkout/return state validation) as the systemic fix — versus continuing to patch the 91 individual occurrences or only fixing the upstream ArcGIS/DataCenterMap fetch errors. Also decide whether to bundle the separate read-only-context write bug (INSERT into discovered_platforms) into the same work item.

## Rolled-up targets — class `runtime_error` (class collapse, 2026-08-17)

This doc is now the single obligation for **4 occurrences** of
`runtime_error`. The other 3 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `[reliability] Brain finding: runtime_error:Sync log error: c` — was `prop-100041-reliability-brain-finding-runtime-error-sync.md` (filed 2026-07-17)
- `[reliability] Brain finding: runtime_error:ArcGIS error from` — was `prop-100053-reliability-brain-finding-runtime-error-arcgi.md` (filed 2026-07-17)
- `dchub://runtim` — was `agenda-100179-reliability-brain-finding-runtime-error-healt.md` (filed 2026-08-07)
- `[reliability] Brain finding: runtime_error:PG query failed, ` — was `prop-100046-reliability-brain-finding-runtime-error-pg-qu.md` (filed 2026-08-15)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
