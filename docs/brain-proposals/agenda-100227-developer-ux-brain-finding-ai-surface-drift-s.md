<!-- fingerprint:f7bb4dd0a3494d141cbfa79a620eb874 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:server_card:free_tier_anon @ https://dchub.cloud/.well-kno

> Auto-captured from an **approved** brain agenda item (#100227). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-25T21:48:14.781627Z · agenda #100227_

## The approved recommendation

Choose between (a) approving a one-time engineering task: single canonical tier/pricing source + build-time generation of server-card.json/mcp.json/AGENTS.md + CI gate that blocks deploy on canon mismatch, versus (b) continuing per-field sentinel auto-fixes; and decide whether the current live values (e.g. '3 calls/day') or the canon expectations are the intended truth before any sync runs.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
