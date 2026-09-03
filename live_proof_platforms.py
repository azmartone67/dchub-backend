"""live_proof_platforms.py — who counts as a platform that calls our tools.

THE DEFECT THIS EXISTS FOR. /api/v1/stats/live-proof publishes platforms_30d,
the source of the homepage line naming which platforms call MCP tools and how
often. Until 2026-09-03 it counted raw mcp_tool_calls with no is_real_external
filter and no self-traffic exclusion, while /api/ai/tracking counted the same
30 days with both. Claude read 1,071 in the headline against 492 on its card.

The gap was never a double-count — both sides collapse vendor aliases. It was
OUR OWN traffic: the operator's agent client writes mcp_client 'claude' /
user_agent 'node', byte-identical to a prospect's. The funnel on the same page
already excludes it under DEFINITION v4. The headline did not.

★ THE RULE THAT NEEDED A HOME. A platform whose every call was ours is not an
integrating platform, and naming it is the whole defect in miniature. Dropping
it is a judgement, and a judgement inline in a route is a judgement nobody can
test — which is exactly how it survived: a mutation that kept those rows in the
headline passed every source-text guard written against the endpoint.

Removing a number from a headline is not the same as hiding it. Every row keeps
its gross sibling, and `excluded` names what went and why, so "no exclusion
applied" is never confusable with "applied, found nothing".

Pure and import-free ON PURPOSE: tests/ never imports Flask or the DB, and
flask_mcp_endpoints pulls psycopg2 at import. Same lesson as
relay_specificity.py and continuation_compliance.py.
"""


def shape_platforms(recognized_rows, self_traffic_prefixes=()):
    """rows of (platform, calls, calls_including_self_traffic) -> (list, excluded).

    `recognized_rows` has already passed the endpoint's allowlist — this module
    decides only what the FILTERED counts mean, never who is a recognized
    vendor. Rows are assumed to come from ONE query, so `calls` and its gross
    sibling can never be paired from different lineages.

    Returns (platforms_30d, excluded):
      platforms_30d  [{platform, calls, calls_including_self_traffic}] — calls
                     descending, then platform, so the order is deterministic
                     and two requests a second apart cannot reorder ties.
      excluded       {self_traffic_session_prefixes, calls_removed,
                      platforms_removed_entirely, basis}
    """
    clean, dropped, removed_calls = [], [], 0

    for row in recognized_rows or ():
        try:
            platform = row[0]
            calls = int(row[1] or 0)
            gross = int(row[2] or 0)
        except (TypeError, ValueError, IndexError):
            # A malformed row is a wiring fault, not a platform. It is not
            # guessed at and not silently counted as zero real calls, which
            # would publish it as "removed entirely" — a claim about traffic
            # we never actually observed.
            continue
        if not isinstance(platform, str) or not platform.strip():
            continue
        platform = platform.strip().lower()
        calls = max(calls, 0)
        # The filtered count is a SUBSET of the gross one. A gross below it can
        # only mean the two were paired from different queries; trust neither.
        gross = max(gross, calls)
        removed_calls += gross - calls
        if calls > 0:
            clean.append({"platform": platform, "calls": calls,
                          "calls_including_self_traffic": gross})
        elif gross > 0:
            dropped.append(platform)

    clean.sort(key=lambda d: (-d["calls"], d["platform"]))
    dropped.sort()
    return clean, {
        "self_traffic_session_prefixes": list(self_traffic_prefixes or ()),
        "calls_removed": removed_calls,
        "platforms_removed_entirely": dropped,
        "basis": (
            "mcp_calls_identity WHERE is_public_ip AND is_real_external, minus "
            "DECLARED operator self-traffic sessions "
            "(mcp_calls_deloop.self_traffic_session_prefixes). Declared, never "
            "inferred: the operator's client is byte-identical to a "
            "prospect's, and inventing a behavioural rule would delete real "
            "leads. calls_including_self_traffic is that same real-external "
            "population with our sessions added back — not an unfiltered "
            "total. A platform whose every call was ours is dropped from "
            "platforms_30d and named in platforms_removed_entirely: it is not "
            "an integrating platform, and naming it as one was the defect."),
    }
