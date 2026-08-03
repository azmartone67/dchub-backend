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

One thing: it keeps a single deduped `qa-superuser` GitHub issue current, and
comments only when something actually changed (NEW / REGRESSED / RECOVERED /
FLAPPING). It never merges, deploys, executes a plan, or writes to `main`.

That boundary is deliberate on both sides. It does not cross the human-merge line
that has held since the autonomy core was written. But it is also not a seventh
read-only shell — the finding of shell #39 was that six months of better instruments
were pointed at an unchanged engine, and every shell before it ended *"names an
actuator per lane, fires nothing"*.

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
