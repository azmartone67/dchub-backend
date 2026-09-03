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
`:control` / `:ineligible`) since r-arms, so the same query answers the copy
experiment: does a line that names what was withheld get acted on more often
than one that does not? If the two rates are indistinguishable, the wording is
not the variable and nobody should spend another cycle on copy.

★ IT SPLITS BY CLIENT — on an identity, not on whatever string arrived. The
gate defaults `mcp_client` to the literal "mcp", the name of the PROTOCOL, so
the naive split would put its largest row under a protocol name and call it the
top caller. parse_client below folds that set into `mcp-generic-client`, using
mcp_calls_identity's own vocabulary so both surfaces name the cohort alike.

★ AND IT HAS TO SPLIT AT ALL. Measured 2026-09-03 on the 7-day window:
45 distinct external agents called MCP tools and ONE of them accounted for
65.8% of the calls. A single pooled compliance rate over that population is not
"what agents do" — it is what the top caller does, wearing the plural. The
by_client and concentration blocks exist so it can never be read the other way
round. Same defect canonical_top_caller_sql was written for in August, one
funnel over.

★ IT SEPARATES "REFUSED" FROM "HAD NO TURN", which is the difference between a
finding and a libel. An agent can only act on the continuation if its session
survives the gate. Some clients arrive through a hosted connector gateway that
mints a NEW server-side session per call — Grok's own account of xAI's
connector, 2026-09-03 — and such a session cannot, even in principle, contain a
post-gate call. Pooled in, it contributes a structural zero that reads exactly
like an agent ignoring us. So every bucket also carries `continued_sessions`:
sessions with ANY real external call after the gate. A bucket where nothing
continued is UNMEASURED, not 0% — there was no turn in which to comply.

WHAT THIS REFUSES TO DO. A rate over a zero denominator is not 0%, it is
UNMEASURED, and the difference matters more here than anywhere: this feature
deployed today, so "no tagged signals yet" and "agents ignore us" produce the
same 0 and mean opposite things. Every summary below carries `state` —
MEASURED or UNMEASURED — and a rate is present only in the first case.

Pure and import-free ON PURPOSE: tests/ never imports Flask or the DB, and
flask_mcp_endpoints pulls psycopg2 at import, so logic left in the route would
be untested by default. Same lesson as relay_specificity.py.
"""
import re

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

# A gate whose row carries no client string at all. Its own bucket, never
# folded into a named client and never dropped: an unattributed session still
# met a gate, and hiding it would shrink the denominator every rate is taken
# over.
UNATTRIBUTED = "unattributed"

# ★ "mcp" is the name of the PROTOCOL, not of a client — and the gate DEFAULTS
# to it: `body.get('mcp_client') or body.get('platform') or 'mcp'`. So a caller
# that declares nothing is written down as "mcp", and without this the largest
# row in by_client would be a protocol name wearing a client's clothes, with
# `concentration` naming it as the top caller.
#
# mcp_calls_identity learned this on 2026-07-28: trusting the string as an
# identity is how 87% of call volume ended up in a bucket described as
# "unattributed". It was never unattributed — it was one generic string being
# read as a name. That view's fix gives these rows their own REAL bucket, and
# the vocabulary below is deliberately identical to the view's so the two
# surfaces name the same cohort the same way. A bare UUID is refused for the
# same reason the view refuses it: a session id is not a client name.
_GENERIC_CLIENT_NAMES = ("mcp", "mcp-client", "client", "default")
GENERIC_CLIENT = "mcp-generic-client"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Concentration at or above this share of gated sessions means the pooled rate
# is tracking one caller. Not a significance threshold — a reading instruction,
# published beside the number so nobody has to guess where "dominated" starts.
_CONCENTRATION_FLOOR = 0.50

# `mcp_client` is caller-supplied text and one vendor may call under several
# ids, so its cardinality is bounded by nothing we control. by_client lists the
# largest this many and FOLDS the tail into one row — folded, never dropped, so
# the client view still reconciles with `totals`.
_CLIENT_ROWS_MAX = 25
OTHER_CLIENTS = "_other_clients"


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


def parse_client(mcp_client):
    """Normalize the client label into a bucket that is actually an identity.

    Lowercased and trimmed so 'Claude', 'claude ' and 'claude' are one row
    rather than three — a client split across casing variants would understate
    its own concentration, which is the one number this block exists to state.

    Three outcomes, and they mean different things:
      - a real name          -> that name
      - a generic/protocol   -> GENERIC_CLIENT, a real cohort of unnamed
        string or a UUID        clients, NOT an absence (see the note above)
      - nothing at all       -> UNATTRIBUTED
    """
    if not isinstance(mcp_client, str):
        return UNATTRIBUTED
    name = mcp_client.strip().lower()
    if not name:
        return UNATTRIBUTED
    if name in _GENERIC_CLIENT_NAMES or _UUID_RE.match(name):
        return GENERIC_CLIENT
    return name


def summarize_compliance(rows):
    """rows: iterable of
       (message_shown, mcp_client, gated_sessions, continued_sessions,
        acted_sessions)

    Returns {arms, by_client, concentration, totals, comparison}, where every
    entry carries a `state`. A rate appears only when the denominator is
    non-zero AND at least one session survived the gate.

    ★ The row shape is FIVE fields. A shorter row is a wiring mistake, not a
    variant to absorb: there is no honest default for `continued` — using
    `gated` invents opportunity that may not have existed, using `acted`
    invents the opposite. Malformed rows are counted in `dropped_rows` rather
    than silently skipped, because a query that quietly stopped matching would
    otherwise look exactly like agents that quietly stopped complying.
    """
    arm_buckets = {}
    client_buckets = {}
    dropped = 0

    for row in rows or ():
        try:
            msg, client = row[0], row[1]
            gated = int(row[2] or 0)
            continued = int(row[3] or 0)
            acted = int(row[4] or 0)
        except (TypeError, ValueError, IndexError):
            dropped += 1
            continue
        if gated < 0 or continued < 0 or acted < 0:
            dropped += 1
            continue
        # Subsets can never exceed their supersets: acted within continued
        # within gated.
        continued = min(continued, gated)
        acted = min(acted, continued)
        _add(arm_buckets, parse_arm(msg), gated, continued, acted)
        _add(client_buckets, parse_client(client), gated, continued, acted)

    arms = {arm: _finish(arm_buckets.get(arm))
            for arm in _ARMS + ("legacy_shape_assigned", "untagged")}
    by_client, folded = _bounded_clients(client_buckets)

    total = _empty()
    for b in arm_buckets.values():
        for k in total:
            total[k] += b[k]

    out = {
        "arms": arms,
        "by_client": by_client,
        "by_client_rows": {
            "listed": len(by_client) - (1 if folded else 0),
            "folded_into_other": folded,
            "cap": _CLIENT_ROWS_MAX,
            "other_key": OTHER_CLIENTS,
            "why": "mcp_client is caller-supplied and unbounded; the tail is "
                   "folded into one row rather than dropped, so by_client "
                   "still sums to totals",
        },
        # Concentration is computed on the FULL client set, before folding —
        # truncating first could change which client is on top.
        "concentration": _concentration(client_buckets, total),
        "totals": _finish(total if total["gated_sessions"] else None),
        "continuation_tools": list(CONTINUATION_TOOLS),
        "dropped_rows": dropped,
    }
    out["comparison"] = _compare(arms["treatment"], arms["control"],
                                 out["concentration"])
    return out


def _bounded_clients(client_buckets):
    """The largest _CLIENT_ROWS_MAX clients, with the tail folded into one row.

    An unbounded by_client turns a diagnostic into a response that can grow
    without limit on caller-supplied text. Folding — not dropping — keeps the
    sums reconciling with `totals`, which is the property that makes the two
    views checkable against each other.
    """
    ordered = sorted(client_buckets.items(),
                     key=lambda kv: (-kv[1]["gated_sessions"], kv[0]))
    head, tail = ordered[:_CLIENT_ROWS_MAX], ordered[_CLIENT_ROWS_MAX:]
    out = {name: _finish(b) for name, b in head}
    if tail:
        rest = _empty()
        for _, b in tail:
            for k in rest:
                rest[k] += b[k]
        folded = out.get(OTHER_CLIENTS)          # a caller literally named this
        if folded:                               # merges in rather than vanishes
            for k in rest:
                rest[k] += folded.get(k, 0)
        out[OTHER_CLIENTS] = _finish(rest)
    return out, len(tail)


def _empty():
    return {"gated_sessions": 0, "continued_sessions": 0, "acted_sessions": 0}


def _add(buckets, key, gated, continued, acted):
    b = buckets.setdefault(key, _empty())
    b["gated_sessions"] += gated
    b["continued_sessions"] += continued
    b["acted_sessions"] += acted


def _finish(b):
    """One bucket -> a published summary, with the two UNMEASURED cases named.

    They are different failures and the `why` says which:
      - nothing was gated        -> this population was never observed at all
      - gated, nothing continued -> observed, and it had no chance to act
    Both are UNMEASURED. Neither is 0%.
    """
    if not b or b["gated_sessions"] <= 0:
        return {**_empty(), "state": "UNMEASURED",
                "why": "no gated sessions in this window — a rate over zero is "
                       "not 0%, and this feature is new enough that an empty "
                       "bucket most likely means not-yet rather than never"}
    gated = b["gated_sessions"]
    continued = b["continued_sessions"]
    acted = b["acted_sessions"]
    if continued <= 0:
        return {"gated_sessions": gated, "continued_sessions": 0,
                "acted_sessions": 0, "state": "UNMEASURED",
                "why": f"{gated} sessions met a gate and NONE made any further "
                       "call — the session ended at the gate, so there was no "
                       "turn in which to comply. Unobservable, not refusal: a "
                       "client whose sessions are one call long (a hosted "
                       "connector minting a session per call) lands here"}
    return {"gated_sessions": gated,
            "continued_sessions": continued,
            "acted_sessions": acted,
            "acted_rate": round(acted / gated, 4),
            "acted_rate_of_continued": round(acted / continued, 4),
            "state": "MEASURED",
            "read": "acted_rate is over ALL gated sessions; "
                    "acted_rate_of_continued is over the sessions that had a "
                    "turn after the gate. Where they diverge, the gap is dead "
                    "sessions, not refusal"}


def _concentration(client_buckets, total):
    """Is the pooled rate describing agents, or describing one caller?

    ★ THE UNIT IS SESSIONS, NOT CALLS. /api/v1/ai/reach publishes a caller
    concentration measured in TOOL CALLS over mcp_calls_identity. This one is
    gated SESSIONS over mcp_upgrade_signals. They will not agree and must never
    be quoted against each other — that cross-basis division is precisely the
    contradiction canonical_top_caller_sql was written to end. The unit is
    named in the payload so the mistake takes an act of will.
    """
    gated_total = total["gated_sessions"]
    if not client_buckets or gated_total <= 0:
        return {"state": "UNMEASURED", "unit": "gated_sessions",
                "distinct_clients": len(client_buckets),
                "why": "no gated sessions to concentrate"}

    top, top_b = max(client_buckets.items(),
                     key=lambda kv: (kv[1]["gated_sessions"], kv[0]))
    top_gated = top_b["gated_sessions"]
    share = round(top_gated / gated_total, 4)

    rest = _empty()
    for name, b in client_buckets.items():
        if name == top:
            continue
        for k in rest:
            rest[k] += b[k]

    return {
        "state": "MEASURED",
        "unit": "gated_sessions",
        "distinct_clients": len(client_buckets),
        "top_client": top,
        "top_client_gated_sessions": top_gated,
        "top_client_share": share,
        "dominated": share >= _CONCENTRATION_FLOOR,
        "net_of_top_client": _finish(rest if rest["gated_sessions"] else None),
        "why": "numerator and denominator both come from THIS query, so they "
               "cannot be paired with a figure from another lineage. The unit "
               "is gated SESSIONS — /api/v1/ai/reach concentrates TOOL CALLS "
               "on a different table; the two are not comparable and never sum",
    }


def _compare(t, c, concentration=None):
    """The experiment's verdict, or an honest refusal to give one.

    Compares ONLY the randomized arms. `ineligible` and `legacy_shape_assigned`
    are excluded by construction: the first could not have been quantified, and
    the second was assigned by payload shape rather than at random.
    """
    if t["state"] != "MEASURED" or c["state"] != "MEASURED":
        return {"state": "UNMEASURED",
                "why": "both randomized arms need a non-zero denominator, and "
                       "at least one session per arm that continued past the "
                       "gate, before they can be compared"}
    out = {"state": "MEASURED",
           "treatment_rate": t["acted_rate"],
           "control_rate": c["acted_rate"],
           "difference": round(t["acted_rate"] - c["acted_rate"], 4),
           "note": "A difference near zero says the WORDING is not the "
                   "variable — stop optimizing copy and question whether the "
                   "client surfaces any trailing content at all. This is a "
                   "raw difference, not a significance test; read it with the "
                   "denominators beside it."}
    # Assignment is per session and salted, so concentration does NOT break the
    # comparison internally — within the dominant caller the split is still
    # random. What it changes is who the answer is ABOUT. Say that precisely,
    # rather than implying the estimate is invalid.
    if concentration and concentration.get("dominated"):
        out["generalizes_to"] = (
            "{} — {:.1%} of gated sessions are that one client. Randomization "
            "holds inside it, so this difference is a valid estimate OF THAT "
            "CALLER and not of agents in general. Read by_client before "
            "generalizing.".format(concentration["top_client"],
                                   concentration["top_client_share"]))
    return out
