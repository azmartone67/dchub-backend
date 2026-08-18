<!-- fingerprint:11e6cf1599588f76295684a60e536842 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — audit_H Power-plants twin divergence live: /api/energy-discovery/status publishes 13,446 from abandoned power_plants_eia wh... (observed at: dchub://audit/SH52-052). What is the root cause, and is there a single unambiguous find-and-replace fix in one file that resolves it?

IF AND ONLY IF a single mechanical fix exists, end your answer with a fenced block exactly like:
```remedy
{"file": "routes/example.py", "find": "<exact current text>", "replace": "<exact new text>"}
```
Rules for that block: `find` must be text that appears EXACTLY ONCE in that file, copied verbatim; never guess a path or a line number; never propose a change under .github/. If the fix is config, data, ops, or a judgement call — or you are not certain the find string is unique — OMIT the block entirely and say plainly why no mechanical fix applies. An omitted block is a correct and expected answer.

> Auto-captured from an **approved** brain inv item (#100177). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T03:01:03.559871Z · inv #100177_

## The approved recommendation

Approve a verification task: (1) open the energy-discovery status route source and confirm whether it reads power_plants_eia; (2) if so, decide between a minimal table-name swap to the live power-plants source vs the more durable canonical_stats-style refactor used for the state_of_power.py hardcodes. No remedy block is emitted because the handler source and an exact, unique find string were not in evidence — proposing one would violate the verbatim/uniqueness rule.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
