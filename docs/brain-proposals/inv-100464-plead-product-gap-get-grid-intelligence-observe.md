<!-- fingerprint:241c64c9a9289dafa415bc94b3ff87dc -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — plead_product_gap:get_grid_intelligence (observed at: dchub://product-lead/gap/tool/get_grid_intelligence). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100464). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-03T08:56:05.031069Z · inv #100464_

## The approved recommendation

Pull the source of the get_grid_intelligence handler in dchub-mcp-server (server.mjs / the grid tool module) so the paywall response body can be inspected for missing claim_free_key + email_capture coaching before any code change is proposed.

## Rolled-up targets — class `plead_product_gap` (class collapse, 2026-09-26)

This doc is now the single obligation for **5 occurrences** of
`plead_product_gap`. The other 4 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `get_grid_intelligence @ dchub://product-lead/gap/tool/get_grid_intelligence` — was `inv-100464-plead-product-gap-get-grid-intelligence-observe.md` (filed 2026-09-03)
- `get_interconnection_queue @ dchub://product-lead/gap/tool/get_interconnection_queue` — was `inv-100465-plead-product-gap-get-interconnection-queue-obs.md` (filed 2026-09-03)
- `campus center hyperscale northern virginia @ dchub://product-lead/gap/intent/campus center hyperscale northern virginia` — was `inv-100509-plead-product-gap-campus-center-hyperscale-north.md` (filed 2026-09-04)
- `list_transactions @ dchub://product-lead/gap/tool/list_transactions` — was `inv-100529-plead-product-gap-list-transactions-observed-at.md` (filed 2026-09-05)
- `get_water_risk @ dchub://product-lead/gap/tool/get_water_risk` — was `inv-100627-plead-product-gap-get-water-risk-observed-at-d.md` (filed 2026-09-12)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
