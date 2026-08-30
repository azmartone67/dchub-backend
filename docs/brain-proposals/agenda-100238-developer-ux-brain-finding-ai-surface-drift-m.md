<!-- fingerprint:950b1271396712a6382acad706d77cb6 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:mcp_json:developer_period @ https://dchub.cloud/.well-know

> Auto-captured from an **approved** brain agenda item (#100238). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-30T21:02:38.540703Z · agenda #100238_

## The approved recommendation

Approve building a single canonical tier-metadata source with generated AI surfaces and a deploy-blocking canon gate (class fix), versus letting the existing self-heal auto-fixer continue patching each drifted field per surface (instance fixes). Also decide the semantic question: are per-day and per-month rate limits meant to differ by surface, or must all surfaces state one canonical unit?

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
