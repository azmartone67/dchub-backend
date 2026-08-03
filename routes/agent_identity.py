"""agent_identity.py — ONE resolved identity, one answer (#49 lane 4, 2026-08-02).

`partner → key → agent → call → outcome` is an entity graph that exists only as
separate counts, each surface re-deriving "how many agents" from raw call rows
with its own WHERE clause. The result is measured and repeated: the agent portal
says 62, reach says 99, funnel says 99, and one payload carries
real_external_7d=2637 next to real_external_calls_7d=8641. Nothing is wrong with
any single number — there is no node to make them the same number.

★CORRECTION (2026-08-03). The first version of this module resolved identity
itself from raw mcp_tool_calls rows — and was WRONG to. `mcp_calls_identity`
already exists: a view generated from mcp_calls_deloop by
scripts/render_identity_views.py and refined across six migrations, which hashes
the client IP to `agent_id`, NULLs Cloudflare-POP first hops so a POP can never
be counted as an agent, tags platform from user_agent, and carries
is_public_ip / is_real_external including registry-crawler exclusions.

Adding a second resolver to a problem whose definition is "too many opinions"
is the same mistake as adding a fifth store, wearing different clothes. So this
module now READS the canonical view and does not re-derive identity. What it
adds — and the reason it still exists — is the part the view has no opinion on:

  1. BEHAVIOUR. The view says who an agent is; it does not say whether that
     agent is demand or a crawler enumerating every tool once.
  2. DISCIPLINE. identity_counts() cannot hand back a bare total. The splits
     come with it, because printing one number without saying how it was
     derived is exactly how 62 / 99 / 99 happened.

★WHY THE SURFACES DISAGREE, now that the view has been read: flywheel and
monetization already count DISTINCT agent_id from this view. /api/v1/ai/reach
counts raw DISTINCT ip_address, which counts Cloudflare POPs and internal
traffic as agents. That is the gap — not two defensible methods, one correct
grain and one that predates it.

★THE VIEW HAS NO api_key COLUMN, so there is no "keyed" tier here. The kind
split is what the view can actually support: platform-tagged (a recognised
client) vs untagged. Inventing a strength tier the data cannot back would be
the same confident guess this whole effort exists to remove.

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


CANONICAL_VIEW = "mcp_calls_identity"


def _view_column(cur, column: str):
    """Introspected, never guessed. None when information_schema itself could
    not be read — 'the column is missing' and 'I could not look' are different
    answers and only one of them is a finding."""
    try:
        cur.execute("SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_name = %s AND column_name = %s",
                    (CANONICAL_VIEW, column))
        r = cur.fetchone()
    except Exception:
        return None
    if r is None:
        return None
    return int(r[0] or 0) > 0


def identity_counts(cur, days: int = 7) -> dict:
    """THE canonical answer. One query, one resolution, one set of numbers.

    Returns {ok, days, resolved_by: {keyed, platform, anonymous}, behaviour:
    {integration, enumeration, sparse}, total, note} — or {ok: False, error}
    when it could not be computed.

    ★NEVER returns a bare `total` without the splits beside it. A surface that
    prints one number without saying how it was resolved is how 62 / 99 / 99
    happened; making the breakdown unavoidable is most of the fix."""
    out = {"ok": False, "days": int(days), "source": CANONICAL_VIEW,
           "resolved_by": {}, "behaviour": {}, "total": 0}
    has = _view_column(cur, "agent_id")
    if has is None:
        out["error"] = ("information_schema unreadable — UNMEASURED, not zero. "
                        "The canonical view is introspected, not assumed.")
        return out
    if not has:
        # ★DO NOT fall back to resolving identity here. A second-best answer
        # that looks like the canonical one is how the surfaces diverged in the
        # first place; saying "the canon is missing" is the useful failure.
        out["error"] = (f"{CANONICAL_VIEW}.agent_id absent — the canonical "
                        f"identity view has not been applied. Run the "
                        f"migrations under migrations/*mcp_calls_identity*; "
                        f"this module deliberately does NOT re-derive "
                        f"identity as a fallback.")
        return out
    try:
        # One row per agent, at the view's grain. is_public_ip + is_real_external
        # are the view's own exclusions (Cloudflare POPs, internal traffic,
        # registry crawlers) — reusing them is the whole point of reading the
        # canon instead of re-deriving it.
        cur.execute("""
            SELECT agent_id,
                   MIN(COALESCE(platform, ''))  AS platform,
                   COUNT(DISTINCT tool_name)    AS tools,
                   COUNT(*)                     AS calls
              FROM mcp_calls_identity
             WHERE created_at >= NOW() - make_interval(days => %s)
               AND agent_id IS NOT NULL AND agent_id <> ''
               AND is_public_ip AND is_real_external
             GROUP BY agent_id
        """, (int(days),))
        rows = cur.fetchall() or []
    except Exception as e:  # noqa: BLE001
        out["error"] = f"query_failed: {str(e)[:160]} — UNMEASURED, not zero"
        return out

    by_kind = {PLATFORM: 0, ANONYMOUS: 0}
    by_behaviour = {INTEGRATION: 0, ENUMERATION: 0, SPARSE: 0}
    for r in rows:
        platform = str(r[1] or "").strip().lower()
        tagged = bool(platform) and platform not in SYNTHETIC
        by_kind[PLATFORM if tagged else ANONYMOUS] += 1
        by_behaviour[classify(r[2], r[3])] += 1

    out["ok"] = True
    out["resolved_by"] = by_kind
    out["behaviour"] = by_behaviour
    out["total"] = len(rows)
    out["note"] = (
        f"{len(rows)} agents in {days}d at the {CANONICAL_VIEW} grain "
        f"(COUNT DISTINCT agent_id, the view's own POP/internal/crawler "
        f"exclusions applied). {by_kind[PLATFORM]} platform-tagged, "
        f"{by_kind[ANONYMOUS]} untagged. {by_behaviour[ENUMERATION]} match an "
        f"enumeration signature and are NOT demand. ★A surface reporting a "
        f"different number is almost certainly counting raw ip_address, which "
        f"counts Cloudflare POPs and internal traffic as agents.")
    return out


def demand_total(counts: dict) -> int:
    """The north-star number: identities that look like real integrations.

    Deliberately EXCLUDES enumeration and includes sparse — a crawler is not a
    user, but a new integration's first day is."""
    if not isinstance(counts, dict) or not counts.get("ok"):
        return 0
    b = counts.get("behaviour") or {}
    return int(b.get(INTEGRATION, 0)) + int(b.get(SPARSE, 0))
