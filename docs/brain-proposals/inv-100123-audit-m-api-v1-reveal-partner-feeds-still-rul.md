<!-- fingerprint:ba7e43747eee95a3985146f41cfc316b -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — audit_M /api/v1/reveal-* partner feeds still Rule-#3 edge-cached (known-open bypass never applied) (observed at: dchub://audit/SH52-084). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100123). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-14T06:04:51.990306Z · inv #100123_

## The approved recommendation

Decide whether to (a) open/assign an edge-config task to inspect and reorder the CF cache ruleset so the known-open bypass precedes Rule-#3 for /api/v1/reveal-* (verifying against SH52-084 afterward), and (b) arm the disarmed front-door routing guard from PR #184 so this class of edge-shadowing is caught automatically. No mechanical single-file code fix is proposed: the root cause sits in Cloudflare ruleset configuration, and no verbatim file text exists in the evidence to construct a safe, provably-unique find/replace — so the remedy block is intentionally omitted.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
