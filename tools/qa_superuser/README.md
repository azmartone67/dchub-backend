# QA super-user — outside-in QA from the caller's seat

Every master shell in this repo reads the database. This one uses the product.

That difference is the whole point. The most expensive recurring defect class here
is one no database read can see: a surface that reports health it cannot actually
have. `/api/land-power/status` returned a hardcoded `"healthy"` for four months.
`get_backup_status` reported 9/9 feeds healthy while the newest news record was two
months in the future. A shell reported "all consistent" on top of a lane that had
silently skipped. In each case the tables were fine and the *caller* was not.

So this harness sits where the caller sits — anonymous agent, paying key, browser,
crawler — and asserts on what comes back on the wire.

## What it covers

| Surface | Seats | What it asserts |
| --- | --- | --- |
| **MCP agent seat** | anon, paid | handshake; tool count vs the server's own advertised number; what a paying caller actually receives; paid-beats-anon; the return nudge; the `execute_plan` front door; whether the quota meter measures anything |
| **Public web / paywall** | anon | every public page renders; discovery surfaces exist; **edge vs origin agreement**; AI-crawler access in the *served* robots.txt; every checkout link resolves |
| **Data honesty** | anon, paid | future-dated records anywhere a caller can see them; every feed's `health` claim cross-examined against its own published evidence |
| **Media / GEO** | public | the feed parses and carries items; nothing published with a future date; no verbatim republication |

## The four rules it enforces on itself

These are encoded in `finding.py` and enforced at construction, not documented and
hoped for.

1. **BLIND is not RED.** A probe that could not look reports *unobserved* and is
   never counted as a failure. A watcher problem must never masquerade as a dead
   platform.
2. **Every check states its own failure condition.** `red_when` is a required
   field. A check whose author cannot write that sentence does not belong in the
   critical set.
3. **No invented targets.** A dimension worth watching but lacking a threshold the
   platform itself defines is a GAUGE: it reports a number and never votes. Where a
   threshold is used it is the platform's own — a feed's declared `refresh_interval`,
   the ledger's existing 6h future-date tolerance, the server's own advertised tool
   count.
4. **Every finding records its seat and the exact field it read.** An absence proven
   with the wrong auth, or by reading `content[].text` when the thing lives in
   `structuredContent`, is not an absence.

## The must-fail control

Every run begins with a check that **must** come back red — a deliberately wrong
expectation asserted against the real transport. If it does not fire, the run does
not publish a green board: every PASS is demoted to *unobserved* and the board says
loudly that it cannot be trusted. Observed REDs survive, because a failure is still
evidence even from a suspect instrument; what gets withheld is reassurance.

This exists because a silently-empty test suite shipped twice on 2026-07-28 and left
the backend with no gate for hours, rendered as an ordinary green job.

## What it fires

Autonomously, one thing: it keeps a single deduped `qa-superuser` GitHub issue
current, comments only when something actually changed (NEW / REGRESSED /
RECOVERED / FLAPPING), and **closes an issue once the check that raised it passes
again** — verified from the same seat, on current state rather than on catching a
transition. It never merges, deploys, executes a plan, or writes to `main`.

Everything beyond that — investigating a finding, proposing a fix — happens only
when a human presses the button. See [From finding to diff](#from-finding-to-diff).

That boundary is deliberate on both sides. It does not cross the human-merge line
that has held since the autonomy core was written. But it is also not a seventh
read-only shell — the finding of shell #39 was that six months of better instruments
were pointed at an unchanged engine, and every shell before it ended *"names an
actuator per lane, fires nothing"*.

## From finding to diff

A red finding moves through three human-gated steps. Each one is a button on the
dashboard; none of them writes to `main`.

```
probe          symptom, observed from a real caller's seat
  └─ investigate    root cause, adversarially refuted
       └─ propose        a PR someone reads and merges
```

**Investigate** hands the finding (with its evidence inlined) to
`/api/v1/brain/investigate`. The result is stored against the finding, bound to
`evidence_sha`, and posted as a comment on that finding's own GitHub issue —
rewritten in place on re-investigation, not stacked. The refutation verdict leads
the comment: this investigator refutes ~70% of its own drafts, so printing only
the conclusion would present the 30% and the 70% identically.

**Propose** (`tools/qa_superuser/propose.py`) turns an investigated finding into a
PR through `brain_pr_opener` — the lane that already exists, is already
admin-gated, and already has a kill switch and a daily change budget. Four gates:

| gate | why |
| --- | --- |
| survived its own refutation | code from a knocked-down recommendation is the plausible-but-wrong fix |
| investigation is `current`, not `stale` | a fix from last week's observation fixes a different problem |
| `find` present **exactly once** in the real file | ambiguity is refused, not resolved |
| bounded blast radius | `del ≫ ins` is a stale-copy revert, not a fix |

`.github/` is off-limits outright. Every other guard here rests on a reviewer
seeing the diff; CI config is what decides how much a reviewer is shown.

**This lane declines more often than it succeeds, and that is the design.** Most
findings on this platform are not single-string fixes — they are config, data, or
a choice between two valid remedies. The clearest case arrived while it was being
built: #2228's own remedy said two opposite fixes existed and that picking wrong
would re-create the Neon stampede. An auto-fixer reading *"a no-store path is
being cached → add a bypass rule"* would have picked wrong. So the refusal reason
is surfaced verbatim in the UI — `'find' appears 3× — ambiguous` is the useful
output, not a red button.

**Nothing here merges.** The last step is always a human.

## State

Durable state lives on the **`qa-superuser-state`** branch at `state/board.json` —
off `main`, and off the backend it watches. It carries `first_seen`, `failing_since`
and a flap counter per finding, which is what makes "this regressed today" separable
from "this has been red for three weeks".

If prior state is unreadable, the run publishes the board but claims **no** deltas —
a transient API failure must not fire a full-board "everything is NEW" alarm.

## Running it

```bash
python3 -m tools.qa_superuser.run
```

```bash
QA_DRY_RUN=1 python3 -m tools.qa_superuser.run
```

Exit code is `0` even with red findings — the first real defect must not switch the
watcher off. It exits non-zero only when the must-fail control did not fire.

## Probe traffic

Every request self-identifies as **`dchub-qa-superuser/1.0`**.

★ Downstream analytics and shells must exclude it by **User-Agent**, never by
platform tag: the MCP server overwrites the platform field, so a platform-based
filter excludes nothing while appearing to work — that cost shell #38 an entire
probe run.

## Adding a check

Append a `Finding` in the relevant `probe_*.py`. Before you do, answer three
questions; if you cannot, it belongs as a GAUGE:

1. What exactly makes this RED? (it becomes `red_when`)
2. Which seat sees it, and which field? (it becomes `basis`)
3. Is the threshold one the platform defines for itself, or one you chose?

Extending coverage is cheap. Extending it *honestly* is the part that matters —
wrong evidence is worse than none, and this codebase has the scars to prove it.
