<!-- fingerprint:5b7c3490ec0d05caf145a0fdb79d506e -->
# Brain proposal — [reliability] Brain finding: dcpi_snapshot_history_missing @ https://dchub-backend-production.up.railway

> Auto-captured from an **approved** brain agenda item (#100109). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-17T20:15:25.816080Z · agenda #100109_

## The approved recommendation

Choose the remediation architecture: (a) transactional write-through history inside the recompute job (recommended — no new job to stall), or (b) the previously proposed separate weekly snapshot cron. Also decide the retention window/rollup policy for the history table and approve gating dcpi_snapshot_history_missing resolution on verified >7-day history depth.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
