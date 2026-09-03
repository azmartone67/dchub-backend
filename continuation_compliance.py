"""continuation_compliance.py — did the AGENT act on the continuation?

THE MISSING ROW. The instrumentation table for this funnel has eight rows and
seven were already answerable: what was requested, which capability was gated,
which agent, whether the answer was partly delivered, whether a human clicked,
whether the token redeemed. The eighth — "did the agent surface the
continuation at all?" — was called unmeasurable server-side, and the whole
diagnosis has been stuck behind it: a link that is never opened cannot tell you
whether the human ignored it or never saw it.

IT IS MEASURABLE, because the continuation instruction is itself a TOOL CALL.
Every gated response names `next_tool` — `unlock_more_data` on the paid path,
`claim_free_key` on the anonymous one, `bind_email` on the high-intent one. An
agent that preserved and acted on the instruction calls that tool, in the same
session, after the gate. Both sides carry `session_id` (mcp_upgrade_signals
writes it; mcp_calls_identity exposes it), so the join needs no new telemetry
and no human in the loop.

That reframes the question from "did a human open a link" (n=1 over 5,704, and
therefore useless) to "did the agent do the thing we asked", which happens
thousands of times a week and is fully observed.

★ IT SPLITS BY ARM. `message_shown` carries the randomized arm (`:treatment` /
`:control` / `:ineligible`) since r-arms, so the same query answers the copy experiment: does a line that
names what was withheld get acted on more often than one that does not? If the
two rates are indistinguishable, the wording is not the variable and nobody
should spend another cycle on copy.

WHAT THIS REFUSES TO DO. A rate over a zero denominator is not 0%, it is
UNMEASURED, and the difference matters more here than anywhere: this feature
deployed today, so "no tagged signals yet" and "agents ignore us" produce the
same 0 and mean opposite things. Every summary below carries `state` —
MEASURED or UNMEASURED — and a rate is present only in the first case.

Pure and import-free ON PURPOSE: tests/ never imports Flask or the DB, and
flask_mcp_endpoints pulls psycopg2 at import, so logic left in the route would
be untested by default. Same lesson as relay_specificity.py.
"""

# The tools a gated response actually instructs the agent to call next. Kept
# here as the single list the query and the summary agree on; adding a
# `next_tool` to the server without adding it here silently under-counts
# compliance, which reads as agents ignoring us.
CONTINUATION_TOOLS = ("unlock_more_data", "claim_free_key", "bind_email")

# The randomized arms. `treatment` carries the quantified line; `control` is an
# eligible response that deliberately got the generic one; `ineligible` is a gate
# that measured nothing and could not have been quantified either way.
_ARMS = ("treatment", "control", "ineligible")

# ★ The PRE-RANDOMIZATION labels. The first cut assigned `quantified`/`generic`
# by PAYLOAD SHAPE, not at random, so those rows are a different experiment —
# array-returning tools in one arm, scalar-returning in the other. They are
# bucketed here and EXCLUDED from the comparison. Pooling them into the arms
# whose names they resemble is exactly the contamination the rename avoided;
# doing it in the reader instead of the writer would be no better.
_LEGACY_ARMS = ("quantified", "generic")


def parse_arm(message_shown):
    """'trial_preview:quantified' -> 'quantified'. Anything else -> 'untagged'.

    'untagged' is its own bucket rather than being folded into 'generic': rows
    written before the tagging deployed carry no arm at all, and counting them
    as the control would silently inflate whichever way it went.
    """
    if not isinstance(message_shown, str):
        return "untagged"
    _, sep, tail = message_shown.partition(":")
    tail = tail.strip().lower()
    if not sep:
        return "untagged"
    if tail in _ARMS:
        return tail
    if tail in _LEGACY_ARMS:
        return "legacy_shape_assigned"
    return "untagged"


def summarize_compliance(rows):
    """rows: iterable of (message_shown, gated_sessions, acted_sessions).

    Returns {arms: {arm: {...}}, totals: {...}} where every entry carries a
    `state`. A rate appears only when the denominator is non-zero.
    """
    buckets = {}
    for row in rows or ():
        try:
            msg, gated, acted = row[0], int(row[1] or 0), int(row[2] or 0)
        except (TypeError, ValueError, IndexError):
            continue
        if gated < 0 or acted < 0:
            continue
        acted = min(acted, gated)          # a subset can never exceed its set
        b = buckets.setdefault(parse_arm(msg), {"gated_sessions": 0, "acted_sessions": 0})
        b["gated_sessions"] += gated
        b["acted_sessions"] += acted

    arms = {}
    for arm in _ARMS + ("legacy_shape_assigned", "untagged"):
        b = buckets.get(arm, {"gated_sessions": 0, "acted_sessions": 0})
        arms[arm] = _finish(b)

    total = {"gated_sessions": sum(b["gated_sessions"] for b in buckets.values()),
             "acted_sessions": sum(b["acted_sessions"] for b in buckets.values())}
    out = {"arms": arms, "totals": _finish(total),
           "continuation_tools": list(CONTINUATION_TOOLS)}
    out["comparison"] = _compare(arms["treatment"], arms["control"])
    return out


def _finish(b):
    gated = b["gated_sessions"]
    if gated <= 0:
        return {"gated_sessions": 0, "acted_sessions": 0, "state": "UNMEASURED",
                "why": "no gated sessions in this window — a rate over zero is "
                       "not 0%, and this feature is new enough that an empty "
                       "bucket most likely means not-yet rather than never"}
    return {"gated_sessions": gated, "acted_sessions": b["acted_sessions"],
            "acted_rate": round(b["acted_sessions"] / gated, 4),
            "state": "MEASURED"}


def _compare(q, g):
    """The experiment's verdict, or an honest refusal to give one.

    Compares ONLY the randomized arms. `ineligible` and `legacy_shape_assigned`
    are excluded by construction: the first could not have been quantified, and
    the second was assigned by payload shape rather than at random.
    """
    if q["state"] != "MEASURED" or g["state"] != "MEASURED":
        return {"state": "UNMEASURED",
                "why": "both randomized arms need a non-zero denominator before "
                       "they can be compared"}
    return {"state": "MEASURED",
            "treatment_rate": q["acted_rate"],
            "control_rate": g["acted_rate"],
            "difference": round(q["acted_rate"] - g["acted_rate"], 4),
            "note": "A difference near zero says the WORDING is not the "
                    "variable — stop optimizing copy and question whether the "
                    "client surfaces any trailing content at all. This is a "
                    "raw difference, not a significance test; read it with the "
                    "denominators beside it."}
