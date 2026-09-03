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
    ("agent_pay", "pricing"): ("judgment",
        "★ RECLASSIFIED 2026-09-03 from `build`, whose reason was factually "
        "wrong. Nothing mints a $0.10 FIAT charge — mpp-hook.mjs:95-103 "
        "prices every covered tool, flagships included, at $0.50; the $0.10 "
        "is the x402/USDC rail, which has no fiat floor. The build-shaped "
        "check `pr_floor` (advertised price clears the $0.50 SPT floor, "
        "critical) therefore PASSES and structurally cannot fail. The only "
        "reachable red is `pr_flagship` (0.50 <= 0.10), and the lane's own "
        "docstring calls it 'an open commercial decision, not a bug; "
        "acknowledge it with MPP_FLAGSHIP_PREMIUM_ACK=1'. Its test is named "
        "test_pricing_lane_passes_only_when_the_premium_is_acknowledged — the "
        "suite's own definition of green is that a human decided"),
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
        "★RE-MEASURED 2026-09-03 01:57Z — the reason below replaces 'counts "
        "bare-{} internal fetchers still to migrate in routes/', which is "
        "DONE: that check now reads '15 migrated, none remaining' and PASSES. "
        "The lane fails on a different check — 'every L14 context probe "
        "answered' (critical), 1/11 unmeasurable: expansion ReadTimeout "
        "against 127.0.0.1:8080 at an 8s read timeout. A loopback probe that "
        "cannot answer inside 8s is the defect, not the migration"),
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
    ("loop_flywheel", "infra"): ("instrument",
        "★ RECLASSIFIED 2026-09-03 from `owner-flag`. Both owner-shaped "
        "checks are already GREEN in prod (NEON_REPLICA_URL set, "
        "DCHUB_ROLE=web). The only check that can go red is a HARDCODED "
        "COUNTDOWN — `_NEON_MIGRATION_DUE = date(2026,10,5)` at "
        "loop_flywheel_master_shell.py:66 — which observes nothing about the "
        "system. The migration it counts down to COMPLETED 2026-07-13 (Azure "
        "deleted 08-05; the live DB is AWS Oregon), and the constant was "
        "added 11 days AFTER that cutover. It goes FAIL on 2026-09-13 and "
        "OVERDUE from 10-06, permanently, and no owner action clears it — "
        "only an engineer deleting the countdown or asserting the live host. "
        "★ That deletion is real pending work, not just a reclassification"),
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
    ("loop_flywheel", "cron"): ("build",
        "★RE-MEASURED 2026-09-03 01:45Z — the reason below replaces 'asserts "
        "dead-man board clear ... circular by construction', which D2 "
        "(2026-09-02) had ALREADY fixed the same day this table was written: "
        "that check now fails on LATE only, reads '202 feeds, 0 overdue' and "
        "PASSES. The lane fails on its OTHER check, cron_dupes — the WAVE 4 "
        "work order to retire ~314 overlapping scheduled jobs. That is an "
        "engineer's job, not a broken measurement, hence build not "
        "instrument. ★AND cron_dupes is a hardcoded False, not a "
        "measurement: it takes no reading, so it will stay red after the "
        "duplicates are retired and can never record its own completion. "
        "Bonded by tests/test_triage_reasons_match_the_failing_check.py"),

    # ── relay_closure ────────────────────────────────────────────────
    ("relay_closure", "A/redeem_declared_vs_writer"): ("build",
        "declared redeem basis vs writer liveness — a consistency check"),
    ("relay_closure", "B/relay_demand_verdict"): ("judgment",
        "★ RECLASSIFIED 2026-09-03 from `commercial`, whose reason was "
        "inverted with respect to which state is RED. relay_closure_master_"
        "shell.py:676-678 builds `reds` from status in "
        "(FAIL, ESCALATE, NEEDS_ATTRIBUTION); STOP_ENVELOPE_WORK — the "
        "genuinely demand-shaped verdict — is NOT in that set and never "
        "reddens the board. The states that DO redden it are ones asking a "
        "human to decide what the reading means"),
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

_FAIL_TOKEN = _re.compile(r"([A-Za-z0-9_/]+)=FAIL")
# Any verdict token at all means the note NAMED its lanes — including a
# note whose lanes are every one unknown. Detecting "named" from =PASS
# alone read an all-`?` note as unreadable; caught by the guard.
_VERDICT_TOKEN = _re.compile(r"[A-Za-z0-9_/]+=(?:PASS|FAIL|\?)")
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
    # The note may legitimately name lanes with none FAILing — all PASS, all
    # unknown, or a mix. Any verdict token proves the lanes were named.
    if _VERDICT_TOKEN.search(note):
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
        # ★ A feed that is not a master shell HAS no lanes, so "its lanes are
        # unnamed" is not a fact about it. Measured live 2026-09-03: the first
        # deploy reported red_lanes_unnamed=4 where 3 was correct, because
        # `iso-intl` — an ISO data feed, not a shell — was folded in beside
        # the three shells that really do hide their lane names. An inflated
        # blind-spot count is the same defect as a deflated one: the number
        # stops meaning what it says.
        "not_a_shell": shell is None,
        "failing_lanes": [],
        "code_actionable_count": 0,
        "not_code_count": 0,
        "unclassified_count": 0,
    }
    if shell is None:
        out["note"] = ("not a master shell — this feed has no lanes, so it is "
                       "neither triaged nor counted as an unnamed-lane gap")
        return out
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


def format_lane_verdicts(pairs) -> str:
    """The canonical `lanes: a=PASS b=FAIL` string the board can parse back.

    ★ Lives BESIDE parse_failing_lanes on purpose. Three shells
    (agentic_loop, growth_funnel, webmcp) each invented their own note format
    — "PASS 2 FAIL 2", "3 failing / 1 unknown of 4 lanes", "lanes 2/4 pass" —
    all of which COUNT failures without NAMING them, so nothing could triage
    them from the board. A writer that lives next to its reader cannot drift
    from it; a fourth format invented in a fourth file would.

    `pairs` is (lane_id, verdict); verdict is normalised to PASS / FAIL / ?
    so an unmeasured lane is never written as a failure.

    ★ CONTRACT: this applies NO length bound. An earlier version of this docstring
    claimed one "to stay inside record_beat's 280-char note cap". Both halves
    were false: record_beat truncates nothing — the 280-char cap lives in the
    HTTP beat() handler (ingest_runs.py:163), which the in-process shells do
    not go through. Real notes today are 58-75 chars so nothing truncates, but
    a shell growing past ~30 lanes would need a bound ADDED here, not assumed.
    Guarded by test_the_formatter_applies_no_bound_and_does_not_claim_one."""
    out = []
    for lane, verdict in pairs or ():
        v = str(verdict or "?").upper()
        if v not in ("PASS", "FAIL"):
            v = "?"
        out.append(f"{lane}={v}")
    return ("lanes: " + " ".join(out)) if out else ""


def rollup_triage(triage_dicts):
    """Aggregate per-feed triage blocks into the board's `red_triage`.

    ★ EXTRACTED 2026-09-03. This arithmetic used to live inline in
    deadman(), which needs a live DB, so the only thing any test could reach
    was a substring of the handler's source. An adversarial review showed the
    consequence: swapping `code_actionable` and `not_code` in the rollup
    passed the entire suite. A sum nothing can execute is a sum nothing
    guards."""
    tri = [t for t in (triage_dicts or ()) if isinstance(t, dict)]
    return {
        "code_actionable": sum(t.get("code_actionable_count", 0) for t in tri),
        "not_code": sum(t.get("not_code_count", 0) for t in tri),
        "unclassified": sum(t.get("unclassified_count", 0) for t in tri),
        # Only MASTER SHELLS have lanes. A non-shell feed (iso-intl and
        # friends) is not a hidden-lane gap and must not inflate the count.
        "red_lanes_unnamed": sum(1 for t in tri
                                 if not t.get("lanes_named")
                                 and not t.get("triage_skipped")
                                 and not t.get("not_a_shell")),
        "basis": ("failing lanes parsed from each red shell's own beat note "
                  "and classified by routes/lane_triage.LANE_TRIAGE, which "
                  "keys on WHO CLOSES IT. code_actionable = build|instrument "
                  "(an engineer). not_code = commercial|owner-flag|judgment|"
                  "diagnose — correct reds that no PR clears. "
                  "red_lanes_unnamed = shells whose note counts failures "
                  "without naming them; that is a blind spot, NOT a zero. "
                  "Non-shell feeds are excluded from that count."),
    }


# Lanes whose board-facing id does not derive from their function name.
# relay_closure emits display names ("B/relay_demand_verdict") while its
# functions are _lane_b_demand etc. Spelled out rather than guessed, so the
# anti-rot guard can resolve a real callable instead of matching source text.
LANE_FN_ALIASES: dict[tuple[str, str], str] = {
    ("relay_closure", "A/redeem_declared_vs_writer"): "_lane_a_redeem",
    ("relay_closure", "B/relay_demand_verdict"): "_lane_b_demand",
    ("relay_closure", "C/mint_attributability"): "_lane_c_attributability",
    ("relay_closure", "D/typed_params_window"): "_lane_d_typed_params",
    ("relay_closure", "E/schema_selection_asks"): "_lane_e_asks",
}
