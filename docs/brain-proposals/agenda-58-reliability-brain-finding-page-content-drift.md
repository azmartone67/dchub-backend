# Brain proposal — [reliability] Brain finding: page_content_drift:/api/v1/brain/heartbeat @ /api/v1/brain/heartbeat (seen

> Auto-captured from an **approved** brain agenda item (#58). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-04T07:09:00.903705Z · agenda #58_

## The approved recommendation

Approve (a) moving /api/v1/brain/heartbeat and the health-endpoint class from content-hash drift monitoring to a schema/normalized-field check, and (b) implementing a global finding fingerprint + dedup/throttle layer in the Brain pipeline — versus the cheaper but non-durable alternative of a one-off suppression rule for this single endpoint. Also decide whether one manual diff of the heartbeat responses is required first to rule out a real regression before suppression goes live.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
