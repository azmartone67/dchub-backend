"""lane_triage.py — which shell lanes a CODE FIX can close, and which it cannot.

WHY THIS EXISTS
===============
On 2026-09-02 the dead-man board carried 10 red `*-shell-daily` feeds. Read
lane by lane, most of them were not defects:

  loop_control/agent_identity   asserts "no one caller is >40pct of real
                                calls". chain-hire is 66.8%. The lane is
                                CORRECT and no PR can clear it — the fix is
                                demand.
  agent_pay/demand              asserts "a REAL agent has ever asked to pay".
                                None has. Also correct, also not code.
  loop_control/counter_canon    fails on a GREP COUNT of files matching
                                /DISTINCT\\s+agent_id/, and its own note says
                                "a grep hit is not proof two counters
                                DISAGREE". It measures the wrong thing.
  loop_flywheel/cron            asserts "dead-man board clear" — it reads the
                                aggregate board, so it is red whenever ANY
                                shell is red, INCLUDING ITSELF. It cannot
                                clear until everything else already has.

★ A board where most red is structurally unclearable trains everyone to scroll
past all of it — which is how `failover-canary` sat red while the Render DR
mirror drifted 112 commits behind. Splitting "someone should fix this" from
"this is the business" is what makes the remaining red mean something.

THE VOCABULARY IS NOT NEW — IT IS PROMOTED
==========================================
`audit_closure_master_shell.DEFERRED` has classified 79 findings as
build/commercial/judgment/owner-flag/diagnose since 2026-08. It was a bare
string in a dict literal: nothing validated it, nothing else could reuse it,
and it was applied to FINDINGS but never to LANES. This module promotes it to
a named, validated constant and applies it to lanes, and
tests/test_lane_triage.py bonds the two so they cannot drift apart.

★ The distinction is already house doctrine in at least three places, each
invented locally and never generalised:
  · agent_pay/_lane_rail_health — "Keeps 'nobody paid' from being misread as
    'the rail is broken' — the error that cost two and a half weeks. Settle
    FAILURES are a bug signal; abandonment is a demand signal. They must
    never share a number."
  · growth_funnel/_lane_distribution — check `dist_owner`: "this lane is
    honest about who can close it".
  · webmcp `_LANES` — each tuple's 4th element IS the actuator.

CLASSIFY BY ACTUATOR — WHO CLOSES IT
====================================
Not by severity, not by how bad the number is. The only question is who acts.
"""
from __future__ import annotations

# class → who closes a red in this class. `instrument` is the ONE addition to
# the audit_closure vocabulary; it is called out rather than slipped in.
LANE_CLASSES: dict[str, str] = {
    "build":      "an engineer changes code — this is a defect",
    "commercial": "the business changes (customers, demand, pricing, BD). "
                  "No PR closes it; red here belongs on a growth dashboard, "
                  "not a health board",
    "judgment":   "a human decides something; the code is waiting on the call",
    "owner-flag": "the owner acts OUTSIDE the codebase — a vendor dashboard, "
                  "a secret, an infra toggle. Verified live 2026-09-02: the "
                  "failover mirror was closed by a Render auto-deploy flip, "
                  "not by a commit",
    "diagnose":   "nobody can act yet — it needs investigation before it can "
                  "be classified as anything else",
    # ── the one extension ────────────────────────────────────────────
    "instrument": "the CHECK is wrong, not the system. Fix the measurement. "
                  "★ Added 2026-09-02 because conflating 'the system is "
                  "broken' with 'the measurement is broken' is the exact "
                  "error that sent this session hunting a JP grid URL that "
                  "was never broken (the file simply was not published yet)",
}

# Only a class whose red an ENGINEER can clear is code-actionable.
CODE_ACTIONABLE = frozenset({"build", "instrument"})

# (shell module stem, lane id) → (class, why)
# `why` is the evidence, not a restatement of the class.
LANE_TRIAGE: dict[tuple[str, str], tuple[str, str]] = {
    # ── agent_pay ────────────────────────────────────────────────────
    ("agent_pay", "demand"): ("commercial",
        "asserts a REAL agent has ever asked to pay, ever settled, and paid "
        "in 30d. None has. Pure demand — no code closes it"),
    ("agent_pay", "reachability"): ("build",
        "gated preview must carry a live challenge, the offer must need no "
        "magic flag, pre-wall/under-cap offers must reach granted calls — "
        "all plumbing"),
    ("agent_pay", "pricing"): ("build",
        "the $0.10 flagship list price is unsettleable under Stripe's $0.50 "
        "floor; a real coherence defect"),
    ("agent_pay", "rail_health"): ("build",
        "the lane exists to separate settle FAILURES (bug) from abandonment "
        "(demand); its failing check is the bug half"),
    ("agent_pay", "metric_integrity"): ("build",
        "regression guard pinning the synthetic-traffic filter"),

    # ── agentic_loop ─────────────────────────────────────────────────
    ("agentic_loop", "graduation"): ("build",
        "action-class registry and grant-gate mechanics"),
    ("agentic_loop", "human_queues"): ("judgment",
        "asserts every human queue has an age, a ceiling and a one-click "
        "decision — the queues are waiting on humans to decide"),
    ("agentic_loop", "learn"): ("build",
        "corpus registration, embedding freshness and recall self-test"),
    ("agentic_loop", "detectors_with_fix"): ("build",
        "measures the detector merge rule that #3054 shipped"),

    # ── context_integrity ────────────────────────────────────────────
    ("context_integrity", "envelope"): ("build",
        "counts bare-{} internal fetchers still to migrate in routes/ — a "
        "code migration with a finite end"),
    ("context_integrity", "lessons"): ("build",
        "lesson composition and blindness-share mechanics"),
    ("context_integrity", "retire"): ("build",
        "report-only inventory of _proposed_ drafts and overlapping shells"),
    ("context_integrity", "loop_edges"): ("build",
        "every non-source loop must declare an input"),

    # ── growth_funnel ────────────────────────────────────────────────
    ("growth_funnel", "attribution"): ("diagnose",
        "new-agent attribution coverage. Whether this is fixable at all is "
        "unsettled — rotating egress IPs make per-agent attribution "
        "structurally hard; investigate before classifying"),
    ("growth_funnel", "front_door"): ("commercial",
        "asserts the front-door nudge CONVERTS; conversion is demand"),
    ("growth_funnel", "distribution"): ("commercial",
        "channel listing reach. The lane already carries a `dist_owner` "
        "check that is 'honest about who can close it' — not engineering"),
    ("growth_funnel", "compounding"): ("commercial",
        "asserts returning agents compound with acquisition — retention"),

    # ── loop_control ─────────────────────────────────────────────────
    ("loop_control", "cron_liveness"): ("build",
        "asserts no cron is past its stale threshold, using the stamped "
        "interval or the declared one. This is the lane that the 'seen "
        "x477455' misread hid a real outage behind — a code defect"),
    ("loop_control", "count_semantics"): ("build",
        "value-not-count classifier and findings dedup"),
    ("loop_control", "triage_wired"): ("build",
        "approvals must reach a durable actuator, not journal"),
    ("loop_control", "surface_canon"): ("build",
        "every AI surface must quote ONE facility count within the pinned band"),
    ("loop_control", "writer_discipline"): ("build",
        "one canonical writer plus UNIQUE(issue,url) on brain_findings"),
    ("loop_control", "agent_identity"): ("commercial",
        "asserts no one caller is >40pct of real calls; chain-hire is 66.8%. "
        "The lane is correct and no PR clears it — the fix is demand"),
    ("loop_control", "counter_canon"): ("instrument",
        "fails on a GREP COUNT of files matching /DISTINCT agent_id/, and its "
        "own note concedes 'a grep hit is not proof two counters DISAGREE'. "
        "It should compare the VALUES the surfaces publish"),
    ("loop_control", "relay_two_artifact"): ("build",
        "actuator is stated in the lane: mint TWO artifacts, agent-redeemable "
        "and human-openable"),

    # ── loop_flywheel ────────────────────────────────────────────────
    ("loop_flywheel", "infra"): ("owner-flag",
        "Neon migration schedule, read-replica provisioning, role split — "
        "infra configuration, not a commit"),
    ("loop_flywheel", "edge"): ("build",
        "admin prefixes must document a no-cache policy and admin responses "
        "must declare no-store; CF Rule #3 caches /api/v1/* with "
        "mode:override_origin, which ignores no-store, so this is edge "
        "configuration an engineer owns"),
    ("loop_flywheel", "failover"): ("owner-flag",
        "the Render mirror. Proven 2026-09-02: closed by flipping Render "
        "auto-deploy, not by code"),
    ("loop_flywheel", "identity"): ("build",
        "reused claims must re-stamp the live session"),
    ("loop_flywheel", "rag"): ("build",
        "corpus registration and cosine gates for the live provider"),
    ("loop_flywheel", "mcp"): ("build",
        "live tool manifest served and its source of truth documented"),
    ("loop_flywheel", "ai_doors"): ("commercial",
        "asserts owed doors carry REAL agent calls — distribution demand"),
    ("loop_flywheel", "inventory"): ("build",
        "report-only counts plus discovery-queue accrual"),
    ("loop_flywheel", "cron"): ("instrument",
        "asserts 'dead-man board clear', i.e. it reads the AGGREGATE board. "
        "It is red whenever any shell is red, including itself, so it cannot "
        "clear until everything else already has. Circular by construction"),

    # ── relay_closure ────────────────────────────────────────────────
    ("relay_closure", "A/redeem_declared_vs_writer"): ("build",
        "declared redeem basis vs writer liveness — a consistency check"),
    ("relay_closure", "B/relay_demand_verdict"): ("commercial",
        "counts REAL external relay opens. Demand, and the shell states its "
        "own actuators are 'NONE — read-only'"),
    ("relay_closure", "C/mint_attributability"): ("diagnose",
        "whether minted claims can be attributed to a platform at all; the "
        "shell deliberately does NOT fire this lane's actuator"),
    ("relay_closure", "D/typed_params_window"): ("commercial",
        "execute_plan planner-selection share. audit_closure already "
        "classified the same fact (SH52-011, adoption 0/242 episodes) as "
        "commercial — this agrees with it rather than re-deciding"),
    ("relay_closure", "E/schema_selection_asks"): ("build",
        "the lane names its own actuator: 'tool description strings in "
        "dchub-mcp-server — NAMED, not fired here'"),

    # ── webmcp ───────────────────────────────────────────────────────
    ("webmcp", "attribution"): ("build",
        "the ai_tracking webmcp classifier must be wired"),
    ("webmcp", "headers"): ("build",
        "Origin-Trial header serving via _headers / _webmcp_enable + CF purge"),
    ("webmcp", "token"): ("owner-flag",
        "the lane's own actuator: 're-register trial at "
        "developer.chrome.com/origintrials + rotate env'. A vendor console"),
    ("webmcp", "drift"): ("build",
        "BOUND_API_PATHS and the js bindings must be updated together"),
}

# audit_closure is DELIBERATELY absent: it is a meta-shell whose lanes
# aggregate findings that already carry their own class in DEFERRED.
# Classifying its lanes would class the same work twice, at two grains, and
# the two copies would drift. Read DEFERRED for those.
UNCLASSIFIED_SHELLS: dict[str, str] = {
    "audit_closure": "meta-shell; its lanes aggregate findings that already "
                     "carry a class in audit_closure_master_shell.DEFERRED",
}


def classify(shell: str, lane: str) -> tuple[str, str] | None:
    """(class, why) for one lane, or None when it has never been classified.

    None is NOT 'build'. An unclassified lane is unknown, and calling it a
    defect is the guess this module exists to stop."""
    return LANE_TRIAGE.get((shell, lane))


def is_code_actionable(shell: str, lane: str) -> bool | None:
    """True/False, or None when unclassified — never a silent False."""
    hit = classify(shell, lane)
    return None if hit is None else hit[0] in CODE_ACTIONABLE


def split_lanes(lanes):
    """Partition (shell, lane) pairs into what an engineer can clear and what
    they cannot. Returns {code_actionable, not_code, unclassified}, each a
    list of (shell, lane, klass, why) — unclassified carries klass None."""
    out = {"code_actionable": [], "not_code": [], "unclassified": []}
    for shell, lane in lanes or ():
        hit = classify(shell, lane)
        if hit is None:
            out["unclassified"].append((shell, lane, None, None))
        elif hit[0] in CODE_ACTIONABLE:
            out["code_actionable"].append((shell, lane, hit[0], hit[1]))
        else:
            out["not_code"].append((shell, lane, hit[0], hit[1]))
    return out


# ── reading the board ─────────────────────────────────────────────────
# The dead-man ledger stores each shell's lane verdicts as FREE TEXT in
# `note`. Three formats are in use and three shells use none of them:
#
#   A  "lanes: demand=FAIL reachability=FAIL pricing=PASS ..."   agent_pay
#   B  "closure 59/138 (42.8%) · p0_incidents=FAIL secrets=PASS" audit_closure
#   C  "lanes: 5 | reds: B/relay_demand_verdict,C/mint_..."      relay_closure
#   -  "3 failing / 1 unknown of 4 lanes"                        growth_funnel
#   -  "lanes 2/4 pass"                                          webmcp
#   -  "PASS 2 FAIL 2 ? 0 | filed 0 | rate 0.602"                agentic_loop
#
# ★ The last three COUNT their failures without NAMING them, so nothing —
# not this module, not a human — can triage those from the board. That is a
# real gap and it is reported as `lanes_named: false`, never as "no failing
# lanes". A count is not a name, and an unparseable note is not an empty one.
import re as _re

_FAIL_TOKEN = _re.compile(r"([A-Za-z0-9_]+)=FAIL")
_REDS_LIST = _re.compile(r"reds:\s*([^|]+)")
_FEED_SUFFIX = "-shell-daily"


def feed_to_shell(feed: str) -> str | None:
    """'agent-pay-shell-daily' -> 'agent_pay'. None for non-shell feeds."""
    if not feed or not feed.endswith(_FEED_SUFFIX):
        return None
    return feed[: -len(_FEED_SUFFIX)].replace("-", "_")


def parse_failing_lanes(note: str) -> tuple[list[str], bool]:
    """(failing lane names, whether the note NAMES its lanes at all).

    ★ The bool is the whole point. `([], True)` means the note named its
    lanes and none failed; `([], False)` means the note could not be read and
    we know nothing. Collapsing those two is how a blind spot reads as
    health — the same error as `no_new_data` asserted without evidence."""
    if not note:
        return [], False
    named = _FAIL_TOKEN.findall(note)
    if named:
        return named, True
    m = _REDS_LIST.search(note)
    if m:
        body = m.group(1).strip()
        if body.lower() in ("none", "-", ""):
            return [], True
        return [x.strip() for x in body.split(",") if x.strip()], True
    # The note may legitimately name lanes with none failing (all PASS).
    if "=PASS" in note or "=pass" in note:
        return [], True
    return [], False


def triage_feed(feed: str, note: str) -> dict:
    """Classify one board feed's failing lanes. Never raises.

    `lanes_named` false means the shell counts its failures without naming
    them — reported, never silently treated as zero."""
    shell = feed_to_shell(feed)
    lanes, named = parse_failing_lanes(note or "")
    out = {
        "shell": shell,
        "lanes_named": bool(named and shell),
        "failing_lanes": [],
        "code_actionable_count": 0,
        "not_code_count": 0,
        "unclassified_count": 0,
    }
    if shell in UNCLASSIFIED_SHELLS:
        # ★ Empty `failing_lanes` here means NOT TRIAGED, not "nothing
        # failed" — audit_closure names its lanes perfectly well, we simply
        # decline to class them twice. Say so in a field, not only in prose.
        out["triage_skipped"] = True
        out["note"] = UNCLASSIFIED_SHELLS[shell]
        return out
    if not out["lanes_named"]:
        out["note"] = ("this shell reports how MANY lanes failed but not "
                       "WHICH — nothing can triage it from the board")
        return out
    for lane in lanes:
        hit = classify(shell, lane)
        if hit is None:
            out["failing_lanes"].append(
                {"lane": lane, "class": None, "code_actionable": None})
            out["unclassified_count"] += 1
            continue
        klass, why = hit
        actionable = klass in CODE_ACTIONABLE
        out["failing_lanes"].append({"lane": lane, "class": klass,
                                     "code_actionable": actionable,
                                     "why": why})
        if actionable:
            out["code_actionable_count"] += 1
        else:
            out["not_code_count"] += 1
    return out
