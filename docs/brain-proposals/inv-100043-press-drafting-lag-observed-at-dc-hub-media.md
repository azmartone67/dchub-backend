<!-- fingerprint:c44e13072f072ae4bd02a623ba6bc9be -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — press_drafting_lag (observed at: /dc-hub-media). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it? If there is no mechanical fix, say so plainly and explain why.

> Auto-captured from an **approved** brain inv item (#100043). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-08T17:28:32.175841Z · inv #100043_

## The approved recommendation

Choose between: (A) treat this as operational — re-run `gh workflow run press-rss.yml` in dchub-frontend and verify the baked /dc-hub-media date advances past the DB's newest press row; (B) additionally commission an audit of the press_drafting_lag detector to rule out a 9999h sentinel/null-timestamp bug; or (C) reject both and demand fresh evidence (current DB press date vs. public page date, workflow run logs) before any action. There is no find-and-replace option to approve.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
