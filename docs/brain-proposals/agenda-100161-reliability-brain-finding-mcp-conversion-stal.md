<!-- fingerprint:be8647a6cf695cee776ff166b29c5bc5 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: mcp_conversion_stale_critical @ mcp_upgrade_signals: 7d window (seen x2440)

> Auto-captured from an **approved** brain agenda item (#100161). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-03T19:53:35.519691Z · agenda #100161_

## The approved recommendation

Choose the scope of the fix: (A) approve a platform-wide stateful-finding redesign (fingerprint + update-in-place + auto-resolve) covering all chronic detectors including cron_silently_dead, (B) limit the redesign to mcp_conversion_stale_critical plus a denominator/lookback audit, or (C) prioritize the underlying signals→codes funnel leak first and accept continued finding noise until the ratio recovers. A and C are complementary; decide sequencing and whether both get resourced now.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
