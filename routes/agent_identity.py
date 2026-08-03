"""agent_identity.py — ONE resolved identity, one answer (#49 lane 4, 2026-08-02).

`partner → key → agent → call → outcome` is an entity graph that exists only as
separate counts, each surface re-deriving "how many agents" from raw call rows
with its own WHERE clause. The result is measured and repeated: the agent portal
says 62, reach says 99, funnel says 99, and one payload carries
real_external_7d=2637 next to real_external_calls_7d=8641. Nothing is wrong with
any single number — there is no node to make them the same number.

★NO NEW TABLE, NO NEW CRON. The fix for "every surface computes its own answer"
is not a fifth store that can drift from the other four; it is ONE resolver that
every surface calls. Materialising can come later if the query cost justifies it;
adding a table first would just move the disagreement somewhere harder to see.

★THE RESOLUTION IS A HEURISTIC AND SAYS SO. Every identity carries a `kind` that
states how it was resolved, because the three signals are not equally good:

    keyed      api_key present — strongest. One key is one integration.
    platform   no key, but a declared platform. Medium: several agents behind
               one platform label collapse into one identity.
    anonymous  ip_address only — WEAKEST, and it is wrong in both directions:
               NAT and shared egress collapse distinct agents into one, while a
               dynamic IP splits one agent across many.

A consumer that reports `total` without reporting the kind split is making the
same mistake the three surfaces already make. identity_counts() therefore
returns the split, and the caller cannot get a single number out of it without
having seen the breakdown.

Leaf module: no flask, lazy psycopg2, pure functions where possible.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

KEYED = "keyed"
PLATFORM = "platform"
ANONYMOUS = "anonymous"
IDENTITY_KINDS = (KEYED, PLATFORM, ANONYMOUS)

# Synthetic callers — our own probes and harnesses. Kept in sync with the
# white-glove shell deliberately rather than imported: a refactor there must not
# silently change what the canonical count means.
SYNTHETIC = ("dchub-internal", "dchub-selfheal", "value-harness",
             "fixwave-probe", "reviewer-sim")

# ── behavioural classification ────────────────────────────────────────
# A real integration calls a few tools repeatedly. A crawler calls everything
# once. Measured 2026-08-02: ~75 of 99 "real external agents" hit essentially
# every tool ~50x (median 47, p90 62) — an enumeration signature, not demand.
ENUMERATION_MIN_TOOLS = 30
ENUMERATION_MAX_CALLS_PER_TOOL = 2.0
INTEGRATION_MIN_CALLS_PER_TOOL = 3.0

ENUMERATION = "enumeration"
INTEGRATION = "integration"
SPARSE = "sparse"


def classify(distinct_tools, calls) -> str:
    """How this caller BEHAVES. Pure.

    enumeration  broad and shallow — everything once, nothing twice.
    integration  narrow and deep — a few tools, called repeatedly.
    sparse       too little traffic to say. ★Not a failure state and NOT
                 lumped in with enumeration: a new integration's first day
                 looks sparse, and counting it as a crawler would make the
                 north-star number punish exactly the arrivals it should
                 be celebrating.
    """
    try:
        t = int(distinct_tools or 0)
        c = int(calls or 0)
    except Exception:
        return SPARSE
    if t <= 0 or c <= 0:
        return SPARSE
    per_tool = c / t
    if t >= ENUMERATION_MIN_TOOLS and per_tool < ENUMERATION_MAX_CALLS_PER_TOOL:
        return ENUMERATION
    if per_tool >= INTEGRATION_MIN_CALLS_PER_TOOL:
        return INTEGRATION
    return SPARSE


def identity_key(row) -> tuple:
    """(key, kind) for one call row. Pure.

    Strongest available signal wins. Returns ("", "") when the row carries no
    identifying signal at all — callers must skip those rather than bucket them
    together, because "unidentifiable" is not an identity."""
    if not isinstance(row, dict):
        return "", ""
    api_key = str(row.get("api_key") or "").strip()
    if api_key:
        return f"key:{api_key[:60]}", KEYED
    platform = str(row.get("platform") or "").strip().lower()
    if platform and platform not in SYNTHETIC:
        return f"plat:{platform[:60]}", PLATFORM
    ip = str(row.get("ip_address") or "").strip()
    if ip:
        return f"ip:{ip[:60]}", ANONYMOUS
    return "", ""


def _tool_column(cur) -> str:
    """Which column names the tool. Introspected, never guessed — the tree uses
    `tool` in ~163 places and `tool_name` in one. "" when neither exists, None
    when information_schema itself could not be read."""
    for cand in ("tool", "tool_name"):
        try:
            cur.execute("SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_name = 'mcp_tool_calls' "
                        "AND column_name = %s", (cand,))
            r = cur.fetchone()
        except Exception:
            return None
        if r is None:
            return None
        if int(r[0] or 0) > 0:
            return cand
    return ""


def identity_counts(cur, days: int = 7) -> dict:
    """THE canonical answer. One query, one resolution, one set of numbers.

    Returns {ok, days, resolved_by: {keyed, platform, anonymous}, behaviour:
    {integration, enumeration, sparse}, total, note} — or {ok: False, error}
    when it could not be computed.

    ★NEVER returns a bare `total` without the splits beside it. A surface that
    prints one number without saying how it was resolved is how 62 / 99 / 99
    happened; making the breakdown unavoidable is most of the fix."""
    out = {"ok": False, "days": int(days),
           "resolved_by": {}, "behaviour": {}, "total": 0}
    col = _tool_column(cur)
    if col is None:
        out["error"] = ("information_schema unreadable — UNMEASURED. "
                        "The tool column is introspected, not assumed.")
        return out
    if not col:
        out["error"] = "mcp_tool_calls has neither `tool` nor `tool_name`"
        return out
    synth = "', '".join(SYNTHETIC)
    try:
        cur.execute(f"""
            SELECT COALESCE(api_key, '') AS api_key,
                   COALESCE(platform, '') AS platform,
                   COALESCE(ip_address, '') AS ip_address,
                   COUNT(DISTINCT {col}) AS tools,
                   COUNT(*) AS calls
              FROM mcp_tool_calls
             WHERE created_at >= NOW() - make_interval(days => %s)
               AND COALESCE(platform, '') NOT IN ('{synth}')
             GROUP BY 1, 2, 3
        """, (int(days),))
        rows = cur.fetchall() or []
    except Exception as e:  # noqa: BLE001
        out["error"] = f"query_failed: {str(e)[:160]}"
        return out

    # Fold call-row groups into identities. Two rows that resolve to the same
    # key are ONE agent — which is the entire point, and the reason a bare
    # COUNT(DISTINCT ip_address) has always been a different number.
    agents = {}
    for r in rows:
        row = {"api_key": r[0], "platform": r[1], "ip_address": r[2]}
        key, kind = identity_key(row)
        if not key:
            continue
        a = agents.setdefault(key, {"kind": kind, "tools": 0, "calls": 0})
        # Distinct-tool counts from different rows overlap, so summing would
        # over-count breadth and mislabel an integration as an enumerator.
        # MAX is the honest floor from this projection.
        a["tools"] = max(a["tools"], int(r[3] or 0))
        a["calls"] += int(r[4] or 0)

    by_kind = {k: 0 for k in IDENTITY_KINDS}
    by_behaviour = {INTEGRATION: 0, ENUMERATION: 0, SPARSE: 0}
    for a in agents.values():
        by_kind[a["kind"]] = by_kind.get(a["kind"], 0) + 1
        by_behaviour[classify(a["tools"], a["calls"])] += 1

    out["ok"] = True
    out["resolved_by"] = by_kind
    out["behaviour"] = by_behaviour
    out["total"] = len(agents)
    out["note"] = (
        f"{len(agents)} resolved identities in {days}d. `total` is only "
        f"meaningful next to resolved_by: {by_kind[KEYED]} keyed (strong), "
        f"{by_kind[PLATFORM]} platform-only (medium), "
        f"{by_kind[ANONYMOUS]} IP-only (weak — NAT collapses distinct agents, "
        f"dynamic IPs split one). {by_behaviour[ENUMERATION]} match an "
        f"enumeration signature and are NOT demand.")
    return out


def demand_total(counts: dict) -> int:
    """The north-star number: identities that look like real integrations.

    Deliberately EXCLUDES enumeration and includes sparse — a crawler is not a
    user, but a new integration's first day is."""
    if not isinstance(counts, dict) or not counts.get("ok"):
        return 0
    b = counts.get("behaviour") or {}
    return int(b.get(INTEGRATION, 0)) + int(b.get(SPARSE, 0))
