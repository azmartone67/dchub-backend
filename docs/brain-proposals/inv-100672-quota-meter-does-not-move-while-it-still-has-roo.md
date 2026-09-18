<!-- fingerprint:28d10720b77b1b43b031a86d068e11dd -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — Quota meter does NOT move while it still has room to — observed from the anon seat on mcp: get_fiber_intel: call 1 remaining_full_today=2 ({'market': 'ashburn'}) then call 2 remaining_full_today=2 ({'market': 'dallas'}) — IDENTICAL despite budget remaining What is the root cause and the smallest correct fix?

> Auto-captured from an **approved** brain inv item (#100672). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-18T08:55:10.828153Z · inv #100672_

## The approved recommendation

Open the get_fiber_intel handler in dchub_mcp_server.py (the block referenced by the 'PATCH 2' comment at scripts/wire_metro_fiber.py:96) and add a logging assertion plus a reproduction: run two sequential anon-seat get_fiber_intel calls (ashburn then dallas) and diff the persisted quota row before/after each — verify whether the decrement write executes on the same code path that populates structuredContent.remaining_full_today, and if it does not, move the decrement to execute before that field is serialized.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
