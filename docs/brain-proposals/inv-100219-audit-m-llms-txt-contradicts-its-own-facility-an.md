<!-- fingerprint:4c242811032c80131f9c485c45d3583a -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — audit_M llms.txt contradicts its own facility and deal counts (15,700+ vs 16,900+; 1,600+ vs 1,700+) (observed at: dchub://audit/SH52-125). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100219). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T19:13:15.455853Z · inv #100219_

## The approved recommendation

Choose the remediation path: (a) hand-edit llms.txt now with values you designate canonical (which figure — 18,373 or 18,542 — is the official verified count, and what is the official deal count?), or (b) authorize the durable fix: generate llms.txt's headline metrics from canonical_stats at build/publish time and register it with the consistency radar so cross_surface_metric_divergence covers it. No remedy block is emitted because the file's contents and path are not in evidence, two independent contradictions exist (so no single replace resolves the audit), and the correct deal figure is not measured.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
