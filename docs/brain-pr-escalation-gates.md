# Brain auto-PR escalation gates (spec)

Source: ChatGPT design session, 2026-07-18 — reviewing our model-relations →
brain_self_director → L22 draft-PR path. Distilled here as the implementation
spec. Principle: **auto-open a draft PR only when evidence indicates a contract
improvement, not merely a model preference.**

## Decision ladder

| State | Evidence threshold | Action |
|---|---|---|
| Observation | single evaluation | store only (model_relations_runs) |
| Proposal | one evaluator names a structural gap | agenda item (current brain_self_director behavior) |
| Candidate | multiple independent evaluations agree | queue for synthesis |
| Draft PR | all gates pass | L22 opens draft PR with tests + rationale |

Candidate → Draft PR is the ONLY automated code-gen step.

## Positive gates (ALL required)

1. **Stability**: same structural recommendation `repeat_count >= 3` across
   different runs/prompts (ideally different platforms' models — we have 7 lanes).
2. **Verdict-difference**: a measurable expected improvement (integration score,
   first-call success, contract-failure rate). No metric → stays a proposal.
3. **Deterministic evidence**: schema-validation failures, CI failures, repeated
   HTTP responses, reproducible regressions. "Feels cleaner" never escalates.
4. **Contract locality**: only OpenAPI/schemas/docs/recipes/CI/envelopes/
   orchestration metadata auto-escalate. Ranking, business logic, pricing,
   data interpretation → human approval.

## Hard blocks (any one prevents auto-PR)

- Active instability in recent evals (5xx / transport / timeout spikes — the
  evaluator may be reacting to infra, not design). Our r-429-backoff reduces
  false positives here but the block stays.
- Conflicting evaluator recommendations → surface both, never synthesize.
- Business-policy surface (pricing, auth policy, quota, permissions, licensing).
- Backwards-compat breaks (removed/renamed fields, changed semantics).
- Low-confidence provenance (one model, one run, weak evidence).
- **Negative-evidence gate**: don't escalate anything fully explained by
  transient infra, stale docs/manifests, evaluator API misuse, or a change
  already landed in the current version.

## Confidence score

```
pr_confidence = 0.30*repeatability + 0.25*evaluator_agreement
              + 0.20*deterministic_evidence + 0.15*measured_improvement
              + 0.10*locality          # auto-open only above 0.85
```

## Draft-PR contents (all required or don't open)

Problem statement · evidence · proposed change · compatibility impact · tests ·
rollback notes. Labels: contract / recipe / schema / ci / documentation /
breaking-change / human-review-required.

## Feedback loop

Every merged PR is re-evaluated on the next scheduled run; attach the result
(improved → close proposal; no measurable improvement → mark ineffective).
Closes the learning loop instead of assuming merged == beneficial.

## Implementation notes (ours)

- Stability counting: `model_relations_runs.verdict->top_structural_gap`
  normalized + counted across platforms/runs; episode semantics in
  brain_findings already give recurrence bookkeeping.
- The hard-block "active instability" signal exists: http_5xx column per run.
- First target: wire the ladder into brain_self_director._eval_findings_candidates
  (today it proposes on verdict_diff alone = the "Proposal" rung).
