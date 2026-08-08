# Brain × QA-Superuser tag-team — the self-healing operating contract

_Established 2026-08-07 after the full-platform audit (138 findings). This is
the charter for how DC Hub heals itself without a human in the fix loop. It is
the authoritative description; the code is the implementation._

## The one rule that never bends

**Never auto-exec L8.** The brain gains detection, proposal, and merge-of-a-
mechanical-class power — never the power to run arbitrary actions, touch money
movement, or bypass the gates below. Every autonomous step is reversible and
attributable.

## The loop (who does what)

```
  DETECT              PROPOSE             ACT                 VERIFY
  ┌────────────┐      ┌────────────┐      ┌──────────────┐    ┌──────────────┐
  │ QA super-  │      │ brain L4/L5│      │ auto-merge   │    │ shell #52    │
  │ user (4h)  │─┐    │ propose    │─────▶│ (mechanical  │───▶│ audit-closure│
  │ shell #52  │ ├───▶│ stage      │      │  class only, │    │ + canary +   │
  │ detectors  │ │    │ + recorder │      │  ARMED)      │    │ auto-revert  │
  │ deadman    │─┘    └────────────┘      └──────────────┘    └──────────────┘
  └────────────┘            │                    │                   │
        ▲                   │ non-code           │ breaker           │ regression
        │                   ▼ findings           ▼ on revert         ▼
        │            ┌────────────┐        ┌──────────────┐    ┌──────────────┐
        └────────────│ ROUTE to   │        │ HALT + alert │    │ re-open the  │
          escalation │ right owner│        │ operator     │    │ finding      │
                     └────────────┘        └──────────────┘    └──────────────┘
```

### DETECT — three independent detectors, always on
- **QA superuser** (every 4h) — outside-in, real-envelope probing. Finds what
  agents actually experience. Its findings are the gold standard (it caught
  the caller_tier lie the internal checks missed).
- **Shell #52 audit-closure** (daily) — machine-verifies the 138 audit
  findings; reds the board on any regression; beats the deadman ledger.
- **Deadman + triage router** (every 2h) — dead loops become per-feed
  `[deadman-triage]` work items that auto-close on green.

### PROPOSE — brain L4/L5, instrumented
- Every run records `considered / generated / outcome-histogram` to
  `brain_propose_runs`; `GET /api/v1/brain/propose-stage/status` exposes the
  three-state truth (FLOWING / JAMMED). Six zero-output runs with work
  available files a `propose_stage_jammed` finding — green-doing-nothing now
  reads BLOCKED.
- Duplicate-spec dedup consults **landed** proposals, not just open PRs — the
  6× treadmill is closed.

### ACT — mechanical auto-merge, already armed
`routes/brain_automerge.py`, `BRAIN_AUTOMERGE_ENABLED=1`, scoped to
`brain/autofix-*` branches only, rate-capped, with a post-merge canary whose
inverse search→replace auto-reverts a regression and trips a persisted breaker
that halts the lane until a human clears it.

**Mechanical class (auto-merge eligible)** — reversible, single-file,
non-semantic:
- canon-number / stale-string heal (facility counts, versions, floors)
- missing-cron / `_DISPATCH` additions caught by the scheduler class-guard
- guard/test/assertion additions (they can only make CI stricter)
- comment/docstring credential redaction (names-only)

**Gated class (draft PR → human/chip merge)** — anything touching:
- request logic, control flow, or query semantics
- money movement, pricing, tier/quota enforcement, auth
- data writes, schema, or ingestion
- the mcp-server repo (the brain cannot PR it — route as an issue instead)

### VERIFY — shell #52 is the shared scoreboard
Both detectors write findings; the brain works them down; shell #52 confirms
each closes **only when its own checker passes** (never by proxy, never by ack
while red). Closure % is the single honest number for "are we healing."

## Escalation ladder (what reaches a human)
1. A finding the brain triages as **not code-fixable** → routed to its owner:
   config/env → operator worklist; mcp-server → mcp-server issue; genuinely
   terminal → resolved/wont_fix (and it leaves the actionable count).
2. **Breaker tripped** (any auto-revert) → lane halts, operator alerted.
3. **Propose stage JAMMED** (6 zero-output runs) → `propose_stage_jammed`
   finding surfaces at the top of the backlog.
4. **A red feed persists** past its triage window → its `[deadman-triage]`
   issue stays open with an owner.

## What the operator still owns (by design, not omission)
- Merging the **gated class** (logic/money/auth/data) — via the chip mechanism
  or directly.
- The **arming decisions** (which env flags are on) and the breaker reset.
- The **BD-gated** items code can't advance (partner submissions, NLR renewal).

## Honest status at establishment (2026-08-07)
Loop verdict: **FLOWING**, throttled + triaging — not jammed. 15 of 54
findings code-fixable and draining through the rate cap; 39 triaged as
non-code and pending correct routing. The auto-merge lane is armed and idle
because the propose stage (correctly) emits slowly. The remaining work to
"fully hand off" is the finding-routing in the escalation ladder above, not
new machinery.

---

# The full remit (2026-08-08)

The owner's instruction was broader than bug-fixing: *fix bugs, improve chunks
and RAG, do graph engineering, make the site faster and more efficient — cover
the entire site — and stop making me iterate.* This section is the standing
charter for that. Everything above still holds; this widens the surface the
loop is responsible for and closes the three gaps the tag-team left open.

## What the squasher owns now

| domain | detector | who acts |
|---|---|---|
| code defects | QA super-user · shell #52 · deadman | brain L4/L5 → auto-merge (mechanical) or draft PR |
| **retrieval / RAG quality** | `probe_retrieval` recall + citation lanes | brain, via the same worklist |
| **latency / efficiency** | `probe_retrieval` latency lane (edge budget) | brain; over-budget paths are RED |
| audit backlog (138) | shell #52 registry → `brain_audit_intake` | brain, capped at 8 OPEN-RED per cycle |
| mcp-server defects | QA super-user | routed as ONE deduped issue on that repo |
| config / env | brain triage | operator worklist (never a code PR) |

## The three closures shipped 2026-08-08

1. **Finding router** (`routes/brain_finding_router.py`) — triaged findings
   leave the actionable count and go to their owner. The mirror's permanent
   "54/0 jam" self-grade was counting 39 already-decided findings as backlog.
   `GET /api/v1/brain/finding-routes` is the four-bucket truth.
2. **Audit intake** (`routes/brain_audit_intake.py`) — shell #52's OPEN-RED
   rows become brain worklist items. Verified live: closure 2.9% of 138, 8
   rows seeded, all 8 visible in `/api/v1/heal/findings`.
3. **Retrieval + latency lanes** (`tools/qa_superuser/probe_retrieval.py`) —
   the board can finally see whether search finds what exists, whether answers
   are cited, and whether a path is inside the edge's own time budget.

## Rules this remit does NOT relax

Widening the surface widens the blast radius, so the discipline tightens
rather than loosens:

- **A new domain does not get a new severity ladder.** RAG and perf findings
  are RED only against a threshold the platform declares about itself (an
  empty answer for an entity its own index returned; the CF zone's 15s route
  budget). Everything else is a GAUGE. *"Slow" and "irrelevant" are opinions
  until the platform states otherwise.*
- **Seat before verdict.** Recall is judged from the paid seat because the
  anon seat is served a trimmed set. A lane that cannot get a valid control
  reports BLIND and spends nothing.
- **Graph engineering stays PROPOSE-ONLY.** Schema, edges and ingestion are
  gated class: the brain may open a draft PR, never auto-merge one. Nothing
  in this remit moves a data write into the mechanical class.
- **Never auto-exec L8.** Unchanged, and unchangeable by this document.

## What still reaches the operator

Unchanged from the ladder above, plus: **the audit's own critical items are
not code-fixable and were never going to be** — the quota gateway consumer,
the credential rotations, the tier/paywall decisions. The loop's job is to
keep them visible and un-forgotten, not to pretend it can close them.
