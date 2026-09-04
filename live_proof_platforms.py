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


# ★ The line between "integrated" and "tried it once". TWO active days is the
# weakest claim that still means the platform came BACK — which is the whole
# difference the hero sentence rests on. Deliberately not a call-count
# threshold: chatgpt's 33 calls and grok's 41 both landed inside one day, so
# volume cannot separate them from claude-ai's 742 across ten.
_RECURRING_MIN_DAYS = 2


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
            # ── r-burst-vs-adoption (2026-09-04) ──────────────────────────
            # A 30-day CALL COUNT cannot tell a platform that integrated from
            # one that ran a single test. Measured the day this shipped:
            #
            #   claude-ai  742 calls over 10 active days, last call today
            #   claude     213 calls over 24 active days, last call yesterday
            #   grok        41 calls over  1 active day,  2026-08-30
            #   chatgpt     33 calls over  1 active day,  2026-08-13
            #
            # The hero sentence named all of them identically — "Claude.
            # ChatGPT. Grok. They don't guess … they call DC Hub's MCP tools"
            # — so two single-day bursts carried the same weight as a platform
            # calling every week. And chatgpt's burst was 8 days from rolling
            # out of the window, at which point the sentence would have
            # corrected itself by DELETION, having been wrong the whole time.
            #
            # active_days and last_call are OPTIONAL trailing columns: rows
            # from an older query shape still work and simply carry None,
            # which downstream renders as unknown rather than as "one day".
            active_days = int(row[3]) if len(row) > 3 and row[3] is not None else None
            last_call = row[4] if len(row) > 4 else None
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
            # `recurring` is the claim the hero sentence actually needs, and it
            # is UNKNOWN (None) rather than False when active_days is absent —
            # a missing measurement must not read as "this platform came once".
            recurring = None if active_days is None else active_days >= _RECURRING_MIN_DAYS
            clean.append({"platform": platform, "calls": calls,
                          "calls_including_self_traffic": gross,
                          "active_days": active_days,
                          "last_call": (last_call.isoformat()
                                        if hasattr(last_call, "isoformat")
                                        else (str(last_call) if last_call else None)),
                          "recurring": recurring})
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
            "an integrating platform, and naming it as one was the defect. "
            "Each row also carries active_days, last_call and recurring "
            "(active_days >= %d): a 30-day call COUNT cannot tell a platform "
            "that integrated from one that ran a single test, and naming both "
            "the same way was the second defect. recurring is null, not false, "
            "when active_days was not measured." % _RECURRING_MIN_DAYS),
    }
