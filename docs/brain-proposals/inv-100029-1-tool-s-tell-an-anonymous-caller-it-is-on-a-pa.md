<!-- fingerprint:ae4b97b478f443c466c26ceb11899172 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 tool(s) tell an ANONYMOUS caller it is on a paid tier — observed from the anon seat on mcp: get_energy_prices -> caller_tier='pro' — no API key was sent; 3 tool(s) called; all tier fields seen: ['get_energy_prices:caller_tier=pro'] What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100029). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-06T03:25:05.785420Z · inv #100029_

## The approved recommendation

Choose: (a) treat this as verified-fixed pending a fresh anon probe confirming PR #136 is deployed and get_energy_prices no longer reports 'pro' for un-keyed callers, or (b) if the probe post-dates the deploy and still shows 'pro', authorize a regression investigation into a second tier-resolution path that #136 missed. Also decide whether to re-open the get_energy_prices zero-conversion finding for re-measurement now that anon tier gating may have changed.

## Triage — 2026-08-16 (spec-debt sweep #2) — CLOSED, exact re-file

Same condition as `docs/brain-proposals/inv-100025-1-tool-s-tell-an-anonymous-caller-it-is-on-a-pa.md`, filed earlier and STILL OPEN as
the canonical obligation. The two differ only in the live counts embedded in
the title — identical once numbers are stripped. Closing this copy does not
close the obligation: act on inv-100025-1-tool-s-tell-an-anonymous-caller-it-is-on-a-pa.md.

## Human checklist

- [x] Confirm this is still worth doing — the CONDITION is still open — this COPY is not; canonical doc is inv-100025-1-tool-s-tell-an-anonymous-caller-it-is-on-a-pa.md
- [x] Scope it to a concrete change (file(s) + approach) — scoping belongs to inv-100025-1-tool-s-tell-an-anonymous-caller-it-is-on-a-pa.md, which stays open
- [x] Implement + verify — not applicable to a duplicate — implement against inv-100025-1-tool-s-tell-an-anonymous-caller-it-is-on-a-pa.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-16 as an exact re-file of inv-100025-1-tool-s-tell-an-anonymous-caller-it-is-on-a-pa.md (spec-debt sweep #2)