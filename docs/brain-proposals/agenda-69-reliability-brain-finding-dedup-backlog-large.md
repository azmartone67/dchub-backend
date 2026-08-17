<!-- fingerprint:03966e4a5258010b6f74174e646a6778 -->
# Brain proposal — [reliability] Brain finding: dedup_backlog_large @ /api/v1/facilities/delta (value 21,904)

> Auto-captured from an **approved** brain agenda item (#69). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-09T06:28:29.663317Z · agenda #69_

## The approved recommendation

Choose the remediation order: (A) first reconcile the verified-count discrepancy (400 in canonical_stats vs 0 in facility breakdowns) to confirm whether verification flags were wiped or the queries disagree, then repair the dedup cron/merge logic in discovery_routes.py; or (B) immediately restart/force a bulk dedup run and accept the risk that a flag-semantics bug re-zeroes the results. Also decide whether to fund provider-name normalization (Equinix variants, ~2,812 unknown-provider rows) as part of this fix or defer it.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
