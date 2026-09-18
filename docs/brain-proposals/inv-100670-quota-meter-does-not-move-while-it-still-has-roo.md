<!-- fingerprint:9a0c28aba7611eb83b5a81660a58dfc5 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — Quota meter does NOT move while it still has room to — observed from the anon seat on mcp: rank_markets: call 1 remaining_full_today=2 ({'limit': 5}) then call 2 remaining_full_today=2 ({'limit': 7}) — IDENTICAL despite budget remaining What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100670). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-18T00:59:25.905053Z · inv #100670_

## The approved recommendation

Pull the mcp_tool_calls quota ledger rows for the anon seat's two consecutive rank_markets calls (limit=5 then limit=7) with timestamps and the decrement delta, and cross-read tools/qa_superuser/probe_mcp.py:632 to confirm whether remaining_full_today is meant to decrement on rank_markets at all before writing any fix.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
