<!-- fingerprint:f3efb74086c4f7752628114b35df9c6f -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — plead_product_gap:list_transactions (observed at: dchub://product-lead/gap/tool/list_transactions). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100529). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-05T06:23:19.530934Z · inv #100529_

## The approved recommendation

Pull the list_transactions handler source and its most recent successful tool_call response from the MCP gateway, confirm it returns non-empty transaction rows, and if so mark the plead_product_gap:list_transactions finding as a false-positive/re-channel in the brain lane-driver worklist rather than opening a code fix.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100464-plead-product-gap-get-grid-intelligence-observe.md`, which stays
OPEN as the single obligation for `plead_product_gap`. This doc's target —
`list_transactions @ dchub://product-lead/gap/tool/list_transactions` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100464-plead-product-gap-get-grid-intelligence-observe.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100464-plead-product-gap-get-grid-intelligence-observe.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100464-plead-product-gap-get-grid-intelligence-observe.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100464-plead-product-gap-get-grid-intelligence-observe.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100464-plead-product-gap-get-grid-intelligence-observe.md (class collapse)