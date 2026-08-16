<!-- fingerprint:ae4b97b478f443c466c26ceb11899172 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — 1 tool(s) tell an ANONYMOUS caller it is on a paid tier — observed from the anon seat on mcp: get_energy_prices -> caller_tier='pro' — no API key was sent; 3 tool(s) called; all tier fields seen: ['get_energy_prices:caller_tier=pro'] What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100037). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-07T07:45:32.411008Z · inv #100037_

## The approved recommendation

Choose the fix scope: (a) minimal patch — fail-closed caller_tier='anonymous' when no valid key, at the single tier-resolution chokepoint, plus one contract-healer invariant; or (b) that patch plus a full sweep of all 82 live tools for tool-tier→caller-tier conflation. Also decide severity: if repro shows real pro data was served to anon callers (not just a mislabel), this becomes an entitlement leak requiring immediate hotfix rather than a metrics fix.

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