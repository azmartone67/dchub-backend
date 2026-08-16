<!-- fingerprint:ae4b97b478f443c466c26ceb11899172 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 tool(s) tell an ANONYMOUS caller it is on a paid tier — observed from the anon seat on mcp: get_energy_prices -> caller_tier='pro' — no API key was sent; 3 tool(s) called; all tier fields seen: ['get_energy_prices:caller_tier=pro'] What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100039). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-07T15:15:35.682567Z · inv #100039_

## The approved recommendation

Choose: (A) accept that PR #2329 closed this and schedule only a verification probe (anon call to get_energy_prices asserting caller_tier != 'pro'), or (B) treat #2329 as insufficient and authorize a code audit of the shared tier-resolution/fallback path to enforce fail-closed 'anonymous' tier for un-keyed callers across all 82 tools. Also decide whether to add a permanent regression fence (automated anon-seat probe asserting no paid tier is ever returned without a validated key).

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